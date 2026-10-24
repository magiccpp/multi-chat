"""Build each participant's view of the conversation (Shared vs Isolated) and its system prompt."""

from __future__ import annotations

from dataclasses import dataclass, field

import base64
import json

from app.agent.tools import shell_note
from app.providers.base import ChatMessage, ImagePart, ToolCall
from app.tables import Message

MAX_IMAGES = 8  # most recent images sent to a model; older ones are mentioned by name only


@dataclass
class WorkspaceView:
    kind: str = "none"  # none | runner | ssh
    label: str = ""
    os: str = ""
    shell: str = ""
    root: str = ""


@dataclass
class AttachmentData:
    id: int
    filename: str
    mime: str
    kind: str  # image | document | binary
    text: str | None = None
    data: bytes | None = None


@dataclass
class ContextInput:
    model_id: int
    model_name: str
    phase: str  # plan | build
    context_mode: str  # isolated | shared (parallel, sees earlier turns) | relay (one after another)
    messages: list[Message]
    names: dict[int, str]  # model id -> display name
    votes: dict[int, int] = field(default_factory=dict)  # message id -> +1/-1
    workspace: WorkspaceView = field(default_factory=WorkspaceView)
    has_tools: bool = True
    builder_id: int | None = None
    attachments: dict[int, AttachmentData] = field(default_factory=dict)
    supports_images: bool = True
    turn_id: str = ""  # the turn being answered; in shared mode its other replies are still being written
    memory: str = ""  # the user's global memory (like ~/.claude/CLAUDE.md)
    project_memory: list[tuple[str, str]] = field(default_factory=list)  # (file name, text) from the folder


SEES_OTHERS = ("shared", "relay")


def _shell_hint(ws: WorkspaceView) -> str:
    if ws.shell.lower().startswith(("powershell", "pwsh")):
        return (
            f"{shell_note(ws.shell, ws.os)} Commands run non-interactively (stdin is closed). Use non-interactive flags, "
            "e.g. `winget install --id X -e --silent --accept-package-agreements --accept-source-agreements`, "
            "`pip install ...`, `npm install --yes`. Installs that need administrator rights will fail; tell the "
            "user to run them elevated instead."
        )
    return (
        "Commands run in bash, non-interactively (stdin is closed). Use non-interactive flags, e.g. "
        "`apt-get install -y`, `pip install ...`, `npm install --yes`. Commands needing sudo with a password will fail."
    )


def system_prompt(ci: ContextInput) -> str:
    parts = [
        f"You are {ci.model_name}, one of several AI models working with a user in MultiChat, "
        "a multi-model coding assistant.",
    ]
    if ci.phase == "plan":
        parts.append(
            "Phase: PLAN. Understand the problem and propose a concrete approach. Investigate first: read files "
            "with the read-only tools and, when available, run read-only commands with run_command to check code, "
            "configuration, installed software, services, logs and remote machines (over ssh, docker exec, kubectl "
            "exec). Do not modify anything: commands that write files, install or uninstall software, change settings "
            "or restart services are refused in PLAN, so put them in your plan for the BUILD phase. Public GitHub repos "
            "can be read with web_fetch (https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>, "
            "https://api.github.com/repos/<owner>/<repo>/contents/<path>). Be specific: name "
            "files, functions, commands and steps. The user will compare the models' answers and pick one model to "
            "build."
        )
    else:
        parts.append(
            "Phase: BUILD. The user picked you as the builder. Implement the agreed plan using the tools: read before "
            "you edit, make focused changes, run tests or commands to verify, and finish with a short summary of "
            "what changed and how it was verified."
        )
    if ci.context_mode in SEES_OTHERS:
        when = (
            "The others answer this message at the same time as you; you see their replies from earlier turns."
            if ci.context_mode == "shared"
            else "The models answer one after another; you see the replies given before yours."
        )
        parts.append(
            f'Context: shared. You see the other models\' replies as "[Name said]: ...". {when} Build on good ideas, '
            "point out mistakes, and avoid repeating what was already said. Write only your own reply: do not copy the "
            'other replies or start with a "[Name said]:" prefix.'
        )
    else:
        parts.append("Context: isolated. Answer independently; you cannot see the other models' replies.")

    ws = ci.workspace
    if ws.kind == "none" or not ci.has_tools:
        parts.append(
            "No folder is attached, so you have no file or command tools. Put code in fenced blocks headed by the "
            "file path, or give unified diffs."
        )
    else:
        parts.append(
            f"Workspace: {ws.label}. Root folder: {ws.root} on {ws.os or 'unknown OS'}. File paths in tools are "
            f"relative to the root. {_shell_hint(ws)}"
        )

    if ci.memory.strip():
        parts.append("Memory — standing instructions and facts from the user; follow them in every reply:\n" + ci.memory.strip())
    for name, text in ci.project_memory:
        parts.append(f"Project instructions from {name} in the folder (follow them):\n{text.strip()}")

    if ci.phase == "build":
        handoff = _handoff(ci)
        if handoff:
            parts.append(handoff)
    return "\n\n".join(parts)


