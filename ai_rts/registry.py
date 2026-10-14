"""技能注册表：理解层和技能库之间的桥。

- tool_specs()   把每个技能导出成 LLM tool（name / description / input_schema）
- find(text)     用别名做粗匹配，给理解层当提示，真正的意图识别还是 LLM 做
- instantiate()  把 LLM 输出的参数 JSON 变成技能实例
"""
from __future__ import annotations

from typing import Any

from .schema import build_schema, from_dict
from .skill import Skill


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, type[Skill[Any]]] = {}

    def register(self, cls: type[Skill[Any]]) -> type[Skill[Any]]:
        for attr in ("name", "description", "Params"):
            if not getattr(cls, attr, None):
                raise ValueError(f"{cls.__name__} 缺少类属性 {attr}")
        if cls.name in self._skills:
            raise ValueError(f"技能名重复：{cls.name}")
        self._skills[cls.name] = cls
        return cls

    def get(self, name: str) -> type[Skill[Any]]:
        return self._skills[name]

    def names(self) -> list[str]:
        return list(self._skills)

    def find(self, text: str) -> list[type[Skill[Any]]]:
        hits = []
        for cls in self._skills.values():
            if text == cls.name or text in cls.aliases or any(a in text for a in cls.aliases):
                hits.append(cls)
        return hits

    def schema(self, name: str) -> dict[str, Any]:
        return build_schema(self.get(name).Params)

    def tool_specs(self) -> list[dict[str, Any]]:
        specs = []
        for cls in self._skills.values():
            desc = cls.description
            if cls.aliases:
                desc += " 常见说法：" + "、".join(cls.aliases) + "。"
            specs.append({"name": cls.name, "description": desc, "input_schema": build_schema(cls.Params)})
        return specs

    def instantiate(self, name: str, params: dict[str, Any] | None = None) -> Skill[Any]:
        cls = self.get(name)
        return cls(from_dict(cls.Params, params or {}))
