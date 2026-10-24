from __future__ import annotations

import asyncio
import base64
import posixpath
import shlex
import stat as statmod
from typing import Any

import asyncssh

from app import crypto
from app.tables import SshConnection
from app.workspaces import gitsnap
from app.workspaces.base import (
    Change,
    CommandResult,
    DirEntry,
    OutputCallback,
    Workspace,
    WorkspaceError,
    WorkspaceInfo,
    confine,
)

MAX_READ = 2_000_000
_pool: dict[tuple, asyncssh.SSHClientConnection] = {}
_pool_lock = asyncio.Lock()


def _pattern(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


async def scan_host_key(host: str, port: int) -> tuple[str, str]:
    """Return (openssh public key line, SHA256 fingerprint) offered by the server."""
    key = await asyncio.wait_for(asyncssh.get_server_host_key(host, port), 15)
    if key is None:
        raise WorkspaceError("server did not present a host key")
    return key.export_public_key().decode().strip(), key.get_fingerprint()


def fingerprint(key_line: str | None) -> str | None:
    if not key_line:
        return None
    try:
        return asyncssh.import_public_key(key_line).get_fingerprint()
    except (asyncssh.KeyImportError, ValueError):
        return None


def _options(row: SshConnection) -> dict[str, Any]:
    if not row.host_key:
        raise WorkspaceError(f"host key for {row.name} is not trusted yet; confirm its fingerprint first")
    opts: dict[str, Any] = {
        "host": row.host,
        "port": row.port,
        "username": row.username,
        "known_hosts": asyncssh.import_known_hosts(f"{_pattern(row.host, row.port)} {row.host_key}\n"),
        "connect_timeout": 15,
    }
    if row.auth == "password":
        opts["password"] = crypto.decrypt(row.password_enc)
        opts["client_keys"] = None
        opts["agent_path"] = None
    elif row.auth == "key":
        key_text = crypto.decrypt(row.private_key_enc)
        if not key_text:
            raise WorkspaceError(f"no private key stored for {row.name}")
        opts["client_keys"] = [asyncssh.import_private_key(key_text, crypto.decrypt(row.passphrase_enc))]
        opts["agent_path"] = None
    return opts


async def connect(row: SshConnection) -> asyncssh.SSHClientConnection:
    key = (row.id, row.host, row.port, row.username, row.host_key, row.private_key_enc, row.password_enc)
    async with _pool_lock:
        conn = _pool.get(key)
        if conn is not None and not conn.is_closed():
            return conn
        try:
            conn = await asyncssh.connect(**_options(row))
        except asyncssh.HostKeyNotVerifiable as exc:
            raise WorkspaceError(
                f"host key of {row.host} does not match the pinned key; refusing to connect ({exc})"
            ) from exc
        except (OSError, asyncssh.Error) as exc:
            raise WorkspaceError(f"ssh connection to {row.host} failed: {exc}") from exc
        _pool[key] = conn
        return conn


def _ps_encoded(script: str) -> str:
    return "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(
        script.encode("utf-16-le")
    ).decode()


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class SshWorkspace(Workspace):
    def __init__(self, row: SshConnection, root: str):
        self.row = row
        self.powershell = row.shell == "powershell"
        self.root = root
        self.info = WorkspaceInfo(
            kind="ssh",
            label=f"ssh:{row.username}@{row.host}:{root}",
            os="Windows" if self.powershell else "Linux/Unix",
            shell="powershell" if self.powershell else "bash",
            root=root,
            hostname=row.host,
        )

    def _path(self, path: str) -> str:
        # SFTP paths use forward slashes even on Windows OpenSSH (e.g. /C:/src/app).
        return confine(self.root.replace("\\", "/"), (path or ".").replace("\\", "/"))

    async def _sftp(self) -> asyncssh.SFTPClient:
        conn = await connect(self.row)
        return await conn.start_sftp_client()

    async def list_dir(self, path: str) -> list[DirEntry]:
        full = self._path(path)
        try:
            async with await self._sftp() as sftp:
                names = await sftp.readdir(full)
        except asyncssh.SFTPError as exc:
            raise WorkspaceError(f"cannot list {path or '.'}: {exc.reason}") from exc
        entries = []
        for n in names:
            if n.filename in (".", ".."):
                continue
            is_dir = statmod.S_ISDIR(n.attrs.permissions or 0)
            entries.append(DirEntry(n.filename, is_dir, n.attrs.size or 0))
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))

    async def read_file(self, path: str) -> str | None:
        full = self._path(path)
        try:
            async with await self._sftp() as sftp:
                if not await sftp.exists(full):
                    return None
                attrs = await sftp.stat(full)
                if (attrs.size or 0) > MAX_READ:
                    raise WorkspaceError(f"{path} is too large to read ({attrs.size} bytes)")
                async with sftp.open(full, "rb") as f:
                    data = await f.read()
        except asyncssh.SFTPError as exc:
            raise WorkspaceError(f"cannot read {path}: {exc.reason}") from exc
        return data.decode("utf-8", errors="replace")

    async def write_file(self, path: str, content: str) -> None:
        full = self._path(path)
        try:
            async with await self._sftp() as sftp:
                parent = posixpath.dirname(full)
                if parent and not await sftp.exists(parent):
                    await sftp.makedirs(parent, exist_ok=True)
                async with sftp.open(full, "wb") as f:
                    await f.write(content.encode())
        except asyncssh.SFTPError as exc:
            raise WorkspaceError(f"cannot write {path}: {exc.reason}") from exc

    async def delete_file(self, path: str) -> None:
        full = self._path(path)
        try:
            async with await self._sftp() as sftp:
                if await sftp.exists(full):
                    await sftp.remove(full)
        except asyncssh.SFTPError as exc:
            raise WorkspaceError(f"cannot delete {path}: {exc.reason}") from exc

    def _wrap(self, command: str) -> str:
        if self.powershell:
            root = self.root.lstrip("/") if self.root[2:3] == ":" else self.root
            return _ps_encoded(f"Set-Location -LiteralPath {_ps_quote(root)}; {command}")
        return f"cd {shlex.quote(self.root)} && {command}"

    async def _exec(self, command: str, timeout: int = 120) -> tuple[int, str]:
        conn = await connect(self.row)
        try:
            result = await asyncio.wait_for(conn.run(self._wrap(command), check=False), timeout)
        except TimeoutError as exc:
            raise WorkspaceError(f"command timed out after {timeout}s") from exc
        out = (result.stdout or "") + (result.stderr or "")
        return int(result.exit_status if result.exit_status is not None else -1), str(out)

    async def grep(self, pattern: str, path: str, glob: str | None, max_results: int) -> str:
        target = self._path(path)
        if self.powershell:
            filt = f"-Filter {_ps_quote(glob)} " if glob else ""
            cmd = (
                f"Get-ChildItem -LiteralPath {_ps_quote(target)} -Recurse -File {filt}-ErrorAction SilentlyContinue | "
                f"Select-String -Pattern {_ps_quote(pattern)} | Select-Object -First {max_results} | "
                'ForEach-Object { "$($_.Path):$($_.LineNumber):$($_.Line)" }'
            )
        else:
            inc = f"--include={shlex.quote(glob)} " if glob else ""
            cmd = (
                f"grep -rnIE --exclude-dir=.git --exclude-dir=node_modules {inc}-e {shlex.quote(pattern)} "
                f"{shlex.quote(target)} | head -n {int(max_results)}"
            )
        _, out = await self._exec(cmd)
        return out.replace(self.root.rstrip("/") + "/", "")

    async def run_command(self, command: str, timeout: int, on_output: OutputCallback | None) -> CommandResult:
        conn = await connect(self.row)
        chunks: list[str] = []
        process = await conn.create_process(self._wrap(command), stderr=asyncssh.STDOUT)
        process.stdin.write_eof()

        async def pump() -> None:
            while True:
                data = await process.stdout.read(4096)
                if not data:
                    break
                chunks.append(data)
                if on_output:
                    await on_output(data)

        timed_out = False
        try:
            await asyncio.wait_for(pump(), timeout)
            await process.wait()
        except TimeoutError:
            timed_out = True
            process.kill()
        return CommandResult(exit_code=process.exit_status, output="".join(chunks), timed_out=timed_out)

    async def snapshot_begin(self) -> str | None:
        if self.powershell:
            return None
        return await gitsnap.begin(self._exec)

    async def snapshot_changes(self, ref: str) -> list[Change]:
        return await gitsnap.changes(self._exec, ref)

    async def run_agent_cli(
        self, driver: str, args: list[str], env: dict[str, str], stdin: str | None, on_line: OutputCallback
    ) -> int:
        exe = {"claude_code": "claude", "cursor": "cursor-agent"}[driver]
        if self.powershell:
            sets = "".join(f"$env:{k}={_ps_quote(v)}; " for k, v in env.items())
            command = sets + "& " + " ".join(_ps_quote(a) for a in [exe, *args])
        else:
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            command = f"{exports} {shlex.join([exe, *args])}".strip()
        conn = await connect(self.row)
        process = await conn.create_process(self._wrap(command))
        if stdin:
            process.stdin.write(stdin)
        process.stdin.write_eof()
        async for line in process.stdout:
            await on_line(line.rstrip("\n"))
        await process.wait()
        return int(process.exit_status or 0)


async def browse(row: SshConnection, path: str) -> dict[str, Any]:
    conn = await connect(row)
    async with conn.start_sftp_client() as sftp:
        target = path or await sftp.realpath(".")
        try:
            names = await sftp.readdir(target)
        except asyncssh.SFTPError as exc:
            raise WorkspaceError(f"cannot list {target}: {exc.reason}") from exc
        dirs = sorted(
            n.filename
            for n in names
            if n.filename not in (".", "..") and statmod.S_ISDIR(n.attrs.permissions or 0)
        )
    return {"path": target, "entries": [{"name": d, "is_dir": True} for d in dirs]}
