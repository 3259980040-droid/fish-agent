"""生活页：她的群开关、潜水、习惯、记住了什么、待办事件与彩排。"""

from __future__ import annotations
from locations import project_path

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QDoubleSpinBox, QGridLayout, QHBoxLayout, QPlainTextEdit, QSpinBox, QVBoxLayout, QWidget,
)

from .pages import BasePage, Table, _button, _label
from .theme import COLORS, SPACE
from .widgets import Card, Divider, StatusPill

# A rehearsal is a real model run: it never goes through Workbench.dispatch and
# never runs on the UI thread.
REHEARSE_LIMIT = '6'
REHEARSE_TIMEOUT_S = 900
EVENTS_EVERY_S = 15.0
PYTHONS = (Path(r'E:\Program Files\DSH\runtime\python313\python.exe'), Path('C:/Python313/python.exe'))
KIND_LABELS = (('fact', '事实'), ('preference', '偏好'), ('relationship', '关系'), ('event', '事件'),
               ('promise', '约定'), ('meme', '梗'), ('emotion', '情绪'))
SOCIAL_LABELS = (('topic', '正在聊的话题'), ('thought', '想说还没说'), ('impression', '对群友的印象'))
ENGINE_LABELS = {'dsh': '旧群通道', 'shadow': '只算不发', 'fish': '自己发言'}
STATE_LABELS = {'open': '待办', 'completed': '已完成', 'cancelled': '已取消'}
_INSTANCES: dict[str, Any] = {}


def _in_process(root: Path):
    """One Workbench per data root, built only when the local service is down."""
    key = str(Path(root).resolve())
    if key not in _INSTANCES:
        from workbench import Workbench
        _INSTANCES[key] = Workbench(Path(root))
    return _INSTANCES[key]


def dispatch(root, action: str, params: dict[str, Any]):
    """Every life action is one Workbench.dispatch, local service first."""
    import requests
    from tool_host import request
    try:
        return request(Path(root), '/workbench', {'action': action, 'params': params}, role='qq', timeout=20)
    except (requests.ConnectionError, OSError):
        return _in_process(Path(root)).dispatch(action, params)


