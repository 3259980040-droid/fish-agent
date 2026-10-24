"""Group-scope tools: one read-only slice, pinned to the chat that bound it.

Nothing here is a second agent: the group runs the same fish_chat core and the same
tool_host executors as the owner's private chat, behind a narrower gate. A group
member holds no owner permission, so the catalogue, the executor and the history
target are all checked here as real calls, not as catalogues of intentions.
"""
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fish_chat import CATALOGUES, respond, tool_definitions
from governance import Governance
from group_decide import fish_candidate
from group_policy import GroupPolicy
from memory_store import MemoryStore
from model_gateway import ChatOptions, ChatResult
from social_context import SocialContext
from tool_host import ToolHost, bind_session, builtins

OWNER = '111111'
GROUP = '1043230169'
CHAT = 'group:' + GROUP
OTHER = '770011022'
OTHER_CHAT = 'group:' + OTHER
SENDER = '2100000001'
OTHER_SECRET = 'OTHER-GROUP-CANARY-77'
GROUP_TEXT = '周三五点开组会'
DDL = ('CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT, '
       'user_id TEXT, text TEXT, replied INTEGER DEFAULT 0, conversation_id TEXT, sender_id TEXT, '
       'direction TEXT, channel_message_id TEXT, reply_to TEXT, segments TEXT, status TEXT)')


