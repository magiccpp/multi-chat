import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.approvals import Decision, approvals
from app.agent.events import bus, sse
from app.agent.loop import TurnError, participants, turns
from app.agent.tools import unified_diff
from app.db import get_db
from app.schemas import (
    ApprovalIn,
    ChatIn,
    FileChangeOut,
    MessageOut,
    ModelSettingsIn,
    ModelSettingsPatch,
    ParticipantOut,
    PickBuilderIn,
    RevertIn,
    SessionIn,
    SessionModelsIn,
    SessionOut,
    SessionPatch,
    UsageRow,
    WorkspaceIn,
)
from app.tables import BuilderPick, Endpoint, FileChange, Message, Model, Runner, Session, SessionModel, SshConnection, Usage, Vote
from app.workspaces.base import WorkspaceError
from app.workspaces.factory import workspace_for, workspace_label

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


async def _get(db: AsyncSession, session_id: int) -> Session:
    row = await db.get(Session, session_id)
    if row is None:
        raise HTTPException(404, "session not found")
    return row


async def _session_models(db: AsyncSession, session_id: int) -> dict[int, SessionModel]:
    rows = await db.execute(select(SessionModel).where(SessionModel.session_id == session_id, SessionModel.left_at.is_(None)))
    return {sm.model_id: sm for sm in rows.scalars()}


async def session_out(db: AsyncSession, s: Session) -> SessionOut:
    out = SessionOut.model_validate(s)
    settings = await _session_models(db, s.id)
    out.participants = [
        ParticipantOut(
            model_id=m.id, display_name=m.display_name, color=m.color, endpoint_kind=e.kind, position=i,
            context_window=settings[m.id].context_window if m.id in settings else None,
            context_length=m.context_length, effort=settings[m.id].effort if m.id in settings else None,
            supports_effort=m.supports_effort,
        )
        for i, (m, e) in enumerate(await participants(db, s.id))
    ]
    out.workspace_label = await workspace_label(db, s)
    out.busy = turns.busy(s.id)
    cost, tokens = (await db.execute(
        select(func.coalesce(func.sum(Usage.cost), 0), func.coalesce(func.sum(Usage.input_tokens + Usage.output_tokens), 0))
        .where(Usage.session_id == s.id)
    )).one()
    out.cost, out.tokens = float(cost), int(tokens)
    return out


def _check_settings(model: Model, context_window: int | None, effort: str | None) -> None:
    if context_window is not None and model.context_length and context_window > model.context_length:
        raise HTTPException(422, f"{model.display_name} has a context window of at most {model.context_length:,} tokens")
    if effort is not None and not model.supports_effort:
        raise HTTPException(422, f"{model.display_name} doesn't support a reasoning effort setting")


async def _apply_settings(db: AsyncSession, session_id: int, settings: dict[int, ModelSettingsIn]) -> None:
    """Settings chosen when a session starts (or while it is still empty)."""
    current = await _session_models(db, session_id)
    for model_id, cfg in settings.items():
        sm = current.get(model_id)
        if sm is None:
            raise HTTPException(422, f"model {model_id} is not in this session")
        model = await db.get(Model, model_id)
        _check_settings(model, cfg.context_window, cfg.effort)
        sm.context_window, sm.effort = cfg.context_window, cfg.effort


async def _has_messages(db: AsyncSession, session_id: int) -> bool:
    return (await db.execute(select(Message.id).where(Message.session_id == session_id).limit(1))).first() is not None


