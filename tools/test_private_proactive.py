"""想说没说：一天一次、夜间不提、主人不在不提的主动私聊。

全是可丢的东西：临时根目录、假模型、假时钟、只记账的投递口。这里要回答的问题是
一条「想说还没说」的念头真的会变成一条主人私聊吗，一天是不是最多一条（重启之后
还是不是），夜里和主人两小时没说话的时候她是不是真的闭嘴。
"""
import asyncio
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]

import private_proactive
from memory_store import MemoryStore
from model_gateway import ChatResult
from storage import database, write_json

OWNER = '10001'
SHANGHAI = timezone(timedelta(hours=8))
SAID = '主人，你今天是不是很累呀。'
THOUGHT = '想问主人今天是不是很累'


class FakeGateway:
    """Scripted model: never opens a connection, records what it was sent."""

    base = 'https://fake.invalid'
    model = 'fake-model'

    def __init__(self, replies=None, error=None):
        self.replies = list(replies or [])
        self.error = error
        self.requests = []

    def chat(self, messages, **kwargs):
        self.requests.append(messages)
        if self.error is not None:
            raise self.error
        if not self.replies:
            raise AssertionError('假模型被调用的次数超过本用例提供的回复')
        return ChatResult(self.replies.pop(0), {}, 'stop')


class FakeDeliver:
    """Records every delivery attempt and returns a scripted receipt."""

    def __init__(self, receipt=None, error=None):
        self.calls = []
        self.receipt = receipt if receipt is not None else {'ok': True, 'payload': {'message_id': 'mid-1'}}
        self.error = error

    def __call__(self, *, chat_id, text, request_id, row):
        self.calls.append({'chat_id': chat_id, 'text': text, 'request_id': request_id,
                           'thought_id': row.get('id')})
        if self.error is not None:
            raise self.error
        return dict(self.receipt)


class FakeChannel:
    """The existing QQ channel's action(), with a confirmed receipt."""

    def __init__(self):
        self.sent = []

    async def action(self, name, params, timeout=None):
        self.sent.append(dict(params))
        return {'ok': True, 'payload': {'message_id': 'mid-' + uuid.uuid4().hex[:6]}}


class FakeGovernance:
    def __init__(self):
        self.audits = []

    def config(self):
        return {'paused': False, 'send_policy': {'owner_private': {'owner_reply': True}}}

    def check_output(self, text):
        return None

    def audit(self, kind, reason, text=''):
        self.audits.append((kind, reason))


class FakeLoop:
    def __init__(self, root):
        self.root = root
        self.owner = OWNER
        self.channel = FakeChannel()
        self.governance = FakeGovernance()


def rows(root):
    """The one-a-day ledger, as it sits on disk."""
    with database(private_proactive.db_path(root)) as db:
        try:
            return [dict(row) for row in db.execute(
                'SELECT * FROM private_proactive ORDER BY day').fetchall()]
        except sqlite3.OperationalError:  # nothing has been written yet
            return []


def db_files(root):
    return {path.name for path in (Path(root) / 'data/qq').iterdir() if path.suffix == '.db'}


class PrivateProactiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_json(self.root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'model': 'fake-model', 'base_url': 'https://fake.invalid',
                        'apiKeyEnv': 'FISH_TEST_QQ_KEY'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.root / 'channels/fish_qq/config.json', {
            'allowlist': {'owner_id': OWNER, 'group_ids': ['1043230169']},
            'bot': {'display_name': '大肥鱼'}})
        self.store = MemoryStore(self.root, 'qq')
        # 开发机自己的环境变量不许混进用例里。
        environ = patch.dict(os.environ, {'FISH_PRIVATE_PROACTIVE': ''}, clear=False)
        environ.start()
        self.addCleanup(environ.stop)
        self.audits = []

    # -- 布景 ---------------------------------------------------------------
    def daytime(self, minutes=0):
        """一个白天窗口里的假时刻：2026-10-20 15:00 上海时间，可以往后挪。"""
        moment = datetime(2026, 10, 20, 15, 0, tzinfo=SHANGHAI).timestamp()
        return moment + minutes * 60

    def thought(self, text=THOUGHT):
        return self.store.remember_social('thought', text, scope='owner', source='owner-test')

    def activity(self, moment):
        """主人真的在私聊里说过话：写进既有的 qq.db，时间戳由假时钟定。"""
        path = self.root / 'data/qq/qq.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'ts TEXT, chat_id TEXT, user_id TEXT, text TEXT, replied INTEGER DEFAULT 0, '
                       'direction TEXT)')
            db.execute('INSERT INTO messages(ts,chat_id,user_id,text,replied,direction) '
                       'VALUES(?,?,?,?,0,?)',
                       (datetime.fromtimestamp(moment).strftime('%Y-%m-%d %H:%M:%S'),
                        'private:' + OWNER, OWNER, '我回来了', 'inbound'))
            db.commit()

    def run_tick(self, deliver, gateway=None, now=None, root=None):
        return private_proactive.tick(
            now=now if now is not None else self.daytime(), deliver=deliver, gateway=gateway,
            root=root or self.root, owner=OWNER,
            audit=lambda kind, reason: self.audits.append((kind, reason)))

    # 1
    def test_the_first_satisfied_day_says_it_once_and_records_it(self):
        thought = self.thought()
        now = self.daytime()
        self.activity(now - 300)
        deliver, gateway = FakeDeliver(), FakeGateway([SAID])
        report = self.run_tick(deliver, gateway, now=now)
        self.assertEqual(1, len(deliver.calls))
        self.assertEqual(SAID, deliver.calls[0]['text'])
        self.assertEqual('private:' + OWNER, deliver.calls[0]['chat_id'])
        self.assertEqual('private-thought:2026-10-20:' + thought['id'], deliver.calls[0]['request_id'])
        self.assertEqual(('sent', 'model'), (report['state'], report['source']))
        self.assertEqual(1, len(gateway.requests))
        self.assertIn(THOUGHT, str(gateway.requests[0][-1]))
        ledger = rows(self.root)
        self.assertEqual(1, len(ledger))
        self.assertEqual(('2026-10-20', thought['id'], 'sent'),
                         (ledger[0]['day'], ledger[0]['thought_id'], ledger[0]['state']))
        self.assertEqual(THOUGHT[:20], ledger[0]['excerpt'])
        kinds = [kind for kind, _reason in self.audits]
        self.assertEqual(['private_thought_proactive'], kinds)
        self.assertIn(THOUGHT[:20], self.audits[0][1])
        # 状态只落在既有的 memory.db 里，没有为这套主动新建库文件。
        self.assertIn('memory.db', db_files(self.root))
        self.assertNotIn('private_proactive.db', db_files(self.root))

    # 2
    def test_the_second_time_of_the_same_day_is_silent_with_a_reason(self):
        self.thought()
        now = self.daytime()
        self.activity(now - 60)
        first = FakeDeliver()
        self.assertEqual('sent', self.run_tick(first, FakeGateway([SAID]), now=now)['state'])
        self.assertEqual(1, len(first.calls))
        again, gateway = FakeDeliver(), FakeGateway(['不该被调用。'])
        report = self.run_tick(again, gateway, now=now + 600)
        self.assertEqual([], again.calls)
        self.assertEqual([], gateway.requests)
        self.assertEqual('silent', report['action'])
        self.assertIn('今天已经主动提过一次', report['reason'])
        self.assertEqual('private_thought_proactive_skip', self.audits[-1][0])
        self.assertIn('今天已经主动提过一次', self.audits[-1][1])
        self.assertEqual(1, len(rows(self.root)))

    # 3
    def test_the_daily_count_survives_a_restart(self):
        thought = self.thought()
        now = self.daytime()
        self.activity(now - 60)
        self.assertEqual('sent', self.run_tick(FakeDeliver(), FakeGateway([SAID]), now=now)['state'])
        restarted = importlib.reload(private_proactive)
        after, gateway = FakeDeliver(), FakeGateway(['不该被调用。'])
        report = restarted.tick(now=now + 3600, deliver=after, gateway=gateway,
                               root=self.root, owner=OWNER)
        self.assertEqual([], after.calls)
        self.assertEqual([], gateway.requests)
        self.assertIn('今天已经主动提过一次', report['reason'])
        self.assertEqual(thought['id'], rows(self.root)[0]['thought_id'])
        # 隔天就重新有一次机会（主人隔天也要真的说过话）。
        self.activity(self.daytime(minutes=24 * 60) - 60)
        tomorrow = restarted.tick(now=self.daytime(minutes=24 * 60), deliver=FakeDeliver(),
                                  gateway=FakeGateway([SAID]), root=self.root, owner=OWNER)
        self.assertEqual('sent', tomorrow['state'])
        self.assertEqual(['2026-10-20', '2026-10-21'], [row['day'] for row in rows(self.root)])

    # 4
    def test_the_night_window_stays_silent(self):
        now = datetime(2026, 10, 20, 0, 30, tzinfo=SHANGHAI).timestamp()
        self.thought()
        self.activity(now - 120)
        deliver, gateway = FakeDeliver(), FakeGateway(['不该被调用。'])
        report = self.run_tick(deliver, gateway, now=now)
        self.assertEqual([], deliver.calls)
        self.assertEqual([], gateway.requests)
        self.assertIn('夜间', report['reason'])
        self.assertEqual([], rows(self.root))
        self.assertEqual('private_thought_proactive_skip', self.audits[-1][0])
        # 边界：22:29 与 07:29 都算夜里，07:30 与 23:30 各按窗口的开口处算。
        at = lambda h, m: datetime(2026, 10, 20, h, m, tzinfo=SHANGHAI)
        self.assertTrue(private_proactive.in_night(at(23, 30).timestamp()))
        self.assertFalse(private_proactive.in_night(at(23, 29).timestamp()))
        self.assertTrue(private_proactive.in_night(at(7, 29).timestamp()))
        self.assertFalse(private_proactive.in_night(at(7, 30).timestamp()))

    # 5
    def test_two_hours_without_the_owner_is_silent(self):
        now = self.daytime()
        self.thought()
        self.activity(now - 3 * 3600)
        deliver, gateway = FakeDeliver(), FakeGateway(['不该被调用。'])
        report = self.run_tick(deliver, gateway, now=now)
        self.assertEqual([], deliver.calls)
        self.assertIn('两小时', report['reason'])
        self.assertEqual([], rows(self.root))
        # 两小时以内就是活的：窗口内说过话，她就开口。
        self.activity(now - 60)
        speak = FakeDeliver()
        self.assertEqual('sent', self.run_tick(speak, FakeGateway([SAID]), now=now)['state'])
        self.assertEqual(1, len(speak.calls))

    # 6
    def test_no_thought_no_send(self):
        now = self.daytime()
        self.activity(now - 60)
        deliver, gateway = FakeDeliver(), FakeGateway(['不该被调用。'])
        report = self.run_tick(deliver, gateway, now=now)
        self.assertEqual([], deliver.calls)
        self.assertIn('没有想说还没说的话', report['reason'])
        self.assertEqual([], rows(self.root))

    # 7
    def test_an_expired_thought_is_never_raised(self):
        thought = self.thought()
        with database(private_proactive.db_path(self.root)) as db:
            db.execute('UPDATE memories SET expires=? WHERE id=?',
                       (self.daytime() - 60, thought['id']))
        now = self.daytime()
        self.activity(now - 60)
        deliver = FakeDeliver()
        report = self.run_tick(deliver, FakeGateway(['不该被调用。']), now=now)
        self.assertEqual([], deliver.calls)
        self.assertIn('没有想说还没说的话', report['reason'])

    # 8
    def test_an_unknown_receipt_is_never_resent_the_same_day(self):
        thought = self.thought()
        now = self.daytime()
        self.activity(now - 60)
        unknown = FakeDeliver({'ok': False, 'error': {'code': '回执超时', 'may_have_sent': True}})
        report = self.run_tick(unknown, FakeGateway([SAID]), now=now)
        self.assertEqual('failed', report['action'])
        self.assertEqual('uncertain', rows(self.root)[0]['state'])
        again = FakeDeliver()
        second = self.run_tick(again, FakeGateway(['不该被调用。']), now=now + 300)
        self.assertEqual([], again.calls)
        self.assertIn('今天已经主动提过一次', second['reason'])
        self.assertEqual(thought['id'], rows(self.root)[0]['thought_id'])

    # 9
    def test_the_maintenance_hook_sends_through_the_existing_private_path(self):
        thought = self.thought()
        now = self.daytime()
        self.activity(now - 60)
        loop = FakeLoop(self.root)
        with patch('model_gateway.ModelGateway', lambda *args, **kwargs: FakeGateway([SAID])):
            report = asyncio.run(private_proactive.heartbeat(loop, now=now))
        self.assertEqual('sent', report['state'])
        self.assertEqual(1, len(loop.channel.sent))
        self.assertEqual('private:' + OWNER, loop.channel.sent[0]['chat_id'])
        self.assertIn('2026-10-20', loop.channel.sent[0]['idempotency_key'])
        self.assertEqual('sent', rows(self.root)[0]['state'])
        self.assertEqual(['private_thought_proactive'], [kind for kind, _ in loop.governance.audits])
        self.assertIn(thought['id'], rows(self.root)[0]['thought_id'])

    # 10
    def test_the_switch_turns_the_whole_thing_off(self):
        self.thought()
        now = self.daytime()
        self.activity(now - 60)
        loop = FakeLoop(self.root)
        with patch.dict(os.environ, {'FISH_PRIVATE_PROACTIVE': '0'}):
            self.assertEqual(None, asyncio.run(private_proactive.heartbeat(loop, now=now)))
        self.assertEqual([], loop.channel.sent)
        self.assertEqual([], rows(self.root))


if __name__ == '__main__':
    unittest.main()
