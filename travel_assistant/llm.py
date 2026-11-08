from __future__ import annotations

import json
import os
import re
from typing import Any, Awaitable, Callable, Protocol

DeltaCallback = Callable[[str], Awaitable[None]]

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class LLMConfigError(RuntimeError):
    pass


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

        stream = await self._client.chat.completions.create(stream=True, **kwargs)
        parts: list[str] = []
        async for chunk in stream:
            if not chunk.choices:
                continue
            text = chunk.choices[0].delta.content
            if text:
                parts.append(text)
                await on_delta(text)
        return "".join(parts)

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