def ago(minutes):
    return (datetime.now() - timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def final(text):
    return ChatResult(text, {}, 'stop')


def call(name, arguments, call_id='call-1'):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


class FakeGateway:
    """A model that answers from the replies it was handed; it never dials out."""

    base = 'https://fake.invalid'
    model = 'fake-model'

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests, self.kwargs = [], []

    def chat(self, messages, **kwargs):
        self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        self.kwargs.append({key: value for key, value in kwargs.items() if key != 'cancelled'})
        if not self.replies:
            raise AssertionError('假模型被调用的次数超过本用例提供的回复')
        return self.replies.pop(0)

    def payloads(self, turn=1):
        return [json.loads(message['content']) for message in self.requests[turn]
                if message.get('role') == 'tool']


class GroupToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write_stores()
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        self.host = ToolHost(self.root)
        self.host.serve(background_workers=False, listen_port=0)
        self.addCleanup(self.stop_host)
        config = json.loads((self.root / 'harness/agents.json').read_text(encoding='utf-8'))
        config['runtime']['tool_host_port'] = int(self.host.server.server_address[1])
        write_json(self.root / 'harness/agents.json', config)
        self.session = 'group-fish:' + CHAT
        bind_session(self.root, 'qq', self.session, 'chat:' + CHAT, SENDER)

    def stop_host(self):
        self.host.stop.set()
        self.host.server.shutdown()
        self.host.server.server_close()

    def write_stores(self):
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
        # Only this chat is authorised for reading: OTHER stays listed but unread.
        GroupPolicy(self.root).configure(CHAT, {'read': True})

    def message(self, text, *, chat=CHAT, mid='', minutes=2, sender=SENDER):
        with closing(sqlite3.connect(self.root / 'data/qq/qq.db')) as db:
            db.execute('INSERT INTO messages(ts,chat_id,user_id,text,replied,conversation_id,sender_id,'
                       'direction,channel_message_id,reply_to,segments,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ago(minutes), chat, sender, text, 0, 'conv', sender, 'inbound', mid, None,
                        json.dumps([{'type': 'text', 'text': text}], ensure_ascii=False), 'observed'))
            db.commit()

    def execute(self, name, arguments, *, session=None, call_id=None):
        return self.host.execute('qq', session or self.session, name, arguments,
                                 call_id or uuid.uuid4().hex)

    def payload(self, text=GROUP_TEXT, mid='g-1', sender=SENDER):
        return {'chat_type': 'group', 'chat_id': CHAT, 'message_id': mid,
                'sender': {'id': sender, 'display_name': '群友甲'}, 'reply_to': '',
                'mentions_bot': False, 'segments': [{'type': 'text', 'text': text}]}

    # 1
    def test_a_group_publishes_the_same_table_as_every_other_surface(self):
        # One table for the role: a group window sees the same abilities the owner's
        # own chat does, in a plain-text round too. Which of them this group may
        # actually use is decided inside the executor, not by hiding the tool.
        names = [definition['function']['name'] for definition in tool_definitions('qq', 'group')]
        # 24 = 原来的 21 项加 fish_say / fish_send_sticker / fish_image_look：
        # 说话必须调用发送工具，看不懂的图必须能自己看一眼。
        self.assertEqual(24, len(names))
        self.assertEqual(set(CATALOGUES['qq']), set(names))
        self.assertEqual(len(CATALOGUES['qq']), len(names))
        definitions = tool_definitions('qq', 'group', emoji_state={'previewed': set(), 'tools': []})
        self.assertEqual(names, [definition['function']['name'] for definition in definitions])
        known = {definition['name']: definition for definition in builtins('qq')}
        for definition in definitions:  # same schema object the executor is bound to
            self.assertEqual({'type', 'function'}, set(definition))
            self.assertEqual(known[definition['function']['name']], definition['function'])

    # 2
    def test_group_scope_refuses_in_the_tool_with_a_chinese_reason(self):
        cases = {
            'fish_group_list': ({}, '群里不能列出主人的授权群'),
            'fish_owner_admin': ({'action': 'status'}, '群里不能用主人的管理工具'),
            'fish_memory_save': ({'text': '群里想记的东西'}, '群里不能读写主人的记忆'),
            'fish_memory_complete': ({'id': 'abcdef0123456789'}, '群里不能读写主人的记忆'),
            'fish_group_send': ({'chat_id': CHAT, 'text': '不该出去'}, '群里不能代发群消息'),
        }
        for index, (name, (arguments, expected)) in enumerate(cases.items()):
            with self.subTest(tool=name):
                with self.assertRaises(PermissionError) as caught:
                    self.execute(name, arguments, call_id='refuse-' + str(index))
                self.assertIn(expected, str(caught.exception))
        # Every refusal is a row: the decision can be read back after the fact.
        with closing(sqlite3.connect(self.root / 'data/qq/governance.db')) as db:
            kinds = [row[0] for row in db.execute("SELECT kind FROM audit")]
        self.assertEqual(len(cases), kinds.count('tool.scope.refused'))

    # 3
    def test_group_history_ignores_a_group_id_the_model_supplies(self):
        self.message(GROUP_TEXT, mid='mine-1')
        self.message(OTHER_SECRET, chat=OTHER_CHAT, mid='other-1')
        GroupPolicy(self.root).configure(OTHER_CHAT, {'read': True})  # authorised, still not my chat
        result = self.execute('fish_group_history', {'group_id': OTHER, 'minutes': 60})
        self.assertTrue(result['authorized'])
        self.assertEqual(GROUP, result['group_id'])
        self.assertEqual(CHAT, result['chat'])
        self.assertEqual(['mine-1'], [message['message_id'] for message in result['messages']])
        self.assertNotIn(OTHER_SECRET, json.dumps(result, ensure_ascii=False))

    # 4
    def test_read_authorisation_still_decides_the_group_scope(self):
        self.message(OTHER_SECRET, chat=OTHER_CHAT, mid='other-2')
        self.assertEqual([], self.execute('fish_group_history', {'minutes': 60})['messages'])
        other_session = 'group-fish:' + OTHER_CHAT
        bind_session(self.root, 'qq', other_session, 'chat:' + OTHER_CHAT, SENDER)
        with self.assertRaises(PermissionError) as caught:
            self.execute('fish_group_history', {'minutes': 60}, session=other_session)
        self.assertIn('群未获主人读取授权：' + OTHER_CHAT, str(caught.exception))
        self.message('本群自己的记录', mid='mine-2')
        self.assertEqual(['mine-2'], [message['message_id'] for message in
                                      self.execute('fish_group_history', {'minutes': 60})['messages']])

    # 5
    def test_the_owner_surfaces_publish_that_same_one_table(self):
        self.assertEqual(1, len(CATALOGUES))
        self.assertEqual(24, len(CATALOGUES['qq']))
        for tool_context in ('owner-private', 'owner-desktop'):
            offered = [definition['function']['name']
                       for definition in tool_definitions('qq', tool_context)]
            self.assertEqual(set(CATALOGUES['qq']), set(offered))
        self.assertIn('fish_group_send', CATALOGUES['qq'])
        self.assertIn('fish_doc_save', CATALOGUES['qq'])

    # 6
    def test_fish_candidate_sends_the_group_context_and_only_this_groups_memory(self):
        revision = SocialContext(self.root).save_persona('qq', '群里的 Fish', '同一个人格')['id']
        store = MemoryStore(self.root, 'qq')
        store.save('本群暗号：' + GROUP_TEXT, category='约定', scope='chat:' + CHAT, source='manual')
        store.save('OWNER-PRIVATE-CANARY 主人私聊里说的周末安排', category='日程', scope='owner',
                   source='manual')
        answer = patch('fish_chat.respond', return_value='好').start()
        self.addCleanup(patch.stopall)
        candidate = fish_candidate(self.root, CHAT, payload=self.payload())
        self.assertNotIn('error', candidate)
        options = answer.call_args.kwargs
        self.assertEqual('group', options['tool_context'])
        self.assertEqual('chat:' + CHAT, options['scope'])
        self.assertEqual('group-fish:' + CHAT, options['session'])
        self.assertEqual(revision, options['persona']['id'])
        self.assertIn('本群暗号', options['memory'])
        self.assertNotIn('OWNER-PRIVATE-CANARY', options['memory'])
        self.assertNotIn('主人私聊里说的周末安排', options['memory'])

    # 7
    def test_the_table_is_only_what_she_has_and_every_name_has_an_executor(self):
        known = {definition['name']: definition for definition in builtins('qq')}
        for name in CATALOGUES['qq']:
            self.assertIn(name, known)
            self.assertTrue(known[name]['description'].strip(), name)
        # The reply contract and the owner's console stay out of the table: they are
        # not something she picks up in the middle of a conversation.
        for excluded in ('fish_emoji_choose', 'fish_emoji_delete', 'fish_chat_reply',
                         'fish_owner_admin', 'fish_owner_send'):
            self.assertNotIn(excluded, CATALOGUES['qq'])

    # 8
    def test_a_group_turn_reaches_history_and_is_refused_the_rest(self):
        self.message(GROUP_TEXT, mid='turn-1')
        self.message(OTHER_SECRET, chat=OTHER_CHAT, mid='turn-2')
        gateway = FakeGateway([ChatResult('', {}, 'tool_calls', (
                                  call('fish_group_history', {'group_id': OTHER, 'minutes': 60}, 'c1'),
                                  call('fish_group_send', {'chat_id': OTHER_CHAT, 'text': '不该出去'}, 'c2'))),
                               final('好，看到了。')])
        with patch('model_gateway.ModelGateway', lambda *args, **kwargs: gateway), \
                patch('model_gateway.chat_options', lambda root: ChatOptions('high', 512, 30)):
            text = respond(self.root, 'qq', persona={'id': 'rev-1', 'body': '你是一条鱼。'},
                           history=[], history_source='test', memory='', scope='chat:' + CHAT,
                           references='', current=[{'type': 'text', 'text': GROUP_TEXT}],
                           session=self.session, cancelled=None, origin='test:group',
                           output='text', tool_context='group')
        self.assertEqual('好，看到了。', text)
        offered = [definition['function']['name'] for definition in gateway.kwargs[0]['tools']]
        self.assertEqual(24, len(offered))
        history_payload, refused = gateway.payloads()[0], gateway.payloads()[1]
        self.assertEqual(GROUP, history_payload['group_id'])
        self.assertEqual(['turn-1'], [message['message_id'] for message in history_payload['messages']])
        self.assertNotIn(OTHER_SECRET, json.dumps(history_payload, ensure_ascii=False))
        # The tool is published on this surface and the executor still refuses it:
        # the answer is a reason the model can read, not a missing name.
        self.assertIn('群里不能代发群消息', refused['error'])
        with self.assertRaises(PermissionError) as caught:
            self.execute('fish_group_send', {'chat_id': OTHER_CHAT, 'text': '不该出去'})
        self.assertIn('群里不能代发群消息', str(caught.exception))


    # 9
    def test_a_session_that_only_claims_a_group_scope_stays_locked_out(self):
        self.message(GROUP_TEXT, mid='claim-1')
        forged = 'chat:group:' + GROUP + ':' + uuid.uuid4().hex
        bind_session(self.root, 'qq', forged, 'chat:' + CHAT, '')
        with self.assertRaises(PermissionError) as caught:
            self.host.execute('qq', forged, 'fish_group_history', {'minutes': 60}, 'forged-1')
        self.assertIn('群历史与网页工具仅用于主人私聊或本地控制台', str(caught.exception))
        self.assertNotIn(GROUP_TEXT, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
