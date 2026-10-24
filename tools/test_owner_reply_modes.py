"""Owner-only thinking display, immutable modes, and default compatibility."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'harness'), str(ROOT/'tools')]
import dependencies
from test_social_base import Fixture
from social_context import SocialContext
from owner_reply import format_owner_reply, THINKING_EXCERPT_CHARS
from dsh_web import DSHWebClient


def event(seq, kind, blocks=None):
    data = {'message': {'content': blocks}} if blocks is not None else {'reason': {'kind': 'completed'}}
    return {'type': 'event', 'event': {'seq': seq, 'type': kind, 'data': data}}


class ReplyModesTests(Fixture):
    def test_default_digest_and_legacy_rows_remain_compatible(self):
        store = SocialContext(self.root)
        a = store.save_persona('qq','A','body',global_rules='global',workspace_rules='workspace')
        old = hashlib.sha256(json.dumps(['body','global','workspace'],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(a['digest'], old)
        self.assertEqual(a['owner_reply_mode'],'final')
        space=store.create_space('qq',revision=a['id'],origin='owner-qq')
        store.select_owner_space(space['id'],self.owner)
        b=store.save_persona('qq','B','body',parent=a['id'],owner_reply_mode='thinking_excerpt')
        self.assertNotEqual(a['digest'],b['digest'])
        self.assertEqual(store.space(space['id'])['revision'],a['id'])
        self.assertEqual(store.owner_space(self.owner)['id'],space['id'])
        self.assertFalse(b['approved'])
        self.assertFalse(store.deployment(self.group)['enabled'])
        self.assertEqual(store.save_persona('qq','C','edited',parent=b['id'])['owner_reply_mode'],'thinking_excerpt')
        with self.assertRaises(ValueError):store.save_persona('qq','bad','body',owner_reply_mode='arbitrary')

    def test_display_uses_bounded_actual_excerpt_and_explicit_absence(self):
        final='1. 保存\n2. 核对\n3. 重开'
        self.assertEqual(format_owner_reply(final,'private reasoning','final'),final)
        shown=format_owner_reply(final,'甲'*200,'thinking_excerpt')
        self.assertIn('甲'*THINKING_EXCERPT_CHARS+'…（已截断）',shown)
        self.assertTrue(shown.endswith('回复：'+final))
        self.assertIn('未返回可展示',format_owner_reply(final,'','thinking_excerpt'))
        with patch.dict(os.environ,{'FISH_TEST_API_TOKEN':'fixture-reasoning-secret'}):
            self.assertNotIn('fixture-reasoning-secret',format_owner_reply('ok','fixture-reasoning-secret','thinking_excerpt'))

    def test_final_reasoning_does_not_take_tool_planning_or_earlier_turns(self):
        records=[event(1,'assistant/message',[{'type':'reasoning','text':'old'},{'type':'text','text':'old final'}]),
                 event(2,'assistant/message',[{'type':'reasoning','text':'tool planning'},{'type':'tool-call','name':'read'}]),
                 event(3,'assistant/message',[{'type':'reasoning','text':'actual final thought'},{'type':'text','text':'final'}])]
        self.assertEqual(DSHWebClient._final_reasoning(records),'actual final thought')
        self.assertEqual(DSHWebClient._final_reasoning([event(4,'assistant/message',[{'type':'text','text':'no reasoning'}])]),'')

    def test_runtime_reasoning_is_opt_in_and_current_turn_only(self):
        async def run():
            client=DSHWebClient('http://127.0.0.1/')
            client._baseline_seq['s']=100
            records=[event(1,'assistant/message',[{'type':'reasoning','text':'old'},{'type':'text','text':'old final'}]),
                     event(150,'assistant/message',[{'type':'reasoning','text':'tool planning'},{'type':'tool-call'}]),
                     event(200,'assistant/message',[{'type':'reasoning','text':'actual'},{'type':'text','text':'final'}]),event(201,'turn/end')]
            client._cursor=AsyncMock(return_value=201);client._page=AsyncMock(return_value={'records':records})
            a=await client.wait_response('s',0,2)
            b=await client.wait_response('s',0,2,include_reasoning=True)
            self.assertEqual(a,{'ok':True,'text':'final'})
            self.assertEqual(b,{'ok':True,'text':'final','reasoning':'actual'})
        asyncio.run(run())

    def run_owner(self, mode, *, cancelled=False, blocked=False, chat_id=None):
        from qq_agent_loop import QQAgentLoop
        from owner_tests import process
        from model_gateway import ChatResult
        store=SocialContext(self.root);p=store.save_persona('qq','test','body',owner_reply_mode=mode)
        space=store.create_space('qq',revision=p['id'],origin='owner-qq');store.select_owner_space(space['id'],self.owner)
        loop=QQAgentLoop(self.root);loop.channel=Mock(action=AsyncMock(return_value={'ok':True,'payload':{'message_id':'out-'+p['id']}}))
        conv=loop.store.get_or_create_conversation('test:'+space['id'],'qq');payload={'chat_id':chat_id or 'private:'+self.owner,'segments':[{'type':'text','text':'hello'}]}
        mid=loop.store.record_message(conv['id'],payload['chat_id'],self.owner,'inbound','in-'+p['id'],None,payload['segments'],'hello','queued')
        def response(messages,**kwargs):
            if cancelled:loop.private_cancel.set()
            # 她的话由 fish_say 交出去，然后结束这一轮：正文本身不再当消息。
            if any(isinstance(m,dict) and m.get('role')=='tool' for m in messages):
                return ChatResult('', {}, 'stop')
            return ChatResult('', {}, 'tool_calls', ({'id':'call-1','type':'function',
                'function':{'name':'fish_say','arguments':json.dumps({'texts':['actual final']})}},))
        client=Mock(base='https://api.deepseek.com',model='test',chat=Mock(side_effect=response))
        if blocked:loop.governance.check_output=Mock(side_effect=PermissionError('blocked'))
        with patch('model_gateway.ModelGateway',return_value=client),patch('web_knowledge.WebKnowledge.resolve',return_value=''),patch('conversation_style.private_context',return_value=''),patch('memory_store.MemoryStore.context',return_value=''):
            asyncio.run(process(loop,{'message_id':mid,'conversation':conv,'payload':payload,'space_id':space['id'],'text':'hello'}))
        return loop,client

    def test_migrated_owner_uses_final_fish_api_for_each_legacy_display_mode(self):
        for mode in ('thinking_excerpt', 'final'):
            loop,client=self.run_owner(mode)
            # 一次生成：她说了一句（fish_say），然后同一轮结束；没有第二次再判。
            self.assertEqual(2, client.chat.call_count)
            self.assertEqual(loop.channel.action.await_args.args[1]['segments'][0]['text'],'actual final')
            self.assertIsNone(loop.dsh)

    def test_reasoning_never_delivers_after_cancel_or_output_block(self):
        for options in ({'cancelled':True},{'blocked':True}):
            loop,_=self.run_owner('thinking_excerpt',**options)
            loop.channel.action.assert_not_awaited()

    def test_reasoning_display_rejects_group_destination(self):
        with self.assertRaises(PermissionError):self.run_owner('thinking_excerpt',chat_id=self.chat)

    def test_editor_mode_drafts_and_saved_revisions_are_independent(self):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtWidgets import QApplication
        from fish_desktop.services import FishServices
        from fish_desktop.social import SocialPage
        app=QApplication.instance() or QApplication([])
        store=SocialContext(self.root)
        a=store.save_persona('qq','plain','body');b=store.save_persona('qq','thought','body',owner_reply_mode='thinking_excerpt')
        page=SocialPage(FishServices(self.root));page.refresh()
        page.versions.setCurrentIndex(page.versions.findData(a['id']))
        page.reply_mode.setCurrentIndex(page.reply_mode.findData('thinking_excerpt'))
        page.versions.setCurrentIndex(page.versions.findData(b['id']))
        page.versions.setCurrentIndex(page.versions.findData(a['id']))
        self.assertEqual(page.reply_mode.currentData(),'thinking_excerpt')
        self.assertTrue(page.dirty)
        page._save_persona()
        self.assertNotEqual(page.selected,a['id'])
        self.assertEqual(store.persona(page.selected)['owner_reply_mode'],'thinking_excerpt')
        self.assertEqual(store.persona(a['id'])['owner_reply_mode'],'final')
        page.close();app.processEvents()


if __name__=='__main__':unittest.main()
