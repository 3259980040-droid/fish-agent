"""Role-specific DeepSeek calls and an atomic request/usage ledger."""
from __future__ import annotations
from locations import project_path
import json
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
import requests
import httpx
from storage import database, read_json, role_dir


@dataclass(frozen=True)
class ChatOptions:
    effort: str = 'high'
    max_tokens: int = 8192
    timeout_seconds: float = 180


@dataclass(frozen=True)
class ChatResult:
    text: str
    usage: dict
    finish_reason: str
    tool_calls: tuple = ()
    reasoning_content: str = field(default='', repr=False)
    first_output_at: float | None = None



class ModelRequestError(RuntimeError):
    """An actual provider HTTP failure, with safe actionable text and its status."""
    def __init__(self, status_code, payload=None):
        from governance import sanitize_fault_detail
        self.status_code = int(status_code)
        detail = payload.get('error', payload) if isinstance(payload, dict) else {}
        detail = detail if isinstance(detail, dict) else {}
        raw = str(detail.get('message') or detail.get('code') or '')
        safe = sanitize_fault_detail(raw).partition(': ')[2]
        self.provider_code = str(detail.get('code') or '')[:80]
        reasons = {400:'模型请求格式不符合服务要求', 401:'模型认证失败，请检查 API 密钥',
                   402:'DeepSeek API 余额不足，请充值当前 API 账户后再试',
                   403:'模型服务拒绝访问', 422:'模型请求参数不符合服务要求',
                   429:'模型服务请求过于频繁', 500:'模型服务内部错误',503:'模型服务暂时不可用'}
        reason = reasons.get(self.status_code, '模型请求失败')
        if safe and self.status_code in (400,422): reason += '：' + safe
        super().__init__(f'{reason}（HTTP {self.status_code}）')


def response_error(status_code, response_body):
    try:
        payload = json.loads(response_body)
    except (ValueError, TypeError, UnicodeDecodeError):
        payload = {}
    return ModelRequestError(status_code, payload)


def chat_options(root):
    settings = read_json(project_path(root, 'harness/control.json'))
    effort = settings.get('chat_reasoning_effort', 'high')
    maximum = int(settings.get('desktop_chat_max_tokens', 8192))
    timeout = float(settings.get('desktop_chat_timeout_seconds', 180))
    if effort not in ('off', 'low', 'high', 'max') or not 256 <= maximum <= 65536 or not 1 <= timeout <= 1800:
        raise ValueError('本地聊天的思考强度、输出 token 或超时设置无效')
    return ChatOptions(effort, maximum, timeout)


