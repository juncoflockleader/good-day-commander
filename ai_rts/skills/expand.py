"""开分基：预留资金，派农民过去，钱到就放，盯到建完。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..arbiter import GrantStatus, Requirements
from ..conditions import AbortPolicy, EnemyNearby
from ..events import EventKind, Severity
from ..skill import Estimate, Skill, SkillContext, SkillStatus
from ..types import (ROLE_MINERAL, ROLE_TOWN_HALL, ROLE_WORKER, Location, NamedRegion, UnitId,
                     UnitSelector, UnitSpec, describe_location, param)
from ..world import WorldView

ARRIVE = 2.0


@dataclass(frozen=True)
class ExpandParams:
    at: Location = param("在哪开。默认高地下的自然扩张位。", NamedRegion.MY_NATURAL)
    worker: UnitSelector = param("派哪个农民去放。",
                                 UnitSelector((UnitSpec(ROLE_WORKER, 1),), prefer="nearest"))
    pull_early_seconds: float = param(
        "钱还差大约多少秒到位时就先派农民出发，到了正好放。0 表示钱够了再走。", 6.0)
    transfer_workers: int = param("建成后从别处转多少农民过去。0 表示交给经济管理器处理。", 0)
    abort: AbortPolicy = param("什么情况下放弃。默认放基地的点附近有敌军就撤农民。",
                               AbortPolicy(when=EnemyNearby(radius=10.0, min_supply=2.0),
                                           retreat_to=NamedRegion.MY_MAIN))


class Expand(Skill[ExpandParams]):
    name = "expand"
    description = "在指定位置开一个分基地：预留资金，派农民过去，钱到就放，直到建筑完工。"
    aliases = ("开矿", "开分基", "分基", "扩张", "开二矿", "开三矿")
    Params = ExpandParams
    tick_every = 12

    # ---------- 规划期 ----------

    def requirements(self, world: WorldView) -> Requirements:
        cost = world.cost(ROLE_TOWN_HALL)
        return Requirements(units=(self.params.worker,), minerals=cost.minerals, gas=cost.gas)

    def check(self, world: WorldView) -> list[str]:
        errors = []
        if not world.my_units(ROLE_WORKER):
            errors.append("没有农民")
        if world.find_placement(ROLE_TOWN_HALL, world.resolve(self.params.at)) is None:
            errors.append(f"{describe_location(self.params.at)} 放不下基地，可能已经有建筑")
        return errors

    def estimate(self, world: WorldView) -> Estimate | None:
        threat = world.threat_at(world.resolve(self.params.at), 12.0)
        if threat.enemy_count:
            return Estimate(risk="high", note="目标位置附近有敌军")
        return Estimate(risk="low")

    # ---------- 运行期 ----------

    def on_start(self, ctx: SkillContext) -> None:
        site = ctx.world.find_placement(ROLE_TOWN_HALL, ctx.world.resolve(self.params.at))
        self._site = site if site is not None else ctx.world.resolve(self.params.at)
        self._structure: UnitId | None = None
        self.set_phase(ctx, "waiting_funds", f"准备在 {describe_location(self.params.at)} 开矿，等资金")

    def tick(self, ctx: SkillContext) -> SkillStatus:
        world = ctx.world
        if self.phase in ("waiting_funds", "moving", "placing"):
            if not ctx.units():
                return SkillStatus.FAILED
            if self.should_abort(ctx):
                self.retreat(ctx)
                ctx.emit(EventKind.BLOCKED, "开矿点附近有敌军，农民先撤", Severity.ALERT)
                return SkillStatus.ABORTED

        if self.phase == "waiting_funds":
            funds = ctx.arbiter.funds(ctx.skill_id)
            early = self.params.pull_early_seconds
            if funds.ready or (funds.eta_seconds is not None and funds.eta_seconds <= early):
                ctx.cmd.move(ctx.unit_ids(), self._site)
                self.set_phase(ctx, "moving", "农民出发")
        elif self.phase == "moving":
            worker = ctx.units()[0]
            if world.distance(worker.pos, self._site) <= ARRIVE and ctx.arbiter.funds(ctx.skill_id).ready:
                if ctx.cmd.build(worker.id, ROLE_TOWN_HALL, self._site):
                    self.set_phase(ctx, "placing", "资金到位，放基地")
                else:
                    alt = world.find_placement(ROLE_TOWN_HALL, self._site)
                    if alt is None:
                        ctx.emit(EventKind.BLOCKED, "基地放不下", Severity.ALERT)
                        return SkillStatus.FAILED
                    self._site = alt
                    ctx.emit(EventKind.BLOCKED, "原位置放不下，换到旁边", Severity.NOTICE)
        elif self.phase == "placing":
            placed = [u for u in world.units_near(self._site, 3.0, "me") if u.has_role(ROLE_TOWN_HALL)]
            if placed:
                self._structure = placed[0].id
                ctx.arbiter.unreserve(ctx.skill_id)
                self.set_phase(ctx, "building", "基地已放下，建造中", Severity.NOTICE)
        elif self.phase == "building":
            s = world.unit(self._structure) if self._structure is not None else None
            if s is None:
                ctx.emit(EventKind.CASUALTY, "建造中的基地被拆", Severity.ALERT)
                return SkillStatus.FAILED
            if s.is_complete:
                self._transfer(ctx)
                return SkillStatus.SUCCEEDED
        return SkillStatus.RUNNING

    def summarize(self, ctx: SkillContext, status: SkillStatus) -> str:
        where = describe_location(self.params.at)
        if status is SkillStatus.SUCCEEDED:
            return f"{where} 分基建成"
        return f"{where} 开矿{status.value}，停在 {self.phase}"

    def result_data(self, ctx: SkillContext) -> dict[str, Any]:
        return {"structure": self._structure, "site": [self._site.x, self._site.y]}

    # ---------- 内部 ----------

    def _transfer(self, ctx: SkillContext) -> None:
        """建成后追加租一批农民转过去。演示运行期动态追加租约。"""
        n = self.params.transfer_workers
        if n <= 0:
            return
        req = Requirements(units=(UnitSelector((UnitSpec(ROLE_WORKER, n),), prefer="any", near=NamedRegion.MY_MAIN),))
        grant = ctx.arbiter.request(ctx.skill_id, self.name, req, ctx.priority)
        if grant.status is not GrantStatus.GRANTED:
            ctx.emit(EventKind.BLOCKED, f"要转 {n} 个农民，仲裁器没批：{grant.reason}", Severity.NOTICE)
            return
        patches = [u for u in ctx.world.units_near(self._site, 10.0, "neutral") if u.has_role(ROLE_MINERAL)]
        if not patches:
            return
        for w in grant.units:
            ctx.cmd.gather(w, patches[0].id)
        ctx.emit(EventKind.PROGRESS, f"转了 {len(grant.units)} 个农民到新矿", Severity.INFO)
