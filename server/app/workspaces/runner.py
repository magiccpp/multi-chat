from __future__ import annotations

from typing import Any

from app.runners.hub import RunnerError, RunnerOffline, hub
from app.workspaces.base import (
    Change,
    CommandResult,
    DirEntry,
    LineCallback,
    OutputCallback,
    Workspace,
    WorkspaceError,
    WorkspaceInfo,
)


class RunnerWorkspace(Workspace):
    """Tools executed on the user's own machine by a connected runner (desktop app / TUI)."""

    def __init__(self, runner_id: int, runner_name: str, root: str, os_name: str, shell: str, hostname: str, clis: dict):
        self.runner_id = runner_id
        self.root = root
        self.info = WorkspaceInfo(
            kind="runner",
            label=f"{runner_name}:{root}",
            os=os_name,
            shell=shell,
            root=root,
            hostname=hostname,
            clis=clis,
        )

    async def _call(self, method: str, params: dict[str, Any], **kw: Any) -> dict[str, Any]:
        try:
            return await hub.get(self.runner_id).call(method, {"root": self.root, **params}, **kw)
        except RunnerOffline as exc:
            raise WorkspaceError(f"{exc}; start the MultiChat desktop app or `multichat runner` on that machine") from exc
        except RunnerError as exc:
            raise WorkspaceError(str(exc)) from exc
        except TimeoutError as exc:
            raise WorkspaceError(f"runner did not answer {method} in time") from exc

    async def list_dir(self, path: str) -> list[DirEntry]:
        result = await self._call("fs.list", {"path": path})
        return [DirEntry(e["name"], bool(e["is_dir"]), int(e.get("size") or 0)) for e in result["entries"]]

    async def read_file(self, path: str) -> str | None:
        result = await self._call("fs.read", {"path": path})
        return result["content"] if result.get("exists") else None

    async def write_file(self, path: str, content: str) -> None:
        await self._call("fs.write", {"path": path, "content": content})

    async def delete_file(self, path: str) -> None:
        await self._call("fs.delete", {"path": path})

    async def grep(self, pattern: str, path: str, glob: str | None, max_results: int) -> str:
        result = await self._call("fs.grep", {"pattern": pattern, "path": path, "glob": glob, "max_results": max_results})
        return result["output"]

    async def run_command(self, command: str, timeout: int, on_output: OutputCallback | None) -> CommandResult:
        chunks: list[str] = []

        async def on_stream(data: dict[str, Any]) -> None:
            text = data.get("text", "")
            chunks.append(text)
            if on_output:
                await on_output(text)

        result = await self._call(
            "shell.run", {"command": command, "timeout": timeout}, on_stream=on_stream, timeout=timeout + 30
        )
        return CommandResult(
            exit_code=result.get("exit_code"),
            output=result.get("output") or "".join(chunks),
            timed_out=bool(result.get("timed_out")),
        )

    async def snapshot_begin(self) -> str | None:
        return (await self._call("snapshot.begin", {}, timeout=300)).get("ref")

    async def snapshot_changes(self, ref: str) -> list[Change]:
        result = await self._call("snapshot.changes", {"ref": ref}, timeout=300)
        return [Change(c["path"], c.get("before"), c.get("after")) for c in result.get("changes", [])]

    async def run_agent_cli(
        self,
        driver: str,
        args: list[str],
        env: dict[str, str],
        stdin: str | None,
        on_line: LineCallback,
        approval: dict[str, Any] | None = None,
    ) -> int:
        async def on_stream(data: dict[str, Any]) -> None:
            if "line" in data:
                await on_line(data["line"], bool(data.get("stderr")))

        result = await self._call(
            "agent_cli.run",
            {"driver": driver, "args": args, "env": env, "stdin": stdin, "approval": approval},
            on_stream=on_stream,
            timeout=None,
        )
        return int(result.get("exit_code") or 0)
