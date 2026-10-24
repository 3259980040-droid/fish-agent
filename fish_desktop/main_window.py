"""Fish navigation; each setting has one primary editing location."""

from __future__ import annotations

import atexit

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QStackedWidget, QVBoxLayout, QWidget,
)

from .pages import EventsPage, QQPage
from .workspace import OverviewPage
from .conversation import ChatPage
from .system_settings import SystemSettingsPage
from .resources import ResourcesPage
from .social import SocialPage
from .research import ResearchPage
from .life import LifePage
from .services import FishServices
from .theme import COLORS, QSS
from .widgets import Avatar, icon


class MainWindow(QMainWindow):
    def __init__(self, services: FishServices, manage_shutdown: bool = True):
        super().__init__()
        self.services = services
        self.manage_shutdown = manage_shutdown
        self._shutdown_done = False
        self.setWindowTitle("大鱼 Fish")
        self.resize(1440, 900)
        self.setMinimumSize(1100, 720)
        self.setStyleSheet(QSS)

        central = QWidget()
        self.setCentralWidget(central)
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(188)
        nav_layout = QVBoxLayout(sidebar)
        nav_layout.setContentsMargins(16, 20, 16, 14)
        nav_layout.setSpacing(6)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        brand.addWidget(Avatar(36))
        names = QVBoxLayout()
        names.setSpacing(0)
        name = QLabel("大鱼 Fish")
        name.setObjectName("BrandName")
        names.addWidget(name)
        sub = QLabel("DEEPSEEK")
        sub.setObjectName("BrandSub")
        names.addWidget(sub)
        brand.addLayout(names)
        brand.addStretch(1)
        nav_layout.addLayout(brand)
        nav_layout.addSpacing(18)

        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.setFocusPolicy(Qt.NoFocus)
        self.nav.setIconSize(QSize(17, 17))
        self.pages = [
            ("概览", OverviewPage(services), "overview"),
            ("聊天", ChatPage(services), "fish"),
            ("生活", LifePage(services), "play"),
            ("研究", ResearchPage(services), "research"),
            ("大鱼", QQPage(services), "fish"),
            ('资源管理', ResourcesPage(services), 'agents'),
            ("事件", EventsPage(services), "events"),
            ("设置", SystemSettingsPage(services), "settings"),
            ('人格与群聊', SocialPage(services), 'agents'),
        ]
        labels = {"概览": "工作台", "聊天": "对话", "大鱼": "QQ 消息", "研究": "视频研究",
                  "事件": "运行记录", "设置": "系统设置"}
        for name, _page, icon_name in self.pages:
            item = QListWidgetItem(icon(icon_name, 18, COLORS["secondary"]), labels.get(name, name))
            item.setSizeHint(QSize(0, 44))
            self.nav.addItem(item)
        nav_layout.addWidget(self.nav, 1)
        footer = QLabel("你的 DeepSeek 空间\nQQ 与研究分别保存")
        footer.setObjectName("Caption")
        nav_layout.addWidget(footer)
        shell.addWidget(sidebar)

        self.stack = QStackedWidget()
        for _name, page, _icon in self.pages:
            self.stack.addWidget(page)
        shell.addWidget(self.stack, 1)

        self.nav.currentRowChanged.connect(self.switch_page)
        self.pages[0][1].navigate.connect(self.navigate)
        self.page("研究").followup.connect(self._research_followup)
        self.page('大鱼').draftRequested.connect(self._qq_followup)
        self.nav.setCurrentRow(0)
        self._timer = QTimer(self)
        self._timer.setInterval(3500)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        if self.manage_shutdown:
            atexit.register(self._shutdown_once)
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._shutdown_once)

    def _shutdown_once(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self.services.shutdown()
        except Exception:
            pass

    def switch_page(self, index: int) -> None:
        if index < 0 or index >= len(self.pages):
            return
        self.stack.setCurrentIndex(index)
        self.nav.setCurrentRow(index)
        page = self.pages[index][1]
        if hasattr(page, "refresh"):
            page.refresh()

    def navigate(self, name: str) -> None:
        if name == 'Agent 配置':
            self.navigate('设置')
            self.page('设置').tabs.setCurrentWidget(self.page('设置').models)
            return
        for index, (title, _page, _icon) in enumerate(self.pages):
            if title == name:
                self.switch_page(index)
                return

    def page(self, name):
        return next(page for title, page, _icon in self.pages if title == name)

    def _research_followup(self, text):
        chat = self.pages[1][1]
        self.navigate('聊天')
        chat.roles.setCurrentIndex(1)
        chat.editor.setPlainText(text)
        chat.editor.setFocus()

    def _qq_followup(self, draft):
        chat = self.pages[1][1]; self.navigate('聊天'); chat.roles.setCurrentIndex(0); chat._new()
        chat.editor.setPlainText(draft['text']); chat.add_files(draft['files']); chat.editor.setFocus()

    def _poll(self) -> None:
        page = self.stack.currentWidget()
        if page is not None and hasattr(page, "refresh") and not getattr(page, "loading", False):
            page.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        if self.manage_shutdown:
            self._shutdown_once()
        from .workers import Runner
        for timer in self.findChildren(QTimer):
            timer.stop()
        for runner in self.findChildren(Runner):
            runner.shutdown()
        super().closeEvent(event)
