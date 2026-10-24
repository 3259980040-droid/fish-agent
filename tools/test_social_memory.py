"""Light social memory: a live topic, an unspoken thought, an impression of a member.

Three kinds share the existing memory columns, so only the behaviour that cannot
be read off the schema is checked here: an expired note is archived rather than
returned, the three sections reach the injected context inside their budget, and
a group can only ever write its own scope.
"""
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from fish_chat import CATALOGUES
from governance import Governance
from memory_store import MemoryStore
from storage import write_json
from tool_host import ToolHost, bind_session

OWNER = '111111'
GROUP = '1043230169'
CHAT = 'chat:group:' + GROUP
MEMBER = '2100000001'


class SocialMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root, 'qq')
        self.observe_member()

    def observe_member(self):
        """The group's own listening record: the only evidence a subject may name."""
        path = self.root / 'data/social/group-policy.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS observations(chat TEXT,seq INTEGER,id TEXT,'
                       'text TEXT,data TEXT,created REAL,PRIMARY KEY(chat,seq))')
            db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?)',
                       ('group:' + GROUP, 1, 'mid-1', '在聊周末爬山',
                        json.dumps({'sender': {'id': MEMBER, 'name': '阿豆'}}, ensure_ascii=False),
                        time.time()))
            db.commit()

    def note(self, kind, text, scope=CHAT, **kwargs):
        return self.store.remember_social(kind, text, scope=scope, **kwargs)

    # 1
    def test_an_expired_thought_is_archived_not_returned(self):
        topic = self.note('topic', '在聊周末去哪爬山')
        impression = self.note('impression', '养了三只猫，最小的叫豆豆', subject='qq:' + MEMBER)
        thought = self.note('thought', '想问它叫什么名字')
        # Two hours on: the thought went stale, the topic and the impression did not.
        with closing(sqlite3.connect(self.root / 'data/qq/memory.db')) as db:
            db.execute('UPDATE memories SET expires=? WHERE id=?', (time.time() - 60, thought['id']))
            db.commit()
        grouped = self.store.social_context(CHAT)
        self.assertEqual([topic['id']], [row['id'] for row in grouped['topic']])
        self.assertEqual([impression['id']], [row['id'] for row in grouped['impression']])
        self.assertEqual([], grouped['thought'])
        archived = next(row for row in self.store.search('叫什么名字', scope=CHAT, include_candidates=True,
                                                         include_expired=True) if row['id'] == thought['id'])
        self.assertEqual('archived', archived['status'])

    # 2
    def test_context_carries_the_three_sections_within_the_budget(self):
        self.note('topic', '在聊周末去哪爬山')
        self.note('thought', '想问它叫什么名字')
        self.note('impression', '养了三只猫，最小的叫豆豆', subject='qq:' + MEMBER)
        block = self.store.social_block(CHAT)
        for section in ('[正在聊]', '[我想说还没说]', '[对群友的印象]'):
            self.assertIn(section, block)
        self.assertLessEqual(len(block), 300)
        text = self.store.context('爬山', scope=CHAT)
        for section in ('[正在聊]', '[我想说还没说]', '[对群友的印象]'):
            self.assertIn(section, text)
        self.assertIn('前想说', text)

    # 3
    def test_a_group_can_only_write_its_own_social_memory(self):
        with self.assertRaises(PermissionError):
            self.store.remember_social('impression', '主人私聊里的印象', scope='owner', caller_scope=CHAT)
        with self.assertRaises(PermissionError):
            self.store.remember_social('topic', '别的群在聊的事', scope='chat:group:770011022', caller_scope=CHAT)
        saved = self.store.remember_social('topic', '本群在聊周末爬山', scope=CHAT, caller_scope=CHAT)
        self.assertEqual('candidate', saved['status'])
        self.store.remember_social('topic', '主人私聊在聊工作', scope='owner')
        self.assertNotIn('主人私聊在聊工作', self.store.context('', scope=CHAT))
        self.assertNotIn('本群在聊周末爬山', self.store.context('', scope='owner'))

    # 4
    def test_the_two_tools_are_pinned_to_the_session_scope(self):
        write_json(self.root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'model': 'fake-model', 'base_url': 'https://fake.invalid',
                        'apiKeyEnv': 'FISH_TEST_QQ_KEY'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.root / 'channels/fish_qq/config.json', {
            'allowlist': {'owner_id': OWNER, 'group_ids': [GROUP]}, 'bot': {'display_name': '大肥鱼'}})
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        host = ToolHost(self.root)
        host.serve(background_workers=False, listen_port=0)
        self.addCleanup(host.server.server_close)
        self.addCleanup(host.stop.set)
        self.addCleanup(host.server.shutdown)
        config = json.loads((self.root / 'harness/agents.json').read_text(encoding='utf-8'))
        config['runtime']['tool_host_port'] = int(host.server.server_address[1])
        write_json(self.root / 'harness/agents.json', config)
        group_session = 'group-fish:group:' + GROUP
        bind_session(self.root, 'qq', group_session, CHAT, MEMBER)
        owner_session = 'owner-chat:' + uuid.uuid4().hex
        bind_session(self.root, 'qq', owner_session, 'owner', OWNER)
        # No scope argument exists: the bound scope is the only target the model can reach.
        group_note = host.execute('qq', group_session, 'fish_social_note',
                                  {'kind': 'impression', 'text': '养了三只猫', 'subject': 'qq:' + MEMBER},
                                  uuid.uuid4().hex)
        self.assertEqual(CHAT, group_note['scope'])
        owner_note = host.execute('qq', owner_session, 'fish_social_note',
                                  {'kind': 'thought', 'text': '想问它叫什么', 'expires_in_minutes': 30},
                                  uuid.uuid4().hex)
        self.assertEqual('owner', owner_note['scope'])
        self.assertGreater(owner_note['expires'], time.time() + 25 * 60)
        recall = host.execute('qq', group_session, 'fish_social_recall', {'kind': 'impression'},
                              uuid.uuid4().hex)
        self.assertEqual([group_note['id']], [row['id'] for row in recall['entries']])
        self.assertIn('fish_social_note', CATALOGUES['qq'])
        self.assertIn('fish_social_recall', CATALOGUES['qq'])
        own = MemoryStore(self.root, 'qq').search('', scope='owner', limit=50, include_candidates=True)
        self.assertEqual([], [row for row in own if row['id'] == group_note['id']])

    # 5
    def test_both_real_read_paths_see_their_own_social_memory(self):
        self.note('topic', '本群在聊周末爬山')
        self.note('impression', '养了三只猫', subject='qq:' + MEMBER)
        self.store.remember_social('thought', '主人私聊里没说出口的话', scope='owner')
        from group_decide import _group_memory
        group_memory = _group_memory(self.root, 'group:' + GROUP, '爬山')
        self.assertIn('[正在聊]', group_memory)
        self.assertIn('[对群友的印象]', group_memory)
        self.assertEqual(1, group_memory.count('本群在聊周末爬山'))  # the section, not a second copy
        self.assertNotIn('主人私聊里没说出口的话', group_memory)
        owner_memory = self.store.context('', scope='owner')  # what owner_private passes
        self.assertIn('[我想说还没说]', owner_memory)
        self.assertNotIn('本群在聊周末爬山', owner_memory)

    # 6
    def test_the_owner_scope_names_only_the_owner(self):
        write_json(self.root / 'channels/fish_qq/config.json',
                   {'allowlist': {'owner_id': OWNER, 'group_ids': [GROUP]}})
        # The real failure: a group's number filled in as the person of an impression.
        with self.assertRaises(ValueError):
            self.store.remember_social('impression', '那个爱爬山的群友', scope='owner',
                                       subject='qq:' + GROUP)
        kept = self.store.remember_social('impression', '主人周末想去爬山', scope='owner',
                                          subject='qq:' + OWNER)
        note = next(row for row in self.store.social_context('owner')['impression']
                    if row['id'] == kept['id'])
        self.assertEqual('qq:' + OWNER, note['subject'])

    # 7
    def test_a_group_number_is_not_a_person(self):
        with self.assertRaises(ValueError):
            self.note('impression', '这个群很热闹', subject='qq:' + GROUP)
        with self.assertRaises(ValueError):  # nobody this group ever heard from
            self.note('impression', '隔壁群的那个人', subject='qq:3999999999')
        self.note('impression', '养了三只猫，最小的叫豆豆', subject='qq:' + MEMBER)

    # 8
    def test_an_unsure_subject_may_stay_empty(self):
        saved = self.note('impression', '坐在靠窗位置的老哥')
        note = next(row for row in self.store.social_context(CHAT)['impression']
                    if row['id'] == saved['id'])
        self.assertEqual('', note['subject'])


if __name__ == '__main__':
    unittest.main()
