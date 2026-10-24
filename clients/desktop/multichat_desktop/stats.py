"""Statistics window: price and performance per model (KPI tiles, table, charts)."""

from __future__ import annotations

from typing import Any

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QStackedBarSeries,
    QValueAxis,
)
from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from multichat_desktop.controller import Controller
from multichat_desktop.theme import colors, is_dark

COLUMNS = [
    ("Model", "display_name"),
    ("Endpoint", "endpoint"),
    ("Tokens in", "input_tokens"),
    ("Tokens out", "output_tokens"),
    ("Cost $", "cost"),
    ("Replies", "replies"),
    ("▲", "upvotes"),
    ("▼", "downvotes"),
    ("Approval", "approval"),
    ("Builder picks", "builder_picks"),
    ("Win rate", "win_rate"),
    ("Avg latency s", "avg_latency_ms"),
    ("TTFT s", "avg_ttft_ms"),
    ("tok/s", "tokens_per_s"),
    ("$ per ▲", "cost_per_upvote"),
]


def fmt(key: str, value: Any) -> str:
    if value is None:
        return "–"
    if key in ("approval", "win_rate"):
        return f"{value * 100:.0f}%"
    if key in ("avg_latency_ms", "avg_ttft_ms"):
        return f"{value / 1000:.2f}"
    if key in ("cost", "cost_per_upvote"):
        return f"{value:.4f}"
    if key in ("input_tokens", "output_tokens"):
        return f"{value:,}"
    return str(value)


class NumItem(QTableWidgetItem):
    """Sorts by the raw number, not the formatted text."""

    def __init__(self, text: str, value: float | None) -> None:
        super().__init__(text)
        self.value = -1.0 if value is None else float(value)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        return self.value < getattr(other, "value", 0.0)


class Tile(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        self.value = QLabel("–")
        self.value.setStyleSheet("font-size: 22px; font-weight: bold;")
        label = QLabel(title)
        label.setObjectName("muted")
        layout.addWidget(self.value)
        layout.addWidget(label)


class StatsWindow(QWidget):
    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.setWindowTitle("MultiChat — statistics")
        self.resize(1200, 820)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.range = QComboBox()
        for label, days in (("All time", None), ("Last 30 days", 30), ("Last 7 days", 7), ("Today", 1)):
            self.range.addItem(label, days)
        self.range.currentIndexChanged.connect(self.reload)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.reload)
        top.addWidget(QLabel("Period:"))
        top.addWidget(self.range)
        top.addStretch(1)
        top.addWidget(refresh)
        layout.addLayout(top)

        tiles = QGridLayout()
        self.tiles = {k: Tile(t) for k, t in (("cost", "Total cost"), ("tokens", "Tokens"), ("votes", "Votes ▲ / ▼"), ("best", "Most picked builder"))}
        for i, tile in enumerate(self.tiles.values()):
            tiles.addWidget(tile, 0, i)
        layout.addLayout(tiles)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([c for c, _ in COLUMNS])
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 2)

        charts = QHBoxLayout()
        self.cost_chart = self._chart_view()
        self.win_chart = self._chart_view()
        self.daily_chart = self._chart_view()
        for v in (self.cost_chart, self.win_chart, self.daily_chart):
            charts.addWidget(v)
        layout.addLayout(charts, 3)
        self.reload()

    def _chart_view(self) -> QChartView:
        view = QChartView()
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setMinimumHeight(260)
        return view

    def reload(self) -> None:
        days = self.range.currentData()
        date_from = None
        if days:
            date_from = QDateTime.currentDateTimeUtc().addDays(-days).toString(Qt.DateFormat.ISODate)
        self.ctl.call(self.ctl.client.stats(date_from), self.show_stats)

    def show_stats(self, stats: dict[str, Any]) -> None:
        rows = stats["models"]
        totals = stats["totals"]
        self.tiles["cost"].value.setText(f"${totals['cost']:.4f}")
        self.tiles["tokens"].value.setText(f"{totals['input_tokens'] + totals['output_tokens']:,}")
        self.tiles["votes"].value.setText(f"{totals['upvotes']} / {totals['downvotes']}")
        best = max(rows, key=lambda r: r["builder_picks"], default=None)
        self.tiles["best"].value.setText(best["display_name"] if best and best["builder_picks"] else "–")

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, (_, key) in enumerate(COLUMNS):
                value = row.get(key)
                text = ("~" if row["estimated"] and key in ("input_tokens", "output_tokens") else "") + fmt(key, value)
                item = NumItem(text, value) if isinstance(value, (int, float)) or value is None else QTableWidgetItem(text)
                if key == "display_name":
                    item = QTableWidgetItem("● " + text)
                    item.setForeground(QColor(row["color"]))
                if key not in ("display_name", "endpoint"):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, item)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()

        self.cost_chart.setChart(self._bars(rows, "cost", "Cost per model ($)"))
        self.win_chart.setChart(self._bars(rows, "win_rate", "Win rate % (picked as builder)", scale=100, cap=100))
        self.daily_chart.setChart(self._daily(stats["daily"], rows))

    def _base_chart(self, title: str) -> QChart:
        chart = QChart()
        chart.setTitle(title)
        chart.setTheme(QChart.ChartTheme.ChartThemeDark if is_dark() else QChart.ChartTheme.ChartThemeLight)
        chart.setBackgroundBrush(QColor(colors()["panel"]))
        chart.legend().setVisible(False)
        return chart

    def _bars(self, rows: list[dict], key: str, title: str, scale: float = 1, cap: float | None = None) -> QChart:
        """One bar per model in the model's own color (stacked series with one set per model)."""
        chart = self._base_chart(title)
        data = [r for r in rows if r.get(key) is not None]
        series = QStackedBarSeries()
        for i, r in enumerate(data):
            bar = QBarSet(r["display_name"])
            bar.append([r[key] * scale if j == i else 0 for j in range(len(data))])
            bar.setColor(QColor(r["color"]))
            series.append(bar)
        chart.addSeries(series)
        axis_x = QBarCategoryAxis()
        axis_x.append([r["display_name"] for r in data])
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)
        axis_y = QValueAxis()
        top = max([r[key] * scale for r in data] + [1e-9]) * 1.15
        axis_y.setRange(0, min(top, cap) if cap else top)
        chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)
        return chart

    def _daily(self, daily: list[dict], rows: list[dict]) -> QChart:
        """Cost per day, one colored bar per model in each day group."""
        chart = self._base_chart("Cost per day ($)")
        chart.legend().setVisible(True)
        days = sorted({d["day"] for d in daily})[-14:]
        series = QBarSeries()
        top = 1e-9
        for r in rows:
            values = {d["day"]: d["cost"] for d in daily if d["model_id"] == r["model_id"]}
            if not values:
                continue
            bar = QBarSet(r["display_name"])
            bar.append([values.get(day, 0.0) for day in days])
            bar.setColor(QColor(r["color"]))
            series.append(bar)
            top = max([top, *values.values()])
        chart.addSeries(series)
        axis_x = QBarCategoryAxis()
        axis_x.append([QDateTime.fromString(day, "yyyy-MM-dd").toString("MMM d") for day in days])
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)
        axis_y = QValueAxis()
        axis_y.setRange(0, top * 1.15)
        chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)
        return chart
