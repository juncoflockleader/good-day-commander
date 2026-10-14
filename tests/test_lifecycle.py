"""三个技能在沙盒里跑完整生命周期。python -m unittest 即可，不依赖 pytest。"""
from __future__ import annotations

import unittest

from ai_rts.arbiter import Priority
from ai_rts.events import EventKind
from ai_rts.runner import EventLog, SimpleArbiter, SimpleRunner
from ai_rts.sandbox import build_scenario
from ai_rts.skill import TERMINAL, SkillStatus
from ai_rts.skills import default_registry
from ai_rts.types import NamedRegion, Point

MAX_FRAMES = 24 * 180   # 三分钟游戏时间


def make() -> tuple:
    world = build_scenario()
    log = EventLog()
    arbiter = SimpleArbiter(world)
    runner = SimpleRunner(world, world.cmd, arbiter, log, deaths=world.deaths)
    return world, runner, log, default_registry()


def run_until(world, runner, done, max_frames=MAX_FRAMES) -> int:
    for _ in range(max_frames):
        world.step()
        runner.tick()
        if done():
            break
    return world.frame


class LifecycleTest(unittest.TestCase):
    def test_scout_sees_enemy_base_and_returns(self):
        world, runner, log, reg = make()
        task = runner.launch(reg.instantiate("scout", {"target": "enemy_main", "pattern": "circle"}), Priority.DIRECT)
        run_until(world, runner, lambda: task.status in TERMINAL)
        self.assertIs(task.status, SkillStatus.SUCCEEDED, task.result())
        seen = {s["type"] for s in task.result().data["seen"]}
        self.assertIn("NEXUS", seen)
        self.assertIn("PROBE", seen)
        self.assertIn("NEXUS", task.result().summary)
        phases = [e.data["phase"] for e in log.of(task.id) if e.kind is EventKind.PHASE]
        self.assertEqual(phases, ["moving", "circling", "returning"])

    def test_scout_peek_turns_around_at_target(self):
        world, runner, log, reg = make()
        task = runner.launch(reg.instantiate("scout", {"target": "enemy_main"}), Priority.DIRECT)
        run_until(world, runner, lambda: task.status in TERMINAL)
        self.assertIs(task.status, SkillStatus.SUCCEEDED, task.result())
        self.assertTrue(task.result().data["reached"])
        phases = [e.data["phase"] for e in log.of(task.id) if e.kind is EventKind.PHASE]
        self.assertEqual(phases, ["moving", "returning"])

    def test_expand_waits_for_money_then_builds(self):
        world, runner, log, reg = make()
        task = runner.launch(reg.instantiate("expand", {"at": "my_natural"}), Priority.PLANNED)
        run_until(world, runner, lambda: task.status in TERMINAL)
        self.assertIs(task.status, SkillStatus.SUCCEEDED, task.result())
        halls = world.units_near(world.resolve(NamedRegion.MY_NATURAL), 4.0, "me")
        self.assertTrue(any(h.type == "COMMAND_CENTER" and h.is_complete for h in halls))
        phases = [e.data["phase"] for e in log.of(task.id) if e.kind is EventKind.PHASE]
        self.assertEqual(phases, ["waiting_funds", "moving", "placing", "building"])

    def test_drop_harass_kills_workers_then_extracts(self):
        world, runner, log, reg = make()
        task = runner.launch(reg.instantiate("drop_harass", {}), Priority.DIRECT)
        run_until(world, runner, lambda: task.status in TERMINAL)
        self.assertIs(task.status, SkillStatus.SUCCEEDED, task.result())
        self.assertGreaterEqual(task.result().kills, 1)
        self.assertTrue(task.result().data["returned"])
        kinds = {e.kind for e in log.of(task.id)}
        self.assertIn(EventKind.KILL, kinds)
        phases = [e.data["phase"] for e in log.of(task.id) if e.kind is EventKind.PHASE]
        self.assertEqual(phases[:4], ["loading", "transit", "unloading", "harass"])
        self.assertEqual(phases[-1], "returning")

    def test_cancel_during_harass_extracts_first(self):
        world, runner, log, reg = make()
        task = runner.launch(reg.instantiate("drop_harass", {}), Priority.DIRECT)
        run_until(world, runner, lambda: task.phase == "harass")
        self.assertEqual(task.phase, "harass")
        task.cancel("玩家改主意了")
        self.assertIs(task.status, SkillStatus.RUNNING)       # 先收尾，没有立刻结束
        run_until(world, runner, lambda: task.status in TERMINAL)
        self.assertIs(task.status, SkillStatus.ABORTED, task.result())
        self.assertTrue(task.result().data["returned"])

    def test_higher_priority_steals_units(self):
        world, runner, log, reg = make()
        low = runner.launch(reg.instantiate("drop_harass", {}), Priority.STANDING)
        run_until(world, runner, lambda: low.status is SkillStatus.RUNNING)
        high = runner.launch(reg.instantiate("drop_harass", {}), Priority.DIRECT)
        run_until(world, runner, lambda: high.status is SkillStatus.RUNNING)
        self.assertIs(low.status, SkillStatus.ABORTED)
        self.assertTrue(any(e.kind is EventKind.REVOKED for e in log.of(low.id)))
        run_until(world, runner, lambda: high.status in TERMINAL)
        self.assertIn(high.status, TERMINAL)

    def test_all_three_concurrently(self):
        world, runner, log, reg = make()
        tasks = [
            runner.launch(reg.instantiate("scout", {"target": {"hour": 4}}), Priority.DIRECT),
            runner.launch(reg.instantiate("expand", {}), Priority.PLANNED),
            runner.launch(reg.instantiate("drop_harass", {}), Priority.DIRECT),
        ]
        run_until(world, runner, lambda: all(t.status in TERMINAL for t in tasks))
        for t in tasks:
            self.assertIs(t.status, SkillStatus.SUCCEEDED, (t.name, t.result()))

    def test_check_failure_is_reported_not_crashed(self):
        world, runner, log, reg = make()
        for u in list(world.my_units("DROPSHIP")):
            del world.units[u.id]
        task = runner.launch(reg.instantiate("drop_harass", {}), Priority.DIRECT)
        self.assertIs(task.status, SkillStatus.FAILED)
        self.assertIn("运输机", task.result().summary)

    def test_leased_commander_rejects_foreign_units(self):
        from ai_rts.world import NotLeased
        world, runner, log, reg = make()
        task = runner.launch(reg.instantiate("scout", {}), Priority.DIRECT)
        run_until(world, runner, lambda: task.status is SkillStatus.RUNNING)
        foreign = [u.id for u in world.my_units("GOLIATH")]
        with self.assertRaises(NotLeased):
            task.ctx.cmd.move(foreign, Point(0, 0))


if __name__ == "__main__":
    unittest.main()
