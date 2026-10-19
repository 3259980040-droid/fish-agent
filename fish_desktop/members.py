"""Owner-local member management; private scores never use a model tool."""
# refactoring-ui §2.1/§2.8/§3.6: roster is primary, selected-member form secondary,
# audit tertiary. One primary action saves the selected member. Reuse Qt cards,
# SPACE page/section/gap/small and existing theme colors; no new palette.
import re
import time
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLineEdit, QPlainTextEdit, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView, QScrollArea, QFrame)
from .pages import BasePage, _button, _label
from .theme import SPACE
from .widgets import Card
from social_roster import SocialRoster


class MembersPanel(BasePage):
    def __init__(self, services):
        super().__init__(services)
        self.roster = SocialRoster(services.root)
        self.uid = ''; self.loaded_group = ''; self.dirty = False; self.drafts = {}
        layout = QVBoxLayout(self); layout.setSpacing(SPACE['gap'])
        row = QHBoxLayout(); self.groups = QComboBox(); row.addWidget(self.groups, 1)
        self.select_btn = _button('设为管理群'); row.addWidget(self.select_btn)
        self.sync_btn = _button('同步群成员'); row.addWidget(self.sync_btn); layout.addLayout(row)
        self.summary = _label('', 'Muted'); self.summary.setWordWrap(True); layout.addWidget(self.summary)
        self.auto = QCheckBox('随群聊学习自动更新关系（锁定成员除外）'); layout.addWidget(self.auto)
        layout.addWidget(self.toast)
        splitter = QSplitter(Qt.Horizontal); layout.addWidget(splitter, 1)
        listing = QWidget(); left = QVBoxLayout(listing); left.setContentsMargins(0,0,0,0)
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索昵称、群名片或 QQ 号'); left.addWidget(self.search)
        self.table = QTableWidget(0, 5); self.table.setHorizontalHeaderLabels(['成员', 'QQ 号', '状态', '好感', '锁定'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.table.verticalHeader().hide()
        header = self.table.horizontalHeader(); header.setSectionResizeMode(0,QHeaderView.Stretch)
        for i in range(1,5): header.setSectionResizeMode(i,QHeaderView.ResizeToContents)
        left.addWidget(self.table, 1); splitter.addWidget(listing)
        details = QWidget(); right = QVBoxLayout(details); right.setContentsMargins(0,0,0,0)
        self.card = Card('成员档案'); form = QFormLayout(); self.card.body.addLayout(form); right.addWidget(self.card)
        self.identity = _label('从左侧选择成员', 'Muted'); self.identity.setTextFormat(Qt.PlainText); self.identity.setWordWrap(True)
        form.addRow('成员', self.identity)
        self.aliases = QLineEdit(); self.aliases.setPlaceholderText('多个别称用逗号分隔'); form.addRow('别称',self.aliases)
        self.score = QSpinBox(); self.score.setRange(0,100); form.addRow('好感度',self.score)
        self.locked = QCheckBox('固定此数值，暂停自动增减'); form.addRow('锁定',self.locked)
        self.preferences = QPlainTextEdit(); self.preferences.setMaximumHeight(160)
        self.preferences.setPlaceholderText('明确偏好、相处习惯和需要保留的限定；此处资料会按对象用于回复。')
        form.addRow('相处偏好',self.preferences)
        self.save_btn = _button('保存成员档案',primary=True); self.card.body.addWidget(self.save_btn)
        note = _label('好感度与变更记录仅供本地管理。普通群回复只取得长度预算等行为参数。', 'Caption')
        note.setWordWrap(True); self.card.body.addWidget(note)
        self.events = QTableWidget(0,3); self.events.setHorizontalHeaderLabels(['时间','事件','变化'])
        self.events.setEditTriggers(QAbstractItemView.NoEditTriggers); self.events.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.events.setSelectionMode(QAbstractItemView.SingleSelection); self.events.verticalHeader().hide()
        self.events.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); right.addWidget(self.events,1)
        self.undo_btn = _button('撤销选中的数值变化'); right.addWidget(self.undo_btn)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.NoFrame);scroll.setWidget(details)
        splitter.addWidget(scroll);splitter.setSizes([520,420])
        self.groups.currentIndexChanged.connect(self._group_changed)
        self.select_btn.clicked.connect(self._select_group); self.sync_btn.clicked.connect(self._sync)
        self.auto.toggled.connect(self._auto_changed); self.search.textChanged.connect(self._rows)
        self.table.itemSelectionChanged.connect(self._person); self.save_btn.clicked.connect(self._save)
        self.undo_btn.clicked.connect(self._undo)
        for signal in (self.aliases.textEdited,self.score.valueChanged,self.locked.toggled,self.preferences.textChanged): signal.connect(self._edited)
        self.card.setEnabled(False); self.undo_btn.setEnabled(False)

    def _edited(self,*_):
        if self.uid: self.dirty=True

    def _keep(self):
        if self.uid and self.dirty:
            self.drafts[(self.loaded_group,self.uid)]=(self.score.value(),self.locked.isChecked(),self.preferences.toPlainText(),self.aliases.text())

    def refresh(self):
        from storage import read_json
        groups=[str(x) for x in read_json(self.services.root/'channels/fish_qq/config.json').get('allowlist',{}).get('group_ids',[])]
        settings=self.roster.settings()
        if not settings['group_id'] and len(groups)==1: settings=self.roster.configure(groups[0])
        selected=self.groups.currentData() or settings['group_id']
        self.groups.blockSignals(True);self.groups.clear()
        for gid in groups:self.groups.addItem(gid,gid)
        self.groups.setCurrentIndex(max(0,self.groups.findData(selected)));self.groups.blockSignals(False)
        self._group_changed()

    def _group_changed(self):
        self._keep();self.uid='';self.dirty=False;self.loaded_group=self.groups.currentData() or ''
        settings=self.roster.settings();active=self.loaded_group and self.loaded_group==settings['group_id']
        self.auto.blockSignals(True);self.auto.setChecked(settings['auto_affinity']);self.auto.blockSignals(False)
        self.auto.setEnabled(bool(active));self.sync_btn.setEnabled(bool(active));self.select_btn.setEnabled(bool(self.loaded_group) and not active)
        self.summary.setText('当前管理群 '+self.loaded_group+'。同步只读取基础资料；自动更新还需开启此群的旁听和学习。' if active else '请选择白名单群并设为管理群；已有档案会保留。')
        self._rows()

    def _select_group(self):
        try:self.roster.configure(self.loaded_group,self.auto.isChecked());self._group_changed()
        except Exception as exc:self._toast(str(exc))

    def _auto_changed(self,value):
        try:self.roster.configure(self.loaded_group,bool(value));self._toast('已保存自动更新设置。')
        except Exception as exc:self._toast(str(exc))

    def _rows(self,*_):
        self._keep();uid=self.uid
        active=self.loaded_group and self.loaded_group==self.roster.settings()['group_id']
        rows=self.roster.members(self.loaded_group,self.search.text()) if active else []
        self.table.blockSignals(True);self.table.setRowCount(len(rows));index=-1
        for i,row in enumerate(rows):
            values=[row['card'] or row['nickname'] or '未命名',row['user_id'],'在群' if row['present'] else '已退群',str(row['affinity']),'已锁定' if row['locked'] else '自动']
            for j,value in enumerate(values):
                item=QTableWidgetItem(value);item.setData(Qt.UserRole,row['user_id']);self.table.setItem(i,j,item)
            if row['user_id']==uid:index=i
        self.table.blockSignals(False)
        if rows:self.table.selectRow(max(0,index));self._person()
        else:
            self.uid='';self.dirty=False;self.card.setEnabled(False);self.events.setRowCount(0);self.undo_btn.setEnabled(False)
            self.identity.setText('暂无匹配成员。连接 NapCat 后同步群成员。')

    def _person(self):
        self._keep();row=self.table.currentRow()
        if row<0:return
        self.uid=self.table.item(row,0).data(Qt.UserRole)
        p=self.roster.member(self.loaded_group,self.uid)
        values=self.drafts.get((self.loaded_group,self.uid),(p['affinity'],p['locked'],p['preferences'],', '.join(p['aliases'])))
        for control in (self.score,self.locked,self.preferences,self.aliases):control.blockSignals(True)
        self.identity.setText((p['card'] or p['nickname'] or '未命名')+' · '+self.uid)
        self.score.setValue(values[0]);self.locked.setChecked(values[1]);self.preferences.setPlainText(values[2]);self.aliases.setText(values[3])
        for control in (self.score,self.locked,self.preferences,self.aliases):control.blockSignals(False)
        self.dirty=(self.loaded_group,self.uid) in self.drafts;self.card.setEnabled(True)
        rows=self.roster.events(self.loaded_group,self.uid);self.events.setRowCount(len(rows))
        labels={'manual':'主人调整','manual_lock':'锁定设置','helpful':'提供帮助','warm':'友善互动','hostile':'直接敌意','boundary':'相处边界','neutral':'无数值变化','undo':'撤销变化'}
        for i,event in enumerate(rows):
            data=[time.strftime('%m-%d %H:%M',time.localtime(event['created'])),labels.get(event['kind'],'互动')+('（已撤销）' if event['undone'] else ''),f"{event['delta']:+d}"]
            for j,value in enumerate(data):
                item=QTableWidgetItem(value);item.setData(Qt.UserRole,event['id'] if event['delta'] and not event['undone'] and event['kind']!='undo' else '')
                self.events.setItem(i,j,item)
        self.undo_btn.setEnabled(bool(rows))

    def _save(self):
        if not self.uid:return
        try:
            aliases=[s.strip() for s in re.split('[,，]',self.aliases.text()) if s.strip()]
            self.roster.save_member(self.loaded_group,self.uid,affinity=self.score.value(),locked=self.locked.isChecked(),preferences=self.preferences.toPlainText(),aliases=aliases)
            self.drafts.pop((self.loaded_group,self.uid),None);self.dirty=False;self._rows();self._toast('已保存；后续回复按对象读取新资料。')
        except Exception as exc:self._toast(str(exc))

    def _undo(self):
        row=self.events.currentRow();item=self.events.item(row,0) if row>=0 else None
        if self.dirty:self._toast('请先保存当前成员的修改，再撤销数值变化。');return
        if not item or not item.data(Qt.UserRole):self._toast('请选择一条尚未撤销、确有数值变化的记录。');return
        try:self.roster.undo_event(self.loaded_group,item.data(Qt.UserRole));self._rows();self._toast('已撤销这次数值变化，之后的变化保留。')
        except Exception as exc:self._toast(str(exc))

    def _sync(self):
        from social_members import sync_from_channel
        gid=self.loaded_group;self.sync_btn.setEnabled(False)
        def complete(result):
            if self.loaded_group==gid:self._rows()
            self._toast(f"同步完成：{result['total']} 人，新增 {result['added']} 人，离群 {result['left']} 人；已有档案保留。")
        self._submit(lambda:sync_from_channel(self.services.root,gid),complete,on_done=lambda:self.sync_btn.setEnabled(self.loaded_group==self.roster.settings()['group_id']))
