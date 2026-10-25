"""Explicit owner commands for temporary, group-local persona trial windows.

Trial authorization is separate from final persona approval/deployment. A trial
never selects the owner's private route or grants group members owner powers.
"""
from locations import project_path
import asyncio
import hmac
import re
import time
import uuid
from pathlib import Path

from governance import Governance
from group_policy import GroupPolicy
from social_context import SocialContext, identifier
from storage import database, read_json

VERBS = ('/人格版本', '/测试窗口', '/新建测试', '/切换测试', '/结束测试')


def test_scope(group, space):
    if not re.fullmatch(r'[0-9]{5,15}', str(group)):
        raise ValueError('无效群号')
    return 'chat:group:' + str(group) + ':test:' + identifier(space)


def parse_scope(scope):
    match = re.fullmatch(r'chat:group:([0-9]{5,15}):test:([a-f0-9]{32})(?::task:[a-f0-9]{32})?', str(scope))
    return match.groups() if match else None


class GroupTests:
    def __init__(self, root):
        self.root = Path(root)
        self.contexts = SocialContext(root)
        self.path = self.contexts.path
        with database(self.path) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS group_test_spaces(
                space TEXT PRIMARY KEY REFERENCES spaces(id), group_id TEXT NOT NULL,
                owner TEXT NOT NULL, runtime_digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS group_test_routes(
                group_id TEXT PRIMARY KEY, space TEXT NOT NULL REFERENCES spaces(id),
                owner TEXT NOT NULL, updated REAL NOT NULL);
            ''')

    def require_controller(self, group, sender):
        owner = self.contexts.owner()
        if not owner or not hmac.compare_digest(str(sender).encode(), owner.encode()):
            raise PermissionError('只有配置的主人 QQ 可以控制群测试')
        allow = read_json(project_path(self.root, 'channels/fish_qq/config.json')).get('allowlist', {}).get('group_ids', [])
        if str(group) not in {str(v) for v in allow}:
            raise PermissionError('此群不在白名单中')
        if not Governance(self.root).permitted(sender, 'persona.test'):
            raise PermissionError('主人账号的人格测试权限已关闭')

    def window(self, group, sid):
        identifier(sid)
        with database(self.path) as db:
            row = db.execute('SELECT * FROM group_test_spaces WHERE space=? AND group_id=?', (sid, str(group))).fetchone()
        if not row:
            raise ValueError('此测试窗口不属于当前群')
        self.require_controller(group, row['owner'])
        space = self.contexts.space(sid)
        if row['runtime_digest'] != self.contexts.runtime_digest('qq'):
            raise PermissionError('模型配置已变化，请新建群测试窗口')
        if space['role'] != 'qq' or space['origin'] != 'group':
            raise PermissionError('群测试窗口类型不匹配')
        return {**space, 'group_id': str(group), 'owner': row['owner'], 'test_scope': test_scope(group, sid)}

    def current(self, group):
        with database(self.path) as db:
            row = db.execute('SELECT * FROM group_test_routes WHERE group_id=?', (str(group),)).fetchone()
        if not row:
            return None
        try:
            space = self.window(group, row['space'])
            return space if row['owner'] == space['owner'] else None
        except (ValueError, PermissionError):
            return None

    def policy(self, space):
        return GroupPolicy(self.root, path=self.contexts.directory(space['id']) / 'group-policy.db')

    def create(self, group, revision, sender):
        self.require_controller(group, sender)
        persona = self.contexts.persona(revision)
        if persona['role'] != 'qq':
            raise ValueError('群测试只能使用 QQ 人格')
        space = self.contexts.create_space('qq', persona['name'] + ' · 群测试', revision=revision, origin='group')
        cfg = GroupPolicy(self.root).config('group:' + str(group))
        self.policy(space).configure('group:' + str(group), {**cfg, 'listen': True, 'reply': True, 'learn': False, 'reply_limits': False})
        with database(self.path) as db:
            db.execute('INSERT INTO group_test_spaces VALUES(?,?,?,?)', (space['id'], str(group), str(sender), self.contexts.runtime_digest('qq')))
        return self.select(group, space['id'], sender)

    def select(self, group, sid, sender):
        self.require_controller(group, sender)
        space = self.window(group, sid)
        # Retire unfinished candidates; selecting a window never replays an old
        # unanswered trigger. Its native history and summary remain intact.
        policy = self.policy(space)
        with database(policy.path) as db:
            db.execute('BEGIN IMMEDIATE')
            policy._state(db, 'group:' + str(group))
            policy._retire(db, 'group:' + str(group), 'switched')
            db.execute('UPDATE groups SET handled=cursor WHERE chat=?', ('group:' + str(group),))
        policy.configure('group:' + str(group), {'listen': True, 'reply': True, 'learn': False})
        with database(self.path) as db:
            db.execute('INSERT OR REPLACE INTO group_test_routes VALUES(?,?,?,?)', (str(group), sid, str(sender), time.time()))
            db.execute('INSERT INTO audit(kind,subject,actor,created) VALUES(?,?,?,?)', ('group_test_selected', sid, str(sender), time.time()))
        return space

    def leave(self, group, sender):
        self.require_controller(group, sender)
        with database(self.path) as db:
            db.execute('DELETE FROM group_test_routes WHERE group_id=?', (str(group),))
            db.execute('INSERT INTO audit(kind,subject,actor,created) VALUES(?,?,?,?)', ('group_test_ended', str(group), str(sender), time.time()))

    def windows(self, group):
        with database(self.path) as db:
            rows = db.execute('SELECT space FROM group_test_spaces WHERE group_id=? AND owner=? ORDER BY rowid DESC LIMIT 30', (str(group), self.contexts.owner())).fetchall()
        result = []
        for row in rows:
            try:
                result.append(self.window(group, row['space']))
            except (ValueError, PermissionError):
                pass
        return result


async def command(loop, payload, sender):
    if payload.get('chat_type') != 'group' or not re.fullmatch(r'group:[0-9]{5,15}', str(payload.get('chat_id', ''))):
        return False
    text = ''.join(s.get('text', '') for s in payload.get('segments', []) if s.get('type') == 'text').strip()
    pieces = text.split(maxsplit=1)
    verb, arg = (pieces + [''])[:2] if pieces else ('', '')
    if verb not in VERBS:
        return False
    # Use transport-normalized mention metadata and actual sender QQ, never a
    # display name, quote, model instruction or textual '@大肥鱼'.
    if not payload.get('mentions_bot') or str(sender) != SocialContext(loop.root).owner():
        return True
    tests = GroupTests(loop.root)
    group = payload['chat_id'].split(':')[1]
    try:
        tests.require_controller(group, sender)
        if verb == '/人格版本':
            response = '\n'.join(p['id'][:8] + ' · ' + p['name'] for p in tests.contexts.personas('qq')[:30])
        elif verb == '/新建测试':
            space = tests.create(group, tests.contexts.resolve(arg.strip()), sender)
            mode = '正式回复＋最多160字思考片段' if space['persona'].get('owner_reply_mode') == 'thinking_excerpt' else '仅正式回复'
            response = f"已开启本群测试窗口 {space['id']}\n人格 {space['revision'][:8]} · {mode}。群友现在可 @我聊天；/结束测试 退出。"
        elif verb == '/切换测试':
            space = tests.select(group, tests.contexts.resolve(arg.strip(), kind='spaces'), sender)
            response = f"已切回本群测试窗口 {space['id']}\n人格 {space['revision'][:8]}，沿用此窗口的聊天记录。"
        elif verb == '/结束测试':
            tests.leave(group, sender)
            response = '本群测试已结束，记录保留。' + ('已回到此前正式群聊。' if tests.contexts.deployment(group)['enabled'] else '本群恢复不发言。')
        else:
            current = tests.current(group)
            response = '当前群测试：' + (current['id'] if current else '未开启') + '\n'
            response += '\n'.join(s['id'] + ' · 人格 ' + s['revision'][:8] for s in tests.windows(group))
    except (ValueError, PermissionError) as exc:
        response = str(exc)
    await loop.channel.action('send_message', {'chat_id': payload['chat_id'], 'segments': [{'type': 'text', 'text': response}],
        'idempotency_key': 'group-test-command:' + str(payload.get('message_id', uuid.uuid4().hex))})
    return True
