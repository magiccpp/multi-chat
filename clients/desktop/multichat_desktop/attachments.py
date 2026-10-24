"""Attachments in the desktop app: files and images waiting in the composer, and the ones shown on message cards."""

from __future__ import annotations

import mimetypes
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QWidget

MAX_SIZE = 20 * 1024 * 1024  # the server's limit
THUMB = 56
CARD_THUMB = 160


@dataclass
class PendingAttachment:
    filename: str
    mime: str
    data: bytes

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def from_path(path: str) -> PendingAttachment:
    p = Path(path)
    size = p.stat().st_size
    if size > MAX_SIZE:
        raise ValueError(f"{p.name} is {human_size(size)}; the limit is {human_size(MAX_SIZE)}")
    return PendingAttachment(p.name, mimetypes.guess_type(p.name)[0] or "application/octet-stream", p.read_bytes())


def from_image(image: QImage, name: str | None = None) -> PendingAttachment:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return PendingAttachment(name or time.strftime("pasted-image-%H%M%S.png"), "image/png", bytes(buf.data()))


def from_mime(md: QMimeData) -> list[PendingAttachment]:
    """Files and images in a clipboard paste or a drop. Text is left to the editor."""
    out: list[PendingAttachment] = []
    paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile() and os.path.isfile(u.toLocalFile())]
    for path in paths:
        out.append(from_path(path))
    if not paths and md.hasImage():
        image = md.imageData()
        if isinstance(image, QPixmap):
            image = image.toImage()
        if isinstance(image, QImage) and not image.isNull():
            out.append(from_image(image))
    return out


def has_attachments(md: QMimeData) -> bool:
    return md.hasImage() or any(u.isLocalFile() and os.path.isfile(u.toLocalFile()) for u in md.urls())


def thumbnail(data: bytes, side: int) -> QPixmap | None:
    pix = QPixmap()
    if not pix.loadFromData(QByteArray(data)):
        return None
    return pix.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


def open_bytes(filename: str, data: bytes) -> None:
    """Open a downloaded attachment with the system's default app."""
    folder = Path(tempfile.gettempdir()) / "multichat-attachments"
    folder.mkdir(exist_ok=True)
    path = folder / Path(filename).name
    path.write_bytes(data)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class PendingChip(QFrame):
    """One attachment waiting in the composer: thumbnail or file icon, name, size, ✕."""

    removed = Signal(object)

    def __init__(self, att: PendingAttachment) -> None:
        super().__init__()
        self.att = att
        self.setObjectName("attachment")
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 2, 2)
        row.setSpacing(4)
        icon = QLabel()
        pix = thumbnail(att.data, THUMB) if att.is_image else None
        if pix is not None:
            icon.setPixmap(pix)
        else:
            icon.setText("🖼" if att.is_image else "📄")
        row.addWidget(icon)
        name = QLabel(f"{att.filename}\n{human_size(len(att.data))}")
        name.setObjectName("muted")
        row.addWidget(name)
        close = QToolButton()
        close.setText("✕")
        close.setToolTip("Remove")
        close.setAutoRaise(True)
        close.clicked.connect(lambda: self.removed.emit(self))
        row.addWidget(close)
        self.setToolTip(f"{att.filename} · {att.mime} · {human_size(len(att.data))}")


class AttachmentView(QWidget):
    """Attachments of a sent message: image thumbnails and document chips; click to open."""

    def __init__(self, ctl, attachments: list[dict]) -> None:  # noqa: ANN001
        super().__init__()
        self.ctl = ctl
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(6)
        for a in attachments:
            if a.get("kind") == "image":
                label = QLabel("🖼 loading…")
                label.setToolTip(f"{a['filename']} — click to open")
                label.setCursor(Qt.CursorShape.PointingHandCursor)
                label.mousePressEvent = lambda _e, a=a: self._open(a)  # noqa: B023
                row.addWidget(label)
                self.ctl.fetch_attachment(a["id"], lambda data, label=label: self._show(label, data))
            else:
                btn = QToolButton()
                btn.setObjectName("chip")
                icon = "📄" if a.get("kind") == "document" else "📦"
                btn.setText(f"{icon} {a['filename']}  ({human_size(a.get('size', 0))})")
                btn.setToolTip("Open" + ("" if a.get("kind") == "document" else " (the models only see the file name)"))
                btn.clicked.connect(lambda _=False, a=a: self._open(a))
                row.addWidget(btn)
        row.addStretch(1)

    @staticmethod
    def _show(label: QLabel, data: bytes) -> None:
        pix = thumbnail(data, CARD_THUMB)
        if pix is None:
            label.setText("🖼 (cannot display)")
        else:
            label.setPixmap(pix)

    def _open(self, a: dict) -> None:
        self.ctl.fetch_attachment(a["id"], lambda data: open_bytes(a["filename"], data))
