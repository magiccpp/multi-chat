"""Memory, as in Claude Code: the user's global memory (stored on the server, like ~/.claude/CLAUDE.md) and the
project's own instruction file in the attached folder (CLAUDE.md, AGENTS.md or MULTICHAT.md). Both go into every
model's system prompt."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config_sync import get_setting, set_setting
from app.workspaces.base import Workspace, WorkspaceError

PROJECT_FILES = ("MULTICHAT.md", "CLAUDE.md", "AGENTS.md")
MAX_CHARS = 20_000


async def load(db: AsyncSession) -> str:
    return str((await get_setting(db, "memory")).get("text", ""))


async def save(db: AsyncSession, text: str) -> str:
    text = text.strip()[: MAX_CHARS * 2]
    await set_setting(db, "memory", {"text": text})
    await db.commit()
    return text


async def append(db: AsyncSession, line: str) -> str:
    current = await load(db)
    line = line.strip().lstrip("#").strip()
    return await save(db, (current + "\n" if current else "") + f"- {line}")


async def project_files(workspace: Workspace) -> list[tuple[str, str]]:
    """Instruction files at the folder's root, read once per turn."""
    if not workspace.supports_tools:
        return []
    found = []
    for name in PROJECT_FILES:
        try:
            text = await workspace.read_file(name)
        except WorkspaceError:
            continue
        if text and text.strip():
            found.append((name, text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n…(truncated)"))
    return found
