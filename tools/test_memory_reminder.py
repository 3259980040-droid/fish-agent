"""Due-event reminders: the trigger, the ledger, and the restart guarantee.

Everything here is disposable: a temporary root, a scripted model, and a
delivery hook that only records. The questions these tests answer are whether a
due event really becomes one owner private message, whether that message is
produced by the same decision core (fish_chat.respond) and not by a second
brain, and whether a restart or an unknown receipt can make it speak twice.
"""
import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import memory_reminder
from memory_reminder import compose, db_path, heartbeat, reset, tick
from memory_store import MemoryStore
from model_gateway import ChatResult
from storage import database, write_json

OWNER = '10001'


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
                           'memory_id': row.get('id')})
        if self.error is not None:
            raise self.error
        return dict(self.receipt)


class StubStore:
    """A memory store that offers one row regardless of its action."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.asked = []

    def due_events(self, scope, now=None, limit=10):
        return [dict(row) for row in self.rows]

    def mark_asked(self, memory_id, *, scope, at=None):
        self.asked.append(memory_id)
        return {'ok': True, 'id': memory_id, 'asked': 1}


class FakeChannel:
    """The existing QQ channel's action() with a confirmed receipt."""

    def __init__(self, receipt=None):
        self.sent = []
        self.receipt = receipt

    async def action(self, name, params, timeout=None):
        self.sent.append(dict(params))
        return dict(self.receipt or {'ok': True, 'payload': {'message_id': 'mid-' + uuid.uuid4().hex[:6]}})


class FakeGovernance:
    def __init__(self, allowed=True):
        self.allowed = allowed

    def config(self):
        return {'paused': False, 'send_policy': {'owner_private': {'owner_reply': self.allowed}}}

    def check_output(self, text):
        return None


class FakeLoop:
    def __init__(self, root, channel, *, allowed=True):
        self.root = root
        self.channel = channel
        self.owner = OWNER
        self.governance = FakeGovernance(allowed)


def ledger(root):
    with database(db_path(root)) as db:
        try:
            rows = db.execute('SELECT * FROM memory_reminders ORDER BY memory_id,due').fetchall()
        except sqlite3.OperationalError:  # no tick has written the ledger yet
            return []
    return [dict(row) for row in rows]


def asked(root, memory_id):
    with database(db_path(root)) as db:
        return int(db.execute('SELECT asked FROM memories WHERE id=?', (memory_id,)).fetchone()[0])


