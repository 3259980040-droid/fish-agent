"""Local, bounded, redacted snapshots of model-facing requests.

This records the Harness LLM boundary, not the provider's internal processing.
No attachment bytes, credentials or private model reasoning are retained.
"""
from locations import project_path
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


TOOL_PREVIEW_CHARS = 2000
TOOL_SUMMARY_CHARS = 1200


def _clip(value, limit):
    return str(value)[:limit]


def _set_of(value, limit=5):
    """Distinct short labels of one source-like field; dicts contribute their own source."""
    items = value if isinstance(value, list) else [value]
    seen = []
    for item in items:
        if isinstance(item, dict):
            item = item.get('source') or item.get('observed_from') or ''
        text = _clip(item, 60) if isinstance(item, (str, int, float)) else ''
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


COVERAGE_NUMBERS = ('requested_minutes', 'span_minutes', 'gaps_count')
COVERAGE_TIMES = ('first_message_time', 'last_message_time')


def _coverage(data):
    """The nested coverage object of a group read: real first/last time, span and source.

    The window says what was asked for; coverage says what the batch really covers, so
    this is the evidence that decides whether a claim may call the record complete.
    """
    coverage = data.get('coverage')
    if not isinstance(coverage, dict):
        return None
    picked = {}
    for name in COVERAGE_NUMBERS:
        value = coverage.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            picked[name] = value
    for name in COVERAGE_TIMES:
        if isinstance(coverage.get(name), str):
            picked[name] = _clip(coverage[name], 40)
    if isinstance(coverage.get('source'), str):
        picked['source'] = redact(_clip(coverage['source'], 40))
    if isinstance(coverage.get('complete'), bool):
        picked['complete'] = coverage['complete']
    if isinstance(coverage.get('reason'), str):
        picked['reason'] = redact(_clip(coverage['reason'], 160))
    return picked or None


def tool_summary(content):
    """Fixed-size structure of one tool result: keys, counts and ids only, never a message body.

    Returns None when the content is not a JSON object, so the caller keeps the old
    truncation for free-form tool text.
    """
    if not isinstance(content, str):
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    summary = {'truncated_original': len(content) > TOOL_PREVIEW_CHARS}
    keys = [_clip(key, 40) for key in list(data)[:12]]
    if keys:
        summary['keys'] = keys
    gaps = data.get('gaps')
    if isinstance(gaps, list):
        summary['gaps_count'] = len(gaps)
        summarized = [redact(_clip(item, 120)) for item in gaps[:3] if isinstance(item, (str, int, float))]
        if summarized:
            summary['gaps'] = summarized
    for name in ('source', 'observed_from'):
        if name in data:
            values = _set_of(data[name])
            if values:
                summary[name] = values
    window = data.get('window')
    if not isinstance(window, dict):
        window = {name: data[name] for name in ('minutes', 'from', 'to') if name in data}
    picked = {name: window[name] for name in ('minutes', 'from', 'to')
              if name in window and isinstance(window[name], (int, float, str))}
    if picked:
        summary['window'] = {name: _clip(value, 40) if isinstance(value, str) else value
                             for name, value in picked.items()}
    counts = data.get('counts')
    if isinstance(counts, dict):
        summary['counts'] = {_clip(name, 40): counts[name] for name in list(counts)[:6]
                             if isinstance(counts[name], (int, float, str, bool))}
    ids = []
    if isinstance(data.get('message_ids'), list):
        ids = data['message_ids'][:5]
    elif isinstance(data.get('messages'), list):
        ids = [item.get('message_id') or item.get('id') for item in data['messages'][:5]
               if isinstance(item, dict)]
    ids = [_clip(item, 40) for item in ids if item not in (None, '') and isinstance(item, (str, int))]
    if ids:
        summary['message_ids'] = ids
    if isinstance(data.get('authorized'), (bool, str)):
        authorized = data['authorized']
        summary['authorized'] = authorized if isinstance(authorized, bool) else redact(_clip(authorized, 40))
    if isinstance(data.get('error'), str):
        summary['error'] = redact(_clip(data['error'], 120))
    if isinstance(data.get('text'), str):
        summary['text_chars'] = len(data['text'])
    if isinstance(data.get('ok'), bool):
        summary['ok'] = data['ok']
    if isinstance(data.get('url'), str):
        summary['url'] = redact(_clip(data['url'], 200))
    if isinstance(data.get('title'), str):
        summary['title'] = redact(_clip(data['title'], 120))
    if isinstance(data.get('truncated'), bool):
        summary['truncated'] = data['truncated']
    coverage = _coverage(data)
    if coverage:
        summary['coverage'] = coverage
    return _shrink(summary)


def _shrink(summary):
    """Keep the summary under a fixed size by dropping detail from the least useful end.

    The coverage scalars are the evidence a completeness claim depends on, so they are
    the last thing to go: the key list and the coverage reason are sacrificed first.
    """
    def size():
        return len(json.dumps(summary, ensure_ascii=False))
    while size() > TOOL_SUMMARY_CHARS and isinstance(summary.get('keys'), list) and len(summary['keys']) > 1:
        summary['keys'].pop()
    if size() > TOOL_SUMMARY_CHARS:
        summary.pop('keys', None)
    coverage = summary.get('coverage')
    if size() > TOOL_SUMMARY_CHARS and isinstance(coverage, dict):
        coverage.pop('reason', None)
    for name in ('gaps', 'message_ids', 'source', 'observed_from'):
        while size() > TOOL_SUMMARY_CHARS and isinstance(summary.get(name), list) and len(summary[name]) > 1:
            summary[name].pop()
    counts = summary.get('counts')
    while size() > TOOL_SUMMARY_CHARS and isinstance(counts, dict) and len(counts) > 3:
        counts.pop(list(counts)[-1])
    for name in ('gaps', 'message_ids', 'source', 'observed_from', 'counts', 'url', 'title', 'error'):
        if size() <= TOOL_SUMMARY_CHARS:
            break
        summary.pop(name, None)
    return summary


class RequestInspector:
    def __init__(self, root):
        self.root = Path(root)
        self.path = project_path(self.root, 'data/social/request-inspector.db')
        with database(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS requests(
                id INTEGER PRIMARY KEY, role TEXT, session TEXT, scope TEXT,
                revision TEXT, created REAL, payload TEXT)''')

    def capture(self, role, identity, payload):
        if role not in ('qq', 'research') or not identity.get('session'):
            raise ValueError('请求未绑定有效身份')
        # Only known, already-sanitized fields may cross the runtime boundary.
        allowed = {k: payload.get(k) for k in ('provider', 'model', 'purpose',
                   'messages', 'tools', 'omitted', 'system', 'temperature', 'maxTokens', 'effort') if k in payload}
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
                cleaned = {}
                for name, item in value.items():
                    if name == 'content' and value.get('role') == 'tool':
                        summary = tool_summary(item)
                        cleaned[name] = clean(item, name) if summary is None else summary
                    else:
                        cleaned[name] = clean(item, str(name))
                return cleaned
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
        elif re.fullmatch(r'chat:group:[0-9]{5,15}:test:[a-f0-9]{32}(?::task:[a-f0-9]{32})?', identity['scope']):
            from social_context import SocialContext
            revision = SocialContext(self.root).space(identity['scope'].split(':')[4])['revision']
        elif re.fullmatch(r'chat:group:[0-9]{5,15}:task:[a-f0-9]{32}', identity['scope']):
            from group_tasks import GroupTaskStore
            revision = GroupTaskStore(self.root).get(identity['scope'].rsplit(':',1)[1])['revision']
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
