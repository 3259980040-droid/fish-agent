"""Fish administration and the existing QQ delivery channel.

Plan CRUD and occurrences belong to FishSchedules. This facade preserves the
management API without creating a DSH session or calling its scheduler.
"""
from locations import project_path
import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from storage import database, read_json
from governance import Governance, control_token
import send_policy

ZONE = 'Asia/Shanghai'
TIMING = ('after_seconds', 'at', 'every_seconds', 'daily', 'weekly', 'cron')
PENDING_STATES = ('due', 'catchup', 'queued', 'running', 'sent')
KIND_TO_TIMING = {'after': 'after_seconds', 'at': 'at', 'every': 'every_seconds', 'daily': 'daily', 'weekly': 'weekly', 'cron': 'cron'}


def zone_info(name):
    try:
        return ZoneInfo(str(name or ZONE))
    except Exception:
        raise ValueError('未知时区：' + str(name))


def _as_items(value):
    return value if isinstance(value, list) else [value]


def timing_from_row(timing, row):
    """Model time arguments, falling back to fields the native schedule reports."""
    timing = dict(timing) if isinstance(timing, dict) else {}
    present = [k for k in TIMING if timing.get(k) not in (None, '', {}, [])]
    if present:
        return {present[0]: timing[present[0]]}, present[0]
    kind = row.get('kind', 'at')
    timing = KIND_TO_TIMING.get(kind, kind)
    if kind == 'after':
        return {'after_seconds': int(row.get('afterSeconds') or 60)}, timing
    if kind == 'every':
        return {'every_seconds': int(row.get('everySeconds') or 60)}, timing
    if kind in ('daily', 'weekly'):
        value = {'time': str(row.get('time', ''))[:8], 'time_zone': row.get('timeZone') or ZONE}
        if kind == 'weekly':
            value['weekdays'] = list(row.get('weekdays') or [])
        return {timing: value}, timing
    if kind == 'cron':
        return {'cron': {'expression': row.get('expression', ''), 'time_zone': row.get('timeZone') or ZONE}}, timing
    return {'at': {'date': str(row.get('scheduledAt', ''))[:10], 'time': str(row.get('scheduledAt', ''))[11:19],
                   'time_zone': row.get('timeZone') or ZONE}}, 'at'


def describe(timing, when_iso=''):
    """Owner-facing Asia/Shanghai description of a schedule."""
    key = next((k for k in TIMING if k in timing), '')
    value = timing.get(key)
    when = send_policy.display(when_iso) if when_iso else ''
    if key == 'after_seconds':
        n = int(value)
        text = (f'{n // 86400} 天后' if n >= 86400 else f'{n // 3600} 小时后' if n >= 3600 else f'{n // 60} 分钟后' if n >= 60 else f'{n} 秒后')
        return f'一次：{text}' + (f'（约 {when}）' if when else '')
    if key == 'at':
        value = value if isinstance(value, dict) else {}
        return '一次：' + (f"{value.get('date', '')} {value.get('time', '')}".strip() or when)
    if key == 'every_seconds':
        return f'每 {int(value)} 秒重复'
    if key in ('daily', 'weekly'):
        value = value if isinstance(value, dict) else {'time': str(value)}
        zone = value.get('time_zone') or ZONE
        if key == 'daily':
            return f"每天 {value.get('time', '')}（{zone}）"
        names = '一二三四五六日'
        days = ','.join('周' + names[int(d) - 1] if 1 <= int(d) <= 7 else str(d) for d in value.get('weekdays') or [])
        return f"每{days or '周?'} {value.get('time', '')}（{zone}）"
    if key == 'cron':
        value = value if isinstance(value, dict) else {'expression': str(value)}
        return f"Cron {value.get('expression', '')}（{value.get('time_zone') or ZONE}）"
    return str(timing)


