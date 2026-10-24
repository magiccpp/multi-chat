"""Run one turn of an agent-CLI participant (Claude Code, Cursor, GitHub Copilot) where the workspace lives."""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from app import crypto
from app import db as dbmod
from app.agent.context import ContextInput, WorkspaceView, build_messages, system_prompt
from app.agent.events import bus
from app.agent.tools import unified_diff
from app.providers.agent_cli.base import CliDriver, CliRun, render_transcript
from app.providers.agent_cli.claude_code import ClaudeCodeDriver
from app.providers.agent_cli.copilot import CopilotDriver
from app.providers.agent_cli.cursor import CursorDriver
from app.providers.base import UsageEvent, estimate_tokens
from app.tables import AgentCliState, Endpoint, FileChange, Message, Model, Session, Vote
from app.workspaces.base import Workspace, WorkspaceError
from app.workspaces.runner import RunnerWorkspace

DRIVERS: dict[str, CliDriver] = {d.name: d for d in (ClaudeCodeDriver(), CursorDriver(), CopilotDriver())}
LABELS = {"claude_code": "Claude Code", "cursor": "Cursor", "copilot": "GitHub Copilot"}
PHASE_NOTE = {
    "plan": "[MultiChat — phase PLAN: analyse and propose; do not modify files.]",
    "build": "[MultiChat — phase BUILD: you were picked as the builder. Implement the plan, verify it, then summarise.]",
}


async def _new_message(session_id: int, turn_id: str, model: Model, phase: str, content: str = "", status: str = "streaming") -> Message:
    from app.agent.loop import next_seq

    async with dbmod.sessionmaker()() as db:
        msg = Message(
            session_id=session_id,
            seq=await next_seq(db, session_id),
            turn_id=turn_id,
            role="assistant",
            model_id=model.id,
            phase=phase,
            content=content,
            status=status,
        )
        db.add(msg)
        await db.commit()
    return msg


