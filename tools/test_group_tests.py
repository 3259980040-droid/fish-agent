"""Group A/B trials: actual sender, pinned scope, privacy and in-flight checks."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'harness'), str(ROOT/'tools')]
import dependencies
from test_social_base import Fixture
from group_tests import GroupTests, command
from social_context import SocialContext
from group_policy import GroupPolicy
from memory_store import MemoryStore
from storage import database, read_json, write_json
from qq_agent_loop import QQAgentLoop
from group_workflow import GroupWorkflow


class GroupTrialTests(Fixture):
    def candidates(self):
        c = SocialContext(self.root)
        return c.save_persona('qq','A','same body'), c.save_persona('qq','B','same body',owner_reply_mode='thinking_excerpt')

    def payload(self, text='hello', *, sender=None, mention=True, mid='1', group=None):
        return {'chat_type':'group','chat_id':'group:'+(group or self.group),'message_id':mid,
                'sender':{'id':sender or self.owner,'display_name':'主人名字也可以伪造'},
                'mentions_bot':mention,'segments':[{'type':'at','user_id':'20001'},{'type':'text','text':text}]}

    def loop(self):
        loop=QQAgentLoop(self.root)
        loop.channel=Mock(bot={'id':'20001'},action=AsyncMock(return_value={'ok':True,'payload':{'message_id':'command-out'}}))
        return loop

    def test_normalized_mention_and_owner_id_required_no_private_route_change(self):
        a,b=self.candidates(); tests=GroupTests(self.root); loop=self.loop()
        own=tests.contexts.create_space('qq',revision=a['id'],origin='owner-qq');tests.contexts.select_owner_space(own['id'],self.owner)
        for sender,mention in [('999999999',True),(self.owner,False)]:
            asyncio.run(command(loop,self.payload('/新建测试 '+a['id'][:8],sender=sender,mention=mention),sender))
            self.assertIsNone(tests.current(self.group))
        asyncio.run(command(loop,self.payload('/新建测试 '+a['id'][:8]),self.owner))
        self.assertEqual(tests.current(self.group)['revision'],a['id'])
        self.assertEqual(tests.contexts.owner_space(self.owner)['id'],own['id'])
        self.assertFalse(tests.contexts.persona(a['id'])['approved'])
        self.assertFalse(tests.contexts.deployment(self.group)['enabled'])

    def test_group_allowlist_scope_and_runtime_change_close_trial(self):
        a,_=self.candidates(); tests=GroupTests(self.root)
        with self.assertRaises(PermissionError):tests.create('666666666',a['id'],self.owner)
        first=tests.create(self.group,a['id'],self.owner)
        with self.assertRaises(ValueError):tests.window('666666666',first['id'])
        with self.assertRaises(ValueError):tests.select(self.group,tests.contexts.create_space('qq',origin='owner-qq')['id'],self.owner)
        cfg=read_json(self.root/'harness/agents.json');cfg['agents'][0]['model']='changed';write_json(self.root/'harness/agents.json',cfg)
        self.assertIsNone(tests.current(self.group))

    def test_histories_snapshots_and_summary_survive_switch_without_crossing(self):
        a,b=self.candidates(); tests=GroupTests(self.root)
        sa=tests.create(self.group,a['id'],self.owner);pa=tests.policy(sa)
        pa.observe(self.payload('CANARY_A'));pa.save_summary(self.chat,1,'summary A')
        sb=tests.create(self.group,b['id'],self.owner);pb=tests.policy(sb)
        self.assertEqual(pb.snapshot(self.chat)['messages'],[])
        pb.observe(self.payload('CANARY_B',mid='2'))
        tests.select(self.group,sa['id'],self.owner)
        self.assertEqual(pa.snapshot(self.chat)['summary'],'summary A')
        self.assertNotIn('CANARY_B',json.dumps(pa.snapshot(self.chat)))
        self.assertEqual(pa.candidate(self.chat)['action'],'silent')
        self.assertFalse(pa.config(self.chat)['learn'])
        m=MemoryStore(self.root,'qq')
        with self.assertRaises(PermissionError):m.save('bad',scope=sa['test_scope'])
        self.assertEqual(m.enqueue('x','bad',sa['test_scope']).get('skipped'),'test-space-readonly')

    def run_trial(self, *, mode='final', mutation='', blocked=False):
        a,b=self.candidates();tests=GroupTests(self.root)
        trial=tests.create(self.group,(b if mode=='thinking_excerpt' else a)['id'],self.owner)
        workflow=GroupWorkflow(self.root,trial=trial);workflow.policy.configure(self.chat,{'debounce_s':0})
        loop=self.loop();loop._deliver_reply=AsyncMock(return_value={'ok':True,'payload':{'message_id':'sent-'+trial['id']}})
        client=Mock(auth=AsyncMock(),create_session=AsyncMock(return_value='s-'+trial['id']),baseline=AsyncMock(return_value=0),prompt=AsyncMock())
        async def response(*args,**kwargs):
            if mutation=='switch':tests.create(self.group,a['id'],self.owner)
            elif mutation=='end':tests.leave(self.group,self.owner)
            elif mutation=='pause':workflow.policy.configure(self.chat,{'reply':False})
            return {'ok':True,'text':'formal final','reasoning':'真实API片段'*50}
        client.wait_response=AsyncMock(side_effect=response)
        if blocked:loop.governance.check_output=Mock(side_effect=PermissionError('blocked'))
        payload=self.payload('hello friend',sender='999999999',mid='in-'+trial['id'])
        async def run():
            with patch('runtime_adapter.DSHRuntimeAdapter.connect',return_value=client),patch('tool_host.bind_session') as bind,patch('tool_host.set_group_context') as context,patch.object(loop.runtime,'cancel',return_value={'ok':True}):
                loop.group_test_workflows[trial['id']]=workflow
                await loop._on_group(payload)
                await asyncio.gather(*list(loop.group_tasks))
                return bind,context
        bind,context=asyncio.run(run())
        return tests,trial,workflow,loop,client,bind,context

    def test_group_members_chat_a_sends_final_and_inbound_is_recorded(self):
        tests,trial,w,loop,client,bind,context=self.run_trial()
        self.assertEqual(loop._deliver_reply.await_count,1)
        text=loop._deliver_reply.await_args.args[1][0]['text']
        self.assertEqual(text,'formal final')
        self.assertNotIn('include_reasoning',client.wait_response.await_args.kwargs)
        self.assertTrue(bind.call_args.args[3].startswith(trial['test_scope']+':task:'))
        with closing(loop.store.connect()) as db:
            rows=db.execute('SELECT direction,text FROM messages ORDER BY id').fetchall()
        self.assertEqual([r['direction'] for r in rows],['inbound','outbound'])
        self.assertEqual(tests.contexts.space(trial['id'])['session'],'')

    def test_b_adds_only_bounded_actual_reasoning_to_same_formal_reply(self):
        tests,trial,w,loop,client,bind,context=self.run_trial(mode='thinking_excerpt')
        self.assertTrue(client.wait_response.await_args.kwargs['include_reasoning'])
        text=loop._deliver_reply.await_args.args[1][0]['text']
        self.assertIn('思考片段（API 返回）',text);self.assertIn('已截断',text)
        self.assertTrue(text.endswith('回复：formal final'));self.assertLess(len(text),240)

    def test_switch_end_pause_and_output_block_cancel_candidate(self):
        for mutation in ('switch','end','pause'):
            with self.subTest(mutation=mutation):
                *_,loop,client,bind,context=self.run_trial(mutation=mutation)
                loop._deliver_reply.assert_not_awaited()
        *_,loop,client,bind,context=self.run_trial(mode='thinking_excerpt',blocked=True)
        loop._deliver_reply.assert_not_awaited()

    def test_verified_owner_keeps_group_memory_scope(self):
        from tool_host import ToolHost
        a,_=self.candidates();tests=GroupTests(self.root);trial=tests.create(self.group,a['id'],self.owner)
        host=ToolHost(self.root)
        MemoryStore(self.root,'qq').save('OWNER_PRIVATE_CANARY',scope='owner')
        host.bind('qq','trial-session',trial['test_scope'],self.owner)
        identity=host.identity('qq','trial-session');self.assertTrue(identity['owner'])
        identities=read_json(self.root/'harness/runtime/qq.identities.json')
        self.assertTrue(identities['trial-session']['isolated'])
        self.assertEqual(identities['trial-session']['persona_revision'],a['id'])
        from request_inspector import RequestInspector
        inspector=RequestInspector(self.root)
        inspector.capture('qq',identity,{'messages':[{'role':'user','content':'hello'}]})
        self.assertEqual(inspector.latest('qq','trial-session')['revision'],a['id'])
        self.assertEqual(host.execute('qq','trial-session','fish_memory_search',{},'1'),[])
        for name,args in [('fish_memory_save',{'text':'bad'}),('fish_emoji_search',{'context':'hi'})]:
            with self.assertRaises(PermissionError):host.execute('qq','trial-session',name,args,'2')
        tests.leave(self.group,self.owner)
        with self.assertRaises(PermissionError):host.execute('qq','trial-session','fish_memory_search',{},'3')

    def test_real_group_rate_budget_is_shared_across_windows(self):
        a,b=self.candidates();tests=GroupTests(self.root);sa=tests.create(self.group,a['id'],self.owner)
        wa=GroupWorkflow(self.root,trial=sa);wa.policy.configure(self.chat,{'max_total_min':1,'reply_limits':True})
        wa.log(self.chat,1,'direct','sent','fake local delivery')
        sb=tests.create(self.group,b['id'],self.owner);wb=GroupWorkflow(self.root,trial=sb);wb.policy.configure(self.chat,{'max_total_min':1,'reply_limits':True})
        self.assertFalse(wb.within_budget(self.chat))

    def test_group_owner_commands_run_through_real_event_dispatch_and_stop(self):
        a,b=self.candidates();loop=self.loop();tests=GroupTests(self.root)
        async def run():
            await loop.on_event({'event':'message.received','payload':self.payload('/新建测试 '+a['id'][:8],mid='c1')})
            sa=tests.current(self.group)
            await loop.on_event({'event':'message.received','payload':self.payload('/新建测试 '+b['id'][:8],mid='c2')})
            self.assertNotEqual(tests.current(self.group)['id'],sa['id'])
            await loop.on_event({'event':'message.received','payload':self.payload('/切换测试 '+sa['id'][:8],mid='c3')})
            self.assertEqual(tests.current(self.group)['id'],sa['id'])
            await loop.on_event({'event':'message.received','payload':self.payload('/结束测试',mid='c4')})
            self.assertIsNone(tests.current(self.group))
        asyncio.run(run())
        self.assertEqual(loop.channel.action.await_count,4)
        self.assertFalse(GroupPolicy(self.root).config(self.chat)['reply'])

    def test_new_message_during_generation_is_not_left_without_a_task(self):
        a,_=self.candidates();tests=GroupTests(self.root);trial=tests.create(self.group,a['id'],self.owner)
        loop=self.loop();w=GroupWorkflow(self.root,trial=trial);w.policy.configure(self.chat,{'debounce_s':0})
        entered=asyncio.Event();release=asyncio.Event();seen=[]
        client=Mock(auth=AsyncMock(),create_session=AsyncMock(side_effect=['s-first','s-latest']),baseline=AsyncMock(return_value=0),prompt=AsyncMock())
        async def response(*args,**kwargs):
            seen.append(args[0])
            if len(seen)==1:
                entered.set();await release.wait()
            return {'ok':True,'text':'completed'}
        client.wait_response=AsyncMock(side_effect=response)
        loop._deliver_reply=AsyncMock(side_effect=[{'ok':True,'payload':{'message_id':'out-first'}},{'ok':True,'payload':{'message_id':'out-latest'}}])
        loop.group_test_workflows[trial['id']]=w
        async def run():
            with patch('runtime_adapter.DSHRuntimeAdapter.connect',return_value=client),patch('tool_host.bind_session'),patch('tool_host.set_group_context'):
                await loop._on_group(self.payload('first',mid='first'))
                await entered.wait()
                await loop._on_group(self.payload('latest',mid='latest'))
                release.set()
                await asyncio.gather(*list(loop.group_tasks))
                self.assertEqual(len(seen),2)
                self.assertEqual(loop._deliver_reply.await_count,2)
        asyncio.run(run())


if __name__=='__main__':unittest.main()
