"""Build each participant's view of the conversation (Shared vs Isolated) and its system prompt."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.providers.base import ChatMessage, ToolCall
from app.tables import Message


@dataclass
class WorkspaceView:
    kind: str = "none"  # none | runner | ssh
    label: str = ""
    os: str = ""
    shell: str = ""
    root: str = ""


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
            "Phase: PLAN. Understand the problem and propose a concrete approach. You may inspect files with the "
            "read-only tools, but do not try to modify anything. Be specific: name files, functions and steps. "
            "The user will compare the models' answers and pick one model to build."
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


def build_messages(ci: ContextInput) -> list[ChatMessage]:
    results: dict[str, Message] = {m.tool_call_id: m for m in ci.messages if m.role == "tool" and m.tool_call_id}
    out: list[ChatMessage] = []
    for m in ci.messages:
        if m.role == "user":
            if _addressed_to(m, ci.model_id):
                out.append(ChatMessage(role="user", content=m.content))
            elif ci.context_mode == "shared":
                to = ", ".join(ci.names.get(r, "?") for r in m.recipients or [])
                out.append(ChatMessage(role="user", content=f"[User, to {to}]: {m.content}"))
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
            merged[-1] = ChatMessage(role="user", content=merged[-1].content + "\n\n" + m.content)
        else:
            merged.append(m)
    return merged
