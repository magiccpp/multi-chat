"""GitHub Copilot CLI in programmatic mode: `copilot -p ... -s` (plain-text output).

Plain text is the documented, stable output, so every turn sends the full
transcript instead of resuming a CLI session; token usage is estimated.
"""

from __future__ import annotations

from app.providers.agent_cli.base import CliDriver, CliInvocation, CliRun, ParsedLine, model_flag


class CopilotDriver(CliDriver):
    name = "copilot"
    supports_resume = False

    def build(self, run: CliRun) -> CliInvocation:
        args = ["-p", run.prompt, "-s", *model_flag(run.model_name)]
        env = {"GH_TOKEN": run.api_key} if run.api_key else {}
        inv = CliInvocation(args=args, env=env)
        if run.phase == "plan":
            args += ["--deny-tool", "write", "--deny-tool", "shell"]
        else:
            args.append("--allow-all-tools")
            if run.approve_edits == "ask" or run.approve_commands == "ask":
                inv.notices.append("Copilot cannot ask before each step; it auto-applies changes. Use Revert to undo a turn.")
        return inv

    def parse(self, line: str) -> ParsedLine:
        return ParsedLine(text=line + "\n")
