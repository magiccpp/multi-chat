from fastapi import APIRouter, Depends, HTTPException, WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_token, websocket_authorized
from app.db import get_db
from app.runners.hub import RunnerError, RunnerOffline, hub
from app.schemas import RunnerOut
from app.tables import Runner

router = APIRouter(prefix="/api/runners", tags=["runners"])
ws_router = APIRouter()


def runner_out(r: Runner) -> RunnerOut:
    out = RunnerOut.model_validate(r)
    out.online = hub.online(r.id)
    return out


@router.get("", response_model=list[RunnerOut], dependencies=[Depends(require_token)])
async def list_runners(db: AsyncSession = Depends(get_db)) -> list[RunnerOut]:
    return [runner_out(r) for r in (await db.execute(select(Runner).order_by(Runner.name))).scalars()]


@router.delete("/{runner_id}", status_code=204, dependencies=[Depends(require_token)])
async def delete_runner(runner_id: int, db: AsyncSession = Depends(get_db)) -> None:
    row = await db.get(Runner, runner_id)
    if row is None:
        raise HTTPException(404, "runner not found")
    await db.delete(row)
    await db.commit()


@router.get("/{runner_id}/browse", dependencies=[Depends(require_token)])
async def browse(runner_id: int, path: str = "") -> dict:
    """List folders on the runner machine (inside its allowed roots) for the folder picker."""
    try:
        return await hub.get(runner_id).call("fs.browse", {"path": path}, timeout=30)
    except RunnerOffline as exc:
        raise HTTPException(409, str(exc)) from exc
    except RunnerError as exc:
        raise HTTPException(400, str(exc)) from exc


@ws_router.websocket("/api/runners/connect")
async def connect(ws: WebSocket) -> None:
    if not websocket_authorized(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    await hub.serve(ws)
