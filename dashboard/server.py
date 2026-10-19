#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大肥鱼 Mission Control —— 本地只读监控面板（零依赖）

用法:
  python dashboard/server.py --port 3099
  打开 http://127.0.0.1:3099
  演示数据: http://127.0.0.1:3099/?demo=1

数据来源:
  - 端口 3080 / 3090 探活
  - knowledge/index.json      笔记数、标签、今日新增
  - shared/digest/latest.md   日报时间与预览
  - qq-work/state.json        QQ 实例今日轮数/回复/花费（由 qq-gate 或 agent 写入）
  - qq-work/log/events.jsonl  群聊动态
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
INDEX = HERE / "index.html"
STATE = ROOT / "qq-work" / "state.json"
EVENTS = ROOT / "qq-work" / "log" / "events.jsonl"
KB_INDEX = ROOT / "knowledge" / "index.json"
DIGEST = ROOT / "shared" / "digest" / "latest.md"

RESEARCH_PORT = 3080
QQ_PORT = 3090


def now() -> datetime:
    return datetime.now().astimezone()


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def top_tags(notes: dict, limit: int = 8) -> list[dict]:
    counts: dict[str, int] = {}
    for note in notes.values():
        for tag in note.get("tags", []) or []:
            key = str(tag)
            counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"tag": tag, "count": count} for tag, count in ordered[:limit]]


def read_feed(limit: int = 12) -> list[dict]:
    if not EVENTS.exists():
        return []
    items: list[dict] = []
    for line in EVENTS.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items[-limit:]


def digest_info() -> dict:
    info = {"updated_at": None, "age_hours": None, "preview": ""}
    if not DIGEST.exists():
        return info
    stat = DIGEST.stat()
    updated = datetime.fromtimestamp(stat.st_mtime).astimezone()
    info["updated_at"] = iso(updated)
    info["age_hours"] = round((now() - updated).total_seconds() / 3600, 1)
    text = DIGEST.read_text(encoding="utf-8", errors="replace")
    info["preview"] = "\n".join(text.splitlines()[:14]).strip()
    return info


def build_status() -> dict:
    state = read_json(STATE, {})
    qq_state = state.get("qq", {}) if isinstance(state.get("qq"), dict) else {}

    kb = read_json(KB_INDEX, {})
    notes = kb.get("notes", {}) if isinstance(kb.get("notes"), dict) else {}
    today = now().date().isoformat()
    notes_today = sum(1 for note in notes.values() if str(note.get("captured")) == today)

    today_stats = qq_state.get("today", {}) or {}
    budget_state = qq_state.get("budget", {}) or {}
    used = float(today_stats.get("cost_cny", 0) or 0)
    per_day = float(budget_state.get("cny_per_day", 5) or 5)
    hard_stop = float(budget_state.get("hard_stop_cny", 4.5) or 4.5)

    research_up = port_open(RESEARCH_PORT)
    qq_up = port_open(QQ_PORT)
    digest = digest_info()

    alerts: list[dict] = []
    if not research_up:
        alerts.append({"level": "warn", "text": f"研究实例未启动（:{RESEARCH_PORT}）"})
    if not qq_up:
        alerts.append({"level": "warn", "text": f"QQ 实例未启动（:{QQ_PORT}）"})
    if used >= hard_stop:
        alerts.append({"level": "danger", "text": f"今日已花 ¥{used:.2f}，超过硬停线 ¥{hard_stop:.2f}"})
    elif used >= per_day * 0.8:
        alerts.append({"level": "warn", "text": f"今日预算已用 {used / per_day * 100:.0f}%"})
    if digest["age_hours"] is not None and digest["age_hours"] > 26:
        alerts.append({"level": "warn", "text": f"日报 {digest['age_hours']:.0f} 小时未更新"})
    if qq_state.get("bilibili_blocked"):
        alerts.append({"level": "danger", "text": "B 站触发验证，已停"})
    if not qq_state.get("owner_configured", False):
        alerts.append({"level": "info", "text": "主人 QQ 还没填"})

    return {
        "generated_at": iso(now()),
        "mock": False,
        "instances": {
            "research": {
                "up": research_up,
                "port": RESEARCH_PORT,
                "url": f"http://127.0.0.1:{RESEARCH_PORT}",
            },
            "qq": {
                "up": qq_up,
                "port": QQ_PORT,
                "url": f"http://127.0.0.1:{QQ_PORT}",
            },
        },
        "qq": {
            "mode": qq_state.get("mode", "active"),
            "online": bool(qq_state.get("online", qq_up)),
            "group": qq_state.get("group", ""),
            "owner": qq_state.get("owner", ""),
            "today": {
                "turns": int(today_stats.get("turns", 0) or 0),
                "replies": int(today_stats.get("replies", 0) or 0),
                "media": int(today_stats.get("media", 0) or 0),
                "cost_cny": used,
            },
            "budget": {
                "cny_per_day": per_day,
                "used": used,
                "hard_stop_cny": hard_stop,
            },
            "last_reply": qq_state.get("last_reply"),
            "feed": qq_state.get("feed") or read_feed(),
        },
        "knowledge": {
            "notes": len(notes),
            "today": notes_today,
            "tags": top_tags(notes),
        },
        "digest": digest,
        "alerts": alerts,
    }


