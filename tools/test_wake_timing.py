"""06-01: the 45-minute clock and the 3-second follow-up are the same one wake queue.

Fake model, real tool host, temporary root. The clock is moved by writing its stored
deadline, so nothing here waits 45 minutes.
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
from fish_agent.execution import AUTO_INTERVAL_S, FOLLOWUP_DELAY_S, state as st
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


def wanting(*calls):
    return ChatResult('', {}, 'tool_calls', tuple(calls))


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


class WakeTimingTests(unittest.TestCase):
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
        # The background host owns the runtime; this test host stands in for it.
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

    def conversation(self):
        wb = Workbench(self.root)
        return wb.dispatch('chat.new', {'title': '时钟测试'})['id'], wb

    def submit(self, wb, cid, text):
        return wb.dispatch('chat.send', {'id': cid, 'text': text, 'request_id': uuid.uuid4().hex})

    def wait_job(self, wb, job_id, timeout=25):
        deadline = time.time() + timeout
        row = wb.dispatch('chat.job', {'job_id': job_id})
        while row['status'] in ('queued', 'running') and time.time() < deadline:
            time.sleep(0.03)
            row = wb.dispatch('chat.job', {'job_id': job_id})
        return row

    def wait_status(self, execution, timeout=25):
        deadline = time.time() + timeout
        row = self.runtime.execution.status(execution)
        while row and row['status'] in ('queued', 'running') and time.time() < deadline:
            time.sleep(0.03)
            row = self.runtime.execution.status(execution)
        return row

    def state_value(self, key):
        return self.runtime.state.get_state(key)

    def runs(self):
        with database(self.root / 'data/social/experience.db') as db:
            return [dict(row) for row in db.execute('SELECT * FROM agent_runs ORDER BY created,rowid')]

    def outbound(self, cid):
        from desktop_chat import DesktopChat
        rows = DesktopChat(self.root, 'qq', None).snapshot(cid)['messages']
        return [row['text'] for row in rows if row['direction'] == 'outbound']

    # ------------------------------------------------------------------ tests
    def test_a_finished_turn_restarts_the_45_minute_clock(self):
        self.gateway([saying('一句'), final('')])
        cid, wb = self.conversation()
        job = self.submit(wb, cid, '你好')
        self.assertEqual(self.wait_job(wb, job['job_id'])['status'], 'complete')
        deadline = float(self.state_value('auto_deadline'))
        self.assertAlmostEqual(deadline, time.time() + AUTO_INTERVAL_S, delta=20)

    def test_a_due_clock_starts_one_turn_without_any_new_message(self):
        self.gateway([saying('一句'), final(''), saying('到点想了一句'), final('')])
        cid, wb = self.conversation()
        start_deadline = self.state_value('auto_deadline')
        job = self.submit(wb, cid, '先聊一句')
        self.assertEqual(self.wait_job(wb, job['job_id'])['status'], 'complete')
        # The turn's own finally re-arms the clock; wait for that, then move it back.
        wait = time.time() + 5
        while self.state_value('auto_deadline') == start_deadline and time.time() < wait:
            time.sleep(0.05)
        before = len(self.runs())
        self.runtime.state.set_state('auto_deadline', str(time.time() - 1))
        deadline = time.time() + 10
        while len(self.runs()) == before and time.time() < deadline:
            time.sleep(0.05)
        runs = self.runs()
        self.assertEqual(len(runs), before + 1)
        timer = runs[-1]
        self.assertEqual(timer['kind'], 'timer')
        settled = self.wait_status(timer['id'])
        self.assertEqual(settled['status'], st.COMPLETE, settled)
        self.assertEqual(self.outbound(cid)[-1], '到点想了一句')
        wait = time.time() + 5
        while not self.state_value('auto_deadline') and time.time() < wait:
            time.sleep(0.05)
        self.assertAlmostEqual(float(self.state_value('auto_deadline')), time.time() + AUTO_INTERVAL_S, delta=20)

    def test_the_followup_threshold_duplicate_and_limit_are_program_checks(self):
        execution = self.runtime.execution
        with self.assertRaises(ValueError):
            execution.request_followup(0.5, '不够感兴趣', 'call-low')
        first = execution.request_followup(0.9, '有意思', 'call-a')
        self.assertTrue(first['accepted'])
        self.assertEqual(first['consecutive'], 1)
        self.assertEqual(first['delay_s'], FOLLOWUP_DELAY_S)
        duplicate = execution.request_followup(0.9, '有意思', 'call-a')
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['consecutive'], 1)
        self.assertEqual(execution.request_followup(0.95, '还想看', 'call-b')['consecutive'], 2)
        self.assertEqual(execution.request_followup(1, '继续', 'call-c')['consecutive'], 3)
        with self.assertRaises(ValueError):
            execution.request_followup(0.9, '第四次', 'call-d')

    def test_an_external_message_clears_the_consecutive_limit(self):
        self.gateway([saying('一句'), final('')])
        execution = self.runtime.execution
        for index in range(3):
            execution.request_followup(0.9, 'topic%d' % index, 'call-%d' % index)
        self.assertEqual(self.state_value('followup_count'), '3')
        cid, wb = self.conversation()
        job = self.submit(wb, cid, '新消息')
        self.assertEqual(self.wait_job(wb, job['job_id'])['status'], 'complete')
        self.assertEqual(self.state_value('followup_count'), '0')
        # The limit is lifted again for a new call.
        self.assertEqual(execution.request_followup(0.9, 'again', 'call-z')['consecutive'], 1)

    def test_the_model_followup_wakes_three_seconds_after_the_turn_and_says_its_line(self):
        self.gateway([
            wanting(tool_call('fish_say', {'texts': ['第一句']}, 'call-say'),
                    tool_call('fish_think_again', {'interest': 0.9, 'topic': '这个话题还想再看一眼'}, 'call-think')),
            final(''),
            saying('三秒后又想了一句'), final(''),
        ])
        cid, wb = self.conversation()
        job = self.submit(wb, cid, '说个有意思的')
        settled = self.wait_job(wb, job['job_id'])
        self.assertEqual(settled['status'], 'complete', settled)
        self.assertEqual(self.outbound(cid)[0], '第一句')
        self.assertEqual(self.state_value('followup_count'), '1')
        wait = time.time() + 5
        while not self.state_value('followup_due') and time.time() < wait:
            time.sleep(0.05)
        due = float(self.state_value('followup_due'))
        self.assertAlmostEqual(due, time.time() + FOLLOWUP_DELAY_S, delta=20)
        deadline = time.time() + 12
        runs = self.runs()
        while len(runs) < 2 and time.time() < deadline:
            time.sleep(0.1)
            runs = self.runs()
        self.assertGreaterEqual(len(runs), 2)
        followup = [run for run in runs if run['kind'] == 'followup']
        self.assertTrue(followup)
        settled = self.wait_status(followup[0]['id'])
        self.assertEqual(settled['status'], st.COMPLETE, settled)
        self.assertEqual(self.outbound(cid)[-1], '三秒后又想了一句')

    def test_a_running_turn_holds_the_clock_so_it_does_not_double_think(self):
        state = self.runtime.state
        run_id = state.create_run(kind='message', source='desktop', conversation='probe')
        with database(state.path) as db:
            db.execute("UPDATE agent_runs SET status='running' WHERE id=?", (run_id,))
        before = str(time.time() - 5)
        state.set_state('auto_deadline', before)
        time.sleep(1.5)
        self.assertEqual([run for run in self.runs() if run['kind'] == 'timer'], [])
        self.assertEqual(state.get_state('auto_deadline'), before)


if __name__ == '__main__':
    unittest.main()
