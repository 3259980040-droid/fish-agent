"""Shared desktop components, QQ message history and event views."""

from __future__ import annotations
from locations import project_path

import json
from typing import Any

from PySide6.QtCore import Qt, QTimer, QDate, Signal
from PySide6.QtGui import QBrush, QColor, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QLineEdit, QDateEdit, QCheckBox,
)

from .services import FishServices
from .theme import COLORS, SPACE
from .widgets import Avatar, Card, Divider, StatusPill, MessageBubble, icon
from .workers import Runner

STATUS_CN = {
    "running": "运行中", "ready": "就绪", "connected": "已连接",
    "stopped": "已停止", "degraded": "降级", "unavailable": "不可用",
    "disconnected": "未连接", "failed": "失败", "processed": "已处理",
    "sent": "已发送", "cancelled": "已取消", "queued": "排队中",
    "pending": "待发送", "active": "活跃", "partial": "部分完成",
    "completed": "已完成", "done": "完成", "metadata_only": "仅元数据",
    "subtitle_read": "已读字幕", "ok": "正常", "error": "错误",
    "starting": "启动中", "connecting": "连接中", "timeout": "超时",
    "unconfigured": "未配置",
}


def status_cn(value: Any) -> str:
    text = str(value or "")
    return STATUS_CN.get(text, text or "未知")


def state_pill(value: Any) -> str:
    text = str(value or "").lower()
    if text in ("running", "ready", "connected", "ok", "done", "completed", "processed", "sent"):
        return "good"
    if text in ("degraded", "connecting", "queued", "partial", "pending", "warning"):
        return "warn"
    if text in ("failed", "error", "unavailable", "timeout"):
        return "bad"
    return "neutral"


def direction_cn(value: Any) -> str:
    return {"inbound": "入站", "outbound": "出站"}.get(str(value), str(value or ""))


def kind_cn(value: Any) -> str:
    return {"qq_turn": "QQ 轮次", "qq_message": "QQ 消息", "research_sync": "研究同步"}.get(
        str(value), str(value or "")
    )


def modalities_cn(value: Any) -> str:
    mapping = {"subtitle": "字幕", "metadata": "元数据", "audio": "音频",
               "video": "视频", "frames": "帧", "ocr": "OCR"}
    try:
        items = json.loads(value) if isinstance(value, str) else (value or [])
        if isinstance(items, list):
            return "、".join(mapping.get(str(item), str(item)) for item in items)
    except Exception:
        pass
    return str(value or "")


def _label(text: str = "", name: str = "") -> QLabel:
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    return label


def _set(label: QLabel, text: str, color: str | None = None) -> None:
    label.setText(text)
    if color:
        label.setStyleSheet(f"color: {color};")


def _button(text: str, primary: bool = False, danger: bool = False) -> QPushButton:
    button = QPushButton(text)
    if primary:
        button.setObjectName("PrimaryButton")
    elif danger:
        button.setObjectName("DangerButton")
    return button


def _tech_toggle(text: str = "技术详情") -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setCheckable(True)
    button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    button.setIcon(icon("chevron", 14))
    return button


class Table(QTableWidget):
    def __init__(self, headers: list[str], parent: QWidget | None = None):
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(headers)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.setShowGrid(False)
        self.setWordWrap(False)

    def fill(self, rows: list[list[str]], widths: list[int] | None = None) -> None:
        selected = self.item(self.currentRow(), 0)
        selected_key = selected.text() if selected else None
        scroll = self.verticalScrollBar().value()
        self.setRowCount(0)
        for row in rows:
            index = self.rowCount()
            self.insertRow(index)
            for col, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.setItem(index, col, item)
        if widths:
            for col, width in enumerate(widths):
                self.setColumnWidth(col, width)
        if selected_key:
            for index in range(self.rowCount()):
                if self.item(index, 0).text() == selected_key:
                    self.selectRow(index)
                    break
        self.verticalScrollBar().setValue(scroll)