def mock_status() -> dict:
    base = now()
    feed = [
        {"at": "20:41", "user": "阿岚", "text": "这显卡又涨价了", "reply": "等等党又输一年。"},
        {"at": "20:39", "user": "小K", "text": "大肥鱼推荐个视频", "reply": "等下，我去 B 站翻翻，别催。"},
        {"at": "20:35", "user": "老王", "text": "今天好累", "reply": "那就别卷了，早点睡。鱼都不加班。"},
        {"at": "20:28", "user": "阿岚", "text": "你是不是机器人", "reply": "是啊，一条电子鱼。主人把我捞进群的。"},
        {"at": "20:12", "user": "小K", "text": "[表情]", "reply": ""},
        {"at": "19:58", "user": "老王", "text": "在吗", "reply": "在，刚翻了个身。"},
    ]
    return {
        "generated_at": iso(base),
        "mock": True,
        "instances": {
            "research": {"up": True, "port": RESEARCH_PORT, "url": f"http://127.0.0.1:{RESEARCH_PORT}"},
            "qq": {"up": True, "port": QQ_PORT, "url": f"http://127.0.0.1:{QQ_PORT}"},
        },
        "qq": {
            "mode": "active",
            "online": True,
            "group": "123456789",
            "owner": "10001",
            "today": {"turns": 42, "replies": 11, "media": 2, "cost_cny": 0.31},
            "budget": {"cny_per_day": 5, "used": 0.31, "hard_stop_cny": 4.5},
            "last_reply": {"at": "20:41", "text": "等等党又输一年。"},
            "feed": feed,
        },
        "knowledge": {
            "notes": 12,
            "today": 3,
            "tags": [
                {"tag": "llm", "count": 5},
                {"tag": "agent", "count": 4},
                {"tag": "本地部署", "count": 3},
                {"tag": "qq", "count": 2},
            ],
        },
        "digest": {
            "updated_at": iso(base),
            "age_hours": 2.1,
            "preview": "# 2026-10-17 知识入库 3 条\n\n## 本地部署 Agent 的三种路线\n- 类型：video｜评分：4\n- 来源：https://www.bilibili.com/video/BV...\n- 摘要：从显存、并发、运维三个角度对比本地推理方案。",
        },
        "alerts": [{"level": "info", "text": "当前是演示数据（?demo=1）"}],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def _send(self, code: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            query = parse_qs(parsed.query)
            payload = mock_status() if query.get("mock", ["0"])[0] == "1" else build_status()
            self._send(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if parsed.path in ("/", "/index.html"):
            self._send(200, INDEX.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return
        self._send(404, "not found", "text/plain; charset=utf-8")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="大肥鱼 Mission Control")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3099)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"大肥鱼 Mission Control: http://{args.host}:{args.port}")
    print(f"演示数据: http://{args.host}:{args.port}/?demo=1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
