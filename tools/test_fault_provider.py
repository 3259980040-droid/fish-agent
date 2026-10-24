"""H1 approved provider diagnostics: real gates, fake responses, no paid requests."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import group_decide
import group_prescreen
from qq_agent_loop import QQAgentLoop
from storage import database
from test_social_base import Fixture

class ProviderFaultTests(Fixture):
    def setUp(self):
        super().setUp()
        self.loop = QQAgentLoop(self.root)
        self.payload = {'chat_id': self.chat, 'segments': [{'type': 'text', 'text': '可以接话吗'}]}
        self.policy = SimpleNamespace(config=lambda chat: {
            'energy_enabled': False, 'target_replies_per_hour': 0,
            'prescreen_enabled': True, 'prescreen_timeout_s': 10})

    def judge(self, error):
        with patch('group_decide._last_sent', return_value=0), \
                patch('group_decide._judge_limit', return_value=10), \
                patch('group_decide._judge_material', return_value={'context': 'fake'}), \
                patch('group_decide._judge_spend', return_value=True), \
                patch('group_decide._judge', side_effect=error) as call:
            result = asyncio.run(self.loop._fish_verdict(self.chat, self.payload, '可以接话', 'active', None))
        self.assertEqual(call.call_count, 1, 'judge原先只有一次调用，本批不增加重试')
        self.assertFalse(result['speak'])
        self.assertEqual(result['reason'], 'judge-error:' + type(error).__name__)
        self.assertIn('error', result)
        return result

    def prescreen(self, error):
        with patch('group_prescreen.material', return_value={'context': 'fake'}), \
                patch('model_gateway.ModelGateway.__init__', return_value=None), \
                patch('model_gateway.ModelGateway.json', side_effect=error) as call:
            result = group_prescreen.score(self.root, self.chat, self.payload)
        self.assertEqual(call.call_count, group_prescreen.RETRIES + 1)
        self.assertEqual((result['score'], result['reason'], result['calls']),
                         (0, '判断失败，按 0 分处理', group_prescreen.RETRIES + 1))
        self.assertIn('error', result)
        with patch('group_prescreen.score', return_value=result):
            self.assertFalse(asyncio.run(self.loop._fish_prescreen(
                self.policy, self.chat, self.payload, 'active', None, Mock())))
        return result

    def test_401_403_are_internal_auth_and_never_store_or_display_provider_secrets(self):
        for status in (401, 403, 401):
            raw = RuntimeError(f'HTTP {status} https://private.example/api?token=secret-value Bearer sk-secret-1234567890 token=hidden-token')
            for run in (self.judge, self.prescreen):
                result = run(raw)
                self.assertNotIn('private.example', result['error'])
                self.assertNotIn('secret-value', result['error'])
                self.assertNotIn('sk-secret', result['error'])
                self.assertNotIn('hidden-token', result['error'])
        snapshot = self.loop.governance.fault_snapshot()
        self.assertEqual(len(snapshot['items']), 2)
        for row in snapshot['items']:
            self.assertEqual((row['code'], row['consecutive']), ('auth', 3))
            self.assertEqual(row['reason'], '认证失败，需要检查密钥')
            self.assertNotIn('detail', row, '内部诊断不原样投影给UI')
        with database(self.loop.governance.db) as db:
            rows = [dict(r) for r in db.execute('SELECT source,stage,code,detail FROM faults ORDER BY stage')]
            audits = [r['reason'] for r in db.execute('SELECT reason FROM audit')]
        for text in [str(rows), *audits]:
            for secret in ('private.example', 'secret-value', 'sk-secret', 'hidden-token'):
                self.assertNotIn(secret, text)
        print('H1 provider真跑：judge每轮1次；prescreen每轮%d次（沿用原RETRIES）；3轮同类聚合为judge/prescreen各1卡。' % (group_prescreen.RETRIES + 1))
        print('H1 sanitized_fault_rows:', rows)

    def test_generated_error_detail_cannot_leak_into_round_history_reason(self):
        import group_round
        raw = 'RuntimeError: HTTP 401 https://private.example/api?token=secret-value Bearer sk-secret-1234567890'
        with patch('group_round._generate', return_value={'error': raw}):
            result = asyncio.run(group_round._speak(self.loop, self.chat, {'payload': self.payload}))
        self.assertEqual(result['reason'], '认证失败，需要检查密钥')
        self.assertNotIn('private.example', str(result))
        self.assertNotIn('secret-value', str(result))

    def test_timeout_and_protocol_are_not_auth_or_normal_silence(self):
        self.judge(TimeoutError('provider timed out'))
        self.judge(ValueError('JSON 格式错误'))
        cards = self.loop.governance.fault_snapshot()['items']
        self.assertEqual({row['code'] for row in cards}, {'timeout', 'protocol'})
        self.assertTrue(all(row['reason'] != '认证失败，需要检查密钥' for row in cards))

if __name__ == '__main__':
    unittest.main()
