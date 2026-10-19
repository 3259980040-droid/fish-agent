"""Editable local rules with draft validation and a send-free preview."""
import json
import tempfile
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QToolButton, QVBoxLayout, QWidget
from .pages import BasePage, _button, _label
from .widgets import Card


class GuardSettingsPage(BasePage):
    def __init__(self, services):
        super().__init__(services); self.loaded = False
        root = QVBoxLayout(self); root.addWidget(self.toast)
        card = Card('回复规则', '阻断词、正则与分类规则统一保存。测试使用当前全部草稿，不调用模型、不发送消息。')
        card.outer.setAlignment(Qt.AlignTop)
        card.body.addWidget(_label('阻断词或短语 · 每行一项', 'CardTitle'))
        self.blocked = QPlainTextEdit(); self.blocked.setMaximumHeight(100); card.body.addWidget(self.blocked)
        self.patterns = QPlainTextEdit(); self.patterns.setPlaceholderText('可选：阻断正则表达式，每行一项'); self.patterns.setMaximumHeight(80)
        card.body.addWidget(self.patterns)
        advanced = QToolButton(); advanced.setText('高级分类规则与补充词表'); advanced.setCheckable(True); card.body.addWidget(advanced)
        self.advanced = QWidget(); detail = QVBoxLayout(self.advanced); detail.setContentsMargins(0, 0, 0, 0)
        self.rules = QPlainTextEdit(); self.rules.setPlaceholderText('规则 JSON'); self.rules.setMinimumHeight(200)
        detail.addWidget(self.rules, 1)
        detail.addWidget(_label('分类规则补充词表 · 每行一项', 'CardTitle'))
        self.terms = QPlainTextEdit(); self.terms.setMaximumHeight(80); detail.addWidget(self.terms)
        card.body.addWidget(self.advanced); self.advanced.hide(); advanced.toggled.connect(self.advanced.setVisible)
        row = QHBoxLayout(); save = _button('保存全部回复规则', primary=True); test = _button('测试全部草稿 · 不发送')
        row.addWidget(save); row.addWidget(test); row.addStretch(); card.body.addLayout(row)
        self.sample = QPlainTextEdit(); self.sample.setPlaceholderText('在这里输入测试回复，测试不会调用模型或发送 QQ 消息。'); self.sample.setMaximumHeight(100)
        card.body.addWidget(self.sample)
        self.result = _label('', 'Caption'); self.result.setWordWrap(True); card.body.addWidget(self.result)
        root.addWidget(card); root.addStretch(1)
        save.clicked.connect(self._save); test.clicked.connect(self._test)

    def refresh(self):
        if self.loaded or self.loading: return
        directory = self.services.root / 'harness/safety'
        def done(data):
            self.rules.setPlainText(data[0]); self.terms.setPlainText(data[1]); self.loaded = True
            self.blocked.setPlainText('\n'.join(data[2]['blocked_text'])); self.patterns.setPlainText('\n'.join(data[2]['block_patterns']))
        from governance import Governance
        self._submit(lambda: ((directory / 'rules.json').read_text(encoding='utf-8'), (directory / 'local_terms.txt').read_text(encoding='utf-8') if (directory / 'local_terms.txt').is_file() else '', Governance(self.services.root).config()), done)

    def _draft(self, text, terms, sample=None, *, blocked_text=None, block_patterns=None):
        from storage import settings_lock, write_json, commit_files
        from guard import Guard, ACTION_ORDER, SEVERITY_ORDER
        from governance import Governance
        data = json.loads(text)
        if not isinstance(data.get('categories'), list) or len(data['categories']) > 100: raise ValueError('categories 需为最多 100 项的数组')
        for cat in data['categories']:
            if cat.get('action', 'block') not in ACTION_ORDER or cat.get('severity', 'medium') not in SEVERITY_ORDER: raise ValueError('规则 action 或 severity 无效')
            if not isinstance(cat.get('patterns', []), list) or len(cat.get('patterns', [])) > 200: raise ValueError('每类最多 200 条正则')
            if any(not isinstance(p, str) or len(p) > 4000 for p in cat.get('patterns', [])): raise ValueError('正则表达式无效或过长')
        directory = self.services.root / 'harness/safety'; directory.mkdir(parents=True, exist_ok=True)
        with settings_lock(self.services.root), tempfile.TemporaryDirectory(prefix='.rule-draft-', dir=directory) as tmp:
            temporary = Path(tmp); rules = temporary / 'harness/safety/rules.json'
            write_json(rules, data)
            for cat in data['categories']:
                if cat.get('terms_file'):
                    if cat['terms_file'] != 'local_terms.txt': raise ValueError('词表文件请使用 local_terms.txt')
            rules.with_name('local_terms.txt').write_text(terms, encoding='utf-8')
            current = Governance(self.services.root).config()
            write_json(temporary / 'harness/control.json', current)
            preview = Governance(temporary)
            preview.save({'blocked_text': current['blocked_text'] if blocked_text is None else blocked_text,
                          'block_patterns': current['block_patterns'] if block_patterns is None else block_patterns})
            guard = Guard(rules)
            if sample is not None:
                verdict = guard.check(sample)
                try: preview._check_output(sample)
                except PermissionError as exc: verdict.update(allowed=False, action='block', reason=str(exc))
                return verdict
            commit_files(self.services.root, {directory / 'rules.json': rules.read_bytes(),
                directory / 'local_terms.txt': terms.encode('utf-8'),
                self.services.root / 'harness/control.json': preview.path.read_bytes()})
            Governance(self.services.root).audit('settings', '回复规则已统一更新')
            return {'ok': True}

    def _save(self):
        if self.loading: return
        text, terms = self.rules.toPlainText(), self.terms.toPlainText()
        custom = {'blocked_text': self.blocked.toPlainText().splitlines(), 'block_patterns': self.patterns.toPlainText().splitlines()}
        self._submit(lambda: self._draft(text, terms, **custom), lambda _: self._toast('全部回复规则已保存，下次检查立即生效。'))

    def _test(self):
        if self.loading: return
        text, terms, sample = self.rules.toPlainText(), self.terms.toPlainText(), self.sample.toPlainText()
        custom = {'blocked_text': self.blocked.toPlainText().splitlines(), 'block_patterns': self.patterns.toPlainText().splitlines()}
        def done(v): self.result.setText(('放行' if v['allowed'] else '阻断') + ' · ' + v.get('reason', ', '.join(h['name'] or h['category'] for h in v['hits'])) + '\n已检查全部当前草稿；未保存、未发送。')
        self._submit(lambda: self._draft(text, terms, sample, **custom), done)
