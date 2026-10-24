"""Event lifecycle: progress (action) is separate from visibility (status).

Completion and cancellation are different facts, a rescheduled event keeps one
row with a new deadline, and nothing on these paths deletes a source.
"""
import tempfile
import re
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import memory_store
from governance import Governance
from memory_store import MemoryStore
from storage import database, write_json
from tool_host import ToolHost, builtins

OWNER = '10001'
GROUP = '1043230169'
SHANGHAI = timezone(timedelta(hours=8))


def shanghai(moment):
    return datetime.fromtimestamp(float(moment), SHANGHAI).strftime('%Y-%m-%d %H:%M')


def row_of(store, memory_id, scope='owner'):
    return next(item for item in store.search('', scope=scope, limit=200, include_candidates=True)
                if item['id'] == memory_id)


class LifecycleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root, 'qq')

    def event(self, text='主人明天上午十点去体检。', minutes=-10, **kwargs):
        due = time.time() + minutes * 60
        saved = self.store.save(text, category='约定', scope='owner', source='owner-test',
                                source_text='主人说 ' + shanghai(due) + ' 要去体检', kind='event',
                                due=due, **kwargs)
        return saved['id'], due

    # 1
    def test_complete_and_cancel_are_different_and_neither_archives(self):
        done_id, _ = self.event(text='完成这件事')
        cancelled_id, _ = self.event(text='取消那件事')
        done = self.store.complete(done_id, scope='owner', note='主人说交了')
        cancelled = self.store.cancel(cancelled_id, scope='owner', note='主人说不做了')
        self.assertEqual('completed', done['action'])
        self.assertEqual('cancelled', cancelled['action'])
        done_row, cancelled_row = row_of(self.store, done_id), row_of(self.store, cancelled_id)
        self.assertEqual('completed', done_row['action'])
        self.assertEqual('cancelled', cancelled_row['action'])
        self.assertNotEqual(done_row['status'], 'archived')
        self.assertNotEqual(cancelled_row['status'], 'archived')
        self.assertEqual('active', cancelled_row['status'])
        self.assertIn('已完成于', done_row['source_text'])
        self.assertIn('已取消于', cancelled_row['source_text'])
        self.assertIn('主人说交了', done_row['source_text'])
        self.assertIn('主人说不做了', cancelled_row['source_text'])
        self.assertNotIn('已完成于', cancelled_row['source_text'])
        self.assertTrue(self.store.history(done_id))
        self.assertTrue(self.store.history(cancelled_id))

    # 2
    def test_finished_events_leave_the_todo_but_stay_readable(self):
        done_id, done_due = self.event(text='主人今天要交作业。', minutes=-30)
        cancelled_id, _ = self.event(text='主人要去看展。', minutes=-20)
        self.assertEqual({done_id, cancelled_id},
                         {row['id'] for row in self.store.due_events('owner')})
        block = self.store.context('交作业', scope='owner')
        self.assertIn(done_id[:8], block)
        self.store.complete(done_id, scope='owner', note='主人说交了')
        self.store.cancel(cancelled_id, scope='owner', note='主人说去不了了')
        self.assertEqual([], self.store.due_events('owner'))
        block = self.store.context('交作业 看展', scope='owner')
        self.assertNotIn('已到期的待办', block)
        self.assertNotIn('未到期', block)
        kept = row_of(self.store, done_id)
        self.assertEqual('active', kept['status'])
        self.assertIn('主人说 ' + shanghai(done_due), kept['source_text'])
        self.assertIn('已完成于', kept['source_text'])
        for memory_id in (done_id, cancelled_id):
            self.assertEqual('active', row_of(self.store, memory_id)['status'])
            self.assertTrue(self.store.history(memory_id))

    # 3
    def test_archived_event_is_still_due_because_archiving_is_not_completing(self):
        mid, _ = self.event(text='归档了但还没做')
        with database(self.store.path) as db:
            db.execute("UPDATE memories SET status='archived' WHERE id=?", (mid,))
        self.assertEqual('open', row_of(self.store, mid)['action'])
        self.assertEqual([mid], [row['id'] for row in self.store.due_events('owner')])

    # 4
    def test_reopen_puts_a_finished_event_back_on_the_todo(self):
        mid, _ = self.event(text='主人要重新做这件事')
        self.store.complete(mid, scope='owner', note='主人说做完了')
        self.assertEqual([], self.store.due_events('owner'))
        reopened = self.store.reopen(mid, scope='owner')
        self.assertEqual('open', reopened['action'])
        self.assertEqual([mid], [row['id'] for row in self.store.due_events('owner')])
        self.assertIn('已完成于', row_of(self.store, mid)['source_text'])

    # 5
    def test_same_text_reschedule_keeps_one_row_and_the_new_deadline(self):
        text = '主人周五要交周报。'
        first_due, second_due = time.time() + 3600, time.time() + 86400
        first = self.store.save(text, category='约定', scope='owner', source='owner-test',
                                source_text='主人说 ' + shanghai(first_due) + ' 交周报',
                                kind='event', due=first_due)
        second = self.store.save(text, category='约定', scope='owner', source='owner-test',
                                 source_text='主人说改到 ' + shanghai(second_due) + ' 交了',
                                 kind='event', due=second_due)
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(second.get('rescheduled'))
        self.assertEqual(1, len([item for item in self.store.search('', scope='owner', limit=200)
                                 if item['text'] == text]))
        row = row_of(self.store, first['id'])
        self.assertAlmostEqual(second_due, row['due'], places=3)
        self.assertIn('改期于', row['source_text'])
        self.assertIn('新时间 ' + shanghai(second_due), row['source_text'])
        history = self.store.history(first['id'])
        self.assertEqual(2, len(history))
        self.assertIn(shanghai(first_due), history[0]['source_text'] + history[1]['source_text'])
        self.assertIn(shanghai(second_due), history[0]['source_text'])

    # 6
    def test_a_different_text_is_a_second_event(self):
        first_due, second_due = time.time() + 3600, time.time() + 7200
        first = self.store.save('主人周四要交表。', category='约定', scope='owner', source='owner-test',
                                kind='event', due=first_due)
        second = self.store.save('主人周四要交报告。', category='约定', scope='owner', source='owner-test',
                                 kind='event', due=second_due)
        self.assertNotEqual(first['id'], second['id'])
        self.assertFalse(second.get('rescheduled'))
        rows = [item for item in self.store.search('', scope='owner', limit=200)
                if item['text'].startswith('主人周四要交')]
        self.assertEqual(2, len(rows))
        self.assertAlmostEqual(first_due, row_of(self.store, first['id'])['due'], places=3)
        self.assertAlmostEqual(second_due, row_of(self.store, second['id'])['due'], places=3)

    # 7
    def test_asked_three_times_stops_the_reminder_without_closing_the_event(self):
        mid, _ = self.event(text='问了三次的事')
        for _ in range(3):
            self.store.mark_asked(mid, scope='owner')
        self.assertEqual([], self.store.due_events('owner'))
        row = row_of(self.store, mid)
        self.assertEqual('open', row['action'])
        self.assertEqual(3, row['asked'])
        self.assertEqual('active', row['status'])


class LifecycleToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'harness').mkdir(parents=True, exist_ok=True)
        (self.root / 'data').mkdir(parents=True, exist_ok=True)
        write_json(self.root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'model': 'fake-model', 'base_url': 'https://fake.invalid',
                        'apiKeyEnv': 'FISH_TEST_QQ_KEY'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.root / 'channels/fish_qq/config.json',
                   {'allowlist': {'owner_id': OWNER, 'group_ids': [int(GROUP)]},
                    'bot': {'display_name': '大肥鱼'}})
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        self.host = ToolHost(self.root)
        self.addCleanup(lambda: self.host.stop.set())
        self.owner_session = 'qq-private:' + uuid.uuid4().hex
        self.host.bind('qq', self.owner_session, 'owner', OWNER, False, chat_mode='owner')
        self.store = MemoryStore(self.root, 'qq')

    def save_event(self, text, due, call_id, **extra):
        return self.host.execute('qq', self.owner_session, 'fish_memory_save',
                                 {'text': text, 'category': '约定', 'evidence': 'model_inference',
                                  'kind': 'event', 'due': shanghai(due), 'due_text': '主人说' + shanghai(due),
                                  **extra}, call_id)

    # 8
    def test_event_id_reschedules_one_row_through_the_tool(self):
        first_due, second_due = time.time() + 3600, time.time() + 90000
        saved = self.save_event('主人周三要交报告。', first_due, 'call-1')
        moved = self.save_event('主人周三要交报告。', second_due, 'call-2', event_id=saved['id'])
        self.assertEqual(saved['id'], moved['id'])
        self.assertEqual(shanghai(second_due), shanghai(moved['due']))
        rows = [item for item in self.store.search('', scope='owner', limit=200, include_candidates=True)
                if item['text'] == '主人周三要交报告。']
        self.assertEqual(1, len(rows))
        self.assertEqual(shanghai(second_due), shanghai(rows[0]['due']))
        self.assertIn('改期于', rows[0]['source_text'])
        self.assertIn('原定 ' + shanghai(first_due), rows[0]['source_text'])
        self.assertEqual(2, len(self.store.history(saved['id'])))

    # 9
    def test_rescheduling_a_finished_event_is_refused(self):
        saved = self.save_event('主人要交报告。', time.time() + 3600, 'call-1')
        self.host.execute('qq', self.owner_session, 'fish_memory_complete',
                          {'id': saved['id'], 'note': '主人说交了'}, 'call-2')
        with self.assertRaises(ValueError):
            self.save_event('主人要交报告。', time.time() + 90000, 'call-3', event_id=saved['id'])

    # 10
    def test_tool_cancel_flag_records_a_cancellation(self):
        saved = self.save_event('主人要去看展。', time.time() + 3600, 'call-1')
        cancelled = self.host.execute('qq', self.owner_session, 'fish_memory_complete',
                                      {'id': saved['id'], 'note': '主人说去不了了', 'cancel': True},
                                      'call-2')
        self.assertEqual('cancelled', cancelled['action'])
        row = next(item for item in self.store.search('', scope='owner', limit=200, include_candidates=True)
                   if item['id'] == saved['id'])
        self.assertIn('已取消于', row['source_text'])
        self.assertNotIn('已完成于', row['source_text'])
        # Cancelling is progress, not visibility: the row keeps the status it had.
        self.assertEqual('candidate', row['status'])
        self.assertFalse([item for item in self.store.due_events('owner') if item['id'] == saved['id']])

    # 11
    def test_group_and_forged_sessions_cannot_complete_or_cancel(self):
        saved = self.save_event('主人要交报告。', time.time() + 3600, 'call-1')
        group_session = 'group-fish:group:' + GROUP
        self.host.bind('qq', group_session, 'chat:group:' + GROUP, '20002', False)
        for arguments in ({'id': saved['id']}, {'id': saved['id'], 'cancel': True}):
            with self.assertRaises(PermissionError):
                self.host.execute('qq', group_session, 'fish_memory_complete', arguments, 'call-2')
        forged = 'qq-plain:' + uuid.uuid4().hex
        self.host.bind('qq', forged, 'chat:group:' + GROUP, OWNER, False)
        with self.assertRaises(PermissionError):
            self.host.execute('qq', forged, 'fish_memory_complete', {'id': saved['id']}, 'call-3')
        row = next(item for item in self.store.search('', scope='owner', limit=200, include_candidates=True)
                   if item['id'] == saved['id'])
        self.assertEqual('open', row['action'])

    # 12
    def test_descriptions_explain_due_events_and_explicit_completion(self):
        catalogue = {tool['name']: tool for tool in builtins('qq')}
        save = catalogue['fish_memory_save']
        for instruction in ('kind=event', 'due', '主人私聊', '每条最多提醒一次',
                            '超过48小时', '群聊不会主动提醒'):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, save['description'])
        for field in ('kind', 'due', 'due_text', 'event_id'):
            with self.subTest(field=field):
                self.assertIn(field, save['parameters']['properties'])
        complete = catalogue['fish_memory_complete']
        self.assertIn('cancel', complete['parameters']['properties'])
        self.assertIn('已取消', complete['description'])
        self.assertIn('明确说做完或取消', complete['description'])

    # 13
    def test_lifecycle_paths_never_delete_a_source(self):
        source = Path(memory_store.__file__).read_text(encoding='utf-8')
        region = source[source.index('    def complete('):source.index('    def profile(')]
        self.assertNotIn('DELETE', region.upper())
        for method in ('def complete', 'def cancel', 'def reopen', 'def reschedule', 'def due_events'):
            self.assertIn(method, region)
        evidence = Path(memory_store.__file__).with_name('memory_evidence.py').read_text(encoding='utf-8')
        self.assertNotIn('DELETE', evidence.upper())
        columns = set()
        for clause in re.findall(r'UPDATE memories SET (.+?) WHERE', region, re.S):
            columns.update(re.findall(r'([a-z_]+)\s*=', clause))
        self.assertEqual({'action', 'asked', 'due', 'last_asked', 'source_text', 'updated'} & columns, columns)


if __name__ == '__main__':
    unittest.main()
