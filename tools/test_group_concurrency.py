"""Concurrent group topics, bounded transport, quotes and read-only shared facts."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import AsyncMock,Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness'),str(ROOT/'tools')]
import dependencies
from test_social_base import Fixture
from group_tests import GroupTests
from group_tasks import GroupTaskStore,child_scope
from group_dialogue import GroupDialogue
from group_workflow import GroupWorkflow
from qq_agent_loop import QQAgentLoop
from memory_store import MemoryStore
from dsh_web import DSHWebClient
from storage import database


class ConcurrentTests(Fixture):
    def test_rebuilt_session_carries_selected_reply_recipient_for_other_person(self):
        self.setup_dialogue()
        async def run():
            await self.d.observe(self.payload('我喜欢乌龙茶','person-a',sender='333333333'))
            await self.drain()
            question=self.payload('你刚才回的是谁','person-b',sender='444444444')
            question['reply_to']='out-1'
            await self.d.observe(question);await self.drain()
        asyncio.run(run())
        envelope=json.loads(list(self.contexts.values())[-1]);context=envelope['context']
        self.assertIn('"reply_target": "444444444"',context)
        self.assertIn('"reply_target_id": "333333333"',context)
        self.assertIn('"message_id": "out-1"',context)
        self.assertEqual(sum(c.create_session.await_count for c in self.clients),2)

    def test_recent_background_picture_uses_prompt_attachments_not_text_envelope(self):
        self.setup_dialogue();self.d.workflow.policy.configure(self.chat,{'mode':'mention'})
        picture=self.payload('刚才这张图','picture',direct=False)
        picture['segments'].append({'type':'image','url':'https://example.test/picture.jpg'})
        def media(segments,text,**kwargs):
            return [{'type':'text','text':text}]+([{'type':'image','mediaType':'image/jpeg','data':'pixel-fixture'}] if any(s.get('type')=='image' for s in segments) else [])
        async def run():
            with patch('multimodal.MediaService.prepare_content',side_effect=media):
                await self.d.observe(picture);await self.d.observe(self.payload('这图怎么接','ask'));await self.drain()
        asyncio.run(run())
        self.assertTrue(any(b.get('type')=='image' for b in self.clients[-1].content))
        envelope=json.loads(list(self.contexts.values())[-1])
        self.assertTrue(all(b.get('type')=='text' for m in envelope['history'] for b in m['content']))
        from group_history import GroupHistory
        after=GroupHistory(self.root,self.d.jobs,self.loop.store,self.chat,self.trial['id'],'another-persona')
        self.assertEqual(after.key,self.d.history.key)

    def test_mention_without_words_is_a_valid_prompt(self):
        self.setup_dialogue()
        async def run():
            await self.d.observe(self.payload(' \n ','blank-at'));await self.drain()
        asyncio.run(run())
        self.assertEqual(len(self.sent),1)
        self.assertIn('没有附加文字',self.clients[0].content[0]['text'])
        self.assertEqual(self.sent[0]['reply_to'],'blank-at')

    def test_reply_style_reaches_actual_group_request(self):
        self.setup_dialogue()
        async def run():
            await self.d.observe(self.payload('请反驳一个观点并展开理由','discussion'));await self.drain()
        asyncio.run(run())
        context=json.loads(list(self.contexts.values())[-1])['context']
        self.assertIn('20–500',context)
        self.assertIn('200–400',context)
        self.assertIn('不要每次都加动作',context)
        self.assertIn('"max_chars": 500',context)
        self.assertNotIn('日常回复的长度预算为',context)
        self.assertEqual(len(self.sent),1)

    def setup_dialogue(self,mode='final'):
        self.tests=GroupTests(self.root)
        p=self.tests.contexts.save_persona('qq','same fish','同一个人格',owner_reply_mode=mode)
        self.trial=self.tests.create(self.group,p['id'],self.owner)
        self.loop=QQAgentLoop(self.root);self.sent=[];self.clients=[];self.contexts={};self.bound=[]
        async def action(name,payload,**kwargs):
            self.sent.append(payload);return {'ok':True,'payload':{'message_id':'out-'+str(len(self.sent))}}
        self.loop.channel=Mock(bot={'id':'20001'},action=AsyncMock(side_effect=action))
        w=GroupWorkflow(self.root,trial=self.trial);w.policy.configure(self.chat,{'debounce_s':0})
        self.d=GroupDialogue(self.loop,w,self.chat,merge_s=.025)
        self.loop.group_dialogues[self.trial['id']]=self.d
        self.loop.group_test_workflows[self.trial['id']]=w
        self.on_reply=None
        def client_factory(*args,**kwargs):
            c=Mock(auth=AsyncMock(),create_session=AsyncMock(return_value='s-'+uuid.uuid4().hex),baseline=AsyncMock(return_value=0))
            async def prompt(s,content):c.content=content
            async def result(*args,**kwargs):
                if self.on_reply:return await self.on_reply(c,kwargs)
                return {'ok':True,'text':'答：'+c.content[0]['text'],'reasoning':'API实际思考片段'*40}
            c.prompt=AsyncMock(side_effect=prompt);c.wait_response=AsyncMock(side_effect=result);self.clients.append(c);return c
        self.patches=[patch('runtime_adapter.DSHRuntimeAdapter.connect',side_effect=client_factory),
            patch('tool_host.bind_session',side_effect=lambda *args:self.bound.append(args)),
            patch('tool_host.set_group_context',side_effect=lambda root,s,text:self.contexts.update({s:text})),
            patch.object(self.loop.runtime,'cancel',return_value={'ok':True})]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def payload(self,text,mid,sender='333333333',direct=True):
        return {'chat_id':self.chat,'chat_type':'group','message_id':mid,'sender':{'id':sender,'display_name':'群友'},
            'mentions_bot':direct,'segments':[{'type':'text','text':text}]}

    async def until(self,predicate):
        async def wait():
            while not predicate():await asyncio.sleep(.005)
        await asyncio.wait_for(wait(),3)

    async def drain(self):
        for _ in range(5):
            tasks=[t for t in self.loop.group_tasks if not t.done()]
            if not tasks:return
            await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),4)

    def test_two_people_reply_in_order_with_distinct_focused_sessions(self):
        self.setup_dialogue();release=asyncio.Event();started=asyncio.Event()
        async def reply(c,kwargs):
            if 'SLOW_A' in c.content[0]['text']:
                started.set();await release.wait()
            return {'ok':True,'text':'DONE '+c.content[0]['text']}
        self.on_reply=reply
        async def run():
            await self.d.observe(self.payload('SLOW_A','a'))
            await started.wait()
            await self.d.observe(self.payload('FAST_B','b',sender='444444444'))
            await asyncio.sleep(.08)
            self.assertEqual(len(self.clients),1)
            self.assertEqual(self.sent,[])
            release.set();await self.drain()
        asyncio.run(run())
        self.assertEqual([s['reply_to'] for s in self.sent],['a','b'])
        self.assertEqual(sum(c.create_session.await_count for c in self.clients),2)
        self.assertNotEqual(self.clients[0].prompt.await_args.args[0],self.clients[1].prompt.await_args.args[0])
        with database(self.d.jobs.path) as db:
            snapshot=json.loads(db.execute("SELECT snapshot FROM jobs WHERE root='b'").fetchone()[0])
        self.assertEqual(snapshot['request_view']['runtime']['strategy'],'fresh-focused-view-v1')
        self.assertEqual(snapshot['request_view']['runtime']['previous_session'],self.clients[0].prompt.await_args.args[0])
        self.assertEqual(self.tests.contexts.space(self.trial['id'])['session'],'')

    def test_followup_plain_text_merges_before_generation_and_deduplicates(self):
        self.setup_dialogue();self.d.merge_s=.2
        async def run():
            await self.d.observe(self.payload('看这张图','a'))
            await self.d.observe(self.payload('补充：重点看衣服','b',direct=False))
            await self.d.observe(self.payload('补充：重点看衣服','b',direct=False))
            await self.drain()
        asyncio.run(run());self.assertEqual(len(self.sent),1)
        self.assertIn('重点看衣服',self.sent[0]['segments'][0]['text'])
        self.assertEqual(self.sent[0]['reply_to'],'a')

    def test_unrelated_chat_does_not_cancel_direct_generation(self):
        self.setup_dialogue();started=asyncio.Event();release=asyncio.Event()
        async def reply(c,kwargs):started.set();await release.wait();return {'ok':True,'text':'原问题回答'}
        self.on_reply=reply
        async def run():
            await self.d.observe(self.payload('原问题','a'));await started.wait()
            await self.d.observe(self.payload('无关闲聊','b',sender='444444444',direct=False));release.set();await self.drain()
        asyncio.run(run());self.assertEqual(len(self.sent),1);self.assertEqual(self.sent[0]['reply_to'],'a')

    def test_explicit_context_clear_cancels_inflight_reply(self):
        self.setup_dialogue();started=asyncio.Event();release=asyncio.Event()
        async def reply(c,kwargs):started.set();await release.wait();return {'ok':True,'text':'已清理上下文中的旧回答'}
        self.on_reply=reply
        async def run():
            await self.d.observe(self.payload('原问题','a'));await started.wait()
            self.d.workflow.policy.clear(self.chat);release.set();await self.drain()
        asyncio.run(run());self.assertEqual(self.sent,[])

    def test_ordered_window_does_not_drop_waiting_people(self):
        self.setup_dialogue();active=0;peak=0;release=asyncio.Event()
        async def reply(c,kwargs):
            nonlocal active,peak
            active+=1;peak=max(peak,active);await release.wait();active-=1
            return {'ok':True,'text':'ok'}
        self.on_reply=reply
        async def run():
            for n in range(5):await self.d.observe(self.payload('question'+str(n),str(n),sender=str(333333330+n)))
            await self.until(lambda:peak==1);release.set();await self.drain()
        asyncio.run(run());self.assertEqual(peak,1);self.assertEqual(len(self.sent),5)
        self.assertEqual([s['reply_to'] for s in self.sent],list(map(str,range(5))))

    def test_same_person_followup_waits_for_its_thread_and_reads_previous_reply(self):
        self.setup_dialogue();started=asyncio.Event();release=asyncio.Event();calls=[]
        async def reply(c,kwargs):
            calls.append(c.content[0]['text'])
            if len(calls)==1:started.set();await release.wait()
            return {'ok':True,'text':'线程回复'+str(len(calls))}
        self.on_reply=reply
        async def run():
            await self.d.observe(self.payload('第一问','a'));await started.wait()
            await self.d.observe(self.payload('接着上一问','b'))
            await asyncio.sleep(.06);self.assertEqual(len(calls),1)
            release.set();await self.drain()
        asyncio.run(run());self.assertEqual(len(calls),2)
        self.assertNotEqual(self.clients[0].prompt.await_args.args[0],self.clients[1].prompt.await_args.args[0])
        envelope=json.loads(self.contexts[self.clients[1].prompt.await_args.args[0]])
        self.assertIn('线程回复1',json.dumps(envelope['history'],ensure_ascii=False))
        from storage import database
        with database(self.d.history.path) as db:
            history=[r['message'] for r in db.execute('SELECT message FROM dialogue_messages WHERE scope=? ORDER BY seq',(self.d.history.key,))]
        self.assertIn('线程回复1','\n'.join(history))

    def test_cancellation_during_send_keeps_uncertain_without_duplicate_notice(self):
        self.setup_dialogue();entered=asyncio.Event()
        async def sending(name,payload,**kwargs):
            self.sent.append(payload);entered.set();await asyncio.Event().wait()
        self.loop.channel.action=AsyncMock(side_effect=sending)
        async def run():
            await self.d.observe(self.payload('hello','a'));await entered.wait();await self.d.cancel();await self.drain()
        asyncio.run(run());self.assertEqual(len(self.sent),1)
        with closing(self.loop.store.connect()) as db:self.assertEqual(db.execute("SELECT status FROM messages WHERE channel_message_id='a'").fetchone()[0],'uncertain')

    def test_reply_limit_switch_reaches_transport_and_private_is_unchanged(self):
        self.setup_dialogue()
        from channels.fish_qq.policy import unlimited_group
        self.assertTrue(unlimited_group(self.root,self.chat))
        self.assertFalse(unlimited_group(self.root,'private:'+self.owner))
        self.d.workflow.policy.configure(self.chat,{'reply_limits':True})
        self.assertFalse(unlimited_group(self.root,self.chat))

    def test_rolling_summary_remains_temporary_and_does_not_block_direct_reply(self):
        self.setup_dialogue();entered=asyncio.Event();release=asyncio.Event()
        for n in range(24):self.d.workflow.policy.observe(self.payload('背景'+str(n),'background-'+str(n),direct=False))
        async def slow_summary(snapshot):entered.set();await release.wait();self.d.workflow.policy.save_summary(self.chat,snapshot['cursor'],'旧约定与消息编号仍保留')
        async def run():
            with patch.object(self.d,'summarize',side_effect=slow_summary):
                await self.d.observe(self.payload('明确提问','a'));await entered.wait()
                await self.until(lambda:len(self.sent)==1)
                release.set();await self.drain()
        asyncio.run(run());self.assertEqual(len(self.sent),1)
        self.assertIn('旧约定',self.d.workflow.policy.snapshot(self.chat)['summary'])
        self.assertEqual(MemoryStore(self.root,'qq').search(scope='chat:'+self.chat),[])

    def test_switching_window_cancels_queued_and_running_jobs(self):
        self.setup_dialogue();started=asyncio.Event();release=asyncio.Event()
        async def reply(c,kwargs):started.set();await release.wait();return {'ok':True,'text':'不可发送'}
        self.on_reply=reply
        async def run():
            await self.d.observe(self.payload('原问题','a'));await started.wait()
            await self.d.cancel();release.set();await self.drain()
        asyncio.run(run());self.assertEqual(self.sent,[])
        with closing(self.loop.store.connect()) as db:self.assertEqual(db.execute("SELECT status FROM messages WHERE channel_message_id='a'").fetchone()[0],'cancelled')

    def test_b_excerpt_shared_group_facts_and_private_memory_excluded(self):
        self.setup_dialogue(mode='thinking_excerpt')
        m=MemoryStore(self.root,'qq');m.save('GREEN_TEA_GROUP_FACT',scope='chat:'+self.chat);m.save('OWNER_PRIVATE_CANARY',scope='owner')
        async def run():await self.d.observe(self.payload('GREEN_TEA_GROUP_FACT','a'));await self.drain()
        asyncio.run(run());shown=self.sent[0]['segments'][0]['text']
        self.assertIn('思考片段（API 返回）',shown);self.assertIn('已截断',shown)
        text='\n'.join(self.contexts.values());self.assertIn('GREEN_TEA_GROUP_FACT',text);self.assertNotIn('OWNER_PRIVATE_CANARY',text)
        self.assertTrue(self.clients[0].wait_response.await_args.kwargs['include_reasoning'])

    def test_failure_is_recorded_and_gets_one_quoted_notice(self):
        self.setup_dialogue()
        async def reply(c,kwargs):raise RuntimeError('model unavailable')
        self.on_reply=reply
        async def run():await self.d.observe(self.payload('hello','a'));await self.drain()
        asyncio.run(run());self.assertEqual(len(self.sent),1);self.assertEqual(self.sent[0]['reply_to'],'a')
        self.assertIn('没能完成',self.sent[0]['segments'][0]['text'])
        with closing(self.loop.store.connect()) as db:self.assertEqual(db.execute("SELECT status FROM messages WHERE channel_message_id='a'").fetchone()[0],'failed')

    def test_job_identity_shares_group_memory_without_parent_binding_or_owner_power(self):
        self.setup_dialogue();store=GroupTaskStore(self.root)
        job=store.add(self.payload('hello','a'),0,self.trial['id'],self.trial['revision']);job=store.claim(job['id'],{})
        from tool_host import ToolHost
        host=ToolHost(self.root);scope=child_scope(self.chat,self.trial['id'],job['id'])
        host.bind('qq','child-session',scope,job['sender'])
        self.assertFalse(host.identity('qq','child-session')['owner'])
        self.assertEqual(self.tests.contexts.space(self.trial['id'])['session'],'')
        m=MemoryStore(self.root,'qq');m.save('shared-group',scope='chat:'+self.chat);m.save('owner-only',scope='owner')
        rows=host.execute('qq','child-session','fish_memory_search',{},'search')
        self.assertEqual([r['text'] for r in rows],['shared-group'])
        with self.assertRaises(PermissionError):host.execute('qq','child-session','fish_memory_save',{'text':'bad'},'save')
        with self.assertRaises(PermissionError):m.save('bad',scope=scope)
        store.update(job['id'],state='sent')
        with self.assertRaises(PermissionError):host.execute('qq','child-session','fish_memory_search',{},'late')

    def test_restart_marks_old_jobs_interrupted_without_replay(self):
        self.setup_dialogue();store=self.d.jobs
        job=store.add(self.payload('old','a'),0,self.trial['id'],self.trial['revision'])
        store.recover(self.chat,self.trial['id']);self.assertEqual(store.get(job['id'])['state'],'interrupted')
        self.assertEqual(self.sent,[])


class TransportTests(unittest.TestCase):
    def test_configured_gib_and_receive_snapshot_larger_than_old_megabyte(self):
        from websockets.asyncio.server import serve
        async def run():
            async def handler(ws):
                request=json.loads(await ws.recv())
                await ws.send(json.dumps({'type':'item','streamId':request['streamId'],'value':{'type':'snapshot','cursor':42,'padding':'x'*(2*1024*1024)}}))
            server=await serve(handler,'127.0.0.1',0)
            try:
                with patch.dict(os.environ,{'FISH_DSH_FOLLOW_MAX_MIB':'1024'}):client=DSHWebClient('http://127.0.0.1/')
                self.assertEqual(client.follow_max_bytes,1024**3)
                client.ws_url='ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
                self.assertEqual(await client._cursor('fixture'),42)
            finally:server.close();await server.wait_closed()
        asyncio.run(run())
        with patch.dict(os.environ,{'FISH_DSH_FOLLOW_MAX_MIB':'unlimited'}):self.assertIsNone(DSHWebClient('http://127.0.0.1/').follow_max_bytes)

    def test_size_error_does_not_hide_for_two_minutes(self):
        async def run():
            c=DSHWebClient('http://127.0.0.1/');c._cursor=AsyncMock(side_effect=RuntimeError('sent 1009 frame exceeds limit'))
            result=await asyncio.wait_for(c.wait_response('s',0,120),.5)
            self.assertFalse(result['ok']);self.assertIn('读取超过允许大小',result['error'])
        asyncio.run(run())

    def test_explicitly_unlimited_group_bypasses_count_caps(self):
        from channels.fish_qq.policy import OutboundLimiter
        p=OutboundLimiter(1,1);p.hit('group:12345',now=100)
        self.assertFalse(p.check('group:12345',now=101)[0]);self.assertTrue(p.check('group:12345',now=101,unlimited=True)[0])
        self.assertFalse(p.check('private:12345',now=101)[0])


if __name__=='__main__':unittest.main()
