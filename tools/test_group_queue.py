"""Offline checks for group request merge, quote threading and recovery."""
import tempfile
import time
import unittest
from pathlib import Path

from group_tasks import GroupTaskStore


def payload(message, text, *, sender='200000001', quote=None):
    return {'chat_id': 'group:123456789', 'message_id': message,
            'sender': {'id': sender}, 'reply_to': quote,
            'segments': [{'type': 'text', 'text': text}]}


class GroupQueueTests(unittest.TestCase):
    def test_only_consecutive_supplements_merge(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            store = GroupTaskStore(Path(temp))
            first = store.add(payload('m1', '第一个请求'), 1, '', 'r1')
            merged = store.add(payload('m2', '补充一下'), 2, '', 'r1', supplement=True, consecutive=True)
            self.assertEqual(first['id'], merged['id'])
            other = store.add(payload('m3', '独立请求'), 3, '', 'r1')
            self.assertNotEqual(first['id'], other['id'])

    def test_quote_uses_existing_thread_and_cancel_target(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            store = GroupTaskStore(Path(temp))
            first = store.add(payload('root', '原请求'), 1, '', 'r1')
            reply = store.add(payload('quote', '引用回复', quote='root'), 2, '', 'r1')
            self.assertEqual(first['thread'], reply['thread'])
            self.assertEqual(store.cancel_target('group:123456789', '', '200000001', 'root'), first['id'])

    def test_restart_marks_live_jobs_interrupted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            store = GroupTaskStore(Path(temp))
            first = store.add(payload('m1', '任务'), 1, '', 'r1')
            store.recover('group:123456789', '')
            self.assertEqual(store.get(first['id'])['state'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
