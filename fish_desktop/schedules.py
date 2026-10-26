"""Persistent native DSH tasks, with QQ delivery receipts in the same editor.

Primary: task editor and Save. Secondary: task list, timing and delivery detail.
Use existing theme tokens, Card and button hierarchy; no additional navigation.
"""
from locations import project_path
import json
from PySide6.QtCore import Qt, QDateTime
from PySide6.QtWidgets import (QComboBox,QFormLayout,QHBoxLayout,QLineEdit,
    QPlainTextEdit,QSpinBox,QDateTimeEdit,QTimeEdit,QVBoxLayout)
from .pages import BasePage,Table,_button,_label
from .widgets import Card


class SchedulePanel(BasePage):
    def __init__(self,services):
        super().__init__(services); self.rows=[]; self.selected='';self.original_timing=None
        layout=QVBoxLayout(self); layout.addWidget(self.toast)
        top=QHBoxLayout(); top.addWidget(_label('到期自动唤醒 DeepSeek，按指定目标实际发送；需要 Fish 后台与 QQ 在线。','Muted'),1)
        refresh=_button('刷新任务'); refresh.clicked.connect(self.refresh); top.addWidget(refresh); layout.addLayout(top)
        body=QHBoxLayout(); self.table=Table(['任务','目标','时间','状态']); body.addWidget(self.table,1)
        card=Card('任务设置'); form=QFormLayout();self.form=form;card.body.addLayout(form);body.addWidget(card,1);layout.addLayout(body,1)
        self.title=QLineEdit();form.addRow('名称',self.title)
        self.target=QComboBox();self.target.addItem('主人私聊','owner')
        from storage import read_json
        for gid in read_json(project_path(services.root, 'channels/fish_qq/config.json')).get('allowlist',{}).get('group_ids',[]):self.target.addItem('群 '+str(gid),'group:'+str(gid))
        form.addRow('发送到',self.target)
        self.kind=QComboBox()
        for text,key in [('延后一次','after_seconds'),('指定日期一次','at'),('每隔一段时间','every_seconds'),('每天固定时间','daily'),('每周固定时间','weekly'),('Cron 规则','cron')]:self.kind.addItem(text,key)
        form.addRow('执行方式',self.kind)
        self.seconds=QSpinBox();self.seconds.setRange(5,31536000);self.seconds.setValue(60);self.seconds.setSuffix(' 秒');form.addRow('间隔',self.seconds)
        self.at=QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600));self.at.setCalendarPopup(True);self.at.setDisplayFormat('yyyy-MM-dd HH:mm:ss');form.addRow('日期时间',self.at)
        self.daily=QTimeEdit();self.daily.setDisplayFormat('HH:mm:ss');form.addRow('每日时间',self.daily)
        self.zone=QLineEdit('Asia/Shanghai');form.addRow('时区',self.zone)
        self.weekdays=QLineEdit('1,2,3,4,5');self.weekdays.setPlaceholderText('1 为周一，7 为周日');form.addRow('星期',self.weekdays)
        self.cron=QLineEdit('0 9 * * 1-5');form.addRow('Cron',self.cron)
        self.prompt=QPlainTextEdit();self.prompt.setPlaceholderText('到点要做什么、发什么。也可以要求先联网查询再发送。');card.body.addWidget(self.prompt,1)
        buttons=QHBoxLayout();new=_button('新建');delete=_button('删除任务',danger=True);self.save=_button('保存任务',primary=True)
        buttons.addWidget(new);buttons.addWidget(delete);buttons.addStretch();buttons.addWidget(self.save);card.body.addLayout(buttons)
        self.detail=QPlainTextEdit();self.detail.setReadOnly(True);self.detail.setMaximumHeight(130);self.detail.setPlaceholderText('选中任务查看唤醒和实际发送回执');layout.addWidget(self.detail)
        self.kind.currentIndexChanged.connect(self._timing)
        new.clicked.connect(self._new);delete.clicked.connect(self._delete);self.save.clicked.connect(self._save)
        self.table.itemSelectionChanged.connect(self._select);self._timing()

    def _timing(self):
        kind=self.kind.currentData();self.seconds.setMinimum(60 if kind=='every_seconds' else 5);self.seconds.setEnabled(kind in ('after_seconds','every_seconds'));self.at.setEnabled(kind=='at');self.daily.setEnabled(kind in ('daily','weekly'));self.zone.setEnabled(kind in ('daily','at','weekly','cron'));self.weekdays.setEnabled(kind=='weekly');self.cron.setEnabled(kind=='cron')
        for widget in (self.seconds,self.at,self.daily,self.zone,self.weekdays,self.cron):
            visible=widget.isEnabled();widget.setVisible(visible);self.form.labelForField(widget).setVisible(visible)

    def refresh(self):
        if self.loading:return
        from bot_control import BotControl
        self._submit(lambda:BotControl(self.services.root).schedules(),self._loaded)

    def _loaded(self,rows):
        self.rows=rows;self.table.blockSignals(True)
        self.table.fill([[r['title'],r.get('target',''),self._when(r),{'active':'进行中','inactive':'已结束'}.get(r.get('status'),r.get('status',''))] for r in rows])
        from PySide6.QtWidgets import QHeaderView
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.blockSignals(False)
        match=next((i for i,r in enumerate(rows) if r['id']==self.selected),None)
        if match is not None:self.table.selectRow(match);self._select()

    @staticmethod
    def _when(row):
        from datetime import datetime
        instant=row.get('scheduledAt','')
        try:instant=datetime.fromisoformat(instant).astimezone().strftime('%m-%d %H:%M:%S')
        except ValueError:pass
        prefix={'after':'一次','at':'一次','every':'每 '+str(row.get('everySeconds',''))+' 秒','daily':'每天 '+row.get('time','')[:8],'weekly':'每周','cron':'Cron'}.get(row.get('kind'),'')
        return prefix+' · '+instant

    def _new(self):
        self.selected='';self.table.clearSelection();self.table.setCurrentCell(-1,-1);self.title.clear();self.prompt.clear();self.target.setEnabled(True);self.detail.clear();self._toast('新建任务：填写目标、时间和任务内容后保存。')

    def _select(self):
        index=self.table.currentRow()
        if not 0<=index<len(self.rows):return
        row=self.rows[index];self.selected=row['id'];self.title.setText(row['title']);self.prompt.setPlainText(row['prompt']);self.target.setEnabled(False)
        target=row.get('target','');self.target.setCurrentIndex(max(0,self.target.findData('owner' if target.startswith('private:') else target)))
        wake=row.get('lastDelivery',{}).get('deliveredAt','尚未触发')
        states={'SUCCEEDED':'已送达','FAILED':'发送失败','UNKNOWN':'需要核对','EXECUTING':'正在发送'}
        sent=[states.get(r['state'],r['state'])+' · 消息编号 '+str(json.loads(r['receipt']).get('payload',{}).get('message_id','')) for r in row.get('sendReceipts',[])]
        self.detail.setPlainText('最近唤醒：'+wake+'\n实际发送：'+('\n'.join(sent) if sent else '暂无发送回执'))
        spec=row;kind=row.get('kind','at')
        self.kind.setCurrentIndex(max(0,self.kind.findData({'every':'every_seconds','after':'after_seconds'}.get(kind,kind))))
        if kind in ('every','after'):self.seconds.setValue(int(spec.get('everySeconds',spec.get('afterSeconds',60))))
        if kind=='at':self.at.setDateTime(QDateTime.fromString(row['scheduledAt'],Qt.ISODate).toLocalTime())
        if kind in ('daily','weekly'):
            from PySide6.QtCore import QTime
            self.daily.setTime(QTime.fromString(spec['time'][:8],'HH:mm:ss'));self.zone.setText(spec['timeZone'])
            if kind=='weekly':self.weekdays.setText(','.join(map(str,spec['weekdays'])))
        if kind=='cron':self.cron.setText(spec['expression']);self.zone.setText(spec['timeZone'])
        self.original_timing=self._request()

    def _request(self):
        kind=self.kind.currentData()
        if kind in ('after_seconds','every_seconds'):return {kind:self.seconds.value()}
        if kind=='daily':return {'daily':{'time':self.daily.time().toString('HH:mm:ss'),'time_zone':self.zone.text().strip()}}
        if kind=='weekly':return {'weekly':{'time':self.daily.time().toString('HH:mm:ss'),'time_zone':self.zone.text().strip(),'weekdays':[int(x.strip()) for x in self.weekdays.text().split(',')]}}
        if kind=='cron':return {'cron':{'expression':self.cron.text().strip(),'time_zone':self.zone.text().strip()}}
        return {'at':{'date':self.at.date().toString('yyyy-MM-dd'),'time':self.at.time().toString('HH:mm:ss'),'time_zone':self.zone.text().strip()}}

    def _save(self):
        if self.loading:return
        from bot_control import BotControl
        title=self.title.text().strip();prompt=self.prompt.toPlainText().strip()
        if not title or not prompt:self._toast('请填写名称和任务内容');return
        try:timing=self._request()
        except ValueError:self._toast('星期请填写 1–7 的数字，用逗号分隔');return
        params={'title':title,'prompt':prompt}
        if self.selected:
            key,value=next(iter(timing.items()))
            params['id']=self.selected
            if timing!=self.original_timing:
                if key=='after_seconds':
                    value=QDateTime.currentDateTimeUtc().addSecs(value).toString(Qt.ISODateWithMs);key='at'
                params['change']={'kind':{'every_seconds':'every'}.get(key,key),key:value}
            action='schedule_update'
        else:params.update(target=self.target.currentData(),**timing);action='schedule_create'
        def saved(result):
            self.selected=result['id'];self.target.setEnabled(False);self._toast('已保存到 DSH 持久定时器。')
        self._submit(lambda:BotControl(self.services.root).execute(action,params),saved,on_done=self.refresh)

    def _delete(self):
        if self.loading or not self.selected:return
        from bot_control import BotControl
        key=self.selected
        self._submit(lambda:BotControl(self.services.root).execute('schedule_delete',{'id':key}),lambda result:self._new(),on_done=self.refresh)
