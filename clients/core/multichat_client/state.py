"""Client-side view of a session, updated from server events. Both UIs render from this."""

from __future__ import annotations

import time
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


EFFORTS = ["low", "medium", "high", "xhigh", "max"]
EFFORT_LABELS = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "X-High", "max": "Max"}
SMALL_WINDOW = 256_000  # the smaller choice offered for big models
CHOICE_ABOVE = 512_000  # models with a larger maximum get a choice: 256K or their maximum


def context_choices(context_length: int | None) -> list[int]:
    """Context windows a user can pick for a model: [256K, max] when the maximum is over 512K, else just [max]."""
    if not context_length:
        return []
    return [SMALL_WINDOW, context_length] if context_length > CHOICE_ABOVE else [context_length]


def window_label(tokens: int | None) -> str:
    """256K, 1M (1,048,576 counts as 1M), 1.5M, 2M ..."""
    if not tokens:
        return "default"
    binary = tokens % 65_536 == 0  # 262,144 = 256K, 1,048,576 = 1M; 256,000 and 128,000 stay decimal
    if tokens >= 1_000_000:
        millions = tokens / 1_048_576 if binary else tokens / 1_000_000
        return f"{millions:.2f}".rstrip("0").rstrip(".") + "M"
    return f"{tokens // 1024}K" if binary else f"{round(tokens / 1000)}K"


def choice_label(tokens: int, choices: list[int]) -> str:
    """'256K', or for the model's maximum 'Max: 1M (1,048,576 tokens)'."""
    if choices and tokens == choices[-1]:
        return f"Max: {window_label(tokens)} ({tokens:,} tokens)"
    return window_label(tokens)


@dataclass
class UsageView:
    model_id: int | None
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    estimated: bool = False


SHOW_AFTER = 3.0  # seconds of waiting before a tip appears (approvals show at once)
STALL_AFTER = 20.0  # seconds without new text, while a reply is being written, before a tip appears
STALL_HINT = ("It may be thinking or writing a long tool call; the connection is still open. "
              "Stop cancels the turn.")


def elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s} s" if s < 60 else f"{s // 60} min {s % 60:02d} s"


@dataclass
class Wait:
    """A tip about what a model is waiting for: "Beta: running `uname -a` on This PC · 12 s"."""

    model_id: int
    name: str
    color: str
    state: str
    text: str
    hint: str | None
    seconds: float

    @property
    def line(self) -> str:
        return f"{self.name}: {self.text} · {elapsed(self.seconds)}"


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
        # model id -> {state, text, hint, since, last}: what it is waiting for (server `status` events)
        self.status: dict[int, dict[str, Any]] = {}

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
        self.status = {}

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

    def participant(self, model_id: int | None) -> dict[str, Any] | None:
        return next((p for p in self.participants if p["model_id"] == model_id), None)

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

    def waits(self, now: float | None = None) -> list[Wait]:
        """Tips for long waits, in participant order: a status older than SHOW_AFTER, or a reply that stopped
        producing text for STALL_AFTER."""
        now = time.monotonic() if now is None else now
        order = {p["model_id"]: i for i, p in enumerate(self.participants)}
        out = []
        for mid, s in sorted(self.status.items(), key=lambda kv: order.get(kv[0], 999)):
            if s["state"] == "writing":
                idle = now - s["last"]
                if idle >= STALL_AFTER:
                    out.append(Wait(mid, self.name(mid), self.color(mid), "stalled", "no new output", STALL_HINT, idle))
                continue
            waited = now - s["since"]
            if waited >= SHOW_AFTER or s["state"] == "approval":
                out.append(Wait(mid, self.name(mid), self.color(mid), s["state"], s["text"] or s["state"], s["hint"], waited))
        return out

    # ---- events ----
    def apply(self, event: dict[str, Any]) -> str:
        """Apply a server event; returns its type so the UI can decide what to redraw."""
        etype = event.get("type", "")
        if etype == "turn_start":
            self.busy = True
        elif etype == "turn_end":
            self.busy = False
            self.approvals.clear()
            self.status.clear()
        elif etype == "status":
            mid, state = event.get("model_id"), event.get("state")
            if not state:
                self.status.pop(mid, None)
            else:
                now = time.monotonic()
                prev = self.status.get(mid)
                since = prev["since"] if prev and prev["state"] == state else now
                self.status[mid] = {"state": state, "text": event.get("text"), "hint": event.get("hint"),
                                    "since": since, "last": now}
        elif etype in ("message", "message_start", "message_end"):
            self._add(MessageView.from_json(event["message"]))
        elif etype == "delta":
            m = self.messages.get(event["message_id"])
            if m is not None:
                m.content += event["text"]
            if event.get("model_id") in self.status:
                self.status[event["model_id"]]["last"] = time.monotonic()
        elif etype == "tool_call":
            m = self.messages.get(event["message_id"])
            if m is not None and not any(c.get("id") == event["call"]["id"] for c in m.tool_calls):
                m.tool_calls.append(event["call"])
        elif etype == "tool_progress":
            self.progress[event["call_id"]] = self.progress.get(event["call_id"], "") + event["text"]
            if event.get("model_id") in self.status:
                self.status[event["model_id"]]["last"] = time.monotonic()
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
