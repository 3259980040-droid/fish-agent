"""04-03: one curation program for daily, Fish tool and the client button.

No paid model: the gateway is scripted; all stores live on a temporary root.
"""
import json
import sqlite3
import sys
import tempfile
import threading
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
from locations import project_path
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
        self.messages = project_path(self.root, 'data/qq/qq.db')
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

    def evidence(self, case, record, gateway):
        print(json.dumps({'case': case, 'run': record['id'], 'status': record['status'],
            'range': record['range'], 'source_limits': record['request'].get('source_limits'),
            'created': record['created'], 'started': record['started'], 'finished': record['finished'],
            'progress': record['progress'], 'result': record['result'],
            'substitute_model_inputs': [item[1] for item in gateway.calls]}, ensure_ascii=False), flush=True)

    # -- one program, one reference ---------------------------------------
    def test_one_fixed_run_automatically_finishes_61_sources_and_excludes_late_arrivals(self):
        tail = '超过16000字符的真实尾部标记R04'
        for number in range(1, 62):
            text = '第%d份原始资料：主人记下了北门%d号。' % (number, number)
            if number == 61:
                text += '很长的原始资料。' * 2000 + tail
            self.add_message(number, text, at=self.start + number)
        case = self
        class BatchedGateway(FakeGateway):
            def json(self, system, user, **kwargs):
                if '小结' not in system and len(self.calls) == 60:
                    case.add_message(62, '执行中新来的资料，留下一范围。', at=case.start + 62)
                return super().json(system, user, **kwargs)
        gateway = BatchedGateway()
        accepted = curation.submit(self.root, start=self.start, end=self.end, autostart=False,
                                    steps=['range', 'learn', 'digest'])
        done = curation.execute(self.root, accepted['id'], gateway=gateway)
        self.assertEqual(done['status'], 'done', done)
        counts = MemoryStore(self.root, 'qq').learning_status()['counts']
        self.assertEqual(counts.get('done'), 61, done)
        self.assertEqual(done['result']['learn']['batch_count'], 2)
        self.assertEqual(done['result']['learn']['remaining'], 0)
        self.assertEqual(done['result']['range']['messages'], 61)
        self.assertEqual(len(gateway.calls), 62, '61次提炼完成后只更新一次小结')
        self.assertEqual(done['result']['model_calls'], 62)
        self.assertIn(tail, gateway.calls[60][1], '实际替代API收到完整来源尾部')
        self.assertTrue(any('北门61' in row['text'] for row in MemoryStore(self.root, 'qq').search('北门61', scope=None)))
        next_run = curation.submit(self.root, start=self.start, end=self.end, autostart=False,
                                   steps=['range', 'learn'])
        next_done = curation.execute(self.root, next_run['id'], gateway=FakeGateway())
        self.assertEqual(next_done['result']['new_messages'], 1)
        self.evidence('61_sources_two_batches_full_text', done, gateway)

    def test_real_interruption_keeps_unprocessed_sources_and_resumes_the_same_fixed_run(self):
        for number in range(1, 5):
            self.add_message(number, '第%d份北门资料。' % number, at=self.start + number)
        class InterruptedGateway(FakeGateway):
            def json(self, system, user, **kwargs):
                self.calls.append((system, user))
                if len(self.calls) == 1:
                    return {'memories': []}  # 正常没有事实也已经实际处理。
                raise RuntimeError('scripted API quota interruption')
        gateway = InterruptedGateway()
        accepted = curation.submit(self.root, start=self.start, end=self.end, autostart=False,
                                    steps=['range', 'learn', 'digest'])
        interrupted = curation.execute(self.root, accepted['id'], gateway=gateway)
        self.assertNotEqual(interrupted['status'], 'done')
        self.assertEqual(len(gateway.calls), 2, '实际API失败后不能继续逐条调用或暗中重试')
        self.assertEqual(interrupted['result']['learn']['completed'], 1)
        self.assertEqual(interrupted['result']['learn']['remaining'], 3)
        self.assertEqual(MemoryStore(self.root, 'qq').learning_status()['counts'],
                         {'done': 1, 'failed': 1, 'pending': 2})
        self.evidence('substitute_API_interruption', interrupted, gateway)
        self.add_message(5, '中断后新来的原文留下一轮。', at=self.start + 5)
        resumed = curation.submit(self.root, autostart=False)
        self.assertEqual(resumed['id'], accepted['id'])
        self.assertEqual(resumed['range_end'], accepted['range_end'])
        retry_gateway = FakeGateway()
        done = curation.execute(self.root, resumed['id'], gateway=retry_gateway)
        self.assertEqual(done['status'], 'done', done['error'])
        self.assertEqual(done['result']['new_messages'], 3)
        self.assertEqual(MemoryStore(self.root, 'qq').learning_status()['counts'], {'done': 4})
        self.assertEqual(len(retry_gateway.calls), 4, '只续剩余3份，再更新一次小结')
        self.evidence('resume_same_fixed_run', done, retry_gateway)

    def test_daily_worker_catches_older_sources_and_queued_group_material_then_resumes_after_pause(self):
        from governance import Governance
        from group_policy import GroupPolicy
        self.add_owner_message('昨天还没整理的北门原文。', at=self.now - 20 * 3600)
        GroupPolicy(self.root).configure('group:123456', {'listen': True, 'learn': True})
        store = MemoryStore(self.root, 'qq')
        store.enqueue('qq-group:group:123456:r04', '群友说自己周六会去北门。',
                      'chat:group:123456', subject='qq:30001')
        case = self
        class PausingGateway(FakeGateway):
            def json(self, system, user, **kwargs):
                if not self.calls:
                    self.calls.append((system, user))
                    Governance(case.root).save({'paused': True})
                    return {'memories': []}
                return super().json(system, user, **kwargs)
        stop = threading.Event()
        worker = curation.ensure_worker(self.root, stop=stop)
        def close_worker():
            stop.set()
            worker.join(6)
        self.addCleanup(close_worker)
        gateway = PausingGateway()
        with patch('model_gateway.ModelGateway', return_value=gateway):
            accepted = curation.tick(self.root, self.now + 2)['run']
            def settled():
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    record = curation.status(self.root, accepted['id'])['run']
                    if record['status'] not in ('queued', 'running'):
                        return record
                    time.sleep(.03)
                self.fail('每日原后台未结清')
            partial = settled()
            self.assertLess(partial['range_start'], self.now - 12 * 3600)
            self.assertEqual(partial['status'], 'partial')
            self.assertEqual(partial['result']['completed'], 1)
            self.assertEqual(partial['result']['remaining'], 1)
            self.assertEqual(partial['digest']['state'], 'skipped')
            self.evidence('daily_older_sources_paused', partial, gateway)
            Governance(self.root).save({'paused': False})
            resumed = curation.submit(self.root)
            self.assertEqual(resumed['id'], accepted['id'])
            done = settled()
            self.assertEqual(done['status'], 'done', done['error'])
            self.assertEqual(done['result']['completed'], 2)
            self.assertEqual(store.learning_status()['counts'], {'done': 2})
            self.assertEqual(done['result']['range']['sources']['learning_jobs'], 1)
            self.evidence('daily_same_run_resumed', done, gateway)

    def test_daily_tool_and_client_share_one_run_reference(self):
        self.add_owner_message()
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
