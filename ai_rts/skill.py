"""战术层核心：一个技能的生命周期。

技能是"一次性、有始有终"的任务：开矿、侦查、空投。常驻的管理器（经济、生产）不是技能，
以后另开一个 Manager 基类，但复用这里的 SkillContext 和事件。

生命周期（执行器驱动）：

    PENDING --launch--> WAITING --租约到位--> on_start --> RUNNING --tick...--> SUCCEEDED / FAILED / ABORTED
                           |                                 |
                           +--DENIED--> FAILED               +-- on_revoked / on_cancel --> ABORTED，或 RUNNING 继续收尾

技能分两个时期：
- 规划期（start 前）：requirements / check / estimate，不需要租约，理解层拿来做可行性反馈
  （"没有运输机，要先造一个吗？"）；
- 运行期：on_start / tick / on_revoked / on_cancel / on_finish，只通过 ctx 看世界、下命令、发事件。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Generic, Literal, Protocol, Sequence, TypeVar

from .arbiter import Arbiter, Lease, Priority, Requirements, Revocation
from .conditions import AbortPolicy
from .events import EventKind, EventSink, Severity, SkillEvent
from .types import Frame, Seconds, UnitId, UnitType
from .world import Commander, UnitView, WorldView


class SkillStatus(Enum):
    PENDING = "pending"
    WAITING = "waiting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


TERMINAL = frozenset({SkillStatus.SUCCEEDED, SkillStatus.FAILED, SkillStatus.ABORTED})


@dataclass(frozen=True)
class Estimate:
    """规划期的预估，反馈层用来告诉玩家"空投大约 40 秒到，目标附近有防空"。"""

    eta_seconds: float | None = None
    risk: Literal["low", "medium", "high"] = "medium"
    note: str = ""


@dataclass
class LeaseStats:
    """执行器按租约统计，技能和撤退条件都能读。"""

    kills: int = 0
    casualties: int = 0


@dataclass
class SkillResult:
    status: SkillStatus
    summary: str                       # 一句人话，反馈层直接用
    data: dict[str, Any] = field(default_factory=dict)
    kills: int = 0
    casualties: int = 0
    duration_seconds: float = 0.0


@dataclass
class SkillContext:
    """技能运行期能摸到的一切。执行器构造，技能只读。"""

    world: WorldView
    cmd: Commander          # 租约限定版
    lease: Lease
    arbiter: Arbiter
    events: EventSink
    skill_id: int
    skill_name: str
    priority: Priority
    started_frame: Frame
    started_seconds: Seconds
    stats: LeaseStats = field(default_factory=LeaseStats)

    def units(self) -> list[UnitView]:
        """还活着的租用单位。"""
        out: list[UnitView] = []
        for uid in self.lease.units:
            u = self.world.unit(uid)
            if u is not None:
                out.append(u)
        return out

    def unit_ids(self) -> list[UnitId]:
        return [u.id for u in self.units()]

    def units_of(self, type_or_role: UnitType) -> list[UnitView]:
        return [u for u in self.units() if u.type == type_or_role or u.has_role(type_or_role)]

    def elapsed(self) -> Seconds:
        return self.world.seconds - self.started_seconds

    def emit(self, kind: EventKind, message: str, severity: Severity = Severity.INFO, **data: Any) -> None:
        self.events.emit(SkillEvent(
            frame=self.world.frame,
            skill_id=self.skill_id,
            skill_name=self.skill_name,
            kind=kind,
            severity=severity,
            message=message,
            data=data,
        ))


P = TypeVar("P")


class Skill(ABC, Generic[P]):
    """技能基类。子类要定义四个类属性和三个抽象方法，其余按需覆盖。"""

    name: ClassVar[str]                        # 注册名，也是 LLM tool 名
    description: ClassVar[str]                 # 给 LLM 看的一句话
    aliases: ClassVar[tuple[str, ...]] = ()    # 黑话，帮理解层匹配："偷矿""开分基"
    Params: ClassVar[type]                     # frozen dataclass，字段用 types.param 写说明
    tick_every: ClassVar[int] = 8              # 每几帧 tick 一次

    def __init__(self, params: P) -> None:
        self.params = params
        self.phase = "init"

    # ---------- 规划期 ----------

    @abstractmethod
    def requirements(self, world: WorldView) -> Requirements:
        """需要什么单位和资源。执行器拿它去仲裁器申请。"""

    def check(self, world: WorldView) -> list[str]:
        """前置条件。返回空列表表示能开始，否则每条是一句能直接念给玩家的原因。"""
        return []

    def estimate(self, world: WorldView) -> Estimate | None:
        return None

    # ---------- 运行期 ----------

    @abstractmethod
    def on_start(self, ctx: SkillContext) -> None:
        """租约到位后调一次。下第一批命令，初始化状态。"""

    @abstractmethod
    def tick(self, ctx: SkillContext) -> SkillStatus:
        """每 tick_every 帧调一次。返回 RUNNING 继续，返回终态结束。"""

    def on_revoked(self, ctx: SkillContext, revoked: Revocation) -> SkillStatus:
        """租约里的单位被更高优先级抢走。默认按 abort 策略决定。"""
        policy = self.abort_policy()
        if policy is not None and policy.on_revoked == "continue" and ctx.units():
            return SkillStatus.RUNNING
        self.retreat(ctx)
        return SkillStatus.ABORTED

    def on_cancel(self, ctx: SkillContext, reason: str) -> SkillStatus:
        """玩家或上层取消。返回 ABORTED 立刻结束；返回 RUNNING 表示要收尾（比如先把人接走），
        执行器会继续 tick 直到技能自己走到终态，超时则强制结束。"""
        self.retreat(ctx)
        return SkillStatus.ABORTED

    def on_finish(self, ctx: SkillContext, status: SkillStatus) -> SkillResult:
        """进入终态后调一次，租约还没释放。"""
        return SkillResult(
            status=status,
            summary=self.summarize(ctx, status),
            data=self.result_data(ctx),
            kills=ctx.stats.kills,
            casualties=ctx.stats.casualties,
            duration_seconds=ctx.elapsed(),
        )

    def summarize(self, ctx: SkillContext, status: SkillStatus) -> str:
        return f"{self.name} {status.value}"

    def result_data(self, ctx: SkillContext) -> dict[str, Any]:
        return {}

    # ---------- 观测 ----------

    def snapshot(self) -> dict[str, Any]:
        """当前状态，反馈层回答"空投到哪了"时用。"""
        return {"name": self.name, "phase": self.phase}

    # ---------- 子类可用的工具 ----------

    def set_phase(self, ctx: SkillContext, phase: str, message: str | None = None,
                  severity: Severity = Severity.INFO) -> None:
        self.phase = phase
        ctx.emit(EventKind.PHASE, message or phase, severity, phase=phase)

    def abort_policy(self) -> AbortPolicy | None:
        return getattr(self.params, "abort", None)

    def should_abort(self, ctx: SkillContext) -> bool:
        policy = self.abort_policy()
        return policy is not None and policy.when is not None and policy.when.evaluate(ctx)

    def retreat(self, ctx: SkillContext) -> None:
        """按 abort 策略把所有租用单位撤走，没策略就原地停。"""
        ids = ctx.unit_ids()
        if not ids:
            return
        policy = self.abort_policy()
        if policy is not None and policy.retreat_to is not None:
            ctx.cmd.move(ids, ctx.world.resolve(policy.retreat_to))
        else:
            ctx.cmd.stop(ids)


# ---------- 执行器侧的契约，技能库的消费者 ----------


class SkillHandle(Protocol):
    """launch 之后上层拿到的句柄。理解层用它回答"取消空投"和"侦查到哪了"。"""

    @property
    def id(self) -> int: ...
    @property
    def name(self) -> str: ...
    @property
    def status(self) -> SkillStatus: ...
    @property
    def phase(self) -> str: ...

    def cancel(self, reason: str = "player") -> None: ...
    def result(self) -> SkillResult | None: ...


class SkillRunner(Protocol):
    """技能执行器。拥有技能实例，负责：
    向仲裁器申请租约并在 WAITING 期间重试；按 tick_every 调 tick；
    把租约被收回转成 on_revoked；维护 ctx.stats；
    进入终态时调 on_finish，发 DONE / FAILED / ABORTED 事件，释放租约。"""

    def launch(self, skill: Skill[Any], priority: Priority) -> SkillHandle: ...
    def active(self) -> Sequence[SkillHandle]: ...
    def tick(self, world: WorldView) -> None: ...
