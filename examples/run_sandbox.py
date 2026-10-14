"""在沙盒里同时跑侦查、开矿、空投，把事件流打出来。这就是反馈层未来的输入。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ai_rts.arbiter import Priority  # noqa: E402
from ai_rts.runner import EventLog, SimpleArbiter, SimpleRunner  # noqa: E402
from ai_rts.sandbox import build_scenario  # noqa: E402
from ai_rts.skill import TERMINAL  # noqa: E402
from ai_rts.skills import default_registry  # noqa: E402

world = build_scenario()
log = EventLog(verbose=True)
arbiter = SimpleArbiter(world)
runner = SimpleRunner(world, world.cmd, arbiter, log, deaths=world.deaths)
reg = default_registry()

orders = [
    ("scout", {"target": {"hour": 4}}, Priority.DIRECT),            # 一个农民去侦查 4 点钟
    ("expand", {"at": "my_natural"}, Priority.PLANNED),              # 去高地下开分基
    ("drop_harass", {"target": "enemy_mineral_line"}, Priority.DIRECT),   # 空投金甲偷矿
]
tasks = []
for name, params, prio in orders:
    skill = reg.instantiate(name, params)
    est = skill.estimate(world)
    print(f"-> {name} {params}  预估: {est}")
    tasks.append(runner.launch(skill, prio))

for _ in range(24 * 180):
    world.step()
    runner.tick()
    if all(t.status in TERMINAL for t in tasks):
        break

print()
for t in tasks:
    r = t.result()
    print(f"{t.name}: {t.status.value}  {r.summary}  (耗时 {r.duration_seconds:.0f}s)")
