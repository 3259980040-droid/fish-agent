"""fish_group_send: the owner's private-chat route into one authorised group.

The tool is one executor and two catalogues, so the same checks are made here as
real calls: the published catalogue, the owner-only scope gate, the whitelist and
the chat's own switches, the reservation and frequency refusals, and one real
receipt that a repeated call must reuse instead of sending twice.
"""
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from bot_control import BotControl
from fish_chat import CATALOGUES, tool_definitions
from governance import Governance
from group_policy import GroupPolicy
from tool_host import ToolHost, bind_session, builtins

OWNER = '3259980040'
GROUP = '1043230169'
CHAT = 'group:' + GROUP
OTHER = '770011022'
OTHER_CHAT = 'group:' + OTHER
SENDER = '2100000001'
QUOTED = '7400010001'
DDL = ('CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT, '
       'user_id TEXT, text TEXT, replied INTEGER DEFAULT 0, conversation_id TEXT, sender_id TEXT, '
       'direction TEXT, channel_message_id TEXT, reply_to TEXT, segments TEXT, status TEXT)')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class GroupSendToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_json(self.root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'model': 'fake-model', 'base_url': 'https://fake.invalid',
                        'apiKeyEnv': 'FISH_TEST_QQ_KEY'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.root / 'channels/fish_qq/config.json', {
            'allowlist': {'owner_id': OWNER, 'group_ids': [GROUP, OTHER]},
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
        self.owner = 'qq-private:' + uuid.uuid4().hex
        bind_session(self.root, 'qq', self.owner, 'owner', OWNER, False, chat_mode='owner')

    def stop_host(self):
        self.host.stop.set()
        self.host.server.shutdown()
        self.host.server.server_close()

    def ready(self, chat=CHAT, **switches):
        GroupPolicy(self.root).configure(chat, {'read': True, 'listen': True, 'reply': True, **switches})

    def execute(self, name, arguments, *, session=None, call_id=None):
        return self.host.execute('qq', session or self.owner, name, arguments, call_id or uuid.uuid4().hex)

    def send(self, arguments, **kwargs):
        return self.execute('fish_group_send', arguments, **kwargs)

    def fake_channel(self, calls, receipt=None):
        def transport(target, text, request_id, reply_to=''):
            calls.append({'target': target, 'text': text, 'request_id': request_id, 'reply_to': reply_to})
            return receipt or {'ok': True, 'type': 'action.result',
                               'payload': {'chat_id': target, 'message_id': '8800' + str(len(calls))}}
        return patch.object(BotControl, '_channel_transport', staticmethod(transport))

    # 1
    def test_published_in_the_one_table_and_refused_inside_a_group(self):
        published = {definition['name'] for definition in builtins('qq')}
        self.assertEqual(1, len(CATALOGUES))
        self.assertEqual(24, len(CATALOGUES['qq']))
        self.assertIn('fish_group_send', CATALOGUES['qq'])
        self.assertLessEqual(set(CATALOGUES['qq']), published)  # 公布的名字都在执行器里
        for context in ('owner-private', 'owner-desktop', 'group'):
            self.assertEqual(24, len(tool_definitions('qq', context)), context)

    # 2
    def test_only_the_owner_private_scope_reaches_the_group(self):
        self.ready()
        group_session = 'group-fish:' + CHAT
        bind_session(self.root, 'qq', group_session, 'chat:' + CHAT, SENDER)
        with self.assertRaises(PermissionError) as caught:
            self.send({'chat_id': CHAT, 'text': '群里不该有人能自己发'}, session=group_session)
        self.assertIn('群里不能代发群消息', str(caught.exception))
        for index, scope in enumerate(('chat:' + CHAT, 'space:abcdef12', 'control:group:' + GROUP,
                                       'work:desktop-1', 'chat:group:' + GROUP + ':w1:t1')):
            with self.subTest(scope=scope):
                identity = {'role': 'qq', 'owner': 1, 'desktop': 0, 'scope': scope, 'session': 'x'}
                with self.assertRaises(PermissionError) as refused:
                    self.host.group_send(identity, {'chat_id': CHAT, 'text': '不该出去'})
                self.assertIn('只对主人的独立私聊开放', str(refused.exception))
        with self.assertRaises(PermissionError):  # 主人身份没有核验过
            self.host.group_send({'role': 'qq', 'owner': 0, 'desktop': 0, 'scope': 'owner'},
                                 {'chat_id': CHAT, 'text': '不该出去'})

    # 3
    def test_unlisted_chat_and_withdrawn_switches_are_refused(self):
        with self.assertRaises(PermissionError) as caught:
            self.send({'chat_id': OTHER_CHAT, 'text': '发不出去的'})
        self.assertIn('不在主人的发送白名单里：' + OTHER_CHAT, str(caught.exception))
        with self.assertRaises(ValueError) as bad:
            self.send({'chat_id': OTHER, 'text': '群号没有前缀'})
        self.assertIn('chat_id 必须是 group:群号', str(bad.exception))
        GroupPolicy(self.root).configure(OTHER_CHAT, {'read': True})  # 只给了读取
        with self.assertRaises(PermissionError) as unauthorized:
            self.send({'chat_id': OTHER_CHAT, 'text': '发不出去的'})
        self.assertIn('不在主人的发送白名单里', str(unauthorized.exception))
        # 名单说可以发，但发送前重新读到的群设置已经关掉发言：这一步必须自己拦。
        self.ready(chat=CHAT, listen=True, reply=False)
        with patch('group_read.authorized_groups',
                   return_value=[{'chat': CHAT, 'group_id': GROUP, 'reply': True}]):
            with self.assertRaises(PermissionError) as closed:
                self.send({'chat_id': CHAT, 'text': '发不出去的'})
        self.assertIn('已关闭旁听或发言', str(closed.exception))
        Governance(self.root).save({'paused': True})
        with self.assertRaises(PermissionError):
            self.send({'chat_id': CHAT, 'text': '发不出去的'})

    # 4
    def test_one_real_receipt_and_a_repeat_is_idempotent(self):
        self.ready()
        calls = []
        with self.fake_channel(calls):
            first = self.send({'chat_id': CHAT, 'text': '今晚八点开组会'})
            again = self.send({'chat_id': CHAT, 'text': '今晚八点开组会'})
            quoted = self.send({'chat_id': CHAT, 'text': '八点半也行', 'reply_to': QUOTED})
        self.assertEqual('sent', first['status'])
        self.assertTrue(first['ok'])
        self.assertEqual('88001', first['message_id'])
        self.assertEqual([CHAT], [call['target'] for call in calls[:1]])
        self.assertEqual('sent', again['status'])
        self.assertEqual('88001', again['message_id'])
        self.assertIs(True, again.get('repeat'))
        self.assertIn('owner-group-send:', first['request_id'])
        # 三次调用只真实发了两条：同一分钟同一段正文复用既有回执
        self.assertEqual(2, len(calls))
        self.assertEqual(QUOTED, calls[1]['reply_to'])
        self.assertEqual('', calls[0]['reply_to'])
        self.assertEqual('88002', quoted['message_id'])

    # 5
    def test_a_generation_in_flight_and_a_spent_budget_both_stop_it(self):
        self.ready()
        calls = []
        with closing(sqlite3.connect(self.root / 'data/social/group-policy.db')) as db:
            db.execute("INSERT INTO reservations(token,chat,cursor,kind,state,created)"
                       " VALUES('live',?,0,'direct','active',?)", (CHAT, time.time()))
            db.commit()
        with self.fake_channel(calls):
            with self.assertRaises(PermissionError) as busy:
                self.send({'chat_id': CHAT, 'text': '先别抢'})
        self.assertIn('正在生成回复', str(busy.exception))
        self.assertEqual([], calls)
        with closing(sqlite3.connect(self.root / 'data/social/group-policy.db')) as db:
            db.execute("DELETE FROM reservations WHERE token='live'")
            db.commit()
        BotControl(self.root)  # 台账就是定时发送同一本 sends 表
        with closing(sqlite3.connect(self.root / 'data/social/owner-control.db')) as db:
            for index in range(5):
                db.execute("INSERT INTO sends VALUES(?,?,?,?,?,?)",
                           ('earlier-' + str(index), CHAT, '早先的一条', 'SUCCEEDED', '{}', time.time() - 60))
            db.commit()
        with self.fake_channel(calls):
            with self.assertRaises(PermissionError) as spent:
                self.send({'chat_id': CHAT, 'text': '第六条'})
        self.assertIn('达到上限 5 条', str(spent.exception))
        self.assertEqual([], calls)


if __name__ == '__main__':
    unittest.main()
