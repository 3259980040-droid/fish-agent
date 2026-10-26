"""Everyday chat: photos are regular message attachments, no recognition step.

Search §2.1/§2.8: matched text is primary, time secondary, conversation title
tertiary. Keep the existing type/colors/spacing and the one primary Send action;
use selection plus a border to identify the focused message (§5.7).
"""
from __future__ import annotations
from locations import project_path

import uuid
import json
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize, QTimer, QDate
from PySide6.QtGui import QIcon, QPixmap, QTextDocument
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QDateEdit, QToolButton, QLayout, QLineEdit,
    QListWidgetItem, QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget, QDialog)

from .pages import BasePage, _button, _label
from .widgets import Card, MessageBubble


class Composer(QPlainTextEdit):
    filesAdded = Signal(list)
    submitted = Signal()

    def __init__(self, directory, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.setAcceptDrops(True)

    def insertFromMimeData(self, source):
        if source.hasUrls():
            paths = [u.toLocalFile() for u in source.urls() if u.isLocalFile()]
            if paths:
                self.filesAdded.emit(paths)
                return
        if source.hasImage():
            directory = self.directory()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (uuid.uuid4().hex + ".png")
            pixmap = QPixmap.fromImage(source.imageData())
            if pixmap.save(str(path)):
                self.filesAdded.emit([str(path)])
            return
        super().insertFromMimeData(source)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        self.insertFromMimeData(event.mimeData())
        event.acceptProposedAction()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() == Qt.ControlModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class ChatPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        self.role = "qq"
        self.cid = ""
        self.attachments = []
        self._drafts = {}
        self._fingerprint = None
        self.history_limit = 100
        self.focus_id = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 24)
        # Primary: conversation and Send. Secondary: workspace/persona settings.
        # Reuse theme.tokens spacing, existing Card and button hierarchy.
        header = self._header("对话", "用 DeepSeek 处理本地文件，或在独立窗口试聊人格。", root)
        self.roles = QComboBox()
        self.roles.addItem("大肥鱼 · QQ 角色", "qq")
        self.roles.addItem("研究员 · 研究角色", "research")
        header.addWidget(self.roles)
        self.personas = QComboBox(); self.personas.setMaximumWidth(290); self.personas.setToolTip('新建窗口所绑定的人格版本；已有窗口保持原版本')
        header.addWidget(self.personas)
        settings = QHBoxLayout()
        self.chat_mode = QComboBox(); self.chat_mode.addItem('本地工作 · 文件与终端', 'work'); self.chat_mode.addItem('人格聊天 · 独立窗口', 'chat')
        settings.addWidget(self.chat_mode)
        self.workspace = QLineEdit(str(self.services.root)); self.workspace.setPlaceholderText('新建本地工作对话的目录'); settings.addWidget(self.workspace, 1)
        self.workspace_btn = _button('选择目录'); settings.addWidget(self.workspace_btn)
        self.persona_reload = _button('重载当前人格'); settings.addWidget(self.persona_reload)
        root.addLayout(settings)
        self.workspace_btn.clicked.connect(self._workspace)
        self.chat_mode.currentIndexChanged.connect(lambda: [w.setEnabled(self.chat_mode.currentData() == 'work') for w in (self.workspace, self.workspace_btn)])
        self.persona_reload.clicked.connect(self._reload_persona)
        permissions=QHBoxLayout();permissions.addWidget(_label('本地工具权限','Caption'))
        self.tool_permission=QComboBox();self.tool_permission.addItem('完整本机权限 · 文件与命令','danger-full-access');self.tool_permission.addItem('只写工作目录 · 扩展操作需授权','workspace-write');permissions.addWidget(self.tool_permission)
        self.permission_apply=_button('应用到当前工作对话');permissions.addWidget(self.permission_apply)
        self.native_panel=_button('工具与授权面板');permissions.addWidget(self.native_panel);permissions.addStretch();root.addLayout(permissions)
        self.permission_apply.clicked.connect(self._permission);self.native_panel.clicked.connect(self._native_panel)
        self.workspace_btn.setText('新对话目录');self.workspace.setToolTip('此目录用于新建工作对话；当前对话的目录显示在聊天上方。')
        root.addWidget(self.toast)
        body = QHBoxLayout()
        history = Card("历史对话")
        self.search = QPlainTextEdit()
        self.search.setPlaceholderText("搜索对话内容")
        self.search.setFixedHeight(48)
        history.body.addWidget(self.search)
        self.period = QComboBox(); self.period.addItems(['全部时间', '最近 7 天', '最近 30 天', '指定日期'])
        self.date = QDateEdit(QDate.currentDate()); self.date.setCalendarPopup(True); self.date.setDisplayFormat('yyyy-MM-dd'); self.date.hide()
        history.body.addWidget(self.period); history.body.addWidget(self.date)
        self.conversations = QListWidget()
        self.conversations.setObjectName("ConversationList")
        self.conversations.setWordWrap(True)
        self.conversations.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        history.body.addWidget(self.conversations, 1)
        self.older_btn = _button('加载更早消息'); history.body.addWidget(self.older_btn)
        self.new_btn = _button("新建对话")
        history.body.addWidget(self.new_btn)
        history.setFixedWidth(235)
        body.addWidget(history)
        chat = Card("")
        context_bar = QHBoxLayout()
        self.context_label = _label('', 'Caption'); self.context_label.setWordWrap(True); context_bar.addWidget(self.context_label, 1)
        self.inspect_btn = _button('本轮输入检查'); context_bar.addWidget(self.inspect_btn)
        chat.body.addLayout(context_bar)
        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.messages = QWidget()
        self.messages_layout = QVBoxLayout(self.messages)
        self.messages_layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.messages_layout.addStretch()
        self.scroll.setWidget(self.messages)
        chat.body.addWidget(self.scroll, 1)
        self.files = QListWidget()
        self.files.setIconSize(QSize(54, 54))
        self.files.setMaximumHeight(86)
        self.files.hide()
        chat.body.addWidget(self.files)
        self.editor = Composer(lambda: project_path(self.services.root, "data", self.role, "pasted"))
        self.editor.setPlaceholderText("发送消息，或粘贴、拖入照片…  Ctrl+Enter 发送")
        self.editor.setFixedHeight(100)
        chat.body.addWidget(self.editor)
        controls = QHBoxLayout()
        self.attach_btn = _button("添加照片 / 文件")
        self.remove_btn = _button("移除所选附件")
        self.remove_btn.hide()
        self.cancel_btn = _button("停止回复")
        self.cancel_btn.setEnabled(False)
        self.send_btn = _button("发送", primary=True)
        for button in (self.attach_btn, self.remove_btn): controls.addWidget(button)
        controls.addStretch()
        controls.addWidget(self.cancel_btn)
        controls.addWidget(self.send_btn)
        chat.body.addLayout(controls)
        body.addWidget(chat, 1)
        root.addLayout(body, 1)
        self.roles.currentIndexChanged.connect(self._role)
        self.new_btn.clicked.connect(self._new)
        self.search.textChanged.connect(self._filter)
        self.period.currentIndexChanged.connect(self._period); self.date.dateChanged.connect(self.refresh)
        self.older_btn.clicked.connect(self._older)
        self.conversations.currentItemChanged.connect(self._select)
        self.editor.filesAdded.connect(self.add_files)
        self.editor.submitted.connect(self._send)
        self.attach_btn.clicked.connect(self._browse)
        self.remove_btn.clicked.connect(self._remove)
        self.send_btn.clicked.connect(self._send)
        self.cancel_btn.clicked.connect(self._cancel)
        self.inspect_btn.clicked.connect(self._inspect)
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(600)
        self.live_timer.timeout.connect(self._poll_turn)
        self.live_timer.start()

    def _role(self):
        self._remember_draft()
        self.role = self.roles.currentData()
        self.cid = ""
        self.focus_id = 0
        self._restore_draft()
        self._fingerprint = None
        self.refresh()

    def _remember_draft(self):
        self._drafts[(self.role, self.cid)] = (self.editor.toPlainText(), list(self.attachments))

    def _restore_draft(self):
        text, files = self._drafts.get((self.role, self.cid), ('', []))
        self.editor.setPlainText(text); self.attachments = list(files); self._files()

    def _browse(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "添加照片或文件", "", "媒体 (*.png *.jpg *.jpeg *.gif *.webp *.mp4 *.mkv *.mov *.webm *.wav *.mp3 *.m4a *.ogg)")
        self.add_files(paths)

    def add_files(self, paths):
        for value in paths:
            path = Path(value)
            if len(self.attachments) >= 3:
                self._toast("每条消息最多 3 个附件")
                break
            ext = path.suffix.lower()
            kind = "image" if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp") else "video" if ext in (".mp4", ".mkv", ".mov", ".webm") else "audio" if ext in (".wav", ".mp3", ".m4a", ".ogg") else ""
            if not kind or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                self._toast("请选择 20MB 以内的图片、视频或语音")
                continue
            self.attachments.append({"type": kind, "path": str(path.resolve())})
        self._files()

    def _files(self):
        self.files.clear()
        for entry in self.attachments:
            item = QListWidgetItem(Path(entry["path"]).name)
            if entry["type"] == "image": item.setIcon(QIcon(entry["path"]))
            self.files.addItem(item)
        self.files.setVisible(bool(self.attachments))
        self.remove_btn.setVisible(bool(self.attachments))

    def _remove(self):
        index = self.files.currentRow()
        if index >= 0:
            self.attachments.pop(index)
            self._files()

    def _new(self):
        if self.loading: return
        self._remember_draft()
        try:
            self.cid = self.services.chat(self.role).new(revision=self.personas.currentData() or '',mode=self.chat_mode.currentData(),workspace=self.workspace.text(),permission=self.tool_permission.currentData())
        except Exception as exc:
            self._toast(str(exc)); return
        self._restore_draft()
        self.focus_id = 0
        self.search.blockSignals(True); self.search.clear(); self.search.blockSignals(False)
        self._fingerprint = None
        self.refresh()

    def _workspace(self):
        path = QFileDialog.getExistingDirectory(self, '选择本地工作目录', self.workspace.text())
        if path: self.workspace.setText(path)

    def _permission(self):
        if self.loading:return
        try:
            self.services.chat(self.role).set_work_permission(self.cid,self.tool_permission.currentData());self._toast('已保存工具权限，下次发送任务生效。');self.refresh()
        except Exception as exc:self._toast(str(exc))

    def _native_panel(self):
        role=self.role
        def url():
            from runtime_adapter import DSHRuntimeAdapter
            return DSHRuntimeAdapter(self.services.root,role,self.services.runtime).connect().launch_url
        def opened(value):
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(value))
        self._submit(url,opened)

    def _reload_persona(self):
        if self.loading: return
        try:
            result = self.services.chat(self.role).reload_persona(self.cid, self.personas.currentData() or '')
            self._toast(result['note']); self._fingerprint = None; self.refresh()
        except Exception as exc: self._toast(str(exc))

    def _period(self):
        self.focus_id = 0
        self.date.setVisible(self.period.currentIndex() == 3); self.refresh()

    def _filter(self):
        self.focus_id = 0; self._fingerprint = None
        if self.search.toPlainText().strip(): self.cid = ''
        self.refresh()

    def _older(self):
        self.history_limit = min(2000, self.history_limit + 100); self.refresh()

    def _snapshot(self):
        index = self.period.currentIndex(); after = before = ''
        if index in (1, 2): after = QDate.currentDate().addDays(-7 if index == 1 else -30).toString('yyyy-MM-dd')
        if index == 3: after = before = self.date.date().toString('yyyy-MM-dd')
        return self.services.chat(self.role).snapshot(self.cid, self.search.toPlainText(), after, before, self.history_limit, around=self.focus_id)

    def _select(self, item, _previous):
        if item is None or self.loading: return
        self._remember_draft()
        selection = item.data(Qt.UserRole)
        self.cid = selection['conversation_id'] if isinstance(selection, dict) else selection
        self.focus_id = selection.get('id', 0) if isinstance(selection, dict) else 0
        self._restore_draft()
        self._fingerprint = None
        self._render(self._snapshot())

    def refresh(self):
        if self.loading: return
        from social_context import SocialContext
        rows = SocialContext(self.services.root).personas(self.role)
        selected = self.personas.currentData()
        self.personas.blockSignals(True); self.personas.clear()
        if self.role == 'qq':
            if not rows: rows = [SocialContext(self.services.root).ensure_default('qq')]
            for p in rows: self.personas.addItem(p['name'] + ' · ' + p['id'][:8], p['id'])
        else: self.personas.addItem('研究角色 · 日常区', '')
        self.personas.setCurrentIndex(max(0, self.personas.findData(selected))); self.personas.blockSignals(False)
        self._render(self._snapshot())

    def _render(self, data):
        self.older_btn.setEnabled(bool(data.get('has_more')) and self.history_limit < 2000 and not self.loading)
        self.older_btn.setText('加载更多上下文' if data.get('focus_id') else '加载更早消息')
        if self.cid != data['conversation_id']:
            self._remember_draft(); self.cid = data['conversation_id']; self._restore_draft()
        space = data.get('space')
        settings = data.get('settings', {})
        permission='完整本机权限' if settings.get('permission')=='danger-full-access' else '工作目录权限'
        self.context_label.setText(('本地工作 · DSH 文件、编辑和终端工具 · '+permission+'\n工作目录：' + settings.get('workspace', '') + ' · 使用已配置的 DeepSeek API') if settings.get('mode') == 'work' else ('独立测试 · ' + space['persona']['name'] + ' · 版本 ' + space['revision'][:8] + '\n本窗口的历史、记忆和附件独立。') if space else
                                  '日常区 · 原有会话沿用旧范围。新建 QQ 测试窗口后使用独立记忆。')
        activity=data.get('activity',{})
        if activity.get('state')=='waiting_approval':self.context_label.setText(self.context_label.text()+'\n等待工具授权：'+activity.get('tool','')+'；打开“工具与授权面板”处理。')
        elif activity.get('state')=='tool':self.context_label.setText(self.context_label.text()+'\n正在使用工具：'+activity.get('tool',''))
        self.focus_id = data.get('focus_id', 0)
        self.conversations.blockSignals(True)
        self.conversations.clear()
        hits = data.get('search_hits', [])
        for row in hits or data["conversations"]:
            item = QListWidgetItem((row['text'][:90] + '\n' + row['created_at']) if hits else row["title"])
            item.setToolTip(row['text'] if hits else row['title'])
            item.setData(Qt.UserRole, {'conversation_id':row['conversation_id'],'id':row['id']} if hits else row["id"])
            self.conversations.addItem(item)
            if (hits and row['id']==self.focus_id) or (not hits and row["id"] == self.cid): self.conversations.setCurrentItem(item)
        self.conversations.blockSignals(False)
        fingerprint = (self.cid, self.focus_id, tuple((r["id"], r["status"], r["text"]) for r in data["messages"]))
        if fingerprint == self._fingerprint: return
        bar = self.scroll.verticalScrollBar()
        bottom = bar.maximum() - bar.value() < 40 or self._fingerprint is None
        position = bar.value()
        self._fingerprint = fingerprint
        while self.messages_layout.count():
            item = self.messages_layout.takeAt(0)
            if item.widget(): item.widget().hide(); item.widget().deleteLater()
        if not data["messages"]:
            self.messages_layout.addWidget(_label("发一条消息，或直接发照片开始对话。", "Muted"))
        focused = None
        for row in data["messages"]:
            meta = row["created_at"] + " · " + {"done": "完成", "processing": "正在回复", "failed": "未完成", "cancelled": "已取消", "blocked": "已拦截"}.get(row["status"], row["status"])
            bubble = MessageBubble(row["text"], row["direction"], meta, "bad" if row["status"] in ("failed", "blocked") else "neutral")
            if row['id'] == self.focus_id:
                bubble.setProperty('searchMatch', True); focused = bubble
            if row["direction"] == "outbound":
                bubble.set_markdown(row['text'])
            for attachment in row["segments"]:
                if attachment["type"] == "image":
                    preview = QLabel()
                    preview.setPixmap(QPixmap(attachment["path"]).scaled(320, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    bubble.layout().insertWidget(0, preview)
            if row["error"]:
                detail = _label(row["error"], "Caption")
                detail.setTextFormat(Qt.PlainText)
                detail.setWordWrap(True)
                bubble.layout().addWidget(detail)
            if row['direction'] == 'inbound' and row['status'] in ('failed', 'blocked', 'cancelled'):
                retry = QToolButton(); retry.setText('重试这条消息')
                retry.clicked.connect(lambda _=False, record=dict(row): self._retry(record)); bubble.layout().addWidget(retry)
            self.messages_layout.addWidget(bubble, alignment=Qt.AlignRight if row["direction"] == "inbound" else Qt.AlignLeft)
        self.messages_layout.addStretch()
        if focused is not None:
            QTimer.singleShot(30, lambda: self.scroll.ensureWidgetVisible(focused, 0, 24))
        else:
            QTimer.singleShot(30, lambda: bar.setValue(bar.maximum() if bottom else position))

    def _poll_turn(self):
        if self.loading:
            self._render(self.services.chat(self.role).snapshot(self.cid))

    def _retry(self, row):
        if self.loading: return
        self.editor.setPlainText(row['text']); self.attachments = list(row['segments']); self._files(); self._send()

    def closeEvent(self, event):
        self.live_timer.stop()
        super().closeEvent(event)

    def _busy(self, busy):
        for widget in (self.roles, self.conversations, self.new_btn, self.send_btn, self.attach_btn, self.remove_btn): widget.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        self.editor.setReadOnly(busy)

    def _send(self):
        if self.loading: return
        text, attachments = self.editor.toPlainText(), list(self.attachments)
        if not text.strip() and not attachments: return
        chat = self.services.chat(self.role)
        if not self.cid:
            try:self.cid = chat.new(revision=self.personas.currentData() or '',mode=self.chat_mode.currentData(),workspace=self.workspace.text(),permission=self.tool_permission.currentData())
            except Exception as exc:self._toast(str(exc));return
        cid = self.cid
        self.focus_id = 0
        self.search.blockSignals(True); self.search.clear(); self.search.blockSignals(False)
        self._busy(True)
        self._toast("正在回复…")
        def result(data):
            self.editor.clear()
            self.attachments = []
            self._files()
            self._render(data)
            self._toast("回复完成")
        def error(message):
            self._render(chat.snapshot(cid))
            self._toast(message)
        self._submit(lambda: chat.send(cid, text, attachments), result, error, lambda: self._busy(False))

    def _cancel(self):
        self.runner.submit(self.services.chat(self.role).cancel, lambda _: self._toast("已请求停止"), lambda error: self._toast(error))

    def _inspect(self):
        from request_inspector import RequestInspector, redact
        from social_context import SocialContext
        chat = self.services.chat(self.role)
        context = chat.conversation_context(self.cid) if self.cid else None
        with closing(chat.connect()) as db:
            row = db.execute('SELECT session_id FROM conversations WHERE id=?', (self.cid,)).fetchone()
        session = row['session_id'] if row else ''
        data = RequestInspector(self.services.root).latest(self.role, session or '')
        if context:
            data['static_preview'] = {'label': '静态快照，不表示已发送给 API', 'revision': context['revision'],
                'text': redact(SocialContext(self.services.root).prompt(context['id']))}
        dialog = QDialog(self); dialog.setWindowTitle('本轮输入检查 · 脱敏本地记录'); dialog.resize(920, 680)
        layout = QVBoxLayout(dialog); editor = QPlainTextEdit(); editor.setReadOnly(True)
        editor.setPlainText(json.dumps(data, ensure_ascii=False, indent=2)); layout.addWidget(editor)
        close = _button('关闭'); close.clicked.connect(dialog.accept); layout.addWidget(close)
        dialog.exec()
