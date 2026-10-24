"""Deterministic offline provider for tests and key-less demos.

The reply depends only on the conversation. A user line of the form
``@tool <name> {"json": "args"}`` makes the model call that tool (if offered);
once tool results come back it summarises them.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

from app.providers.base import (
    ChatMessage,
    Done,
    Provider,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolSpec,
    UsageEvent,
    estimate_tokens,
)

TOOL_LINE = re.compile(r"^@tool\s+(\w+)\s*(\{.*\})?\s*$", re.MULTILINE)


class MockProvider(Provider):
    def __init__(self, *, api_key: str | None, base_url: str | None) -> None:
        super().__init__(api_key=api_key, base_url=base_url)
        query = parse_qs(urlparse(base_url or "").query)
        self.delay = float(query.get("delay", ["0"])[0])

    async def stream(
        self, model: str, system: str, messages: list[ChatMessage], tools: list[ToolSpec], effort: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        offered = {t.name for t in tools}
        calls: list[ToolCall] = []
        last = messages[-1] if messages else ChatMessage(role="user")

        if last.role == "tool":
            results = []
            for m in reversed(messages):
                if m.role != "tool":
                    break
                results.append(m.content.strip().splitlines()[0][:120] if m.content.strip() else "(empty)")
            text = f"[{model}] Done. Tool results: " + " | ".join(reversed(results))
        else:
            user_text = last.content
            for i, match in enumerate(TOOL_LINE.finditer(user_text)):
                name, raw = match.group(1), match.group(2)
                if name in offered:
                    calls.append(ToolCall(id=f"mock_{len(messages)}_{i}", name=name, arguments=json.loads(raw or "{}")))
            others = len(re.findall(r"^\[.+? said\]:", user_text, re.MULTILINE))
            first_line = next((ln for ln in user_text.splitlines() if ln.strip() and not ln.startswith(("[", "<"))), "")
            text = f"[{model}] My take on: {first_line[:160]}"
            if others:
                text += f" (I read {others} other answer{'s' if others > 1 else ''}.)"
            if effort:
                text += f" (effort: {effort})"
            omitted = re.search(r"\[(\d+) earlier messages were left out", "\n".join(m.content or "" for m in messages))
            if omitted:
                text += f" (context: {omitted.group(1)} earlier messages left out)"
            if "Memory — standing instructions" in system:
                text += f" (memory: {system.split('Memory — standing instructions', 1)[1].count(chr(10) + '- ')} items)"
            for name in re.findall(r"Project instructions from (\S+) in the folder", system):
                text += f" (project instructions: {name})"
            if any("[Summary of the earlier conversation" in (m.content or "") for m in messages):
                text += " (working from a summary)"
            if "You are compacting a long conversation" in system:
                text = f"[{model}] Summary: the user sent {user_text.count('User:')} long messages about details."
            if last.images:
                text += f" (I see {len(last.images)} image{'s' if len(last.images) > 1 else ''}.)"
            docs = re.findall(r'<attachment name="([^"]+)"', user_text)
            if docs:
                text += " (I read " + ", ".join(docs) + ".)"
            if calls:
                text += f" Calling {', '.join(c.name for c in calls)}."

        for word in re.findall(r"\S+\s*", text):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield TextDelta(word)
        for call in calls:
            yield ToolCallEvent(call)

        prompt = system + "".join(m.content for m in messages)
        yield UsageEvent(input_tokens=estimate_tokens(prompt), output_tokens=estimate_tokens(text))
        yield Done("tool_use" if calls else "end_turn")

    async def list_models(self) -> list[dict]:
        return [{"id": "mock-alpha"}, {"id": "mock-beta"}, {"id": "mock-gamma"}]
