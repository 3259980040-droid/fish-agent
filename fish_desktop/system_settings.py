"""Editable owner permissions, connections and advanced media options."""
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget, QScrollArea
from .pages import BasePage, SettingsPage, _button, _label
from .configuration import MultimodalPage, _input
from .widgets import Card
from .data_settings import DataPage


class OwnerPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        self._loaded = False
        root = QVBoxLayout(self)
        root.addWidget(self.toast)
        card = Card("主人账号与管理权限", "只认实际 QQ 号。昵称、消息里的自称和图片都不能授予权限。")
        form = QFormLayout()
        self.owner = _input("主人 QQ 号")
        self.owner.setMaximumWidth(360)  # §3.3: size the field to its content.
        form.addRow("主人 QQ", self.owner)
        self.permissions = {}
        row = QGridLayout()
        from governance import CAPABILITIES
        for index, (key, title) in enumerate(CAPABILITIES.items()):
            check = QCheckBox(title)
            self.permissions[key] = check
            row.addWidget(check, index // 4, index % 4)
        form.addRow("私聊管理权限", row)
        self.paused = QCheckBox("暂停 QQ 角色任务与回复")
        form.addRow(self.paused)
        self.daily_limits = QComboBox()
        self.daily_limits.addItem('无上限（继续统计用量）', False)
        self.daily_limits.addItem('自定义每日上限', True)
        self._daily_limit_roles = {'qq': True, 'research': True}
        form.addRow('QQ 每日用量模式', self.daily_limits)
        self.calls = QSpinBox(); self.calls.setRange(1, 10000)
        self.replies = QSpinBox(); self.replies.setRange(1, 10000)
        self.tools = QSpinBox(); self.tools.setRange(1, 10000)
        self.model_calls = QSpinBox(); self.model_calls.setRange(1, 10000)
        self.tokens = QSpinBox(); self.tokens.setRange(1000, 100000000)
        for widget in (self.calls, self.replies, self.tools, self.model_calls, self.tokens): widget.setMaximumWidth(220)
        form.addRow("每角色每日处理轮数", self.calls)
        form.addRow("每日 QQ 发出条数", self.replies)
        form.addRow("每轮工具调用上限", self.tools)
        form.addRow('每角色每日模型请求上限', self.model_calls)
        form.addRow('已报告 token 的暂停阈值', self.tokens)
        self.daily_limits.currentIndexChanged.connect(lambda _: [w.setEnabled(self.daily_limits.currentData()) for w in
            (self.calls, self.replies, self.model_calls, self.tokens)])
        card.body.addWidget(_label('关闭后，QQ 不按每日处理轮数、发出条数、模型请求数或 token 总量暂停；用量继续统计。B 站研究侧保持独立设置。', 'Caption'))
        card.body.addLayout(form)
        card.body.addWidget(_label("主人任务认真执行，必要时允许详细回答；人格中的懒或顶嘴不能代替执行。管理权限由程序执行。", "Caption"))
        root.addWidget(card)
        rules = Card("本地阻断规则", "每行一项。命中后不发出原回复，并在本地留下阻断原因。")
        self.terms = QPlainTextEdit(); self.terms.setPlaceholderText("需要阻断的词或短语，每行一个")
        self.terms.setMaximumHeight(100)
        self.patterns = QPlainTextEdit(); self.patterns.setPlaceholderText("可选：正则表达式，每行一个")
        self.patterns.setMaximumHeight(80)
        rules.body.addWidget(self.terms); rules.body.addWidget(self.patterns)
        self.save_btn = _button("保存主人权限与规则", primary=True)
        rules.body.addWidget(self.save_btn)
        root.addWidget(rules)
        self.state = _label("", "Caption")
        self.state.setWordWrap(True)
        root.addWidget(self.state)
        root.addStretch()
        self.save_btn.clicked.connect(self._save)

    def refresh(self):
        if self._loaded or self.loading: return
        self._submit(self.services.governance_snapshot, self._apply)

    def _apply(self, data):
        cfg = data["config"]
        self.owner.setText(data["owner_id"])
        for key, widget in self.permissions.items(): widget.setChecked(key in cfg["owner_permissions"])
        self.paused.setChecked(cfg["paused"])
        self.calls.setValue(cfg["max_daily_calls"]); self.replies.setValue(cfg["max_daily_replies"])
        self.tools.setValue(cfg["max_tool_calls_per_turn"])
        self.model_calls.setValue(cfg['max_model_requests_daily']); self.tokens.setValue(cfg['max_model_tokens_daily'])
        self._daily_limit_roles = dict(cfg.get('daily_usage_limits', {'qq': True, 'research': True}))
        self.daily_limits.setCurrentIndex(self.daily_limits.findData(self._daily_limit_roles.get('qq', True)))
        for widget in (self.calls, self.replies, self.model_calls, self.tokens):
            widget.setEnabled(self.daily_limits.currentData())
        self.terms.setPlainText("\n".join(cfg["blocked_text"]))
        self.patterns.setPlainText("\n".join(cfg["block_patterns"]))
        audit = data.get("audit", [])
        mode='自定义每日上限' if self.daily_limits.currentData() else '无上限（用量继续统计）'
        self.state.setText('当前 QQ 每日用量：'+mode+'；今日处理轮数：' + str(data['usage'].get('calls', 0)) + '；最近记录：' + (audit[0]['reason'] if audit else '暂无'))
        self._loaded = True

    def _save(self):
        if self.loading: return
        values = {"owner_permissions": [key for key, w in self.permissions.items() if w.isChecked()],
            "paused": self.paused.isChecked(), "max_daily_calls": self.calls.value(), "max_daily_replies": self.replies.value(),
            "max_tool_calls_per_turn": self.tools.value(), "blocked_text": self.terms.toPlainText().splitlines(),
            "block_patterns": self.patterns.toPlainText().splitlines()}
        values.update(max_model_requests_daily=self.model_calls.value(), max_model_tokens_daily=self.tokens.value())
        values['daily_usage_limits'] = {**self._daily_limit_roles, 'qq': self.daily_limits.currentData()}
        owner = self.owner.text()
        def saved(value):
            self._toast(value['message'])
            self._apply(self.services.governance_snapshot())
        self._submit(lambda: self.services.save_governance(values, owner), saved)


class ConnectionsPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        self._loaded = False
        root = QVBoxLayout(self)
        root.addWidget(self.toast)
        card = Card("QQ 连接与运行时", "密钥留空保留已有值。保存后在工作台重启相应角色。")
        form = QFormLayout()
        self.url = _input("ws://127.0.0.1:3001")
        self.napcat = _input("NapCat 连接密钥", True)
        self.core = _input("Fish 通道密钥", True)
        self.cli = _input("现有 dsh.CMD 或启动程序路径")
        self.node = _input("留空自动选择现有 Node")
        for title, widget in (("NapCat 地址", self.url), ("NapCat 密钥", self.napcat), ("通道密钥", self.core), ("DSH 启动器", self.cli), ("Node", self.node)):
            form.addRow(title, widget)
        card.body.addLayout(form)
        self.note = _label("", "Caption")
        card.body.addWidget(self.note)
        self.save_btn = _button("保存连接设置", primary=True)
        card.body.addWidget(self.save_btn)
        root.addWidget(card)
        # §2.1/3.3: separate network choices from account credentials; one save
        # action per card, with editable fields sized by the parent layout.
        network = Card('网络连接方式', '应用于模型、B 站读取、HTTP 插件和内置视频工具。保存后重启角色；本机连接始终直连。')
        self.network_mode = QComboBox()
        for label, value in [('使用系统环境代理', 'system'), ('直接连接', 'direct'), ('指定 HTTP 代理', 'proxy')]: self.network_mode.addItem(label, value)
        self.proxy = _input('http://127.0.0.1:7890')
        form = QFormLayout(); form.addRow('连接方式', self.network_mode); form.addRow('代理地址', self.proxy); network.body.addLayout(form)
        row = QHBoxLayout(); self.network_save = _button('保存网络设置', primary=True); self.network_test = _button('测试当前填写的连接')
        row.addWidget(self.network_save); row.addWidget(self.network_test); row.addStretch(); network.body.addLayout(row)
        self.network_note = _label('', 'Caption'); self.network_note.setWordWrap(True); network.body.addWidget(self.network_note)
        root.addWidget(network)
        self.network_mode.currentIndexChanged.connect(lambda: self.proxy.setEnabled(self.network_mode.currentData() == 'proxy'))
        self.network_save.clicked.connect(self._save_network); self.network_test.clicked.connect(self._test_network)
        root.addStretch()
        self.save_btn.clicked.connect(self._save)

    def refresh(self):
        if self._loaded or self.loading: return
        self._submit(self.services.connection_snapshot, self._apply)

    def _apply(self, data):
        self.url.setText(data["napcat_url"]); self.cli.setText(data["cli"]); self.node.setText(data["node"])
        from network_config import settings
        cfg = settings(self.services.root); self.network_mode.setCurrentIndex(self.network_mode.findData(cfg['mode'])); self.proxy.setText(cfg['proxy']); self.proxy.setEnabled(cfg['mode'] == 'proxy')
        self.note.setText("通道密钥：" + ("已配置" if data["core_key_set"] else "未配置") + "；NapCat 密钥：" + ("已配置" if data["napcat_key_set"] else "未配置"))
        self._loaded = True

    def _save(self):
        values = {"napcat_url": self.url.text(), "napcat_token": self.napcat.text(), "core_token": self.core.text(), "cli": self.cli.text(), "node": self.node.text()}
        def saved(value):
            self.napcat.clear(); self.core.clear(); self._toast(value["message"])
        if not self.loading: self._submit(lambda: self.services.save_connections(values), saved)

    def _network_values(self): return {'mode': self.network_mode.currentData(), 'proxy': self.proxy.text()}

    def _save_network(self):
        if self.loading: return
        from network_config import NetworkConfig
        values = self._network_values()
        self._submit(lambda: NetworkConfig(self.services.root).save(values), lambda _: self.network_note.setText('已保存。重启正在运行的角色后生效；独立插件也需重新启动。'), on_error=self.network_note.setText)

    def _test_network(self):
        if self.loading: return
        from network_config import test_connection
        values = self._network_values(); self.network_note.setText('正在测试连接…')
        self._submit(lambda: test_connection(self.services.root, values), self.network_note.setText, on_error=self.network_note.setText)


class SystemSettingsPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 24)
        self._header("系统设置", "主人权限、连接和媒体选项。照片在普通聊天中默认理解。", root)
        self.tabs = QTabWidget()
        self.owner_page = OwnerPage(services)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame); scroll.setWidget(self.owner_page)
        self.tabs.addTab(scroll, "主人与权限")
        connections = QScrollArea(); connections.setWidgetResizable(True); connections.setFrameShape(QScrollArea.NoFrame); connections.setWidget(ConnectionsPage(services))
        self.tabs.addTab(connections, "连接设置")
        media = MultimodalPage(services)
        splitter = media.findChild(QSplitter)
        splitter.widget(1).hide()
        media.findChild(QLabel, "PageTitle").setText("媒体选项")
        self.tabs.addTab(media, "媒体与工具")
        data = QScrollArea(); data.setWidgetResizable(True); data.setFrameShape(QScrollArea.NoFrame); data.setWidget(DataPage(services))
        self.tabs.addTab(data, "数据与升级")
        from .guard_settings import GuardSettingsPage
        guard_scroll = QScrollArea(); guard_scroll.setWidgetResizable(True); guard_scroll.setFrameShape(QScrollArea.NoFrame)
        guard_scroll.setWidget(GuardSettingsPage(services)); self.tabs.addTab(guard_scroll, "本地检测规则")
        root.addWidget(self.tabs, 1)
        self.tabs.currentChanged.connect(self.refresh)

    def refresh(self):
        page = self.tabs.currentWidget()
        if isinstance(page, QScrollArea): page = page.widget()
        if page and not page.loading: page.refresh()
