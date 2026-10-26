"""Persistent provider context, real budget/checkpoints, no network or production data."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]

import test_context_compaction_dsh as fixture
from chat_request import assemble_chat_request
from context_budget import ContextBudget
from fish_agent.experience.model_context import ModelContext
from model_gateway import ChatOptions
from storage import database


class ModelContextTests(unittest.TestCase):
    conversation = fixture.CompressionTests.conversation
    record = fixture.CompressionTests.record

    def setUp(self):
        fixture.CompressionTests.setUp(self)
        self.cid = self.conversation()
        self.gateway = fixture.FakeGateway([])
        self.budget = ContextBudget(self.root, self.gateway,
            metadata={'id': 'fake-model', 'context_window': 100000}, tokenizer=fixture.CharTokenizer())

    def prepare(self, context, number, text, *, memory='', image=None, history=None, surface=None):
        mid = self.record(self.cid, str(text))
        current = [{'type': 'text', 'text': text}]
        if image:
            current.append({'type': 'image_url', 'image_url': {'url': image}})
        source = {'current_reference': 'qq:' + str(mid), 'surface': surface or []}
        with patch('chat_request.now_line', return_value='fixed clock'):
            request = assemble_chat_request(persona='stable persona', history=history or [], memory=memory,
                expression={'prompt': ''}, references='', current=current,
                runtime_state='actual tools available', sources={'history': source})
        return context.prepare(request, route='fake|model', execution='run-' + str(number),
            original={'reference': source['current_reference'], 'source': 'qq.db',
                      'conversation': self.cid, 'id': mid, 'place': 'private:' + fixture.OWNER,
                      'time': '2026-10-26 12:%02d:00' % number})

    def seed(self):
        self.prior = self.record(self.cid, 'old source' * 50)
        history = [{'role': 'user', 'content': 'old source' * 50, '_fish_layer': 1,
                    '_fish_refs': ['qq:' + str(self.prior)]}]
        surface = [{'reference': 'qq.db:' + str(self.prior), 'source': 'qq.db',
                    'conversation': self.cid, 'id': self.prior, 'place': 'private:' + fixture.OWNER,
                    'time': '2026-10-26 11:59:00'}]
        for number in range(6):
            context = ModelContext(self.root)
            messages = self.prepare(context, number, str(number) + 'u' * 480,
                                    history=history if number == 0 else [], surface=surface)
            self.budget.fit(messages, [], 0)
            context.save(messages, self.budget)
            messages.append({'role': 'assistant', 'content': 'a' * 480, 'reasoning_content': 'reason'})
            context.save(messages, self.budget)
        return ModelContext(self.root)

    def test_restart_and_place_change_keep_protocol_and_instance_isolation(self):
        context = ModelContext(self.root)
        messages = self.prepare(context, 1, '主人第一轮', memory='第一轮检索')
        self.budget.fit(messages, [], 0)
        context.save(messages, self.budget)
        messages.extend([{'role': 'assistant', 'content': '', 'reasoning_content': 'provider reasoning',
            'tool_calls': [{'id': 'call-original', 'type': 'function',
                            'function': {'name': 'fish_shell', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call-original', 'content': '{"ok":true}'},
            {'role': 'assistant', 'content': '没有发言', 'reasoning_content': 'quiet by choice'}])
        self.budget.fit(messages, [], 0)
        context.save(messages, self.budget)
        previous = json.loads(json.dumps(messages))
        restarted = ModelContext(self.root)
        following = self.prepare(restarted, 2, '换到群里继续', memory='第二轮不同检索')
        self.budget.fit(following, [], 0)
        self.assertEqual(following[:len(previous)], previous)
        self.assertEqual(following[-1]['role'], 'user')
        self.assertTrue(any(m.get('tool_call_id') == 'call-original' for m in following))
        self.assertEqual(ModelContext(self.root / 'another-instance').retained(), [])

    def test_compaction_uses_original_checkpoint_and_preserves_recent_tail(self):
        context = self.seed()
        messages = self.prepare(context, 7, 'u' * 700)
        tail = json.loads(json.dumps(messages[-3:]))
        summary = fixture.FakeGateway(['已经讨论六轮，记住共同方案，保留后续问题。'])
        result = context.compact(messages, self.budget, [], ChatOptions('high', 100, 20), summary)
        self.assertTrue(result['committed'])
        self.assertEqual(messages[-3:], tail)
        with database(context.state.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM context_checkpoints').fetchone()[0], 1)
            self.assertGreater(db.execute('SELECT COUNT(*) FROM chat_catalog').fetchone()[0], 0)
            catalog = db.execute('SELECT * FROM chat_catalog WHERE place=? AND from_message_id<=? '
                                 'AND to_message_id>=?', ('private:' + fixture.OWNER, self.prior, self.prior)).fetchone()
            self.assertIsNotNone(catalog, '首次采用的既有原件也必须能按真实场所回查')
            self.assertEqual(catalog['start_time'], '2026-10-26 11:59:00')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM compactions WHERE state='pending'").fetchone()[0], 0)
        loaded = ModelContext(self.root)
        self.assertEqual(loaded.value['checkpoint_id'], result['checkpoint_id'])
        clean = lambda rows: [{k: v for k, v in m.items() if not k.startswith('_fish_')} for m in rows]
        self.assertEqual(clean(loaded.retained()[-3:]), clean(tail))
        with database(self.store.db_path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 8)

    def test_failed_summary_leaves_context_and_checkpoint_unchanged(self):
        context = self.seed()
        before = context.state.get_state(context.key)
        messages = context.retained()
        # The provider has billed this call, even though its empty summary cannot
        # be published. Rejection must preserve both the context and real usage.
        gateway = fixture.FakeGateway([''])
        with self.assertRaisesRegex(ValueError, '续聊摘要为空'):
            context.compact(messages, self.budget, [], ChatOptions('high', 100, 20), gateway, force=True)
        self.assertEqual(context.state.get_state(context.key), before)
        with database(context.state.path) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='context_checkpoints'").fetchone()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM context_checkpoints').fetchone()[0] if exists else 0, 0)
        with database(self.budget.path) as db:
            row = db.execute("SELECT usage,error FROM context_requests WHERE origin='context-compaction'").fetchone()
            self.assertEqual(json.loads(row['usage'])['prompt_tokens'], 11)
            self.assertIn('续聊摘要为空', row['error'])

    def test_deletion_during_first_preparation_rejects_the_writer(self):
        from fish_agent.execution.runner import execute

        def prepare():
            # The first reader has not published a versions map yet.
            ModelContext.forget(self.root, ['desktop:123'])
            return {'execution': 'run-first', 'persona': {}, 'current': []}

        def run(*args, continuation, **kwargs):
            self.assertFalse(continuation.save([{'role': 'user', 'content': 'deleted original'}], self.budget))
            return 'complete'

        with patch('fish_agent.execution.runner.run_model_loop', side_effect=run):
            execute(self.root, 'qq', prepare=prepare, origin='test')
        self.assertEqual(ModelContext(self.root).retained(), [])

    def test_images_stay_in_media_and_deletion_invalidates_an_inflight_projection(self):
        uri = 'data:image/png;base64,AAECAwQFBgcICQ=='
        context = ModelContext(self.root)
        messages = self.prepare(context, 1, '看图', image=uri)
        self.budget.fit(messages, [], 0)
        context.save(messages, self.budget)
        self.assertNotIn('AAECAwQFBgcICQ==', context.state.get_state(context.key))
        resumed = ModelContext(self.root)
        self.assertEqual(resumed.retained()[-1]['content'][-1]['image_url']['url'], uri)
        references = list(context.value['versions'])
        ModelContext.forget(self.root, references)
        self.assertFalse(resumed.save(resumed.retained(), self.budget))
        self.assertEqual(ModelContext(self.root).retained(), [])


if __name__ == '__main__':
    unittest.main()