class BasePage(QWidget):
    def __init__(self, services: FishServices, parent: QWidget | None = None):
        super().__init__(parent)
        self.services = services
        self.runner = Runner(self)
        self.loading = False
        self._pending_jobs = 0
        self.page_title = "Fish"
        self.page_subtitle = ""
        self.toast = _label("", "Muted")
        self.toast.setTextFormat(Qt.PlainText)
        self.toast.setWordWrap(True)

    def _submit(self, fn, on_result, on_error=None, on_done=None) -> None:
        self._pending_jobs += 1
        self.loading = True
        self.runner.submit(
            fn, on_result,
            on_error or (lambda message: self._toast(message, COLORS["danger"])),
            lambda: self._done(on_done),
        )

    def _done(self, on_done=None) -> None:
        self._pending_jobs = max(0, self._pending_jobs - 1)
        self.loading = bool(self._pending_jobs)
        for name in ("start_btn", "stop_btn", "save_btn", "test_btn", "sync_btn"):
            widget = getattr(self, name, None)
            if widget is not None and not self.loading:
                widget.setEnabled(True)
        if on_done:
            on_done()

    def _toast(self, message: str, color: str = COLORS["secondary"]) -> None:
        _set(self.toast, message, color)

    def refresh(self) -> None:
        pass

    def closeEvent(self, event) -> None:
        self.runner.shutdown()
        super().closeEvent(event)

    def _header(self, title: str, subtitle: str, parent_layout: QVBoxLayout) -> QHBoxLayout:
        row = QHBoxLayout()
        box = QVBoxLayout()
        box.setSpacing(2)
        box.addWidget(_label(title, "PageTitle"))
        if subtitle:
            box.addWidget(_label(subtitle, "PageSubtitle"))
        row.addLayout(box)
        row.addStretch(1)
        parent_layout.addLayout(row)
        return row




