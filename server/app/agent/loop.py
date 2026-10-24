"""Runs a turn: fans a user message out to participants (PLAN) or drives the builder's tool loop (BUILD)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app import db as dbmod
from app import memory
from app.agent import status as waits
from app.agent.context import (
    COMPACT_AT,
    KEEP_RECENT,
    SUMMARY_SYSTEM,
    AttachmentData,
    ContextInput,
    WorkspaceView,
    build_messages,
    fit_to_window,
    history_tokens,
    merge_users,
    summary_message,
    system_prompt,
    transcript_for_summary,
)
from app.agent.events import bus
from app.agent.tools import ToolContext, execute, tools_for
from app.config import get_settings
from app.config_sync import get_setting
from app.pricing import cost_for
from app.providers.base import ChatMessage, Progress, ProviderError, TextDelta, ToolCall, ToolCallEvent, UsageEvent
from app.providers.registry import provider_for
from app.tables import Attachment, Compaction, Endpoint, FileChange, Message, Model, Session, SessionModel, Usage, Vote
from app.workspaces.base import NoWorkspace, Workspace, WorkspaceError
from app.workspaces.factory import workspace_for

log = logging.getLogger(__name__)


class TurnError(Exception):
    pass


@dataclass
class ModelContext:
    session: Session
    system: str
    history: list
    effort: str | None
    window: int | None  # the model's context window in this session
    used: int  # estimated tokens of system + history + tools before any trimming
    ci: ContextInput
    prefix: list  # [summary message] when an earlier compaction applies


@dataclass
class RuntimeDefaults:
    max_steps: int
    command_timeout: int
    output_limit: int
    plan_commands: str = "auto"
    plan_command_timeout: int = 120


async def runtime_defaults(db: AsyncSession) -> RuntimeDefaults:
    s = get_settings()
    d = await get_setting(db, "defaults")
    return RuntimeDefaults(
        max_steps=int(d.get("max_steps", s.max_steps)),
        command_timeout=int(d.get("command_timeout", s.command_timeout)),
        output_limit=int(d.get("tool_output_limit", s.tool_output_limit)),
        plan_commands=str(d.get("plan_commands", "auto")),
        plan_command_timeout=int(d.get("plan_command_timeout", 120)),
    )


async def next_seq(db: AsyncSession, session_id: int) -> int:
    result = await db.execute(
        update(Session).where(Session.id == session_id).values(next_seq=Session.next_seq + 1).returning(Session.next_seq)
    )
    return int(result.scalar_one()) - 1


async def participants(db: AsyncSession, session_id: int) -> list[tuple[Model, Endpoint]]:
    rows = await db.execute(
        select(Model, Endpoint)
        .join(SessionModel, SessionModel.model_id == Model.id)
        .join(Endpoint, Endpoint.id == Model.endpoint_id)
        .where(SessionModel.session_id == session_id, SessionModel.left_at.is_(None))
        .order_by(SessionModel.position, SessionModel.id)
    )
    return [(m, e) for m, e in rows.all()]


def message_event(m: Message, **extra: Any) -> dict[str, Any]:
    return {
        "id": m.id,
        "seq": m.seq,
        "turn_id": m.turn_id,
        "role": m.role,
        "model_id": m.model_id,
        "phase": m.phase,
        "content": m.content,
        "tool_calls": m.tool_calls,
        "tool_call_id": m.tool_call_id,
        "recipients": m.recipients,
        "attachments": m.attachments,
        "status": m.status,
        **extra,
    }


async def load_attachments(db: AsyncSession, messages: list[Message]) -> dict[int, AttachmentData]:
    ids = [a["id"] for m in messages for a in (m.attachments or [])]
    if not ids:
        return {}
    rows = await db.execute(select(Attachment).where(Attachment.id.in_(ids)).options(undefer(Attachment.data), undefer(Attachment.text)))
    return {
        a.id: AttachmentData(a.id, a.filename, a.mime, a.kind, a.text, a.data if a.kind == "image" else None)
        for a in rows.scalars()
    }


class TurnManager:
    def __init__(self) -> None:
        self.tasks: dict[int, asyncio.Task] = {}

    def busy(self, session_id: int) -> bool:
        task = self.tasks.get(session_id)
        return task is not None and not task.done()

    async def start(
        self, session_id: int, content: str, recipients: list[int] | None, attachment_ids: list[int] | None = None
    ) -> tuple[str, int]:
        if self.busy(session_id):
            raise TurnError("a turn is already running in this session")
        async with dbmod.sessionmaker()() as db:
            session = await db.get(Session, session_id)
            if session is None:
                raise TurnError("session not found")
            active = [m.id for m, _ in await participants(db, session_id)]
            if session.phase == "build":
                if not session.builder_model_id:
                    raise TurnError("pick a builder model before sending in BUILD mode")
                targets = [session.builder_model_id]
            else:
                targets = [m for m in active if recipients is None or m in recipients]
            if not targets:
                raise TurnError("no participating models; add models to the session first")
            files = []
            for aid in attachment_ids or []:
                a = await db.get(Attachment, aid)
                if a is None or a.session_id != session_id:
                    raise TurnError(f"attachment {aid} does not belong to this session")
                files.append({"id": a.id, "filename": a.filename, "mime": a.mime, "size": a.size, "kind": a.kind})
            turn_id = str(uuid.uuid4())
            user = Message(
                session_id=session_id,
                seq=await next_seq(db, session_id),
                turn_id=turn_id,
                role="user",
                phase=session.phase,
                content=content,
                recipients=targets,
                attachments=files or None,
            )
            db.add(user)
            await db.commit()
            phase, mode = session.phase, session.context_mode

        bus.begin_turn(session_id)
        bus.publish(session_id, {"type": "turn_start", "turn_id": turn_id, "phase": phase, "targets": targets})
        bus.publish(session_id, {"type": "message", "message": message_event(user)})
        task = asyncio.create_task(self._run(session_id, turn_id, phase, mode, targets))
        self.tasks[session_id] = task
        return turn_id, user.id

    async def stop(self, session_id: int) -> bool:
        task = self.tasks.get(session_id)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def context_report(self, session_id: int) -> list[dict[str, Any]]:
        """Per participant: estimated tokens of its history now, its context window, and compactions so far (like
        Claude Code's /context). Agent CLIs manage their own context."""
        out = []
        async with dbmod.sessionmaker()() as db:
            for model, endpoint in await participants(db, session_id):
                n = (await db.execute(select(func.count(Compaction.id)).where(
                    Compaction.session_id == session_id, Compaction.model_id == model.id))).scalar_one()
                if endpoint.kind == "agent_cli":
                    out.append({"model_id": model.id, "display_name": model.display_name, "used": None, "window": None,
                                "percent": None, "compactions": n, "note": "the agent CLI manages its own context"})
                    continue
                ctx = await self._context(db, session_id, model, NoWorkspace(), False)
                pct = round(100 * ctx.used / ctx.window, 1) if ctx.window else None
                out.append({"model_id": model.id, "display_name": model.display_name, "used": ctx.used,
                            "window": ctx.window, "percent": pct, "compactions": n,
                            "note": f"compacts automatically at {int(COMPACT_AT * 100)}%" if ctx.window else "window unknown"})
        return out

    async def compact_now(self, session_id: int, model_ids: list[int] | None = None) -> list[dict[str, Any]]:
        """Summarise each model's older history now (Claude Code's /compact)."""
        if self.busy(session_id):
            raise TurnError("a turn is running; wait for it to finish")
        turn_id = f"compact-{uuid.uuid4().hex[:12]}"
        results = []
        async with dbmod.sessionmaker()() as db:
            targets = [(m, e) for m, e in await participants(db, session_id)
                       if e.kind != "agent_cli" and (not model_ids or m.id in model_ids)]
        for model, endpoint in targets:
            async with dbmod.sessionmaker()() as db:
                ctx = await self._context(db, session_id, model, NoWorkspace(), False)
            ctx.window = ctx.window or 128_000
            waits.board.set(session_id, model.id, waits.COMPACTING, "compacting its context (summarising the older "
                            "conversation)")
            try:
                done = await self._compact(session_id, turn_id, model, provider_for(endpoint), ctx, manual=True)
            finally:
                waits.board.clear(session_id, model.id)
            results.append({"model_id": model.id, "display_name": model.display_name, "compacted": done})
        return results

    async def wait(self, session_id: int) -> None:
        task = self.tasks.get(session_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self, session_id: int, turn_id: str, phase: str, mode: str, targets: list[int]) -> None:
        status = "complete"
        try:
            if mode == "relay" and phase == "plan":
                for model_id in targets[1:]:
                    waits.board.set(session_id, model_id, waits.QUEUED, "waiting for its turn (relay mode answers one "
                                    "model at a time, each seeing the replies before it)")
                for model_id in targets:  # each sees the replies before it
                    await self._participant(session_id, turn_id, model_id)
            else:
                await asyncio.gather(*(self._participant(session_id, turn_id, m) for m in targets))
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("turn failed")
            status = "error"
            bus.publish(session_id, {"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            bus.publish(session_id, {"type": "turn_end", "turn_id": turn_id, "status": status})
            bus.end_turn(session_id)

    async def _participant(self, session_id: int, turn_id: str, model_id: int) -> None:
        async with dbmod.sessionmaker()() as db:
            model = await db.get(Model, model_id)
            endpoint = await db.get(Endpoint, model.endpoint_id) if model else None
            session = await db.get(Session, session_id)
            if model is None or endpoint is None or session is None:
                return
            try:
                workspace = await workspace_for(db, session)
            except WorkspaceError as exc:
                bus.publish(session_id, {"type": "error", "model_id": model_id, "message": str(exc)})
                return
        try:
            if endpoint.kind == "agent_cli":
                from app.providers.agent_cli.run import run_agent_cli_turn

                await run_agent_cli_turn(session_id, turn_id, model, endpoint, workspace)
            else:
                await self._llm(session_id, turn_id, model, endpoint, workspace)
        finally:
            waits.board.clear(session_id, model_id)
            await workspace.close()

    async def _context(
        self, db: AsyncSession, session_id: int, model: Model, workspace: Workspace, has_tools: bool, turn_id: str = "",
        tools: list | None = None, project_memory: list[tuple[str, str]] | None = None,
    ) -> ModelContext:
        session = await db.get(Session, session_id)
        await db.refresh(session)
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars())
        compaction = (await db.execute(
            select(Compaction).where(Compaction.session_id == session_id, Compaction.model_id == model.id)
            .order_by(Compaction.id.desc()).limit(1)
        )).scalar_one_or_none()
        if compaction:  # the summary stands in for everything before it
            messages = [m for m in messages if m.id >= compaction.before_message_id]
        names = {m.id: m.display_name for m in (await db.execute(select(Model))).scalars()}
        votes = {
            v.message_id: v.value
            for v in (
                await db.execute(select(Vote).join(Message, Message.id == Vote.message_id).where(Message.session_id == session_id))
            ).scalars()
        }
        info = workspace.info
        ci = ContextInput(
            model_id=model.id,
            model_name=model.display_name,
            phase=session.phase,
            context_mode=session.context_mode,
            messages=messages,
            names=names,
            votes=votes,
            workspace=WorkspaceView(kind=info.kind, label=info.label, os=info.os, shell=info.shell, root=info.root),
            has_tools=has_tools,
            builder_id=session.builder_model_id,
            attachments=await load_attachments(db, messages),
            supports_images=model.supports_images,
            turn_id=turn_id,
            memory=await memory.load(db),
            project_memory=project_memory or [],
        )
        system = system_prompt(ci)
        prefix = [summary_message(compaction.summary)] if compaction else []
        history = merge_users([*prefix, *build_messages(ci)])
        sm = (await db.execute(select(SessionModel).where(
            SessionModel.session_id == session_id, SessionModel.model_id == model.id, SessionModel.left_at.is_(None)
        ))).scalar_one_or_none()
        window = (sm.context_window if sm else None) or model.context_length
        overhead = sum(len(t.description) + len(json.dumps(t.parameters)) for t in tools or []) // 4
        used = history_tokens(system, history, overhead)
        if window:
            history, _ = fit_to_window(system, history, window, overhead)  # safety net; compaction comes first
        effort = sm.effort if sm and model.supports_effort else None
        return ModelContext(session, system, history, effort, window, used, ci, prefix)

    async def _compact(self, session_id: int, turn_id: str, model: Model, provider, ctx: ModelContext,  # noqa: ANN001
                       manual: bool = False) -> bool:
        """Summarise the older part of this model's history (keeping ~30% of the window word for word) once it fills
        ~80% of the context window; `manual` (/compact) keeps only the latest exchange. Returns False if there is
        nothing to summarise or the request failed."""
        messages = ctx.ci.messages
        users = [m for m in messages if m.role == "user"]
        if not users or not ctx.window:
            return False
        budget, kept, boundary = int(ctx.window * KEEP_RECENT), 0, None
        for m in reversed(messages):
            kept += len(m.content or "") // 4 + 4
            if manual or kept > budget:
                break
            if m.role == "user":
                boundary = m.id  # the kept part starts at a user message, so tool calls stay with their results
        boundary = min(boundary or users[-1].id, users[-1].id)  # the current question always stays
        older = [m for m in messages if m.id < boundary]
        if not older:
            return False
        older_history = merge_users([*ctx.prefix, *build_messages(replace(ctx.ci, messages=older))])
        transcript = transcript_for_summary(older_history, int(ctx.window * 0.7 * 4))
        started = time.monotonic()
        text: list[str] = []
        usage: UsageEvent | None = None
        try:
            async for ev in provider.stream(model.model_name, SUMMARY_SYSTEM,
                                            [ChatMessage("user", f"Conversation so far:\n\n{transcript}\n\nWrite the summary now.")], []):
                if isinstance(ev, TextDelta):
                    text.append(ev.text)
                elif isinstance(ev, UsageEvent):
                    usage = ev
        except ProviderError as exc:
            log.warning("compaction for %s failed: %s", model.display_name, exc)
            return False
        summary = "".join(text).strip()
        if not summary:
            return False
        async with dbmod.sessionmaker()() as db:
            session = await db.get(Session, session_id)
            db.add(Compaction(session_id=session_id, model_id=model.id, before_message_id=boundary, summary=summary,
                              tokens_before=ctx.used))
            msg = Message(session_id=session_id, seq=await next_seq(db, session_id), turn_id=turn_id, role="summary",
                          model_id=model.id, phase=session.phase, status="streaming",
                          content=f"Context compacted: {len(older)} earlier messages (~{ctx.used:,} tokens of a "
                                  f"{ctx.window:,}-token window) were summarised.")
            db.add(msg)
            await db.commit()
        bus.publish(session_id, {"type": "message_start", "message": message_event(msg)})
        await self._finish(session_id, msg, f"{msg.content}\n\n{summary}", [], "complete", usage, model, started, None)
        return True

    async def _llm(self, session_id: int, turn_id: str, model: Model, endpoint: Endpoint, workspace: Workspace) -> None:
        provider = provider_for(endpoint)
        async with dbmod.sessionmaker()() as db:
            defaults = await runtime_defaults(db)
            web = await get_setting(db, "web")
            web_search_cfg = await get_setting(db, "web_search")

        project_memory = await memory.project_files(workspace)  # CLAUDE.md / AGENTS.md / MULTICHAT.md, once per turn
        if not model.supports_tools:  # say so, rather than leave the model to claim it "has no shell"
            bus.publish(session_id, {"type": "notice", "model_id": model.id, "message": (
                f"{model.display_name} is set up without tool calling, so it can't read files, run commands or fetch "
                "pages here; it answers from the conversation only. Endpoints → Check availability re-reads its "
                "capabilities.")})

        async def record_change(path: str, before: str | None, after: str | None) -> None:
            async with dbmod.sessionmaker()() as db:
                db.add(FileChange(session_id=session_id, turn_id=turn_id, model_id=model.id, path=path, before=before, after=after))
                await db.commit()

        for step in range(defaults.max_steps):
            async with dbmod.sessionmaker()() as db:
                session = await db.get(Session, session_id)
                tools = (
                    tools_for(session.phase, workspace, bool(web_search_cfg.get("provider")), defaults.plan_commands)
                    if model.supports_tools
                    else []
                )
                ctx = await self._context(db, session_id, model, workspace, bool(tools) and workspace.supports_tools, turn_id,
                                          tools, project_memory)
            # at the start of this model's turn: compact when the history fills ~80% of its context window
            if step == 0 and ctx.window and ctx.used > COMPACT_AT * ctx.window:
                waits.board.set(
                    session_id, model.id, waits.COMPACTING,
                    f"compacting its context: ~{ctx.used:,} of {ctx.window:,} tokens used",
                    f"At ~{int(COMPACT_AT * 100)}% of its context window a model summarises the older conversation so "
                    "the next request fits. This takes one extra request to the same model.",
                )
                if await self._compact(session_id, turn_id, model, provider, ctx):
                    async with dbmod.sessionmaker()() as db:
                        ctx = await self._context(db, session_id, model, workspace, bool(tools) and workspace.supports_tools,
                                                  turn_id, tools, project_memory)
            session, system, history, effort = ctx.session, ctx.system, ctx.history, ctx.effort
            async with dbmod.sessionmaker()() as db:
                msg = Message(
                    session_id=session_id,
                    seq=await next_seq(db, session_id),
                    turn_id=turn_id,
                    role="assistant",
                    model_id=model.id,
                    phase=session.phase,
                    status="streaming",
                )
                db.add(msg)
                await db.commit()
                approve_edits, approve_commands = session.approve_edits, session.approve_commands
                phase = session.phase
            bus.publish(session_id, {"type": "message_start", "message": message_event(msg)})
            note = Notes(session_id, model.id)
            note(waits.WAITING,
                 f"waiting for the reply to start ({endpoint.name})" if step == 0
                 else f"reading the tool results; waiting for its next reply ({endpoint.name})",
                 waits.waiting_hint(endpoint.name, ctx.used, ctx.window, effort))

            text: list[str] = []
            calls: list[ToolCall] = []
            usage: UsageEvent | None = None
            started = time.monotonic()
            ttft: int | None = None
            status = "complete"
            try:
                async for ev in provider.stream(model.model_name, system, history, tools, effort=effort):
                    if isinstance(ev, TextDelta):
                        if ttft is None:
                            ttft = int((time.monotonic() - started) * 1000)
                        text.append(ev.text)
                        bus.publish(session_id, {"type": "delta", "message_id": msg.id, "model_id": model.id, "text": ev.text})
                        note(waits.WRITING, "writing its answer")
                    elif isinstance(ev, Progress):
                        note.progress(ev)
                    elif isinstance(ev, ToolCallEvent):
                        calls.append(ev.call)
                    elif isinstance(ev, UsageEvent):
                        usage = ev
            except ProviderError as exc:
                status = "error"
                text.append(f"\n\n[error from {endpoint.name}: {exc}]")
                bus.publish(session_id, {"type": "error", "model_id": model.id, "message_id": msg.id, "message": str(exc)})
            except asyncio.CancelledError:
                status = "cancelled"
                await asyncio.shield(self._finish(session_id, msg, "".join(text), [], status, None, model, started, ttft))
                raise
            await self._finish(session_id, msg, "".join(text), calls, status, usage, model, started, ttft)
            if status != "complete" or not calls:
                return

            def tool_context() -> ToolContext:
                return ToolContext(
                    session_id=session_id,
                    turn_id=turn_id,
                    model_id=model.id,
                    workspace=workspace,
                    approve_edits=approve_edits,
                    approve_commands=approve_commands,
                    command_timeout=defaults.command_timeout,
                    output_limit=defaults.output_limit,
                    web=web,
                    web_search=web_search_cfg,
                    emit=lambda ev: bus.publish(session_id, ev),
                    record_change=record_change,
                    phase=phase,
                    plan_commands=defaults.plan_commands,
                    plan_command_timeout=defaults.plan_command_timeout,
                )

            for call in calls:
                bus.publish(
                    session_id,
                    {"type": "tool_call", "message_id": msg.id, "model_id": model.id, "call": call.to_json()},
                )
            where = workspace.info.label
            if phase == "plan":
                note(waits.TOOL, *waits.describe_calls(calls, where, defaults.plan_command_timeout))
                # PLAN tools only read, so a reply's calls run side by side (a slow command doesn't hold up the
                # rest); results are stored in the order the model asked for them.
                tasks = [asyncio.ensure_future(execute(call, tool_context())) for call in calls]
                try:
                    results = await asyncio.gather(*tasks)
                except asyncio.CancelledError:
                    for t in tasks:
                        t.cancel()
                    for call, t in zip(calls, tasks):
                        done = t.done() and not t.cancelled() and t.exception() is None
                        text = t.result() if done else "(cancelled by the user)"
                        await asyncio.shield(self._tool_message(session_id, turn_id, model, call, text))
                    raise
                for call, result in zip(calls, results):
                    await self._tool_message(session_id, turn_id, model, call, result)
                continue
            for call in calls:  # BUILD: in order; edits and commands may depend on each other
                note(waits.TOOL, *waits.describe_calls([call], where, defaults.command_timeout))
                try:
                    result = await execute(call, tool_context())
                except asyncio.CancelledError:
                    await asyncio.shield(self._tool_message(session_id, turn_id, model, call, "(cancelled by the user)"))
                    raise
                await self._tool_message(session_id, turn_id, model, call, result)
        bus.publish(
            session_id,
            {"type": "notice", "model_id": model.id, "message": f"stopped after {defaults.max_steps} steps"},
        )

    async def _finish(
        self,
        session_id: int,
        msg: Message,
        text: str,
        calls: list[ToolCall],
        status: str,
        usage: UsageEvent | None,
        model: Model,
        started: float,
        ttft: int | None,
        external: bool = False,
    ) -> None:
        async with dbmod.sessionmaker()() as db:
            row = await db.get(Message, msg.id)
            row.content = text
            # External calls were run by an agent CLI itself; they are shown but never replayed to API models.
            row.tool_calls = [{**c.to_json(), **({"external": True} if external else {})} for c in calls] or None
            row.status = status
            if usage is None:
                from app.providers.base import estimate_tokens

                usage = UsageEvent(input_tokens=0, output_tokens=estimate_tokens(text), estimated=True)
            cost = usage.cost if usage.cost is not None else cost_for(model, usage.input_tokens, usage.output_tokens)
            u = Usage(
                message_id=row.id,
                session_id=session_id,
                model_id=model.id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost=cost,
                latency_ms=int((time.monotonic() - started) * 1000),
                ttft_ms=ttft,
                estimated=usage.estimated,
            )
            db.add(u)
            await db.commit()
            bus.publish(
                session_id,
                {
                    "type": "usage",
                    "message_id": row.id,
                    "model_id": model.id,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cost": u.cost,
                    "estimated": u.estimated,
                },
            )
            bus.publish(session_id, {"type": "message_end", "message": message_event(row)})

    async def _tool_message(self, session_id: int, turn_id: str, model: Model, call: ToolCall, result: str) -> None:
        async with dbmod.sessionmaker()() as db:
            session = await db.get(Session, session_id)
            row = Message(
                session_id=session_id,
                seq=await next_seq(db, session_id),
                turn_id=turn_id,
                role="tool",
                model_id=model.id,
                phase=session.phase,
                content=result,
                tool_call_id=call.id,
            )
            db.add(row)
            await db.commit()
        bus.publish(session_id, {"type": "tool_result", "model_id": model.id, "call_id": call.id, "message": message_event(row)})


class Notes:
    """Status updates for one model reply: publishes when the state changes, and progress (characters of reasoning
    or of a tool call being written) at most every 2 seconds."""

    EVERY = 2.0

    def __init__(self, session_id: int, model_id: int) -> None:
        self.session_id, self.model_id = session_id, model_id
        self.state: str | None = None
        self.at = 0.0

    def __call__(self, state: str, text: str, hint: str | None = None) -> None:
        self.state, self.at = state, time.monotonic()
        waits.board.set(self.session_id, self.model_id, state, text, hint)

    def progress(self, ev: Progress) -> None:
        state = waits.THINKING if ev.kind == "thinking" else waits.TOOL_ARGS
        if state == self.state and time.monotonic() - self.at < self.EVERY:
            return
        if state == waits.THINKING:
            self(state, "thinking before it answers",
                 f"Reasoning models think first; ~{ev.chars // 4:,} tokens of reasoning so far." if ev.chars > 40 else None)
        else:
            self(state, f"writing a {ev.name or 'tool'} call ({size(ev.chars)} so far)",
                 "Large tool calls (a whole file for write_file) take a while; nothing shows until the call is complete.")


def size(chars: int) -> str:
    return f"{chars:,} characters" if chars < 10_000 else f"{chars / 1024:.0f} KB"


turns = TurnManager()
