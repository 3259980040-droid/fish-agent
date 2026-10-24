"""只读的能力答复不该被身份核验挡回来。

fish_capabilities 只报她自己的工具表和当前开关，不读任何人的聊天；同一个道理适用于
主人自己的群授权和她的研究兴趣。所以这三个工具在没有绑定会话时也回答，而写记忆、
发消息、碰主人记忆的仍然必须配对——边界只放开了"她能做什么"这一句。
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
from governance import Governance
from group_policy import GroupPolicy
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


class OpenReadTests(unittest.TestCase):
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
        self.host = ToolHost(self.root)
        self.host.serve(background_workers=False, listen_port=0)
        self.addCleanup(self.stop_host)
        config = json.loads((self.root / 'harness/agents.json').read_text(encoding='utf-8'))
        config['runtime']['tool_host_port'] = int(self.host.server.server_address[1])
        write_json(self.root / 'harness/agents.json', config)

    def stop_host(self):
        self.host.stop.set()
        self.host.server.shutdown()
        self.host.server.server_close()

    def call(self, name, arguments=None, session=''):
        return self.host.execute('qq', session, name, arguments or {}, uuid.uuid4().hex)

    # 1
    def test_capabilities_answer_without_any_bound_session(self):
        value = self.call('fish_capabilities', session='')  # 没有任何 bind_session
        print('[unbound fish_capabilities brief]', value['brief'])
        print('[unbound fish_capabilities abilities]', json.dumps(value['abilities'], ensure_ascii=False))
        self.assertTrue(value['brief'])
        self.assertIn('能力是同一套', value['brief'])
        self.assertIn('24', value['brief'])
        self.assertEqual(24, len(value['abilities']))
        self.assertIn('fish_memory_search', value['abilities'])
        self.assertIn('fish_doc_organize', value['abilities'])
        self.assertFalse(value.get('captured'))  # 没有快照就是没有，不编

    # 2
    def test_a_group_surface_still_answers_capabilities(self):
        session = 'group-fish:' + CHAT
        bind_session(self.root, 'qq', session, 'chat:' + CHAT, SENDER)
        value = self.call('fish_capabilities', session=session)
        print('[group fish_capabilities brief]', value['brief'])
        self.assertIn('这个群里读写的只有本群', value['brief'])
        self.assertIn('24', value['brief'])

    # 3
    def test_the_other_read_only_answers_do_not_need_a_pairing_either(self):
        GroupPolicy(self.root).configure(CHAT, {'read': True})
        groups = self.call('fish_group_list')
        print('[unbound fish_group_list]', json.dumps(groups, ensure_ascii=False))
        self.assertEqual([CHAT], [row['chat'] for row in groups['groups']])
        interests = self.call('fish_interests_read')
        self.assertIn('directions', interests)

    # 4
    def test_writing_and_sending_still_require_the_pairing(self):
        for name, arguments in (('fish_memory_save', {'text': '没人核验也想写'}),
                                ('fish_doc_organize', {'action': 'list'}),
                                ('fish_group_send', {'chat_id': CHAT, 'text': '不该出去'}),
                                ('fish_self_control', {'action': 'pause'})):
            with self.subTest(tool=name):
                with self.assertRaises(PermissionError) as caught:
                    self.call(name, arguments)
                self.assertIn('会话没有经过程序身份核验', str(caught.exception))

    # 5
    def test_a_group_still_cannot_write_an_owner_memory(self):
        session = 'group-fish:' + CHAT
        bind_session(self.root, 'qq', session, 'chat:' + CHAT, SENDER)
        with self.assertRaises(PermissionError) as caught:
            self.call('fish_memory_save', {'text': '群里想记的东西'}, session=session)
        self.assertIn('群里不能读写主人的记忆', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
