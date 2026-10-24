"""Tool definitions offered to models and their execution against a Workspace."""

from __future__ import annotations

import difflib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app import crypto
from app.agent.approvals import approvals
from app.providers.base import ToolCall, ToolSpec
from app.web.fetch import FetchError, web_fetch
from app.web.search import SearchError, web_search
from app.workspaces.base import Workspace, WorkspaceError

LIST_DIR = ToolSpec(
    "list_dir",
    "List files and folders at a path relative to the workspace root.",
    {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path, default '.'"}}},
)
READ_FILE = ToolSpec(
    "read_file",
    "Read a text file. Returns numbered lines. Use offset/limit for large files.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "First line (1-based)", "minimum": 1},
            "limit": {"type": "integer", "description": "Max lines, default 2000", "minimum": 1},
        },
        "required": ["path"],
    },
)
GREP = ToolSpec(
    "grep",
    "Search file contents recursively with a regular expression. Returns path:line:text matches.",
    {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Folder or file to search, default '.'"},
            "glob": {"type": "string", "description": "Only files matching this glob, e.g. *.py"},
        },
        "required": ["pattern"],
    },
)
WRITE_FILE = ToolSpec(
    "write_file",
    "Create or overwrite a file with the given content.",
    {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
)
EDIT_FILE = ToolSpec(
    "edit_file",
    "Replace an exact string in a file. old_string must match exactly once unless replace_all is true. "
    "Read the file first.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["path", "old_string", "new_string"],
    },
)
RUN_COMMAND = ToolSpec(
    "run_command",
    "Run a shell command in the workspace root (non-interactive; stdin is closed). Returns output and exit code. "
    "Use it to run tests, builds, git, package managers, downloads and installs.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "Seconds, default 600", "minimum": 1},
        },
        "required": ["command"],
    },
)
WEB_FETCH = ToolSpec(
    "web_fetch",
    "Download a web page or file over http(s) and return it as readable text/markdown.",
    {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
)
WEB_SEARCH = ToolSpec(
    "web_search",
    "Search the web. Returns titles, URLs and snippets.",
    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
)

READ_ONLY = [LIST_DIR, READ_FILE, GREP]
WRITE = [WRITE_FILE, EDIT_FILE, RUN_COMMAND]


def tools_for(phase: str, workspace: Workspace, web_search_enabled: bool) -> list[ToolSpec]:
    tools: list[ToolSpec] = []
    if workspace.supports_tools:
        tools += READ_ONLY
        if phase == "build":
            tools += WRITE
    tools.append(WEB_FETCH)
    if web_search_enabled:
        tools.append(WEB_SEARCH)
    return tools


def unified_diff(path: str, before: str | None, after: str | None) -> str:
    return "".join(
        difflib.unified_diff(
            (before or "").splitlines(keepends=True),
            (after or "").splitlines(keepends=True),
            fromfile=f"a/{path}" if before is not None else "/dev/null",
            tofile=f"b/{path}" if after is not None else "/dev/null",
        )
    )


@dataclass
class ToolContext:
    session_id: int
    turn_id: str
    model_id: int
    workspace: Workspace
    approve_edits: str
    approve_commands: str
    command_timeout: int
    output_limit: int
    web: dict[str, Any]
    web_search: dict[str, Any]
    emit: Callable[[dict[str, Any]], None]
    record_change: Callable[[str, str | None, str | None], Awaitable[None]]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n… ({len(text) - limit} characters omitted) …\n{tail}"


async def _approve(ctx: ToolContext, call: ToolCall, payload: dict[str, Any]) -> str | None:
    decision = await approvals.request(ctx.session_id, call.id, {"model_id": ctx.model_id, "tool": call.name, **payload})
    if decision.approve:
        return None
    note = f" User note: {decision.note}" if decision.note else ""
    return f"The user rejected this {payload['kind']}.{note} Do not retry it unchanged; adjust or ask."


async def _apply_write(ctx: ToolContext, call: ToolCall, path: str, before: str | None, after: str) -> str:
    diff = unified_diff(path, before, after)
    if ctx.approve_edits == "ask":
        rejected = await _approve(ctx, call, {"kind": "edit", "path": path, "diff": diff})
        if rejected:
            return rejected
    await ctx.workspace.write_file(path, after)
    await ctx.record_change(path, before, after)
    ctx.emit({"type": "file_changed", "path": path, "diff": diff, "model_id": ctx.model_id, "turn_id": ctx.turn_id})
    added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return f"{'Created' if before is None else 'Updated'} {path} (+{added} -{removed})."


async def execute(call: ToolCall, ctx: ToolContext) -> str:
    args = call.arguments
    try:
        if "_raw" in args:
            return f"Error: arguments were not valid JSON: {args['_raw'][:200]}"
        name = call.name
        if name == "list_dir":
            entries = await ctx.workspace.list_dir(args.get("path") or ".")
            if not entries:
                return "(empty)"
            return "\n".join(f"{e.name}/" if e.is_dir else f"{e.name}  ({e.size} bytes)" for e in entries)
        if name == "read_file":
            text = await ctx.workspace.read_file(args["path"])
            if text is None:
                return f"Error: {args['path']} does not exist."
            lines = text.splitlines()
            offset = max(1, int(args.get("offset") or 1))
            limit = int(args.get("limit") or 2000)
            chunk = lines[offset - 1 : offset - 1 + limit]
            body = "\n".join(f"{i:6}\t{line}" for i, line in enumerate(chunk, offset))
            more = len(lines) - (offset - 1 + len(chunk))
            if more > 0:
                body += f"\n… {more} more lines (use offset)"
            return _truncate(body or "(empty file)", ctx.output_limit)
        if name == "grep":
            out = await ctx.workspace.grep(args["pattern"], args.get("path") or ".", args.get("glob"), 200)
            return _truncate(out.strip() or "No matches.", ctx.output_limit)
        if name == "write_file":
            before = await ctx.workspace.read_file(args["path"])
            return await _apply_write(ctx, call, args["path"], before, args["content"])
        if name == "edit_file":
            path = args["path"]
            before = await ctx.workspace.read_file(path)
            if before is None:
                return f"Error: {path} does not exist. Use write_file to create it."
            old, new = args["old_string"], args["new_string"]
            count = before.count(old)
            if count == 0:
                return "Error: old_string was not found. Read the file and copy the text exactly."
            if count > 1 and not args.get("replace_all"):
                return f"Error: old_string matches {count} times. Add context or set replace_all."
            after = before.replace(old, new) if args.get("replace_all") else before.replace(old, new, 1)
            return await _apply_write(ctx, call, path, before, after)
        if name == "run_command":
            command = args["command"]
            timeout = min(int(args.get("timeout") or ctx.command_timeout), ctx.command_timeout * 6)
            if ctx.approve_commands == "ask":
                rejected = await _approve(ctx, call, {"kind": "command", "command": command})
                if rejected:
                    return rejected

            async def on_output(text: str) -> None:
                ctx.emit({"type": "tool_progress", "call_id": call.id, "model_id": ctx.model_id, "text": text})

            result = await ctx.workspace.run_command(command, timeout, on_output)
            status = "timed out" if result.timed_out else f"exit code {result.exit_code}"
            return _truncate(f"[{status}]\n{result.output}", ctx.output_limit)
        if name == "web_fetch":
            return await web_fetch(args["url"], allow_private=bool(ctx.web.get("allow_private")), max_chars=ctx.output_limit)
        if name == "web_search":
            key = crypto.decrypt(ctx.web_search.get("api_key_enc"))
            return await web_search(ctx.web_search, key, args["query"])
        return f"Error: unknown tool {name!r}."
    except KeyError as exc:
        return f"Error: missing argument {exc}."
    except (WorkspaceError, FetchError, SearchError) as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001 - tool failures go back to the model, not up the stack
        return f"Error: {type(exc).__name__}: {exc}"
