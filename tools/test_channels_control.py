"""Offline checks for the native channel participation controls."""
import json
import tempfile
import unittest
from pathlib import Path

from group_policy import GroupPolicy
from workbench import Workbench


class ChannelControlTests(unittest.TestCase):
    def test_only_allowlisted_group_can_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'channels/fish_qq').mkdir(parents=True)
            (root / 'channels/fish_qq/config.json').write_text(
                json.dumps({'allowlist': {'group_ids': ['123456789']}}), encoding='utf-8')
            workbench = Workbench(root)
            result = workbench.dispatch('channels.update', {
                'group_id': '123456789', 'listen': True, 'reply': False,
            })
            self.assertTrue(result['listen'])
            self.assertFalse(result['reply'])
            self.assertTrue(GroupPolicy(root).config('group:123456789')['listen'])
            with self.assertRaises(ValueError):
                workbench.dispatch('channels.update', {'group_id': 'other-group', 'listen': True})

    def test_reply_requires_listen(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'channels/fish_qq').mkdir(parents=True)
            (root / 'channels/fish_qq/config.json').write_text(
                json.dumps({'allowlist': {'group_ids': ['123456789']}}), encoding='utf-8')
            workbench = Workbench(root)
            result = workbench.dispatch('channels.update', {
                'group_id': '123456789', 'listen': False, 'reply': True,
            })
            self.assertFalse(result['listen'])
            self.assertFalse(result['reply'])


if __name__ == '__main__':
    unittest.main()
