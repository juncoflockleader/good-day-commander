"""仲裁器接口：谁能用哪些单位、谁能花哪笔钱。

这是分层之后最硬的问题之一。开矿、攀科技、出兵都要矿；空投和防守都想要同一队金甲。
技能不直接拿单位，向仲裁器申请租约；资金不直接花，先预留。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Iterable, Protocol

from .types import UnitId, UnitSelector


class Priority(IntEnum):
    REFLEX = 100         # 微操层的防御反应，如拉农民躲
    DIRECT = 80          # 玩家此刻点名的指令
    PLANNED = 60         # 战略计划里到点触发的
    STANDING = 40        # 作战层常备管理器的日常
    OPPORTUNISTIC = 20   # AI 自己提议的顺手事


@dataclass(frozen=True)
class Requirements:
    """技能启动需要什么。requirements() 返回，执行器交给仲裁器。"""

    units: tuple[UnitSelector, ...] = ()
    minerals: int = 0
    gas: int = 0
    supply: int = 0
    exclusive: bool = True     # 租到的单位是否独占。False 允许和别的非独占租约共享，比如纯观察类技能


@dataclass
class Lease:
    """一个技能当前持有的单位。执行器维护，单位死亡会从 units 移除。"""

    id: int
    skill_id: int
    priority: Priority
    units: set[UnitId] = field(default_factory=set)


@dataclass(frozen=True)
class Revocation:
    """租约里的部分单位被更高优先级抢走。"""

    units: frozenset[UnitId]
    taken_by: str
    reason: str


class GrantStatus(Enum):
    GRANTED = "granted"
    QUEUED = "queued"      # 单位暂时没有或被占，排队等
    DENIED = "denied"      # 根本不可能满足，如没有运输机


@dataclass(frozen=True)
class Grant:
    status: GrantStatus
    lease: Lease | None = None
    units: frozenset[UnitId] = frozenset()   # 本次调用新给的单位
    reason: str = ""


@dataclass(frozen=True)
class FundsStatus:
    ready: bool
    eta_seconds: float | None    # 按当前收入估计还要多久，None 表示算不出


class Arbiter(Protocol):
    def request(self, skill_id: int, skill_name: str, req: Requirements, priority: Priority) -> Grant:
        """申请单位并登记资金预留。同一 skill_id 再次调用是追加到已有租约。"""
        ...

    def release(self, lease: Lease, units: Iterable[UnitId] | None = None) -> None:
        """归还部分或全部单位。"""
        ...

    def funds(self, skill_id: int) -> FundsStatus:
        """这个技能预留的钱到位了没有。按优先级排队，高优先级先攒。"""
        ...

    def unreserve(self, skill_id: int) -> None: ...
