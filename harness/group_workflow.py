"""Shared group policy, passive observation and decision records."""
from locations import project_path
import json
import sqlite3
import time
from pathlib import Path
from group_policy import GroupPolicy
from social_context import SocialContext
from storage import database, read_json


class GroupWorkflow:
    def __init__(self, root, *, trial=None):
        self.root = Path(root); self.contexts = SocialContext(root); self.trial = trial
        if trial:
            from group_tests import GroupTests
            self.policy = GroupTests(root).policy(trial)
        else:
            self.policy = GroupPolicy(root)
        self.latest = {}; self.path = project_path(self.root, 'data/social/decisions.db')
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY,chat TEXT,cursor INTEGER,kind TEXT,stage TEXT,reason TEXT,created REAL)')

    def log(self, chat, cursor, kind, stage, reason):
        if self.trial:
            reason = '测试 ' + self.trial['id'][:8] + ' · ' + str(reason)
        with database(self.path) as db:
            db.execute('INSERT INTO decisions(chat,cursor,kind,stage,reason,created) VALUES(?,?,?,?,?,?)',(chat,cursor,kind,stage,str(reason)[:200],time.time()))
            db.execute('DELETE FROM decisions WHERE id NOT IN (SELECT id FROM decisions ORDER BY id DESC LIMIT 1000)')

    def decisions(self, chat):
        with database(self.path) as db:
            return [dict(r) for r in db.execute('SELECT cursor,kind,stage,reason,created FROM decisions WHERE chat=? ORDER BY id DESC LIMIT 30',(chat,))]

    def allowed(self, loop, chat, revision=''):
        group = chat.split(':',1)[1]
        allow = read_json(project_path(self.root, 'channels/fish_qq/config.json')).get('allowlist',{}).get('group_ids',[])
        if self.trial:
            from group_tests import GroupTests
            current = GroupTests(self.root).current(group)
            return bool(group in {str(v) for v in allow} and not loop.governance.config()['paused']
                and current and current['id'] == self.trial['id'] and (not revision or current['revision'] == revision))
        from group_tests import GroupTests
        if GroupTests(self.root).current(group):
            return False
        dep = self.contexts.deployment(group)
        return bool(group in {str(v) for v in allow} and not loop.governance.config()['paused'] and dep['enabled'] and (not revision or dep['revision']==revision))

    def within_budget(self, chat):
        # Shared physical-group limits survive A/B switches and new windows.
        now = time.time(); cfg = self.policy.config(chat)
        if not cfg['reply_limits']:
            return True
        with database(self.path) as db:
            rows = db.execute("SELECT kind,created FROM decisions WHERE chat=? AND stage='sent' AND created>?", (chat, now-3600)).fetchall()
        if sum(r['created'] > now-60 for r in rows) >= cfg['max_total_min']:
            return False
        return True

    async def observe(self, loop, payload):
        chat = str(payload.get('chat_id','')); group = chat.split(':',1)[-1]
        allow = read_json(project_path(self.root, 'channels/fish_qq/config.json')).get('allowlist',{}).get('group_ids',[])
        sender = str((payload.get('sender') or {}).get('id',''))
        if group not in {str(v) for v in allow} or not sender or sender==str((loop.channel.bot or {}).get('id','')) or loop.governance.config()['paused']: return
        observed = self.policy.observe(payload)
        if not observed.get('accepted'): return
        text=''.join(s.get('text','') for s in payload.get('segments',[]) if s.get('type')=='text')
        if self.trial:
            conv = loop.store.get_or_create_conversation('group-test:' + self.trial['id'], 'qq')
            try:
                loop.store.record_message(conv['id'], chat, sender, 'inbound', str(payload['message_id']), payload.get('reply_to'),
                    payload.get('segments', []), text, 'observed', runtime_session_id=self.contexts.space(self.trial['id'])['session'])
            except sqlite3.IntegrityError:
                return  # Replayed transport message, including across window switches.
        if text.strip() and not self.trial and self.policy.config(chat)['learn']:
            from memory_store import MemoryStore
            MemoryStore(self.root,'qq').enqueue('qq-group:'+chat+':'+str(payload['message_id']),text,'chat:'+chat,subject='qq:'+sender)
        self.latest[chat] = payload
        # Generation is owned exclusively by GroupDialogue.