class LifePage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        self.page_title = "生活"
        self.page_subtitle = "她的状态、开关和习惯"
        self._status: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._events_at = 0.0
        self._params_dirty = False
        self._painting = False

        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE["page"], SPACE["page"], SPACE["page"], SPACE["page"])
        root.setSpacing(SPACE["section"])
        head = self._header("生活", "她的状态、开关和习惯", root)
        self.reload_btn = _button("刷新")
        head.addWidget(self.reload_btn)
        root.addWidget(self.toast)

        columns = QHBoxLayout()
        columns.setSpacing(SPACE["card"])
        left, right = QVBoxLayout(), QVBoxLayout()
        left.setSpacing(SPACE["card"])
        right.setSpacing(SPACE["card"])
        columns.addLayout(left, 2)
        columns.addLayout(right, 3)
        root.addLayout(columns, 1)
        self._build_group(left)
        self._build_habits(left)
        self._build_rehearse(left)
        self._build_memory(right)
        self._build_events(right)
        self.reload_btn.clicked.connect(self.refresh)

    # ---------------------------------------------------------------- building
    def _build_group(self, column: QVBoxLayout) -> None:
        card = Card("群", "她在群里做什么：旁听、发言、学习，以及要不要潜水")
        self.group_line = _label("—", "Muted")
        card.body.addWidget(self.group_line)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self.group_pills: dict[str, StatusPill] = {}
        for row, (key, name) in enumerate((("engine", "决策引擎"), ("listen", "旁听"), ("reply", "发言"),
                                           ("learn", "学习"), ("read", "读消息"), ("dive", "潜水"))):
            grid.addWidget(_label(name, "Caption"), row, 0)
            pill = StatusPill("—", "neutral")
            self.group_pills[key] = pill
            grid.addWidget(pill, row, 1)
        grid.setColumnStretch(2, 1)
        card.body.addLayout(grid)
        self.dive_line = _label("", "Caption")
        self.habit_line = _label("", "Caption")
        card.body.addWidget(self.dive_line)
        card.body.addWidget(self.habit_line)
        card.body.addWidget(Divider())

        switches = QHBoxLayout()
        switches.setSpacing(8)
        self.switch_buttons: dict[str, Any] = {}
        for key, name in (("listen", "旁听"), ("reply", "发言"), ("learn", "学习")):
            button = _button(name)
            button.setCheckable(True)
            button.clicked.connect(lambda checked, k=key: self._set_switch(k, checked))
            self.switch_buttons[key] = button
            switches.addWidget(button)
        switches.addStretch(1)
        card.body.addLayout(switches)

        dive = QHBoxLayout()
        dive.setSpacing(8)
        dive.addWidget(_label("潜水", "Caption"))
        self.dive_minutes = QSpinBox()
        self.dive_minutes.setRange(5, 240)
        self.dive_minutes.setValue(30)
        self.dive_minutes.setSuffix(" 分钟")
        self.dive_minutes.setFixedWidth(110)
        dive.addWidget(self.dive_minutes)
        self.dive_btn = _button("开始潜水")
        self.wake_btn = _button("叫醒她")
        dive.addWidget(self.dive_btn)
        dive.addWidget(self.wake_btn)
        dive.addStretch(1)
        card.body.addLayout(dive)
        card.body.addWidget(_label("潜水期间她照看照听，只是不开口，到点自己醒。", "Caption"))
        column.addWidget(card)
        self.dive_btn.clicked.connect(self._dive)
        self.wake_btn.clicked.connect(self._wake)

    def _build_habits(self, column: QVBoxLayout) -> None:
        card = Card("习惯", "她主动说话的节奏：冷却、唤醒概率、回访、每小时上限")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 86400)
        self.cooldown.setSuffix(" 秒")
        self.wake = QDoubleSpinBox()
        self.wake.setRange(0.0, 1.0)
        self.wake.setSingleStep(0.05)
        self.wake.setDecimals(2)
        self.followup = QSpinBox()
        self.followup.setRange(0, 60)
        self.followup.setSuffix(" 秒")
        self.hour_cap = QSpinBox()
        self.hour_cap.setRange(0, 120)
        self.hour_cap.setSuffix(" 条")
        rows = (("主动回复冷却", self.cooldown, "上一条主动回复之后隔多久才考虑再说一句"),
                ("唤醒概率", self.wake, "1 表示每条消息都进入判断，0 表示普通消息不判断"),
                ("回访窗口", self.followup, "发出后继续看这段对话多久，好接上刚来的话"),
                ("每小时上限", self.hour_cap, "一小时里她最多主动说几句"))
        for index, (name, widget, hint) in enumerate(rows):
            widget.setFixedWidth(120)
            grid.addWidget(_label(name, "Value"), index, 0)
            grid.addWidget(widget, index, 1)
            grid.addWidget(_label(hint, "Caption"), index, 2)
            widget.valueChanged.connect(self._mark_dirty)
        grid.setColumnStretch(2, 1)
        card.body.addLayout(grid)
        self.save_btn = _button("保存习惯", primary=True)
        card.body.addWidget(self.save_btn)
        self.save_btn.clicked.connect(self._save_params)
        column.addWidget(card)

    def _build_rehearse(self, column: QVBoxLayout) -> None:
        card = Card("彩排", "用真实的群消息重新问她一遍，看看她会说什么")
        self.rehearse_btn = _button("看看她会说什么", primary=True)
        card.body.addWidget(self.rehearse_btn)
        card.body.addWidget(_label("真实模型、真实判断，一句话都不发；大约要 1–5 分钟。", "Caption"))
        self.rehearse_output = QPlainTextEdit()
        self.rehearse_output.setReadOnly(True)
        self.rehearse_output.setMinimumHeight(150)
        self.rehearse_output.setPlaceholderText("彩排结果会显示在这里：时间 / 群友 / 原话 / 她的回复。这是彩排，没有真发。")
        card.body.addWidget(self.rehearse_output)
        column.addWidget(card, 1)
        self.rehearse_btn.clicked.connect(self._rehearse)

    def _build_memory(self, column: QVBoxLayout) -> None:
        card = Card("她记住了什么", "长期记忆、社交笔记、未完成的事件与提醒台账")
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        self.count_labels: dict[str, Any] = {}
        for index, (key, name) in enumerate(KIND_LABELS):
            column_index, row_index = (index % 4) * 2, index // 4
            grid.addWidget(_label(name, "Caption"), row_index, column_index)
            value = _label("0", "StatValue")
            self.count_labels[key] = value
            grid.addWidget(value, row_index, column_index + 1)
        card.body.addLayout(grid)
        card.body.addWidget(Divider())
        social = QGridLayout()
        social.setHorizontalSpacing(14)
        social.setVerticalSpacing(6)
        self.social_labels: dict[str, Any] = {}
        for index, (key, name) in enumerate(SOCIAL_LABELS):
            social.addWidget(_label(name, "Caption"), 0, index * 2)
            value = _label("0", "StatValue")
            self.social_labels[key] = value
            social.addWidget(value, 0, index * 2 + 1)
        card.body.addLayout(social)
        card.body.addWidget(Divider())
        self.open_events_line = _label("未完成事件：—", "Value")
        self.next_due_line = _label("最近到期：—", "Muted")
        self.reminder_line = _label("提醒台账：—", "Muted")
        for widget in (self.open_events_line, self.next_due_line, self.reminder_line):
            widget.setWordWrap(True)
            card.body.addWidget(widget)
        column.addWidget(card)

    def _build_events(self, column: QVBoxLayout) -> None:
        card = Card("事件", "她记下的待办；标完成后不再提，取消也不会再问")
        self.events = Table(["时间", "类型", "内容", "状态", "操作"])
        self.events.setMinimumHeight(220)
        card.body.addWidget(self.events, 1)
        column.addWidget(card, 1)

    # -------------------------------------------------------------- backend
    def _action(self, action: str, params: dict[str, Any], on_result, on_done=None) -> None:
        self._submit(lambda: dispatch(self.services.root, action, params), on_result, self._fail, on_done)

    def refresh(self) -> None:
        if self.loading:
            return
        self._action("life.status", {}, self._apply)

    def _apply(self, data: dict[str, Any]) -> None:
        self._status = data or {}
        group = self._status.get("group") or {}
        self._paint_group(group)
        self._paint_habits(group)
        self._paint_counts(self._status.get("memory") or {}, self._status.get("reminder") or {})
        if time.time() - self._events_at >= EVENTS_EVERY_S:
            self._events_at = time.time()
            self._action("life.events", {"limit": 30}, self._paint_events)

    def _after_group(self, data: dict[str, Any], message: str) -> None:
        self._apply(data)
        self._toast(message, COLORS["success"])

    def _fail(self, message: str) -> None:
        text = str(message or "").strip()
        head, _, rest = text.partition(": ")
        if rest and head.isascii() and head.replace("_", "").isalnum():
            text = rest
        self._toast("没做成：" + (text or "操作失败"), COLORS["danger"])
        self.rehearse_btn.setEnabled(True)

    # --------------------------------------------------------------- painting
    def _paint_group(self, group: dict[str, Any]) -> None:
        chat = str(group.get("chat") or "")
        self.group_line.setText("群 " + chat.split(":", 1)[1] if chat else "还没有授权群")
        engine = str(group.get("engine") or "")
        self.group_pills["engine"].set_text(ENGINE_LABELS.get(engine, engine or "—"), "info")
        for key in ("listen", "reply", "learn", "read"):
            on = bool(group.get(key))
            self.group_pills[key].set_text("开" if on else "关", "good" if on else "neutral")
        dive_until = float(group.get("dive_until") or 0)
        now = float(self._status.get("now") or time.time())
        diving = dive_until > now
        self.group_pills["dive"].set_text("潜水中" if diving else "在听", "warn" if diving else "good")
        self.dive_line.setText(("潜水到 " + time.strftime("%H:%M", time.localtime(dive_until))) if diving
                               else "现在没有潜水，她随时可能接话")
        self.habit_line.setText("冷却 %d 秒 · 唤醒概率 %.2f · 回访 %d 秒 · 每小时最多 %d 条%s" % (
            int(group.get("active_cooldown_s") or 0), float(group.get("wake_probability") or 0),
            int(group.get("followup_s") or 0), int(group.get("max_active_hour") or 0),
            "" if group.get("reply_limits") else "（每分钟上限已关）"))
        for key, button in self.switch_buttons.items():
            button.setChecked(bool(group.get(key)))

    def _paint_habits(self, group: dict[str, Any]) -> None:
        if self._params_dirty:
            return
        self._painting = True
        try:
            self.cooldown.setValue(int(group.get("active_cooldown_s") or 0))
            self.wake.setValue(float(group.get("wake_probability") if group.get("wake_probability") is not None else 1.0))
            self.followup.setValue(int(group.get("followup_s") or 0))
            self.hour_cap.setValue(int(group.get("max_active_hour") or 0))
        finally:
            self._painting = False

    def _mark_dirty(self, *_value) -> None:
        if not self._painting:
            self._params_dirty = True

    def _paint_counts(self, memory: dict[str, Any], reminder: dict[str, Any]) -> None:
        kinds = memory.get("kinds") or {}
        for key, label in self.count_labels.items():
            label.setText(str(int(kinds.get(key) or 0)))
        social = memory.get("social") or {}
        for key, label in self.social_labels.items():
            label.setText(str(int(social.get(key) or 0)))
        self.open_events_line.setText("未完成事件：%d 条" % int(memory.get("open_events") or 0))
        due = str(memory.get("next_due_text") or "")
        self.next_due_line.setText("最近到期：" + (due or "没有定时的待办"))
        line = "提醒：%s ｜ 台账 %d 条" % (str(reminder.get("source") or "—"), int(reminder.get("ledger_count") or 0))
        if reminder.get("last_state"):
            line += " ｜ 最近 " + str(reminder.get("last_state"))
            if reminder.get("last_receipt"):
                line += "（" + str(reminder.get("last_receipt")) + "）"
        self.reminder_line.setText(line)

    def _paint_events(self, rows: Any) -> None:
        self._events = list(rows or [])
        table = []
        for row in self._events:
            state = str(row.get("state") or STATE_LABELS.get(str(row.get("action")), ""))
            table.append([self._when(row.get("due") or row.get("created")), "事件",
                          str(row.get("text") or ""), state, ""])
        self.events.fill(table, widths=[130, 60, 380, 70, 150])
        for index, row in enumerate(self._events):
            memory_id = str(row.get("id") or "")
            host = QWidget()
            box = QHBoxLayout(host)
            box.setContentsMargins(4, 2, 4, 2)
            box.setSpacing(6)
            done = _button("完成")
            cancel = _button("取消", danger=True)
            for button in (done, cancel):
                button.setFixedHeight(24)
                box.addWidget(button)
            box.addStretch(1)
            done.clicked.connect(lambda _=False, mid=memory_id: self._event_done(mid, False))
            cancel.clicked.connect(lambda _=False, mid=memory_id: self._event_done(mid, True))
            self.events.setCellWidget(index, 4, host)

    @staticmethod
    def _when(value: Any) -> str:
        try:
            moment = float(value or 0)
        except (TypeError, ValueError):
            return ""
        if moment <= 0:
            return ""
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(moment))
        except (OSError, ValueError):
            return ""

    # ---------------------------------------------------------------- actions
    def _chat(self) -> str:
        return str((self._status.get("group") or {}).get("chat") or "")

    def _set_switch(self, key: str, value: bool) -> None:
        if self.loading:
            return
        self._toast("保存群开关…", COLORS["warning"])
        self._action("life.group.set", {key: bool(value)},
                     lambda data: self._after_group(data, "群开关已保存"))

    def _dive(self) -> None:
        if self.loading:
            return
        minutes = int(self.dive_minutes.value())
        self._toast("让她潜 %d 分钟…" % minutes, COLORS["warning"])
        self._action("life.dive", {"chat": self._chat(), "minutes": minutes},
                     lambda data: self._after_group(data, "她潜水了 %d 分钟，到点自己醒" % minutes))

    def _wake(self) -> None:
        if self.loading:
            return
        self._toast("叫醒她…", COLORS["warning"])
        self._action("life.wake", {"chat": self._chat()},
                     lambda data: self._after_group(data, "已叫醒，她回到群里了"))

    def _save_params(self) -> None:
        if self.loading:
            return
        params = {"chat": self._chat(), "active_cooldown_s": int(self.cooldown.value()),
                  "wake_probability": round(float(self.wake.value()), 2),
                  "followup_s": int(self.followup.value()), "max_active_hour": int(self.hour_cap.value())}
        self._toast("保存习惯…", COLORS["warning"])
        self._action("life.params", params, self._saved_params)

    def _saved_params(self, data: dict[str, Any]) -> None:
        self._params_dirty = False
        self._apply(data)
        self._toast("习惯已保存", COLORS["success"])

    def _event_done(self, memory_id: str, cancel: bool) -> None:
        if self.loading:
            return
        self._toast("取消这件事…" if cancel else "标记完成…", COLORS["warning"])
        self._action("life.event.done", {"id": memory_id, "cancel": bool(cancel), "limit": 30},
                     self._paint_events, on_done=self._after_event)

    def _after_event(self) -> None:
        self._events_at = 0.0
        self.refresh()

    # --------------------------------------------------------------- rehearsal
    @staticmethod
    def _python() -> str:
        for candidate in PYTHONS:
            if candidate.is_file():
                return str(candidate)
        return sys.executable

    def _rehearse_env(self, root: Path) -> dict[str, str]:
        from .services import load_user_env
        load_user_env()
        env = dict(os.environ)
        env["FISH_HARNESS_HOME"] = str(root)
        env["PYTHONIOENCODING"] = "utf-8"
        parts = [str(root), str(project_path(root, "harness")), str(project_path(root, "vendor", "python"))]
        for part in env.get("PYTHONPATH", "").split(os.pathsep):
            if part and part not in parts:
                parts.append(part)
        env["PYTHONPATH"] = os.pathsep.join(parts)
        return env

    def _rehearse(self) -> None:
        if self.loading:
            return
        self.rehearse_btn.setEnabled(False)
        self.rehearse_output.setPlainText("正在彩排：真实模型调用，大约 1–5 分钟，期间不会发出任何 QQ 消息…")
        self._toast("彩排进行中，可以先去别的页面", COLORS["warning"])
        self._submit(self._rehearse_work, self._rehearse_done, self._rehearse_failed,
                     lambda: self.rehearse_btn.setEnabled(True))

    def _rehearse_work(self) -> dict[str, Any]:
        root = Path(self.services.root)
        folder = Path(tempfile.mkdtemp(prefix="fish-rehearse-"))
        target = folder / "rehearse.json"
        try:
            process = subprocess.run(
                [self._python(), str(root / "tools" / "rehearse_group.py"),
                 "--limit", REHEARSE_LIMIT, "--json", str(target)],
                cwd=str(root), env=self._rehearse_env(root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=REHEARSE_TIMEOUT_S,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            output = (process.stdout or "") + "\n" + (process.stderr or "")
            payload: dict[str, Any] = {}
            if target.is_file():
                try:
                    payload = json.loads(target.read_text(encoding="utf-8"))
                except ValueError:
                    payload = {}
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        if not payload:
            tail = [line for line in output.strip().splitlines() if line.strip()][-6:]
            raise ValueError("彩排没跑成：" + (" / ".join(tail) if tail else "没有输出"))
        return payload

    def _rehearse_failed(self, message: str) -> None:
        self.rehearse_output.setPlainText("彩排没有跑成。命令行里可以直接重试：\n"
                                          + "python tools/rehearse_group.py --limit %s\n\n%s"
                                          % (REHEARSE_LIMIT, message))
        self._fail(message)

    def _rehearse_done(self, payload: dict[str, Any]) -> None:
        items = payload.get("items") or []
        meta = payload.get("meta") or {}
        lines = ["这是彩排，没有真发。用真实的群消息重新问她一遍，记录如下：", ""]
        for item in items:
            stamp = str(item.get("ts") or "")
            lines.append("%s  %s：%s" % (stamp[11:19], item.get("sender_tail") or "", item.get("display") or ""))
            if item.get("sent"):
                lines.append("    → 她说：「%s」" % (item.get("reply") or ""))
            else:
                lines.append("    → 没开口：%s" % (item.get("reason") or ""))
        if not items:
            lines.append("（这次没有可彩排的消息）")
        lines.append("")
        lines.append("共 %d 条；她说 %s 句，沉默 %s 句；用时 %s 秒。" % (
            len(items), meta.get("sent_count", 0), meta.get("silent_count", 0), meta.get("elapsed_s", "?")))
        same = meta.get("sha256_before") == meta.get("sha256_after")
        lines.append("线上库校验：" + ("前后逐字节一致，一个字节都没动" if same else "有变动，请看命令行输出"))
        lines.append("这是彩排，没有真发。")
        self.rehearse_output.setPlainText("\n".join(lines))
        self._toast("彩排完成：一句都没发出去", COLORS["success"])
