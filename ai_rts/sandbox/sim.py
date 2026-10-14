"""极简沙盒模拟器。

目的只有一个：让技能跑完完整生命周期。没有地形、没有碰撞、没有真实数值，
但移动、攻击、采矿、建造、装卸、视野、死亡归因都有，而且确定性。

每个单位有一个命令队列，每帧执行队首命令。空闲的战斗单位自动接敌；
被打的单位会拉响警报，附近空闲的友军会来打攻击者。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Iterable, Sequence

from ..types import (ClockPosition, Cost, Frame, Location, NamedRegion, NearUnit, PlayerId, Point, Resources,
                     Seconds, UnitId, UnitType)
from ..world import Death, Owner, RouteMode, ThreatEstimate
from .catalog import CATALOG, FPS, RACE_ROLES, UnitTypeInfo

NEUTRAL: PlayerId = 0
ALARM_RADIUS = 40.0
GATHER_FRAMES = 48
GATHER_AMOUNT = 8
SUPPLY_CAP = 200
SEEN_CELL = 8.0


def dist(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def step_toward(a: Point, b: Point, d: float) -> Point:
    total = dist(a, b)
    if total <= d:
        return b
    return Point(a.x + (b.x - a.x) * d / total, a.y + (b.y - a.y) * d / total)


@dataclass
class Order:
    kind: str
    point: Point | None = None
    target: UnitId | None = None
    units: tuple[UnitId, ...] = ()
    structure: str | None = None
    wait: int = 0
    auto: bool = False     # 自动接敌产生的


class SimUnit:
    """实现 UnitView。"""

    def __init__(self, uid: UnitId, info: UnitTypeInfo, owner: PlayerId, pos: Point, complete: bool = True) -> None:
        self._id = uid
        self.info = info
        self._owner = owner
        self._pos = pos
        self._hp = info.hp
        self.orders: Deque[Order] = deque()
        self.cooldown = 0
        self.in_transport: UnitId | None = None
        self.cargo_ids: list[UnitId] = []
        self.complete = complete
        self.build_progress = 0
        self.carrying = False
        self.last_attacker: UnitId | None = None

    @property
    def id(self) -> UnitId:
        return self._id

    @property
    def type(self) -> UnitType:
        return self.info.name

    @property
    def owner(self) -> PlayerId:
        return self._owner

    @property
    def pos(self) -> Point:
        return self._pos

    @property
    def hp(self) -> float:
        return self._hp

    @property
    def hp_max(self) -> float:
        return self.info.hp

    @property
    def supply(self) -> float:
        return self.info.supply

    @property
    def is_idle(self) -> bool:
        return not self.orders

    @property
    def is_flying(self) -> bool:
        return self.info.is_flying

    @property
    def is_structure(self) -> bool:
        return self.info.is_structure

    @property
    def is_complete(self) -> bool:
        return self.complete

    @property
    def cargo(self) -> tuple[UnitId, ...]:
        return tuple(self.cargo_ids)

    @property
    def order(self) -> str | None:
        return self.orders[0].kind if self.orders else None

    def has_role(self, role: str) -> bool:
        return role in self.info.roles

    def __repr__(self) -> str:
        return f"<{self.type}#{self.id} p{self.owner} ({self.pos.x:.0f},{self.pos.y:.0f}) hp{self.hp:.0f}>"


class SandboxWorld:
    """实现 WorldView，并持有整个模拟状态。"""

    def __init__(self, width: float, height: float, me: PlayerId,
                 races: dict[PlayerId, str], regions: dict[NamedRegion, Point]) -> None:
        self.width = width
        self.height = height
        self._me = me
        self.races = races
        self.regions = dict(regions)
        self._frame: Frame = 0
        self.units: dict[UnitId, SimUnit] = {}
        self.minerals: dict[PlayerId, int] = {}
        self.gas: dict[PlayerId, int] = {}
        self.deaths: list[Death] = []
        self._next_id = 1
        self._visible_enemies: set[UnitId] = set()
        self._known_enemy_structures: dict[UnitId, SimUnit] = {}
        self._last_seen: dict[tuple[int, int], Frame] = {}
        self.cmd = SandboxCommander(self)

    # ---------- 搭场景 ----------

    def spawn(self, type_name: str, owner: PlayerId, pos: Point, complete: bool = True) -> SimUnit:
        info = CATALOG[type_name]
        u = SimUnit(self._next_id, info, owner, pos, complete=complete or not info.is_structure or info.build_time == 0)
        self._next_id += 1
        self.units[u.id] = u
        self.minerals.setdefault(owner, 0)
        self.gas.setdefault(owner, 0)
        return u

    # ---------- 推进 ----------

    def step(self) -> None:
        for u in list(self.units.values()):
            if u.id not in self.units:
                continue
            if u.in_transport is not None:
                t = self.units.get(u.in_transport)
                if t is not None:
                    u._pos = t.pos
                continue
            if u.cooldown > 0:
                u.cooldown -= 1
            if u.info.is_structure and not u.complete:
                u.build_progress += 1
                if u.build_progress >= u.info.build_time:
                    u.complete = True
                continue
            self._auto_acquire(u)
            self._run_order(u)
        self._collect_deaths()
        self._update_vision()
        self._frame += 1

    def run(self, frames: int, each_frame: Callable[[], None] | None = None) -> None:
        for _ in range(frames):
            self.step()
            if each_frame is not None:
                each_frame()

    # ---------- 单位行为 ----------

    def _can_target(self, u: SimUnit, v: SimUnit) -> bool:
        if u.info.damage <= 0:
            return False
        return u.info.can_attack_air if v.is_flying else u.info.can_attack_ground

    def _hostiles(self, u: SimUnit, radius: float) -> list[SimUnit]:
        out = []
        for v in self.units.values():
            if v.owner in (u.owner, NEUTRAL) or v.in_transport is not None:
                continue
            if self._can_target(u, v) and dist(u.pos, v.pos) <= radius:
                out.append(v)
        return out

    def _nearest_hostile(self, u: SimUnit, radius: float) -> SimUnit | None:
        hs = self._hostiles(u, radius)
        return min(hs, key=lambda v: dist(u.pos, v.pos)) if hs else None

    def _auto_acquire(self, u: SimUnit) -> None:
        if not u.info.aggressive or u.orders:
            return
        target = self._nearest_hostile(u, u.info.sight)
        if target is not None:
            u.orders.appendleft(Order("attack", target=target.id, auto=True))

    def _advance(self, u: SimUnit, point: Point, eps: float) -> bool:
        if u.info.speed > 0:
            p = step_toward(u.pos, point, u.info.speed / FPS)
            u._pos = Point(min(max(p.x, 0.0), self.width), min(max(p.y, 0.0), self.height))
        return dist(u.pos, point) <= eps

    def _engage(self, u: SimUnit, v: SimUnit) -> None:
        if dist(u.pos, v.pos) <= u.info.range + 0.5:
            if u.cooldown == 0:
                self._hit(u, v)
                u.cooldown = u.info.cooldown
        else:
            self._advance(u, v.pos, u.info.range)

    def _hit(self, attacker: SimUnit, victim: SimUnit) -> None:
        victim._hp -= attacker.info.damage
        victim.last_attacker = attacker.id
        self._alarm(victim, attacker)

    def _alarm(self, victim: SimUnit, attacker: SimUnit) -> None:
        if victim.owner == NEUTRAL:
            return
        for w in self.units.values():
            if (w.owner == victim.owner and w.info.aggressive and not w.info.is_structure
                    and w.in_transport is None and not w.orders
                    and dist(w.pos, victim.pos) <= ALARM_RADIUS and self._can_target(w, attacker)):
                w.orders.append(Order("attack", target=attacker.id, auto=True))

    def _run_order(self, u: SimUnit) -> None:
        if not u.orders:
            return
        o = u.orders[0]
        k = o.kind
        if k in ("move", "patrol"):
            if o.point is not None and self._advance(u, o.point, 0.5):
                u.orders.popleft()
        elif k == "attack_move":
            hostile = self._nearest_hostile(u, u.info.sight)
            if hostile is not None:
                self._engage(u, hostile)
            elif o.point is not None and self._advance(u, o.point, 0.5):
                u.orders.popleft()
        elif k == "attack":
            v = self.units.get(o.target) if o.target is not None else None
            if v is None or v.in_transport is not None or not self._can_target(u, v):
                u.orders.popleft()
                return
            self._engage(u, v)
        elif k == "hold":
            return
        elif k == "gather":
            self._gather(u, o)
        elif k == "build":
            self._build(u, o)
        elif k == "building":
            s = self.units.get(o.target) if o.target is not None else None
            if s is None or s.complete:
                u.orders.popleft()
        elif k == "train":
            o.wait += 1
            if o.structure is not None and o.wait >= CATALOG[o.structure].build_time:
                self.spawn(o.structure, u.owner, Point(u.pos.x + 3, u.pos.y + 3))
                u.orders.popleft()
        elif k == "load":
            self._load(u, o)
        elif k == "unload":
            if o.point is not None and self._advance(u, o.point, 1.0):
                self._unload_all(u, o.point)
                u.orders.popleft()
        else:
            u.orders.popleft()

    def _gather(self, u: SimUnit, o: Order) -> None:
        if u.carrying:
            halls = [h for h in self.units.values()
                     if h.owner == u.owner and h.has_role("town_hall") and h.complete]
            if not halls:
                return
            th = min(halls, key=lambda h: dist(u.pos, h.pos))
            if self._advance(u, th.pos, 3.0):
                self.minerals[u.owner] = self.minerals.get(u.owner, 0) + GATHER_AMOUNT
                u.carrying = False
            return
        res = self.units.get(o.target) if o.target is not None else None
        if res is None:
            u.orders.popleft()
            return
        if self._advance(u, res.pos, 1.5):
            o.wait += 1
            if o.wait >= GATHER_FRAMES:
                o.wait = 0
                u.carrying = True

    def _build(self, u: SimUnit, o: Order) -> None:
        if o.point is None or o.structure is None or not self._advance(u, o.point, 2.5):
            return
        info = CATALOG[o.structure]
        if self.minerals.get(u.owner, 0) < info.cost.minerals or not self.can_place(o.structure, o.point):
            u.orders.popleft()
            return
        self.minerals[u.owner] -= info.cost.minerals
        self.gas[u.owner] = self.gas.get(u.owner, 0) - info.cost.gas
        s = self.spawn(o.structure, u.owner, o.point, complete=False)
        u.orders[0] = Order("building", target=s.id)

    def _load(self, u: SimUnit, o: Order) -> None:
        pending = [self.units[i] for i in o.units if i in self.units and self.units[i].in_transport is None]
        if not pending or len(u.cargo_ids) >= u.info.cargo_slots:
            u.orders.popleft()
            return
        v = min(pending, key=lambda x: dist(u.pos, x.pos))
        if self._advance(u, v.pos, 2.0):
            v.in_transport = u.id
            v.orders.clear()
            v._pos = u.pos
            u.cargo_ids.append(v.id)

    def _unload_all(self, u: SimUnit, point: Point) -> None:
        for i, cid in enumerate(u.cargo_ids):
            v = self.units.get(cid)
            if v is None:
                continue
            v.in_transport = None
            v._pos = Point(point.x + (i % 2) * 1.5 - 0.75, point.y + (i // 2) * 1.5 - 0.75)
        u.cargo_ids.clear()

    # ---------- 死亡与视野 ----------

    def _kill(self, u: SimUnit, killer: UnitId | None) -> None:
        if u.id not in self.units:
            return
        k = self.units.get(killer) if killer is not None else None
        self.deaths.append(Death(self._frame, u.id, u.owner, u.type, killer, k.owner if k else None))
        del self.units[u.id]
        self._known_enemy_structures.pop(u.id, None)
        if u.in_transport is not None:
            t = self.units.get(u.in_transport)
            if t is not None and u.id in t.cargo_ids:
                t.cargo_ids.remove(u.id)
        for cid in list(u.cargo_ids):
            c = self.units.get(cid)
            if c is not None:
                self._kill(c, killer)

    def _collect_deaths(self) -> None:
        for u in [u for u in self.units.values() if u.hp <= 0]:
            self._kill(u, u.last_attacker)

    def _my_eyes(self) -> list[SimUnit]:
        return [m for m in self.units.values() if m.owner == self._me and m.in_transport is None and m.info.sight > 0]

    def _update_vision(self) -> None:
        eyes = self._my_eyes()
        visible: set[UnitId] = set()
        for e in self.units.values():
            if e.owner in (self._me, NEUTRAL) or e.in_transport is not None:
                continue
            if any(dist(m.pos, e.pos) <= m.info.sight for m in eyes):
                visible.add(e.id)
                if e.info.is_structure:
                    self._known_enemy_structures[e.id] = e
        self._visible_enemies = visible
        for m in eyes:
            r = int(m.info.sight / SEEN_CELL) + 1
            cx, cy = int(m.pos.x / SEEN_CELL), int(m.pos.y / SEEN_CELL)
            for gx in range(cx - r, cx + r + 1):
                for gy in range(cy - r, cy + r + 1):
                    centre = Point((gx + 0.5) * SEEN_CELL, (gy + 0.5) * SEEN_CELL)
                    if dist(m.pos, centre) <= m.info.sight:
                        self._last_seen[(gx, gy)] = self._frame

    # ---------- WorldView ----------

    @property
    def frame(self) -> Frame:
        return self._frame

    @property
    def seconds(self) -> Seconds:
        return self._frame / FPS

    @property
    def me(self) -> PlayerId:
        return self._me

    def resolve(self, loc: Location) -> Point:
        if isinstance(loc, Point):
            return loc
        if isinstance(loc, NamedRegion):
            if loc is NamedRegion.MY_ARMY:
                army = [u for u in self.my_units("army")]
                return _centroid(army) or self.regions[NamedRegion.MY_MAIN]
            if loc is NamedRegion.ENEMY_ARMY:
                army = [u for u in self.enemies_visible() if u.has_role("army")]
                c = _centroid(army)
                return c or self.regions.get(loc) or self.regions[NamedRegion.ENEMY_MAIN]
            if loc not in self.regions:
                raise ValueError(f"这张图没有定义区域 {loc.value}")
            return self.regions[loc]
        if isinstance(loc, ClockPosition):
            angle = math.radians((loc.hour % 12) * 30)
            r = 0.4 * min(self.width, self.height)
            return Point(self.width / 2 + r * math.sin(angle), self.height / 2 - r * math.cos(angle))
        if isinstance(loc, NearUnit):
            u = self.unit(loc.unit)
            if u is None:
                raise ValueError(f"单位 {loc.unit} 不存在或不可见")
            return u.pos
        raise TypeError(f"不认识的位置 {loc!r}")

    def unit(self, uid: UnitId) -> SimUnit | None:
        u = self.units.get(uid)
        if u is None:
            return None
        if u.owner in (self._me, NEUTRAL) or uid in self._visible_enemies:
            return u
        return None

    def my_units(self, type_or_role: UnitType | None = None) -> Sequence[SimUnit]:
        return [u for u in self.units.values() if u.owner == self._me
                and (type_or_role is None or u.type == type_or_role or u.has_role(type_or_role))]

    def enemies_visible(self, near: Point | None = None, radius: float | None = None) -> Sequence[SimUnit]:
        out = []
        for uid in self._visible_enemies:
            u = self.units.get(uid)
            if u is None:
                continue
            if near is not None and radius is not None and dist(near, u.pos) > radius:
                continue
            out.append(u)
        return out

    def enemy_structures_known(self) -> Sequence[SimUnit]:
        return list(self._known_enemy_structures.values())

    def units_near(self, pos: Point, radius: float, owner: Owner = "any") -> Sequence[SimUnit]:
        out = []
        for u in self.units.values():
            if u.in_transport is not None or dist(pos, u.pos) > radius:
                continue
            if owner == "me" and u.owner != self._me:
                continue
            if owner == "neutral" and u.owner != NEUTRAL:
                continue
            if owner == "enemy" and (u.owner in (self._me, NEUTRAL) or u.id not in self._visible_enemies):
                continue
            if owner == "any" and u.owner not in (self._me, NEUTRAL) and u.id not in self._visible_enemies:
                continue
            out.append(u)
        return out

    def resources(self) -> Resources:
        used = sum(u.supply for u in self.units.values() if u.owner == self._me and not u.is_structure)
        return Resources(self.minerals.get(self._me, 0), self.gas.get(self._me, 0), int(used), SUPPLY_CAP)

    def type_for(self, type_or_role: UnitType, owner: PlayerId | None = None) -> str:
        if type_or_role in CATALOG:
            return type_or_role
        race = self.races[self._me if owner is None else owner]
        t = RACE_ROLES.get(race, {}).get(type_or_role)
        if t is None:
            raise ValueError(f"{race} 没有角色 {type_or_role}")
        return t

    def cost(self, type_or_role: UnitType) -> Cost:
        return CATALOG[self.type_for(type_or_role)].cost

    def speed(self, type_or_role: UnitType) -> float:
        return CATALOG[self.type_for(type_or_role)].speed

    def is_visible(self, pos: Point) -> bool:
        return any(dist(m.pos, pos) <= m.info.sight for m in self._my_eyes())

    def last_seen(self, pos: Point) -> Frame | None:
        return self._last_seen.get((int(pos.x / SEEN_CELL), int(pos.y / SEEN_CELL)))

    def threat_at(self, pos: Point, radius: float) -> ThreatEstimate:
        count, supply, aa, det = 0, 0.0, False, False
        for u in self.enemies_visible(near=pos, radius=radius):
            if u.has_role("worker") or u.info.damage <= 0:
                continue
            count += 1
            supply += u.supply
            aa = aa or u.info.can_attack_air
            det = det or u.info.is_detector
        return ThreatEstimate(count, supply, aa, det)

    def distance(self, a: Point, b: Point) -> float:
        return dist(a, b)

    def route(self, a: Point, b: Point, mode: RouteMode = "ground") -> Sequence[Point]:
        if mode != "air_edge":
            return [a, b]
        return [a, *self._edge_path(a, b), b]

    def _edge_path(self, a: Point, b: Point) -> list[Point]:
        m = 1.0
        w, h = self.width - m, self.height - m
        # 边：0 上 1 右 2 下 3 左
        def nearest_edge(p: Point) -> tuple[int, Point]:
            cands = [(0, Point(p.x, m)), (1, Point(w, p.y)), (2, Point(p.x, h)), (3, Point(m, p.y))]
            return min(cands, key=lambda c: dist(p, c[1]))
        corners = {0: {Point(m, m), Point(w, m)}, 1: {Point(w, m), Point(w, h)},
                   2: {Point(w, h), Point(m, h)}, 3: {Point(m, h), Point(m, m)}}
        ea_i, ea = nearest_edge(a)
        eb_i, eb = nearest_edge(b)
        if ea_i == eb_i:
            return [ea, eb]
        if (ea_i - eb_i) % 4 in (1, 3):
            return [ea, next(iter(corners[ea_i] & corners[eb_i])), eb]
        mid = (ea_i + 1) % 4
        return [ea, next(iter(corners[ea_i] & corners[mid])), next(iter(corners[mid] & corners[eb_i])), eb]

    def can_place(self, structure: UnitType, at: Point) -> bool:
        info = CATALOG[self.type_for(structure)]
        if not (info.footprint <= at.x <= self.width - info.footprint and info.footprint <= at.y <= self.height - info.footprint):
            return False
        for u in self.units.values():
            if u.is_structure and dist(u.pos, at) < u.info.footprint + info.footprint + 0.5:
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


def _centroid(units: Iterable[SimUnit]) -> Point | None:
    pts = [u.pos for u in units]
    if not pts:
        return None
    return Point(sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts))


class SandboxCommander:
    """实现 Commander。对已死单位的命令静默忽略，和真游戏一致。"""

    def __init__(self, world: SandboxWorld) -> None:
        self.world = world

    def _each(self, units: Iterable[UnitId]) -> list[SimUnit]:
        return [u for u in (self.world.units.get(i) for i in units) if u is not None]

    def _issue(self, units: Iterable[UnitId], order: Order, queue: bool = False) -> None:
        for u in self._each(units):
            if not queue:
                u.orders.clear()
            u.orders.append(Order(order.kind, order.point, order.target, order.units, order.structure))

    def move(self, units: Iterable[UnitId], to: Point, queue: bool = False) -> None:
        self._issue(units, Order("move", point=to), queue)

    def attack_move(self, units: Iterable[UnitId], to: Point, queue: bool = False) -> None:
        self._issue(units, Order("attack_move", point=to), queue)

    def attack(self, units: Iterable[UnitId], target: UnitId) -> None:
        self._issue(units, Order("attack", target=target))

    def patrol(self, units: Iterable[UnitId], to: Point) -> None:
        self._issue(units, Order("patrol", point=to))

    def stop(self, units: Iterable[UnitId]) -> None:
        for u in self._each(units):
            u.orders.clear()

    def hold(self, units: Iterable[UnitId]) -> None:
        self._issue(units, Order("hold"))

    def gather(self, worker: UnitId, resource: UnitId) -> None:
        self._issue([worker], Order("gather", target=resource))

    def build(self, worker: UnitId, structure: UnitType, at: Point) -> bool:
        u = self.world.units.get(worker)
        if u is None:
            return False
        t = self.world.type_for(structure, u.owner)
        if self.world.minerals.get(u.owner, 0) < CATALOG[t].cost.minerals or not self.world.can_place(t, at):
            return False
        self._issue([worker], Order("build", point=at, structure=t))
        return True

    def train(self, producer: UnitId, unit: UnitType) -> bool:
        u = self.world.units.get(producer)
        if u is None:
            return False
        t = self.world.type_for(unit, u.owner)
        if self.world.minerals.get(u.owner, 0) < CATALOG[t].cost.minerals:
            return False
        self.world.minerals[u.owner] -= CATALOG[t].cost.minerals
        self._issue([producer], Order("train", structure=t), queue=True)
        return True

    def research(self, structure: UnitId, upgrade: str) -> bool:
        return False

    def load(self, transport: UnitId, units: Iterable[UnitId]) -> None:
        self._issue([transport], Order("load", units=tuple(units)))

    def unload_all(self, transport: UnitId, at: Point) -> None:
        self._issue([transport], Order("unload", point=at))
