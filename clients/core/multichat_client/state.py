"""Client-side view of a session, updated from server events. Both UIs render from this."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MessageView:
    id: int
    seq: int
    role: str
    model_id: int | None
    content: str = ""
    status: str = "complete"
    phase: str = "plan"
    turn_id: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    recipients: list[int] | None = None
    vote: int = 0
    attachments: list[dict[str, Any]] = field(default_factory=list)  # {id, filename, mime, size, kind}
    usage: dict[str, Any] | None = None  # {input_tokens, output_tokens, cost, estimated} of this reply

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> MessageView:
        return cls(
            id=d["id"],
            seq=d.get("seq", 0),
            role=d["role"],
            model_id=d.get("model_id"),
            content=d.get("content") or "",
            status=d.get("status", "complete"),
            phase=d.get("phase", "plan"),
            turn_id=d.get("turn_id", ""),
            tool_calls=d.get("tool_calls") or [],
            tool_call_id=d.get("tool_call_id"),
            recipients=d.get("recipients"),
            vote=d.get("vote", 0),
            attachments=d.get("attachments") or [],
            usage=d.get("usage"),
        )


@dataclass
class UsageView:
    model_id: int | None
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    estimated: bool = False


class SessionState:
    def __init__(self) -> None:
        self.session: dict[str, Any] = {}
        self.models: dict[int, dict[str, Any]] = {}
        self.messages: dict[int, MessageView] = {}
        self.usage: dict[int | None, UsageView] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.progress: dict[str, str] = {}  # tool call id -> live command output
        self.results: dict[str, MessageView] = {}  # tool call id -> tool result message
        self.file_changes: list[dict[str, Any]] = []
        self.busy = False
        self.errors: list[str] = []

    # ---- loading ----
    def load(self, session: dict, messages: list[dict], usage: list[dict], models: list[dict]) -> None:
        self.session = session
        self.models = {m["id"]: m for m in models}
        self.messages = {}
        self.results = {}
        for m in messages:
            self._add(MessageView.from_json(m))
        self.usage = {
            u["model_id"]: UsageView(u["model_id"], u["input_tokens"], u["output_tokens"], u["cost"], u["estimated"])
            for u in usage
        }
        self.busy = bool(session.get("busy"))

    def _add(self, view: MessageView) -> None:
        old = self.messages.get(view.id)
        if old is not None and not view.vote:
            view.vote = old.vote
        if old is not None and view.usage is None:
            view.usage = old.usage
        self.messages[view.id] = view
        if view.role == "tool" and view.tool_call_id:
            self.results[view.tool_call_id] = view

    # ---- queries ----
    @property
    def session_id(self) -> int | None:
        return self.session.get("id")

    @property
    def participants(self) -> list[dict[str, Any]]:
        return list(self.session.get("participants") or [])

    def name(self, model_id: int | None) -> str:
        if model_id is None:
            return "You"
        m = self.models.get(model_id)
        return m["display_name"] if m else f"model {model_id}"

    def color(self, model_id: int | None) -> str:
        m = self.models.get(model_id or -1)
        return m["color"] if m else "#9ca3af"

    def ordered(self, model_id: int | None = None) -> list[MessageView]:
        """Messages in order; with model_id, that model's thread (user prompts it received + its replies)."""
        items = sorted(self.messages.values(), key=lambda m: m.id)
        if model_id is None:
            return items
        return [
            m
            for m in items
            if m.model_id == model_id or (m.role == "user" and (not m.recipients or model_id in m.recipients))
        ]

    def by_seq(self, seq: int) -> MessageView | None:
        return next((m for m in self.messages.values() if m.seq == seq), None)

    def latest_reply(self, model_id: int | None = None) -> MessageView | None:
        replies = [m for m in self.ordered(model_id) if m.role == "assistant"]
        return replies[-1] if replies else None

    @property
    def is_empty(self) -> bool:
        """No messages yet: a fresh session that "New session" can reuse."""
        return bool(self.session) and not self.messages and not self.busy

    def total_cost(self) -> float:
        return sum(u.cost for u in self.usage.values())

    # ---- events ----
    def apply(self, event: dict[str, Any]) -> str:
        """Apply a server event; returns its type so the UI can decide what to redraw."""
        etype = event.get("type", "")
        if etype == "turn_start":
            self.busy = True
        elif etype == "turn_end":
            self.busy = False
            self.approvals.clear()
        elif etype in ("message", "message_start", "message_end"):
            self._add(MessageView.from_json(event["message"]))
        elif etype == "delta":
            m = self.messages.get(event["message_id"])
            if m is not None:
                m.content += event["text"]
        elif etype == "tool_call":
            m = self.messages.get(event["message_id"])
            if m is not None and not any(c.get("id") == event["call"]["id"] for c in m.tool_calls):
                m.tool_calls.append(event["call"])
        elif etype == "tool_progress":
            self.progress[event["call_id"]] = self.progress.get(event["call_id"], "") + event["text"]
        elif etype == "tool_result":
            self._add(MessageView.from_json(event["message"]))
        elif etype == "usage":
            u = self.usage.setdefault(event["model_id"], UsageView(event["model_id"]))
            u.input_tokens += event["input_tokens"]
            u.output_tokens += event["output_tokens"]
            u.cost += event["cost"]
            u.estimated = u.estimated or event["estimated"]
            m = self.messages.get(event.get("message_id") or -1)
            if m is not None:
                mu = m.usage or {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "estimated": False}
                m.usage = {
                    "input_tokens": mu["input_tokens"] + event["input_tokens"],
                    "output_tokens": mu["output_tokens"] + event["output_tokens"],
                    "cost": mu["cost"] + event["cost"],
                    "estimated": mu["estimated"] or event["estimated"],
                }
        elif etype == "approval_required":
            self.approvals[event["call_id"]] = event
        elif etype == "approval_resolved":
            self.approvals.pop(event["call_id"], None)
        elif etype == "vote":
            m = self.messages.get(event["message_id"])
            if m is not None:
                m.vote = event["value"]
        elif etype == "session":
            self.session = event["session"]
        elif etype == "file_changed":
            self.file_changes.append(event)
        elif etype == "error":
            self.errors.append(event.get("message", "error"))
        return etype
