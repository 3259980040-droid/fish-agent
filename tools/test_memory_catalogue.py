"""One catalogue, the scope decided inside the tool: memory is the sharpest case.

tool_definitions() no longer filters by surface, so a group window publishes the same
fish_memory_* executors the owner's private chat does - the model sees one set of
abilities everywhere. What a group may actually do is answered by the executor
itself: it searches this group's own memory and can neither write nor close an
owner memory, and the refusal says so in Chinese and leaves an audit row.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from fish_chat import CATALOGUES, tool_definitions
from governance import Governance
from group_policy import GroupPolicy
from memory_store import MemoryStore
from tool_host import ToolHost, bind_session, builtins

MEMORY_TOOLS = ('fish_memory_search', 'fish_memory_save', 'fish_memory_complete')
CONTEXTS = ('owner-private', 'owner-desktop', 'group')
OWNER = '3259980040'
GROUP = '1043230169'
CHAT = 'group:' + GROUP
SENDER = '2100000001'
GROUP_SECRET = '本群暗号：周三开组会'
OWNER_SECRET = 'OWNER-PRIVATE-CANARY 主人的周末安排'
DDL = ('CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT, '
       'user_id TEXT, text TEXT, replied INTEGER, conversation_id TEXT, sender_id TEXT, '
       'direction TEXT, channel_message_id TEXT, reply_to TEXT, segments TEXT, status TEXT)')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_json(self.root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'model': 'fake-model', 'base_url': 'https://fake.invalid',
                        'apiKeyEnv': 'FISH_TEST_QQ_KEY'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.root / 'channels/fish_qq/config.json', {
            'allowlist': {'owner_id': OWNER, 'group_ids': [GROUP]},
            'bot': {'display_name': '大肥鱼'}})
        (self.root / 'data/qq').mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.root / 'data/qq/qq.db')) as db:
            db.execute(DDL)
            db.commit()
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        GroupPolicy(self.root).configure(CHAT, {'read': True})
        self.host = ToolHost(self.root)
        self.host.serve(background_workers=False, listen_port=0)
        self.addCleanup(self.stop_host)
        config = json.loads((self.root / 'harness/agents.json').read_text(encoding='utf-8'))
        config['runtime']['tool_host_port'] = int(self.host.server.server_address[1])
        write_json(self.root / 'harness/agents.json', config)
        self.owner = 'qq-private:' + uuid.uuid4().hex
        bind_session(self.root, 'qq', self.owner, 'owner', OWNER, False, chat_mode='owner')
        self.group = 'group-fish:' + CHAT
        bind_session(self.root, 'qq', self.group, 'chat:' + CHAT, SENDER)

    def stop_host(self):
        self.host.stop.set()
        self.host.server.shutdown()
        self.host.server.server_close()

    def offered(self, tool_context):
        return [item['function']['name'] for item in tool_definitions('qq', tool_context)]

    def execute(self, name, arguments, *, session=None):
        return self.host.execute('qq', session or self.group, name, arguments, uuid.uuid4().hex)

    # 1
    def test_every_surface_offers_one_identical_tool_table(self):
        table = list(CATALOGUES['qq'])
        self.assertEqual(1, len(CATALOGUES))
        for tool_context in CONTEXTS:
            offered = self.offered(tool_context)
            self.assertEqual(len(table), len(offered), tool_context)
            self.assertEqual(set(table), set(offered), tool_context)
            for name in MEMORY_TOOLS:
                self.assertIn(name, offered, tool_context)

    # 2
    def test_memory_schemas_are_the_executor_objects(self):
        known = {definition['name']: definition for definition in builtins('qq')}
        for tool_context in CONTEXTS:
            for item in tool_definitions('qq', tool_context):
                name = item['function']['name']
                if name not in MEMORY_TOOLS:
                    continue
                self.assertEqual(known[name], item['function'])
                self.assertEqual('object', item['function']['parameters']['type'])

    def add_owner_message(self, text, mid='9400010001'):
        with closing(sqlite3.connect(self.root / 'data/qq/qq.db')) as db:
            db.execute('INSERT INTO messages(ts,chat_id,user_id,text,replied,conversation_id,sender_id,'
                       'direction,channel_message_id,reply_to,segments,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('2026-02-12 10:00:00', 'private:' + OWNER, OWNER, text, 0, 'conv', OWNER,
                        'inbound', mid, None, '[]', 'observed'))
            db.commit()

    # 3
    def test_a_group_searches_only_its_own_memory(self):
        store = MemoryStore(self.root, 'qq')
        store.save(GROUP_SECRET, category='约定', scope='chat:' + CHAT, source='manual')
        store.save(OWNER_SECRET, category='日程', scope='owner', source='manual')
        found = self.execute('fish_memory_search', {'query': '暗号'})
        texts = json.dumps(found, ensure_ascii=False)
        self.assertIn(GROUP_SECRET, texts)
        self.assertNotIn(OWNER_SECRET, texts)
        self.assertNotIn('周末安排', texts)

    # 4
    def test_a_group_neither_writes_nor_closes_an_owner_memory(self):
        for name, arguments in (('fish_memory_save', {'text': '群里想记的东西'}),
                                ('fish_memory_complete', {'id': 'abcdef0123456789'})):
            with self.subTest(tool=name):
                with self.assertRaises(PermissionError) as caught:
                    self.execute(name, arguments)
                self.assertIn('群里不能读写主人的记忆', str(caught.exception))
        with closing(sqlite3.connect(self.root / 'data/qq/governance.db')) as db:
            kinds = [row[0] for row in db.execute(
                "SELECT kind FROM audit WHERE kind='tool.scope.refused'")]
        self.assertEqual(2, len(kinds))

    # 5
    def test_the_owner_private_chat_still_stores_and_removes_a_memory(self):
        self.add_owner_message('记得周四去体检')
        saved = self.execute('fish_memory_save',
                             {'text': '周四去体检', 'category': '日程', 'evidence': 'owner_quote',
                              'source_message_id': '9400010001', 'source_quote': '记得周四去体检'},
                             session=self.owner)
        memory_id = saved.get('id') or saved.get('memory_id')
        self.assertTrue(memory_id, saved)
        rows = MemoryStore(self.root, 'qq').search('体检', scope='owner', limit=20)
        self.assertEqual([memory_id], [row['id'] for row in rows])
        self.assertTrue(self.execute('fish_memory_delete', {'id': memory_id}, session=self.owner)['ok'])
        self.assertEqual([], MemoryStore(self.root, 'qq').search('体检', scope='owner', limit=20))
        self.assertEqual([], MemoryStore(self.root, 'qq').search('体检', scope='owner', limit=20,
                                                                 include_candidates=True))


if __name__ == '__main__':
    unittest.main()
