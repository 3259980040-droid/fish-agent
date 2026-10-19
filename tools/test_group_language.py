"""Local language retrieval, public search, cleanup and replay contracts."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
import dependencies
from group_language import GroupLanguage
from web_knowledge import WebKnowledge
from data_retention import DataRetention
from memory_store import MemoryStore
from group_tasks import GroupTaskStore
from storage import database
from storage import write_json
from governance import Governance
from qq_agent_loop import ChannelClient


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.chat='group:123456';self.owner='12345678'
        self.snapshot={'chat_id':self.chat,'messages':[{'message_id':'m1','sender':{'id':self.owner},'text':'记住，这里叫杏目，不要乱改名。'}]}
        self.item={'message_id':'m1','kind':'alias','trigger':'杏目','content':'本群沿用杏目这个叫法。'}

    def test_scoped_owner_correction_and_source_validation(self):
        s=GroupLanguage(self.root)
        self.assertEqual(s.learn(self.snapshot,[self.item],self.owner,'window-a'),1)
        self.assertEqual(len(s.select(self.chat,'window-a','你知道杏目吗')),1)
        self.assertEqual(s.select(self.chat,'window-b','杏目'),[])
        self.assertEqual(s.select('group:999999','window-a','杏目'),[])
        self.assertEqual(s.learn(self.snapshot,[{**self.item,'message_id':'invented'}],self.owner,'window-a'),0)
        self.assertEqual(s.learn(self.snapshot,[self.item],self.owner,'window-a'),0)

    def test_verified_owner_has_management_in_group_and_private(self):
        write_json(self.root/'channels/fish_qq/config.json',{'allowlist':{'owner_id':self.owner}})
        policy=Governance(self.root)
        self.assertIn('本轮来自主人账号',policy.identity_prompt(self.owner,private=False))
        self.assertIn('fish_owner_admin',policy.identity_prompt(self.owner,private=False))
        self.assertTrue(policy.permitted(self.owner,'memory.write',private=False))
        self.assertIn('普通聊天参与者',policy.identity_prompt('33333333',private=False))

    def test_peer_examples_are_candidates_and_cannot_overwrite_owner(self):
        s=GroupLanguage(self.root);s.learn(self.snapshot,[self.item],self.owner,'a')
        msg={'message_id':'m2','sender':{'id':'33333333'},'text':'随便改名吧'}
        s.learn({'chat_id':self.chat,'messages':[msg]},[{**self.item,'message_id':'m2','content':'错误新称呼'}],self.owner,'a')
        self.assertEqual(s.select(self.chat,'a','杏目')[0]['content'],self.item['content'])
        s.learn({'chat_id':self.chat,'messages':[msg]},[{**self.item,'message_id':'m2','trigger':'新梗'}],self.owner,'a')
        self.assertEqual(s.select(self.chat,'a','新梗')[0]['status'],'candidate')

    def test_approved_candidate_is_retrieved_and_disabled_is_not_relearned(self):
        s=GroupLanguage(self.root);snap={**self.snapshot,'messages':[{**self.snapshot['messages'][0],'sender':{'id':'33333333'}}]}
        s.learn(snap,[self.item],self.owner,'a');entry=s.items(self.chat,'a')[0]
        s.manage(self.chat,'a',entry['id'][:8],'active','owner-approved')
        self.assertEqual(len(s.select(self.chat,'a','杏目')),1)
        with self.assertRaises(ValueError):s.manage(self.chat,'b',entry['id'][:8],'disabled','wrong-window')
        s.manage(self.chat,'a',entry['id'][:8],'disabled','owner-deleted')
        self.assertEqual(s.learn(self.snapshot,[self.item],self.owner,'a'),0)
        self.assertEqual(s.select(self.chat,'a','杏目'),[])

    def test_search_query_and_cache_have_no_private_profile_dependency(self):
        service=WebKnowledge(self.root)
        self.assertEqual(service.query_for('哈喽'), '')
        self.assertEqual(service.query_for('查一下 QQ 12345678'), '')
        response=SimpleNamespace(ok=True,json=lambda:{'results':[{'title':'官方说明','url':'https://example.org','summary':'返回内容'}]})
        with (patch('web_knowledge.ModelGateway',return_value=SimpleNamespace(key='test',base='https://api.deepseek.com')),
              patch('web_knowledge.requests.post',return_value=response) as post):
            first=service.resolve(self.chat,'上网搜一下官方说明')
            second=service.resolve(self.chat,'上网搜一下官方说明')
        self.assertEqual(post.call_count,1);self.assertEqual(first,second)
        self.assertIn('已代本会话执行',first);self.assertIn('https://example.org',first)

    def test_search_failure_does_not_claim_success(self):
        service=WebKnowledge(self.root)
        with (patch('web_knowledge.ModelGateway',return_value=SimpleNamespace(key='test',base='https://api.deepseek.com')),
              patch('web_knowledge.requests.post',return_value=SimpleNamespace(ok=False,status_code=403))):
            result=service.resolve(self.chat,'上网搜一下资料')
        self.assertIn('查询失败：HTTP 403',result)
        self.assertNotIn('已代本会话执行',result)

    def test_cleanup_preserves_memory_and_running_jobs_and_backup_restores(self):
        store=MemoryStore(self.root,'qq');saved=store.save('验收偏好：蓝色',scope='owner')['id']
        GroupTaskStore(self.root);WebKnowledge(self.root)
        now=time.time()
        with database(self.root/'data/social/group-replies.db') as db:
            for state in ('sent','generating'):
                db.execute('INSERT INTO jobs(id,state,snapshot,updated) VALUES(?,?,?,?)',(state,state,'{"keep":true}',now-9*86400))
        with database(self.root/'data/qq/web-cache.db') as db:
            db.execute('INSERT INTO searches VALUES(?,?,?,?,?,?)',('old',self.chat,'q','[]',0,now-1))
        result=DataRetention(self.root).run(now)
        self.assertEqual(store.search(scope='owner')[0]['id'],saved)
        with database(self.root/'data/social/group-replies.db') as db:
            self.assertEqual(db.execute("SELECT snapshot FROM jobs WHERE id='sent'").fetchone()[0],'{}')
            self.assertNotEqual(db.execute("SELECT snapshot FROM jobs WHERE id='generating'").fetchone()[0],'{}')
        manifest=json.loads((Path(result['backup'])/'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(DataRetention.verify(result['backup']),manifest)
        entry=manifest['files']['data/qq/memory.db']
        with closing(sqlite3.connect(Path(result['backup'])/entry['file'])) as db:
            self.assertEqual(db.execute('SELECT id FROM memories').fetchone()[0],saved)
        self.assertTrue(DataRetention(self.root).run(now+10)['skipped'])

    def test_checkpoint_tracks_processed_frame_not_latest_received(self):
        checkpoint=self.root/'cursor.json'
        async def run():
            c=ChannelClient('ws://unused','unused',lambda frame:asyncio.sleep(0),checkpoint=checkpoint)
            c.last_seq=100
            task=asyncio.create_task(c._dispatch_events())
            await c._events.put({'epoch':'e1','seq':7})
            await c._events.join();task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.assertEqual(json.loads(checkpoint.read_text())['seq'],7)
            restored=ChannelClient('ws://unused','unused',None,checkpoint=checkpoint)
            self.assertEqual((restored.epoch,restored.last_seq),('e1',7))
        asyncio.run(run())


if __name__=='__main__':unittest.main(verbosity=2)
