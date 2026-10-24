"""Offline checks for persona selection and reference-preserving cleanup."""
import json
import multiprocessing
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import storage
from desktop_chat import DesktopChat
from group_tests import GroupTests, test_scope
from memory_store import MemoryStore
from persona_library import PersonaLibrary
from social_context import SocialContext
from storage import database


def _new_desktop_in_child(root, revision, attempted, acquired, connection):
    """Spawn target: observe the real file lock without changing runtime code."""
    original = storage.settings_lock

    @contextmanager
    def observed_lock(*args, **kwargs):
        attempted.set()
        with original(*args, **kwargs):
            acquired.set()
            yield

    try:
        chat = DesktopChat(Path(root), 'qq', None)
        with patch('storage.settings_lock', observed_lock):
            cid = chat.new('跨进程创建', revision=revision)
        connection.send(('ok', cid))
    except Exception as exc:
        connection.send(('error', repr(exc)))
    finally:
        connection.close()


class PersonaSelectionTests(unittest.TestCase):
    def test_apply_keeps_history_and_switches_revision(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            (root / 'harness/agents').mkdir(parents=True)
            (root / 'channels/fish_qq').mkdir(parents=True)
            (root / 'channels/fish_qq/config.json').write_text(
                '{"allowlist":{"owner_id":"200000001"}}', encoding='utf-8')
            (root / 'harness/agents.json').write_text(
                '{"agents":[{"id":"qq","name":"Test","persona":"harness/agents/qq.persona.md"}]}',
                encoding='utf-8')
            (root / 'harness/agents/qq.persona.md').write_text('base persona', encoding='utf-8')

            contexts = SocialContext(root)
            base = contexts.ensure_default('qq')
            owner_space = contexts.create_space('qq', 'owner', revision=base['id'], origin='owner-qq')
            contexts.select_owner_space(owner_space['id'], '200000001')
            memory = MemoryStore(root, 'qq')
            fact = memory.save('主人喜欢先看结论。', scope='owner', source='manual')

            library = PersonaLibrary(root)
            selected = library.add('新人格', '新的表达方式。', source='test')
            result = library.apply(selected['id'])

            self.assertEqual(result['revision'], selected['id'])
            self.assertEqual(contexts.space(owner_space['id'])['revision'], selected['id'])
            self.assertIn('新的表达方式', contexts.prompt(owner_space['id']))
            self.assertEqual(contexts.persona(base['id'])['body'], 'base persona')
            self.assertIn(fact['id'], [row['id'] for row in memory.search('结论', scope='owner')])


class PersonaClearReferenceTests(unittest.TestCase):
    owner = '200000001'
    group = '300000001'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self._configure(self.root)
        self.contexts = SocialContext(self.root)
        self.library = PersonaLibrary(self.root)

    def _configure(self, root):
        (root / 'harness/agents').mkdir(parents=True)
        (root / 'channels/fish_qq').mkdir(parents=True)
        (root / 'channels/fish_qq/config.json').write_text(json.dumps({
            'allowlist': {'owner_id': self.owner, 'group_ids': [self.group]}}), encoding='utf-8')
        (root / 'harness/agents.json').write_text(json.dumps({'agents': [
            {'id': 'qq', 'name': 'Test', 'persona': 'harness/agents/qq.persona.md'},
            {'id': 'research', 'name': 'Research', 'persona': 'harness/agents/research.persona.md'}
        ]}), encoding='utf-8')
        (root / 'harness/agents/qq.persona.md').write_text('base persona', encoding='utf-8')

    def test_clear_preserves_three_desktop_conversations_and_both_reference_columns(self):
        keep = self.contexts.ensure_default('qq')
        by_both = self.library.add('双列相同', '第一条桌面对话')
        by_space = self.library.add('只由窗口引用', '第二条桌面窗口')
        by_revision = self.library.add('只由版本引用', '第二和第三条桌面版本')
        unused = self.library.add('未使用', '可以清理')
        old_space = self.contexts.create_space('qq', revision=unused['id'])
        chat = DesktopChat(self.root, 'qq', None)
        first = chat.new('双列相同', revision=by_both['id'])
        second = chat.new('双列不同', revision=by_space['id'])
        third = chat.new('版本独立引用', revision=by_revision['id'])
        self.contexts.change_persona(chat.conversation_context(third)['id'], keep['id'])
        with closing(chat.connect()) as db:
            db.execute('UPDATE conversations SET revision=? WHERE id=?',
                       (by_revision['id'], second))
            db.commit()
            before = [dict(row) for row in db.execute(
                'SELECT id,space_id,revision FROM conversations ORDER BY id')]

        result = self.library.clear(keep['id'])

        self.assertEqual(result['deleted'], 1)
        self.assertEqual(result['windows'], 1)
        protected = {row['id']: row for row in result['protected']}
        self.assertEqual({key: row['reference_count'] for key, row in protected.items()}, {
            by_both['id']: 1, by_space['id']: 1, by_revision['id']: 2})
        self.assertTrue(all(isinstance(row['reference_count'], int) and
                            '桌面对话' in row['reason'] for row in protected.values()))
        with closing(chat.connect()) as db:
            after = [dict(row) for row in db.execute(
                'SELECT id,space_id,revision FROM conversations ORDER BY id')]
        self.assertEqual(after, before)
        self.assertEqual({row['id'] for row in after}, {first, second, third})
        self.assertEqual(len({row['space_id'] for row in after}), 3)
        for row in after:
            self.assertTrue(row['space_id'])
            self.assertEqual(self.contexts.persona(row['revision'])['id'], row['revision'])
            self.assertEqual(chat.conversation_context(row['id'])['id'], row['space_id'])
        with self.assertRaisesRegex(ValueError, '窗口不存在'):
            self.contexts.space(old_space['id'])
        with self.assertRaisesRegex(ValueError, '人格版本不存在'):
            self.contexts.persona(unused['id'])

    def test_fresh_root_clear_without_legacy_tables_does_not_create_desktop_database(self):
        first = self.library.clear()
        self.assertEqual(first['deleted'], 0)
        self.assertEqual(first['windows'], 0)
        unused = self.library.add('全新根的闲置人格', '可以清理')
        old_space = self.contexts.create_space('qq', revision=unused['id'])
        second = self.library.clear(first['kept']['id'])
        self.assertEqual(second['deleted'], 1)
        self.assertEqual(second['windows'], 1)
        self.assertFalse((self.root / 'data/qq/desktop-chat.db').exists())
        with database(self.contexts.path) as db:
            names = {row['name'] for row in db.execute('SELECT name FROM sqlite_master')}
        self.assertNotIn('group_test_routes', names)
        self.assertNotIn('group_test_spaces', names)
        payload = json.loads(Path(second['backup']).read_text(encoding='utf-8'))
        self.assertEqual(payload['group_test_routes'], [])
        self.assertEqual(payload['group_test_spaces'], [])
        self.assertIn(old_space['id'], {row['id'] for row in payload['spaces']})

    def test_backup_restores_personas_spaces_and_owner_and_group_routes(self):
        keep = self.contexts.ensure_default('qq')
        self.contexts.set_default('qq', keep['id'])
        owner_persona = self.contexts.save_persona(
            'qq', '主人窗口人格', '保留窗口绑定', owner_reply_mode='thinking_excerpt')
        owner_space = self.contexts.create_space(
            'qq', '主人窗口', revision=owner_persona['id'], origin='owner-qq')
        self.contexts.select_owner_space(owner_space['id'], self.owner)
        group_persona = self.library.add('群窗口人格', '保留群绑定', source='test-group')
        group_space = self.contexts.create_space(
            'qq', '群窗口', revision=group_persona['id'], origin='group')
        GroupTests(self.root)
        with database(self.contexts.path) as db:
            db.execute('INSERT INTO group_test_spaces VALUES(?,?,?,?)',
                       (group_space['id'], self.group, self.owner, self.contexts.runtime_digest('qq')))
            db.execute('INSERT INTO group_test_routes VALUES(?,?,?,?)',
                       (self.group, group_space['id'], self.owner, time.time()))
            db.execute('INSERT INTO deployments VALUES(?,?,?,?,?)',
                       (self.group, group_persona['id'], 0, self.owner, time.time()))
            db.execute('INSERT INTO approvals(revision,owner,digest,created,runtime_digest) VALUES(?,?,?,?,?)',
                       (owner_persona['id'], self.owner, owner_persona['digest'], time.time(),
                        self.contexts.runtime_digest('qq')))
        unused = self.library.add('清掉但能恢复', '孤立的旧窗口', source='test-unused')
        old_space = self.contexts.create_space('qq', revision=unused['id'])
        research = self.contexts.save_persona('research', '研究人格', '另一个角色也必须备份')

        result = self.library.clear(keep['id'])

        self.assertEqual(result['deleted'], 1)
        payload = json.loads(Path(result['backup']).read_text(encoding='utf-8'))
        self.assertEqual(payload['version'], 2)
        self.assertEqual({row['id'] for row in payload['personas']},
                         {keep['id'], owner_persona['id'], group_persona['id'], unused['id'], research['id']})
        self.assertEqual(payload['sources'][unused['id']]['source'], 'test-unused')
        restored_root = Path(self.temp.name) / 'restored'
        self._configure(restored_root)
        restored = SocialContext(restored_root)
        PersonaLibrary(restored_root)
        groups = GroupTests(restored_root)
        tables = ('personas', 'persona_output_modes', 'default_personas', 'approvals',
                  'spaces', 'deployments', 'owner_routes', 'group_test_spaces', 'group_test_routes')
        with database(restored.path) as db:
            for table in tables:
                for row in payload[table]:
                    columns = list(row)
                    db.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' +
                               ','.join('?' for _ in columns) + ')', [row[key] for key in columns])
            for revision, source in payload['sources'].items():
                db.execute('INSERT INTO persona_sources VALUES(?,?,?)',
                           (revision, source['source'], source['format']))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(restored.owner_space(self.owner)['id'], owner_space['id'])
        self.assertEqual(restored.owner_space(self.owner)['revision'], owner_persona['id'])
        self.assertEqual(groups.current(self.group)['id'], group_space['id'])
        self.assertEqual(groups.current(self.group)['revision'], group_persona['id'])
        self.assertEqual(restored.space(old_space['id'])['revision'], unused['id'])
        self.assertEqual(restored.persona(research['id'])['role'], 'research')
        self.assertEqual(restored.persona(owner_persona['id'])['owner_reply_mode'], 'thinking_excerpt')
        self.assertEqual(restored.persona(owner_persona['id'])['approved_owner'], self.owner)

    def test_unreadable_desktop_references_abort_without_deleting_personas(self):
        keep = self.contexts.ensure_default('qq')
        unused = self.library.add('不得误删', '引用扫描失败时保留')
        desktop = self.root / 'data/qq/desktop-chat.db'
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_bytes(b'not a sqlite database')
        with self.assertRaisesRegex(ValueError, '引用检查失败，已停止人格清理'):
            self.library.clear(keep['id'])
        self.assertEqual(self.contexts.persona(unused['id'])['id'], unused['id'])

    def test_tool_identities_preserve_space_group_scope_and_session_bindings(self):
        keep = self.contexts.ensure_default('qq')
        direct = self.library.add('工具空间', 'scope 与 session 指向同一窗口只计一次')
        trial = self.library.add('群工具空间', '群测试 scope 仍有引用')
        session_only = self.library.add('工具会话', '只按 runtime session 找到窗口')
        unused = self.library.add('没有工具引用', '可以清理')
        direct_space = self.contexts.create_space('qq', revision=direct['id'])
        trial_space = self.contexts.create_space('qq', revision=trial['id'], origin='group')
        session_space = self.contexts.create_space('qq', revision=session_only['id'])
        self.contexts.bind_runtime(direct_space['id'], 'direct-session')
        self.contexts.bind_runtime(session_space['id'], 'session-only')
        with database(self.root / 'data/tool-host.db') as db:
            db.execute('CREATE TABLE identities(role TEXT,session TEXT,scope TEXT,owner INTEGER,'
                       'desktop INTEGER,sender TEXT,updated REAL,PRIMARY KEY(role,session))')
            db.executemany('INSERT INTO identities VALUES(?,?,?,?,?,?,?)', [
                ('qq', 'direct-session', 'space:' + direct_space['id'], 1, 1, self.owner, time.time()),
                ('qq', 'trial-session', test_scope(self.group, trial_space['id']), 1, 0, self.owner, time.time()),
                ('qq', 'session-only', 'owner', 1, 0, self.owner, time.time())])

        result = self.library.clear(keep['id'])

        self.assertEqual(result['deleted'], 1)
        self.assertEqual({row['id']: row['references'] for row in result['protected']}, {
            direct['id']: {'工具会话': 1}, trial['id']: {'工具会话': 1},
            session_only['id']: {'工具会话': 1}})
        for space in (direct_space, trial_space, session_space):
            self.assertEqual(self.contexts.space(space['id'])['revision'], space['revision'])
        with self.assertRaisesRegex(ValueError, '人格版本不存在'):
            self.contexts.persona(unused['id'])

    def test_backup_file_failure_stops_before_any_deletion(self):
        keep = self.contexts.ensure_default('qq')
        unused = self.library.add('备份失败不得删除', '完整保留')
        space = self.contexts.create_space('qq', revision=unused['id'])
        (self.root / 'backups').write_text('普通文件挡住备份目录', encoding='utf-8')

        with self.assertRaisesRegex(ValueError, '人格备份失败，未执行清理'):
            self.library.clear(keep['id'])

        self.assertEqual(self.contexts.persona(unused['id'])['id'], unused['id'])
        self.assertEqual(self.contexts.space(space['id'])['revision'], unused['id'])

    def test_unknown_keep_is_rejected_without_deleting_personas(self):
        keep = self.contexts.ensure_default('qq')
        unused = self.library.add('明确保留错误不能代替选择', '不得删除')
        with self.assertRaisesRegex(ValueError, '要保留的 QQ 人格版本不存在'):
            self.library.clear('0' * 32)
        self.assertEqual({row['id'] for row in self.contexts.personas('qq')},
                         {keep['id'], unused['id']})

    def _observed_lock(self, waiting_name, attempted, acquired, order):
        original = storage.settings_lock

        @contextmanager
        def observed(*args, **kwargs):
            name = threading.current_thread().name
            if name == waiting_name:
                order.append(name + '尝试进锁')
                attempted.set()
            with original(*args, **kwargs):
                order.append(name + '持锁')
                if name == waiting_name:
                    acquired.set()
                yield
                order.append(name + '事务完成')
        return observed

    def _start_worker(self, name, function, results, errors):
        def run():
            try:
                results[name] = function()
            except Exception as exc:
                errors.append((name, repr(exc)))
        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        return thread

    def _join_workers(self, threads, errors):
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads), '并发操作未正常结束')
        self.assertEqual(errors, [])

    def test_creation_holds_lock_until_desktop_commit_before_clear(self):
        keep = self.contexts.ensure_default('qq')
        selected = self.library.add('正在创建的桌面人格', '创建完成前不能清理')
        unused = self.library.add('闲置人格', '仍应清理')
        self.contexts.create_space('qq', revision=unused['id'])
        chat = DesktopChat(self.root, 'qq', None)
        space_committed, release = threading.Event(), threading.Event()
        attempted, acquired = threading.Event(), threading.Event()
        order, results, errors, threads = [], {}, [], []
        original = SocialContext.create_space

        def pause_after_space(context, *args, **kwargs):
            space = original(context, *args, **kwargs)
            order.append('空间已提交，对话未提交')
            space_committed.set()
            if not release.wait(10):
                raise TimeoutError('创建测试未收到释放信号')
            return space

        with patch('storage.settings_lock', self._observed_lock('清理', attempted, acquired, order)), \
                patch.object(SocialContext, 'create_space', pause_after_space):
            try:
                threads.append(self._start_worker('创建', lambda: chat.new(
                    '并发桌面对话', revision=selected['id']), results, errors))
                self.assertTrue(space_committed.wait(5), '创建未到达空间提交点')
                threads.append(self._start_worker('清理', lambda: self.library.clear(keep['id']), results, errors))
                self.assertTrue(attempted.wait(5), '清理未尝试进锁')
                self.assertFalse(acquired.is_set(), '清理提前进入了创建事务')
                self.assertNotIn('清理', results)
                order.append('确认清理等待，释放创建')
            finally:
                release.set()
                self._join_workers(threads, errors)

        cid, cleared = results['创建'], results['清理']
        space = chat.conversation_context(cid)
        self.assertEqual(space['revision'], selected['id'])
        with closing(chat.connect()) as db:
            revision = db.execute('SELECT revision FROM conversations WHERE id=?', (cid,)).fetchone()[0]
        self.assertEqual(self.contexts.persona(revision)['id'], selected['id'])
        self.assertIn(selected['id'], {row['id'] for row in cleared['protected']})
        self.assertEqual(cleared['deleted'], 1)
        with self.assertRaisesRegex(ValueError, '人格版本不存在'):
            self.contexts.persona(unused['id'])
        self.assertLess(order.index('创建事务完成'), order.index('清理持锁'))
        print('X10 创建先行：' + ' → '.join(order) +
              '；结果：新对话空间和人格可解析，已保护；闲置人格删除1条。', flush=True)

    def test_clear_holds_lock_before_new_then_both_finish_without_dangling_space(self):
        keep = self.contexts.ensure_default('qq')
        unused = self.library.add('清理先行的闲置人格', '仍需真实删除')
        self.contexts.create_space('qq', revision=unused['id'])
        chat = DesktopChat(self.root, 'qq', None)
        backing_up, release = threading.Event(), threading.Event()
        attempted, acquired = threading.Event(), threading.Event()
        order, results, errors, threads = [], {}, [], []
        original = self.library._backup

        def pause_backup(*args, **kwargs):
            order.append('清理已进入备份点')
            backing_up.set()
            if not release.wait(10):
                raise TimeoutError('清理测试未收到释放信号')
            return original(*args, **kwargs)

        with patch('storage.settings_lock', self._observed_lock('创建', attempted, acquired, order)), \
                patch.object(self.library, '_backup', side_effect=pause_backup):
            try:
                threads.append(self._start_worker('清理', lambda: self.library.clear(keep['id']), results, errors))
                self.assertTrue(backing_up.wait(5), '清理未到达备份点')
                threads.append(self._start_worker('创建', lambda: chat.new(
                    '清理后的桌面对话', revision=keep['id']), results, errors))
                self.assertTrue(attempted.wait(5), '创建未尝试进锁')
                self.assertFalse(acquired.is_set(), '创建提前进入了清理事务')
                self.assertNotIn('创建', results)
                order.append('确认创建等待，释放清理')
            finally:
                release.set()
                self._join_workers(threads, errors)

        self.assertEqual(results['清理']['deleted'], 1)
        space = chat.conversation_context(results['创建'])
        self.assertEqual(space['revision'], keep['id'])
        with closing(chat.connect()) as db:
            revision = db.execute('SELECT revision FROM conversations WHERE id=?',
                                  (results['创建'],)).fetchone()[0]
        self.assertEqual(self.contexts.persona(revision)['id'], keep['id'])
        with self.assertRaisesRegex(ValueError, '人格版本不存在'):
            self.contexts.persona(unused['id'])
        with self.assertRaisesRegex(ValueError, '人格版本不存在'):
            chat.new('不能复活已删人格', revision=unused['id'])
        self.assertLess(order.index('清理事务完成'), order.index('创建持锁'))
        print('X10 清理先行：' + ' → '.join(order) +
              '；结果：闲置人格删除1条，新对话创建完成且空间和人格可解析。', flush=True)

    def test_new_obeys_settings_file_lock_in_another_process(self):
        keep = self.contexts.ensure_default('qq')
        context = multiprocessing.get_context('spawn')
        attempted, acquired = context.Event(), context.Event()
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_new_desktop_in_child,
                                  args=(str(self.root), keep['id'], attempted, acquired, child))
        order = ['父进程持锁']
        try:
            with storage.settings_lock(self.root):
                process.start()
                child.close()
                self.assertTrue(attempted.wait(10), '子进程未尝试进锁')
                order.append('子进程已尝试进锁')
                self.assertFalse(acquired.wait(.2), '子进程绕过了文件锁')
                self.assertFalse(parent.poll(), '子进程在锁释放前完成')
                order.append('确认跨进程等待，释放父进程锁')
            self.assertTrue(parent.poll(10), '子进程未返回创建结果')
            status, value = parent.recv()
            self.assertEqual(status, 'ok', value)
            self.assertTrue(acquired.is_set())
            order.append('子进程持锁并完成创建')
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            chat = DesktopChat(self.root, 'qq', None)
            self.assertEqual(chat.conversation_context(value)['revision'], keep['id'])
            print('X10 跨进程：' + ' → '.join(order) + '；结果：空间和人格可解析。', flush=True)
        finally:
            if process.pid is not None:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            parent.close()
            child.close()

    def test_reload_without_space_commits_revision_before_clear_and_is_protected(self):
        keep = self.contexts.ensure_default('qq')
        selected = self.library.add('重载目标 Q', '桌面对话尚未绑定空间')
        self.library.add('可清理的人格 R', '仍需真实删除')
        chat = DesktopChat(self.root, 'qq', None)
        cid = chat.new('无空间对话', isolated=False, revision=keep['id'])
        validated, release = threading.Event(), threading.Event()
        attempted, acquired = threading.Event(), threading.Event()
        order, results, errors, threads = [], {}, [], []
        original = SocialContext.persona

        def pause_after_validation(context, revision):
            persona = original(context, revision)
            if revision == selected['id'] and threading.current_thread().name == '重载':
                order.append('重载已验证 Q，对话尚未更新')
                validated.set()
                if not release.wait(10):
                    raise TimeoutError('重载测试未收到释放信号')
            return persona

        with patch('storage.settings_lock', self._observed_lock('清理', attempted, acquired, order)), \
                patch.object(SocialContext, 'persona', pause_after_validation):
            try:
                threads.append(self._start_worker('重载', lambda: chat.reload_persona(
                    cid, selected['id']), results, errors))
                self.assertTrue(validated.wait(5), '重载未到达人格验证完成点')
                threads.append(self._start_worker('清理', lambda: self.library.clear(keep['id']), results, errors))
                self.assertTrue(attempted.wait(5), '清理未尝试进锁')
                self.assertFalse(acquired.is_set(), '清理提前进入了重载事务')
                order.append('确认清理等待，释放重载')
            finally:
                release.set()
                self._join_workers(threads, errors)

        self.assertTrue(results['重载']['ok'])
        with closing(chat.connect()) as db:
            row = db.execute('SELECT space_id,revision FROM conversations WHERE id=?', (cid,)).fetchone()
        self.assertEqual(row['space_id'], '')
        self.assertEqual(row['revision'], selected['id'])
        self.assertEqual(self.contexts.persona(row['revision'])['id'], selected['id'])
        self.assertIn(selected['id'], {item['id'] for item in results['清理']['protected']})
        self.assertEqual(results['清理']['deleted'], 1)
        self.assertLess(order.index('重载事务完成'), order.index('清理持锁'))
        print('X10 无空间重载先行：' + ' → '.join(order) +
              '；结果：revision=Q 且可解析，Q 被保护，闲置人格删除1条。', flush=True)

    def test_clear_before_reload_without_space_rejects_deleted_revision(self):
        keep = self.contexts.ensure_default('qq')
        selected = self.library.add('将被清理的 Q', '没有任何存续引用')
        chat = DesktopChat(self.root, 'qq', None)
        cid = chat.new('无空间旧对话', isolated=False, revision=keep['id'])
        backing_up, release = threading.Event(), threading.Event()
        attempted, acquired = threading.Event(), threading.Event()
        order, results, errors, threads = [], {}, [], []
        original = self.library._backup

        def pause_backup(*args, **kwargs):
            order.append('清理已进入备份点')
            backing_up.set()
            if not release.wait(10):
                raise TimeoutError('清理测试未收到释放信号')
            return original(*args, **kwargs)

        def reload_deleted():
            try:
                chat.reload_persona(cid, selected['id'])
            except ValueError as exc:
                order.append('重载明确失败：' + str(exc))
                return str(exc)
            raise AssertionError('已删除的人格 Q 不应重载成功')

        with patch('storage.settings_lock', self._observed_lock('重载', attempted, acquired, order)), \
                patch.object(self.library, '_backup', side_effect=pause_backup):
            try:
                threads.append(self._start_worker('清理', lambda: self.library.clear(keep['id']), results, errors))
                self.assertTrue(backing_up.wait(5), '清理未到达备份点')
                threads.append(self._start_worker('重载', reload_deleted, results, errors))
                self.assertTrue(attempted.wait(5), '重载未尝试进锁')
                self.assertFalse(acquired.is_set(), '重载提前进入了清理事务')
                order.append('确认重载等待，释放清理')
            finally:
                release.set()
                self._join_workers(threads, errors)

        self.assertEqual(results['清理']['deleted'], 1)
        self.assertEqual(results['重载'], '人格版本不存在')
        with closing(chat.connect()) as db:
            row = db.execute('SELECT space_id,revision FROM conversations WHERE id=?', (cid,)).fetchone()
        self.assertEqual(row['space_id'], '')
        self.assertEqual(row['revision'], keep['id'])
        self.assertEqual(self.contexts.persona(row['revision'])['id'], keep['id'])
        with self.assertRaisesRegex(ValueError, '人格版本不存在'):
            self.contexts.persona(selected['id'])
        self.assertLess(order.index('清理事务完成'), order.index('重载持锁'))
        print('X10 无空间清理先行：' + ' → '.join(order) +
              '；结果：Q 已删除，重载明确中文失败，原 revision 保持可解析。', flush=True)


if __name__ == '__main__':
    unittest.main()
