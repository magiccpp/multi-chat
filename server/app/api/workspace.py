from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.tables import Session
from app.workspaces.base import WorkspaceError
from app.workspaces.factory import workspace_for

router = APIRouter(prefix="/api/sessions/{session_id}/workspace", tags=["workspace"])


async def _workspace(db: AsyncSession, session_id: int):  # noqa: ANN202
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    try:
        ws = await workspace_for(db, session)
    except WorkspaceError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not ws.supports_tools:
        raise HTTPException(409, "no folder is attached to this session")
    return ws


@router.get("/tree")
async def tree(session_id: int, path: str = ".", db: AsyncSession = Depends(get_db)) -> dict:
    ws = await _workspace(db, session_id)
    try:
        entries = await ws.list_dir(path)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"path": path, "entries": [e.to_json() for e in entries]}


@router.get("/file")
async def file(session_id: int, path: str, db: AsyncSession = Depends(get_db)) -> dict:
    ws = await _workspace(db, session_id)
    try:
        content = await ws.read_file(path)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc
    if content is None:
        raise HTTPException(404, "file not found")
    return {"path": path, "content": content}
