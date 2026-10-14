"""最简执行器和仲裁器。

SimpleArbiter：按角色挑单位发租约，优先级高的可以从优先级低的租约里抢单位；
资金按优先级排队预留，用最近十秒的收入估 ETA。

SimpleRunner：驱动技能生命周期，维护击杀和阵亡统计，把事件交给 EventSink。
"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable, Deque, Iterable, Sequence

from .arbiter import Arbiter, FundsStatus, Grant, GrantStatus, Lease, Priority, Requirements, Revocation
from .events import EventKind, EventSink, Severity, SkillEvent
from .skill import TERMINAL, LeaseStats, Skill, SkillContext, SkillResult, SkillStatus
from .types import NamedRegion, UnitId, UnitSelector
from .world import Commander, Death, NotLeased, UnitView, WorldView

CANCEL_GRACE_SECONDS = 30.0
INCOME_WINDOW_FRAMES = 240


# ---------- 事件 ----------


class EventLog(EventSink):
    """攒事件，可选同时打印。反馈层来之前，这就是玩家看到的东西。"""

    def __init__(self, verbose: bool = False, fps: int = 24) -> None:
        self.events: list[SkillEvent] = []
        self.verbose = verbose
        self.fps = fps

    def emit(self, event: SkillEvent) -> None:
        self.events.append(event)
        if self.verbose:
            print(self.format(event))

    def format(self, e: SkillEvent) -> str:
        t = e.frame / self.fps
        return f"[{e.frame:5d} {t:6.1f}s] {e.skill_name}#{e.skill_id:<2} {e.severity.name:<8} {e.kind.value:<9} {e.message}"

    def of(self, skill_id: int) -> list[SkillEvent]:
        return [e for e in self.events if e.skill_id == skill_id]


# ---------- 租约限定的 Commander ----------


class LeasedCommander(Commander):
    def __init__(self, inner: Commander, lease: Lease) -> None:
        self._inner = inner
        self._lease = lease

    def _check(self, units: Iterable[UnitId]) -> list[UnitId]:
        ids = list(units)
        bad = [u for u in ids if u not in self._lease.units]
        if bad:
            raise NotLeased(f"单位 {bad} 不在租约 {self._lease.id} 里")
        return ids

    def move(self, units: Iterable[UnitId], to: Any, queue: bool = False) -> None:
        self._inner.move(self._check(units), to, queue)

    def attack_move(self, units: Iterable[UnitId], to: Any, queue: bool = False) -> None:
        self._inner.attack_move(self._check(units), to, queue)

    def attack(self, units: Iterable[UnitId], target: UnitId) -> None:
        self._inner.attack(self._check(units), target)

    def patrol(self, units: Iterable[UnitId], to: Any) -> None:
        self._inner.patrol(self._check(units), to)

    def stop(self, units: Iterable[UnitId]) -> None:
        self._inner.stop(self._check(units))

    def hold(self, units: Iterable[UnitId]) -> None:
        self._inner.hold(self._check(units))

    def gather(self, worker: UnitId, resource: UnitId) -> None:
        self._check([worker])
        self._inner.gather(worker, resource)

    def build(self, worker: UnitId, structure: str, at: Any) -> bool:
        self._check([worker])
        return self._inner.build(worker, structure, at)

    def train(self, producer: UnitId, unit: str) -> bool:
        self._check([producer])
        return self._inner.train(producer, unit)

    def research(self, structure: UnitId, upgrade: str) -> bool:
        self._check([structure])
        return self._inner.research(structure, upgrade)

    def load(self, transport: UnitId, units: Iterable[UnitId]) -> None:
        self._check([transport])
        self._inner.load(transport, self._check(units))

    def unload_all(self, transport: UnitId, at: Any) -> None:
        self._check([transport])
        self._inner.unload_all(transport, at)


# ---------- 仲裁器 ----------


class SimpleArbiter(Arbiter):
    def __init__(self, world: WorldView) -> None:
        self.world = world
        self.leases: dict[int, Lease] = {}            # lease id -> lease
        self._by_skill: dict[int, Lease] = {}
        self._reservations: dict[int, tuple[int, int, Priority]] = {}   # skill id -> (minerals, gas, priority)
        self._next_lease = 1
        self._income: Deque[tuple[int, int]] = deque()
        self.on_revoke: Callable[[Lease, Revocation], None] | None = None

    # 执行器每帧调一次，采样收入
    def observe(self) -> None:
        self._income.append((self.world.frame, self.world.resources().minerals))
        while self._income and self._income[0][0] < self.world.frame - INCOME_WINDOW_FRAMES:
            self._income.popleft()

    def _income_rate(self) -> float:
        if len(self._income) < 2:
            return 0.0
        (f0, m0), (f1, m1) = self._income[0], self._income[-1]
        if f1 <= f0:
            return 0.0
        # 花钱会让曲线掉下去，只看涨的部分
        gained = sum(max(0, b[1] - a[1]) for a, b in zip(self._income, list(self._income)[1:]))
        return gained / (f1 - f0)

    def _held_exclusive(self) -> dict[UnitId, Lease]:
        out: dict[UnitId, Lease] = {}
        for lease in self.leases.values():
            if getattr(lease, "exclusive", True):
                for u in lease.units:
                    out[u] = lease
        return out

    def _pick(self, selector: UnitSelector, priority: Priority, taken: set[UnitId]) -> tuple[list[UnitView], dict[int, list[UnitId]]] | None:
        """返回选中的单位和需要从哪些租约里抢的单位；None 表示凑不齐。"""
        held = self._held_exclusive()
        anchor = self.world.resolve(selector.near) if selector.near is not None else self.world.resolve(NamedRegion.MY_MAIN)
        chosen: list[UnitView] = []
        steals: dict[int, list[UnitId]] = {}
        for spec in selector.specs:
            pool = [u for u in self.world.my_units(spec.type) if u.id not in taken and u.is_complete]
            if not pool:
                return None
            free = [u for u in pool if u.id not in held]
            stealable = [u for u in pool if u.id in held and held[u.id].priority < priority]
            if selector.prefer == "idle":
                free.sort(key=lambda u: (not u.is_idle, self.world.distance(u.pos, anchor)))
            elif selector.prefer == "nearest":
                free.sort(key=lambda u: self.world.distance(u.pos, anchor))
            stealable.sort(key=lambda u: held[u.id].priority)
            got = free[:spec.count]
            for u in stealable[:spec.count - len(got)]:
                got.append(u)
                steals.setdefault(held[u.id].id, []).append(u.id)
            if len(got) < spec.count and not selector.allow_partial:
                return None
            chosen.extend(got)
            taken.update(u.id for u in got)
        return chosen, steals

    def request(self, skill_id: int, skill_name: str, req: Requirements, priority: Priority) -> Grant:
        if req.minerals or req.gas:
            self._reservations[skill_id] = (req.minerals, req.gas, priority)
        taken: set[UnitId] = set()
        picks: list[UnitView] = []
        steals: dict[int, list[UnitId]] = {}
        for selector in req.units:
            r = self._pick(selector, priority, taken)
            if r is None:
                exists = any(self.world.my_units(s.type) for s in selector.specs)
                want = "、".join(f"{s.count} 个 {s.type}" for s in selector.specs)
                if not exists:
                    return Grant(GrantStatus.DENIED, reason=f"没有 {want}")
                return Grant(GrantStatus.QUEUED, reason=f"{want} 暂时都被占用")
            picks.extend(r[0])
            for lid, ids in r[1].items():
                steals.setdefault(lid, []).extend(ids)

        lease = self._by_skill.get(skill_id)
        if lease is None:
            lease = Lease(self._next_lease, skill_id, priority)
            setattr(lease, "exclusive", req.exclusive)
            self._next_lease += 1
            self.leases[lease.id] = lease
            self._by_skill[skill_id] = lease
        for lid, ids in steals.items():
            victim = self.leases[lid]
            victim.units.difference_update(ids)
            if self.on_revoke is not None:
                self.on_revoke(victim, Revocation(frozenset(ids), skill_name, f"优先级 {priority.name} 的 {skill_name} 需要"))
        new_ids = frozenset(u.id for u in picks)
        lease.units.update(new_ids)
        return Grant(GrantStatus.GRANTED, lease=lease, units=new_ids)

    def release(self, lease: Lease, units: Iterable[UnitId] | None = None) -> None:
        if units is None:
            lease.units.clear()
            self.leases.pop(lease.id, None)
            self._by_skill.pop(lease.skill_id, None)
        else:
            lease.units.difference_update(units)

    def funds(self, skill_id: int) -> FundsStatus:
        mine = self._reservations.get(skill_id)
        if mine is None:
            return FundsStatus(ready=True, eta_seconds=0.0)
        needed = 0
        for sid, (m, g, p) in self._reservations.items():
            if (p, -sid) >= (mine[2], -skill_id):
                needed += m
        have = self.world.resources().minerals
        if have >= needed:
            return FundsStatus(ready=True, eta_seconds=0.0)
        rate = self._income_rate()
        eta = (needed - have) / rate / 24.0 if rate > 0 else None
        return FundsStatus(ready=False, eta_seconds=eta)

    def unreserve(self, skill_id: int) -> None:
        self._reservations.pop(skill_id, None)


# ---------- 执行器 ----------


class SkillTask:
    """实现 SkillHandle。"""

    def __init__(self, task_id: int, skill: Skill[Any], priority: Priority, runner: SimpleRunner) -> None:
        self._id = task_id
        self.skill = skill
        self.priority = priority
        self._runner = runner
        self._status = SkillStatus.PENDING
        self._result: SkillResult | None = None
        self.req: Requirements | None = None
        self.lease: Lease | None = None
        self.ctx: SkillContext | None = None
        self.cancel_deadline: int | None = None
        self.waiting_emitted = False
        self.last_tick_frame: int | None = None

    @property
    def id(self) -> int:
        return self._id

    @property
    def name(self) -> str:
        return self.skill.name

    @property
    def status(self) -> SkillStatus:
        return self._status

    @property
    def phase(self) -> str:
        return self.skill.phase

    def cancel(self, reason: str = "player") -> None:
        self._runner.cancel(self, reason)

    def result(self) -> SkillResult | None:
        return self._result


class SimpleRunner:
    def __init__(self, world: WorldView, cmd: Commander, arbiter: SimpleArbiter, events: EventSink,
                 deaths: Sequence[Death] | None = None) -> None:
        self.world = world
        self.cmd = cmd
        self.arbiter = arbiter
        self.events = events
        self.tasks: list[SkillTask] = []
        self._next_id = 1
        self._deaths = deaths           # 沙盒的 deaths 列表；真游戏适配器要提供同样的东西
        self._deaths_seen = 0
        arbiter.on_revoke = self._on_revoke

    def launch(self, skill: Skill[Any], priority: Priority) -> SkillTask:
        task = SkillTask(self._next_id, skill, priority, self)
        self._next_id += 1
        self.tasks.append(task)
        errors = skill.check(self.world)
        if errors:
            self._finish(task, SkillStatus.FAILED, "；".join(errors))
            return task
        task.req = skill.requirements(self.world)
        task._status = SkillStatus.WAITING
        return task

    def active(self) -> Sequence[SkillTask]:
        return [t for t in self.tasks if t.status not in TERMINAL]

    def by_lease(self, lease: Lease) -> SkillTask | None:
        return next((t for t in self.tasks if t.lease is lease), None)

    def tick(self, world: WorldView | None = None) -> None:
        self.arbiter.observe()
        self._attribute_deaths()
        for task in list(self.tasks):
            if task.status is SkillStatus.WAITING:
                self._try_start(task)
            elif task.status is SkillStatus.RUNNING:
                self._run(task)

    def cancel(self, task: SkillTask, reason: str) -> None:
        if task.status in TERMINAL:
            return
        if task.ctx is None:
            self._finish(task, SkillStatus.ABORTED, f"启动前取消：{reason}")
            return
        status = task.skill.on_cancel(task.ctx, reason)
        if status in TERMINAL:
            self._finish(task, status)
        else:
            task.cancel_deadline = self.world.frame + int(CANCEL_GRACE_SECONDS * 24)
            task.ctx.emit(EventKind.PROGRESS, f"收到取消，正在收尾：{reason}", Severity.NOTICE)

    # ---------- 内部 ----------

    def _emit(self, task: SkillTask, kind: EventKind, message: str, severity: Severity, **data: Any) -> None:
        self.events.emit(SkillEvent(self.world.frame, task.id, task.name, kind, severity, message, data))

    def _try_start(self, task: SkillTask) -> None:
        assert task.req is not None
        grant = self.arbiter.request(task.id, task.name, task.req, task.priority)
        if grant.status is GrantStatus.DENIED:
            self._finish(task, SkillStatus.FAILED, f"仲裁器拒绝：{grant.reason}")
            return
        if grant.status is GrantStatus.QUEUED:
            if not task.waiting_emitted:
                self._emit(task, EventKind.WAITING, f"等待：{grant.reason}", Severity.INFO)
                task.waiting_emitted = True
            return
        assert grant.lease is not None
        task.lease = grant.lease
        task.ctx = SkillContext(
            world=self.world, cmd=LeasedCommander(self.cmd, grant.lease), lease=grant.lease,
            arbiter=self.arbiter, events=self.events, skill_id=task.id, skill_name=task.name,
            priority=task.priority, started_frame=self.world.frame, started_seconds=self.world.seconds,
        )
        task._status = SkillStatus.RUNNING
        self._emit(task, EventKind.STARTED, f"开始，租到单位 {sorted(grant.lease.units)}", Severity.INFO)
        task.skill.on_start(task.ctx)

    def _run(self, task: SkillTask) -> None:
        assert task.ctx is not None
        if task.cancel_deadline is not None and self.world.frame >= task.cancel_deadline:
            self._finish(task, SkillStatus.ABORTED, "取消后收尾超时，强制结束")
            return
        if task.last_tick_frame is not None and self.world.frame - task.last_tick_frame < task.skill.tick_every:
            return
        task.last_tick_frame = self.world.frame
        status = task.skill.tick(task.ctx)
        if status in TERMINAL:
            self._finish(task, status)

    def _on_revoke(self, lease: Lease, revocation: Revocation) -> None:
        task = self.by_lease(lease)
        if task is None or task.ctx is None or task.status in TERMINAL:
            return
        self._emit(task, EventKind.REVOKED, f"单位 {sorted(revocation.units)} 被 {revocation.taken_by} 抢走", Severity.NOTICE)
        status = task.skill.on_revoked(task.ctx, revocation)
        if status in TERMINAL:
            self._finish(task, status)

    def _attribute_deaths(self) -> None:
        if self._deaths is None:
            return
        new = self._deaths[self._deaths_seen:]
        self._deaths_seen = len(self._deaths)
        for d in new:
            for task in self.tasks:
                if task.status is not SkillStatus.RUNNING or task.lease is None or task.ctx is None:
                    continue
                if d.victim in task.lease.units:
                    task.lease.units.discard(d.victim)
                    task.ctx.stats.casualties += 1
                    self._emit(task, EventKind.CASUALTY, f"损失 {d.victim_type}", Severity.NOTICE, unit=d.victim)
                if d.killer is not None and d.killer in task.lease.units and d.victim_owner != self.world.me:
                    task.ctx.stats.kills += 1
                    self._emit(task, EventKind.KILL, f"击杀 {d.victim_type}", Severity.INFO, unit=d.victim)

    def _finish(self, task: SkillTask, status: SkillStatus, reason: str | None = None) -> None:
        if task.ctx is not None:
            result = task.skill.on_finish(task.ctx, status)
            if reason:
                result.summary = f"{result.summary}（{reason}）"
        else:
            result = SkillResult(status=status, summary=reason or status.value)
        task._status = status
        task._result = result
        kind = {SkillStatus.SUCCEEDED: EventKind.DONE, SkillStatus.FAILED: EventKind.FAILED}.get(status, EventKind.ABORTED)
        sev = Severity.ALERT if status is SkillStatus.FAILED else Severity.NOTICE
        self._emit(task, kind, result.summary, sev, kills=result.kills, casualties=result.casualties)
        if task.lease is not None:
            self.arbiter.release(task.lease)
        self.arbiter.unreserve(task.id)
