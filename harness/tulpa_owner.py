"""Explicit owner-private execution route for the original Tulpa MCP."""
from pathlib import Path
import os
import subprocess
import time
from storage import read_json

PLUGIN_ID = 'tulpa_owner'
PREFIX = 'fishp__tulpa_owner__'


def configuration(root):
    return read_json(Path(root) / 'data/integrations/tulpa/owner-route.json', {})


def ensure_service(root):
    """Restore the original MCP after Fish restarts, without opening a window."""
    import requests
    root = Path(root)
    if not configuration(root).get('enabled'):
        return
    def healthy():
        try:
            reply = requests.get('http://127.0.0.1:3120/health', timeout=1,
                                 proxies={'http': None, 'https': None, 'all': None}).json()
            return reply.get('source') == str(root / 'vendor/integrations/tulpa/source')
        except (requests.RequestException, ValueError):
            return False
    if healthy():
        return
    parent = int(os.environ.get('FISH_LOOP_PARENT_PID', str(os.getpid())))
    with (root / 'data/integrations/tulpa/service.log').open('a', encoding='utf-8') as output:
        subprocess.Popen([str(root / 'vendor/integrations/tulpa/venv/Scripts/pythonw.exe'),
                          str(root / 'harness/tulpa_service.py'), '--parent', str(parent)],
                         cwd=str(root), stdout=output, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for _ in range(60):
        if healthy():
            return
        time.sleep(.25)
    raise RuntimeError('原版 Tulpa MCP 尚未连接 QQ，请查看独立服务记录')


def require_route(root, identity):
    from governance import Governance
    config = configuration(root)
    if not (config.get('enabled') and identity['role'] == 'qq' and identity['owner']
            and (identity['desktop'] or
                Governance(root).is_owner(identity['sender']))):
        raise PermissionError('Tulpa MCP 需要当前 Fish 主人身份，不再依赖历史窗口')
    return config


