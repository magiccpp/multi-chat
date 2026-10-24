"""Runs a turn: fans a user message out to participants (PLAN) or drives the builder's tool loop (BUILD)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app import db as dbmod
from app.agent.context import AttachmentData, ContextInput, WorkspaceView, build_messages, system_prompt
from app.agent.events import bus
from app.agent.tools import ToolContext, execute, tools_for
from app.config import get_settings
from app.config_sync import get_setting
from app.pricing import cost_for
from app.providers.base import ProviderError, TextDelta, ToolCall, ToolCallEvent, UsageEvent
from app.providers.registry import provider_for
from app.tables import Attachment, Endpoint, FileChange, Message, Model, Session, SessionModel, Usage, Vote
from app.workspaces.base import Workspace, WorkspaceError
from app.workspaces.factory import workspace_for

log = logging.getLogger(__name__)


class TurnError(Exception):
    pass


@dataclass
class RuntimeDefaults:
    max_steps: int
    command_timeout: int
    output_limit: int
    plan_commands: str = "auto"


async def runtime_defaults(db: AsyncSession) -> RuntimeDefaults:
    s = get_settings()
    d = await get_setting(db, "defaults")
    return RuntimeDefaults(
        max_steps=int(d.get("max_steps", s.max_steps)),
        command_timeout=int(d.get("command_timeout", s.command_timeout)),
        output_limit=int(d.get("tool_output_limit", s.tool_output_limit)),
        plan_commands=str(d.get("plan_commands", "auto")),
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

    async def wait(self, session_id: int) -> None:
        task = self.tasks.get(session_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self, session_id: int, turn_id: str, phase: str, mode: str, targets: list[int]) -> None:
        status = "complete"
        try:
            if phase == "plan" and mode == "isolated":
                await asyncio.gather(*(self._participant(session_id, turn_id, m) for m in targets))
            else:
                for model_id in targets:
                    await self._participant(session_id, turn_id, model_id)
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
            await workspace.close()

    async def _context(
        self, db: AsyncSession, session_id: int, model: Model, workspace: Workspace, has_tools: bool
    ) -> tuple[Session, str, list]:
        session = await db.get(Session, session_id)
        await db.refresh(session)
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars())
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
        )
        return session, system_prompt(ci), build_messages(ci)

    async def _llm(self, session_id: int, turn_id: str, model: Model, endpoint: Endpoint, workspace: Workspace) -> None:
        provider = provider_for(endpoint)
        async with dbmod.sessionmaker()() as db:
            defaults = await runtime_defaults(db)
            web = await get_setting(db, "web")
            web_search_cfg = await get_setting(db, "web_search")

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
                session, system, history = await self._context(db, session_id, model, workspace, bool(tools) and workspace.supports_tools)
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

            text: list[str] = []
            calls: list[ToolCall] = []
            usage: UsageEvent | None = None
            started = time.monotonic()
            ttft: int | None = None
            status = "complete"
            try:
                async for ev in provider.stream(model.model_name, system, history, tools):
                    if isinstance(ev, TextDelta):
                        if ttft is None:
                            ttft = int((time.monotonic() - started) * 1000)
                        text.append(ev.text)
                        bus.publish(session_id, {"type": "delta", "message_id": msg.id, "model_id": model.id, "text": ev.text})
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

            for call in calls:
                bus.publish(
                    session_id,
                    {"type": "tool_call", "message_id": msg.id, "model_id": model.id, "call": call.to_json()},
                )
                ctx = ToolContext(
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
                )
                try:
                    result = await execute(call, ctx)
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


turns = TurnManager()
