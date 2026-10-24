"""File operations confined to the folders the user allowed this runner to touch."""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

MAX_READ = 2_000_000
MAX_GREP_FILE = 1_000_000
SKIP_DIRS = {".git", ".multichat", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build", ".tox", ".idea"}


class FsError(Exception):
    pass


class Roots:
    def __init__(self, roots: list[str]) -> None:
        self.roots = [Path(r).expanduser().resolve() for r in roots]

    def check(self, path: str | Path) -> Path:
        """Resolve an absolute path and require it to be inside an allowed root."""
        p = Path(path).expanduser().resolve()
        for root in self.roots:
            if p == root or p.is_relative_to(root):
                return p
        raise FsError(f"{path} is outside the folders this runner is allowed to use")

    def join(self, root: str, rel: str) -> Path:
        base = self.check(root)
        target = (base / (rel or ".")).resolve()
        if target != base and not target.is_relative_to(base):
            raise FsError(f"path {rel!r} is outside the workspace")
        return target


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def list_dir(path: Path) -> list[dict]:
    if not path.is_dir():
        raise FsError(f"{path.name or path} is not a folder")
    entries = []
    for entry in os.scandir(path):
        try:
            is_dir = entry.is_dir()
            size = 0 if is_dir else entry.stat().st_size
        except OSError:
            continue
        entries.append({"name": entry.name, "is_dir": is_dir, "size": size})
    return sorted(entries, key=lambda e: (not e["is_dir"], e["name"].lower()))


def read(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    if path.is_dir():
        raise FsError(f"{path.name} is a folder")
    size = path.stat().st_size
    if size > MAX_READ:
        raise FsError(f"{path.name} is too large to read ({size} bytes)")
    data = path.read_bytes()
    if is_binary(data):
        return {"exists": True, "content": f"(binary file, {size} bytes)", "binary": True}
    return {"exists": True, "content": data.decode("utf-8", errors="replace")}


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" keeps the model's line endings exactly as given.
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def delete(path: Path) -> None:
    if path.is_file() or path.is_symlink():
        path.unlink()


def walk_files(base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def grep(root: Path, target: Path, pattern: str, glob: str | None, max_results: int) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise FsError(f"invalid regular expression: {exc}") from exc
    files = [target] if target.is_file() else walk_files(target)
    out: list[str] = []
    for f in files:
        if glob and not fnmatch.fnmatch(f.name, glob):
            continue
        try:
            if f.stat().st_size > MAX_GREP_FILE:
                continue
            data = f.read_bytes()
        except OSError:
            continue
        if is_binary(data):
            continue
        rel = f.relative_to(root).as_posix() if f.is_relative_to(root) else str(f)
        for lineno, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(line):
                out.append(f"{rel}:{lineno}:{line[:300]}")
                if len(out) >= max_results:
                    return "\n".join(out) + f"\n… (stopped at {max_results} matches)"
    return "\n".join(out)
