"""技能发出的结构化事件，是反馈层的唯一输入。

分工：
- 技能自己发 PHASE / CONTACT / PROGRESS / BLOCKED，带业务语义；
- 执行器统一发 STARTED / WAITING / REVOKED / CASUALTY / KILL / DONE / FAILED / ABORTED。

severity 不是日志级别，是"该不该打断玩家"的级别，反馈层据此做注意力管理。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Protocol

from .types import Frame


class Severity(IntEnum):
    DEBUG = 0      # 只进日志
    INFO = 1       # 攒着，汇总时再说
    NOTICE = 2     # 值得单独一句
    ALERT = 3      # 应该打断玩家
    CRITICAL = 4   # 必须立刻说


class EventKind(str, Enum):
    STARTED = "started"
    WAITING = "waiting"      # 在等租约或资金
    PHASE = "phase"          # 阶段切换
    CONTACT = "contact"      # 看到敌方东西
    PROGRESS = "progress"    # 阶段内的里程碑
    BLOCKED = "blocked"      # 卡住了但没放弃
    CASUALTY = "casualty"
    KILL = "kill"
    REVOKED = "revoked"      # 单位被抢走
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass(frozen=True)
class SkillEvent:
    frame: Frame
    skill_id: int
    skill_name: str
    kind: EventKind
    severity: Severity
    message: str                                   # 已经是人话，反馈层可直接用也可重写
    data: dict[str, Any] = field(default_factory=dict)   # 结构化细节，给反馈层做聚合


class EventSink(Protocol):
    def emit(self, event: SkillEvent) -> None: ...
