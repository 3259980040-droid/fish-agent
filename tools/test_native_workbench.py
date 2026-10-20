"""User flows through the native workspace's real local HTTP adapter."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
from test_social_base import Fixture
from governance import control_token
from social_context import SocialContext
from storage import read_json
from tool_host import ToolHost
import requests


class NativeWorkbenchTests(Fixture):
    def setUp(self):
        super().setUp()
        self.host = ToolHost(self.root)
        self.host.serve(background_workers=False, listen_port=0)
        self.origin = 'http://127.0.0.1:' + str(self.host.server.server_port)

    def tearDown(self):
        if hasattr(self.host, '_workbench'):
            self.host._workbench.close()
        self.host.server.shutdown()
        self.host.server.server_close()
        super().tearDown()

    def call(self, action, **params):
        response = requests.post(self.origin + '/workbench', json={'action': action, 'params': params},
                                 headers={'x-fish-token': control_token(self.root)}, timeout=10,
                                 proxies={'http': None, 'https': None, 'all': None})
        value = response.json()
        self.assertTrue(value.get('ok'), value)
        return value['result']

    def test_persona_memory_unlimited_and_same_local_conversation(self):
        self.assertEqual(self.call('status')['owner'], self.owner)
        persona = self.call('personas.save', name='窗口角色', body='自然说话，动作按需。')
        self.call('personas.apply', revision=persona['id'])
        self.assertEqual(self.call('status')['persona']['id'], persona['id'])
        self.call('memory.save', text='主人喜欢自然连续的回复', category='偏好')
        self.assertIn('自然连续', self.call('memory.search', query='回复')[0]['text'])
        self.assertTrue(self.call('settings.save', daily_limits=False)['daily_limits']['unlimited'])
        cid = self.call('chat.new')['id']
        before = self.call('chat.snapshot', id=cid)
        self.call('chat.mode', id=cid, mode='work', workspace=str(self.root))
        after = self.call('chat.snapshot', id=cid)
        self.assertEqual(after['settings']['mode'], 'work')
        self.assertEqual(before['space']['id'], after['space']['id'])
        self.call('chat.mode', id=cid, mode='chat')
        self.assertEqual(self.call('chat.snapshot', id=cid)['settings']['mode'], 'chat')

    def test_private_delivery_protocol_survives_work_mode(self):
        contexts = SocialContext(self.root)
        space = contexts.create_space('qq', origin='owner-qq')
        contexts.select_owner_space(space['id'], self.owner)
        with patch('tulpa_owner.matches', return_value=True):
            self.host.bind('qq', 'same-private-session', space['scope'], self.owner, chat_mode='work')
        identities = read_json(self.root / 'harness/runtime/qq.identities.json')
        identity = identities['same-private-session']
        self.assertEqual(identity['chat_mode'], 'tulpa')
        self.assertEqual(identity['owner_mode'], 'work')
        self.assertTrue(identity['owner_coding'])
        self.assertEqual(self.host.memory_scope(identity), 'owner')
        self.assertEqual(contexts.owner_space(self.owner)['session'], 'same-private-session')


if __name__ == '__main__':
    unittest.main()
