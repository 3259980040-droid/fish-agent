"""Offline checks for the owner's unlimited/local-limit settings boundary."""
import json
import tempfile
import unittest
from pathlib import Path

from governance import Governance, daily_limits_enabled
from workbench import Workbench


class UnlimitedSettingsTests(unittest.TestCase):
    def test_unlimited_is_effective_and_reversible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'harness').mkdir(parents=True)
            (root / 'channels/fish_qq').mkdir(parents=True)
            (root / 'channels/fish_qq/config.json').write_text(json.dumps({'allowlist': {'owner_id': '200000001'}}), encoding='utf-8')
            (root / 'harness/agents.json').write_text(json.dumps({'agents': [{'id': 'qq', 'name': 'Test', 'persona': 'harness/agents/qq.persona.md'}]}), encoding='utf-8')
            (root / 'harness/agents').mkdir(parents=True)
            (root / 'harness/agents/qq.persona.md').write_text('test', encoding='utf-8')
            workbench = Workbench(root)
            unlimited = workbench.dispatch('settings.save', {'daily_limits': {'unlimited': True}})
            self.assertTrue(unlimited['daily_limits']['unlimited'])
            self.assertFalse(daily_limits_enabled(Governance(root).config(), 'qq'))

            limited = workbench.dispatch('settings.save', {
                'daily_limits': {'unlimited': False, 'limit': 7},
            })
            self.assertFalse(limited['daily_limits']['unlimited'])
            config = Governance(root).config()
            self.assertTrue(daily_limits_enabled(config, 'qq'))
            self.assertEqual(config['max_daily_calls'], 7)

    def test_invalid_limit_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'harness').mkdir(parents=True)
            (root / 'channels/fish_qq').mkdir(parents=True)
            (root / 'channels/fish_qq/config.json').write_text(json.dumps({'allowlist': {'owner_id': '200000001'}}), encoding='utf-8')
            (root / 'harness/agents.json').write_text(json.dumps({'agents': [{'id': 'qq', 'name': 'Test', 'persona': 'harness/agents/qq.persona.md'}]}), encoding='utf-8')
            (root / 'harness/agents').mkdir(parents=True)
            (root / 'harness/agents/qq.persona.md').write_text('test', encoding='utf-8')
            workbench = Workbench(root)
            with self.assertRaises(Exception):
                workbench.dispatch('settings.save', {'daily_limits': {'unlimited': False, 'limit': 0}})
            self.assertTrue(Governance(root).config()['daily_usage_limits']['qq'])


if __name__ == '__main__':
    unittest.main()
