"""Role-specific DeepSeek calls and an atomic request/usage ledger."""
from __future__ import annotations
import json
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
import requests
from storage import database, read_json, role_dir


class ModelLedger:
    def __init__(self, root: Path, role: str):
        self.root, self.role = root, role
        self.path = role_dir(root, role) / 'model-usage.db'
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,day TEXT,origin TEXT,status TEXT,input_tokens INTEGER DEFAULT 0,output_tokens INTEGER DEFAULT 0,created REAL)')

    def reserve(self, origin):
        from governance import daily_limits_enabled
        settings = read_json(self.root / 'harness/control.json')
        maximum = int(settings.get('max_model_requests_daily', 300))
        tokens = int(settings.get('max_model_tokens_daily', 2000000))
        day = date.today().isoformat()
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT COUNT(*),COALESCE(SUM(input_tokens+output_tokens),0) FROM requests WHERE day=?', (day,)).fetchone()
            if daily_limits_enabled(settings, self.role) and (row[0] >= maximum or row[1] >= tokens):
                raise PermissionError('今日模型请求或已报告的 token 用量达到上限')
            rid = uuid.uuid4().hex
            db.execute('INSERT INTO requests(id,day,origin,status,created) VALUES(?,?,?,?,?)', (rid, day, origin[:100], 'reserved', time.time()))
        return rid

    def finish(self, rid, usage=None, status='done'):
        usage = usage or {}
        i = int(usage.get('prompt_tokens', usage.get('inputTokens', 0)))
        if 'inputTokens' in usage:
            i += int(usage.get('cacheReadTokens', 0)) + int(usage.get('cacheWriteTokens', 0))
        o = int(usage.get('completion_tokens', usage.get('outputTokens', 0)))
        with database(self.path) as db:
            db.execute('UPDATE requests SET status=?,input_tokens=MAX(input_tokens,?),output_tokens=MAX(output_tokens,?) WHERE id=?', (status, max(0, i), max(0, o), rid))

    def snapshot(self):
        from governance import daily_limits_enabled
        with database(self.path) as db:
            row = db.execute('SELECT COUNT(*) requests,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens FROM requests WHERE day=?', (date.today().isoformat(),)).fetchone()
            pending = db.execute("SELECT COUNT(*) FROM requests WHERE day=? AND status IN ('reserved','unknown')", (date.today().isoformat(),)).fetchone()[0]
        enabled = daily_limits_enabled(read_json(self.root / 'harness/control.json'), self.role)
        note = ('token 按服务商回传统计；未回传及并行在途请求可能超出 token 阈值，请求次数为硬上限。' if enabled
                else '本角色每日用量限制已关闭；调用次数和服务商回传 token 继续累计统计。')
        return {**dict(row), 'unreported': pending, 'daily_limits_enabled': enabled, 'note': note}


class ModelGateway:
    def __init__(self, root: Path, role: str):
        from core import HarnessConfig
        from secrets_store import SecretStore
        self.root, self.role = root, role
        agent = HarnessConfig(root).agent(role)
        if agent is None:
            raise ValueError('角色未配置')
        self.model = agent.model
        self.base = getattr(agent, 'base_url', '') or 'https://api.deepseek.com'
        parsed = urlparse(self.base)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('API 地址不合法')
        self.key = SecretStore(root).get('agent-' + role, agent.apiKeyEnv)
        self.ledger = ModelLedger(root, role)

    def call(self, messages, *, origin='background', cancelled=None, json_output=False):
        if not self.key:
            raise ValueError('请先在角色设置保存 API Key')
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('已取消')
        from governance import Governance
        if Governance(self.root, self.role).config()['paused']:
            raise PermissionError('主人已暂停此角色')
        rid = self.ledger.reserve(origin)
        body = {'model': self.model, 'messages': messages, 'thinking': {'type': 'disabled'}, 'max_tokens': 4096}
        usage = None
        if json_output:
            body['response_format'] = {'type': 'json_object'}
        try:
            response = requests.post(self.base.rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + self.key}, json=body, timeout=(8, 100), **__import__('network_config').request_options(self.root, self.base))
            if not response.ok:
                raise RuntimeError(f'DeepSeek 请求失败（HTTP {response.status_code}）')
            data = response.json()
            usage = data.get('usage')
            self.ledger.finish(rid, usage, 'done' if usage else 'unknown')
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError('已取消')
            if Governance(self.root, self.role).config()['paused']:
                raise PermissionError('主人已暂停此角色，结果未继续处理')
            text = data['choices'][0]['message']['content']
            return text
        except Exception as exc:
            self.ledger.finish(rid, usage, ('cancelled' if isinstance(exc, InterruptedError) else 'failed') if usage else 'unknown')
            raise

    def json(self, system, user, **kwargs):
        text = self.call([{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], json_output=True, **kwargs)
        try:
            result = json.loads(text)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise RuntimeError('模型未返回有效的 JSON 对象，未保存推测结果')
