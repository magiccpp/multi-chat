"""Claude Code headless: `claude -p --output-format stream-json`."""

from __future__ import annotations

from app.providers.agent_cli.base import CliDriver, CliInvocation, CliRun, ParsedLine, loads, model_flag


class ClaudeCodeDriver(CliDriver):
    name = "claude_code"
    supports_bridge = True

    def build(self, run: CliRun) -> CliInvocation:
        args = ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", *model_flag(run.model_name)]
        env: dict[str, str] = {}
        inv = CliInvocation(args=args, env=env, stdin=run.prompt)
        if run.api_key:
            # Bare mode reads only ANTHROPIC_API_KEY and skips local hooks/plugins/CLAUDE.md discovery.
            args.insert(0, "--bare")
            env["ANTHROPIC_API_KEY"] = run.api_key
        if run.resume_id:
            args += ["--resume", run.resume_id]
        if run.phase == "plan":
            args += ["--permission-mode", "plan"]
            return inv
        wants_ask = run.approve_edits == "ask" or run.approve_commands == "ask"
        if wants_ask and run.bridge_available:
            args += ["--permission-mode", "default" if run.approve_edits == "ask" else "acceptEdits"]
            inv.use_bridge = True
        elif run.approve_commands == "auto":
            args += ["--permission-mode", "acceptEdits", "--allowedTools", "Bash"]
            if run.approve_edits == "ask":
                inv.notices.append("Claude Code: per-edit approval needs a runner; edits are auto-accepted here.")
        else:
            # No bridge (e.g. SSH) and commands need approval: edits allowed, shell commands denied.
            args += ["--permission-mode", "acceptEdits"]
            inv.notices.append("Claude Code: command approval needs a runner; shell commands will be denied.")
        return inv

    def parse(self, line: str) -> ParsedLine:
        ev = loads(line)
        if ev is None:
            return ParsedLine()
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init":
            return ParsedLine(session_id=ev.get("session_id"))
        if etype == "stream_event":
            event = ev.get("event") or {}
            delta = event.get("delta") or {}
            if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta" and ev.get("parent_tool_use_id") is None:
                return ParsedLine(text=delta.get("text", ""))
            return ParsedLine()
        if etype == "assistant" and ev.get("parent_tool_use_id") is None:
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    return ParsedLine(
                        tool_call={"id": block.get("id", ""), "name": block.get("name", ""), "arguments": block.get("input") or {}}
                    )
            return ParsedLine()
        if etype == "result":
            usage = ev.get("usage") or {}
            tokens_in = (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
            )
            return ParsedLine(
                final_text=ev.get("result") if isinstance(ev.get("result"), str) else None,
                session_id=ev.get("session_id"),
                usage={"input_tokens": tokens_in, "output_tokens": usage.get("output_tokens") or 0, "cost": ev.get("total_cost_usd")},
                error=(ev.get("result") or "failed") if ev.get("is_error") else None,
            )
        return ParsedLine()