async def _set_models(db: AsyncSession, session_id: int, model_ids: list[int]) -> None:
    joined = {
        sm.model_id
        for sm in (
            await db.execute(select(SessionModel).where(SessionModel.session_id == session_id, SessionModel.left_at.is_(None)))
        ).scalars()
    }
    for mid in model_ids:
        model = await db.get(Model, mid)
        if model is None:
            raise HTTPException(404, f"model {mid} not found")
        if mid not in joined and not model.available:
            raise HTTPException(422, f"{model.display_name} is unavailable: {model.availability_detail or 'endpoint check failed'}")
    current = {
        sm.model_id: sm
        for sm in (
            await db.execute(select(SessionModel).where(SessionModel.session_id == session_id, SessionModel.left_at.is_(None)))
        ).scalars()
    }
    now = datetime.now(UTC)
    for mid, sm in current.items():
        if mid not in model_ids:
            sm.left_at = now
    for pos, mid in enumerate(model_ids):
        if mid in current:
            current[mid].position = pos
        else:
            db.add(SessionModel(session_id=session_id, model_id=mid, position=pos))


async def _apply_workspace(db: AsyncSession, s: Session, ws: WorkspaceIn) -> None:
    if ws.kind == "none":
        s.workspace_kind, s.runner_id, s.ssh_connection_id, s.workspace_path = "none", None, None, None
        return
    if not ws.path:
        raise HTTPException(422, "a folder path is required")
    if ws.kind == "runner":
        runner = await db.get(Runner, ws.runner_id) if ws.runner_id else None
        if runner is None:
            raise HTTPException(404, "runner not found")
        s.workspace_kind, s.runner_id, s.ssh_connection_id = "runner", runner.id, None
    else:
        conn = await db.get(SshConnection, ws.ssh_connection_id) if ws.ssh_connection_id else None
        if conn is None:
            raise HTTPException(404, "ssh connection not found")
        s.workspace_kind, s.ssh_connection_id, s.runner_id = "ssh", conn.id, None
    s.workspace_path = ws.path


@router.get("", response_model=list[SessionOut])
async def list_sessions(limit: int = 50, db: AsyncSession = Depends(get_db)) -> list[SessionOut]:
    rows = (await db.execute(select(Session).order_by(Session.updated_at.desc()).limit(limit))).scalars().all()
    return [await session_out(db, s) for s in rows]


@router.post("", response_model=SessionOut, status_code=201)
async def create_session(body: SessionIn, db: AsyncSession = Depends(get_db)) -> SessionOut:
    from app.config_sync import get_setting

    if body.reuse_empty:
        latest = (await db.execute(select(Session).order_by(Session.updated_at.desc()).limit(1))).scalar_one_or_none()
        if latest is not None and not turns.busy(latest.id):
            has_messages = (await db.execute(select(Message.id).where(Message.session_id == latest.id).limit(1))).first()
            if not has_messages:
                latest.title, latest.context_mode = body.title, body.context_mode
                if body.workspace.kind != "none":
                    await _apply_workspace(db, latest, body.workspace)
                if body.model_ids:
                    await _set_models(db, latest.id, body.model_ids)
                await db.flush()
                await _apply_settings(db, latest.id, body.model_settings)
                await db.commit()
                await db.refresh(latest)
                return await session_out(db, latest)
    defaults = await get_setting(db, "defaults")
    s = Session(
        title=body.title,
        context_mode=body.context_mode,
        approve_edits=defaults.get("approve_edits", "auto"),
        approve_commands=defaults.get("approve_commands", "ask"),
    )
    db.add(s)
    await db.flush()
    await _apply_workspace(db, s, body.workspace)
    await _set_models(db, s.id, body.model_ids)
    await db.flush()
    await _apply_settings(db, s.id, body.model_settings)
    await db.commit()
    await db.refresh(s)
    return await session_out(db, s)


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: int, db: AsyncSession = Depends(get_db)) -> SessionOut:
    return await session_out(db, await _get(db, session_id))


