"""Before/after snapshots of a folder so changes made by agent CLIs can be shown and reverted.

Git repos: the working tree is staged into a throw-away index and written as a tree
object (the user's index, stash and branches are untouched). Other folders: small text
files are copied to a cache directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from multichat_client.config import config_dir
from multichat_client.runner.fs import SKIP_DIRS, is_binary

MAX_FILE = 1_000_000
MAX_TOTAL = 100_000_000
MAX_FILES = 20_000
CACHE = config_dir() / "snapshots"


def _git(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, env={**os.environ, **(env or {})}, check=False
    )


def _is_git(root: Path) -> bool:
    if not shutil.which("git"):
        return False
    r = _git(root, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == b"true"


def _write_tree(root: Path) -> str | None:
    fd, tmp = tempfile.mkstemp(prefix="multichat-index-")
    os.close(fd)
    try:
        index = _git(root, "rev-parse", "--git-path", "index").stdout.decode().strip()
        index_path = (root / index) if index and not os.path.isabs(index) else Path(index)
        if index_path.exists():
            shutil.copyfile(index_path, tmp)
        else:
            os.unlink(tmp)
        env = {"GIT_INDEX_FILE": tmp}
        if _git(root, "add", "-A", ".", env=env).returncode != 0:
            return None
        r = _git(root, "write-tree", env=env)
        return r.stdout.decode().strip() if r.returncode == 0 else None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _git_show(root: Path, tree: str, path: str) -> str | None:
    size = _git(root, "cat-file", "-s", f"{tree}:{path}")
    if size.returncode != 0:
        return None
    if int(size.stdout.strip() or 0) > MAX_FILE:
        return "(file too large to snapshot)"
    r = _git(root, "show", f"{tree}:{path}")
    if r.returncode != 0:
        return None
    if is_binary(r.stdout):
        return "(binary file)"
    return r.stdout.decode("utf-8", errors="replace")


def _files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                if p.is_symlink() or p.stat().st_size > MAX_FILE:
                    continue
            except OSError:
                continue
            yield p


def _digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def begin(root: Path) -> str:
    if _is_git(root):
        tree = _write_tree(root)
        if tree:
            return f"git:{tree}"
    snap_id = uuid.uuid4().hex
    dest = CACHE / snap_id
    dest.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    total = 0
    for p in _files(root):
        data = p.read_bytes()
        if is_binary(data):
            continue
        total += len(data)
        if total > MAX_TOTAL or len(manifest) >= MAX_FILES:
            break
        rel = p.relative_to(root).as_posix()
        manifest[rel] = _digest(data)
        target = dest / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (dest / "manifest.json").write_text(json.dumps(manifest))
    return f"copy:{snap_id}"


def _text(path: Path) -> str:
    # bytes, not read_text(): keep CRLF as-is so before/after compare equally on Windows
    return path.read_bytes().decode("utf-8", errors="replace")


def changes(root: Path, ref: str) -> list[dict]:
    kind, _, value = ref.partition(":")
    if kind == "git":
        new = _write_tree(root)
        if not new:
            return []
        r = _git(root, "diff-tree", "-r", "--no-renames", "--name-only", value, new)
        paths = [p for p in r.stdout.decode().splitlines() if p.strip()]
        return [{"path": p, "before": _git_show(root, value, p), "after": _git_show(root, new, p)} for p in paths]

    dest = CACHE / value
    manifest: dict[str, str] = json.loads((dest / "manifest.json").read_text())
    result = []
    seen = set()
    for p in _files(root):
        rel = p.relative_to(root).as_posix()
        seen.add(rel)
        data = p.read_bytes()
        if is_binary(data):
            continue
        if manifest.get(rel) != _digest(data):
            before = _text(dest / "files" / rel) if rel in manifest else None
            result.append({"path": rel, "before": before, "after": data.decode("utf-8", errors="replace")})
    for rel in manifest.keys() - seen:
        result.append({"path": rel, "before": _text(dest / "files" / rel), "after": None})
    shutil.rmtree(dest, ignore_errors=True)
    return result
