"""Main-chat tools: one catalogue, the real ToolHost gate, offline models."""
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from current_capabilities import current_capabilities
from fish_chat import CATALOGUES, respond, tool_definitions
from governance import Governance
from group_policy import GroupPolicy
from model_gateway import ChatOptions, ChatResult
from request_inspector import RequestInspector
from tool_host import ToolHost, bind_session, builtins
from web_knowledge import WebKnowledge

OWNER = '3259980040'
GROUP = '1043230169'
CHAT = 'group:' + GROUP
OTHER = '770011022'
OTHER_CHAT = 'group:' + OTHER
SENDER = '2100000001'
MID = '7400010001'
SECRET = 'LEAK-CANARY-T3-SECRET'
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


def wanting(*calls):
    return ChatResult('', {}, 'tool_calls', tuple(calls))


def history(*, gaps=('该群未开启旁听或学习，未监听时段的资料不完整',), complete=False):
    """One read_group body as the tool really returns it, with its coverage facts."""
    return {'group_id': GROUP, 'chat': CHAT, 'authorized': True,
            'window': {'minutes': 120, 'from': '2026-02-12 08:40:01', 'to': '2026-02-12 10:40:01'},
            'messages': [], 'references': [], 'more': False, 'next_before': None,
            'counts': {'returned': 0, 'inbound': 0, 'bot_outbound': 0},
            'gaps': list(gaps), 'names_note': '',
            'coverage': {'requested_minutes': 120, 'first_message_time': '2026-02-12 10:00:01',
                         'last_message_time': '2026-02-12 10:40:01', 'span_minutes': 40,
                         'source': 'none', 'complete': complete,
                         'reason': '该群未开启旁听或学习，未监听时段没有记录',
                         'gaps_count': len(gaps)},
            'coverage_note': ('覆盖范围：已读 0 条，时间 2026-02-12 10:00:01 至 2026-02-12 10:40:01，'
                              '实际跨度 40 分钟，来源 none；不完整：该群未开启旁听或学习，未监听时段没有记录；'
                              '缺口 ' + str(len(gaps)) + ' 条。')}


