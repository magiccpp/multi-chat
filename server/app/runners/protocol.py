"""Wire protocol between the server and runners (mirrored in clients/core/multichat_client/runner/protocol.py).

runner -> server   {"type": "hello", "name", "os", "shell", "hostname", "roots": [...], "clis": {...}, "version"}
server -> runner   {"type": "welcome", "runner_id"}
server -> runner   {"type": "request", "id", "method", "params"}
runner -> server   {"type": "stream", "id", "data": {...}}          (zero or more, e.g. command output)
runner -> server   {"type": "response", "id", "result": {...}}  or  {"type": "response", "id", "error": "..."}
server -> runner   {"type": "cancel", "id"}
either             {"type": "ping"} / {"type": "pong"}
"""

PROTOCOL_VERSION = 1

METHODS = {
    "fs.browse",  # {path} -> {path, entries}; "" lists the allowed roots
    "fs.list",  # {root, path} -> {entries}
    "fs.read",  # {root, path} -> {exists, content}
    "fs.write",  # {root, path, content}
    "fs.delete",  # {root, path}
    "fs.grep",  # {root, pattern, path, glob, max_results} -> {output}
    "shell.run",  # {root, command, timeout} -> stream {text}; result {exit_code, timed_out}
    "snapshot.begin",  # {root} -> {ref}
    "snapshot.changes",  # {root, ref} -> {changes: [{path, before, after}]}
    "agent_cli.run",  # {root, driver, args, env, stdin} -> stream {line}; result {exit_code}
}
