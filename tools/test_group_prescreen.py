"""先判后生成：假模型、临时根，一条消息都不发。

这里验的是四件事：
1 预筛通过 -> 才生成、才发；
2 预筛不过 -> 不生成、不发，audit 里留分数和原因；
3 判断模型吐不出合法 JSON -> 重试到上限，然后按 0 分处理（fail closed）；
4 精力与每小时目标：她自己的出站台账就是那扇滑动窗口，精力不够时只回 @。
"""
import asyncio
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
import model_gateway
import group_decide
import group_prescreen
from governance import Governance
from group_policy import GroupPolicy
from group_tests import GroupTests
from social_context import SocialContext
from storage import database
from qq_agent_loop import QQAgentLoop
from test_social_base import Fixture


class FakeChannel:
    """唯一的传输层：只记录，从不发送。"""

    def __init__(self):
        self.bot = {'id': '20001'}
        self.sent = []

    async def action(self, name, args, timeout=0):
        self.sent.append({'name': name, **args})
        return {'ok': True, 'payload': {'message_id': 'out-' + str(len(self.sent))}}


def good(content=9, will=9, social=8, timing=8, coherence=9, reason='有话头'):
    return {'content': content, 'will': will, 'social': social, 'timing': timing,
            'coherence': coherence, 'reason': reason}


