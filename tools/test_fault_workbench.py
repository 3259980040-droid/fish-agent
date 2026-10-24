"""Read-only fault display uses real ledgers in disposable roots, never model calls."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from governance import Governance
from social_context import SocialContext
from workbench import Workbench


class FaultWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # This action must not need Workbench's job constructor or a running service.
        self.workbench = Workbench.__new__(Workbench)
        self.workbench.root = self.root

    def config(self):
        (self.root / 'channels/fish_qq').mkdir(parents=True, exist_ok=True)
        (self.root / 'channels/fish_qq/config.json').write_text(
            json.dumps({'allowlist': {'owner_id': '123456789'}}), encoding='utf-8')
        (self.root / 'harness/agents').mkdir(parents=True, exist_ok=True)
        (self.root / 'harness/agents.json').write_text(json.dumps({'agents': [
            {'id': 'qq', 'name': 'Test', 'persona': 'harness/agents/qq.persona.md'}]}), encoding='utf-8')
        (self.root / 'harness/agents/qq.persona.md').write_text('test persona', encoding='utf-8')

    def snapshot(self):
        result = {}
        for path in self.root.rglob('*'):
            if not path.is_file() or path.name.endswith(('-wal', '-shm')):
                continue
            if path.suffix == '.db':
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                    result[str(path.relative_to(self.root))] = tuple(db.iterdump())
            else:
                result[str(path.relative_to(self.root))] = path.read_bytes()
        return result

    def test_fresh_unbound_root_shows_chinese_reasons_without_writes(self):
        before = self.snapshot()
        state = self.workbench.dispatch('faults.status', {})
        self.assertEqual(before, self.snapshot())
        self.assertEqual(state['items'], [])
        self.assertIn('尚未绑定主人 QQ 账号', state['configuration'][0]['reason'])
        self.assertIn('人格库尚未建立', state['configuration'][1]['reason'])

    def test_missing_selected_persona_does_not_create_or_switch_default(self):
        self.config()
        contexts = SocialContext(self.root)
        default = contexts.ensure_default('qq')
        old = contexts.save_persona('qq', 'selected', 'selected persona')
        space = contexts.create_space('qq', revision=old['id'], origin='owner-qq')
        contexts.select_owner_space(space['id'], '123456789')
        with closing(sqlite3.connect(contexts.path)) as db, db:
            db.execute('DELETE FROM personas WHERE id=?', (old['id'],))
        before = self.snapshot()
        state = self.workbench.dispatch('faults.status', {})
        self.assertEqual(before, self.snapshot())
        self.assertEqual(state['configuration'][0]['code'], 'persona_missing')
        self.assertIn('没有切换默认人格', state['configuration'][0]['reason'])
        self.assertEqual(contexts.persona(default['id'])['id'], default['id'])

    def test_valid_default_without_test_route_is_not_reported_as_missing(self):
        self.config()
        SocialContext(self.root).ensure_default('qq')
        before = self.snapshot()
        state = self.workbench.dispatch('faults.status', {})
        self.assertEqual(state['configuration'], [])
        self.assertEqual(before, self.snapshot())

    def test_missing_bound_window_has_specific_reason(self):
        self.config()
        contexts = SocialContext(self.root)
        contexts.ensure_default('qq')
        with closing(sqlite3.connect(contexts.path)) as db, db:
            db.execute('INSERT INTO owner_routes VALUES(?,?)', ('123456789', 'f' * 32))
        state = self.workbench.dispatch('faults.status', {})
        self.assertEqual(len(state['configuration']), 1)
        self.assertEqual(state['configuration'][0]['code'], 'space_missing')
        self.assertIn('主人已绑定的私聊窗口不存在', state['configuration'][0]['reason'])

    def test_existing_empty_context_db_reports_structure_problem_without_creating_tables(self):
        self.config()
        path = self.root / 'data/social/context.db'
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)):
            pass
        before = self.snapshot()
        state = self.workbench.dispatch('faults.status', {})
        self.assertEqual(before, self.snapshot())
        self.assertEqual(state['configuration'][0]['code'], 'context_read_error')
        self.assertIn('结构不完整', state['configuration'][0]['reason'])

    def test_malformed_config_reports_chinese_read_failure(self):
        self.config()
        (self.root / 'channels/fish_qq/config.json').write_text('{', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '配置不是有效 JSON'):
            self.workbench.dispatch('faults.status', {})

    def test_aggregation_silence_and_recovery_are_read_from_same_ledger(self):
        self.config()
        SocialContext(self.root).ensure_default('qq')
        policy = Governance(self.root)
        for at in (100, 110, 120):
            policy.fault('owner_private', RuntimeError('model failed'), stage='model', now=at)
        failed = self.workbench.dispatch('faults.status', {})
        self.assertEqual(len(failed['items']), 1)
        self.assertEqual(failed['items'][0]['consecutive'], 3)
        self.assertEqual(failed['items'][0]['first_failed'], 100)
        self.assertEqual(failed['items'][0]['last_failed'], 120)
        policy.fault_outcome('group:123', 'silent', stage='model', reason='她决定保持沉默', now=130)
        silent = self.workbench.dispatch('faults.status', {})
        self.assertEqual(len(silent['items']), 1)
        self.assertEqual(silent['latest'][0]['outcome'], 'silent')
        policy.fault_recovered('owner_private', stage='model', now=140)
        before = self.snapshot()
        recovered = self.workbench.dispatch('faults.status', {})
        self.assertEqual(before, self.snapshot())
        self.assertEqual(recovered['items'][0]['consecutive'], 0)
        self.assertEqual(recovered['items'][0]['recovered_at'], 140)
        self.assertEqual(recovered['items'][0]['total'], 3)


if __name__ == '__main__':
    unittest.main()
