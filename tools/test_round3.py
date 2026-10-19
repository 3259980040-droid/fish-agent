"""Round 3 negative and recovery scenarios; private roots, no real sends."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/'harness')]
from fish_desktop.bootstrap import initialize
from storage import read_json,write_json,database
from governance import Governance
from memory_store import MemoryStore
from emoji_store import EmojiStore
from model_gateway import ModelGateway
from tool_host import ToolHost
from desktop_chat import DesktopChat
from research_autonomy import ResearchControl
from PIL import Image


class Acceptance(unittest.TestCase):
    def setUp(self):
        parent=ROOT/'artifacts/round3';parent.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='acceptance-',dir=parent)
        self.root=initialize(Path(self.temp.name))
        channel=read_json(self.root/'channels/fish_qq/config.json')
        channel['allowlist']['owner_id']='12345678';write_json(self.root/'channels/fish_qq/config.json',channel)

    def tearDown(self): self.temp.cleanup()

    def test_t11_reassign_owner_revokes_existing_private_tool_session(self):
        host=ToolHost(self.root);host.bind('qq','old-owner','owner','12345678')
        MemoryStore(self.root,'qq').save('OWNER-PRIVATE-4738')
        self.assertTrue(host.execute('qq','old-owner','fish_memory_search',{'query':'4738'},'before'))
        cfg=read_json(self.root/'channels/fish_qq/config.json');cfg['allowlist']['owner_id']='87654321';write_json(self.root/'channels/fish_qq/config.json',cfg)
        with self.assertRaises(PermissionError):host.execute('qq','old-owner','fish_memory_search',{'query':'4738'},'after')
        host.bind('qq','new-owner','owner','87654321')
        self.assertTrue(host.execute('qq','new-owner','fish_memory_search',{'query':'4738'},'new'))

    def test_t06_disable_collection_does_not_start_pending_model_job(self):
        store=EmojiStore(self.root);image=self.root/'sticker.png';Image.new('RGB',(32,32),'blue').save(image)
        store.enqueue(image,'synthetic-photo');store.configure({'auto_collect':False})
        gateway=Mock();gateway.json.return_value={'is_meme':True,'description':'蓝色表情','tags':['开心']}
        self.assertFalse(store.learn_one(gateway));gateway.json.assert_not_called();self.assertEqual(store.list(),[])

    def test_t06_disable_collection_discards_result_already_in_flight(self):
        store=EmojiStore(self.root);image=self.root/'sticker.png';Image.new('RGB',(32,32),'blue').save(image)
        store.enqueue(image,'synthetic-photo');gateway=Mock()
        def result(*a,**k):
            store.configure({'auto_collect':False});return {'is_meme':True,'description':'蓝色表情','tags':['开心']}
        gateway.json.side_effect=result;store.learn_one(gateway)
        self.assertEqual(store.list(),[])

    def test_t11_cancel_after_usage_keeps_accounting_known(self):
        gateway=ModelGateway(self.root,'research');gateway.key='synthetic-key';cancel=threading.Event()
        response=Mock(ok=True)
        def data():
            cancel.set();return {'usage':{'prompt_tokens':45,'completion_tokens':8},'choices':[{'message':{'content':'not delivered'}}]}
        response.json.side_effect=data
        with patch('model_gateway.requests.post',return_value=response):
            with self.assertRaises(InterruptedError):gateway.call([{'role':'user','content':'fixture'}],cancelled=cancel)
        usage=gateway.ledger.snapshot();self.assertEqual(usage['unreported'],0);self.assertEqual(usage['input_tokens'],45)

    def test_t11_pause_and_exhausted_budget_start_no_http_request(self):
        gateway=ModelGateway(self.root,'research');gateway.key='synthetic-key'
        policy=Governance(self.root,'research');policy.save({'paused':True})
        with patch('model_gateway.requests.post') as post:
            with self.assertRaises(PermissionError):gateway.call([])
            post.assert_not_called()
        policy.save({'paused':False,'max_model_requests_daily':1})
        gateway.ledger.reserve('test-reservation')
        with patch('model_gateway.requests.post') as post:
            with self.assertRaises(PermissionError):gateway.call([])
            post.assert_not_called()

    def test_t05_restart_correction_and_deletion_change_runtime_context(self):
        store=MemoryStore(self.root,'qq');mid=store.save('QA_MEMORY 喜欢绿色',source='qq:fixture-1')['id']
        self.assertIn('绿色',MemoryStore(self.root,'qq').context('QA_MEMORY'))
        store.save('QA_MEMORY 喜欢蓝色',memory_id=mid)
        context=MemoryStore(self.root,'qq').context('QA_MEMORY')
        self.assertIn('蓝色',context);self.assertNotIn('绿色',context)
        self.assertTrue(store.save('QA_MEMORY 喜欢绿色',source='qq:fixture-1').get('blocked'))
        store.delete(mid);self.assertNotIn('蓝色',MemoryStore(self.root,'qq').context('QA_MEMORY'))
        self.assertNotIn('蓝色',MemoryStore(self.root,'research').context('QA_MEMORY'))

    def test_t05_disabling_learning_discards_result_already_in_flight(self):
        store=MemoryStore(self.root,'qq');store.configure(True);store.enqueue('fixture-inflight','请记住我喜欢绿色')
        gateway=Mock()
        def result(*args,**kwargs):
            store.configure(False);return {'memories':[{'text':'喜欢绿色','category':'偏好','confidence':.95}]}
        gateway.json.side_effect=result
        store.learn_one(gateway)
        self.assertEqual(store.search(),[])

    def test_t03_search_reaches_message_older_than_current_window(self):
        chat=DesktopChat(self.root,'qq',Mock());cid=chat.new('a very long history')
        first=chat._record(cid,'inbound','OLD-HIT-5194',[],'done')
        for n in range(130):chat._record(cid,'outbound',f'later message {n}',[],'done')
        result=chat.snapshot(cid,'OLD-HIT-5194')
        self.assertTrue(result.get('search_hits'))
        self.assertEqual(result['search_hits'][0]['id'],first)
        focused=chat.snapshot(cid,around=first)
        self.assertTrue(any(r['id']==first for r in focused['messages']))

    def test_t07_changed_interest_and_exclusion_survive_feedback(self):
        c=ResearchControl(self.root);c.configure({'keywords':['缓存'],'exclude':['广告'],'auto':False})
        self.assertTrue(c.matches({'title':'缓存的取舍'})[0])
        self.assertFalse(c.matches({'title':'缓存广告'})[0])
        c.configure({'keywords':['数据库']})
        self.assertFalse(c.matches({'title':'缓存的取舍'})[0]);self.assertTrue(c.matches({'title':'数据库性能'})[0])
        self.assertEqual(ResearchControl(self.root).config()['exclude'],['广告'])

    def test_t07_interest_change_during_discovery_discards_old_candidates(self):
        from research_autonomy import ResearchWorker
        control=ResearchControl(self.root);control.configure({'keywords':['旧主题'],'sources':['search']})
        adapter=Mock()
        def search(*args):
            control.configure({'keywords':['新主题']});return [{'bvid':'BV0000000005','title':'旧主题'}]
        adapter.search.side_effect=search;worker=ResearchWorker(self.root,adapter=adapter)
        with patch('research_autonomy.ModelGateway') as model:
            with self.assertRaisesRegex(InterruptedError,'兴趣已更改'):worker.discover()
            model.assert_not_called()
        self.assertEqual(control.snapshot()['jobs'],[])

    def test_t08_pause_stops_discovery_before_any_source_request(self):
        from research_autonomy import ResearchWorker
        control=ResearchControl(self.root);control.configure({'keywords':['AI'],'sources':['search'],'paused':True})
        adapter=Mock();worker=ResearchWorker(self.root,adapter=adapter)
        with self.assertRaises(InterruptedError):worker.discover()
        adapter.search.assert_not_called()
        control.configure({'paused':False});Governance(self.root,'research').save({'paused':True})
        with self.assertRaises(InterruptedError):worker.discover()
        adapter.search.assert_not_called()

    def test_t08_failed_job_needs_manual_retry_and_counts_against_quota(self):
        c=ResearchControl(self.root);c.configure({'daily_limit':2})
        item=c.enqueue({'bvid':'BV0000000001'});job=c.claim();c.update(job['id'],status='failed',error='network fixture')
        self.assertIsNone(c.claim());self.assertTrue(c.enqueue({'bvid':'BV0000000001'}).get('duplicate'))
        c.enqueue({'bvid':'BV0000000001'},force=True);job=c.claim();self.assertIsNotNone(job)
        c.update(job['id'],status='failed',error='second attempt');c.enqueue({'bvid':'BV0000000001'},force=True)
        self.assertIsNone(c.claim());self.assertEqual(c.snapshot()['jobs'][0]['status'],'queued')

    def test_t10_cancel_mid_download_removes_partial_file_and_stops_before_upload(self):
        from multimodal import MediaService
        service = MediaService(self.root, 'qq'); cancel = threading.Event()
        session = Mock(); response = Mock(is_redirect=False, headers={})
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        def chunks(*args):
            yield b'partial'
            cancel.set(); yield b'must-not-write'
        response.iter_content.side_effect = chunks; session.get.return_value = response
        directory = self.root / 'download'; directory.mkdir()
        with patch('network_config.session', return_value=session), patch.object(service, '_validate_media_url'):
            with self.assertRaises(InterruptedError): service._download('https://gchat.qpic.cn/test', directory, cancel)
        self.assertFalse((directory / 'attachment.bin').exists()); session.close.assert_called_once()

    def test_t04_nested_auth_error_is_visible_redacted_and_failed_plugin_is_disabled(self):
        from plugin_registry import PluginRegistry
        import requests
        registry = PluginRegistry(self.root)
        registry.save({'id':'auth_test','name':'Auth fixture','kind':'mcp_http','url':'http://127.0.0.1:1/mcp?token=SECRET-TEST','roles':['research']})
        with patch.object(registry, '_run', return_value=[{'name':'read_echo','inputSchema':{'type':'object'}}]): registry.discover('auth_test')
        registry.toggle('auth_test', True)
        response = requests.Response(); response.status_code = 401
        failure = ExceptionGroup('SECRET-TEST', [requests.HTTPError('SECRET-TEST', response=response)])
        with patch.object(registry, '_run', side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 401'): registry.discover('auth_test')
        item = registry.entries()[0]
        self.assertFalse(item['enabled']); self.assertFalse(item['test_ok']); self.assertNotIn('SECRET-TEST', str(registry.snapshot()['events']))
        with self.assertRaises(ValueError): registry.toggle('auth_test', True)
        with patch.object(registry, '_run', return_value=[{'name':'read_echo','inputSchema':{'type':'object'}}]): registry.discover('auth_test')
        registry.toggle('auth_test', True); self.assertTrue(registry.entries()[0]['test_ok'])

    def test_t12_data_relocation_preserves_both_roles_keys_and_delivery_state(self):
        from updater import migrate_data, migration_target
        from fish_desktop.bootstrap import selected_root, locate_root
        from secrets_store import SecretStore
        from qq_store import QQStore
        MemoryStore(self.root,'qq').save('QQ-SIDE-RELOCATE')
        MemoryStore(self.root,'research').save('RESEARCH-SIDE-RELOCATE')
        SecretStore(self.root).put('agent-qq','synthetic-protected-key')
        state = self.root / 'channel-state/delivery.json'; state.parent.mkdir(); state.write_text('{"delivered":"delivery-19"}')
        target = self.root.parent / (self.root.name + '-migrated')
        with tempfile.TemporaryDirectory(prefix='appdata-fixture-') as local:
            env = dict(os.environ); env.pop('FISH_HARNESS_HOME', None); env['LOCALAPPDATA'] = local
            with patch.dict(os.environ, env, clear=True):
                try:
                    self.assertTrue(migrate_data(self.root,target)['old_data_retained'])
                    self.assertEqual(selected_root(),target); self.assertEqual(locate_root(),target)
                    self.assertTrue(MemoryStore(target,'qq').search('QQ-SIDE-RELOCATE'))
                    self.assertFalse(any('QQ-SIDE-RELOCATE' in r['text'] for r in MemoryStore(target,'research').search('QQ-SIDE-RELOCATE')))
                    self.assertTrue(MemoryStore(target,'research').search('RESEARCH-SIDE-RELOCATE'))
                    self.assertEqual(SecretStore(target).get('agent-qq'),'synthetic-protected-key')
                    self.assertEqual(read_json(target/'channel-state/delivery.json'),read_json(state))
                    self.assertTrue(MemoryStore(self.root,'qq').search('QQ-SIDE-RELOCATE'))
                    with self.assertRaises(ValueError): migration_target(self.root, self.root/'nested')
                    with self.assertRaises(ValueError): migration_target(self.root, target)
                finally:
                    import shutil
                    if target.exists(): shutil.rmtree(target)

    def test_t02_network_config_reaches_python_httpx_and_bundled_node_proxy(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from network_config import NetworkConfig, request_options, httpx_options, environment
        import requests, subprocess
        import dependencies, httpx
        seen = []
        class Proxy(BaseHTTPRequestHandler):
            def do_GET(handler):
                seen.append(handler.path); data=b'network-fixture-3819'
                handler.send_response(200); handler.send_header('Content-Length',str(len(data))); handler.end_headers(); handler.wfile.write(data)
            def do_CONNECT(handler):
                seen.append('CONNECT:'+handler.path)
                handler.send_response(200); handler.end_headers()
                while handler.rfile.readline().strip(): pass
                data=b'network-fixture-3819'
                handler.connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(data)).encode()+b'\r\nConnection: close\r\n\r\n'+data)
            def log_message(*args): pass
        server = ThreadingHTTPServer(('127.0.0.1',0), Proxy); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        proxy = 'http://127.0.0.1:'+str(server.server_port); url='http://fish-fixture.invalid/proxy-check'
        try:
            NetworkConfig(self.root).save({'mode':'proxy','proxy':proxy})
            with requests.get(url, timeout=5, **request_options(self.root,url)) as result: self.assertIn('3819',result.text)
            with httpx.Client(**httpx_options(self.root,url)) as client: self.assertIn('3819',client.get(url).text)
            node = ROOT/'vendor/runtime/node/node.exe'
            value = subprocess.run([str(node),'-e',"fetch(process.argv[1]).then(r=>r.text()).then(console.log)",url],env=environment(self.root,os.environ),capture_output=True,text=True,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(value.returncode,0,value.stderr); self.assertIn('3819',value.stdout)
            self.assertEqual(len(seen),3)
            self.assertIsNone(request_options(self.root,proxy)['proxies']['http'])
            self.assertFalse(httpx_options(self.root,proxy)['trust_env'])
            NetworkConfig(self.root).save({'mode':'direct'})
            env=environment(self.root,{'HTTPS_PROXY':'http://invalid:1','NODE_USE_ENV_PROXY':'1'})
            self.assertNotIn('HTTPS_PROXY',env); self.assertNotIn('NODE_USE_ENV_PROXY',env)
            with self.assertRaises(ValueError): NetworkConfig(self.root).save({'mode':'proxy','proxy':'http://user:SECRET@localhost:7890'})
            self.assertEqual(__import__('network_config').settings(self.root)['mode'],'direct')
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_t12_runtime_temp_is_writable_and_separate_for_both_roles(self):
        import subprocess
        from core import RuntimeManager,HarnessConfig
        manager=RuntimeManager(HarnessConfig(self.root))
        qq=manager.runtime_env('qq');research=manager.runtime_env('research')
        self.assertNotEqual(qq['TEMP'],research['TEMP'])
        self.assertTrue(Path(qq['TEMP']).is_relative_to(self.root/'data/qq'))
        script="const f=require('fs'),p=require('path'),o=require('os'); const t=f.mkdtempSync(p.join(o.tmpdir(),'spill-probe-')); f.writeFileSync(p.join(t,'data'),'ok'); f.rmSync(t,{recursive:true});"
        for env in (qq,research):
            result=subprocess.run([str(ROOT/'vendor/runtime/node/node.exe'),'-e',script],env=env,capture_output=True,text=True,encoding='utf-8',timeout=8,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_t11_native_paths_reject_junction_and_array_escape(self):
        import subprocess
        outside = self.root / 'data/research'; outside.mkdir(parents=True,exist_ok=True)
        work = self.root/'qq-work'; work.mkdir(exist_ok=True)
        env=os.environ.copy(); env.update(FISH_QA_LINK=str(work/'escape'),FISH_QA_TARGET=str(outside))
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command','New-Item -ItemType Junction -Path $env:FISH_QA_LINK -Target $env:FISH_QA_TARGET | Out-Null'],env=env,check=True,capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        script = self.root/'guard-check.mjs'
        script.write_text("import {lstatSync} from 'node:fs';\nimport {toolDenial} from "+json.dumps((ROOT/'harness/runtime/fish-control/lib/index.js').as_uri())+";\n"+"const root=process.argv[2]; if(!lstatSync(root+'/qq-work/escape').isSymbolicLink()) throw Error('fixture link missing');\n"+"const check=args=>toolDenial({name:'read_file',arguments:args},{},root,'qq');\n"+"if(!check({path:'escape/private.txt'}) || !check({paths:['../data/research/private.txt']}) || check({path:'safe.txt'})) process.exit(1);\n",encoding='utf-8')
        result=subprocess.run([str(ROOT/'vendor/runtime/node/node.exe'),str(script),str(self.root)],capture_output=True,text=True,encoding='utf-8',timeout=8,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr)

    def test_t08_blocked_research_child_keeps_lease_and_is_cancelled(self):
        import subprocess,time
        from research_autonomy import ResearchWorker
        control=ResearchControl(self.root);control.enqueue({'bvid':'BV0000000004'});job=control.claim()
        with database(control.path) as db:db.execute('UPDATE jobs SET lease=1 WHERE id=?',(job['id'],))
        worker=ResearchWorker(self.root);popen=subprocess.Popen;created=[];result={}
        def child(*args,**kwargs):
            if '--backend' not in args[0] or 'research-job' not in args[0]: return popen(*args,**kwargs)
            process=popen([sys.executable,'-c','import time;time.sleep(60)'],**kwargs);created.append(process);return process
        def work():result.update(worker.run_job(job))
        with patch('research_autonomy.subprocess.Popen',side_effect=child):
            thread=threading.Thread(target=work);thread.start()
            try:
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    with database(control.path) as db:lease=db.execute('SELECT lease FROM jobs WHERE id=?',(job['id'],)).fetchone()[0]
                    if lease>time.time()-30:break
                    time.sleep(.02)
                self.assertGreater(lease,time.time()-30)
                self.assertIsNone(control.claim())
                self.assertEqual(control.snapshot()['jobs'][0]['status'],'running')
                worker.cancel(job['id']);thread.join(8)
                self.assertFalse(thread.is_alive());self.assertEqual(result['status'],'cancelled')
                self.assertTrue(created and created[0].poll() is not None)
            finally:worker.stop.set();thread.join(8)

    def test_t11_qq_stop_cancels_group_generation_tasks(self):
        from qq_agent_loop import QQAgentLoop
        from unittest.mock import AsyncMock
        async def check():
            loop=QQAgentLoop(self.root);loop.channel=AsyncMock();loop.runtime=Mock()
            task=asyncio.create_task(asyncio.sleep(60));loop.group_tasks.add(task)
            await loop.stop();self.assertTrue(task.cancelled());loop.channel.stop.assert_awaited_once()
        asyncio.run(check())

    @unittest.skipUnless(os.name=='nt','Windows job object contract')
    def test_t12_crashed_parent_does_not_leave_its_child_running(self):
        import ctypes,subprocess,time
        from ctypes import wintypes
        script=self.root/'job-parent.py';pidfile=self.root/'child.pid'
        script.write_text('import sys,subprocess,time\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(ROOT/'harness'))+')\nfrom process_tools import OwnedJob\njob=OwnedJob()\np=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],creationflags=subprocess.CREATE_NO_WINDOW)\njob.attach(p)\nPath(sys.argv[1]).write_text(str(p.pid))\ntime.sleep(60)\n',encoding='utf-8')
        parent=subprocess.Popen([sys.executable,str(script),str(pidfile)],creationflags=subprocess.CREATE_NO_WINDOW)
        kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
        handle=None
        try:
            deadline=time.monotonic()+6
            while not pidfile.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(pidfile.exists());handle=kernel.OpenProcess(0x100001,False,int(pidfile.read_text()));self.assertTrue(handle)
            parent.kill();parent.wait(5)
            self.assertEqual(kernel.WaitForSingleObject(handle,5000),0)
        finally:
            if parent.poll() is None:parent.kill();parent.wait(5)
            if handle:
                if kernel.WaitForSingleObject(handle,0)==258:kernel.TerminateProcess(handle,1)
                kernel.CloseHandle(handle)


if __name__=='__main__':unittest.main(verbosity=2)
