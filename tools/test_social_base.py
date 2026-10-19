"""Social foundation acceptance with local disposable data and mock transport."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,Mock,patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
import dependencies
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from fish_desktop.bootstrap import initialize
from storage import database,read_json,write_json
from social_context import SocialContext
from memory_store import MemoryStore
from group_policy import GroupPolicy


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=initialize(Path(self.tmp.name)/'personal')
        self.owner='123456789'; self.group='987654321'; self.chat='group:'+self.group
        cfg=read_json(self.root/'channels/fish_qq/config.json'); cfg['allowlist'].update(owner_id=self.owner,group_ids=[self.group]); write_json(self.root/'channels/fish_qq/config.json',cfg)
    def tearDown(self): self.tmp.cleanup()


class ContextTests(Fixture):
    def test_exact_owner_approval_and_new_revision_do_not_replace_group(self):
        c=SocialContext(self.root); p=c.save_persona('qq','测试 A','原人格'); space=c.create_space('qq',revision=p['id'],origin='owner-qq')
        with self.assertRaises(PermissionError): c.approve(p['id'],self.owner)
        with self.assertRaises(PermissionError): c.record_owner_reply(space['id'],'111111111')
        c.record_owner_reply(space['id'],self.owner); c.approve(p['id'],self.owner)
        c.deploy(self.group,p['id'],self.owner,enabled=True)
        q=c.save_persona('qq','测试 B','新版本',parent=p['id'])
        self.assertFalse(q['approved']); self.assertEqual(c.deployment(self.group)['revision'],p['id'])
        with self.assertRaises(PermissionError): c.deploy(self.group,q['id'],self.owner,enabled=True)
        cfg=read_json(self.root/'channels/fish_qq/config.json'); cfg['allowlist']['owner_id']='222222222';write_json(self.root/'channels/fish_qq/config.json',cfg)
        self.assertFalse(c.deployment(self.group)['enabled'])

    def test_spaces_pin_runtime_and_owner_route_independently(self):
        c=SocialContext(self.root); a=c.create_space('qq'); b=c.create_space('qq',origin='owner-qq')
        c.bind_runtime(a['id'],'a-session')
        with self.assertRaises(PermissionError): c.bind_runtime(a['id'],'new-session')
        with self.assertRaises(Exception): c.bind_runtime(b['id'],'a-session')
        c.select_owner_space(b['id'],self.owner)
        self.assertEqual(SocialContext(self.root).owner_space(self.owner)['id'],b['id'])
        with self.assertRaises(ValueError): c.select_owner_space(a['id'],self.owner)
        c.leave_owner_space(self.owner);self.assertIsNone(c.owner_space(self.owner))

    def test_rule_snapshot_is_immutable_after_source_edit(self):
        c=SocialContext(self.root); p=c.save_persona('qq','A','正文',global_rules='global-A',workspace_rules='workspace-A')
        a=c.create_space('qq',revision=p['id']); (self.root/'harness/global/AGENTS.md').write_text('global-B')
        self.assertIn('global-A',c.prompt(a['id'])); self.assertNotIn('global-B',c.prompt(a['id']))
        q=c.save_persona('qq','B','另一正文',parent=p['id']);self.assertEqual(q['global_rules'],'global-A')

    def test_model_change_invalidates_old_approval_and_requires_new_test(self):
        c=SocialContext(self.root);p=c.ensure_default('qq');space=c.create_space('qq',revision=p['id'],origin='owner-qq')
        c.record_owner_reply(space['id'],self.owner);c.approve(p['id'],self.owner)
        cfg=read_json(self.root/'harness/agents.json');cfg['agents'][0]['model']='different-fixture-model';write_json(self.root/'harness/agents.json',cfg)
        self.assertFalse(c.persona(p['id'])['approved'])
        with self.assertRaises(PermissionError):c.approve(p['id'],self.owner)
        c.record_owner_reply(space['id'],self.owner);self.assertTrue(c.approve(p['id'],self.owner)['approved'])

    def test_qq_command_is_private_verified_and_does_not_approve_untried_version(self):
        from owner_tests import command
        from governance import Governance
        loop=Mock(root=self.root,governance=Governance(self.root),channel=Mock(action=AsyncMock()))
        p=SocialContext(self.root).ensure_default('qq')
        event={'chat_type':'group','chat_id':self.chat,'segments':[{'type':'text','text':'/新建测试 '+p['id']}]}
        self.assertFalse(asyncio.run(command(loop,event,self.owner)))
        event.update(chat_type='private',chat_id='private:'+self.owner,message_id='test-command-1')
        self.assertTrue(asyncio.run(command(loop,event,self.owner)))
        c=SocialContext(self.root);self.assertIsNotNone(c.owner_space(self.owner));self.assertFalse(c.deployment(self.group)['enabled'])
        event['segments']=[{'type':'text','text':'/认可人格 '+p['id']}]
        asyncio.run(command(loop,event,self.owner));self.assertFalse(c.persona(p['id'])['approved'])

    def test_desktop_session_cwd_scope_and_model_history_are_independent(self):
        from desktop_chat import DesktopChat
        chat=DesktopChat(self.root,'qq',Mock()); a=chat.new('A'); b=chat.new('B')
        def client(session):return Mock(auth=AsyncMock(),create_session=AsyncMock(return_value=session),baseline=AsyncMock(return_value=0),prompt=AsyncMock(),wait_response=AsyncMock(return_value={'ok':True,'text':'已收到'}))
        ca,cb=client('session-A'),client('session-B')
        with patch('tool_host.bind_session'),patch.object(chat,'_client',side_effect=[ca,cb]):
            chat.send(a,'SECRET_A_92',[]);chat.send(b,'B 本轮',[])
        self.assertNotIn('SECRET_A_92',str(cb.prompt.await_args))
        ac,bc=chat.conversation_context(a),chat.conversation_context(b)
        self.assertNotEqual(ac['id'],bc['id']);self.assertEqual(ac['session'],'session-A')
        self.assertEqual(ca.create_session.await_args.kwargs['cwd'],str(SocialContext(self.root).directory(ac['id'])))
        self.assertNotIn('SECRET_A_92',str(chat.snapshot(b)['messages']))


class MemoryTests(Fixture):
    def test_scope_subject_expiry_and_deletion_are_independent(self):
        m=MemoryStore(self.root,'qq'); a='chat:group:900001';b='chat:group:900002'
        m.save('公开资料',scope='public');mid=m.save('账号喜欢乌龙茶',scope=a,subject='qq:10001',aliases=['阿茶'],kind='preference')['id']
        self.assertEqual(m.search(scope=b),[]);self.assertEqual(m.search(scope='chat:'+self.chat),[])
        self.assertEqual(m.search('阿茶',scope=a)[0]['id'],mid)
        self.assertEqual(m.profile('qq:10001',scope=a)[0]['id'],mid)
        m.save('过期情绪',scope=a,kind='emotion',expires=time.time()-1)
        self.assertEqual(m.search('过期',scope=a),[])
        with self.assertRaises(PermissionError):m.save('挪区',scope=b,memory_id=mid)
        m.delete(mid);self.assertTrue(m.save('账号喜欢乌龙茶',scope=a,source='old-auto')['blocked'])
        self.assertTrue(m.save('账号喜欢乌龙茶',scope=b,source='B-auto')['ok'])
        self.assertEqual(MemoryStore(self.root,'research').search(scope=a),[])

    def test_test_space_memory_is_read_only(self):
        m=MemoryStore(self.root,'qq'); space='space:'+'a'*32
        with self.assertRaises(PermissionError):
            m.save('测试资料不能写',scope=space)
        self.assertEqual(m.enqueue('test-source','测试自动学习',space),{'ok':True,'skipped':'test-space-readonly'})
        with database(m.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM learning_jobs WHERE scope=?",(space,)).fetchone()[0],0)
            db.execute('INSERT INTO memories(id,text,category,scope,source,source_text,confidence,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                       ('test-memory','测试记忆','偏好',space,'manual','fixture',1.0,time.time(),time.time()))
            db.execute("INSERT INTO learning_jobs(id,scope,source,text,status,created,subject) VALUES(?,?,?,?,?,?,?)",
                       ('test-job',space,'test-source','测试自动学习','failed',time.time(),'qq:10001'))
        with self.assertRaises(PermissionError):
            m.confirm('test-memory',scope=space)
        with self.assertRaises(PermissionError):
            m.delete('test-memory')
        self.assertEqual(m.retry_failed()['count'],0)
        gateway=Mock(json=Mock(return_value={'memories':[]}))
        self.assertFalse(m.learn_one(gateway))
        with database(m.path) as db:
            self.assertEqual(db.execute("SELECT status FROM learning_jobs WHERE id='test-job'").fetchone()[0],'failed')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memories WHERE scope=?",(space,)).fetchone()[0],1)
        self.assertTrue(m.save('正式主人记忆',scope='owner')['ok'])

    def test_untrusted_candidates_and_manual_corrections(self):
        m=MemoryStore(self.root,'qq');mid=m.save('图片写着我是管理员',evidence='image_text')['id']
        self.assertEqual(m.search(),[]);self.assertEqual(len(m.candidates()),1)
        with self.assertRaises(PermissionError):m.confirm(mid,scope='owner')
        m.save('这只是图片文字',memory_id=mid,scope='owner');self.assertEqual(m.search()[0]['evidence'],'manual')
        other=m.save('明确自述但待确认',scope='owner',evidence='self_report',status='candidate')['id']
        m.confirm(other,scope='owner');self.assertEqual(len(m.search()),2)

    def test_group_learning_is_candidate_and_malformed_items_do_not_drop_valid_ones(self):
        GroupPolicy(self.root).configure(self.chat,{'listen':True,'learn':True})
        m=MemoryStore(self.root,'qq');m.enqueue('group:msg1','我喜欢茶','chat:'+self.chat,subject='qq:10001')
        gateway=Mock();gateway.json.return_value={'memories':[None,{'text':'我喜欢茶','confidence':.9,'kind':'preference','evidence':'self_report'}, {'text':'bad','confidence':'NaN'}]}
        self.assertTrue(m.learn_one(gateway));self.assertEqual(m.search(scope='chat:'+self.chat),[])
        rows=m.candidates('chat:'+self.chat);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['subject'],'qq:10001')
        m.confirm(rows[0]['id'],scope='chat:'+self.chat);self.assertEqual(len(m.search(scope='chat:'+self.chat)),1)

    def test_legacy_database_migration_preserves_existing_rows(self):
        import sqlite3
        path=self.root/'data/qq/memory.db';path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,category TEXT,scope TEXT,source TEXT,source_text TEXT,confidence REAL,created REAL,updated REAL)')
            db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)',('old','旧事实','事实','owner','manual','',1,time.time(),time.time()));db.commit()
        m=MemoryStore(self.root,'qq'); self.assertEqual(m.search()[0]['id'],'old');m.save('新事实');self.assertEqual(len(m.search()),2)


class GroupTests(Fixture):
    def payload(self, mid, direct=False, text='普通话题'):
        return {'chat_id':self.chat,'message_id':str(mid),'sender':{'id':'10001','name':'阿茶'},'segments':[{'type':'text','text':text}], 'mentions_bot':direct}
    def enabled(self,**changes):
        p=GroupPolicy(self.root);p.configure(self.chat,dict(listen=True,reply=True,debounce_s=0,**changes));return p
    def test_default_off_observe_dedup_bounds_and_lookback(self):
        p=GroupPolicy(self.root);self.assertFalse(p.observe(self.payload(1))['accepted'])
        p.configure(self.chat,{'listen':True});self.assertFalse(p.config(self.chat)['reply'])
        for i in range(75):p.observe(self.payload(i),now=100+i)
        self.assertEqual(len(p.snapshot(self.chat,60)['messages']),60);self.assertEqual(p.snapshot(self.chat)['cursor'],75)
        self.assertFalse(GroupPolicy(self.root).observe(self.payload(74),now=200)['accepted'])
        self.assertEqual(p.candidate(self.chat,now=200)['action'],'silent')
    def test_single_reservation_cursor_change_cancel_and_no_restart_retry(self):
        p=self.enabled();p.observe(self.payload(1),now=100);token=p.reserve(self.chat,1,'active',now=100)
        self.assertTrue(token);self.assertFalse(GroupPolicy(self.root).reserve(self.chat,1,'active',now=101))
        self.assertTrue(p.valid(token,now=102));p.observe(self.payload(2),now=103);self.assertFalse(p.valid(token,now=104))
        p.finish(token,False,now=104);self.assertEqual(p.candidate(self.chat,now=105)['cursor'],2)
        t2=p.reserve(self.chat,2,'active',now=105);p.finish(t2,False,now=106)
        self.assertEqual(GroupPolicy(self.root).candidate(self.chat,now=107)['action'],'silent')
    def test_direct_obeys_total_limit_and_active_has_separate_cooldown(self):
        p=self.enabled(max_total_min=1);p.observe(self.payload(1,True),now=100);t=p.reserve(self.chat,1,'direct',now=100);p.finish(t,True,now=101)
        p.observe(self.payload(2,True),now=102);self.assertEqual(p.candidate(self.chat,now=102)['action'],'wait')
        self.assertEqual(p.candidate(self.chat,now=162)['action'],'candidate')
    def test_pause_clear_summary_inflight_and_expired_reservation(self):
        p=self.enabled();p.observe(self.payload(1,True),now=100);t=p.reserve(self.chat,1,'direct',now=100)
        p.configure(self.chat,{'reply':False});self.assertFalse(p.valid(t,now=101))
        p.configure(self.chat,{'reply':True});self.assertEqual(p.candidate(self.chat,now=102)['action'],'silent')
        p.observe(self.payload(2),now=103);t=p.reserve(self.chat,2,'active',now=103)
        self.assertFalse(p.valid(t,now=283));self.assertFalse(p.reserve(self.chat,2,'active',now=283))
        p.clear(self.chat);p.save_summary(self.chat,2,'旧摘要');self.assertEqual(p.snapshot(self.chat)['summary'],'')
    def test_listen_without_deployment_never_calls_model(self):
        from group_workflow import GroupWorkflow
        from governance import Governance
        workflow=GroupWorkflow(self.root);workflow.policy.configure(self.chat,{'listen':True,'reply':True})
        loop=Mock(governance=Governance(self.root),channel=Mock(bot={'id':'20001'}),group_tasks=set())
        with patch('model_gateway.ModelGateway') as gateway:
            asyncio.run(workflow.observe(loop,self.payload(1)));gateway.assert_not_called()
        self.assertFalse(workflow.pending)

    def test_generation_is_cancelled_when_new_topic_or_pause_arrives(self):
        from group_workflow import GroupWorkflow
        from qq_agent_loop import QQAgentLoop
        c=SocialContext(self.root);p=c.ensure_default('qq');space=c.create_space('qq',revision=p['id'],origin='owner-qq')
        c.record_owner_reply(space['id'],self.owner);c.approve(p['id'],self.owner);c.deploy(self.group,p['id'],self.owner,enabled=True)
        async def check():
            for mutate in ('topic','pause'):
                loop=QQAgentLoop(self.root);workflow=loop.social_workflow
                workflow.policy.configure(self.chat,{'listen':True,'reply':True,'debounce_s':0})
                mid='trigger-'+mutate;payload=self.payload(mid,True);workflow.policy.observe(payload);workflow.latest[self.chat]=payload
                client=Mock(auth=AsyncMock(),create_session=AsyncMock(return_value='session-'+mutate),baseline=AsyncMock(return_value=0),prompt=AsyncMock())
                async def response(*args,**kwargs):
                    if mutate=='topic':workflow.policy.observe(self.payload('new-topic',text='话题已经变化'))
                    else:workflow.policy.configure(self.chat,{'reply':False})
                    return {'ok':True,'text':'本应被撤销的候选'}
                client.wait_response=AsyncMock(side_effect=response);loop._deliver_reply=AsyncMock(return_value={'ok':True})
                with patch('runtime_adapter.DSHRuntimeAdapter.connect',return_value=client),patch('tool_host.bind_session'):
                    await workflow.run(loop,self.chat)
                loop._deliver_reply.assert_not_awaited()
        asyncio.run(check())

    def test_owner_test_only_records_acceptance_after_delivery(self):
        from qq_agent_loop import QQAgentLoop
        from owner_tests import process
        c=SocialContext(self.root);p=c.ensure_default('qq');space=c.create_space('qq',revision=p['id'],origin='owner-qq')
        async def check():
            loop=QQAgentLoop(self.root);conv=loop.store.get_or_create_conversation('test:'+space['id'],'qq')
            client=Mock(auth=AsyncMock(),create_session=AsyncMock(return_value='owner-test-session'),baseline=AsyncMock(return_value=0),prompt=AsyncMock(),wait_response=AsyncMock(return_value={'ok':True,'text':'测试答复'}))
            for index,success in enumerate((False,True)):
                mid=loop.store.record_message(conv['id'],'private:'+self.owner,self.owner,'inbound','in-'+str(index),None,[],'你好','queued')
                item={'space_id':space['id'],'message_id':mid,'conversation':conv,'text':'你好','payload':{'chat_id':'private:'+self.owner,'segments':[]}}
                loop._deliver_reply=AsyncMock(return_value={'ok':success,'payload':{'message_id':'fake-out-'+str(index)}})
                with patch('runtime_adapter.DSHRuntimeAdapter.connect',return_value=client),patch('owner_tests.bind_session'):
                    await process(loop,item)
                if not success:
                    with self.assertRaises(PermissionError):c.approve(p['id'],self.owner)
            self.assertFalse(c.persona(p['id'])['approved']);self.assertTrue(c.approve(p['id'],self.owner)['approved'])
        asyncio.run(check())


class RuntimeGuardTests(Fixture):
    def test_native_plugin_guards_and_capture_redaction(self):
        script=self.root/'probe.mjs';module=(ROOT/'harness/runtime/fish-control/lib/index.js').as_uri()
        script.write_text('import assert from "node:assert/strict";\nimport {toolDenial,snapshotRequest,independentMessages} from '+json.dumps(module)+''';
const root=process.argv[2], own=root+'/data/qq/spaces/abc';
const cfg={isolated:true,allowed_paths:[own],fishTools:['fish_memory_search'],nativeTools:['private_plugin']};
assert.ok(toolDenial({name:'glob',arguments:{pattern:'**/*'}},cfg,root,'qq'));
assert.ok(toolDenial({name:'read_file',arguments:{path:root+'/data/qq/secret.txt'}},cfg,root,'qq'));
assert.ok(toolDenial({name:'private_plugin',arguments:{}},cfg,root,'qq'));
assert.equal(toolDenial({name:'read_file',arguments:{path:own+'/safe.txt'}},cfg,root,'qq'),undefined);
assert.equal(toolDenial({name:'fish_memory_search',arguments:{query:'hi'}},cfg,root,'qq'),undefined);
const snapshot=snapshotRequest({model:'fixture',messages:[{role:'user',content:[{type:'text',text:'sk-abcdef123 password=hide-me'},{type:'image',data:'IMAGE-BYTES'}]},{role:'assistant',content:[{type:'thinking',text:'PRIVATE-REASONING'}]}]});
assert.ok(!JSON.stringify(snapshot).includes('PRIVATE-REASONING'));assert.ok(!JSON.stringify(snapshot).includes('IMAGE-BYTES'));assert.ok(!JSON.stringify(snapshot).includes('hide-me'));
assert.equal(independentMessages({kind:'enter',messages:[{source:{kind:'agent-instructions'}},{source:{kind:'user'}}]},true).messages.length,1);
console.log('native guard and redaction passed');
''',encoding='utf-8')
        result=subprocess.run([str(ROOT/'vendor/runtime/node/node.exe'),str(script),str(self.root)],capture_output=True,text=True,encoding='utf-8',timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr)
    def test_capture_is_bound_redacted_and_retention_limited(self):
        from request_inspector import RequestInspector
        c=SocialContext(self.root);space=c.create_space('qq');i=RequestInspector(self.root)
        identity={'session':'s1','scope':space['scope']}
        for _ in range(14):i.capture('qq',identity,{'model':'fixture','messages':[{'role':'user','content':'sk-abcdef password=hide-me'}], 'authorization':'SECRET'})
        data=i.latest('qq','s1');self.assertNotIn('hide-me',str(data));self.assertNotIn('SECRET',str(data));self.assertEqual(data['revision'],space['revision'])
        with database(i.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],12)
        self.assertFalse(i.latest('research','s1')['captured'])


if __name__=='__main__':unittest.main()
