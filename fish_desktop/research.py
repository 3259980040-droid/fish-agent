"""Research flow: queue/results first, owner directions second, account third.

Refactoring UI §2.1/§2.8/§3.6: one primary '逛一轮' action; task state and
selected report outrank provenance. Existing page/section/gap tokens govern
spacing. Settings have one Save action and remain untouched by status polling.
"""
import json
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
    QListWidget, QPlainTextEdit, QScrollArea, QSpinBox, QSplitter, QTabWidget,
    QTextBrowser, QVBoxLayout, QWidget)
from .pages import BasePage, Table, _button, _label
from .configuration import _input
from .theme import SPACE
from .widgets import Card


def _control(root):
    from research_autonomy import ResearchControl
    return ResearchControl(root)


class InterestsPage(BasePage):
    def __init__(self, services):
        super().__init__(services); self.loaded = False
        root = QVBoxLayout(self); root.setSpacing(SPACE['section']); root.addWidget(self.toast)
        card = Card('主人指定的兴趣', '兴趣方向由你决定；研究反馈只调整这些方向内的排序，不会替你改写方向。')
        self.fields = {}
        form = QFormLayout(); form.setVerticalSpacing(SPACE['gap'])
        for key, title, hint in [('directions', '兴趣方向', '例如：AI 产品的实际应用、游戏设计'), ('keywords', '搜索关键词', '每行一个；未填时使用兴趣方向搜索'), ('exclude', '排除关键词', '每行一个；优先于推荐理由'), ('ups', '指定 UP 主', '每行一个数字 UID')]:
            field = QPlainTextEdit(); field.setPlaceholderText(hint); field.setMaximumHeight(80); field.setMinimumWidth(300)
            self.fields[key] = field; form.addRow(title, field)
        card.body.addLayout(form)
        source_row = QHBoxLayout(); self.sources = {}
        for key, label in [('search', '关键词搜索'), ('ups', '指定 UP'), ('favorites', '选定收藏夹'), ('recommendations', '首页推荐')]:
            w = QCheckBox(label); self.sources[key] = w; source_row.addWidget(w)
        source_row.addStretch(); card.body.addLayout(source_row)
        self.auto = QCheckBox('自动按计划发现并研究新内容'); card.body.addWidget(self.auto)
        limits = QFormLayout(); limits.setVerticalSpacing(SPACE['gap']); self.numbers = {}
        for key, label, low, high, suffix in [('interval_minutes', '发现间隔', 30, 10080, ' 分钟'), ('daily_limit', '每天最多处理', 1, 100, ' 条'), ('per_run_limit', '每轮最多新增', 1, 30, ' 条'), ('max_video_minutes', '单个视频时长上限', 1, 240, ' 分钟'), ('max_task_minutes', '每条任务运行上限', 1, 120, ' 分钟')]:
            w = QSpinBox(); w.setRange(low, high); w.setSuffix(suffix); w.setMaximumWidth(220); self.numbers[key] = w; limits.addRow(label, w)
        card.body.addLayout(limits); self.save_btn = _button('保存兴趣与计划', primary=True); actions = QHBoxLayout(); actions.addWidget(self.save_btn); actions.addStretch(); card.body.addLayout(actions)
        root.addWidget(card); root.addStretch(); self.save_btn.clicked.connect(self._save)

    def refresh(self):
        if not self.loaded and not self.loading: self._submit(lambda: _control(self.services.root).config(), self._apply)

    def _apply(self, cfg):
        self.loaded = True
        for key, w in self.fields.items(): w.setPlainText('\n'.join(cfg[key]))
        for key, w in self.sources.items(): w.setChecked(key in cfg['sources'])
        for key, w in self.numbers.items(): w.setValue(cfg[key])
        self.auto.setChecked(cfg['auto'])

    def _save(self):
        if self.loading: return
        values = {key: w.toPlainText().splitlines() for key, w in self.fields.items()}
        values.update({key: w.value() for key, w in self.numbers.items()}); values.update(auto=self.auto.isChecked(), sources=[key for key, w in self.sources.items() if w.isChecked()])
        def work():
            result = _control(self.services.root).configure(values)
            from tool_host import ensure_host
            ensure_host(self.services.root)
            return result
        self._submit(work, lambda _: self._toast('兴趣和计划已保存；不会向 B 站发评论或私信。'))


