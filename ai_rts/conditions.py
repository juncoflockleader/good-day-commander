"""撤退 / 中止条件。

技能参数里的 abort.when 用这些声明式条件拼出来。LLM 能直接输出，
反馈层也能把它翻译回人话（"附近敌军超过 6 人口就撤"）。
每个条件最后一个字段是 kind，做 JSON 解析时的判别；放最后是为了
代码里还能按位置传参，比如 HpBelow(0.3)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, Union, runtime_checkable

from .types import Location

if TYPE_CHECKING:
    from .skill import SkillContext


@runtime_checkable
class Condition(Protocol):
    def evaluate(self, ctx: SkillContext) -> bool: ...


@dataclass(frozen=True)
class EnemyNearby:
    """任一租用单位附近出现敌军，且敌军总人口不低于 min_supply。"""

    radius: float = 8.0
    min_supply: float = 0.0
    kind: Literal["enemy_nearby"] = "enemy_nearby"

    def evaluate(self, ctx: SkillContext) -> bool:
        for u in ctx.units():
            t = ctx.world.threat_at(u.pos, self.radius)
            if t.enemy_count > 0 and t.enemy_supply >= self.min_supply:
                return True
        return False


@dataclass(frozen=True)
class HpBelow:
    """任一租用单位血量比例低于 fraction。"""

    fraction: float = 0.3
    kind: Literal["hp_below"] = "hp_below"

    def evaluate(self, ctx: SkillContext) -> bool:
        return any(u.hp_max > 0 and u.hp / u.hp_max < self.fraction for u in ctx.units())


@dataclass(frozen=True)
class Elapsed:
    """技能开始后过了多少游戏秒。"""

    seconds: float = 60.0
    kind: Literal["elapsed"] = "elapsed"

    def evaluate(self, ctx: SkillContext) -> bool:
        return ctx.elapsed() >= self.seconds


@dataclass(frozen=True)
class Casualties:
    """租用单位累计阵亡数达到 count。"""

    count: int = 1
    kind: Literal["casualties"] = "casualties"

    def evaluate(self, ctx: SkillContext) -> bool:
        return ctx.stats.casualties >= self.count


@dataclass(frozen=True)
class Kills:
    """累计击杀数达到 count。"打够就撤"。"""

    count: int = 1
    kind: Literal["kills"] = "kills"

    def evaluate(self, ctx: SkillContext) -> bool:
        return ctx.stats.kills >= self.count


@dataclass(frozen=True)
class AnyOf:
    conditions: tuple[ConditionSpec, ...] = ()
    kind: Literal["any_of"] = "any_of"

    def evaluate(self, ctx: SkillContext) -> bool:
        return any(c.evaluate(ctx) for c in self.conditions)


@dataclass(frozen=True)
class AllOf:
    conditions: tuple[ConditionSpec, ...] = ()
    kind: Literal["all_of"] = "all_of"

    def evaluate(self, ctx: SkillContext) -> bool:
        return bool(self.conditions) and all(c.evaluate(ctx) for c in self.conditions)


@dataclass(frozen=True)
class Not:
    condition: ConditionSpec | None = None
    kind: Literal["not"] = "not"

    def evaluate(self, ctx: SkillContext) -> bool:
        return self.condition is not None and not self.condition.evaluate(ctx)


ConditionSpec = Union[EnemyNearby, HpBelow, Elapsed, Casualties, Kills, AnyOf, AllOf, Not]


@dataclass(frozen=True)
class AbortPolicy:
    """技能的撤退策略。放在参数里，玩家一句"打不过就撤回主基地"就是改这里。"""

    when: ConditionSpec | None = None            # 触发条件，None 表示永不主动撤
    retreat_to: Location | None = None           # 撤到哪，None 表示原地停
    on_revoked: Literal["abort", "continue"] = "abort"   # 单位被更高优先级抢走时：整个放弃，还是剩下的继续
