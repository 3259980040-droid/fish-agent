"""01-02: the owner's QQ private message runs in the one main execution.

The host side is the real workbench runtime with a fake model and a fake QQ control
server; the QQ side is the real QQAgentLoop with a fake channel. Nothing here opens
a websocket or calls a provider.
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
from model_gateway import ChatOptions, ChatResult
from qq_store import QQStore
from social_context import SocialContext
from storage import database
from tool_host import ToolHost

OWNER = '3259980040'


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
        self.options = []
        self.lock = threading.Lock()
        self.gate = threading.Event()
        self.gate.set()
        self.called = threading.Event()

    def chat(self, messages, **kwargs):
        with self.lock:
            self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False, default=str)))
            self.options.append({key: value for key, value in kwargs.items() if key != 'cancelled'})
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
    """Stands in for the QQ process's local control server; records one delivery."""

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
                cls.received.append({'path': self.path,
                                     'token': self.headers.get('x-fish-token', ''),
                                     'body': body})
                payload = json.dumps({'ok': True, 'sent': True, 'state': 'sent', 'error': '',
                                      'receipt': {'message_id': 'fake-qq-1'}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True, name='fake-qq-control').start()
        return server, int(server.server_address[1])


class FakeChannel:
    """The QQ transport without a websocket: records what would go out."""

    def __init__(self):
        self.sent = []
        self.epoch = 1
        self.last_seq = 0
        self.bot = {'id': 'bot-1'}
        import asyncio
        self._events = asyncio.Queue()
        self._processing = False

    async def action(self, name, payload, timeout=30, **kwargs):
        self.sent.append({'name': name, 'payload': payload})
        return {'ok': True, 'payload': {'message_id': 'qq-mid-' + str(len(self.sent))}}


class QQPrivateFlowTests(unittest.TestCase):
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
        write_json(self.root / 'channels/fish_qq/config.json', {'allowlist': {'owner_id': OWNER}})
        SocialContext(self.root).ensure_default('qq')
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        self.store = QQStore(self.root)
        self.conversation = self.store.get_or_create_conversation('private:' + OWNER, 'qq')
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
        self.control, self.control_port = FakeQQControl.start()
        self.addCleanup(self.close_control)
        self.port_patch = patch.dict(os.environ, {'FISH_QQ_CONTROL_PORT': str(self.control_port)})
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

    def inbound(self, text='在吗'):
        return self.store.record_message(self.conversation['id'], 'private:' + OWNER, OWNER,
                                         'inbound', 'chan-' + text, None, [], text, 'queued')

    def wait_run(self, runtime, execution, timeout=20):
        deadline = time.time() + timeout
        row = runtime.execution.status(execution)
        while row and row['status'] in ('queued', 'running') and time.time() < deadline:
            time.sleep(0.03)
            row = runtime.execution.status(execution)
        return row

    def qq_row(self, message):
        with database(self.store.db_path) as db:
            return dict(db.execute('SELECT * FROM messages WHERE id=?', (message,)).fetchone())

    def test_one_private_message_runs_in_the_host_and_delivers_through_the_channel(self):
        fake = self.gateway([saying('私聊答复'), final('')])
        runtime = get_runtime(self.root)
        runtime.start()
        message = self.inbound('在吗')
        submission = runtime.execution.submit(
            source='qq-private', conversation=self.conversation['id'], message=message,
            text='在吗', reference='qq:' + str(message))
        row = self.wait_run(runtime, submission['execution'])
        self.assertEqual(row['status'], st.COMPLETE, row)
        # The host never opened the websocket itself: the QQ adapter received the
        # decided bubbles and returned the real receipt the run records.
        self.assertEqual(len(FakeQQControl.received), 1)
        delivered = FakeQQControl.received[0]
        self.assertEqual(delivered['path'], '/deliver')
        self.assertEqual(delivered['body']['target'], 'private:' + OWNER)
        self.assertEqual([bubble['text'] for bubble in delivered['body']['bubbles']], ['私聊答复'])
        self.assertEqual(delivered['body']['message_id'], str(message))
        self.assertEqual(delivered['body']['key'], 'qq-agent:' + submission['execution'])
        self.assertTrue(json.loads(row['result'])['delivery']['sent'])
        # No duplicate generation and no second model turn for one message.
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(self.qq_row(message)['status'], 'processing')
        with database(self.root / 'data/social/experience.db') as db:
            input_row = db.execute('SELECT * FROM agent_inputs WHERE reference=?', ('qq:' + str(message),)).fetchone()
            run_row = db.execute('SELECT * FROM agent_runs WHERE id=?', (submission['execution'],)).fetchone()
        self.assertEqual(input_row['considered'], 1)
        self.assertEqual(run_row['status'], st.COMPLETE)

    def test_busy_private_messages_are_merged_and_the_client_waits_its_turn(self):
        fake = self.gateway([saying('第一轮'), final(''), saying('合并轮'), final('')])
        fake.gate.clear()
        runtime = get_runtime(self.root)
        runtime.start()
        # Hold the consumer open with the first turn, then submit the rest.
        first_message = self.inbound('第一句')
        first = runtime.execution.submit(source='qq-private', conversation=self.conversation['id'],
                                         message=first_message, text='第一句',
                                         reference='qq:' + str(first_message))
        self.assertTrue(fake.called.wait(10))
        second_message = self.inbound('第二句')
        third_message = self.inbound('第三句')
        second = runtime.execution.submit(source='qq-private', conversation=self.conversation['id'],
                                          message=second_message, text='第二句',
                                          reference='qq:' + str(second_message))
        third = runtime.execution.submit(source='qq-private', conversation=self.conversation['id'],
                                         message=third_message, text='第三句',
                                         reference='qq:' + str(third_message))
        fake.gate.set()
        row = self.wait_run(runtime, first['execution'])
        self.assertEqual(row['status'], st.COMPLETE)
        self.assertEqual(self.wait_run(runtime, second['execution'])['status'], st.COMPLETE)
        self.assertEqual(self.wait_run(runtime, third['execution'])['status'], st.COMPLETE)
        runs = [runtime.execution.status(item['execution']) for item in (first, second, third)]
        self.assertEqual([run['id'] for run in runs], [first['execution'], second['execution'], second['execution']])
        self.assertEqual(len(json.loads(runs[1]['inputs'])), 2)  # second and third merged
        # Delivery order follows the turns, not the number of messages.
        self.assertEqual(len(FakeQQControl.received), 2)

    def test_the_queue_keeps_arrival_order_across_places_without_mixing_them(self):
        state = get_runtime(self.root).state
        qq_input = state.record_input(reference='qq:1', source='qq-private', conversation=self.conversation['id'],
                                      message='1', text='私聊')
        desktop_input = state.record_input(reference='desktop:1', source='desktop', conversation='conv-1',
                                           message='1', text='客户端')
        qq_run = state.create_run(kind='message', source='qq-private', conversation=self.conversation['id'],
                                  input_reference=qq_input['reference'])
        desktop_run = state.create_run(kind='message', source='desktop', conversation='conv-1',
                                       input_reference=desktop_input['reference'])
        first, inputs = state.claim_next()
        self.assertEqual(first['id'], qq_run)  # oldest first
        self.assertEqual([item['reference'] for item in inputs], ['qq:1'])  # one place only
        second, inputs = state.claim_next()
        self.assertEqual(second['id'], desktop_run)
        self.assertEqual([item['reference'] for item in inputs], ['desktop:1'])

    def test_the_qq_side_submits_the_reference_and_does_not_generate(self):
        from owner_private import submit
        message = self.inbound('交给主执行')
        calls = []

        class Loop:
            root = self.root
            store = self.store

            def watch_private(self, execution, item):
                calls.append(('watch', execution, item['message_id']))

        with patch('tool_host.request', lambda root, route, body, **kwargs: calls.append((route, body)) or
                   {'execution': 'run-1', 'status': 'queued'}):
            submission = submit(Loop(), {'message_id': message, 'text': '交给主执行',
                                         'payload': {'chat_id': 'private:' + OWNER},
                                         'conversation': {'id': self.conversation['id']}})
        self.assertEqual(submission['execution'], 'run-1')
        self.assertEqual(calls[0][0], '/submit')
        self.assertEqual(calls[0][1]['source'], 'qq-private')
        self.assertEqual(calls[0][1]['reference'], 'qq:' + str(message))
        self.assertEqual(calls[1], ('watch', 'run-1', message))
        self.assertEqual(self.qq_row(message)['status'], 'submitted')
        self.assertEqual(self.qq_row(message)['runtime_turn_id'], 'run-1')

    def test_the_channel_end_sends_decided_bubbles_and_records_the_receipt(self):
        from qq_agent_loop import QQAgentLoop
        loop = QQAgentLoop(self.root)
        loop.store = self.store
        loop.conversation = self.conversation
        loop.channel = FakeChannel()
        message = self.inbound('送到私聊')
        from experience import ExperienceTurn
        turn = ExperienceTurn(self.root, self.store.db_path, self.conversation['id'], message, channel='qq')
        turn.session = 'qq-private:' + self.conversation['id']
        turn.prepare()
        self.store.create_turn('run-test', self.conversation['id'], '', 'run-test')
        result = asyncio.run(loop.private_deliver({
            'target': 'private:' + OWNER, 'bubbles': [{'kind': 'text', 'text': '好的'}],
            'key': 'qq-agent:test', 'turn': 'run-test', 'message_id': message}))
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['sent'], result)
        self.assertEqual(result['state'], 'sent')
        self.assertEqual([item['payload']['segments'][0]['text'] for item in loop.channel.sent], ['好的'])
        inbound = self.qq_row(message)
        self.assertEqual(inbound['status'], 'processed')
        with database(self.store.db_path) as db:
            outbound = [dict(row) for row in db.execute(
                "SELECT * FROM messages WHERE direction='outbound' ORDER BY id")]
            turn_row = db.execute('SELECT * FROM runtime_turns WHERE id=?', ('run-test',)).fetchone()
        self.assertEqual([row['status'] for row in outbound], ['sent'])
        self.assertEqual(outbound[0]['channel_message_id'], 'qq-mid-1')
        self.assertEqual(turn_row['status'], 'done')
        with database(self.root / 'data/social/experience.db') as db:
            turn = db.execute('SELECT * FROM experience_turns WHERE reference=?', ('qq:' + str(message),)).fetchone()
        self.assertEqual(turn['outcome'], 'sent')
        self.assertIsNotNone(turn['sending'])
        self.assertIsNotNone(turn['receipt'])

    def test_the_channel_end_refuses_a_target_that_is_not_the_verified_owner(self):
        from qq_agent_loop import QQAgentLoop
        loop = QQAgentLoop(self.root)
        loop.store = self.store
        loop.conversation = self.conversation
        loop.channel = FakeChannel()
        result = asyncio.run(loop.private_deliver({
            'target': 'private:999', 'bubbles': [{'kind': 'text', 'text': 'x'}],
            'key': 'k', 'turn': 't', 'message_id': 1}))
        self.assertFalse(result['ok'])
        self.assertEqual(loop.channel.sent, [])


if __name__ == '__main__':
    unittest.main()
