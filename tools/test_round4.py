"""Maintenance failure/progress boundaries; synthetic local data only."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import AsyncMock, Mock, patch
import asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from updater import prepare_release


def release(path, corrupt=False):
    values = {'Fish.exe':b'synthetic test executable - never run', '_internal/data.txt':b'fixture'}
    manifest = {'product':'Fish','format':1,'version':'0.3.2','files':{name:hashlib.sha256(value).hexdigest() for name,value in values.items()}}
    if corrupt: manifest['files']['Fish.exe']='0'*64
    with zipfile.ZipFile(path,'w') as z:
        for name,value in values.items():z.writestr(name,value)
        z.writestr('fish-release.json',json.dumps(manifest))


class MaintenanceTests(unittest.TestCase):
    def test_installed_program_recovers_profile_without_launch_environment(self):
        from fish_desktop.bootstrap import initialize, locate_root
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp); root = initialize(work / 'personal')
            profile = work / 'profile'; app = profile / 'FishApp'
            app.mkdir(parents=True)
            (app / 'location.json').write_text(json.dumps({'root': str(root)}), encoding='utf-8')
            exe = app / 'releases/Fish-test/Fish.exe'; exe.parent.mkdir(parents=True); exe.touch()
            with patch.dict(os.environ, {'LOCALAPPDATA': str(work / 'unrelated-global')}), \
                    patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)):
                os.environ.pop('FISH_HARNESS_HOME', None)
                self.assertEqual(locate_root(), root)
                self.assertEqual(Path(os.environ['LOCALAPPDATA']), profile)

    def test_explicit_data_root_wins_while_installed_profile_is_retained(self):
        from fish_desktop.bootstrap import initialize, locate_root
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp); root = initialize(work / 'explicit')
            profile = work / 'profile'; app = profile / 'FishApp'; app.mkdir(parents=True)
            (app / 'location.json').write_text(json.dumps({'root': str(work / 'unused')}), encoding='utf-8')
            exe = app / 'releases/Fish-test/Fish.exe'; exe.parent.mkdir(parents=True); exe.touch()
            with patch.dict(os.environ, {'LOCALAPPDATA': str(work / 'global'), 'FISH_HARNESS_HOME': str(root)}), \
                    patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)):
                self.assertEqual(locate_root(), root)
                self.assertEqual(Path(os.environ['LOCALAPPDATA']), profile)

    def test_successful_maintenance_startup_remembers_data_for_later_manual_launch(self):
        self._maintenance_startup(True)

    def test_failed_maintenance_does_not_replace_existing_selected_directory(self):
        self._maintenance_startup(False)

    def _maintenance_startup(self, ok):
        from fish_desktop.bootstrap import initialize,select_root,selected_root,locate_root
        from fish_desktop.app import main
        with tempfile.TemporaryDirectory() as tmp:
            parent=Path(tmp);root=initialize(parent/'personal');other=initialize(parent/'other')
            (root/'harness/setup.json').write_text(json.dumps({'completed':True}))
            result=root/'harness/maintenance/result-test.json';result.parent.mkdir(parents=True)
            result.write_text(json.dumps({'ok':ok,'error':'fixture failed maintenance' if not ok else ''}))
            env={'LOCALAPPDATA':str(parent/'local'),'FISH_MAINTENANCE_RESULT':str(result)}
            with patch.dict(os.environ,env),patch.object(sys,'argv',['Fish.exe']):
                select_root(other)
                service=Mock(root=root);application=Mock();application.exec.return_value=0
                with patch('PySide6.QtWidgets.QApplication',return_value=application),patch('PySide6.QtGui.QFontDatabase.addApplicationFont'),\
                     patch('fish_desktop.services.FishServices',return_value=service),patch('fish_desktop.main_window.MainWindow'),\
                     patch('fish_desktop.app._acquire_single_instance',return_value=True),patch('PySide6.QtWidgets.QMessageBox.information'),patch('PySide6.QtWidgets.QMessageBox.warning'):
                    self.assertEqual(main(),0)
                self.assertEqual(selected_root(),root if ok else other)
                # Simulate a later launch from outside the project, without the
                # environment inherited from the maintenance helper.
                os.environ.pop('FISH_HARNESS_HOME',None)
                with patch.object(sys,'frozen',True,create=True),patch.object(sys,'executable',str(parent/'installed/Fish.exe')):
                    self.assertEqual(locate_root(),root if ok else other)

    def test_restore_recreates_empty_role_homes_before_runtime_preflight(self):
        from fish_desktop.bootstrap import initialize
        from backup import BackupManager
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);source=initialize(work/'source');target=work/'restored'
            BackupManager(source).create(work/'backup.zip')
            BackupManager(target).restore(work/'backup.zip')
            self.assertFalse((target/'homes/qq').exists())
            initialize(target)
            self.assertTrue((target/'homes/qq').is_dir())
            self.assertTrue((target/'homes/research').is_dir())
            self.assertTrue((target/'harness/runtime/fish-control/cordis.patch.yml').is_file())

    def test_partial_control_install_retries_instead_of_trusting_leftover_link(self):
        from fish_desktop.bootstrap import initialize
        from core import HarnessConfig,RuntimeManager
        from unittest.mock import patch
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root=initialize(Path(tmp));runtime=RuntimeManager(HarnessConfig(root))
            profile=runtime.profile_dir('qq');link=profile/'node_modules/fish-control';link.mkdir(parents=True)
            (link/'cordis.patch.yml').write_text('[]')
            (profile/'package.json').write_text(json.dumps({'dependencies':{},'dsh':{'profile':{'bundles':[]}}}))
            with patch.object(runtime,'_cmd',return_value=['synthetic-cli']),patch.object(runtime,'run_cli',return_value=subprocess.CompletedProcess([],0,'','')) as run:
                result=runtime.ensure_control_plugin('qq')
            self.assertTrue(result['ok']);run.assert_called_once()

    def test_install_reports_complete_only_after_checksums_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp); release(work/'release.zip'); events=[]
            result=prepare_release(work/'release.zip',work/'releases',progress=lambda current,total:events.append((current,total)))
            self.assertEqual(events[0],(0,2));self.assertEqual(events[-1],(2,2))
            self.assertTrue(Path(result['exe']).is_file())

    def test_checksum_failure_does_not_report_completion_or_replace_running_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);release(work/'release.zip',corrupt=True);events=[]
            running=work/'Fish.exe';running.write_bytes(b'old running build')
            with self.assertRaisesRegex(ValueError,'校验失败'):
                prepare_release(work/'release.zip',work/'releases',progress=lambda current,total:events.append((current,total)))
            self.assertNotIn((2,2),events);self.assertEqual(running.read_bytes(),b'old running build')
            self.assertEqual(len(list((work/'releases').glob('*/INSTALL-FAILED.txt'))),1)

    def test_gui_busy_progress_nested_jobs_and_error_recovery(self):
        from PySide6.QtWidgets import QApplication
        from fish_desktop.data_settings import DataPage
        app=QApplication.instance() or QApplication([])
        def pump_until(predicate):
            end=time.monotonic()+8
            while time.monotonic()<end:
                app.processEvents()
                if predicate():return
                time.sleep(.01)
            self.fail('Qt callback timeout')
        with tempfile.TemporaryDirectory() as tmp:
            service=type('FixtureServices',(),{'root':Path(tmp)})()
            page=DataPage(service);page.show();app.processEvents()
            held=threading.Event();started=threading.Event()
            def extraction():
                page._progress_updates.append((3,9));started.set();held.wait(6);raise ValueError('synthetic invalid checksum')
            def previewed(_):page._submit(extraction,lambda _:None)
            try:
                page._submit(lambda:True,previewed)
                pump_until(lambda:started.is_set() and page.progress.maximum()==9)
                self.assertTrue(page.loading);self.assertEqual(page._pending_jobs,1)
                self.assertEqual(page.progress.value(),3)
                self.assertIn('3 / 9',page.toast.text());self.assertFalse(page.progress.isTextVisible())
                self.assertTrue(all(not button.isEnabled() for button in (page.backup_btn,page.restore_btn,page.upgrade_btn,page.location_btn,page.wizard_btn,page.diagnostic_btn)))
                held.set();pump_until(lambda:not page.loading)
                self.assertIn('synthetic invalid checksum',page.toast.text())
                self.assertTrue(page.progress.isHidden())
                self.assertTrue(all(button.isEnabled() for button in (page.backup_btn,page.restore_btn,page.upgrade_btn,page.location_btn,page.wizard_btn,page.diagnostic_btn)))
            finally:
                held.set();page.runner.shutdown();page.close()


class CompletionAuditTests(unittest.TestCase):
    def setUp(self):
        from fish_desktop.bootstrap import initialize
        from governance import Governance
        self.temp = tempfile.TemporaryDirectory(prefix='completion-audit-')
        self.root = initialize(Path(self.temp.name))
        self.policy = Governance(self.root)
        self.policy.save({'owner_permissions': ['status', 'memory.write']}, owner_id='12345678')

    def tearDown(self):
        self.temp.cleanup()

    def test_owner_prompt_has_current_enabled_rights_and_revokes_old_identity(self):
        first = self.policy.identity_prompt('12345678')
        self.assertIn('查看状态、保存记忆', first)
        self.assertNotIn('删除记忆', first)
        self.policy.save({'owner_permissions': ['memory.delete']})
        self.assertIn('删除记忆', self.policy.identity_prompt('12345678'))
        self.assertNotIn('查看状态', self.policy.identity_prompt('12345678'))
        self.assertNotIn('删除记忆', self.policy.identity_prompt('12345678', private=False))
        self.policy.save({}, owner_id='87654321')
        self.assertIn('无主人管理权限', self.policy.identity_prompt('12345678'))
        self.assertIn('删除记忆', self.policy.identity_prompt('87654321'))

    def test_console_full_permissions_and_qq_subset_are_distinct(self):
        self.policy.save({'owner_permissions': []})
        self.assertIn('主人尚未启用管理权限', self.policy.identity_prompt('12345678'))
        console = self.policy.identity_prompt(desktop=True)
        self.assertIn('客户端本地控制台', console)
        self.assertIn('删除记忆', console)

    def test_owner_status_lists_rights_but_ordinary_or_revoked_status_sends_nothing(self):
        from qq_agent_loop import QQAgentLoop
        loop = QQAgentLoop.__new__(QQAgentLoop)
        loop.governance, loop.owner = self.policy, '12345678'
        loop.queue = __import__('queue').Queue()
        loop.channel = Mock(); loop.channel.action = AsyncMock()
        payload = {'chat_type': 'private', 'message_id': 'fixture-status', 'segments': [{'type': 'text', 'text': '/状态'}]}
        self.assertTrue(asyncio.run(loop._admin_command(payload, '12345678')))
        text = loop.channel.action.call_args.args[1]['segments'][0]['text']
        self.assertIn('查看状态、保存记忆', text)
        self.assertNotIn('删除记忆', text)
        loop.channel.action.reset_mock()
        self.assertFalse(asyncio.run(loop._admin_command(payload, '87654321')))
        self.policy.save({'owner_permissions': []})
        self.assertTrue(asyncio.run(loop._admin_command(payload, '12345678')))
        loop.channel.action.assert_not_called()

    def test_sound_cues_are_not_speech_but_short_speech_and_lyrics_are(self):
        from research_pipeline import has_substantive_subtitles
        for text in ('[00:00:03] ♪ 音乐 ♪\n[00:00:07] ♪ 音乐 ♪', '(Music)\n[Applause]', '♪♫', '[00:00:02] 【背景音乐】', ''):
            with self.subTest(text=text): self.assertFalse(has_substantive_subtitles(text))
        for text in ('[00:00:01] 好', '♪ 这是实际歌词 ♪', '音乐理论', '[00:00:02] [Music]\n[00:00:03] 点击发送'):
            with self.subTest(text=text): self.assertTrue(has_substantive_subtitles(text))

    def worker_fixture(self, cached=''):
        from research_autonomy import ResearchWorker
        from research_store import ResearchStore
        video = {'bvid': 'BV0000000001', 'cid': 1, 'title': '前端测试', 'duration': 20,
                 'url': 'https://www.bilibili.com/video/BV0000000001'}
        adapter = Mock(); adapter.get_video.return_value = video
        adapter.get_subtitle.return_value = {'text': '[00:00:03] ♪ 音乐 ♪'}
        adapter.download_video.return_value = self.root/'synthetic-video.mp4'
        reviewer = Mock(); reviewer.review.return_value = {'summary': 'fixture'}
        worker = ResearchWorker(self.root, adapter, reviewer)
        worker.control.enqueue(video); job = worker.control.claim()
        if cached:
            store = ResearchStore(self.root); sid, _ = store.upsert_source(video, 0)
            store.record_observation(sid, 'subtitle_read', 'subtitle', cached, [])
        return worker, job, adapter, reviewer

    def test_cached_music_only_subtitles_trigger_video_and_sampled_report(self):
        worker, job, adapter, reviewer = self.worker_fixture('[00:00:03] ♪ 音乐 ♪')
        with patch('research_pipeline.LocalVideoAnalyzer') as analyzer:
            analyzer.return_value.analyze.return_value = {'ok': True, 'text': '[00:00:05] 画面中出现发送按钮', 'modality': 'frames'}
            result = worker.process(job)
        self.assertTrue(result['ok']); adapter.download_video.assert_called_once()
        self.assertEqual(reviewer.review.call_args.args[2], 'video_sampled')
        self.assertEqual(worker.control.snapshot()['jobs'][0]['status'], 'done')

    def test_video_fallback_failure_stays_failed_without_a_fake_report(self):
        worker, job, adapter, reviewer = self.worker_fixture()
        with patch('research_pipeline.LocalVideoAnalyzer') as analyzer:
            analyzer.return_value.analyze.return_value = {'ok': False, 'error': '测试：视频工具不可用'}
            result = worker.process(job)
        self.assertFalse(result['ok']); reviewer.review.assert_not_called(); reviewer.save.assert_not_called()
        self.assertEqual(worker.control.snapshot()['jobs'][0]['status'], 'failed')
        self.assertIn('视频工具不可用', worker.control.snapshot()['jobs'][0]['error'])

    def test_cached_real_subtitles_resume_without_repeated_download_or_vision(self):
        worker, job, adapter, reviewer = self.worker_fixture('[00:00:03] 点击发送')
        with patch('research_pipeline.LocalVideoAnalyzer') as analyzer:
            result = worker.process(job)
        self.assertTrue(result['ok']); analyzer.assert_not_called()
        adapter.get_subtitle.assert_not_called(); adapter.download_video.assert_not_called()
        self.assertEqual(reviewer.review.call_args.args[2], 'subtitle_read')

    def test_refreshed_source_replaces_old_auto_memory_but_preserves_human_correction(self):
        from memory_store import MemoryStore
        store = MemoryStore(self.root, 'research'); source = 'https://www.bilibili.com/video/BV0000000001'
        first = store.save('旧的资料不足摘要', category='视频研究', source=source)
        store.save('另一条旧摘要', category='视频研究', source=source)
        refreshed = store.save('已有画面后的新摘要', category='视频研究', source=source, replace_source=True)
        self.assertEqual(first['id'], refreshed['id']); self.assertEqual(len(store.search()), 1)
        self.assertEqual(store.search()[0]['text'], '已有画面后的新摘要')
        self.assertNotIn('旧摘要', store.context('摘要'))
        store.save('主人手工纠正的摘要', memory_id=refreshed['id'])
        self.assertTrue(store.save('自动更新不应覆盖', category='视频研究', source=source, replace_source=True)['blocked'])
        self.assertEqual(store.search()[0]['text'], '主人手工纠正的摘要')

    def test_deleted_source_is_not_relearned_by_research_refresh(self):
        from memory_store import MemoryStore
        store = MemoryStore(self.root, 'research')
        saved = store.save('待删除的摘要', source='video:fixture')
        store.delete(saved['id'])
        self.assertTrue(store.save('同源新摘要', source='video:fixture', replace_source=True)['blocked'])
        self.assertEqual(store.search(), [])

    def response(self, reason, text=''):
        from dsh_web import DSHWebClient
        client = DSHWebClient('http://127.0.0.1:1/?launch=fixture')
        records = []
        if text:
            records.append({'type': 'event', 'event': {'seq': 1, 'type': 'assistant/message', 'data': {'message': {'content': [{'type': 'text', 'text': text}]}}}})
        records.append({'type': 'event', 'event': {'seq': 2, 'type': 'turn/end', 'data': {'reason': reason}}})
        client._cursor = AsyncMock(return_value=2); client._page = AsyncMock(return_value={'records': records})
        return asyncio.run(client.wait_response('test-session', 0, 2))

    def test_turn_error_explains_quota_and_redacts_secrets(self):
        with patch.dict(os.environ, {'FISH_QQ_DEEPSEEK_API_KEY': 'private-key-fixture'}):
            result = self.response({'kind': 'error', 'error': {'message': '今日模型请求已达上限 private-key-fixture https://user:pass@api.example.test/v1?token=hidden-query Bearer hidden-auth sk-hiddenvalue api_key=hidden-assignment'}})
        self.assertFalse(result['ok']); self.assertIn('今日模型请求已达上限', result['error'])
        for secret in ('private-key-fixture', 'user:pass', 'hidden-query', 'hidden-auth', 'sk-hiddenvalue', 'hidden-assignment'):
            self.assertNotIn(secret, result['error'])

    def test_cancelled_turn_never_returns_partial_answer_and_completed_turn_does(self):
        cancelled = self.response({'kind': 'interrupted'}, '半截回复')
        self.assertTrue(cancelled['cancelled']); self.assertNotIn('text', cancelled)
        completed = self.response({'kind': 'completed'}, '完整回复')
        self.assertEqual(completed, {'ok': True, 'text': '完整回复'})


if __name__=='__main__':unittest.main(verbosity=2)