@router.patch("/{session_id}", response_model=SessionOut)
async def update_session(session_id: int, body: SessionPatch, db: AsyncSession = Depends(get_db)) -> SessionOut:
    s = await _get(db, session_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("phase") == "build" and not (data.get("builder_model_id") or s.builder_model_id):
        raise HTTPException(422, "pick a builder model before switching to BUILD")
    for field, value in data.items():
        setattr(s, field, value)
    await db.commit()
    await db.refresh(s)
    out = await session_out(db, s)
    bus.publish(session_id, {"type": "session", "session": out.model_dump(mode="json")})
    return out


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: int, db: AsyncSession = Depends(get_db)) -> None:
    await turns.stop(session_id)
    await db.delete(await _get(db, session_id))
    await db.commit()


@router.patch("/{session_id}/models/{model_id}", response_model=SessionOut)
async def update_model_settings(
    session_id: int, model_id: int, body: ModelSettingsPatch, db: AsyncSession = Depends(get_db)
) -> SessionOut:
    """Change a participant's effort (any time) or context window (only before the first message)."""
    s = await _get(db, session_id)
    sm = (await _session_models(db, session_id)).get(model_id)
    if sm is None:
        raise HTTPException(404, "that model is not in this session")
    model = await db.get(Model, model_id)
    sent = body.model_fields_set
    if "context_window" in sent and body.context_window != sm.context_window:
        if await _has_messages(db, session_id):
            raise HTTPException(409, "the context window is chosen when a session starts and can't be changed afterwards; start a new session")
        _check_settings(model, body.context_window, None)
        sm.context_window = body.context_window
    if "effort" in sent:
        _check_settings(model, None, body.effort)
        sm.effort = body.effort
    await db.commit()
    await db.refresh(s)
    out = await session_out(db, s)
    bus.publish(session_id, {"type": "session", "session": out.model_dump(mode="json")})
    return out


@router.put("/{session_id}/models", response_model=SessionOut)
async def set_models(session_id: int, body: SessionModelsIn, db: AsyncSession = Depends(get_db)) -> SessionOut:
    s = await _get(db, session_id)
    await _set_models(db, session_id, body.model_ids)
    if s.builder_model_id and s.builder_model_id not in body.model_ids:
        s.builder_model_id = None
        s.phase = "plan"
    await db.commit()
    await db.refresh(s)
    out = await session_out(db, s)
    bus.publish(session_id, {"type": "session", "session": out.model_dump(mode="json")})
    return out


@router.patch("/{session_id}/workspace", response_model=SessionOut)
async def set_workspace(session_id: int, body: WorkspaceIn, db: AsyncSession = Depends(get_db)) -> SessionOut:
    s = await _get(db, session_id)
    await _apply_workspace(db, s, body)
    await db.commit()
    await db.refresh(s)
    out = await session_out(db, s)
    bus.publish(session_id, {"type": "session", "session": out.model_dump(mode="json")})
    return out


