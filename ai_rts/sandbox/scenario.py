"""标准测试场景：我方人族在左上，敌方神族在右下，128x128。"""
from __future__ import annotations

from ..types import NamedRegion, Point
from .sim import NEUTRAL, SandboxWorld


def build_scenario(my_minerals: int = 50) -> SandboxWorld:
    regions = {
        NamedRegion.MY_MAIN: Point(16, 16),
        NamedRegion.MY_NATURAL: Point(16, 44),
        NamedRegion.MY_THIRD: Point(44, 16),
        NamedRegion.MY_RAMP: Point(26, 26),
        NamedRegion.MY_MINERAL_LINE: Point(10, 16),
        NamedRegion.ENEMY_MAIN: Point(112, 112),
        NamedRegion.ENEMY_NATURAL: Point(112, 84),
        NamedRegion.ENEMY_THIRD: Point(84, 112),
        NamedRegion.ENEMY_RAMP: Point(102, 102),
        NamedRegion.ENEMY_MINERAL_LINE: Point(118, 118),
        NamedRegion.MAP_CENTER: Point(64, 64),
    }
    w = SandboxWorld(128, 128, me=1, races={1: "terran", 2: "protoss"}, regions=regions)
    w.minerals = {NEUTRAL: 0, 1: my_minerals, 2: 50}

    # 我方主基地：一个基地、三片矿、八个农民在采、两个金甲和一架运输机待命
    w.spawn("COMMAND_CENTER", 1, Point(16, 16))
    my_patches = [w.spawn("MINERAL_FIELD", NEUTRAL, Point(8, y)) for y in (12, 16, 20)]
    for i in range(8):
        scv = w.spawn("SCV", 1, Point(12, 12 + i))
        w.cmd.gather(scv.id, my_patches[i % 3].id)
    for y in (40, 44, 48):
        w.spawn("MINERAL_FIELD", NEUTRAL, Point(8, y))      # 自然扩张位的矿
    w.spawn("GOLIATH", 1, Point(20, 20))
    w.spawn("GOLIATH", 1, Point(21, 20))
    w.spawn("DROPSHIP", 1, Point(20, 24))

    # 敌方主基地：一个基地、三片矿、八个探机在采、四个狂热者在基地另一侧巡防
    w.spawn("NEXUS", 2, Point(112, 112))
    en_patches = [w.spawn("MINERAL_FIELD", NEUTRAL, Point(120, y)) for y in (116, 120, 124)]
    for i in range(8):
        p = w.spawn("PROBE", 2, Point(116, 114 + i))
        w.cmd.gather(p.id, en_patches[i % 3].id)
    for i in range(4):
        w.spawn("ZEALOT", 2, Point(100 + i, 118))
    w._update_vision()
    return w