class BiliAccountPage(BasePage):
    def __init__(self, services):
        super().__init__(services); self.loaded = False; self.login = None; self.polling = False
        root = QVBoxLayout(self); root.setSpacing(SPACE['section']); root.addWidget(self.toast)
        card = Card('B 站账号与收藏夹', '登录用于读取个人收藏和推荐。扫码确认后凭证在本机加密保存。')
        self.status = _label('尚未检查登录状态', 'Muted'); card.body.addWidget(self.status)
        actions = QHBoxLayout(); self.login_btn = _button('扫码登录', primary=True); self.check_btn = _button('检查登录'); self.favorites_btn = _button('读取收藏夹')
        for w in (self.login_btn, self.check_btn, self.favorites_btn): actions.addWidget(w)
        actions.addStretch(); card.body.addLayout(actions)
        self.qr = _label(''); self.qr.setAlignment(Qt.AlignLeft); card.body.addWidget(self.qr)
        row = QHBoxLayout(); self.favorites = QComboBox(); self.favorites.setMinimumWidth(300); select = _button('使用此收藏夹')
        row.addWidget(self.favorites); row.addWidget(select); row.addStretch(); card.body.addLayout(row)
        self.selected = _label('', 'Caption'); card.body.addWidget(self.selected); root.addWidget(card); root.addStretch()
        self.login_btn.clicked.connect(self._start); self.check_btn.clicked.connect(self._status); self.favorites_btn.clicked.connect(self._favorites); select.clicked.connect(self._select)
        self.timer = QTimer(self); self.timer.setInterval(3000); self.timer.timeout.connect(self._poll)

    def refresh(self):
        if not self.loaded and not self.loading: self._status()

    def _status(self):
        self.loaded = True
        from bilibili_source import BilibiliSourceAdapter
        def done(data):
            self.loaded = True; self.status.setText(('已登录：' + data['name'] + ' · UID ' + data['uid']) if data['logged_in'] else '未登录。搜索公开内容通常仍可使用，读取个人收藏需要登录。')
            from research_store import ResearchStore
            fav = ResearchStore(self.services.root).selected_favorite()
            self.selected.setText('当前收藏夹：' + (fav['title'] if fav else '尚未选择'))
        self._submit(lambda: BilibiliSourceAdapter(self.services.root).login_status(), done)

    def _start(self):
        if self.loading: return
        from bili_login import BiliLogin
        self.login = BiliLogin(self.services.root)
        def done(result):
            self.qr.setPixmap(QPixmap(result['path']).scaled(240, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation)); self.status.setText('请用 B 站客户端扫码，并在手机上确认登录。'); self.timer.start()
        self._submit(self.login.start, done)

    def _poll(self):
        if self.polling or not self.login: return
        self.polling = True
        def done(result):
            state = result['state']; self.status.setText({'waiting': '等待扫码', 'scanned': '已扫码，等待手机确认', 'expired': '二维码已过期，请重新生成', 'done': '登录完成', 'unknown': '登录状态暂时未知'}.get(state, state))
            if state in ('done', 'expired'):
                self.timer.stop(); self.qr.clear()
                if state == 'done': self._status()
        self.runner.submit(self.login.poll, done, lambda message: self._toast(message), lambda: setattr(self, 'polling', False))

    def _favorites(self):
        if self.loading: return
        from bilibili_source import BilibiliSourceAdapter
        def done(rows):
            self.favorites.clear()
            for row in rows: self.favorites.addItem(row['title'] + ' · ' + str(row['media_count']) + ' 条', row)
            if not rows: self._toast('没有可读取的收藏夹，请检查登录状态。')
        self._submit(lambda: BilibiliSourceAdapter(self.services.root).list_favorites(), done)

    def _select(self):
        row = self.favorites.currentData()
        if not row: return
        from research_store import ResearchStore
        self._submit(lambda: ResearchStore(self.services.root).select_favorite(row['media_id'], row['title']), lambda _: self.selected.setText('当前收藏夹：' + row['title']))


