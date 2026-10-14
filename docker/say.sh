#!/bin/sh
# 一句话直接在真游戏里执行。先在宿主机用 LLM 把话翻成指令，再交给 docker/run.sh。
#   docker/say.sh "空投枪兵偷矿" --spawn MEDIVAC:1,MARINE:4 --save-replay replays/say.SC2Replay
set -e
cd "$(dirname "$0")/.."
SENTENCE="$1"; shift
ORDERS=$(./.venv/bin/python examples/interpret.py --emit "$SENTENCE") || { echo "没有解析出可执行的指令" >&2; exit 1; }
while IFS= read -r line; do
  [ -n "$line" ] && set -- "$@" "$line"
done <<EOF2
$ORDERS
EOF2
exec docker/run.sh "$@"
