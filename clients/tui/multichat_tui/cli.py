"""`multichat` command: the TUI by default, plus plain subcommands for scripts.

  multichat                         open the terminal UI (latest session)
  multichat --session 12            open a session
  multichat run "prompt" --models gpt-5,claude [--json]
  multichat vote <message-id|#n> up|down|clear [--session ID]
  multichat history [--session ID]
  multichat stats [--from DATE] [--to DATE] [--json]
  multichat sessions | models
  multichat runner [--root DIR ...] [--name NAME]
  multichat login [--url URL] [--token TOKEN]
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from rich.console import Console

from multichat_client import config as cfgmod
from multichat_client.client import ApiError, MultiChatClient
from multichat_client.state import MessageView, SessionState
from multichat_tui.commands import match_model
from multichat_tui.render import message_body, message_header, money, stats_table, usage_text

console = Console()
VOTES = {"up": 1, "+1": 1, "down": -1, "-1": -1, "clear": 0, "0": 0}


def _client(args: argparse.Namespace) -> MultiChatClient:
    cfg = cfgmod.load()
    url = args.url or cfg.url
    token = args.token or cfg.token
    if not url or not token:
        console.print("[red]No server configured.[/] Run [bold]multichat login[/] or set MULTICHAT_URL and MULTICHAT_TOKEN.")
        sys.exit(2)
    return MultiChatClient(url, token)


async def _latest_session(c: MultiChatClient, session_id: int | None) -> int:
    if session_id:
        return session_id
    sessions = await c.sessions(limit=1)
    if not sessions:
        console.print("[red]No sessions yet.[/]")
        sys.exit(1)
    return sessions[0]["id"]


async def cmd_run(args: argparse.Namespace) -> int:
    async with _client(args) as c:
        models = await c.models(enabled_only=True)
        if args.session:
            sid = args.session
            if args.models:
                ids = [_resolve(m, models)["id"] for m in args.models.split(",")]
                await c.set_session_models(sid, ids)
        else:
            if not args.models:
                console.print("[red]--models is required for a new session[/] (e.g. --models gpt-5,claude)")
                return 2
            ids = [_resolve(m, models)["id"] for m in args.models.split(",")]
            title = args.title or args.prompt.strip().splitlines()[0][:60]
            workspace = None
            if args.folder:
                workspace = await _workspace_spec(c, args.folder)
            sid = (await c.create_session(title, ids, args.mode, workspace))["id"]
        session = await c.session(sid)
        st = SessionState()
        st.load(session, [], [], models)
        attachment_ids = []
        for path in args.attach or []:
            p = Path(path).expanduser()
            if not p.is_file():
                console.print(f"[red]no such file: {path}[/]")
                return 2
            try:
                attachment_ids.append((await c.upload_attachment(sid, p.name, p.read_bytes()))["id"])
            except ApiError as exc:
                console.print(f"[red]{p.name}: {exc.detail}[/]")
                return 1
        finished: list[MessageView] = []
        async for ev in c.chat(sid, args.prompt, attachment_ids=attachment_ids):
            st.apply(ev)
            if ev["type"] == "message_end":
                m = st.messages[ev["message"]["id"]]
                finished.append(m)
                if not args.json:
                    console.print(message_header(st, m))
                    console.print(f"[dim]message id {m.id}[/]")
                    console.print(message_body(st, m))
                    console.print()
            elif ev["type"] == "approval_required" and not args.json:
                console.print(f"[yellow]approval needed for {ev.get('kind')}: {ev.get('command') or ev.get('path')} "
                              f"— approve it in the TUI/desktop app, or set /approve cmds auto[/]")
            elif ev["type"] in ("error", "notice") and not args.json:
                console.print(f"[red]{ev.get('message')}[/]")
        if args.json:
            for m in finished:
                print(json.dumps({"session_id": sid, "message_id": m.id, "seq": m.seq, "model": st.name(m.model_id),
                                  "model_id": m.model_id, "status": m.status, "content": m.content}))
        else:
            console.print(usage_text(st))
            console.print(f"[dim]session {sid} · vote with: multichat vote <message id> up|down[/]")
        return 0


def _resolve(name: str, models: list[dict]) -> dict:
    m = match_model(name, models)
    if m is None:
        console.print(f"[red]No model matches {name!r}.[/] Known: " + ", ".join(x["display_name"] for x in models if x["available"]))
        sys.exit(2)
    if not m["available"]:
        console.print(f"[red]{m['display_name']} is unavailable right now:[/] {m.get('availability_detail') or '?'}")
        sys.exit(2)
    return m


async def _workspace_spec(c: MultiChatClient, spec: str) -> dict:
    if spec.startswith("ssh:"):
        _, name, path = spec.split(":", 2)
        conn = next((x for x in await c.ssh_connections() if x["name"] == name), None)
        if conn is None:
            console.print(f"[red]No SSH connection {name!r}[/]")
            sys.exit(2)
        return {"kind": "ssh", "ssh_connection_id": conn["id"], "path": path}
    if spec.startswith("runner:"):
        _, name, path = spec.split(":", 2)
        runner = next((x for x in await c.runners() if x["name"] == name), None)
        if runner is None:
            console.print(f"[red]No runner {name!r}[/]")
            sys.exit(2)
        return {"kind": "runner", "runner_id": runner["id"], "path": path}
    console.print("[red]--folder takes ssh:<connection>:<path> or runner:<name>:<path>[/]")
    sys.exit(2)


async def cmd_vote(args: argparse.Namespace) -> int:
    value = VOTES.get(args.value)
    if value is None:
        console.print("[red]value must be up, down or clear[/]")
        return 2
    async with _client(args) as c:
        session_id = None
        if args.ref.startswith("#"):
            session_id = await _latest_session(c, args.session)
        try:
            r = await c.vote(args.ref, value, session_id)
        except ApiError as exc:
            console.print(f"[red]{exc.detail}[/]")
            return 1
        mark = {1: "[green]▲ upvoted[/]", -1: "[red]▼ downvoted[/]", 0: "vote cleared"}[value]
        console.print(f"{mark} message {r['message_id']} (#{r['seq']})")
        return 0


async def cmd_history(args: argparse.Namespace) -> int:
    async with _client(args) as c:
        sid = await _latest_session(c, args.session)
        session, messages, usage, models = await asyncio.gather(c.session(sid), c.messages(sid), c.usage(sid), c.models())
        st = SessionState()
        st.load(session, messages, usage, models)
        if args.json:
            print(json.dumps(messages, indent=2, default=str))
            return 0
        console.print(f"[bold]{session['title']}[/]  [dim]session {sid} · {session['phase'].upper()} · "
                      f"{session['context_mode']} · {session['workspace_label']}[/]\n")
        for m in st.ordered():
            if m.role == "tool":
                continue
            console.print(message_header(st, m), f"[dim](id {m.id})[/]")
            console.print(message_body(st, m))
            console.print()
        console.print(usage_text(st))
        return 0


async def cmd_stats(args: argparse.Namespace) -> int:
    async with _client(args) as c:
        stats = await c.stats(args.date_from, args.date_to)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            console.print(stats_table(stats, compact=console.width < 140))
        return 0


async def cmd_sessions(args: argparse.Namespace) -> int:
    async with _client(args) as c:
        for s in await c.sessions():
            names = ", ".join(p["display_name"] for p in s["participants"])
            console.print(f"[bold]{s['id']:>4}[/]  {s['title']}  [green]{money(s.get('cost') or 0)}[/]  [dim]{s['phase'].upper()} · {s['context_mode']} · "
                          f"{s['workspace_label']}[/]  [cyan]{names}[/]")
        return 0


async def cmd_models(args: argparse.Namespace) -> int:
    async with _client(args) as c:
        for m in await c.models():
            state = "" if m["enabled"] else " [dim](disabled)[/]"
            if not m["available"]:
                state += f" [grey50](unavailable: {m.get('availability_detail') or '?'})[/]"
            console.print(f"[{m['color']}]●[/] [bold]{m['display_name']}[/]  [dim]{m['endpoint_name']} · {m['model_name']} · "
                          f"${m['input_price']}/${m['output_price']} per 1M[/]{state}")
        return 0


async def cmd_runner(args: argparse.Namespace) -> int:
    from multichat_client.runner.agent import Runner

    cfg = cfgmod.load()
    roots = args.root or cfg.runner_roots or [os.getcwd()]
    c = _client(args)
    await c.close()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    def status(state: str, detail: str) -> None:
        console.print(f"[dim]{state}[/] {detail}")

    runner = Runner(c.url, c.token, roots, name=args.name or cfg.runner_name or None, on_status=status)
    console.print(f"Runner sharing: {', '.join(roots)}")
    await runner.run_forever()
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    cfg = cfgmod.load()
    url = args.url or input(f"Server URL [{cfg.url or 'http://localhost:8000'}]: ").strip() or cfg.url or "http://localhost:8000"
    token = args.token or getpass.getpass("API token: ").strip() or cfg.token

    async def check() -> dict[str, Any]:
        async with MultiChatClient(url, token) as c:
            await c.health()
            return {"models": len(await c.models())}

    try:
        info = asyncio.run(check())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not connect: {exc}[/]")
        return 1
    cfg.url, cfg.token = url.rstrip("/"), token
    cfgmod.save(cfg)
    console.print(f"[green]Saved[/] {cfgmod.CONFIG_PATH} ({info['models']} models available)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="multichat", description="MultiChat: chat with several LLMs, then build with the best one.")
    p.add_argument("--url", help="server URL (default: config / MULTICHAT_URL)")
    p.add_argument("--token", help="API token (default: config / MULTICHAT_TOKEN)")
    p.add_argument("--session", type=int, help="session id to open")
    p.add_argument("--no-runner", action="store_true", help="do not let the server run tasks on this machine")
    p.add_argument("--root", action="append", help="folder the runner may use (repeatable; default: current folder)")
    sub = p.add_subparsers(dest="command")

    r = sub.add_parser("run", help="send one prompt and print the replies")
    r.add_argument("prompt")
    r.add_argument("--models", help="comma separated model names")
    r.add_argument("--mode", choices=["shared", "isolated"], default="isolated")
    r.add_argument("--title")
    r.add_argument("--folder", help="ssh:<connection>:<path> or runner:<name>:<path>")
    r.add_argument("--json", action="store_true")
    r.add_argument("--session", type=int, dest="session")
    r.add_argument("--attach", action="append", metavar="FILE", help="attach a document or image (repeatable)")

    v = sub.add_parser("vote", help="vote on a reply")
    v.add_argument("ref", help="message id, or #n (per-session index)")
    v.add_argument("value", choices=sorted(VOTES))
    v.add_argument("--session", type=int, dest="session")

    h = sub.add_parser("history", help="print a session with message ids and votes")
    h.add_argument("--session", type=int, dest="session")
    h.add_argument("--json", action="store_true")

    s = sub.add_parser("stats", help="price and performance per model")
    s.add_argument("--from", dest="date_from")
    s.add_argument("--to", dest="date_to")
    s.add_argument("--json", action="store_true")

    sub.add_parser("sessions", help="list sessions")
    sub.add_parser("models", help="list models configured on the server")

    rn = sub.add_parser("runner", help="run only the runner (headless), so the server can work on this machine")
    rn.add_argument("--root", action="append")
    rn.add_argument("--name")

    lg = sub.add_parser("login", help="save the server URL and token")
    lg.add_argument("--url", dest="url")
    lg.add_argument("--token", dest="token")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "run": cmd_run,
        "vote": cmd_vote,
        "history": cmd_history,
        "stats": cmd_stats,
        "sessions": cmd_sessions,
        "models": cmd_models,
        "runner": cmd_runner,
    }
    if args.command == "login":
        return cmd_login(args)
    if args.command in handlers:
        try:
            return asyncio.run(handlers[args.command](args))
        except KeyboardInterrupt:
            return 130
        except ApiError as exc:
            console.print(f"[red]{exc.detail}[/]")
            return 1

    from multichat_tui.app import MultiChatApp

    client = _client(args)
    app = MultiChatApp(
        client, cfgmod.load(), session_id=args.session, start_runner=not args.no_runner, runner_roots=args.root
    )
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
