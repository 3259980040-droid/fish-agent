"""Daily workspace. Role-local actions inspired by NapCat's instance cards."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QLabel, QWidget, QGridLayout

from .pages import BasePage, _button, _label, status_cn
from .widgets import Card, Avatar, StatusPill, StatBlock
from .theme import COLORS, SPACE


class OverviewPage(BasePage):
    navigate = Signal(str)

    def __init__(self, services):
        super().__init__(services)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 24)
        root.setSpacing(20)
        header = self._header("工作台", "两个角色，各自记忆。由你设定方向，让 DeepSeek 开始行动。", root)
        refresh = _button("刷新状态")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        root.addWidget(self.toast)

        self.cards = {}
        row = QHBoxLayout()
        row.setSpacing(20)
        for aid, title, subtitle in (("qq", "大肥鱼", "QQ 陪伴 · 独立对话与人格"),
                                     ("research", "研究员", "B 站与视频 · 独立知识与观察")):
            card = Card("")
            top = QHBoxLayout()
            top.addWidget(Avatar(56))
            names = QVBoxLayout()
            names.addWidget(_label(title, "HeroTitle"))
            names.addWidget(_label(subtitle, "Muted"))
            top.addLayout(names); top.addStretch()
            pill = StatusPill("读取中")
            top.addWidget(pill)
            card.body.addLayout(top)
            body = _label("正在检查本地状态…", "HeroText")
            body.setWordWrap(True)
            body.setMinimumHeight(72)
            card.body.addWidget(body)
            actions = QHBoxLayout()
            start = _button("启动角色", primary=True)
            stop = _button("停止")
            settings = _button("配置")
            start.clicked.connect(lambda _=False, a=aid: self._action(a, "start"))
            stop.clicked.connect(lambda _=False, a=aid: self._action(a, "stop"))
            settings.clicked.connect(lambda: self.navigate.emit("Agent 配置"))
            actions.addWidget(start); actions.addWidget(stop); actions.addStretch(); actions.addWidget(settings)
            card.body.addLayout(actions)
            more = _button("查看 QQ 对话" if aid == "qq" else "打开视频研究")
            more.clicked.connect(lambda _=False, a=aid: self.navigate.emit("大鱼" if a == "qq" else "研究"))
            card.body.addWidget(more)
            row.addWidget(card, 1)
            self.cards[aid] = {"pill": pill, "body": body, "start": start, "stop": stop}
        root.addLayout(row)

        stats = QHBoxLayout()
        self.stats = {}
        for key, label in (("inbound", "收到的消息"), ("outbound", "发出的回复"),
                           ("processed", "已读视频"), ("failed", "待处理问题")):
            card = Card("")
            value = StatBlock(label, "—")
            self.stats[key] = value.value
            card.body.addWidget(value)
            stats.addWidget(card)
        root.addLayout(stats)

        bottom = QHBoxLayout()
        recent = Card("最近对话")
        self.recent = _label("还没有对话记录", "HeroText")
        self.recent.setWordWrap(True)
        self.recent.setTextInteractionFlags(self.recent.textInteractionFlags())
        recent.body.addWidget(self.recent)
        recent.body.addStretch()
        bottom.addWidget(recent, 3)
        quick = Card("和角色聊聊", "直接发消息或照片，DeepSeek 默认理解图片；两个角色的对话分别保存。")
        quick.body.addStretch()
        media = _button("开始对话", primary=True)
        media.clicked.connect(lambda: self.navigate.emit("聊天"))
        quick.body.addWidget(media)
        logs = _button("查看运行记录")
        logs.clicked.connect(lambda: self.navigate.emit("事件"))
        quick.body.addWidget(logs)
        bottom.addWidget(quick, 2)
        root.addLayout(bottom, 1)
        self.health = _label("正在检查 QQ 连接…", "Caption")
        root.addWidget(self.health)

    def _action(self, aid, action):
        self._toast("正在启动…" if action == "start" else "正在停止…")
        controls = self.cards[aid]
        controls["start"].setEnabled(False); controls["stop"].setEnabled(False)
        def done():
            controls["start"].setEnabled(True); controls["stop"].setEnabled(True)
        def result(value):
            self._toast("操作完成" if value.get("ok") else str(value.get("reason") or "未就绪，请查看运行记录"))
        self._submit(lambda: self.services.control_agent(aid, action), result, on_done=done)

    def refresh(self):
        if not self.loading:
            self._submit(self.services.overview, self._apply)

    def _apply(self, data):
        qq_on = bool(data.get("loop") and data.get("fishqq") and data.get("napcat"))
        card = self.cards["qq"]
        card["pill"].set_text("在线" if qq_on else "未启动", "good" if qq_on else "neutral")
        last = data.get("last_message") or {}
        card["body"].setText("QQ 已连接，正在等待你的消息。\n" + (str(last.get("text", ""))[:76] if last else "") if qq_on
                              else "从这里启动 QQ 角色。人格、模型和图片理解都可以在客户端调整。")
        counts = data.get("research_counts") or {}
        state = (data.get("research_runtime") or {}).get("state")
        self.cards["research"]["pill"].set_text(status_cn(state), "good" if state == "running" else "neutral")
        favorite = data.get("favorite") or {}
        self.cards["research"]["body"].setText("当前来源：" + str(favorite.get("title") or "尚未选择收藏夹") +
            "\n读取字幕或分析画面，观察结果留在研究侧。")
        for aid, active in (("qq", qq_on), ("research", state == "running")):
            self.cards[aid]["start"].setEnabled(not active)
            self.cards[aid]["stop"].setEnabled(active)
        grouped = data.get("message_counts", [])
        self.stats["inbound"].setText(str(sum(n for d, s, n in grouped if d == "inbound")))
        self.stats["outbound"].setText(str(sum(n for d, s, n in grouped if d == "outbound" and s == "sent")))
        source_counts = dict(data.get("source_counts", []))
        self.stats["processed"].setText(str(source_counts.get("processed", source_counts.get("done", 0))))
        self.stats["failed"].setText(str(source_counts.get("failed", 0)))
        messages = data.get("recent_messages", [])[-4:]
        self.recent.setText("\n\n".join(("你  ·  " if m.get("direction") == "inbound" else "大肥鱼  ·  ") +
                                     str(m.get("text") or "[附件]")[:130] for m in messages) or "还没有对话。启动后，在 QQ 私聊中向大肥鱼打个招呼。")
        self.health.setText("QQ 连接 " + ("正常" if data.get("napcat") else "未连接") +
                            "   ·   消息通道 " + ("就绪" if data.get("fishqq") else "未启动") +
                            "   ·   角色服务 " + ("运行中" if data.get("loop") else "未启动"))
