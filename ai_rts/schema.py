"""dataclass 参数 <-> JSON schema / dict。

理解层需要两个方向：把技能参数导出成 LLM tool 的 input_schema；把 LLM 输出的 JSON 解析回参数对象。
支持：标量、Literal、Enum、Union（含 Optional）、tuple/list、嵌套 dataclass、递归 dataclass。
不引第三方库。
"""
from __future__ import annotations

import collections.abc
import dataclasses
import enum
import types
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

MISSING = dataclasses.MISSING
_NONE = type(None)
_UNION_ORIGINS: tuple[Any, ...] = (Union, getattr(types, "UnionType", Union))
_SEQ_ORIGINS: tuple[Any, ...] = (list, tuple, set, frozenset, collections.abc.Sequence)


def to_jsonable(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: to_jsonable(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, enum.Enum):
        return v.value
    if isinstance(v, (list, tuple, set, frozenset)):
        return [to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): to_jsonable(x) for k, x in v.items()}
    return v


def _doc(t: type) -> str | None:
    doc = getattr(t, "__doc__", None)
    if not doc or doc.startswith(t.__name__ + "(") or doc == "An enumeration.":
        return None
    return " ".join(doc.split())


def build_schema(cls: type) -> dict[str, Any]:
    """根是展开的 object（LLM tool 要求），嵌套类型放 $defs。"""
    defs: dict[str, Any] = {}
    root = _schema(cls, defs)
    if "$ref" in root:
        root = dict(defs[root["$ref"].rsplit("/", 1)[-1]])
    if defs:
        root["$defs"] = defs
    return root


def _schema(t: Any, defs: dict[str, Any]) -> dict[str, Any]:
    origin = get_origin(t)
    if t is _NONE:
        return {"type": "null"}
    if t is bool:
        return {"type": "boolean"}
    if t is int:
        return {"type": "integer"}
    if t is float:
        return {"type": "number"}
    if t is str:
        return {"type": "string"}
    if t is Any:
        return {}
    if origin is Literal:
        return {"enum": list(get_args(t))}
    if origin in _UNION_ORIGINS:
        return {"anyOf": [_schema(a, defs) for a in get_args(t)]}
    if origin in _SEQ_ORIGINS:
        args = [a for a in get_args(t) if a is not Ellipsis]
        return {"type": "array", "items": _schema(args[0] if args else Any, defs)}
    if origin is dict or t is dict:
        return {"type": "object"}
    if isinstance(t, type) and issubclass(t, enum.Enum):
        s: dict[str, Any] = {"enum": [m.value for m in t]}
        if _doc(t):
            s["description"] = _doc(t)
        return s
    if dataclasses.is_dataclass(t):
        name = t.__name__
        if name not in defs:
            defs[name] = {}  # 占位，挡住递归
            hints = get_type_hints(t)
            props: dict[str, Any] = {}
            required: list[str] = []
            for f in dataclasses.fields(t):
                s = dict(_schema(hints[f.name], defs))
                doc = f.metadata.get("doc")
                if doc:
                    s["description"] = doc
                if f.default is not MISSING:
                    s["default"] = to_jsonable(f.default)
                elif f.default_factory is not MISSING:  # type: ignore[misc]
                    s["default"] = to_jsonable(f.default_factory())  # type: ignore[misc]
                else:
                    required.append(f.name)
                props[f.name] = s
            d: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
            if required:
                d["required"] = required
            if _doc(t):
                d["description"] = _doc(t)
            defs[name] = d
        return {"$ref": f"#/$defs/{name}"}
    raise TypeError(f"schema: 不支持的类型 {t!r}")


def from_dict(t: Any, v: Any) -> Any:
    """按类型 t 解析 v。类型不匹配抛 TypeError / ValueError，Union 靠这个逐个试。"""
    origin = get_origin(t)
    if t is Any:
        return v
    if t is _NONE:
        if v is None:
            return None
        raise TypeError(f"期望 null，得到 {v!r}")
    if t is bool:
        if isinstance(v, bool):
            return v
        raise TypeError(f"期望 bool，得到 {v!r}")
    if t is int:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not v.is_integer()):
            raise TypeError(f"期望整数，得到 {v!r}")
        return int(v)
    if t is float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError(f"期望数字，得到 {v!r}")
        return float(v)
    if t is str:
        if not isinstance(v, str):
            raise TypeError(f"期望字符串，得到 {v!r}")
        return v
    if origin is Literal:
        if v not in get_args(t):
            raise ValueError(f"{v!r} 不在 {get_args(t)} 里")
        return v
    if origin in _UNION_ORIGINS:
        members = get_args(t)
        if v is None:
            if _NONE in members:
                return None
            raise TypeError("不能为 null")
        if isinstance(v, dict) and "kind" in v:
            for m in members:
                if dataclasses.is_dataclass(m):
                    kf = {f.name: f for f in dataclasses.fields(m)}.get("kind")
                    if kf is not None and kf.default == v["kind"]:
                        return from_dict(m, v)
        errors: list[str] = []
        for m in members:
            if m is _NONE:
                continue
            try:
                return from_dict(m, v)
            except (TypeError, ValueError, KeyError) as e:
                errors.append(f"{getattr(m, '__name__', m)}: {e}")
        raise ValueError(f"{v!r} 不匹配任何一种类型。" + " | ".join(errors))
    if origin in _SEQ_ORIGINS:
        if isinstance(v, (str, bytes, dict)) or not isinstance(v, collections.abc.Iterable):
            raise TypeError(f"期望数组，得到 {v!r}")
        args = [a for a in get_args(t) if a is not Ellipsis]
        item = args[0] if args else Any
        return tuple(from_dict(item, x) for x in v)
    if origin is dict or t is dict:
        if not isinstance(v, dict):
            raise TypeError(f"期望对象，得到 {v!r}")
        return dict(v)
    if isinstance(t, type) and issubclass(t, enum.Enum):
        if isinstance(v, t):
            return v
        return t(v)
    if dataclasses.is_dataclass(t):
        if isinstance(v, t):
            return v
        if not isinstance(v, dict):
            raise TypeError(f"期望 {t.__name__} 对象，得到 {v!r}")
        hints = get_type_hints(t)
        names = {f.name for f in dataclasses.fields(t)}
        unknown = set(v) - names
        if unknown:
            raise ValueError(f"{t.__name__} 没有字段 {sorted(unknown)}")
        return t(**{k: from_dict(hints[k], x) for k, x in v.items()})
    raise TypeError(f"from_dict: 不支持的类型 {t!r}")
