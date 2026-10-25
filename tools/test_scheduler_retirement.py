"""06-01/06-02: the old two-hour patrol is paused even on an already-seeded install."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]

from agent_scheduler import AgentScheduler
from bot_control import BotControl
from storage import database


class SchedulerRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data/social').mkdir(parents=True, exist_ok=True)
        self.control = BotControl(self.root)
        self.plan = self.control.create({'target': 'owner', 'title': '两小时活动巡查',
                                         'prompt': '看看群里有没有人说话', 'after_seconds': 7200,
                                         'purpose': 'autonomy'})
        with database(self.control.path) as db:
            db.execute("UPDATE scheduled SET configuration_key='autonomy-patrol' WHERE id=?", (self.plan['id'],))
            db.execute('CREATE TABLE IF NOT EXISTS automation_bootstrap(name TEXT PRIMARY KEY,created REAL)')
            db.execute("INSERT OR IGNORE INTO automation_bootstrap VALUES('unified-wakes-v1',?)", (time.time(),))

    def state_of(self, plan_id):
        with database(self.control.path) as db:
            return db.execute('SELECT state FROM scheduled WHERE id=?', (plan_id,)).fetchone()['state']

    def test_an_existing_seed_still_pauses_the_old_patrol(self):
        self.assertEqual(self.state_of(self.plan['id']), 'active')
        with database(self.control.path) as db:
            before = db.execute('SELECT COUNT(*) FROM scheduled').fetchone()[0]
        AgentScheduler(self.root, threading.Event()).initialise()
        self.assertEqual(self.state_of(self.plan['id']), 'paused')
        # The seed gate still prevents a second copy of the morning/evening plans.
        with database(self.control.path) as db:
            after = db.execute('SELECT COUNT(*) FROM scheduled').fetchone()[0]
            keys = {row['configuration_key'] for row in db.execute('SELECT configuration_key FROM scheduled')}
        self.assertEqual(before, after)
        self.assertNotIn('autonomy-morning', keys)

    def test_a_fresh_install_seeds_the_two_visible_plans_and_no_patrol(self):
        other = Path(self.temp.name) / 'fresh'
        (other / 'data/social').mkdir(parents=True, exist_ok=True)
        control = BotControl(other)
        AgentScheduler(other, threading.Event()).initialise()
        with database(control.path) as db:
            rows = [dict(row) for row in db.execute("SELECT title,configuration_key,state,purpose FROM scheduled")]
        keys = {row['configuration_key'] for row in rows}
        self.assertIn('autonomy-morning', keys)
        self.assertIn('autonomy-evening', keys)
        self.assertNotIn('autonomy-patrol', keys)
        self.assertFalse(any(row['title'] == '两小时活动巡查' for row in rows))
        self.assertTrue(all(row['state'] == 'active' for row in rows if row['configuration_key']))


if __name__ == '__main__':
    unittest.main()
