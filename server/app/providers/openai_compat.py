"""OpenAI and every OpenAI-compatible vendor (OpenRouter, DeepSeek, Gemini, xAI, Groq, Mistral, Ollama…)."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI

from app.providers.base import (
    ChatMessage,
    Done,
    Provider,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolSpec,
    UsageEvent,
    estimate_tokens,
)


# Vendor model ids that are not chat models (embeddings, speech, images, moderation, legacy completions).
# MultiChat's levels -> what OpenAI-style APIs accept (the top two map to "high").
EFFORT_LEVELS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
OPENAI_REASONING = re.compile(r"^(o[1-9]|gpt-5)", re.I)  # OpenAI models that take reasoning_effort
NON_CHAT = re.compile(r"embed|tts|whisper|dall-e|moderation|transcribe|audio|realtime|image|davinci|babbage|search", re.I)


def to_openai_messages(system: str, messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in m.tool_calls
                ]
            elif not m.content:
                item["content"] = ""
            out.append(item)
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        elif m.images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": m.content or "(see attached images)"}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:{i.mime};base64,{i.data_b64}"}} for i in m.images]
            out.append({"role": "user", "content": parts})
        else:
            out.append({"role": "user", "content": m.content})
    return out


def to_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in tools
    ]


def _parse_args(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"value": value}
    except json.JSONDecodeError:
        return {"_raw": raw}


class OpenAICompatProvider(Provider):
    def __init__(self, *, api_key: str | None, base_url: str | None) -> None:
        super().__init__(api_key=api_key, base_url=base_url)
        self.client = AsyncOpenAI(api_key=api_key or "not-needed", base_url=base_url or None, max_retries=2)

    @property
    def is_openrouter(self) -> bool:
        return "openrouter.ai" in (self.base_url or "")

    async def stream(
        self, model: str, system: str, messages: list[ChatMessage], tools: list[ToolSpec], effort: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": to_openai_messages(system, messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = to_openai_tools(tools)
        if self.is_openrouter:
            kwargs["extra_body"] = {"usage": {"include": True}}
        if effort:
            level = EFFORT_LEVELS.get(effort, "medium")
            if self.is_openrouter:  # OpenRouter's unified reasoning parameter, mapped per model by OpenRouter
                kwargs["extra_body"]["reasoning"] = {"effort": level}
            else:
                kwargs["reasoning_effort"] = level
        try:
            stream = await self.client.chat.completions.create(**kwargs)
        except openai.BadRequestError as exc:
            # A few compatible servers reject stream_options; retry once without it.
            if "stream_options" not in str(exc):
                raise ProviderError(str(exc)) from exc
            kwargs.pop("stream_options")
            stream = await self.client.chat.completions.create(**kwargs)
        except openai.OpenAIError as exc:
            raise ProviderError(str(exc)) from exc

        calls: dict[int, dict[str, str]] = {}
        text_len = 0
        usage: UsageEvent | None = None
        finish: str | None = None
        try:
            async for chunk in stream:
                if chunk.usage is not None:
                    extra = chunk.usage.model_extra or {}
                    cost = extra.get("cost")
                    usage = UsageEvent(
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                        cost=float(cost) if cost is not None else None,
                    )
                for choice in chunk.choices:
                    delta = choice.delta
                    if delta.content:
                        text_len += len(delta.content)
                        yield TextDelta(delta.content)
                    for tc in delta.tool_calls or []:
                        slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["args"] += tc.function.arguments
                    if choice.finish_reason:
                        finish = choice.finish_reason
        except openai.OpenAIError as exc:
            raise ProviderError(str(exc)) from exc

        for index in sorted(calls):
            slot = calls[index]
            yield ToolCallEvent(
                ToolCall(id=slot["id"] or f"call_{index}", name=slot["name"], arguments=_parse_args(slot["args"]))
            )
        if usage is None:
            prompt_chars = sum(len(m.content) for m in messages) + len(system)
            usage = UsageEvent(
                input_tokens=prompt_chars // 4,
                output_tokens=estimate_tokens("x" * text_len),
                estimated=True,
            )
        yield usage
        yield Done(finish)

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            page = await self.client.models.list()
        except openai.OpenAIError as exc:
            raise ProviderError(str(exc)) from exc
        result = []
        for m in page.data:
            extra = m.model_extra or {}
            item: dict[str, Any] = {"id": m.id, "chat": not NON_CHAT.search(m.id)}
            if extra.get("name"):
                item["name"] = str(extra["name"])
            pricing = extra.get("pricing")
            if pricing:  # OpenRouter: USD per token as strings
                try:
                    item["input_price"] = float(pricing.get("prompt", 0)) * 1_000_000
                    item["output_price"] = float(pricing.get("completion", 0)) * 1_000_000
                except (TypeError, ValueError):
                    pass
            arch = extra.get("architecture") or {}
            if arch:  # OpenRouter: modalities tell us chat/text output and image input
                item["chat"] = "text" in (arch.get("output_modalities") or ["text"])
                item["supports_images"] = "image" in (arch.get("input_modalities") or [])
            if "supported_parameters" in extra:
                params = extra.get("supported_parameters") or []
                item["supports_tools"] = "tools" in params
                item["supports_effort"] = "reasoning" in params
            elif OPENAI_REASONING.match(m.id):
                item["supports_effort"] = True
            ctx = extra.get("context_length") or (extra.get("top_provider") or {}).get("context_length")
            if isinstance(ctx, int) and ctx > 0:
                item["context_length"] = ctx
            result.append(item)
        return result

    async def check_key(self) -> None:
        if not self.is_openrouter:
            return  # /models already needs a valid key on OpenAI-style APIs
        # OpenRouter's model list is public, so ask for the key's own record instead.
        url = (self.base_url or "").rstrip("/") + "/key"
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                resp = await c.get(url, headers={"Authorization": f"Bearer {self.api_key or ''}"})
        except httpx.HTTPError as exc:
            raise ProviderError(f"cannot reach OpenRouter: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ProviderError("OpenRouter rejected the API key")
        if resp.status_code >= 400:
            raise ProviderError(f"OpenRouter key check failed: HTTP {resp.status_code}")
