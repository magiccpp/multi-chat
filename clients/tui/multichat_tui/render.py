"""Rich renderables for messages, usage and stats, shared by the TUI and the plain CLI."""

from __future__ import annotations

import json
import re
from typing import Any

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from multichat_client.state import MessageView, SessionState, Wait, elapsed

BR = re.compile(r"<br\s*/?>", re.IGNORECASE)  # models put <br> in table cells
VOTE_MARK = {1: ("▲", "green"), -1: ("▼", "red")}


def money(value: float) -> str:
    if value == 0:
        return "$0"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def args_summary(arguments: dict[str, Any], limit: int = 80) -> str:
    if not arguments:
        return ""
    if "command" in arguments:
        text = str(arguments["command"])
    elif "path" in arguments or "file_path" in arguments:
        text = str(arguments.get("path") or arguments.get("file_path"))
    elif "url" in arguments:
        text = str(arguments["url"])
    elif "pattern" in arguments:
        text = f"/{arguments['pattern']}/"
    elif "query" in arguments:
        text = str(arguments["query"])
    else:
        text = json.dumps(arguments)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def message_header(st: SessionState, m: MessageView, *, focused: bool = False) -> Text:
    t = Text()
    t.append(f"#{m.seq} ", style="dim")
    if m.role == "summary":
        t.append(f"🗜 {st.name(m.model_id)}", style=f"bold {st.color(m.model_id)}")
        t.append("  context compacted", style="dim")
        return t
    if m.role == "user":
        t.append("You", style="bold")
        if m.recipients and len(m.recipients) < len(st.participants):
            t.append(" → " + ", ".join(st.name(r) for r in m.recipients), style="dim")
    else:
        t.append(f"● {st.name(m.model_id)}", style=f"bold {st.color(m.model_id)}")
        if m.phase == "build":
            t.append("  BUILD", style="bold magenta")
    if m.vote in VOTE_MARK:
        mark, color = VOTE_MARK[m.vote]
        t.append(f"  {mark}", style=f"bold {color}")
    if m.status == "streaming":
        t.append("  …", style="yellow")
    elif m.status in ("error", "cancelled"):
        t.append(f"  [{m.status}]", style="red")
    if m.role == "assistant" and m.usage:
        u = m.usage
        approx = "~" if u.get("estimated") else ""
        t.append(f"  {approx}{tokens(u['input_tokens'] + u['output_tokens'])} tok · {money(u['cost'])}", style="dim")
    if focused:
        t.append("   + up  - down  0 clear  b builder  y copy  enter details", style="dim italic")
    return t


def tool_lines(st: SessionState, m: MessageView, *, expanded: bool) -> list[RenderableType]:
    out: list[RenderableType] = []
    for call in m.tool_calls:
        line = Text("  ⚙ ", style="cyan")
        line.append(call.get("name", "tool"), style="bold cyan")
        summary = args_summary(call.get("arguments") or {})
        if summary:
            line.append(f"  {summary}", style="cyan")
        if call.get("external"):
            line.append("  (run by the agent CLI)", style="dim")
        out.append(line)
        result = st.results.get(call.get("id", ""))
        live = st.progress.get(call.get("id", ""), "")
        body = result.content if result else live
        if not body:
            if not call.get("external") and st.busy:
                out.append(Text("    running…", style="dim yellow"))
            continue
        lines = body.rstrip().splitlines()
        shown = lines if expanded else lines[:4]
        out.append(Text("    " + "\n    ".join(shown), style="dim"))
        if len(lines) > len(shown):
            out.append(Text(f"    … {len(lines) - len(shown)} more lines (enter to expand)", style="dim italic"))
    return out


def message_body(st: SessionState, m: MessageView, *, expanded: bool = False) -> RenderableType:
    parts: list[RenderableType] = []
    if m.content.strip():
        parts.append(Markdown(BR.sub(" · ", m.content)) if m.role == "assistant" else Text(m.content))
    elif m.role == "assistant" and m.status == "streaming" and not m.tool_calls:
        parts.append(Text("thinking…", style="dim italic"))
    if m.attachments:
        parts.append(attachments_text(m.attachments))
    parts.extend(tool_lines(st, m, expanded=expanded))
    return Group(*parts)


