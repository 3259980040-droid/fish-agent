"""Local, bounded, redacted snapshots of model-facing requests.

This records the Harness LLM boundary, not the provider's internal processing.
No attachment bytes, credentials or private model reasoning are retained.
"""
from pathlib import Path
import json
import os
import re
import time
from storage import database


def redact(text):
    text = str(text)
    for name, secret in os.environ.items():
        if any(w in name.upper() for w in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD')) and len(secret) >= 6:
            text = text.replace(secret, '[已隐藏]')
    text = re.sub(r'data:[^\s"\']+;base64,[A-Za-z0-9+/=]+', '[附件数据已省略]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[已隐藏]', text)
    text = re.sub(r'(?i)\bBearer\s+[^\s,;"\']+', 'Bearer [已隐藏]', text)
    text = re.sub(r'(?i)(["\']?(?:api[_-]?key|token|secret|password|authorization)["\']?\s*[:=]\s*)["\']?[^\s,;"\'}]+', r'\1[已隐藏]', text)
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[已隐藏]@', text)
    text = re.sub(r'(https?://[^\s?"\']+)\?[^\s"\']*', r'\1?[查询参数已隐藏]', text)
    return text


class RequestInspector:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'data/social/request-inspector.db'
        with database(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS requests(
                id INTEGER PRIMARY KEY, role TEXT, session TEXT, scope TEXT,
                revision TEXT, created REAL, payload TEXT)''')

    def capture(self, role, identity, payload):
        if role not in ('qq', 'research') or not identity.get('session'):
            raise ValueError('请求未绑定有效身份')
        # Only known, already-sanitized fields may cross the runtime boundary.
        allowed = {k: payload.get(k) for k in ('provider', 'model', 'purpose',
                   'messages', 'tools', 'omitted', 'system', 'temperature', 'maxTokens') if k in payload}
        raw = json.dumps(allowed, ensure_ascii=False)
        if len(raw.encode('utf-8')) > 240000:
            raise ValueError('请求检查快照超过本地上限')
        # Defense in depth against raw reasoning, headers or image bytes.
        def clean(value, key=''):
            if re.search(r'thinking|reasoning|signature|authorization|password|api.?key|token|secret|base64', key, re.I):
                return '[已省略]'
            if isinstance(value, dict):
                if value.get('type') in ('thinking', 'reasoning', 'redacted_thinking'):
                    return {'type': value['type'], 'omitted': True}
                return {str(k): clean(v, str(k)) for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v) for v in value[:300]]
            if isinstance(value, str):
                return redact(value[:40000])
            return value
        allowed = clean(allowed)
        revision = ''
        if identity['scope'].startswith('space:'):
            from social_context import SocialContext
            revision = SocialContext(self.root).space(identity['scope'][6:])['revision']
        elif re.fullmatch(r'chat:group:[0-9]{5,15}:test:[a-f0-9]{32}', identity['scope']):
            from social_context import SocialContext
            revision = SocialContext(self.root).space(identity['scope'].rsplit(':', 1)[1])['revision']
        with database(self.path) as db:
            cur = db.execute('INSERT INTO requests(role,session,scope,revision,created,payload) VALUES(?,?,?,?,?,?)',
                (role, identity['session'], identity['scope'], revision, time.time(), json.dumps(allowed, ensure_ascii=False)))
            db.execute('DELETE FROM requests WHERE role=? AND session=? AND id NOT IN (SELECT id FROM requests WHERE role=? AND session=? ORDER BY id DESC LIMIT 12)',
                (role, identity['session'], role, identity['session']))
            db.execute('DELETE FROM requests WHERE id NOT IN (SELECT id FROM requests ORDER BY id DESC LIMIT 300)')
        return {'id': cur.lastrowid}

    def latest(self, role, session):
        with database(self.path) as db:
            row = db.execute('SELECT * FROM requests WHERE role=? AND session=? ORDER BY id DESC LIMIT 1', (role, session)).fetchone()
        if not row:
            return {'captured': False, 'note': '尚无实际请求快照。发送一轮后可查看；静态预览不能代替实际输入。'}
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        result.update(captured=True, note='Harness LLM 调用边界的脱敏快照；服务商内部处理不可见。图片与内部思考省略，截断见 omitted。')
        return result
