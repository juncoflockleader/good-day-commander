"""在真正的星际 2 里下指令。

    .venv/bin/python examples/sc2_run.py --map maps/Simple64.SC2Map \
        'scout={"target":"enemy_main","pattern":"circle"}' expand \
        'drop_harass={"cargo":{"specs":[{"type":"MARINE","count":4}]}}' \
        --spawn MEDIVAC:1,MARINE:4 --realtime

Mac 客户端跑不了 API 模式时，用 docker/run.sh 在无头 Linux 客户端里跑，参数一样。

每个位置参数是 技能名 或 技能名={参数 JSON}。--spawn 用调试接口在主基地变出单位，方便测空投。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import os  # noqa: E402

import sc2.sc2process  # noqa: E402
from sc2.bot_ai import BotAI  # noqa: E402
from sc2.data import Difficulty, Race  # noqa: E402
from sc2.ids.unit_typeid import UnitTypeId  # noqa: E402
from sc2.main import run_game  # noqa: E402
from sc2.maps import Map  # noqa: E402
from sc2.player import Bot, Computer  # noqa: E402

from ai_rts.adapters.sc2 import SC2Commander, SC2World  # noqa: E402
from ai_rts.arbiter import Priority  # noqa: E402
from ai_rts.runner import EventLog, SimpleArbiter, SimpleRunner  # noqa: E402
from ai_rts.skill import TERMINAL  # noqa: E402
from ai_rts.skills import default_registry  # noqa: E402


def install_sc2_preload() -> None:
    """只给 SC2 进程注入 LD_PRELOAD，不影响 python 自己。

    Apple Silicon 上在 Rosetta 里跑 Linux 客户端需要 docker/alignmutex.c 这个垫片，
    容器里设 SC2_LD_PRELOAD=/opt/alignmutex.so 即可。"""
    preload = os.environ.get("SC2_LD_PRELOAD")
    if not preload:
        return
    real_launch = sc2.sc2process.SC2Process._launch

    def launch(self):
        # 子进程继承 os.environ；只在 spawn 的瞬间设置，python 自己不受影响
        os.environ["LD_PRELOAD"] = preload
        try:
            return real_launch(self)
        finally:
            os.environ.pop("LD_PRELOAD", None)

    sc2.sc2process.SC2Process._launch = launch
    print(f"SC2 进程将使用 LD_PRELOAD={preload}")


def parse_order(text: str) -> tuple[str, dict]:
    if "=" in text:
        name, raw = text.split("=", 1)
        return name, json.loads(raw)
    return text, {}


class SkillBot(BotAI):
    def __init__(self, orders: list[tuple[str, dict]], spawn: dict[str, int], time_limit: float) -> None:
        super().__init__()
        self.orders = orders
        self.spawn = spawn
        self.time_limit = time_limit
        self.tasks = []
        self.done = False
        self.launched = False

    async def on_start(self) -> None:
        if self.spawn:
            await self.client.debug_create_unit(
                [[UnitTypeId[name], count, self.start_location.towards(self.game_info.map_center, 6), self.player_id]
                 for name, count in self.spawn.items()])
        self.world = SC2World(self)
        await self.world.prepare()
        self.cmd = SC2Commander(self.world)
        self.log = EventLog(verbose=True, fps=22.4)
        self.arbiter = SimpleArbiter(self.world)
        self.runner = SimpleRunner(self.world, self.cmd, self.arbiter, self.log, deaths=self.world.deaths)
        print("== 地图语义 ==")
        for k, v in self.world.regions.items():
            print(f"  {k.value:<20} ({v.x:.0f}, {v.y:.0f})")

    def launch_orders(self) -> None:
        """调试接口变出来的单位要下一步才进观测，所以不在 on_start 里下令。"""
        reg = default_registry()
        print("== 下令 ==")
        for name, params in self.orders:
            skill = reg.instantiate(name, params)
            errors = skill.check(self.world)
            est = skill.estimate(self.world)
            print(f"  {name} {params}")
            print(f"    check: {errors or 'ok'}   estimate: {est}")
            self.tasks.append(self.runner.launch(skill, Priority.DIRECT))
        self.launched = True

    async def on_unit_destroyed(self, unit_tag: int) -> None:
        if hasattr(self, "world"):
            self.world.on_unit_destroyed(unit_tag)

    async def on_step(self, iteration: int) -> None:
        if self.done:
            return
        self.world.sync()
        if not self.launched:
            if iteration < 2:
                return
            self.launch_orders()
        self.runner.tick()
        finished = all(t.status in TERMINAL for t in self.tasks)
        if finished or self.time > self.time_limit:
            self.done = True
            print("\n== 结果 ==")
            for t in self.tasks:
                r = t.result()
                if r is None:
                    print(f"  {t.name}: {t.status.value}，停在 {t.phase}（超时）")
                else:
                    print(f"  {t.name}: {t.status.value}  {r.summary}  (耗时 {r.duration_seconds:.0f}s)")
            await self.client.leave()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("orders", nargs="*", default=['scout={"target":"enemy_main","pattern":"circle"}', "expand"])
    ap.add_argument("--map", default="maps/Simple64.SC2Map")
    ap.add_argument("--race", default="Terran", choices=["Terran", "Protoss", "Zerg"])
    ap.add_argument("--enemy", default="Protoss", choices=["Terran", "Protoss", "Zerg", "Random"])
    ap.add_argument("--difficulty", default="VeryEasy")
    ap.add_argument("--spawn", default="", help="例：MEDIVAC:1,MARINE:4")
    ap.add_argument("--realtime", action="store_true", help="按正常速度跑，方便看")
    ap.add_argument("--time-limit", type=float, default=300.0, help="游戏内秒")
    ap.add_argument("--save-replay", default="", help="结束后把 replay 存到这个路径，可在正常客户端里回看")
    args = ap.parse_args()

    install_sc2_preload()
    orders = [parse_order(o) for o in args.orders]
    spawn = {}
    for item in filter(None, args.spawn.split(",")):
        name, count = item.split(":")
        spawn[name.strip().upper()] = int(count)
    map_path = pathlib.Path(args.map).resolve()
    if not map_path.exists():
        sys.exit(f"地图不存在：{map_path}")

    run_game(
        Map(map_path),
        [Bot(Race[args.race], SkillBot(orders, spawn, args.time_limit)),
         Computer(Race[args.enemy], Difficulty[args.difficulty])],
        realtime=args.realtime,
        save_replay_as=args.save_replay or None,
    )


if __name__ == "__main__":
    main()
