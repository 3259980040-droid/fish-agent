"""Diving and wake probability: two ways she decides not to speak.

The smallest check that the two gates exist and that only the right surface can
set them: one dive deadline in the candidate, one roll that never touches a
mention, and the two tool scopes (her own group, the owner's whitelisted group).
No model call, no transport, no real group.
"""
import sys
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from test_social_base import Fixture
from group_policy import GroupPolicy
from tool_host import ToolHost


class DiveTests(Fixture):
    def setUp(self):
        super().setUp()
        self.policy = GroupPolicy(self.root)
        # read is the owner's authorisation: it is what puts a group on the list the
        # owner may dive, so the whitelist case has to hang off it.
        self.policy.configure(self.chat, {'read': True, 'listen': True, 'reply': True, 'debounce_s': 0})
        self.host = ToolHost(self.root)

    def payload(self, mid, direct=False):
        return {'chat_id': self.chat, 'message_id': str(mid), 'sender': {'id': '333333333'},
                'segments': [{'type': 'text', 'text': '普通话题'}], 'mentions_bot': direct}

    def talk(self, name, arguments, session):
        return self.host.execute('qq', session, name, arguments, uuid.uuid4().hex)

    # 1
    def test_a_dive_silences_the_chat_until_its_deadline(self):
        self.policy.configure(self.chat, {'dive_until': 1060.0})
        self.policy.observe(self.payload('dive-1'), now=1000.0)
        dived = self.policy.candidate(self.chat, now=1001.0)
        self.assertEqual('silent', dived['action'])
        self.assertIn('潜水', dived['reason'])
        self.assertEqual('candidate', self.policy.candidate(self.chat, now=1060.0)['action'])

    # 2
    def test_the_group_dives_itself_and_never_another_group(self):
        session = 'group-fish:' + self.chat
        self.host.bind('qq', session, 'chat:' + self.chat, '333333333')
        before = time.time()
        result = self.talk('fish_group_dive', {'minutes': 30}, session)
        self.assertTrue(before + 1800 <= result['dive_until'] <= time.time() + 1800)
        self.assertEqual(result['dive_until'], self.policy.config(self.chat)['dive_until'])
        with self.assertRaises(PermissionError):
            self.talk('fish_group_dive', {'minutes': 30, 'chat_id': 'group:770011022'}, session)
        self.assertEqual(0, self.talk('fish_group_wake', {}, session)['dive_until'])

    # 3
    def test_the_owner_dives_a_whitelisted_group_and_not_an_unlisted_one(self):
        session = 'qq-private:' + uuid.uuid4().hex
        self.host.bind('qq', session, 'owner', self.owner, False, chat_mode='owner')
        with self.assertRaises(PermissionError):
            self.talk('fish_group_dive', {'minutes': 30, 'chat_id': 'group:770011022'}, session)
        result = self.talk('fish_group_dive', {'minutes': 30, 'chat_id': self.chat}, session)
        self.assertGreater(result['dive_until'], time.time())

    # 4
    def test_wake_probability_spares_a_mention(self):
        self.policy.configure(self.chat, {'wake_probability': 0})
        self.policy.observe(self.payload('roll-1'), now=1000.0)
        missed = self.policy.candidate(self.chat, now=1001.0)
        self.assertEqual(('wait', '本次未被唤醒'), (missed['action'], missed['reason']))
        self.policy.observe(self.payload('roll-2', direct=True), now=1002.0)
        self.assertEqual('candidate', self.policy.candidate(self.chat, now=1003.0)['action'])

    # 5
    def test_dive_and_wake_report_what_is_still_unread(self):
        from storage import database
        session = 'group-fish:' + self.chat
        self.host.bind('qq', session, 'chat:' + self.chat, '333333333')
        with database(self.policy.path) as db:
            db.execute('UPDATE groups SET cursor=5, handled=2 WHERE chat=?', (self.chat,))
        dived = self.talk('fish_group_dive', {'minutes': 30}, session)
        self.assertEqual(3, dived['unread'])
        self.assertIn('你还有 3 条没看', dived['note'])
        self.assertIn('不需要再叫醒', dived['note'])
        with database(self.policy.path) as db:
            # The dive reports the gap; it never consumes it.
            self.assertEqual(2, db.execute('SELECT handled FROM groups WHERE chat=?', (self.chat,)).fetchone()[0])
        self.assertEqual(3, self.talk('fish_group_wake', {}, session)['unread'])
        with database(self.policy.path) as db:
            db.execute('UPDATE groups SET handled=cursor WHERE chat=?', (self.chat,))
        caught_up = self.talk('fish_group_dive', {'minutes': 30}, session)
        self.assertEqual(0, caught_up['unread'])
        self.assertIn('没有没看的新消息', caught_up['note'])
        fresh_chat, fresh = 'group:556677889', 'group-fish:group:556677889'
        self.host.bind('qq', fresh, 'chat:' + fresh_chat, '333333333')
        unknown = self.talk('fish_group_dive', {'minutes': 30}, fresh)
        self.assertEqual(0, unknown['unread'])
        self.assertIn('读不到', unknown['note'])


if __name__ == '__main__':
    unittest.main()
