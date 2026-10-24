"""Workspace snapshots via git trees, without touching the user's index, stash or branches.

`begin` stages the whole working tree (respecting .gitignore) into a throw-away index and
writes a tree object; `changes` does the same again and diffs the two trees.
Works over any "run a shell command" primitive, so SSH and runners share the logic.
"""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable

from app.workspaces.base import Change

Exec = Callable[[str], Awaitable[tuple[int, str]]]
MAX_FILE = 1_000_000

_WRITE_TREE = (
    'tmp=$(mktemp) && idx=$(git rev-parse --git-path index) && {{ cp "$idx" "$tmp" 2>/dev/null || true; }} '
    '&& GIT_INDEX_FILE="$tmp" git add -A {path} >/dev/null 2>&1 && GIT_INDEX_FILE="$tmp" git write-tree; rc=$?; rm -f "$tmp"; exit $rc'
)


async def begin(run: Exec) -> str | None:
    code, out = await run("git rev-parse --is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        return None
    code, out = await run(_WRITE_TREE.format(path="."))
    return out.strip().splitlines()[-1] if code == 0 and out.strip() else None


async def _show(run: Exec, tree: str, path: str) -> str | None:
    code, out = await run(f"git cat-file -s {tree}:{shlex.quote(path)}")
    if code != 0:
        return None
    if int(out.strip() or 0) > MAX_FILE:
        return "(file too large to snapshot)"
    code, out = await run(f"git show {tree}:{shlex.quote(path)}")
    return out if code == 0 else None


async def changes(run: Exec, ref: str) -> list[Change]:
    code, out = await run(_WRITE_TREE.format(path="."))
    if code != 0 or not out.strip():
        return []
    new = out.strip().splitlines()[-1]
    code, out = await run(f"git diff-tree -r --no-renames --name-only {ref} {new}")
    if code != 0:
        return []
    result = []
    for path in [p for p in out.splitlines() if p.strip()]:
        result.append(Change(path=path, before=await _show(run, ref, path), after=await _show(run, new, path)))
    return result
