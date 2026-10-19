"""One editor for daily persona files, and one for shared source rules."""
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QPlainTextEdit, QVBoxLayout
from .pages import BasePage, _button, _label
from .theme import SPACE
from .widgets import Card


class PersonaSourcePage(BasePage):
    def __init__(self, services, *, shared=False):
        super().__init__(services)
        self.shared = shared
        self.source = 'global' if shared else 'qq'
        self.drafts = {}
        root = QVBoxLayout(self); root.setSpacing(SPACE['gap']); root.addWidget(self.toast)
        card = Card('两个角色的共享规则' if shared else '日常人格',
                    '这里编辑共享规则原文。已有测试和发布版本使用各自快照，不随此修改。' if shared else
                    '用于日常私聊和普通本地对话。已有测试窗口和群使用固定版本，不随此修改。')
        if not shared:
            self.roles = QComboBox(); self.roles.addItem('大肥鱼 · QQ 角色', 'qq'); self.roles.addItem('研究员 · B 站与视频', 'research')
            card.body.addWidget(self.roles); self.roles.currentIndexChanged.connect(self._choose)
        self.editor = QPlainTextEdit(); card.body.addWidget(self.editor, 1)
        row = QHBoxLayout(); self.save_btn = _button('保存共享规则' if shared else '保存日常人格', primary=True)
        self.reload_btn = _button('放弃草稿并重载'); row.addWidget(self.save_btn); row.addWidget(self.reload_btn); row.addStretch(); card.body.addLayout(row)
        self.note = _label('保存后，日常会话在角色下次启动时加载。固定版本请在“人格版本”中另存为新版本。', 'Caption')
        self.note.setWordWrap(True); card.body.addWidget(self.note)
        root.addWidget(card, 1)
        self.save_btn.clicked.connect(self._save); self.reload_btn.clicked.connect(self._reload)

    def _remember(self):
        if self.source in self.drafts:
            self.drafts[self.source]['text'] = self.editor.toPlainText()

    def _choose(self):
        self._remember(); self.source = self.roles.currentData(); self._load()

    def _load(self):
        if self.source not in self.drafts:
            self.drafts[self.source] = self.services.persona_source(self.source)
        self.editor.setPlainText(self.drafts[self.source]['text'])

    def refresh(self):
        if not self.drafts:
            self._load()

    def _reload(self):
        if self.loading: return
        snapshot = self.services.persona_source(self.source)
        self.drafts[self.source] = snapshot; self._load()

    def _save(self):
        if self.loading or self.source not in self.drafts: return
        source, text, digest = self.source, self.editor.toPlainText(), self.drafts[self.source]['digest']
        def done(value):
            self.drafts[source] = {**value, 'saved': value['text']}
            self._toast('已保存；当前测试窗口与群绑定版本保持固定。')
        self._submit(lambda: self.services.save_persona_source(source, text, digest), done)
