"""One stream: the owner's private chat and every authorised group, one timeline.

Reading is no longer a scoped privilege - the private chat and the groups are the
same experience to her, so a bound group reads the private chat too and what she
chooses to say about it is her own judgement. So these tests pin what a read
really returns (merged order, readable attachment lines, the newest-N rule) and
the observation row it leaves behind - never a refusal.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401

from governance import Governance
from group_policy import GroupPolicy
from group_read import ACTIVITY_MAX_LINES, recent_lines
from tool_host import ToolHost, bind_session
from workbench import Workbench

OWNER = '111111'
GROUP = '1043230169'
CHAT = 'group:' + GROUP
OTHER = '770011022'
OTHER_CHAT = 'group:' + OTHER
SENDER = '2100000001'
PRIVATE = 'private:' + OWNER
DDL = ('CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT, '
       'user_id TEXT, text TEXT, replied INTEGER DEFAULT 0, conversation_id TEXT, sender_id TEXT, '
       'direction TEXT, channel_message_id TEXT, reply_to TEXT, segments TEXT, status TEXT)')
ROSTER = (('20000001', '群名片甲'), ('20000002', '群名片乙'))


def ago(minutes):
    return (datetime.now() - timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class ActivityStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_json(self.root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'model': 'fake-model', 'base_url': 'https://fake.invalid',
                        'apiKeyEnv': 'FISH_TEST_QQ_KEY'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.root / 'channels/fish_qq/config.json', {
            'allowlist': {'owner_id': OWNER, 'group_ids': [GROUP, OTHER]},
            'bot': {'display_name': '大肥鱼'}})
        self.qq = self.root / 'data/qq/qq.db'
        self.qq.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.qq)) as db:
            db.execute(DDL)
            db.commit()
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        policy = GroupPolicy(self.root)
        for chat in (CHAT, OTHER_CHAT):
            policy.configure(chat, {'read': True, 'listen': True, 'reply': True})
        self.roster()
        self.host = ToolHost(self.root)
        self.host.serve(background_workers=False, listen_port=0)
        self.addCleanup(self.stop_host)
        config = json.loads((self.root / 'harness/agents.json').read_text(encoding='utf-8'))
        config['runtime']['tool_host_port'] = int(self.host.server.server_address[1])
        write_json(self.root / 'harness/agents.json', config)
        self.owner_session = 'qq-private:' + uuid.uuid4().hex
        bind_session(self.root, 'qq', self.owner_session, 'owner', OWNER, False, chat_mode='owner')
        self.group_session = 'group-fish:' + CHAT
        bind_session(self.root, 'qq', self.group_session, 'chat:' + CHAT, SENDER)

    def stop_host(self):
        self.host.stop.set()
        self.host.server.shutdown()
        self.host.server.server_close()

    def roster(self):
        path = self.root / 'data/social/people.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS members (group_id TEXT, user_id TEXT, nickname TEXT, '
                       'card TEXT, PRIMARY KEY (group_id, user_id))')
            for user_id, card in ROSTER:
                db.execute('INSERT OR REPLACE INTO members VALUES(?,?,?,?)', (GROUP, user_id, card, card))
            db.commit()

    def add(self, minutes, text, *, chat=CHAT, mid='', uid=SENDER, direction='inbound', segments=None):
        with closing(sqlite3.connect(self.qq)) as db:
            db.execute('INSERT INTO messages(ts,chat_id,user_id,text,replied,conversation_id,sender_id,'
                       'direction,channel_message_id,reply_to,segments,status) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ago(minutes), chat, uid, text, 0, 'conv', uid, direction, mid, None,
                        json.dumps(segments if segments is not None else [{'type': 'text', 'text': text}],
                                   ensure_ascii=False), 'observed'))
            db.commit()

    def theirs(self, minutes, text, mid='', uid='20000001'):
        self.add(minutes, text, mid=mid, uid=uid)

    def mine(self, minutes, text, mid=''):
        self.add(minutes, text, mid=mid, uid='bot', direction='outbound')

    def private(self, minutes, text, *, mid='', from_owner=True):
        self.add(minutes, text, chat=PRIVATE, mid=mid, uid=OWNER if from_owner else 'bot',
                 direction='inbound' if from_owner else 'outbound')

    def audit(self, kind):
        with closing(sqlite3.connect(self.root / 'data/qq/governance.db')) as db:
            return [str(row[0]) for row in db.execute('SELECT reason FROM audit WHERE kind=?', (kind,))]

    # 1
    def test_the_two_sources_are_one_timeline_in_real_time(self):
        self.theirs(30, '群里说的第一句', mid='g-1')
        self.private(20, '主人私聊里说的', mid='p-1')
        self.theirs(10, '群里说的第二句', mid='g-2', uid='20000002')
        value = recent_lines(self.root, kind='all', hours=6)
        print()
        for line in value['lines']:
            print('  [%s] %s | %s | %s' % (line['where'], line['ts'], line['who'], line['text']))
        self.assertEqual('all', value['kind'])
        self.assertEqual(6, value['hours'])
        self.assertEqual(['group:' + GROUP, 'private', 'group:' + GROUP],
                         [line['where'] for line in value['lines']])
        self.assertEqual([line['ts'] for line in value['lines']],
                         sorted(line['ts'] for line in value['lines']))
        self.assertEqual(['群名片甲', '主人', '群名片乙'],
                         [line['who'] for line in value['lines']])

    # 2
    def test_her_own_words_are_in_the_same_stream_and_named(self):
        self.mine(12, '我也说一句', mid='g-3')
        self.private(8, '私聊里我回的', mid='p-2', from_owner=False)
        value = recent_lines(self.root, kind='all', hours=6)
        self.assertEqual(['outbound', 'outbound'], [line['direction'] for line in value['lines']])
        self.assertEqual(['大肥鱼', '大肥鱼'], [line['who'] for line in value['lines']])

    # 3
    def test_a_picture_or_a_sticker_is_a_readable_line(self):
        self.add(9, '', mid='g-4', segments=[{'type': 'image', 'url': 'https://example.invalid/a.png'}])
        self.add(8, '', mid='g-5', segments=[{'type': 'face', 'id': '182'}])
        self.add(7, '看这个', mid='g-6', segments=[{'type': 'text', 'text': '看这个'},
                                                     {'type': 'image', 'url': 'https://example.invalid/b.png'}])
        value = recent_lines(self.root, kind='group', hours=6)
        self.assertEqual(['[图片]', '[表情]', '看这个 [图片]'], [line['text'] for line in value['lines']])

    # 4
    def test_a_group_window_reads_the_private_chat_and_only_records_it(self):
        self.private(15, '主人私聊里的周末安排', mid='p-3')
        value = self.host.execute('qq', self.group_session, 'fish_activity',
                                  {'kind': 'private'}, uuid.uuid4().hex)
        print()
        print('[group fish_activity kind=private]', json.dumps(value, ensure_ascii=False))
        self.assertEqual('private', value['kind'])
        self.assertEqual(['主人私聊里的周末安排'], [line['text'] for line in value['lines']])
        self.assertEqual(['private'], [line['where'] for line in value['lines']])
        # The read is an observation, not a violation: one row, with what was read.
        rows = self.audit('tool.activity')
        self.assertEqual(1, len(rows))
        self.assertIn('kind=private', rows[0])
        self.assertIn('hours=6', rows[0])
        self.assertIn('lines=1', rows[0])
        self.assertEqual([], self.audit('tool.scope.refused'))

    # 5
    def test_owner_and_group_both_read_the_same_table(self):
        deltas = [self.host.execute('qq', session, 'fish_stream', {}, uuid.uuid4().hex)
                  for session in (self.owner_session, self.group_session)]
        for value in deltas:
            self.assertEqual('all', value['kind'])
            self.assertEqual(6, value['hours'])
            self.assertIn('lines', value)
            self.assertIn('truncated', value)
        self.assertEqual(len(self.audit('tool.activity')), 2)

    # 6
    def test_the_limit_keeps_the_newest_lines_and_says_it_truncated(self):
        for minutes in (50, 40, 30, 20, 10):
            self.theirs(minutes, '第%s分钟' % minutes, mid='g-' + str(minutes))
        value = recent_lines(self.root, kind='group', hours=6, limit=2)
        self.assertEqual(['第20分钟', '第10分钟'], [line['text'] for line in value['lines']])
        self.assertTrue(value['truncated'])
        self.assertEqual(60, ACTIVITY_MAX_LINES)
        with self.assertRaises(ValueError) as caught:
            recent_lines(self.root, kind='group', hours=6, limit=ACTIVITY_MAX_LINES + 1)
        self.assertIn('limit', str(caught.exception))

    # 7
    def test_a_window_older_than_the_hours_asked_is_not_in_the_answer(self):
        self.theirs(600, '十小时前的一句', mid='g-old')
        self.theirs(5, '刚说的一句', mid='g-new')
        value = recent_lines(self.root, kind='group', hours=6)
        self.assertEqual(['刚说的一句'], [line['text'] for line in value['lines']])
        self.assertFalse(value['truncated'])

    # 8
    def test_the_workbench_action_returns_the_array_the_panel_reads(self):
        self.theirs(10, '群里的一句', mid='g-7')
        self.private(5, '私聊的一句', mid='p-4')
        panel = Workbench(self.root).dispatch('activity.recent', {'limit': 20})
        print()
        print('[workbench activity.recent]', json.dumps(panel, ensure_ascii=False))
        self.assertEqual({'group:' + GROUP, 'private'}, {row['where'] for row in panel['rows']})
        self.assertEqual(2, len(panel['rows']))
        for row in panel['rows']:
            self.assertLessEqual({'ts', 'where', 'who', 'direction', 'text'}, set(row))
        strung = Workbench(self.root).dispatch('activity.recent', {'limit': '20', 'kind': 'private'})
        self.assertEqual(['private'], [row['where'] for row in strung['rows']])


if __name__ == '__main__':
    unittest.main()
