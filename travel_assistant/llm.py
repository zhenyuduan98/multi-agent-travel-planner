from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

if TYPE_CHECKING:
    from travel_assistant.tools import Source, ToolBox

DeltaCallback = Callable[[str], Awaitable[None]]
ToolEventCallback = Callable[[str, dict[str, Any], "ToolCallRecord"], Awaitable[None]]

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class LLMConfigError(RuntimeError):
    pass


@dataclass
class ToolCallRecord:
    name: str
    arguments: str
    ok: bool
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": self.arguments, "ok": self.ok, "summary": self.summary}


@dataclass
class AgentResult:
    """带工具的子 Agent 一次执行的结果：最终文本 + 调用过哪些工具 + 收集到的来源。"""

    text: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    sources: list["Source"] = field(default_factory=list)


class LLM(Protocol):
    model: str

    async def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        on_delta: DeltaCallback | None = None,
    ) -> str: ...

    async def run_agent(
        self,
        system: str,
        user: str,
        tools: "ToolBox",
        *,
        max_rounds: int = 6,
        on_delta: DeltaCallback | None = None,
        on_tool: ToolEventCallback | None = None,
        require_tool: bool = True,
    ) -> AgentResult: ...

    async def aclose(self) -> None: ...


class QwenLLM:
    """DashScope 的 OpenAI 兼容接口。传入 on_delta 时以流式方式返回文本增量。"""

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.3,
    ) -> None:
        from openai import AsyncOpenAI

        api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            raise LLMConfigError("需要 DASHSCOPE_API_KEY（系统环境变量、.env 或 shell 导出）。")
        self.model = model or os.getenv("QWEN_DEFAULT_MODEL", "qwen-plus")
        self.temperature = temperature
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or os.getenv("QWEN_BASE_URL", DEFAULT_BASE_URL),
        )

    async def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        on_delta: DeltaCallback | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        if on_delta is None:
            response = await self._client.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""

        text, _ = await self._stream(kwargs, on_delta)
        return text

    async def _stream(
        self, kwargs: dict[str, Any], on_delta: DeltaCallback | None
    ) -> tuple[str, list[dict[str, str]]]:
        """流式请求，返回 (文本, 工具调用列表)。工具调用的参数是分片到达的，按 index 拼接。"""

        stream = await self._client.chat.completions.create(stream=True, **kwargs)
        parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                parts.append(delta.content)
                if on_delta is not None:
                    await on_delta(delta.content)
            for tc in delta.tool_calls or []:
                slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                slot["id"] += tc.id or ""
                if tc.function is not None:
                    slot["name"] += tc.function.name or ""
                    slot["arguments"] += tc.function.arguments or ""
        return "".join(parts), [calls[i] for i in sorted(calls)]

    async def run_agent(
        self,
        system: str,
        user: str,
        tools: "ToolBox",
        *,
        max_rounds: int = 6,
        on_delta: DeltaCallback | None = None,
        on_tool: ToolEventCallback | None = None,
        require_tool: bool = True,
    ) -> AgentResult:
        """工具调用循环：模型可多轮、并行地调用工具，直到给出最终回答或达到轮数上限。

        require_tool=True 时第一轮强制调用工具，避免模型跳过核实直接凭记忆作答。
        """

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        result = AgentResult(text="")
        seen_urls: set[str] = set()

        for round_index in range(max_rounds + 1):
            kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature}
            if round_index < max_rounds:
                kwargs["tools"] = tools.specs
                kwargs["parallel_tool_calls"] = True
                if round_index == 0 and require_tool:
                    kwargs["tool_choice"] = "required"
            text, calls = await self._stream(kwargs, on_delta)
            if not calls:
                result.text = text
                return result

            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in calls
                    ],
                }
            )
            outcomes = await asyncio.gather(*(tools.call(c["name"], c["arguments"]) for c in calls))
            for call, outcome in zip(calls, outcomes):
                record = ToolCallRecord(call["name"], call["arguments"], outcome.ok, outcome.content[:160])
                result.tool_calls.append(record)
                for source in outcome.sources:
                    if source.url not in seen_urls:
                        seen_urls.add(source.url)
                        result.sources.append(source)
                if on_tool is not None:
                    await on_tool("tool_result", {"name": call["name"], "arguments": call["arguments"]}, record)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": outcome.content})

        result.text = text
        return result

    async def aclose(self) -> None:
        await self._client.close()


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_object(text: str) -> dict[str, Any]:
    """容忍 ```json 代码块和前后多余文字，取出第一个 JSON 对象。"""

    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出里没有 JSON 对象")
    value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型输出的 JSON 顶层必须是对象")
    return value
