"""The real window workbench/host/runtime boundary, with one offline model."""
from pathlib import Path
import json,sys,threading,time,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness'),str(ROOT/'tools')]
import dependencies
from test_social_base import Fixture
from test_unified_execution import FakeBudget
from locations import InstanceCatalog,project_path
from workbench import Workbench
from tool_host import ToolHost
from model_gateway import ChatResult
from fish_agent import get_runtime,reset_runtime
from storage import read_json,write_json

class InstanceLifecycle(Fixture):
    def setUp(self):
        super().setUp()
        guard=patch('model_gateway.ModelGateway',side_effect=AssertionError('离线验收禁止真实模型'))
        guard.start();self.addCleanup(guard.stop)

    def tearDown(self):
        self.doCleanups()
        super().tearDown()

    def test_create_route_parallel_pause_and_reopen_real_windows(self):
        wb=Workbench(self.root)
        first=wb.dispatch('instances.create',{'name':'一号','persona':{'name':'甲','body':'人格甲唯一正文'},'running':False})
        preset=wb.dispatch('personas.create',{'name':'乙','body':'人格乙唯一正文'})
        self.assertTrue(any(p['revision']==preset['id'] for p in wb.dispatch('instances.presets',{})['presets']))
        second=wb.dispatch('instances.create',{'name':'二号','persona':{'instance_id':'main','revision':preset['id']},'running':False})
        ids=[first['id'],second['id']]
        self.assertNotEqual(*ids)
        catalog=InstanceCatalog(self.root)
        self.assertEqual(catalog.resolve(ids[0]).root.parent,catalog.resolve(ids[1]).root.parent)
        self.assertNotEqual(catalog.resolve(ids[0]).workspace,catalog.resolve(ids[1]).workspace)
        blank=wb.dispatch('instances.create',{'name':'未设人格'})
        self.assertFalse(blank['persona_configured']);self.assertFalse(blank['enabled'])
        self.assertEqual(wb.dispatch('status',{'instance_id':blank['id']})['persona']['body'],'')
        with self.assertRaisesRegex(ValueError,'尚未配置人格'):
            wb.dispatch('instances.set_running',{'id':blank['id'],'running':True})
        self.addCleanup(lambda:[reset_runtime(catalog.resolve(row['id'])) for row in catalog.records()])
        host=ToolHost(self.root);host.serve(background_workers=False,listen_port=0)
        def shutdown():
            host.stop.set();host.server.shutdown();host.server.server_close();host.close_instances()
        self.addCleanup(shutdown)
        cfg=read_json(project_path(self.root,'harness/agents.json'))
        cfg['runtime']['tool_host_port']=host.server.server_address[1]
        write_json(project_path(self.root,'harness/agents.json'),cfg)
        entered,released=threading.Event(),threading.Event();requests=[]
        self.addCleanup(released.set)
        class Gateway:
            base='https://fake.invalid';model='fake-model'
            def __init__(self,*args):pass
            def chat(self,messages,**options):
                encoded=json.dumps(messages,ensure_ascii=False);requests.append(encoded)
                if messages[-1]['role']=='tool':return ChatResult('',{},'stop')
                a='人格甲唯一正文' in encoded
                if a:entered.set();released.wait(10)
                text='甲回复' if a else '乙回复'
                return ChatResult('',{},'tool_calls',({'id':'send','type':'function','function':{'name':'fish_say','arguments':json.dumps({'texts':[text]},ensure_ascii=False)}},))
        from context_budget import _tokenizer
        tokenizer=_tokenizer('E:/Program Files/DSH/source/fish/vendor/tokenizers/deepseek-v4/tokenizer.json')
        with patch('model_gateway.ModelGateway',Gateway),patch('context_budget.ContextBudget',FakeBudget),patch('desktop_context._tokens',lambda root,text:len(tokenizer.encode(str(text or '')).ids)),patch('bot_control.BotControl._channel_transport',return_value={'ok':True,'payload':{'message_id':'offline-plan-receipt'}}):
            for iid in ids:
                result=wb.dispatch('instances.set_running',{'id':iid,'running':True})
                self.assertTrue(result['enabled']);self.assertTrue(result['running'])
            cid_a=wb.dispatch('chat.new',{'instance_id':ids[0],'title':'甲对话'})['id']
            cid_b=wb.dispatch('chat.new',{'instance_id':ids[1],'title':'乙对话'})['id']
            a=wb.dispatch('chat.send',{'instance_id':ids[0],'id':cid_a,'text':'甲的私有原文'})
            self.assertTrue(entered.wait(5),str(wb.dispatch('chat.job',{'instance_id':ids[0],'job_id':a['job_id']})))
            b=wb.dispatch('chat.send',{'instance_id':ids[1],'id':cid_b,'text':'乙的私有原文'})
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                job=wb.dispatch('chat.job',{'instance_id':ids[1],'job_id':b['job_id']})
                if job['status'] not in ('queued','running'):break
                time.sleep(.02)
            self.assertEqual(job['status'],'complete',job)
            self.assertFalse(released.is_set(),'第二窗口被第一窗口阻塞')
            released.set()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                job=wb.dispatch('chat.job',{'instance_id':ids[0],'job_id':a['job_id']})
                if job['status'] not in ('queued','running'):break
                time.sleep(.02)
            self.assertEqual(job['status'],'complete',job)
            plan=wb.dispatch('schedules.create',{'instance_id':ids[1],'title':'窗口乙到期任务','prompt':'窗口乙定时提醒','after_seconds':1,'purpose':'reminder'})
            wb.dispatch('instances.set_running',{'id':ids[1],'running':False})
            time.sleep(1.1)
            before=next(p for p in wb.dispatch('schedules.list',{'instance_id':ids[1]}) if p['id']==plan['id'])
            self.assertEqual(before['occurrences'],[],'暂停时不能提交到期回合')
            wb.dispatch('instances.set_running',{'id':ids[1],'running':True})
            deadline=time.monotonic()+6
            while time.monotonic()<deadline:
                settled=next(p for p in wb.dispatch('schedules.list',{'instance_id':ids[1]}) if p['id']==plan['id'])
                if settled.get('occurrenceState')=='sent':break
                time.sleep(.03)
            self.assertEqual(settled.get('occurrenceState'),'sent',settled)
            self.assertTrue(settled['occurrences'][0]['execution'])
            self.assertEqual(settled['sendReceipts'][0]['messageId'],'offline-plan-receipt')
            self.assertFalse(any(p['id']==plan['id'] for p in wb.dispatch('schedules.list',{'instance_id':ids[0]})))
        from tool_host import request
        for iid in ids:
            context=catalog.resolve(iid)
            request(context.root,'/bind',{'role':'qq','session':'same-tools','scope':'owner','sender':self.owner,'desktop':True},role='qq')
            def use(name,args):
                return request(context.root,'/execute',{'session':'same-tools','name':name,'arguments':args,'call_id':name},role='qq',admin=False)
            workspace=use('fish_workspace',{})
            self.assertTrue(Path(workspace['files']).is_relative_to(context.workspace))
            written=use('fish_file_write',{'path':str(Path(workspace['files'])/'result.txt'),'text':iid})
            self.assertEqual(Path(written['path']).read_text(encoding='utf-8'),iid)
            process=use('fish_shell',{'command':"[Console]::Write('instance-output')",'wait_seconds':5})
            self.assertEqual(process['status'],'completed',process)
            self.assertIn('instance-output',process['output'])
            self.assertEqual(Path(process['workdir']),Path(workspace['files']))
            self.assertTrue(Path(process['output_path']).is_relative_to(context.workspace))
            persisted=use('fish_process',{'id':process['id'],'action':'status'})
            self.assertEqual(persisted['output_path'],process['output_path'])
        for encoded in requests:
            self.assertFalse('人格甲唯一正文' in encoded and '人格乙唯一正文' in encoded)
            self.assertFalse('甲的私有原文' in encoded and '乙的私有原文' in encoded)
        renamed=wb.dispatch('instances.rename',{'id':ids[0],'name':'新名字'})
        self.assertEqual(renamed['id'],ids[0]);self.assertEqual(renamed['name'],'新名字')
        wb.dispatch('settings.save',{'instance_id':ids[0],'paused':True})
        rows={r['id']:r for r in wb.dispatch('instances.list',{})['instances']}
        self.assertFalse(rows[ids[0]]['enabled']);self.assertFalse(rows[ids[0]]['running'])
        self.assertTrue(rows[ids[1]]['running'])
        reopened=Workbench(self.root)
        self.assertTrue(reopened.dispatch('settings.get',{'instance_id':ids[0]})['paused'])
        self.assertEqual(reopened.dispatch('personas.current',{'instance_id':ids[1]})['body'],'人格乙唯一正文')
        self.assertTrue(reopened.dispatch('instances.set_running',{'id':ids[0],'running':True})['running'])

if __name__=='__main__':unittest.main()
