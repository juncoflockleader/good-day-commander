"""空投骚扰：装人、绕路、落地打农民、情况不对接走。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..arbiter import Requirements
from ..conditions import AbortPolicy, AnyOf, EnemyNearby, HpBelow
from ..events import EventKind, Severity
from ..skill import Estimate, Skill, SkillContext, SkillStatus
from ..types import (ROLE_TRANSPORT, ROLE_WORKER, Location, NamedRegion, Point, UnitId, UnitSelector,
                     UnitSpec, describe_location, param)
from ..world import RouteMode, UnitView, WorldView

ARRIVE = 3.0
UNLOAD_RADIUS = 2.0
ENGAGE_RADIUS = 10.0


@dataclass(frozen=True)
class DropHarassParams:
    cargo: UnitSelector = param("装什么去。默认两个金甲。", UnitSelector((UnitSpec("GOLIATH", 2),)))
    transport: UnitSelector = param("用哪架运输机。", UnitSelector((UnitSpec(ROLE_TRANSPORT, 1),)))
    target: Location = param("投到哪。默认对方矿区。", NamedRegion.ENEMY_MINERAL_LINE)
    route: Literal["edge", "direct"] = param("edge 贴地图边绕过去，direct 直飞。", "edge")
    focus: Literal["workers", "army", "structures"] = param("落地后优先打什么。", "workers")
    extract: bool = param("撤退时是否接走部队。False 就地放弃，运输机自己回。", True)
    abort: AbortPolicy = param(
        "什么情况下撤。默认附近敌军人口超过 6，或任一单位血量低于 30%。",
        AbortPolicy(when=AnyOf((EnemyNearby(radius=8.0, min_supply=6.0), HpBelow(0.3))),
                    retreat_to=NamedRegion.MY_MAIN))


class DropHarass(Skill[DropHarassParams]):
    name = "drop_harass"
    description = "用运输机把一队部队空投到指定位置骚扰，默认打农民，情况不对就接走撤回。"
    aliases = ("空投", "偷矿", "空降", "骚扰")
    Params = DropHarassParams

    # ---------- 规划期 ----------

    def requirements(self, world: WorldView) -> Requirements:
        return Requirements(units=(self.params.cargo, self.params.transport))

    def check(self, world: WorldView) -> list[str]:
        errors = []
        for spec in self.params.cargo.specs:
            have = len(world.my_units(spec.type))
            if have < spec.count and not self.params.cargo.allow_partial:
                errors.append(f"{spec.type} 只有 {have} 个，需要 {spec.count}")
        if not world.my_units(self.params.transport.specs[0].type):
            errors.append("没有可用的运输机")
        return errors

    def estimate(self, world: WorldView) -> Estimate | None:
        dest = world.resolve(self.params.target)
        t_type = self.params.transport.specs[0].type
        eta = None
        transports = world.my_units(t_type)
        if transports:
            path = world.route(transports[0].pos, dest, self._route_mode())
            length = sum(world.distance(a, b) for a, b in zip(path, path[1:]))
            speed = world.speed(t_type)
            eta = length / speed if speed > 0 else None
        threat = world.threat_at(dest, 10.0)
        if threat.has_anti_air:
            return Estimate(eta_seconds=eta, risk="high", note="目标附近有防空")
        return Estimate(eta_seconds=eta, risk="medium")

    # ---------- 运行期 ----------

    def on_start(self, ctx: SkillContext) -> None:
        t_type = self.params.transport.specs[0].type
        transport = ctx.units_of(t_type)[0]
        self._transport: UnitId = transport.id
        self._cargo: list[UnitId] = [u.id for u in ctx.units() if u.id != transport.id]
        self._dest = ctx.world.resolve(self.params.target)
        self._home = ctx.world.resolve(NamedRegion.MY_MAIN)
        self._path = list(ctx.world.route(transport.pos, self._dest, self._route_mode()))
        self._target: UnitId | None = None
        self._returned = False
        self._cancelled = False
        ctx.cmd.move(self._cargo, transport.pos)
        ctx.cmd.load(self._transport, self._cargo)
        self.set_phase(ctx, "loading", f"装载 {len(self._cargo)} 个单位，目标 {describe_location(self.params.target)}")

    def tick(self, ctx: SkillContext) -> SkillStatus:
        world = ctx.world
        transport = world.unit(self._transport)
        cargo = [u for u in ctx.units() if u.id in self._cargo]
        if transport is None:
            return self._transport_lost(ctx, cargo)

        phase = self.phase
        if phase == "loading":
            if set(transport.cargo) >= {u.id for u in cargo}:
                self._fly(ctx, self._path)
                self.set_phase(ctx, "transit", "装载完毕，起飞")
        elif phase == "transit":
            if self.should_abort(ctx):
                self._fly(ctx, [self._home])
                self.set_phase(ctx, "returning", "途中遇到威胁，放弃空投", Severity.NOTICE)
            elif world.distance(transport.pos, self._dest) <= UNLOAD_RADIUS:
                ctx.cmd.unload_all(self._transport, self._dest)
                self.set_phase(ctx, "unloading", "到达目标，卸货")
        elif phase == "unloading":
            if not transport.cargo:
                self._retarget(ctx, cargo)
                self.set_phase(ctx, "harass", "部队落地，开始骚扰", Severity.NOTICE)
        elif phase == "harass":
            if not cargo:
                self._fly(ctx, [self._home])
                self.set_phase(ctx, "returning", "空投部队全部阵亡，运输机撤回", Severity.ALERT)
            elif self.should_abort(ctx):
                self._extract_or_leave(ctx, cargo, "触发撤退条件")
            else:
                self._retarget(ctx, cargo)
        elif phase == "extracting":
            if not cargo or set(transport.cargo) >= {u.id for u in cargo}:
                self._fly(ctx, [self._home])
                self.set_phase(ctx, "returning", "接到部队，返航")
            else:
                ctx.cmd.load(self._transport, [u.id for u in cargo])
        elif phase == "returning":
            if world.distance(transport.pos, self._home) <= ARRIVE:
                self._returned = True
                if self._cancelled or ctx.stats.kills == 0:
                    return SkillStatus.ABORTED
                return SkillStatus.SUCCEEDED
        elif phase == "stranded":
            if not cargo:
                return SkillStatus.FAILED
            if self.should_abort(ctx):
                self.retreat(ctx)
                return SkillStatus.ABORTED
            self._retarget(ctx, cargo)
        return SkillStatus.RUNNING

    def on_cancel(self, ctx: SkillContext, reason: str) -> SkillStatus:
        """落地期间被取消，先把人接走再结束。"""
        self._cancelled = True
        cargo = [u for u in ctx.units() if u.id in self._cargo]
        if self.phase == "harass" and ctx.world.unit(self._transport) is not None and cargo:
            self._extract_or_leave(ctx, cargo, f"收到取消，{reason}")
            return SkillStatus.RUNNING
        return super().on_cancel(ctx, reason)

    def summarize(self, ctx: SkillContext, status: SkillStatus) -> str:
        where = describe_location(self.params.target)
        tail = "部队已撤回" if self._returned else "部队没能撤回"
        return f"空投 {where}：击杀 {ctx.stats.kills}，损失 {ctx.stats.casualties}，{tail}"

    def result_data(self, ctx: SkillContext) -> dict[str, Any]:
        return {"returned": self._returned, "final_phase": self.phase}

    # ---------- 内部 ----------

    def _route_mode(self) -> RouteMode:
        return "air_edge" if self.params.route == "edge" else "air"

    def _fly(self, ctx: SkillContext, path: list[Point]) -> None:
        ctx.cmd.move([self._transport], path[0])
        for p in path[1:]:
            ctx.cmd.move([self._transport], p, queue=True)

    def _transport_lost(self, ctx: SkillContext, cargo: list[UnitView]) -> SkillStatus:
        if self.phase in ("loading", "transit", "returning") or not cargo:
            ctx.emit(EventKind.CASUALTY, "运输机被击落", Severity.ALERT)
            return SkillStatus.FAILED
        if self.phase != "stranded":
            self.set_phase(ctx, "stranded", "运输机被击落，落地部队失去接应，继续作战", Severity.ALERT)
        return SkillStatus.RUNNING

    def _extract_or_leave(self, ctx: SkillContext, cargo: list[UnitView], why: str) -> None:
        ids = [u.id for u in cargo]
        if self.params.extract:
            ctx.cmd.move([self._transport], cargo[0].pos)
            ctx.cmd.move(ids, cargo[0].pos)
            ctx.cmd.load(self._transport, ids)
            self.set_phase(ctx, "extracting", f"{why}，接走部队", Severity.NOTICE)
        else:
            self.retreat(ctx)
            self._fly(ctx, [self._home])
            self.set_phase(ctx, "returning", f"{why}，放弃部队，运输机返航", Severity.NOTICE)

    def _retarget(self, ctx: SkillContext, cargo: list[UnitView]) -> None:
        """选目标。只决定打谁，走位集火归微操层。"""
        center = cargo[0].pos
        enemies = list(ctx.world.enemies_visible(near=center, radius=ENGAGE_RADIUS))
        focus = self.params.focus
        if focus == "workers":
            pick = [e for e in enemies if e.has_role(ROLE_WORKER)]
        elif focus == "structures":
            pick = [e for e in enemies if e.is_structure]
        else:
            pick = [e for e in enemies if not e.is_structure and not e.has_role(ROLE_WORKER)]
        if not pick:
            pick = [e for e in enemies if not e.is_structure] or enemies
        ids = [u.id for u in cargo]
        if pick:
            target = min(pick, key=lambda e: ctx.world.distance(center, e.pos))
            if target.id != self._target:
                ctx.cmd.attack(ids, target.id)
                self._target = target.id
        else:
            self._target = None
            ctx.cmd.attack_move(ids, self._dest)
