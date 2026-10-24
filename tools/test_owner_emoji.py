"""Owner Fish API -> existing emoji assets -> per-bubble history and channel receipts."""
import asyncio
import base64
import json
import unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
from PIL import Image
from test_owner_private import OwnerPrivateFixture
from model_gateway import ChatResult,ChatOptions
from emoji_store import EmojiStore
from chat_output import decode_reply,protocol_history
from desktop_context import DesktopContext
from multimodal import MediaService
from storage import database
import threading


def image_asset(root, *, gif=False, note='蓝色测试表情'):
    path=root/('fixture.gif' if gif else 'fixture.png')
    image=Image.new('RGB',(24,24),'blue')
    if gif:image.save(path,save_all=True,append_images=[Image.new('RGB',(24,24),'green')],duration=180,loop=0)
    else:image.save(path)
    return EmojiStore(root).add(path,note)['id'],path


def tool(name,arguments,call_id):
    return {'id':call_id,'type':'function','function':{'name':name,'arguments':json.dumps(arguments)}}


class OwnerEmojiTests(OwnerPrivateFixture):
    def test_existing_zero_tool_limit_remains_unlimited(self):
        from owner_private import process
        asset,_=image_asset(self.root)
        self.loop.governance.save({'max_tool_calls_per_turn':0})
        # 她要发的是一张表情：第八轮把表情交出去，第九轮结束（正文不发）。
        self.gateway.chat.side_effect=[ChatResult('',{},'tool_calls',(
            tool('fish_emoji_search',{'context':'蓝色'},'search-'+str(i)),)) for i in range(7)]+[
            ChatResult('',{},'tool_calls',(tool('fish_send_sticker',{'emoji_id':asset},'sticker'),)),
            ChatResult('',{},'stop')]
        asyncio.run(process(self.loop,self.item('发这张已收藏的蓝色表情')))
        self.assertEqual(self.gateway.chat.call_count,9)
        self.assertEqual(self.loop.channel.action.call_args.args[1]['segments'],[{'type':'image','asset_id':asset}])

    def test_unknown_asset_requires_actual_preview_and_owner_history_keeps_sticker(self):
        from owner_private import process
        asset,_=image_asset(self.root,note='')
        raw=json.dumps({'bubbles':[{'kind':'sticker','emoji_id':asset}]})
        with self.assertRaisesRegex(ValueError,'尚未预览'):decode_reply(self.root,raw)
        self.gateway.chat.side_effect=[
            ChatResult('',{},'tool_calls',(tool('fish_emoji_search',{'context':'蓝色'},'search'),),'PRIVATE_REASONING_CANARY'),
            ChatResult('',{},'tool_calls',(tool('fish_emoji_read',{'id':asset},'read'),)),
            ChatResult('',{},'tool_calls',(tool('fish_send_sticker',{'emoji_id':asset},'sticker'),)),
            ChatResult('',{},'stop')]
        self.loop.channel.action.return_value={'ok':True,'payload':{'message_id':'real-mock-image'}}
        item=self.item('发一个蓝色表情，不要文字代替')
        with patch('chat_output.bubble_delay',return_value=0):asyncio.run(process(self.loop,item))
        calls=self.gateway.chat.call_args_list
        # A 批把主人私聊目录扩成统一工具集：这里只要求表情工具在场，
        # 全局目录的精确断言见 tools/test_owner_chat_tools.py。
        self.assertLessEqual({'fish_emoji_search','fish_emoji_read'},{t['function']['name'] for t in calls[0].kwargs['tools']})
        final=calls[-1].args[0]
        self.assertTrue(any(isinstance(m.get('content'),list) and any(b.get('type')=='image_url' for b in m['content']) for m in final))
        self.assertIsNone(self.loop.dsh)
        self.assertEqual(self.loop.channel.action.call_args.args[1]['segments'],[{'type':'image','asset_id':asset}])
        context=DesktopContext(self.root,self.loop.store.db_path,channel='qq')
        history,_=context.history(item['conversation']['id'],10**9,MediaService(self.root,'qq'),self.gateway,threading.Event())
        # 历史里留下的是她真发出去的东西，不再是一条气泡 JSON 信封。
        self.assertEqual(protocol_history(history)[-1]['content'],'[表情:'+asset+']')
        with database(self.root/'data/social/request-inspector.db') as db:
            payload=''.join(r['payload'] for r in db.execute('SELECT payload FROM requests'))
        self.assertNotIn('PRIVATE_REASONING_CANARY',payload)
        self.assertNotIn('data:image/',payload)

    def test_text_success_image_failure_history_is_partial(self):
        from owner_private import process
        asset,_=image_asset(self.root)
        self.gateway.chat.side_effect=[
            ChatResult('',{},'tool_calls',(tool('fish_say',{'texts':['给你这个']},'text-1'),
                                           tool('fish_send_sticker',{'emoji_id':asset},'sticker-1'))),
            ChatResult('',{},'stop')]
        self.loop.channel.action.side_effect=[{'ok':True,'payload':{'message_id':'text-ok'}},
            {'ok':False,'error':{'code':'emoji_unavailable','may_have_sent':False}},
            {'ok':True,'payload':{'message_id':'notice'}}]
        item=self.item('发文字再发表情')
        with patch('chat_output.bubble_delay',return_value=0):asyncio.run(process(self.loop,item))
        with database(self.loop.store.db_path) as db:
            rows=[dict(r) for r in db.execute("SELECT status,channel_message_id,segments FROM messages WHERE direction='outbound' ORDER BY id")]
        self.assertEqual([r['status'] for r in rows],['sent','failed'])
        self.assertEqual(rows[0]['channel_message_id'],'text-ok')
        history,_=DesktopContext(self.root,self.loop.store.db_path,channel='qq').history(item['conversation']['id'],10**9,
            MediaService(self.root,'qq'),self.gateway,threading.Event())
        self.assertNotIn(asset,json.dumps(history))

    def test_reservation_and_send_results_are_distinct_and_gif_bytes_preserved(self):
        asset,path=image_asset(self.root,gif=True)
        library=EmojiStore(self.root);chat='private:'+self.owner
        reservation=library.reserve_send(asset,chat,request_id='first')
        self.assertEqual(base64.b64decode(reservation['file'].split('://',1)[1]),path.read_bytes())
        self.assertEqual(library.read(asset)['frames'],2)
        library.finish_send('first','failed',error='not sent')
        library.reserve_send(asset,chat,request_id='second')
        library.finish_send('second','sending');library.recover_sends()
        with database(library.path) as db:
            states={r['request_id']:r['state'] for r in db.execute('SELECT * FROM sends')}
        self.assertEqual(states,{'first':'failed','second':'unknown'})
        with self.assertRaises(PermissionError):library.reserve_send(asset,chat,request_id='second')
        other='private:7654321';library.reserve_send(asset,other,request_id='confirmed-before-crash')
        library.finish_send('confirmed-before-crash','sending')
        library.recover_sends({'confirmed-before-crash':{'chat_id':other,'message_id':'confirmed'}})
        with database(library.path) as db:
            self.assertEqual(db.execute("SELECT state FROM sends WHERE request_id='confirmed-before-crash'").fetchone()[0],'sent')


class EmojiChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from channels.fish_qq.tests.test_contract import ContractTests
        self.f=ContractTests();await self.f.asyncSetUp()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    async def test_actual_static_gif_channel_and_durable_duplicate(self):
        from channels.fish_qq.tests.test_contract import OWNER,GROUP
        from channels.fish_qq.spool import Outbox
        library=EmojiStore(self.f.tmp)
        for gif,chat in ((False,'private:'+OWNER),(True,'group:'+GROUP)):
            asset,path=image_asset(self.f.tmp,gif=gif)
            payload={'chat_id':chat,'segments':[{'type':'image','asset_id':asset}],'idempotency_key':'gif' if gif else 'static'}
            first=await self.f.action('send_message',payload,action_id='one'+str(gif))
            self.assertTrue(first['ok'],first)
            sent=next(a for a in reversed(self.f.fake.actions) if a['action'].startswith('send_'))
            self.assertEqual(base64.b64decode(sent['params']['message'][0]['data']['file'].split('://',1)[1]),path.read_bytes())
            count=len(self.f.fake.actions)
            self.f.app.idempotency.clear();self.f.app.outbox=Outbox(self.f.app.config.state_dir)
            again=await self.f.action('send_message',payload,action_id='two'+str(gif))
            self.assertEqual(again['payload']['message_id'],first['payload']['message_id'])
            self.assertEqual(len(self.f.fake.actions),count)
        with database(library.path) as db:rows=[dict(r) for r in db.execute('SELECT * FROM sends')]
        self.assertEqual([r['state'] for r in rows],['sent','sent'])
        self.assertTrue(all(json.loads(r['receipt'])['message_id'] for r in rows))

    async def test_known_failure_releases_usage_unknown_does_not_retry(self):
        from channels.fish_qq.tests.test_contract import OWNER
        asset,_=image_asset(self.f.tmp);payload={'chat_id':'private:'+OWNER,'segments':[{'type':'image','asset_id':asset}]}
        self.f.fake.fail_send=True
        first=await self.f.action('send_message',payload|{'idempotency_key':'failure'})
        self.assertFalse(first['ok'])
        self.f.fake.fail_send=False;self.f.fake.drop_send=True
        unknown=await self.f.action('send_message',payload|{'idempotency_key':'unknown'},action_id='timeout')
        self.assertTrue(unknown['error']['may_have_sent'])
        count=len(self.f.fake.actions)
        again=await self.f.action('send_message',payload|{'idempotency_key':'unknown'},action_id='repeat')
        self.assertFalse(again['ok']);self.assertEqual(len(self.f.fake.actions),count)
        with database(EmojiStore(self.f.tmp).path) as db:states=[r['state'] for r in db.execute('SELECT state FROM sends ORDER BY id')]
        self.assertEqual(states,['failed','unknown'])

    async def test_invalid_ids_never_confirm_normal_sends_or_repeat(self):
        from channels.fish_qq.tests.test_contract import OWNER
        asset,_=image_asset(self.f.tmp);library=EmojiStore(self.f.tmp)
        cfg=library.config()|{'cooldown_seconds':0,'same_meme_seconds':0}
        for index,value in enumerate((None,'',0,True,{},1.5)):
            with self.subTest(value=value),patch('harness.emoji_store.EmojiStore.config',return_value=cfg):
                action=AsyncMock(return_value={'data':{'message_id':value}})
                payload={'chat_id':'private:'+OWNER,'segments':[{'type':'image','asset_id':asset}],
                         'idempotency_key':'invalid-'+str(index)}
                with patch.object(self.f.app.onebot,'send_action',action):
                    result=await self.f.app._send_message(payload)
                    self.assertFalse(result['ok']);self.assertTrue(result['error']['may_have_sent'])
                    await self.f.app._send_message(payload)
                    self.assertEqual(action.await_count,1)
                row=self.f.app.outbox.find_request(payload['chat_id'],payload['idempotency_key'])
                self.assertEqual(row['status'],'unknown');self.assertNotIn('receipt',row)
        with database(library.path) as db:
            self.assertEqual({r['state'] for r in db.execute('SELECT state FROM sends')},{'unknown'})

    async def test_invalid_ids_never_confirm_recovered_queue(self):
        from channels.fish_qq.tests.test_contract import OWNER
        asset,_=image_asset(self.f.tmp);library=EmojiStore(self.f.tmp)
        cfg=library.config()|{'cooldown_seconds':0,'same_meme_seconds':0}
        for index,value in enumerate((None,'',0,[],False,'None')):
            with self.subTest(value=value),patch('harness.emoji_store.EmojiStore.config',return_value=cfg):
                payload={'chat_id':'private:'+OWNER,'segments':[{'type':'image','asset_id':asset}],
                         'idempotency_key':'recover-invalid-'+str(index)}
                record=self.f.app.outbox.add('send_message',payload)
                self.f.app.recovered_queued=[record]
                action=AsyncMock(return_value={'data':{'message_id':value}})
                with patch.object(self.f.app.onebot,'send_action',action):await self.f.app._retry_queued()
                self.assertEqual(self.f.app.outbox.get(record['id'])['status'],'unknown')
        with database(library.path) as db:
            self.assertEqual({r['state'] for r in db.execute('SELECT state FROM sends')},{'unknown'})


