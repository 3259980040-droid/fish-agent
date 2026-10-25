"""08-01: consecutive 3-second file batches, real sources and range reads.

The batch window is exercised with explicit reception times; one desktop round runs
through the real workbench/runtime/tool host with a fake model; the large-original
check uses the existing attachment store.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]

from fish_agent import get_runtime, reset_runtime
from fish_agent.execution import state as st
from fish_agent.experience.batches import BatchLog
from governance import Governance
from model_gateway import ChatOptions, ChatResult
from social_context import SocialContext
from storage import database
from tool_host import ToolHost
from workbench import Workbench

OWNER = '3259980040'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def tool_call(name, arguments, call_id='call-1'):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


class FakeGateway:
    base = 'https://fake.invalid'
    model = 'fake-model'

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.lock = threading.Lock()

    def chat(self, messages, **kwargs):
        with self.lock:
            self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False, default=str)))
            if not self.replies:
                raise AssertionError('假模型被调用的次数超过本用例提供的回复')
            reply = self.replies.pop(0)
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


def saying(text):
    return ChatResult('', {}, 'tool_calls', (tool_call('fish_say', {'texts': [text]}),))


def final(text=''):
    return ChatResult(text, {}, 'stop')


class BatchWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = BatchLog(self.root)

    def test_fixed_windows_boundary_and_a_new_round(self):
        place = 'desktop:conv-1'
        first = self.log.register(place=place, reference='desktop:1', resource='a', received=100.0)
        second = self.log.register(place=place, reference='desktop:1', resource='b', received=101.0)
        self.assertEqual(first, second)
        # Exactly on the boundary belongs to the next window.
        boundary = self.log.register(place=place, reference='desktop:2', resource='c', received=103.0)
        self.assertNotEqual(boundary, first)
        same = self.log.register(place=place, reference='desktop:2', resource='d', received=103.5)
        self.assertEqual(same, boundary)
        batches = {item['id']: item for item in self.log.state(place)}
        self.assertEqual(batches[first]['opens'], 100.0)
        self.assertEqual(batches[first]['closes'], 103.0)  # a new file never extends it
        self.assertEqual([item['resource'] for item in batches[first]['items']], ['a', 'b'])
        self.assertEqual([item['seq'] for item in batches[first]['items']], [1, 2])
        self.assertEqual([item['reference'] for item in batches[first]['items']],
                         ['desktop:1', 'desktop:1'])
        # A long idle gap is a new reception round, not empty batches.
        later = self.log.register(place=place, reference='desktop:3', resource='e', received=130.0)
        self.assertNotIn(later, batches)
        newest = self.log.state(place)[0]
        self.assertEqual(newest['opens'], 130.0)
        self.assertEqual(len(newest['items']), 1)

    def test_take_waits_only_for_a_file_turn_and_hands_each_batch_once(self):
        place = 'qq-private:conv'
        self.log.register(place=place, reference='qq:1', resource='x', received=time.time())
        started = time.time()
        taken = self.log.take(place, wait_for_close=True)
        waited = time.time() - started
        self.assertGreaterEqual(waited, 2.0)
        self.assertLess(waited, 4.5)
        self.assertEqual(len(taken), 1)
        self.assertEqual([item['resource'] for item in taken[0]['items']], ['x'])
        # A handed batch is not handed again; a fresh open batch is not stolen.
        self.assertEqual(self.log.take(place, wait_for_close=False), [])
        self.log.register(place=place, reference='qq:2', resource='y', received=time.time())
        self.assertEqual(self.log.take(place, wait_for_close=False), [])
        self.log.finish([taken[0]['id']], ok=True)
        self.assertEqual(self.log.state(place)[0]['state'], 'open')
        self.assertTrue(self.log.covered(['qq:1']))

    def test_a_large_original_is_registered_and_read_by_range(self):
        from attachment_store import AttachmentStore
        big = self.root / 'big.txt'
        block = ('0123456789' * 100 + '\n').encode()
        with big.open('wb') as handle:
            for _ in range(26 * 1024):
                handle.write(block)
        store = AttachmentStore(self.root, 'qq')
        record = store.import_path('conv-big', big, name='big.txt', source='test', type_hint='file')
        self.assertGreater(record['size'], 20 * 1024 * 1024)
        self.assertIsNone(store.limits()['original_max_bytes'])
        chunk = store.read_range(record['id'], offset=1000, length=100, session='test:08')
        self.assertEqual(chunk['start'], 1000)
        self.assertEqual(chunk['end'], 1100)
        self.assertEqual(chunk['text'], big.read_bytes()[1000:1100].decode('utf-8'))
        self.assertEqual(chunk['size'], record['size'])


class BatchTurnTests(unittest.TestCase):
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
        self.runtime = get_runtime(self.root)
        self.runtime.start()
        self.host.runtime = self.runtime

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

    def wait_job(self, wb, job_id, timeout=30):
        deadline = time.time() + timeout
        row = wb.dispatch('chat.job', {'job_id': job_id})
        while row['status'] in ('queued', 'running') and time.time() < deadline:
            time.sleep(0.05)
            row = wb.dispatch('chat.job', {'job_id': job_id})
        return row

    def test_a_deliberate_silent_file_turn_still_consumes_its_batch(self):
        # The model reads the file and chooses not to speak: that turn already used the
        # batch, so a later text turn must not receive the same file again.
        fake = self.gateway([final(''), saying('文件里是一句静默批次的测试内容'), final('')])
        wb = Workbench(self.root)
        cid = wb.dispatch('chat.new', {'title': '静默批次'})['id']
        path = self.root / 'quiet.txt'
        path.write_text('静默批次的文件内容', encoding='utf-8')
        first = wb.dispatch('chat.send', {'id': cid, 'text': '看看这个文件',
                                          'attachments': [{'path': str(path), 'type': 'file'}],
                                          'request_id': uuid.uuid4().hex})
        settled = self.wait_job(wb, first['job_id'])
        self.assertEqual(settled['status'], 'complete', settled)
        batches = wb.dispatch('attachments.batches', {'id': cid})['batches']
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]['state'], 'consumed')
        view = wb.dispatch('turn.view', {'execution': settled['execution']})
        self.assertTrue(view['silent'])
        # A later text-only turn does not inherit the file.
        second = wb.dispatch('chat.send', {'id': cid, 'text': '刚才那个文件里写了什么？',
                                           'request_id': uuid.uuid4().hex})
        second_settled = self.wait_job(wb, second['job_id'])
        self.assertEqual(second_settled['status'], 'complete')
        with database(self.root / 'data/social/experience.db') as db:
            runs = [dict(row) for row in db.execute('SELECT * FROM agent_runs ORDER BY created')]
        self.assertEqual(runs[1]['status'], st.COMPLETE)
        self.assertEqual(runs[1]['error'], '')  # it ran, rather than being merged away
        # The silent turn consumed the batch; the next turn is handed no batch of its own.
        self.assertEqual(wb.dispatch('turn.view', {'execution': second_settled['execution']})['batches'], [])
        self.assertEqual(wb.dispatch('attachments.batches', {'id': cid})['batches'][0]['state'], 'consumed')

    def test_two_files_in_one_window_are_handled_by_one_turn(self):
        fake = self.gateway([saying('收到两个文件'), final('')])
        wb = Workbench(self.root)
        cid = wb.dispatch('chat.new', {'title': '批次测试'})['id']
        first_file = self.root / 'first.txt'
        first_file.write_text('alpha content for the batch', encoding='utf-8')
        second_file = self.root / 'second.txt'
        second_file.write_text('beta content for the batch', encoding='utf-8')
        first = wb.dispatch('chat.send', {'id': cid, 'text': '看这两个文件',
                                          'attachments': [{'path': str(first_file), 'type': 'file'}],
                                          'request_id': uuid.uuid4().hex})
        time.sleep(0.2)
        second = wb.dispatch('chat.send', {'id': cid, 'text': '还有第二个',
                                           'attachments': [{'path': str(second_file), 'type': 'file'}],
                                           'request_id': uuid.uuid4().hex})
        self.assertEqual(self.wait_job(wb, first['job_id'])['status'], 'complete')
        self.assertEqual(self.wait_job(wb, second['job_id'])['status'], 'complete')
        with database(self.root / 'data/social/experience.db') as db:
            runs = [dict(row) for row in db.execute('SELECT * FROM agent_runs ORDER BY created')]
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]['status'], st.COMPLETE)
        self.assertEqual(runs[1]['status'], st.SILENT)
        self.assertTrue(json.loads(runs[1]['result'])['merged'])
        self.assertEqual(len(fake.requests), 2)  # one fish_say + one final, only for the batch turn
        first_request = json.dumps(fake.requests[0], ensure_ascii=False)
        self.assertIn('alpha content for the batch', first_request)
        self.assertIn('beta content for the batch', first_request)
        batches = wb.dispatch('attachments.batches', {'id': cid})['batches']
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]['items']), 2)
        with database(self.root / 'data/qq/desktop-chat.db') as db:
            inbound = [dict(row) for row in db.execute(
                "SELECT id,segments FROM messages WHERE direction='inbound' ORDER BY id")]
        file_refs = ['desktop:' + str(row['id']) for row in inbound
                     if any(segment.get('type') == 'file'
                            for segment in json.loads(row['segments'] or '[]'))]
        self.assertEqual([item['reference'] for item in batches[0]['items']], file_refs)
        self.assertEqual(batches[0]['state'], 'consumed')
        rows = wb.dispatch('chat.snapshot', {'id': cid})['messages']
        self.assertEqual(len([row for row in rows if row['direction'] == 'outbound']), 1)


if __name__ == '__main__':
    unittest.main()
