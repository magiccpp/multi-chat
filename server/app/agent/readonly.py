"""Decide whether a shell command only reads. PLAN lets models run these to inspect code, configuration and remote
machines; anything else waits for BUILD.

Deliberately conservative: a command passes only when every part of it (each piece of a pipeline or command list,
every `$(...)` / script block, the command run by `ssh host ...`, `docker exec`, `cmd /c`, ...) is a known read-only
command used in a read-only way. Unknown commands are refused, with the reason, so the model can pick another way
or describe the change for the builder. Works for PowerShell, cmd and POSIX shells.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# ---------------------------------------------------------------- simple read-only commands (any arguments)

ALWAYS = {
    # POSIX
    "ls", "ll", "dir", "vdir", "cat", "tac", "head", "tail", "less", "more", "wc", "grep", "egrep", "fgrep", "rg", "ag",
    "ack", "tree", "pwd", "echo", "printf", "whoami", "id", "groups", "hostname", "uname", "date", "uptime", "df", "du",
    "free", "ps", "pgrep", "pstree", "env", "printenv", "which", "whereis", "type", "file", "stat", "realpath",
    "readlink", "basename", "dirname", "uniq", "cut", "tr", "nl", "column", "diff", "cmp", "comm", "md5sum", "sha1sum",
    "sha256sum", "sha512sum", "shasum", "b2sum", "cksum", "jq", "yq", "xxd", "hexdump", "od", "strings", "lsof",
    "netstat", "ss", "ping", "traceroute", "tracepath", "mtr", "nslookup", "dig", "host", "whois", "lsblk", "blkid",
    "findmnt", "lscpu", "lsusb", "lspci", "lsmod", "getent", "last", "lastlog", "w", "who", "users", "locale",
    "getenforce", "sestatus", "nproc", "arch", "true", "false", "test", "[", "cd", "pushd", "popd", "export",
    "ulimit", "umask", "fold", "fmt", "expand", "unexpand", "paste", "join", "look", "seq", "yes", "sleep", "tput",
    "cal", "zcat", "zgrep", "zless", "bzcat", "xzcat", "lz4cat", "zstdcat", "base64", "iconv", "nm", "objdump",
    "readelf", "ldd", "size", "lsattr", "getfacl", "namei", "pidof", "vmstat", "iostat", "mpstat", "sar", "dmidecode",
    "inxi", "neofetch", "fastfetch", "hostid", "sensors", "lshw", "hwinfo", "glxinfo", "vainfo", "vdpauinfo", "clinfo",
    "upower", "acpi", "lsipc", "lsns", "lslocks", "lsirq", "numactl_none", "cpupower_none", "tty", "stty", "awk", "gawk", "mawk", "nawk", "watch_none",
    # Windows cmd
    "findstr", "fc", "comp", "where", "ver", "vol", "systeminfo", "tasklist", "driverquery", "getmac", "quser", "qwinsta",
    "pathping", "tracert", "chdir", "cls", "nbtstat_none", "query", "whoami.exe",
    # PowerShell aliases of read-only cmdlets
    "gci", "gc", "gi", "gl", "gps", "gsv", "gcm", "gm", "gp", "gpv", "ghy", "h", "history", "sls", "select", "where",
    "?", "%", "foreach", "sort", "measure", "fl", "ft", "fw", "fc_none", "group", "compare", "diff", "write", "oh",
    "gal", "gdr", "gmo", "gwmi", "gcim", "gjb", "gu", "gv", "rvpa", "cvpa", "dbp_none", "sl", "pwd", "popd", "pushd",
}

# PowerShell verbs whose cmdlets only read or format (Get-ChildItem, Test-Path, Select-String, ConvertTo-Json, ...).
SAFE_VERBS = {"get", "test", "select", "measure", "format", "sort", "group", "where", "compare", "find", "resolve",
              "convertto", "convertfrom", "split", "join", "foreach", "write", "out", "show", "search", "read", "trace",
              "debug", "confirm", "wait", "use", "push", "pop", "set-location"}
# exceptions inside the safe verbs
UNSAFE_CMDLETS = {"format-volume", "format-disk", "out-file", "out-printer", "write-eventlog", "use-transaction",
                  "show-command", "get-credential_none"}
SAFE_CMDLETS = {"set-location", "push-location", "pop-location", "import-csv", "import-clixml", "import-powershelldatafile",
                "tee-object_none", "start-sleep", "new-timespan", "new-object_none", "add-type_none"}

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "powershell", "pwsh", "cmd"}
WRAPPERS = {"time", "nice", "ionice", "nohup_none", "timeout", "command", "builtin", "exec_none", "stdbuf", "env"}
CONTROL = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "foreach", "in", "case", "esac",
           "switch", "try", "catch", "finally", "elseif", "{", "}", "!", "begin", "process", "end"}
VERSION_ARGS = {"--version", "-version", "version", "-v", "--help", "-help", "-h", "help", "/?", "-?", "--info", "-vv"}

# words that change things when used as a subcommand of a "check if read-only" tool
MUTATING_WORDS = {
    "add", "delete", "del", "remove", "rm", "set", "unset", "create", "update", "modify", "edit", "install",
    "uninstall", "reinstall", "up", "down", "start", "stop", "restart", "reload", "enable", "disable", "kill", "flush",
    "reset", "purge", "clean", "prune", "push", "apply", "import", "write", "mv", "move", "rename", "mount", "umount",
    "unmount", "connect", "disconnect", "on", "off", "attach", "detach", "load", "unload", "revoke", "grant", "deploy",
    "destroy", "upgrade", "downgrade", "commit", "merge", "rebase", "checkout", "switch", "restore", "init", "new",
    "run", "exec", "execute", "eval", "replace", "patch", "scale", "rollout", "drain", "cordon", "uncordon", "taint",
    "label", "annotate", "expose", "autoscale", "certificate", "cp", "copy", "sync", "pull", "fetch", "clone", "login",
    "logout", "release", "renew", "register", "unregister", "lock", "unlock", "suspend", "resume", "pin", "unpin",
    "configure", "--set", "--add", "--delete", "--remove", "-w", "--write", "change", "activate", "deactivate",
    "rotate", "vacuum", "erase", "format", "wipe", "truncate", "send", "post", "put", "fix",
}


class Verdict:
    def __init__(self, ok: bool, reason: str = "") -> None:
        self.ok = ok
        self.reason = reason

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return f"Verdict({self.ok}, {self.reason!r})"


OK = Verdict(True)


def no(reason: str) -> Verdict:
    return Verdict(False, reason)


# ---------------------------------------------------------------- lexing

def _scan_groups(text: str) -> tuple[str, list[str]]:
    """Replace nested code ($(...), `...`, (...), {...}, <(...)) with a placeholder; return the flat text and the
    nested pieces (which are checked as commands themselves)."""
    out: list[str] = []
    nested: list[str] = []
    i, n = 0, len(text)
    quote = ""
    while i < n:
        c = text[i]
        if quote == "'":
            out.append(c)
            if c == "'":
                quote = ""
            i += 1
            continue
        if c == "'" and not quote:
            quote = "'"
            out.append(c)
            i += 1
            continue
        if c == '"':
            quote = "" if quote == '"' else '"'
            out.append(c)
            i += 1
            continue
        opener = None
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            opener, start = "(", i + 2
        elif c == "@" and i + 1 < n and text[i + 1] in "({":
            opener, start = text[i + 1], i + 2
        elif c == "<" and i + 1 < n and text[i + 1] == "(":
            opener, start = "(", i + 2
        elif c == "$" and i + 1 < n and text[i + 1] == "{":
            # ${var} is a variable, not code
            end = text.find("}", i)
            out.append(text[i : end + 1] if end > 0 else text[i:])
            i = end + 1 if end > 0 else n
            continue
        elif not quote and c in "({":
            opener, start = c, i + 1
        elif c == "`" and quote != '"' and i + 1 < n and text[i + 1] not in " \n\t" and "`" in text[i + 1 :]:
            # POSIX command substitution; a PowerShell escape is a backtick before one character
            end = text.find("`", i + 1)
            if end - i > 2:
                nested.append(text[i + 1 : end])
                out.append(" __SUB__ ")
                i = end + 1
                continue
        if opener:
            closer = ")" if opener == "(" else "}"
            depth, j, q = 1, start, ""
            while j < n and depth:
                d = text[j]
                if q:
                    if d == q:
                        q = ""
                elif d in "'\"":
                    q = d
                elif d == opener:
                    depth += 1
                elif d == closer:
                    depth -= 1
                j += 1
            nested.append(text[start : j - 1] if depth == 0 else text[start:])
            out.append(" __SUB__ ")
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out), nested


def _split_commands(text: str) -> list[str]:
    """Split on | || && ; & and newlines outside quotes."""
    parts, cur, quote, i = [], [], "", 0
    while i < len(text):
        c = text[i]
        if quote:
            cur.append(c)
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c in "|;&\n":
            parts.append("".join(cur))
            cur = []
            if i + 1 < len(text) and text[i + 1] == c and c in "|&":
                i += 1
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _tokens(segment: str) -> list[str]:
    toks, cur, quote, quoted = [], [], "", False
    for c in segment:
        if quote:
            if c == quote:
                quote = ""
            else:
                cur.append(c)
        elif c in "'\"":
            quote, quoted = c, True
        elif c.isspace():
            if cur or quoted:
                toks.append("".join(cur))
            cur, quoted = [], False
        else:
            cur.append(c)
    if cur or quoted:
        toks.append("".join(cur))
    return toks


ALLOWED_REDIRECTS = re.compile(
    r"(\d?>&\d|[*\d]?>\s*(\$null|/dev/null|nul)\b|<\s*/dev/null)", re.IGNORECASE
)
MUTATING_METHOD = re.compile(
    r"(::|\.)\s*(write|append|delete|move|copy|create|set|start|kill|exit|encrypt|decrypt|save|remove|add|clear|"
    r"invoke|load|replace|refresh|close|stop|terminate|shutdown|reboot|install|uninstall|put|post|upload|download|"
    r"execute|run|open(?!read|text))\w*\s*\(",
    re.IGNORECASE,
)


def _name(token: str) -> str:
    base = re.split(r"[\\/]", token)[-1].lower()
    for ext in (".exe", ".com", ".cmd", ".bat"):
        if base.endswith(ext) and base != ext:
            return base[: -len(ext)]
    return base


def _positionals(args: list[str], takes_value: set[str] = frozenset()) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-") or (a.startswith("/") and len(a) > 1 and "/" not in a[1:]):
            if a in takes_value:
                skip = True
            continue
        out.append(a)
    return out


def _has(args: list[str], *flags: str) -> bool:
    lowered = [a.lower() for a in args]
    return any(a == f or a.startswith(f + "=") for f in flags for a in lowered)


def _no_mutating_words(args: list[str]) -> Verdict:
    for a in args:
        if a.lower() in MUTATING_WORDS:
            return no(f"'{a}' changes something")
    return OK


# ---------------------------------------------------------------- tools with read-only subcommands

GIT_READ = {
    "status", "log", "diff", "show", "ls-files", "ls-tree", "ls-remote", "cat-file", "rev-parse", "rev-list",
    "describe", "blame", "annotate", "grep", "shortlog", "whatchanged", "name-rev", "merge-base", "for-each-ref",
    "show-ref", "show-branch", "count-objects", "version", "help", "var", "check-ignore", "check-attr", "diff-tree",
    "diff-index", "diff-files", "range-diff", "cherry", "verify-commit", "verify-tag", "fsck", "branch", "tag",
    "remote", "stash", "config", "worktree", "submodule", "reflog", "notes", "lfs",
}


def _git(args: list[str]) -> Verdict:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in ("-c",) or a.startswith("--exec-path") or a.startswith("--config-env"):
            return no("git -c can run programs")
        if a in ("-C", "--git-dir", "--work-tree", "--namespace"):
            i += 1
        i += 1
    if i >= len(args):
        return OK
    sub, rest = args[i].lower(), args[i + 1 :]
    if sub not in GIT_READ:
        return no(f"git {sub} changes the repository (or isn't known to be read-only)")
    if _has(rest, "--output", "-o") and sub in ("log", "diff", "show", "format-patch"):
        return no("writes a file")
    if sub == "grep" and _has(rest, "-o", "--open-files-in-pager", "-O"):
        return no("opens a pager program")
    if sub == "fsck" and _has(rest, "--lost-found"):
        return no("writes lost-found")
    pos = _positionals(rest, {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort",
                              "--format", "-n"})
    if sub == "branch":
        if _has(rest, "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-u", "--set-upstream-to",
                "--unset-upstream", "--edit-description", "-f", "--force", "-t", "--track", "--no-track"):
            return no("changes branches")
        if pos and not _has(rest, "--list", "-l", "-a", "--all", "-r", "--remotes"):
            return no("git branch <name> creates a branch")
    if sub == "tag":
        if _has(rest, "-d", "--delete", "-a", "--annotate", "-s", "--sign", "-m", "--message", "-f", "--force", "-F", "-u"):
            return no("changes tags")
        if pos and not _has(rest, "-l", "--list"):
            return no("git tag <name> creates a tag")
    if sub == "remote" and pos and pos[0] not in ("show", "get-url"):
        return no(f"git remote {pos[0]} changes remotes")
    if sub == "stash" and (not pos or pos[0] not in ("list", "show")):
        return no("git stash changes the working tree")
    if sub == "config":
        if pos[:1] in (["get"], ["list"]) or _has(rest, "--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch"):
            return OK
        return no("git config without --get/--list changes configuration")
    if sub == "worktree" and pos[:1] != ["list"]:
        return no("changes worktrees")
    if sub == "submodule" and pos[:1] not in (["status"], ["summary"]):
        return no("changes submodules")
    if sub == "reflog" and pos[:1] not in ([], ["show"], ["exists"]):
        return no("changes the reflog")
    if sub == "notes" and pos[:1] not in ([], ["list"], ["show"]):
        return no("changes notes")
    if sub == "lfs" and pos[:1] not in (["ls-files"], ["status"], ["env"], ["version"], ["logs"]):
        return no("changes LFS state")
    return OK


def _sub_table(allowed: dict[str, set[str] | None], name: str, takes_value: set[str] = frozenset()) -> Callable[[list[str]], Verdict]:
    """`allowed` maps a subcommand to None (any arguments) or to the allowed second-level subcommands."""

    def check(args: list[str]) -> Verdict:
        pos = _positionals(args, takes_value)
        if not pos:
            if all(a.lower() in VERSION_ARGS for a in args):
                return OK
            return no(f"{name} without a read-only subcommand")
        sub = pos[0].lower()
        if sub not in allowed:
            return no(f"{name} {sub} isn't known to be read-only")
        second = allowed[sub]
        if second is not None and (len(pos) < 2 or pos[1].lower() not in second):
            return no(f"{name} {sub} {pos[1] if len(pos) > 1 else ''} isn't known to be read-only".replace("  ", " "))
        if _has(args, "-f", "--follow", "-w", "--watch") and sub in ("logs", "get", "events", "log"):
            return no("follows output forever; drop -f/--watch")
        return _no_mutating_words(pos[2 if second is not None else 1 :])

    return check


CONTAINER = {
    "ps": None, "images": None, "inspect": None, "logs": None, "version": None, "info": None, "top": None, "port": None,
    "history": None, "search": None, "diff": None, "stats": None,
    "container": {"ls", "list", "inspect", "logs", "top", "port", "diff", "stats"},
    "image": {"ls", "list", "inspect", "history"}, "network": {"ls", "list", "inspect"},
    "volume": {"ls", "list", "inspect"}, "context": {"ls", "list", "inspect", "show"}, "system": {"df", "info"},
    "compose": {"ps", "ls", "logs", "config", "images", "top", "version", "port"},
    "buildx": {"ls", "version", "du", "inspect"}, "node": {"ls", "inspect", "ps"}, "service": {"ls", "inspect", "ps", "logs"},
    "stack": {"ls", "ps", "services"}, "secret": {"ls", "inspect"}, "config": {"ls", "inspect"},
}
DOCKER_OPTS = {"-H", "--host", "--context", "-c", "--config", "-l", "--log-level", "-f", "--file", "-p", "--project-name"}


def _container_cli(name: str) -> Callable[[list[str]], Verdict]:
    table = _sub_table(CONTAINER, name, DOCKER_OPTS)

    def check(args: list[str]) -> Verdict:
        pos = _positionals(args, DOCKER_OPTS)
        if pos[:1] == ["exec"] or pos[:2] in (["container", "exec"], ["compose", "exec"]):
            return _exec_inner(args, {"-e", "--env", "-u", "--user", "-w", "--workdir", "--env-file", "--detach-keys",
                                      "--index"}, skip_first=2 if pos[0] in ("container", "compose") else 1)
        if pos[:1] == ["stats"] or pos[:2] == ["container", "stats"]:
            if not _has(args, "--no-stream"):
                return no("docker stats streams forever; add --no-stream")
        return table(args)

    return check


def _exec_inner(args: list[str], takes_value: set[str], skip_first: int = 1) -> Verdict:
    """docker exec [opts] container cmd... / kubectl exec pod [opts] -- cmd..."""
    rest, i, seen = list(args), 0, 0
    while i < len(rest) and seen < skip_first:
        if not rest[i].startswith("-"):
            seen += 1
        i += 1
    rest = rest[i:]
    if "--" in rest:
        inner = rest[rest.index("--") + 1 :]
    else:
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            j += 2 if rest[j] in takes_value else 1
        inner = rest[j + 1 :]
    if not inner:
        return no("exec without a command opens an interactive shell")
    return check_command(" ".join(_quote(t) for t in inner))


def _quote(t: str) -> str:
    return t if t and not re.search(r"[\s'\"|;&<>]", t) else "'" + t.replace("'", "'\\''") + "'"


def _kubectl(args: list[str]) -> Verdict:
    opts = {"-n", "--namespace", "--context", "--kubeconfig", "-c", "--container", "-l", "--selector", "-o", "--output",
            "--cluster", "--user", "-s", "--server"}
    pos = _positionals(args, opts)
    if pos[:1] == ["exec"]:
        return _exec_inner(args, opts, skip_first=2)
    table = {"get": None, "describe": None, "logs": None, "top": None, "explain": None, "version": None,
             "api-resources": None, "api-versions": None, "cluster-info": None, "diff": None, "events": None,
             "auth": {"can-i", "whoami"},
             "config": {"view", "get-contexts", "current-context", "get-clusters", "get-users"},
             "rollout": {"status", "history"}}
    return _sub_table(table, "kubectl", opts)(args)


def _ssh(args: list[str]) -> Verdict:
    takes = {"-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S",
             "-W", "-w", "-B", "-P"}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a[:2] in ("-L", "-R", "-D", "-W", "-w") or a in ("-N", "-f", "-M", "-O"):
            return no("ssh tunnels and background sessions aren't allowed in PLAN")
        if a[:2] == "-o" and re.search(r"(?i)(localcommand|proxycommand|permitlocalcommand|remotecommand)", " ".join(args[i : i + 2])):
            return no("ssh -o ...Command can run local programs")
        i += 2 if a in takes else 1
    if i >= len(args):
        return no("ssh needs a host")
    remote = args[i + 1 :]
    if not remote:
        return no("interactive ssh isn't possible (no keyboard); give the remote command, e.g. ssh host \"cat /etc/os-release\"")
    return check_command(" ".join(remote) if len(remote) > 1 else remote[0])


def _shell(name: str, args: list[str]) -> Verdict:
    if not args:
        return no(f"{name} without -c reads a script from its input (or opens an interactive shell)")
    low = [a.lower() for a in args]
    for flag in ("-c", "-command", "/c", "/k", "-c:", "--command"):
        if flag in low:
            inner = args[low.index(flag) + 1 :]
            if not inner:
                return no(f"{name} {flag} without a command")
            return check_command(" ".join(inner) if len(inner) > 1 else inner[0])
    if name in ("powershell", "pwsh"):
        if any(a in low for a in ("-file", "-f", "-encodedcommand", "-enc", "-e", "-ec")):
            return no("running a script file or encoded command can do anything")
        rest = [a for a in args if not a.startswith("-")]
        if rest:
            return check_command(" ".join(rest))
    if all(a in VERSION_ARGS for a in low):
        return OK
    return no(f"{name} would run a script")


def _curl(args: list[str]) -> Verdict:
    if _has(args, "-o", "--output", "-O", "--remote-name", "--remote-name-all", "-T", "--upload-file", "-d", "--data",
            "--data-raw", "--data-binary", "--data-urlencode", "-F", "--form", "--json", "-c", "--cookie-jar",
            "--config", "-K", "-D", "--dump-header", "--create-dirs"):
        return no("curl that sends data or writes files")
    for k, a in enumerate(args):
        if a in ("-X", "--request") and k + 1 < len(args) and args[k + 1].upper() not in ("GET", "HEAD", "OPTIONS"):
            return no(f"curl -X {args[k + 1]} changes data on the server")
        if a.startswith("-X") and len(a) > 2 and a[2:].upper() not in ("GET", "HEAD", "OPTIONS"):
            return no("curl with a non-GET method")
    return OK


def _web_cmdlet(args: list[str]) -> Verdict:
    if _has(args, "-outfile", "-body", "-infile", "-form", "-passthru_none"):
        return no("sends data or writes a file")
    for k, a in enumerate(args):
        if a.lower() == "-method" and k + 1 < len(args) and args[k + 1].upper() not in ("GET", "HEAD", "OPTIONS"):
            return no(f"-Method {args[k + 1]} changes data on the server")
    return OK


def _version_only(name: str) -> Callable[[list[str]], Verdict]:
    def check(args: list[str]) -> Verdict:
        if args and all(a.lower() in VERSION_ARGS for a in args):
            return OK
        return no(f"{name} can run code; only {name} --version is allowed in PLAN")

    return check


def _python(args: list[str]) -> Verdict:
    low = [a.lower() for a in args]
    if low[:2] == ["-m", "pip"] or low[:2] == ["-m", "pip3"]:
        return _sub_table(PIP, "pip")(args[2:])
    if low[:2] in (["-m", "site"], ["-m", "platform"]):
        return OK
    return _version_only("python")(args)


PIP = {"list": None, "show": None, "freeze": None, "check": None, "config": {"list", "get", "debug"},
       "index": {"versions"}, "debug": None, "inspect": None, "help": None, "search": None}


def _winget(args: list[str]) -> Verdict:
    opts = {"-s", "--source", "--id", "--name", "-q", "--query", "--moniker", "--tag", "--count", "-n", "--scope"}
    pos = _positionals(args, opts)
    if not pos:
        return OK if all(a.lower() in VERSION_ARGS or a.lower() == "--info" for a in args) else no("winget without a subcommand")
    sub = pos[0].lower()
    if sub in ("list", "ls", "search", "find", "show", "view", "hash", "features", "--info"):
        return OK
    if sub == "source" and pos[1:2] in (["list"], ["ls"], ["export"]):
        return OK
    if sub == "pin" and pos[1:2] in (["list"], ["ls"]):
        return OK
    if sub == "upgrade" and len(pos) == 1 and not _has(args, "--all", "-r", "--recurse", "--id", "--name", "-q", "--query",
                                                         "--moniker", "--manifest", "-m"):
        return OK  # just lists available upgrades
    return no(f"winget {sub} changes installed software")


def _flag_tool(name: str, allowed_first: tuple[str, ...], deny: tuple[str, ...] = ()) -> Callable[[list[str]], Verdict]:
    """Tools driven by flags (dpkg -l, rpm -qa, pacman -Q, iptables -L, ...)."""

    def check(args: list[str]) -> Verdict:
        flags = [a for a in args if a.startswith("-")]
        if not flags:
            return no(f"{name} without a read-only flag")
        if any(a.lower().startswith(d) for d in deny for a in flags):
            return no(f"{name} {flags[0]} changes something")
        if not any(f.startswith(allowed_first) for f in flags):
            return no(f"{name} {flags[0]} isn't known to be read-only")
        return OK

    return check


def _deny_words(name: str) -> Callable[[list[str]], Verdict]:
    return _no_mutating_words


def _windows_slash(name: str, allowed: tuple[str, ...], deny: tuple[str, ...] = ()) -> Callable[[list[str]], Verdict]:
    def check(args: list[str]) -> Verdict:
        low = [a.lower() for a in args]
        if any(a.startswith(d) for d in deny for a in low):
            return no(f"{name} with {next(a for a in low if any(a.startswith(d) for d in deny))} changes something")
        if allowed and not any(a.startswith(allowed) for a in low):
            return no(f"{name}: only {', '.join(allowed)} are allowed in PLAN")
        return OK

    return check


def _cloud_verbs(name: str, ok_last: set[str], ok_prefixes: tuple[str, ...]) -> Callable[[list[str]], Verdict]:
    def check(args: list[str]) -> Verdict:
        pos = [p.lower() for p in _positionals(args, {"--query", "-o", "--output", "--subscription", "-g", "--resource-group",
                                                      "--project", "--region", "--profile", "--zone", "-n", "--name",
                                                      "--format", "--filter"})]
        if not pos:
            return OK if all(a.lower() in VERSION_ARGS for a in args) else no(f"{name} without a command")
        if name == "aws":
            if pos[:2] in (["s3", "ls"], ["sts", "get-caller-identity"], ["configure", "list"], ["configure", "get"],
                           ["logs", "tail"]) and not _has(args, "--follow"):
                return OK
            if len(pos) >= 2 and pos[1].startswith(("describe-", "list-", "get-", "head-")):
                return OK
            return no(f"aws {' '.join(pos[:2])} isn't known to be read-only")
        if pos[-1] in ok_last or pos[-1].startswith(ok_prefixes) or pos == ["version"]:
            v = _no_mutating_words(pos)
            return v if not v else OK
        return no(f"{name} {' '.join(pos)} isn't known to be read-only")

    return check


def _gh(args: list[str]) -> Verdict:
    pos = [p.lower() for p in _positionals(args, {"-R", "--repo", "-L", "--limit", "-q", "--jq", "-t", "--template",
                                                   "-s", "--state", "--json", "-H", "--header"})]
    if pos[:1] == ["api"]:
        if _has(args, "-f", "-F", "--field", "--raw-field", "--input"):
            return no("gh api with fields sends data")
        for k, a in enumerate(args):
            if a in ("-X", "--method") and k + 1 < len(args) and args[k + 1].upper() != "GET":
                return no("gh api with a non-GET method")
        return OK
    table = {"pr": {"list", "view", "status", "diff", "checks"}, "issue": {"list", "view", "status"},
             "repo": {"view", "list"}, "run": {"list", "view"}, "workflow": {"list", "view"},
             "release": {"list", "view"}, "auth": {"status"}, "status": None, "search": None, "browse_none": None,
             "gist": {"list", "view"}, "label": {"list"}, "cache": {"list"}, "variable": {"list"}, "secret": {"list"}}
    return _sub_table(table, "gh")(pos)


SPECIAL: dict[str, Callable[[list[str]], Verdict]] = {
    "git": _git,
    "docker": _container_cli("docker"), "podman": _container_cli("podman"), "nerdctl": _container_cli("nerdctl"),
    "docker-compose": _sub_table({k: None for k in CONTAINER["compose"]}, "docker-compose", DOCKER_OPTS),
    "kubectl": _kubectl, "oc": _kubectl, "k": _kubectl,
    "helm": _sub_table({"list": None, "ls": None, "status": None, "get": None, "history": None, "show": None,
                        "inspect": None, "search": None, "version": None, "template": None, "lint": None, "env": None,
                        "repo": {"list", "ls"}, "dependency": {"list", "ls"}, "plugin": {"list", "ls"}}, "helm"),
    "systemctl": _sub_table({s: None for s in ("status", "show", "list-units", "list-unit-files", "list-timers",
                                               "list-sockets", "list-dependencies", "list-jobs", "is-active", "is-enabled",
                                               "is-failed", "is-system-running", "cat", "get-default", "help")}, "systemctl"),
    "journalctl": lambda a: no("follows output forever; drop -f") if _has(a, "-f", "--follow") else (
        no("journalctl maintenance flags change logs") if any(x.startswith(("--vacuum", "--rotate", "--flush", "--sync",
                                                                             "--relinquish", "--setup-keys", "--update-catalog"))
                                                             for x in a) else OK),
    "service": lambda a: OK if a == ["--status-all"] or (a and a[-1] == "status") else no("service <name> <action> changes services"),
    "npm": _sub_table({s: None for s in ("ls", "list", "ll", "la", "view", "v", "info", "show", "outdated", "root",
                                          "bin", "prefix", "why", "explain", "doctor", "search", "help", "query", "fund")}
                      | {"config": {"get", "list", "ls"}, "audit": None, "pkg": {"get"}}, "npm"),
    "pnpm": _sub_table({s: None for s in ("ls", "list", "ll", "view", "info", "outdated", "root", "bin", "why", "audit",
                                           "help", "licenses")} | {"config": {"get", "list"}}, "pnpm"),
    "yarn": _sub_table({s: None for s in ("list", "info", "outdated", "why", "audit", "licenses", "bin", "help",
                                           "versions")} | {"config": {"get", "list"}}, "yarn"),
    "pip": _sub_table(PIP, "pip"), "pip3": _sub_table(PIP, "pip"),
    "python": _python, "python3": _python, "py": _python,
    "node": _version_only("node"), "ruby": _version_only("ruby"), "perl": _version_only("perl"),
    "java": _version_only("java"), "javac": _version_only("javac"), "rustc": _version_only("rustc"),
    "php": _version_only("php"), "deno": _version_only("deno"), "bun": _version_only("bun"),
    "code": _version_only("code"), "gcc": _version_only("gcc"), "clang": _version_only("clang"),
    "dotnet": lambda a: OK if all(x.lower() in VERSION_ARGS | {"--list-sdks", "--list-runtimes", "--info"} for x in a) and a
    else _sub_table({"list": None, "sdk": {"check"}, "workload": {"list", "search"}, "nuget": {"locals"}}, "dotnet")(a),
    "cargo": _sub_table({s: None for s in ("tree", "metadata", "search", "pkgid", "verify-project", "locate-project",
                                            "version", "help")}, "cargo"),
    "go": lambda a: no("go env -w changes settings") if _has(a, "-w", "-u") else _sub_table(
        {"version": None, "env": None, "list": None, "doc": None, "help": None, "mod": {"graph", "why"}}, "go")(a),
    "winget": _winget,
    "choco": _sub_table({s: None for s in ("list", "search", "find", "info", "outdated", "help")}
                        | {"config": {"list", "get"}, "source": {"list"}, "feature": {"list"}, "pin": {"list"}}, "choco"),
    "scoop": _sub_table({s: None for s in ("list", "info", "search", "status", "which", "cat", "checkup", "prefix",
                                            "export", "help", "depends")} | {"bucket": {"list", "known"}}, "scoop"),
    "apt": _sub_table({s: None for s in ("list", "show", "search", "policy", "depends", "rdepends", "showsrc",
                                          "changelog")}, "apt"),
    "apt-cache": _sub_table({s: None for s in ("show", "showpkg", "showsrc", "search", "policy", "depends", "rdepends",
                                                "madison", "pkgnames", "stats", "unmet")}, "apt-cache"),
    "dpkg": _flag_tool("dpkg", ("-l", "--list", "-L", "--listfiles", "-s", "--status", "-S", "--search", "-p",
                                "--print-avail", "--get-selections", "--print-architecture", "--print-foreign",
                                "-C", "--audit", "-V", "--verify", "--compare-versions")),
    "dpkg-query": lambda a: OK,
    "rpm": _flag_tool("rpm", ("-q", "--query", "-V", "--verify"), ("-e", "-i", "-U", "-F", "--erase", "--install",
                                                                   "--upgrade", "--freshen", "--import", "--rebuilddb")),
    "yum": _sub_table({s: None for s in ("list", "info", "search", "provides", "whatprovides", "repolist", "repoinfo",
                                          "check-update", "deplist", "repoquery")} | {"history": {"list", "info"},
                                                                                      "module": {"list", "info"},
                                                                                      "group": {"list", "info"}}, "yum"),
    "brew": _sub_table({s: None for s in ("list", "ls", "info", "search", "outdated", "deps", "uses", "config",
                                           "doctor", "leaves", "desc", "cat", "log", "--prefix", "--cellar")}
                       | {"services": {"list", "info"}}, "brew"),
    "snap": _sub_table({s: None for s in ("list", "info", "find", "version", "connections", "services", "changes",
                                           "tasks", "warnings")}, "snap"),
    "flatpak": _sub_table({s: None for s in ("list", "info", "search", "remotes", "remote-ls", "history", "ps")}, "flatpak"),
    "pacman": _flag_tool("pacman", ("-Q", "-Ss", "-Si", "-Sl", "-Sg", "-F"), ("-S ", "-R", "-U", "-Sy", "-Su", "-Fy", "-D")),
    "apk": _sub_table({s: None for s in ("info", "search", "list", "policy", "dot", "stats", "version")}, "apk"),
    "zypper": _sub_table({s: None for s in ("search", "se", "info", "if", "packages", "pa", "patterns", "products", "repos",
                                             "lr", "list-updates", "lu", "patches")}, "zypper"),
    "terraform": lambda a: no("-out writes a plan file") if any(x.startswith("-out") for x in a) else _sub_table(
        {s: None for s in ("show", "plan", "output", "validate", "version", "providers", "graph")}
        | {"state": {"list", "show", "pull"}, "workspace": {"list", "show"},
           "fmt": None}, "terraform")(a) if not (a[:1] == ["fmt"] and not _has(a, "-check", "--check")) else no("terraform fmt rewrites files; add -check"),
    "aws": _cloud_verbs("aws", set(), ()),
    "az": _cloud_verbs("az", {"list", "show", "version"}, ("list-", "show-")),
    "gcloud": _cloud_verbs("gcloud", {"list", "describe", "info", "version", "get-value"}, ()),
    "gh": _gh,
    "ssh": _ssh,
    "curl": _curl,
    "invoke-webrequest": _web_cmdlet, "iwr": _web_cmdlet, "invoke-restmethod": _web_cmdlet, "irm": _web_cmdlet,
    "wget": lambda a: OK if _has(a, "--spider") or ("-O" in a and a[a.index("-O") + 1 : a.index("-O") + 2] == ["-"])
    or any(x in ("-qO-", "-O-") for x in a) else no("wget saves files; use curl or wget -qO-"),
    "find": lambda a: no(f"find {next(x for x in a if x in FIND_DENY)} changes or runs things") if any(x in FIND_DENY for x in a) else OK,
    "sed": lambda a: no("sed -i edits files") if any(x == "-i" or x.startswith(("-i", "--in-place")) for x in a if x.startswith("-")) else OK,
    "sort": lambda a: no("sort -o writes a file") if _has(a, "-o", "--output", "/o") else OK,
    "tar": lambda a: OK if (a and (a[0].lstrip("-").startswith("t") or "t" in a[0].lstrip("-") and not set("xcru") & set(a[0].lstrip("-")) or _has(a, "--list"))) else no("only tar -t (list) is allowed in PLAN"),
    "unzip": lambda a: OK if _has(a, "-l", "-t", "-v", "-Z") else no("only unzip -l/-t is allowed in PLAN"),
    "7z": lambda a: OK if a[:1] in (["l"], ["t"]) else no("only 7z l/t is allowed in PLAN"),
    "openssl": lambda a: OK if a[:1] and a[0] in ("x509", "s_client", "version", "verify", "crl", "asn1parse", "dgst",
                                                  "list", "ciphers", "req") and not _has(a, "-out", "-new", "-newkey", "-keyout") else no("this openssl command writes files"),
    "xargs": lambda a: _exec_after_flags(a, {"-I", "-i", "-n", "-P", "-d", "-E", "-L", "-s", "-a", "--arg-file", "--delimiter",
                                             "--max-args", "--max-procs", "--replace"}),
    "mount": lambda a: OK if not [x for x in a if not x.startswith("-")] else no("mount <device> changes the system"),
    "dmesg": lambda a: no("dmesg -c clears the log") if _has(a, "-c", "-C", "--clear", "-n", "-D", "-E", "--console-off", "--console-on") else OK,
    "sysctl": lambda a: no("sysctl -w / key=value changes kernel settings") if _has(a, "-w", "--write", "-p", "--load", "--system") or any("=" in x for x in a) else OK,
    "crontab": lambda a: OK if a == ["-l"] or (len(a) == 3 and a[0] == "-u" and a[2] == "-l") or (len(a) == 3 and a[2] == "-l") else no("only crontab -l is allowed in PLAN"),
    "iptables": _flag_tool("iptables", ("-L", "-S", "--list", "--list-rules"), ("-A", "-I", "-D", "-R", "-F", "-X", "-N", "-P", "-Z", "-E", "--append", "--insert", "--delete", "--flush")),
    "ip6tables": _flag_tool("ip6tables", ("-L", "-S", "--list"), ("-A", "-I", "-D", "-R", "-F", "-X", "-N", "-P", "-Z", "-E")),
    "nft": lambda a: OK if a[:1] == ["list"] else no("only nft list is allowed in PLAN"),
    "ufw": lambda a: OK if a[:1] in (["status"], ["show"], ["app"]) and "update" not in a else no("only ufw status/show is allowed"),
    "firewall-cmd": lambda a: OK if a and all(x.startswith(("--list", "--get", "--query", "--state", "--zone", "--info", "--permanent")) for x in a) else no("only firewall-cmd --list/--get/--query are allowed"),
    "ip": _deny_words("ip"), "ifconfig": lambda a: OK if len([x for x in a if not x.startswith("-")]) <= 1 else no("ifconfig with settings changes the network"),
    "nmcli": _deny_words("nmcli"), "timedatectl": _deny_words("timedatectl"), "hostnamectl": _deny_words("hostnamectl"),
    "localectl": _deny_words("localectl"), "loginctl": lambda a: OK if not a or a[0].startswith(("list-", "show-", "session-status", "user-status", "seat-status")) else no("loginctl action changes sessions"),
    "resolvectl": lambda a: OK if not a or a[0] in ("status", "query", "statistics", "dns", "domain", "service") and len(a) <= 2 else no("resolvectl with settings changes DNS"),
    "lvs": lambda a: OK, "pvs": lambda a: OK, "vgs": lambda a: OK,
    "zpool": lambda a: OK if a[:1] in (["list"], ["status"], ["get"], ["iostat"], ["history"]) else no("only zpool list/status/get"),
    "zfs": lambda a: OK if a[:1] in (["list"], ["get"]) else no("only zfs list/get"),
    "virsh": lambda a: OK if a and (a[0].startswith(("list", "dom", "net-list", "net-info", "pool-list", "vol-list", "nodeinfo", "version", "capabilities", "dumpxml")) and a[0] not in ("domrename",)) else no("virsh action changes VMs"),
    "wsl": lambda a: OK if a and a[0].lower() in ("-l", "--list", "--status", "--version", "-v", "--help") else no("only wsl --list/--status in PLAN"),
    "reg": lambda a: OK if a[:1] and a[0].lower() == "query" else no("only reg query is allowed in PLAN"),
    "sc": lambda a: no("in PowerShell 'sc' is Set-Content; use sc.exe query"),
    "sc.exe": lambda a: OK if a[:1] and a[0].lower() in ("query", "queryex", "qc", "qdescription", "qfailure", "qtriggerinfo", "qprivs", "qsidtype", "enumdepend", "getdisplayname", "getkeyname", "sdshow") else no("sc.exe action changes services"),
    "schtasks": lambda a: OK if _has(a, "/query") and not _has(a, "/create", "/delete", "/change", "/run", "/end") else no("only schtasks /query is allowed"),
    "netsh": lambda a: OK if {"show", "dump"} & {x.lower() for x in a} and _no_mutating_words(a) else no("only netsh ... show is allowed in PLAN"),
    "wmic": lambda a: OK if any(x.lower() in ("get", "list") for x in a) and not any(x.lower() in ("call", "delete", "set", "create") for x in a) else no("only wmic ... get/list is allowed"),
    "bcdedit": lambda a: OK if not a or [x.lower() for x in a] in (["/enum"], ["/enum", "all"], ["/v"]) else no("bcdedit with options changes boot settings"),
    "dism": lambda a: OK if a and all(x.lower() in ("/online", "/english") or x.lower().startswith(("/format:", "/get-", "/featurename:", "/packagename:", "/capabilityname:")) for x in a) else no("only dism /online /get-* is allowed"),
    "icacls": _windows_slash("icacls", (), ("/grant", "/deny", "/remove", "/reset", "/setowner", "/setintegritylevel", "/save", "/restore", "/inheritance", "/substitute")),
    "attrib": lambda a: no("attrib +x/-x changes attributes") if any(x.startswith(("+", "-")) for x in a) else OK,
    "ipconfig": _windows_slash("ipconfig", (), ("/release", "/renew", "/flushdns", "/registerdns", "/setclassid")),
    "arp": lambda a: OK if not a or a[0].lower() in ("-a", "-g", "-v") else no("arp -s/-d changes the ARP table"),
    "route": lambda a: OK if a[:1] and a[0].lower() in ("print", "-n") else (OK if not a else no("route add/delete changes routing")),
    "nbtstat": lambda a: no("nbtstat -R/-RR purges caches") if any(x in ("-R", "-RR") for x in a) else OK,
    "certutil": lambda a: OK if a[:1] and a[0].lower() in ("-hashfile", "-dump", "-store", "-verify", "-dumppfx_none") and not any(x.lower() in ("-delstore", "-addstore", "-importpfx", "-repairstore", "-setreg", "-urlcache", "-delkey") for x in a) else no("this certutil command changes or downloads things"),
    "net": lambda a: OK if a[:1] and a[0].lower() in ("view", "statistics", "share", "user", "localgroup", "session", "use", "accounts", "config", "time", "group")
    and not any(x.startswith("/") and x.lower() not in ("/domain",) for x in a[1:]) and (a[0].lower() not in ("share", "use", "session", "accounts") or len(a) == 1) and (a[0].lower() not in ("user", "localgroup", "group") or len(a) <= 2) else no("this net command changes users, shares or services"),
    "assoc": lambda a: no("assoc x=y changes file associations") if any("=" in x for x in a) else OK,
    "ftype": lambda a: no("ftype x=y changes file types") if any("=" in x for x in a) else OK,
    "path": lambda a: OK if not a else no("path <value> changes PATH"),
    "set": lambda a: OK,  # process-local only
    "chcp": lambda a: OK if not a else no("chcp <n> changes the code page"),
    "wsl.exe": lambda a: OK if a and a[0].lower() in ("-l", "--list", "--status", "--version") else no("only wsl --list/--status"),
    "start-sleep": lambda a: OK,
    # hardware
    "nvidia-smi": lambda a: no("this nvidia-smi option changes GPU settings") if any(
        x.split("=")[0] in NVIDIA_SET for x in a) else OK,
    "rocm-smi": lambda a: no("this rocm-smi option changes GPU settings") if any(
        x.startswith(("--set", "--reset", "-r", "--load", "--save", "--autorespond")) for x in a) else OK,
    "smartctl": lambda a: no("smartctl -s/-t/-X changes the drive") if any(
        x.split("=")[0] in ("-s", "--smart", "-t", "--test", "-X", "--abort", "-o", "--offlineauto", "-S", "--saveauto", "--set") for x in a) else OK,
    "nvme": lambda a: OK if a[:1] and a[0] in ("list", "smart-log", "id-ctrl", "id-ns", "error-log", "list-subsys", "version", "show-regs", "fw-log") else no("this nvme command changes the drive"),
    "hdparm": lambda a: OK if a and all(x in ("-I", "-i", "-g", "-C") or not x.startswith("-") for x in a) else no("only hdparm -I/-i/-g/-C is allowed"),
    "top": lambda a: OK if _has(a, "-b", "-bn1") or any(x.startswith("-b") for x in a) else no("top is interactive; use top -b -n 1"),
    "iotop": lambda a: OK if any(x.startswith("-b") for x in a) else no("iotop is interactive; use iotop -b -n 1"),
}
NVIDIA_SET = {"-pm", "--persistence-mode", "-e", "--ecc-config", "-p", "--reset-ecc-errors", "-c", "--compute-mode",
              "-dm", "--driver-model", "-fdm", "--force-driver-model", "--gom", "-r", "--gpu-reset", "-vm",
              "--virt-mode", "-lgc", "--lock-gpu-clocks", "-rgc", "--reset-gpu-clocks", "-lmc", "--lock-memory-clocks",
              "-rmc", "--reset-memory-clocks", "-ac", "--applications-clocks", "-rac", "--reset-applications-clocks",
              "-pl", "--power-limit", "-cc", "--cuda-clocks", "-am", "--accounting-mode", "-caa",
              "--clear-accounted-apps", "--auto-boost-default", "--auto-boost-permission", "mig", "-mig", "--multi-instance-gpu"}
FIND_DENY = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}


def _exec_after_flags(args: list[str], takes_value: set[str]) -> Verdict:
    j = 0
    while j < len(args) and args[j].startswith("-"):
        j += 2 if args[j] in takes_value else 1
    inner = args[j:]
    return check_command(" ".join(_quote(t) for t in inner)) if inner else OK


# ---------------------------------------------------------------- main entry

def _check_segment(segment: str, depth: int) -> Verdict:
    toks = _tokens(segment)
    # leading variable assignments: $x = ..., VAR=value cmd
    while toks:
        if re.fullmatch(r"\$[\w:]+", toks[0]) and len(toks) > 1 and toks[1] in ("=", "+="):
            toks = toks[2:]
            continue
        if re.fullmatch(r"\$[\w:.]+(=|\+=).*", toks[0]) or re.fullmatch(r"[A-Za-z_]\w*=.*", toks[0]):
            toks = toks[1:]
            continue
        break
    if not toks:
        return OK
    head = toks[0]
    if head == "__SUB__" or head.startswith("__SUB__") or head.startswith("$") or head.startswith("[") or re.fullmatch(r"[-\d.]+", head) or head in ("@", "__SUB__."):
        # an expression: (Get-Content x).Count, $x | ..., [math]::Round(1.5), 42
        rest = toks[1:]
        if rest and rest[0] in ("=", "+="):
            return OK
        return OK
    name = "sc.exe" if head.lower().endswith("sc.exe") else _name(head)
    args = toks[1:]
    if name in CONTROL:
        rest = " ".join(_quote(t) for t in args if t not in ("__SUB__",))
        if name in ("for", "foreach", "case", "switch", "in", "select") or not rest:
            return OK
        return _check_segment(rest, depth + 1)
    if name in WRAPPERS:
        if name == "env":
            while args and ("=" in args[0] or args[0].startswith("-")):
                args = args[1:]
        elif name == "timeout":
            while args and (args[0].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", args[0])):
                args = args[1:]
        else:
            while args and args[0].startswith("-"):
                args = args[1:]
        return _check_segment(" ".join(_quote(t) for t in args), depth + 1) if args else OK
    if name in SHELLS:
        return _shell(name, args)
    if name in SPECIAL:
        return SPECIAL[name](args)
    if name in ALWAYS:
        return OK
    if "-" in name and re.fullmatch(r"[a-z]+-[a-z0-9]+", name):  # PowerShell Verb-Noun
        if name in UNSAFE_CMDLETS:
            return no(f"{head} writes or changes something")
        if name in SAFE_CMDLETS:
            return OK
        verb = name.split("-", 1)[0]
        if verb in SAFE_VERBS:
            if name.startswith("out-") and name not in ("out-string", "out-host", "out-null", "out-default", "out-gridview_none"):
                return no(f"{head} writes output somewhere")
            return OK
        return no(f"{head} changes something ('{verb}' cmdlets aren't read-only)")
    if args and all(a.lower() in VERSION_ARGS for a in args):
        return OK  # foo --version
    return no(f"'{head}' isn't known to be read-only")


def check_command(command: str, _depth: int = 0) -> Verdict:
    """Return a truthy Verdict if `command` only reads; otherwise a falsy one with the reason."""
    if _depth > 6:
        return no("too deeply nested")
    text = command.strip()
    if not text:
        return no("empty command")
    if re.search(r"(?i)\b(iex|invoke-expression|invoke-command|icm|start-process|saps|invoke-item|ii)\b", text):
        return no("runs other programs or code")
    if re.search(r"(?i)-encodedcommand\b|\s-enc\s|-ec\s", text):
        return no("encoded commands can do anything")
    if MUTATING_METHOD.search(text):
        return no(f"calls a method that changes things ({MUTATING_METHOD.search(text).group(0).strip()})")
    flat, nested = _scan_groups(text)
    for inner in nested:
        if inner.strip().startswith("$") and "|" not in inner and not re.search(r"\b[A-Za-z]+-[A-Za-z]+\b", inner):
            continue  # an expression like ($x -gt 5)
        if re.fullmatch(r"\s*[\w$.@'\"\s,:=+\-*/%<>!]*", inner) and not re.search(r"\b[A-Za-z]+-[A-Za-z]+\b|\s", inner.strip()):
            continue  # a value: (1), ($env:PATH)
        v = check_command(inner, _depth + 1)
        if not v:
            return v
    redirect_free = ALLOWED_REDIRECTS.sub(" ", flat)
    if re.search(r">", _strip_quotes(redirect_free)):
        return no("redirects output into a file (>)")
    for segment in _split_commands(redirect_free):
        v = _check_segment(segment, _depth)
        if not v:
            return v
    return OK


def _strip_quotes(text: str) -> str:
    return re.sub(r"'[^']*'|\"[^\"]*\"", "''", text)
