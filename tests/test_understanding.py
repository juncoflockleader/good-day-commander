"""理解层：用假 LLM 测解析、校验、修复轮和可行性检查，不联网。"""
from __future__ import annotations

import json
import unittest

from ai_rts.llm import LLMReply, ToolCall
from ai_rts.sandbox import build_scenario
from ai_rts.skills import default_registry
from ai_rts.understanding import Interpreter, describe_situation
from ai_rts.vocab import for_game


class FakeLLM:
    name = "fake"

    def __init__(self, *replies: LLMReply) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, list]] = []

    def complete(self, system, user, tools):
        self.calls.append((system, user, tools))
        return self.replies.pop(0)


def make(*replies):
    reg = default_registry()
    llm = FakeLLM(*replies)
    return Interpreter(reg, llm, for_game("sandbox")), llm, reg


class UnderstandingTest(unittest.TestCase):
    def test_tool_calls_become_orders(self):
        interp, llm, reg = make(LLMReply(
            text="收到，两个金甲空投对面矿区。",
            tool_calls=[ToolCall("drop_harass", {"cargo": {"specs": [{"type": "GOLIATH", "count": 2}]},
                                                 "target": "enemy_mineral_line"})]))
        r = interp.interpret("空投金甲偷矿")
        self.assertEqual([o.skill for o in r.orders], ["drop_harass"])
        self.assertEqual(r.errors, [])
        self.assertEqual(r.rounds, 1)
        self.assertIn("收到", r.reply)
        self.assertTrue(r.orders[0].as_arg().startswith("drop_harass={"))
        reg.instantiate(r.orders[0].skill, json.loads(r.orders[0].as_arg().split("=", 1)[1]))
        self.assertEqual(llm.calls[0][2][0]["name"], "scout")        # 工具表来自注册表
        self.assertIn("金甲", llm.calls[0][0])                         # 词表进了系统提示

    def test_invalid_arguments_get_one_repair_round(self):
        bad = LLMReply(text="", tool_calls=[ToolCall("scout", {"target": {"hour": 9, "minute": 30}})])
        good = LLMReply(text="改好了。", tool_calls=[ToolCall("scout", {"target": {"hour": 9}})])
        interp, llm, _ = make(bad, good)
        r = interp.interpret("一个农民去侦查9点钟")
        self.assertEqual(r.rounds, 2)
        self.assertEqual(len(r.orders), 1)
        self.assertEqual(r.errors, [])
        self.assertIn("参数不合法", llm.calls[1][1])

    def test_unknown_skill_and_bad_json_are_reported(self):
        interp, _, _ = make(
            LLMReply(text="", tool_calls=[ToolCall("nuke", {}), ToolCall("scout", None, "{broken")]),
            LLMReply(text="", tool_calls=[ToolCall("nuke", {})]))
        r = interp.interpret("核弹")
        self.assertEqual(r.orders, [])
        self.assertTrue(any("没有叫 nuke" in e for e in r.errors))

    def test_world_check_surfaces_feasibility(self):
        world = build_scenario()
        for u in list(world.my_units("DROPSHIP")):
            del world.units[u.id]
        interp, _, _ = make(LLMReply(text="", tool_calls=[ToolCall("drop_harass", {})]))
        r = interp.interpret("空投偷矿", situation=describe_situation(world), world=world)
        self.assertEqual(len(r.orders), 1)
        self.assertTrue(any("运输机" in e for e in r.errors))

    def test_no_tool_calls_is_a_plain_reply(self):
        interp, _, _ = make(LLMReply(text="没有运输机，先造一架？", tool_calls=[]))
        r = interp.interpret("空投")
        self.assertEqual(r.orders, [])
        self.assertIn("运输机", r.reply)


if __name__ == "__main__":
    unittest.main()
