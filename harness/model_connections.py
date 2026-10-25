"""One model-connection source: the agent spec the gateway really reads.

The gateway reads `harness/agents.json` plus `SecretStore('agent-<role>')`, so this
module reads and writes exactly that source. There is no second connection registry:
the old `harness/models.json` was never read by a gateway and is not consulted here.
"""
from __future__ import annotations
from locations import project_path

import json
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from core import HarnessConfig, save_agent_settings
from secrets_store import SecretStore

ROLES = ('qq', 'research')
# Uses map to the role whose real requests already read that configuration.
USES = (
    {'use': 'chat', 'role': 'qq', 'label': '聊天与工具调用'},
    {'use': 'memory', 'role': 'qq', 'label': '记忆整理与小结核对'},
    {'use': 'research', 'role': 'research', 'label': '资料调研'},
    {'use': 'vision', 'role': '', 'label': '图片理解', 'supported': False,
     'reason': '图片理解按已有的多模态服务配置，不在本页切换；此处只显示状态，不冒充可应用'},
    {'use': 'speech', 'role': '', 'label': '语音', 'supported': False,
     'reason': '语音用途尚未接通，只保留说明，不生成空按钮'},
)


def _key(root, role, agent):
    secret = SecretStore(root)
    path = secret._path('agent-' + role)
    value = secret.get('agent-' + role, getattr(agent, 'apiKeyEnv', '') or '')
    source = ('保护存储' if path.exists() else
              ('环境变量 ' + agent.apiKeyEnv if value and getattr(agent, 'apiKeyEnv', '') else
               ('已设置' if value else '未设置')))
    return value, source


def _connection(root, role):
    agent = HarnessConfig(root).agent(role)
    if agent is None:
        return None
    value, source = _key(root, role, agent)
    return {'id': role, 'role': role, 'name': agent.name, 'base': getattr(agent, 'base_url', ''),
            'model': agent.model, 'key_ref': 'agent-' + role, 'has_key': bool(value),
            'key_source': source, 'env': getattr(agent, 'apiKeyEnv', '')}


def describe(root):
    root = Path(root)
    connections = [item for item in (_connection(root, role) for role in ROLES) if item]
    uses = []
    for use in USES:
        item = dict(use)
        if item.get('supported', True):
            connection = next((c for c in connections if c['role'] == item['role']), None)
            item['connection'] = connection['id'] if connection else ''
            item['ready'] = bool(connection and connection['has_key'] and connection['model'])
            item['model'] = connection['model'] if connection else ''
        else:
            item['connection'] = ''
            item['ready'] = False
        uses.append(item)
    return {'connections': connections, 'uses': uses,
            'source': 'harness/agents.json + Windows 保护存储（agent-<role>）',
            'note': '当前实际请求读取这里；旧的 models.json 不再被读取。凭据只显示是否已设置，不回显。'}


def _validate(base, model):
    parsed = urlparse(str(base))
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('接口地址需为 http(s) 完整地址，且不带账号密码')
    text = str(model).strip()
    if not text or len(text) > 120 or any(ch.isspace() for ch in text):
        raise ValueError('模型标识需为 1–120 个非空白字符；服务不支持发现时直接手填真实标识')
    return str(base).rstrip('/'), text


def save(root, *, role='qq', base, model, key='', name=''):
    """Save one connection and return the newly effective configuration and time."""
    root = Path(root)
    if role not in ROLES:
        raise ValueError('未知用途角色；聊天与记忆使用 qq，资料调研使用 research')
    base, model = _validate(base, model)
    agent = HarnessConfig(root).agent(role)
    if agent is None:
        raise ValueError('角色未配置')
    updates = {'base_url': base, 'model': model}
    if str(name).strip():
        updates['name'] = str(name).strip()[:60]
    # The one protected place the gateway reads; a blank key keeps the current one.
    if str(key).strip():
        SecretStore(root).put('agent-' + role, str(key).strip())
    save_agent_settings(project_path(root, 'harness/agents.json'), role, updates)
    _forget_metadata(root, base, model)
    connection = _connection(root, role)
    return {'connection': connection, 'effective_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'applied_uses': [u['use'] for u in USES if u.get('supported', True) and u['role'] == role],
            'note': '保存后下一次对应请求即读取这里；已清除旧窗口缓存，预算会按新提供商与模型重新解析。'}


def test(root, *, role='qq', base='', model='', key=''):
    """Probe the connection's model list; a failure is a reported result, not an empty list."""
    root = Path(root)
    connection = _connection(root, role)
    if connection is None:
        return {'ok': False, 'reason': '角色未配置'}
    base = str(base or connection['base']).rstrip('/')
    model = str(model or connection['model'])
    value = str(key or '').strip() or _key(root, role, HarnessConfig(root).agent(role))[0]
    if not value:
        return {'ok': False, 'connection': connection, 'reason': '还没有保存凭据，无法测试连接'}
    try:
        request = urllib.request.Request(base + '/models', headers={'Authorization': 'Bearer ' + value})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=20) as reply:
            body = reply.read().decode('utf-8', 'replace')
        names = []
        try:
            data = json.loads(body)
            names = [str(item.get('id')) for item in data.get('data', []) if isinstance(item, dict) and item.get('id')]
        except (TypeError, ValueError):
            names = []
        return {'ok': True, 'status': 200, 'models': names[:80], 'model': model,
                'discovery': bool(names), 'note': '' if names else '服务没有返回模型列表；可直接手填真实模型标识'}
    except Exception as exc:  # noqa: BLE001 - the page shows the real reason
        return {'ok': False, 'connection': connection, 'reason': type(exc).__name__ + ': ' + str(exc)[:180]}


def discover(root, *, role='qq', base='', key=''):
    result = test(root, role=role, base=base, key=key)
    return {'ok': result['ok'], 'models': result.get('models', []),
            'reason': result.get('reason', ''), 'note': result.get('note', '')}


def remove(root, *, role='qq'):
    """Remove the protected credential of a connection; a used use becomes unconfigured."""
    root = Path(root)
    secret = SecretStore(root)
    removed = False
    path = secret._path('agent-' + role)
    if path.exists():
        path.unlink()
        removed = True
    connection = _connection(root, role)
    uses = [u['use'] for u in USES if u.get('supported', True) and u['role'] == role and (connection or not removed)]
    return {'removed': removed, 'connection': connection, 'affected_uses': uses,
            'ready': bool(connection and connection['has_key']),
            'note': ('凭据已移除；聊天用途现在未配置，下一次请求会明确失败，不会回退到其他模型或人格。'
                     if removed else '没有找到要移除的凭据')}


def _forget_metadata(root, base, model):
    """Clear the exact provider+model window cache so a switch resolves the real window."""
    path = project_path(Path(root), 'data/social/model-metadata.json')
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding='utf-8') or '{}')
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    key = str(base).rstrip('/') + '|' + str(model)
    if key not in data:
        return
    data.pop(key, None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
