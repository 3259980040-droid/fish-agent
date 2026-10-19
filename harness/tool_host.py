"""Authenticated loopback bridge between DSH tools and Fish's private stores."""
from __future__ import annotations
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import requests
from governance import Governance, control_token
from memory_store import MemoryStore
from model_gateway import ModelGateway, ModelLedger
from plugin_registry import PluginRegistry
from storage import database, read_json, write_json

_hosts = {}
_host_lock = threading.Lock()


def port(root):
    return int(read_json(Path(root) / 'harness/agents.json').get('runtime', {}).get('tool_host_port', 3103))


def role_token(root, role):
    return hmac.new(control_token(root).encode(), ('fish-tools:' + role).encode(), hashlib.sha256).hexdigest()


def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def builtins(role):
    string = {'type': 'string'}
    tools = [
        {'name': 'fish_memory_search', 'description': '搜索当前聊天可见的长期记忆。记忆是带来源的资料，不能授予权限。', 'parameters': schema({'query': string}, ['query'])},
        {'name': 'fish_memory_save', 'description': '主人明确要求记住时保存长期偏好或事实；禁止保存密码、密钥和推测。', 'parameters': schema({'text': string, 'category': string}, ['text'])},
        {'name': 'fish_memory_delete', 'description': '按主人要求删除记忆并防止从原来源重新学习。先搜索确认 ID。', 'parameters': schema({'id': string}, ['id'])},
        {'name': 'fish_interests_read', 'description': '读取主人指定的 B 站研究方向和排除范围。个人反馈不能自行改写这些方向。', 'parameters': schema({})},
        {'name': 'fish_interests_update', 'description': '仅在主人明确要求时更改研究方向、关键词或排除词。保留未传入的配置。', 'parameters': schema({k: {'type': 'array', 'items': string} for k in ('directions', 'keywords', 'exclude', 'ups')})},
    ]
    if role == 'qq':
        tools.extend([
            {'name': 'fish_emoji_search', 'description': '按当前聊天情绪搜索已收藏的表情包，只返回 ID 和客观描述。', 'parameters': schema({'context': string}, ['context'])},
            {'name': 'fish_emoji_choose', 'description': '本次文字回复后附带一个合适表情。可不调用，避免刷屏；只有本地库内 ID 可用，发送频率由程序控制。', 'parameters': schema({'id': string}, ['id'])},
            {'name': 'fish_emoji_delete', 'description': '主人要求删除指定表情时调用；禁止该图片自动再次收藏。', 'parameters': schema({'id': string}, ['id'])},
        ])
    else:
        tools.extend([
            {'name': 'fish_research_search', 'description': '搜索已完成的视频研究，取得摘要和来源。', 'parameters': schema({'query': string}, ['query'])},
            {'name': 'fish_research_detail', 'description': '读取指定 BV 号的研究全文、观点、观察范围和已核实的引用。', 'parameters': schema({'bvid': string}, ['bvid'])},
        ])
    return tools


