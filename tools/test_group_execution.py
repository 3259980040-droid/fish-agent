"""01-03: group messages feed the one main execution; @ and 8 messages trigger it.

Real tool host, real group policy store, fake model, fake QQ control server and fake
channel. No websocket and no group message is ever sent.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]

from fish_agent import get_runtime, reset_runtime
from fish_agent.execution import state as st
from governance import Governance
from group_policy import GroupPolicy
from model_gateway import ChatOptions, ChatResult
from qq_store import QQStore
from social_context import SocialContext
from storage import database
from tool_host import ToolHost

OWNER = '3259980040'
GROUP = '1043230169'
CHAT = 'group:' + GROUP


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def tool_call(name, arguments, call_id='call-1'):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


def saying(text):
    return ChatResult('', {}, 'tool_calls', (tool_call('fish_say', {'texts': [text]}),))


def final(text=''):
    return ChatResult(text, {}, 'stop')


class FakeGateway:
    base = 'https://fake.invalid'
    model = 'fake-model'

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.lock = threading.Lock()
        self.gate = threading.Event()
        self.gate.set()
        self.called = threading.Event()

    def chat(self, messages, **kwargs):
        with self.lock:
            self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False, default=str)))
        self.called.set()
        self.gate.wait(15)
        with self.lock:
            if not self.replies:
                raise AssertionError('假模型被调用的次数超过本用例提供的回复')
            reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if reply.first_output_at is None:
            from dataclasses import replace
            reply = replace(reply, first_output_at=time.time())
        return reply

    def renders(self, index=0):
        return json.dumps(self.requests[index], ensure_ascii=False)


class FakeBudget:
    def __init__(self, root, gateway):
        self.root, self.gateway = root, gateway

    def fit(self, messages, tools, output_reserve):
        for message in messages:
            for key in ('_fish_layer', '_fish_refs', '_fish_bundle', 'fish_bubbles'):
                message.pop(key, None)
        return {'estimated_input_tokens': 10, 'input_ceiling': 1000, 'covered': [],
                'trimmed': [], 'layers': {'0': 10}}

    def begin(self, *args):
        return 'accounting'

    def finish(self, *args, **kwargs):
        return None


class FakeQQControl:
    received = []

    @classmethod
    def start(cls):
        cls.received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(length) or b'{}')
                cls.received.append({'path': self.path, 'body': body})
                payload = json.dumps({'ok': True, 'sent': True, 'state': 'sent', 'error': '',
                                      'receipt': {'message_id': 'fake-group-1'}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True, name='fake-qq-group').start()
        return server, int(server.server_address[1])


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.epoch = 1
        self.last_seq = 0
        self.bot = {'id': 'bot-1'}
        self._events = asyncio.Queue()
        self._processing = False

    async def action(self, name, payload, timeout=30, **kwargs):
        self.sent.append({'name': name, 'payload': payload})
        return {'ok': True, 'payload': {'message_id': 'qq-group-mid-' + str(len(self.sent))}}


class GroupExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'harness').mkdir(parents=True, exist_ok=True)
        write_json(self.root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'name': 'Fish', 'model': 'fake-model',
                        'base_url': 'https://fake.invalid', 'apiKeyEnv': 'FISH_TEST_KEY',
                        'persona': 'harness/agents/qq.persona.md'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.root / 'channels/fish_qq/config.json', {
            'allowlist': {'owner_id': OWNER, 'group_ids': [GROUP]},
            'bot': {'display_name': '大肥鱼', 'id': 'bot-1'}})
        SocialContext(self.root).ensure_default('qq')
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        self.policy = GroupPolicy(self.root)
        self.policy.configure(CHAT, {'listen': True, 'reply': True, 'engine': 'fish'})
        self.store = QQStore(self.root)
        self.conversation = self.store.get_or_create_conversation(CHAT, 'qq')
        self.host = ToolHost(self.root)
        self.host.serve(background_workers=False, listen_port=0)
        self.addCleanup(self.stop_host)
        config = json.loads((self.root / 'harness/agents.json').read_text(encoding='utf-8'))
        config['runtime']['tool_host_port'] = int(self.host.server.server_address[1])
        write_json(self.root / 'harness/agents.json', config)
        from context_budget import _tokenizer
        tokenizer = _tokenizer(str(ROOT / 'vendor/tokenizers/deepseek-v4/tokenizer.json'))
        counter = patch('desktop_context._tokens',
                        lambda root, text: len(tokenizer.encode(str(text or '')).ids))
        counter.start()
        self.addCleanup(counter.stop)
        self.control, port = FakeQQControl.start()
        self.addCleanup(self.close_control)
        self.port_patch = patch.dict(os.environ, {'FISH_QQ_CONTROL_PORT': str(port)})
        self.port_patch.start()
        self.addCleanup(self.port_patch.stop)

    def close_control(self):
        self.control.shutdown()
        self.control.server_close()

    def stop_host(self):
        reset_runtime(self.root)
        self.host.stop.set()
        self.host.server.shutdown()
        self.host.server.server_close()

    def gateway(self, replies):
        instance = FakeGateway(replies)

        def make(*args, **kwargs):
            return instance
        for start in (patch('model_gateway.ModelGateway', side_effect=make),
                      patch('context_budget.ContextBudget', FakeBudget),
                      patch('model_gateway.chat_options', lambda root: ChatOptions('high', 512, 30))):
            start.start()
            self.addCleanup(start.stop)
        return instance

    def inbound(self, text, channel_id, *, sender='2100000001', name='群友甲', mention=False):
        segments = [{'type': 'text', 'text': text}]
        if mention:
            segments.insert(0, {'type': 'at', 'user_id': 'bot-1'})
        row = self.store.record_message(self.conversation['id'], CHAT, sender, 'inbound',
                                        channel_id, None, segments, text, 'observed')
        payload = {'chat_id': CHAT, 'message_id': channel_id,
                   'sender': {'id': sender, 'display_name': name},
                   'segments': segments, 'reply_to': '', 'text': text,
                   'mentions_bot': mention, 'reply_to_bot': False}
        return row, payload

    def submit(self, runtime, row, payload, text):
        return runtime.execution.submit(source='qq-group', conversation=self.conversation['id'],
                                        message=row, text=text, kind='message',
                                        reference='qq:' + str(row), payload=payload)

    def wait_run(self, runtime, execution, timeout=25):
        deadline = time.time() + timeout
        row = runtime.execution.status(execution) if execution else None
        while row and row['status'] in ('queued', 'running') and time.time() < deadline:
            time.sleep(0.03)
            row = runtime.execution.status(execution)
        return row

    def runs(self):
        with database(self.root / 'data/social/experience.db') as db:
            return [dict(row) for row in db.execute('SELECT * FROM agent_runs ORDER BY created,rowid')]

    def test_seven_group_messages_wait_and_the_eighth_starts_one_turn(self):
        runtime = get_runtime(self.root)
        runtime.start()
        for index in range(7):
            row, payload = self.inbound('普通第%d句' % index, 'gm-%d' % index)
            result = self.submit(runtime, row, payload, '普通第%d句' % index)
            self.assertEqual(result['execution'], '', '前七条只登记，不开启回合')
        self.assertEqual(self.runs(), [])
        self.gateway([saying('群里答一句'), final('')])
        row, payload = self.inbound('普通第八句', 'gm-8')
        result = self.submit(runtime, row, payload, '普通第八句')
        self.assertTrue(result['execution'], '第八条触发一次思考')
        settled = self.wait_run(runtime, result['execution'])
        self.assertEqual(settled['status'], st.COMPLETE, settled)
        runs = self.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(json.loads(runs[0]['inputs'])), 8)  # all waiting inputs merged
        self.assertEqual(len(FakeQQControl.received), 1)
        delivered = FakeQQControl.received[0]['body']
        self.assertEqual(delivered['target'], CHAT)
        self.assertEqual([bubble['text'] for bubble in delivered['bubbles']], ['群里答一句'])

    def test_a_mention_starts_a_turn_without_waiting_for_the_count(self):
        runtime = get_runtime(self.root)
        runtime.start()
        for index in range(3):
            row, payload = self.inbound('背景第%d句' % index, 'gm-b%d' % index)
            self.assertEqual(self.submit(runtime, row, payload, '背景')['execution'], '')
        self.gateway([saying('叫我吗'), final('')])
        row, payload = self.inbound('叫她一下', 'gm-at', mention=True)
        result = self.submit(runtime, row, payload, '叫她一下')
        self.assertTrue(result['execution'])
        settled = self.wait_run(runtime, result['execution'])
        self.assertEqual(settled['status'], st.COMPLETE, settled)
        runs = self.runs()
        self.assertEqual(len(json.loads(runs[0]['inputs'])), 4)
        delivered = FakeQQControl.received[0]['body']
        self.assertTrue(delivered['direct'])
        self.assertEqual(delivered['reply_to'], 'gm-at')

    def test_group_messages_arriving_while_thinking_wait_and_are_merged(self):
        runtime = get_runtime(self.root)
        runtime.start()
        fake = self.gateway([saying('第一轮'), final(''), saying('合并轮'), final('')])
        fake.gate.clear()
        row, payload = self.inbound('第一句', 'gm-1', mention=True)
        first = self.submit(runtime, row, payload, '第一句')
        self.assertTrue(fake.called.wait(10))
        queued = []
        for index in range(2, 10):
            row, payload = self.inbound('等待第%d句' % index, 'gm-%d' % index)
            queued.append(self.submit(runtime, row, payload, '等待第%d句' % index))
        triggered = [item['execution'] for item in queued if item['execution']]
        self.assertEqual(len(triggered), 1, queued)
        fake.gate.set()
        self.assertEqual(self.wait_run(runtime, first['execution'])['status'], st.COMPLETE)
        self.assertEqual(self.wait_run(runtime, triggered[0])['status'], st.COMPLETE)
        runs = self.runs()
        self.assertEqual([run['status'] for run in runs], [st.COMPLETE, st.COMPLETE])
        self.assertEqual(len(json.loads(runs[1]['inputs'])), 8)
        self.assertEqual(len(FakeQQControl.received), 2)
        self.assertIn('等待第9句', fake.renders(2))

    def test_the_host_transport_accepts_every_standard_source(self):
        # Every entry submits through this one route; a missing stored record must
        # fail the turn honestly instead of the route rejecting the source.
        self.host.runtime = get_runtime(self.root)
        cases = [
            ('desktop', 'conv-x', {'message': 999999, 'reference': 'desktop:999999'}),
            ('qq-private', self.conversation['id'], {'message': 999998, 'reference': 'qq:999998'}),
            ('qq-group', self.conversation['id'], {'message': 999997, 'reference': 'qq:999997',
                                                   'payload': {'chat_id': CHAT, 'message_id': 'x', 'mentions_bot': True}}),
            ('plan', self.conversation['id'], {'message': 999996, 'reference': 'plan:x:y',
                                               'kind': 'plan', 'payload': {}}),
        ]
        for source, conversation, extra in cases:
            body = {'source': source, 'conversation': conversation, 'text': '', **extra}
            result = self.host.dispatch('/submit', body, role='qq', admin=True)
            self.assertTrue(result.get('execution'), (source, result))
            settled = self.wait_run(get_runtime(self.root), result['execution'])
            self.assertEqual(settled['status'], st.FAILED, (source, settled))
            self.assertNotIn('未知输入来源', settled['error'])

    def test_a_transport_group_turn_keeps_its_place_for_a_later_wake(self):
        runtime = get_runtime(self.root)
        runtime.start()
        self.host.runtime = runtime
        row, payload = self.inbound('@我 在吗', 'gm-place', mention=True)
        body = {'source': 'qq-group', 'conversation': self.conversation['id'],
                'message': str(row), 'text': '@我 在吗', 'place': CHAT,
                'reference': 'qq:' + str(row), 'kind': 'message', 'payload': payload}
        self.gateway([saying('在的'), final(''), saying('自主看一眼'), final('')])
        result = self.host.dispatch('/submit', body, role='qq', admin=True)
        settled = self.wait_run(runtime, result['execution'])
        self.assertEqual(settled['status'], st.COMPLETE, settled)
        with database(self.root / 'data/social/experience.db') as db:
            place = db.execute('SELECT place FROM agent_inputs WHERE reference=?',
                               ('qq:' + str(row),)).fetchone()[0]
        self.assertEqual(place, CHAT)
        # The 45-minute wake after a group input must build group materials, not fail
        # with "自主回合缺少群场所" because the transport dropped the place.
        deadline = time.time() + 5
        start = runtime.state.get_state('auto_deadline')
        while runtime.state.get_state('auto_deadline') == start and time.time() < deadline:
            time.sleep(0.05)
        runtime.state.set_state('auto_deadline', str(time.time() - 1))
        wake_deadline = time.time() + 15
        timer = None
        while time.time() < wake_deadline:
            with database(self.root / 'data/social/experience.db') as db:
                found = db.execute("SELECT id FROM agent_runs WHERE kind='timer' ORDER BY created DESC LIMIT 1").fetchone()
            if found:
                timer = found[0]
                break
            time.sleep(0.1)
        self.assertIsNotNone(timer, 'the clock should have started a group wake')
        view = self.wait_run(runtime, timer)
        self.assertEqual(view['status'], st.COMPLETE, view)
        self.assertEqual(view['place'], CHAT)

    def test_a_paused_or_reply_off_group_never_reaches_a_turn(self):
        from qq_agent_loop import QQAgentLoop
        loop = QQAgentLoop(self.root)
        loop.store = self.store
        loop.conversation = self.conversation
        loop.channel = FakeChannel()
        bubbles = [{'kind': 'text', 'text': '不该发出'}]
        self.policy.configure(CHAT, {'reply': False})
        result = asyncio.run(loop.group_deliver({'target': CHAT, 'bubbles': bubbles,
                                                 'key': 'k', 'reply_to': 'gm-1'}))
        self.assertTrue(result['ok'])
        self.assertFalse(result['sent'])
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(loop.channel.sent, [])
        self.policy.configure(CHAT, {'reply': True, 'dive_until': time.time() + 600})
        result = asyncio.run(loop.group_deliver({'target': CHAT, 'bubbles': bubbles,
                                                 'key': 'k', 'reply_to': 'gm-1'}))
        self.assertEqual(result['state'], 'blocked')

    def test_dive_and_hourly_limit_are_real_gates_and_a_mention_bypasses_the_limit(self):
        from qq_agent_loop import QQAgentLoop
        loop = QQAgentLoop(self.root)
        loop.store = self.store
        loop.conversation = self.conversation
        loop.channel = FakeChannel()
        bubbles = [{'kind': 'text', 'text': '潜水中不该发'}]
        self.policy.configure(CHAT, {'dive_until': time.time() + 600})
        result = asyncio.run(loop.group_deliver({'target': CHAT, 'bubbles': bubbles,
                                                 'key': 'k', 'reply_to': 'gm-1'}))
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(loop.channel.sent, [])
        self.policy.configure(CHAT, {'dive_until': 0, 'reply_limits': True, 'max_active_hour': 1})
        self.store.record_message(self.conversation['id'], CHAT, 'bot', 'outbound', '', None,
                                  [{'type': 'text', 'text': '上一句'}], '上一句', 'sent')
        result = asyncio.run(loop.group_deliver({'target': CHAT, 'bubbles': bubbles,
                                                 'key': 'k', 'reply_to': 'gm-2'}))
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('上限', result['error'])
        result = asyncio.run(loop.group_deliver({'target': CHAT, 'bubbles': bubbles,
                                                 'key': 'k', 'reply_to': 'gm-3', 'direct': True,
                                                 'reference': 'qq:' + str(self.store.counts() and 2)}))
        self.assertTrue(result['ok'])
        self.assertTrue(result['sent'], result)
        self.assertEqual(len(loop.channel.sent), 1)


if __name__ == '__main__':
    unittest.main()
