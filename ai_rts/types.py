"""基础语义类型。

技能参数不用裸坐标和裸 id，用这里的语义类型，原因有三：
1. 理解层（LLM）只需要输出 "my_natural"、"9 点钟" 这种人话级别的值；
2. 把语义值解析成坐标的工作集中在 WorldView.resolve，技能自己不碰地图；
3. 注册表能从这些类型自动导出 JSON schema，技能就直接是 LLM 的 tool。
"""
from __future__ import annotations

from dataclasses import MISSING, dataclass, field
from enum import Enum
from typing import Any, Literal, Union

Frame = int        # 游戏帧
Seconds = float    # 游戏内秒
UnitId = int
PlayerId = int
UnitType = str     # 具体兵种名由游戏适配层定义，如 "SCV"、"GOLIATH"；也接受下面的角色名

# 角色名：跨种族的抽象单位类别，适配层负责映射到具体兵种
ROLE_WORKER = "worker"
ROLE_TRANSPORT = "transport"
ROLE_TOWN_HALL = "town_hall"
ROLE_ARMY = "army"
ROLE_MINERAL = "mineral"
ROLE_GAS = "gas"


def param(doc: str, default: Any = MISSING, *, default_factory: Any = MISSING) -> Any:
    """带说明的 dataclass 字段。说明会写进 JSON schema 的 description，LLM 靠它理解参数。"""
    kwargs: dict[str, Any] = {"metadata": {"doc": doc}}
    if default is not MISSING:
        kwargs["default"] = default
    if default_factory is not MISSING:
        kwargs["default_factory"] = default_factory
    return field(**kwargs)


@dataclass(frozen=True)
class Point:
    """地图坐标。"""

    x: float
    y: float


class NamedRegion(str, Enum):
    """地图语义区域。理解层把"高地下""对面矿区"解析到这里，地图数据把这里解析到坐标。"""

    MY_MAIN = "my_main"
    MY_NATURAL = "my_natural"            # 高地下
    MY_THIRD = "my_third"
    MY_RAMP = "my_ramp"
    MY_MINERAL_LINE = "my_mineral_line"
    MY_ARMY = "my_army"                  # 我方主力当前位置
    ENEMY_MAIN = "enemy_main"
    ENEMY_NATURAL = "enemy_natural"
    ENEMY_THIRD = "enemy_third"
    ENEMY_RAMP = "enemy_ramp"
    ENEMY_MINERAL_LINE = "enemy_mineral_line"
    ENEMY_ARMY = "enemy_army"            # 最近一次看到的敌方主力位置
    MAP_CENTER = "map_center"


@dataclass(frozen=True)
class ClockPosition:
    """钟面方位，以地图中心为原点，12 点朝上。"""

    hour: int


@dataclass(frozen=True)
class NearUnit:
    """跟着某个单位。"""

    unit: UnitId


# 技能参数里的"位置"。start 之前都要经 WorldView.resolve 变成 Point。
Location = Union[Point, NamedRegion, ClockPosition, NearUnit]


def describe_location(loc: Location) -> str:
    """给反馈层用的人话。"""
    if isinstance(loc, NamedRegion):
        return loc.value
    if isinstance(loc, ClockPosition):
        return f"{loc.hour} 点钟方向"
    if isinstance(loc, Point):
        return f"({loc.x:.0f}, {loc.y:.0f})"
    if isinstance(loc, NearUnit):
        return f"单位 {loc.unit} 附近"
    return str(loc)


@dataclass(frozen=True)
class UnitSpec:
    """要多少个什么单位。type 可以是具体兵种，也可以是角色名。"""

    type: UnitType
    count: int = 1


@dataclass(frozen=True)
class UnitSelector:
    """从哪些单位里挑。仲裁器按这个挑单位发租约。"""

    specs: tuple[UnitSpec, ...]
    prefer: Literal["idle", "nearest", "any"] = "idle"
    near: Location | None = None       # prefer=nearest 时相对哪里
    allow_partial: bool = False        # 凑不齐也先给


@dataclass(frozen=True)
class Cost:
    minerals: int = 0
    gas: int = 0
    supply: int = 0


@dataclass(frozen=True)
class Resources:
    minerals: int
    gas: int
    supply_used: int
    supply_cap: int