def human_size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def attachments_text(attachments: list[dict]) -> Text:
    t = Text()
    for a in attachments:
        icon = {"image": "🖼", "document": "📄"}.get(a.get("kind", ""), "📦")
        t.append(f"{icon} {a['filename']}", style="cyan")
        t.append(f" ({human_size(a.get('size', 0))})  ", style="dim")
    return t


def diff_renderable(diff: str) -> RenderableType:
    return Syntax(diff or "(no changes)", "diff", theme="ansi_dark", word_wrap=True)


WAIT_ICONS = {"approval": "✋", "tool": "⚙", "thinking": "…", "compacting": "⇣", "queued": "⏸"}


def wait_lines(waits: list[Wait]) -> Text:
    """ "⏳ Beta: running `uname -a` on This PC · 12 s" plus a dim hint line per wait."""
    t = Text()
    for i, w in enumerate(waits):
        if i:
            t.append("\n")
        t.append(WAIT_ICONS.get(w.state, "⏳") + " ")
        t.append(w.name, style=f"bold {w.color}")
        t.append(f": {w.text} ")
        t.append(f"· {elapsed(w.seconds)}", style="dim")
        if w.hint:
            t.append(f"\n   {w.hint}", style="dim italic")
    return t


def usage_text(st: SessionState) -> Text:
    t = Text()
    rows = [st.usage[k] for k in sorted(st.usage, key=lambda k: -(st.usage[k].cost))]
    for u in rows:
        t.append("● ", style=st.color(u.model_id))
        approx = "~" if u.estimated else ""
        t.append(f"{st.name(u.model_id)} {approx}{tokens(u.input_tokens + u.output_tokens)} {money(u.cost)}  ")
    if rows:
        t.append(f"Σ {money(st.total_cost())}", style="bold")
    else:
        t.append("no tokens used yet", style="dim")
    return t


def stats_table(stats: dict[str, Any], *, compact: bool = False) -> Table:
    """Per-model table; `compact` keeps the columns that fit in ~80 terminal columns."""

    def pct(v: float | None) -> str:
        return "–" if v is None else f"{v * 100:.0f}%"

    def secs(ms: float | None) -> str:
        return "–" if ms is None else f"{ms / 1000:.1f}s"

    columns: list[tuple[str, Any]] = [
        ("Model", lambda r: Text("● ", style=r["color"]) + Text(r["display_name"])),
        ("Endpoint", lambda r: r["endpoint"]),
        ("Tokens in", lambda r: ("~" if r["estimated"] else "") + tokens(r["input_tokens"])),
        ("Tokens out", lambda r: ("~" if r["estimated"] else "") + tokens(r["output_tokens"])),
        ("Tokens", lambda r: ("~" if r["estimated"] else "") + tokens(r["input_tokens"] + r["output_tokens"])),
        ("Cost", lambda r: money(r["cost"])),
        ("Replies", lambda r: str(r["replies"])),
        ("▲", lambda r: str(r["upvotes"])),
        ("▼", lambda r: str(r["downvotes"])),
        ("Approval", lambda r: pct(r["approval"])),
        ("Picks", lambda r: str(r["builder_picks"])),
        ("Win", lambda r: pct(r["win_rate"])),
        ("Latency", lambda r: secs(r["avg_latency_ms"])),
        ("TTFT", lambda r: secs(r["avg_ttft_ms"])),
        ("tok/s", lambda r: "–" if r["tokens_per_s"] is None else f"{r['tokens_per_s']:.0f}"),
        ("$/▲", lambda r: "–" if r["cost_per_upvote"] is None else money(r["cost_per_upvote"])),
    ]
    keep = (
        {"Model", "Tokens", "Cost", "▲", "▼", "Win", "Latency", "$/▲"}
        if compact
        else {name for name, _ in columns} - {"Tokens"}
    )
    columns = [c for c in columns if c[0] in keep]
    table = Table(title="Model statistics", expand=True)
    for name, _ in columns:
        table.add_column(name, justify="left" if name in ("Model", "Endpoint") else "right", no_wrap=name == "Model")
    for r in stats["models"]:
        table.add_row(*(fn(r) for _, fn in columns))
    t = stats["totals"]
    table.caption = (
        f"Total {money(t['cost'])} · {tokens(t['input_tokens'])} in / {tokens(t['output_tokens'])} out · "
        f"▲ {t['upvotes']} ▼ {t['downvotes']}   (~ = estimated tokens)"
    )
    return table


