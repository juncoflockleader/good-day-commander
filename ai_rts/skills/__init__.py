from ..registry import SkillRegistry
from .drop_harass import DropHarass
from .expand import Expand
from .scout import Scout

__all__ = ["DropHarass", "Expand", "Scout", "default_registry"]


def default_registry() -> SkillRegistry:
    reg = SkillRegistry()
    for cls in (Scout, Expand, DropHarass):
        reg.register(cls)
    return reg
