"""Focused action-to-delivery checks; no external provider or real QQ sends."""
import asyncio
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
        client = Mock(auth=AsyncMock(),create_session=AsyncMock(return_value='fixture-chat'),baseline=AsyncMock(return_value=0),
            prompt=AsyncMock(),wait_response=AsyncMock(return_value={'ok':True,'text':'不应重复发送的尾声'}))
        bubbles=[{'kind':'text','text':'坏了'}, {'kind':'text','text':'让你发现了'}, {'kind':'sticker','emoji_id':asset}]
        def request(root,route,*args,**kwargs):
            return {'bubbles':bubbles} if route=='/chat/take' else {'asset':''}
        with patch('owner_tests.DSHRuntimeAdapter.connect',return_value=client),patch('owner_tests.bind_session'),patch('owner_computer.apply_permissions',return_value=False),patch('tool_host.request',side_effect=request),patch('chat_output.bubble_delay',return_value=0):
            asyncio.run(process(loop,{'message_id':mid,'conversation':conv,'payload':payload,'space_id':space['id'],'text':'聊聊'}))
        sent=[call.args[1] for call in loop.channel.action.await_args_list]
        self.assertEqual([v['segments'] for v in sent],[[{'type':'text','text':'坏了'}],[{'type':'text','text':'让你发现了'}],[{'type':'image','asset_id':asset}]])
        self.assertEqual(len({v['idempotency_key'] for v in sent}),3)


if __name__ == '__main__':
    unittest.main()
