"""Event memories: a plan is saved, recalled, completed, and then stops asking."""
import json
import re
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import memory_store
from governance import Governance
from memory_store import MemoryStore
from storage import write_json
from tool_host import ToolHost, builtins

OWNER = '10001'
GROUP = '1043230169'
SHANGHAI = timezone(timedelta(hours=8))


def shanghai(moment):
    return datetime.fromtimestamp(float(moment), SHANGHAI).strftime('%Y-%m-%d %H:%M')


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root, 'qq')

    def event(self, text='主人明天上午十点去体检。', minutes=10, **kwargs):
        due = time.time() + minutes * 60
        saved = self.store.save(text, category='约定', scope='owner', source='owner-test',
                                source_text='主人说 ' + shanghai(due) + ' 要去体检', kind='event',
                                due=due, evidence='owner_quote', **kwargs)
        return saved['id'], due

    def test_future_event_is_shown_as_an_unfinished_item(self):
        mid, due = self.event()
        block = self.store.context('体检', scope='owner')
        self.assertIn(mid[:8], block)
        self.assertIn(shanghai(due), block)
        self.assertIn('未到期', block)
        self.assertNotIn('可以主动提一次', block)
        self.assertEqual([], self.store.due_events('owner'))

    def test_overdue_event_asks_once_not_repeatedly(self):
        mid, due = self.event(minutes=-10, text='主人今天要交作业。')
        block = self.store.context('作业', scope='owner')
        self.assertIn(mid[:8], block)
        self.assertIn(shanghai(due), block)
        self.assertIn('可以主动提一次', block)
        self.assertIn('不要重复催', block)
        self.assertEqual([mid], [row['id'] for row in self.store.due_events('owner')])

    def test_complete_keeps_evidence_and_stops_the_todo(self):
        mid, due = self.event(minutes=-5, text='主人今天要交作业。')
        done = self.store.complete(mid, scope='owner', note='主人说已经交了')
        self.assertEqual({'ok': True, 'id': mid, 'action': 'completed'},
                         {'ok': done['ok'], 'id': done['id'], 'action': done['action']})
        row = next(item for item in self.store.search('交作业', scope='owner', include_candidates=True)
                   if item['id'] == mid)
        # Progress and visibility are separate: finishing is not archiving.
        self.assertEqual('active', row['status'])
        self.assertEqual('completed', row['action'])
        self.assertIn('主人说 ' + shanghai(due), row['source_text'])
        self.assertIn('已完成于', row['source_text'])
        self.assertIn('主人说已经交了', row['source_text'])
        self.assertTrue(self.store.history(mid))
        self.assertEqual([], self.store.due_events('owner'))
        # The fact stays readable, but it is no longer a pending item.
        block = self.store.context('交作业', scope='owner')
        self.assertIn('主人今天要交作业。', block)
        self.assertNotIn('已到期的待办', block)
        self.assertNotIn('未到期', block)

    def test_non_event_and_missing_memory_cannot_complete(self):
        fact = self.store.save('主人喜欢蓝色。', scope='owner', source='owner-test')['id']
        self.assertFalse(self.store.complete(fact, scope='owner')['ok'])
        self.assertFalse(self.store.complete('missing-id', scope='owner')['ok'])
        other = self.store.save('群里的约定', scope='chat:group:' + GROUP, source='owner-test',
                                kind='event', due=time.time() + 600)['id']
        self.assertFalse(self.store.complete(other, scope='owner')['ok'])

    def test_due_events_only_returns_open_unasked_events(self):
        past = time.time() - 60
        future = self.store.save('明天的事', scope='owner', source='owner-test', kind='event',
                                 due=time.time() + 3600)['id']
        fact = self.store.save('普通事实', scope='owner', source='owner-test')['id']
        archived = self.store.save('已归档事件', scope='owner', source='owner-test', kind='event',
                                   due=past, status='archived')['id']
        finished = self.store.save('已经做完的事件', scope='owner', source='owner-test', kind='event',
                                   due=past)['id']
        self.store.complete(finished, scope='owner')
        asked = self.store.save('问过三次的事件', scope='owner', source='owner-test', kind='event',
                                due=past)['id']
        due = self.store.save('到期事件', scope='owner', source='owner-test', kind='event',
                              due=past)['id']
        for _ in range(3):
            self.store.mark_asked(asked, scope='owner')
        ids = [row['id'] for row in self.store.due_events('owner')]
        # Archiving hides a row without finishing it, so it is still due.
        self.assertEqual({archived, due}, set(ids))
        for kept_out in (future, fact, finished, asked):
            self.assertNotIn(kept_out, ids)
        marked = self.store.mark_asked(due, scope='owner', at=1700000000.0)
        self.assertEqual({'ok': True, 'id': due, 'asked': 1},
                         {'ok': marked['ok'], 'id': marked['id'], 'asked': marked['asked']})
        row = next(item for item in self.store.search('到期事件', scope='owner') if item['id'] == due)
        self.assertEqual(1700000000.0, row['last_asked'])
        self.assertEqual(1, row['asked'])

    def test_a_later_resave_does_not_erase_the_due_time(self):
        mid, due = self.event()
        self.store.save('主人明天上午十点去体检，带报告。', category='约定', scope='owner',
                        source='owner-test', memory_id=mid, kind='event', evidence='owner_quote')
        row = next(item for item in self.store.search('体检', scope='owner') if item['id'] == mid)
        self.assertAlmostEqual(due, row['due'], places=3)

    def test_new_methods_never_delete_original_evidence(self):
        source = Path(memory_store.__file__).read_text(encoding='utf-8')
        region = source[source.index('    def complete('):source.index('    def profile(')]
        self.assertNotIn('DELETE', region.upper())
        columns = set()
        for clause in re.findall(r'UPDATE memories SET (.+?) WHERE', region, re.S):
            columns.update(re.findall(r'([a-z_]+)\s*=', clause))
        self.assertEqual({'action', 'asked', 'due', 'last_asked', 'source_text', 'updated'}
                         & columns, columns)


