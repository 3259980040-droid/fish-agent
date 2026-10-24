"""记忆概要与人格清空：月/周/日三级、按批注重写、先备份再删。

模型换成固定返回，库与日期还是生产那套。这里钉住四件事：范围编号按真实日历算边界、
同一个范围只留一行、改写回填并保存「改前改后」、clear 先写 backups/ 再删且保住正在用的
那张人格（包括正在被群测试窗口占用的）。
"""
import json
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
import memory_digest
from group_tests import GroupTests
from memory_store import MemoryStore
from persona_library import PersonaLibrary
from social_context import SocialContext
from storage import database
from test_social_base import Fixture


class DigestScopeTests(Fixture):
    def setUp(self):
        super().setUp()
        self.memory = self.root / 'data/qq/memory.db'
        MemoryStore(self.root, 'qq').save('主人喜欢安静', category='偏好', scope='owner',
                                          source='manual', source_text='测试写入')
        # The seeded memory is stamped now, so the periods under test are the real
        # ones; a fixed 2024 week would summarize an empty range instead.
        self.today = memory_digest._current('day')
        self.week = memory_digest._current('week')
        self.month = memory_digest._current('month')

    def tearDown(self):
        # Windows can still hold data/qq/memory.db while the temporary tree is being
        # removed. The tree lives under the temp folder and goes with the process, so
        # a locked file must not turn a passing assertion into an error.
        try:
            super().tearDown()
        except PermissionError:
            pass

    def test_period_keys_follow_the_real_calendar(self):
        start, end = memory_digest._period_bounds('week', '2024-W23')
        self.assertEqual(datetime.fromtimestamp(start).strftime('%Y-%m-%d'), '2024-06-03')
        self.assertEqual(int(end - start), 7 * 86400)
        start, end = memory_digest._period_bounds('month', '2024-02')
        self.assertEqual(datetime.fromtimestamp(start).strftime('%Y-%m-%d'), '2024-02-01')
        self.assertEqual(datetime.fromtimestamp(end).strftime('%Y-%m-%d'), '2024-03-01')
        for scope, value in (('day', '2024-13-01'), ('week', '2024-W54'), ('month', '2024-13'), ('hour', '2024-06')):
            with self.assertRaises(ValueError):
                memory_digest._period(scope, value)

    def test_each_scope_keeps_one_row_and_the_list_merges_them(self):
        with patch('model_gateway.ModelGateway.json', return_value={'digest': '写下来的一段'}):
            day = memory_digest.build(self.root, 'day', self.today)
            week = memory_digest.build(self.root, 'week', self.week)
            month = memory_digest.build(self.root, 'month', self.month)
            again = memory_digest.build(self.root, 'week', self.week, force=True)
        self.assertEqual((day['scope'], week['scope'], month['scope']), ('day', 'week', 'month'))
        self.assertEqual(again['text'], '写下来的一段')
        rows = memory_digest.list_digests(self.root)
        self.assertEqual(sorted(row['scope'] for row in rows), ['day', 'month', 'week'])
        self.assertEqual(len(memory_digest.list_digests(self.root, scope='week')), 1)
        self.assertEqual(memory_digest.list_digests(self.root, scope='week')[0]['day'], self.week)

    def test_revise_rewrites_that_one_paragraph_from_the_owner_note(self):
        with patch('model_gateway.ModelGateway.json', return_value={'digest': '原样的一段'}):
            memory_digest.build(self.root, 'month', self.month)
        seen = {}

        def rewrite(system, user, **kwargs):
            seen['system'] = system
            seen['payload'] = json.loads(user)
            return {'digest': '改过的一段'}

        with patch('model_gateway.ModelGateway.json', side_effect=rewrite):
            result = memory_digest.revise(self.root, 'month', self.month, '  六月的咖啡那次我不在  ')
        self.assertEqual(result['before'], '原样的一段')
        self.assertEqual(result['after'], '改过的一段')
        self.assertEqual(result['note'], '六月的咖啡那次我不在')
        self.assertEqual(seen['payload']['current_digest'], '原样的一段')
        self.assertEqual(seen['payload']['owner_note'], '六月的咖啡那次我不在')
        self.assertEqual(memory_digest.list_digests(self.root, scope='month')[0]['text'], '改过的一段')
        with self.assertRaises(ValueError):
            memory_digest.revise(self.root, 'month', self.month, '   ')

    def test_a_missing_period_is_written_before_it_is_corrected(self):
        calls = []

        def answer(system, user, **kwargs):
            calls.append(json.loads(user))
            return {'digest': '第一次' if len(calls) == 1 else '第二次'}

        with patch('model_gateway.ModelGateway.json', side_effect=answer):
            result = memory_digest.revise(self.root, 'day', self.today, '那天我没出门')
        self.assertEqual(result['before'], '第一次')
        self.assertEqual(result['after'], '第二次')
        self.assertEqual(calls[1]['current_digest'], '第一次')


