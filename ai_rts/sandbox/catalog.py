"""沙盒单位表。数值只求数量级像星际 1，不求精确。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..types import Cost

FPS = 24   # 游戏帧率，星际 1 fastest 约 23.8


@dataclass(frozen=True)
class UnitTypeInfo:
    name: str
    roles: frozenset[str] = frozenset()
    hp: float = 100.0
    supply: float = 0.0
    speed: float = 0.0            # tiles / 游戏秒
    damage: float = 0.0
    cooldown: int = 22            # 帧
    range: float = 1.0
    sight: float = 7.0
    is_structure: bool = False
    is_flying: bool = False
    can_attack_ground: bool = True
    can_attack_air: bool = False
    is_detector: bool = False
    aggressive: bool = True       # 空闲时自动接敌，被打时响应警报
    build_time: int = 0           # 帧
    cost: Cost = Cost()
    cargo_slots: int = 0
    footprint: float = 0.0        # 建筑占地半径，放置检查用


def _t(name: str, **kw: Any) -> UnitTypeInfo:
    roles = frozenset(kw.pop("roles", ()))
    return UnitTypeInfo(name=name, roles=roles, **kw)


CATALOG: dict[str, UnitTypeInfo] = {u.name: u for u in [
    # 人族
    _t("SCV", roles=["worker"], hp=60, supply=1, speed=3.7, damage=5, cooldown=15, range=1, sight=7,
       aggressive=False, cost=Cost(50, 0, 1), build_time=20 * FPS),
    _t("MARINE", roles=["army"], hp=40, supply=1, speed=3.0, damage=6, cooldown=15, range=4, sight=7,
       can_attack_air=True, cost=Cost(50, 0, 1), build_time=24 * FPS),
    _t("GOLIATH", roles=["army"], hp=125, supply=2, speed=3.6, damage=12, cooldown=22, range=5, sight=8,
       can_attack_air=True, cost=Cost(100, 50, 2), build_time=40 * FPS),
    _t("DROPSHIP", roles=["transport"], hp=150, supply=2, speed=5.5, damage=0, sight=8, is_flying=True,
       can_attack_ground=False, aggressive=False, cargo_slots=4, cost=Cost(100, 100, 2), build_time=50 * FPS),
    _t("COMMAND_CENTER", roles=["town_hall"], hp=1500, is_structure=True, aggressive=False,
       can_attack_ground=False, sight=10, build_time=40 * FPS, cost=Cost(400, 0, 0), footprint=2.0),
    # 神族
    _t("PROBE", roles=["worker"], hp=40, supply=1, speed=3.7, damage=5, cooldown=22, range=1, sight=8,
       aggressive=False, cost=Cost(50, 0, 1), build_time=20 * FPS),
    _t("ZEALOT", roles=["army"], hp=160, supply=2, speed=3.0, damage=16, cooldown=22, range=1, sight=7,
       cost=Cost(100, 0, 2), build_time=40 * FPS),
    _t("DRAGOON", roles=["army"], hp=180, supply=2, speed=3.5, damage=20, cooldown=30, range=4, sight=8,
       can_attack_air=True, cost=Cost(125, 50, 2), build_time=50 * FPS),
    _t("SHUTTLE", roles=["transport"], hp=140, supply=2, speed=5.0, damage=0, sight=8, is_flying=True,
       can_attack_ground=False, aggressive=False, cargo_slots=4, cost=Cost(200, 0, 2), build_time=60 * FPS),
    _t("PHOTON_CANNON", roles=["defense"], hp=300, is_structure=True, damage=20, cooldown=22, range=7,
       sight=11, can_attack_air=True, is_detector=True, cost=Cost(150, 0, 0), build_time=50 * FPS, footprint=1.0),
    _t("NEXUS", roles=["town_hall"], hp=1750, is_structure=True, aggressive=False, can_attack_ground=False,
       sight=11, build_time=40 * FPS, cost=Cost(400, 0, 0), footprint=2.0),
    # 中立
    _t("MINERAL_FIELD", roles=["mineral"], hp=1e9, is_structure=True, aggressive=False,
       can_attack_ground=False, sight=0, footprint=1.0),
]}

RACE_ROLES: dict[str, dict[str, str]] = {
    "terran": {"worker": "SCV", "transport": "DROPSHIP", "town_hall": "COMMAND_CENTER"},
    "protoss": {"worker": "PROBE", "transport": "SHUTTLE", "town_hall": "NEXUS"},
}
