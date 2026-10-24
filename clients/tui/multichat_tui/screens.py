from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, SelectionList, Static
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from multichat_tui.commands import COMMANDS
from multichat_tui.render import bars, diff_renderable, money, stats_table

MODAL_CSS = """
{name} {{ align: center middle; }}
{name} > Vertical {{
    width: 90%; max-width: 140; height: auto; max-height: 90%;
    border: thick $accent; background: $surface; padding: 1 2;
}}
{name} .title {{ text-style: bold; margin-bottom: 1; }}
{name} .hint {{ color: $text-muted; margin-top: 1; }}
"""


class ApprovalScreen(ModalScreen[tuple[bool, str | None]]):
    """Approve or reject an edit / command proposed by the builder."""

    DEFAULT_CSS = MODAL_CSS.format(name="ApprovalScreen") + "ApprovalScreen VerticalScroll { height: auto; max-height: 30; }"
    BINDINGS = [
        Binding("a", "approve", "Approve"),
        Binding("y", "approve", "Approve", show=False),
        Binding("r", "reject", "Reject"),
        Binding("n", "reject", "Reject", show=False),
        Binding("escape", "dismiss_later", "Decide later"),
    ]

    def __init__(self, event: dict[str, Any], model_name: str) -> None:
        super().__init__()
        self.event = event
        self.model_name = model_name

    def compose(self) -> ComposeResult:
        ev = self.event
        kind = ev.get("kind")
        with Vertical():
            if kind == "edit":
                yield Label(f"{self.model_name} wants to edit {ev.get('path')}", classes="title")
                with VerticalScroll():
                    yield Static(diff_renderable(ev.get("diff", "")))
            elif kind == "command":
                yield Label(f"{self.model_name} wants to run a command", classes="title")
                yield Static(Text("$ " + ev.get("command", ""), style="bold yellow"))
            else:
                yield Label(f"{self.model_name} wants to use {ev.get('tool')}", classes="title")
                yield Static(ev.get("detail", ""))
            yield Label("a approve   r reject   esc decide later", classes="hint")

    def action_approve(self) -> None:
        self.dismiss((True, None))

    def action_reject(self) -> None:
        self.dismiss((False, None))

    def action_dismiss_later(self) -> None:
        self.dismiss(None)  # type: ignore[arg-type]