class PersonaClearTests(Fixture):
    def setUp(self):
        super().setUp()
        self.contexts = SocialContext(self.root)
        self.library = PersonaLibrary(self.root)
        # Group-test tables live in the same context database; creating them here
        # is what a group test would have done before the clear ran.
        GroupTests(self.root)

    def _space(self, revision, name='窗口'):
        return self.contexts.create_space('qq', name, revision=revision, origin='owner-qq')

    def test_create_stores_a_persona_without_applying_it(self):
        live = self.contexts.ensure_default('qq')
        made = self.library.create('新写的她', '说话慢一点。')
        self.assertEqual(made['name'], '新写的她')
        self.assertEqual(self.contexts.persona(self.contexts.ensure_default('qq')['id'])['id'], live['id'])
        with self.assertRaises(ValueError):
            self.library.create('   ', '正文')

    def test_clear_backs_up_first_then_keeps_only_what_is_in_use(self):
        live = self.contexts.ensure_default('qq')
        space = self._space(live['id'], '主人窗口')
        self.contexts.select_owner_space(space['id'], self.owner)
        stale = self.contexts.save_persona('qq', '旧版本', '旧的正文')
        self._space(stale['id'], '旧窗口')
        busy = self.contexts.save_persona('qq', '群测试里的人格', '被群测试占着')
        busy_space = self._space(busy['id'], '群测试窗口')
        with database(self.contexts.path) as db:
            db.execute('INSERT INTO group_test_spaces(space,group_id,owner,runtime_digest) VALUES(?,?,?,?)',
                       (busy_space['id'], self.group, self.owner, ''))
            db.execute('INSERT INTO group_test_routes(group_id,space,owner,updated) VALUES(?,?,?,?)',
                       (self.group, busy_space['id'], self.owner, time.time()))
        result = self.library.clear()
        self.assertEqual(result['kept']['id'], live['id'])
        self.assertEqual([row['id'] for row in result['protected']], [busy['id']])
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(result['windows'], 1)
        remaining = {row['id'] for row in self.contexts.personas('qq')}
        self.assertEqual(remaining, {live['id'], busy['id']})
        backup = Path(result['backup'])
        self.assertTrue(backup.is_file())
        self.assertTrue(backup.is_relative_to(self.root / 'backups'))
        payload = json.loads(backup.read_text(encoding='utf-8'))
        self.assertEqual({row['id'] for row in payload['personas']}, {live['id'], stale['id'], busy['id']})
        self.assertEqual(payload['kept'], live['id'])
        # The owner's window still resolves after the cleanup.
        self.assertEqual(self.contexts.owner_space(self.owner)['revision'], live['id'])


class WorkbenchActionTests(Fixture):
    """The three new actions as the panel calls them, through the real dispatcher."""

    def setUp(self):
        super().setUp()
        MemoryStore(self.root, 'qq').save('主人喜欢安静', category='偏好', scope='owner',
                                          source='manual', source_text='测试写入')
        from workbench import Workbench
        self.workbench = Workbench(self.root)
        self.week = memory_digest._current('week')

    def tearDown(self):
        try:
            super().tearDown()
        except PermissionError:
            pass

    def test_the_panel_actions_route_to_their_handlers(self):
        made = self.workbench.dispatch('personas.create', {'name': '面板新人格', 'body': '说话慢一点。'})
        self.assertEqual(made['name'], '面板新人格')
        cleared = self.workbench.dispatch('personas.clear', {})
        self.assertEqual(set(cleared), {'kept', 'deleted', 'backup', 'protected', 'windows'})
        self.assertTrue(Path(cleared['backup']).is_file())
        with patch('model_gateway.ModelGateway.json', return_value={'digest': '这一周的一段'}):
            ran = self.workbench.dispatch('memory.digest.run', {'scope': 'week', 'force': True})
            self.assertEqual(ran['digest']['scope'], 'week')
            self.assertEqual(ran['digest']['day'], self.week)
            revised = self.workbench.dispatch('memory.digest.revise',
                                              {'scope': 'week', 'period': self.week, 'note': '这周我没出门'})
        self.assertEqual(revised['before'], '这一周的一段')
        self.assertEqual(revised['after'], '这一周的一段')
        listed = self.workbench.dispatch('memory.digest.list', {'scope': 'week'})
        self.assertEqual([row['day'] for row in listed], [self.week])


if __name__ == '__main__':
    unittest.main()