class ResearchPage(BasePage):
    followup = Signal(str)

    def __init__(self, services):
        super().__init__(services); self.data = {}; self.bvid = ''; self._fingerprint = None
        root = QVBoxLayout(self); root.setContentsMargins(SPACE['page'], SPACE['page'], SPACE['page'], SPACE['page']); root.setSpacing(SPACE['gap'])
        header = self._header('视频研究', '按你指定的方向发现内容，保留来源，逐步形成记忆和看法。', root)
        self.run_btn = _button('现在逛一轮', primary=True); header.addWidget(self.run_btn)
        root.addWidget(self.toast)
        self.tabs = QTabWidget(); root.addWidget(self.tabs, 1)
        work = QWidget(); layout = QVBoxLayout(work); layout.setSpacing(SPACE['gap'])
        toolbar = QHBoxLayout(); self.state = _label('等待加载', 'Muted'); toolbar.addWidget(self.state, 1)
        pause = _button('暂停'); resume = _button('继续'); self.stop_btn = _button('停止所选任务'); retry = _button('重试所选任务')
        for w in (pause, resume, self.stop_btn, retry): toolbar.addWidget(w)
        layout.addLayout(toolbar)
        add = QHBoxLayout(); self.target = _input('粘贴 BV 号或 bilibili.com/video/BV… 链接，直接指定要研究的视频'); add_btn = _button('加入任务')
        add.addWidget(self.target); add.addWidget(add_btn); layout.addLayout(add)
        split = QSplitter(Qt.Vertical); self.jobs = Table(['视频', '状态', '当前步骤', '发现理由']); split.addWidget(self.jobs)
        report_card = Card('研究详情')
        self.report = QTextBrowser(); self.report.setOpenExternalLinks(False); self.report.setMarkdown('选择一条任务查看研究结果。\n\n还没有任务时，先在“兴趣与计划”里填写方向，再点“现在逛一轮”；也可以直接加入一个 BV 号。')
        report_card.body.addWidget(self.report, 1)
        feedback = QHBoxLayout(); self.reason = _input('可选：反馈原因'); feedback.addWidget(self.reason, 1)
        self.like, self.dislike, self.ask = _button('更想看这类'), _button('少看这类'), _button('在聊天中继续问')
        for w in (self.like, self.dislike, self.ask): feedback.addWidget(w)
        report_card.body.addLayout(feedback); split.addWidget(report_card); split.setSizes([260, 460]); layout.addWidget(split, 1)
        self.tabs.addTab(work, '任务与研究')
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame); self.interests = InterestsPage(services); scroll.setWidget(self.interests); self.tabs.addTab(scroll, '兴趣与计划')
        self.account = BiliAccountPage(services); self.tabs.addTab(self.account, '账号与收藏夹')
        self.run_btn.clicked.connect(self._discover); add_btn.clicked.connect(self._enqueue)
        pause.clicked.connect(lambda: self._command('/research/pause', {})); resume.clicked.connect(lambda: self._command('/research/resume', {})); self.stop_btn.clicked.connect(self._stop); retry.clicked.connect(self._retry)
        self.jobs.itemSelectionChanged.connect(self._selected); self.tabs.currentChanged.connect(lambda _: self.refresh())
        self.like.clicked.connect(lambda: self._feedback(1)); self.dislike.clicked.connect(lambda: self._feedback(-1)); self.ask.clicked.connect(self._ask)

    def refresh(self):
        if self.tabs.currentIndex() == 1: self.interests.refresh(); return
        if self.tabs.currentIndex() == 2: self.account.refresh(); return
        if not self.loading: self._submit(lambda: _control(self.services.root).snapshot(), self._render)

    def _render(self, data):
        selected_bvid = self.bvid
        self.data = data; cfg = data['config']
        self.state.setText(('已暂停' if cfg['paused'] else '自动研究已开启' if cfg['auto'] else '手动运行') + f" · 每天最多 {cfg['daily_limit']} 条 · {len(data['reviews'])} 份研究")
        rows = data['jobs']; self.jobs.blockSignals(True)
        labels = {'queued': '待处理', 'running': '进行中', 'done': '已完成', 'failed': '失败', 'paused': '已暂停', 'cancelled': '已停止', 'interrupted': '上次中断'}
        self.jobs.fill([[r['title'] or r['bvid'], labels.get(r['status'], r['status']), r['stage'], r['origin']] for r in rows], [320, 90, 220, 220])
        self.jobs.blockSignals(False)
        for index, job in enumerate(rows):
            if job['bvid'] == selected_bvid:
                self.jobs.selectRow(index); self._selected(); break
        if cfg.get('last_error'): self._toast('上次发现失败：' + cfg['last_error'])

    def _command(self, route, body, timeout=15):
        if self.loading: return
        def work():
            from tool_host import ensure_host, request
            ensure_host(self.services.root)
            return request(self.services.root, route, body, role='research', timeout=timeout)
        def done(result):
            self._toast('操作已完成' + (f"，新增 {result['added']} 条任务" if 'added' in result else '')); self.loading = False; self.refresh()
        self._submit(work, done)

    def _discover(self): self._command('/research/discover', {}, 180)

    def _enqueue(self):
        import re
        match = re.search(r'BV[0-9A-Za-z]{10}', self.target.text())
        if not match: self._toast('请输入有效 BV 号或视频链接'); return
        bvid = match.group()
        def work():
            result = _control(self.services.root).enqueue({'bvid': bvid}, origin='主人指定')
            from tool_host import ensure_host
            ensure_host(self.services.root)
            return result
        def done(result):
            self.target.clear(); self._toast('已有这条任务，未重复添加' if result.get('duplicate') else '已加入研究队列'); self.loading = False; self.refresh()
        self._submit(work, done)

    def _job(self):
        index = self.jobs.currentRow()
        return self.data.get('jobs', [])[index] if 0 <= index < len(self.data.get('jobs', [])) else None

    def _selected(self):
        job = self._job()
        if not job: return
        self.bvid = job['bvid']
        try: self.report.setMarkdown(_control(self.services.root).detail(self.bvid)['body'])
        except ValueError:
            self.report.setPlainText(job['title'] + '\n' + job['url'] + '\n\n当前步骤：' + job['stage'] + ('\n\n原因：' + job['error'] if job['error'] else '\n\n完成后将在这里显示摘要、看法、兴趣关联和证据。'))

    def _stop(self):
        job = self._job()
        if job: self._command('/research/cancel', {'id': job['id']})

    def _retry(self):
        job = self._job()
        if not job or self.loading: return
        if job['status'] == 'running': self._toast('任务仍在进行，请先停止'); return
        def done(_): self._toast('已重新排队；已有的观察资料会复用。'); self.loading = False; self.refresh()
        self._submit(lambda: _control(self.services.root).enqueue(job, origin=job['origin'], force=True), done)

    def _feedback(self, rating):
        if not self.bvid or self.loading: return
        bvid, reason = self.bvid, self.reason.text()
        self._submit(lambda: _control(self.services.root).feedback(bvid, rating, reason), lambda result: self._toast(result['message']))

    def _ask(self):
        if not self.bvid: return
        self.followup.emit(f'请读取 {self.bvid} 的研究详情，基于已取得的资料继续解释：')
