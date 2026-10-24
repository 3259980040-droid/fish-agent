"""The read water level in the group context: one line, read-only, never unknown.

groups.handled is where a run stopped reading, groups.cursor where the group
arrived. The line built here reports both; a store that cannot answer contributes
nothing rather than a guess.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
import group_decide

CHAT = 'group:987654321'


def store(root, *, handled, cursor):
    path = Path(root) / 'data/social/group-policy.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE IF NOT EXISTS groups(chat TEXT PRIMARY KEY,config TEXT,cursor INTEGER DEFAULT 0,handled INTEGER DEFAULT 0,unanswered INTEGER DEFAULT 0);'
                     'CREATE TABLE IF NOT EXISTS observations(chat TEXT,seq INTEGER,id TEXT,text TEXT,data TEXT,created REAL,PRIMARY KEY(chat,seq));')
    db.execute('INSERT OR REPLACE INTO groups VALUES(?,?,?,?,0)', (CHAT, '{}', cursor, handled))
    for seq in range(1, cursor + 1):
        db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?)',
                   (CHAT, seq, 'o-' + str(seq), '第' + str(seq) + '条', '{}', 0.0))
    db.commit()
    db.close()


class WatermarkTest(unittest.TestCase):
    def test_line_reports_both_levels_and_the_new_rows(self):
        with tempfile.TemporaryDirectory() as root:
            store(root, handled=2, cursor=5)
            line = group_decide._read_position(root, CHAT)
        self.assertIn('上次看到第2条', line)
        self.assertIn('现在到第5条', line)
        self.assertIn('新增3条', line)
        self.assertNotIn('未知', line)
        self.assertLessEqual(len(line), 60)

    def test_first_read_is_named_instead_of_zero(self):
        with tempfile.TemporaryDirectory() as root:
            store(root, handled=0, cursor=5)
            line = group_decide._read_position(root, CHAT)
        self.assertIn('第一次看这个群', line)
        self.assertNotIn('未知', line)
        self.assertNotIn('上次看到第0条', line)
        self.assertLessEqual(len(line), 60)

    def test_a_store_that_cannot_answer_contributes_no_line(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(group_decide._read_position(root, CHAT), '')
            store(root, handled=0, cursor=0)
            self.assertEqual(group_decide._read_position(root, CHAT), '')

    def test_reply_scope_reaches_the_speaking_turn_only_once(self):
        speaking = group_decide._prompt_notes([], True)
        self.assertEqual(speaking, group_decide.READ_SCOPE)
        # The owner is the highest account: a group turn is the same Fish as the
        # private one, so the old "only the worthiest line" rule is gone on purpose.
        self.assertIn('跟私聊里是同一个你', speaking)
        self.assertEqual(group_decide._prompt_notes([], False), '')
        quoted = group_decide._prompt_notes([{'message_id': 'q-1', 'sender_id': '1', 'name': '甲',
                                             'text': '原文', 'reply_to': ''}], True)
        self.assertEqual(quoted.count(group_decide.READ_SCOPE), 1)
        self.assertIn('q-1', quoted)


if __name__ == '__main__':
    unittest.main()