def _handoff(ci: ContextInput) -> str:
    lines: list[str] = []
    if ci.context_mode == "isolated":
        plan_replies = [
            m
            for m in ci.messages
            if m.role == "assistant" and m.phase == "plan" and m.model_id != ci.model_id and m.content.strip()
        ]
        if plan_replies:
            lines.append("Plan discussion from the other models (for reference):")
            for m in plan_replies:
                mark = {1: " ▲ upvoted by the user", -1: " ▼ downvoted by the user"}.get(ci.votes.get(m.id, 0), "")
                lines.append(f"--- #{m.seq} {ci.names.get(m.model_id or 0, 'model')}{mark}:\n{m.content.strip()}")
    voted = [(m, ci.votes[m.id]) for m in ci.messages if m.id in ci.votes and m.role == "assistant"]
    if voted:
        ups = [f"#{m.seq} ({ci.names.get(m.model_id or 0, '?')})" for m, v in voted if v > 0]
        downs = [f"#{m.seq} ({ci.names.get(m.model_id or 0, '?')})" for m, v in voted if v < 0]
        feedback = "User votes:"
        if ups:
            feedback += " upvoted " + ", ".join(ups) + "."
        if downs:
            feedback += " downvoted " + ", ".join(downs) + "."
        lines.append(feedback + " Prefer the upvoted ideas.")
    return "\n".join(lines)


def _addressed_to(m: Message, model_id: int) -> bool:
    return not m.recipients or model_id in m.recipients


def _recent_images(ci: ContextInput) -> set[int]:
    keep: list[int] = []
    for m in reversed(ci.messages):
        for a in reversed(m.attachments or []):
            if a.get("kind") == "image" and len(keep) < MAX_IMAGES:
                keep.append(a["id"])
    return set(keep)


def _user_message(ci: ContextInput, m: Message, images_ok: set[int], prefix: str = "") -> ChatMessage:
    """User text plus attached documents (inline) and images (as image parts when the model can see them)."""
    text = prefix + m.content
    images: list[ImagePart] = []
    for meta in m.attachments or []:
        a = ci.attachments.get(meta["id"])
        name = meta.get("filename", "file")
        if a is None:
            text += f"\n\n[attachment {name} is no longer available]"
        elif a.kind == "document":
            text += f'\n\n<attachment name="{name}">\n{a.text or ""}\n</attachment>'
        elif a.kind == "image":
            if not ci.supports_images:
                text += f"\n\n[image {name} omitted: this model cannot read images]"
            elif prefix:
                text += f"\n\n[image {name}, sent to another model]"
            elif a.id not in images_ok or a.data is None:
                text += f"\n\n[image {name} attached earlier]"
            else:
                images.append(ImagePart(a.mime, base64.b64encode(a.data).decode()))
        else:
            text += f'\n\n<attachment name="{name}">(binary file of type {a.mime}; its content cannot be shown)</attachment>'
    return ChatMessage(role="user", content=text, images=images)


def build_messages(ci: ContextInput) -> list[ChatMessage]:
    results: dict[str, Message] = {m.tool_call_id: m for m in ci.messages if m.role == "tool" and m.tool_call_id}
    images_ok = _recent_images(ci)
    out: list[ChatMessage] = []
    for m in ci.messages:
        if m.role == "user":
            if _addressed_to(m, ci.model_id):
                out.append(_user_message(ci, m, images_ok))
            elif ci.context_mode in SEES_OTHERS:
                to = ", ".join(ci.names.get(r, "?") for r in m.recipients or [])
                out.append(_user_message(ci, m, images_ok, prefix=f"[User, to {to}]: "))
        elif m.role == "assistant" and m.model_id == ci.model_id:
            calls = [ToolCall.from_json(c) for c in (m.tool_calls or []) if not c.get("external")]
            if not ci.has_tools and calls:
                # Tools were available earlier (folder detached since): keep only the text.
                calls = []
            if not m.content and not calls:
                continue
            out.append(ChatMessage(role="assistant", content=m.content, tool_calls=calls))
            for c in calls:
                result = results.get(c.id)
                out.append(
                    ChatMessage(
                        role="tool",
                        tool_call_id=c.id,
                        content=result.content if result else "(no result: the call was cancelled)",
                    )
                )
        elif m.role == "assistant" and ci.context_mode in SEES_OTHERS and m.content.strip():
            if ci.context_mode == "shared" and ci.turn_id and m.turn_id == ci.turn_id:
                continue  # answered in parallel: still being written
            name = ci.names.get(m.model_id or 0, "Another model")
            out.append(ChatMessage(role="user", content=f"[{name} said]: {m.content.strip()}"))
    return merge_users(out)


