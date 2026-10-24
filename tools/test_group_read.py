"""Read-only group history projection: authorisation, windows, paging, gaps."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from group_policy import GroupPolicy
import group_read
from group_read import authorized_groups, read_group
from social_context import SocialContext

GROUP = '123456'
CHAT = 'group:' + GROUP
OTHER = '999888'
OTHER_CHAT = 'group:' + OTHER
SPACE = 'abc123def456abc123def456abc123de'
MISSING_SPACE = 'fff000fff000fff000fff000fff000ff'
SECRET = 'LEAK-CANARY-9f3'
DDL = ('CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT, '
       'user_id TEXT, text TEXT, replied INTEGER DEFAULT 0, conversation_id TEXT, sender_id TEXT, '
       'direction TEXT, channel_message_id TEXT, reply_to TEXT, segments TEXT, status TEXT)')


def ago(minutes):
    return (datetime.now() - timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')


def connect(path, *, read_only=False):
    target = Path(path).as_uri() + '?mode=ro' if read_only else str(path)
    db = sqlite3.connect(target, uri=read_only)
    db.row_factory = sqlite3.Row
    return db


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class GroupReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'channels/fish_qq').mkdir(parents=True)
        self.write_config([GROUP, OTHER])
        self.qq = self.root / 'data/qq/qq.db'
        self.qq.parent.mkdir(parents=True)
        with closing(connect(self.qq)) as db:
            db.execute(DDL)
            db.commit()
        self.policy = GroupPolicy(self.root)
        self.write_roster()

    def write_config(self, groups):
        (self.root / 'channels/fish_qq/config.json').write_text(json.dumps(
            {'allowlist': {'owner_id': '111111', 'group_ids': groups}, 'bot': {'display_name': '大肥鱼'}}),
            encoding='utf-8')

    def write_roster(self, rows=(('20000001', '群名片甲', '昵称甲'), ('20000002', '', '昵称乙'))):
        path = self.root / 'data/social/people.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS members (group_id TEXT, user_id TEXT, nickname TEXT, '
                       'card TEXT, PRIMARY KEY (group_id, user_id))')
            for user_id, card, nickname in rows:
                db.execute('INSERT OR REPLACE INTO members VALUES(?,?,?,?)', (GROUP, user_id, nickname, card))
            db.commit()

    def add(self, minutes, text='内容', *, mid='', uid='20000001', direction='inbound', reply_to=None,
            status='observed', segments=None, chat=CHAT):
        with closing(connect(self.qq)) as db:
            cur = db.execute(
                'INSERT INTO messages(ts,chat_id,user_id,text,replied,conversation_id,sender_id,direction,'
                'channel_message_id,reply_to,segments,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (ago(minutes), chat, uid, text, 0, 'conv', uid, direction, mid, reply_to,
                 json.dumps(segments or [{'type': 'text', 'text': text}], ensure_ascii=False), status))
            db.commit()
            return int(cur.lastrowid)

    def grant(self, chat=CHAT, **updates):
        return self.policy.configure(chat, {'read': True, **updates})

    def observe(self, mid, sender, *, chat=CHAT, seq=1):
        with closing(connect(self.root / 'data/social/group-policy.db')) as db:
            db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?)',
                       (chat, seq, mid, '旁听时记下的内容',
                        json.dumps({'message_id': mid, 'sender': sender}, ensure_ascii=False), 0.0))
            db.commit()

    def digest(self):
        return SocialContext(self.root).runtime_digest('qq')

    def write_context(self, spaces, routed=None, digest=None):
        path = self.root / 'data/social/context.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = digest or self.digest()
        with closing(connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, role TEXT NOT NULL, '
                       'name TEXT NOT NULL, revision TEXT NOT NULL, session TEXT NOT NULL DEFAULT \'\', '
                       'origin TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS group_test_spaces (space TEXT PRIMARY KEY, '
                       'group_id TEXT NOT NULL, owner TEXT NOT NULL, runtime_digest TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS group_test_routes (group_id TEXT PRIMARY KEY, '
                       'space TEXT NOT NULL, owner TEXT NOT NULL, updated REAL NOT NULL)')
            for space in spaces:
                db.execute('INSERT OR REPLACE INTO spaces VALUES(?,?,?,?,?,?,?,?)',
                           (space, 'qq', '群测试', 'rev', '', 'group', 0.0, 0.0))
                db.execute('INSERT OR REPLACE INTO group_test_spaces VALUES(?,?,?,?)',
                           (space, GROUP, '111111', digest))
            if routed:
                db.execute('INSERT OR REPLACE INTO group_test_routes VALUES(?,?,?,?)',
                           (GROUP, routed, '111111', 0.0))
            db.commit()

    def write_space(self, space, rows, config=None):
        path = self.root / 'data/qq/spaces' / space / 'group-policy.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS observations (chat TEXT, seq INTEGER, id TEXT, '
                       'text TEXT, data TEXT, created REAL, PRIMARY KEY(chat,seq))')
            db.execute('CREATE TABLE IF NOT EXISTS groups (chat TEXT PRIMARY KEY, config TEXT)')
            for mid, name, seq in rows:
                db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?)',
                           (CHAT, seq, mid, '窗口里记下的内容',
                            json.dumps({'message_id': mid, 'sender': {'id': '20000001', 'name': name}},
                                       ensure_ascii=False), 0.0))
            if config is not None:
                db.execute('INSERT OR REPLACE INTO groups VALUES(?,?)',
                           (CHAT, json.dumps(config, ensure_ascii=False)))
            db.commit()

    def counts(self, path, table='messages'):
        with closing(connect(path, read_only=True)) as db:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def schema(self, path):
        with closing(connect(path, read_only=True)) as db:
            return [tuple(row) for row in db.execute('SELECT type,name FROM sqlite_master ORDER BY name')]

    def test_unauthorised_groups_are_refused_without_leaking(self):
        self.add(5, SECRET, mid='5001')
        with self.assertRaises(PermissionError) as no_row:
            read_group(self.root, GROUP)
        self.assertIn('群未获主人读取授权：' + CHAT, str(no_row.exception))
        self.assertNotIn(SECRET, str(no_row.exception))
        self.grant(read=False)
        with self.assertRaises(PermissionError):
            read_group(self.root, GROUP)
        self.write_config([])
        self.grant()
        with self.assertRaises(PermissionError):
            read_group(self.root, GROUP)
        self.assertEqual([], authorized_groups(self.root))

    def test_authorised_read_matches_sqlite(self):
        self.grant()
        self.add(6, '第一条', mid='9001')
        self.add(4, '第二条', mid='9002', uid='20000002',
                 segments=[{'type': 'image'}, {'type': 'text', 'text': '第二条'}])
        self.add(2, '机器人的话', mid='9003', uid='bot', direction='outbound', status='sent')
        result = read_group(self.root, GROUP)
        with closing(connect(self.qq, read_only=True)) as db:
            direct = db.execute('SELECT id,ts,channel_message_id,sender_id,text FROM messages '
                                'ORDER BY id').fetchall()
        self.assertEqual([(m['row_id'], m['time'], m['message_id'], m['sender_id'], m['text'])
                          for m in result['messages']],
                         [(r['id'], r['ts'], r['channel_message_id'], r['sender_id'], r['text']) for r in direct])
        self.assertEqual(['9001', '9002', '9003'], [m['message_id'] for m in result['messages']])
        self.assertEqual(['inbound', 'inbound', 'bot_outbound'], [m['direction'] for m in result['messages']])
        self.assertEqual(['text'], result['messages'][0]['attachment_types'])
        self.assertEqual(['image', 'text'], result['messages'][1]['attachment_types'])
        self.assertEqual('群名片甲', result['messages'][0]['sender_name'])
        self.assertEqual('昵称乙', result['messages'][1]['sender_name'])
        self.assertEqual('大肥鱼', result['messages'][2]['sender_name'])
        self.assertEqual('qqstore', result['messages'][0]['source'])
        self.assertEqual({'returned': 3, 'inbound': 2, 'bot_outbound': 1}, result['counts'])
        self.assertFalse(result['more'])
        self.assertEqual(result['messages'][0]['row_id'], result['next_before'])
        self.assertTrue(result['authorized'])

    def test_window_limit_and_paging(self):
        self.grant()
        self.add(300, '很久以前', mid='8000')
        rows = {}
        for index in range(8):
            rows['8' + str(index)] = self.add(28 - index, '第' + str(index) + '条', mid='8' + str(index))
        first = read_group(self.root, GROUP, limit=3)
        self.assertEqual(['85', '86', '87'], [m['message_id'] for m in first['messages']])
        self.assertTrue(first['more'])
        self.assertEqual(rows['85'], first['next_before'])
        second = read_group(self.root, GROUP, limit=3, before=first['next_before'])
        self.assertEqual(['82', '83', '84'], [m['message_id'] for m in second['messages']])
        self.assertTrue(second['more'])
        self.assertFalse({m['row_id'] for m in first['messages']} & {m['row_id'] for m in second['messages']})
        third = read_group(self.root, GROUP, limit=3, before=second['next_before'])
        self.assertEqual(['80', '81'], [m['message_id'] for m in third['messages']])
        self.assertFalse(third['more'])
        seen = [m['message_id'] for m in first['messages'] + second['messages'] + third['messages']]
        self.assertEqual(8, len(set(seen)))
        self.assertNotIn('8000', seen)

    def test_quoted_message_is_read_back_or_reported_missing(self):
        self.grant()
        quoted = self.add(9, '被引用的原话', mid='7001')
        self.add(8, '引用回复', mid='7002', reply_to='7001')
        result = read_group(self.root, GROUP, limit=1)
        self.assertEqual(['7002'], [m['message_id'] for m in result['messages']])
        self.assertEqual([quoted], [m['row_id'] for m in result['references']])
        self.assertEqual('被引用的原话', result['references'][0]['text'])
        self.add(7, '引用不存在的话', mid='7003', reply_to='6999')
        missing = read_group(self.root, GROUP, limit=1)
        self.assertEqual([], missing['references'])
        self.assertIn('引用的消息 6999 不在本地记录中', missing['gaps'])

    def test_empty_window_reports_gaps_without_raising(self):
        self.grant()
        result = read_group(self.root, GROUP, minutes=5)
        self.assertEqual([], result['messages'])
        self.assertEqual([], result['references'])
        self.assertFalse(result['more'])
        self.assertIsNone(result['next_before'])
        self.assertIn('该时间窗内没有记录到群消息', result['gaps'])
        self.assertIn('该群未开启旁听或学习，未监听时段的资料不完整', result['gaps'])

    def test_stores_are_unchanged_by_reading(self):
        self.grant()
        self.add(3, '只读检查', mid='6001')
        policy = self.root / 'data/social/group-policy.db'
        before = self.snapshot(self.qq) + self.snapshot(policy) + (self.policy_rows(),)
        result = read_group(self.root, GROUP, limit=50)
        after = self.snapshot(self.qq) + self.snapshot(policy) + (self.policy_rows(),)
        self.assertEqual(1, len(result['messages']))
        self.assertEqual(before, after)
        self.assertIn(('table', 'messages'), self.schema(self.qq))
        self.assertEqual(1, self.counts(self.qq))

    def snapshot(self, path):
        return (sha(path), self.counts(path, 'messages' if path.name == 'qq.db' else 'groups'),
                tuple(self.schema(path)))

    def policy_rows(self):
        with closing(connect(self.root / 'data/social/group-policy.db', read_only=True)) as db:
            return [tuple(row) for row in db.execute('SELECT chat,config,cursor FROM groups ORDER BY chat')]

    def test_read_is_independent_of_listen_and_reply(self):
        config = self.grant()
        self.assertFalse(config['listen'])
        self.assertFalse(config['reply'])
        listed = authorized_groups(self.root)
        self.assertEqual(1, len(listed))
        self.assertTrue(listed[0]['read'])
        self.assertFalse(listed[0]['listen'])
        self.assertFalse(listed[0]['reply'])
        self.assertEqual(0, listed[0]['messages_stored'])
        self.policy.configure(OTHER_CHAT, {'listen': True, 'reply': True, 'read': False})
        self.add(2, '其他群的消息', mid='5002', chat=OTHER_CHAT)
        self.add(1, '本群的消息', mid='5003')
        with self.assertRaises(PermissionError):
            read_group(self.root, OTHER)
        self.assertEqual(['5003'], [m['message_id'] for m in read_group(self.root, GROUP)['messages']])
        listed = authorized_groups(self.root)
        self.assertEqual([GROUP], [g['group_id'] for g in listed])
        self.assertEqual(1, listed[0]['messages_stored'])
        self.assertTrue(listed[0]['last_message_at'])

    def test_arguments_are_validated_before_any_read(self):
        self.grant()
        for group in ('12', 'abcdef', '1234567890123456', ''):
            with self.assertRaises(ValueError):
                read_group(self.root, group)
        for minutes in (0, 121, '30', 30.5):
            with self.assertRaises(ValueError):
                read_group(self.root, GROUP, minutes=minutes)
        for limit in (0, 51, '5'):
            with self.assertRaises(ValueError):
                read_group(self.root, GROUP, limit=limit)
        for before in (0, -3, '9'):
            with self.assertRaises(ValueError):
                read_group(self.root, GROUP, before=before)
        window = read_group(self.root, GROUP, minutes=45)['window']
        self.assertEqual(45, window['minutes'])
        self.assertLessEqual(window['from'], window['to'])

    def test_outbound_provenance_exposes_only_public_facts(self):
        self.grant()
        self.add(2, '机器人的话', mid='9003', uid='bot', direction='outbound', status='sent')
        state = self.root / 'channel-state'
        state.mkdir(parents=True)
        (state / 'outbox.jsonl').write_text(json.dumps({
            'id': 'ob-1', 'action': 'send_message',
            'payload': {'chat_id': CHAT, 'idempotency_key': 'owner-send:private-secret',
                        'segments': [{'type': 'text', 'text': '机器人的话'}]},
            'created_at': 100.0, 'expires_at': 700.0, 'status': 'confirmed', 'updated_at': 123.0,
            'origin_kind': 'group_agent', 'receipt': {'chat_id': CHAT, 'message_id': '9003'}},
            ensure_ascii=False) + '\n', encoding='utf-8')
        message = read_group(self.root, GROUP)['messages'][0]
        self.assertEqual({'origin_kind': 'group_agent', 'confirmed_at': 123.0,
                          'origin_description': '由本群聊天任务生成并发送'}, message['provenance'])
        self.assertNotIn('private-secret', json.dumps(message, ensure_ascii=False))

    def test_participation_follows_the_main_policy_without_a_window(self):
        self.grant()
        listed = authorized_groups(self.root)[0]
        self.assertEqual('main', listed['participation_source'])
        self.assertEqual((False, False, False), (listed['listen'], listed['learn'], listed['reply']))
        self.assertEqual((False, False, False),
                         (listed['main_listen'], listed['main_learn'], listed['main_reply']))
        self.assertIn('按主策略生效', listed['participation_note'])
        self.policy.configure(CHAT, {'listen': True, 'learn': True, 'reply': True})
        listed = authorized_groups(self.root)[0]
        self.assertEqual('main', listed['participation_source'])
        self.assertEqual((True, True, True), (listed['listen'], listed['learn'], listed['reply']))
        self.assertEqual((True, True, True),
                         (listed['main_listen'], listed['main_learn'], listed['main_reply']))

    def test_active_window_takes_over_listen_and_reply(self):
        self.grant()
        self.write_context([SPACE], routed=SPACE)
        self.write_space(SPACE, [], config={'listen': True, 'learn': True, 'reply': True,
                                            'reply_limits': False, 'mode': 'familiar'})
        listed = authorized_groups(self.root)[0]
        self.assertEqual('space:' + SPACE[:8], listed['participation_source'])
        self.assertEqual((True, True, True), (listed['listen'], listed['learn'], listed['reply']))
        self.assertEqual((False, False, False),
                         (listed['main_listen'], listed['main_learn'], listed['main_reply']))
        self.assertTrue(listed['read'])
        self.assertIn('群测试窗口 space:' + SPACE[:8], listed['participation_note'])
        self.assertIn('窗口结束后会回到主策略', listed['participation_note'])

    def test_window_values_are_reported_as_they_are(self):
        self.grant()
        self.write_context([SPACE], routed=SPACE)
        self.write_space(SPACE, [], config={'listen': True, 'learn': False, 'reply': False})
        listed = authorized_groups(self.root)[0]
        self.assertEqual((True, False, False), (listed['listen'], listed['learn'], listed['reply']))
        self.assertEqual('space:' + SPACE[:8], listed['participation_source'])

    def test_window_without_a_group_row_runs_on_defaults(self):
        self.grant(listen=True, learn=True, reply=True)
        self.write_context([SPACE], routed=SPACE)
        self.write_space(SPACE, [])
        listed = authorized_groups(self.root)[0]
        self.assertEqual('space:' + SPACE[:8], listed['participation_source'])
        self.assertEqual((False, False, False), (listed['listen'], listed['learn'], listed['reply']))
        self.assertEqual((True, True, True),
                         (listed['main_listen'], listed['main_learn'], listed['main_reply']))

    def test_stale_window_digest_falls_back_to_the_main_policy(self):
        self.grant()
        self.write_context([SPACE], routed=SPACE, digest='stale')
        self.write_space(SPACE, [], config={'listen': True, 'reply': True})
        listed = authorized_groups(self.root)[0]
        self.assertEqual('main', listed['participation_source'])
        self.assertEqual((False, False), (listed['listen'], listed['reply']))

    def test_window_digest_matches_social_context(self):
        self.assertEqual(self.digest(), group_read._runtime_digest(self.root))

    def test_a_window_cannot_revoke_the_read_authorisation(self):
        self.grant()
        self.write_context([SPACE], routed=SPACE)
        self.write_space(SPACE, [], config={'listen': True, 'reply': True, 'read': False})
        listed = authorized_groups(self.root)[0]
        self.assertTrue(listed['read'])
        self.add(2, '还能读到', mid='5101')
        self.assertEqual(['5101'], [m['message_id'] for m in read_group(self.root, GROUP)['messages']])
        self.policy.configure(CHAT, {'read': False})
        self.assertEqual([], authorized_groups(self.root))

    def test_listing_permissions_writes_no_store(self):
        self.grant()
        self.write_context([SPACE], routed=SPACE)
        self.write_space(SPACE, [], config={'listen': True, 'reply': True})
        paths = [self.root / 'data/social/group-policy.db', self.root / 'data/social/context.db',
                 self.root / 'data/qq/spaces' / SPACE / 'group-policy.db']
        before = [sha(path) for path in paths]
        listed = authorized_groups(self.root)
        self.assertEqual(before, [sha(path) for path in paths])
        self.assertEqual('space:' + SPACE[:8], listed[0]['participation_source'])

    def test_name_source_separates_observation_from_current_roster(self):
        self.grant()
        self.observe('3001', {'id': '20000001', 'card': '当时群名片丙'})
        self.add(5, '旁听时记下的', mid='3001')
        self.add(4, '只有名册能对上', mid='3002', uid='20000002')
        self.add(3, '机器人的话', mid='3003', uid='bot', direction='outbound', status='sent')
        self.add(2, '陌生人的话', mid='3004', uid='20009999')
        rows = {message['message_id']: message for message in read_group(self.root, GROUP)['messages']}
        self.assertEqual(('当时群名片丙', 'observed_at_message'),
                         (rows['3001']['sender_name'], rows['3001']['name_source']))
        self.assertEqual(('昵称乙', 'current_roster'),
                         (rows['3002']['sender_name'], rows['3002']['name_source']))
        self.assertEqual(('大肥鱼', 'bot_display'),
                         (rows['3003']['sender_name'], rows['3003']['name_source']))
        self.assertEqual(('', 'unknown'), (rows['3004']['sender_name'], rows['3004']['name_source']))

    def test_names_note_says_roster_names_are_not_the_old_nickname(self):
        self.grant()
        self.add(2, '只有名册的', mid='2001')
        result = read_group(self.root, GROUP)
        self.assertEqual('current_roster', result['messages'][0]['name_source'])
        note = result['names_note']
        self.assertIn('当前名册', note)
        self.assertIn('不保证是发消息当时的昵称', note)
        self.assertIn('没有任何旁听记录', note)
        self.observe('2002', {'id': '20000002', 'nickname': '当时昵称丁'})
        self.add(1, '旁听记下的', mid='2002', uid='20000002')
        observed = read_group(self.root, GROUP)
        self.assertIn('有 1 条旁听记录', observed['names_note'])
        self.assertNotIn('没有任何旁听记录', observed['names_note'])
        rows = {message['message_id']: message for message in observed['messages']}
        self.assertEqual('observed_at_message', rows['2002']['name_source'])
        self.assertEqual('current_roster', rows['2001']['name_source'])

    def test_snapshot_from_a_group_test_window_is_marked_as_such(self):
        self.grant()
        self.write_context([SPACE], routed=SPACE)
        self.write_space(SPACE, [('3001', '测试窗口名片', 1)])
        self.add(5, '测试窗口里的消息', mid='3001')
        result = read_group(self.root, GROUP)
        message = result['messages'][0]
        self.assertEqual(('测试窗口名片', 'observed_at_message', 'space:' + SPACE[:8]),
                         (message['sender_name'], message['name_source'], message['observed_from']))
        note = result['names_note']
        self.assertIn('群测试窗口', note)
        self.assertIn('space:' + SPACE[:8], note)
        self.assertIn('来自群测试窗口的旁听快照', note)
        self.assertNotIn('没有任何旁听记录', note)
        self.assertIn('该群消息来自活动的群测试窗口 space:' + SPACE[:8] + '，主策略未开启旁听', result['gaps'])

    def test_main_store_wins_and_is_marked_main(self):
        self.grant()
        self.observe('3101', {'id': '20000001', 'card': '主库名片'})
        self.write_context([SPACE], routed=SPACE)
        self.write_space(SPACE, [('3101', '测试窗口名片', 1)])
        self.add(4, '两处都记过的消息', mid='3101')
        message = read_group(self.root, GROUP)['messages'][0]
        self.assertEqual(('主库名片', 'observed_at_message', 'main'),
                         (message['sender_name'], message['name_source'], message['observed_from']))

    def test_without_any_snapshot_the_roster_warning_stays(self):
        self.grant()
        self.add(3, '只有名册的', mid='3201')
        result = read_group(self.root, GROUP)
        message = result['messages'][0]
        self.assertEqual(('current_roster', ''), (message['name_source'], message['observed_from']))
        self.assertIn('没有任何旁听记录', result['names_note'])
        self.assertIn('该群未开启旁听或学习，未监听时段的资料不完整', result['gaps'])

    def test_missing_or_corrupt_window_store_never_blocks_reading(self):
        self.grant()
        self.write_context([MISSING_SPACE, SPACE], routed=SPACE)
        broken = self.root / 'data/qq/spaces' / SPACE / 'group-policy.db'
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_bytes(b'this is not a database')
        self.add(2, '仍然要能读到', mid='3301')
        result = read_group(self.root, GROUP)
        self.assertEqual(['3301'], [m['message_id'] for m in result['messages']])
        self.assertEqual('仍然要能读到', result['messages'][0]['text'])
        self.assertEqual(('current_roster', ''),
                         (result['messages'][0]['name_source'], result['messages'][0]['observed_from']))
        self.assertIn('没有任何旁听记录', result['names_note'])

    def test_reading_writes_no_store(self):
        self.grant()
        self.write_context([SPACE], routed=SPACE)
        self.write_space(SPACE, [('3401', '窗口名片', 1)])
        self.add(1, '只读检查', mid='3401')
        paths = [self.root / 'data/social/group-policy.db', self.root / 'data/social/context.db',
                 self.root / 'data/qq/spaces' / SPACE / 'group-policy.db']
        before = [sha(path) for path in paths]
        result = read_group(self.root, GROUP)
        self.assertEqual(before, [sha(path) for path in paths])
        self.assertEqual('space:' + SPACE[:8], result['messages'][0]['observed_from'])

    def test_unknown_sender_is_reported_not_invented(self):
        self.grant()
        self.add(2, '陌生人说话', mid='4001', uid='20009999')
        result = read_group(self.root, GROUP)
        self.assertEqual('', result['messages'][0]['sender_name'])
        self.assertEqual('20009999', result['messages'][0]['sender_id'])
        self.assertIn('本地没有该发送者的名称记录：20009999', result['gaps'])

    def test_coverage_reports_the_real_extent_of_the_batch(self):
        self.grant()
        self.add(41, '最早一句', mid='7101')
        self.add(21, '中间一句', mid='7102')
        self.add(1, '最后一句', mid='7103')
        result = read_group(self.root, GROUP, minutes=120)
        coverage = result['coverage']
        self.assertEqual(120, coverage['requested_minutes'])
        self.assertEqual(result['messages'][0]['time'], coverage['first_message_time'])
        self.assertEqual(result['messages'][-1]['time'], coverage['last_message_time'])
        self.assertEqual(40, coverage['span_minutes'])
        self.assertEqual('none', coverage['source'])
        self.assertFalse(coverage['complete'])
        self.assertEqual(len(result['gaps']), coverage['gaps_count'])
        self.assertIn('未监听时段没有记录', coverage['reason'])

    def test_coverage_note_uses_real_times_and_no_duration_words(self):
        self.grant()
        self.add(41, '最早一句', mid='7201')
        self.add(21, '中间一句', mid='7202')
        self.add(1, '最后一句', mid='7203')
        result = read_group(self.root, GROUP, minutes=120)
        note = result['coverage_note']
        coverage = result['coverage']
        self.assertIn(coverage['first_message_time'], note)
        self.assertIn(coverage['last_message_time'], note)
        self.assertIn('40 分钟', note)
        self.assertIn('缺口', note)
        self.assertIn('不完整', note)
        self.assertNotIn('一小时', note)
        self.assertNotIn('两小时', note)
        self.assertLessEqual(len(note), 200)

    def test_coverage_of_an_empty_window_is_zero_span_and_says_so(self):
        self.grant()
        result = read_group(self.root, GROUP, minutes=60)
        coverage = result['coverage']
        self.assertEqual([], result['messages'])
        self.assertEqual(60, coverage['requested_minutes'])
        self.assertEqual('', coverage['first_message_time'])
        self.assertEqual('', coverage['last_message_time'])
        self.assertEqual(0, coverage['span_minutes'])
        self.assertEqual('none', coverage['source'])
        self.assertFalse(coverage['complete'])
        self.assertGreaterEqual(coverage['gaps_count'], 1)
        self.assertIn('该窗口内没有记录', result['coverage_note'])
        self.assertNotIn('一小时', result['coverage_note'])

    def test_coverage_names_the_window_that_really_collected(self):
        self.grant()
        self.write_context([SPACE], routed=SPACE)
        self.write_space(SPACE, [('7301', '窗口名片', 1)])
        self.add(1, '窗口里记下的话', mid='7301')
        coverage = read_group(self.root, GROUP)['coverage']
        self.assertEqual('space:' + SPACE[:8], coverage['source'])
        self.assertFalse(coverage['complete'])
        self.assertIn('群测试窗口 space:' + SPACE[:8], coverage['reason'])


if __name__ == '__main__':
    unittest.main()