class PrescreenTests(Fixture):
    def setUp(self):
        super().setUp()
        contexts = SocialContext(self.root)
        revision = contexts.save_persona('qq', '主人私聊人格', '主人私聊里的 Fish')['id']
        contexts.set_default('qq', revision)
        GroupTests(self.root).create(self.group, revision, self.owner)
        self.policy = GroupPolicy(self.root)
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True,
                                          'debounce_s': 0, 'read': True})
        self.loop = QQAgentLoop(self.root)
        self.loop.channel = FakeChannel()
        # 没有真连接、没有真模型：任何一条走漏的调用都会当场炸掉这条测试。
        for target in (patch('qq_agent_loop.connect', side_effect=AssertionError('真实连接')),
                       patch.object(model_gateway.ModelGateway, 'chat',
                                    side_effect=AssertionError('真实模型调用'))):
            target.start()
            self.addCleanup(target.stop)
        observe = patch('group_dialogue.GroupDialogue.observe', new=AsyncMock())
        self.observed = observe.start()
        self.addCleanup(observe.stop)
        # 判断层是别人的模块，这一层只验门槛：它永远说"可以说"。
        judge = patch.object(group_decide, 'worth_speaking',
                             Mock(return_value={'speak': True, 'reason': '值得说', 'judge_calls': 1}))
        judge.start()
        self.addCleanup(judge.stop)

    def payload(self, text='这句怎么回', mid=None, *, mentions=False):
        return {'chat_type': 'group', 'chat_id': self.chat,
                'message_id': mid or 'm-' + uuid.uuid4().hex[:8],
                'sender': {'id': '333333333', 'display_name': '群友甲'},
                'reply_to': '', 'mentions_bot': mentions, 'reply_to_bot': False,
                'segments': [{'type': 'text', 'text': text}]}

    def settle(self):
        async def drain():
            while self.loop.group_tasks:
                await asyncio.gather(*list(self.loop.group_tasks), return_exceptions=True)
        return drain()

    def drive(self, coro):
        return asyncio.run(coro)

    def reservations(self):
        with database(self.policy.path) as db:
            return [dict(row) for row in
                    db.execute('SELECT cursor,kind,state FROM reservations ORDER BY cursor')]

    def model(self, *replies):
        """假模型：按顺序吐这几次的 JSON，吐完就一直重复最后一个。"""
        queue = list(replies)

        def fake(self_gateway, system, user, **kwargs):
            assert 'content' in system and 'JSON' in system  # 规则确实在 system 里
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return fake

    # 1
    def test_score_passes_then_generation_happens_once(self):
        audit = Mock()
        events = []

        def generate(*args, **kwargs):
            events.append('generate')
            return {'engine': 'fish', 'text': '这句我接', 'tools': []}

        async def go():
            with patch.object(model_gateway.ModelGateway, 'json', self.model(good())), \
                    patch.object(group_prescreen, 'score', wraps=group_prescreen.score), \
                    patch.object(group_decide, 'fish_candidate', Mock(side_effect=generate)), \
                    patch.object(Governance, 'audit', audit):
                await self.loop._on_group(self.payload(mid='p-1'))
                await self.settle()
        self.drive(go())
        self.assertEqual(events, ['generate'])
        self.assertEqual([s['reply_to'] for s in self.loop.channel.sent], ['p-1'])
        self.assertEqual([(r['kind'], r['state']) for r in self.reservations()], [('active', 'sent')])
        # 一分精力换一句：发出去了才扣。
        self.assertAlmostEqual(self.policy.energy(self.chat), 0.9, delta=0.001)
        self.assertEqual([call.args[0] for call in audit.call_args_list], [])

    # 2
    def test_a_low_score_generates_nothing_and_sends_nothing(self):
        audit = Mock()
        generate = Mock()

        async def go():
            with patch.object(model_gateway.ModelGateway, 'json',
                              self.model(good(content=2, will=3, social=1, timing=2, coherence=2,
                                              reason='别人在聊具体事'))), \
                    patch.object(group_decide, 'fish_candidate', generate), \
                    patch.object(Governance, 'audit', audit):
                await self.loop._on_group(self.payload(mid='p-low'))
                await self.settle()
        self.drive(go())
        self.assertEqual(generate.call_count, 0)
        self.assertEqual(self.loop.channel.sent, [])
        kinds = [call.args[0] for call in audit.call_args_list]
        self.assertEqual(kinds, ['group_prescreen_skip'])
        reason = audit.call_args_list[0].args[1]
        self.assertIn('0.20', reason)          # 加权后的分数在账上
        self.assertIn('别人在聊具体事', reason)   # 原因也在账上
        self.assertIn('content=2', reason)      # 五维明细一并留档
        # 没开口就不占冷却：预留记成 skipped，下一次还能正常判断
        self.assertEqual([(r['kind'], r['state']) for r in self.reservations()], [('active', 'skipped')])
        self.assertAlmostEqual(self.policy.energy(self.chat), 1.0, delta=0.001)

    # 3
    def test_invalid_json_is_retried_then_scored_zero(self):
        audit = Mock()
        generate = Mock()
        calls = []

        def broken(self_gateway, system, user, **kwargs):
            calls.append(user)
            raise RuntimeError('模型未返回有效的 JSON 对象，未保存推测结果')

        async def go():
            with patch.object(model_gateway.ModelGateway, 'json', broken), \
                    patch.object(group_decide, 'fish_candidate', generate), \
                    patch.object(Governance, 'audit', audit):
                await self.loop._on_group(self.payload(mid='p-bad'))
                await self.settle()
        self.drive(go())
        self.assertEqual(len(calls), group_prescreen.RETRIES + 1)  # 首次 + 两次重试
        self.assertEqual(generate.call_count, 0)
        self.assertEqual(self.loop.channel.sent, [])
        kinds = [call.args[0] for call in audit.call_args_list]
        self.assertIn('group_prescreen_skip', kinds)
        self.assertIn('group_prescreen_failed', kinds)

    # 3'
    def test_score_itself_reports_the_calls_and_fails_closed(self):
        raw = group_prescreen.score.__doc__
        self.assertIn('calls', raw)
        body = {'message_id': 'p-direct', 'sender': {'display_name': '群友甲'},
                'segments': [{'type': 'text', 'text': '这句在说什么'}]}
        with patch.object(model_gateway.ModelGateway, 'json', self.model(good())):
            verdict = group_prescreen.score(self.root, self.chat, body)
        self.assertEqual(verdict['calls'], 1)
        self.assertGreaterEqual(verdict['score'], 0.85)
        self.assertEqual(sorted(verdict['dims']), ['coherence', 'content', 'social', 'timing', 'will'])
        with patch.object(model_gateway.ModelGateway, 'json',
                          self.model({'content': 5}, good())):
            second = group_prescreen.score(self.root, self.chat, body)
        self.assertEqual(second['calls'], 2)  # 缺维度算一次失败，再问一次就成立

    # 4
    def test_energy_falls_with_each_active_reply_and_recovers(self):
        self.policy.configure(self.chat, {'prescreen_enabled': False, 'energy_cost': 0.1,
                                          'energy_recover_per_min': 0.004, 'active_cooldown_s': 0,
                                          'reply_limits': False})
        audit = Mock()
        generate = Mock(return_value={'engine': 'fish', 'text': '好', 'tools': []})

        async def go():
            with patch.object(group_decide, 'fish_candidate', generate), \
                    patch.object(Governance, 'audit', audit):
                for index in range(3):
                    await self.loop._on_group(self.payload('第%d句' % index, mid='e-%d' % index))
                    await self.settle()
        self.drive(go())
        self.assertEqual(len(self.loop.channel.sent), 3)
        self.assertAlmostEqual(self.policy.energy(self.chat), 0.7, delta=0.005)
        # 十分钟后按 0.004/分钟回血：0.7 + 0.04
        later = time.time() + 600
        self.assertAlmostEqual(self.policy.energy(self.chat, later), 0.74, delta=0.005)
        # 回满封顶 1.0
        self.assertAlmostEqual(self.policy.energy(self.chat, time.time() + 86400), 1.0, delta=0.001)

    # 4'
    def test_exhausted_energy_blocks_active_but_not_a_mention(self):
        self.policy.configure(self.chat, {'prescreen_enabled': False, 'energy_cost': 0.1})
        self.policy.spend_energy(self.chat, 1.0)
        audit = Mock()
        generate = Mock(return_value={'engine': 'fish', 'text': '被叫到就说', 'tools': []})

        async def go():
            with patch.object(group_decide, 'fish_candidate', generate), \
                    patch.object(Governance, 'audit', audit):
                await self.loop._on_group(self.payload('随便一句', mid='n-1'))
                await self.settle()
                await self.loop._on_group(self.payload('@你一下', mid='n-2', mentions=True))
                await self.settle()
        self.drive(go())
        self.assertEqual([s['reply_to'] for s in self.loop.channel.sent], ['n-2'])
        self.assertIn('group_energy_skip', [call.args[0] for call in audit.call_args_list])

    # 5
    def test_the_hourly_target_reads_her_own_outbound_and_skips_the_prescreen(self):
        self.policy.configure(self.chat, {'target_replies_per_hour': 1})
        conversation = self.loop.store.get_or_create_conversation(self.chat, 'qq')
        self.loop.store.record_message(conversation['id'], self.chat, 'bot', 'outbound',
                                       'out-1', None, [{'type': 'text', 'text': '刚说过一句'}],
                                       '刚说过一句', 'sent')
        audit = Mock()
        generate = Mock()
        score_calls = []

        async def go():
            with patch.object(model_gateway.ModelGateway, 'json',
                              side_effect=lambda *a, **k: score_calls.append(1) or good()), \
                    patch.object(group_decide, 'fish_candidate', generate), \
                    patch.object(Governance, 'audit', audit):
                await self.loop._on_group(self.payload(mid='w-1'))
                await self.settle()
        self.drive(go())
        self.assertEqual(score_calls, [])  # 目标已满：连便宜的判断都不问
        self.assertEqual(generate.call_count, 0)
        self.assertEqual(self.loop.channel.sent, [])
        reason = audit.call_args_list[0].args[1]
        self.assertIn('1 条', reason)


if __name__ == '__main__':
    unittest.main()
