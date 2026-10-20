"""Native DSH Fish panel operations, backed by the existing Fish services."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
import uuid

from storage import database


class Workbench:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'data/social/workbench.db'
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='fish-chat')
        self._chat_instance = None
        self._selected_conversation = ''
        self._lock = threading.Lock()
        with database(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, conversation TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL)''')
            db.execute("UPDATE jobs SET status='failed',error='服务已重启，原记录保留，请继续任务',updated=? WHERE status IN ('queued','running')", (time.time(),))

    def chat(self):
        with self._lock:
            if self._chat_instance is None:
                from core import HarnessConfig, RuntimeManager
                from desktop_chat import DesktopChat
                self._chat_instance = DesktopChat(self.root, 'qq', RuntimeManager(HarnessConfig(self.root)))
            return self._chat_instance

    def settings(self):
        from governance import Governance
        cfg = Governance(self.root).config()
        used = int(Governance(self.root).snapshot()['usage'].get('calls', 0))
        limit = int(cfg['max_daily_calls'])
        return {'paused': cfg['paused'], 'daily_limits': {
                    'limit': limit, 'used': used, 'remaining': max(0, limit - used),
                    'unlimited': not cfg['daily_usage_limits']['qq']},
                'owner_permissions': cfg['owner_permissions']}

    def status(self):
        from governance import Governance
        from owner_mode import OwnerMode
        from social_context import SocialContext
        policy = Governance(self.root)
        contexts = SocialContext(self.root)
        space = contexts.owner_space(policy.owner_id())
        persona = space['persona'] if space else contexts.ensure_default('qq')
        mode = OwnerMode(self.root).get()
        from governance import CAPABILITIES
        return {**self.settings(), **mode, 'owner': policy.owner_id(), 'owner_capabilities': CAPABILITIES,
                'send_policy': policy.config()['send_policy'],
                'permissions': '、'.join(policy.owner_capabilities(desktop=True).values()),
                'persona': {k: persona[k] for k in ('id', 'name', 'body')},
                'paths': {'root': str(self.root), 'workspace': mode['workspace']}}

    def dispatch(self, action, params):
        from bot_control import BotControl
        from governance import Governance
        from memory_store import MemoryStore
        from persona_library import PersonaLibrary
        from social_context import SocialContext
        if not isinstance(params, dict):
            raise ValueError('操作参数必须是对象')
        if action == 'status':
            return self.status()
        if action == 'mode.set':
            from owner_mode import OwnerMode
            OwnerMode(self.root).set(params['mode'], params.get('workspace'))
            return self.status()
        if action == 'settings.get':
            return self.status()
        if action == 'settings.save':
            changes = {}
            if 'paused' in params:
                if type(params['paused']) is not bool:
                    raise ValueError('暂停开关必须是布尔值')
                changes['paused'] = params['paused']
            for key in ('owner_permissions', 'send_policy'):
                if key in params:
                    changes[key] = params[key]
            if 'daily_limits' in params:
                value = params['daily_limits']
                if type(value) is bool:
                    changes['daily_usage_limits'] = {'qq': value}
                elif isinstance(value, dict) and type(value.get('unlimited')) is bool:
                    changes['daily_usage_limits'] = {'qq': not value['unlimited']}
                    if value.get('limit') is not None:
                        changes['max_daily_calls'] = value['limit']
                else:
                    raise ValueError('请设置每日次数或选择无上限')
            Governance(self.root).save(changes)
            return self.status()
        if action == 'send.settings':
            return BotControl(self.root).execute('send_get')
        if action == 'memory.status':
            from memory_maintenance import MemoryMaintenance
            return MemoryMaintenance(self.root).status()
        if action == 'channels.status':
            import requests
            from core import HarnessConfig
            from governance import control_token
            from group_policy import GroupPolicy
            from storage import read_json
            config = read_json(self.root / 'channels/fish_qq/config.json', {})
            headers = {'x-fish-token': control_token(self.root)}
            loop = {}
            try:
                loop = requests.get('http://127.0.0.1:3102/status', headers=headers, timeout=2,
                                    proxies={'http': None, 'https': None, 'all': None}).json()
            except (requests.RequestException, ValueError):
                pass
            ready = False
            try:
                port = HarnessConfig(self.root).runtime_port('qq')
                ready = bool(requests.get(f'http://127.0.0.1:{port}/fish/status', headers=headers, timeout=2,
                                         proxies={'http': None, 'https': None, 'all': None}).json().get('ok'))
            except (requests.RequestException, ValueError):
                pass
            groups = []
            for group_id in config.get('allowlist', {}).get('group_ids', []):
                value = GroupPolicy(self.root).config('group:' + str(group_id))
                groups.append({'id': str(group_id), 'listen': bool(value.get('listen')), 'reply': bool(value.get('reply'))})
            bot = loop.get('bot') or config.get('bot') or {}
            return {'owner': Governance(self.root).owner_id(), 'bot': str(bot.get('id', 'unknown')),
                    'private_route': 'private:' + Governance(self.root).owner_id(), 'groups': groups,
                    'services': [{'name': '主人消息处理', 'ready': bool(loop), 'detail': '已连接' if loop else '未连接'},
                                 {'name': '原生 DSH 工作环境', 'ready': ready, 'detail': '可用' if ready else '未启动'},
                                 {'name': '工具与记忆', 'ready': True, 'detail': '已连接'}],
                    'note': '主人交办的单次群发送与群聊自动参与分开设置。'}
        if action == 'channels.update':
            from group_policy import GroupPolicy
            from storage import read_json
            group_id = str(params.get('group_id', ''))
            config = read_json(self.root / 'channels/fish_qq/config.json', {})
            allowed = {str(value) for value in config.get('allowlist', {}).get('group_ids', [])}
            if group_id not in allowed:
                raise ValueError('只能修改白名单群')
            changes = {key: params[key] for key in ('listen', 'reply') if key in params}
            if not changes:
                raise ValueError('没有要保存的群设置')
            if any(type(value) is not bool for value in changes.values()):
                raise ValueError('群设置必须是开关值')
            return GroupPolicy(self.root).configure('group:' + group_id, changes)
        if action == 'personas.list':
            contexts = SocialContext(self.root)
            return [{**row, 'body': contexts.persona(row['id'])['body']} for row in PersonaLibrary(self.root).list()]
        if action == 'personas.save':
            return PersonaLibrary(self.root).add(params['name'], params['body'], source='DSH Fish 人格编辑')
        if action == 'personas.import':
            rows = PersonaLibrary(self.root).import_text(params['text'], name=params.get('name', '导入人格'), source=params.get('source', 'DSH Fish 导入'))
            return [{'id': row['id'], 'name': row['name'], 'body': row['body']} for row in rows]
        if action == 'personas.apply':
            result = PersonaLibrary(self.root).apply(params['revision'])
            cid = params.get('conversation_id') or self._selected_conversation
            if cid:
                self.chat().reload_persona(cid, params['revision'])
            return result
        if action == 'memory.search':
            return MemoryStore(self.root, 'qq').search(params.get('query', ''), scope='owner', limit=60)
        if action == 'memory.save':
            store = MemoryStore(self.root, 'qq')
            memory_id = params.get('id')
            if memory_id and not any(row['id'] == memory_id for row in store.search(scope='owner', limit=10000)):
                raise ValueError('记忆不在主人可编辑范围')
            result = store.save(params['text'], category=params.get('category', '约定'), scope='owner', source='manual', source_text='主人在 DSH Fish 记忆页保存', memory_id=memory_id)
            return next(row for row in store.search(scope='owner', limit=10000) if row['id'] == result['id'])
        if action.startswith('schedules.'):
            control = BotControl(self.root)
            if action == 'schedules.list':
                return control.schedules()
            if action == 'schedules.create':
                return control.execute('schedule_create', {'target': 'owner', **params})
            if action == 'schedules.delete':
                return control.execute('schedule_delete', {'id': params['id']})
            if action == 'schedules.update':
                return control.execute('schedule_update', params)
        if action == 'chat.new':
            from owner_mode import OwnerMode
            contexts = SocialContext(self.root)
            owner_space = contexts.owner_space(contexts.owner())
            revision = params.get('revision') or (owner_space['revision'] if owner_space else contexts.ensure_default('qq')['id'])
            mode = params.get('mode', 'chat')
            workspace = OwnerMode(self.root).get()['workspace'] if mode == 'work' else ''
            return {'id': self.chat().new(params.get('title', '新对话'), revision=revision, mode=mode, workspace=workspace)}
        if action.startswith('attachments.'):
            import base64
            from attachment_store import AttachmentStore
            cid = str(params['id'])
            self.chat()._mode(cid)
            store = AttachmentStore(self.root, 'qq')
            if action == 'attachments.import':
                value = params.get('data', '')
                if not isinstance(value, str) or len(value) > 28 * 1024 * 1024:
                    raise ValueError('附件超过 20 MiB')
                try:
                    content = base64.b64decode(value, validate=True)
                except ValueError:
                    raise ValueError('附件内容读取失败') from None
                return store.import_bytes(cid, params['name'], content)
            if action == 'attachments.list':
                return store.list(cid)
            if action == 'attachments.search':
                return store.search(params.get('query', ''), conversation_id=cid)
        if action in ('chat.list', 'chat.snapshot'):
            result = self.chat().snapshot(params.get('id', ''))
            self._selected_conversation = result['conversation_id']
            for row in result['messages']:
                row['id'] = str(row['id'])
            return result
        if action == 'chat.mode':
            return self.chat().set_mode(params['id'], params['mode'], params.get('workspace', ''))
        if action == 'chat.send':
            text = params.get('text', '')
            attachments = params.get('attachments', [])
            if not isinstance(text, str) or not text.strip() and not attachments:
                raise ValueError('请输入消息或添加附件')
            cid = str(params['id'])
            self.chat().snapshot(cid)
            job = uuid.uuid4().hex
            now = time.time()
            with database(self.path) as db:
                db.execute('INSERT INTO jobs(id,conversation,status,created,updated) VALUES(?,?,?,?,?)', (job, cid, 'queued', now, now))
            self._executor.submit(self._send, job, cid, text, attachments)
            return {'job_id': job, 'status': 'queued'}
        if action == 'chat.job':
            with database(self.path) as db:
                row = db.execute('SELECT * FROM jobs WHERE id=?', (params['job_id'],)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            return dict(row)
        if action == 'chat.cancel':
            cid = str(params['id'])
            with database(self.path) as db:
                db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE conversation=? AND status='queued'", (time.time(), cid))
            chat = self.chat()
            return chat.cancel() if chat.active_cid == cid else {'ok': True}
        raise ValueError('未知操作：' + str(action))

    def _send(self, job, cid, text, attachments):
        with database(self.path) as db:
            row = db.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()
            if row is None or row['status'] != 'queued':
                return
            db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), job))
        try:
            self.chat().send(cid, text, attachments)
        except Exception as error:
            from dsh_web import safe_runtime_error
            state, message = 'failed', safe_runtime_error(str(error) or type(error).__name__)
        else:
            state, message = 'complete', ''
        with database(self.path) as db:
            db.execute('UPDATE jobs SET status=?,error=?,updated=? WHERE id=?', (state, message, time.time(), job))

    def close(self):
        if self._chat_instance:
            self._chat_instance.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
