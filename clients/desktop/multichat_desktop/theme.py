"""Light/dark palette following the Windows setting, plus shared colors."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication


def is_dark() -> bool:
    hints = QGuiApplication.styleHints()
    try:
        return hints.colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # Qt < 6.5
        return QGuiApplication.palette().window().color().lightness() < 128


TOKENS = {
    "dark": {
        "bg": "#1e1f22", "panel": "#2b2d31", "panel2": "#313338", "text": "#e6e6e6", "muted": "#9aa0a6",
        "accent": "#e8a33d", "border": "#3c3f45", "add": "#1f3d2a", "del": "#4a1f24", "addfg": "#7ee2a8", "delfg": "#ff8a8a",
    },
    "light": {
        "bg": "#f7f7f8", "panel": "#ffffff", "panel2": "#eef0f3", "text": "#1f2328", "muted": "#656d76",
        "accent": "#b86e00", "border": "#d0d7de", "add": "#dafbe1", "del": "#ffebe9", "addfg": "#116329", "delfg": "#a40e26",
    },
}


def colors() -> dict[str, str]:
    return TOKENS["dark" if is_dark() else "light"]


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")
    c = colors()
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(c["bg"]))
    pal.setColor(QPalette.ColorRole.Base, QColor(c["panel"]))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(c["panel2"]))
    pal.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Button, QColor(c["panel2"]))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(c["accent"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#000000"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["muted"]))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["panel"]))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(c["text"]))
    app.setPalette(pal)
    app.setStyleSheet(
        f"""
        QToolBar {{ border: none; spacing: 6px; padding: 4px; background: {c['panel']}; }}
        QToolBar#activity {{ background: {c['panel2']}; }}
        QToolBar#activity QToolButton {{ padding: 8px 6px; border-radius: 6px; font-size: 11px; }}
        QToolBar#activity QToolButton:checked {{ background: {c['bg']}; border-left: 3px solid {c['accent']}; }}
        QMainWindow::separator {{ background: {c['border']}; width: 6px; height: 6px; }}
        QMainWindow::separator:hover {{ background: {c['accent']}; }}
        QSplitter::handle {{ background: {c['border']}; }}
        QSplitter::handle:hover {{ background: {c['accent']}; }}
        QSplitter::handle:horizontal {{ width: 6px; }}
        QSplitter::handle:vertical {{ height: 6px; }}
        QDockWidget::title {{ padding: 6px; background: {c['panel2']}; }}
        QStatusBar {{ background: {c['panel']}; border-top: 1px solid {c['border']}; }}
        QFrame#card {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 8px; }}
        QFrame#card[focused="true"] {{ border: 1px solid {c['accent']}; }}
        QFrame#approval {{ background: {c['panel']}; border: 2px solid {c['accent']}; border-radius: 8px; }}
        QLabel#muted {{ color: {c['muted']}; }}
        QFrame#attachment {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 6px; }}
        QFrame#waitbar {{ background: {c['panel2']}; border-top: 1px solid {c['border']}; }}
        QFrame#costbar {{ background: {c['panel2']}; border-bottom: 1px solid {c['border']}; }}
        QToolButton#costchip {{ border: 1px solid {c['border']}; border-radius: 10px; padding: 1px 8px; background: {c['panel']}; }}
        QToolButton#chip {{ border: 1px solid {c['border']}; border-radius: 10px; padding: 2px 8px; }}
        QPushButton#chip {{ border: 1px solid {c['border']}; border-radius: 10px; padding: 2px 10px; }}
        QPushButton#chip:checked {{ background: {c['accent']}; color: black; border-color: {c['accent']}; }}
        QPushButton#primary {{ background: {c['accent']}; color: black; border-radius: 6px; padding: 6px 14px; font-weight: bold; }}
        QToolButton#vote {{ border: none; padding: 2px 6px; font-size: 13px; }}
        QToolButton#vote:checked {{ background: {c['panel2']}; border-radius: 4px; }}
        QPlainTextEdit#composer {{ border: 1px solid {c['border']}; border-radius: 8px; padding: 6px; }}
        QTabBar::tab {{ padding: 6px 12px; }}
        """
    )
