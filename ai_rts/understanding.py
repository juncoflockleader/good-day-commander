"""理解层：玩家的一句话 → 若干技能调用。

流程：系统提示（角色 + 规则 + 词表）+ 当前局势 + 玩家原话 → LLM 带 tools 调用 →
每个 tool call 经注册表实例化校验 → 校验失败的拿错误信息再问一轮让它修 → 返回 Interpretation。
LLM 不碰执行，只产出结构化指令；执行交给 SkillRunner。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .arbiter import Priority
from .llm import LLMClient, LLMReply, ToolCall
from .registry import SkillRegistry
from .skill import Skill
from .vocab import Vocab
from .world import WorldView

SYSTEM_TEMPLATE = """你是一名 RTS 游戏的参谋，站在指挥官身边。指挥官用自然语言下命令，你把每条命令翻译成一次技能调用（工具调用）。

规则：
1. 一句话里可能有多条命令，每条对应一次工具调用；同一句话里的命令默认并发执行。
2. 指挥官没说的参数不要瞎填，直接省略，技能有合理默认值。位置只能用区域名或钟点方位。
3. 单位一律用词表里的内部名。指挥官说的黑话按词表翻译；词表里没有的说法按常识对应到最接近的内部名。
4. 当前游戏没有的单位，用最接近的替代，并在回复里说一句你替换了什么。
5. 做不到、信息明显不够、或者命令和当前局势矛盾（比如没有运输机却要空投）时，不要调用工具，用一两句话说明或反问。
6. 调用工具之外的回复要短，像参谋复述命令一样：说清你理解成了什么，不要寒暄。

{vocab}
"""


@dataclass
class Order:
    skill: str
    params: dict[str, Any]
    priority: Priority = Priority.DIRECT

    def as_arg(self) -> str:
        """sc2_run.py / docker/run.sh 接受的格式：name={json}。"""
        return f"{self.skill}={json.dumps(self.params, ensure_ascii=False)}" if self.params else self.skill


@dataclass
class Interpretation:
    text: str                                   # 玩家原话
    orders: list[Order]
    reply: str                                  # 参谋的回话
    errors: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    rounds: int = 1

    def summary(self) -> str:
        lines = [f"玩家：{self.text}"]
        if self.reply:
            lines.append(f"参谋：{self.reply}")
        for o in self.orders:
            lines.append(f"  -> {o.as_arg()}")
        for e in self.errors:
            lines.append(f"  !! {e}")
        return "\n".join(lines)


def describe_situation(world: WorldView) -> str:
    """把当前局势压成几行字给 LLM。只给它决定参数需要的信息。"""
    counts: dict[str, int] = {}
    for u in world.my_units():
        counts[u.type] = counts.get(u.type, 0) + 1
    res = world.resources()
    units = "、".join(f"{t}×{n}" for t, n in sorted(counts.items()))
    return (f"当前局势：晶体矿 {res.minerals}，气 {res.gas}，人口 {res.supply_used}/{res.supply_cap}。"
            f"我方单位：{units or '无'}。")


class Interpreter:
    def __init__(self, registry: SkillRegistry, llm: LLMClient, vocab: Vocab, max_repairs: int = 1) -> None:
        self.registry = registry
        self.llm = llm
        self.vocab = vocab
        self.max_repairs = max_repairs
        self._system = SYSTEM_TEMPLATE.format(vocab=vocab.render())
        self._tools = registry.tool_specs()

    @property
    def system_prompt(self) -> str:
        return self._system

    def interpret(self, text: str, situation: str | None = None, world: WorldView | None = None) -> Interpretation:
        """把一句话变成指令。world 给了的话，还会跑每个技能的 check() 把可行性问题报出来。"""
        user = f"{situation}\n\n指挥官说：{text}" if situation else f"指挥官说：{text}"
        usage: dict[str, int] = {"input": 0, "output": 0}
        reply = self._call(user, usage)
        orders, errors, replies = self._validate(reply)
        rounds = 1
        while errors and rounds <= self.max_repairs:
            fix_prompt = (f"{user}\n\n你上一次的调用有问题：\n" + "\n".join(f"- {e}" for e in errors)
                          + "\n请修正后重新给出全部工具调用。")
            reply = self._call(fix_prompt, usage)
            orders, errors, replies = self._validate(reply)
            rounds += 1
        if world is not None:
            for o in orders:
                skill = self.registry.instantiate(o.skill, o.params)
                for problem in skill.check(world):
                    errors.append(f"{o.skill}：{problem}")
        return Interpretation(text=text, orders=orders, reply=replies.strip(), errors=errors, usage=usage, rounds=rounds)

    # ---------- 内部 ----------

    def _call(self, user: str, usage: dict[str, int]) -> LLMReply:
        reply = self.llm.complete(self._system, user, self._tools)
        usage["input"] += reply.usage.get("input", 0)
        usage["output"] += reply.usage.get("output", 0)
        return reply

    def _validate(self, reply: LLMReply) -> tuple[list[Order], list[str], str]:
        orders: list[Order] = []
        errors: list[str] = []
        for call in reply.tool_calls:
            if call.arguments is None:
                errors.append(f"{call.name} 的参数不是合法 JSON：{call.raw_arguments[:120]}")
                continue
            if call.name not in self.registry.names():
                errors.append(f"没有叫 {call.name} 的技能，可用：{', '.join(self.registry.names())}")
                continue
            try:
                self.registry.instantiate(call.name, call.arguments)
            except (TypeError, ValueError, KeyError) as e:
                errors.append(f"{call.name} 参数不合法：{e}")
                continue
            orders.append(Order(call.name, call.arguments))
        return orders, errors, reply.text


def instantiate_orders(registry: SkillRegistry, orders: list[Order]) -> list[Skill[Any]]:
    return [registry.instantiate(o.skill, o.params) for o in orders]
