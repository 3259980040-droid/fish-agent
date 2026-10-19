"""Phase 4C 六页：View + Control Surface，不包含业务 SQL。"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt, QTimer, QDate, Signal
from PySide6.QtGui import QBrush, QColor, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QTabWidget, QTableWidget, QTableWidgetItem,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QLineEdit, QDateEdit, QCheckBox,
)

from .services import FishServices
from .theme import COLORS, SPACE
from .widgets import Avatar, Card, Divider, StatBlock, StatusPill, MessageBubble, icon
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


class OverviewPage(BasePage):
    def __init__(self, services: FishServices):
        super().__init__(services)
        self.page_title = "概览"
        self.page_subtitle = "大鱼与研究的日常状态"
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE["page"], SPACE["page"], SPACE["page"], SPACE["page"])
        root.setSpacing(SPACE["section"])

        head = self._header("概览", "大鱼与研究的日常状态", root)
        self.start_btn = _button("启动 Fish", primary=True)
        self.stop_btn = _button("停止 Fish", danger=True)
        self.refresh_btn = _button("刷新")
        head.addWidget(self.start_btn)
        head.addWidget(self.stop_btn)
        head.addWidget(self.refresh_btn)
        root.addWidget(self.toast)

        hero = QGridLayout()
        hero.setHorizontalSpacing(SPACE["card"])
        hero.setVerticalSpacing(SPACE["card"])

        fish_card = Card("大鱼 AGENT")
        fish_top = QHBoxLayout()
        fish_top.setSpacing(12)
        fish_top.addWidget(Avatar(54))
        fish_name = QVBoxLayout()
        fish_name.setSpacing(2)
        fish_name.addWidget(_label("大鱼", "SectionTitle"))
        fish_name.addWidget(_label("QQ 私聊 · 主人专属", "Muted"))
        fish_top.addLayout(fish_name)
        fish_top.addStretch(1)
        self.fish_pill = StatusPill("离线", "neutral")
        fish_top.addWidget(self.fish_pill)
        fish_card.body.addLayout(fish_top)
        self.qq_values: dict[str, QLabel] = {}
        for key, title in (("channel", "QQ 通道"), ("conversation", "当前会话"), ("reply", "最近回复")):
            row = QHBoxLayout()
            row.addWidget(_label(title, "Caption"))
            row.addStretch(1)
            value = _label("—")
            value.setAlignment(Qt.AlignRight)
            value.setWordWrap(True)
            self.qq_values[key] = value
            row.addWidget(value)
            fish_card.body.addLayout(row)
        hero.addWidget(fish_card, 0, 0, 1, 2)

        research_card = Card("研究 AGENT")
        research_top = QHBoxLayout()
        research_top.addWidget(Avatar(34))
        research_top.addWidget(_label("收藏夹研究", "Value"))
        research_top.addStretch(1)
        self.research_pill = StatusPill("—", "neutral")
        research_top.addWidget(self.research_pill)
        research_card.body.addLayout(research_top)
        self.research_values: dict[str, QLabel] = {}
        for key, title in (("sources", "视频源"), ("processed", "已处理"),
                           ("failed", "失败"), ("sync", "最近同步")):
            row = QHBoxLayout()
            row.addWidget(_label(title, "Caption"))
            row.addStretch(1)
            value = _label("—")
            self.research_values[key] = value
            row.addWidget(value)
            research_card.body.addLayout(row)
        hero.addWidget(research_card, 0, 2)

        health_card = Card("系统健康")
        self.health_pills: dict[str, StatusPill] = {}
        for key, title in (("runtime", "Runtime"), ("fishqq", "fish-qq"),
                           ("napcat", "NapCat"), ("loop", "Agent Loop")):
            row = QHBoxLayout()
            row.addWidget(_label(title, "Value"))
            row.addStretch(1)
            pill = StatusPill("—", "neutral")
            self.health_pills[key] = pill
            row.addWidget(pill)
            health_card.body.addLayout(row)
        hero.addWidget(health_card, 1, 0, 1, 3)
        hero.setColumnStretch(0, 1)
        hero.setColumnStretch(1, 1)
        hero.setColumnStretch(2, 1)
        root.addLayout(hero)

        activity_card = Card("最近活动")
        activity_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.activity = _label("尚无记录", "Muted")
        self.activity.setWordWrap(True)
        activity_card.body.addWidget(self.activity)
        activity_card.body.addStretch(1)
        activity_footer = _label("状态来自本机 Core · 关闭窗口停止 Fish-owned，NapCat 保留", "Caption")
        activity_card.body.addWidget(activity_footer)
        root.addWidget(activity_card, 1)

        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.refresh_btn.clicked.connect(self.refresh)

    def _control(self, fn, label: str) -> None:
        self._toast(f"{label}…", COLORS["warning"])
        self.start_btn.setDisabled(True)
        self.stop_btn.setDisabled(True)
        self._submit(fn, self._control_result)

    def _control_result(self, result: Any) -> None:
        ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
        reason = result.get("reason") if isinstance(result, dict) else ""
        self._toast("完成" if ok else f"失败：{reason or 'unknown'}",
                    COLORS["success"] if ok else COLORS["danger"])
        self.start_btn.setDisabled(False)
        self.stop_btn.setDisabled(False)
        QTimer.singleShot(100, self.refresh)

    def _start(self) -> None:
        self._control(self.services.start_fish, "启动 Fish")

    def _stop(self) -> None:
        self._control(self.services.stop_fish, "停止 Fish")

    def refresh(self) -> None:
        if self.loading:
            return
        self._submit(self.services.overview, self._apply)

    def _apply(self, data: dict[str, Any]) -> None:
        runtime = data["qq_runtime"]
        loop = data.get("loop")
        online = bool(runtime.get("state") == "running" and data["fishqq"] and loop)
        self.fish_pill.set_text("在线" if online else "离线", "good" if online else "bad")
        _set(self.qq_values["channel"], "已连接" if data["fishqq"] else "不可用",
             COLORS["success"] if data["fishqq"] else COLORS["danger"])
        conv = data.get("conversation") or {}
        _set(self.qq_values["conversation"], str(conv.get("id") or "未建立"))
        last = data.get("last_message") or {}
        reply = (last.get("text") or "尚无回复") if last.get("direction") == "outbound" else "尚无回复"
        _set(self.qq_values["reply"], str(reply)[:80])

        counts = data.get("research_counts") or {}
        sources = counts.get("sources", 0)
        failed = 0
        processed = 0
        for status, number in data.get("source_counts", []):
            if status == "failed":
                failed += number
            elif status == "done":
                processed += number
        self.research_pill.set_text(
            status_cn((data.get("last_run") or {}).get("status", "—")),
            state_pill((data.get("last_run") or {}).get("status")),
        )
        _set(self.research_values["sources"], str(sources))
        _set(self.research_values["processed"], str(processed))
        _set(self.research_values["failed"], str(failed))
        _set(self.research_values["sync"], str((data.get("last_run") or {}).get("started_at") or "—"))

        self.health_pills["runtime"].set_text(status_cn(runtime.get("state")),
                                              state_pill(runtime.get("state")))
        self.health_pills["fishqq"].set_text("就绪" if data["fishqq"] else "不可用",
                                             "good" if data["fishqq"] else "bad")
        self.health_pills["napcat"].set_text("已连接" if data["napcat"] else "未连接",
                                             "good" if data["napcat"] else "bad")
        self.health_pills["loop"].set_text("运行中" if loop else "已停止",
                                           "good" if loop else "neutral")
        if last:
            lines = []
            for message in (data.get("recent_messages") or [])[-5:]:
                text = (message.get("text") or "").replace("\n", " ")
                lines.append(
                    f"{direction_cn(message.get('direction'))} · {status_cn(message.get('status'))} · "
                    f"{message.get('ts') or ''}\n{text[:140]}"
                )
            self.activity.setText("\n\n".join(lines) if lines else
                                  f"{direction_cn(last.get('direction'))} · {status_cn(last.get('status'))} · "
                                  f"{last.get('ts')}\n{(last.get('text') or '')[:240]}")
        else:
            self.activity.setText("尚无 QQ 消息")
        self._toast("已刷新")


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
        controls = QHBoxLayout()
        self.start_btn = _button("启动运行时")
        self.stop_btn = _button("停止运行时")
        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)
        status.body.addLayout(controls)
        self.cancel_btn = _button("取消当前生成", danger=True)
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
        self.start_btn.clicked.connect(lambda: self._control(self.services.start_runtime, "启动运行时"))
        self.stop_btn.clicked.connect(lambda: self._control(self.services.stop_runtime, "停止运行时"))
        self.cancel_btn.clicked.connect(self._cancel)

    def _control(self, fn, text: str) -> None:
        self._toast(f"{text}…", COLORS["warning"])
        self._submit(fn, lambda result: self._control_done(result, text))

    def _control_done(self, result: Any, text: str) -> None:
        ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
        self._toast(f"{text} {'完成' if ok else '失败'}", COLORS["success"] if ok else COLORS["danger"])
        QTimer.singleShot(100, self.refresh)

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
                if candidate and Path(candidate).resolve().is_relative_to((self.services.root / 'data/qq').resolve()):
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
                if path.is_file() and path.resolve().is_relative_to((self.services.root / 'data/qq').resolve()):
                    allowed = {'image': ('.jpg', '.jpeg', '.png', '.gif', '.webp'), 'video': ('.mp4', '.mkv', '.webm', '.mov'), 'audio': ('.wav', '.mp3', '.ogg', '.m4a')}
                    if path.suffix.lower() not in allowed[kind]:
                        directory = self.services.root / 'data/qq/retry' / uuid.uuid4().hex; directory.mkdir(parents=True)
                        target = directory / ('attachment' + {'image': '.jpg', 'video': '.mp4', 'audio': '.ogg'}[kind])
                        if kind == 'image':
                            from PIL import Image
                            with Image.open(path) as image: image.convert('RGB').save(target)
                        else: shutil.copy2(path, target)
                        path = target
                    files.append(str(path)); continue
                if segment.get('url'):
                    directory = self.services.root / 'data/qq/retry' / uuid.uuid4().hex; directory.mkdir(parents=True)
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


class ResearchPage(BasePage):
    def __init__(self, services: FishServices):
        super().__init__(services)
        self.page_title = "研究"
        self.page_subtitle = "收藏夹与视频理解"
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE["page"], SPACE["page"], SPACE["page"], SPACE["page"])
        root.setSpacing(SPACE["section"])

        head = self._header("研究", "收藏夹与视频理解", root)
        self.sync_btn = _button("同步收藏夹", primary=True)
        self.refresh_btn = _button("刷新")
        head.addWidget(self.sync_btn)
        head.addWidget(self.refresh_btn)
        root.addWidget(self.toast)

        summary_card = Card("概览")
        summary = QHBoxLayout()
        self.fav_label = _label("—", "Value")
        fav_box = QVBoxLayout()
        fav_box.addWidget(_label("收藏夹", "Caption"))
        fav_box.addWidget(self.fav_label)
        summary.addLayout(fav_box)
        summary.addStretch(1)
        self.stats: dict[str, StatBlock] = {}
        for key, title in (("total", "视频总数"), ("processed", "已处理"),
                           ("partial", "部分完成"), ("failed", "失败"), ("sync", "最近同步")):
            block = StatBlock(title)
            self.stats[key] = block
            summary.addWidget(block)
        summary_card.body.addLayout(summary)
        root.addWidget(summary_card)

        sources = Card("视频列表")
        self.table = Table(["标题", "状态", "观察级别", "模态", "重试", "警告 / 错误"])
        self.table.setMinimumHeight(260)
        sources.body.addWidget(self.table)
        root.addWidget(sources, 1)

        self.advanced_btn = _tech_toggle("派生记忆 / 同步记录")
        root.addWidget(self.advanced_btn)
        self.advanced_panel = QWidget()
        adv = QHBoxLayout(self.advanced_panel)
        adv.setContentsMargins(0, 0, 0, 0)
        mem_card = Card("派生记忆")
        self.memory = Table(["来源", "摘要"])
        mem_card.body.addWidget(self.memory)
        adv.addWidget(mem_card, 2)
        run_card = Card("同步记录")
        self.runs = Table(["开始", "状态", "处理", "失败"])
        run_card.body.addWidget(self.runs)
        adv.addWidget(run_card, 1)
        self.advanced_panel.setVisible(False)
        self.advanced_btn.toggled.connect(self.advanced_panel.setVisible)
        root.addWidget(self.advanced_panel)

        self.refresh_btn.clicked.connect(self.refresh)
        self.sync_btn.clicked.connect(self._sync)

    def _sync(self) -> None:
        self._toast("调用现有研究流水线…", COLORS["warning"])
        self.sync_btn.setDisabled(True)
        self._submit(self.services.sync_research, self._sync_done,
                     on_done=lambda: self.sync_btn.setDisabled(False))

    def _sync_done(self, result: Any) -> None:
        ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
        status = ((result.get("result") or {}).get("status") if isinstance(result, dict) else "") or ""
        reason = result.get("reason") if isinstance(result, dict) else ""
        word = "同步完成" if status == "completed" else "同步部分完成" if ok else "同步失败"
        self._toast(f"{word}" if ok else f"{word}：{reason or result}",
                    COLORS["success"] if ok else COLORS["danger"])
        QTimer.singleShot(100, self.refresh)

    def refresh(self) -> None:
        if self.loading:
            return
        self._submit(self.services.research_snapshot, self._apply)

    def _apply(self, data: dict[str, Any]) -> None:
        fav = data.get("favorite") or {}
        self.fav_label.setText(f"{fav.get('title', '未选择收藏夹')}  ·  media_id={fav.get('media_id', '—')}")
        sources = data.get("sources", [])
        counts = {"total": len(sources), "processed": 0, "partial": 0, "failed": 0}
        for source in sources:
            status = source.get("status")
            if status == "done":
                counts["processed"] += 1
            elif status == "failed":
                counts["failed"] += 1
            elif status in ("partial", "metadata_only"):
                counts["partial"] += 1
        run = (data.get("runs") or [{}])[0]
        for key, block in self.stats.items():
            if key == "sync":
                block.value.setText(status_cn(run.get("status", "—")))
            else:
                block.value.setText(str(counts.get(key, 0)))
        rows = []
        for source in sources:
            rows.append([
                str(source.get("title") or source.get("bvid") or ""),
                status_cn(source.get("status")),
                status_cn(source.get("observation_level")),
                modalities_cn(source.get("processed_modalities")),
                str(source.get("retry_count") or 0),
                str(source.get("last_error") or source.get("warnings") or ""),
            ])
        self.table.fill(rows, [330, 90, 100, 180, 55, 220])
        self.memory.fill([[str(m.get("source_id") or ""), str(m.get("summary") or "")[:260]]
                          for m in data.get("memories", [])], [70, 520])
        self.runs.fill([[str(r.get("started_at") or ""), status_cn(r.get("status")),
                         str(r.get("processed") or 0), str(r.get("failed") or 0)]
                        for r in data.get("runs", [])], [160, 90, 60, 60])
        self._toast("已刷新")


class AgentsPage(BasePage):
    def __init__(self, services: FishServices):
        super().__init__(services)
        self.page_title = "Agent 配置"
        self.page_subtitle = "人格、模型与工作区"
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE["page"], SPACE["page"], SPACE["page"], SPACE["page"])
        root.setSpacing(SPACE["section"])

        head = self._header("Agent 配置", "人格、模型与工作区", root)
        self.refresh_btn = _button("刷新")
        head.addWidget(self.refresh_btn)
        root.addWidget(self.toast)

        columns = QHBoxLayout()
        columns.setSpacing(SPACE["card"])
        self.cards: dict[str, dict[str, QLabel]] = {}
        for agent_id, title in (("research", "研究 Agent"), ("qq", "QQ Agent")):
            card = Card(title)
            top = QHBoxLayout()
            top.addWidget(Avatar(34))
            top.addWidget(_label(title, "Value"))
            top.addStretch(1)
            state_pill = StatusPill("—", "neutral")
            top.addWidget(state_pill)
            card.body.addLayout(top)
            values: dict[str, QLabel] = {}
            for key, label_text in (
                ("provider", "提供方"), ("model", "模型"), ("key", "API key"),
                ("workspace", "工作区"), ("persona", "人格文件"),
            ):
                row = QHBoxLayout()
                row.addWidget(_label(label_text, "Caption"))
                row.addStretch(1)
                value = _label("—")
                value.setAlignment(Qt.AlignRight)
                value.setTextInteractionFlags(Qt.TextSelectableByMouse)
                values[key] = value
                row.addWidget(value)
                card.body.addLayout(row)
            values["runtime"] = state_pill
            advanced_btn = _tech_toggle("高级 / 技术详情")
            card.body.addWidget(advanced_btn)
            advanced = QWidget()
            adv_layout = QVBoxLayout(advanced)
            adv_layout.setContentsMargins(0, 4, 0, 0)
            adv_layout.setSpacing(4)
            projection = _label("—", "Muted")
            values["projection"] = projection
            adv_layout.addWidget(projection)
            button_row = QHBoxLayout()
            preview_btn = _button("人格预览")
            config_btn = _button("生效配置")
            button_row.addWidget(preview_btn)
            button_row.addWidget(config_btn)
            adv_layout.addLayout(button_row)
            advanced.setVisible(False)
            advanced_btn.toggled.connect(advanced.setVisible)
            preview_btn.clicked.connect(lambda _=False, aid=agent_id: self._preview(aid))
            config_btn.clicked.connect(lambda _=False, aid=agent_id: self._config(aid))
            card.body.addWidget(advanced)
            self.cards[agent_id] = values
            columns.addWidget(card, 1)
        root.addLayout(columns)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(220)
        self.output.setPlaceholderText("人格预览 / 生效配置（不含 secret）")
        root.addWidget(self.output)
        root.addStretch(1)
        self.refresh_btn.clicked.connect(self.refresh)

    def refresh(self) -> None:
        if self.loading:
            return
        self._submit(self.services.agents_snapshot, self._apply)

    def _apply(self, data: dict[str, Any]) -> None:
        for agent in data.get("agents", []):
            values = self.cards.get(agent["id"])
            if not values:
                continue
            key = agent.get("api_key") or {}
            _set(values["provider"], str(agent.get("provider")))
            _set(values["model"], str(agent.get("model")))
            _set(values["key"], "set" if key.get("set") else "missing",
                 COLORS["success"] if key.get("set") else COLORS["danger"])
            _set(values["workspace"], str(agent.get("workspace")))
            _set(values["persona"], str(agent.get("persona")))
            _set(values["projection"], f"投影 {agent.get('preview_chars')} 字符 / 约 {agent.get('preview_tokens')} token")
            values["runtime"].set_text(status_cn((agent.get("status") or {}).get("state")),
                                       state_pill((agent.get("status") or {}).get("state")))
        self._toast("已刷新")

    def _preview(self, agent_id: str) -> None:
        self._submit(lambda: self.services.persona_preview(agent_id), self._show_text)

    def _config(self, agent_id: str) -> None:
        self._submit(lambda: self.services.effective_config(agent_id), self._show_json)

    def _show_text(self, result: dict[str, Any]) -> None:
        self.output.setPlainText(result.get("text", ""))

    def _show_json(self, result: dict[str, Any]) -> None:
        self.output.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))


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


class SettingsPage(BasePage):
    def __init__(self, services: FishServices):
        super().__init__(services)
        self.page_title = "设置"
        self.page_subtitle = "配置与诊断"
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE["page"], SPACE["page"], SPACE["page"], SPACE["page"])
        root.setSpacing(SPACE["section"])

        head = self._header("设置", "配置与诊断", root)
        self.refresh_btn = _button("刷新")
        head.addWidget(self.refresh_btn)
        root.addWidget(self.toast)

        self.tabs = QTabWidget()
        self.general = Table(["项目", "值"])
        self.runtime = Table(["项目", "值"])
        self.qq = Table(["项目", "值"])
        self.research = Table(["项目", "值"])
        diagnostics = QWidget()
        diag_layout = QVBoxLayout(diagnostics)
        diag_layout.setContentsMargins(12, 12, 12, 12)
        self.diag = QPlainTextEdit()
        self.diag.setReadOnly(True)
        diag_layout.addWidget(self.diag)
        self.tabs.addTab(self.general, "常规")
        self.tabs.addTab(self.runtime, "运行时")
        self.tabs.addTab(self.qq, "QQ / NapCat")
        self.tabs.addTab(self.research, "数据位置")
        self.tabs.addTab(diagnostics, "诊断")
        root.addWidget(self.tabs, 1)
        self.refresh_btn.clicked.connect(self.refresh)

    def refresh(self) -> None:
        if self.loading:
            return
        self._submit(self.services.settings_snapshot, self._apply)

    def _apply(self, data: dict[str, Any]) -> None:
        self.general.fill([
            ["产品", "大鱼 Fish"],
            ["版本", "2026.10 · UI 与多模态更新"],
            ["项目根", str(self.services.root)],
            ["关闭行为", "停止 Fish-owned，保留 NapCat"],
            ["群回复", "off（正式默认）"],
            ["说明", "PySide6 桌面客户端；长期 Memory 尚未实现"],
        ], [120, 700])
        self.runtime.fill([
            ["DSH CLI", str(data.get("cli") or "")],
            ["Node.js", str(data.get("node") or "")],
            ["DSH 版本", str(data.get("version") or "")],
            ["Profile", str(data.get("profile") or "")],
            ["Homes", json.dumps(data.get("homes"), ensure_ascii=False)],
            ["Ports", json.dumps(data.get("ports"), ensure_ascii=False)],
        ], [120, 760])
        self.qq.fill([
            ["Owner", str(self.services.owner_id() or "<owner-qq>")],
            ["NapCat", str(data.get("napcat") or "")],
            ["fish-qq", str(data.get("fishqq") or "")],
            ["Token", json.dumps(data.get("tokens"), ensure_ascii=False)],
        ], [120, 760])
        self.research.fill([
            ["research.db", str((data.get("db") or {}).get("research") or "")],
            ["qq.db", str((data.get("db") or {}).get("qq") or "")],
            ["harness.db", str((data.get("db") or {}).get("harness") or "")],
        ], [120, 760])
        self.diag.setPlainText(json.dumps({
            "health": data.get("health"), "homes": data.get("homes"),
            "ports": data.get("ports"), "db": data.get("db"),
        }, ensure_ascii=False, indent=2))
        self._toast("只读诊断；token/API key 只显示 set/missing")
