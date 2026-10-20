"""Offline checks for retrieving relevant memory into the prompt context."""
import tempfile
import unittest
from pathlib import Path

from memory_store import MemoryStore


class MemoryContextTests(unittest.TestCase):
    def test_relevant_owner_fact_is_added_with_source(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            store = MemoryStore(Path(temp), 'qq')
            store.save('主人喜欢先看结论，再看细节。', category='偏好', scope='owner',
                       source='owner-test', source_text='主人明确说过')
            block = store.context('结论 细节', scope='owner')
            self.assertIn('主人喜欢先看结论', block)
            self.assertIn('owner-test', block)
            self.assertIn('本轮检索', block)


if __name__ == '__main__':
    unittest.main()
