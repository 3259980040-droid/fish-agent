from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..theme import COLORS, SPACE, STATE_COLORS


class Card(QFrame):
    def __init__(self, title: str = "", subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(SPACE["card"], SPACE["card"], SPACE["card"], SPACE["card"])
        self.outer.setSpacing(10)
        if title:
            heading = QLabel(title)
            heading.setObjectName("CardTitle")
            self.outer.addWidget(heading)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("Muted")
            self.outer.addWidget(sub)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        self.outer.addLayout(self.body)


class SectionHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        label = QLabel(title)
        label.setObjectName("SectionTitle")
        layout.addWidget(label)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("Muted")
            layout.addWidget(sub)


class Divider(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)


class StatBlock(QWidget):
    def __init__(self, label: str, value: str = "—", parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.label = QLabel(label)
        self.label.setObjectName("Caption")
        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        layout.addWidget(self.label)
        layout.addWidget(self.value)


class StatusPill(QLabel):
    def __init__(self, text: str = "—", state: str = "neutral", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("StatusPill")
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.setStyleSheet("background: transparent; border: none; padding: 0;")
        self._state = state
        self.set_state(state)

    def set_state(self, state: str) -> None:
        if state not in STATE_COLORS:
            state = "neutral"
        self._state = state
        self.update()

    def set_text(self, text: str, state: str | None = None) -> None:
        self.setText(text)
        if state:
            self.set_state(state)
        self.updateGeometry()

    def sizeHint(self):  # noqa: N802
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance(self.text()) + 18, 22)

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(STATE_COLORS.get(self._state, COLORS["secondary"]))
        fill = QColor(color)
        fill.setAlpha(30)
        border = QColor(color)
        border.setAlpha(70)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        painter.setPen(border)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, self.height() / 2, self.height() / 2)
        painter.setPen(color)
        metrics = QFontMetrics(self.font())
        width = metrics.horizontalAdvance(self.text()) + 18
        painter.drawText(QRectF(0, 0, max(width, self.width()), self.height()),
                         Qt.AlignCenter, self.text())
        painter.end()
