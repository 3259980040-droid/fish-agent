"""Changing an old-history lookup must preserve the continuous request prefix."""
import json
import builtins
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]

import test_context_compaction_dsh as fixture
from chat_request import assemble_chat_request
from context_budget import ContextBudget
from desktop_context import DesktopContext
from fish_agent.experience import compaction
from locations import project_path
from storage import database


class ContextCachePrefixTests(unittest.TestCase):
    conversation = fixture.CompressionTests.conversation
    record = fixture.CompressionTests.record

    def setUp(self):
        fixture.CompressionTests.setUp(self)
        self.ledger = project_path(self.root, 'data/social/experience.db')
        with database(self.ledger) as db:
            compaction._schema(db)

    def test_different_old_recollections_keep_the_continuous_history_prefix(self):
        cid = self.conversation()
        alpha = self.record(cid, 'alpha 是旧计划的原话')
        beta = self.record(cid, 'beta 是另一段旧资料')
        recent = self.record(cid, '这里是仍然连续保留的最近经历')
        with database(self.ledger) as db:
            db.execute('INSERT INTO context_checkpoints(context_key,summary,source_ranges,created) '
                       'VALUES(?,?,?,?)', ('qq.db:' + cid, '同一份已提交摘要',
                           json.dumps([{'source': 'qq.db', 'conversation': cid, 'to': beta}]), 1))
        context = DesktopContext(self.root, self.store.db_path, channel='qq')

        def request(text):
            current = self.record(cid, text)
            history, source = context.history(cid, current, None, None, None)
            with patch('chat_request.now_line', return_value='固定的测试时间'):
                body = assemble_chat_request(persona='固定人格', history=history, memory='',
                    expression={'prompt': '固定表达'}, references='', current=text,
                    sources={'history': source})
            return body['messages'], source

        first, first_source = request('alpha')
        second, second_source = request('beta')
        # Provider-boundary messages: system, unchanged checkpoint, continuous raw history.
        self.assertEqual(first[:3], second[:3])
        self.assertEqual(first[2]['_fish_refs'], ['qq:' + str(recent)])
        for messages, source, old, phrase in ((first, first_source, alpha, 'alpha 是旧计划的原话'),
                                               (second, second_source, beta, 'beta 是另一段旧资料')):
            recalled = [m for m in messages if m.get('_fish_refs') == ['qq:' + str(old)]]
            self.assertEqual(len(recalled), 1)
            self.assertIn(phrase, recalled[0]['content'])
            self.assertGreater(messages.index(recalled[0]), 2)
            self.assertEqual(recalled[0]['_fish_layer'], 4)
            node = next(n for n in source['surface'] if n['id'] == old)
            self.assertEqual(node['layer'], 4)  # Recalled originals do not re-enter compaction.

    def test_expiring_cross_place_material_does_not_split_local_history(self):
        cid = self.conversation()
        first = self.record(cid, '本场所较早的连续记录')
        other_cid = self.conversation('group:20000')
        other = self.record(other_cid, '其他场所的无关话题', 'group:20000')
        recent = self.record(cid, '本场所较新的连续记录')
        with database(self.store.db_path) as db:
            for mid, stamp in ((first, '2026-10-26 20:00:00'),
                               (other, '2026-10-26 20:20:00'),
                               (recent, '2026-10-26 20:40:00')):
                db.execute('UPDATE messages SET ts=? WHERE id=?', (stamp, mid))
        context = DesktopContext(self.root, self.store.db_path, channel='qq')
        def history(at):
            return context.history(cid, None, None, None, None,
                event={'reference': 'diagnostic:clock', 'ts': at, 'text': '继续'})[0]
        before = history('2026-10-26 20:45:00')
        after = history('2026-10-26 20:55:00')
        self.assertEqual(before[:2], after[:2])
        self.assertEqual([m['_fish_refs'][0] for m in before[:2]],
                         ['qq:' + str(first), 'qq:' + str(recent)])
        self.assertTrue(any(m['_fish_refs'] == ['qq:' + str(other)] for m in before))

    def test_runtime_state_changes_only_the_tail(self):
        # Capability lookup imports this entry adapter at runtime; it must not
        # retain an import of the retired state_first helper.
        import fish_chat
        def request(state):
            return assemble_chat_request(persona='固定人格',
                history=[{'role': 'user', 'content': '连续历史'}], memory='',
                expression={'prompt': '固定表达'}, references='', current='本轮输入',
                sources={}, runtime_state=state)['messages']
        with patch('chat_request.now_line', return_value='固定时间'):
            first, second = request('群场所的实际状态'), request('主人场所的实际状态')
        self.assertEqual(first[:2], second[:2])
        self.assertEqual(first[-2]['content'], '群场所的实际状态')
        self.assertEqual(first[-2]['_fish_layer'], 0)

    def budget(self):
        return ContextBudget(self.root, type('Gateway', (), {'model': 'fake-model'})(),
            metadata={'id': 'fake-model', 'context_window': 100000},
            tokenizer=fixture.CharTokenizer())

    @staticmethod
    def tool_pair(name, size):
        return [{'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': name, 'type': 'function', 'function': {'name': 'fish_shell', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': name, 'content': 't' * size}]

    def test_tool_growth_trims_reference_tail_and_preserves_protocol(self):
        budget = self.budget()
        history = [{'role': 'user', 'content': 'h' * 1000, '_fish_layer': 1},
                    *[{'role': 'user', 'content': letter * 1800, '_fish_layer': 4,
                       '_fish_refs': [letter]} for letter in ('c', 'b', 'a')]]
        with patch('chat_request.now_line', return_value='fixed time'):
            messages = assemble_chat_request(persona='s' * 500, history=history,
                memory='important memory', expression={'prompt': ''}, references='',
                current='current', sources={})['messages']
        budget.fit(messages, [], 0)
        stable = json.loads(json.dumps(messages[:4]))
        messages.extend(self.tool_pair('call-1', 1800))
        report = budget.fit(messages, [], 0)
        self.assertEqual(messages[:4], stable)
        self.assertEqual(report['trimmed'][0]['references'], ['a'])
        self.assertTrue(any(m.get('content') == 'important memory' for m in messages))
        self.assertEqual(messages[-2]['tool_calls'][0]['id'], messages[-1]['tool_call_id'])

    def test_recycled_message_id_cannot_turn_tool_call_into_optional_material(self):
        budget = self.budget()
        messages = [{'role': 'system', 'content': 's' * 500},
                    {'role': 'user', 'content': 'a' * 2000, '_fish_layer': 4},
                    {'role': 'user', 'content': 'b' * 2000, '_fish_layer': 4},
                    {'role': 'user', 'content': 'current', '_fish_layer': 0}]
        discarded_ids = [id(m) for m in messages[1:3]]
        budget.fit(messages, [], 0)
        messages.extend(self.tool_pair('call-1', 6000))
        budget.fit(messages, [], 0)  # Both optional messages are now gone.
        self.assertTrue(all(id(m) not in discarded_ids for m in messages))
        next_call, next_result = self.tool_pair('call-2', 1000)
        next_call['content'] = 'r' * 1000
        messages.extend([next_call, next_result])
        # CPython may recycle a discarded dict's address. Control that allocator
        # event, not the budget decision: the new call must remain a required call.
        def recycled_id(value):
            return discarded_ids[0] if value is next_call else builtins.id(value)
        with patch('context_budget.id', side_effect=recycled_id, create=True):
            with self.assertRaisesRegex(ValueError, '工具协议'):
                budget.fit(messages, [], 0)
        self.assertIn(next_call, messages)
        self.assertIn(next_result, messages)


if __name__ == '__main__':
    unittest.main()
