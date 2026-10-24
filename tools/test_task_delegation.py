"""Focused deterministic tests of task identity, final states and real artifact bytes."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from task_delegation import TaskDelegation
from storage import database
import delegation_artifacts


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'harness').mkdir()
        self.started, self.release = threading.Event(), threading.Event()
        self.block = False
        self.created = []
        owner = self
        class FakeRun:
            def __init__(self, root, directory, session, config, cancelled):
                self.directory, self.session = directory, session
                self.stopped = threading.Event()
                owner.created.append(self)
            def run(self, prompt, notify):
                notify('running', {'session': self.session})
                owner.started.set()
                if owner.block:
                    owner.release.wait(3)
                work = self.directory / 'workspace'
                work.mkdir(parents=True, exist_ok=True)
                (work / 'article.md').write_text('真实附件字节，用于单元测试。', encoding='utf-8')
                (work / 'fish-artifacts.json').write_text('{"files":["article.md"]}', encoding='utf-8')
                return SimpleNamespace(final_response='完成', finish_reason='completed', events=[])
            def close(self):
                self.stopped.set()
                owner.release.set()
        self.fake = patch('task_delegation.DSHDelegatedRun', FakeRun)
        self.config = patch('task_delegation.execution_config', return_value={'timeout_seconds': 30, 'model': 'unit-test'})
        self.fake.start(); self.config.start()
        self.addCleanup(self.fake.stop); self.addCleanup(self.config.stop)
        self.service = TaskDelegation(self.root)
        self.addCleanup(self.service.close)

    def submit(self, request='a'*32, text='生成文章'):
        return self.service.submit(request, '测试', text)

    def settled(self, task):
        for _ in range(100):
            row = self.service.get(task)
            if row['status'] not in ('queued', 'starting', 'running', 'cancel_requested'):
                return row
            time.sleep(.02)
        self.fail('task did not settle')

    def test_verified_artifact_download_returns_archived_bytes(self):
        row = self.settled(self.submit()['id'])
        self.assertEqual((row['status'], row['artifact_status']), ('completed', 'ready'))
        artifact = row['artifacts'][0]
        Path(artifact['source_path']).write_text('之后修改源文件', encoding='utf-8')
        response = self.service.download(row['id'], artifact['id'])
        import base64
        self.assertEqual(base64.b64decode(response['data']).decode(), '真实附件字节，用于单元测试。')
        self.assertEqual(self.service.get(row['id'])['artifact_status'], 'served')
        with self.assertRaises(ValueError):
            self.service.download(row['id'], 'unrelated-attachment')

    def test_submission_identity_is_stable_but_same_text_can_be_new_task(self):
        first = self.submit()
        second = self.submit()
        self.assertEqual(first['id'], second['id'])
        self.settled(first['id'])
        third = self.submit('b'*32)
        self.assertNotEqual(first['id'], third['id'])
        self.settled(third['id'])
        self.assertEqual(len(self.created), 2)
        with self.assertRaises(ValueError):
            self.submit(text='编号相同但意图不同')

    def test_cancelled_runtime_cannot_be_overwritten_by_late_completion(self):
        self.block = True
        task = self.submit()['id']
        self.assertTrue(self.started.wait(2))
        row = self.service.cancel(task)
        self.assertIn(row['status'], ('cancel_requested', 'cancelled'))
        row = self.settled(task)
        self.assertEqual(row['status'], 'cancelled')
        time.sleep(.05)
        self.assertEqual(self.service.get(task)['status'], 'cancelled')
        self.assertEqual(self.service.get(task)['artifacts'], [])
        self.assertTrue(self.created[0].stopped.is_set())

    def test_timeout_has_unknown_outcome_without_retry(self):
        self.block = True
        task = self.submit()['id']
        self.assertTrue(self.started.wait(2))
        self.service.cancel(task, timeout=True)
        row = self.settled(task)
        self.assertEqual(row['status'], 'unknown')
        self.assertIn('超时', row['error'])
        self.assertEqual(len(self.created), 1)

    def test_restart_marks_unconfirmed_task_unknown_without_replaying(self):
        task = self.submit()['id']
        self.settled(task)
        with database(self.service.path) as db:
            db.execute("UPDATE tasks SET status='running' WHERE id=?", (task,))
        restarted = TaskDelegation(self.root)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.get(task)['status'], 'unknown')
        self.assertEqual(len(self.created), 1)

    def test_disabled_owner_permission_prevents_start(self):
        (self.root / 'harness/control.json').write_text('{"owner_permissions":[]}', encoding='utf-8')
        with self.assertRaises(PermissionError):
            self.submit()
        self.assertEqual(self.created, [])

    def test_claimed_path_outside_workspace_is_rejected_before_import(self):
        directory = self.root / 'task'
        workspace = directory / 'workspace'
        workspace.mkdir(parents=True)
        (directory / 'outside.md').write_text('outside', encoding='utf-8')
        (workspace / 'fish-artifacts.json').write_text('{"files":["../outside.md"]}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '工作目录'):
            delegation_artifacts.collect(self.root, 'a'*32, directory, [])
        self.assertFalse((self.root / 'data/qq/attachments').exists())

    def test_model_path_text_is_not_an_output_manifest(self):
        directory = self.root / 'task'
        (directory / 'workspace').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, '成果清单'):
            delegation_artifacts.collect(self.root, 'a'*32, directory,
                [{'type': 'assistant/message', 'data': {'text': 'C:/somewhere/result.md'}}])

    def test_startup_exception_settles_instead_of_staying_running(self):
        with patch('task_delegation.DSHDelegatedRun', side_effect=RuntimeError('startup failed')):
            row = self.settled(self.submit()['id'])
        self.assertEqual(row['status'], 'failed')
        self.assertIn('startup failed', row['error'])


if __name__ == '__main__':
    unittest.main()
