"""What each model is waiting for right now, published as `status` events so the apps can show a tip during long
waits ("waiting for the first words from OpenRouter · 25 s", "running `apt list` on This PC", "waiting for your
approval")."""

from __future__ import annotations

from typing import Any

from app.agent.events import bus
from app.providers.base import ToolCall

# state codes the clients understand (unknown ones are shown with their text)
QUEUED = "queued"  # relay mode: an earlier model answers first
STARTING = "starting"  # an agent CLI is being started
COMPACTING = "compacting"
WAITING = "waiting_model"  # request sent, no output yet
THINKING = "thinking"  # the model is reasoning (reasoning tokens arrive, no text yet)
WRITING = "writing"  # text is arriving; the apps watch for stalls
TOOL_ARGS = "tool_args"  # the model is writing a tool call (e.g. a whole file for write_file)
TOOL = "tool"  # a tool is running
APPROVAL = "approval"  # waiting for the user
AGENT = "agent"  # an agent CLI works on its own (its tools, its thinking)


class StatusBoard:
    def __init__(self) -> None:
        self._now: dict[tuple[int, int], dict[str, Any]] = {}

    def get(self, session_id: int, model_id: int) -> dict[str, Any] | None:
        return self._now.get((session_id, model_id))

    def set(self, session_id: int, model_id: int, state: str, text: str, hint: str | None = None) -> dict[str, Any] | None:
        """Publish a new status; returns the previous one (to restore after an approval)."""
        prev = self._now.get((session_id, model_id))
        status = {"state": state, "text": text, "hint": hint}
        self._now[(session_id, model_id)] = status
        if prev != status:
            bus.publish(session_id, {"type": "status", "model_id": model_id, **status})
        return prev

    def restore(self, session_id: int, model_id: int, prev: dict[str, Any] | None) -> None:
        if prev is None:
            self.clear(session_id, model_id)
        else:
            self.set(session_id, model_id, prev["state"], prev["text"], prev.get("hint"))

    def clear(self, session_id: int, model_id: int) -> None:
        if self._now.pop((session_id, model_id), None) is not None:
            bus.publish(session_id, {"type": "status", "model_id": model_id, "state": None, "text": None, "hint": None})


board = StatusBoard()


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_call(call: ToolCall, where: str, timeout: int | None = None) -> str:
    """One line for a tool call: "running `uname -a` on This PC (stops after 120 s)"."""
    a = call.arguments or {}
    on = f" on {where}" if where else ""
    match call.name:
        case "run_command":
            stops = f" (stops after {timeout} s)" if timeout else ""
            return f"running `{_short(a.get('command', ''), 60)}`{on}{stops}"
        case "read_file":
            return f"reading {_short(a.get('path', ''))}{on}"
        case "list_dir":
            return f"listing {_short(a.get('path') or '.')}{on}"
        case "grep":
            return f"searching files for “{_short(a.get('pattern', ''), 40)}”{on}"
        case "write_file":
            return f"writing {_short(a.get('path', ''))}{on}"
        case "edit_file":
            return f"editing {_short(a.get('path', ''))}{on}"
        case "web_fetch":
            return f"fetching {_short(a.get('url', ''))}"
        case "web_search":
            return f"searching the web for “{_short(a.get('query', ''), 50)}”"
    return f"using {call.name}"


def describe_calls(calls: list[ToolCall], where: str, timeout: int | None = None) -> tuple[str, str | None]:
    """Status text and hint for a batch of tool calls."""
    has_command = any(c.name == "run_command" for c in calls)
    hint = "The command's output appears in its tool card as it arrives; Stop cancels it." if has_command else None
    if len(calls) == 1:
        return describe_call(calls[0], where, timeout), hint
    parts = "; ".join(describe_call(c, "", timeout) for c in calls[:3])
    more = f"; +{len(calls) - 3} more" if len(calls) > 3 else ""
    on = f" on {where}" if where else ""
    return f"running {len(calls)} tools at once{on}: {parts}{more}", hint


def waiting_hint(endpoint: str, used: int, window: int | None, effort: str | None) -> str:
    parts = [f"The request went to {endpoint}; no output has arrived yet."]
    if used >= 20_000:
        share = f" ({round(100 * used / window)}% of its window)" if window else ""
        parts.append(f"It is reading ~{used:,} tokens of context{share}, which takes a while.")
    if effort:
        parts.append(f"Effort is {effort}: reasoning models think before they answer, longer at higher effort.")
    parts.append("Rate-limited requests are retried automatically. Stop cancels the turn.")
    return " ".join(parts)


def approval_text(payload: dict[str, Any]) -> str:
    if payload.get("kind") == "command":
        return f"waiting for your approval to run `{_short(payload.get('command', ''), 60)}`"
    path = payload.get("path")
    if path:
        return f"waiting for your approval to change {_short(path)}"
    return f"waiting for your approval ({payload.get('tool', 'tool')})"
