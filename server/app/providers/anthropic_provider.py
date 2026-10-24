from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from app.config import get_settings
from app.providers.base import (
    ChatMessage,
    Done,
    Progress,
    Provider,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolSpec,
    UsageEvent,
)

# Claude models that accept output_config.effort: Opus 4.5 and every 4.6+ / 5.x model.
EFFORT_MODELS = re.compile(r"claude-(opus-4-5|[a-z]+-4-[6-9]|[a-z]+-[5-9])")


def to_anthropic_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Convert to Anthropic blocks; tool results become user turns and same-role turns are merged."""
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        if m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for c in m.tool_calls:
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
            push("assistant", blocks or [{"type": "text", "text": "(no content)"}])
        elif m.role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content or "(empty)"}])
        else:
            blocks = [
                {"type": "image", "source": {"type": "base64", "media_type": i.mime, "data": i.data_b64}} for i in m.images
            ]
            push("user", blocks + [{"type": "text", "text": m.content or "(see attached images)"}])
    return out


class AnthropicProvider(Provider):
    def __init__(self, *, api_key: str | None, base_url: str | None) -> None:
        super().__init__(api_key=api_key, base_url=base_url)
        self.client = AsyncAnthropic(api_key=api_key, base_url=base_url or None, max_retries=2)

    async def stream(
        self, model: str, system: str, messages: list[ChatMessage], tools: list[ToolSpec], effort: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": get_settings().anthropic_max_tokens,
            "messages": to_anthropic_messages(messages),
            "stream": True,
        }
        if system:
            kwargs["system"] = system
        if effort:
            # output_config.effort (GA): low ... max; Opus 4.5 only knows low/medium/high
            if "opus-4-5" in model and effort in ("xhigh", "max"):
                effort = "high"
            kwargs["output_config"] = {"effort": effort}
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]

        input_tokens = output_tokens = 0
        stop_reason: str | None = None
        blocks: dict[int, dict[str, Any]] = {}
        thinking = 0
        try:
            stream = await self.client.messages.create(**kwargs)
            async for event in stream:
                etype = event.type
                if etype == "message_start":
                    u = event.message.usage
                    input_tokens = (
                        (u.input_tokens or 0)
                        + (getattr(u, "cache_read_input_tokens", 0) or 0)
                        + (getattr(u, "cache_creation_input_tokens", 0) or 0)
                    )
                    output_tokens = u.output_tokens or 0
                elif etype == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        blocks[event.index] = {"id": block.id, "name": block.name, "json": ""}
                elif etype == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield TextDelta(delta.text)
                    elif delta.type == "input_json_delta" and event.index in blocks:
                        slot = blocks[event.index]
                        slot["json"] += delta.partial_json
                        yield Progress("tool_args", slot["name"], len(slot["json"]))
                    elif delta.type == "thinking_delta":
                        thinking += len(getattr(delta, "thinking", "") or "")
                        yield Progress("thinking", chars=thinking)
                elif etype == "content_block_stop":
                    slot = blocks.pop(event.index, None)
                    if slot is not None:
                        try:
                            args = json.loads(slot["json"]) if slot["json"] else {}
                        except json.JSONDecodeError:
                            args = {"_raw": slot["json"]}
                        yield ToolCallEvent(ToolCall(id=slot["id"], name=slot["name"], arguments=args))
                elif etype == "message_delta":
                    if event.usage is not None and event.usage.output_tokens is not None:
                        output_tokens = event.usage.output_tokens
                    stop_reason = event.delta.stop_reason or stop_reason
        except anthropic.AnthropicError as exc:
            raise ProviderError(str(exc)) from exc

        yield UsageEvent(input_tokens=input_tokens, output_tokens=output_tokens)
        yield Done(stop_reason)

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            page = await self.client.models.list(limit=100)
        except anthropic.AnthropicError as exc:
            raise ProviderError(str(exc)) from exc
        result = []
        for m in page.data:
            item = {"id": m.id, "name": getattr(m, "display_name", None) or m.id, "chat": True, "supports_images": True,
                    "supports_tools": True, "supports_effort": bool(EFFORT_MODELS.search(m.id))}
            ctx = getattr(m, "max_input_tokens", None)
            if isinstance(ctx, int) and ctx > 0:
                item["context_length"] = ctx
            result.append(item)
        return result
