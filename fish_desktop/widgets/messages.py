from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget, QTextBrowser, QSizePolicy
from ..theme import COLORS, TYPE


class MarkdownBody(QTextBrowser):
    """§4.5: use the document's actual height, including its final paragraph."""
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setObjectName('BubbleRichText')
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setOpenExternalLinks(False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(300)
        self.setStyleSheet(f'QTextBrowser {{ background: transparent; border: none; padding: 0; color: {COLORS["text"]}; font-size: {TYPE["body"]}px; }}')
        self.document().setDocumentMargin(2)
        self.document().setDefaultStyleSheet('p { margin-top: 4px; margin-bottom: 8px; } li { margin-bottom: 4px; }')
        self.setMarkdown(text)
        self.document().contentsChanged.connect(self._height)
        QTimer.singleShot(0, self._height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._height()

    def _height(self):
        self.document().setTextWidth(max(200, self.viewport().width()))
        height = int(self.document().size().height()) + 12
        if self.height() != height:
            self.setFixedHeight(max(32, height))


class MessageBubble(QFrame):
    def __init__(self, text: str, direction: str, meta: str, state: str = "neutral",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("MessageOut" if direction == "outbound" else "MessageIn")
        self.setProperty("state", state)
        self.setMaximumWidth(580)
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 8)
        layout.setSpacing(5)
        body = QLabel(text)
        body.setObjectName("BubbleText")
        body.setWordWrap(True)
        body.setTextFormat(Qt.PlainText)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(body)
        foot = QLabel(meta)
        foot.setObjectName("BubbleMeta")
        foot.setWordWrap(True)
        foot.setProperty("state", "bad" if state == "bad" else "warn" if state == "warn" else "")
        layout.addWidget(foot)

    def set_markdown(self, text):
        old = self.findChild(QLabel, 'BubbleText')
        if old:
            body = MarkdownBody(text, self)
            self.layout().replaceWidget(old, body)
            old.hide(); old.deleteLater()
