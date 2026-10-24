"""Focused action-to-delivery checks; no external provider or real QQ sends."""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'harness'), str(ROOT/'tools')]
import dependencies
from test_social_base import Fixture
from social_context import SocialContext
from tool_host import ToolHost
from emoji_store import EmojiStore
from PIL import Image
from model_gateway import ChatResult
from storage import database


class ChatActions(Fixture):
    def setup_chat(self):
        contexts = SocialContext(self.root)
        revision = contexts.save_persona('qq', 'chat', 'chat')
        space = contexts.create_space('qq', revision=revision['id'], origin='owner-qq')
        contexts.select_owner_space(space['id'], self.owner)
        host = ToolHost(self.root)
        host.bind('qq', 'fixture-chat', space['scope'], self.owner)
        image = self.root/'fixture.png'
        Image.new('RGB', (24, 24), 'blue').save(image)
        asset = EmojiStore(self.root).add(image, '蓝色表情')['id']
        return host, space, asset

    def test_real_store_tools_see_image_prepare_full_queue_and_take_once(self):
        host, space, asset = self.setup_chat()
        found = host.execute('qq', 'fixture-chat', 'fish_emoji_search', {'context':'蓝色'}, 'search')
        self.assertEqual(found[0]['id'], asset)
        viewed = host.execute('qq', 'fixture-chat', 'fish_emoji_read', {'id':asset}, 'read')
        self.assertTrue(Path(viewed['content'][0]['path']).is_file())
        bubbles = [{'kind':'text','text':'坏了'}, {'kind':'text','text':'让你发现了'}, {'kind':'sticker','emoji_id':asset}]
        host.execute('qq','fixture-chat','fish_chat_reply',{'bubbles':bubbles},'plan')
        self.assertEqual(host.dispatch('/chat/take',{'session':'fixture-chat'},'qq',True)['bubbles'],bubbles)
        self.assertEqual(host.dispatch('/chat/take',{'session':'fixture-chat'},'qq',True)['bubbles'],[])

    def test_queue_reaches_existing_transport_in_order_with_each_receipt(self):
        host, space, asset = self.setup_chat()
        from qq_agent_loop import QQAgentLoop
        from owner_tests import process
        loop = QQAgentLoop(self.root)
        loop.channel = Mock(action=AsyncMock(side_effect=[{'ok':True,'payload':{'message_id':'sent-'+str(i)}} for i in range(3)]))
        conv = loop.store.get_or_create_conversation('test:'+space['id'],'qq')
        payload = {'chat_id':'private:'+self.owner, 'segments':[{'type':'text','text':'聊聊'}]}
        mid = loop.store.record_message(conv['id'],payload['chat_id'],self.owner,'inbound','incoming',None,payload['segments'],'聊聊','queued')
        from test_owner_private import said, stopped, spoken
        # 她要说的两句是两次"说"的调用，通道该看到两条；模型自己写的那点正文不发。
        client = Mock(base='https://api.deepseek.com', model='test',
            chat=Mock(side_effect=[said('坏了','让你发现了'), stopped()]))
        with patch('model_gateway.ModelGateway',return_value=client),patch('web_knowledge.WebKnowledge.resolve',return_value=''),patch('conversation_style.private_context',return_value=''),patch('chat_output.bubble_delay',return_value=0):
            asyncio.run(process(loop,{'message_id':mid,'conversation':conv,'payload':payload,'space_id':space['id'],'text':'聊聊'}))
        sent=[call.args[1] for call in loop.channel.action.await_args_list]
        self.assertEqual([v['segments'] for v in sent],[[{'type':'text','text':'坏了'}],[{'type':'text','text':'让你发现了'}]])
        self.assertEqual(len({v['idempotency_key'] for v in sent}),2)
        self.assertEqual(client.chat.call_args.kwargs['options'].effort,'high')
        with database(loop.store.db_path) as db:
            rows=[dict(r) for r in db.execute("SELECT text,status,channel_message_id FROM messages WHERE direction='outbound' ORDER BY id")]
        self.assertEqual([r['status'] for r in rows],['sent','sent'])
        self.assertEqual([r['channel_message_id'] for r in rows],['sent-0','sent-1'])
        self.assertIsNone(loop.dsh)
        seen=[]
        def answer(messages,**kwargs):
            seen.append([dict(message) for message in messages])  # 这一轮真实发出去的样子
            return stopped() if spoken(messages) else said('接上刚才的话。')
        client.chat.reset_mock()
        client.chat.side_effect=answer
        next_mid=loop.store.record_message(conv['id'],payload['chat_id'],self.owner,'inbound','next',None,
            [{'type':'text','text':'继续'}],'继续','queued')
        with patch('model_gateway.ModelGateway',return_value=client),patch('web_knowledge.WebKnowledge.resolve',return_value=''),patch('conversation_style.private_context',return_value=''):
            asyncio.run(process(loop,{'message_id':next_mid,'conversation':conv,'payload':payload | {'segments':[{'type':'text','text':'继续'}]},'space_id':space['id'],'text':'继续'}))
        # 这一轮第一次请求，就是她还没调用发送工具时的输入。
        messages=seen[0]
        self.assertEqual([m['role'] for m in messages],['system','user','assistant','user'])
        # 历史里的她只留下说出口的那两句，不再是一条气泡 JSON 信封。
        self.assertEqual(messages[2]['content'],'坏了\n\n让你发现了')

    def test_later_bubble_stops_after_window_switch_or_pause(self):
        from test_owner_private import OwnerPrivateFixture, said, stopped, spoken
        from owner_private import process
        # Each case uses independent real stores and the existing send method.
        for action in ('switch', 'pause'):
            with self.subTest(action=action):
                fixture=OwnerPrivateFixture();fixture.setUp()
                try:
                    contexts=SocialContext(fixture.root)
                    a=contexts.create_space('qq',origin='owner-qq');b=contexts.create_space('qq',origin='owner-qq')
                    contexts.select_owner_space(a['id'],fixture.owner)
                    item=fixture.item('接着说',space=a)
                    def answer(messages,**kwargs):
                        return stopped() if spoken(messages) else said('先说这一句。','这句不该发送。')
                    fixture.gateway.chat.side_effect=answer
                    async def first(*args,**kwargs):
                        if action=='switch':contexts.select_owner_space(b['id'],fixture.owner)
                        else:fixture.loop.governance.save({'paused':True})
                        return {'ok':True,'payload':{'message_id':'first'}}
                    fixture.loop.channel.action.side_effect=first
                    asyncio.run(process(fixture.loop,item))
                    self.assertEqual(fixture.loop.channel.action.call_count,1)
                    with database(fixture.loop.store.db_path) as db:
                        rows=[dict(r) for r in db.execute("SELECT text,status FROM messages WHERE direction='outbound' ORDER BY id")]
                    self.assertEqual([r['status'] for r in rows],['sent','cancelled'])
                    import threading
                    from desktop_context import DesktopContext
                    from multimodal import MediaService
                    history,_=DesktopContext(fixture.root,fixture.loop.store.db_path,channel='qq').history(
                        item['conversation']['id'],10**9,MediaService(fixture.root,'qq'),fixture.gateway,threading.Event())
                    self.assertEqual([m['role'] for m in history],['user','assistant'])
                    self.assertEqual(history[-1]['content'],'先说这一句。')
                finally:
                    fixture.tearDown();fixture.doCleanups()

    def test_unknown_second_receipt_does_not_resend_first_bubble(self):
        from qq_agent_loop import QQAgentLoop
        from chat_output import delivery_plan
        loop=QQAgentLoop(self.root)
        loop.channel=Mock(action=AsyncMock(side_effect=[{'ok':True,'payload':{'message_id':'first'}},
            TimeoutError('receipt lost'),{'ok':True,'payload':{'message_id':'notice'}}]))
        plan=[{'kind':'text','text':'第一条'}, {'kind':'text','text':'第二条'}]
        with patch('chat_output.bubble_delay',return_value=0):
            result=asyncio.run(loop._deliver_reply('private:'+self.owner,[],'two',bubble_plan=delivery_plan(plan)))
        self.assertFalse(result['ok']);self.assertTrue(result['error']['may_have_sent'])
        self.assertEqual([r['state'] for r in result['payload']['bubbles']],['sent','uncertain'])
        self.assertEqual(loop.channel.action.call_count,3)
        self.assertEqual(sum(c.args[1]['segments']==[{'type':'text','text':'第一条'}] for c in loop.channel.action.call_args_list),1)

    def test_entire_semantic_reply_is_checked_before_first_bubble(self):
        from qq_agent_loop import QQAgentLoop
        from chat_output import delivery_plan,decode_reply
        loop=QQAgentLoop(self.root);loop.channel=Mock(action=AsyncMock())
        loop.governance.save({'blocked_text':['ONLY_BLOCK_SECOND']})
        with patch('chat_output.bubble_delay',return_value=0):
            plan=delivery_plan([{'kind':'text','text':'第一条正常'}, {'kind':'text','text':'ONLY_BLOCK_SECOND'}])
        with self.assertRaises(PermissionError):asyncio.run(loop._deliver_reply('private:'+self.owner,[],'blocked',bubble_plan=plan))
        loop.channel.action.assert_not_called()
        for raw in ('{"bubbles":[', '{"bubbles":[{"kind":"sticker","emoji_id":"fake"}]}'):
            with self.assertRaises(ValueError):decode_reply(self.root,raw)

    def test_transport_failure_after_window_change_suppresses_old_notice(self):
        from qq_agent_loop import QQAgentLoop
        loop=QQAgentLoop(self.root)
        current=True
        async def failed(*args,**kwargs):
            nonlocal current
            current=False
            raise TimeoutError('receipt lost after switch')
        loop.channel=Mock(action=AsyncMock(side_effect=failed))
        result=asyncio.run(loop._deliver_reply('private:'+self.owner,
            [{'type':'text','text':'旧窗口消息'}],'old',validate=lambda:current))
        self.assertFalse(result['ok'])
        loop.channel.action.assert_awaited_once()

    def test_bubble_count_contract_and_validation_share_one_limit(self):
        from chat_output import reply_contract,validate_bubbles
        from group_reply_style import expression_policy
        with patch('chat_output.MAX_BUBBLES',2):
            self.assertIn('1至2项',reply_contract()['prompt'])
            self.assertEqual(len(validate_bubbles(self.root,[{'kind':'text','text':'完整消息'}]*2,check_content=False)),2)
            with self.assertRaisesRegex(ValueError,'1至2个'):
                validate_bubbles(self.root,[{'kind':'text','text':'完整消息'}]*3,check_content=False)
        self.assertNotIn('两三条',expression_policy(semantic_bubbles=True)['prompt'])

    def test_plain_text_and_complete_json_fence_need_no_second_model_call(self):
        from chat_output import decode_reply
        plain='下午好呀，有什么新鲜事想跟我说嘛。'
        self.assertEqual(decode_reply(self.root,plain),[{'kind':'text','text':plain}])
        bubbles=[{'kind':'text','text':'第一句'}, {'kind':'text','text':'补一句'}]
        self.assertEqual(decode_reply(self.root,'```json\n'+json.dumps({'bubbles':bubbles})+'\n```'),bubbles)
        for malformed in ('{"bubbles":[', '好的，下面是回复：{"bubbles":[]}', '```json\n{"bubbles":[]}',
                          '```json\nnot-json\n```', '{"answer":"错误结构"}', '{"bubbles":[]}'):
            with self.subTest(raw=malformed),self.assertRaises(ValueError):decode_reply(self.root,malformed)

    def test_third_unattempted_bubble_is_failed_after_second_send_error(self):
        from test_owner_private import OwnerPrivateFixture, said, stopped, spoken
        from owner_private import process
        for uncertain in (False,True):
            with self.subTest(uncertain=uncertain):
                fixture=OwnerPrivateFixture();fixture.setUp()
                try:
                    def answer(messages,**kwargs):
                        return stopped() if spoken(messages) else said('第一条','第二条','第三条')
                    fixture.gateway.chat.side_effect=answer
                    error=TimeoutError('receipt lost') if uncertain else {'ok':False,'error':{'code':'network_failed'}}
                    fixture.loop.channel.action.side_effect=[{'ok':True,'payload':{'message_id':'first'}},error,
                        {'ok':True,'payload':{'message_id':'notice'}}]
                    with patch('chat_output.bubble_delay',return_value=0):
                        asyncio.run(process(fixture.loop,fixture.item('分别说三条')))
                    with database(fixture.loop.store.db_path) as db:
                        rows=[dict(r) for r in db.execute("SELECT text,status,error,cancelled,channel_message_id FROM messages WHERE direction='outbound' ORDER BY id")]
                    self.assertEqual([r['status'] for r in rows],['sent','uncertain' if uncertain else 'failed','failed'])
                    self.assertEqual(rows[0]['channel_message_id'],'first')
                    self.assertFalse(rows[2]['cancelled'])
                    self.assertIn('本条未尝试发送',rows[2]['error'])
                    self.assertFalse(any(c.args[1]['segments']==[{'type':'text','text':'第三条'}]
                                         for c in fixture.loop.channel.action.await_args_list))
                finally:
                    fixture.tearDown();fixture.doCleanups()

    def test_history_folds_only_same_nonempty_turn_and_keeps_task_notice_separate(self):
        import threading
        from qq_store import QQStore
        from desktop_context import DesktopContext
        from multimodal import MediaService
        store=QQStore(self.root)
        conv=store.get_or_create_conversation('history-bubbles','qq')['id']
        for text,turn in [('第一句','chat-one'),('补一句','chat-one'),('独立任务完成通知',''),
                          ('另一轮回复','chat-two'),('另一条独立通知','')]:
            store.record_message(conv,'private:'+self.owner,'bot','outbound','',None,[],text,'sent',runtime_turn_id=turn)
        history,_=DesktopContext(self.root,store.db_path,channel='qq').history(
            conv,10**9,MediaService(self.root,'qq'),Mock(),threading.Event())
        self.assertEqual([r['content'] for r in history],['第一句\n\n补一句','独立任务完成通知','另一轮回复','另一条独立通知'])


if __name__ == '__main__':
    unittest.main()
