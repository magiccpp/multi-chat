"""Fake agent CLIs used by tests. Written to a bin dir as `claude`, `cursor-agent` and `copilot`."""

CLAUDE = r'''#!PYTHON
import json, os, subprocess, sys
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps({"cli": "claude", "args": args}) + "\n")
if "--version" in args:
    print("9.9.9 (Claude Code)"); sys.exit(0)
prompt = sys.stdin.read()
resumed = "--resume" in args
def out(o): print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init", "session_id": "sess-1", "model": "fake"})
mode = args[args.index("--permission-mode") + 1] if "--permission-mode" in args else "default"
decision = ""
if "--mcp-config" in args:
    cfg = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["multichat"]
    p = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                         env={**os.environ, **cfg["env"]})
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "approve",
        "arguments": {"tool_name": "Bash", "input": {"command": "make test"}, "tool_use_id": "toolu_1"}}}) + "\n")
    p.stdin.flush()
    p.stdout.readline()
    decision = json.loads(json.loads(p.stdout.readline())["result"]["content"][0]["text"])["behavior"]
    p.kill()
out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "make test"}}]}})
if mode in ("acceptEdits", "default") and decision != "deny":
    open("claude_edit.txt", "w").write("edited by claude\n")
for word in ["Claude ", "saw: ", prompt.strip().splitlines()[-1][:40], f" mode={mode}", f" decision={decision}" if decision else ""]:
    out({"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": word}}})
out({"type": "result", "subtype": "success", "is_error": False, "session_id": "sess-1", "result": "ignored",
     "usage": {"input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 20}, "total_cost_usd": 0.08 if resumed else 0.05})
'''

CURSOR = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps({"cli": "cursor", "args": args, "key": os.environ.get("CURSOR_API_KEY")}) + "\n")
if "--version" in args:
    print("2025.10.1"); sys.exit(0)
if "--list-models" in args and os.environ.get("FAKE_CURSOR_LIST") == "garbage":
    print("Error: Not logged in. Run 'agent login'."); sys.exit(1)
if "--list-models" in args:
    print("\x1b[1mAvailable models\x1b[0m\n")
    for line in ["auto - Auto (current)", "gpt-5 - GPT-5", "gpt-5-high - GPT-5 High", "gpt-5-low - GPT-5 Low",
                 "claude-opus-4-8 - Opus 4.8", "claude-opus-4-8-thinking-high - Opus 4.8 Thinking",
                 "claude-opus-4-8-1m - Opus 4.8 1M", "claude-opus-4-8-1m-thinking-high - Opus 4.8 1M Thinking"]:
        print(line)
    print("\nTip: use --model <id> to switch")
    sys.exit(0)
def out(o): print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init", "session_id": "cur-7"})
if "--force" in args:
    open("cursor_edit.txt", "w").write("edited by cursor\n")
out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Cursor plan: " + args[-1].splitlines()[-1][:30]}]}, "session_id": "cur-7"})
out({"type": "result", "subtype": "success", "is_error": False, "result": "Cursor plan", "session_id": "cur-7"})
'''

COPILOT = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps({"cli": "copilot", "args": args}) + "\n")
if "--version" in args:
    print("0.0.400"); sys.exit(0)
prompt = args[args.index("-p") + 1]
print("Copilot answer line 1")
print("about: " + prompt.strip().splitlines()[-1][:30])
'''


def install(bin_dir, python: str) -> None:
    for name, body in (("claude", CLAUDE), ("cursor-agent", CURSOR), ("copilot", COPILOT)):
        path = bin_dir / name
        path.write_text(body.replace("#!PYTHON", f"#!{python}"))
        path.chmod(0o755)
