"""Build each participant's view of the conversation (Shared vs Isolated) and its system prompt."""

from __future__ import annotations

from dataclasses import dataclass, field

import base64

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
    context_mode: str  # shared | isolated
    messages: list[Message]
    names: dict[int, str]  # model id -> display name
    votes: dict[int, int] = field(default_factory=dict)  # message id -> +1/-1
    workspace: WorkspaceView = field(default_factory=WorkspaceView)
    has_tools: bool = True
    builder_id: int | None = None
    attachments: dict[int, AttachmentData] = field(default_factory=dict)
    supports_images: bool = True


def _shell_hint(ws: WorkspaceView) -> str:
    if ws.shell.lower().startswith(("powershell", "pwsh")):
        return (
            "Commands run in Windows PowerShell, non-interactively (stdin is closed). Use non-interactive flags, "
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
            "or restart services are refused in PLAN, so put them in your plan for the BUILD phase. Be specific: name "
            "files, functions, commands and steps. The user will compare the models' answers and pick one model to "
            "build."
        )
    else:
        parts.append(
            "Phase: BUILD. The user picked you as the builder. Implement the agreed plan using the tools: read before "
            "you edit, make focused changes, run tests or commands to verify, and finish with a short summary of "
            "what changed and how it was verified."
        )
    if ci.context_mode == "shared":
        parts.append(
            'Context: shared. You see the other models\' replies as "[Name said]: ...". Build on good ideas, point '
            "out mistakes, and avoid repeating what was already said. Write only your own reply: do not copy the "
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
            elif ci.context_mode == "shared":
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
        elif m.role == "assistant" and ci.context_mode == "shared" and m.content.strip():
            name = ci.names.get(m.model_id or 0, "Another model")
            out.append(ChatMessage(role="user", content=f"[{name} said]: {m.content.strip()}"))
    return _merge_users(out)


def _merge_users(messages: list[ChatMessage]) -> list[ChatMessage]:
    merged: list[ChatMessage] = []
    for m in messages:
        if merged and m.role == "user" and merged[-1].role == "user":
            prev = merged[-1]
            merged[-1] = ChatMessage(role="user", content=prev.content + "\n\n" + m.content, images=prev.images + m.images)
        else:
            merged.append(m)
    return merged
