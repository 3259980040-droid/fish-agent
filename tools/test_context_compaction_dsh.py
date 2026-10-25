"""01-04: long conversations compress with DSH's algorithm and continue.

Everything here is deterministic and local: a temporary root, a scripted model, the
real record stores and the real compression transaction. No network, no QQ, and the
production harness root is never touched.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]

import context_budget
from context_budget import ContextBudget, policy, resolve_spec
from experience import ExperienceTurn
from fish_agent.experience import compaction
from model_gateway import ChatResult
from qq_store import QQStore
from storage import database

OWNER = '10000'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class CharTokenizer:
    """One character is one token: deterministic, and still a tokenizer shape."""

    class _Encoded:
        def __init__(self, ids):
            self.ids = ids

    def encode(self, text, add_special_tokens=False):
        return self._Encoded(list(str(text or '')))


class CharBudget:
    """Stand-in for ContextBudget with fixed, readable token numbers."""

    window = 100000
    ratio = .08

    def __init__(self, root, gateway, **kwargs):
        self.begun, self.finished = [], []

    def ceiling(self, output_reserve):
        return 8000

    def tokens(self, value):
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        return len(value)

    def message_tokens(self, message):
        return 8 + self.tokens(message.get('content') or '')

    def fit(self, messages, tools, output_reserve):
        total = sum(self.message_tokens(message) for message in messages)
        return {'estimated_input_tokens': total, 'input_ceiling': 8000, 'covered': [], 'trimmed': [],
                'pressure_tokens': total, 'threshold_tokens': 6400, 'retain_tokens': 1280,
                'layers_before': {'0': total}, 'compaction_auto': True}

    def begin(self, session, origin, report):
        self.begun.append((session, origin))
        return 'ctx-test-' + str(len(self.begun))

    def finish(self, request_id, usage=None, error=''):
        self.finished.append((request_id, usage or {}, str(error)))


class FakeGateway:
    """A scripted provider that can look at the ledger while it is being called."""

    base = 'https://fake.invalid'
    model = 'fake-model'

    def __init__(self, replies, ledger=None):
        self.replies = list(replies)
        self.requests = []
        self.ledger = ledger
        self.state_at_call = None
        self.checkpoints_at_call = None

    def chat(self, messages, **kwargs):
        self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        if self.ledger is not None:
            with database(self.ledger) as db:
                row = db.execute('SELECT state FROM compactions ORDER BY updated DESC LIMIT 1').fetchone()
                self.state_at_call = row['state'] if row else None
                self.checkpoints_at_call = db.execute(
                    'SELECT COUNT(*) FROM context_checkpoints').fetchone()[0]
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return ChatResult(reply, {'prompt_tokens': 11, 'completion_tokens': 5}, 'stop')


class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'harness').mkdir(parents=True, exist_ok=True)
        write_json(self.root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'name': 'Fish', 'model': 'fake-model',
                        'base_url': 'https://fake.invalid', 'apiKeyEnv': 'FISH_TEST_KEY'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.root / 'harness/context-settings.json', {
            'context_window_ratio': 0.08, 'compact_trigger_ratio': 0.8,
            'retain_ratio': 0.16, 'context_compaction': 'auto'})
        write_json(self.root / 'data/social/model-metadata.json', {
            'https://fake.invalid|fake-model': {
                'id': 'fake-model', 'context_window': 100000,
                'fetched_at': time.time(), 'source': 'https://fake.invalid/models'}})
        counter = patch('desktop_context._tokens', lambda root, text: len(str(text or '')))
        counter.start()
        self.addCleanup(counter.stop)
        self.store = QQStore(self.root)
        self.ledger = self.root / 'data/social/experience.db'
        with database(self.ledger) as db:
            compaction._schema(db)

    # ---------------------------------------------------------------- helpers
    def conversation(self, place='private:' + OWNER):
        return self.store.get_or_create_conversation(place, 'qq')['id']

    def record(self, cid, text, place='private:' + OWNER):
        import uuid
        return self.store.record_message(cid, place, OWNER, 'inbound', 'chan-' + uuid.uuid4().hex,
                                         None, [], text, 'processed')

    def fill(self, cid, count, size=100, place='private:' + OWNER):
        return [self.record(cid, (chr(97 + index % 26) * size), place) for index in range(count)]

    def turn(self, cid, message=None):
        turn = ExperienceTurn(self.root, self.store.db_path, cid, message, channel='qq')
        turn.session = 'qq-private:' + str(cid)
        turn.prepare()
        return turn

    def queued(self, cid):
        with database(self.ledger) as db:
            row = db.execute('SELECT * FROM compactions WHERE source=? AND conversation=?',
                             ('qq.db', str(cid))).fetchone()
        return dict(row) if row else None

    def report(self, pressure, threshold=6400, retain=400):
        return {'pressure_tokens': pressure, 'threshold_tokens': threshold,
                'retain_tokens': retain, 'compaction_auto': policy(self.root)['auto'], 'covered': []}

    def gateway(self, replies):
        gateway = FakeGateway(replies, ledger=self.ledger)
        start = patch('model_gateway.ModelGateway', side_effect=lambda root, role: gateway)
        start.start()
        self.addCleanup(start.stop)
        budget = CharBudget(self.root, gateway)
        budget_patch = patch('context_budget.ContextBudget', side_effect=lambda root, gw, **kw: budget)
        budget_patch.start()
        self.addCleanup(budget_patch.stop)
        self.budget = budget
        return gateway

    def checkpoints(self):
        with database(self.ledger) as db:
            return [dict(row) for row in db.execute('SELECT * FROM context_checkpoints ORDER BY id')]

    def catalog(self):
        with database(self.ledger) as db:
            return [dict(row) for row in db.execute('SELECT * FROM chat_catalog ORDER BY id')]

    def set_setting(self, **values):
        path = self.root / 'harness/context-settings.json'
        current = json.loads(path.read_text(encoding='utf-8'))
        current.update(values)
        write_json(path, current)

    # ------------------------------------------------- one policy, one report
    def test_one_policy_resolves_trigger_and_retention_from_the_real_window(self):
        values = policy(self.root)
        spec = resolve_spec(values, 100000, 8192)
        self.assertEqual(spec['input_ceiling'], 8000)      # 8% ceiling stays the input cap
        self.assertEqual(spec['threshold_tokens'], 6400)   # 80% of that ceiling
        self.assertEqual(spec['retain_tokens'], 1280)      # 16% recent verbatim window
        self.assertTrue(spec['auto'])

    def test_policy_rejects_a_retained_window_that_would_not_free_space(self):
        self.set_setting(retain_ratio=0.9)
        with self.assertRaises(ValueError) as caught:
            policy(self.root)
        self.assertIn('保留', str(caught.exception))
        self.set_setting(retain_ratio=0.16, context_window_ratio=1.5)
        with self.assertRaises(ValueError):
            policy(self.root)

    def test_request_report_carries_the_same_numbers_and_the_measured_pressure(self):
        budget = ContextBudget(self.root, gateway=type('Gateway', (), {
            'base': 'https://fake.invalid', 'model': 'deepseek-v4-test', 'key': 'k'}),
            metadata={'id': 'deepseek-v4-test', 'context_window': 100000, 'source': 'test'},
            tokenizer=CharTokenizer())
        messages = [{'role': 'system', 'content': 'x' * 1000},
                    {'role': 'user', 'content': 'y' * 5000, '_fish_layer': 1, '_fish_refs': ['qq:1']}]
        report = budget.fit(messages, [], 8192)
        self.assertEqual(report['input_ceiling'], 8000)
        self.assertEqual(report['threshold_tokens'], 6400)
        self.assertEqual(report['retain_tokens'], 1280)
        self.assertTrue(report['compaction_auto'])
        self.assertEqual(report['pressure_tokens'], sum(report['layers_before'].values()))
        self.assertGreaterEqual(report['pressure_tokens'], report['estimated_input_tokens'])
        self.assertFalse(report['ceiling_is_target'])

    # ------------------------------------------------------------ the trigger
    def test_pressure_at_the_configured_ratio_queues_one_complete_older_range(self):
        cid = self.conversation()
        ids = self.fill(cid, 20)
        turn = self.turn(cid, ids[-1])
        self.assertEqual(len(turn.surface), 19)  # the current input is not history
        turn.requested(self.report(pressure=6401))
        row = self.queued(cid)
        self.assertIsNotNone(row)
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(row['through'], 17)  # the two newest records stay verbatim
        self.assertEqual(row['pressure_tokens'], 6401)
        self.assertEqual(row['threshold_tokens'], 6400)
        self.assertEqual(row['reason'], 'pressure')
        # Queuing is model-free: no checkpoint and no summary request exists yet.
        self.assertEqual(self.checkpoints(), [])
        self.assertEqual(self.catalog(), [])

    def test_a_real_budget_report_drives_the_same_trigger(self):
        cid = self.conversation()
        ids = self.fill(cid, 30)
        turn = self.turn(cid, ids[-1])
        history, _source = turn.context.history(cid, ids[-1], None, None, None)
        budget = ContextBudget(self.root, gateway=type('Gateway', (), {
            'base': 'https://fake.invalid', 'model': 'deepseek-v4-test', 'key': 'k'}),
            metadata={'id': 'deepseek-v4-test', 'context_window': 100000, 'source': 'test'},
            tokenizer=CharTokenizer())
        report = budget.fit(list(history), [], 8192)
        self.assertGreater(report['pressure_tokens'], report['threshold_tokens'])
        turn.requested(report)  # no injected counter: the surface carries its own prices
        row = self.queued(cid)
        self.assertIsNotNone(row)
        self.assertEqual(row['threshold_tokens'], report['threshold_tokens'])
        self.assertEqual(row['retain_tokens'], report['retain_tokens'])
        self.assertGreater(row['through'], 0)

    def test_pressure_below_the_trigger_and_the_off_switch_queue_nothing(self):
        cid = self.conversation()
        ids = self.fill(cid, 10)
        turn = self.turn(cid, ids[-1])
        turn.requested(self.report(pressure=6399))
        self.assertIsNone(self.queued(cid))
        self.set_setting(context_compaction='off')
        turn = self.turn(cid, ids[-1])
        turn.requested(self.report(pressure=9000))
        self.assertIsNone(self.queued(cid))
        outcome = compaction.request(self.root, source=self.store.db_path,
                                     conversation=cid, through=ids[-1], pressure=9000, threshold=6400)
        self.assertFalse(outcome['queued'])
        self.assertIn('关闭', outcome['reason'])

    def test_selection_keeps_complete_bundles_and_complete_tool_pairs(self):
        surface = [
            {'layer': 1, 'source': 'qq.db', 'conversation': 'c', 'id': 1, 'bundle': 'b1',
             'role': 'user', 'text': 'a' * 100},
            {'layer': 1, 'source': 'qq.db', 'conversation': 'c', 'id': 2, 'bundle': 'b1',
             'role': 'assistant', 'tool_calls': [{'id': 'call-1'}], 'text': 'b' * 40},
            {'layer': 1, 'source': 'qq.db', 'conversation': 'c', 'id': 3, 'bundle': 'b1',
             'role': 'tool', 'tool_call_id': 'call-1', 'text': 'c' * 40},
            {'layer': 2, 'source': 'qq.db', 'conversation': 'other', 'id': 9, 'bundle': 'x',
             'role': 'user', 'text': 'cross-place material is not compacted into this place'},
            {'layer': 1, 'source': 'qq.db', 'conversation': 'c', 'id': 4, 'bundle': 'b2',
             'role': 'user', 'text': 'd' * 100},
            {'layer': 1, 'source': 'qq.db', 'conversation': 'c', 'id': 5, 'bundle': 'b2',
             'role': 'user', 'text': 'e' * 100},
        ]
        region = compaction.select_region(surface, lambda text: len(text), 100)
        # The newest bundle b2 stays whole, and the assistant/tool pair stays whole.
        self.assertEqual([node['id'] for node in region['nodes']], [1, 2, 3])
        self.assertEqual(region['through'], 3)
        self.assertIsNone(compaction.select_region(surface, lambda text: len(text), 10 ** 6))

    def test_immediate_compression_keeps_a_complete_pair_and_the_newest_record(self):
        surface = [
            {'layer': 1, 'source': 'qq.db', 'conversation': 'c', 'id': 1, 'bundle': 'b1',
             'role': 'user', 'text': 'a' * 100},
            {'layer': 1, 'source': 'qq.db', 'conversation': 'c', 'id': 2, 'bundle': 'b1',
             'role': 'assistant', 'tool_calls': [{'id': 'call-1'}], 'text': 'b' * 40},
            {'layer': 1, 'source': 'qq.db', 'conversation': 'c', 'id': 3, 'bundle': 'b1',
             'role': 'tool', 'tool_call_id': 'call-1', 'text': 'c' * 40},
            {'layer': 1, 'source': 'qq.db', 'conversation': 'c', 'id': 4, 'bundle': 'b3',
             'role': 'user', 'text': 'f' * 100},
        ]
        region = compaction.select_region(surface, lambda text: len(text), 0)
        self.assertEqual([node['id'] for node in region['nodes']], [1, 2, 3])

    # --------------------------------------------------- commit and merge
    def test_checkpoint_merges_the_previous_summary_and_publishes_one_commit(self):
        cid = self.conversation()
        ids = self.fill(cid, 20)
        self.turn(cid, ids[-1]).requested(self.report(pressure=6401))
        gateway = self.gateway(['第一份合并摘要', '第二份合并摘要'])
        first = compaction.drain(self.root)
        self.assertTrue(first.get('committed'), first)
        self.assertEqual(gateway.state_at_call, 'running')  # start marker before the model call
        self.assertEqual(gateway.checkpoints_at_call, 0)     # nothing published before success
        rows = self.checkpoints()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['context_key'], 'qq.db:' + str(cid))
        self.assertIsNone(rows[0]['previous_id'])
        ranges = json.loads(rows[0]['source_ranges'])
        self.assertEqual((ranges[0]['from'], ranges[0]['to']), (1, 17))
        self.assertEqual(self.catalog()[0]['from_message_id'], 1)
        self.assertEqual(self.catalog()[0]['to_message_id'], 17)
        row = self.queued(cid)
        self.assertEqual((row['state'], row['processed'], row['attempts']), ('done', 17, 0))
        self.assertEqual(row['checkpoint_id'], rows[0]['id'])
        # The request carried Fish's own instruction and the chosen range, not DSH's
        # coding persona or default authentication.
        rendered = json.dumps(gateway.requests[0], ensure_ascii=False)
        self.assertIn('同一个Fish', rendered)
        self.assertNotIn('AI coding assistant', rendered)
        self.assertEqual(self.budget.begun[0][1], 'context-compaction')
        self.assertEqual(self.budget.finished[0][1], {'prompt_tokens': 11, 'completion_tokens': 5})

        # A later range merges the previous checkpoint into one consolidated summary.
        compaction.request(self.root, source=self.store.db_path, conversation=cid, through=19)
        second = compaction.drain(self.root)
        self.assertTrue(second.get('committed'), second)
        rows = self.checkpoints()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['previous_id'], rows[0]['id'])
        self.assertIn('第一份合并摘要', json.dumps(gateway.requests[1], ensure_ascii=False))
        self.assertEqual(json.loads(rows[1]['source_ranges'])[0]['from'], 18)
        material = compaction.summary_material(self.root, source='qq.db', conversation=cid)
        self.assertEqual((material['kind'], material['through']), ('checkpoint', 19))

    def test_a_failed_summary_does_not_advance_progress_or_publish_a_checkpoint(self):
        cid = self.conversation()
        ids = self.fill(cid, 12)
        self.turn(cid, ids[-1]).requested(self.report(pressure=7000))
        gateway = self.gateway([RuntimeError('测试摘要失败')])
        first = compaction.drain(self.root)
        self.assertTrue(first.get('failed'), first)
        self.assertIn('测试摘要失败', first['error'])
        self.assertEqual(self.checkpoints(), [])
        self.assertEqual(self.catalog(), [])
        row = self.queued(cid)
        self.assertEqual((row['state'], row['processed'], row['attempts']), ('failed', 0, 1))
        self.assertEqual(len(gateway.requests), 1)
        # A later trigger retries; progress still does not move without a committed summary.
        for attempt in (2, 3):
            compaction.request(self.root, source=self.store.db_path, conversation=cid,
                               through=row['through'])
            self.assertTrue(compaction.drain(self.root).get('failed'))
            row = self.queued(cid)
            self.assertEqual((row['state'], row['processed'], row['attempts']), ('failed', 0, attempt))
        outcome = compaction.request(self.root, source=self.store.db_path, conversation=cid,
                                     through=row['through'])
        self.assertFalse(outcome['queued'])
        self.assertIn('连续整理失败', outcome['reason'])
        self.assertEqual(len(gateway.requests), 3)  # no automatic paid retry loop beyond the cap
        self.assertEqual(self.checkpoints(), [])

    def test_a_failed_commit_lands_no_checkpoint_and_no_progress(self):
        first_cid = self.conversation('private:other')
        first_ids = self.fill(first_cid, 6, place='private:other')
        self.turn(first_cid, first_ids[-1]).requested(self.report(pressure=9000, retain=1))
        self.gateway(['已提交的摘要'])
        self.assertTrue(compaction.drain(self.root).get('committed'))
        self.assertEqual(len(self.checkpoints()), 1)
        cid = self.conversation()
        ids = self.fill(cid, 8)
        self.turn(cid, ids[-1]).requested(self.report(pressure=9000, retain=1))
        with database(self.ledger) as db:
            db.execute("CREATE TRIGGER refuse_catalog BEFORE INSERT ON chat_catalog "
                       "BEGIN SELECT RAISE(ABORT,'测试拒绝目录写入'); END")
            db.commit()
        failed = compaction.drain(self.root, gateway=FakeGateway(['不会提交的摘要'], ledger=self.ledger))
        self.assertTrue(failed.get('failed'), failed)
        self.assertIn('测试拒绝目录写入', failed['error'])
        self.assertEqual(len(self.checkpoints()), 1)  # the earlier commit is untouched
        row = self.queued(cid)
        self.assertEqual((row['state'], row['processed']), ('failed', 0))

    # ------------------------------------------------------------ the switch
    def test_the_off_switch_stops_every_automatic_entry_but_not_an_immediate_request(self):
        cid = self.conversation()
        ids = self.fill(cid, 12)
        self.set_setting(context_compaction='off')
        self.turn(cid, ids[-1]).requested(self.report(pressure=9000))
        self.assertIsNone(self.queued(cid))
        gateway = self.gateway(['手动立即压缩的摘要'])
        skipped = compaction.drain(self.root)
        self.assertTrue(skipped.get('skipped'), skipped)
        self.assertEqual(gateway.requests, [])  # the automatic drain made no paid call
        with database(self.ledger) as db:  # a job queued before the switch still waits
            db.execute('INSERT INTO compactions(source,conversation,through,state,updated) '
                       "VALUES('qq.db',?,'0','pending',?)", (str(cid), time.time()))
            db.commit()
        self.assertTrue(compaction.drain(self.root).get('skipped'))
        self.assertEqual(gateway.requests, [])
        manual = compaction.compact_now(self.root, source=self.store.db_path, conversation=cid)
        self.assertTrue(manual.get('committed'), manual)  # one implementation, same transaction
        self.assertEqual(len(gateway.requests), 1)

    # ------------------------------------------------------- legacy summary
    def test_the_old_summary_is_read_once_for_continuity_and_never_written_again(self):
        cid = self.conversation()
        ids = self.fill(cid, 5)
        with database(self.store.db_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS chat_summaries('
                       'conversation TEXT PRIMARY KEY, through INTEGER NOT NULL, summary TEXT NOT NULL)')
            db.execute('INSERT INTO chat_summaries VALUES(?,?,?)', (str(cid), 3, '旧的续聊摘要'))
        material = compaction.summary_material(self.root, source='qq.db', conversation=cid)
        self.assertEqual((material['kind'], material['through']), ('legacy-chat_summaries', 3))
        turn = self.turn(cid, ids[-1])
        history, source = turn.context.history(cid, ids[-1], None, None, None)
        self.assertEqual(source['continuation'], 'legacy-chat_summaries')
        self.assertIn('旧的续聊摘要', history[0]['content'])
        rendered = json.dumps([item['content'] for item in history], ensure_ascii=False)
        self.assertNotIn('正文：' + 'a' * 100, rendered)   # covered by the old summary, not re-sent
        self.assertIn('正文：' + 'd' * 100, rendered)      # newer originals continue verbatim
        gateway = self.gateway(['合并旧摘要的新检查点'])
        compaction.request(self.root, source=self.store.db_path, conversation=cid, through=ids[-1])
        self.assertTrue(compaction.drain(self.root).get('committed'))
        self.assertIn('旧的续聊摘要', json.dumps(gateway.requests[0], ensure_ascii=False))
        with database(self.store.db_path) as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM chat_summaries')]
        self.assertEqual([(row['through'], row['summary']) for row in rows], [(3, '旧的续聊摘要')])
        material = compaction.summary_material(self.root, source='qq.db', conversation=cid)
        self.assertEqual(material['kind'], 'checkpoint')

    def test_a_fresh_store_never_gets_a_chat_summaries_table(self):
        cid = self.conversation()
        ids = self.fill(cid, 6)
        self.turn(cid, ids[-1]).requested(self.report(pressure=9000, retain=1))
        self.gateway(['新检查点'])
        self.assertTrue(compaction.drain(self.root).get('committed'))
        with database(self.store.db_path) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_summaries'").fetchone())

    # ------------------------------------------------------------- retirement
    def test_the_old_parallel_summary_module_is_retired(self):
        self.assertFalse((ROOT / 'harness/context_compaction.py').exists())
        with self.assertRaises(ImportError):
            __import__('context_compaction')
        source = (ROOT / 'harness/tool_host.py').read_text(encoding='utf-8')
        self.assertNotIn('auto_enqueue', source)
        self.assertIn('fish_agent.experience.compaction', source)

    # ----------------------------------------------------------- client view
    def test_the_client_can_read_the_effective_budget_and_the_recent_range(self):
        cid = self.conversation()
        ids = self.fill(cid, 12)
        self.turn(cid, ids[-1]).requested(self.report(pressure=7000))
        self.gateway(['客户端能读到的摘要'])
        self.assertTrue(compaction.drain(self.root).get('committed'))
        status = compaction.status(self.root, 'qq.db:' + str(cid))
        self.assertEqual(status['effective']['window'], 100000)
        self.assertEqual(status['effective']['input_ceiling'], 8000)
        self.assertEqual(status['effective']['threshold_tokens'], 6400)
        self.assertEqual(status['effective']['retain_tokens'], 1280)
        self.assertTrue(status['effective']['auto'])
        self.assertEqual(status['latest']['state'], 'done')
        self.assertEqual(status['latest']['processed'], status['latest']['through'])
        self.assertEqual(status['latest_checkpoint']['ranges'][0]['to'], status['latest']['processed'])
        self.assertEqual((status['checkpoints'], status['catalog']), (1, 1))
        usage = context_budget.snapshot(self.root)
        self.assertEqual(usage['effective']['threshold_tokens'], 6400)

    def test_the_workbench_reads_the_effective_budget_and_the_recent_result(self):
        from governance import Governance
        from social_context import SocialContext
        from workbench import Workbench

        write_json(self.root / 'channels/fish_qq/config.json', {'allowlist': {'owner_id': OWNER}})
        SocialContext(self.root).ensure_default('qq')
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        cid = self.conversation()
        ids = self.fill(cid, 12)
        self.turn(cid, ids[-1]).requested(self.report(pressure=7000))
        self.gateway(['页面能读到的摘要'])
        self.assertTrue(compaction.drain(self.root).get('committed'))
        workbench = Workbench(self.root)
        snapshot = workbench.dispatch('chat.snapshot', {'id': workbench.dispatch('chat.new', {'title': '测试'})['id']})
        self.assertIn('context_compaction', snapshot)
        self.assertEqual(snapshot['context_compaction']['effective']['threshold_tokens'], 6400)
        status = workbench.dispatch('context.status', {})
        self.assertEqual(status['effective_budget']['input_ceiling'], 8000)
        self.assertEqual(status['compaction']['latest']['state'], 'done')
        self.assertEqual(status['compaction']['latest']['context_key'], 'qq.db:' + str(cid))
        self.assertEqual(status['compaction']['latest_checkpoint']['ranges'][0]['to'],
                         status['compaction']['latest']['processed'])


    def test_the_workbench_exposes_the_immediate_compression_entry(self):
        from workbench import Workbench
        with patch("fish_agent.experience.compaction.compact_now",
                   return_value={"committed": True, "checkpoint_id": 9, "through": 12}) as called:
            result = Workbench(self.root).dispatch(
                "context.compact", {"source": "desktop-chat.db", "conversation": "conv-1"})
        self.assertTrue(result["committed"])
        self.assertEqual(called.call_args.kwargs["source"], "desktop-chat.db")
        self.assertEqual(called.call_args.kwargs["conversation"], "conv-1")

if __name__ == '__main__':
    unittest.main()
