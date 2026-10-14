"""极简沙盒：确定性 2D 模拟器，实现 WorldView 和 Commander，让技能跑完整生命周期。"""
from .catalog import CATALOG, FPS
from .scenario import build_scenario
from .sim import SandboxCommander, SandboxWorld

__all__ = ["CATALOG", "FPS", "SandboxCommander", "SandboxWorld", "build_scenario"]
