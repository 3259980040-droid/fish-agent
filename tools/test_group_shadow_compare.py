"""Shadow comparison: only a checkable correlation may pair an outbound reply."""
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import group_shadow_compare as compare

MESSAGES = """
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT, user_id TEXT, text TEXT,
  replied INTEGER DEFAULT 0, conversation_id TEXT, sender_id TEXT, direction TEXT,
  channel_message_id TEXT, reply_to TEXT, segments TEXT, status TEXT);
"""
SHADOWS = """
CREATE TABLE shadows (
  id INTEGER PRIMARY KEY, chat TEXT, message_id TEXT, created REAL, engine TEXT, text TEXT,
  tools TEXT, elapsed_ms INTEGER, error TEXT, source TEXT);
"""
JOBS = """
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, chat TEXT, window TEXT, revision TEXT, sender TEXT, root TEXT,
  thread TEXT, kind TEXT, state TEXT, payload TEXT, snapshot TEXT DEFAULT '{}',
  session TEXT DEFAULT '', response TEXT DEFAULT '', remote TEXT DEFAULT '', error TEXT,
  created REAL, updated REAL);
CREATE TABLE inputs (message TEXT PRIMARY KEY, job TEXT, local_id INTEGER);
"""


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        (self.root / 'data/qq').mkdir(parents=True)
        (self.root / 'data/social').mkdir(parents=True)
        self.qq = self._create(self.root / 'data/qq/qq.db', MESSAGES)
        self.shadow_db = self._create(self.root / 'data/social/group-shadow.db', SHADOWS)
        self.jobs_db = self._create(self.root / 'data/social/group-replies.db', JOBS)
        self.chat = 'group:987654321'
        self.base = datetime(2024, 5, 1, 12, 0, 0)

    @staticmethod
    def _create(path, script):
        with closing(sqlite3.connect(path)) as db:
            db.executescript(script)
        return path

    def stamp(self, seconds=0):
        return (self.base + timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S')

    @staticmethod
    def _write(path, statements):
        with closing(sqlite3.connect(path)) as db:
            with db:
                for sql, args in statements:
                    db.execute(sql, args)

    def inbound(self, mid, text, *, seconds=0):
        self._write(self.qq, [
            ('INSERT INTO messages(ts,chat_id,sender_id,text,direction,channel_message_id,status) '
             'VALUES(?,?,?,?,?,?,?)', (self.stamp(seconds), self.chat, '333333333', text, 'inbound', mid, 'observed'))])

    def outbound(self, text, *, mid='', reply_to='', seconds=0):
        self._write(self.qq, [
            ('INSERT INTO messages(ts,chat_id,sender_id,text,direction,channel_message_id,reply_to,status) '
             'VALUES(?,?,?,?,?,?,?,?)', (self.stamp(seconds), self.chat, 'bot', text, 'outbound', mid, reply_to, 'sent'))])

    def shadow(self, mid, text, *, seconds=10, error=''):
        self._write(self.shadow_db, [
            ('INSERT INTO shadows(chat,message_id,created,engine,text,tools,elapsed_ms,error,source) '
             'VALUES(?,?,?,?,?,?,?,?,?)', (self.chat, mid, (self.base + timedelta(seconds=seconds)).timestamp(),
                                           'shadow', text, '[]', 1200, error, 'engine-shadow'))])

    def job(self, mid, remote):
        self._write(self.jobs_db, [
            ('INSERT INTO jobs(id,chat,root,kind,state,remote,created,updated) VALUES(?,?,?,?,?,?,?,?)',
             (mid + '-job', self.chat, mid, 'direct', 'sent', remote, 0., 0.)),
            ('INSERT INTO inputs VALUES(?,?,?)', (mid, mid + '-job', 0))])

    # 1
    def test_exact_quote_pairs_the_real_reply(self):
        self.inbound('in-1', '这个话题怎么看', seconds=0)
        self.outbound('旧路径的回答', mid='out-1', reply_to='in-1', seconds=6)
        self.shadow('in-1', '影子候选', seconds=10)
        records = compare.collect(self.root)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record['status'], 'matched')
        self.assertEqual(record['actual'], '旧路径的回答')
        self.assertEqual(record['actual_ids'], ['out-1'])
        self.assertEqual(record['sources'], ['reply_to'])
        self.assertEqual(record['shadow'], '影子候选')
        self.assertIn('这个话题怎么看', record['inbound'])
        text = compare.render(self.root, records)
        self.assertIn('旧路径的回答', text)
        self.assertIn('| matched |', text)
        self.assertEqual(compare.summarize(records), {'matched': 1, 'unmatched': 0, 'shadow_only': 0, 'dsh_only': 0})

    # 2
    def test_close_in_time_is_not_a_pairing(self):
        self.inbound('in-A', '问题 A', seconds=0)
        self.inbound('in-B', '问题 B', seconds=0)
        self.outbound('这条是回 B 的', mid='out-B', reply_to='in-B', seconds=5)
        self.shadow('in-A', '影子 A', seconds=8)
        records = compare.collect(self.root)
        record = records[0]
        self.assertEqual(record['message_id'], 'in-A')
        self.assertEqual(record['status'], 'unmatched')
        self.assertEqual(record['actual'], '')
        self.assertEqual(record['actual_ids'], [])
        self.assertEqual(record['nearby'], 1)
        text = compare.render(self.root, records)
        self.assertIn(compare.UNCONFIRMED, text)
        self.assertNotIn('这条是回 B 的', text)
        self.assertEqual(compare.summarize(records)['unmatched'], 1)

    # 3
    def test_shadow_only_when_the_old_path_stayed_silent(self):
        self.inbound('in-3', '没人理的问题', seconds=0)
        self.shadow('in-3', '影子想说', seconds=5)
        record = compare.collect(self.root)[0]
        self.assertEqual(record['status'], 'shadow_only')
        self.assertEqual(record['actual'], '')
        self.assertEqual(record['nearby'], 0)
        text = compare.render(self.root, [record])
        self.assertIn('shadow_only', text)
        self.assertNotIn(compare.UNCONFIRMED, text)

    # 4
    def test_two_concurrent_questions_keep_their_own_answers(self):
        self.inbound('in-A', '问题 A', seconds=0)
        self.inbound('in-B', '问题 B', seconds=0)
        self.outbound('回答 A', mid='out-A', reply_to='in-A', seconds=20)
        self.outbound('回答 B', mid='out-B', reply_to='in-B', seconds=21)
        self.shadow('in-A', '影子 A', seconds=30)
        self.shadow('in-B', '影子 B', seconds=31)
        records = {item['message_id']: item for item in compare.collect(self.root)}
        self.assertEqual(set(records), {'in-A', 'in-B'})
        for mid, own, other in (('in-A', '回答 A', '回答 B'), ('in-B', '回答 B', '回答 A')):
            self.assertEqual(records[mid]['status'], 'matched')
            self.assertEqual(records[mid]['actual'], own)
            self.assertNotIn(other, records[mid]['actual'])
        self.assertEqual(compare.summarize(list(records.values()))['matched'], 2)

    # 5
    def test_reply_ledger_remote_id_is_a_real_correlation(self):
        self.inbound('in-5', '记账关联的问题', seconds=0)
        self.outbound('账本关联的回复', mid='out-5', reply_to='', seconds=4)
        self.job('in-5', 'out-5')
        self.shadow('in-5', '影子 5', seconds=6)
        record = compare.collect(self.root)[0]
        self.assertEqual(record['status'], 'matched')
        self.assertEqual(record['actual'], '账本关联的回复')
        self.assertEqual(record['sources'], ['job'])

    # 6
    def test_reply_without_a_shadow_candidate_is_dsh_only(self):
        self.inbound('in-6', '影子没算出来', seconds=0)
        self.outbound('旧路径照常回了', mid='out-6', reply_to='in-6', seconds=3)
        self.shadow('in-6', '', seconds=9, error='RuntimeError: 模型不可用')
        record = compare.collect(self.root)[0]
        self.assertEqual(record['status'], 'dsh_only')
        self.assertEqual(record['actual'], '旧路径照常回了')
        text = compare.render(self.root, [record])
        self.assertIn('RuntimeError', text)
        self.assertEqual(compare.summarize([record])['dsh_only'], 1)


if __name__ == '__main__':
    unittest.main()
