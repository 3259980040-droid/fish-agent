"""Regression cases from the actual dropped replies and false memories."""
import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from governance import Governance
from qq_agent_loop import QQAgentLoop
from memory_store import MemoryStore
from memory_evidence import save_from_tool
from storage import database
from group_tasks import GroupTaskStore
from group_history import GroupHistory
from conversation_policy import set_community, community_prompt
from multimodal import MediaService
from PIL import Image
from dsh_web import DSHWebClient
from image_knowledge import ImageKnowledge


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def make_loop(self, action):
        loop=QQAgentLoop.__new__(QQAgentLoop)
        loop.owner='3259980040';loop.cancel_event=asyncio.Event()
        loop.governance=Mock()
        loop.governance.config.return_value={'paused':False,'send_policy':{'owner_private':{'owner_reply':True}}}
        loop.channel=Mock(action=AsyncMock(side_effect=action))
        return loop

    async def test_full_long_reply_is_sent_and_only_first_part_quotes(self):
        sent=[]
        async def action(name,payload,**kwargs):
            sent.append(payload)
            return {'ok':True,'payload':{'message_id':str(len(sent))}}
        loop=self.make_loop(action)
        text='这一段调研结果。\n'*700
        result=await loop._deliver_reply('group:1043230169',[{'type':'text','text':text}],'long',reply_to='question')
        self.assertTrue(result['ok'])
        self.assertEqual(''.join(p['segments'][0]['text'] for p in sent),text)
        self.assertEqual(sent[0]['reply_to'],'question')
        self.assertNotIn('reply_to',sent[1])
        self.assertEqual(len(set(p['idempotency_key'] for p in sent)),len(sent))

    async def test_partial_delivery_is_uncertain_and_never_retries(self):
        calls=[]
        async def action(name,payload,**kwargs):
            calls.append(payload)
            if len(calls)==2:raise TimeoutError()
            return {'ok':True,'payload':{'message_id':'first'}}
        result=await self.make_loop(action)._deliver_reply('group:1043230169',[{'type':'text','text':'长文'*2500}],'partial')
        self.assertFalse(result['ok']);self.assertTrue(result['error']['may_have_sent'])
        self.assertEqual(result['error']['sent_parts'],1);self.assertEqual(len(calls),3)
        self.assertIn('没有取得发送回执',calls[-1]['segments'][0]['text'])

    async def test_generation_failure_has_visible_quoted_notice(self):
        loop=self.make_loop(lambda *a,**k:{'ok':True,'payload':{'message_id':'notice'}})
        await loop._failure_notice({'message_id':1191,'payload':{'chat_id':'private:3259980040','message_id':'source'}},'timeout')
        payload=loop.channel.action.call_args.args[1]
        self.assertEqual(payload['reply_to'],'source')
        self.assertIn('超时',payload['segments'][0]['text'])

    async def test_tool_only_completion_does_not_wait_for_nonexistent_final_text(self):
        client=DSHWebClient('http://127.0.0.1:1/?test')
        client._cursor=AsyncMock(return_value=1)
        client._page=AsyncMock(return_value={'records':[{'type':'event','event':{'seq':1,'type':'turn/end','data':{'reason':{'kind':'completed'}}}}]})
        result=await client.wait_response('test',0,timeout=1)
        self.assertTrue(result['ok']);self.assertEqual(result['text'],'')
        self.assertTrue(result['completed_without_text'])


class MemoryAndContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.identity={'sender':'3259980040','session':'test','owner':True}
        self.store=MemoryStore(self.root,'qq')
        self.warm=patch('memory_engine.enqueue_warm');self.warm.start();self.addCleanup(self.warm.stop)

    def test_tool_authority_does_not_make_inference_a_confirmed_fact(self):
        saved=save_from_tool(self.root,self.identity,{'text':'某角色的外文名是模型猜测的名字'},scope='owner')
        self.assertEqual(self.store.search(scope='owner'),[])
        row=self.store.candidates('owner')[0]
        self.assertEqual(row['id'],saved['id']);self.assertEqual(row['evidence'],'model_inference')

    def test_owner_evidence_stores_exact_words_instead_of_model_expansion(self):
        path=self.root/'data/qq/qq.db'
        with database(path) as db:
            db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,direction TEXT,sender_id TEXT,channel_message_id TEXT,text TEXT,chat_id TEXT)')
            db.execute('INSERT INTO messages VALUES(1187,?,?,?,?,?)',('inbound','3259980040','remote','这是赛马娘的爱如往昔的q版角色','private:3259980040'))
        saved=save_from_tool(self.root,self.identity,{'text':'主人喜欢该角色，其外文名是猜测值','evidence':'owner_quote',
            'source_message_id':'1187','source_quote':'这是赛马娘的爱如往昔的q版角色'},scope='owner')
        row=self.store.search(scope='owner')[0]
        self.assertEqual(row['id'],saved['id']);self.assertEqual(row['text'],'这是赛马娘的爱如往昔的q版角色')
        self.assertEqual(row['source'],'qq:1187')

    def test_extractor_cannot_promote_unquoted_preference(self):
        self.store.apply_extraction({'scope':'owner','source':'qq:1187','text':'这是赛马娘的爱如往昔的q版角色','subject':'qq:3259980040'},
            {'memories':[{'text':'主人喜欢爱如往昔','kind':'preference','evidence':'self_report','confidence':.95}]})
        self.assertEqual(self.store.search(scope='owner'),[])
        self.assertEqual(self.store.candidates('owner')[0]['evidence'],'model_inference')

    def test_no_memory_request_is_not_queued(self):
        result=self.store.enqueue('qq:test','这是一轮临时扮演测试，不保存为长期事实和记忆。')
        self.assertEqual(result['skipped'],'user-requested-no-memory')
        with database(self.store.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM learning_jobs').fetchone()[0],0)

    def test_roleplay_followups_stay_out_of_learning_and_daily_replay(self):
        self.store.enqueue('qq:1','接下来几轮你是便利店收银员阿泽。',subject='qq:3259980040')
        outcome=self.store.enqueue('qq:2','钱没有，故事倒有一肚子。',subject='qq:3259980040')
        self.assertEqual(outcome['skipped'],'roleplay-context')
        self.store.enqueue('qq:3','结束临时扮演，恢复原本的人设。',subject='qq:3259980040')
        self.store.enqueue('qq:1','接下来几轮你是便利店收银员阿泽。',subject='qq:3259980040')
        self.store.enqueue('qq:2','钱没有，故事倒有一肚子。',subject='qq:3259980040')
        self.store.enqueue('qq:4','我喜欢喝乌龙茶。',subject='qq:3259980040')
        with database(self.store.path) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT source FROM learning_jobs')],['qq:4'])

    def test_daily_uses_live_message_reference_and_respects_legacy_exclusions(self):
        from memory_maintenance import read_qq_messages
        from tools.test_memory_workflow import qq_db
        config=self.root/'channels/fish_qq/config.json';config.parent.mkdir(parents=True)
        config.write_text('{"allowlist":{"owner_id":"3259980040"}}',encoding='utf-8')
        qq_db(self.root,[('2026-10-20 10:00:00','private:3259980040','3259980040','不要保存长期记忆。','inbound')])
        rows=read_qq_messages(self.root,'qq',0)
        self.assertEqual(rows[0]['source'],'qq:1')
        self.assertEqual(rows[0]['source_aliases'],['qq:900001'])
        with database(self.store.path) as db:
            db.execute('INSERT INTO exclusions VALUES(?,?,?)',('old-digest','qq:900001',0))
        self.store.enqueue(rows[0]['source'],rows[0]['text'],source_aliases=rows[0]['source_aliases'])
        with database(self.store.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM learning_jobs').fetchone()[0],0)

    def test_archived_memory_is_reported_as_already_retired(self):
        self.store.save('错误角色名',category='识图纠错',scope='owner',status='archived')
        with patch.object(self.store,'recall',return_value={'results':[],'semantic':{'status':'lexical-only'}}):
            context=self.store.context('旧记忆删除了吗','owner')
        self.assertIn('已经停用',context)
        self.assertIn('不用再次请求删除',context)

    def test_old_roleplay_archive_does_not_revoke_new_roleplay(self):
        text='接下来几轮你是便利店收银员阿泽'
        self.store.save(text,category='临时扮演',scope='owner',evidence='roleplay',status='archived')
        with patch.object(self.store,'recall',return_value={'results':[],'semantic':{'status':'lexical-only'}}):
            context=self.store.context(text,'owner')
        self.assertNotIn('已经停用',context)
        self.assertNotIn('阿泽',context)

    def test_test_source_is_not_a_real_personal_preference(self):
        self.store.save('验收饮料是乌龙茶',source='qq-test:1',evidence='self_report')
        self.assertEqual(self.store.search(scope='owner'),[])
        self.assertEqual(self.store.candidates('owner')[0]['evidence'],'test_fixture')

    def test_unrelated_history_image_is_not_loaded_but_quoted_image_is(self):
        jobs=GroupTaskStore(self.root)
        with patch.object(GroupHistory,'import_existing'):
            history=GroupHistory(self.root,jobs,Mock(),'group:1043230169','','r')
        old={'message_id':'old','sender':{'id':'other'},'segments':[{'type':'image','url':'https://example.com/image'}]}
        current={'message_id':'now','sender':{'id':'3259980040'},'segments':[{'type':'text','text':'今天聊点别的'}]}
        history.observe(old);history.observe(current)
        job={'sender':'3259980040','payload':[{'payload':current}]}
        with patch.object(MediaService,'prepare_content',return_value=[{'type':'image','data':'fixture'}]) as prepare:
            history.pending_history(job);prepare.assert_not_called()
            current['reply_to']='old'
            history.pending_history(job);prepare.assert_called_once()

    def test_topic_is_shared_and_small_png_keeps_original_bytes(self):
        set_community(self.root,'1043230169','赛马娘游戏交流群')
        self.assertIn('赛马娘',community_prompt(self.root,'group:1043230169',has_image=True))
        image=self.root/'image.png';Image.new('RGB',(128,128),'red').save(image)
        uri=MediaService(self.root,'qq')._image_uri(image)
        self.assertTrue(uri.startswith('data:image/png;'))
        self.assertEqual(base64.b64decode(uri.split(',',1)[1]),image.read_bytes())

    def test_exact_image_uses_owner_correction_and_other_images_do_not(self):
        import json
        config=self.root/'channels/fish_qq/config.json';config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'allowlist':{'owner_id':'3259980040'}}),encoding='utf-8')
        with database(self.root/'data/qq/qq.db') as db:
            db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,direction TEXT,sender_id TEXT,chat_id TEXT,text TEXT,segments TEXT)')
            db.execute('INSERT INTO messages VALUES(1,?,?,?,?,?)',('inbound','3259980040','private:3259980040','图','[{"type":"image"}]'))
            db.execute('INSERT INTO messages VALUES(2,?,?,?,?,?)',('inbound','3259980040','private:3259980040','这是赛马娘的爱如往昔','[]'))
        image=self.root/'known.png';other=self.root/'other.png'
        Image.new('RGB',(128,128),'red').save(image);Image.new('RGB',(128,128),'blue').save(other)
        knowledge=ImageKnowledge(self.root);knowledge.confirm(image,1,2,'这是赛马娘的爱如往昔')
        self.assertIn('爱如往昔',knowledge.prompt(image));self.assertEqual(knowledge.prompt(other),'')


if __name__=='__main__':unittest.main()
