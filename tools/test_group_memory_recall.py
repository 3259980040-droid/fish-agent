"""A group's auto-learned memory is a candidate; recall must still show it, marked.

apply_extraction stores a group item as status='candidate' because a group message
can only ever be evidence='observed'. If recall kept the active-only rule, group
auto-learning would write facts nobody could ever read back, so the store opens
candidates for group scopes only, labels them as unconfirmed in the prompt, and
leaves plain search() and owner/public recall exactly as they were.
"""
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
import memory_engine
from storage import write_json
from test_social_base import Fixture
from memory_store import MemoryStore

UNCONFIRMED = '候选'


class GroupMemoryRecallTests(Fixture):
    def setUp(self):
        super().setUp()
        # What is asserted here is the lexical recall path; the semantic backfill
        # would keep a background thread on memory.db long after the test (and the
        # temp dir cannot be removed under it on Windows).
        write_json(self.root / 'harness' / 'memory-embeddings.json', {'enabled': False})
        self.store = MemoryStore(self.root, 'qq')
        self.scope = 'chat:' + self.chat  # chat:group:<id>, the group's own scope

    def tearDown(self):
        memory_engine._shutdown_all()
        for _ in range(20):
            try:
                self.tmp.cleanup()
                return
            except PermissionError:
                time.sleep(0.05)
        self.tmp.cleanup()

    def save(self, text, *, scope=None, status='candidate', category='事实', source='learning:test'):
        return self.store.save(text, category=category, scope=scope or self.scope, source=source,
                               evidence='observed', status=status)

    def line_with(self, block, needle):
        return next(line for line in block.splitlines() if needle in line)

    # 1
    def test_group_candidate_is_recalled_and_marked_unconfirmed(self):
        self.save('群友甲最近都在夜钓鲫鱼')
        block = self.store.context('夜钓鲫鱼', self.scope)
        self.assertIn('夜钓鲫鱼', block)
        self.assertIn(UNCONFIRMED, self.line_with(block, '夜钓鲫鱼'))

    # 2
    def test_the_same_candidate_stays_invisible_in_owner_recall(self):
        self.save('主人其实喜欢夜钓鲫鱼', scope='owner')
        self.assertNotIn('夜钓鲫鱼', self.store.context('夜钓鲫鱼', 'owner'))
        self.assertEqual([], self.store.recall('夜钓鲫鱼', scope='owner'))
        self.assertEqual([], self.store.search('夜钓鲫鱼', scope='owner'))

    # 3
    def test_group_active_memory_carries_no_candidate_mark(self):
        self.save('本群每晚八点开黑', status='active', source='manual')
        block = self.store.context('开黑', self.scope)
        self.assertIn('开黑', block)
        self.assertNotIn(UNCONFIRMED, self.line_with(block, '开黑'))

    # 4
    def test_recall_growth_is_exactly_the_group_candidates(self):
        self.save('群友乙昨天请假没来', status='active')
        before = {row['id'] for row in self.store.recall('群友', scope=self.scope, limit=200)}
        candidate = self.save('群友乙下周要换工作')
        after = {row['id'] for row in self.store.recall('群友', scope=self.scope, limit=200)}
        self.assertEqual({candidate['id']}, after - before)
        self.assertEqual(set(), before - after)
        # limit still applies to the widened set instead of being pushed aside.
        self.assertEqual(1, len(self.store.recall('群友', scope=self.scope, limit=1)))
        # Plain search keeps the active-only contract: the widening is a recall view.
        self.assertEqual(before, {row['id'] for row in self.store.search('群友', scope=self.scope, limit=200)})

    # 5
    def test_public_candidate_is_not_widened_into_group_recall(self):
        self.save('PUBLIC-CANDIDATE-CANARY 公共区待核实', scope='public')
        self.assertNotIn('CANARY', self.store.context('CANARY', self.scope))
        self.assertEqual([], self.store.search('CANARY', scope='public'))


if __name__ == '__main__':
    unittest.main()
