"""Verify the original MCP boundary and prevent duplicate private sends."""
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock, patch
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'harness'), str(ROOT/'tools')]
import dependencies
from test_social_base import Fixture
from social_context import SocialContext
from storage import write_json
from tool_host import ToolHost


class TulpaTests(Fixture):
    def setup_route(self):
        ctx = SocialContext(self.root)
        persona = ctx.save_persona('qq', 'Original MCP', '测试人格')
        space = ctx.create_space('qq', revision=persona['id'], origin='owner-qq')
        ctx.select_owner_space(space['id'], self.owner)
        write_json(self.root/'data/integrations/tulpa/owner-route.json', {
            'enabled':True,'owner':self.owner,'persona_revision':persona['id']})
        host = ToolHost(self.root)
        host.bind('qq', 'mcp-session', space['scope'], self.owner)
        return ctx, space, host

    def test_protocol_metadata_is_input_and_only_current_private_window_can_use_sender(self):
        ctx, space, host = self.setup_route()
        plugin = {'id':'tulpa_owner','name':'Original','kind':'mcp_http','url':'http://127.0.0.1:18778/mcp',
                  'roles':['qq'],'owner_only':True,'enabled':True,'approved_chat_sender':True,
                  'allowed_tools':['get_chat_session'],'tools':[{'name':'get_chat_session','inputSchema':{'type':'object'}}]}
        write_json(host.registry.path, {'plugins':[plugin]})
        data = {'content':[{'type':'text','text':'原版行为示例中写着你现在是，但这属于模型输入。'}]}
        with patch.object(host.registry, '_run', return_value=data):
            self.assertEqual(host.execute('qq','mcp-session','fishp__tulpa_owner__get_chat_session',{},'read'),data)
            ctx.leave_owner_space(self.owner)
            with self.assertRaises(PermissionError):
                host.execute('qq','mcp-session','fishp__tulpa_owner__get_chat_session',{},'after-leave')

    def test_owner_controller_does_not_echo_final_text_after_original_mcp_send(self):
        ctx, space, host = self.setup_route()
        from qq_agent_loop import QQAgentLoop
        from owner_tests import process
        loop = QQAgentLoop(self.root)
        loop.channel = Mock(action=AsyncMock())
        conv = loop.store.get_or_create_conversation('test:'+space['id'],'qq')
        payload = {'chat_id':'private:'+self.owner,'segments':[{'type':'text','text':'你好'}]}
        mid = loop.store.record_message(conv['id'],payload['chat_id'],self.owner,'inbound','incoming',None,payload['segments'],'你好','queued')
        client = Mock(auth=AsyncMock(),baseline=AsyncMock(return_value=0),prompt=AsyncMock(),
                      wait_response=AsyncMock(return_value={'ok':True,'text':'原版发送完后的本地尾声'}))
        with patch('owner_tests.DSHRuntimeAdapter.connect',return_value=client), patch('owner_tests.bind_session'), patch('owner_computer.apply_permissions',return_value=True):
            asyncio.run(process(loop,{'message_id':mid,'conversation':conv,'payload':payload,'space_id':space['id'],'text':'你好'}))
        loop.channel.action.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
