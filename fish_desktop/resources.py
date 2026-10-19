"""Resources: a selectable library, then editable details and one Save action.

Refactoring UI §2.1/§2.8/§3.6: primary = selected resource and saved state;
secondary = filters and metadata; tertiary = logs/source. Lists use the existing
surface tokens, page/section/gap/small spacing. Mutating rows never resets drafts.
"""
from __future__ import annotations
import json
from pathlib import Path
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QPlainTextEdit, QScrollArea, QSpinBox, QSplitter,
    QTabWidget, QVBoxLayout, QWidget)
from .configuration import _input
from .pages import BasePage, _button, _label
from .theme import SPACE
from .widgets import Card


def _panel(parent, title):
    card = Card(title)
    scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setWidget(card)
    return scroll, card


class MemoryPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        self.rows, self.mid, self.loaded = [], '', False
        layout = QVBoxLayout(self); layout.setSpacing(SPACE['gap'])
        layout.addWidget(self.toast)
        top = QHBoxLayout()
        self.role = QComboBox(); self.role.addItem('QQ 角色记忆', 'qq'); self.role.addItem('研究角色记忆', 'research')
        self.query = _input('搜索记忆内容、分类和来源')
        self.search_btn = _button('搜索')
        for w in (self.role, self.query, self.search_btn): top.addWidget(w)
        layout.addLayout(top)
        controls = QHBoxLayout()
        self.auto_learning = QCheckBox('从新聊天中自动学习长期记忆')
        self.learning_note = _label('', 'Caption'); self.learning_note.setWordWrap(True)
        self.test_note = _label('', 'Caption'); self.test_note.setWordWrap(True)
        apply_learning = _button('保存学习开关'); retry_learning = _button('重试失败记录')
        controls.addWidget(self.auto_learning); controls.addWidget(apply_learning); controls.addWidget(retry_learning); controls.addStretch()
        layout.addLayout(controls); layout.addWidget(self.learning_note); layout.addWidget(self.test_note)
        apply_learning.clicked.connect(self._learning_save); retry_learning.clicked.connect(self._learning_retry)
        split = QSplitter(Qt.Horizontal)
        left = Card('长期记忆')
        self.list = QListWidget(); self.list.setWordWrap(True); self.list.setObjectName('ConversationList')
        left.body.addWidget(self.list, 1)
        new = _button('新增记忆'); left.body.addWidget(new)
        split.addWidget(left)
        scroll, right = _panel(self, '内容与来源')
        form = QFormLayout(); form.setVerticalSpacing(SPACE['gap'])
        self.category = _input('偏好 / 事实 / 约定')
        self.scope = QComboBox(); self.scope.addItem('日常主人私聊和本地可见', 'owner'); self.scope.addItem('旧版公共资料（不进入独立窗口和群）', 'public')
        form.addRow('分类', self.category); form.addRow('可见范围', self.scope)
        self.subject = _input('qq:账号，或 fiction:虚构人物')
        self.aliases = _input('昵称、别称，用中文逗号或英文逗号分开')
        self.memory_kind = QComboBox()
        for value,title in (('fact','事实'),('preference','偏好'),('relationship','关系'),('event','共同事件'),('promise','约定'),('meme','梗与出处'),('emotion','临时情绪')):
            self.memory_kind.addItem(title,value)
        self.expires = _input('Unix 时间戳；0 表示不自动过期'); self.expires.setText('0')
        form.addRow('关联主体',self.subject); form.addRow('昵称与别称',self.aliases); form.addRow('记忆类型',self.memory_kind); form.addRow('过期时间',self.expires)
        right.body.addLayout(form)
        self.text = QPlainTextEdit(); self.text.setPlaceholderText('记录稳定、准确的偏好与事实。保存后下一次聊天会检索当前有效版本。')
        self.text.setMinimumHeight(160); right.body.addWidget(self.text, 1)
        self.source = QPlainTextEdit(); self.source.setReadOnly(True); self.source.setMaximumHeight(160)
        self.source.setPlaceholderText('选中记忆后可以查看来源和原文。')
        right.body.addWidget(_label('来源与原文', 'CardTitle')); right.body.addWidget(self.source)
        row = QHBoxLayout(); self.save_btn = _button('保存记忆', primary=True); self.delete_btn = _button('删除并禁止重新学习')
        row.addWidget(self.save_btn); row.addWidget(self.delete_btn); row.addStretch(); right.body.addLayout(row)
        split.addWidget(scroll); split.setSizes([360, 700]); layout.addWidget(split, 1)
        new.clicked.connect(self._new); self.role.currentIndexChanged.connect(self._role)
        self.search_btn.clicked.connect(self.refresh); self.query.returnPressed.connect(self.refresh)
        self.list.currentRowChanged.connect(self._select)
        self.save_btn.clicked.connect(self._save); self.delete_btn.clicked.connect(self._delete)
        self.scope.currentIndexChanged.connect(self._update_scope_guard)
        self.review = QCheckBox('显示待确认候选'); top.addWidget(self.review); self.review.toggled.connect(self.refresh)
        self.confirm_btn = _button('确认所选候选'); row.insertWidget(2,self.confirm_btn); self.confirm_btn.clicked.connect(self._confirm)
        self._update_scope_guard()

    def store(self):
        from memory_store import MemoryStore
        return MemoryStore(self.services.root, self.role.currentData())

    def _role(self):
        self._new(); self.loaded = False; self.refresh()

    def refresh(self):
        if self.loading: return
        query, store = self.query.text(), self.store()
        self.save_btn.setEnabled(False)
        review = self.review.isChecked()
        def result(): return store.search(query,include_candidates=review), store.learning_status()
        def rendered(data):
            rows, state = data
            self.auto_learning.setChecked(state['enabled'])
            names = {'pending': '待学习', 'running': '学习中', 'done': '已完成', 'failed': '失败'}
            self.learning_note.setText(' · '.join(names.get(k, k) + ' ' + str(v) for k, v in state['counts'].items()) + ('\n最近失败：' + state['errors'][0]['error'] if state['errors'] else ''))
            self._render(rows)
        self._submit(result, rendered)

    def _learning_save(self):
        if self.loading: return
        store, enabled = self.store(), self.auto_learning.isChecked()
        self._submit(lambda: store.configure(enabled), lambda _: self._toast('学习开关已保存；关闭后保留已有记忆和待处理记录。'))

    def _learning_retry(self):
        if self.loading: return
        self._submit(self.store().retry_failed, lambda r: self._toast(f"已重新排队 {r['count']} 条失败记录；启用自动学习后会产生模型请求。"))

    def _render(self, rows):
        self.loaded = True; self.rows = rows
        self.list.blockSignals(True); self.list.clear()
        for row in rows:
            item = QListWidgetItem(row['text'][:100] + '\n' + row['category'] + ' · ' + ('待确认 · ' if row.get('status')=='candidate' else '') + ('主人私有' if row['scope'] == 'owner' else row['scope']))
            item.setData(Qt.UserRole, row['id']); self.list.addItem(item)
            if row['id'] == self.mid: self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        if not rows:
            self._toast('还没有匹配的记忆。可新增，也可在聊天中明确要求记住；自动学习会保留来源。')

    def _select(self, index):
        if not 0 <= index < len(self.rows): return
        row = self.rows[index]; self.mid = row['id']
        self.text.setPlainText(row['text']); self.category.setText(row['category'])
        target = self.scope.findData(row['scope'])
        if target < 0:
            self.scope.addItem(row['scope'], row['scope']); target = self.scope.count()-1
        self.scope.setCurrentIndex(target)
        self._update_scope_guard()
        self.subject.setText(row.get('subject','')); self.aliases.setText('，'.join(row.get('aliases',[])))
        self.memory_kind.setCurrentIndex(max(0,self.memory_kind.findData(row.get('kind','fact')))); self.expires.setText(str(row.get('expires',0)))
        self.source.setPlainText(f"来源：{row['source']}\n证据类别：{row.get('evidence','manual')} · 状态：{row.get('status','active')}\n置信度：{row['confidence']:.2f}\n\n{row['source_text']}")

    def _is_test_scope(self):
        return str(self.scope.currentData() or '').startswith('space:')

    def _done(self, on_done=None):
        super()._done(on_done)
        self._update_scope_guard()

    def _update_scope_guard(self):
        readonly = self._is_test_scope()
        editable = not readonly and not self.loading
        self.save_btn.setEnabled(editable)
        self.confirm_btn.setEnabled(editable)
        self.delete_btn.setEnabled(editable and bool(self.mid))
        self.test_note.setText(
            '独立测试空间：长期记忆只读。这里只显示已有测试资料，不会写入、确认、删除或重学正式记忆。'
            if readonly else ''
        )

    def _new(self):
        self.mid = ''; self.list.clearSelection(); self.text.clear(); self.category.setText('偏好'); self.scope.setCurrentIndex(0); self.source.clear()
        self.subject.clear(); self.aliases.clear(); self.expires.setText('0'); self.memory_kind.setCurrentIndex(0)
        self._update_scope_guard()

    def _save(self):
        if self.loading: return
        if self._is_test_scope():
            self._toast('独立测试空间长期记忆只读，不能写入正式记忆库。')
            return
        store, mid, text, category, scope = self.store(), self.mid, self.text.toPlainText(), self.category.text(), self.scope.currentData()
        if not text.strip(): self._toast('请先填写需要保存的记忆内容。'); return
        self.save_btn.setEnabled(False)
        def saved(result):
            if not result.get('ok'):
                self._toast(result.get('reason', '记忆没有保存')); return
            self.mid = result.get('id', mid); self._toast('记忆已保存；原自动来源不能覆盖人工纠正。'); self.loading = False; self.refresh()
        try:
            meta = {'subject':self.subject.text().strip(),'aliases':[s.strip() for s in self.aliases.text().replace('，',',').split(',') if s.strip()],
                    'kind':self.memory_kind.currentData(),'expires':float(self.expires.text() or '0')}
        except ValueError: self._toast('过期时间请填写数字时间戳，或 0'); self.save_btn.setEnabled(True); return
        self._submit(lambda: store.save(text, category=category, scope=scope, memory_id=mid, **meta), saved)

    def _confirm(self):
        row = next((r for r in self.rows if r['id']==self.mid),None)
        if not row or self.loading: return
        if str(row.get('scope','')).startswith('space:'):
            self._toast('独立测试空间长期记忆只读，不能确认候选记忆。')
            return
        store=self.store()
        def confirmed(_):
            self.loading=False;self._toast('候选已确认，下轮可检索。');self.refresh()
        self._submit(lambda: store.confirm(row['id'],scope=row['scope']),confirmed)

    def _delete(self):
        if not self.mid or self.loading: return
        if self._is_test_scope():
            self._toast('独立测试空间长期记忆只读，不能删除正式记忆。')
            return
        store, mid = self.store(), self.mid
        def done(_):
            self._new(); self._toast('已删除，并禁止原内容或来源重新学习。'); self.loading = False; self.refresh()
        self._submit(lambda: store.delete(mid), done)


class PluginsPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        self.entries, self.loaded = [], False
        root = QVBoxLayout(self); root.setSpacing(SPACE['gap']); root.addWidget(self.toast)
        split = QSplitter(Qt.Horizontal)
        left = Card('已添加的插件')
        self.list = QListWidget(); left.body.addWidget(self.list, 1)
        row = QHBoxLayout(); new = _button('新增'); imp = _button('导入配置'); row.addWidget(new); row.addWidget(imp); left.body.addLayout(row)
        split.addWidget(left)
        scroll, right = _panel(self, '插件配置')
        form = QFormLayout(); form.setVerticalSpacing(SPACE['gap'])
        self.pid, self.name = _input('例如 video_helper'), _input('方便识别的名称')
        self.kind = QComboBox()
        for label, key in [('MCP · 本地程序', 'mcp_stdio'), ('MCP · HTTP', 'mcp_http'), ('MCP · SSE', 'mcp_sse'), ('CLI · JSON 输入输出', 'cli'), ('HTTP · JSON 接口', 'http'), ('DSH · 原生插件', 'dsh')]: self.kind.addItem(label, key)
        self.program = _input('程序本体路径，如 node.exe / python.exe')
        choose = _button('选择程序'); program_row = QHBoxLayout(); program_row.addWidget(self.program); program_row.addWidget(choose)
        self.args = QPlainTextEdit(); self.args.setPlaceholderText('每行一个启动参数；第一行可填项目入口文件的完整路径。'); self.args.setMaximumHeight(88)
        self.url = _input('http://127.0.0.1:端口/mcp')
        self.secret_name = _input('本地程序的凭证变量名，例如 API_KEY')
        self.secret_value = _input('留空保留已保存凭证', True)
        self.qq, self.research = QCheckBox('QQ 角色'), QCheckBox('研究角色')
        roles = QHBoxLayout(); roles.addWidget(self.qq); roles.addWidget(self.research); roles.addStretch()
        self.owner = QCheckBox('仅主人私聊及本地控制台可以调用'); self.owner.setChecked(True)
        self.enabled = QCheckBox('启用已测试的工具')
        for label, widget in [('插件 ID', self.pid), ('名称', self.name), ('类型', self.kind), ('启动程序', program_row), ('启动参数', self.args), ('服务地址', self.url), ('允许的角色', roles), ('调用范围', self.owner)]: form.addRow(label, widget)
        right.body.addLayout(form)
        secret_form = QFormLayout(); secret_form.addRow('凭证变量名', self.secret_name); secret_form.addRow('凭证 / Bearer Token', self.secret_value); right.body.addLayout(secret_form)
        self.tool_list = QListWidget(); self.tool_list.setMaximumHeight(150); right.body.addWidget(_label('允许 AI 调用的工具', 'CardTitle')); right.body.addWidget(self.tool_list)
        self.schemas = QPlainTextEdit(); self.schemas.setMaximumHeight(140); self.schemas.setPlaceholderText('CLI/HTTP 的工具声明（JSON 数组）；MCP 测试后自动发现。')
        right.body.addWidget(_label('工具声明 · 高级', 'CardTitle')); right.body.addWidget(self.schemas)
        right.body.addWidget(self.enabled)
        actions = QHBoxLayout(); self.save_btn = _button('保存配置', primary=True); self.test_btn = _button('测试并发现工具'); self.disable_btn = _button('停用 / 停止调用')
        for w in (self.save_btn, self.test_btn, self.disable_btn): actions.addWidget(w)
        actions.addStretch(); right.body.addLayout(actions)
        self.remove_btn = _button('移除插件及原生安装'); right.body.addWidget(self.remove_btn)
        self.remove_btn.clicked.connect(self._remove)
        warning = _label('外部程序按当前 Windows 用户权限运行。Fish 会限制哪些角色可以调用，并隔离传入的密钥和工作目录；这不等同于操作系统沙箱。', 'Caption'); warning.setWordWrap(True); right.body.addWidget(warning)
        self.logs = QPlainTextEdit(); self.logs.setReadOnly(True); self.logs.setMaximumHeight(120); right.body.addWidget(self.logs)
        self.health = _label('选择插件后查看版本、依赖和最近测试状态。', 'Caption'); self.health.setWordWrap(True); right.body.addWidget(self.health)
        split.addWidget(scroll); split.setSizes([320, 740]); root.addWidget(split, 1)
        new.clicked.connect(self._new); imp.clicked.connect(self._import); choose.clicked.connect(self._choose)
        self.list.currentRowChanged.connect(self._select); self.save_btn.clicked.connect(self._save)
        self.test_btn.clicked.connect(self._test); self.disable_btn.clicked.connect(self._disable)
        self.kind.currentIndexChanged.connect(self._kind); self._kind()

    def registry(self):
        from plugin_registry import PluginRegistry
        return PluginRegistry(self.services.root, self.services.runtime)

    def refresh(self):
        if self.loading or self.loaded: return
        self._submit(self.registry().snapshot, self._render)

    def _render(self, data):
        self.entries = data['plugins']; self.loaded = True
        selected = self.pid.text()
        self.list.blockSignals(True); self.list.clear()
        for item in self.entries:
            state = '已启用' if item['enabled'] else '已停用'
            if item.get('test_ok') is False: state = '测试未通过 · 已停用'
            if item['kind'] == 'dsh' and item['enabled'] and set(item['roles']) != set(item.get('installed_roles', [])): state = '待安装 / 更新'
            version = item.get('server_info', {}).get('version') or item.get('package_version') or '未声明版本'
            row = QListWidgetItem(item['name'] + '\n' + version + ' · ' + state + '\n' + ' / '.join(item['roles']))
            row.setToolTip(__import__('plugin_registry').health_details(item))
            self.list.addItem(row)
            if item['id'] == selected: self.list.setCurrentItem(row)
        self.list.blockSignals(False)
        current = next((p for p in self.entries if p['id'] == selected), None)
        if current: self.health.setText(__import__('plugin_registry').health_details(current))
        self.logs.setPlainText('\n'.join(('成功' if e['ok'] else '失败') + ' · ' + e['plugin'] + ' · ' + e['action'] + ' ' + e['detail'] for e in data['events'][:20]))
        if not self.entries: self._toast('添加现有本地项目或导入 MCP 配置，测试后选择角色并启用。')

    def _kind(self):
        local = self.kind.currentData() in ('mcp_stdio', 'cli', 'dsh')
        self.program.setEnabled(local); self.args.setEnabled(local); self.url.setEnabled(not local)
        self.schemas.setVisible(self.kind.currentData() in ('cli', 'http'))
        self.secret_name.setEnabled(self.kind.currentData() in ('cli', 'mcp_stdio'))
        self.secret_value.setEnabled(self.kind.currentData() != 'dsh')
        self.test_btn.setText('安装 / 更新并重启角色' if self.kind.currentData() == 'dsh' else '测试并发现工具')
        self.program.setPlaceholderText('含 package.json 的本地插件目录' if self.kind.currentData() == 'dsh' else '程序本体路径，如 node.exe / python.exe')

    def _remove(self):
        if self.loading or not self.pid.text().strip(): return
        pid = self.pid.text().strip()
        def done(_):
            self._new(); self.loaded = False; self.loading = False; self.refresh(); self._toast('插件已移除；本地项目文件保留。')
        self._submit(lambda: self.registry().remove(pid), done)

    def _new(self):
        self.pid.clear(); self.pid.setReadOnly(False); self.name.clear(); self.program.clear(); self.args.clear(); self.url.clear(); self.schemas.clear()
        self.qq.setChecked(False); self.research.setChecked(True); self.enabled.setChecked(False); self.owner.setChecked(True)
        self.tool_list.clear(); self.secret_name.clear(); self.secret_value.clear()

    def _select(self, index):
        if not 0 <= index < len(self.entries): return
        item = self.entries[index]; self.pid.setText(item['id']); self.pid.setReadOnly(True); self.name.setText(item['name'])
        self.health.setText(__import__('plugin_registry').health_details(item))
        self.kind.setCurrentIndex(self.kind.findData(item['kind'])); self.program.setText(item.get('command', '')); self.args.setPlainText('\n'.join(item.get('args', [])))
        self.url.setText(item.get('url', '')); self.qq.setChecked('qq' in item['roles']); self.research.setChecked('research' in item['roles'])
        self.owner.setChecked(item['owner_only']); self.enabled.setChecked(item['enabled']); self.schemas.setPlainText(json.dumps(item.get('tools', []), ensure_ascii=False, indent=2))
        self._tools(item.get('tools', []), item)
        self.secret_value.clear(); self.secret_name.setText(next(iter(item.get('secret_env', {})), ''))

    def _tools(self, tools, plugin):
        from plugin_registry import tool_allowed, outbound_tool
        self.tool_list.clear()
        for tool in tools:
            item = QListWidgetItem(tool['name'] + (' · 外发操作未开放' if outbound_tool(tool['name']) else ''))
            item.setData(Qt.UserRole, tool['name']); item.setToolTip(tool.get('description', ''))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if tool_allowed(plugin, tool) else Qt.Unchecked)
            if outbound_tool(tool['name']): item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.tool_list.addItem(item)

    def _values(self):
        result = {'id': self.pid.text().strip(), 'name': self.name.text().strip(), 'kind': self.kind.currentData(), 'command': self.program.text().strip(),
            'args': [v for v in self.args.toPlainText().splitlines() if v], 'url': self.url.text().strip(),
            'roles': [role for role, w in [('qq', self.qq), ('research', self.research)] if w.isChecked()], 'owner_only': self.owner.isChecked(),
            'enabled': self.enabled.isChecked(), 'tools': json.loads(self.schemas.toPlainText() or '[]'), 'timeout': 90}
        if self.tool_list.count(): result['allowed_tools'] = [self.tool_list.item(i).data(Qt.UserRole) for i in range(self.tool_list.count()) if self.tool_list.item(i).checkState() == Qt.Checked]
        if self.secret_value.text():
            if self.kind.currentData() in ('http', 'mcp_http', 'mcp_sse'): result['api_key'] = self.secret_value.text()
            else:
                if not self.secret_name.text().strip(): raise ValueError('请填写凭证环境变量名')
                result['secrets'] = {self.secret_name.text().strip(): self.secret_value.text()}
        return result

    def _save(self):
        if self.loading: return
        try: values = self._values()
        except ValueError as exc: self._toast(str(exc) or '工具声明需为 JSON 数组'); return
        def done(_):
            self.secret_value.clear(); self._toast('配置已保存；测试后再启用。DSH 原生包需点安装/更新应用。'); self.loaded = False; self.loading = False; self.refresh()
        self._submit(lambda: self.registry().save(values), done)

    def _test(self):
        if self.loading: return
        pid = self.pid.text()
        def done(result):
            self._toast('已发现 ' + str(len(result['tools'])) + ' 个工具。请选择允许调用的项目后保存。'); self.schemas.setPlainText(json.dumps(result['tools'], ensure_ascii=False, indent=2)); self._tools(result['tools'], {}); self.loaded = False; self.loading = False; self.refresh()
        def failed(reason):
            self._toast(reason); self.enabled.setChecked(False); self.loaded = False; self.loading = False; self.refresh()
        self._submit(lambda: self.registry().discover(pid), done, on_error=failed)

    def _disable(self):
        if self.loading: return
        pid = self.pid.text()
        def done(_): self.enabled.setChecked(False); self._toast('已停用；当前插件调用已请求停止。'); self.loaded = False; self.loading = False; self.refresh()
        self._submit(lambda: self.registry().toggle(pid, False), done)

    def _choose(self):
        if self.kind.currentData() == 'dsh':
            path = QFileDialog.getExistingDirectory(self, '选择 DSH 原生插件项目')
            if path: self.program.setText(path)
            return
        path, _ = QFileDialog.getOpenFileName(self, '选择程序本体', '', '程序 (*.exe);;所有文件 (*)')
        if path: self.program.setText(path)

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, '导入 Fish 或 MCP 插件配置', '', 'JSON (*.json)')
        if not path: return
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            if 'mcpServers' in data:
                entries = []
                import re, shutil
                for name, server in data['mcpServers'].items():
                    pid = re.sub('[^a-z0-9_]', '_', name.lower())[:35]
                    if not pid or not pid[0].isalpha(): pid = 'plugin_' + pid
                    kind = 'mcp_http' if server.get('url') else 'mcp_stdio'
                    command = server.get('command', '')
                    entries.append({'id': pid, 'name': name, 'kind': kind, 'command': shutil.which(command) or command, 'args': server.get('args', []),
                        'url': server.get('url', ''), 'secrets': server.get('env', {}), 'roles': ['research'], 'enabled': False})
            else: entries = data.get('plugins', [data])
            for item in entries:
                item['enabled'] = False; self.registry().save(item)
            self.loaded = False; self.refresh(); self._toast('已导入并保持停用，请测试并确认角色范围。')
        except Exception as exc: self._toast(str(exc))


class EmojiPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        self.rows, self.asset, self.loaded = [], '', False
        root = QVBoxLayout(self); root.setSpacing(SPACE['gap']); root.addWidget(self.toast)
        controls = QHBoxLayout(); self.query = _input('搜索表情描述和标签'); search = _button('搜索'); imp = _button('导入图片')
        controls.addWidget(self.query); controls.addWidget(search); controls.addWidget(imp); root.addLayout(controls)
        split = QSplitter(Qt.Horizontal)
        self.list = QListWidget(); self.list.setIconSize(QSize(80, 80)); self.list.setWordWrap(True); split.addWidget(self.list)
        scroll, right = _panel(self, '表情与使用设置')
        self.preview = _label('选择一个表情查看'); self.preview.setAlignment(Qt.AlignCenter); right.body.addWidget(self.preview)
        self.description = QPlainTextEdit(); self.description.setMaximumHeight(100); self.description.setPlaceholderText('画面、文字和适合的聊天情绪')
        self.tags = _input('标签，以逗号分隔'); right.body.addWidget(self.description); right.body.addWidget(self.tags)
        actions = QHBoxLayout(); self.save_btn = _button('保存描述', primary=True); delete = _button('删除并阻止再次收藏')
        actions.addWidget(self.save_btn); actions.addWidget(delete); actions.addStretch(); right.body.addLayout(actions)
        self.collect = QCheckBox('自动判断并收藏 QQ 中的表情包'); self.reply = QCheckBox('允许 AI 根据语境选择表情回复')
        right.body.addWidget(self.collect); right.body.addWidget(self.reply)
        form = QFormLayout(); self.capacity = QSpinBox(); self.capacity.setRange(10, 5000); self.cooldown = QSpinBox(); self.cooldown.setRange(30, 86400); self.cooldown.setSuffix(' 秒')
        for widget in (self.capacity, self.cooldown): widget.setMaximumWidth(220)
        form.addRow('表情容量', self.capacity); form.addRow('同一聊天发送间隔', self.cooldown); right.body.addLayout(form)
        settings = _button('应用使用设置'); right.body.addWidget(settings)
        note = _label('普通生活照不会因“收到图片”就直接入库；自动收藏先经过图片分类。发送时还会检查冷却时间、每日上限和本地阻断规则。', 'Caption'); note.setWordWrap(True); right.body.addWidget(note); right.body.addStretch()
        split.addWidget(scroll); split.setSizes([420, 640]); root.addWidget(split, 1)
        self.list.currentRowChanged.connect(self._select); search.clicked.connect(self.refresh); self.query.returnPressed.connect(self.refresh)
        imp.clicked.connect(self._import); self.save_btn.clicked.connect(self._save); delete.clicked.connect(self._delete); settings.clicked.connect(self._settings)

    def store(self):
        from emoji_store import EmojiStore
        return EmojiStore(self.services.root)

    def refresh(self):
        if self.loading: return
        query, store = self.query.text(), self.store()
        self._submit(lambda: {'rows': store.list(query), 'config': store.config()}, self._render)

    def _render(self, data):
        self.rows = data['rows']; self.list.blockSignals(True); self.list.clear()
        for row in self.rows:
            item = QListWidgetItem(QIcon(row['path']), row['description'][:80] + '\n' + row['tags']); self.list.addItem(item)
            if row['id'] == self.asset: self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        if not self.loaded:
            cfg = data['config']; self.collect.setChecked(cfg['auto_collect']); self.reply.setChecked(cfg['auto_reply']); self.capacity.setValue(cfg['capacity']); self.cooldown.setValue(cfg['cooldown_seconds']); self.loaded = True
        if not self.rows: self._toast('表情库为空。导入喜欢的表情，或在 QQ 收到表情后等待自动分类。')

    def _select(self, index):
        if not 0 <= index < len(self.rows): return
        row = self.rows[index]; self.asset = row['id']; self.preview.setPixmap(QPixmap(row['path']).scaled(320, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation)); self.description.setPlainText(row['description']); self.tags.setText(row['tags'])

    def _import(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '导入表情', '', '图片 (*.png *.jpg *.jpeg *.gif *.webp)')
        if not paths: return
        def work():
            store = self.store()
            return [store.add(p, Path(p).stem, source='manual') for p in paths]
        self._submit(work, lambda _: self._reload('已导入并去重。可以编辑描述和标签，帮助 AI 选择。'))

    def _reload(self, message):
        self._toast(message); self.loading = False; self.refresh()

    def _save(self):
        if not self.asset or self.loading: return
        asset, description, tags = self.asset, self.description.toPlainText(), self.tags.text()
        self._submit(lambda: self.store().update(asset, description, tags), lambda _: self._reload('描述已保存'))

    def _delete(self):
        if not self.asset or self.loading: return
        asset = self.asset
        def done(_): self.asset = ''; self.preview.clear(); self.description.clear(); self.tags.clear(); self._reload('已删除，并禁止自动再次收藏。')
        self._submit(lambda: self.store().delete(asset), done)

    def _settings(self):
        values = {'auto_collect': self.collect.isChecked(), 'auto_reply': self.reply.isChecked(), 'capacity': self.capacity.value(), 'cooldown_seconds': self.cooldown.value()}
        self._submit(lambda: self.store().configure(values), lambda _: self._toast('使用设置已应用'))


class ResourcesPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        root = QVBoxLayout(self); root.setContentsMargins(SPACE['page'], SPACE['page'], SPACE['page'], SPACE['page']); root.setSpacing(SPACE['section'])
        self._header('资源管理', '让角色实际使用的插件、长期记忆和表情库。QQ 与研究的记忆分别保存。', root)
        self.tabs = QTabWidget(); self.tabs.addTab(PluginsPage(services), '插件'); self.tabs.addTab(MemoryPage(services), '长期记忆'); self.tabs.addTab(EmojiPage(services), '表情库')
        root.addWidget(self.tabs, 1); self.tabs.currentChanged.connect(lambda _: self.refresh())

    def refresh(self):
        page = self.tabs.currentWidget()
        if not page.loading and not page.loaded: page.refresh()
