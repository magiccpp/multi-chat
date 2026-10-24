from __future__ import annotations

import posixpath
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

OutputCallback = Callable[[str], Awaitable[None]]
LineCallback = Callable[[str, bool], Awaitable[None]]  # (line, is_stderr)


class WorkspaceError(Exception):
    """A tool-level failure that is reported back to the model as the tool result."""


@dataclass
class WorkspaceInfo:
    kind: str  # none | runner | ssh
    label: str = ""
    os: str = ""
    shell: str = ""
    root: str = ""
    hostname: str = ""
    clis: dict[str, Any] = field(default_factory=dict)


@dataclass
class DirEntry:
    name: str
    is_dir: bool
    size: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "is_dir": self.is_dir, "size": self.size}


@dataclass
class CommandResult:
    exit_code: int | None
    output: str
    timed_out: bool = False


@dataclass
class Change:
    path: str
    before: str | None
    after: str | None


def confine(root: str, path: str, *, windows: bool = False) -> str:
    """Join a model-supplied relative path onto root, refusing anything that escapes it."""
    if windows:
        import ntpath

        root_n = ntpath.normpath(root)
        full = ntpath.normpath(ntpath.join(root_n, path or "."))
        if not (full.lower() == root_n.lower() or full.lower().startswith(root_n.lower().rstrip("\\") + "\\")):
            raise WorkspaceError(f"path {path!r} is outside the workspace")
        return full
    root_n = posixpath.normpath(root)
    full = posixpath.normpath(posixpath.join(root_n, path or "."))
    if not (full == root_n or full.startswith(root_n.rstrip("/") + "/")):
        raise WorkspaceError(f"path {path!r} is outside the workspace")
    return full


class Workspace(ABC):
    info: WorkspaceInfo
    supports_tools = True

    @abstractmethod
    async def list_dir(self, path: str) -> list[DirEntry]: ...

    @abstractmethod
    async def read_file(self, path: str) -> str | None:
        """Return file text, or None if it does not exist."""

    @abstractmethod
    async def write_file(self, path: str, content: str) -> None: ...

    @abstractmethod
    async def delete_file(self, path: str) -> None: ...

    @abstractmethod
    async def grep(self, pattern: str, path: str, glob: str | None, max_results: int) -> str: ...

    @abstractmethod
    async def run_command(self, command: str, timeout: int, on_output: OutputCallback | None) -> CommandResult: ...

    async def snapshot_begin(self) -> str | None:
        return None

    async def snapshot_changes(self, ref: str) -> list[Change]:
        return []

    async def run_agent_cli(
        self,
        driver: str,
        args: list[str],
        env: dict[str, str],
        stdin: str | None,
        on_line: LineCallback,
        approval: dict[str, Any] | None = None,
    ) -> int:
        raise WorkspaceError("this workspace cannot run agent CLIs")

    async def close(self) -> None:
        return None


class NoWorkspace(Workspace):
    supports_tools = False

    def __init__(self) -> None:
        self.info = WorkspaceInfo(kind="none", label="No folder")

    async def _no(self, *_: Any, **__: Any) -> Any:
        raise WorkspaceError("no folder is attached to this session")

    list_dir = read_file = write_file = delete_file = grep = run_command = _no  # type: ignore[assignment]
