from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QWidget

from ..theme import COLORS

AVATAR_PATH = Path(__file__).resolve().parent.parent / "assets" / "brand" / "avatar.png"


class Avatar(QWidget):
    def __init__(self, size: int = 44, parent: QWidget | None = None):
        super().__init__(parent)
        self.size = size
        self.setFixedSize(size, size)
        self._pixmap = QPixmap(str(AVATAR_PATH)) if AVATAR_PATH.exists() else None

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addEllipse(rect)
        painter.setClipPath(path)
        if self._pixmap is not None and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(self.size, self.size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            painter.drawPixmap(0, 0, scaled)
            painter.end()
            return
        painter.fillRect(rect, QColor(COLORS["elevated"]))
        painter.setPen(QColor(COLORS["primary"]))
        font = QFont("Microsoft YaHei UI", max(12, int(self.size * 0.38)))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, "鱼")
        painter.end()