class ModelLedger:
    def __init__(self, root: Path, role: str):
        self.root, self.role = root, role
        self.path = role_dir(root, role) / 'model-usage.db'
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,day TEXT,origin TEXT,status TEXT,input_tokens INTEGER DEFAULT 0,output_tokens INTEGER DEFAULT 0,created REAL)')
            # External tool calls (search/fetch) never pass through this gateway's chat path,
            # so they get their own event table in the same db; requests keeps its meaning.
            db.execute('CREATE TABLE IF NOT EXISTS tool_events(id TEXT PRIMARY KEY,day TEXT,origin TEXT,tool TEXT,'
                       'provider TEXT,ok INTEGER,cached INTEGER,elapsed_ms INTEGER,result_count INTEGER,'
                       'text_chars INTEGER,meter TEXT,detail TEXT,created REAL)')

    def reserve(self, origin):
        from governance import daily_limits_enabled
        settings = read_json(project_path(self.root, 'harness/control.json'))
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
        enabled = daily_limits_enabled(read_json(project_path(self.root, 'harness/control.json')), self.role)
        note = ('token 按服务商回传统计；未回传及并行在途请求可能超出 token 阈值，请求次数为硬上限。' if enabled
                else '本角色每日用量限制已关闭；调用次数和服务商回传 token 继续累计统计。')
        return {**dict(row), 'unreported': pending, 'daily_limits_enabled': enabled, 'note': note}

    def record_tool(self, tool, *, origin='web-tool', provider='', ok=True, cached=False, elapsed_ms=0,
                    result_count=0, text_chars=0, meter='unknown', detail=''):
        """One external tool call. meter is only what the provider reported, never an estimate."""
        reported = 'unknown' if meter is None else str(meter).strip()[:200]
        if not reported or reported.lower() in ('none', 'null'):
            reported = 'unknown'
        event = uuid.uuid4().hex
        with database(self.path) as db:
            db.execute('INSERT INTO tool_events(id,day,origin,tool,provider,ok,cached,elapsed_ms,result_count,'
                       'text_chars,meter,detail,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (event, date.today().isoformat(), str(origin or '')[:60], str(tool or '')[:60],
                 str(provider or '')[:60], 1 if ok else 0, 1 if cached else 0, max(0, int(elapsed_ms or 0)),
                 max(0, int(result_count or 0)), max(0, int(text_chars or 0)), reported,
                 str(detail or '')[:120], time.time()))
        return event

    def tool_snapshot(self):
        """Today's external tool totals; no amount is ever derived from latency or hit counts."""
        day = date.today().isoformat()
        with database(self.path) as db:
            row = db.execute('SELECT COUNT(*) calls,COALESCE(SUM(ok),0) ok,'
                             'COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) failed,'
                             'COALESCE(SUM(cached),0) cached,COALESCE(SUM(elapsed_ms),0) elapsed_ms,'
                             'COALESCE(SUM(result_count),0) result_count,COALESCE(SUM(text_chars),0) text_chars,'
                             "COALESCE(SUM(CASE WHEN meter='unknown' THEN 1 ELSE 0 END),0) meter_unknown "
                             'FROM tool_events WHERE day=?', (day,)).fetchone()
            per_tool = db.execute('SELECT tool,COUNT(*) calls,COALESCE(SUM(ok),0) ok,COALESCE(SUM(cached),0) cached '
                                  'FROM tool_events WHERE day=? GROUP BY tool ORDER BY tool', (day,)).fetchall()
            reported = db.execute("SELECT COUNT(*) FROM tool_events WHERE day=? AND meter<>'unknown'", (day,)).fetchone()[0]
        return {**dict(row), 'day': day, 'meter_reported': reported,
                'by_tool': [dict(item) for item in per_tool],
                'note': '外部检索与抓取按调用事件统计；meter 只记录提供商真实回传的计量字段，其余为 unknown，金额与 token 从不估算。'}


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
        self.activity = {}

    def chat(self, messages, *, options=None, cancelled=None, origin='desktop-chat', tools=None, on_event=None):
        """Direct main chat. Closing local HTTP never claims provider-side cancellation."""
        options = options or chat_options(self.root)
        self._check_chat(cancelled)
        rid = self.ledger.reserve(origin)
        self.activity = {'phase': 'connecting', 'execution': 'fish-api', 'model': self.model}
        state = {'usage': None, 'first_output_at': None, 'on_event':on_event}
        if on_event: on_event('phase','connecting')
        try:
            result = asyncio.run(self._chat_wait(messages, options, cancelled, state, tools=tools))
            self._check_chat(cancelled)
        except Exception as exc:
            status = 'cancelled' if isinstance(exc, InterruptedError) else 'failed'
            self.ledger.finish(rid, state['usage'], status)
            self.activity = {'phase': status, 'execution': 'fish-api'}
            raise
        self.ledger.finish(rid, result.usage, 'done' if result.usage else 'unknown')
        self.activity = {'phase': 'complete', 'execution': 'fish-api', 'model': self.model}
        return result

    def _check_chat(self, cancelled):
        if not self.key:
            raise ValueError('请先在 Fish 角色设置保存 API Key，或设置该角色的 Fish 专用环境变量')
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('本地已取消等待；未确认服务商是否停止计算')
        from governance import Governance
        if Governance(self.root, self.role).config()['paused']:
            raise PermissionError('主人已暂停此角色')

    async def _chat_wait(self, messages, options, cancelled, state, *, tools=None):
        task = asyncio.create_task(self._chat_stream(messages, options, state, **({'tools':tools} if tools else {})))
        deadline = time.monotonic() + options.timeout_seconds
        try:
            while not task.done():
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError('本地已取消等待并关闭连接；未确认服务商是否停止计算')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('模型回复超时，本地已关闭连接；未确认服务商是否停止计算')
                await asyncio.wait({task}, timeout=min(.1, remaining))
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _chat_stream(self, messages, options, state, *, tools=None):
        from network_config import httpx_options
        body = {'model': self.model, 'messages': messages, 'max_tokens': options.max_tokens,
                'thinking': {'type': 'disabled' if options.effort == 'off' else 'enabled'},
                'stream': True, 'stream_options': {'include_usage': True}}
        if options.effort != 'off':
            body['reasoning_effort'] = options.effort
        if tools: body['tools'] = tools
        chunks, finish, calls, reasoning = [], None, {}, []
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(options.timeout_seconds, connect=8),
                                         **httpx_options(self.root, self.base)) as client:
                async with client.stream('POST', self.base.rstrip('/') + '/chat/completions',
                                         headers={'Authorization': 'Bearer ' + self.key}, json=body) as response:
                    if response.status_code != 200:
                        raise response_error(response.status_code, await response.aread())
                    async for line in response.aiter_lines():
                        if not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if data == '[DONE]':
                            break
                        try:
                            event = json.loads(data)
                            if event.get('error'):
                                raise RuntimeError('服务商在生成过程中返回错误，未取得完整回复')
                            if event.get('usage'):
                                state['usage'] = event['usage']
                            for choice in event.get('choices', []):
                                delta = choice.get('delta') or {}
                                if state['first_output_at'] is None and any(delta.get(k) for k in ('reasoning_content', 'content', 'tool_calls')):
                                    state['first_output_at'] = time.time()
                                    if state.get('on_event'): state['on_event']('first_output',state['first_output_at'])
                                # Protocol reasoning remains separate from the delivered answer.
                                if delta.get('reasoning_content'):
                                    self.activity = {'phase': 'thinking', 'execution': 'fish-api'}
                                    if tools: reasoning.append(delta['reasoning_content'])
                                    if state.get('on_event'): state['on_event']('reasoning',delta['reasoning_content'])
                                for part in delta.get('tool_calls') or []:
                                    call = calls.setdefault(part['index'], {'id': '', 'type': 'function',
                                        'function': {'name': '', 'arguments': ''}})
                                    if part.get('id'): call['id'] += part['id']
                                    function = part.get('function') or {}
                                    for name in ('name', 'arguments'):
                                        call['function'][name] += function.get(name) or ''
                                if delta.get('content'):
                                    chunks.append(delta['content'])
                                    if state.get('on_event') and self.activity.get('phase')!='replying': state['on_event']('phase','replying')
                                    self.activity = {'phase': 'replying', 'execution': 'fish-api'}
                                if choice.get('finish_reason'):
                                    finish = choice['finish_reason']
                        except (ValueError, TypeError, AttributeError):
                            raise RuntimeError('模型返回了无效的数据流，未取得完整回复') from None
        except httpx.TimeoutException:
            raise TimeoutError('模型连接或回复超时，本地已关闭连接') from None
        except httpx.RequestError:
            raise ConnectionError('Fish 模型连接中断，未取得完整回复') from None
        if finish == 'tool_calls' and tools and calls:
            if any(not c['id'] or not c['function']['name'] for c in calls.values()):
                raise RuntimeError('模型返回了不完整的工具调用，未执行')
            return ChatResult(''.join(chunks), state['usage'] or {}, finish,
                              tuple(calls[i] for i in sorted(calls)), ''.join(reasoning), state['first_output_at'])
        if finish != 'stop':
            reason = '输出 token 上限' if finish == 'length' else '数据流未正常结束（' + str(finish) + '）'
            raise RuntimeError('回复没有完整生成：' + reason + '。原请求保留，未将片段冒充完整回复')
        text = ''.join(chunks).strip()
        if not text:
            raise RuntimeError('模型没有返回可展示的回复正文')
        return ChatResult(text, state['usage'] or {}, finish, reasoning_content=''.join(reasoning), first_output_at=state['first_output_at'])

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
                raise response_error(response.status_code, response.content)
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
