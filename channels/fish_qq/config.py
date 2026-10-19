"""fish-qq 配置：只读环境变量里的 token，不把 token 写进配置文件。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FishQQConfig:
    root: Path
    listen_host: str = "127.0.0.1"
    listen_port: int = 3101
    listen_path: str = "/ws"
    core_token: str = ""
    napcat_url: str = "ws://127.0.0.1:3001"
    napcat_token: str = ""
    owner_id: str = ""
    group_ids: list[str] = field(default_factory=list)
    private_mode: str = "owner_only"
    outbound_per_min: int = 20
    outbound_global_per_min: int = 60
    max_text_chars: int = 4000
    max_segments: int = 20
    max_frame_bytes: int = 1048576
    dedup_window_s: int = 300
    state_dir: Path = Path("channel-state")
    bot_id: str = "unknown"
    display_name: str = "大肥鱼"
    heartbeat_s: int = 30
    heartbeat_timeout_s: int = 90
    send_timeout_s: float = 15.0
    reconnect_delays: list[float] = field(default_factory=lambda: [1, 2, 4, 8, 16, 30, 60])
    log_level: str = "info"

    @classmethod
    def from_file(cls, path: Path, root: Path | None = None) -> "FishQQConfig":
        root = root or path.resolve().parent.parent.parent
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw, root)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], root: Path) -> "FishQQConfig":
        listen = raw.get("listen") or {}
        napcat = raw.get("napcat") or {}
        allow = raw.get("allowlist") or {}
        limits = raw.get("limits") or {}
        bot = raw.get("bot") or {}
        token = os.environ.get(str(listen.get("token_env", "FISH_QQ_TOKEN")), "")
        napcat_token = os.environ.get(str(napcat.get("token_env", "NAPCAT_TOKEN")), "")
        state_dir = Path(str(raw.get("spool_dir", "channel-state")))
        if not state_dir.is_absolute():
            state_dir = root / state_dir
        return cls(
            root=root,
            listen_host=str(listen.get("host", "127.0.0.1")),
            listen_port=int(listen.get("port", 3101)),
            listen_path=str(listen.get("path", "/ws")),
            core_token=token,
            napcat_url=str(napcat.get("url", "ws://127.0.0.1:3001")),
            napcat_token=napcat_token,
            owner_id=str(allow.get("owner_id", "")),
            group_ids=[str(v) for v in (allow.get("group_ids") or [])],
            private_mode=str(allow.get("private_mode", "owner_only")),
            outbound_per_min=int(limits.get("outbound_per_min", 20)),
            outbound_global_per_min=int(limits.get("outbound_global_per_min", 60)),
            max_text_chars=int(limits.get("max_text_chars", 4000)),
            max_segments=int(limits.get("max_segments", 20)),
            max_frame_bytes=int(limits.get("max_frame_bytes", 1048576)),
            dedup_window_s=int(raw.get("dedup_window_s", 300)),
            state_dir=state_dir,
            bot_id=str(bot.get("id", raw.get("bot_id", "unknown"))),
            display_name=str(bot.get("display_name", "大肥鱼")),
            heartbeat_s=int(napcat.get("heartbeat_s", 30)),
            heartbeat_timeout_s=int(napcat.get("heartbeat_timeout_s", 90)),
            send_timeout_s=float(napcat.get("send_timeout_s", 15.0)),
            reconnect_delays=[float(x) for x in (napcat.get("reconnect_delays") or [1, 2, 4, 8, 16, 30, 60])],
            log_level=str(raw.get("log_level", "info")),
        )

    def validate(self) -> None:
        if self.listen_host != "127.0.0.1":
            raise ValueError("listen.host 必须是 127.0.0.1")
        if not self.core_token:
            raise ValueError("FISH_QQ_TOKEN 未设置")
        if not self.owner_id:
            raise ValueError("allowlist.owner_id 未设置")
        if self.listen_port < 0 or self.listen_port > 65535:
            raise ValueError("listen.port 无效")
