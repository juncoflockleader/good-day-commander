"""把技能库导出成 LLM tool 列表，并演示 JSON 参数解析回技能实例。"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ai_rts.skills import default_registry  # noqa: E402

reg = default_registry()

print("=== tool specs ===")
print(json.dumps(reg.tool_specs(), ensure_ascii=False, indent=2))

print("\n=== 别名粗匹配 ===")
for text in ("空投金甲偷矿", "一个农民去侦查9点钟", "去高地下开分基"):
    print(f"{text!r} -> {[c.name for c in reg.find(text)]}")

print("\n=== LLM 输出的 JSON 解析回参数对象 ===")
samples = [
    ("scout", {"target": {"hour": 9}}),
    ("expand", {"at": "my_natural", "transfer_workers": 4}),
    ("drop_harass", {
        "cargo": {"specs": [{"type": "GOLIATH", "count": 2}]},
        "target": "enemy_mineral_line",
        "abort": {
            "when": {"kind": "any_of", "conditions": [
                {"kind": "enemy_nearby", "radius": 8, "min_supply": 6},
                {"kind": "hp_below", "fraction": 0.3},
                {"kind": "kills", "count": 8},
            ]},
            "retreat_to": "my_main",
        },
    }),
]
for name, params in samples:
    skill = reg.instantiate(name, params)
    print(f"{name}: {skill.params}")

print("\n=== 错误参数会被拒绝 ===")
try:
    reg.instantiate("scout", {"target": {"hour": 9, "minute": 30}})
except (TypeError, ValueError) as e:
    print("ok:", e)
