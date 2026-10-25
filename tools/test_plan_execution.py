"""06-02: due plans and manual runs enter the one main execution; results land on the plan.

Fake model, fake channel transport, real plan/occurrence stores and tool host.
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

from bot_control import BotControl
from fish_agent import get_runtime, reset_runtime
from fish_agent.execution import state as st
from governance import Governance
from model_gateway import ChatOptions, ChatResult
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


class FakeTransport:
    sent = []

    @classmethod
    def __call__(cls, target, text, request_id, segments=None):
        cls.sent.append({'target': target, 'text': text, 'request_id': request_id})
        return {'ok': True, 'payload': {'message_id': 'fake-send-' + str(len(cls.sent))}}


class PlanExecutionTests(unittest.TestCase):
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
        FakeTransport.sent = []
        transport = patch.object(BotControl, '_channel_transport', FakeTransport())
        transport.start()
        self.addCleanup(transport.stop)
        self.runtime = get_runtime(self.root)
        self.runtime.start()
        self.host.runtime = self.runtime
        self.control = BotControl(self.root)

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

    def plan(self, *, purpose='reminder', after=1, prompt='提醒我喝水'):
        return self.control.create({'target': 'owner', 'title': '测试计划', 'prompt': prompt,
                                    'after_seconds': after, 'purpose': purpose})

    def due_slot(self, plan):
        """Make the plan due now and let the time wheel detect it."""
        with database(self.root / 'data/social/owner-control.db') as db:
            db.execute("UPDATE scheduled SET next_at=? WHERE id=?",
                       ('2000-01-01T00:00:00+00:00', plan['id']))
        self.control.plans.detect()
        pending = [item for item in self.control.plans.pending() if item['id'] == plan['id']]
        self.assertTrue(pending, 'the occurrence should be pending')
        return pending[0]['slot']

    def wait_run(self, execution, timeout=25):
        deadline = time.time() + timeout
        row = self.runtime.execution.status(execution)
        while row and row['status'] in ('queued', 'running') and time.time() < deadline:
            time.sleep(0.03)
            row = self.runtime.execution.status(execution)
        return row

    def wait_occurrence(self, plan, slot, timeout=15):
        deadline = time.time() + timeout
        row = self.occurrence(plan, slot)
        while row['state'] in ('due', 'catchup', 'running', 'queued') and time.time() < deadline:
            time.sleep(0.05)
            row = self.occurrence(plan, slot)
        return row

    def occurrence(self, plan, slot):
        with database(self.root / 'data/social/owner-control.db') as db:
            return dict(db.execute('SELECT * FROM occurrences WHERE schedule_id=? AND slot=?',
                                   (plan['id'], slot)).fetchone())

    def test_a_due_reminder_runs_in_the_main_execution_and_its_receipt_lands_on_the_plan(self):
        self.gateway([saying('该喝水了'), final('')])
        plan = self.plan()
        slot = self.due_slot(plan)
        submitted = self.runtime.plans.dispatch()
        self.assertEqual(len(submitted), 1, submitted)
        execution = submitted[0]['execution']
        settled = self.wait_run(execution)
        self.assertEqual(settled['status'], st.COMPLETE, settled)
        occurrence = self.wait_occurrence(plan, slot)
        self.assertEqual(occurrence['state'], 'sent', occurrence)
        self.assertTrue(occurrence['receipt_id'].startswith('sched:' + plan['id']))
        # The page can follow the instance to the one execution that served it.
        self.assertEqual(occurrence['execution'], execution)
        self.assertEqual([item['text'] for item in FakeTransport.sent], ['该喝水了'])
        self.assertEqual(FakeTransport.sent[0]['target'], 'private:' + OWNER)
        # The turn is recorded as a plan turn with the delivery receipt it produced.
        with database(self.root / 'data/social/experience.db') as db:
            run = dict(db.execute('SELECT * FROM agent_runs WHERE id=?', (execution,)).fetchone())
        self.assertEqual(run['kind'], 'plan')
        self.assertEqual(json.loads(run['result'])['delivery']['state'], 'SUCCEEDED')

    def test_a_paused_plan_runs_one_manual_instance_and_stays_paused(self):
        self.gateway([saying('手动跑一次'), final('')])
        plan = self.plan()
        before = self.control.plans.get(plan['id'])
        self.control.plans.update({'id': plan['id'], 'state': 'paused'})
        paused = self.control.plans.get(plan['id'])
        manual = self.control.plans.run_now(plan['id'])
        self.assertEqual(manual['state'], 'manual')
        pending = [item for item in self.control.plans.pending() if item['id'] == plan['id']]
        self.assertEqual([item['state'] for item in pending], ['manual'])
        submitted = self.runtime.plans.dispatch()
        self.assertEqual(len(submitted), 1)
        settled = self.wait_run(submitted[0]['execution'])
        self.assertEqual(settled['status'], st.COMPLETE, settled)
        self.assertEqual(self.wait_occurrence(plan, manual['slot'])['state'], 'sent')
        after = self.control.plans.get(plan['id'])
        self.assertEqual(after['state'], 'paused')          # still paused
        self.assertEqual(after['next_at'], paused['next_at'])  # period untouched
        self.assertEqual(before['timing'], after['timing'])

    def test_a_reminder_cannot_be_silenced_by_choice(self):
        # No fish_say: the runner's delivery_required fallback still hands her words over.
        self.gateway([final('到点提醒：喝水')])
        plan = self.plan()
        slot = self.due_slot(plan)
        self.runtime.plans.dispatch()
        deadline = time.time() + 20
        while time.time() < deadline:
            occurrence = self.occurrence(plan, slot)
            if occurrence['state'] not in ('due', 'catchup', 'running', 'queued'):
                break
            time.sleep(0.05)
        self.assertEqual(occurrence['state'], 'sent', occurrence)
        self.assertEqual([item['text'] for item in FakeTransport.sent], ['到点提醒：喝水'])

    def test_an_autonomy_opportunity_may_do_nothing(self):
        self.gateway([final('')])
        plan = self.plan(purpose='autonomy', prompt='自己看看要不要说话')
        slot = self.due_slot(plan)
        self.runtime.plans.dispatch()
        deadline = time.time() + 20
        while time.time() < deadline:
            occurrence = self.occurrence(plan, slot)
            if occurrence['state'] not in ('due', 'catchup', 'running', 'queued'):
                break
            time.sleep(0.05)
        self.assertEqual(occurrence['state'], 'no_action', occurrence)
        self.assertEqual(FakeTransport.sent, [])

    def test_a_repeated_dispatch_does_not_run_the_same_occurrence_twice(self):
        self.gateway([saying('一次'), final('')])
        plan = self.plan()
        slot = self.due_slot(plan)
        first = self.runtime.plans.dispatch()
        second = self.runtime.plans.dispatch()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['execution'], second[0]['execution'])
        self.wait_run(first[0]['execution'])
        self.wait_occurrence(plan, slot)
        with database(self.root / 'data/social/experience.db') as db:
            runs = [dict(row) for row in db.execute("SELECT * FROM agent_runs WHERE source='plan'")]
        self.assertEqual(len(runs), 1)
        self.assertEqual(self.occurrence(plan, slot)['state'], 'sent')


if __name__ == '__main__':
    unittest.main()
