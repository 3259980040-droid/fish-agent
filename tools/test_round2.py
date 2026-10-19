"""Second-round integration contracts; no real QQ sends or paid model calls."""
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))
from storage import write_json
from memory_store import MemoryStore
from model_gateway import ModelLedger
from emoji_store import EmojiStore
from plugin_registry import PluginRegistry
from tool_host import ToolHost
from governance import Governance


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        write_json(self.root / 'harness/agents.json', {'agents': [{'id': 'qq'}, {'id': 'research'}]})
        write_json(self.root / 'channels/fish_qq/config.json', {'allowlist': {'owner_id': '12345678'}})
        safety = self.root / 'harness/safety'; safety.mkdir(parents=True)
        write_json(safety / 'rules.json', {'rules': []})

    def tearDown(self):
        self.temp.cleanup()

    def test_memory_separation_and_visibility(self):
        qq = MemoryStore(self.root, 'qq'); research = MemoryStore(self.root, 'research')
        qq.save('主人喜欢蓝色', scope='owner')
        qq.save('群里每周讨论科学', scope='chat:group:100')
        qq.save('公开偏好是简洁', scope='public')
        self.assertEqual(len(qq.search(scope='owner')), 2)
        self.assertEqual(len(qq.search(scope='chat:group:100')), 1)  # Group contexts no longer inherit legacy public memories.
        self.assertEqual(len(qq.search(scope='chat:group:200')), 0)
        self.assertEqual(research.search(), [])
        self.assertIn('主人喜欢蓝色', qq.context('蓝色', 'owner'))
        self.assertNotIn('主人喜欢蓝色', qq.context('蓝色', 'chat:group:100'))

    def test_migrated_missing_runtime_paths_recover_to_the_bundle(self):
        from fish_desktop.bootstrap import initialize
        from storage import read_json
        root = self.root / 'migrated'; initialize(root)
        config = read_json(root / 'harness/agents.json')
        config['runtime'].update(cli=str(root / 'old-machine/dsh.CMD'), node=str(root / 'old-machine/node.exe'))
        write_json(root / 'harness/agents.json', config); initialize(root)
        restored = read_json(root / 'harness/agents.json')['runtime']
        self.assertEqual(restored['cli'], '@bundle/runtime/fish-dsh.mjs')
        self.assertEqual(restored['node'], '@bundle/runtime/node/node.exe')

    def test_learning_switch_and_interrupted_job_need_explicit_retry(self):
        from storage import database
        from unittest.mock import Mock
        store = MemoryStore(self.root, 'qq'); store.configure(False)
        store.enqueue('disabled:1', '请记住我喜欢蓝色')
        self.assertEqual(store.learning_status()['counts'], {})
        store.configure(True); store.enqueue('enabled:1', '请记住我喜欢蓝色')
        with database(store.path) as db:
            db.execute("UPDATE learning_jobs SET status='running',started=1")
        gateway = Mock()
        self.assertFalse(store.learn_one(gateway)); gateway.json.assert_not_called()
        self.assertEqual(store.learning_status()['counts'], {'failed': 1})
        self.assertEqual(store.retry_failed()['count'], 1)
        gateway.json.return_value = {'memories': [{'text': '喜欢蓝色', 'confidence': .95}]}
        self.assertTrue(store.learn_one(gateway))
        self.assertEqual(store.search('蓝色')[0]['source'], 'enabled:1')

    def test_backup_migrates_attachment_paths_without_rewriting_chat_text(self):
        import sqlite3
        from contextlib import closing
        from backup import BackupManager
        source = self.root / 'old'; target = self.root / 'new'
        path = source / 'data/qq/desktop-chat.db'; path.parent.mkdir(parents=True)
        image = source / 'data/qq/photo.jpg'; image.write_bytes(b'synthetic image')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE messages(segments TEXT,text TEXT)')
            db.execute('INSERT INTO messages VALUES(?,?)', (json.dumps([{'path': str(image)}]), str(image)))
        archive = self.root / 'portable.zip'; BackupManager(source).create(archive)
        result = BackupManager(target).restore(archive)
        with closing(sqlite3.connect(target / 'data/qq/desktop-chat.db')) as db: segments, text = db.execute('SELECT * FROM messages').fetchone()
        self.assertEqual(json.loads(segments)[0]['path'], str(target / 'data/qq/photo.jpg'))
        self.assertEqual(text, str(image)); self.assertGreater(result['relocated_fields'], 0)

    def test_research_stop_reaps_a_blocked_child_process(self):
        import subprocess, time
        from unittest.mock import patch
        from research_autonomy import ResearchWorker
        worker = ResearchWorker(self.root)
        worker.control.enqueue({'bvid': 'BV1234567890'}, origin='主人指定')
        job = worker.control.claim(); self.assertIsNotNone(job)
        created = []; actual_popen = subprocess.Popen; ready = threading.Event()
        def launch(*args, **kwargs):
            if '--backend' not in args[0]: return actual_popen(*args, **kwargs)
            proc = actual_popen([sys.executable, '-c', 'import time;time.sleep(60)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            created.append(proc); ready.set(); return proc
        with patch('research_autonomy.subprocess.Popen', side_effect=launch):
            thread = threading.Thread(target=lambda: worker.run_job(job)); thread.start()
            self.assertTrue(ready.wait(5)); worker.cancel(job['id']); thread.join(15)
        self.assertFalse(thread.is_alive()); self.assertIsNotNone(created[0].poll())
        self.assertEqual(worker.control.snapshot()['jobs'][0]['status'], 'cancelled')

    def test_memory_correction_and_tombstone(self):
        store = MemoryStore(self.root, 'qq')
        mid = store.save('喜欢绿色', source='qq:1')['id']
        store.save('喜欢蓝色', memory_id=mid)
        self.assertFalse(store.save('喜欢绿色', source='qq:1')['ok'])
        store.delete(mid)
        self.assertFalse(store.save('喜欢蓝色', source='qq:2')['ok'])
        self.assertEqual(store.search(), [])

    def test_atomic_request_limit(self):
        write_json(self.root / 'harness/control.json', {'max_model_requests_daily': 7})
        ledger = ModelLedger(self.root, 'qq')
        def attempt(_):
            try: return ledger.reserve('concurrent')
            except PermissionError: return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            ids = list(pool.map(attempt, range(30)))
        self.assertEqual(sum(bool(x) for x in ids), 7)
        self.assertEqual(ledger.snapshot()['requests'], 7)
        ModelLedger(self.root, 'research').reserve('separate')

    def test_owner_identity_and_capabilities(self):
        host = ToolHost(self.root)
        host.bind('qq', 'owner-session', 'owner', '12345678')
        host.bind('qq', 'group-session', 'chat:group:10', '12345678')
        self.assertFalse(host.identity('qq', 'group-session')['owner'])
        with self.assertRaises(PermissionError): host.execute('qq', 'group-session', 'fish_memory_save', {'text': '假主人要求'}, '1')
        host.execute('qq', 'owner-session', 'fish_memory_save', {'text': '喜欢蓝色'}, '2')
        self.assertEqual(host.execute('qq', 'group-session', 'fish_memory_search', {'query': '蓝色'}, '3'), [])
        cfg = Governance(self.root).config(); cfg['owner_permissions'] = ['status']; Governance(self.root).save(cfg)
        with self.assertRaises(PermissionError): host.execute('qq', 'owner-session', 'fish_memory_save', {'text': '不能保存'}, '4')

    def test_emoji_dedupe_delete_cooldown(self):
        from PIL import Image
        source = self.root / 'sample.png'; Image.new('RGB', (32, 32), 'blue').save(source)
        store = EmojiStore(self.root)
        result = store.add(source, '开心蓝色', '开心')
        self.assertTrue(store.add(source)['duplicate'])
        asset = store.reserve_send(result['id'], 'private:12345678')
        self.assertTrue(asset['file'].startswith('base64://'))
        with self.assertRaises(PermissionError): store.reserve_send(result['id'], 'private:12345678')
        store.delete(result['id'])
        self.assertFalse(store.add(source, automatic=True)['ok'])

    def test_cli_plugin_real_bridge_and_env_scrub(self):
        script = self.root / 'cli.py'
        script.write_text("import sys,json,os\nx=json.load(sys.stdin)\nprint(json.dumps({'echo':x['arguments']['text'],'secret':os.environ.get('FISH_CONTROL_TOKEN','absent')}))\n", encoding='utf-8')
        registry = PluginRegistry(self.root)
        registry.save({'id': 'echo_test', 'kind': 'cli', 'command': sys.executable, 'args': [str(script)], 'roles': ['research'], 'owner_only': True,
                       'tools': [{'name': 'echo', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}}]})
        registry.discover('echo_test'); registry.toggle('echo_test', True)
        os.environ['FISH_CONTROL_TOKEN'] = 'do-not-forward'
        try:
            result = registry.execute('fishp__echo_test__echo', {'text': '中文输入'}, 'research', owner=True)
            self.assertEqual(result, {'echo': '中文输入', 'secret': 'absent'})
        finally: os.environ.pop('FISH_CONTROL_TOKEN', None)
        with self.assertRaises(PermissionError): registry.execute('fishp__echo_test__echo', {'text': 'x'}, 'qq', owner=True)
        with self.assertRaises(PermissionError): registry.execute('fishp__echo_test__echo', {'text': 'x'}, 'research', owner=False)
        registry.toggle('echo_test', False)
        with self.assertRaises(PermissionError): registry.execute('fishp__echo_test__echo', {'text': 'x'}, 'research', owner=True)

    def test_mcp_sdk_stdio_discovery_and_execution(self):
        script = self.root / 'mcp_fixture.py'
        script.write_text('''import sys,json
for line in sys.stdin:
 x=json.loads(line); m=x.get('method'); i=x.get('id')
 if i is None: continue
 if m=='initialize': r={'protocolVersion':x['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
 elif m=='tools/list': r={'tools':[{'name':'echo','description':'fixture','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}]}
 elif m=='tools/call': r={'content':[{'type':'text','text':x['params']['arguments']['text']}],'isError':False}
 else: r={}
 print(json.dumps({'jsonrpc':'2.0','id':i,'result':r}),flush=True)
''', encoding='utf-8')
        registry = PluginRegistry(self.root)
        registry.save({'id': 'mcp_test', 'kind': 'mcp_stdio', 'command': sys.executable, 'args': [str(script)], 'roles': ['research']})
        self.assertEqual(registry.discover('mcp_test')['tools'][0]['name'], 'echo')
        registry.toggle('mcp_test', True)
        result = registry.execute('fishp__mcp_test__echo', {'text': '真实 SDK 测试'}, 'research', owner=True)
        self.assertEqual(result['content'][0]['text'], '真实 SDK 测试')

    def test_research_owner_filter_queue_and_cancel(self):
        from research_autonomy import ResearchControl, ResearchWorker
        control = ResearchControl(self.root)
        control.configure({'keywords': ['AI'], 'exclude': ['广告'], 'max_video_minutes': 10, 'sources': ['search'], 'daily_limit': 1})
        class Adapter:
            def search(self, *_):
                return [{'bvid': 'BV1234567890', 'title': 'AI 实验', 'duration': 60},
                        {'bvid': 'BV1234567891', 'title': 'AI 广告', 'duration': 60},
                        {'bvid': 'BV1234567892', 'title': '无关游戏', 'duration': 60}]
        worker = ResearchWorker(self.root, adapter=Adapter())
        self.assertEqual(worker.discover()['added'], 1)
        self.assertEqual(worker.discover()['added'], 0)
        job = control.claim(); self.assertIsNotNone(job)
        self.assertIsNone(control.claim())
        control.cancel(job['id'])
        with self.assertRaises(InterruptedError): worker._check(job['id'])
        self.assertEqual(control.snapshot()['config']['keywords'], ['AI'])

    def test_research_requires_semantics_and_removes_invented_citations(self):
        from research_autonomy import ResearchReviewer
        class Gateway:
            def json(self, system, user, **kwargs):
                self.input = user
                return {'summary': '开头讲实验甲，末尾明确报告实验乙。', 'key_points': ['实验甲', '实验乙'], 'opinion': '两个实验应该分开复现。',
                        'interest_reason': '对应主人指定的科学方向。', 'uncertainties': [], 'interest_score': .8,
                        'evidence': [{'claim': '末尾实验', 'quote': '末尾的实验乙成功了', 'timestamp': '99:99'}, {'claim': '虚构引用', 'quote': '根本不存在的句子', 'timestamp': None}]}
        gateway = Gateway(); reviewer = ResearchReviewer(self.root, gateway)
        text = '开头的实验甲失败了。' + '中间资料。' * 1800 + '\n末尾的实验乙成功了。'
        report = reviewer.review({'bvid': 'BV1234567890', 'title': '科学实验', 'url': 'https://www.bilibili.com/video/BV1234567890'}, text, 'subtitle_read')
        self.assertIn('末尾的实验乙成功了', gateway.input)
        self.assertEqual(len(report['evidence']), 1)
        self.assertIsNone(report['evidence'][0]['timestamp'])
        self.assertIn('移除', report['uncertainties'][-1])

    def test_backup_sqlite_restore_and_zip_traversal(self):
        from backup import BackupManager
        from storage import database
        import zipfile
        store = MemoryStore(self.root, 'qq'); mid = store.save('备份时的记忆')['id']
        archive = self.root / 'backups/test.zip'
        manager = BackupManager(self.root); manager.create(archive)
        store.save('备份后的内容', memory_id=mid)
        manager.restore(archive)
        self.assertEqual(MemoryStore(self.root, 'qq').search()[0]['text'], '备份时的记忆')
        bad = self.root / 'unsafe.zip'
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr('fish-backup.json', json.dumps({'format': 'fish-backup', 'version': 1, 'files': {'data/../../escape': 'x'}}))
            z.writestr('data/../../escape', 'x')
        with self.assertRaises(ValueError): manager.inspect(bad)
        self.assertFalse((self.root.parent / 'escape').exists())

    def test_portable_key_backup_validates_password_before_changes(self):
        if os.name != 'nt': self.skipTest('DPAPI current-user contract')
        from backup import BackupManager
        from secrets_store import SecretStore
        SecretStore(self.root).put('agent-qq', 'synthetic-key-before')
        manager = BackupManager(self.root); archive = self.root / 'backups/keys.zip'
        manager.create(archive, portable_keys=True, password='strong-synthetic-passphrase')
        SecretStore(self.root).put('agent-qq', 'synthetic-key-after')
        with self.assertRaises(ValueError): manager.restore(archive, password='wrong-password-here')
        self.assertEqual(SecretStore(self.root).get('agent-qq'), 'synthetic-key-after')
        manager.restore(archive, password='strong-synthetic-passphrase')
        self.assertEqual(SecretStore(self.root).get('agent-qq'), 'synthetic-key-before')


if __name__ == '__main__': unittest.main(verbosity=2)
