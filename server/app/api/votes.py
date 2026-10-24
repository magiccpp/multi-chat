from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.events import bus
from app.db import get_db
from app.schemas import VoteIn
from app.tables import Message, Vote

router = APIRouter(prefix="/api", tags=["votes"])


async def _resolve(db: AsyncSession, ref: str, session_id: int | None) -> Message:
    """`ref` is a message id, or `#n` (per-session index) together with session_id."""
    if ref.startswith("#"):
        if session_id is None:
            raise HTTPException(422, "#index references need session_id")
        msg = (
            await db.execute(select(Message).where(Message.session_id == session_id, Message.seq == int(ref[1:])))
        ).scalar_one_or_none()
    else:
        msg = await db.get(Message, int(ref))
    if msg is None:
        raise HTTPException(404, "message not found")
    if msg.role != "assistant" or msg.model_id is None:
        raise HTTPException(422, "only model replies can be voted on")
    return msg


@router.put("/messages/{ref}/vote")
async def vote(ref: str, body: VoteIn, session_id: int | None = None, db: AsyncSession = Depends(get_db)) -> dict:
    msg = await _resolve(db, ref, session_id)
    row = (await db.execute(select(Vote).where(Vote.message_id == msg.id))).scalar_one_or_none()
    if body.value == 0:
        if row is not None:
            await db.delete(row)
    elif row is None:
        db.add(Vote(message_id=msg.id, value=body.value))
    else:
        row.value = body.value
    await db.commit()
    bus.publish(msg.session_id, {"type": "vote", "message_id": msg.id, "value": body.value})
    return {"message_id": msg.id, "seq": msg.seq, "value": body.value}
