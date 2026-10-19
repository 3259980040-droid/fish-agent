"""Editable local rules with draft validation and a send-free preview."""
import json
import uuid
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QVBoxLayout
from .pages import BasePage, _button, _label
from .widgets import Card


class GuardSettingsPage(BasePage):
    def __init__(self, services):
        super().__init__(services); self.loaded = False
        root = QVBoxLayout(self); root.addWidget(self.toast)
        card = Card('本地检测规则', '回复和工具输出在发出前检测。规则可能误拦或漏检，可在这里修改并用样例检验。')
        self.rules = QPlainTextEdit(); self.rules.setPlaceholderText('规则 JSON'); self.rules.setMinimumHeight(200)
        card.body.addWidget(self.rules, 1)
        card.body.addWidget(_label('补充词表 · 每行一项', 'CardTitle'))
        self.terms = QPlainTextEdit(); self.terms.setMaximumHeight(80); card.body.addWidget(self.terms)
        row = QHBoxLayout(); save = _button('保存规则', primary=True); test = _button('测试当前规则草稿 · 不发送')
        row.addWidget(save); row.addWidget(test); row.addStretch(); card.body.addLayout(row)
        self.sample = QPlainTextEdit(); self.sample.setPlaceholderText('在这里输入测试回复，测试不会调用模型或发送 QQ 消息。'); self.sample.setMaximumHeight(100)
        card.body.addWidget(self.sample)
        self.result = _label('', 'Caption'); self.result.setWordWrap(True); card.body.addWidget(self.result)
        root.addWidget(card, 1)
        save.clicked.connect(self._save); test.clicked.connect(self._test)

    def refresh(self):
        if self.loaded or self.loading: return
        directory = self.services.root / 'harness/safety'
        def done(data):
            self.rules.setPlainText(data[0]); self.terms.setPlainText(data[1]); self.loaded = True
        self._submit(lambda: ((directory / 'rules.json').read_text(encoding='utf-8'), (directory / 'local_terms.txt').read_text(encoding='utf-8') if (directory / 'local_terms.txt').is_file() else ''), done)

    def _draft(self, text, terms, sample=None):
        from storage import settings_lock, write_json
        from guard import Guard, ACTION_ORDER, SEVERITY_ORDER
        data = json.loads(text)
        if not isinstance(data.get('categories'), list) or len(data['categories']) > 100: raise ValueError('categories 需为最多 100 项的数组')
        for cat in data['categories']:
            if cat.get('action', 'block') not in ACTION_ORDER or cat.get('severity', 'medium') not in SEVERITY_ORDER: raise ValueError('规则 action 或 severity 无效')
            if not isinstance(cat.get('patterns', []), list) or len(cat.get('patterns', [])) > 200: raise ValueError('每类最多 200 条正则')
            if any(not isinstance(p, str) or len(p) > 4000 for p in cat.get('patterns', [])): raise ValueError('正则表达式无效或过长')
        directory = self.services.root / 'harness/safety'; directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / ('.draft-' + uuid.uuid4().hex); temporary.mkdir()
        try:
            write_json(temporary / 'rules.json', data)
            for cat in data['categories']:
                if cat.get('terms_file'):
                    if cat['terms_file'] != 'local_terms.txt': raise ValueError('词表文件请使用 local_terms.txt')
            (temporary / 'local_terms.txt').write_text(terms, encoding='utf-8')
            guard = Guard(temporary / 'rules.json')
            if sample is not None: return guard.check(sample)
            with settings_lock(self.services.root):
                from storage import commit_files
                commit_files(self.services.root, {directory / 'rules.json': (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode(), directory / 'local_terms.txt': terms.encode()})
            return {'ok': True}
        finally:
            for file in temporary.iterdir(): file.unlink()
            temporary.rmdir()

    def _save(self):
        if self.loading: return
        text, terms = self.rules.toPlainText(), self.terms.toPlainText()
        self._submit(lambda: self._draft(text, terms), lambda _: self._toast('规则已保存，下次检查立即生效。'))

    def _test(self):
        if self.loading: return
        text, terms, sample = self.rules.toPlainText(), self.terms.toPlainText(), self.sample.toPlainText()
        def done(v): self.result.setText(('放行' if v['allowed'] else '阻断') + ' · ' + ', '.join(h['name'] or h['category'] for h in v['hits']) + '\n这里只测试当前规则草稿；主人附加阻断词仍在“主人与权限”中配置。')
        self._submit(lambda: self._draft(text, terms, sample), done)
