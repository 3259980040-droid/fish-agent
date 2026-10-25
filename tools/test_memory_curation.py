"""04-03: one curation program for daily, Fish tool and the client button.

No paid model: the gateway is scripted; all stores live on a temporary root.
"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]

import daily_curate
import knowledge
import memory_curation as curation
from memory_maintenance import MemoryMaintenance
from memory_store import MemoryStore
from storage import database, role_dir
from workbench import Workbench

TZ = timezone(timedelta(hours=8))
QQ_SCHEMA = (
    'CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT, '
    'user_id TEXT, text TEXT, conversation_id TEXT, sender_id TEXT, direction TEXT, '
    'channel_message_id TEXT, reply_to TEXT, segments TEXT, status TEXT)')


class FakeGateway:
    key = 'fake'

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def json(self, system, user, **kwargs):
        if self.fail:
            raise RuntimeError('scripted model failure')
        self.calls.append((system, user))
        if 'relationship_event' in system:
            return {'relationship_event': 'neutral'}
        if '小结' in system:
            return {'digest': '今天记下了主人下周去北门出差。'}
        text = str(user)[:40]
        return {'memories': [{'text': text, 'category': '事件', 'kind': 'event',
                              'evidence': 'self_report', 'source_quote': str(user),
                              'confidence': 0.95}]}


class CurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'harness').mkdir()
        (self.root / 'channels/fish_qq').mkdir(parents=True)
        (self.root / 'data/qq').mkdir(parents=True)
        (self.root / 'harness/memory-embeddings.json').write_text('{"enabled": false}', encoding='utf-8')
        (self.root / 'channels/fish_qq/config.json').write_text(
            '{"bot_id":"10001","allowlist":{"owner_id":"20001"}}', encoding='utf-8')
        self.now = time.time()
        self.start = self.now - 3600
        self.end = self.now + 60
        self.messages = self.root / 'data/qq/qq.db'
        with closing(sqlite3.connect(self.messages)) as db:
            db.execute(QQ_SCHEMA)
            db.commit()

    # -- helpers -----------------------------------------------------------
    def add_message(self, message_id, text, at=None):
        stamp = datetime.fromtimestamp(self.now if at is None else at, TZ).strftime('%Y-%m-%d %H:%M:%S')
        with closing(sqlite3.connect(self.messages)) as db:
            db.execute(
                'INSERT INTO messages(id,ts,chat_id,user_id,text,conversation_id,sender_id,'
                'direction,channel_message_id,reply_to,segments,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (message_id, stamp, 'private:20001', '20001', text, 'c1', '20001',
                 'inbound', str(9000 + message_id), '', '[]', 'processed'))
            db.commit()

    def add_owner_message(self, text='我下周要去北门出差', message_id=1, at=None):
        self.add_message(message_id, text, at if at is not None else self.now - 600)

    def run_once(self, gateway, **kwargs):
        options = {'start': self.start, 'end': self.end,
                   'day': datetime.fromtimestamp(self.now, TZ).strftime('%Y-%m-%d'),
                   'requested_by': 'tool'}
        options.update(kwargs)
        return curation.run_now(self.root, gateway=gateway, **options)

    def runs(self):
        with database(role_dir(self.root, 'qq') / 'memory.db') as db:
            return [dict(row) for row in db.execute('SELECT * FROM memory_curate_runs ORDER BY created')]

    # -- one program, one reference ---------------------------------------
    def test_daily_tool_and_client_share_one_run_reference(self):
        with patch.object(curation, 'ensure_worker', lambda root: None):
            daily = curation.tick(self.root, self.now)
        self.assertTrue(daily['submitted'], daily)
        run = daily['run']
        self.assertEqual(run['requested_by'], 'daily')
        same_tool = curation.submit(self.root, start=run['range_start'], end=run['range_end'],
                                    requested_by='tool', autostart=False)
        same_client = curation.submit(self.root, start=run['range_start'], end=run['range_end'],
                                      requested_by='client', autostart=False)
        self.assertEqual(same_tool['id'], run['id'])
        self.assertEqual(same_client['id'], run['id'])
        self.assertTrue(same_tool.get('reused'))
        self.assertEqual(len(self.runs()), 1)
        with patch.object(curation, 'ensure_worker', lambda root: None):
            same_legacy_daily = daily_curate.tick(self.root, self.now)
            same_maintenance = MemoryMaintenance(self.root).tick(self.now)
        self.assertEqual(same_legacy_daily['run']['id'], run['id'])
        self.assertEqual(same_maintenance['run']['id'], run['id'])
        gateway = FakeGateway()
        self.add_owner_message()
        done = curation.execute(self.root, run['id'], gateway=gateway)
        self.assertEqual(done['status'], 'done', done)
        self.assertGreaterEqual(done['result']['saved_count'], 1)
        status = curation.status(self.root, run['id'])
        self.assertEqual(status['run']['status'], 'done')
        self.assertTrue(status['run']['progress'])

    def test_tick_is_idempotent_and_does_not_create_a_second_daily_loop(self):
        with patch.object(curation, 'ensure_worker', lambda root: None):
            first = curation.tick(self.root, self.now)
            second = curation.tick(self.root, self.now)
        self.assertTrue(first['submitted'])
        self.assertFalse(second['submitted'])
        self.assertEqual(first['run']['id'], second['run']['id'])
        self.assertIn('正在整理', second['skipped'])
        curation.execute(self.root, first['run']['id'], gateway=FakeGateway())
        with patch.object(curation, 'ensure_worker', lambda root: None):
            third = curation.tick(self.root, self.now)
        self.assertFalse(third['submitted'])
        self.assertEqual(third['run']['id'], first['run']['id'])
        self.assertIn('已经整理', third['skipped'])
        self.assertEqual(len(self.runs()), 1)

    # -- the program -------------------------------------------------------
    def test_program_saves_memories_revisions_digest_and_fts(self):
        self.add_owner_message('我下周要去北门出差')
        gateway = FakeGateway()
        record = self.run_once(gateway)
        self.assertEqual(record['status'], 'done', record)
        result = record['result']
        self.assertGreaterEqual(result['new_messages'], 1)
        self.assertGreaterEqual(result['saved_count'], 1)
        self.assertEqual(result['failures'], [])
        saved = result['saved'][0]
        self.assertEqual(saved['scope'], 'owner')
        self.assertEqual(saved['status'], 'active')
        store = MemoryStore(self.root, 'qq')
        found = store.search('北门', scope=None, limit=20)
        self.assertTrue(any(row['id'] == saved['id'] for row in found), found)
        self.assertGreaterEqual(len(store.history(saved['id'])), 1)
        self.assertTrue(any(hit['reference'] == 'memory:' + saved['id']
                            for hit in knowledge.search(self.root, '北门')['hits']))
        with database(role_dir(self.root, 'qq') / 'memory.db') as db:
            digest = db.execute('SELECT text FROM memory_digest').fetchone()
        self.assertIsNotNone(digest)
        self.assertIn('北门', digest[0])
        self.assertGreaterEqual(result['model_calls'], 2)

    def test_unchanged_material_is_skipped_without_a_model_call(self):
        self.add_owner_message('我下周要去北门出差')
        gateway = FakeGateway()
        first = self.run_once(gateway)
        self.assertGreaterEqual(first['result']['saved_count'], 1)
        calls = len(gateway.calls)
        second = self.run_once(gateway)
        self.assertEqual(second['status'], 'done')
        self.assertEqual(second['result']['new_messages'], 0)
        self.assertEqual(second['result']['saved_count'], 0)
        self.assertEqual(second['result']['model_calls'], 0)
        self.assertEqual(len(gateway.calls), calls)
        self.assertGreaterEqual(second['result']['skipped_existing'], 1)

    def test_failure_is_recorded_without_progress_and_rerun_works(self):
        self.add_owner_message('我下周要去北门出差')
        window = datetime.fromtimestamp(self.now, TZ).replace(hour=4, minute=0, second=0, microsecond=0)
        failed = self.run_once(FakeGateway(fail=True), requested_by='daily', window=window.timestamp())
        self.assertIn(failed['status'], ('partial', 'failed'))
        self.assertEqual(failed['result']['range']['start'], self.start)
        self.assertTrue(failed['result']['failures'])
        self.assertIn('scripted model failure', failed['error'])
        with database(role_dir(self.root, 'qq') / 'memory.db') as db:
            done = db.execute('SELECT 1 FROM memory_curate_state WHERE key=?',
                              ('daily_done:' + str(int(window.timestamp())),)).fetchone()
            jobs = [tuple(row) for row in db.execute('SELECT source,status,attempts FROM learning_jobs')]
        self.assertIsNone(done)
        self.assertEqual(jobs[0][1], 'failed')
        self.assertGreaterEqual(jobs[0][2], 1)
        good = self.run_once(FakeGateway())
        self.assertEqual(good['status'], 'done', good)
        self.assertGreaterEqual(good['result']['saved_count'], 1)
        self.assertEqual(good['result']['failures'], [])

    def test_expanded_range_is_a_queued_new_run_and_unchanged_is_skipped(self):
        self.add_owner_message('我下周要去北门出差', message_id=1, at=self.start + 10)
        self.add_owner_message('周六还要去看展览', message_id=2, at=self.start + 120)
        narrow = curation.submit(self.root, start=self.start, end=self.start + 60,
                                 day=datetime.fromtimestamp(self.now, TZ).strftime('%Y-%m-%d'),
                                 requested_by='tool', autostart=False)
        wide = curation.submit(self.root, start=self.start, end=self.start + 240,
                               day=datetime.fromtimestamp(self.now, TZ).strftime('%Y-%m-%d'),
                               requested_by='client', autostart=False)
        self.assertNotEqual(narrow['id'], wide['id'])
        self.assertEqual(len(self.runs()), 2)
        first = curation.execute(self.root, narrow['id'], gateway=FakeGateway())
        self.assertEqual(first['status'], 'done')
        second = curation.execute(self.root, wide['id'], gateway=FakeGateway())
        self.assertEqual(second['status'], 'done')
        self.assertEqual(second['result']['new_messages'], 1)
        self.assertGreaterEqual(second['result']['skipped_existing'], 1)
        self.assertEqual(second['result']['saved_count'], 1)

    # -- entries -----------------------------------------------------------
    def test_workbench_and_fish_tool_use_the_same_program(self):
        page = Workbench(self.root)
        with patch.object(curation, 'ensure_worker', lambda root: None):
            accepted = page.dispatch('memory.curate.run', {'start': self.start, 'end': self.end})
            same = page.dispatch('memory.curate.status', {'run_id': accepted['id']})
        self.assertEqual(accepted['status'], 'queued')
        self.assertEqual(same['run']['id'], accepted['id'])
        self.assertEqual(len(self.runs()), 1)

        from tool_host import ToolHost, builtins
        self.assertIn('fish_memory_curate', {item['name'] for item in builtins('qq')})
        host = ToolHost(self.root)
        host.identity = lambda role, session: {
            'role': role, 'session': session, 'scope': 'owner', 'owner': 1,
            'desktop': 1, 'sender': '20001'}
        with patch.object(curation, 'ensure_worker', lambda root: None):
            from_tool = host.execute('qq', 'curate-check', 'fish_memory_curate',
                                     {'action': 'start', 'start': self.start, 'end': self.end},
                                     'call-curate-1')
        self.assertEqual(from_tool['id'], accepted['id'])
        with patch.object(curation, 'ensure_worker', lambda root: None):
            status = host.execute('qq', 'curate-check', 'fish_memory_curate',
                                  {'action': 'status', 'run_id': accepted['id']},
                                  'call-curate-2')
        self.assertEqual(status['run']['id'], accepted['id'])


if __name__ == '__main__':
    unittest.main()