class EventToolTests(unittest.TestCase):
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

    def test_owner_can_save_an_event_and_complete_it(self):
        due = shanghai(time.time() + 900)
        saved = self.host.execute('qq', self.owner_session, 'fish_memory_save',
                                  {'text': '主人说周三要交报告。', 'category': '约定',
                                   'evidence': 'model_inference', 'kind': 'event',
                                   'due': due, 'due_text': '主人说周三要交报告'}, 'call-1')
        row = next(item for item in MemoryStore(self.root, 'qq').search(
            '报告', scope='owner', include_candidates=True) if item['id'] == saved['id'])
        self.assertEqual('event', row['kind'])
        self.assertAlmostEqual(shanghai(row['due']), due)
        self.assertIn('约定原话：主人说周三要交报告', row['source_text'])
        done = self.host.execute('qq', self.owner_session, 'fish_memory_complete',
                                 {'id': saved['id'], 'note': '主人说交了'}, 'call-2')
        self.assertEqual('completed', done['action'])
        store = MemoryStore(self.root, 'qq')
        self.assertFalse(store.due_events('owner'))
        self.assertIn('已完成于', next(item['source_text'] for item in store.search(
            '报告', scope='owner', include_candidates=True) if item['id'] == saved['id']))

    def test_group_scope_cannot_complete_events(self):
        saved = self.host.execute('qq', self.owner_session, 'fish_memory_save',
                                  {'text': '主人说周三要交报告。', 'category': '约定',
                                   'evidence': 'model_inference', 'kind': 'event',
                                   'due': shanghai(time.time() + 900)}, 'call-1')
        group_session = 'group-fish:group:' + GROUP
        self.host.bind('qq', group_session, 'chat:group:' + GROUP, '20002', False)
        with self.assertRaises(PermissionError):
            self.host.execute('qq', group_session, 'fish_memory_complete', {'id': saved['id']}, 'call-2')
        forged = 'qq-plain:' + uuid.uuid4().hex
        self.host.bind('qq', forged, 'chat:group:' + GROUP, OWNER, False)
        with self.assertRaises(PermissionError):
            self.host.execute('qq', forged, 'fish_memory_complete', {'id': saved['id']}, 'call-3')
        self.assertEqual('candidate', next(item['status'] for item in MemoryStore(self.root, 'qq').search(
            '报告', scope='owner', include_candidates=True) if item['id'] == saved['id']))

    def test_tool_catalogue_explains_when_to_use_events(self):
        catalogue = {tool['name']: tool for tool in builtins('qq')}
        self.assertIn('fish_memory_complete', catalogue)
        save = catalogue['fish_memory_save']
        self.assertIn('kind', save['parameters']['properties'])
        self.assertIn('due', save['parameters']['properties'])
        self.assertIn('due_text', save['parameters']['properties'])
        self.assertIn('kind=event', save['description'])
        self.assertIn('fish_memory_complete', save['description'])
        complete = catalogue['fish_memory_complete']
        self.assertEqual(['id'], complete['parameters']['required'])
        self.assertIn('不再主动提', complete['description'])


if __name__ == '__main__':
    unittest.main()