async def run_agent_cli_turn(session_id: int, turn_id: str, model: Model, endpoint: Endpoint, workspace: Workspace) -> None:
    from app.agent.loop import message_event, turns

    driver = DRIVERS.get(endpoint.driver or "")
    label = LABELS.get(endpoint.driver or "", endpoint.name)

    async with dbmod.sessionmaker()() as db:
        session = await db.get(Session, session_id)
        phase = session.phase
        if driver is None or not workspace.supports_tools:
            reason = (
                f"unknown agent CLI driver {endpoint.driver!r}"
                if driver is None
                else f"{label} works inside a folder. Open a folder on This PC (runner) or an SSH host for this session."
            )
            msg = await _new_message(session_id, turn_id, model, phase, f"[{reason}]", "error")
            bus.publish(session_id, {"type": "message_end", "message": message_event(msg)})
            bus.publish(session_id, {"type": "error", "model_id": model.id, "message": reason})
            return
        state = (
            await db.execute(select(AgentCliState).where(AgentCliState.session_id == session_id, AgentCliState.model_id == model.id))
        ).scalar_one_or_none()
        if state is None:
            state = AgentCliState(session_id=session_id, model_id=model.id, last_seen_message_id=0, last_total_cost=0.0)
            db.add(state)
            await db.flush()
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars())
        names = {m.id: m.display_name for m in (await db.execute(select(Model))).scalars()}
        votes = {
            v.message_id: v.value
            for v in (
                await db.execute(select(Vote).join(Message, Message.id == Vote.message_id).where(Message.session_id == session_id))
            ).scalars()
        }
        info = workspace.info
        resume_id = state.cli_session_id if driver.supports_resume else None
        subset = [m for m in messages if m.id > state.last_seen_message_id] if resume_id else messages
        ci = ContextInput(
            model_id=model.id,
            model_name=model.display_name,
            phase=phase,
            context_mode=session.context_mode,
            messages=subset,
            names=names,
            votes=votes,
            workspace=WorkspaceView(kind=info.kind, label=info.label, os=info.os, shell=info.shell, root=info.root),
            has_tools=True,
            builder_id=session.builder_model_id,
        )
        history = [m for m in build_messages(ci) if not (resume_id and m.role == "assistant")]
        if resume_id:
            prompt = PHASE_NOTE[phase] + "\n\n" + render_transcript(None, history)
        else:
            prompt = render_transcript(system_prompt(ci), history)
        last_seen = messages[-1].id if messages else 0
        approve_edits, approve_commands = session.approve_edits, session.approve_commands
        prev_total = state.last_total_cost or 0.0
        await db.commit()

    invocation = driver.build(
        CliRun(
            prompt=prompt,
            phase=phase,
            model_name=model.model_name,
            api_key=crypto.decrypt(endpoint.api_key_enc),
            resume_id=resume_id,
            approve_edits=approve_edits,
            approve_commands=approve_commands,
            bridge_available=isinstance(workspace, RunnerWorkspace),
        )
    )
    for notice in invocation.notices:
        bus.publish(session_id, {"type": "notice", "model_id": model.id, "message": notice})

    snapshot_ref = None
    if phase == "build":
        try:
            snapshot_ref = await workspace.snapshot_begin()
        except WorkspaceError as exc:
            bus.publish(session_id, {"type": "notice", "model_id": model.id, "message": f"no snapshot, Revert unavailable: {exc}"})

    msg = await _new_message(session_id, turn_id, model, phase)
    bus.publish(session_id, {"type": "message_start", "message": message_event(msg)})

    text: list[str] = []
    calls: list[dict] = []
    stderr_tail: list[str] = []
    final_text: str | None = None
    usage: dict | None = None
    cli_session: str | None = resume_id
    error: str | None = None
    started = time.monotonic()
    ttft: int | None = None

    async def on_line(line: str, is_err: bool) -> None:
        nonlocal final_text, usage, cli_session, error, ttft
        if is_err:
            stderr_tail.append(line)
            del stderr_tail[:-20]
            return
        parsed = driver.parse(line)
        if parsed.text:
            if ttft is None:
                ttft = int((time.monotonic() - started) * 1000)
            text.append(parsed.text)
            bus.publish(session_id, {"type": "delta", "message_id": msg.id, "model_id": model.id, "text": parsed.text})
        if parsed.tool_call:
            call = {**parsed.tool_call, "external": True}
            calls.append(call)
            bus.publish(session_id, {"type": "tool_call", "message_id": msg.id, "model_id": model.id, "call": call})
        if parsed.session_id:
            cli_session = parsed.session_id
        if parsed.final_text is not None:
            final_text = parsed.final_text
        if parsed.usage:
            usage = parsed.usage
        if parsed.error:
            error = parsed.error

    status = "complete"
    exit_code = 0
    try:
        exit_code = await workspace.run_agent_cli(
            driver.name,
            invocation.args,
            invocation.env,
            invocation.stdin,
            on_line,
            approval={"session_id": session_id, "model_id": model.id} if invocation.use_bridge else None,
        )
    except WorkspaceError as exc:
        error = str(exc)
    except asyncio.CancelledError:
        status = "cancelled"

    if not text and final_text:
        text.append(final_text)
        bus.publish(session_id, {"type": "delta", "message_id": msg.id, "model_id": model.id, "text": final_text})
    if error or (exit_code and not text):
        status = "error" if status != "cancelled" else status
        detail = error or ("\n".join(stderr_tail[-5:]) or f"exited with code {exit_code}")
        text.append(f"\n\n[{label} error: {detail}]")
        bus.publish(session_id, {"type": "error", "model_id": model.id, "message_id": msg.id, "message": detail})

    reply = "".join(text)
    reported_cost = (usage or {}).get("cost")
    cost = None
    if reported_cost is not None:
        cost = max(0.0, float(reported_cost) - (prev_total if resume_id else 0.0))
    usage_event = (
        UsageEvent(int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), cost=cost)
        if usage
        else UsageEvent(estimate_tokens(prompt), estimate_tokens(reply), estimated=True)
    )

    async def finish() -> None:
        from app.providers.base import ToolCall

        await turns._finish(
            session_id,
            msg,
            reply,
            [ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"]) for c in calls],
            status,
            usage_event,
            model,
            started,
            ttft,
            external=True,
        )
        async with dbmod.sessionmaker()() as db:
            st = (
                await db.execute(select(AgentCliState).where(AgentCliState.session_id == session_id, AgentCliState.model_id == model.id))
            ).scalar_one()
            if status == "complete":
                st.cli_session_id = cli_session
                st.last_seen_message_id = last_seen
                if reported_cost is not None:
                    st.last_total_cost = float(reported_cost)
            await db.commit()
        if snapshot_ref:
            await _record_changes(session_id, turn_id, model.id, workspace, snapshot_ref)

    await asyncio.shield(finish())
    if status == "cancelled":
        raise asyncio.CancelledError


async def _record_changes(session_id: int, turn_id: str, model_id: int, workspace: Workspace, ref: str) -> None:
    try:
        changes = await workspace.snapshot_changes(ref)
    except WorkspaceError as exc:
        bus.publish(session_id, {"type": "notice", "model_id": model_id, "message": f"could not compute changes: {exc}"})
        return
    async with dbmod.sessionmaker()() as db:
        for c in changes:
            db.add(FileChange(session_id=session_id, turn_id=turn_id, model_id=model_id, path=c.path, before=c.before, after=c.after))
        await db.commit()
    for c in changes:
        bus.publish(
            session_id,
            {"type": "file_changed", "path": c.path, "diff": unified_diff(c.path, c.before, c.after), "model_id": model_id, "turn_id": turn_id},
        )
