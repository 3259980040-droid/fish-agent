"""Offline checks that the read switch survives the workbench channel update.

read is an authorisation dimension of its own: it must not be dropped silently
just because the desktop settings panel has no checkbox for it yet.
"""
import json
import tempfile
import unittest
from pathlib import Path

import requests
from unittest.mock import patch

from group_policy import GroupPolicy
from workbench import Workbench

GROUP = '123456789'
CHAT = 'group:' + GROUP
OTHER = '987654321'


class ChannelReadSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'channels/fish_qq').mkdir(parents=True)
        (self.root / 'channels/fish_qq/config.json').write_text(
            json.dumps({'allowlist': {'group_ids': [GROUP]}}), encoding='utf-8')
        self.workbench = Workbench(self.root)

    def update(self, **params):
        return self.workbench.dispatch('channels.update', {'group_id': GROUP, **params})

    def stored(self):
        return GroupPolicy(self.root).config(CHAT)

    def listed(self):
        """The stored group entry a settings panel reads; no local service is contacted."""
        with patch('requests.get', side_effect=requests.ConnectionError()):
            status = self.workbench.dispatch('channels.status', {})
        return next(row for row in status['groups'] if row['id'] == GROUP)

    def test_read_alone_is_saved_instead_of_being_swallowed(self):
        result = self.update(read=True)
        self.assertIs(True, result['read'])
        self.assertIs(True, self.stored()['read'])

    def test_read_does_not_disturb_listen_or_reply(self):
        self.update(listen=True)
        self.assertIs(True, self.stored()['listen'])
        self.assertIs(False, self.stored()['read'])
        read_on = self.update(read=True)
        self.assertEqual({'listen': True, 'read': True, 'reply': False},
                         {key: read_on[key] for key in ('listen', 'read', 'reply')})
        self.assertIs(True, self.stored()['listen'])
        self.assertIs(False, self.stored()['reply'])
        self.update(read=False)
        stored = self.stored()
        self.assertIs(False, stored['read'])
        self.assertIs(True, stored['listen'])

    def test_reply_still_requires_listen(self):
        result = self.update(listen=False, reply=True, read=True)
        self.assertIs(False, result['listen'])
        self.assertIs(False, result['reply'])
        self.assertIs(True, result['read'])
        stored = self.stored()
        self.assertEqual({'listen': False, 'reply': False, 'read': True},
                         {key: stored[key] for key in ('listen', 'reply', 'read')})

    def test_status_shows_the_same_read_value_as_the_policy(self):
        self.update(read=True)
        self.assertIs(True, self.listed()['read'])
        self.assertIs(True, self.stored()['read'])
        self.update(read=False)
        self.assertIs(False, self.listed()['read'])
        self.assertIs(False, self.stored()['read'])
        self.assertIs(False, self.listed()['listen'])
        self.assertIs(False, self.listed()['reply'])

    def test_other_group_still_refused(self):
        with self.assertRaises(ValueError) as refused:
            self.workbench.dispatch('channels.update', {'group_id': OTHER, 'read': True})
        self.assertIn('只能修改白名单群', str(refused.exception))
        self.assertIs(False, GroupPolicy(self.root).config('group:' + OTHER)['read'])

    def test_non_boolean_read_is_rejected_before_writing(self):
        with self.assertRaises(ValueError) as refused:
            self.update(read='yes')
        self.assertIn('群设置必须是开关值', str(refused.exception))
        self.assertIs(False, self.stored()['read'])

    def test_empty_update_still_says_nothing_to_save(self):
        with self.assertRaises(ValueError) as refused:
            self.update()
        self.assertIn('没有要保存的群设置', str(refused.exception))


if __name__ == '__main__':
    unittest.main()