class ToolWireTests(unittest.TestCase):
    def test_receipt_id_contract_accepts_signed_integer_and_legacy_decimal_string(self):
        from channels.fish_qq.server import receipt_message_id
        for value in (123,-123,'456','-456'):
            self.assertEqual(receipt_message_id({'data':{'message_id':value}}),str(value))
        for result in ({},None,{'data':[]},{'data':{'message_id':None}}):
            self.assertIsNone(receipt_message_id(result))

    def test_fragmented_tool_stream_is_returned_for_fish_execution(self):
        import httpx
        from test_desktop_direct_chat import GatewayTests
        fixture=GatewayTests();fixture.setUp()
        try:
            requests=[]
            def response(request):
                requests.append(json.loads(request.content))
                events=[{'choices':[{'delta':{'reasoning_content':'PRIVATE_PROTOCOL_REASONING'}}]},
                    {'choices':[{'delta':{'tool_calls':[{'index':0,'id':'call-1','type':'function',
                        'function':{'name':'fish_emoji_read','arguments':'{"id":'}}]}}]},
                    {'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'arguments':'"asset"}'}}]},'finish_reason':'tool_calls'}]},
                    {'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':7}}]
                return httpx.Response(200,text=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n')
            fixture.transport(response)
            result=fixture.gateway.chat([{'role':'user','content':'预览'}],options=ChatOptions('high',1024,2),
                tools=[{'type':'function','function':{'name':'fish_emoji_read','parameters':{'type':'object'}}}])
            self.assertEqual(json.loads(result.tool_calls[0]['function']['arguments']),{'id':'asset'})
            self.assertEqual(result.reasoning_content,'PRIVATE_PROTOCOL_REASONING')
            self.assertNotIn('PRIVATE_PROTOCOL_REASONING',repr(result))
            self.assertIn('tools',requests[0])
        finally:fixture.doCleanups()


if __name__=='__main__':unittest.main()
