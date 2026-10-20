"""Focused checks for memory correction and duplicate processing."""
import tempfile
import unittest
from pathlib import Path

from memory_store import MemoryStore


class MemoryCorrectionTests(unittest.TestCase):
    def test_preference_correction_keeps_history_and_new_value_wins(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            store = MemoryStore(Path(temp), 'qq')
            old = store.save('以前喜欢大量动作。', category='偏好', scope='owner',
                             source='manual', evidence='self_report')
            new = store.save('后来改为按需使用。', category='偏好', scope='owner',
                             memory_id=old['id'], source='manual',
                             source_text='主人明确更正：改成按需使用。', evidence='self_report')
            self.assertEqual(new['id'], old['id'])
            rows = store.search('动作 按需', scope='owner')
            active = {row['text'] for row in rows}
            self.assertIn('后来改为按需使用。', active)
            history = store.history(old['id'])
            self.assertEqual([row['text'] for row in history],
                             ['后来改为按需使用。', '以前喜欢大量动作。'])
            self.assertEqual(history[0]['operation'], 'correction')

    def test_duplicate_message_does_not_create_another_fact(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            store = MemoryStore(Path(temp), 'qq')
            first = store.save('主人喜欢先看结论。', category='偏好', scope='owner', source='manual')
            second = store.save('主人喜欢先看结论。', category='偏好', scope='owner', source='manual')
            self.assertTrue(second.get('duplicate'))
            self.assertEqual(first['id'], second['id'])
            self.assertEqual(len(store.history(first['id'])), 1)


if __name__ == '__main__':
    unittest.main()
