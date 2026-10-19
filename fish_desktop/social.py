"""Local persona revision editor and explicit per-group participation controls."""
import time
import json
import difflib
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
    QLineEdit, QPlainTextEdit, QSpinBox, QTabWidget, QVBoxLayout, QWidget, QScrollArea, QLayout, QFrame, QDialog)
from .pages import BasePage, _button, _label
from .widgets import Card


class SocialPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        self.selected = ''
        self.dirty = False
        self._drafts = {}
        root = QVBoxLayout(self); root.setContentsMargins(28, 26, 28, 24)
        self._header('人格与群聊', '先在独立窗口和主人 QQ 测试，再由主人认可具体版本。群发言单独开启。', root)
        root.addWidget(self.toast)
        self.tabs = QTabWidget(); root.addWidget(self.tabs, 1)
        persona = QWidget(); layout = QVBoxLayout(persona)
        top = QHBoxLayout(); self.versions = QComboBox(); self.versions.setMinimumWidth(340)
        top.addWidget(self.versions, 1)
        self.reload_btn = _button('刷新版本'); top.addWidget(self.reload_btn)
        layout.addLayout(top)
        self.state = _label('', 'Muted'); self.state.setTextFormat(Qt.PlainText); self.state.setWordWrap(True); layout.addWidget(self.state)
        self.name = QLineEdit(); self.name.setPlaceholderText('人格名称'); layout.addWidget(self.name)
        output_row = QHBoxLayout(); output_row.addWidget(_label('QQ 测试输出', 'Caption'))
        self.reply_mode = QComboBox()
        self.reply_mode.addItem('只发正式回复', 'final')
        self.reply_mode.addItem('思考片段（最多 160 字）＋正式回复', 'thinking_excerpt')
        self.reply_mode.setToolTip('思考片段取自 API 的实际返回，用于主人私聊测试或主人明确开启的群测试。正式群聊仍只发正文。保存后形成新版本。')
        output_row.addWidget(self.reply_mode, 1); layout.addLayout(output_row)
        self.reply_mode.currentIndexChanged.connect(self._edited)
        self.edit_tabs = QTabWidget(); layout.addWidget(self.edit_tabs, 1)
        self.body = QPlainTextEdit(); self.global_rules = QPlainTextEdit(); self.workspace_rules = QPlainTextEdit()
        for title, editor in (('本窗口人格', self.body), ('共同规则快照', self.global_rules), ('工作区规则快照', self.workspace_rules)):
            self.edit_tabs.addTab(editor, title)
            editor.textChanged.connect(self._edited)
        self.name.textEdited.connect(self._edited)
        note = _label('保存会创建新的待测版本。已有窗口和已发布版本保持固定；认可需使用主人 QQ 的 /认可人格 命令。', 'Caption')
        note.setWordWrap(True); layout.addWidget(note)
        actions = QHBoxLayout(); self.test_btn = _button('新建本地测试窗口'); actions.addWidget(self.test_btn)
        self.command_btn = _button('主人 QQ 测试指令'); actions.addWidget(self.command_btn); actions.addStretch()
        self.diff_btn=_button('对比原版本');actions.insertWidget(2,self.diff_btn)
        self.input_btn=_button('查看主人测试输入');actions.insertWidget(3,self.input_btn)
        self.save_btn = _button('另存为待测版本', primary=True); actions.addWidget(self.save_btn); layout.addLayout(actions)
        self.tabs.addTab(persona, '人格版本')
        group = QWidget(); gl = QVBoxLayout(group)
        card = Card('按群设置'); form = QFormLayout(); card.body.addLayout(form); gl.addWidget(card)
        self.groups = QComboBox(); form.addRow('白名单群', self.groups)
        self.listen = QCheckBox('允许旁听并保存有界上下文'); form.addRow('旁听', self.listen)
        self.learn = QCheckBox('从新旁听文字提取候选记忆，确认后才用于回复'); form.addRow('学习',self.learn)
        self.mode = QComboBox(); self.mode.addItem('熟悉群友：合适时接话', 'familiar'); self.mode.addItem('只回应提及和回复', 'mention'); form.addRow('参与方式', self.mode)
        self.reply_limits = QCheckBox('启用每分钟、每小时及无人接续的次数上限')
        form.addRow('回复次数限制', self.reply_limits)
        self.numbers = {}
        for key, title, lo, hi in (('debounce_s', '消息合并等待（秒）', 0, 60), ('active_cooldown_s', '主动发言冷却（秒）', 0, 86400),
                ('max_active_hour', '每小时主动发言上限', 0, 120), ('max_total_min', '每分钟全部回复上限', 1, 60), ('max_unanswered_active', '无人接续时最多主动发言', 0, 10)):
            spin = QSpinBox(); spin.setRange(lo, hi); self.numbers[key] = spin; form.addRow(title, spin)
        self.reply_limits.toggled.connect(lambda enabled: [self.numbers[k].setEnabled(enabled) for k in ('max_active_hour','max_total_min','max_unanswered_active')])
        self.quiet_start = QLineEdit(); self.quiet_start.setPlaceholderText('例如 23:00，留空不设静默')
        self.quiet_end = QLineEdit(); self.quiet_end.setPlaceholderText('例如 08:00，与开始同时填写')
        form.addRow('主动静默开始', self.quiet_start); form.addRow('主动静默结束', self.quiet_end)
        self.deployment = _label('', 'Muted'); self.deployment.setWordWrap(True); card.body.addWidget(self.deployment)
        row = QHBoxLayout(); self.group_save = _button('保存旁听与频率', primary=True); self.pause = _button('停止群发言', danger=True)
        row.addWidget(self.group_save); row.addWidget(self.pause); row.addStretch(); card.body.addLayout(row)
        self.clear_context = _button('清理此群旁听记录'); row.insertWidget(2,self.clear_context)
        self.publish = _button('将当前已认可版本发布到此群并开启发言'); card.body.addWidget(self.publish)
        card.body.addWidget(_label('保存设置不会打开群回复。关闭次数限制后不按分钟或小时限次；主动冷却和静默时段仍生效。明确提问独立处理并引用原消息。', 'Caption'))
        self.group_log = QPlainTextEdit(); self.group_log.setReadOnly(True); self.group_log.setPlaceholderText('群决策记录将在模拟与运行后显示。'); gl.addWidget(self.group_log, 1)
        gl.setSizeConstraint(QLayout.SetMinimumSize)
        group_scroll=QScrollArea();group_scroll.setWidgetResizable(True);group_scroll.setFrameShape(QFrame.NoFrame);group_scroll.setWidget(group)
        self.tabs.addTab(group_scroll, '群聊参与')
        from .members import MembersPanel
        self.members = MembersPanel(services); self.tabs.addTab(self.members, '成员管理')
        self.tabs.currentChanged.connect(lambda index: self.members.refresh() if index==2 else None)
        self.reload_btn.clicked.connect(lambda: self._load_versions(force=True))
        self.versions.currentIndexChanged.connect(self._load_persona)
        self.save_btn.clicked.connect(self._save_persona); self.test_btn.clicked.connect(self._test)
        self.command_btn.clicked.connect(self._commands)
        self.diff_btn.clicked.connect(self._diff);self.input_btn.clicked.connect(self._owner_input)
        self.groups.currentIndexChanged.connect(self._load_group)
        self.group_save.clicked.connect(self._save_group); self.pause.clicked.connect(self._pause); self.publish.clicked.connect(self._publish)
        self.clear_context.clicked.connect(self._clear_context)

    def _edited(self):
        self.dirty = True
        self.state.setText('编辑中：保存后生成新版本；当前窗口和群发布版本不会被覆盖。')

    def refresh(self):
        if not self.versions.count():
            self._load_versions()
        # Do not replace editor contents while the user is editing.

    def _load_versions(self, force=False):
        from social_context import SocialContext
        store = SocialContext(self.services.root)
        rows = store.personas() or [store.ensure_default('qq')]
        current = self.selected
        self.versions.blockSignals(True); self.versions.clear()
        for p in rows:
            self.versions.addItem(f"{p['name']} · {p['id'][:8]} · {'主人已认可' if p['approved'] else '待测'}", p['id'])
        index = self.versions.findData(current); self.versions.setCurrentIndex(max(0, index)); self.versions.blockSignals(False)
        self._load_persona()
        from storage import read_json
        selected = self.groups.currentData(); self.groups.blockSignals(True); self.groups.clear()
        for gid in read_json(self.services.root / 'channels/fish_qq/config.json').get('allowlist', {}).get('group_ids', []):
            self.groups.addItem(str(gid), str(gid))
        self.groups.setCurrentIndex(max(0, self.groups.findData(selected))); self.groups.blockSignals(False); self._load_group()

    def _load_persona(self):
        if not self.versions.currentData(): return
        if self.selected and self.dirty:
            self._drafts[self.selected] = (self.name.text(),self.body.toPlainText(),self.global_rules.toPlainText(),self.workspace_rules.toPlainText(),self.reply_mode.currentData())
        from social_context import SocialContext
        p = SocialContext(self.services.root).persona(self.versions.currentData()); self.selected = p['id']
        name,body,common,workspace,mode = self._drafts.get(p['id'],(p['name'],p['body'],p['global_rules'],p['workspace_rules'],p.get('owner_reply_mode','final')))
        for editor, value in ((self.body, body), (self.global_rules, common), (self.workspace_rules, workspace)):
            editor.blockSignals(True); editor.setPlainText(value); editor.blockSignals(False)
        self.name.setText(name); self.dirty = p['id'] in self._drafts
        self.reply_mode.blockSignals(True); self.reply_mode.setCurrentIndex(max(0,self.reply_mode.findData(mode))); self.reply_mode.blockSignals(False)
        self.state.setText(f"版本 {p['id']}\n配置摘要 {p['digest'][:16]} · {'主人已认可' if p['approved'] else '等待主人测试与认可'}")

    def _save_persona(self):
        from social_context import SocialContext
        try:
            p = SocialContext(self.services.root).save_persona('qq', self.name.text(), self.body.toPlainText(), parent=self.selected,
                global_rules=self.global_rules.toPlainText(), workspace_rules=self.workspace_rules.toPlainText(), owner_reply_mode=self.reply_mode.currentData())
            self._drafts.pop(self.selected,None); self.dirty=False;self.selected = p['id']; self._load_versions(); self._toast('已创建待测版本。先在主人 QQ 测试并认可。')
        except Exception as exc: self._toast(str(exc))

    def _test(self):
        if self.dirty:
            self._toast('请先保存为待测版本，再建立对应测试窗口。'); return
        cid = self.services.chat('qq').new('人格测试 · ' + self.name.text(), revision=self.selected)
        self._toast('已创建独立本地窗口 ' + cid[:8] + '，可到“对话”中打开；主人 QQ 验收仍需单独完成。')

    def _commands(self):
        self._toast('主人 QQ 私聊发送：/新建测试 ' + self.selected + '；聊天后满意再发 /认可人格 ' + self.selected + '。/测试窗口 查看，/结束测试 返回日常。')

    def _viewer(self,title):
        dialog=QDialog(self);dialog.setWindowTitle(title);dialog.resize(960,680)
        layout=QVBoxLayout(dialog);editor=QPlainTextEdit();editor.setReadOnly(True);layout.addWidget(editor)
        close=_button('关闭');close.clicked.connect(dialog.accept);layout.addWidget(close)
        return dialog,layout,editor

    def _diff(self):
        from social_context import SocialContext
        store=SocialContext(self.services.root);p=store.persona(self.selected)
        parent=store.persona(p['parent']) if p['parent'] else {'body':'','global_rules':'','workspace_rules':''}
        dialog,layout,editor=self._viewer('人格版本对比')
        before='\n'.join(parent[k] for k in ('body','global_rules','workspace_rules'))
        after='\n'.join((self.body.toPlainText(),self.global_rules.toPlainText(),self.workspace_rules.toPlainText()))
        before += '\n主人测试输出：' + parent.get('owner_reply_mode','final')
        after += '\n主人测试输出：' + str(self.reply_mode.currentData())
        editor.setPlainText(''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile=p['parent'] or '初始空版本',tofile=self.selected+('（含未保存草稿）' if self.dirty else ''))))
        dialog.exec()

    def _owner_input(self):
        from social_context import SocialContext
        from request_inspector import RequestInspector
        spaces=[s for s in SocialContext(self.services.root).spaces(origin='owner-qq') if s['revision']==self.selected]
        if not spaces:self._toast('此版本还没有主人 QQ 测试窗口。先用 /新建测试 建立。');return
        dialog,layout,editor=self._viewer('主人 QQ 测试 · 实际输入检查')
        choice=QComboBox();layout.insertWidget(0,choice)
        for s in spaces:choice.addItem(s['name']+' · '+s['id'][:8],s['session'])
        def show():editor.setPlainText(json.dumps(RequestInspector(self.services.root).latest('qq',choice.currentData() or ''),ensure_ascii=False,indent=2))
        choice.currentIndexChanged.connect(show);show();dialog.exec()

    def _load_group(self):
        gid = self.groups.currentData()
        for button in (self.group_save, self.pause, self.publish,self.clear_context): button.setEnabled(bool(gid))
        if not gid: self.deployment.setText('先在系统设置保存白名单群。'); return
        from group_policy import GroupPolicy
        from social_context import SocialContext
        from group_tests import GroupTests
        tests = GroupTests(self.services.root); trial = tests.current(gid)
        cfg = (tests.policy(trial) if trial else GroupPolicy(self.services.root)).config('group:' + gid)
        self.listen.setChecked(cfg['listen']); self.mode.setCurrentIndex(max(0, self.mode.findData(cfg['mode'])))
        self.learn.setChecked(cfg['learn'])
        self.reply_limits.setChecked(cfg['reply_limits'])
        for k, spin in self.numbers.items(): spin.setValue(cfg[k])
        for k in ('max_active_hour','max_total_min','max_unanswered_active'):self.numbers[k].setEnabled(cfg['reply_limits'])
        self.quiet_start.setText(cfg['quiet_start']); self.quiet_end.setText(cfg['quiet_end'])
        dep = SocialContext(self.services.root).deployment(gid)
        self.deployment.setText(('群发言已开启 · 人格 ' + dep['revision'][:8]) if dep['enabled'] else '群发言已停止；' + dep.get('reason', '发布记录保留。'))
        if trial:
            self.deployment.setText('群测试进行中 · 人格 ' + trial['revision'][:8] + ' · 窗口 ' + trial['id'][:8] + '；长期学习关闭。')
        from group_workflow import GroupWorkflow
        stages={'judge':'判断话题','skip':'继续旁听','silent':'继续旁听','cancel':'撤销候选','generate':'正在生成','sent':'已发送','failed':'未完成'}
        self.group_log.setPlainText('\n'.join(time.strftime('%H:%M:%S',time.localtime(r['created']))+' · '+stages.get(r['stage'],r['stage'])+' · '+r['reason']
            for r in GroupWorkflow(self.services.root).decisions('group:'+gid)))

    def _save_group(self):
        from group_policy import GroupPolicy
        try:
            cfg = {'listen': self.listen.isChecked(), 'learn':self.learn.isChecked(), 'reply_limits':self.reply_limits.isChecked(), 'mode': self.mode.currentData(), 'quiet_start': self.quiet_start.text().strip(), 'quiet_end': self.quiet_end.text().strip(), **{k: v.value() for k, v in self.numbers.items()}}
            GroupPolicy(self.services.root).configure('group:' + self.groups.currentData(), cfg)
            from group_tests import GroupTests
            tests = GroupTests(self.services.root); trial = tests.current(self.groups.currentData())
            if trial:
                tests.policy(trial).configure('group:' + self.groups.currentData(), {**cfg, 'learn': False})
            self._load_group(); self._toast('已保存此群的旁听与频率。')
        except Exception as exc: self._toast(str(exc))

    def _pause(self):
        from group_policy import GroupPolicy
        from social_context import SocialContext
        gid = self.groups.currentData()
        GroupPolicy(self.services.root).configure('group:' + gid, {'reply': False}); SocialContext(self.services.root).pause_group(gid)
        from group_tests import GroupTests
        tests = GroupTests(self.services.root)
        tests.leave(gid, SocialContext(self.services.root).owner())
        self._load_group(); self._toast('已停止此群发言，发送前会再次核对。')

    def _clear_context(self):
        from group_policy import GroupPolicy
        GroupPolicy(self.services.root).clear('group:'+self.groups.currentData())
        from group_tests import GroupTests
        tests=GroupTests(self.services.root); trial=tests.current(self.groups.currentData())
        if trial: tests.policy(trial).clear('group:'+self.groups.currentData())
        self._load_group();self._toast('已清理此群旁听原文与摘要，并撤销在途候选。频率计数保留；长期记忆请到资源管理中处理。')

    def _publish(self):
        from group_policy import GroupPolicy
        from social_context import SocialContext
        try:
            if self.dirty: raise ValueError('当前编辑尚未保存，不能发布')
            gid = self.groups.currentData(); contexts = SocialContext(self.services.root)
            # Exact revision approval can only be obtained through owner QQ.
            contexts.deploy(gid, self.selected, contexts.owner(), enabled=True)
            GroupPolicy(self.services.root).configure('group:' + gid, {'listen': True, 'reply': True})
            self._load_group(); self._toast('已将主人认可的此版本发布到指定群。')
        except Exception as exc: self._toast(str(exc))
