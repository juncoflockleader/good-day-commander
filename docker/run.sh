#!/bin/sh
# 在无头 Linux 客户端里跑 examples/sc2_run.py，参数原样透传。
#   docker/run.sh 'scout={"target":"enemy_main","pattern":"circle"}' expand --spawn MEDIVAC:1,MARINE:4 --save-replay replays/test.SC2Replay
# 仓库挂到 /work，maps/ 同时挂进客户端的地图目录；replay 写到 replays/。
set -e
cd "$(dirname "$0")/.."
mkdir -p replays
# Apple Silicon 上 Rosetta 跑 SC2 需要 alignmutex 垫片；x86 宿主机把 SC2_LD_PRELOAD 设成空即可
exec docker run --rm --platform linux/amd64 \
  -e SC2_LD_PRELOAD="${SC2_LD_PRELOAD-/opt/alignmutex.so}" \
  -v "$PWD:/work" \
  -v "$PWD/maps:/root/StarCraftII/maps/local" \
  -w /work \
  ai-rts-sc2 examples/sc2_run.py "$@"
