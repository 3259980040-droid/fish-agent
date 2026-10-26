from pathlib import Path
import sys, tempfile, json, unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness'),str(ROOT/'tools')]
import dependencies
from test_social_base import Fixture
from workbench import Workbench
from tool_host import ToolHost
from locations import InstanceCatalog
from storage import read_json, write_json

class InstanceToolTests(Fixture):
    def second(self):
        catalog=InstanceCatalog(self.root);catalog.initial()
        data=read_json(catalog.path)
        data['instances'].append({'id':'second','name':'二','running':True,'initial':False})
        write_json(catalog.path,data)
        directory=self.root/'data/instances/second';directory.mkdir()
        write_json(directory/'instance.json',{'id':'second','host_root':str(self.root)})
        return directory

    def test_window_send_switches_and_shared_cli_discovery_execute(self):
        second=self.second();a,b=Workbench(self.root),Workbench(second)
        script=self.root/'echo.py';script.write_text('import json,sys\nv=json.load(sys.stdin)\nprint(json.dumps({"echo":v["arguments"]["text"]}))',encoding='utf-8')
        tool={'name':'echo_text','description':'本机回显','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}
        a.dispatch('plugins.save',{'instance_id':'main','manifest':{'id':'test_cli','name':'本机工具','kind':'cli','command':sys.executable,'args':[str(script)],'roles':['qq'],'enabled':False,'tools':[tool]}})
        a.dispatch('plugins.test',{'instance_id':'main','id':'test_cli','role':'qq'})
        a.dispatch('plugins.enable',{'instance_id':'main','id':'test_cli','enabled':True})
        b.dispatch('plugins.enable',{'instance_id':'second','id':'test_cli','enabled':False})
        ha,hb=ToolHost(self.root),ToolHost(second)
        for host in (ha,hb):host.bind('qq','owner-tools','owner',self.owner)
        self.assertEqual(ha.execute('qq','owner-tools','fishp__test_cli__echo_text',{'text':'真实返回'},'cli1')['echo'],'真实返回')
        with self.assertRaises(PermissionError):hb.execute('qq','owner-tools','fishp__test_cli__echo_text',{'text':'关闭'},'cli2')
        self.assertFalse(ha.registry.entries()[0]['enabled'])
        for left,right in ((True,True),(True,False),(False,True),(False,False)):
            for bench,host,enabled in ((a,ha,left),(b,hb,right)):
                bench.dispatch('tools.settings.save',{'instance_id':host.instance_id,'patch':{'send':{'private':enabled}}})
                if enabled:
                    self.assertTrue(host.execute('qq','owner-tools','fish_say',{'texts':['待发']},'say')['ok'])
                else:
                    with self.assertRaisesRegex(PermissionError,'私聊发送'):
                        host.execute('qq','owner-tools','fish_say',{'texts':['不应排队']},'say')
                self.assertEqual(bench.dispatch('tools.settings.get',{'instance_id':host.instance_id})['settings']['send']['private'],enabled)
        a.dispatch('tools.settings.save',{'instance_id':'main','patch':{'send':{'group':True,'groups':{self.group:False}}}})
        from send_policy import policy_check
        self.assertFalse(policy_check(a.root,self.chat,action='fish_autonomous')[0])
        ha.computer.interaction.close();hb.computer.interaction.close()

    def test_discovered_tool_joins_same_loop_and_failures_return_to_original_call(self):
        from unittest.mock import patch
        from test_unified_execution import FakeGateway, FakeBudget
        from model_gateway import ChatResult
        from fish_agent.execution.runner import run_model_loop
        from turn_trace import TurnTrace
        from locations import project_path
        bench=Workbench(self.root)
        script=self.root/'model-echo.py'
        script.write_text('import json,sys\nv=json.load(sys.stdin)\nif v["arguments"]["text"]=="fail":sys.exit(2)\nprint(json.dumps({"echo":v["arguments"]["text"]}))',encoding='utf-8')
        definition={'name':'echo_text','description':'按需回显','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}
        bench.dispatch('plugins.save',{'manifest':{'id':'echo_cli','kind':'cli','name':'回显','command':sys.executable,'args':[str(script)],'roles':['qq'],'tools':[definition]}})
        bench.dispatch('plugins.test',{'id':'echo_cli','role':'qq'})
        bench.dispatch('plugins.enable',{'id':'echo_cli','enabled':True})
        bench.dispatch('tools.settings.save',{'patch':{'send':{'private':False}}})
        write_json(project_path(self.root,'harness/context-settings.json'),{'tools_visible':['fish_tool_list','fish_say'],'mcp_tools_visible':False})
        host=ToolHost(self.root);host.bind('qq','same-loop','owner',self.owner)
        host.serve(background_workers=False,listen_port=0)
        def stop():
            host.stop.set();host.server.shutdown();host.server.server_close();host.computer.interaction.close()
        self.addCleanup(stop)
        config=read_json(project_path(self.root,'harness/agents.json'))
        config['runtime']['tool_host_port']=host.server.server_address[1]
        write_json(project_path(self.root,'harness/agents.json'),config)
        def call(cid,name,args):
            return ChatResult('',{},'tool_calls',({'id':cid,'type':'function','function':{'name':name,'arguments':json.dumps(args)}},))
        alias='fishp__echo_cli__echo_text'
        class Gateway(FakeGateway):
            def chat(self,messages,**kwargs):
                if 'tools' in kwargs:kwargs['tools']=json.loads(json.dumps(kwargs['tools']))
                return super().chat(messages,**kwargs)
        gateway=Gateway([call('find','fish_tool_list',{'query':'echo_text'}),
                             call('failed',alias,{'text':'fail'}),
                             call('good',alias,{'text':'成功'}),ChatResult('已收到实际结果',{},'stop')])
        trace=TurnTrace(self.root,'qq','same-loop','offline-r03')
        with patch('model_gateway.ModelGateway',return_value=gateway),patch('context_budget.ContextBudget',FakeBudget):
            answer=run_model_loop(self.root,'qq',persona={'body':'Fish','id':'fixture'},history=[],history_source={},memory='',scope='owner',references='',
                current=[{'type':'text','text':'测试发现并执行'}],session='same-loop',cancelled=None,origin='offline-r03',tool_context='owner-private',trace=trace)
        self.assertEqual(answer,'已收到实际结果')
        self.assertEqual(len(gateway.requests),4)
        self.assertNotIn('fish_say',[d['function']['name'] for d in gateway.options[0]['tools']])
        self.assertNotIn(alias,[d['function']['name'] for d in gateway.options[0]['tools']])
        self.assertIn(alias,[d['function']['name'] for d in gateway.options[1]['tools']])
        returned=[m for m in gateway.requests[-1] if m['role']=='tool']
        self.assertEqual([m['tool_call_id'] for m in returned],['find','failed','good'])
        self.assertIn('error',json.loads(returned[1]['content']))
        self.assertEqual(json.loads(returned[2]['content'])['echo'],'成功')
        self.assertEqual([e['value']['call_id'] for e in trace.events if e['kind']=='tool_result'],['find','failed','good'])

    def test_message_send_keeps_its_tool_name_until_each_local_bubble_leaves(self):
        from unittest.mock import patch
        from bot_control import BotControl
        from fish_agent.settings.tools import ToolSettings
        host=ToolHost(self.root);host.bind('qq','message-tool','owner',self.owner)
        self.addCleanup(host.computer.interaction.close)
        sent=[]
        def transport(target,text,key,**options):
            sent.append(text)
            ToolSettings(self.root).save({'tools':{'fish_message_send':False}})
            return {'ok':True,'payload':{'message_id':'receipt-'+str(len(sent))}}
        with patch.object(BotControl,'_channel_transport',staticmethod(transport)):
            result=host.execute('qq','message-tool','fish_message_send',{'target':'private:'+self.owner,'texts':['第一段','第二段']},'two-local-bubbles')
        self.assertEqual(sent,['第一段'])
        self.assertEqual(result['state'],'BLOCKED')
        self.assertIn('fish_message_send',str(result))

if __name__=='__main__':unittest.main()