class ModelsScreen(ModalScreen[list[int] | None]):
    """Tick the models (from any endpoint) that take part in this session."""

    DEFAULT_CSS = MODAL_CSS.format(name="ModelsScreen") + "ModelsScreen SelectionList { height: auto; max-height: 30; }"
    BINDINGS = [Binding("enter", "save", "Save", priority=True), Binding("escape", "cancel", "Cancel")]

    def __init__(self, models: list[dict], selected: list[int], runner_clis: set[str]) -> None:
        super().__init__()
        self.models = models
        self.selected = selected
        self.runner_clis = runner_clis

    def compose(self) -> ComposeResult:
        items = []
        for m in self.models:
            if not m["enabled"]:
                continue
            label = Text()
            label.append("● ", style=m["color"])
            label.append(m["display_name"])
            label.append(f"  {m['endpoint_name']}", style="dim")
            if m["endpoint_kind"] == "agent_cli":
                label.append("  agent CLI", style="magenta")
            items.append(Selection(label, m["id"], m["id"] in self.selected))
        with Vertical():
            yield Label("Models in this session", classes="title")
            yield SelectionList[int](*items)
            yield Label("space toggle   enter save   esc cancel   (agent CLIs need a folder on a runner or SSH host)", classes="hint")

    def action_save(self) -> None:
        chosen = list(self.query_one(SelectionList).selected)
        # keep the existing order for models that stay, append new ones
        ordered = [m for m in self.selected if m in chosen] + [m for m in chosen if m not in self.selected]
        self.dismiss(ordered)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionsScreen(ModalScreen[int | None]):
    DEFAULT_CSS = MODAL_CSS.format(name="SessionsScreen") + "SessionsScreen OptionList { height: auto; max-height: 30; }"
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, sessions: list[dict]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        options = []
        for s in self.sessions:
            t = Text()
            t.append(f"{s['title']}", style="bold")
            t.append(f"  {s['phase'].upper()} · {s['context_mode']} · {s['workspace_label']}", style="dim")
            t.append("  " + ", ".join(p["display_name"] for p in s["participants"]), style="cyan")
            options.append(Option(t, id=str(s["id"])))
        with Vertical():
            yield Label("Sessions", classes="title")
            yield OptionList(*options) if options else Label("No sessions yet.")
            yield Label("enter open   esc cancel", classes="hint")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(int(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextScreen(ModalScreen[None]):
    """Read-only modal used for /help, /stats and /endpoints."""

    DEFAULT_CSS = MODAL_CSS.format(name="TextScreen") + "TextScreen VerticalScroll { height: auto; max-height: 40; }"
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close", show=False)]

    def __init__(self, title: str, body: Any) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text, classes="title")
            with VerticalScroll():
                yield Static(self.body)
            yield Label("esc close", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)


def help_body() -> Table:
    t = Table(show_header=False, box=None, expand=True)
    t.add_column(style="bold cyan", no_wrap=True)
    t.add_column()
    for key, text in (
        ("enter", "send   (alt+enter or ctrl+j: new line)"),
        ("tab / shift+tab", "next / previous model tab (All, then one per model)"),
        ("alt+↑ / alt+↓", "focus replies from the input; ↑/↓ move between them"),
        ("+  -  0", "upvote / downvote / clear the focused reply"),
        ("b", "pick the focused reply's model as builder (switches to BUILD)"),
        ("enter (on a reply)", "expand tool output"),
        ("y", "copy the focused reply"),
        ("1..9", "quick-vote after a PLAN turn (when /quickvote is on)"),
        ("ctrl+b", "toggle the sidebar"),
        ("ctrl+p", "command palette"),
        ("esc", "back to the input   ·   ctrl+c quit"),
    ):
        t.add_row(key, text)
    t.add_row("", "")
    for c in COMMANDS:
        t.add_row(c.usage, c.help)
    return t


def stats_body(stats: dict[str, Any], compact: bool = False) -> Group:
    pct = lambda v: f"{v * 100:.0f}%"  # noqa: E731
    return Group(
        stats_table(stats, compact=compact),
        bars(stats, "cost", "Cost per model"),
        bars(stats, "win_rate", "Win rate (picked as builder / sessions joined)", fmt=pct),
        bars(stats, "approval", "Approval (▲ / all votes)", fmt=pct),
    )


def endpoints_body(endpoints: list[dict], models: list[dict], settings: dict) -> Group:
    t = Table(title="Endpoints (configured on the server)", expand=True)
    for col in ("Name", "Kind", "Base URL", "Key", "Source", "Models"):
        t.add_column(col)
    for e in endpoints:
        names = ", ".join(m["display_name"] for m in models if m["endpoint_id"] == e["id"])
        kind = e["kind"] + (f" ({e['driver']})" if e.get("driver") else "")
        t.add_row(
            Text(e["name"], style="bold" if e["enabled"] else "dim strike"),
            kind,
            e.get("base_url") or "",
            e.get("api_key_masked") or "–",
            e["source"],
            names,
        )
    prices = Table(title="Model prices (USD per 1M tokens)", expand=True)
    for col in ("Model", "Vendor id", "Input", "Output"):
        prices.add_column(col)
    for m in models:
        prices.add_row(Text(m["display_name"], style=m["color"]), m["model_name"], money(m["input_price"]), money(m["output_price"]))
    search = settings.get("web_search", {})
    footer = Text(f"web_search: {search.get('provider') or 'not configured'}   ·   edit config.yaml on the server, then POST /api/admin/reload-config", style="dim")
    return Group(t, prices, footer)
