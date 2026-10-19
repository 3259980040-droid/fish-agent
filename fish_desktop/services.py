"""Fish Desktop 的 application/service facade。

UI 只调用这一层；这里再调用 RuntimeManager / fish-qq / QQStore / ResearchStore。
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import shutil
import sqlite3
import subprocess
import sys
import time
import threading
from contextlib import closing
from pathlib import Path
from typing import Any


from process_tools import _create_kill_on_close_job, _assign_job


def _hidden_flags() -> int:
    if os.name != "nt":
        return 0
    return (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0))


def find_project_root() -> Path:
    from .bootstrap import locate_root
    return locate_root()


PROJECT = find_project_root()
_CODE_HARNESS = Path(__file__).resolve().parents[1] / 'harness'
if not getattr(sys, 'frozen', False) and str(_CODE_HARNESS) not in sys.path:
    sys.path.insert(0, str(_CODE_HARNESS))

import requests

from core import HarnessConfig, RuntimeManager
from qq_store import QQStore
from research_store import ResearchStore
from storage import locked_settings


def load_user_env() -> None:
    """把 User 环境里的 token/key 读进当前进程，不打印值。"""
    names = (
        "FISH_QQ_TOKEN", "NAPCAT_TOKEN",
        "FISH_QQ_DEEPSEEK_API_KEY", "FISH_RESEARCH_DEEPSEEK_API_KEY",
    )
    if os.name != "nt":
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in names:
                if os.environ.get(name):
                    continue
                try:
                    value, _typ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if value:
                    os.environ[name] = str(value)
    except Exception:
        pass


def tcp_open(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_json(url: str, method: str = "GET", timeout: float = 0.5, root: Path = PROJECT) -> dict[str, Any] | None:
    try:
        from governance import control_token
        response = requests.request(method, url, timeout=timeout, headers={"x-fish-token": control_token(root)}, proxies={"http":None,"https":None,"all":None})
        if response.status_code >= 400:
            return None
        return response.json()
    except Exception:
        return None


def kill_port(port: int, root: Path = PROJECT) -> bool:
    """只用于 Fish-owned 端口；调用方不得传 NapCat 的 3001。"""
    if port == 3001:
        return False
    try:
        output = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    pids = set()
    for line in output.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0] == "TCP" and parts[1].rsplit(":", 1)[-1] == str(port)
                and parts[-2] == "LISTENING" and parts[-1].isdigit()):
            pids.add(parts[-1])
    killed = False
    for pid in pids:
        # A listening port alone is not proof of ownership. Only stop packaged
        # Fish backends or a DSH web process on this project's reserved port.
        probe = subprocess.run(["powershell", "-NoProfile", "-Command",
            f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
            creationflags=_hidden_flags())
        try:
            info = json.loads(probe.stdout or "{}") or {}
        except ValueError:
            continue
        command = str(info.get("CommandLine", "")).lower()
        executable = str(info.get("ExecutablePath", "")).lower()
        # Installed releases live outside the data root. The transport's exact
        # --config argument still establishes which project's process this is.
        owns_config = False
        try:
            arguments = [part.strip('"') for part in shlex.split(command, posix=False)]
            owns_config = (arguments[arguments.index('--backend') + 1] == 'fish-qq'
                and arguments[arguments.index('--config') + 1]
                == str((root / 'channels/fish_qq/config.json').resolve()).lower())
        except (ValueError, IndexError):
            pass
        owned_backend = (executable.endswith("fish.exe") and "--backend" in command
                         and (str(root.resolve()).lower() in executable or owns_config))
        owned_runtime = False
        if port in (3080, 3090) and "dsh" in command and f"--port {port}" in command:
            token_path = root / "harness" / "runtime" / "control.token"
            if token_path.exists():
                token = token_path.read_text(encoding="utf-8").strip()
                if token:
                    try:
                        response = requests.get(f"http://127.0.0.1:{port}/fish/status",
                            headers={"x-fish-token": token}, timeout=1, proxies={"http":None,"https":None,"all":None})
                        owned_runtime = response.ok and response.json().get("ok") is True
                    except (requests.RequestException, ValueError):
                        pass
        if owned_backend or owned_runtime:
            result = subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True,
                                    creationflags=_hidden_flags())
            killed = killed or result.returncode == 0
    return killed


class FishServices:
    def __init__(self, root: Path = PROJECT):
        self.root = root
        self.config = HarnessConfig(root)
        self.runtime = RuntimeManager(self.config)
        self.qq = QQStore(root)
        self.research = ResearchStore(root)
        self.fishqq_proc: subprocess.Popen | None = None
        self.loop_proc: subprocess.Popen | None = None
        self.job = _create_kill_on_close_job()
        self.runtime.on_process_created = lambda process: _assign_job(self.job, process)
        self._config_lock = threading.RLock()
        self._chat_sessions = {}
        self._chat_locks = {a: threading.Lock() for a in ("qq", "research")}
        self._chat_cancel = {a: threading.Event() for a in ("qq", "research")}
        self._chats = {}
        load_user_env()

    def start_background(self):
        # Previews and tests do not call this; normal startup resumes authorized
        # queued work and owner-enabled schedules without another model turn.
        def start():
            try:
                from tool_host import ensure_host
                ensure_host(self.root)
            except Exception as exc:
                from governance import Governance
                Governance(self.root).audit('background_start_failed', type(exc).__name__)
        threading.Thread(target=start, daemon=True, name='fish-background-start').start()

    # ---------- status ----------
    def runtime_state(self, agent_id: str) -> dict[str, Any]:
        state = self.runtime.status(agent_id)
        port = self.config.runtime_port(agent_id)
        if port and tcp_open("127.0.0.1", int(port)):
            from core import root_id
            health = http_json(f'http://127.0.0.1:{port}/fish/status', root=self.root)
            valid = health and health.get('root_id') == root_id(self.root) and health.get('role') == agent_id
            state = {**state, 'state': 'running' if valid else 'unavailable', 'reason': '已核验角色服务' if valid else '端口已占用，身份未核验；如为旧角色请重启'}
        return state

    def overview(self) -> dict[str, Any]:
        qq_runtime = self.runtime_state("qq")
        research_runtime = self.runtime_state("research")
        fishqq = tcp_open("127.0.0.1", 3101)
        napcat = tcp_open("127.0.0.1", 3001)
        loop = http_json("http://127.0.0.1:3102/status", timeout=0.4, root=self.root)
        with closing(self.qq.connect()) as conn:
            conv = conn.execute(
                "SELECT * FROM conversations WHERE chat_id=? AND agent_id='qq' "
                "ORDER BY updated_at DESC LIMIT 1", ("private:" + self.owner_id(),)
            ).fetchone()
            if conv is None:
                conv = conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1").fetchone()
            cid = conv["id"] if conv else ""
            last = conn.execute(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (cid,)
            ).fetchone()
            recent = conn.execute(
                "SELECT direction, status, text, ts FROM messages WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT 6", (cid,)
            ).fetchall()
            counts = conn.execute(
                "SELECT direction, status, count(*) FROM messages WHERE conversation_id=? "
                "GROUP BY direction, status", (cid,)
            ).fetchall()
        with closing(self.research.connect()) as conn:
            fav = conn.execute("SELECT * FROM favorites ORDER BY selected_at DESC LIMIT 1").fetchone()
            source_counts = conn.execute("SELECT status, count(*) FROM sources GROUP BY status").fetchall()
            last_run = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "qq_runtime": qq_runtime,
            "research_runtime": research_runtime,
            "fishqq": fishqq,
            "napcat": napcat,
            "loop": loop,
            "conversation": dict(conv) if conv else None,
            "last_message": dict(last) if last else None,
            "recent_messages": [dict(row) for row in reversed(recent)],
            "message_counts": [tuple(row) for row in counts],
            "favorite": dict(fav) if fav else None,
            "source_counts": [tuple(row) for row in source_counts],
            "research_counts": self.research.counts(),
            "last_run": dict(last_run) if last_run else None,
        }

    def qq_snapshot(self, conversation_id='', search='', after='', before='', limit=100) -> dict[str, Any]:
        with closing(self.qq.connect()) as conn:
            conversations = conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 100").fetchall()
            conv = conn.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone() if conversation_id else None
            conv = conv or conn.execute(
                "SELECT * FROM conversations WHERE chat_id=? AND agent_id='qq' "
                "ORDER BY updated_at DESC LIMIT 1", ("private:" + self.owner_id(),)
            ).fetchone()
            if conv is None:
                conv = conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1").fetchone()
            cid = conv["id"] if conv else ""
            conditions, parameters = ['conversation_id=?'], [cid]
            if search: conditions.append('text LIKE ?'); parameters.append('%' + search + '%')
            if after: conditions.append('ts>=?'); parameters.append(after + ' 00:00:00')
            if before: conditions.append('ts<=?'); parameters.append(before + ' 23:59:59')
            messages = conn.execute(
                "SELECT id, direction, status, sender_id, text, channel_message_id, "
                "runtime_turn_id, error, cancelled, ts, segments FROM messages WHERE " + ' AND '.join(conditions) +
                " ORDER BY id DESC LIMIT ?", (*parameters, min(2000, max(20, int(limit))))
            ).fetchall()
            turns = conn.execute(
                "SELECT id, status, runtime_session_id, started_at, finished_at, error "
                "FROM runtime_turns WHERE conversation_id=? ORDER BY started_at DESC LIMIT 20", (cid,)
            ).fetchall()
            grouped = conn.execute(
                "SELECT direction, status, count(*) FROM messages WHERE conversation_id=? "
                "GROUP BY direction, status", (cid,)
            ).fetchall()
        message_counts = {"inbound": 0, "outbound": 0, "failed": 0, "cancelled": 0}
        for direction, status, count in grouped:
            if direction in message_counts:
                message_counts[direction] += count
            if status in ("failed", "cancelled"):
                message_counts[status] += count
        return {
            "conversation": dict(conv) if conv else None,
            'conversations': [dict(r) for r in conversations],
            "messages": [dict(row) for row in reversed(messages)],
            "turns": [dict(row) for row in turns],
            "counts": self.qq.counts(),
            "message_counts": message_counts,
            "runtime": self.runtime_state("qq"),
            "fishqq": tcp_open("127.0.0.1", 3101),
            "napcat": tcp_open("127.0.0.1", 3001),
            "loop": http_json("http://127.0.0.1:3102/status", timeout=0.4, root=self.root),
        }

    def research_snapshot(self) -> dict[str, Any]:
        with closing(self.research.connect()) as conn:
            fav = conn.execute("SELECT * FROM favorites ORDER BY selected_at DESC LIMIT 1").fetchone()
            sources = conn.execute(
                "SELECT bvid, title, author, status, observation_level, "
                "available_modalities, processed_modalities, warnings, retry_count, "
                "last_error, updated_at FROM sources ORDER BY id DESC LIMIT 100"
            ).fetchall()
            memory = conn.execute("SELECT source_id, summary, path, created_at FROM memories ORDER BY id DESC LIMIT 50").fetchall()
            runs = conn.execute(
                "SELECT id, media_id, started_at, finished_at, total, processed, skipped, failed, status, note "
                "FROM sync_runs ORDER BY id DESC LIMIT 10"
            ).fetchall()
        return {
            "favorite": dict(fav) if fav else None,
            "sources": [dict(row) for row in sources],
            "memories": [dict(row) for row in memory],
            "runs": [dict(row) for row in runs],
            "counts": self.research.counts(),
            "runtime": self.runtime_state("research"),
        }

    def agents_snapshot(self) -> dict[str, Any]:
        result = []
        for agent in self.config.agents:
            key = self.runtime.api_key_status(agent.id)
            preview = self.runtime.personas.preview(agent.id)
            result.append({
                "id": agent.id,
                "name": agent.name,
                "provider": agent.provider,
                "model": agent.model,
                "api_key": key,
                "workspace": agent.workspace,
                "persona": agent.persona,
                "preview_chars": preview["chars"],
                "preview_tokens": preview["tokens_estimate"],
                "status": self.runtime_state(agent.id),
            })
        return {"agents": result}

    def effective_config(self, agent_id: str) -> dict[str, Any]:
        return self.runtime.effective_config(agent_id)

    def persona_preview(self, agent_id: str) -> dict[str, Any]:
        return self.runtime.personas.preview(agent_id)

    def _persona_source_path(self, source):
        if source == 'global':
            return self.root / 'harness/global/AGENTS.md'
        agent = HarnessConfig(self.root).agent(source) if source in ('qq', 'research') else None
        if agent is None:
            raise ValueError('未知的人格来源')
        return self.root / agent.persona

    def persona_source(self, source):
        import hashlib
        path = self._persona_source_path(source)
        raw = path.read_bytes() if path.exists() else b''
        return {'text': raw.decode('utf-8'), 'digest': hashlib.sha256(raw).hexdigest()}

    @locked_settings
    def save_persona_source(self, source, text, digest):
        from storage import commit_files
        if not isinstance(text, str) or len(text) > 100000:
            raise ValueError('人格或共享规则需为不超过100000字的文本')
        if self.persona_source(source)['digest'] != digest:
            raise ValueError('内容已在其他窗口修改，请重新载入后核对，未覆盖新内容。')
        commit_files(self.root, {self._persona_source_path(source): text.encode('utf-8')})
        return self.persona_source(source)

    def agent_editor(self, agent_id: str) -> dict[str, Any]:
        agent = self.config.agent(agent_id)
        if agent is None:
            raise ValueError("未知角色")
        sources = self.runtime.personas.sources(agent_id)
        return {"id": agent.id, "name": agent.name, "model": agent.model, "provider": agent.provider, "base_url": agent.base_url,
                "key_set": self.runtime.api_key_status(agent_id)["set"],
                "persona": sources[1][1].read_text(encoding="utf-8") if len(sources) > 1 and sources[1][1].exists() else "",
                "global": sources[0][1].read_text(encoding="utf-8") if sources[0][1].exists() else ""}

    @locked_settings
    def save_agent(self, agent_id: str, values: dict) -> dict:
        from multimodal import atomic_json
        from secrets_store import SecretStore
        with self._config_lock:
            current = HarnessConfig(self.root)
            agent = current.agent(agent_id)
            if not agent:
                raise ValueError("未知角色")
            model = str(values.get("model", "")).strip()
            name = str(values.get("name", "")).strip()
            from urllib.parse import urlparse
            base_url = str(values.get('base_url', agent.base_url)).strip().rstrip('/')
            parsed = urlparse(base_url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('API 地址需为不含密码、查询参数的 HTTP(S) 地址')
            if not model or not name or len(model) > 150 or len(name) > 80:
                raise ValueError("请填写有效的名称与模型")
            SecretStore(self.root).put(f"agent-{agent_id}", values.get("api_key", ""))
            for key, path in (("persona", self.root / agent.persona),
                              ("global", self.root / "harness" / "global" / "AGENTS.md")):
                if key not in values:
                    continue
                text = str(values[key])
                if len(text) > 100000:
                    raise ValueError("指令文本过长")
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.read_text(encoding="utf-8") != text:
                    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
                path.write_text(text.rstrip() + "\n", encoding="utf-8")
            for record in current.raw["agents"]:
                if record["id"] == agent_id:
                    record.update(name=name, model=model, base_url=base_url)
            atomic_json(current.path, current.raw)
            self.config = HarnessConfig(self.root)
            self.runtime.config = self.config
            self.runtime.personas.config = self.config
            self.runtime.write_overlay(agent_id)
            self.runtime.personas.project(agent_id)
        return {"ok": True, "restart_required": True, "message": "已保存。重启该角色后应用到运行时；全局规则同时影响两个角色。"}

    def multimodal_snapshot(self) -> dict:
        from multimodal import MediaService
        return MediaService(self.root).status()

    def owner_id(self) -> str:
        from governance import Governance
        return Governance(self.root).owner_id()

    def chat(self, role: str):
        from desktop_chat import DesktopChat
        if role not in ("qq", "research"):
            raise ValueError("未知角色")
        with self._config_lock:
            if role not in self._chats:
                self._chats[role] = DesktopChat(self.root, role, self.runtime)
            return self._chats[role]

    def connection_snapshot(self) -> dict:
        from secrets_store import SecretStore
        path = self.root / "channels/fish_qq/config.json"
        channel = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return {"napcat_url": channel.get("napcat", {}).get("url", "ws://127.0.0.1:3001"),
                "cli": self.config.runtime.cli, "node": self.config.runtime.node,
                "core_key_set": bool(SecretStore(self.root).get("channel-core", "FISH_QQ_TOKEN")),
                "napcat_key_set": bool(SecretStore(self.root).get("channel-napcat", "NAPCAT_TOKEN"))}

    @locked_settings
    def save_connections(self, values: dict) -> dict:
        from urllib.parse import urlparse
        from secrets_store import SecretStore
        from governance import write_json
        url = str(values.get("napcat_url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("NapCat 地址需为不含密码的 ws:// 或 wss:// 地址")
        cli = str(values.get("cli", "")).strip()
        node = str(values.get("node", "")).strip()
        for title, value in (("DSH 启动器", cli), ("Node", node)):
            from core import resolve
            if value and not resolve(self.root, value).is_file():
                raise ValueError(title + "文件不存在")
        path = self.root / "channels/fish_qq/config.json"
        channel = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        channel.setdefault("napcat", {}).update(url=url, token_env="NAPCAT_TOKEN")
        channel.setdefault("listen", {}).update(host="127.0.0.1", port=3101, path="/ws", token_env="FISH_QQ_TOKEN")
        for ref, key in (("channel-core", "core_token"), ("channel-napcat", "napcat_token")):
            SecretStore(self.root).put(ref, values.get(key, ""))
        write_json(path, channel)
        raw = HarnessConfig(self.root).raw
        raw.setdefault("runtime", {}).update(cli=cli, node=node)
        write_json(self.root / "harness/agents.json", raw)
        self.config = HarnessConfig(self.root)
        self.runtime.config = self.config
        self.runtime.personas.config = self.config
        return {"ok": True, "message": "连接设置已保存，重启对应角色后生效。密钥留空会保留。"}

    def governance_snapshot(self) -> dict:
        from governance import Governance
        return Governance(self.root).snapshot()

    def save_governance(self, values: dict, owner_id: str) -> dict:
        from governance import Governance
        Governance(self.root).save(values, owner_id)
        self.runtime.personas.project("qq")
        return {"ok": True, "message": "已保存主人权限与本地规则；更换主人账号后请重启 QQ 角色。"}

    def save_multimodal(self, values: dict) -> dict:
        from multimodal import save_config
        save_config(self.root, values)
        return {"ok": True, "message": "已保存，下一个附件使用新设置。识别结果继续交给 DeepSeek。"}

    def media_models(self, values: dict | None = None) -> list[str]:
        from multimodal import MediaService
        media = MediaService(self.root)
        if values:
            media.config["vision"].update(values)
        return media.models()

    def analyze_media(self, path: str, kind: str, question: str = "") -> dict:
        from multimodal import MediaService
        return MediaService(self.root, "research").analyze(path, kind, question)

    def control_agent(self, agent_id: str, action: str) -> dict:
        if agent_id == "qq":
            return self.start_fish() if action == "start" else self.stop_fish()
        if action == "start":
            result = self.runtime.start(agent_id)
            proc = self.runtime.procs.get(agent_id)
            if proc:
                _assign_job(self.job, proc)
            return result
        return self.runtime.stop(agent_id)

    def events_snapshot(self) -> dict[str, Any]:
        events = []
        try:
            with closing(sqlite3.connect(self.root / "harness" / "harness.db")) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT ts, agent_id, kind, detail FROM events ORDER BY id DESC LIMIT 100"
                ).fetchall()
                events = [dict(row) for row in rows]
        except Exception:
            pass
        incidents = []
        qq_turns = []
        failed_messages = []
        runs = []
        try:
            with closing(self.qq.connect()) as conn:
                incidents = [dict(row) for row in conn.execute(
                    "SELECT ts, kind, detail FROM incidents ORDER BY id DESC LIMIT 50"
                ).fetchall()]
                qq_turns = [dict(row) for row in conn.execute(
                    "SELECT started_at ts, 'qq_turn' kind, id || ' ' || status detail "
                    "FROM runtime_turns ORDER BY started_at DESC LIMIT 50"
                ).fetchall()]
                failed_messages = [dict(row) for row in conn.execute(
                    "SELECT ts, 'qq_message' kind, direction || ' ' || status || ' ' || "
                    "substr(coalesce(error, text, ''), 1, 180) detail FROM messages "
                    "WHERE status IN ('failed','cancelled') ORDER BY id DESC LIMIT 50"
                ).fetchall()]
        except Exception:
            pass
        try:
            with closing(self.research.connect()) as conn:
                runs = [dict(row) for row in conn.execute(
                    "SELECT started_at ts, 'research_sync' kind, "
                    "coalesce(status,'') || ' processed=' || processed || ' failed=' || failed detail "
                    "FROM sync_runs ORDER BY id DESC LIMIT 30"
                ).fetchall()]
        except Exception:
            pass
        merged = events + incidents + qq_turns + failed_messages + runs
        merged.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
        return {"events": merged}

    def settings_snapshot(self) -> dict[str, Any]:
        return {
            "cli": self.config.runtime.cli,
            "node": str(self.runtime.node_path() or "未找到"),
            "version": self.config.runtime.version,
            "profile": self.config.runtime.profile,
            "homes": {a.id: str(self.config.runtime_home(a.id)) for a in self.config.agents},
            "ports": {a.id: self.config.runtime_port(a.id) for a in self.config.agents},
            "db": {
                "harness": str(self.root / "harness" / "harness.db"),
                "research": str(self.root / "data" / "research" / "research.db"),
                "qq": str(self.root / "data" / "qq" / "qq.db"),
            },
            "napcat": "ws://127.0.0.1:3001",
            "fishqq": "ws://127.0.0.1:3101/ws",
            "tokens": {
                "FISH_QQ_TOKEN": bool(os.environ.get("FISH_QQ_TOKEN")),
                "NAPCAT_TOKEN": bool(os.environ.get("NAPCAT_TOKEN")),
            },
            "health": {
                "dsh_cli": Path(self.config.runtime.cli).exists() if self.config.runtime.cli else False,
                "napcat": tcp_open("127.0.0.1", 3001),
                "fishqq": tcp_open("127.0.0.1", 3101),
                "agent_loop": bool(http_json("http://127.0.0.1:3102/status", timeout=0.4, root=self.root)),
            },
        }

    # ---------- controls ----------
    def start_fish(self) -> dict[str, Any]:
        from secrets_store import SecretStore
        for key, ref in (("FISH_QQ_TOKEN", "channel-core"), ("NAPCAT_TOKEN", "channel-napcat")):
            value = SecretStore(self.root).get(ref, key)
            if value:
                os.environ[key] = value
        load_user_env()
        if not os.environ.get("FISH_QQ_TOKEN"):
            return {"ok": False, "reason": "FISH_QQ_TOKEN 未设置"}
        if not tcp_open("127.0.0.1", 3101):
            log = self.root / "channel-state" / "fish-qq-ui.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["FISH_HARNESS_HOME"] = str(self.root)
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--backend", "fish-qq",
                       "--config", str(self.root / "channels" / "fish_qq" / "config.json")]
            else:
                cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / 'launch_fish.py'), '--backend', 'fish-qq', '--config',
                       str(self.root / 'channels/fish_qq/config.json')]
            self.fishqq_proc = subprocess.Popen(
                cmd, cwd=str(self.root), env=env,
                stdout=open(log, "a", encoding="utf-8"), stderr=subprocess.STDOUT,
                creationflags=_hidden_flags(),
            )
            _assign_job(self.job, self.fishqq_proc)
        if not tcp_open("127.0.0.1", 3102):
            log = self.root / "harness" / "qq-agent-loop.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["FISH_HARNESS_HOME"] = str(self.root)
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--backend", "qq-agent-loop"]
                cwd = str(self.root)
            else:
                cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / 'launch_fish.py'), '--backend', 'qq-agent-loop']
                cwd = str(self.root)
            self.loop_proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdout=open(log, "a", encoding="utf-8"), stderr=subprocess.STDOUT,
                creationflags=_hidden_flags(),
            )
            _assign_job(self.job, self.loop_proc)
        deadline = time.time() + 12
        while time.time() < deadline:
            if tcp_open("127.0.0.1", 3101) and tcp_open("127.0.0.1", 3102):
                return {"ok": True}
            time.sleep(0.2)
        return {"ok": False, "reason": "fish-qq / agent loop 未在 12s 内就绪"}

    def stop_fish(self) -> dict[str, Any]:
        http_json("http://127.0.0.1:3102/shutdown", method="POST", timeout=2, root=self.root)
        time.sleep(0.5)
        for proc in (self.loop_proc, self.fishqq_proc):
            if proc is not None and proc.poll() is None:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                               creationflags=_hidden_flags())
        kill_port(3102, self.root)
        kill_port(3101, self.root)
        self.runtime.stop("qq")
        kill_port(3090, self.root)
        return {"ok": True}

    def shutdown(self) -> None:
        self.runtime.closing.set()
        for chat in self._chats.values():
            chat.cancel()
        self.stop_fish()
        self.runtime.stop_all()
        from tool_host import shutdown_host
        shutdown_host(self.root)

    def cancel_turn(self) -> dict[str, Any]:
        return http_json("http://127.0.0.1:3102/cancel", method="POST", timeout=5, root=self.root) or {
            "ok": False, "error": "agent loop unavailable",
        }

    def sync_research(self) -> dict[str, Any]:
        from bilibili_source import BilibiliSourceAdapter, SourceAuthError
        from research_autonomy import ResearchControl
        from tool_host import ensure_host

        favorite = self.research.selected_favorite()
        if not favorite:
            return {"ok": False, "reason": "未选择收藏夹"}
        adapter = BilibiliSourceAdapter(self.root)
        try:
            control = ResearchControl(self.root)
            jobs = [control.enqueue(video, origin='主人指定收藏夹') for video in adapter.list_items(int(favorite['media_id']))[:control.config()['per_run_limit']]]
            ensure_host(self.root)
            result = {'status': 'queued', 'added': sum(not j.get('duplicate', False) for j in jobs), 'note': '已加入研究队列；生成语义摘要后才会显示研究完成。'}
        except SourceAuthError as exc:
            return {"ok": False, "reason": f"B 站未登录：{exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": result.get("status") == 'queued',
            "result": result,
            "reason": result.get("note") or ("partial" if result.get("status") == "partial" else ""),
        }
