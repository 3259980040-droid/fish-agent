"""Editable role configuration and multimodal workbench."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QPlainTextEdit, QProgressBar, QScrollArea,
    QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)
from .pages import BasePage, _button, _label
from .widgets import Card


def _input(placeholder="", password=False):
    widget = QLineEdit()
    widget.setPlaceholderText(placeholder)
    if password:
        widget.setEchoMode(QLineEdit.Password)
    return widget


class AgentsPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        root = QVBoxLayout(self); root.setContentsMargins(28, 26, 28, 24); root.setSpacing(16)
        self._header("Agent 配置", "你来定义性格与规则；每个角色独立保存模型与密钥。", root)
        root.addWidget(self.toast)
        row = QHBoxLayout()
        self.roles = QListWidget(); self.roles.setObjectName("RoleList"); self.roles.setFixedWidth(165)
        self.roles.addItems(["大肥鱼\nQQ 角色", "研究员\nB 站与视频"])
        row.addWidget(self.roles)
        card = Card("角色设置")
        form = QFormLayout(); form.setSpacing(12)
        self.name = _input("角色名称"); self.model = _input("DeepSeek 模型名称")
        self.key = _input("留空保留已有密钥", True)
        self.base_url = _input('https://api.deepseek.com')
        form.addRow("角色名称", self.name); form.addRow("DeepSeek 模型", self.model); form.addRow("API 密钥", self.key)
        form.addRow('API 地址', self.base_url)
        card.body.addLayout(form)
        self.key_note = _label("", "Caption"); card.body.addWidget(self.key_note)
        self.tabs = QTabWidget()
        self.persona = QPlainTextEdit(); self.persona.setPlaceholderText("身份、语气、习惯与示例对话…")
        self.global_rules = QPlainTextEdit(); self.global_rules.setPlaceholderText("两个角色共同遵守的指令…")
        self.tabs.addTab(self.persona, "本角色人格"); self.tabs.addTab(self.global_rules, "全局注入 · 两边共用")
        card.body.addWidget(self.tabs, 1)
        actions = QHBoxLayout()
        self.save_btn = _button("保存设置", primary=True)
        self.restart_btn = _button("重启该角色并应用")
        actions.addWidget(self.save_btn); actions.addWidget(self.restart_btn); actions.addStretch()
        card.body.addLayout(actions)
        card.body.addWidget(_label("保存不会打断当前对话。重启后加载新模型和人格。密钥由 Windows 加密保存。", "Caption"))
        row.addWidget(card, 1); root.addLayout(row, 1)
        self._drafts = {}; self._current = None; self._loaded = False
        self.roles.currentRowChanged.connect(self._choose)
        self.save_btn.clicked.connect(self._save)
        self.restart_btn.clicked.connect(self._restart)
        self.roles.setCurrentRow(0)

    def _values(self):
        return {"name": self.name.text(), "model": self.model.text(), "api_key": self.key.text(),
                'base_url': self.base_url.text(),
                "persona": self.persona.toPlainText(), "global": self.global_rules.toPlainText()}

    def _choose(self, row):
        if self._current and self._loaded:
            self._drafts[self._current] = self._values()
        self._current = ("qq", "research")[max(0, row)]
        aid = self._current
        self._loaded = False
        self.save_btn.setEnabled(False)
        self._submit(lambda: self.services.agent_editor(aid), lambda v: self._apply(aid, v))

    def _apply(self, aid, value):
        if aid != self._current:
            return
        draft = self._drafts.get(aid, value)
        self.name.setText(draft["name"]); self.model.setText(draft["model"])
        self.base_url.setText(draft.get('base_url', 'https://api.deepseek.com'))
        self.persona.setPlainText(draft["persona"]); self.global_rules.setPlainText(draft["global"])
        self.key.setText(draft.get("api_key", ""))
        self.key_note.setText("DeepSeek API 密钥：" + ("已配置" if value.get("key_set") else "未配置") + "；新密钥留空不会覆盖。")
        self._loaded = True

    def _save(self):
        if not self._loaded or self.loading:
            return
        aid, values = self._current, self._values()
        self.save_btn.setEnabled(False)
        def saved(result):
            self._drafts.pop(aid, None); self.key.clear()
            self._toast(result["message"])
        self._submit(lambda: self.services.save_agent(aid, values), saved)

    def _restart(self):
        if self.loading or not self._loaded:
            return
        aid = self._current
        self._toast("正在重启该角色…")
        self.restart_btn.setEnabled(False)
        def work():
            self.services.control_agent(aid, "stop")
            return self.services.control_agent(aid, "start")
        self._submit(work, lambda r: self._toast("重启完成" if r.get("ok") else str(r.get("reason", "未能启动，请查看运行记录"))),
                     on_done=lambda: self.restart_btn.setEnabled(True))

    def refresh(self):
        # Do not overwrite in-progress edits on the navigation polling timer.
        pass


class MultimodalPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        root = QVBoxLayout(self); root.setContentsMargins(28, 26, 28, 24); root.setSpacing(16)
        self._header("多模态", "图片直接交给 DeepSeek；视频工具提取画面与字幕，再由 DeepSeek 理解。", root)
        root.addWidget(self.toast)
        splitter = QSplitter(Qt.Horizontal)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame)
        settings = QWidget(); left = QVBoxLayout(settings); left.setContentsMargins(0, 0, 8, 0); left.setSpacing(16)
        self.sections = {}
        for section, title, detail in (("vision", "DeepSeek 原生图片理解", "复用每个角色已有的 DeepSeek 模型与 API 密钥。"),
                                        ("speech", "语音转写 · 可选工具", "语音需要转写工具；图片无需额外的视觉服务。")):
            card = Card(title, detail)
            enabled = QCheckBox("允许向 DeepSeek 传入图片" if section == "vision" else "启用语音转写工具")
            base = _input("http://127.0.0.1:11434/v1")
            model = _input("服务提供的模型名称")
            key = _input("API 密钥；本地免密服务可留空", True)
            form = QFormLayout(); form.setSpacing(10)
            form.addRow(enabled)
            if section != "vision":
                form.addRow("服务地址", base); form.addRow("模型名称", model); form.addRow("API 密钥", key)
            card.body.addLayout(form)
            if section == "vision":
                self.native_note = _label("支持原生图片输入的 deepseek-flash；QQ 图片随原消息进入同一回合。", "Muted")
                self.native_note.setWordWrap(True); card.body.addWidget(self.native_note)
                check = _button("检查 DeepSeek 连接")
                check.clicked.connect(self._models); card.body.addWidget(check)
            self.sections[section] = {"enabled": enabled, "base_url": base, "model": model, "api_key": key}
            left.addWidget(card)
        card = Card("接入范围与视频工具")
        self.qq_enabled = QCheckBox("QQ 收到附件时自动理解")
        self.research_enabled = QCheckBox("研究侧启用多模态补充")
        card.body.addWidget(self.qq_enabled); card.body.addWidget(self.research_enabled)
        form = QFormLayout()
        self.command = _input("自动查找已安装的 mcp-video-analyzer")
        self.frames = QSpinBox(); self.frames.setRange(1, 12)
        form.addRow("视频工具", self.command); form.addRow("最多抽样帧", self.frames)
        card.body.addLayout(form)
        self.video_state = _label("", "Caption"); self.video_state.setWordWrap(True); card.body.addWidget(self.video_state)
        self.save_btn = _button("保存多模态设置", primary=True); self.save_btn.clicked.connect(self._save)
        card.body.addWidget(self.save_btn); left.addWidget(card)
        notice = _label("图片和视频抽样帧使用角色的 DeepSeek API；记忆与本地媒体记录仍按角色分开保存。", "Caption")
        notice.setWordWrap(True); left.addWidget(notice); left.addStretch()
        scroll.setWidget(settings); splitter.addWidget(scroll)

        test = Card("试读一个文件", "结果保存在研究侧；QQ 附件的结果单独保存。")
        source_row = QHBoxLayout()
        self.source = _input("选择本地文件，或粘贴可访问的视频链接")
        browse = _button("选择文件"); browse.clicked.connect(self._browse)
        source_row.addWidget(self.source, 1); source_row.addWidget(browse); test.body.addLayout(source_row)
        self.kind = QComboBox(); self.kind.addItem("图片", "image"); self.kind.addItem("视频", "video"); self.kind.addItem("语音", "audio")
        test.body.addWidget(self.kind)
        self.preview = _label("选择图片后预览；视频会提取关键帧与字幕。", "Muted")
        self.preview.setAlignment(Qt.AlignCenter); self.preview.setMinimumHeight(150); self.preview.setMaximumHeight(220)
        self.preview.setWordWrap(True); test.body.addWidget(self.preview)
        self.question = _input("可选：重点看什么？例如“读图中文字”或“描述视频中的操作”")
        test.body.addWidget(self.question)
        self.test_btn = _button("开始理解", primary=True); self.test_btn.clicked.connect(self._analyze)
        test.body.addWidget(self.test_btn)
        self.progress = QProgressBar(); self.progress.setRange(0, 0); self.progress.hide(); test.body.addWidget(self.progress)
        self.output = QPlainTextEdit(); self.output.setReadOnly(True)
        self.output.setPlaceholderText("这里显示实际识别内容、使用的模态，以及未能观察的部分。")
        test.body.addWidget(self.output, 1)
        self.history = QComboBox(); self.history.addItem("最近识别记录")
        self.history.currentIndexChanged.connect(self._history); test.body.addWidget(self.history)
        splitter.addWidget(test); splitter.setSizes([480, 620]); root.addWidget(splitter, 1)
        self._loaded = False; self._observations = []

    def _values(self):
        values = {section: {"enabled": widgets["enabled"].isChecked(),
                  **{key: widgets[key].text().strip() for key in ("base_url", "model", "api_key")}}
                  for section, widgets in self.sections.items()}
        values.update(qq_enabled=self.qq_enabled.isChecked(), research_enabled=self.research_enabled.isChecked(),
                      video={"command": self.command.text().strip(), "max_frames": self.frames.value()})
        values["vision"]["mode"] = "deepseek"
        return values

    def refresh(self):
        if not self.loading and not self._loaded:
            self._submit(self.services.multimodal_snapshot, self._apply)

    def _apply(self, data):
        cfg = data["config"]
        for section, widgets in self.sections.items():
            widgets["enabled"].setChecked(cfg[section]["enabled"])
            for key in ("base_url", "model"):
                widgets[key].setText(cfg[section][key])
            widgets["api_key"].setPlaceholderText("已保存；留空保留" if data.get(section + "_key_set") else "本地免密可留空")
        self.qq_enabled.setChecked(cfg["qq_enabled"]); self.research_enabled.setChecked(cfg["research_enabled"])
        self.command.setText(cfg["video"]["command"]); self.frames.setValue(cfg["video"]["max_frames"])
        self.video_state.setText("视频提取工具已就绪" if data["video"].get("available") else data["video"].get("reason", "未配置"))
        self._observations = data.get("history", [])
        self.history.blockSignals(True); self.history.clear(); self.history.addItem("最近识别记录")
        for item in self._observations:
            self.history.addItem(f"{item['created_at']} · {item['kind']} · {item.get('source_name')}")
        self.history.blockSignals(False); self._loaded = True

    def _save(self):
        if self.loading:
            return
        values = self._values()
        def saved(result):
            for widgets in self.sections.values(): widgets["api_key"].clear()
            self._toast(result["message"])
        self._submit(lambda: self.services.save_multimodal(values), saved)

    def _models(self):
        if self.loading:
            return
        values = self._values()["vision"]
        self._toast("正在连接视觉服务…")
        self._submit(lambda: self.services.media_models(values),
                     lambda names: self._toast("可用模型：" + ("、".join(names) or "服务未返回模型列表")))

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要理解的文件", "", "媒体文件 (*.png *.jpg *.jpeg *.webp *.gif *.mp4 *.mkv *.mov *.webm *.mp3 *.wav *.m4a *.ogg);;所有文件 (*)")
        if not path: return
        self.source.setText(path)
        suffix = Path(path).suffix.lower()
        kind = "image" if suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif") else "audio" if suffix in (".wav", ".mp3", ".m4a", ".ogg") else "video"
        self.kind.setCurrentIndex(self.kind.findData(kind))
        if kind == "image":
            self.preview.setPixmap(QPixmap(path).scaled(440, 210, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.preview.setPixmap(QPixmap()); self.preview.setText(Path(path).name)

    def _analyze(self):
        if self.loading:
            return
        source, kind, question = self.source.text().strip(), self.kind.currentData(), self.question.text().strip()
        if not source:
            self._toast("请先选择文件或填写视频链接"); return
        values = self._values()
        self.test_btn.setEnabled(False); self.progress.show(); self._toast("正在提取与理解媒体…")
        def work():
            self.services.save_multimodal(values)
            return self.services.analyze_media(source, kind, question)
        def result(value):
            self.output.setPlainText(value["text"] + "\n\n使用：" + "、".join(value["modalities"]) +
                "\n耗时：" + str(value["elapsed_s"]) + " 秒\n" + "\n".join(value.get("warnings", [])))
            self._toast("识别完成；观察已保存在研究侧")
            for widgets in self.sections.values(): widgets["api_key"].clear()
        self._submit(work, result, on_error=lambda error: (self.output.setPlainText(error), self._toast("识别未完成，原因见结果区")),
                     on_done=lambda: self.progress.hide())

    def _history(self, index):
        if 0 < index <= len(self._observations):
            item = self._observations[index - 1]
            self.output.setPlainText(item["text"] + "\n\n" + "\n".join(item.get("warnings", [])))
