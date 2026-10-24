"""Recent runtime regressions only: local temp roots, scripted provider, no QQ sends."""
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from experience import ExperienceTurn, was_considered
from governance import Governance, classify_fault
from model_gateway import ModelGateway, ChatOptions
from qq_agent_loop import QQAgentLoop
from qq_store import QQStore
from storage import database, thinking_slot
from desktop_context import DesktopContext


class RuntimeFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root / 'harness/agents.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'agents':[{'id':'qq','model':'fake-model','base_url':'https://fake.invalid'}]}))

    def mark_coverage(self, session, current='qq:1', covered='qq:2'):
        e = ExperienceTurn(self.root, self.root/'data/qq/qq.db', 'unused', channel='qq')
        with database(e.ledger) as db:
            db.execute('INSERT INTO experience_turns VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (e.id,session,current,'2026-10-24 20:00:00',1,None,None,None,'running',''))
        e.covered={current,covered}
        e.finish('considered')

    def test_group_background_does_not_skip_private_worker(self):
        self.mark_coverage('group-fish:group:10000')
        loop=QQAgentLoop.__new__(QQAgentLoop)
        loop.root=self.root
        loop._stop=threading.Event()
        loop.store=Mock()
        loop.store.update_message.side_effect=lambda *a,**k:loop._stop.set()
        called=[]
        async def process(item):
            called.append(item['message_id']);loop._stop.set()
        loop._process=process
        async def run():
            loop.queue=asyncio.Queue()
            await loop.queue.put({'message_id':2,'conversation':{'id':'private-cid'}})
            await asyncio.wait_for(loop._worker(),2)
        asyncio.run(run())
        self.assertEqual(called,[2], 'A group reading private input as context must not consume its private wake')

    def test_same_place_coalescing_is_preserved(self):
        session='qq-private:private-cid'
        self.mark_coverage(session)
        self.assertTrue(was_considered(self.root,'qq:2',session=session))
        self.assertFalse(was_considered(self.root,'qq:2',session='group-fish:group:10000'))

    def test_waiting_private_turn_runs_before_next_group_turn(self):
        waiting=threading.Event();release=threading.Event();order=[];errors=[]
        main=threading.get_ident()
        def audit(_self,kind,*args,**kwargs):
            if kind!='chat.queued':return
            if threading.get_ident()!=main:
                waiting.set()
                if not release.wait(3):raise AssertionError('waiter was not released')
            else:release.set()
        def private():
            try:
                with thinking_slot(self.root,'qq'):order.append('private')
            except BaseException as exc:errors.append(exc)
        with patch.object(Governance,'audit',autospec=True,side_effect=audit):
            with thinking_slot(self.root,'qq'):
                worker=threading.Thread(target=private)
                worker.start()
                self.assertTrue(waiting.wait(3))
            try:
                with thinking_slot(self.root,'qq'):order.append('group-next')
            finally:
                release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(order,['private','group-next'])

    def gateway(self, response):
        with patch('secrets_store.SecretStore.get',return_value='TEST_KEY'):
            gateway=ModelGateway(self.root,'qq')
        original=httpx.AsyncClient
        self.transport_patch=patch('model_gateway.httpx.AsyncClient',side_effect=lambda **kw:original(**kw,transport=httpx.MockTransport(response)))
        self.transport_patch.start();self.addCleanup(self.transport_patch.stop)
        return gateway

    def test_402_reports_balance_without_losing_provider_status(self):
        gateway=self.gateway(lambda request:httpx.Response(402,json={'error':{'code':'insufficient_balance','message':'Insufficient Balance Bearer sk-secret-1234567890'}}))
        with self.assertRaises(RuntimeError) as caught:
            gateway.chat([{'role':'user','content':'hello'}],options=ChatOptions('high',256,2))
        error=caught.exception
        self.assertIn('余额不足',str(error))
        self.assertEqual(classify_fault(error)['code'],'balance')
        self.assertNotIn('sk-secret',str(error))

    def test_400_reports_request_protocol_not_generic_execution_failure(self):
        gateway=self.gateway(lambda request:httpx.Response(400,json={'error':{'message':'Missing reasoning_content field in assistant message'}}))
        with self.assertRaises(RuntimeError) as caught:
            gateway.chat([{'role':'user','content':'hello'}],options=ChatOptions('high',256,2))
        value=classify_fault(caught.exception)
        self.assertEqual(value['code'],'protocol')
        self.assertIn('400',value['reason'])
        self.assertIn('reasoning_content',str(caught.exception))

    def test_stored_fish_speech_remains_history_data_not_invented_protocol(self):
        store=QQStore(self.root)
        conv=store.get_or_create_conversation('group:10000','qq')
        store.record_message(conv['id'],'group:10000','bot','outbound','out1',None,[],'此前已经说过的内容','sent')
        mid=store.record_message(conv['id'],'group:10000','30000','inbound','in2',None,[],'现在继续','observed')
        history,_=DesktopContext(self.root,store.db_path,channel='group:10000').history(conv['id'],mid,None,None,None)
        prior=next(m for m in history if '此前已经说过的内容' in m['content'])
        self.assertEqual(prior['role'],'user')
        self.assertIn('Fish',prior['content'])

    def test_background_summary_does_not_hold_main_thinking_slot(self):
        from context_compaction import enqueue, tick
        from model_gateway import ChatResult
        store=QQStore(self.root)
        conv=store.get_or_create_conversation('private:10000','qq')
        mid=store.record_message(conv['id'],'private:10000','10000','inbound','s1',None,[],'要整理的旧记录','processed')
        enqueue(self.root,store.db_path,conv['id'],mid)
        entered=threading.Event()
        gateway=Mock()
        gateway.chat.side_effect=lambda *a,**kw:(entered.set() or ChatResult('已整理的摘要',{},'stop'))
        budget=Mock(window=65536,ratio=.3)
        budget.message_tokens.return_value=1
        budget.fit.return_value={}
        with patch('model_gateway.ModelGateway',return_value=gateway),patch('context_budget.ContextBudget',return_value=budget):
            worker=threading.Thread(target=tick,args=(self.root,))
            try:
                with thinking_slot(self.root,'qq'):
                    worker.start()
                    self.assertTrue(entered.wait(1),'Background maintenance is incorrectly waiting on the foreground thinking slot')
            finally:worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_final_result_keeps_real_reasoning_for_protocol_consumers(self):
        events=[{'choices':[{'delta':{'reasoning_content':'REAL_PROTOCOL_REASONING'}}]},
                {'choices':[{'delta':{'content':'完成'},'finish_reason':'stop'}]}]
        stream=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n'
        gateway=self.gateway(lambda request:httpx.Response(200,text=stream))
        result=gateway.chat([{'role':'user','content':'hello'}],tools=[{'type':'function','function':{'name':'test','parameters':{'type':'object'}}}],options=ChatOptions('high',256,2))
        self.assertEqual(result.reasoning_content,'REAL_PROTOCOL_REASONING')
        self.assertEqual(result.text,'完成')


if __name__=='__main__':unittest.main()
