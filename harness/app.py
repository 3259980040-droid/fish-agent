#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fish Harness · Phase 1A 客户端

核心原则：客户端先独立运行；Runtime 只是可插拔的一层。
DSH 不存在、runtime 配置为空/错误、DSH 启动失败，都不能让客户端退出。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from core import (
    ChannelManager,
    HarnessConfig,
    RuntimeManager,
    init_databases,
    now_text,
    read_text,
    resolve,
    save_agent_settings,
    write_text,
)

BG = "#0b0f14"
PANEL = "#111821"
PANEL_2 = "#0e141b"
LINE = "#1f2a35"
TEXT = "#e6edf3"
MUTED = "#8b98a5"
GREEN = "#3ddc97"
AMBER = "#f5a524"
RED = "#ff5c5c"
BLUE = "#5aa9ff"

STATE_LABEL = {
    "running": "运行中",
    "stopped": "已停止",
    "unconfigured": "未配置",
    "unavailable": "不可用",
    "failed": "失败",
}
STATE_COLOR = {
    "running": GREEN,
    "stopped": MUTED,
    "unconfigured": AMBER,
    "unavailable": RED,
    "failed": RED,
}


class FishApp(tk.Tk):
    def __init__(self, config_path: Path):
        super().__init__()
        self.config_path = config_path.resolve()
        self.root = self.config_path.parent.parent
        self.config = HarnessConfig(self.root, self.config_path)
        self.runtime = RuntimeManager(self.config)
        self.channels = ChannelManager(self.config)
        self.db_paths = init_databases(self.root)

        self.selected = self.config.agents[0].id if self.config.agents else ""
        self.selected_channel = self.config.channels[0].id if self.config.channels else ""
        self.card_status: dict[str, tk.Label] = {}
        self.card_frames: dict[str, tk.Frame] = {}
        self.field_vars: dict[str, tk.StringVar] = {}

        self.title("Fish Harness · Phase 1A")
        self.geometry("1180x800")
        self.minsize(1000, 700)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._init_style()
        self._build()
        if self.selected:
            self.select_agent(self.selected)
        self.after(900, self.poll)

    def _init_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL, bordercolor=LINE)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL_2, foreground=MUTED, padding=(14, 8))
        style.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", TEXT)])
        style.configure("Treeview", background=PANEL_2, foreground=TEXT, fieldbackground=PANEL_2,
                        rowheight=26, borderwidth=0)
        style.configure("Treeview.Heading", background=PANEL, foreground=MUTED, borderwidth=0)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("TButton", background=PANEL, foreground=TEXT, borderwidth=0, padding=(12, 7))
        style.map("TButton", background=[("active", "#1d2b38")])

    # ---------- UI ----------
    def _build(self) -> None:
        header = tk.Frame(self, bg=PANEL, height=66)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(header, text="🐟 Fish Harness", bg=PANEL, fg=TEXT,
                 font=("Microsoft YaHei UI", 15, "bold")).pack(side="left", padx=18)
        tk.Label(header, text="Phase 1A · Core Independence", bg=PANEL, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")

        self.overall_label = tk.Label(header, text="RUNTIME UNCONFIGURED", bg=PANEL, fg=AMBER,
                                      font=("Microsoft YaHei UI", 9, "bold"))
        self.overall_label.pack(side="right", padx=16)
        tk.Button(header, text="停止当前生成", command=self.cancel_selected,
                  bg=PANEL, fg=TEXT, activebackground="#1d2b38", relief="flat",
                  font=("Microsoft YaHei UI", 9), padx=12, pady=5,
                  cursor="hand2").pack(side="right", padx=4)
        tk.Button(header, text="强行切断", command=self.panic,
                  bg=RED, fg="#1a0000", activebackground="#ff8080", relief="flat",
                  font=("Microsoft YaHei UI", 10, "bold"), padx=16, pady=5,
                  cursor="hand2").pack(side="right", padx=8)

        if self.config.error:
            tk.Label(self, text=f"配置错误：{self.config.error}（客户端仍可使用，修复后重启）",
                     bg="#3a1c1c", fg="#ffb3b3", anchor="w", padx=12, pady=5,
                     font=("Microsoft YaHei UI", 9)).pack(fill="x")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=BG, width=280)
        left.pack(side="left", fill="y", padx=(14, 8), pady=14)
        left.pack_propagate(False)
        tk.Label(left, text="AGENTS", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", pady=(0, 8))
        if not self.config.agents:
            tk.Label(left, text="没有 Agent。\n检查 harness/agents.json。", bg=BG, fg=AMBER,
                     justify="left", font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        for agent in self.config.agents:
            self._agent_card(left, agent)

        tk.Label(left, text="CHANNEL", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", pady=(14, 8))
        self.channel_card = tk.Frame(left, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        self.channel_card.pack(fill="x")
        tk.Label(self.channel_card, text="QQ 通道", bg=PANEL, fg=TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=10, pady=(9, 1))
        self.channel_state_label = tk.Label(self.channel_card, text="未配置", bg=PANEL, fg=AMBER,
                                            anchor="w", font=("Microsoft YaHei UI", 8))
        self.channel_state_label.pack(fill="x", padx=10, pady=(0, 9))

        tk.Button(left, text="打开数据库目录", command=self.open_data_dir,
                  bg=PANEL_2, fg=MUTED, relief="flat", padx=10, pady=6,
                  font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=(14, 4))

        self.nb = ttk.Notebook(body)
        self.nb.pack(side="left", fill="both", expand=True, padx=(8, 14), pady=14)
        self._build_overview()
        self._build_settings()
        self._build_channel()
        self._build_events()
        self._build_database()

        self.statusbar = tk.Label(self, text=str(self.config_path), bg=PANEL_2, fg=MUTED,
                                  anchor="w", padx=12, pady=5, font=("Consolas", 9))
        self.statusbar.pack(fill="x", side="bottom")

    def _agent_card(self, parent, agent) -> None:
        card = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="x", pady=5)
        card.bind("<Button-1>", lambda _e, a=agent: self.select_agent(a.id))

        top = tk.Frame(card, bg=PANEL)
        top.pack(fill="x", padx=10, pady=(9, 1))
        tk.Label(top, text=agent.name, bg=PANEL, fg=TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        status = tk.Label(top, text="—", bg=PANEL, fg=MUTED,
                          font=("Microsoft YaHei UI", 8))
        status.pack(side="right")
        self.card_status[agent.id] = status
        self.card_frames[agent.id] = card

        tk.Label(card, text=agent.workspace or "—", bg=PANEL, fg=MUTED, anchor="w",
                 font=("Consolas", 8)).pack(fill="x", padx=10)

        buttons = tk.Frame(card, bg=PANEL)
        buttons.pack(fill="x", padx=10, pady=(4, 9))
        tk.Button(buttons, text="启动", command=lambda a=agent: self.start_agent(a.id),
                  bg="#173a2c", fg=GREEN, relief="flat", font=("Microsoft YaHei UI", 8),
                  padx=10, pady=3, cursor="hand2").pack(side="left")
        tk.Button(buttons, text="停止", command=lambda a=agent: self.stop_agent(a.id),
                  bg="#3a1c1c", fg="#ff9b9b", relief="flat", font=("Microsoft YaHei UI", 8),
                  padx=10, pady=3, cursor="hand2").pack(side="left", padx=6)

    def _build_overview(self) -> None:
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="总览")
        info = tk.Frame(tab, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        info.pack(fill="x", pady=(0, 10))
        self.info_labels: dict[str, tk.Label] = {}
        for key, label in (
            ("agent", "Agent"), ("state", "Runtime"), ("reason", "原因"),
            ("cli", "CLI"), ("home", "DSH_HOME"), ("port", "端口"),
            ("profile", "DSH profile"), ("api_key", "API key"),
            ("workspace", "工作目录"), ("db", "数据库"), ("persona", "人格文件"),
            ("effective", "有效配置"),
        ):
            row = tk.Frame(info, bg=PANEL)
            row.pack(fill="x", padx=12, pady=2)
            tk.Label(row, text=label, bg=PANEL, fg=MUTED, width=10, anchor="w",
                     font=("Microsoft YaHei UI", 8)).pack(side="left")
            value = tk.Label(row, text="—", bg=PANEL, fg=TEXT, anchor="w",
                             font=("Consolas", 9))
            value.pack(side="left", fill="x", expand=True)
            self.info_labels[key] = value

        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", pady=(0, 6))
        tk.Label(bar, text="日志", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        tk.Button(bar, text="刷新", command=self.refresh_overview,
                  bg=PANEL_2, fg=MUTED, relief="flat", font=("Microsoft YaHei UI", 8),
                  padx=8, pady=2).pack(side="right")

        self.log_text = tk.Text(tab, bg="#080c11", fg="#c7d2dc", insertbackground=TEXT,
                                relief="flat", font=("Consolas", 9), wrap="word",
                                padx=10, pady=8, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def _build_settings(self) -> None:
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="设置")

        top = tk.Frame(tab, bg=BG)
        top.pack(fill="x", pady=(0, 8))
        tk.Label(top, text="Agent", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.agent_var = tk.StringVar(value=self.selected)
        combo = ttk.Combobox(top, textvariable=self.agent_var, values=[a.id for a in self.config.agents],
                             state="readonly", width=16)
        combo.pack(side="left", padx=8)
        combo.bind("<<ComboboxSelected>>", lambda _e: self.load_settings(self.agent_var.get()))
        tk.Button(top, text="保存 Agent 设置", command=self.save_settings,
                  bg="#173a2c", fg=GREEN, relief="flat", padx=10, pady=3).pack(side="right")
        tk.Button(top, text="检查 Runtime", command=self.check_runtime,
                  bg=PANEL, fg=TEXT, relief="flat", padx=10, pady=3).pack(side="right", padx=8)

        grid = tk.Frame(tab, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        grid.pack(fill="x")
        fields = [
            ("name", "名称"), ("model", "模型"), ("provider", "Provider"),
            ("apiKeyEnv", "API key 环境变量"), ("workspace", "工作目录"),
            ("db", "数据库"), ("persona", "人格文件"),
        ]
        for index, (key, label) in enumerate(fields):
            row = index // 2
            col = (index % 2) * 2
            tk.Label(grid, text=label, bg=PANEL, fg=MUTED, anchor="w",
                     font=("Microsoft YaHei UI", 8)).grid(row=row, column=col, sticky="w", padx=10, pady=5)
            var = tk.StringVar()
            self.field_vars[key] = var
            entry = tk.Entry(grid, textvariable=var, bg="#0c1219", fg=TEXT,
                             insertbackground=TEXT, relief="flat", font=("Consolas", 9))
            entry.grid(row=row, column=col + 1, sticky="ew", padx=10, pady=5)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        tk.Label(tab, text="人格（保存后写入 persona 文件；Runtime 重启后生效）",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(10, 4))
        self.persona_text = tk.Text(tab, height=10, bg="#080c11", fg="#c7d2dc",
                                    insertbackground=TEXT, relief="flat",
                                    font=("Microsoft YaHei UI", 10), wrap="word",
                                    padx=10, pady=8)
        self.persona_text.pack(fill="both", expand=True)
        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", pady=(6, 0))
        self.settings_status = tk.Label(bar, text="", bg=BG, fg=MUTED,
                                        font=("Microsoft YaHei UI", 9))
        self.settings_status.pack(side="left")
        tk.Button(bar, text="生成投影", command=self.project_persona,
                  bg=PANEL, fg=TEXT, relief="flat", padx=12, pady=3).pack(side="right")
        tk.Button(bar, text="预览投影", command=self.preview_projection,
                  bg=PANEL, fg=TEXT, relief="flat", padx=12, pady=3).pack(side="right", padx=6)
        tk.Button(bar, text="保存人格", command=self.save_persona,
                  bg=PANEL, fg=TEXT, relief="flat", padx=12, pady=3).pack(side="right")

    def _build_channel(self) -> None:
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="频道")

        top = tk.Frame(tab, bg=BG)
        top.pack(fill="x", pady=(0, 8))
        tk.Label(top, text="Channel", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.channel_var = tk.StringVar(value=self.selected_channel)
        combo = ttk.Combobox(top, textvariable=self.channel_var,
                             values=[c.id for c in self.config.channels],
                             state="readonly", width=16)
        combo.pack(side="left", padx=8)
        combo.bind("<<ComboboxSelected>>", lambda _e: self.load_channel(self.channel_var.get()))
        tk.Button(top, text="启动", command=self.start_channel,
                  bg="#173a2c", fg=GREEN, relief="flat", padx=12, pady=3).pack(side="right")
        tk.Button(top, text="停止", command=self.stop_channel,
                  bg="#3a1c1c", fg="#ff9b9b", relief="flat", padx=12, pady=3).pack(side="right", padx=8)

        self.channel_info = tk.Frame(tab, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        self.channel_info.pack(fill="x")
        self.channel_labels: dict[str, tk.Label] = {}
        for key, label in (("kind", "类型"), ("listen", "监听"), ("command", "启动命令"),
                           ("config", "配置文件"), ("state", "状态"), ("reason", "原因")):
            row = tk.Frame(self.channel_info, bg=PANEL)
            row.pack(fill="x", padx=12, pady=3)
            tk.Label(row, text=label, bg=PANEL, fg=MUTED, width=10, anchor="w",
                     font=("Microsoft YaHei UI", 8)).pack(side="left")
            value = tk.Label(row, text="—", bg=PANEL, fg=TEXT, anchor="w",
                             font=("Consolas", 9))
            value.pack(side="left", fill="x", expand=True)
            self.channel_labels[key] = value

        tk.Label(tab, text="Phase 1A 只验证频道配置与进程生命周期；fish-qq 的 OneBot 实现在 Phase 3。",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(10, 0))

    def _build_events(self) -> None:
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="事件")
        columns = ("ts", "agent", "kind", "detail")
        self.events = ttk.Treeview(tab, columns=columns, show="headings")
        for col, title, width in (("ts", "时间", 150), ("agent", "Agent", 100),
                                  ("kind", "类型", 100), ("detail", "详情", 560)):
            self.events.heading(col, text=title)
            self.events.column(col, width=width, anchor="w")
        self.events.pack(fill="both", expand=True)
        self.refresh_events()

    def _build_database(self) -> None:
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="数据库")
        self.db_labels: dict[str, tk.Label] = {}
        for key, label in (("harness", "harness.db"), ("research", "research.db"), ("qq", "qq.db")):
            row = tk.Frame(tab, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=label, bg=PANEL, fg=MUTED, width=12, anchor="w",
                     font=("Microsoft YaHei UI", 9)).pack(side="left", padx=10, pady=6)
            value = tk.Label(row, text="—", bg=PANEL, fg=TEXT, anchor="w",
                             font=("Consolas", 9))
            value.pack(side="left", fill="x", expand=True)
            self.db_labels[key] = value
        tk.Button(tab, text="刷新", command=self.refresh_db,
                  bg=PANEL, fg=TEXT, relief="flat", padx=12, pady=4).pack(anchor="w", pady=8)
        self.refresh_db()

    # ---------- 数据刷新 ----------
    def select_agent(self, agent_id: str) -> None:
        self.selected = agent_id
        for key, frame in self.card_frames.items():
            frame.configure(highlightbackground=BLUE if key == agent_id else LINE)
        if hasattr(self, "agent_var"):
            self.agent_var.set(agent_id)
        self.load_settings(agent_id)
        self.refresh_overview()

    def refresh_overview(self) -> None:
        agent = self.config.agent(self.selected) if self.selected else None
        if agent is None:
            return
        state = self.runtime.status(agent.id)
        home = self.config.runtime_home(agent.id)
        port = self.config.runtime_port(agent.id)
        self.info_labels["agent"].config(text=f"{agent.name} ({agent.id})")
        self.info_labels["state"].config(
            text=STATE_LABEL.get(state["state"], state["state"]),
            fg=STATE_COLOR.get(state["state"], MUTED),
        )
        self.info_labels["reason"].config(text=state.get("reason", ""))
        self.info_labels["cli"].config(text=self.config.runtime.cli or "—")
        self.info_labels["home"].config(text=str(home) if home else "—")
        self.info_labels["port"].config(text=str(port) if port else "—")
        self.info_labels["workspace"].config(text=agent.workspace or "—")
        self.info_labels["db"].config(text=agent.db or "—")
        self.info_labels["persona"].config(text=agent.persona or "—")
        profile_ready = self.runtime.profile_ready(agent.id)
        self.info_labels["profile"].config(
            text="已初始化" if profile_ready else "未初始化（启动时自动初始化）",
            fg=GREEN if profile_ready else AMBER,
        )
        key = self.runtime.api_key_status(agent.id)
        self.info_labels["api_key"].config(
            text="已设置" if key["set"] else f"未设置（{key['reason']}）",
            fg=GREEN if key["set"] else AMBER,
        )
        if not hasattr(self, "_effective"):
            self._effective = {}
        eff = self._effective.get(agent.id)
        if eff:
            self.info_labels["effective"].config(
                text=f"{eff.get('provider')}/{eff.get('model')}",
                fg=GREEN if eff.get("matches") else AMBER,
            )
        else:
            self.info_labels["effective"].config(text="未检查", fg=MUTED)

        log = "\n".join(self.runtime.logs.get(agent.id, []))
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", log or "（还没有日志）")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def load_settings(self, agent_id: str) -> None:
        agent = self.config.agent(agent_id)
        if agent is None:
            return
        values = {
            "name": agent.name, "model": agent.model, "provider": agent.provider,
            "apiKeyEnv": agent.apiKeyEnv, "workspace": agent.workspace,
            "db": agent.db, "persona": agent.persona,
        }
        for key, value in values.items():
            self.field_vars.setdefault(key, tk.StringVar()).set(value)
        persona_path = resolve(self.root, agent.persona) if agent.persona else None
        text = read_text(persona_path) if persona_path else ""
        self.persona_text.delete("1.0", "end")
        self.persona_text.insert("1.0", text)

    def save_settings(self) -> None:
        agent_id = self.agent_var.get() or self.selected
        if not agent_id:
            return
        updates = {key: var.get().strip() for key, var in self.field_vars.items()}
        try:
            save_agent_settings(self.config_path, agent_id, updates)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))
            return
        self.config = HarnessConfig(self.root, self.config_path)
        self.runtime.config = self.config
        self.channels.config = self.config
        self.settings_status.config(text=f"已保存 · {now_text()}", fg=GREEN)
        self.refresh_overview()

    def save_persona(self) -> None:
        agent = self.config.agent(self.agent_var.get() or self.selected)
        if agent is None or not agent.persona:
            return
        path = resolve(self.root, agent.persona)
        try:
            write_text(path, self.persona_text.get("1.0", "end").rstrip() + "\n")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))
            return
        self.settings_status.config(text=f"人格已保存 · {now_text()}", fg=GREEN)
        self.log_event(agent.id, "persona", str(path))

    def preview_projection(self) -> None:
        agent_id = self.agent_var.get() or self.selected
        if not agent_id:
            return
        preview = self.runtime.personas.preview(agent_id)
        win = tk.Toplevel(self)
        win.title(f"投影预览 · {preview['chars']} chars / ~{preview['tokens_estimate']} tokens")
        win.geometry("900x650")
        win.configure(bg=BG)
        text = tk.Text(win, bg="#080c11", fg="#c7d2dc", insertbackground=TEXT,
                       relief="flat", font=("Consolas", 9), wrap="word",
                       padx=10, pady=8)
        text.pack(fill="both", expand=True)
        text.insert("1.0", preview["text"])
        text.configure(state="disabled")

    def project_persona(self) -> None:
        agent_id = self.agent_var.get() or self.selected
        if not agent_id:
            return
        result = self.runtime.personas.project(agent_id)
        if not result.get("ok"):
            messagebox.showerror("投影失败", result.get("reason", "unknown"))
            return
        self.log_event(agent_id, "projection", result["path"])
        messagebox.showinfo(
            "投影已生成",
            f"{result['path']}\n{result['chars']} chars / ~{result['tokens_estimate']} tokens",
        )

    def check_runtime(self) -> None:
        agent_id = self.agent_var.get() or self.selected
        if not agent_id:
            return
        state = self.runtime.status(agent_id)
        effective = self.runtime.effective_config(agent_id)
        if not hasattr(self, "_effective"):
            self._effective = {}
        self._effective[agent_id] = effective
        self.refresh_overview()
        detail = (
            f"状态：{STATE_LABEL.get(state['state'], state['state'])}\n"
            f"原因：{state.get('reason', '')}\n"
            f"有效配置：{effective.get('provider')}/{effective.get('model')}\n"
            f"匹配：{effective.get('matches')}"
        )
        if not effective.get("ok"):
            detail += f"\n错误：{effective.get('reason', '')}"
        messagebox.showinfo("Runtime", detail)

    def cancel_selected(self) -> None:
        agent_id = self.agent_var.get() or self.selected
        if not agent_id:
            return
        result = self.runtime.cancel(agent_id)
        self.log_event(agent_id, "cancel", str(result))
        if not result.get("ok"):
            messagebox.showwarning(
                "停止当前生成",
                f"取消未生效：{result.get('reason', 'unknown')}\n"
                "确认 fish-control 已装入该 DSH profile。",
            )
        else:
            self.statusbar.config(text=f"已请求取消当前生成 · {now_text()}")

    def load_channel(self, channel_id: str) -> None:
        channel = self.config.channel(channel_id)
        if channel is None:
            return
        state = self.channels.status(channel_id)
        for key, value in (
            ("kind", channel.kind), ("listen", channel.listen),
            ("command", channel.command or "（未配置）"),
            ("config", channel.config or "—"),
            ("state", STATE_LABEL.get(state["state"], state["state"])),
            ("reason", state.get("reason", "")),
        ):
            self.channel_labels[key].config(text=value)

    def start_channel(self) -> None:
        channel_id = self.channel_var.get()
        if not channel_id:
            return
        result = self.channels.start(channel_id)
        self.log_event("*", "channel_start", f"{channel_id}: {result['state']}")
        self.load_channel(channel_id)
        if not result["ok"]:
            messagebox.showwarning("Channel", f"{STATE_LABEL.get(result['state'], result['state'])}\n{result.get('reason', '')}")

    def stop_channel(self) -> None:
        channel_id = self.channel_var.get()
        if not channel_id:
            return
        result = self.channels.stop(channel_id)
        self.log_event("*", "channel_stop", f"{channel_id}: {result['state']}")
        self.load_channel(channel_id)

    # ---------- 操作 ----------
    def start_agent(self, agent_id: str) -> None:
        result = self.runtime.start(agent_id)
        self.log_event(agent_id, "start", f"{result['state']}: {result.get('reason', '')}")
        self.refresh_overview()
        if not result["ok"]:
            messagebox.showwarning(
                "Runtime",
                f"{STATE_LABEL.get(result['state'], result['state'])}\n{result.get('reason', '')}",
            )

    def stop_agent(self, agent_id: str) -> None:
        result = self.runtime.stop(agent_id)
        self.log_event(agent_id, "stop", result["state"])
        self.refresh_overview()

    def panic(self) -> None:
        if not messagebox.askyesno("强行切断", "停止所有 Runtime 和 Channel？"):
            return
        self.runtime.stop_all()
        self.channels.stop_all()
        self.log_event("*", "panic", "stop_all")
        self.statusbar.config(text=f"已停止所有受管进程 · {now_text()}")

    def on_close(self) -> None:
        self.runtime.stop_all()
        self.channels.stop_all()
        self.destroy()

    def log_event(self, agent_id: str, kind: str, detail: str) -> None:
        try:
            with sqlite3.connect(self.db_paths["harness"]) as conn:
                conn.execute(
                    "INSERT INTO events(ts, agent_id, kind, detail) VALUES(?,?,?,?)",
                    (now_text(), agent_id, kind, detail[:1000]),
                )
        except Exception:  # noqa: BLE001
            pass
        self.refresh_events()

    def refresh_events(self) -> None:
        for item in self.events.get_children():
            self.events.delete(item)
        try:
            with sqlite3.connect(self.db_paths["harness"]) as conn:
                rows = conn.execute(
                    "SELECT ts, agent_id, kind, detail FROM events ORDER BY id DESC LIMIT 200"
                ).fetchall()
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            self.events.insert("", "end", values=row)

    def refresh_db(self) -> None:
        for key, path in self.db_paths.items():
            try:
                with sqlite3.connect(path) as conn:
                    tables = conn.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type='table'"
                    ).fetchone()[0]
                self.db_labels[key].config(text=f"{path}  ·  {tables} 张表")
            except Exception as exc:  # noqa: BLE001
                self.db_labels[key].config(text=f"{path}  ·  不可用：{exc}", fg=RED)

    def open_data_dir(self) -> None:
        import os
        os.startfile(self.root / "data")

    def poll(self) -> None:
        running = 0
        for agent in self.config.agents:
            state = self.runtime.status(agent.id)
            if state["state"] == "running":
                running += 1
            label = self.card_status.get(agent.id)
            if label is not None:
                label.config(
                    text=STATE_LABEL.get(state["state"], state["state"]),
                    fg=STATE_COLOR.get(state["state"], MUTED),
                )
        if self.selected:
            self.refresh_overview()
        if self.selected_channel:
            self.load_channel(self.selected_channel)
            channel_state = self.channels.status(self.selected_channel)["state"]
            self.channel_state_label.config(
                text=STATE_LABEL.get(channel_state, channel_state),
                fg=STATE_COLOR.get(channel_state, MUTED),
            )
        if self.config.runtime.cli:
            self.overall_label.config(text=f"RUNTIME · {running} RUNNING", fg=GREEN if running else AMBER)
        else:
            self.overall_label.config(text="RUNTIME UNCONFIGURED", fg=AMBER)
        self.after(1500, self.poll)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Fish Harness Phase 1A")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "agents.json"))
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    app = FishApp(Path(args.config))
    if args.selftest:
        app.withdraw()
        app.update_idletasks()
        app.update()
        for agent in app.config.agents:
            state = app.runtime.status(agent.id)
            print(f"agent {agent.id}: {state['state']} ({state.get('reason', '')})")
        for channel in app.config.channels:
            state = app.channels.status(channel.id)
            print(f"channel {channel.id}: {state['state']} ({state.get('reason', '')})")
        print("db:", ", ".join(f"{k}={v.exists()}" for k, v in app.db_paths.items()))
        app.destroy()
        return 0
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