class QQPage(BasePage):
    draftRequested = Signal(dict)
    def __init__(self, services: FishServices):
        super().__init__(services)
        self.page_title = "大鱼"
        self.page_subtitle = "QQ 私聊"
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE["page"], SPACE["page"], SPACE["page"], SPACE["page"])
        root.setSpacing(SPACE["section"])

        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(Avatar(56))
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(_label("大鱼", "PageTitle"))
        self.qq_line = _label("QQ — · 主人专属", "Muted")
        title_box.addWidget(self.qq_line)
        head.addLayout(title_box)
        head.addStretch(1)
        self.online_pill = StatusPill("离线", "neutral")
        head.addWidget(self.online_pill)
        self.refresh_btn = _button("刷新")
        head.addWidget(self.refresh_btn)
        root.addLayout(head)
        root.addWidget(self.toast)

        filters = QHBoxLayout()
        self.chat_selector = QComboBox(); self.chat_selector.addItem('主人私聊', '')
        self.history_search = QLineEdit(); self.history_search.setPlaceholderText('搜索这段 QQ 对话')
        self.date_filter = QCheckBox('按日期')
        self.after_date = QDateEdit(QDate.currentDate().addDays(-30)); self.before_date = QDateEdit(QDate.currentDate())
        for field in (self.after_date, self.before_date): field.setCalendarPopup(True); field.setDisplayFormat('yyyy-MM-dd'); field.setMaximumWidth(150)
        for field in (self.chat_selector, self.history_search, self.date_filter, self.after_date, self.before_date): filters.addWidget(field)
        root.addLayout(filters)
        self.chat_selector.currentIndexChanged.connect(self.refresh); self.history_search.returnPressed.connect(self.refresh)
        self.date_filter.toggled.connect(self.refresh); self.after_date.dateChanged.connect(self.refresh); self.before_date.dateChanged.connect(self.refresh)

        content = QHBoxLayout()
        content.setSpacing(SPACE["card"])

        conversation = Card("对话")
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.bubble_host = QWidget()
        self.bubble_layout = QVBoxLayout(self.bubble_host)
        self.bubble_layout.setContentsMargins(2, 2, 2, 2)
        self.bubble_layout.setSpacing(8)
        self.bubble_layout.addStretch(1)
        self.scroll.setWidget(self.bubble_host)
        conversation.body.addWidget(self.scroll)
        content.addWidget(conversation, 2)

        side = QVBoxLayout()
        side.setSpacing(SPACE["card"])
        status = Card("状态")
        self.info: dict[str, QLabel] = {}
        self.status_pills: dict[str, StatusPill] = {}
        for key, title in (("runtime", "Runtime"), ("fishqq", "fish-qq"),
                           ("napcat", "NapCat"), ("loop", "Agent Loop")):
            row = QHBoxLayout()
            row.addWidget(_label(title, "Value"))
            row.addStretch(1)
            pill = StatusPill("—", "neutral")
            self.status_pills[key] = pill
            row.addWidget(pill)
            status.body.addLayout(row)
        status.body.addWidget(Divider())
        status.body.addWidget(_label('启动与停止统一在工作台操作。', 'Caption'))
        self.cancel_btn = _button("取消 QQ 在途任务", danger=True)
        status.body.addWidget(self.cancel_btn)
        status.body.addWidget(Divider())
        self.tech_btn = _tech_toggle("技术详情")
        status.body.addWidget(self.tech_btn)
        self.tech_panel = QWidget()
        tech_layout = QGridLayout(self.tech_panel)
        tech_layout.setContentsMargins(0, 4, 0, 0)
        tech_layout.setHorizontalSpacing(10)
        tech_layout.setVerticalSpacing(5)
        for i, (key, title) in enumerate((("conversation", "会话"), ("session", "Session"),
                                          ("port", "Port"), ("endpoint", "Endpoint"))):
            tech_layout.addWidget(_label(title, "Caption"), i, 0)
            value = _label("—", "Muted")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.info[key] = value
            tech_layout.addWidget(value, i, 1)
        self.tech_panel.setVisible(False)
        self.tech_btn.toggled.connect(self.tech_panel.setVisible)
        status.body.addWidget(self.tech_panel)
        side.addWidget(status)

        counts = Card("消息统计")
        self.count_labels: dict[str, QLabel] = {}
        row = QHBoxLayout()
        for key, title in (("inbound", "收到"), ("outbound", "发出"),
                           ("failed", "失败"), ("cancelled", "取消")):
            box = QVBoxLayout()
            box.addWidget(_label(title, "Caption"))
            value = _label("0", "StatValue")
            self.count_labels[key] = value
            box.addWidget(value)
            row.addLayout(box)
        counts.body.addLayout(row)
        side.addWidget(counts)
        side.addStretch(1)
        content.addLayout(side, 1)
        root.addLayout(content, 1)

        self.refresh_btn.clicked.connect(self.refresh)
        self.cancel_btn.clicked.connect(self._cancel)

    def _cancel(self) -> None:
        self._toast("取消当前生成…", COLORS["warning"])
        self._submit(self.services.cancel_turn, self._cancel_done)

    def _cancel_done(self, result: Any) -> None:
        ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
        self._toast("已请求取消" if ok else f"取消失败：{result}",
                    COLORS["success"] if ok else COLORS["danger"])
        QTimer.singleShot(100, self.refresh)

    def refresh(self) -> None:
        if self.loading:
            return
        cid, query = self.chat_selector.currentData() or '', self.history_search.text()
        after = self.after_date.date().toString('yyyy-MM-dd') if self.date_filter.isChecked() else ''
        before = self.before_date.date().toString('yyyy-MM-dd') if self.date_filter.isChecked() else ''
        if after and after > before: self._toast('开始日期不能晚于结束日期'); return
        self._submit(lambda: self.services.qq_snapshot(cid, query, after, before), self._apply)

    def _apply(self, data: dict[str, Any]) -> None:
        conv = data.get("conversation") or {}
        self.chat_selector.blockSignals(True); self.chat_selector.clear()
        for chat in data.get('conversations', []):
            chat_id = chat.get('chat_id', '')
            self.chat_selector.addItem(('主人私聊 · ' if chat_id.startswith('private:') else '群聊 · ') + chat_id.split(':')[-1], chat['id'])
            if chat['id'] == conv.get('id'): self.chat_selector.setCurrentIndex(self.chat_selector.count()-1)
        if not self.chat_selector.count(): self.chat_selector.addItem('主人私聊 · 暂无记录', '')
        self.chat_selector.blockSignals(False)
        loop = data.get("loop") or {}
        bot = (loop.get("bot") or {}).get("id")
        self.qq_line.setText(f"QQ {bot or '—'} · 主人专属")
        online = bool(data.get("runtime", {}).get("state") == "running" and data.get("fishqq") and loop)
        self.online_pill.set_text("在线" if online else "离线", "good" if online else "bad")
        for key, state in (
            ("runtime", (data.get("runtime") or {}).get("state")),
            ("fishqq", "ready" if data.get("fishqq") else "unavailable"),
            ("napcat", "connected" if data.get("napcat") else "disconnected"),
            ("loop", "running" if loop else "stopped"),
        ):
            self.status_pills[key].set_text(status_cn(state), state_pill(state))
        _set(self.info["conversation"], str(conv.get("id") or "—"))
        _set(self.info["session"], str(conv.get("runtime_session_id") or "—"))
        _set(self.info["port"], "3101")
        _set(self.info["endpoint"], "ws://127.0.0.1:3101/ws")

        counts = data.get("message_counts") or {}
        for key, label in self.count_labels.items():
            label.setText(str(counts.get(key, 0)))

        fingerprint = json.dumps(data.get("messages", []), ensure_ascii=False, sort_keys=True)
        if fingerprint == getattr(self, "_message_fingerprint", None):
            return
        self._message_fingerprint = fingerprint
        bar = self.scroll.verticalScrollBar()
        follow = bar.maximum() - bar.value() < 50
        previous_scroll = bar.value()
        while self.bubble_layout.count() > 1:
            item = self.bubble_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        messages = data.get("messages", [])
        if not messages:
            placeholder = _label("还没有对话。给大鱼发一条 QQ 私聊。", "Muted")
            placeholder.setAlignment(Qt.AlignCenter)
            self.bubble_layout.insertWidget(0, placeholder)
        for message in messages:
            state = "bad" if message.get("status") in ("failed", "cancelled") else "neutral"
            meta = f"{message.get('ts') or ''} · {status_cn(message.get('status'))}"
            bubble = MessageBubble(str(message.get("text") or ""),
                                   str(message.get("direction") or ""), meta, state)
            if message.get('direction') == 'outbound': bubble.set_markdown(str(message.get('text') or ''))
            from pathlib import Path
            try: segments = json.loads(message.get('segments') or '[]')
            except ValueError: segments = []
            for segment in segments:
                candidate = segment.get('preview_path') or segment.get('local_path') or segment.get('path')
                if segment.get('asset_id'):
                    from emoji_store import EmojiStore
                    candidate = next((r['path'] for r in EmojiStore(self.services.root).list() if r['id'] == segment['asset_id']), '')
                if candidate and Path(candidate).resolve().is_relative_to((project_path(self.services.root, 'data/qq')).resolve()):
                    pixmap = QPixmap(str(candidate))
                    if not pixmap.isNull():
                        preview = QLabel(); preview.setPixmap(pixmap.scaled(280, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)); bubble.layout().insertWidget(0, preview)
                if segment.get('type') in ('audio', 'video'):
                    bubble.layout().addWidget(_label({'audio': '语音附件', 'video': '视频附件'}[segment['type']] + (' · 未能读取' if segment.get('error') else ''), 'Caption'))
                if segment.get('error'): bubble.layout().addWidget(_label(str(segment['error']), 'Caption'))
            if message.get('error'): bubble.layout().addWidget(_label(str(message['error']), 'Caption'))
            local = QToolButton(); local.setText('带到本地对话'); local.setToolTip('在本地继续处理，不会向 QQ 重发消息。')
            local.clicked.connect(lambda _=False, m=dict(message): self._local_draft(m)); bubble.layout().addWidget(local)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            if message.get("direction") == "outbound":
                row.addStretch(1)
                row.addWidget(bubble)
            else:
                row.addWidget(bubble)
                row.addStretch(1)
            host = QWidget()
            host.setLayout(row)
            self.bubble_layout.insertWidget(self.bubble_layout.count() - 1, host)
        QTimer.singleShot(50, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum() if follow else previous_scroll))
        self._toast("已刷新")

    def _local_draft(self, message):
        if self.loading: return
        def work():
            from pathlib import Path
            from multimodal import MediaService
            import uuid, shutil
            segments = json.loads(message.get('segments') or '[]'); files = []; errors = []
            media = MediaService(self.services.root, 'qq')
            for segment in segments:
                kind = segment.get('type')
                if kind not in ('image', 'video', 'audio'): continue
                path = Path(segment.get('local_path') or segment.get('path') or '')
                if path.is_file() and path.resolve().is_relative_to((project_path(self.services.root, 'data/qq')).resolve()):
                    allowed = {'image': ('.jpg', '.jpeg', '.png', '.gif', '.webp'), 'video': ('.mp4', '.mkv', '.webm', '.mov'), 'audio': ('.wav', '.mp3', '.ogg', '.m4a')}
                    if path.suffix.lower() not in allowed[kind]:
                        directory = project_path(self.services.root, 'data/qq/retry', uuid.uuid4().hex); directory.mkdir(parents=True)
                        target = directory / ('attachment' + {'image': '.jpg', 'video': '.mp4', 'audio': '.ogg'}[kind])
                        if kind == 'image':
                            from PIL import Image
                            with Image.open(path) as image: image.convert('RGB').save(target)
                        else: shutil.copy2(path, target)
                        path = target
                    files.append(str(path)); continue
                if segment.get('url'):
                    directory = project_path(self.services.root, 'data/qq/retry', uuid.uuid4().hex); directory.mkdir(parents=True)
                    try:
                        downloaded = media._download(segment['url'], directory)
                        target = directory / ('attachment' + {'image': '.jpg', 'video': '.mp4', 'audio': '.ogg'}[kind])
                        if kind == 'image':
                            from PIL import Image
                            with Image.open(downloaded) as image: image.convert('RGB').save(target)
                        else: shutil.copy2(downloaded, target)
                        files.append(str(target))
                    except Exception: errors.append('原附件已过期或无法重新获取，请重新添加。')
            return {'text': message.get('text', '') + ('\n' + '\n'.join(errors) if errors else ''), 'files': files}
        self._submit(work, self.draftRequested.emit)






