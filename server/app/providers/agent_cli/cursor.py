"""Cursor headless: `cursor-agent -p --output-format stream-json`.

The parser is deliberately tolerant: it accepts assistant text either as message
content blocks or as plain `text` fields, and reads ids/usage from common keys.
"""

from __future__ import annotations

from app.providers.agent_cli.base import CliDriver, CliInvocation, CliRun, ParsedLine, loads, model_flag


def _text_of(message: object) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type", "text") == "text")
        return str(message.get("text", ""))
    return ""


class CursorDriver(CliDriver):
    name = "cursor"

    def build(self, run: CliRun) -> CliInvocation:
        args = ["-p", "--output-format", "stream-json", *model_flag(run.model_name)]
        env = {"CURSOR_API_KEY": run.api_key} if run.api_key else {}
        inv = CliInvocation(args=args, env=env)
        if run.resume_id:
            args.append(f"--resume={run.resume_id}")
        if run.phase == "build":
            args.append("--force")
            if run.approve_edits == "ask" or run.approve_commands == "ask":
                inv.notices.append("Cursor cannot ask before each step; it auto-applies changes. Use Revert to undo a turn.")
        args.append(run.prompt)
        return inv

    def parse(self, line: str) -> ParsedLine:
        ev = loads(line)
        if ev is None:
            return ParsedLine(text=line + "\n") if line.strip() else ParsedLine()
        etype = ev.get("type")
        sid = ev.get("session_id") or ev.get("chat_id") or ev.get("sessionId")
        if etype == "system":
            return ParsedLine(session_id=sid)
        if etype == "assistant":
            return ParsedLine(text=_text_of(ev.get("message") or ev.get("text")), session_id=sid)
        if etype == "tool_call" and ev.get("subtype", "started") == "started":
            call = ev.get("tool_call") or {}
            name = next(iter(call), "tool") if isinstance(call, dict) and call else ev.get("name", "tool")
            args = call.get(name, {}).get("args", {}) if isinstance(call.get(name), dict) else {}
            return ParsedLine(tool_call={"id": str(ev.get("call_id", "")), "name": str(name), "arguments": args})
        if etype == "result":
            usage = ev.get("usage") or {}
            return ParsedLine(
                final_text=ev.get("result") if isinstance(ev.get("result"), str) else None,
                session_id=sid,
                usage={
                    "input_tokens": usage.get("input_tokens") or usage.get("inputTokens") or 0,
                    "output_tokens": usage.get("output_tokens") or usage.get("outputTokens") or 0,
                    "cost": None,
                }
                if usage
                else None,
                error=(ev.get("result") or "failed") if ev.get("is_error") else None,
            )
        return ParsedLine(session_id=sid)
