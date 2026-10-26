"""01-01: a client submission runs in the one main execution and reads back real results.

The model is a local fake (no network); the tool host, records, delivery, statuses
and cancellation are the real implementations on a temporary root.
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
from governance import Governance
from locations import project_path
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


def final(text):
    return ChatResult(text, {}, 'stop')


class FakeGateway:
    """Scripted model turns; it can hold a request open for concurrency checks."""

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
        # A real provider reports when its first visible output arrived.
        if reply.first_output_at is None:
            from dataclasses import replace
            reply = replace(reply, first_output_at=time.time())
        return reply

    def renders(self, index=0):
        return json.dumps(self.requests[index], ensure_ascii=False)


class FakeBudget:
    """The budget itself has its own tests; here it only has to pass through."""

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


class UnifiedExecutionTests(unittest.TestCase):
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
        # The real tokenizer, from the source tree: the temporary root has no vendor copy.
        from context_budget import _tokenizer
        tokenizer = _tokenizer(str(ROOT / 'vendor/tokenizers/deepseek-v4/tokenizer.json'))
        counter = patch('desktop_context._tokens',
                        lambda root, text: len(tokenizer.encode(str(text or '')).ids))
        counter.start()
        self.addCleanup(counter.stop)

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
        return wb.dispatch('chat.new', {'title': '测试对话'})['id']

    def submit(self, wb, cid, text, request_id=None):
        args = {'id': cid, 'text': text}
        if request_id:
            args['request_id'] = request_id
        return wb.dispatch('chat.send', args)

    def job(self, wb, job_id, timeout=20):
        deadline = time.time() + timeout
        row = None
        while time.time() < deadline:
            row = wb.dispatch('chat.job', {'job_id': job_id})
            if row['status'] in ('complete', 'failed', 'cancelled'):
                return row
            time.sleep(0.03)
        raise AssertionError('job did not settle: ' + json.dumps(row, ensure_ascii=False))

    def snapshot(self, wb, cid):
        return wb.dispatch('chat.snapshot', {'id': cid})

    def runs(self):
        with database(project_path(self.root, 'data/social/experience.db')) as db:
            return [dict(row) for row in db.execute('SELECT * FROM agent_runs ORDER BY created,rowid')]

    def run_of(self, job_id):
        with database(project_path(self.root, 'data/social/experience.db')) as db:
            row = db.execute('SELECT * FROM agent_runs WHERE request=? ORDER BY created LIMIT 1',
                             (job_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ tests
    def test_client_turn_runs_in_the_one_main_execution_and_reads_back_its_result(self):
        wb = Workbench(self.root)
        cid = self.conversation(wb)
        fake = self.gateway([saying('第一句'), final('')])
        job = self.submit(wb, cid, '你好')
        self.assertEqual(job['status'], 'queued')
        settled = self.job(wb, job['job_id'])
        self.assertEqual(settled['status'], 'complete', settled)
        self.assertEqual(settled['error'], '')
        rows = self.snapshot(wb, cid)['messages']
        self.assertEqual([row['direction'] for row in rows], ['inbound', 'outbound'])
        self.assertEqual(rows[0]['status'], 'done')
        self.assertEqual(rows[1]['text'], '第一句')
        runs = self.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['status'], st.COMPLETE)
        # The client's request map points at that one execution, not a second queue.
        with database(wb.path) as db:
            stored = db.execute('SELECT execution FROM jobs WHERE id=?', (job['job_id'],)).fetchone()[0]
        self.assertEqual(stored, runs[0]['id'])
        # The turn used Fish's own persona and the client surface's sending contract.
        offered = [item['function']['name'] for item in fake.options[0].get('tools') or []]
        self.assertIn('fish_say', offered)
        self.assertIn('主人', fake.renders())
        # The five real time points are stamped: arrival, start, first output, send, receipt.
        with database(project_path(self.root, 'data/social/experience.db')) as db:
            turn = db.execute('SELECT * FROM experience_turns ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertIsNotNone(turn)
        self.assertEqual(turn['outcome'], 'sent')
        self.assertIsNotNone(turn['started'])
        self.assertIsNotNone(turn['first_output'])
        self.assertIsNotNone(turn['sending'])
        self.assertIsNotNone(turn['receipt'])

    def test_the_qq_entry_still_runs_through_the_same_loop(self):
        from fish_chat import respond
        from tool_host import bind_session
        self.gateway([saying('私聊答复'), final('')])
        session = 'qq-private:test'
        bind_session(self.root, 'qq', session, 'owner', OWNER)
        answer = respond(
            self.root, 'qq', persona={'id': 'rev-1', 'body': '你是一条鱼。'}, history=[],
            history_source='test', memory='', scope='owner', references='',
            current=[{'type': 'text', 'text': '在吗'}], session=session, cancelled=None,
            origin='test:qq', output='bubbles', tool_context='owner-private',
            emoji_state={'previewed': set(), 'tools': []})
        bubbles = json.loads(answer)['bubbles']
        self.assertEqual([bubble['text'] for bubble in bubbles], ['私聊答复'])

    def test_following_turn_preserves_the_actual_provider_prefix_and_tools(self):
        wb = Workbench(self.root)
        cid = self.conversation(wb)
        fake = self.gateway([saying('第一次回复'), final('本轮结束'),
                             saying('第二次回复'), final('第二轮结束')])
        first = self.submit(wb, cid, '记住我们刚才讨论的方案')
        self.assertEqual(self.job(wb, first['job_id'])['status'], 'complete')
        previous = fake.requests[-1]
        second = self.submit(wb, cid, '接着讨论这个方案')
        self.assertEqual(self.job(wb, second['job_id'])['status'], 'complete')
        following = fake.requests[2]
        self.assertEqual(following[:len(previous)], previous)
        self.assertEqual(following[len(previous)]['role'], 'assistant')
        self.assertEqual(following[len(previous)]['content'], '本轮结束')
        calls = [m for m in following if m.get('tool_calls')]
        self.assertTrue(calls)
        self.assertTrue(any(m.get('tool_call_id') == calls[0]['tool_calls'][0]['id']
                            for m in following))

    def test_repeated_submission_does_not_create_another_input_or_generation(self):
        wb = Workbench(self.root)
        cid = self.conversation(wb)
        fake = self.gateway([saying('答一句'), final('')])
        request_id = uuid.uuid4().hex
        first = self.submit(wb, cid, '同一句', request_id=request_id)
        second = self.submit(wb, cid, '同一句', request_id=request_id)
        self.assertEqual(first, second)
        self.job(wb, first['job_id'])
        self.assertEqual(len(self.snapshot(wb, cid)['messages']), 2)  # one inbound, one outbound
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(len(fake.requests), 2)  # one send tool + one final answer

    def test_new_message_while_thinking_waits_for_the_next_turn(self):
        wb = Workbench(self.root)
        cid = self.conversation(wb)
        fake = self.gateway([saying('第一轮'), final(''), saying('第二轮'), final('')])
        fake.gate.clear()
        first = self.submit(wb, cid, '第一句')
        self.assertTrue(fake.called.wait(10))
        second = self.submit(wb, cid, '思考时才到的第二句')
        run = self.run_of(first['job_id'])
        self.assertEqual(run['status'], 'running')
        self.assertEqual(len(json.loads(run['inputs'])), 1)  # the running turn kept its own range
        fake.gate.set()
        self.assertEqual(self.job(wb, first['job_id'])['status'], 'complete')
        self.assertEqual(self.job(wb, second['job_id'])['status'], 'complete')
        self.assertEqual([item['status'] for item in self.runs()], [st.COMPLETE, st.COMPLETE])
        # The second turn's current input is the message that arrived while busy.
        self.assertIn('思考时才到的第二句', fake.renders(2))

    def test_messages_waiting_together_are_merged_into_one_turn(self):
        wb = Workbench(self.root)
        cid = self.conversation(wb)
        fake = self.gateway([saying('第一轮'), final(''), saying('合并轮'), final('')])
        fake.gate.clear()
        first = self.submit(wb, cid, '第一句')
        self.assertTrue(fake.called.wait(10))
        second = self.submit(wb, cid, '等待中的第二句')
        third = self.submit(wb, cid, '等待中的第三句')
        fake.gate.set()
        self.assertEqual(self.job(wb, first['job_id'])['status'], 'complete')
        self.assertEqual(self.job(wb, second['job_id'])['status'], 'complete')
        self.assertEqual(self.job(wb, third['job_id'])['status'], 'complete')
        runs = self.runs()
        self.assertEqual([run['status'] for run in runs], [st.COMPLETE, st.COMPLETE, st.MERGED])
        merged_run = runs[2]
        self.assertEqual(merged_run['merged_into'], runs[1]['id'])
        # The merged turn covered both inputs, and says so in its own claimed range.
        covered = json.loads(runs[1]['inputs'])
        self.assertEqual(len(covered), 2)
        self.assertIn('等待中的第二句', fake.renders(2))
        self.assertIn('等待中的第三句', fake.renders(2))
        self.assertEqual(len([row for row in self.snapshot(wb, cid)['messages']
                              if row['direction'] == 'outbound']), 2)

    def test_cancellation_keeps_no_reply_and_reports_the_real_state(self):
        wb = Workbench(self.root)
        cid = self.conversation(wb)
        fake = self.gateway([final('晚到的正文')])
        fake.gate.clear()
        job = self.submit(wb, cid, '这一轮要取消')
        self.assertTrue(fake.called.wait(10))
        info = wb.dispatch('chat.cancel', {'id': cid})
        self.assertTrue(info['ok'], info)
        fake.gate.set()
        settled = self.job(wb, job['job_id'])
        self.assertEqual(settled['status'], 'cancelled', settled)
        rows = self.snapshot(wb, cid)['messages']
        self.assertEqual([row['direction'] for row in rows], ['inbound'])
        self.assertEqual(rows[0]['status'], 'cancelled')
        self.assertEqual(self.runs()[-1]['status'], st.CANCELLED)

    def test_model_failure_is_a_failed_job_with_the_real_reason(self):
        wb = Workbench(self.root)
        cid = self.conversation(wb)
        self.gateway([TimeoutError('测试超时')])
        job = self.submit(wb, cid, '这一轮失败')
        settled = self.job(wb, job['job_id'])
        self.assertEqual(settled['status'], 'failed', settled)
        self.assertIn('测试超时', settled['error'])
        rows = self.snapshot(wb, cid)['messages']
        self.assertEqual(rows[-1]['status'], 'failed')

    def test_silent_turn_marks_the_input_considered_without_a_fake_reply(self):
        wb = Workbench(self.root)
        cid = self.conversation(wb)
        self.gateway([final('她想了但没发')])
        job = self.submit(wb, cid, '沉默的输入')
        settled = self.job(wb, job['job_id'])
        self.assertEqual(settled['status'], 'complete', settled)
        rows = self.snapshot(wb, cid)['messages']
        self.assertEqual([row['direction'] for row in rows], ['inbound'])
        self.assertEqual(rows[0]['status'], 'considered')
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.runs()[0]['status'], st.SILENT)

    def test_a_restarted_host_leaves_no_turn_pretending_to_run(self):
        wb = Workbench(self.root)
        cid = self.conversation(wb)
        self.gateway([saying('一次正常回复'), final('')])
        job = self.submit(wb, cid, '正常一轮')
        self.assertEqual(self.job(wb, job['job_id'])['status'], 'complete')
        runtime = get_runtime(self.root)
        runtime.stop()
        with database(self.root / 'data/social/experience.db') as db:
            db.execute("UPDATE agent_runs SET status='running'")
        runtime.start()
        self.assertNotIn('running', {run['status'] for run in self.runs()})
        self.assertEqual(self.runs()[0]['status'], st.FAILED)


if __name__ == '__main__':
    unittest.main()