class EventsPage(BasePage):
    def __init__(self, services: FishServices):
        super().__init__(services)
        self.page_title = "事件"
        self.page_subtitle = "运行时、QQ 与研究事件"
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE["page"], SPACE["page"], SPACE["page"], SPACE["page"])
        root.setSpacing(SPACE["section"])

        head = self._header("事件", "运行时、QQ 与研究事件", root)
        self.filter = QComboBox()
        self.filter.addItems(["全部", "Runtime", "QQ", "Research", "错误"])
        head.addWidget(self.filter)
        self.refresh_btn = _button("刷新")
        head.addWidget(self.refresh_btn)
        root.addWidget(self.toast)

        card = Card("事件流")
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["时间", "类别", "级别", "摘要"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setColumnWidth(0, 80)
        self.tree.setColumnWidth(1, 90)
        self.tree.setColumnWidth(2, 70)
        card.body.addWidget(self.tree)
        root.addWidget(card, 1)

        self._events: list[dict[str, Any]] = []
        self.filter.currentTextChanged.connect(self._render)
        self.refresh_btn.clicked.connect(self.refresh)

    def refresh(self) -> None:
        if self.loading:
            return
        self._submit(self.services.events_snapshot, self._apply)

    @staticmethod
    def _classify(kind: str, detail: str) -> tuple[str, str]:
        text = f"{kind} {detail}".lower()
        failed_count = 0
        if "failed=" in text:
            try:
                tail = text.split("failed=", 1)[1].split()[0]
                failed_count = int(tail)
            except Exception:
                failed_count = 0
        if failed_count > 0 or "error" in text or "cancelled" in text:
            severity = "错误"
        elif "partial" in text or "queued" in text:
            severity = "警告"
        else:
            severity = "信息"
        if kind.startswith("qq_") or " qq " in f" {text} ":
            category = "QQ"
        elif "research" in text or "sync" in text:
            category = "Research"
        else:
            category = "Runtime"
        return category, severity

    def _apply(self, data: dict[str, Any]) -> None:
        self._events = data.get("events", [])
        self._render()
        self._toast(f"{len(self._events)} 条事件")

    def _render(self) -> None:
        wanted = self.filter.currentText()
        self.tree.clear()
        for row in self._events:
            kind = str(row.get("kind") or "")
            detail = str(row.get("detail") or "")
            category, severity = self._classify(kind, detail)
            if wanted == "错误" and severity != "错误":
                continue
            if wanted not in ("全部", "错误") and category != wanted:
                continue
            stamp = str(row.get("ts") or "")
            short_time = stamp[11:19] if len(stamp) >= 19 else stamp
            item = QTreeWidgetItem([short_time, category, severity, detail[:220]])
            color = {"错误": COLORS["danger"], "警告": COLORS["warning"],
                     "信息": COLORS["secondary"]}.get(severity, COLORS["secondary"])
            item.setForeground(1, QBrush(QColor(COLORS["primary"] if category == "QQ" else COLORS["secondary"])))
            item.setForeground(2, QBrush(QColor(color)))
            item.addChild(QTreeWidgetItem([short_time, "详情", "", detail]))
            self.tree.addTopLevelItem(item)
