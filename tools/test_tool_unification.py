"""一套工具表 + 工具内部判作用域：三条证据，跑的是真实执行器。

1. 三种作用域公布的工具清单，数量和名字完全一致；
2. 群作用域写主人记忆被中文拒绝，私聊写成功（跑完删掉这条测试记忆）；
3. 群作用域代发群消息被中文拒绝，私聊走同一套前置校验（假通道，不真发）。
"""
import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from bot_control import BotControl
from fish_chat import CATALOGUES, tool_definitions
from governance import Governance
from group_policy import GroupPolicy
from memory_store import MemoryStore
from tool_host import ToolHost, bind_session

OWNER = '3259980040'
GROUP = '1043230169'
CHAT = 'group:' + GROUP
SENDER = '2100000001'
DDL = ('CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT, '
       'user_id TEXT, text TEXT, replied INTEGER, conversation_id TEXT, sender_id TEXT, '
       'direction TEXT, channel_message_id TEXT, reply_to TEXT, segments TEXT, status TEXT)')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class UnificationTests(unittest.TestCase):
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
        GroupPolicy(self.root).configure(CHAT, {'read': True, 'listen': True, 'reply': True})
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

    def owner_message(self, text, mid='9400010001'):
        with closing(sqlite3.connect(self.root / 'data/qq/qq.db')) as db:
            db.execute('INSERT INTO messages(ts,chat_id,user_id,text,replied,conversation_id,sender_id,'
                       'direction,channel_message_id,reply_to,segments,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('2026-02-12 10:00:00', 'private:' + OWNER, OWNER, text, 0, 'conv', OWNER,
                        'inbound', mid, None, '[]', 'observed'))
            db.commit()

    # 1
    def test_the_three_surfaces_publish_one_identical_list(self):
        print()
        lists = {}
        for context in ('owner-private', 'owner-desktop', 'group'):
            names = [item['function']['name'] for item in tool_definitions('qq', context)]
            lists[context] = names
            print('[' + context + '] %d 个：%s' % (len(names), ', '.join(names)))
        self.assertEqual(list(CATALOGUES['qq']), lists['owner-private'])
        self.assertEqual(lists['owner-private'], lists['owner-desktop'])
        self.assertEqual(lists['owner-private'], lists['group'])
        self.assertEqual(24, len(lists['group']))

    # 2
    def test_a_group_cannot_write_an_owner_memory_and_the_owner_can(self):
        with self.assertRaises(PermissionError) as caught:
            self.host.execute('qq', self.group, 'fish_memory_save', {'text': '群里想记的东西'},
                              uuid.uuid4().hex)
        print()
        print('[group fish_memory_save]', caught.exception)
        self.assertIn('群里不能读写主人的记忆', str(caught.exception))

        self.owner_message('记得周四去体检')
        saved = self.host.execute('qq', self.owner, 'fish_memory_save',
                                  {'text': '周四去体检', 'category': '日程', 'evidence': 'owner_quote',
                                   'source_message_id': '9400010001', 'source_quote': '记得周四去体检'},
                                  uuid.uuid4().hex)
        memory_id = saved.get('id') or saved.get('memory_id')
        print('[owner fish_memory_save] id=' + str(memory_id) + ' status=' + str(saved.get('status')))
        self.assertTrue(memory_id, saved)
        rows = MemoryStore(self.root, 'qq').search('体检', scope='owner', limit=20)
        self.assertEqual([memory_id], [row['id'] for row in rows])
        self.assertTrue(self.host.execute('qq', self.owner, 'fish_memory_delete', {'id': memory_id},
                                          uuid.uuid4().hex)['ok'])
        print('[owner fish_memory_delete] 测试记忆已删除，剩余行数=',
              len(MemoryStore(self.root, 'qq').search('体检', scope='owner', limit=20)))
        self.assertEqual([], MemoryStore(self.root, 'qq').search('体检', scope='owner', limit=20))

    # 3
    def test_a_group_cannot_send_for_the_owner_and_private_passes_the_checks(self):
        with self.assertRaises(PermissionError) as caught:
            self.host.execute('qq', self.group, 'fish_group_send',
                              {'chat_id': CHAT, 'text': '群里不该有人能自己发'}, uuid.uuid4().hex)
        print()
        print('[group fish_group_send]', caught.exception)
        self.assertIn('群里不能代发群消息', str(caught.exception))

        calls = []

        def transport(target, text, request_id, reply_to=''):
            calls.append({'target': target, 'text': text, 'request_id': request_id})
            return {'ok': True, 'type': 'action.result',
                    'payload': {'chat_id': target, 'message_id': '9900' + str(len(calls))}}

        with patch.object(BotControl, '_channel_transport', staticmethod(transport)):
            value = self.host.execute('qq', self.owner, 'fish_group_send',
                                      {'chat_id': CHAT, 'text': '今晚八点开组会'}, uuid.uuid4().hex)
        print('[owner fish_group_send] status=' + str(value.get('status')) +
              ' message_id=' + str(value.get('message_id')) + ' 假通道调用=' + str(len(calls)))
        self.assertEqual('sent', value['status'])
        self.assertEqual([CHAT], [call['target'] for call in calls])


if __name__ == '__main__':
    unittest.main()
