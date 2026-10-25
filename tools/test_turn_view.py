"""07-01 backend linkage: one turn view that follows the real records."""
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

from bot_control import BotControl
from fish_agent import get_runtime, reset_runtime
from fish_agent.execution import state as st
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

    def chat(self, messages, **kwargs):
        with self.lock:
            self.requests.append(messages)
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


class TurnViewTests(unittest.TestCase):
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

    def conversation(self, wb):
        return wb.dispatch('chat.new', {'title': '视图测试'})['id']

    def wait_job(self, wb, job_id, timeout=30):
        deadline = time.time() + timeout
        row = wb.dispatch('chat.job', {'job_id': job_id})
        while row['status'] in ('queued', 'running') and time.time() < deadline:
            time.sleep(0.05)
            row = wb.dispatch('chat.job', {'job_id': job_id})
        return row

    def test_a_client_turn_view_links_trigger_trace_delivery_and_times(self):
        self.gateway([saying('一句话'), final('')])
        wb = Workbench(self.root)
        cid = self.conversation(wb)
        job = wb.dispatch('chat.send', {'id': cid, 'text': '看这一轮', 'request_id': uuid.uuid4().hex})
        settled = self.wait_job(wb, job['job_id'])
        self.assertEqual(settled['status'], 'complete')
        view = wb.dispatch('turn.view', {'execution': settled['execution']})
        self.assertEqual(view['status'], 'complete')
        self.assertEqual(view['trigger']['kind'], 'message')
        self.assertEqual(view['trigger']['source'], 'desktop')
        self.assertTrue(view['trigger']['reference'].startswith('desktop:'))
        self.assertEqual(view['trigger']['reference'], view['reference'])
        kinds = [event['kind'] for event in view['trace']['events']]
        self.assertIn('tool_start', kinds)
        self.assertIn('tool_result', kinds)
        self.assertTrue(view['delivery']['sent'])
        self.assertIsNotNone(view['delivery_times']['receipt'])
        self.assertEqual(view['delivery_times']['outcome'], 'sent')
        self.assertEqual(view['batches'], [])

    def test_a_clock_turn_view_shows_its_own_trigger(self):
        self.gateway([saying('先一句'), final(''), saying('到点了'), final('')])
        wb = Workbench(self.root)
        cid = self.conversation(wb)
        self.wait_job(wb, wb.dispatch('chat.send', {'id': cid, 'text': '先说话',
                                                    'request_id': uuid.uuid4().hex})['job_id'])
        # Move the persisted clock into the past and let the consumer fire it.
        wait = time.time() + 5
        start = self.runtime.state.get_state('auto_deadline')
        while self.runtime.state.get_state('auto_deadline') == start and time.time() < wait:
            time.sleep(0.05)
        self.runtime.state.set_state('auto_deadline', str(time.time() - 1))
        deadline = time.time() + 12
        timer = None
        while time.time() < deadline:
            with database(self.root / 'data/social/experience.db') as db:
                row = db.execute("SELECT id FROM agent_runs WHERE kind='timer' ORDER BY created DESC LIMIT 1").fetchone()
            if row:
                timer = dict(row)['id']
                break
            time.sleep(0.1)
        self.assertIsNotNone(timer, 'the clock should have started a turn')
        deadline = time.time() + 15
        view = None
        while time.time() < deadline:
            view = wb.dispatch('turn.view', {'execution': timer})
            if view['status'] not in ('queued', 'running'):
                break
            time.sleep(0.1)
        self.assertEqual(view['trigger']['kind'], 'timer')
        self.assertEqual(view['trigger']['reason'], '45分钟到了')
        self.assertTrue(view['delivery']['sent'])

    def test_a_plan_turn_view_shows_the_occurrence_it_served(self):
        from test_plan_execution import FakeTransport
        FakeTransport.sent = []
        transport = patch.object(BotControl, '_channel_transport', FakeTransport())
        transport.start()
        self.addCleanup(transport.stop)
        self.gateway([saying('提醒一句'), final('')])
        wb = Workbench(self.root)
        plan = BotControl(self.root).create({'target': 'owner', 'title': '提醒',
                                             'prompt': '提醒主人喝水', 'after_seconds': 1})
        with database(self.root / 'data/social/owner-control.db') as db:
            db.execute("UPDATE scheduled SET next_at=? WHERE id=?",
                       ('2000-01-01T00:00:00+00:00', plan['id']))
        self.runtime.plans.detect()
        submitted = self.runtime.plans.dispatch()
        self.assertEqual(len(submitted), 1)
        deadline = time.time() + 20
        view = None
        while time.time() < deadline:
            view = wb.dispatch('turn.view', {'execution': submitted[0]['execution']})
            if view['status'] not in ('queued', 'running'):
                break
            time.sleep(0.05)
        self.assertEqual(view['trigger']['kind'], 'plan')
        # The run row is written before the occurrence is closed; wait for the real
        # settled state instead of racing the closing write.
        deadline = time.time() + 20
        while time.time() < deadline and (view['plan'] or {}).get('occurrence', {}).get('state') in (
                'running', 'queued', 'due'):
            time.sleep(0.1)
            view = wb.dispatch('turn.view', {'execution': submitted[0]['execution']})
        self.assertIsNotNone(view['plan'])
        self.assertEqual(view['plan']['schedule_id'], plan['id'])
        self.assertEqual(view['plan']['occurrence']['state'], 'sent')
        self.assertTrue(view['delivery']['sent'])
        # The view reads the record the plan turn actually wrote (wake:<plan>:<slot>).
        self.assertTrue(view['reference'].startswith('wake:' + plan['id'] + ':'), view['reference'])
        self.assertNotEqual(view['trace']['events'], [])
        self.assertIsNotNone(view['delivery_times']['receipt'])


if __name__ == '__main__':
    unittest.main()
