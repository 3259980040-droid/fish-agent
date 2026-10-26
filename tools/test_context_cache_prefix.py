"""Changing an old-history lookup must preserve the continuous request prefix."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]

import test_context_compaction_dsh as fixture
from chat_request import assemble_chat_request
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


if __name__ == '__main__':
    unittest.main()