class ReminderTests(unittest.TestCase):
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
        # The developer machine may carry a real switch or waterline in its own
        # environment; a test must never inherit either.
        environ = patch.dict(os.environ, {'FISH_MEMORY_REMINDER': '',
                                          'FISH_MEMORY_REMINDER_SINCE': '',
                                          'FISH_MEMORY_REMINDER_STALE_HOURS': ''}, clear=False)
        environ.start()
        self.addCleanup(environ.stop)

    def event(self, text='主人今天要交作业。', *, minutes=-5, **kwargs):
        due = time.time() + minutes * 60
        saved = self.store.save(text, category='约定', scope='owner', source='owner-test',
                                source_text='主人说过这件事', kind='event', due=due,
                                evidence='owner_quote', **kwargs)
        return saved['id'], due

    def run_tick(self, deliver, gateway=None, **kwargs):
        return tick(deliver=deliver, gateway=gateway, root=self.root, owner=OWNER, **kwargs)

    # 1
    def test_a_future_event_is_not_reminded(self):
        mid, _ = self.event(minutes=30)
        deliver = FakeDeliver()
        report = self.run_tick(deliver, FakeGateway(['还没到时间。']))
        self.assertEqual([], deliver.calls)
        self.assertEqual(0, report['candidates'])
        self.assertEqual(0, asked(self.root, mid))
        self.assertEqual([], ledger(self.root))

    # 2
    def test_a_due_event_becomes_one_owner_private_reminder(self):
        mid, due = self.event()
        deliver = FakeDeliver()
        gateway = FakeGateway(['主人，作业该交了。'])
        report = self.run_tick(deliver, gateway)
        self.assertEqual(1, len(deliver.calls))
        call = deliver.calls[0]
        self.assertEqual('主人，作业该交了。', call['text'])
        self.assertEqual('private:' + OWNER, call['chat_id'])
        self.assertIn(mid, call['request_id'])
        self.assertIn(str(int(due)), call['request_id'])
        self.assertEqual([mid], report['sent'])
        self.assertEqual('model', report['source'][mid])
        self.assertEqual(1, len(gateway.requests))
        self.assertIn('作业', json.dumps(gateway.requests[0][-1], ensure_ascii=False))

    # 3
    def test_a_sent_reminder_marks_asked_and_is_never_repeated(self):
        mid, due = self.event()
        deliver = FakeDeliver()
        self.run_tick(deliver, FakeGateway(['主人，作业该交了。']))
        self.assertEqual(1, asked(self.root, mid))
        rows = ledger(self.root)
        self.assertEqual(1, len(rows))
        self.assertEqual('sent', rows[0]['state'])
        self.assertEqual(mid, rows[0]['memory_id'])
        self.assertEqual(due, rows[0]['due'])
        again = FakeDeliver()
        report = self.run_tick(again, FakeGateway(['不该被调用。']))
        self.assertEqual([], again.calls)
        self.assertEqual([], report['sent'])
        self.assertEqual(1, asked(self.root, mid))

    # 4
    def test_completed_and_cancelled_events_are_not_reminded(self):
        done, _ = self.event('体检已经做完了。')
        cancelled, _ = self.event('这件事取消了。', minutes=-6)
        with database(db_path(self.root)) as db:
            db.execute("UPDATE memories SET action='completed' WHERE id=?", (done,))
            db.execute("UPDATE memories SET action='cancelled' WHERE id=?", (cancelled,))
        deliver = FakeDeliver()
        report = self.run_tick(deliver, FakeGateway(['不该被调用。']))
        self.assertEqual([], deliver.calls)
        self.assertEqual(0, report['candidates'])
        self.assertEqual(0, asked(self.root, done))
        self.assertEqual([], ledger(self.root))

    # 4b
    def test_a_closed_row_offered_anyway_is_skipped_not_reminded(self):
        mid, due = self.event()
        store = StubStore([{'id': mid, 'due': due, 'text': '主人今天要交作业。',
                            'asked': 0, 'action': 'cancelled'}])
        deliver = FakeDeliver()
        with patch.object(memory_reminder, 'MemoryStore', lambda *args, **kwargs: store):
            report = tick(deliver=deliver, gateway=FakeGateway(['不该被调用。']),
                          root=self.root, owner=OWNER)
        self.assertEqual([], deliver.calls)
        self.assertEqual([mid], report['skipped'])
        self.assertEqual([], store.asked)
        self.assertEqual('skipped', ledger(self.root)[0]['state'])
        # A human reset must not smuggle a closed event back into the queue.
        reset(root=self.root)
        with patch.object(memory_reminder, 'MemoryStore', lambda *args, **kwargs: store):
            tick(deliver=FakeDeliver(), gateway=FakeGateway(['不该被调用。']),
                 root=self.root, owner=OWNER)
        self.assertEqual([], store.asked)

    # 5
    def test_an_uncertain_receipt_is_never_resent(self):
        mid, _ = self.event()
        uncertain = FakeDeliver({'ok': False, 'payload': {},
                                 'error': {'code': '回执超时', 'may_have_sent': True}})
        report = self.run_tick(uncertain, FakeGateway(['主人，作业该交了。']))
        self.assertEqual([mid], report['uncertain'])
        self.assertEqual(0, asked(self.root, mid))
        self.assertEqual('uncertain', ledger(self.root)[0]['state'])
        again = FakeDeliver()
        second = self.run_tick(again, FakeGateway(['不该被调用。']))
        self.assertEqual([], again.calls)
        self.assertEqual([], second['sent'])
        self.assertEqual('uncertain', ledger(self.root)[0]['state'])
        # Only an explicit human reset lets it be tried again.
        self.assertEqual(1, reset(memory_id=mid, root=self.root))
        third = FakeDeliver()
        self.run_tick(third, FakeGateway(['主人，作业该交了。']))
        self.assertEqual(1, len(third.calls))
        self.assertEqual(1, asked(self.root, mid))

    # 6
    def test_a_restart_over_the_same_database_does_not_repeat(self):
        mid, _ = self.event()
        first = FakeDeliver()
        self.run_tick(first, FakeGateway(['主人，作业该交了。']))
        self.assertEqual(1, len(first.calls))
        restarted = MemoryStore(self.root, 'qq')
        # asked=1 keeps the event due for the decision core; the ledger is what
        # stops a second process from speaking about it again.
        self.assertEqual([mid], [row['id'] for row in restarted.due_events('owner')])
        second = FakeDeliver()
        self.run_tick(second, FakeGateway(['不该被调用。']))
        self.assertEqual([], second.calls)
        self.assertEqual(1, len(ledger(self.root)))
        self.assertEqual(1, asked(self.root, mid))

    # 7
    def test_a_model_failure_falls_back_to_the_stored_facts(self):
        mid, due = self.event(minutes=-7)
        deliver = FakeDeliver()
        report = self.run_tick(deliver, FakeGateway(error=RuntimeError('模型不可用')))
        self.assertEqual(1, len(deliver.calls))
        self.assertEqual(memory_reminder.template({'text': '主人今天要交作业。', 'due': due}),
                         deliver.calls[0]['text'])
        self.assertIn('主人今天要交作业。', deliver.calls[0]['text'])
        self.assertEqual('template', report['source'][mid])
        self.assertEqual('sent', ledger(self.root)[0]['state'])
        # A model failure alone never writes 'sent' when the send itself failed.
        other, _ = self.event('另一件事。', minutes=-8)
        unknown = FakeDeliver({'ok': False, 'error': {'code': '回执未取得', 'may_have_sent': True}})
        self.run_tick(unknown, FakeGateway(error=RuntimeError('模型不可用')))
        states = {row['memory_id']: row['state'] for row in ledger(self.root)}
        self.assertEqual('uncertain', states[other])
        self.assertEqual(0, asked(self.root, other))

    # 8
    def test_one_tick_raises_at_most_two_reminders(self):
        ids = [self.event('第%d件事。' % index, minutes=-index - 1)[0] for index in range(5)]
        first = FakeDeliver()
        report = self.run_tick(first, FakeGateway(['第一句。', '第二句。', '不该到第三句。']))
        self.assertEqual(2, len(first.calls))
        self.assertEqual(2, len(report['sent']))
        self.assertEqual(3, len(report['deferred']))
        second = FakeDeliver()
        self.run_tick(second, FakeGateway(['第三句。', '第四句。']))
        self.assertEqual(2, len(second.calls))
        third = FakeDeliver()
        self.run_tick(third, FakeGateway(['第五句。']))
        self.assertEqual(1, len(third.calls))
        self.assertEqual(sorted(ids), sorted(row['memory_id'] for row in ledger(self.root)))
        self.assertEqual({1}, {asked(self.root, mid) for mid in ids})

    # 9
    def test_the_module_has_no_second_brain_and_no_second_database(self):
        source = Path(memory_reminder.__file__).read_text(encoding='utf-8')
        for banned in ('sqlite3', 'runtime_adapter', 'RuntimeManager', 'from core import',
                       'HarnessConfig', 'dsh_delegation'):
            self.assertNotIn(banned, source)
        self.assertEqual(self.root / 'data/qq/memory.db', db_path(self.root))
        self.assertTrue((self.root / 'data/qq/memory.db').is_file())

    # 10
    def test_the_composed_sentence_comes_from_the_shared_decision_core(self):
        mid, due = self.event()
        gateway = FakeGateway(['主人，作业该交了。'])
        text, source = compose(self.root, {'id': mid, 'due': due, 'text': '主人今天要交作业。'},
                               gateway=gateway)
        self.assertEqual(('主人，作业该交了。', 'model'), (text, source))
        sent = gateway.requests[0]
        self.assertEqual('system', sent[0]['role'])
        self.assertIn('主人今天要交作业。', json.dumps(sent[-1], ensure_ascii=False))

    # 11
    def test_the_maintenance_hook_is_off_until_the_switch_is_on(self):
        mid, _ = self.event()
        channel = FakeChannel()
        loop = FakeLoop(self.root, channel)
        self.assertEqual(None, asyncio.run(heartbeat(loop)))
        self.assertEqual([], channel.sent)
        self.assertEqual([], ledger(self.root))
        with patch.dict(os.environ, {'FISH_MEMORY_REMINDER': '1'}), \
                patch('model_gateway.ModelGateway',
                      lambda *args, **kwargs: FakeGateway(['主人，作业该交了。'])):
            asyncio.run(heartbeat(loop))
        self.assertEqual(1, len(channel.sent))
        self.assertEqual('private:' + OWNER, channel.sent[0]['chat_id'])
        self.assertIn(mid, channel.sent[0]['idempotency_key'])
        self.assertEqual('sent', ledger(self.root)[0]['state'])
        self.assertEqual(1, asked(self.root, mid))

    # 12
    def test_the_maintenance_hook_respects_the_owner_private_send_gate(self):
        self.event()
        closed = FakeChannel()
        loop = FakeLoop(self.root, closed, allowed=False)
        with patch.dict(os.environ, {'FISH_MEMORY_REMINDER': '1'}), \
                patch('model_gateway.ModelGateway',
                      lambda *args, **kwargs: FakeGateway(['主人，作业该交了。'])):
            asyncio.run(heartbeat(loop))
        self.assertEqual([], closed.sent)
        self.assertEqual(['failed'], [row['state'] for row in ledger(self.root)])

    # 13
    def test_a_stale_backlog_is_retired_instead_of_flooding_the_owner(self):
        old, _ = self.event('五天前就该做的事。', minutes=-5 * 24 * 60)
        now_due, _ = self.event('一小时前就该做的事。', minutes=-60)
        future, _ = self.event('一小时后的事。', minutes=60)
        deliver = FakeDeliver()
        report = self.run_tick(deliver, FakeGateway(['主人，一小时前那件事该做了。']))
        # Exactly one message: the stale one is retired, the future one is not due.
        self.assertEqual([now_due], [call['memory_id'] for call in deliver.calls])
        self.assertEqual('private:' + OWNER, deliver.calls[0]['chat_id'])
        self.assertTrue(deliver.calls[0]['chat_id'].startswith('private:'))
        self.assertNotIn(future, [call['memory_id'] for call in deliver.calls])
        self.assertEqual([old], report['stale'])
        states = {row['memory_id']: row['state'] for row in ledger(self.root)}
        receipts = {row['memory_id']: row['receipt'] for row in ledger(self.root)}
        self.assertEqual('skipped', states[old])
        self.assertEqual('stale:5d', receipts[old])
        self.assertEqual('sent', states[now_due])
        self.assertNotIn(future, states)
        # The retired event is pushed to the ask cap, so nothing re-judges it.
        self.assertEqual(3, asked(self.root, old))
        self.assertEqual(1, asked(self.root, now_due))
        self.assertEqual(0, asked(self.root, future))
        again = FakeDeliver()
        second = self.run_tick(again, FakeGateway(['不该被调用。']))
        self.assertEqual([], again.calls)
        self.assertEqual([], second['stale'])
        self.assertEqual('skipped', {row['memory_id']: row['state'] for row in ledger(self.root)}[old])

    # 14
    def test_the_stale_window_is_configurable(self):
        mid, _ = self.event('两天前就该做的事。', minutes=-2 * 24 * 60)
        with patch.dict(os.environ, {'FISH_MEMORY_REMINDER_STALE_HOURS': '72'}):
            self.assertGreater(memory_reminder.stale_seconds(), 2 * 24 * 3600)
            deliver = FakeDeliver()
            report = self.run_tick(deliver, FakeGateway(['主人，这件事该做了。']))
        self.assertEqual([mid], report['sent'])
        self.assertEqual(['private:' + OWNER], [call['chat_id'] for call in deliver.calls])
        self.assertEqual([], report['stale'])

    # 15
    def test_the_first_enable_waterline_replaces_the_age_rule(self):
        old_due, _ = self.event('五天前就该做的事。', minutes=-5 * 24 * 60)
        waterline = str(int(time.time() - 600))
        with patch.dict(os.environ, {'FISH_MEMORY_REMINDER_SINCE': waterline}):
            deliver = FakeDeliver()
            report = self.run_tick(deliver, FakeGateway(['主人，这件事该做了。']))
        # Recorded after the waterline, so the age rule no longer retires it.
        self.assertEqual([old_due], report['sent'])
        self.assertEqual([], report['stale'])
        self.assertEqual(1, asked(self.root, old_due))
        other, _ = self.event('同样很旧、今天才记下的另一件事。', minutes=-5 * 24 * 60)
        with patch.dict(os.environ, {'FISH_MEMORY_REMINDER_SINCE': str(int(time.time() + 3600))}):
            second = FakeDeliver()
            report = self.run_tick(second, FakeGateway(['不该被调用。']))
        self.assertEqual([], second.calls)
        self.assertEqual([other], report['before_since'])
        states = {row['memory_id']: row['state'] for row in ledger(self.root)}
        receipts = {row['memory_id']: row['receipt'] for row in ledger(self.root)}
        self.assertEqual('skipped', states[other])
        self.assertEqual('before-since', receipts[other])
        self.assertEqual('sent', states[old_due])
        self.assertEqual(3, asked(self.root, other))

    # 16
    def test_the_stale_rule_can_be_switched_off(self):
        mid, _ = self.event('五天前就该做的事。', minutes=-5 * 24 * 60)
        with patch.dict(os.environ, {'FISH_MEMORY_REMINDER_STALE_HOURS': '0'}):
            self.assertEqual(0.0, memory_reminder.stale_seconds())
            deliver = FakeDeliver()
            report = self.run_tick(deliver, FakeGateway(['主人，这件事该做了。']))
        self.assertEqual([mid], report['sent'])
        self.assertEqual(1, asked(self.root, mid))

    # 17
    def test_a_waterline_is_an_epoch_second_or_an_iso_instant(self):
        with patch.dict(os.environ, {'FISH_MEMORY_REMINDER_SINCE': '1893456000'}):
            self.assertEqual(1893456000.0, memory_reminder.since())
        with patch.dict(os.environ, {'FISH_MEMORY_REMINDER_SINCE': '2030-01-02T03:04:05'}):
            self.assertEqual(datetime(2030, 1, 2, 3, 4, 5,
                                      tzinfo=memory_reminder._SHANGHAI).timestamp(),
                             memory_reminder.since())
        # A 'Z' instant keeps its own offset; a naive one is Shanghai time.
        with patch.dict(os.environ, {'FISH_MEMORY_REMINDER_SINCE': '2030-01-02T03:04:05Z'}):
            self.assertEqual(datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp(),
                             memory_reminder.since())
        with patch.dict(os.environ, {'FISH_MEMORY_REMINDER_SINCE': '不是时间'}):
            self.assertIsNone(memory_reminder.since())


if __name__ == '__main__':
    unittest.main()
