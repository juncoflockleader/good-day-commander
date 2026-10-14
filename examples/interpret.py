"""一句话 → 技能调用。理解层的命令行入口。

    examples/interpret.py "空投枪兵偷矿"
    examples/interpret.py --run sandbox "一个农民去侦查9点钟，同时去高地下开分基"
    examples/interpret.py --emit "空投枪兵偷矿"        # 只输出指令行，给 docker/say.sh 用

凭证：DEEPSEEK_API_KEY 或 DEEPSEEK_API_KEY_FILE（指向存 key 的文件）；用 Claude 时 ANTHROPIC_API_KEY。
也会读仓库根目录的 .env。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_rts.arbiter import Priority  # noqa: E402
from ai_rts.llm import LLMError, load_dotenv, make_client  # noqa: E402
from ai_rts.runner import EventLog, SimpleArbiter, SimpleRunner  # noqa: E402
from ai_rts.skill import TERMINAL  # noqa: E402
from ai_rts.skills import default_registry  # noqa: E402
from ai_rts.understanding import Interpreter, describe_situation  # noqa: E402
from ai_rts.vocab import for_game  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="+", help="指挥官的话")
    ap.add_argument("--provider", default="deepseek", choices=["deepseek", "claude"])
    ap.add_argument("--model", default=None, help="deepseek-flash / deepseek-v4-pro / claude-sonnet-5-5 / claude-opus-5-5")
    ap.add_argument("--game", default="sc2", choices=["sc2", "sandbox"], help="词表按哪个游戏；--run sandbox 时自动是 sandbox")
    ap.add_argument("--run", default=None, choices=["sandbox"], help="解析完直接执行")
    ap.add_argument("--emit", action="store_true", help="只输出指令行（name={json}），其余信息走 stderr")
    ap.add_argument("--situation", default=None, help="手写一段当前局势给模型参考")
    ap.add_argument("--show-prompt", action="store_true")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    text = " ".join(args.text)
    game = "sandbox" if args.run == "sandbox" else args.game
    try:
        llm = make_client(args.provider, args.model)
    except LLMError as e:
        print(e, file=sys.stderr)
        return 2
    registry = default_registry()
    interp = Interpreter(registry, llm, for_game(game))
    if args.show_prompt:
        print(interp.system_prompt, file=sys.stderr)

    world = None
    situation = args.situation
    if args.run == "sandbox":
        from ai_rts.sandbox import build_scenario
        world = build_scenario()
        situation = situation or describe_situation(world)

    try:
        result = interp.interpret(text, situation=situation, world=world)
    except LLMError as e:
        print(e, file=sys.stderr)
        return 2

    out = sys.stderr if args.emit else sys.stdout
    print(result.summary(), file=out)
    print(f"（{llm.name} {getattr(llm, 'model', '')}，{result.rounds} 轮，tokens in {result.usage.get('input', 0)} / out {result.usage.get('output', 0)}）", file=out)
    if args.emit:
        for o in result.orders:
            print(o.as_arg())
        return 0 if result.orders and not result.errors else 1
    if args.run != "sandbox" or not result.orders:
        return 0 if not result.errors else 1

    print("\n== 沙盒执行 ==")
    log = EventLog(verbose=True)
    arbiter = SimpleArbiter(world)
    runner = SimpleRunner(world, world.cmd, arbiter, log, deaths=world.deaths)
    tasks = [runner.launch(registry.instantiate(o.skill, o.params), o.priority) for o in result.orders]
    for _ in range(24 * 180):
        world.step()
        runner.tick()
        if all(t.status in TERMINAL for t in tasks):
            break
    print()
    for t in tasks:
        r = t.result()
        print(f"{t.name}: {t.status.value}  {r.summary if r else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
