"""Focused checks for the unified memory model's visibility boundary."""
import tempfile
import unittest
from pathlib import Path

from memory_store import MemoryStore


class MemoryScopeTests(unittest.TestCase):
    def test_owner_management_and_runtime_read_same_fact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = MemoryStore(root, 'qq')
            saved = store.save('主人喜欢先看结论再看细节。', category='偏好', scope='owner', source='manual')
            runtime = store.search('结论 细节', scope='owner')
            management = store.search('结论 细节', scope=None)
            self.assertIn(saved['id'], [row['id'] for row in runtime])
            self.assertIn(saved['id'], [row['id'] for row in management])

    def test_temporary_test_space_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = MemoryStore(root, 'qq')
            test_scope = 'space:' + 'a' * 32
            with self.assertRaises(PermissionError):
                store.save('临时角色设定', category='情境', scope=test_scope, source='manual')


if __name__ == '__main__':
    unittest.main()
