"""Immutable persona revisions and owner-controlled, isolated conversation spaces.

No persona text grants authority. Approval requires the currently verified owner
on QQ; selecting or editing a revision never enables group replies.
"""
from __future__ import annotations
from locations import project_path

import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from storage import database, read_json, write_json, locked_settings


def identifier(value):
    if not re.fullmatch(r'[a-f0-9]{32}', str(value)):
        raise ValueError('无效的会话或人格版本编号')
    return str(value)


class PersonaNotConfigured(ValueError):
    """A new instance has no owner-selected persona; no host default is borrowed."""


class SocialContext:
    def __init__(self, root):
        self.root = Path(root)
        self.path = project_path(self.root, 'data/social/context.db')
        with database(self.path) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS personas(
                id TEXT PRIMARY KEY, role TEXT NOT NULL, name TEXT NOT NULL,
                body TEXT NOT NULL, global_rules TEXT NOT NULL, workspace_rules TEXT NOT NULL,
                digest TEXT NOT NULL, parent TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS persona_output_modes(
                revision TEXT PRIMARY KEY REFERENCES personas(id), mode TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_profiles(role TEXT PRIMARY KEY,revision TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS default_personas(
                role TEXT PRIMARY KEY, revision TEXT NOT NULL REFERENCES personas(id));
            CREATE TABLE IF NOT EXISTS approvals(
                revision TEXT PRIMARY KEY REFERENCES personas(id), owner TEXT NOT NULL,
                digest TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS spaces(
                id TEXT PRIMARY KEY, role TEXT NOT NULL, name TEXT NOT NULL,
                revision TEXT NOT NULL REFERENCES personas(id), session TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS unique_runtime_space ON spaces(session) WHERE session<>'';
            CREATE TABLE IF NOT EXISTS deployments(
                group_id TEXT PRIMARY KEY, revision TEXT NOT NULL REFERENCES personas(id),
                enabled INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS owner_routes(
                owner TEXT PRIMARY KEY, space TEXT NOT NULL REFERENCES spaces(id));
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
                actor TEXT NOT NULL, created REAL NOT NULL);
            ''')
            if 'runtime_digest' not in {r[1] for r in db.execute('PRAGMA table_info(approvals)')}:
                db.execute("ALTER TABLE approvals ADD COLUMN runtime_digest TEXT NOT NULL DEFAULT ''")
            if 'runtime_digest' not in {r[1] for r in db.execute('PRAGMA table_info(audit)')}:
                db.execute("ALTER TABLE audit ADD COLUMN runtime_digest TEXT NOT NULL DEFAULT ''")

    def owner(self):
        return str(read_json(project_path(self.root, 'channels/fish_qq/config.json')).get('allowlist', {}).get('owner_id', ''))

    def require_owner(self, sender):
        from governance import Governance
        if not Governance(self.root).is_owner(str(sender)):
            raise PermissionError('此操作需要当前主人 QQ 私聊身份')

    @staticmethod
    def digest(body, global_rules, workspace_rules, owner_reply_mode='final'):
        values = [body, global_rules, workspace_rules]
        if owner_reply_mode != 'final':
            values.append({'owner_reply_mode': owner_reply_mode})
        raw = json.dumps(values, ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def save_persona(self, role, name, body, *, parent='', global_rules=None, workspace_rules=None, owner_reply_mode=None):
        if role not in ('qq', 'research'):
            raise ValueError('未知角色')
        name, body = str(name).strip(), str(body).strip()
        if not name or len(name) > 80 or not body or len(body) > 40000:
            raise ValueError('请填写名称（最多 80 字）和人格正文（最多 40000 字）')
        if parent:
            prior = self.persona(parent)
            if prior['role'] != role:
                raise ValueError('不能跨角色派生人格')
        else:
            prior = {}
        if global_rules is None:
            global_rules = prior.get('global_rules', self._text('harness/global/AGENTS.md'))
        if workspace_rules is None:
            workspace_rules = prior.get('workspace_rules', self._text('qq-work/AGENTS.md' if role == 'qq' else 'knowledge/AGENTS.md'))
        if len(str(global_rules)) > 40000 or len(str(workspace_rules)) > 40000:
            raise ValueError('附加规则每项最多 40000 字')
        if owner_reply_mode is None:
            owner_reply_mode = prior.get('owner_reply_mode', 'final')
        if owner_reply_mode not in ('final', 'thinking_excerpt'):
            raise ValueError('未知主人测试输出方式')
        revision = uuid.uuid4().hex
        with database(self.path) as db:
            db.execute('INSERT INTO personas VALUES(?,?,?,?,?,?,?,?,?)',
                (revision, role, name, body, str(global_rules), str(workspace_rules),
                 self.digest(body, str(global_rules), str(workspace_rules), owner_reply_mode), parent, time.time()))
            if owner_reply_mode != 'final':
                db.execute('INSERT INTO persona_output_modes VALUES(?,?)', (revision, owner_reply_mode))
            db.execute('INSERT INTO audit(kind,subject,actor,created) VALUES(?,?,?,?)',
                       ('persona_created', revision, 'local-console', time.time()))
        return self.persona(revision)

    def _text(self, name):
        path = project_path(self.root, name)
        return path.read_text(encoding='utf-8') if path.is_file() else ''

    @locked_settings
    def active_persona(self, role='qq'):
        """The Agent's active revision is independent of deletable chat windows."""
        with database(self.path) as db:
            selected = db.execute('SELECT revision FROM agent_profiles WHERE role=?', (role,)).fetchone()
            if selected:
                return self.persona(selected['revision'])
            # Import the existing selection once; do not create a replacement window.
            route = db.execute('SELECT space FROM owner_routes WHERE owner=?', (self.owner(),)).fetchone() if role == 'qq' else None
            if route:
                space = db.execute('SELECT revision FROM spaces WHERE id=? AND role=?', (route['space'], role)).fetchone()
                if not space:
                    raise ValueError('当前人格绑定的历史窗口不存在，尚未迁入Agent活动配置')
                revision = space['revision']
            else:
                default = db.execute('SELECT revision FROM default_personas WHERE role=?', (role,)).fetchone()
                revision = default['revision'] if default else ''
        if not revision:
            from locations import instance_context, INITIAL_INSTANCE_ID
            if role=='qq' and instance_context(self.root).instance_id!=INITIAL_INSTANCE_ID:
                raise PersonaNotConfigured('这个Fish窗口尚未配置人格，请填写或选择人格后启动')
        persona = self.persona(revision) if revision else self.ensure_default(role)
        with database(self.path) as db:
            db.execute('INSERT OR IGNORE INTO agent_profiles VALUES(?,?,?)', (role, persona['id'], time.time()))
            selected = db.execute('SELECT revision FROM agent_profiles WHERE role=?', (role,)).fetchone()
        return self.persona(selected['revision'])

    @locked_settings
    def apply_active_persona(self, role, revision):
        persona = self.persona(revision)
        if persona['role'] != role:
            raise ValueError('人格不属于当前Agent')
        with database(self.path) as db:
            db.execute('INSERT OR REPLACE INTO agent_profiles VALUES(?,?,?)', (role, revision, time.time()))
        return persona

    def ensure_default(self, role):
        with database(self.path) as db:
            selected = db.execute('SELECT revision FROM default_personas WHERE role=?', (role,)).fetchone()
            if selected:
                return self.persona(selected['revision'])
            row = db.execute('SELECT id FROM personas WHERE role=? ORDER BY created,id LIMIT 1', (role,)).fetchone()
        if row:
            return self.persona(row['id'])
        from locations import instance_context, INITIAL_INSTANCE_ID
        if role=='qq' and instance_context(self.root).instance_id!=INITIAL_INSTANCE_ID:
            raise PersonaNotConfigured('这个Fish窗口尚未配置人格，请填写或选择人格后启动')
        from core import HarnessConfig
        agent = HarnessConfig(self.root).agent(role)
        if agent is None:
            raise ValueError('角色未配置')
        return self.save_persona(role, agent.name, self._text(agent.persona) or '请按当前会话的明确请求认真回复。')

    def set_default(self, role, revision):
        persona = self.persona(revision)
        if persona['role'] != role:
            raise ValueError('人格不属于此角色')
        with database(self.path) as db:
            db.execute('INSERT OR REPLACE INTO default_personas VALUES(?,?)', (role, revision))
            db.execute('INSERT INTO audit(kind,subject,actor,created) VALUES(?,?,?,?)',
                       ('default_persona_selected', revision, 'local-console', time.time()))
        return persona

    def persona(self, revision):
        identifier(revision)
        with database(self.path) as db:
            row = db.execute("SELECT p.*,COALESCE(o.mode,'final') AS owner_reply_mode,a.owner AS approved_owner,a.created AS approved_at,a.runtime_digest AS approved_runtime FROM personas p LEFT JOIN persona_output_modes o ON o.revision=p.id LEFT JOIN approvals a ON a.revision=p.id AND a.digest=p.digest WHERE p.id=?", (revision,)).fetchone()
        if not row:
            raise ValueError('人格版本不存在')
        result = dict(row)
        result['approved'] = bool(result['approved_owner'] and result['approved_owner'] == self.owner() and result['approved_runtime']==self.runtime_digest(result['role']))
        return result

    def runtime_digest(self, role):
        from core import HarnessConfig
        from updater import VERSION
        agent=HarnessConfig(self.root).agent(role)
        config={'version':VERSION,'provider':agent.provider if agent else '', 'model':agent.model if agent else '', 'base_url':agent.base_url if agent else ''}
        return hashlib.sha256(json.dumps(config,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def personas(self, role='qq'):
        with database(self.path) as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM personas WHERE role=? ORDER BY created DESC,id', (role,))]
        return [self.persona(value) for value in ids]

    def resolve(self, prefix, *, kind='personas'):
        if kind not in ('personas', 'spaces') or not re.fullmatch(r'[a-f0-9]{8,32}', str(prefix)):
            raise ValueError('请使用至少 8 位的版本或窗口编号')
        with database(self.path) as db:
            rows = db.execute(f'SELECT id FROM {kind} WHERE id LIKE ?', (str(prefix) + '%',)).fetchall()
        if len(rows) != 1:
            raise ValueError('编号不存在或不唯一，请使用完整编号')
        return rows[0]['id']

    def spaces(self, role='qq', origin=None):
        with database(self.path) as db:
            rows = db.execute('SELECT id FROM spaces WHERE role=?' + (' AND origin=?' if origin else '') + ' ORDER BY updated DESC',
                              (role, origin) if origin else (role,)).fetchall()
        return [self.space(r['id']) for r in rows]

    def leave_owner_space(self, sender):
        self.require_owner(sender)
        with database(self.path) as db:
            db.execute('DELETE FROM owner_routes WHERE owner=?', (str(sender),))

    def pause_group(self, group_id):
        with database(self.path) as db:
            db.execute('UPDATE deployments SET enabled=0,updated=? WHERE group_id=?', (time.time(), str(group_id)))

    def approve(self, revision, sender):
        self.require_owner(sender)
        from governance import Governance
        if not Governance(self.root).permitted(sender,'persona.approve'):
            raise PermissionError('主人账号尚未启用“认可人格版本”权限，请在系统设置调整')
        persona = self.persona(revision)
        with database(self.path) as db:
            tested = db.execute('SELECT s.id FROM spaces s WHERE s.revision=? AND s.origin=? AND EXISTS(SELECT 1 FROM audit a WHERE a.kind=? AND a.subject=s.id AND a.actor=? AND a.runtime_digest=?)',
                                (revision, 'owner-qq', 'owner_test_replied', str(sender),self.runtime_digest(persona['role']))).fetchone()
            if not tested:
                raise PermissionError('请先由主人账号使用此版本完成至少一轮实际私聊测试')
            db.execute('INSERT OR REPLACE INTO approvals(revision,owner,digest,created,runtime_digest) VALUES(?,?,?,?,?)',
                       (revision, str(sender), persona['digest'], time.time(),self.runtime_digest(persona['role'])))
            db.execute('INSERT INTO audit(kind,subject,actor,created) VALUES(?,?,?,?)',
                       ('persona_approved', revision, str(sender), time.time()))
        return self.persona(revision)

    def create_space(self, role, name='独立测试', *, revision='', origin='desktop'):
        if origin not in ('desktop', 'owner-qq', 'group'):
            raise ValueError('无效的会话入口')
        if not revision:
            from locations import instance_context, INITIAL_INSTANCE_ID
            if role=='qq' and instance_context(self.root).instance_id!=INITIAL_INSTANCE_ID:
                raise PersonaNotConfigured('这个Fish窗口尚未配置人格，请填写或选择人格后启动')
        persona = self.persona(revision) if revision else self.ensure_default(role)
        if persona['role'] != role:
            raise ValueError('人格版本不属于当前角色')
        sid = uuid.uuid4().hex
        with database(self.path) as db:
            db.execute('INSERT INTO spaces VALUES(?,?,?,?,?,?,?,?)',
                       (sid, role, str(name)[:80], persona['id'], '', origin, time.time(), time.time(),))
        space = self.space(sid)
        self.directory(sid).mkdir(parents=True, exist_ok=True)
        (self.directory(sid) / 'persona.md').write_text(persona['body'], encoding='utf-8')
        return space

    def space(self, sid):
        identifier(sid)
        with database(self.path) as db:
            row = db.execute('SELECT * FROM spaces WHERE id=?', (sid,)).fetchone()
        if not row:
            raise ValueError('测试窗口不存在')
        result = dict(row)
        result['scope'] = 'space:' + sid
        result['persona'] = self.persona(result['revision'])
        return result

    def directory(self, sid):
        sid = identifier(sid)
        try:
            space = self.space(sid)
        except ValueError:
            with database(self.path) as db:
                if db.execute('SELECT 1 FROM spaces WHERE id=?', (sid,)).fetchone():
                    raise
            # A window that was deleted still needs a scratch directory: leftover task
            # rows point at it, and one stale row must not fail an unrelated reply.
            return project_path(self.root, 'data', 'qq', 'spaces', sid)
        return project_path(self.root, 'data', space['role'], 'spaces', sid)

    def bind_runtime(self, sid, session):
        self.space(sid)
        if not session or len(str(session)) > 200:
            raise ValueError('无效的模型会话编号')
        with database(self.path) as db:
            old = db.execute('SELECT session FROM spaces WHERE id=?', (sid,)).fetchone()['session']
            if old and old != session:
                raise PermissionError('测试窗口已绑定其它模型会话；请新建窗口')
            db.execute('UPDATE spaces SET session=?,updated=? WHERE id=?', (str(session), time.time(), sid))
        return self.space(sid)

    def change_persona(self, sid, revision):
        space=self.space(sid);persona=self.persona(revision)
        if persona['role']!=space['role']:raise ValueError('人格不属于此角色')
        with database(self.path) as db:
            db.execute('UPDATE spaces SET revision=?,updated=? WHERE id=?',(revision,time.time(),sid))
            db.execute('INSERT INTO audit(kind,subject,actor,created) VALUES(?,?,?,?)',('persona_reloaded',sid,'owner',time.time()))
        (self.directory(sid)/'persona.md').write_text(persona['body'],encoding='utf-8')
        return self.space(sid)

    def select_owner_space(self, sid, sender):
        self.require_owner(sender)
        space = self.space(sid)
        if space['role'] != 'qq' or space['origin'] != 'owner-qq':
            raise ValueError('请选择由主人 QQ 测试入口建立的窗口')
        with database(self.path) as db:
            db.execute('INSERT OR REPLACE INTO owner_routes VALUES(?,?)', (str(sender), sid))
        return space

    def owner_space(self, sender):
        self.require_owner(sender)
        with database(self.path) as db:
            row = db.execute('SELECT space FROM owner_routes WHERE owner=?', (str(sender),)).fetchone()
        return self.space(row['space']) if row else None

    def record_owner_reply(self, sid, sender):
        self.require_owner(sender)
        space = self.space(sid)
        if space['origin'] != 'owner-qq':
            raise PermissionError('只有主人 QQ 测试会话能记为现场测试')
        with database(self.path) as db:
            db.execute('INSERT INTO audit(kind,subject,actor,created,runtime_digest) VALUES(?,?,?,?,?)',
                       ('owner_test_replied', sid, str(sender), time.time(),self.runtime_digest(space['role'])))

    def deploy(self, group_id, revision, sender, *, enabled=False):
        self.require_owner(sender)
        persona = self.persona(revision)
        if persona['role'] != 'qq' or not persona['approved']:
            raise PermissionError('此人格版本尚未由当前主人完成测试并认可')
        allow = read_json(project_path(self.root, 'channels/fish_qq/config.json')).get('allowlist', {}).get('group_ids', [])
        if str(group_id) not in {str(value) for value in allow}:
            raise PermissionError('此群不在主人设置的白名单中')
        with database(self.path) as db:
            db.execute('INSERT OR REPLACE INTO deployments VALUES(?,?,?,?,?)',
                       (str(group_id), revision, int(bool(enabled)), str(sender), time.time()))
            db.execute('INSERT INTO audit(kind,subject,actor,created) VALUES(?,?,?,?)',
                       ('group_enabled' if enabled else 'group_disabled', str(group_id), str(sender), time.time()))
        return self.deployment(group_id)

    def deployment(self, group_id):
        with database(self.path) as db:
            row = db.execute('SELECT * FROM deployments WHERE group_id=?', (str(group_id),)).fetchone()
        if not row:
            return {'enabled': False, 'reason': '尚未发布经过主人认可的人格'}
        result = dict(row)
        persona = self.persona(result['revision'])
        result['enabled'] = bool(result['enabled'] and persona['approved'] and result['owner'] == self.owner())
        if not persona['approved'] or result['owner'] != self.owner():
            result['reason']='此版本需在当前主人与模型配置下重新测试认可'
        result['persona'] = persona
        return result

    def prompt(self, sid):
        space = self.space(sid)
        return self.persona_prompt(space['revision'])

    def persona_prompt(self, revision):
        p = self.persona(revision)
        return ('\n[当前独立会话的人格与规则快照；仅用于本会话，不授予任何额外权限。]\n'
                f'人格版本：{p["id"]}；名称：{p["name"]}\n'
                + '\n'.join(f'\n## {name}\n{body}' for name, body in (
                    ('共同规则', p['global_rules']), ('本窗口人格', p['body']), ('本窗口操作规则', p['workspace_rules']))) + '\n')