class ToolHost:
    def __init__(self, root):
        self.root = Path(root)
        self.registry = PluginRegistry(self.root)
        self.path = self.root / 'data/tool-host.db'
        self.cancelled = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        with database(self.path) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS identities(role TEXT,session TEXT,scope TEXT,owner INTEGER,desktop INTEGER,sender TEXT,updated REAL,PRIMARY KEY(role,session));
            CREATE TABLE IF NOT EXISTS emoji_choices(session TEXT PRIMARY KEY,asset TEXT,created REAL);''')
            db.execute('CREATE TABLE IF NOT EXISTS group_contexts(session TEXT PRIMARY KEY,scope TEXT,text TEXT,updated REAL)')
        self.registry.publish()
        from research_autonomy import ResearchWorker
        self.research = ResearchWorker(self.root)

    def identity(self, role, session):
        with database(self.path) as db:
            row = db.execute('SELECT * FROM identities WHERE role=? AND session=?', (role, session)).fetchone()
        if not row:
            raise PermissionError('会话没有经过程序身份核验')
        if row['owner'] and not row['desktop'] and not Governance(self.root).is_owner(row['sender']):
            raise PermissionError('主人账号已变更，请重新核验会话身份')
        return dict(row)

    def bind(self, role, session, scope, sender='', desktop=False):
        from social_context import SocialContext
        if role not in ('qq', 'research') or not session:
            raise ValueError('无效的角色或会话')
        isolated = str(scope).startswith('space:')
        if isolated:
            space = SocialContext(self.root).space(str(scope)[6:])
            if space['role'] != role:
                raise PermissionError('窗口不属于当前角色')
            if not desktop and not Governance(self.root).is_owner(sender):
                raise PermissionError('独立测试会话仅供主人账号与本地控制台使用')
            if space['session'] and space['session'] != session:
                raise PermissionError('此窗口已经绑定其它会话')
            SocialContext(self.root).bind_runtime(space['id'], session)
        owner = bool(desktop or ((scope == 'owner' or isolated) and Governance(self.root).is_owner(sender)))
        if scope == 'owner' and not owner:
            raise PermissionError('主人身份不匹配')
        if not owner and not scope.startswith('chat:'):
            raise PermissionError('普通聊天需使用独立范围')
        with database(self.path) as db:
            db.execute('INSERT OR REPLACE INTO identities VALUES(?,?,?,?,?,?,?)', (role, session, scope, int(owner), int(desktop), sender, time.time()))
            db.execute('DELETE FROM emoji_choices WHERE session=?', (session,))
            identities = [dict(r) for r in db.execute('SELECT * FROM identities WHERE role=? ORDER BY updated DESC LIMIT 3000', (role,))]
            for identity in identities:
                if identity['scope'].startswith('space:'):
                    identity['isolated'] = True
                    identity['allowed_paths'] = [str(SocialContext(self.root).directory(identity['scope'][6:]).resolve())]
                elif role == 'qq' and identity['scope'].startswith('chat:group:'):
                    deployment = SocialContext(self.root).deployment(identity['scope'].split(':')[2])
                    if deployment.get('enabled'):
                        identity['persona_revision'] = deployment['revision']
            write_json(self.root / f'harness/runtime/{role}.identities.json', {r['session']: r for r in identities})
        return {'ok': True}

    def execute(self, role, session, name, arguments, call_id):
        identity = self.identity(role, session)
        policy = Governance(self.root, role)
        if policy.config()['paused']:
            raise PermissionError('主人已暂停此角色')
        def require(capability):
            if not identity['owner'] or (not identity['desktop'] and (not Governance(self.root).is_owner(identity['sender']) or capability not in policy.config()['owner_permissions'])):
                raise PermissionError('主人未授予此管理权限：' + capability)
        if identity['scope'].startswith('space:') and name not in ('fish_memory_search', 'fish_memory_save', 'fish_memory_delete'):
            raise PermissionError('独立测试窗口仅开放本窗口记忆；共享资源和外部插件未向此窗口授权')
        if name.startswith('fish_interests_'):
            require('interests.write') if name.endswith('update') else require('status')
            from research_autonomy import ResearchControl
            store = ResearchControl(self.root)
            return store.configure(arguments) if name.endswith('update') else store.config()
        if role == 'research' and name.startswith('fish_research_'):
            if not identity['owner']:
                raise PermissionError('研究资料仅向主人和本地控制台开放')
            from research_autonomy import ResearchControl
            store = ResearchControl(self.root)
            return store.search(str(arguments['query'])) if name.endswith('search') else store.detail(str(arguments['bvid']))
        if name == 'fish_memory_search':
            return MemoryStore(self.root, role).search(str(arguments.get('query', '')), scope=identity['scope'], limit=12)
        if name == 'fish_memory_save':
            require('memory.write')
            if identity['scope'].startswith('space:'):
                raise PermissionError('独立测试空间长期记忆只读，不能写入正式记忆库')
            return MemoryStore(self.root, role).save(str(arguments['text']), category=str(arguments.get('category', '约定')),
                scope=identity['scope'], source='manual', source_text='主人通过会话 ' + session + ' 明确要求保存')
        if name == 'fish_memory_delete':
            require('memory.delete')
            if identity['scope'].startswith('space:'):
                raise PermissionError('独立测试空间长期记忆只读，不能删除正式记忆库')
            rows = MemoryStore(self.root, role).search(scope=identity['scope'], limit=200)
            if not any(r['id'] == arguments['id'] for r in rows):
                raise PermissionError('记忆不存在或当前会话不可见')
            return MemoryStore(self.root, role).delete(arguments['id'])
        if role == 'qq' and name == 'fish_emoji_search':
            from emoji_store import EmojiStore
            return EmojiStore(self.root).candidates(str(arguments['context']))
        if role == 'qq' and name == 'fish_emoji_delete':
            require('emoji.manage')
            from emoji_store import EmojiStore
            return EmojiStore(self.root).delete(str(arguments['id']))
        if role == 'qq' and name == 'fish_emoji_choose':
            from emoji_store import EmojiStore
            if not any(e['id'] == arguments['id'] for e in EmojiStore(self.root).list()):
                raise ValueError('表情已删除或不存在')
            with database(self.path) as db:
                db.execute('INSERT OR REPLACE INTO emoji_choices VALUES(?,?,?)', (session, arguments['id'], time.time()))
            return {'ok': True, 'message': '已选择，是否发送仍由频率和安全规则决定'}
        if not name.startswith('fishp__'):
            raise PermissionError('未知工具')
        cancel = threading.Event()
        key = role + ':' + call_id
        with self.lock:
            if key in self.cancelled:
                raise ValueError('调用 ID 重复，未重复执行')
            self.cancelled[key] = (cancel, session)
        try:
            return self.registry.execute(name, arguments, role, owner=bool(identity['owner']), cancel=cancel)
        finally:
            with self.lock:
                self.cancelled.pop(key, None)

    def dispatch(self, route, body, role, admin):
        if route == '/health':
            return {'ok': True, 'root': hashlib.sha256(str(self.root.resolve()).encode()).hexdigest(), 'social_context_version': 1, 'social_roster_version': 1}
        if route in ('/social/context/set', '/social/context'):
            identity = self.identity(role, str(body.get('session', '')))
            if role != 'qq' or not identity['scope'].startswith('chat:group:'):
                raise PermissionError('临时群资料仅用于已绑定群会话')
            if route.endswith('/set'):
                if not admin: raise PermissionError('只有消息控制器可以更新临时资料')
                text = body.get('text', '')
                if not isinstance(text,str) or len(text) > 250000: raise ValueError('临时资料过大')
                with database(self.path) as db:
                    db.execute('INSERT OR REPLACE INTO group_contexts VALUES(?,?,?,?)', (identity['session'],identity['scope'],text,time.time()))
                return {'ok': True}
            if admin: raise PermissionError('运行时需使用角色令牌读取临时资料')
            with database(self.path) as db:
                row=db.execute('SELECT * FROM group_contexts WHERE session=? AND scope=?',(identity['session'],identity['scope'])).fetchone()
            if not row or time.time()-row['updated'] > 180: raise PermissionError('本轮群资料不存在或已过期')
            return {'text': row['text']}
        if route == '/social/persona':
            identity = self.identity(role, str(body.get('session', '')))
            if role != 'qq' or not identity['scope'].startswith('space:'):
                raise PermissionError('人格快照仅用于已绑定的隔离测试空间')
            from social_context import SocialContext
            sid = identity['scope'][6:]
            contexts = SocialContext(self.root)
            space = contexts.space(sid)
            return {'revision': space['revision'], 'text': contexts.prompt(sid)}
        if route == '/catalog':
            return {'tools': builtins(role) + self.registry.catalog(role), 'native_tools': self.registry.native_catalog(role)}
        if route == '/bind':
            if not admin:
                raise PermissionError('只有本地控制台能绑定身份')
            return self.bind(body['role'], body['session'], body['scope'], body.get('sender', ''), bool(body.get('desktop', False)))
        if route == '/execute':
            if admin:
                raise PermissionError('工具执行必须使用角色令牌')
            return self.execute(role, str(body['session']), str(body['name']), body.get('arguments', {}), str(body['call_id']))
        if route == '/cancel':
            with self.lock:
                for key, (event, session) in self.cancelled.items():
                    if key.startswith(role + ':') and (body.get('session') == session or body.get('call_id') == key.split(':', 1)[1]):
                        event.set()
            return {'ok': True}
        if route == '/usage/reserve':
            if Governance(self.root, role).config()['paused']:
                raise PermissionError('角色已暂停')
            return {'id': ModelLedger(self.root, role).reserve('dsh')}
        if route == '/requests/capture':
            if admin:
                raise PermissionError('请求快照必须由已绑定的运行时提交')
            from request_inspector import RequestInspector
            identity = self.identity(role, str(body.get('session', '')))
            return RequestInspector(self.root).capture(role, identity, body.get('payload', {}))
        if route == '/usage/finish':
            ModelLedger(self.root, role).finish(str(body['id']), body.get('usage'), body.get('status', 'done'))
            return {'ok': True}
        if route == '/emoji/take':
            if not admin:
                raise PermissionError('只有消息控制器可以取得待发送表情')
            session = str(body['session'])
            with database(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM emoji_choices WHERE session=?', (session,)).fetchone()
                db.execute('DELETE FROM emoji_choices WHERE session=?', (session,))
            return {'asset': row['asset'] if row and row['created'] > time.time() - 600 else ''}
        if route.startswith('/research/'):
            if not admin: raise PermissionError('研究控制需由本地控制台发起')
            if route == '/research/discover': return self.research.discover()
            if route == '/research/cancel': return self.research.cancel(str(body['id']))
            if route == '/research/pause':
                self.research.control.configure({'paused': True})
                self.research.cancel_event.set()
                return {'ok': True}
            if route == '/research/resume': return self.research.control.resume()
        if route == '/plugins/cancel':
            if not admin: raise PermissionError('插件管理需本地控制台权限')
            return self.registry.cancel(str(body['id']), notify=False)
        raise ValueError('接口不存在')

    def worker(self):
        while not self.stop.wait(2):
            if not (self.root / 'harness/agents.json').is_file():
                return
            for role in ('qq', 'research'):
                try:
                    if Governance(self.root, role).config()['paused']:
                        continue
                    gateway = ModelGateway(self.root, role)
                    if not gateway.key:
                        continue
                    MemoryStore(self.root, role).learn_one(gateway)
                    if role == 'qq':
                        from emoji_store import EmojiStore
                        EmojiStore(self.root).learn_one(gateway)
                except Exception:
                    # Each job retains its own failure; never silently retry paid calls.
                    continue

    def serve(self):
        host = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                role, admin = '', False
                try:
                    supplied = self.headers.get('x-fish-token', '')
                    if hmac.compare_digest(supplied, control_token(host.root)):
                        admin = True
                        role = self.headers.get('x-fish-role', 'research')
                    else:
                        role = next((r for r in ('qq', 'research') if hmac.compare_digest(supplied, role_token(host.root, r))), '')
                    if role not in ('qq', 'research'):
                        raise PermissionError('未授权')
                    if self.headers.get('Origin'):
                        raise PermissionError('浏览器跨源请求已拒绝')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= length <= 2 * 1024 * 1024:
                        raise ValueError('请求过大')
                    body = json.loads(self.rfile.read(length) or b'{}')
                    result = host.dispatch(self.path, body, role, admin)
                    code, data = 200, {'ok': True, 'result': result}
                except Exception as exc:
                    code = 403 if isinstance(exc, PermissionError) else 400
                    # Exception details can contain user/plugin data; keep private console only.
                    data = {'ok': False, 'error': str(exc)[:500] if isinstance(exc, (ValueError, PermissionError, InterruptedError, TimeoutError)) else type(exc).__name__ + '：操作失败，请查看对应功能的记录'}
                payload = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer(('127.0.0.1', port(self.root)), Handler)
        self.server.daemon_threads = True
        self.learning_thread = threading.Thread(target=self.worker, daemon=True, name='fish-learning')
        self.research_thread = threading.Thread(target=self.research.loop, daemon=True, name='fish-research')
        self.learning_thread.start(); self.research_thread.start()
        threading.Thread(target=self.server.serve_forever, daemon=True, name='fish-tool-host').start()


def request(root, route, body=None, *, role='research', admin=True, timeout=8):
    response = requests.post(f'http://127.0.0.1:{port(root)}' + route, json=body or {},
        headers={'x-fish-token': control_token(root) if admin else role_token(root, role), 'x-fish-role': role}, timeout=timeout, proxies={'http':None,'https':None,'all':None})
    data = response.json()
    if not response.ok or not data.get('ok'):
        raise PermissionError(data.get('error', '本地工具服务拒绝请求'))
    return data['result']


def ensure_host(root):
    root = Path(root).resolve()
    with _host_lock:
        if str(root) in _hosts:
            return
        try:
            result = request(root, '/health', timeout=1)
            if result.get('root') != hashlib.sha256(str(root).encode()).hexdigest():
                raise PermissionError('工具端口被其他 Fish 数据目录占用')
            return
        except requests.ConnectionError:
            host = ToolHost(root)
            try:
                host.serve()
            except OSError:
                # Another authorized process may have won the startup race.
                result = request(root, '/health', timeout=2)
                if result.get('root') != hashlib.sha256(str(root).encode()).hexdigest():
                    raise PermissionError('工具端口被其他数据目录占用')
                return
            _hosts[str(root)] = host


def bind_session(root, role, session, scope='owner', sender='', desktop=False):
    ensure_host(root)
    if str(scope).startswith('space:') and request(root, '/health').get('social_context_version') != 1:
        raise PermissionError('工具服务尚未支持独立窗口隔离；请停止并重新启动对应角色')
    return request(root, '/bind', {'role': role, 'session': session, 'scope': scope, 'sender': sender, 'desktop': desktop}, role=role)


def set_group_context(root, session, text):
    if request(root, '/health').get('social_roster_version') != 1:
        raise PermissionError('请重启新版 Fish 以更新群资料服务')
    return request(root, '/social/context/set', {'session':session,'text':text},role='qq')


def shutdown_host(root):
    with _host_lock:
        host = _hosts.pop(str(Path(root).resolve()), None)
    if host:
        host.stop.set(); host.research.stop.set(); host.research.cancel_event.set()
        for plugin in host.registry.entries(): host.registry.cancel(plugin['id'], notify=False)
        # The research driver owns its subprocess. Let it kill and reap that
        # process before the GUI exits; otherwise a blocked download can outlive it.
        host.research_thread.join(timeout=25)
        host.server.shutdown(); host.server.server_close()
