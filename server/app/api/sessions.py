from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
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
from app.tables import BuilderPick, FileChange, Message, Model, Runner, Session, SessionModel, SshConnection, Usage, Vote
from app.workspaces.base import WorkspaceError
from app.workspaces.factory import workspace_for, workspace_label

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


async def _get(db: AsyncSession, session_id: int) -> Session:
    row = await db.get(Session, session_id)
    if row is None:
        raise HTTPException(404, "session not found")
    return row


async def session_out(db: AsyncSession, s: Session) -> SessionOut:
    out = SessionOut.model_validate(s)
    out.participants = [
        ParticipantOut(model_id=m.id, display_name=m.display_name, color=m.color, endpoint_kind=e.kind, position=i)
        for i, (m, e) in enumerate(await participants(db, s.id))
    ]
    out.workspace_label = await workspace_label(db, s)
    out.busy = turns.busy(s.id)
    return out


async def _set_models(db: AsyncSession, session_id: int, model_ids: list[int]) -> None:
    for mid in model_ids:
        if await db.get(Model, mid) is None:
            raise HTTPException(404, f"model {mid} not found")
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
    out = []
    for m, vote in rows.all():
        item = MessageOut.model_validate(m)
        item.vote = vote or 0
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
        await turns.start(session_id, body.content, body.recipients)
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
    rows = await db.execute(
        select(
            Usage.model_id,
            func.coalesce(Model.display_name, "?"),
            func.coalesce(Model.color, "#6b7280"),
            func.sum(Usage.input_tokens),
            func.sum(Usage.output_tokens),
            func.sum(Usage.cost),
            func.bool_or(Usage.estimated),
        )
        .outerjoin(Model, Model.id == Usage.model_id)
        .where(Usage.session_id == session_id)
        .group_by(Usage.model_id, Model.display_name, Model.color)
        .order_by(func.sum(Usage.cost).desc())
    )
    return [
        UsageRow(
            model_id=mid,
            display_name=name,
            color=color,
            input_tokens=int(i or 0),
            output_tokens=int(o or 0),
            cost=float(c or 0),
            estimated=bool(est),
        )
        for mid, name, color, i, o, c, est in rows.all()
    ]


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