@router.get("/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(session_id: int, db: AsyncSession = Depends(get_db)) -> list[MessageOut]:
    await _get(db, session_id)
    rows = await db.execute(
        select(Message, Vote.value)
        .outerjoin(Vote, Vote.message_id == Message.id)
        .where(Message.session_id == session_id)
        .order_by(Message.id)
    )
    usage = {
        mid: {"input_tokens": int(i or 0), "output_tokens": int(o or 0), "cost": float(c or 0), "estimated": bool(e)}
        for mid, i, o, c, e in (await db.execute(
            select(Usage.message_id, func.sum(Usage.input_tokens), func.sum(Usage.output_tokens), func.sum(Usage.cost),
                   func.bool_or(Usage.estimated))
            .where(Usage.session_id == session_id)
            .group_by(Usage.message_id)
        )).all()
    }
    out = []
    for m, vote in rows.all():
        item = MessageOut.model_validate(m)
        item.vote = vote or 0
        item.usage = usage.get(m.id)
        out.append(item)
    return out


async def _stream(subscription) -> AsyncIterator[str]:
    yield ": connected\n\n"
    async for event in subscription:
        yield sse(event)


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post("/{session_id}/chat")
async def chat(session_id: int, body: ChatIn, db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    """Start a turn and stream its events (SSE) until it ends. The turn keeps running if the client leaves."""
    await _get(db, session_id)
    subscription = bus.subscribe(session_id, replay=False, until_turn_end=True)
    try:
        await turns.start(session_id, body.content, body.recipients, body.attachment_ids)
    except TurnError as exc:
        subscription.close()
        raise HTTPException(409, str(exc)) from exc
    return StreamingResponse(_stream(subscription), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/{session_id}/events")
async def events(session_id: int, db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    """Follow everything happening in a session (replays the running turn first)."""
    await _get(db, session_id)
    subscription = bus.subscribe(session_id, replay=True, until_turn_end=False)
    return StreamingResponse(_stream(subscription), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/{session_id}/context")
async def context_usage(session_id: int, db: AsyncSession = Depends(get_db)) -> list[dict]:
    """How full each model's context window is (estimated), like Claude Code's /context."""
    await _get(db, session_id)
    return await turns.context_report(session_id)


class CompactIn(BaseModel):
    model_ids: list[int] | None = None  # default: every model in the session


@router.post("/{session_id}/compact")
async def compact(session_id: int, body: CompactIn | None = None, db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Summarise the older history of each model now, like Claude Code's /compact."""
    await _get(db, session_id)
    try:
        return await turns.compact_now(session_id, body.model_ids if body else None)
    except TurnError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{session_id}/stop")
async def stop(session_id: int) -> dict:
    return {"stopped": await turns.stop(session_id)}


@router.get("/{session_id}/approvals")
async def pending_approvals(session_id: int) -> list[str]:
    return approvals.pending_for(session_id)


@router.post("/{session_id}/approvals/{call_id}")
async def resolve_approval(session_id: int, call_id: str, body: ApprovalIn) -> dict:
    if not approvals.resolve(session_id, call_id, Decision(body.approve, body.note)):
        raise HTTPException(404, "no pending approval with this id")
    return {"ok": True}


@router.post("/{session_id}/pick-builder", response_model=SessionOut)
async def pick_builder(session_id: int, body: PickBuilderIn, db: AsyncSession = Depends(get_db)) -> SessionOut:
    s = await _get(db, session_id)
    if body.model_id not in [m.id for m, _ in await participants(db, session_id)]:
        raise HTTPException(422, "the builder must be a participant of the session")
    s.builder_model_id = body.model_id
    if body.switch_to_build:
        s.phase = "build"
    db.add(BuilderPick(session_id=session_id, model_id=body.model_id))
    await db.commit()
    await db.refresh(s)
    out = await session_out(db, s)
    bus.publish(session_id, {"type": "session", "session": out.model_dump(mode="json")})
    return out


@router.get("/{session_id}/usage", response_model=list[UsageRow])
async def session_usage(session_id: int, db: AsyncSession = Depends(get_db)) -> list[UsageRow]:
    """Per model: tokens, cost (split into input and output by the model's prices), replies and prices."""
    rows = await db.execute(
        select(
            Usage.model_id,
            func.sum(Usage.input_tokens),
            func.sum(Usage.output_tokens),
            func.sum(Usage.cost),
            func.bool_or(Usage.estimated),
            func.count(func.distinct(Usage.message_id)),
        )
        .where(Usage.session_id == session_id)
        .group_by(Usage.model_id)
        .order_by(func.sum(Usage.cost).desc())
    )
    out = []
    for mid, i, o, c, est, replies in rows.all():
        model = await db.get(Model, mid) if mid else None
        endpoint = await db.get(Endpoint, model.endpoint_id) if model else None
        i, o, c = int(i or 0), int(o or 0), float(c or 0)
        ip, op = (model.input_price or 0, model.output_price or 0) if model else (0, 0)
        ic, oc = (i * ip / 1_000_000, o * op / 1_000_000) if (ip or op) else (None, None)
        if ic is not None and c > 0 and ic + oc > 0:
            scale = c / (ic + oc)  # vendor-reported totals (OpenRouter) win; keep the in/out proportion
            ic, oc = ic * scale, oc * scale
        out.append(UsageRow(
            model_id=mid,
            display_name=model.display_name if model else "?",
            color=model.color if model else "#6b7280",
            input_tokens=i, output_tokens=o, cost=c, estimated=bool(est), replies=int(replies or 0),
            input_price=ip, output_price=op, price_known=bool(ip or op or c > 0 or (endpoint and endpoint.kind == "mock")),
            input_cost=ic, output_cost=oc,
        ))
    return out


@router.get("/{session_id}/changes", response_model=list[FileChangeOut])
async def list_changes(session_id: int, turn_id: str | None = None, db: AsyncSession = Depends(get_db)) -> list[FileChangeOut]:
    q = select(FileChange).where(FileChange.session_id == session_id).order_by(FileChange.id)
    if turn_id:
        q = q.where(FileChange.turn_id == turn_id)
    return [FileChangeOut.model_validate(c) for c in (await db.execute(q)).scalars()]


@router.post("/{session_id}/revert")
async def revert(session_id: int, body: RevertIn, db: AsyncSession = Depends(get_db)) -> dict:
    s = await _get(db, session_id)
    if turns.busy(session_id):
        raise HTTPException(409, "stop the running turn first")
    turn_id = body.turn_id
    if turn_id is None:
        last = (
            await db.execute(
                select(FileChange.turn_id)
                .where(FileChange.session_id == session_id, FileChange.reverted.is_(False))
                .order_by(FileChange.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last is None:
            raise HTTPException(404, "nothing to revert")
        turn_id = last
    changes = (
        await db.execute(
            select(FileChange)
            .where(FileChange.session_id == session_id, FileChange.turn_id == turn_id, FileChange.reverted.is_(False))
            .order_by(FileChange.id.desc())
        )
    ).scalars().all()
    if not changes:
        raise HTTPException(404, "nothing to revert for this turn")
    try:
        workspace = await workspace_for(db, s)
        restored = []
        for c in changes:
            current = await workspace.read_file(c.path)
            if c.before is None:
                await workspace.delete_file(c.path)
            else:
                await workspace.write_file(c.path, c.before)
            c.reverted = True
            restored.append(c.path)
            bus.publish(
                session_id,
                {"type": "file_changed", "path": c.path, "diff": unified_diff(c.path, current, c.before), "turn_id": turn_id, "reverted": True},
            )
    except WorkspaceError as exc:
        await db.commit()
        raise HTTPException(502, str(exc)) from exc
    await db.commit()
    return {"turn_id": turn_id, "restored": sorted(set(restored))}


class AgentApprovalIn(BaseModel):
    model_id: int
    tool_name: str
    input: dict = {}
    tool_use_id: str | None = None


EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


@router.post("/{session_id}/agent-approval")
async def agent_approval(session_id: int, body: AgentApprovalIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Called by the approval bridge that Claude Code runs on a runner; blocks until the user decides."""
    await _get(db, session_id)
    args = body.input
    if body.tool_name == "Bash":
        payload = {"kind": "command", "command": str(args.get("command", ""))}
    elif body.tool_name in EDIT_TOOLS:
        path = str(args.get("file_path") or args.get("notebook_path") or "")
        if "content" in args:
            diff = unified_diff(path, None, str(args["content"]))
        else:
            diff = unified_diff(path, str(args.get("old_string", "")), str(args.get("new_string", "")))
        payload = {"kind": "edit", "path": path, "diff": diff}
    else:
        payload = {"kind": "tool", "detail": json.dumps(args)[:2000]}
    call_id = body.tool_use_id or f"cli-{uuid.uuid4().hex[:12]}"
    decision = await approvals.request(session_id, call_id, {"model_id": body.model_id, "tool": body.tool_name, **payload})
    return {"approve": decision.approve, "note": decision.note}