COMPACT_AT = 0.8  # summarise older history when it fills this much of the model's context window
KEEP_RECENT = 0.3  # ... keeping about this much of the window as the newest messages, word for word
SUMMARY_SYSTEM = (
    "You are compacting a long conversation so that it fits your context window. Write a summary that lets you "
    "continue the work without the full transcript: the user's goals and requirements, decisions made and why, "
    "what was tried and what happened, important facts (file paths, function names, commands, versions, errors, "
    "configuration values, hosts), the current state, and open questions or next steps. Keep exact identifiers. "
    "Use short headed sections and bullet points. Write only the summary."
)


def summary_message(summary: str) -> ChatMessage:
    return ChatMessage(role="user", content="[Summary of the earlier conversation (written by you when your context "
                                            f"window was about 80% full)]:\n{summary}")


def transcript_for_summary(messages: list[ChatMessage], limit: int) -> str:
    """The conversation as plain text for the summarising request, at most `limit` characters (oldest cut first)."""
    lines = []
    for m in messages:
        if m.role == "tool":
            body = m.content if len(m.content) <= 3000 else m.content[:3000] + " …(truncated)"
            lines.append(f"[tool result]\n{body}")
            continue
        text = m.content or ""
        for c in m.tool_calls or []:
            text += f"\n[called {c.name} {json.dumps(c.arguments, ensure_ascii=False)[:500]}]"
        if m.images:
            text += f"\n[{len(m.images)} image(s)]"
        lines.append(f"{'User' if m.role == 'user' else 'You'}:\n{text}")
    out = "\n\n".join(lines)
    return out if len(out) <= limit else "…(start cut)\n" + out[-limit:]


def history_tokens(system: str, messages: list[ChatMessage], overhead: int = 0) -> int:
    return len(system) // 4 + overhead + sum(estimate_tokens(m) for m in messages)


def estimate_tokens(m: ChatMessage) -> int:
    """Rough token count (≈4 characters per token, images ≈1600) — enough to decide what fits."""
    n = len(m.content or "") // 4 + 4
    n += sum(len(json.dumps(c.arguments, ensure_ascii=False)) // 4 + 10 for c in m.tool_calls or [])
    return n + 1600 * len(m.images or [])


def fit_to_window(system: str, messages: list[ChatMessage], window: int, overhead: int = 0) -> tuple[list[ChatMessage], int]:
    """Keep the newest history that fits a `window`-token context (leaving room for the reply); returns the messages
    and how many were left out. A tool call always stays together with its results, and the newest exchange is
    always kept."""
    reserve = min(16_000, window // 4)
    budget = window - reserve - len(system) // 4 - overhead
    units: list[list[ChatMessage]] = []
    for m in messages:
        if m.role == "tool" and units:
            units[-1].append(m)
        else:
            units.append([m])
    kept: list[list[ChatMessage]] = []
    used = 0
    for unit in reversed(units):
        size = sum(estimate_tokens(m) for m in unit)
        if kept and used + size > budget:
            break
        kept.append(unit)
        used += size
    kept.reverse()
    out = [m for unit in kept for m in unit]
    omitted = len(messages) - len(out)
    if omitted:
        note = ChatMessage(role="user", content=f"[{omitted} earlier messages were left out to fit this model's "
                                                f"{window:,}-token context window.]")
        out = merge_users([note, *out])
    return out, omitted


def merge_users(messages: list[ChatMessage]) -> list[ChatMessage]:
    merged: list[ChatMessage] = []
    for m in messages:
        if merged and m.role == "user" and merged[-1].role == "user":
            prev = merged[-1]
            merged[-1] = ChatMessage(role="user", content=prev.content + "\n\n" + m.content, images=prev.images + m.images)
        else:
            merged.append(m)
    return merged
