"""侦查：一个单位去指定位置看一眼，把看到的报回来。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..arbiter import Requirements
from ..conditions import AbortPolicy, HpBelow
from ..events import EventKind, Severity
from ..skill import Estimate, Skill, SkillContext, SkillStatus
from ..types import ROLE_WORKER, Location, NamedRegion, Point, UnitId, UnitSelector, UnitSpec, describe_location, param
from ..world import UnitView, WorldView

ARRIVE = 3.0
SIGHT = 10.0
CIRCLE_RADIUS = 6.0


@dataclass(frozen=True)
class ScoutParams:
    target: Location = param("去哪看：区域名、钟点方位或坐标。", NamedRegion.ENEMY_MAIN)
    unit: UnitSelector = param("派谁去。默认就近抓一个农民。",
                               UnitSelector((UnitSpec(ROLE_WORKER, 1),), prefer="nearest"))
    pattern: Literal["peek", "circle", "stay"] = param(
        "到了之后怎么做：peek 看一眼就回；circle 绕目标转一圈；stay 停着盯。", "peek")
    linger_seconds: float = param("pattern=stay 时盯多久，0 表示盯到被打断为止。", 0.0)
    abort: AbortPolicy = param("什么情况下撤。默认掉血过半就跑回主基地。",
                               AbortPolicy(when=HpBelow(0.5), retreat_to=NamedRegion.MY_MAIN))


class Scout(Skill[ScoutParams]):
    name = "scout"
    description = "派一个单位去指定位置侦查，汇报看到的敌方建筑和部队，然后撤回。"
    aliases = ("侦查", "侦察", "探路", "看一眼")
    Params = ScoutParams

    # ---------- 规划期 ----------

    def requirements(self, world: WorldView) -> Requirements:
        return Requirements(units=(self.params.unit,))

    def check(self, world: WorldView) -> list[str]:
        spec = self.params.unit.specs[0]
        if not world.my_units(spec.type):
            return [f"没有可用的 {spec.type}"]
        return []

    def estimate(self, world: WorldView) -> Estimate | None:
        spec = self.params.unit.specs[0]
        path = world.route(world.resolve(NamedRegion.MY_MAIN), world.resolve(self.params.target), "ground")
        length = sum(world.distance(a, b) for a, b in zip(path, path[1:]))
        speed = world.speed(spec.type)
        return Estimate(eta_seconds=length / speed if speed > 0 else None, risk="low", note="单个单位，损失可控")

    # ---------- 运行期 ----------

    def on_start(self, ctx: SkillContext) -> None:
        self._dest = ctx.world.resolve(self.params.target)
        self._home = ctx.world.resolve(NamedRegion.MY_MAIN)
        self._seen: dict[UnitId, tuple[str, bool]] = {}
        self._reached = False
        self._observe_since = 0.0
        ctx.cmd.move(ctx.unit_ids(), self._dest)
        self.set_phase(ctx, "moving", f"侦查单位出发，目标 {describe_location(self.params.target)}")

    def tick(self, ctx: SkillContext) -> SkillStatus:
        units = ctx.units()
        if not units:
            return SkillStatus.FAILED
        u = units[0]
        world = ctx.world
        self._record_contacts(ctx, u)

        if self.phase != "returning" and self.should_abort(ctx):
            self._go_home(ctx, "侦查单位受威胁，撤回")
            return SkillStatus.RUNNING

        if self.phase == "moving":
            if world.distance(u.pos, self._dest) <= ARRIVE:
                self._reached = True
                if self.params.pattern == "peek":
                    self._go_home(ctx, "到达目标，看一眼就回")
                elif self.params.pattern == "circle":
                    self._circle(ctx, u)
                else:
                    self._observe_since = world.seconds
                    self.set_phase(ctx, "observing", "到达目标，原地观察")
        elif self.phase == "circling":
            if u.is_idle:
                self._go_home(ctx, "绕完一圈，撤回")
        elif self.phase == "observing":
            linger = self.params.linger_seconds
            if linger > 0 and world.seconds - self._observe_since >= linger:
                self._go_home(ctx, "观察时间到，撤回")
        elif self.phase == "returning":
            if world.distance(u.pos, self._home) <= ARRIVE:
                return SkillStatus.SUCCEEDED if self._reached else SkillStatus.ABORTED
        return SkillStatus.RUNNING

    def summarize(self, ctx: SkillContext, status: SkillStatus) -> str:
        where = describe_location(self.params.target)
        if not self._reached:
            return f"侦查 {where}：没到目标就撤了"
        if not self._seen:
            return f"侦查 {where}：没发现任何东西"
        structures = sorted({t for t, s in self._seen.values() if s})
        units = sorted({t for t, s in self._seen.values() if not s})
        parts = []
        if structures:
            parts.append("建筑 " + "、".join(structures))
        if units:
            parts.append("单位 " + "、".join(units))
        return f"侦查 {where}：看到 " + "，".join(parts)

    def result_data(self, ctx: SkillContext) -> dict[str, Any]:
        return {"reached": self._reached,
                "seen": [{"type": t, "structure": s} for t, s in self._seen.values()]}

    # ---------- 内部 ----------

    def _circle(self, ctx: SkillContext, u: UnitView) -> None:
        d, r = self._dest, CIRCLE_RADIUS
        ring = [Point(d.x + r, d.y), Point(d.x, d.y + r), Point(d.x - r, d.y), Point(d.x, d.y - r)]
        ctx.cmd.move([u.id], ring[0])
        for p in ring[1:]:
            ctx.cmd.move([u.id], p, queue=True)
        self.set_phase(ctx, "circling", "到达目标，绕一圈")

    def _go_home(self, ctx: SkillContext, message: str) -> None:
        ctx.cmd.move(ctx.unit_ids(), self._home)
        self.set_phase(ctx, "returning", message)

    def _record_contacts(self, ctx: SkillContext, u: UnitView) -> None:
        for e in ctx.world.enemies_visible(near=u.pos, radius=SIGHT):
            if e.id in self._seen:
                continue
            self._seen[e.id] = (e.type, e.is_structure)
            ctx.emit(EventKind.CONTACT, f"发现敌方 {e.type}",
                     Severity.NOTICE if e.is_structure else Severity.INFO,
                     unit_type=e.type, structure=e.is_structure, pos=[e.pos.x, e.pos.y])