class FakeGateway:
    """Two-turn fake model: it never opens a network connection."""

    base = 'https://fake.invalid'
    model = 'fake-model'

    def __init__(self, replies, cancel=None, cancel_after=0):
        self.replies = list(replies)
        self.requests, self.kwargs = [], []
        self.cancel, self.cancel_after = cancel, cancel_after

    def chat(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        self.kwargs.append(copy.deepcopy({k: v for k, v in kwargs.items() if k != 'cancelled'}))
        if not self.replies:
            raise AssertionError('假模型被调用的次数超过本用例提供的回复')
        result = self.replies.pop(0)
        if self.cancel is not None and len(self.requests) == self.cancel_after:
            self.cancel.set()
        return result

    def tool_payloads(self, turn=1):
        return [json.loads(message['content']) for message in self.requests[turn]
                if message.get('role') == 'tool']


class ChatToolTests(unittest.TestCase):
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
        self.private_session = 'qq-private:' + uuid.uuid4().hex
        bind_session(self.root, 'qq', self.private_session, 'owner', OWNER, False, chat_mode='owner')

    def stop_host(self):
        self.host.stop.set()
        self.host.server.shutdown()
        self.host.server.server_close()

    def write_stores(self):
        (self.root / 'harness').mkdir(parents=True, exist_ok=True)
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
        GroupPolicy(self.root).configure(CHAT, {'read': True})

    def add_message(self, text, *, mid=MID, chat=CHAT, minutes=2, direction='inbound', sender=SENDER,
                    reply_to=None, segments=None):
        with closing(sqlite3.connect(self.root / 'data/qq/qq.db')) as db:
            db.execute('INSERT INTO messages(ts,chat_id,user_id,text,replied,conversation_id,sender_id,'
                       'direction,channel_message_id,reply_to,segments,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ago(minutes), chat, sender, text, 0, 'conv', sender, direction, mid, reply_to,
                        json.dumps(segments or [{'type': 'text', 'text': text}], ensure_ascii=False), 'observed'))
            db.commit()

    def turn(self, tool_context, replies, *, text='在吗', cancelled=None, emoji_state=None,
             cancel_after=0):
        cid = uuid.uuid4().hex
        if tool_context == 'owner-private':
            session = 'qq-private:' + cid
            bind_session(self.root, 'qq', session, 'owner', OWNER, False, chat_mode='owner')
            output = 'bubbles'
            state = emoji_state if emoji_state is not None else {'previewed': set(), 'tools': []}
        elif tool_context == 'owner-desktop':
            session = 'desktop:' + cid
            bind_session(self.root, 'qq', session, 'owner', desktop=True)
            output, state = 'text', None
        else:
            session = 'qq-plain:' + cid
            bind_session(self.root, 'qq', session, 'owner', OWNER, False, chat_mode='owner')
            output, state = 'text', None
        scripted = []
        for reply in replies:
            # 气泡面上她的话只能由发送工具交出去：脚本里那句"最终回复"改由 fish_say 说，
            # 再说一句 stop 结束这一轮（正文不再当消息）。
            scripted.append(wanting(call('fish_say', {'texts': [reply.text]}))
                            if output == 'bubbles' and not reply.tool_calls else reply)
        if output == 'bubbles':
            scripted.append(final(''))
        gateway = FakeGateway(scripted, cancelled, cancel_after)
        with patch('model_gateway.ModelGateway', lambda *args, **kwargs: gateway), \
             patch('model_gateway.chat_options', lambda root: ChatOptions('high', 512, 30)):
            answer = respond(self.root, 'qq', persona={'id': 'rev-1', 'body': '你是一条鱼。'},
                history=[], history_source='test', memory='', scope='owner', references='',
                current=[{'type': 'text', 'text': text}], session=session, cancelled=cancelled,
                origin='test:' + cid, output=output, emoji_state=state, tool_context=tool_context)
        if output == 'bubbles' and answer:
            # 她要说的每一句都在气泡里；.answer 记的是她真正说出口的字。
            answer = ''.join(bubble.get('text', '') for bubble in json.loads(answer)['bubbles']
                             if bubble.get('kind') == 'text')
        gateway.answer, gateway.session, gateway.state = answer, session, state
        return gateway

    # 1
    def test_every_context_publishes_the_same_one_table(self):
        private = self.turn('owner-private', [final('好')])
        names = [definition['function']['name'] for definition in private.kwargs[0]['tools']]
        self.assertEqual(24, len(names))
        self.assertEqual(set(CATALOGUES['qq']), set(names))
        self.assertEqual(1, len(CATALOGUES))  # 一套表，不分作用域
        desktop = self.turn('owner-desktop', [final('好')])
        offered = [definition['function']['name'] for definition in desktop.kwargs[0]['tools']]
        self.assertEqual(names, offered)
        for name in ('fish_emoji_search', 'fish_emoji_read', 'fish_emoji_save', 'fish_doc_save',
                     'fish_doc_organize', 'fish_group_send', 'fish_memory_search',
                     'fish_memory_save', 'fish_memory_complete', 'fish_self_control'):
            self.assertIn(name, offered)
        known = {definition['name']: definition for definition in builtins('qq')}
        for definition in desktop.kwargs[0]['tools']:
            self.assertEqual({'type', 'function'}, set(definition))
            self.assertEqual('function', definition['type'])
            function = definition['function']
            self.assertEqual(known[function['name']], function)
            self.assertEqual('object', function['parameters']['type'])
        plain = self.turn('', [final('好')])
        self.assertNotIn('tools', plain.kwargs[0])

    # 1b
    def test_history_description_names_the_snapshot_source(self):
        definition = next(row for row in builtins('qq') if row['name'] == 'fish_group_history')
        description = definition['description']
        self.assertIn('observed_from', description)
        self.assertIn('群测试窗口', description)
        self.assertIn('主策略', description)

    # 2
    def test_authorised_history_reaches_the_model_as_real_rows(self):
        self.add_message('今晚八点开组会，记得带笔记本', mid=MID, minutes=2)
        state = {'previewed': set(), 'tools': []}
        gateway = self.turn('owner-private', [
            wanting(call('fish_group_history', {'group_id': GROUP, 'minutes': 60})),
            final('好，我看到了。')], emoji_state=state)
        payload = gateway.tool_payloads()[0]
        self.assertTrue(payload['authorized'])
        self.assertEqual(GROUP, payload['group_id'])
        self.assertEqual([MID], [message['message_id'] for message in payload['messages']])
        self.assertEqual('今晚八点开组会，记得带笔记本', payload['messages'][0]['text'])
        self.assertEqual(SENDER, payload['messages'][0]['sender_id'])
        self.assertEqual('inbound', payload['messages'][0]['direction'])
        self.assertEqual('好，我看到了。', gateway.answer)
        # 这一轮她先读历史，再调用发送工具说话：两次调用都要留痕。
        self.assertEqual([{'name': 'fish_group_history', 'arguments': {'group_id': GROUP, 'minutes': 60},
                           'ok': True},
                          {'name': 'fish_say', 'arguments': {'texts': ['好，我看到了。']}, 'ok': True}],
                         state['tools'])
        # Input-side evidence: the rows the model itself received, not just a right answer.
        delivered = [message for message in gateway.requests[1] if message.get('role') == 'tool']
        self.assertEqual(1, len(delivered))
        self.assertIn('今晚八点开组会，记得带笔记本', delivered[0]['content'])
        self.assertIn(MID, delivered[0]['content'])
        # Storage-side evidence: the console snapshot keeps ids and counts, never the body.
        snapshot = RequestInspector(self.root).latest('qq', gateway.session)['payload']
        self.assertEqual(sorted(definition['function']['name'] for definition in
                                tool_definitions('qq', 'owner-private', emoji_state=state)),
                         sorted(tool['function']['name'] for tool in snapshot['tools']))
        # 最后一轮请求里有两条工具结果：读历史的那次，和她说出口的那次。
        stored = [message for message in snapshot['messages'] if message.get('role') == 'tool']
        self.assertEqual(2, len(stored))
        summary = stored[0]['content']
        self.assertEqual([MID], summary['message_ids'])
        self.assertEqual(len(payload['gaps']), summary['gaps_count'])
        self.assertIs(True, summary['authorized'])
        self.assertEqual(60, summary['window']['minutes'])
        self.assertIn('messages', summary['keys'])
        self.assertNotIn('今晚八点开组会，记得带笔记本', json.dumps(stored, ensure_ascii=False))

    # 3
    def test_unauthorised_group_returns_error_without_leaking_text(self):
        self.add_message(SECRET, mid='7400019999', chat=OTHER_CHAT, minutes=2)
        gateway = self.turn('owner-desktop', [
            wanting(call('fish_group_history', {'group_id': OTHER})), final('换个话题吧。')])
        payload = gateway.tool_payloads()[0]
        self.assertIn('error', payload)
        self.assertIn('群未获主人读取授权：' + OTHER_CHAT, payload['error'])
        self.assertNotIn(SECRET, json.dumps(payload, ensure_ascii=False))
        self.assertNotIn('messages', payload)
        self.assertEqual('换个话题吧。', gateway.answer)

    # 4
    def test_group_session_identity_cannot_reach_history_tools(self):
        self.add_message('群里的话', mid=MID, minutes=2)
        session = 'chat:group:' + GROUP + ':' + uuid.uuid4().hex
        bind_session(self.root, 'qq', session, 'chat:group:' + GROUP, '')
        with self.assertRaises(PermissionError) as caught:
            self.host.execute('qq', session, 'fish_group_history', {'group_id': GROUP}, 'forged-1')
        self.assertIn('群历史与网页工具仅用于主人私聊或本地控制台', str(caught.exception))
        self.assertNotIn('群里的话', str(caught.exception))

    # 5
    def test_web_fetch_refuses_loopback_before_any_request(self):
        with patch('requests.post') as post, patch('requests.get') as get:
            value = self.host.execute('qq', self.private_session, 'fish_web_fetch',
                                      {'url': 'http://127.0.0.1/x'}, 'fetch-1')
        self.assertFalse(value['ok'])
        self.assertIn('本机', value['error'])
        post.assert_not_called()
        get.assert_not_called()

    # 6
    def test_web_search_failure_is_an_error_fact(self):
        with patch.object(WebKnowledge, 'search_tool',
                          return_value={'ok': False, 'query': '天气', 'error': '网络请求失败（Timeout）'}):
            gateway = self.turn('owner-desktop', [
                wanting(call('fish_web_search', {'query': '天气'})), final('查不到就直说。')])
        payload = gateway.tool_payloads()[0]
        self.assertFalse(payload['ok'])
        self.assertIn('网络请求失败', payload['error'])
        self.assertEqual('查不到就直说。', gateway.answer)

    # 7
    def test_tool_limit_zero_is_unlimited_and_positive_is_enforced(self):
        Governance(self.root).save({'max_tool_calls_per_turn': 1})
        with self.assertRaises(ValueError) as caught:
            self.turn('owner-desktop', [wanting(call('fish_group_list', {}, 'a'), call('fish_group_list', {}, 'b')),
                                        final('不该到这里')])
        self.assertIn('本轮工具调用达到当前设置的上限', str(caught.exception))
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        gateway = self.turn('owner-desktop', [
            wanting(call('fish_group_list', {}, 'a')), wanting(call('fish_group_list', {}, 'b')),
            wanting(call('fish_group_history', {'group_id': GROUP}, 'c')), final('查完了')])
        self.assertEqual('查完了', gateway.answer)
        self.assertEqual(3, len([message for message in gateway.requests[-1] if message.get('role') == 'tool']))

    # 8
    def test_cancelled_turn_stops_before_model_or_tools(self):
        cancelled = threading.Event()
        cancelled.set()
        gateway = FakeGateway([final('不该生成')])
        with patch('model_gateway.ModelGateway', lambda *args, **kwargs: gateway), \
             patch('model_gateway.chat_options', lambda root: ChatOptions('high', 512, 30)):
            with self.assertRaises(InterruptedError):
                respond(self.root, 'qq', persona={'id': 'rev-1', 'body': '你是一条鱼。'},
                    history=[], history_source='test', memory='', scope='owner', references='',
                    current=[{'type': 'text', 'text': '在吗'}], session=self.private_session,
                    cancelled=cancelled, origin='test:cancel', output='text', tool_context='owner-desktop')
        self.assertEqual([], gateway.requests)

    # 9
    def test_invented_tool_name_is_an_error_result(self):
        gateway = self.turn('owner-desktop', [
            wanting(call('fish_nope', {})), final('好的。')])
        payload = gateway.tool_payloads()[0]
        self.assertIn('error', payload)
        self.assertIn('本轮未提供该工具：fish_nope', payload['error'])
        self.assertEqual('好的。', gateway.answer)

    # 12
    def test_space_desktop_window_publishes_no_tools(self):
        from desktop_chat import DesktopChat
        chat = DesktopChat(self.root, 'qq', Mock())
        cid = chat.new('人格测试窗口')
        space = chat.conversation_context(cid)
        self.assertIsNotNone(space)
        captured = {}

        def answer(root, role, **kwargs):
            captured.update(kwargs)
            return '好'

        media = Mock()
        media.chat_completions_content.return_value = [{'type': 'text', 'text': '在吗'}]
        with patch('tool_host.bind_session') as bind, patch('fish_chat.respond', answer), \
             patch('model_gateway.ModelGateway', lambda *args, **kwargs: Mock()):
            result = chat._chat_turn(cid, 1, [{'type': 'text', 'text': '在吗'}], [], media,
                                     Mock(), space['scope'], space)
        self.assertEqual({'ok': True, 'text': '好'}, result)
        bind.assert_not_called()
        self.assertEqual('', captured['tool_context'])
        self.assertEqual([], tool_definitions('qq', captured['tool_context']))
        self.assertEqual('desktop:' + cid, captured['session'])

    # 10
    def test_capabilities_report_the_published_openai_tool_shape(self):
        gateway = self.turn('owner-private', [final('好')])
        identity = self.host.identity('qq', gateway.session)
        report = current_capabilities(self.root, 'qq', identity)
        self.assertTrue(report['captured'])
        self.assertIn('fish_group_history', report['tools'])
        self.assertIn('fish_group_list', report['tools'])
        self.assertIn('fish_emoji_search', report['tools'])
        self.assertTrue(report['group_history'])
        self.assertTrue(report['group_list'])
        self.assertTrue(report['web_search'])
        self.assertTrue(report['web_fetch'])
        self.assertTrue(report['tool_calls_unlimited'])
        self.assertEqual([], report['local_file_and_terminal_tools'])

    # 11
    def test_a_plain_text_round_publishes_the_sticker_tools_and_answers_no(self):
        # 桌面这一轮不产生气泡，表情仍然在目录里（能力是同一套），只是这一轮接不住：
        # 调用拿到一句中文说明，而不是工具凭空消失。
        gateway = self.turn('owner-desktop', [
            wanting(call('fish_emoji_search', {'context': '想笑'})), final('好')])
        self.assertIsNone(gateway.state)
        self.assertIn('fish_emoji_search',
                      [definition['function']['name'] for definition in gateway.kwargs[0]['tools']])
        payload = gateway.tool_payloads()[0]
        self.assertIn('纯文本回复', payload['error'])
        self.assertEqual('好', gateway.answer)

    # 13
    def test_completeness_claim_gets_exactly_one_correction_round(self):
        with patch('group_read.read_group', return_value=history()):
            gateway = self.turn('owner-desktop', [
                wanting(call('fish_group_history', {'group_id': GROUP, 'minutes': 120})),
                final('群里全都在聊组会的事。'),
                final('我看到的是本地记下的这几条，别的时段我没有记录。')])
        self.assertEqual(3, len(gateway.requests))
        self.assertEqual('我看到的是本地记下的这几条，别的时段我没有记录。', gateway.answer)
        correction = gateway.requests[2][-1]
        self.assertEqual('user', correction['role'])
        self.assertIn('2026-02-12 10:00:01', correction['content'])
        self.assertIn('实际跨度 40 分钟', correction['content'])
        self.assertIn('缺口：该群未开启旁听或学习', correction['content'])
        self.assertIn('不要写固定免责声明', correction['content'])

    # 14
    def test_answer_without_a_completeness_claim_costs_no_extra_round(self):
        with patch('group_read.read_group', return_value=history()):
            gateway = self.turn('owner-desktop', [
                wanting(call('fish_group_history', {'group_id': GROUP, 'minutes': 120})),
                final('我查到的这几条是在说组会。')])
        self.assertEqual(2, len(gateway.requests))
        self.assertEqual('我查到的这几条是在说组会。', gateway.answer)

    # 15
    def test_plain_chat_without_gaps_adds_nothing(self):
        gateway = self.turn('owner-desktop', [final('今天没什么特别的。')])
        self.assertEqual(1, len(gateway.requests))
        self.assertEqual('今天没什么特别的。', gateway.answer)

    # 16
    def test_second_claim_returns_that_text_and_records_one_audit(self):
        with patch('group_read.read_group', return_value=history()):
            gateway = self.turn('owner-desktop', [
                wanting(call('fish_group_history', {'group_id': GROUP, 'minutes': 120})),
                final('群里就这些，全都在聊组会。'),
                final('群里就这些，没有别的。')])
        self.assertEqual(3, len(gateway.requests))
        self.assertEqual('群里就这些，没有别的。', gateway.answer)
        with closing(sqlite3.connect(self.root / 'data/qq/governance.db')) as db:
            kinds = [row[0] for row in db.execute("SELECT kind FROM audit WHERE kind='chat.completeness'")]
        self.assertEqual(['chat.completeness'], kinds)

    # 17
    def test_cancelling_during_the_correction_round_raises(self):
        cancelled = threading.Event()
        with patch('group_read.read_group', return_value=history()):
            with self.assertRaises(InterruptedError):
                self.turn('owner-desktop', [
                    wanting(call('fish_group_history', {'group_id': GROUP, 'minutes': 120})),
                    final('群里全都在聊组会。')], cancelled=cancelled, cancel_after=2)
        self.assertTrue(cancelled.is_set())

    # 18
    def test_a_correct_time_statement_needs_no_qualifier(self):
        with patch('group_read.read_group', return_value=history()):
            gateway = self.turn('owner-desktop', [
                wanting(call('fish_group_history', {'group_id': GROUP, 'minutes': 120})),
                final('我读到的这几条跨了 40 分钟。')])
        self.assertEqual(2, len(gateway.requests))
        self.assertEqual('我读到的这几条跨了 40 分钟。', gateway.answer)

    # 19
    def test_a_stated_uncertainty_about_completeness_needs_no_round(self):
        with patch('group_read.read_group', return_value=history()):
            gateway = self.turn('owner-desktop', [
                wanting(call('fish_group_history', {'group_id': GROUP, 'minutes': 120})),
                final('我无法确认是否包含全部消息。')])
        self.assertEqual(2, len(gateway.requests))
        self.assertEqual('我无法确认是否包含全部消息。', gateway.answer)

    # 20
    def test_an_uncertain_group_question_needs_no_round(self):
        with patch('group_read.read_group', return_value=history()):
            gateway = self.turn('owner-desktop', [
                wanting(call('fish_group_history', {'group_id': GROUP, 'minutes': 120})),
                final('不确定是不是所有人都在聊。')])
        self.assertEqual(2, len(gateway.requests))
        self.assertEqual('不确定是不是所有人都在聊。', gateway.answer)

    # 21
    def test_a_negated_wording_still_claims_coverage(self):
        with patch('group_read.read_group', return_value=history()):
            gateway = self.turn('owner-desktop', [
                wanting(call('fish_group_history', {'group_id': GROUP, 'minutes': 120})),
                final('不是全部，是整个群都在聊。'),
                final('我看到的是本地记下的这几条。')])
        self.assertEqual(3, len(gateway.requests))
        self.assertEqual('我看到的是本地记下的这几条。', gateway.answer)
        self.assertIn('实际跨度 40 分钟', gateway.requests[2][-1]['content'])


if __name__ == '__main__':
    unittest.main()