def bars(stats: dict[str, Any], key: str, title: str, fmt=money, width: int = 30) -> Table:  # noqa: ANN001
    rows = [r for r in stats["models"] if r.get(key) is not None]
    top = max((r[key] for r in rows), default=0) or 1
    table = Table(title=title, show_header=False, box=None, expand=True)
    table.add_column("model", ratio=1)
    table.add_column("bar", ratio=3)
    table.add_column("value", justify="right")
    for r in sorted(rows, key=lambda r: -r[key]):
        n = int(round(width * r[key] / top))
        table.add_row(Text(r["display_name"], style=r["color"]), Text("█" * n, style=r["color"]), fmt(r[key]))
    return table


def cost_table(rows: list[dict[str, Any]]) -> Table:
    """Per-model cost of a session (GET /api/sessions/{id}/usage)."""
    total = sum(r["cost"] for r in rows)
    t = Table(title="Session cost", expand=True)
    for col, just in (("Model", "left"), ("Replies", "right"), ("In tok", "right"), ("Out tok", "right"),
                      ("$/1M in", "right"), ("$/1M out", "right"), ("In $", "right"), ("Out $", "right"),
                      ("Total", "right"), ("Share", "right")):
        t.add_column(col, justify=just)
    for r in rows:
        unknown = not r.get("price_known", True)
        approx = "~" if r["estimated"] else ""
        t.add_row(
            Text("● " + r["display_name"], style=r["color"]), str(r.get("replies", 0)),
            f"{approx}{tokens(r['input_tokens'])}", f"{approx}{tokens(r['output_tokens'])}",
            "?" if unknown else f"{r.get('input_price', 0):g}", "?" if unknown else f"{r.get('output_price', 0):g}",
            "—" if r.get("input_cost") is None else money(r["input_cost"]),
            "—" if r.get("output_cost") is None else money(r["output_cost"]),
            Text("price unknown" if unknown else money(r["cost"]), style="bold"),
            f"{100 * r['cost'] / total:.0f}%" if total else "—",
        )
    t.add_row(Text("Session total", style="bold"), str(sum(r.get("replies", 0) for r in rows)),
              tokens(sum(r["input_tokens"] for r in rows)), tokens(sum(r["output_tokens"] for r in rows)),
              "", "", "", "", Text(money(total), style="bold"), "")
    return t


def context_table(rows: list[dict[str, Any]]) -> Table:
    """GET /api/sessions/{id}/context: how full each model's context window is."""
    t = Table(title="Context windows", expand=True)
    for col, just in (("Model", "left"), ("Used (est.)", "right"), ("Window", "right"), ("Full", "right"),
                      ("Compactions", "right"), ("", "left")):
        t.add_column(col, justify=just)
    for r in rows:
        pct = r.get("percent")
        t.add_row(r["display_name"], f"{r['used']:,}" if r.get("used") is not None else "—",
                  tokens(r["window"]) if r.get("window") else "—",
                  Text(f"{pct:.0f}%" if pct is not None else "—", style="yellow" if (pct or 0) >= 70 else ""),
                  str(r.get("compactions", 0)), Text(r.get("note", ""), style="dim"))
    return t
