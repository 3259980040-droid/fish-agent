"""Staged onboarding settings; no credentials are written until Finish."""
import json
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse
from core import HarnessConfig, RuntimeManager, resolve
from secrets_store import SecretStore
from storage import commit_files, read_json, settings_lock


def validate_api(base, model):
    parsed = urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('API 地址需为不含密码或查询参数的 HTTP(S) 地址')
    if not model.strip() or len(model) > 150: raise ValueError('请填写有效的模型名称')


def save_setup(root, values):
    root = Path(root)
    with settings_lock(root):
        config = HarnessConfig(root); raw = config.raw
        files = {}; protected = SecretStore(root)
        def json_file(relative, value):
            files[root / relative] = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        def secret(ref, value):
            if value.strip(): files[protected._path(ref)] = protected._crypt(value.strip().encode())
        for agent in raw['agents']:
            role = agent['id']; data = values['agents'][role]
            validate_api(data['base_url'], data['model'])
            if not data['name'].strip(): raise ValueError('请填写角色名称')
            agent.update(name=data['name'].strip(), model=data['model'].strip(), base_url=data['base_url'].strip().rstrip('/'))
            secret('agent-' + role, data.get('api_key', ''))
            if 'persona' in data:
                files[root / agent['persona']] = (data['persona'].rstrip() + '\n').encode()
        cli, node = values['cli'].strip(), values['node'].strip()
        for title, value in [('DSH', cli), ('Node', node)]:
            if not value or not resolve(root, value).is_file(): raise ValueError(title + ' 路径不存在')
        raw['runtime'].update(cli=cli, node=node)
        channel = read_json(root / 'channels/fish_qq/config.json')
        owner = values.get('owner_id', '').strip()
        if owner and not re.fullmatch('[1-9][0-9]{4,14}', owner): raise ValueError('主人 QQ 号格式不正确')
        if values.get('enable_qq') and not owner: raise ValueError('接入 QQ 时必须填写主人账号')
        napcat = urlparse(values.get('napcat_url', ''))
        if napcat.scheme not in ('ws', 'wss') or not napcat.hostname or napcat.username or napcat.password: raise ValueError('NapCat 地址需为 ws:// 或 wss:// 地址')
        channel.setdefault('allowlist', {}).update(owner_id=owner, private_mode='owner_only')
        channel.setdefault('napcat', {}).update(url=values['napcat_url'], token_env='NAPCAT_TOKEN')
        for item in raw.get('channels', []):
            if item['id'] == 'qq': item['enabled'] = bool(values.get('enable_qq'))
        secret('channel-napcat', values.get('napcat_token', ''))
        if not protected.get('channel-core', 'FISH_QQ_TOKEN'):
            secret('channel-core', secrets.token_hex(24))
        if 'global' in values: files[root / 'harness/global/AGENTS.md'] = (values['global'].rstrip() + '\n').encode()
        from research_autonomy import ResearchControl
        interests = ResearchControl(root).config()
        interests['directions'] = [v.strip() for v in values.get('directions', []) if v.strip()]
        if len(interests['directions']) > 100: raise ValueError('兴趣方向过多')
        json_file('harness/research-interests.json', interests)
        json_file('harness/agents.json', raw); json_file('channels/fish_qq/config.json', channel)
        json_file('harness/setup.json', {'version': 1, 'completed': True})
        commit_files(root, files)
    manager = RuntimeManager(HarnessConfig(root))
    warnings = []
    for role in ('qq', 'research'):
        try:
            manager.write_overlay(role); manager.personas.project(role)
        except OSError:
            warnings.append(role + ' 的运行时投影将在下次启动时重建')
    return {'ok': True, 'warnings': warnings}
