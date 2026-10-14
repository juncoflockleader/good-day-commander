"""LLM 客户端的最小抽象。

理解层只需要一件事：给系统提示、玩家的话、工具列表，拿回文本和工具调用。
两个后端：DeepSeek（OpenAI 兼容接口，走 urllib，零依赖）和 Claude（Anthropic SDK，有 key 时用）。
工具统一用注册表导出的形状：{"name", "description", "input_schema"}，各后端自己转换。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] | None     # None 表示参数不是合法 JSON
    raw_arguments: str = ""


@dataclass
class LLMReply:
    text: str
    tool_calls: list[ToolCall]
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""


class LLMClient(Protocol):
    name: str

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> LLMReply: ...


# ---------- 凭证 ----------


def load_dotenv(path: Path | str = ".env") -> None:
    """把 KEY=VALUE 行读进环境变量，已有的不覆盖。"""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def key_from_env(var: str) -> str | None:
    """先看 VAR，再看 VAR_FILE 指向的文件。文件内容允许是裸 token 或 KEY=token。"""
    direct = os.environ.get(var)
    if direct:
        return direct.strip()
    file = os.environ.get(var + "_FILE")
    if file and Path(file).is_file():
        raw = Path(file).read_text().strip()
        if "=" in raw and not raw.startswith("sk-"):
            raw = raw.split("=", 1)[1].strip()
        return raw.strip("'\"")
    return None


# ---------- DeepSeek ----------


class DeepSeekClient:
    name = "deepseek"

    def __init__(self, api_key: str | None = None, model: str = "deepseek-flash",
                 base_url: str = "https://api.deepseek.com", timeout: float = 120.0,
                 temperature: float = 0.2) -> None:
        self.api_key = api_key or key_from_env("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise LLMError("没有 DeepSeek key：设 DEEPSEEK_API_KEY，或 DEEPSEEK_API_KEY_FILE 指向存 key 的文件")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> LLMReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise LLMError(f"DeepSeek HTTP {e.code}: {e.read()[:300]!r}") from None
        except urllib.error.URLError as e:
            raise LLMError(f"DeepSeek 连不上：{e.reason}") from None
        msg = data["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = None
            calls.append(ToolCall(fn["name"], args if isinstance(args, dict) else None, raw))
        usage = data.get("usage") or {}
        return LLMReply(text=msg.get("content") or "", tool_calls=calls,
                        usage={"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)},
                        model=data.get("model", self.model))


# ---------- Claude ----------


class ClaudeClient:
    """Anthropic SDK 后端。默认 claude-sonnet-5-5；拒答时服务端按类别自动回退。"""

    name = "claude"

    def __init__(self, model: str = "claude-sonnet-5-5", effort: str = "medium", api_key: str | None = None) -> None:
        import anthropic  # 只在用到时导入
        self.model = model
        self.effort = effort
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> LLMReply:
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=4096,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools],
            tool_choice={"type": "auto"},
            output_config={"effort": self.effort},
            messages=[{"role": "user", "content": user}],
        )
        if resp.stop_reason == "refusal":
            return LLMReply(text="（模型拒绝了这条请求）", tool_calls=[], model=resp.model)
        texts, calls = [], []
        for block in resp.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(block.name, dict(block.input), json.dumps(block.input, ensure_ascii=False)))
        return LLMReply(text="\n".join(texts), tool_calls=calls,
                        usage={"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}, model=resp.model)


def make_client(provider: str, model: str | None = None, **kw: Any) -> LLMClient:
    if provider == "deepseek":
        return DeepSeekClient(model=model or "deepseek-flash", **kw)
    if provider == "claude":
        return ClaudeClient(model=model or "claude-sonnet-5-5", **kw)
    raise LLMError(f"不认识的 provider {provider!r}，可选 deepseek / claude")
