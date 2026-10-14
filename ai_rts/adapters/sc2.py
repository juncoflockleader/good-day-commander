"""星际 2 适配器：用 burnysc2 实现 WorldView 和 Commander。

用法见 examples/sc2_run.py。要点：
- 所有命令通过 python-sc2 的 Unit 方法下达，它们自动进当帧动作队列；
- 星际 2 的 API 不告诉你谁杀了谁，击杀归因是推断：谁的命令目标是死者，或谁在射程内；
- 地图语义区域在 prepare() 里算一次：主矿、自然扩张、矿区、坡口、地图中心。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

from sc2.bot_ai import BotAI
from sc2.data import Race
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit

from ..types import (ClockPosition, Cost, Frame, Location, NamedRegion, NearUnit, PlayerId, Point, Resources,
                     Seconds, UnitId, UnitType)
from ..world import Death, Owner, RouteMode, ThreatEstimate

GAME_LOOPS_PER_SECOND = 22.4
NEUTRAL_OWNER: PlayerId = 16
SEEN_CELL = 8.0
FASTER_SPEED_MULTIPLIER = 1.4

ROLE_TYPES: dict[str, set[UnitTypeId]] = {
    "worker": {UnitTypeId.SCV, UnitTypeId.PROBE, UnitTypeId.DRONE, UnitTypeId.MULE},
    "transport": {UnitTypeId.MEDIVAC, UnitTypeId.WARPPRISM, UnitTypeId.WARPPRISMPHASING, UnitTypeId.OVERLORDTRANSPORT},
    "town_hall": {UnitTypeId.COMMANDCENTER, UnitTypeId.COMMANDCENTERFLYING, UnitTypeId.ORBITALCOMMAND,
                  UnitTypeId.ORBITALCOMMANDFLYING, UnitTypeId.PLANETARYFORTRESS, UnitTypeId.NEXUS,
                  UnitTypeId.HATCHERY, UnitTypeId.LAIR, UnitTypeId.HIVE},
}
NON_ARMY: set[UnitTypeId] = {UnitTypeId.LARVA, UnitTypeId.EGG, UnitTypeId.OVERLORD, UnitTypeId.OVERSEER,
                             UnitTypeId.MULE, UnitTypeId.BROODLING, UnitTypeId.CHANGELING}
RACE_ROLES: dict[Race, dict[str, UnitTypeId]] = {
    Race.Terran: {"worker": UnitTypeId.SCV, "transport": UnitTypeId.MEDIVAC, "town_hall": UnitTypeId.COMMANDCENTER},
    Race.Protoss: {"worker": UnitTypeId.PROBE, "transport": UnitTypeId.WARPPRISM, "town_hall": UnitTypeId.NEXUS},
    Race.Zerg: {"worker": UnitTypeId.DRONE, "transport": UnitTypeId.OVERLORDTRANSPORT, "town_hall": UnitTypeId.HATCHERY},
}
UNLOAD_ABILITY: dict[UnitTypeId, AbilityId] = {
    UnitTypeId.MEDIVAC: AbilityId.UNLOADALLAT_MEDIVAC,
    UnitTypeId.WARPPRISM: AbilityId.UNLOADALLAT_WARPPRISM,
    UnitTypeId.WARPPRISMPHASING: AbilityId.UNLOADALLAT_WARPPRISM,
    UnitTypeId.OVERLORDTRANSPORT: AbilityId.UNLOADALLAT_OVERLORD,
}


def _p2(p: Point) -> Point2:
    return Point2((p.x, p.y))


def _pt(p: Point2) -> Point:
    return Point(float(p.x), float(p.y))


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


class SC2UnitView:
    """实现 UnitView，包一层 python-sc2 的 Unit。"""

    __slots__ = ("u", "_bot")

    def __init__(self, u: Unit, bot: BotAI) -> None:
        self.u = u
        self._bot = bot

    @property
    def id(self) -> UnitId:
        return self.u.tag

    @property
    def type(self) -> UnitType:
        return self.u.type_id.name

    @property
    def owner(self) -> PlayerId:
        return self.u.owner_id

    @property
    def pos(self) -> Point:
        return _pt(self.u.position)

    @property
    def hp(self) -> float:
        return self.u.health + self.u.shield

    @property
    def hp_max(self) -> float:
        return self.u.health_max + self.u.shield_max

    @property
    def supply(self) -> float:
        try:
            return float(self._bot.calculate_supply_cost(self.u.type_id))
        except Exception:
            return 0.0

    @property
    def is_idle(self) -> bool:
        return self.u.is_idle

    @property
    def is_flying(self) -> bool:
        return self.u.is_flying

    @property
    def is_structure(self) -> bool:
        return self.u.is_structure

    @property
    def is_complete(self) -> bool:
        return self.u.is_ready

    @property
    def cargo(self) -> tuple[UnitId, ...]:
        return tuple(sorted(self.u.passengers_tags))

    @property
    def order(self) -> str | None:
        return self.u.orders[0].ability.id.name if self.u.orders else None

    def has_role(self, role: str) -> bool:
        t = self.u.type_id
        if role in ROLE_TYPES:
            return t in ROLE_TYPES[role]
        if role == "army":
            return not self.u.is_structure and t not in ROLE_TYPES["worker"] and t not in NON_ARMY
        if role == "mineral":
            return self.u.is_mineral_field
        if role == "gas":
            return self.u.is_vespene_geyser
        if role == "defense":
            return self.u.is_structure and (self.u.can_attack_ground or self.u.can_attack_air)
        return False

    def __repr__(self) -> str:
        return f"<{self.type}#{self.id} p{self.owner} ({self.pos.x:.0f},{self.pos.y:.0f}) hp{self.hp:.0f}>"


class SC2World:
    """实现 WorldView。每个 on_step 先调 sync()，死亡事件从 on_unit_destroyed 转进来。"""

    def __init__(self, bot: BotAI) -> None:
        self.bot = bot
        self.deaths: list[Death] = []
        self.regions: dict[NamedRegion, Point] = {}
        self._known_structures: dict[UnitId, SC2UnitView] = {}
        self._last_seen: dict[tuple[int, int], Frame] = {}

    # ---------- 生命周期 ----------

    async def prepare(self) -> None:
        """开局算一次地图语义。要在 on_start 里 await。"""
        bot = self.bot
        start = _pt(bot.start_location)
        enemy = _pt(bot.enemy_start_locations[0])
        exps = [_pt(e) for e in bot.expansion_locations_list]

        def nearest_exp(p: Point, exclude: Iterable[Point]) -> Point:
            ex = list(exclude)
            cands = [e for e in exps if _dist(e, p) > 8 and all(_dist(e, x) > 8 for x in ex)]
            return min(cands, key=lambda e: _dist(e, p)) if cands else p

        nxt = await bot.get_next_expansion()
        my_nat = _pt(nxt) if nxt is not None else nearest_exp(start, [])
        en_nat = nearest_exp(enemy, [])
        # 只有两矿位的小图上没有三矿，退回自然扩张位，别指到对面去
        my_third = nearest_exp(my_nat, [start, my_nat, enemy, en_nat])
        en_third = nearest_exp(en_nat, [enemy, en_nat, start, my_nat])
        center = _pt(bot.game_info.map_center)
        try:
            my_ramp = _pt(bot.main_base_ramp.top_center)
        except Exception:
            my_ramp = start
        en_ramp = Point(2 * center.x - my_ramp.x, 2 * center.y - my_ramp.y)   # 按中心对称估

        def mineral_line(p: Point) -> Point:
            fields = bot.mineral_field.closer_than(12, _p2(p))
            return _pt(fields.center) if fields else p

        self.regions = {
            NamedRegion.MY_MAIN: start,
            NamedRegion.MY_NATURAL: my_nat,
            NamedRegion.MY_THIRD: my_third,
            NamedRegion.MY_RAMP: my_ramp,
            NamedRegion.MY_MINERAL_LINE: mineral_line(start),
            NamedRegion.ENEMY_MAIN: enemy,
            NamedRegion.ENEMY_NATURAL: en_nat,
            NamedRegion.ENEMY_THIRD: en_third,
            NamedRegion.ENEMY_RAMP: en_ramp,
            NamedRegion.ENEMY_MINERAL_LINE: mineral_line(enemy),
            NamedRegion.MAP_CENTER: center,
        }

    def sync(self) -> None:
        """每步调一次：记录视野历史，维护已知敌方建筑。"""
        bot = self.bot
        frame = self.frame
        for m in bot.all_own_units:
            r = int(m.sight_range / SEEN_CELL) + 1
            cx, cy = int(m.position.x / SEEN_CELL), int(m.position.y / SEEN_CELL)
            for gx in range(cx - r, cx + r + 1):
                for gy in range(cy - r, cy + r + 1):
                    centre = Point2(((gx + 0.5) * SEEN_CELL, (gy + 0.5) * SEEN_CELL))
                    if m.position.distance_to(centre) <= m.sight_range:
                        self._last_seen[(gx, gy)] = frame
        for s in bot.enemy_structures:
            self._known_structures[s.tag] = SC2UnitView(s, bot)
        present = {s.tag for s in bot.enemy_structures}
        for tag in list(self._known_structures):
            v = self._known_structures[tag]
            if tag not in present and bot.is_visible(_p2(v.pos)):
                del self._known_structures[tag]

    def on_unit_destroyed(self, tag: UnitId) -> None:
        bot = self.bot
        prev = bot._all_units_previous_map.get(tag)
        if prev is None:
            return
        killer: UnitId | None = None
        if prev.is_enemy:
            mine = list(bot._units_previous_map.values())
            for u in mine:
                if u.orders and u.orders[0].target == tag:
                    killer = u.tag
                    break
            if killer is None:
                cands = [u for u in mine
                         if (u.can_attack_air if prev.is_flying else u.can_attack_ground)
                         and u.position.distance_to(prev.position) <= max(u.ground_range, u.air_range) + 1.5]
                if cands:
                    killer = min(cands, key=lambda u: u.position.distance_to(prev.position)).tag
        self.deaths.append(Death(self.frame, tag, prev.owner_id, prev.type_id.name, killer,
                                 bot.player_id if killer is not None else None))

    # ---------- 类型 ----------

    def type_for(self, type_or_role: UnitType) -> UnitTypeId:
        roles = RACE_ROLES.get(self.bot.race, {})
        if type_or_role in roles:
            return roles[type_or_role]
        key = type_or_role.replace("_", "").upper()
        try:
            return UnitTypeId[key]
        except KeyError:
            raise ValueError(f"星际 2 里没有 {type_or_role}") from None

    def _matches(self, u: Unit, type_or_role: UnitType | None) -> bool:
        if type_or_role is None:
            return True
        v = SC2UnitView(u, self.bot)
        if v.has_role(type_or_role):
            return True
        try:
            return u.type_id is UnitTypeId[type_or_role.replace("_", "").upper()]
        except KeyError:
            return False

    # ---------- WorldView ----------

    @property
    def frame(self) -> Frame:
        return int(self.bot.state.game_loop)

    @property
    def seconds(self) -> Seconds:
        return float(self.bot.time)

    @property
    def me(self) -> PlayerId:
        return self.bot.player_id

    def resolve(self, loc: Location) -> Point:
        if isinstance(loc, Point):
            return loc
        if isinstance(loc, NamedRegion):
            if loc is NamedRegion.MY_ARMY:
                army = [v for v in self.my_units("army")]
                if army:
                    return Point(sum(v.pos.x for v in army) / len(army), sum(v.pos.y for v in army) / len(army))
                return self.regions[NamedRegion.MY_MAIN]
            if loc is NamedRegion.ENEMY_ARMY:
                army = [u for u in self.enemies_visible() if u.has_role("army")]
                if army:
                    return Point(sum(u.pos.x for u in army) / len(army), sum(u.pos.y for u in army) / len(army))
                return self.regions[NamedRegion.ENEMY_MAIN]
            if loc not in self.regions:
                raise ValueError(f"这张图没有定义区域 {loc.value}")
            return self.regions[loc]
        if isinstance(loc, ClockPosition):
            rect = self.bot.game_info.playable_area
            cx, cy = rect.x + rect.width / 2, rect.y + rect.height / 2
            r = 0.4 * min(rect.width, rect.height)
            angle = math.radians((loc.hour % 12) * 30)
            # 星际 2 的 y 轴朝上，12 点是 y 大的方向
            return Point(cx + r * math.sin(angle), cy + r * math.cos(angle))
        if isinstance(loc, NearUnit):
            u = self.unit(loc.unit)
            if u is None:
                raise ValueError(f"单位 {loc.unit} 不存在或不可见")
            return u.pos
        raise TypeError(f"不认识的位置 {loc!r}")

    def unit(self, uid: UnitId) -> SC2UnitView | None:
        u = self.bot.all_units.find_by_tag(uid)
        if u is None:
            return None
        if u.is_enemy and u.is_snapshot and not u.is_structure:
            return None
        return SC2UnitView(u, self.bot)

    def my_units(self, type_or_role: UnitType | None = None) -> Sequence[SC2UnitView]:
        return [SC2UnitView(u, self.bot) for u in self.bot.all_own_units if self._matches(u, type_or_role)]

    def enemies_visible(self, near: Point | None = None, radius: float | None = None) -> Sequence[SC2UnitView]:
        out = []
        for u in self.bot.all_enemy_units:
            if u.is_snapshot:
                continue
            if near is not None and radius is not None and u.position.distance_to(_p2(near)) > radius:
                continue
            out.append(SC2UnitView(u, self.bot))
        return out

    def enemy_structures_known(self) -> Sequence[SC2UnitView]:
        return list(self._known_structures.values())

    def units_near(self, pos: Point, radius: float, owner: Owner = "any") -> Sequence[SC2UnitView]:
        p = _p2(pos)
        groups = []
        if owner in ("me", "any"):
            groups.append(self.bot.all_own_units)
        if owner in ("enemy", "any"):
            groups.append([u for u in self.bot.all_enemy_units if not u.is_snapshot])
        if owner in ("neutral", "any"):
            groups.append(self.bot.mineral_field)
            groups.append(self.bot.vespene_geyser)
        return [SC2UnitView(u, self.bot) for g in groups for u in g if u.position.distance_to(p) <= radius]

    def resources(self) -> Resources:
        b = self.bot
        return Resources(int(b.minerals), int(b.vespene), int(b.supply_used), int(b.supply_cap))

    def cost(self, type_or_role: UnitType) -> Cost:
        t = self.type_for(type_or_role)
        c = self.bot.calculate_cost(t)
        try:
            supply = int(self.bot.calculate_supply_cost(t))
        except Exception:
            supply = 0
        return Cost(int(c.minerals), int(c.vespene), supply)

    def speed(self, type_or_role: UnitType) -> float:
        t = self.type_for(type_or_role)
        return float(self.bot.game_data.units[t.value]._proto.movement_speed) * FASTER_SPEED_MULTIPLIER

    def is_visible(self, pos: Point) -> bool:
        return bool(self.bot.is_visible(_p2(pos)))

    def last_seen(self, pos: Point) -> Frame | None:
        return self._last_seen.get((int(pos.x / SEEN_CELL), int(pos.y / SEEN_CELL)))

    def threat_at(self, pos: Point, radius: float) -> ThreatEstimate:
        count, supply, aa, det = 0, 0.0, False, False
        for v in self.enemies_visible(near=pos, radius=radius):
            u = v.u
            if v.has_role("worker") or not (u.can_attack_ground or u.can_attack_air):
                continue
            count += 1
            supply += v.supply
            aa = aa or u.can_attack_air
            det = det or u.is_detector
        return ThreatEstimate(count, supply, aa, det)

    def distance(self, a: Point, b: Point) -> float:
        return _dist(a, b)

    def route(self, a: Point, b: Point, mode: RouteMode = "ground") -> Sequence[Point]:
        if mode != "air_edge":
            return [a, b]
        rect = self.bot.game_info.playable_area
        m = 2.0
        x0, y0, x1, y1 = rect.x + m, rect.y + m, rect.x + rect.width - m, rect.y + rect.height - m

        def nearest_edge(p: Point) -> tuple[int, Point]:
            cands = [(0, Point(p.x, y1)), (1, Point(x1, p.y)), (2, Point(p.x, y0)), (3, Point(x0, p.y))]
            return min(cands, key=lambda c: _dist(p, c[1]))

        corners = {0: {Point(x0, y1), Point(x1, y1)}, 1: {Point(x1, y1), Point(x1, y0)},
                   2: {Point(x1, y0), Point(x0, y0)}, 3: {Point(x0, y0), Point(x0, y1)}}
        ea_i, ea = nearest_edge(a)
        eb_i, eb = nearest_edge(b)
        if ea_i == eb_i:
            return [a, ea, eb, b]
        if (ea_i - eb_i) % 4 in (1, 3):
            return [a, ea, next(iter(corners[ea_i] & corners[eb_i])), eb, b]
        mid = (ea_i + 1) % 4
        return [a, ea, next(iter(corners[ea_i] & corners[mid])), next(iter(corners[mid] & corners[eb_i])), eb, b]

    def can_place(self, structure: UnitType, at: Point) -> bool:
        t = self.type_for(structure)
        p = _p2(at)
        if not self.bot.in_placement_grid(p):
            return False
        radius = float(self.bot.game_data.units[t.value].footprint_radius or 1.0)
        for s in self.bot.all_units:
            if s.is_structure or s.is_mineral_field or s.is_vespene_geyser:
                if s.position.distance_to(p) < radius + (s.footprint_radius or 1.0) + 0.5:
                    return False
        return True

    def find_placement(self, structure: UnitType, near: Point) -> Point | None:
        if self.can_place(structure, near):
            return near
        for r in (2.0, 4.0, 6.0, 8.0):
            for k in range(8):
                ang = math.radians(k * 45)
                p = Point(near.x + r * math.cos(ang), near.y + r * math.sin(ang))
                if self.can_place(structure, p):
                    return p
        return None


class SC2Commander:
    """实现 Commander。python-sc2 的 Unit 方法会自动把命令排进当帧动作队列。"""

    def __init__(self, world: SC2World) -> None:
        self.world = world
        self.bot = world.bot

    def _mine(self, ids: Iterable[UnitId]) -> list[Unit]:
        own = self.bot.all_own_units
        return [u for u in (own.find_by_tag(i) for i in ids) if u is not None]

    def _any(self, uid: UnitId) -> Unit | None:
        return self.bot.all_units.find_by_tag(uid)

    def move(self, units: Iterable[UnitId], to: Point, queue: bool = False) -> None:
        for u in self._mine(units):
            u.move(_p2(to), queue=queue)

    def attack_move(self, units: Iterable[UnitId], to: Point, queue: bool = False) -> None:
        for u in self._mine(units):
            u.attack(_p2(to), queue=queue)

    def attack(self, units: Iterable[UnitId], target: UnitId) -> None:
        t = self._any(target)
        if t is None:
            return
        for u in self._mine(units):
            u.attack(t)

    def patrol(self, units: Iterable[UnitId], to: Point) -> None:
        for u in self._mine(units):
            u.patrol(_p2(to))

    def stop(self, units: Iterable[UnitId]) -> None:
        for u in self._mine(units):
            u.stop()

    def hold(self, units: Iterable[UnitId]) -> None:
        for u in self._mine(units):
            u.hold_position()

    def gather(self, worker: UnitId, resource: UnitId) -> None:
        w, r = self._mine([worker]), self._any(resource)
        if w and r is not None:
            w[0].gather(r)

    def build(self, worker: UnitId, structure: UnitType, at: Point) -> bool:
        w = self._mine([worker])
        if not w:
            return False
        t = self.world.type_for(structure)
        if not self.bot.can_afford(t):
            return False
        w[0].build(t, _p2(at))
        return True

    def train(self, producer: UnitId, unit: UnitType) -> bool:
        p = self._mine([producer])
        if not p:
            return False
        t = self.world.type_for(unit)
        if not self.bot.can_afford(t):
            return False
        p[0].train(t)
        return True

    def research(self, structure: UnitId, upgrade: str) -> bool:
        s = self._mine([structure])
        try:
            up = UpgradeId[upgrade.upper()]
        except KeyError:
            return False
        if not s or not self.bot.can_afford(up):
            return False
        s[0].research(up)
        return True

    def load(self, transport: UnitId, units: Iterable[UnitId]) -> None:
        t = self._mine([transport])
        cargo = self._mine(units)
        if not t or not cargo:
            return
        # 右键运输机就是上车；运输机同时朝部队飞，两边凑
        for u in cargo:
            u.smart(t[0])
        cx = sum(u.position.x for u in cargo) / len(cargo)
        cy = sum(u.position.y for u in cargo) / len(cargo)
        t[0].move(Point2((cx, cy)))

    def unload_all(self, transport: UnitId, at: Point) -> None:
        t = self._mine([transport])
        if not t:
            return
        ability = UNLOAD_ABILITY.get(t[0].type_id, AbilityId.UNLOADALLAT_MEDIVAC)
        t[0](ability, _p2(at))
