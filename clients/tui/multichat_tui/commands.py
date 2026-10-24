"""Slash commands: definitions (for /help, autocomplete and the ctrl+p palette) and parsing."""

from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    help: str


COMMANDS = [
    Command("new", "/new [title]", "Start a new session with the current models (ignored while this one is empty)"),
    Command("attach", "/attach <file>…|clear", "Attach documents or images to your next message"),
    Command("sessions", "/sessions", "Open another session"),
    Command("resume", "/resume", "Pick an earlier session to continue (same as /sessions)"),
    Command("clear", "/clear", "Start fresh in a new session (same as /new)"),
    Command("context", "/context", "How full each model's context window is"),
    Command("compact", "/compact", "Summarise each model's older history now (also automatic at 80%)"),
    Command("memory", "/memory [clear]", "Show (or clear) the memory every model gets; '# text' adds a line"),
    Command("models", "/models", "Choose which models join this session"),
    Command("plan", "/plan", "Switch to PLAN: ask several models, compare answers"),
    Command("build", "/build [model]", "Switch to BUILD (optionally picking the builder)"),
    Command("builder", "/builder <model>", "Pick the builder model"),
    Command("effort", "/effort <model|all> low|medium|high|xhigh|max|default", "Reasoning effort per model (any time)"),
    Command("window", "/window <model|all> 256k|max", "Context window for models over 512K: 256K or their max (only before the first message)"),
    Command("mode", "/mode isolated|shared|relay", "Parallel and independent, parallel seeing earlier replies, or one after another"),
    Command("to", "/to <model,...|all>", "Send the next messages only to these models"),
    Command("folder", "/folder none|here|<path>|ssh:<conn>:<path>", "Attach a folder (this PC's runner, SSH) or none"),
    Command("approve", "/approve edits|cmds auto|ask", "Whether edits / commands need your approval"),
    Command("vote", "/vote <#n|up|down> [up|down|clear]", "Vote on a reply (/vote up = latest in this tab)"),
    Command("quickvote", "/quickvote on|off", "Show a 1..n upvote bar after each PLAN turn"),
    Command("revert", "/revert", "Undo the file changes of the last turn"),
    Command("stop", "/stop", "Stop the running turn"),
    Command("cost", "/cost", "What this session cost, per model (tokens, prices, input/output cost)"),
    Command("stats", "/stats", "Price and performance per model"),
    Command("endpoints", "/endpoints [check]", "Endpoints, key status and models on the server (check = re-check now)"),
    Command("rename", "/rename <title>", "Rename this session"),
    Command("help", "/help", "Keys and commands"),
    Command("quit", "/quit", "Exit"),
]
BY_NAME = {c.name: c for c in COMMANDS}


def parse(text: str) -> tuple[str, list[str]] | None:
    """'/vote 12 up' -> ('vote', ['12', 'up']); returns None for normal messages."""
    if not text.startswith("/") or text.startswith("//"):
        return None
    try:
        parts = shlex.split(text[1:])
    except ValueError:
        parts = text[1:].split()
    if not parts:
        return None
    return parts[0].lower(), parts[1:]


def complete(prefix: str) -> list[Command]:
    p = prefix.lstrip("/").lower()
    return [c for c in COMMANDS if c.name.startswith(p)]


def match_model(name: str, models: list[dict]) -> dict | None:
    """Find a model by id, display name or vendor model name (case-insensitive, prefix allowed)."""
    key = name.strip().lower()
    if key.isdigit():
        return next((m for m in models if m["id"] == int(key)), None)
    for field in ("display_name", "model_name"):
        exact = [m for m in models if m[field].lower() == key]
        if exact:
            return exact[0]
    prefix = [m for m in models if m["display_name"].lower().startswith(key) or m["model_name"].lower().startswith(key)]
    if len(prefix) == 1:
        return prefix[0]
    contains = [m for m in models if key in m["display_name"].lower()]
    return contains[0] if len(contains) == 1 else None
