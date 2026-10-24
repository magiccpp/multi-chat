from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crypto
from app.db import get_db
from app.schemas import HostKeyTrust, SshIn, SshOut, SshPatch
from app.tables import SshConnection
from app.workspaces import ssh
from app.workspaces.base import WorkspaceError

router = APIRouter(prefix="/api/ssh", tags=["ssh"])


def ssh_out(row: SshConnection) -> SshOut:
    out = SshOut.model_validate(row)
    out.host_key_fingerprint = ssh.fingerprint(row.host_key)
    return out


async def _get(db: AsyncSession, conn_id: int) -> SshConnection:
    row = await db.get(SshConnection, conn_id)
    if row is None:
        raise HTTPException(404, "ssh connection not found")
    return row


@router.get("", response_model=list[SshOut])
async def list_connections(db: AsyncSession = Depends(get_db)) -> list[SshOut]:
    return [ssh_out(r) for r in (await db.execute(select(SshConnection).order_by(SshConnection.name))).scalars()]


@router.post("", response_model=SshOut, status_code=201)
async def create_connection(body: SshIn, db: AsyncSession = Depends(get_db)) -> SshOut:
    row = SshConnection(
        name=body.name,
        host=body.host,
        port=body.port,
        username=body.username,
        auth=body.auth,
        private_key_enc=crypto.encrypt(body.private_key),
        passphrase_enc=crypto.encrypt(body.passphrase),
        password_enc=crypto.encrypt(body.password),
        shell=body.shell,
        source="api",
    )
    db.add(row)
    await db.commit()
    return ssh_out(row)


@router.patch("/{conn_id}", response_model=SshOut)
async def update_connection(conn_id: int, body: SshPatch, db: AsyncSession = Depends(get_db)) -> SshOut:
    row = await _get(db, conn_id)
    if row.source == "config":
        raise HTTPException(409, "this connection is managed by config.yaml on the server")
    data = body.model_dump(exclude_unset=True)
    for secret in ("private_key", "passphrase", "password"):
        if secret in data:
            setattr(row, f"{secret}_enc", crypto.encrypt(data.pop(secret)))
    if {"host", "port"} & data.keys():
        row.host_key = None  # a different host must be trusted again
    for field, value in data.items():
        setattr(row, field, value)
    await db.commit()
    return ssh_out(row)


@router.delete("/{conn_id}", status_code=204)
async def delete_connection(conn_id: int, db: AsyncSession = Depends(get_db)) -> None:
    row = await _get(db, conn_id)
    if row.source == "config":
        raise HTTPException(409, "this connection is managed by config.yaml on the server")
    await db.delete(row)
    await db.commit()


@router.post("/{conn_id}/scan-host-key")
async def scan_host_key(conn_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await _get(db, conn_id)
    try:
        key, fp = await ssh.scan_host_key(row.host, row.port)
    except (OSError, TimeoutError, WorkspaceError) as exc:
        raise HTTPException(502, f"cannot reach {row.host}:{row.port}: {exc}") from exc
    return {"fingerprint": fp, "key": key, "pinned": ssh.fingerprint(row.host_key), "matches": row.host_key == key}


@router.post("/{conn_id}/trust-host-key", response_model=SshOut)
async def trust_host_key(conn_id: int, body: HostKeyTrust, db: AsyncSession = Depends(get_db)) -> SshOut:
    """Pin the host key, but only if the server still presents the fingerprint the user confirmed."""
    row = await _get(db, conn_id)
    key, fp = await ssh.scan_host_key(row.host, row.port)
    if fp != body.fingerprint:
        raise HTTPException(409, f"host now presents {fp}, not the confirmed {body.fingerprint}")
    row.host_key = key
    await db.commit()
    return ssh_out(row)


@router.post("/{conn_id}/test")
async def test_connection(conn_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await _get(db, conn_id)
    try:
        conn = await ssh.connect(row)
        result = await conn.run("echo ok", check=False)
        return {"ok": True, "detail": str(result.stdout or "").strip() or "connected"}
    except WorkspaceError as exc:
        return {"ok": False, "detail": str(exc)}


@router.get("/{conn_id}/browse")
async def browse(conn_id: int, path: str = "", db: AsyncSession = Depends(get_db)) -> dict:
    row = await _get(db, conn_id)
    try:
        return await ssh.browse(row, path)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc
