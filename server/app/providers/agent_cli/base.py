"""Shared pieces for agent-CLI participants (Claude Code, Cursor, GitHub Copilot)."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.providers.base import ChatMessage


@dataclass
class CliRun:
    """Everything a driver needs to build one CLI invocation."""

    prompt: str
    phase: str  # plan | build
    model_name: str
    api_key: str | None
    resume_id: str | None
    approve_edits: str
    approve_commands: str
    bridge_available: bool  # the approval MCP bridge can run (runner workspaces)


@dataclass
class CliInvocation:
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)
    stdin: str | None = None
    use_bridge: bool = False
    notices: list[str] = field(default_factory=list)


@dataclass
class ParsedLine:
    text: str = ""  # incremental reply text
    final_text: str | None = None  # full reply (used if no incremental text arrived)
    tool_call: dict[str, Any] | None = None  # {"id", "name", "arguments"} for display
    session_id: str | None = None
    usage: dict[str, Any] | None = None  # {"input_tokens", "output_tokens", "cost"}
    error: str | None = None


class CliDriver(ABC):
    name: str
    supports_resume: bool = True
    supports_bridge: bool = False

    @abstractmethod
    def build(self, run: CliRun) -> CliInvocation: ...

    @abstractmethod
    def parse(self, line: str) -> ParsedLine: ...


def model_flag(model_name: str) -> list[str]:
    return [] if model_name in ("", "default") else ["--model", model_name]


def loads(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def render_transcript(system: str | None, messages: list[ChatMessage]) -> str:
    """Turn a per-model context into one prompt for a CLI agent (which has its own tools and history)."""
    parts = []
    if system:
        parts.append(f"<multichat_instructions>\n{system}\n</multichat_instructions>")
    history = [m for m in messages[:-1] if m.content and m.role in ("user", "assistant")]
    if history:
        lines = [f"You said earlier: {m.content}" if m.role == "assistant" else m.content for m in history]
        parts.append("<conversation_so_far>\n" + "\n\n".join(lines) + "\n</conversation_so_far>")
    if messages:
        parts.append(messages[-1].content)
    return "\n\n".join(parts)
