"""H1: disposable SQLite only; no model, QQ or application service is started."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from governance import Governance, classify_fault, fault_code, sanitize_fault_detail


# Original H1 table dump, kept verbatim as the compatibility fixture (synthetic rows).
H1_V1_DUMP = '''
BEGIN TRANSACTION;
CREATE TABLE faults(source TEXT,code TEXT,stage TEXT,reason TEXT,
    consecutive INTEGER,total INTEGER,first_failed REAL,last_failed REAL,recovered_at REAL,
    uncertain INTEGER,PRIMARY KEY(source,code));
INSERT INTO faults VALUES('legacy','execution_failed','model','旧模型故障',3,5,100,120,0,0);
INSERT INTO faults VALUES('legacy','permission','delivery','旧权限故障',1,1,130,130,0,0);
INSERT INTO faults VALUES('legacy-empty','unknown','','旧历史',1,1,90,90,0,0);
CREATE TABLE fault_notices(token TEXT PRIMARY KEY,source TEXT,code TEXT,
    recovered INTEGER,day TEXT,created REAL,sent INTEGER,uncertain INTEGER,finished REAL);
INSERT INTO fault_notices VALUES('old-token','legacy','execution_failed',0,'2026-10-23',140,1,0,145);
COMMIT;
'''


class FaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fish-faults-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.g = Governance(self.root)

    def test_classification_has_one_safe_reason_and_never_grants_fallback(self):
        cases = [(RuntimeError('HTTP 401 Bearer sk-private-value'), 'authentication'),
                 (ValueError('人格版本不存在'), 'persona_missing'),
                 (PermissionError('权限已经关闭'), 'permission'),
                 (RuntimeError('ProxyError localhost:7890'), 'proxy'),
                 (RuntimeError('ConnectionError https://x?api_key=sk-private-value'), 'connection'),
                 (TimeoutError('late'), 'timeout')]
        for error, code in cases:
            with self.subTest(code=code):
                value = classify_fault(error)
                self.assertEqual(value['code'], code)
                self.assertFalse(value['auto_retry'])
                self.assertNotIn('sk-private-value', str(value))
        value = classify_fault({'error': {'may_have_sent': True, 'code': 'timeout'}}, stage='delivery')
        self.assertEqual(value['code'], 'delivery_uncertain')
        self.assertTrue(value['uncertain'])

    def test_retained_exception_names_are_classified_but_erased_causes_are_not_guessed(self):
        for text, expected in (('judge-error:PermissionError','permission'),
                               ('judge-error:AuthenticationError','authentication'),
                               ('judge-error:PermissionDeniedError','permission'),
                               ('judge-error:RuntimeError','execution_failed'),
                               ('判断失败，按0分处理','execution_failed')):
            with self.subTest(reason=text):
                value = classify_fault(text, stage='prescreen')
                self.assertEqual(value['code'], expected)
                self.assertFalse(value['auto_retry'])
        AuthenticationError = type('AuthenticationError', (Exception,), {})
        self.assertEqual(classify_fault(AuthenticationError())['code'], 'authentication')

    def test_configuration_and_validation_have_specific_safe_reasons(self):
        value = classify_fault(ValueError('尚未配置主人账号'), stage='configuration')
        self.assertEqual(value['code'], 'configuration')
        self.assertIn('QQ 通道配置', value['reason'])
        value = classify_fault({'code':'window-changed','validation_failed':True}, stage='delivery')
        self.assertEqual(value['code'], 'permission')
        self.assertIn('校验未通过', value['reason'])
        self.assertFalse(value['auto_retry'])

    def test_provider_six_codes_and_safe_detail_are_shared_at_return_and_write(self):
        codes = [(RuntimeError('HTTP 401'), 'auth'), (RuntimeError('HTTP 403'), 'auth'),
                 (TimeoutError(), 'timeout'), (ConnectionError(), 'network'),
                 (json.JSONDecodeError('bad JSON','x',0), 'protocol'),
                 (RuntimeError('HTTP 429 quota'), 'limit'), (RuntimeError('opaque'), 'unknown')]
        for error, expected in codes:
            with self.subTest(code=expected): self.assertEqual(fault_code(error), expected)
        details = []
        for status in (401,403):
            raw = RuntimeError(f'HTTP {status} https://api.invalid/v1?api_key=URL_SECRET '
                               'Bearer BEARER_SECRET token=TOKEN_SECRET ' + 'A'*180)
            safe = sanitize_fault_detail(raw)
            self.assertEqual(sanitize_fault_detail(safe), safe)
            row = self.g.fault('provider', {'provider': True, 'message': safe}, stage='judge', now=status)
            self.assertEqual((row['code'],row['reason']), ('auth','认证失败，需要检查密钥'))
            for hidden in ('https://','URL_SECRET','BEARER_SECRET','TOKEN_SECRET','A'*64):
                self.assertNotIn(hidden, row['detail'])
            self.assertTrue(row['detail'].startswith('RuntimeError: '))
            self.assertLessEqual(len(row['detail']), len('RuntimeError: ')+120)
            details.append(row['detail'])
        self.assertNotEqual(*details)
        self.assertNotIn('detail', self.g.fault_snapshot()['items'][0])
        self.g.audit('group_prescreen_failed', str(raw))
        with closing(self.g.connect()) as db:
            persisted = db.execute("SELECT reason FROM audit WHERE kind='group_prescreen_failed'").fetchone()[0]
        for hidden in ('https://','URL_SECRET','BEARER_SECRET','TOKEN_SECRET','A'*64): self.assertNotIn(hidden,persisted)

    def test_repeated_failures_survive_reopen_and_reading_never_recovers(self):
        self.g.fault('group:1', TimeoutError(), now=100)
        second = self.g.fault('group:1', TimeoutError(), now=150)
        self.assertEqual((second['consecutive'], second['total'], second['first_failed'], second['last_failed']), (2,2,100,150))
        view = Governance.read_faults(self.root)
        self.assertEqual(view['items'][0]['consecutive'], 2)
        self.assertEqual(Governance(self.root).fault_snapshot()['items'][0]['consecutive'], 2)
        self.g.fault_recovered('group:1', stage='delivery', now=180)
        self.assertEqual(self.g.fault_snapshot()['items'][0]['consecutive'], 2)
        recovered = self.g.fault_recovered('group:1', stage='model', now=200)
        self.assertEqual((recovered[0]['consecutive'], recovered[0]['recovered_at']), (0,200))
        self.g.fault_recovered('group:1', stage='model', now=220)
        with closing(self.g.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE kind='fault_recovered'").fetchone()[0], 1)
        third = self.g.fault('group:1', TimeoutError(), now=300)
        self.assertEqual((third['consecutive'],third['total'],third['first_failed']), (1,3,300))

    def test_normal_silence_is_visible_but_never_a_failure(self):
        class TurnSilent(Exception): pass
        self.assertIsNone(self.g.fault('owner', TurnSilent(), now=100))
        view = self.g.fault_snapshot()
        self.assertEqual(view['items'], [])
        self.assertEqual(view['latest'][0]['outcome'], 'silent')
        self.assertEqual(view['latest'][0]['updated'], 100)

    def test_notice_claim_is_atomic_cools_30_minutes_and_unknown_is_not_retried(self):
        row = self.g.fault('owner', {'error': {'may_have_sent': True}}, stage='delivery', now=100)
        gate = threading.Barrier(4)
        def claim(_):
            gate.wait(timeout=5)
            return self.g.claim_fault_notice('owner', row['code'], stage='delivery', now=110)
        with ThreadPoolExecutor(max_workers=4) as pool:
            tokens = list(pool.map(claim, range(4)))
        tokens = [token for token in tokens if token]
        self.assertEqual(len(tokens), 1)
        self.assertTrue(self.g.finish_fault_notice(tokens[0], uncertain=True, now=115))
        self.assertFalse(self.g.finish_fault_notice(tokens[0], sent=True, now=116))
        self.assertEqual(self.g.claim_fault_notice('owner', row['code'], stage='delivery', now=1909), '')
        self.assertTrue(self.g.claim_fault_notice('owner', row['code'], stage='delivery', now=1910))
        self.assertFalse(self.g.fault_snapshot()['items'][0]['auto_retry'])
        self.assertEqual(self.g.fault_snapshot()['notices'][-1]['uncertain'], 1)

    def test_same_code_in_two_stages_does_not_overwrite_recover_or_suppress_the_other(self):
        self.g.fault('same', RuntimeError('model failed'), stage='model', now=100)
        self.g.fault('same', RuntimeError('judge failed'), stage='judge', now=110)
        self.assertTrue(self.g.claim_fault_notice('same', 'execution_failed', stage='model', now=120))
        self.assertTrue(self.g.claim_fault_notice('same', 'execution_failed', stage='judge', now=120))
        self.assertEqual(self.g.claim_fault_notice('same', 'execution_failed', stage='judge', now=130), '')
        recovered = self.g.fault_recovered('same', stage='judge', now=140)
        self.assertEqual([row['stage'] for row in recovered], ['judge'])
        rows = {row['stage']:row for row in self.g.fault_snapshot()['items']}
        self.assertEqual((rows['model']['consecutive'], rows['model']['recovered_at']), (1,0))
        self.assertEqual((rows['judge']['consecutive'], rows['judge']['recovered_at']), (0,140))
        self.assertEqual(len(self.g.fault_snapshot()['notices']), 2)

    def test_initial_h1_dump_is_upgraded_transactionally_without_losing_stored_rows(self):
        root = self.root / 'old-h1'
        path = root / 'data/qq/governance.db'
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db:
            db.executescript(H1_V1_DUMP)
            old_rows = db.execute('SELECT * FROM faults ORDER BY code').fetchall()
            old_notices = db.execute('SELECT * FROM fault_notices').fetchall()
        readonly = Governance.read_faults(root)
        self.assertEqual(next(row for row in readonly['items'] if row['stage']=='')['stage_label'], '未分类（历史）')
        with self.assertRaisesRegex(ValueError,'先审批升级'):
            Governance(root)
        migration = Governance.upgrade_fault_schema(root)
        self.assertEqual((migration['before_count'],migration['after_count']), (3,3))
        self.assertNotEqual(migration['before_table_info'],migration['after_table_info'])
        self.assertEqual(Governance.upgrade_fault_schema(root)['after_count'], 3)
        upgraded = Governance(root)
        with closing(upgraded.connect()) as db:
            self.assertEqual(db.execute('SELECT source,code,stage,reason,consecutive,total,first_failed,last_failed,recovered_at,uncertain FROM faults ORDER BY code').fetchall(), old_rows)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM faults WHERE detail='' ").fetchone()[0],3)
            self.assertEqual(db.execute('SELECT token,source,code,recovered,day,created,sent,uncertain,finished FROM fault_notices').fetchall(), old_notices)
            self.assertEqual(db.execute('SELECT stage FROM fault_notices').fetchone()[0], '')
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='faults_h1_v1'").fetchone())
            key = [row[1] for row in sorted(db.execute('PRAGMA table_info(faults)'),key=lambda row:row[5]) if row[5]]
            self.assertEqual(key, ['source','stage','code'])
        upgraded.fault('legacy', RuntimeError('judge failed'), stage='judge', now=200)
        upgraded.fault_recovered('legacy', stage='judge', now=210)
        rows = Governance(root).fault_snapshot()['items']
        original = next(row for row in rows if row['code']=='execution_failed' and row['stage']=='model')
        self.assertEqual((len(rows),original['consecutive'],original['total']), (4,3,5))

    def test_recovery_notice_is_once_for_episode_and_globally_once_per_shanghai_day(self):
        at = 1_800_000_000
        for source in ('one','two'):
            self.g.fault(source, TimeoutError(), now=at)
            self.g.fault_recovered(source, now=at+10)
        first = self.g.claim_fault_notice('one', 'timeout', recovered=True, now=at+20)
        self.assertTrue(first)
        self.assertEqual(self.g.claim_fault_notice('two', 'timeout', recovered=True, now=at+21), '')
        self.assertEqual(self.g.claim_fault_notice('one', 'timeout', recovered=True, now=at+86420), '')
        self.assertTrue(self.g.claim_fault_notice('two', 'timeout', recovered=True, now=at+86420))

    def test_test_fluctuation_keeps_failures_after_a_pass(self):
        self.g.record_test_observation('case-A', False, now=100)
        self.g.record_test_observation('case-A', False, now=110)
        self.g.record_test_observation('case-A', True, now=120)
        row = self.g.fault_snapshot()['tests'][0]
        self.assertEqual((row['runs'],row['failures'],row['consecutive']), (3,2,0))
        self.assertEqual((row['last_failed'],row['last_passed']), (110,120))

    def test_historical_failure_remains_visible_after_many_new_green_cases(self):
        self.g.record_test_observation('old-4-runs-1-failure', False, now=100)
        for moment in (110,120,130):
            self.g.record_test_observation('old-4-runs-1-failure', True, now=moment)
        for number in range(129):
            self.g.record_test_observation('new-green-'+str(number), True, now=1000+number)
        view = self.g.fault_snapshot()['tests']
        self.assertEqual(view[0]['case_name'], 'old-4-runs-1-failure')
        self.assertEqual((view[0]['runs'],view[0]['failures']), (4,1))

    def test_reminders_read_the_existing_switches_and_private_send_setting(self):
        channel = self.root / 'channels/fish_qq/config.json'
        channel.parent.mkdir(parents=True)
        channel.write_text(json.dumps({'allowlist': {'owner_id': '123456789'}}), encoding='utf-8')
        with patch.dict('os.environ', {'FISH_MEMORY_REMINDER': '0', 'FISH_PRIVATE_PROACTIVE': 'false'}):
            states = self.g.reminder_status()
            self.assertFalse(states['memory_reminder']['enabled'])
            self.assertFalse(states['private_proactive']['enabled'])
        with patch.dict('os.environ', {'FISH_MEMORY_REMINDER': '1', 'FISH_PRIVATE_PROACTIVE': '1'}):
            self.assertTrue(self.g.reminder_status()['memory_reminder']['effective'])
            self.g.save({'send_policy': {'owner_private': {'owner_reply': False}}})
            self.assertFalse(self.g.reminder_status()['memory_reminder']['effective'])

    def test_readonly_view_does_not_create_or_upgrade_databases(self):
        other = self.root / 'empty'
        value = Governance.read_faults(other)
        self.assertEqual(value['items'], [])
        self.assertFalse(other.exists())
        legacy = self.root / 'legacy'
        path = legacy / 'data/qq/governance.db'
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE audit(id INTEGER PRIMARY KEY,ts TEXT,kind TEXT,reason TEXT,digest TEXT)')
            db.commit()
        before = path.read_bytes()
        self.assertEqual(Governance.read_faults(legacy)['items'], [])
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__': unittest.main()