class BotControl:
    def __init__(self, root, *, transport=None):
        self.root = Path(root); self.path = project_path(self.root, 'data/social/owner-control.db')
        # transport(target, text, request_id) -> {'ok': bool, 'payload': {...}, 'error': {...}}; default real channel.
        self.transport = transport or self._channel_transport
        with database(self.path) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sessions(target TEXT PRIMARY KEY,session TEXT,revision TEXT);
                CREATE TABLE IF NOT EXISTS sends(id TEXT PRIMARY KEY,target TEXT,text TEXT,state TEXT,receipt TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS scheduled(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,target TEXT,session TEXT,
                    title TEXT,prompt TEXT,original TEXT,timing TEXT,timing_key TEXT,request TEXT,state TEXT,created REAL,updated REAL);
                CREATE TABLE IF NOT EXISTS occurrences(schedule_id TEXT,slot TEXT,state TEXT,attempts INTEGER DEFAULT 0,
                    receipt_id TEXT,note TEXT,created REAL,updated REAL,execution TEXT DEFAULT '',PRIMARY KEY(schedule_id,slot));''')
            existing = {row['name'] for row in db.execute('PRAGMA table_info(occurrences)')}
            if 'execution' not in existing:
                db.execute("ALTER TABLE occurrences ADD COLUMN execution TEXT DEFAULT ''")

    @property
    def plans(self):
        from fish_schedules import FishSchedules
        return FishSchedules(self)

    def validate_target(self, target):
        from social_context import SocialContext
        target = str(target)
        if target == 'owner':
            target = 'private:' + SocialContext(self.root).owner()
        allowed, reason = send_policy.channel_allows(self.root, target)
        if not allowed:
            raise PermissionError(reason)
        return target

    def _rows(self):
        return self.plans.rows()

    def create(self, params, *, session=None):
        return self.plans.create(params, session=session)

    def schedules(self):
        return [self.plans.view(r) for r in self.plans.rows() if r['state'] != 'deleted']

    def update(self, params):
        return self.plans.update(params)

    def delete(self, schedule_id):
        return self.plans.delete(schedule_id)

    def run_now(self, schedule_id):
        return self.plans.run_now(schedule_id)

    def detect(self, now=None):
        return self.plans.detect(now)

    def dispatch(self, changes=None):
        # The Fish scheduler owns the one queue; this action only exposes pending work.
        if changes is None:
            self.plans.detect()
        return self.plans.pending()

    def run_occurrence(self, schedule_id, slot, *, run_turn=None):
        return self.plans.run(schedule_id, slot, run_turn=run_turn)

    def native_occurrence(self, identity, text):
        raise ValueError('DSH 定时执行入口已退役；请使用 Fish 任务页中的本地计划')

    def context(self, session):
        raise ValueError('Fish 定时任务不再经过 DSH 会话注入，请使用 Fish 本地计划')

    def send(self, identity, text, request_id):
        occurrence = self.plans.active(identity['session'])
        if not occurrence:
            raise PermissionError('当前没有正在执行的 Fish 定时任务')
        prefix = 'sched:' + occurrence['schedule_id'] + ':' + occurrence['slot']
        if not request_id.startswith(prefix):
            raise ValueError('本次发送标识与当前到期任务不匹配')
        return self._deliver(occurrence['target'], text, request_id,
            source='owner-reminder' if occurrence['purpose'] == 'reminder' else 'fish-autonomous',
            scheduled=occurrence['purpose'] == 'reminder',
            context='owner:schedule' if occurrence['purpose'] == 'reminder' else 'fish:autonomous')

    def execute(self, action, params, identity=None):
        from persona_library import PersonaLibrary
        policy = Governance(self.root)
        identity = identity or {}
        desktop = bool(identity.get('desktop'))
        if action == 'status':
            return {'owner': policy.owner_id(), 'permissions': policy.owner_capabilities(desktop=desktop)}
        if action in ('pause', 'resume'): return policy.save({'paused': action == 'pause'})
        if action.startswith('memory_'):
            return self._memory(action, params, identity)
        if action == 'personas': return PersonaLibrary(self.root).list()
        if action == 'persona_import':
            return [{'id': p['id'], 'name': p['name']} for p in PersonaLibrary(self.root).import_text(params['text'], name=params.get('name', '导入人格'), source=params.get('source', '主人对话'))]
        if action == 'persona_apply': return PersonaLibrary(self.root).apply(params['revision'], params.get('target', 'owner'), new=bool(params.get('new', False)))
        if action == 'message_send': return self._manual_send(params, identity)
        if action == 'file_send':
            from owner_files import send_file
            return send_file(self.root,params.get('target','owner'),params['path'],params['request_id'],session=identity['session'])
        if action in ('qzone_draft','qzone_publish'):
            from social_publish import execute
            return execute(self.root, action, params)
        if action.startswith('send_'):
            return self._send_settings(action, params, identity)
        if action.startswith('group_'):
            from group_tests import GroupTests
            from group_policy import GroupPolicy
            group = str(params.get('group_id', ''))
            self.validate_target('group:' + group)
            trial = GroupTests(self.root).current(group)
            group_policy = GroupTests(self.root).policy(trial) if trial else GroupPolicy(self.root)
            return group_policy.config('group:' + group) if action == 'group_get' else group_policy.configure('group:' + group, params['settings'])
        if action == 'schedule_list': return self.schedules()
        if action == 'schedule_create': return self.create(params, session=params.get('session'))
        if action == 'schedule_delete': return self.delete(params['id'])
        if action == 'schedule_update': return self.update(params)
        if action == 'schedule_run_now': return self.run_now(params['id'])
        if action == 'schedule_check':
            changes = self.detect()
            return self.dispatch(changes) if params.get('run') else changes
        raise ValueError('未知管理操作')

    def _memory(self, action, params, identity):
        from memory_store import MemoryStore
        scope = str(identity.get('scope', 'owner'))
        if scope.startswith('chat:group:'): scope = ':'.join(scope.split(':')[:3])
        elif scope.startswith('control:group:'): scope = 'chat:' + ':'.join(scope.split(':')[1:3])
        else: scope = 'owner'
        store = MemoryStore(self.root, 'qq')
        if action == 'memory_search': return store.search(params.get('query', ''), scope=scope, limit=30)
        capability = 'memory.write' if action == 'memory_save' else 'memory.delete'
        if identity and not identity.get('desktop') and not Governance(self.root).permitted(identity.get('sender'), capability):
            raise PermissionError('主人尚未启用此记忆管理权限')
        if action == 'memory_save':
            from memory_evidence import save_from_tool
            return save_from_tool(self.root, identity, params, scope=scope)
        if not any(x['id'] == params['id'] for x in store.search(scope=scope, limit=200)):
            raise ValueError('当前范围内未找到此记忆')
        return store.delete(params['id'])

    def _owner_request(self, identity):
        """Identity for owner-authorized manual sends; program state, no per-message prompt."""
        from owner_computer import authorized
        if identity.get('desktop') and identity.get('owner'):
            return True
        if not (identity.get('owner') and identity.get('role') == 'qq'):
            return False
        return bool(authorized(self.root, identity) if not str(identity.get('scope', '')).startswith('control:')
                    else Governance(self.root).permitted(identity.get('sender', ''), 'computer.work'))

    def _manual_send(self, params, identity):
        target = self.validate_target(params.get('target', 'owner'))
        if not identity.get('scope'):
            raise PermissionError('缺少已核验的会话入口')
        # Reaching this tool with a bound owner identity IS the owner's explicit request in the current
        # session, so the decision and the transport use that same context (never an empty one).
        context = 'owner:' + str(identity['scope'])
        ok, reason = send_policy.decide(self.root, target, action='group_send' if target.startswith('group:') else 'owner_reply', context=context)
        if not ok: raise PermissionError(reason)
        if target.startswith('group:') and not (identity.get('owner') and self._owner_request(identity)):
            raise PermissionError('发往群聊需要主人在当前会话的明确要求；公开群自动回复保持关闭')
        if not all(identity.get(k) for k in ('owner', 'scope', 'sender', 'session')) and not identity.get('desktop'):
            raise PermissionError('单次交办发送仅用于已核验主人工作入口')
        request_id = params.get('request_id') or 'owner-send:' + identity['scope'] + ':' + uuid.uuid4().hex[:12]
        occurrence = self.plans.active(identity.get('session', ''))
        if occurrence:
            prefix = 'sched:' + occurrence['schedule_id'] + ':' + occurrence['slot']
            request_id = prefix + ':management:' + request_id
        reminder = bool(occurrence and occurrence['purpose'] == 'reminder')
        result = self._deliver(target, params['text'], request_id,
            source=('owner-reminder' if reminder else 'fish-autonomous') if occurrence else 'owner-request:' + str(identity.get('scope')),
            scheduled=reminder, context=context)
        if result.get('error'): raise PermissionError(result['error'])
        return result

    def _send_settings(self, action, params, identity):
        policy = Governance(self.root)
        values = self._load_settings()
        if action == 'send_get': return values
        changes = params.get('settings') if isinstance(params.get('settings'), dict) else params
        if action == 'send_set':
            values = policy.save({'send_policy': changes})['send_policy']
            return {'message': '发送设置已保存', **self._load_settings()}
        if action == 'send_usage': return send_policy.summary(self.root)
        if action == 'send_test':
            target = self.validate_target(params.get('target', 'owner'))
            ok, reason = send_policy.decide(self.root, target, action='scheduled_result' if target.startswith('private:') else 'group_send',
                                            scheduled=True, context='control:' + target)
            if not ok: raise PermissionError(reason)
            request_id = 'owner-send:policy-test:' + str(identity.get('session', 'desktop'))[:40] + ':' + uuid.uuid4().hex[:8]
            return self._deliver(target, params.get('text') or '发送设置测试：程序可以按当前设置向这个目标发送。', request_id, source='send-test')
        raise ValueError('未知发送设置操作')

    def _load_settings(self):
        cfg = Governance(self.root).config()
        group_policy = {}
        try:
            from group_policy import GroupPolicy
            for meta in self._rows():
                if meta['target'].startswith('group:') and meta['target'] not in group_policy:
                    group_policy[meta['target']] = GroupPolicy(self.root).config(meta['target'])
        except Exception:
            pass
        return {'send_policy': cfg['send_policy'], 'editable': send_policy.SETTABLE,
                'owner_private_route': 'private:' + Governance(self.root).owner_id(),
                'group_policy': {k: {g: v for g, v in c.items() if g in ('listen', 'reply', 'mode')} for k, c in group_policy.items()},
                'public_auto_reply': '由群聊监听/回复开关控制，默认关闭', 'capabilities': send_policy.settings_view(self.root)['capabilities'],
                'actual': send_policy.summary(self.root)}

    # ---------------------------------------------------------------- delivery
    def deliver_bubbles(self, target, bubbles, request_id, **options):
        from chat_output import transport_parts
        result = None
        for index, part in enumerate(transport_parts(bubbles, group=target.startswith('group:'))):
            if part['delay']:
                time.sleep(part['delay'])
            text = ''.join(str(s.get('text') or '') for s in part['segments'] if s.get('type') == 'text')
            result = self._deliver(target, text, request_id + ':part:' + str(index), segments=part['segments'], **options)
            if result['state'] != 'SUCCEEDED':
                return result
        if result is None:
            raise ValueError('没有可发送的内容')
        return result

    def _deliver(self, target, text, request_id, *, source='', scheduled=False, context='', transport=None, segments=None, source_tools=()):
        # No validate_target here: callers decide the target (owner route or the schedule's bound group).
        if not isinstance(text, str) or (not text.strip() and not segments): raise ValueError('消息不能为空')
        if not isinstance(request_id, str) or not request_id.strip(): raise ValueError('需要本次执行的唯一发送编号')
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM sends WHERE id=?', (request_id,)).fetchone()
            if previous:
                if previous['target'] != target or previous['text'] != text: raise ValueError('发送编号已用于其他内容')
                return {'state': previous['state'], 'receipt': json.loads(previous['receipt'] or '{}')}
            db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?)', (request_id, target, text, 'EXECUTING', '{}', time.time()))
        from qq_store import QQStore
        store = QQStore(self.root)
        conversation = store.get_or_create_conversation(target, 'qq')
        message = store.record_message(conversation['id'], target, 'Fish', 'outbound', '', None,
            segments or [{'type': 'text', 'text': text}], text, 'sending', runtime_turn_id=request_id)
        action = ('scheduled_result' if scheduled else 'fish_autonomous' if source == 'fish-autonomous'
                  else 'group_send' if target.startswith('group:') else 'owner_reply')
        ok, reason = send_policy.decide(self.root, target, action=action, scheduled=scheduled, context=context)
        if ok:
            try:
                Governance(self.root).check_output(text)
            except (PermissionError, OSError, ValueError) as exc:
                ok, reason = False, str(exc)
        if not ok:
            receipt = {'ok': False, 'error': {'code': 'policy_blocked', 'message': reason, 'may_have_sent': False}}
            state = 'BLOCKED'
        else:
            try:
                sender = transport or self.transport
                from fish_agent.settings.tools import ToolSettings
                ToolSettings(self.root).require_delivery(target,source_tools)
                receipt = (sender(target, text, request_id, segments=segments,source_tools=source_tools)
                           if sender == self._channel_transport else sender(target, text, request_id))
                payload, error = receipt.get('payload') or {}, receipt.get('error') or {}
                confirmed = bool(receipt.get('ok') and (payload.get('message_id') or payload.get('message_ids')))
                state = ('SUCCEEDED' if confirmed else 'UNKNOWN' if receipt.get('ok') or error.get('may_have_sent') else 'FAILED')
            except PermissionError as exc:
                receipt={'ok':False,'error':{'code':'capability_closed','message':str(exc),'may_have_sent':False}}
                state='BLOCKED'
            except Exception as exc:
                import traceback as _tb
                receipt = {'ok': False, 'error': {'code': type(exc).__name__, 'message': str(exc), 'may_have_sent': True,
                                                 'trace': _tb.format_exc()[-1200:]}}
                state = 'UNKNOWN'
        with database(self.path) as db:
            db.execute('UPDATE sends SET state=?,receipt=? WHERE id=?', (state, json.dumps(receipt, ensure_ascii=False), request_id))
        payload = receipt.get('payload') or {}
        store.update_message(message, status={'SUCCEEDED': 'sent', 'UNKNOWN': 'uncertain'}.get(state, 'failed'),
            channel_message_id=str(payload.get('message_id') or next(iter(payload.get('message_ids') or []), '')),
            error='' if state == 'SUCCEEDED' else json.dumps(receipt.get('error') or '发送未确认', ensure_ascii=False))
        send_policy.record_send(self.root, request_id=request_id, chat_id=target, text=text, action=action, state=state,
                                receipt=receipt, source=source or action, target_label=target)
        return {'state': state, 'target': target, 'request_id': request_id, 'receipt': receipt, **({} if ok else {'error': reason})}

    def _channel_transport(self, target, text, request_id, reply_to='', *, segments=None, source_tools=()):
        """One-shot send over the fish-qq channel (same protocol/token source as the QQ loop).

        Connects once; on failure the outcome is UNKNOWN and the caller must not blind-retry.
        `reply_to` quotes one real message of the same chat; it is optional, so every
        existing caller sends the exact payload it sent before.
        ``FISH_QQ_TRANSPORT`` (documented in RESULT.md) may name an importable factory
        ``(root) -> callable(target, text, key)`` for embeds that own a live channel client.
        """
        root = self.root
        factory = os.environ.get('FISH_QQ_TRANSPORT', '').strip()
        if factory:
            module, _, name = factory.partition(':')
            transport = getattr(__import__(module), name or 'transport')(root)
            return transport(target, text, request_id)

        async def deliver():
            from websockets.asyncio.client import connect
            from secrets_store import SecretStore
            token = os.environ.get('FISH_QQ_TOKEN', '') or SecretStore(root).get('channel-core', 'FISH_QQ_TOKEN')
            if not token:
                return {'ok': False, 'error': {'code': 'no_channel_token', 'may_have_sent': False}}
            async with connect('ws://127.0.0.1:3101/ws', additional_headers={'Authorization': 'Bearer ' + token},
                               max_size=1024 * 1024, open_timeout=5) as ws:
                await ws.send(json.dumps({'v': 1, 'type': 'hello', 'id': 'scheduled-hello', 'ts': '',
                                          'payload': {'client': 'fish-scheduled-sender'}}))
                hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if hello.get('type') != 'hello.ok':
                    return {'ok': False, 'error': {'code': 'channel_hello_failed', 'may_have_sent': False}}
                from fish_agent.settings.tools import ToolSettings
                ToolSettings(root).require_delivery(target,source_tools)
                action_id = 'core-' + uuid.uuid4().hex[:12]
                await ws.send(json.dumps({'v': 1, 'type': 'action', 'id': action_id, 'action': 'send_message', 'ts': '',
                    'payload': {'chat_id': target, 'segments': segments or [{'type': 'text', 'text': text}],
                                **({'reply_to': str(reply_to)} if reply_to else {}),
                                'idempotency_key': 'scheduled:' + request_id}}))
                while True:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), 30))
                    if frame.get('type') == 'action.result' and frame.get('id') == action_id:
                        return frame
        return asyncio.run(deliver())


__all__ = ['BotControl', 'describe', 'timing_from_row', 'ZONE', 'TIMING']
