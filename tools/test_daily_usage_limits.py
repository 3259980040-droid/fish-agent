"""Daily budget switches preserve accounting, isolation, and bounded defaults."""
import concurrent.futures
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
import dependencies
from governance import Governance, daily_limits_enabled
from model_gateway import ModelLedger
from storage import write_json


class DailyUsageLimitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = Governance(self.root, 'qq')
        self.policy.save({'max_daily_calls': 1, 'max_daily_replies': 1,
            'max_model_requests_daily': 1, 'max_model_tokens_daily': 1})

    def test_default_and_invalid_values_still_enforce(self):
        for config in ({}, {'daily_usage_limits': False}, {'daily_usage_limits': {'qq': 0}},
                       {'daily_usage_limits': {'qq': 'false'}}):
            self.assertTrue(daily_limits_enabled(config, 'qq'))
        ledger = ModelLedger(self.root, 'qq')
        ledger.reserve('first')
        with self.assertRaises(PermissionError): ledger.reserve('blocked')

    def test_unlimited_records_tokens_and_requests_then_reenable(self):
        ledger = ModelLedger(self.root, 'qq')
        first = ledger.reserve('first'); ledger.finish(first, {'prompt_tokens': 2000001})
        self.policy.save({'daily_usage_limits': {'qq': False}})
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(lambda _: ledger.reserve('unlimited'), range(12)))
        self.assertEqual(len(set(ids)), 12)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['requests'], 13)
        self.assertEqual(snapshot['input_tokens'], 2000001)
        self.assertFalse(snapshot['daily_limits_enabled'])
        self.policy.save({'daily_usage_limits': {'qq': True}})
        with self.assertRaises(PermissionError): ledger.reserve('blocked-again')

    def test_role_isolation(self):
        self.policy.save({'daily_usage_limits': {'qq': False}})
        ledger = ModelLedger(self.root, 'research')
        ledger.reserve('research-first')
        with self.assertRaises(PermissionError): ledger.reserve('research-blocked')
        research = Governance(self.root, 'research')
        research.consume('calls')
        with self.assertRaises(PermissionError): research.consume('calls')
        self.assertTrue(research.config()['daily_usage_limits']['research'])

    def test_calls_replies_keep_counts_and_can_be_reenabled(self):
        self.policy.save({'daily_usage_limits': {'qq': False}})
        for _ in range(3):
            self.policy.consume('calls'); self.policy.consume('replies')
        self.assertEqual(self.policy.snapshot()['usage'], {'calls': 3, 'replies': 3})
        self.policy.save({'daily_usage_limits': {'qq': True}})
        for kind in ('calls', 'replies'):
            with self.assertRaises(PermissionError): self.policy.consume(kind)

    def test_partial_save_preserves_other_role_and_rejects_invalid(self):
        research = Governance(self.root, 'research')
        research.save({'daily_usage_limits': {'research': False}})
        self.policy.save({'daily_usage_limits': {'qq': False}})
        self.assertEqual(self.policy.config()['daily_usage_limits'], {'qq': False, 'research': False})
        self.policy.save({'paused': True})
        self.assertTrue(self.policy.config()['paused'])
        self.assertEqual(self.policy.config()['daily_usage_limits'], {'qq': False, 'research': False})
        for value in (False, {'qq': 0}, {'other': False}):
            with self.assertRaises(ValueError): self.policy.save({'daily_usage_limits': value})


if __name__ == '__main__':
    unittest.main(verbosity=2)
