"""Minimal stdio MCP server that Claude Code calls for permission prompts.

Claude Code is started with `--permission-prompt-tool mcp__multichat__approve`; each
permission request is forwarded to the MultiChat server, which shows the usual
Approve / Reject prompt in the desktop app and TUI, and the decision is returned.
"""

from __future__ import annotations

import json
import os
import sys

import httpx

TOOL = {
    "name": "approve",
    "description": "Ask the MultiChat user to approve or deny a tool call.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}


def decide(arguments: dict) -> dict:
    url = os.environ["MULTICHAT_URL"].rstrip("/")
    session_id = os.environ["MULTICHAT_SESSION_ID"]
    try:
        resp = httpx.post(
            f"{url}/api/sessions/{session_id}/agent-approval",
            headers={"Authorization": f"Bearer {os.environ['MULTICHAT_TOKEN']}"},
            json={
                "model_id": int(os.environ["MULTICHAT_MODEL_ID"]),
                "tool_name": arguments.get("tool_name", ""),
                "input": arguments.get("input") or {},
                "tool_use_id": arguments.get("tool_use_id"),
            },
            timeout=None,
        )
        resp.raise_for_status()
        result = resp.json()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return {"behavior": "deny", "message": f"MultiChat approval failed: {exc}"}
    if result.get("approve"):
        return {"behavior": "allow", "updatedInput": arguments.get("input") or {}}
    note = result.get("note")
    return {"behavior": "deny", "message": "The user rejected this action." + (f" Note: {note}" if note else "")}


def handle(message: dict) -> dict | None:
    method = message.get("method")
    mid = message.get("id")
    if mid is None:  # notification
        return None
    if method == "initialize":
        version = (message.get("params") or {}).get("protocolVersion", "2025-06-18")
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "multichat", "version": "0.1.0"},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != "approve":
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        decision = decide(params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": json.dumps(decision)}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
