"""Authenticated DSH runtime adapter shared by desktop and QQ conversations."""
from __future__ import annotations
import time
from pathlib import Path
import requests
from dsh_web import DSHWebClient
from governance import control_token
from core import root_id


class DSHRuntimeAdapter:
    def __init__(self, root: Path, role: str, manager):
        if role not in ("qq", "research"):
            raise ValueError("未知角色")
        self.root, self.role, self.manager = root, role, manager

    def connect(self, cancelled=None, *, require_isolation=False, require_group_context=False):
        from tool_host import ensure_host
        ensure_host(self.root)
        port = self.manager.config.runtime_port(self.role)
        if not port:
            raise ValueError("请先配置角色运行时")
        started = False
        missing_since = None
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("已取消")
            try:
                response = requests.get(f"http://127.0.0.1:{port}/fish/connect",
                    headers={"x-fish-token": control_token(self.root)}, timeout=3, proxies={"http":None,"https":None,"all":None})
                if response.status_code == 404:
                    missing_since = missing_since or time.monotonic()
                    if started or time.monotonic() - missing_since < 5:
                        time.sleep(.25)
                        continue
                    raise RuntimeError("当前角色仍在使用旧控制插件，请在工作台停止并重新启动该角色")
                response.raise_for_status()
                data = response.json()
                if data.get("role") != self.role or data.get("guard_active") is not True:
                    raise PermissionError("运行时角色或本地工具拦截不匹配，请重启对应角色")
                if data.get('control_version') != 3:
                    raise PermissionError('角色仍在运行旧版控制服务，请停止并重新启动该角色以应用插件和用量治理')
                if data.get('root_id') != root_id(self.root):
                    raise PermissionError('该端口属于其他 Fish 数据目录，连接已拒绝')
                if require_isolation and data.get('social_context_version') != 1:
                    raise PermissionError('当前角色仍使用不支持独立窗口隔离的运行时；请停止并重新启动该角色')
                if require_group_context and data.get('social_roster_version') != 1:
                    raise PermissionError('群资料服务需要新版运行时，请停止并重新启动 QQ 角色')
                # Reuse an authenticated existing role process, with a separate
                # session per conversation. Never stop another client's runtime.
                return DSHWebClient(data["url"])
            except requests.ConnectionError:
                if not started:
                    result = self.manager.start(self.role)
                    if not result.get("ok"):
                        raise RuntimeError(result.get("reason", "启动失败"))
                    started = True
                    # First profile/plugin installation has its own bounded
                    # timeout; give the newly spawned server a full ready window.
                    deadline = time.monotonic() + 90
                time.sleep(.25)
            except requests.Timeout:
                # The local HTTP server can listen before its control route is
                # responsive. Keep the existing deadline and identity checks;
                # a slow response is not a reason to launch another runtime.
                time.sleep(.25)
        raise RuntimeError("角色服务未在等待时间内就绪，请查看运行记录；首次启动最多等待 90 秒")

    def cancel(self, session_id: str):
        return self.manager.cancel(self.role, session_id)
