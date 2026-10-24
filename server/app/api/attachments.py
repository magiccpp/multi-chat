import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.attachments import AttachmentError, process
from app.db import get_db
from app.schemas import AttachmentIn, AttachmentOut
from app.tables import Attachment, Session

router = APIRouter(tags=["attachments"])


def attachment_out(a: Attachment, text_chars: int | None = None) -> AttachmentOut:
    return AttachmentOut(id=a.id, filename=a.filename, mime=a.mime, size=a.size, kind=a.kind, text_chars=text_chars)


@router.post("/api/sessions/{session_id}/attachments", response_model=AttachmentOut, status_code=201)
async def upload(session_id: int, body: AttachmentIn, db: AsyncSession = Depends(get_db)) -> AttachmentOut:
    """Upload a file (base64). Send its id with the next chat message to attach it."""
    if await db.get(Session, session_id) is None:
        raise HTTPException(404, "session not found")
    try:
        raw = base64.b64decode(body.data_base64, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(422, "data_base64 is not valid base64") from exc
    try:
        p = process(body.filename, body.mime, raw)
    except AttachmentError as exc:
        raise HTTPException(422, str(exc)) from exc
    row = Attachment(session_id=session_id, filename=p.filename, mime=p.mime, size=len(p.data), kind=p.kind, data=p.data, text=p.text)
    db.add(row)
    await db.commit()
    return attachment_out(row, len(p.text) if p.text is not None else None)


@router.get("/api/attachments/{attachment_id}")
async def download(attachment_id: int, db: AsyncSession = Depends(get_db)) -> Response:
    row = await db.get(Attachment, attachment_id, options=[undefer(Attachment.data)])
    if row is None:
        raise HTTPException(404, "attachment not found")
    safe = row.filename.replace('"', "")
    return Response(row.data, media_type=row.mime, headers={"Content-Disposition": f'inline; filename="{safe}"'})
