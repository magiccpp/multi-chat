from sqlalchemy.ext.asyncio import AsyncSession

from app.runners.hub import hub
from app.tables import Runner, Session, SshConnection
from app.workspaces.base import NoWorkspace, Workspace, WorkspaceError
from app.workspaces.runner import RunnerWorkspace
from app.workspaces.ssh import SshWorkspace


async def workspace_for(db: AsyncSession, session: Session) -> Workspace:
    if session.workspace_kind == "runner" and session.runner_id and session.workspace_path:
        runner = await db.get(Runner, session.runner_id)
        if runner is None:
            raise WorkspaceError("the runner attached to this session was removed")
        conn = hub.connections.get(runner.id)
        hello = conn.hello if conn else {}
        return RunnerWorkspace(
            runner.id,
            runner.name,
            session.workspace_path,
            hello.get("os", runner.os),
            hello.get("shell", runner.shell),
            hello.get("hostname", runner.hostname),
            hello.get("clis", runner.clis),
        )
    if session.workspace_kind == "ssh" and session.ssh_connection_id and session.workspace_path:
        row = await db.get(SshConnection, session.ssh_connection_id)
        if row is None:
            raise WorkspaceError("the SSH connection attached to this session was removed")
        return SshWorkspace(row, session.workspace_path)
    return NoWorkspace()


async def workspace_label(db: AsyncSession, session: Session) -> str:
    if session.workspace_kind == "runner" and session.runner_id:
        runner = await db.get(Runner, session.runner_id)
        return f"{runner.name if runner else '?'}:{session.workspace_path}"
    if session.workspace_kind == "ssh" and session.ssh_connection_id:
        row = await db.get(SshConnection, session.ssh_connection_id)
        return f"ssh:{row.username}@{row.host}:{session.workspace_path}" if row else f"ssh:?:{session.workspace_path}"
    return "No folder"
