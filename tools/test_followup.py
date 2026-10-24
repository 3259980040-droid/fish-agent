"""回访窗口（followup_s）：回完之后等一会儿，只补答一次。

七条最小验证，全部跑在假通道上：不开 websocket、不调真实模型、不发真实 QQ 消息。
后三条验续答口径：主动冷却只对"同一段对话的下一句"开口子。
"""
import asyncio
import json
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from test_social_base import Fixture
from test_group_fish_send import FakeChannel
from group_policy import GroupPolicy
from group_tests import GroupTests
from storage import database
from social_context import SocialContext
from qq_agent_loop import QQAgentLoop
import group_decide


class FollowupTests(Fixture):
    def setUp(self):
        super().setUp()
        contexts = SocialContext(self.root)
        revision = contexts.save_persona('qq', '主人私聊人格', '主人私聊里的 Fish')['id']
        contexts.set_default('qq', revision)
        GroupTests(self.root).create(self.group, revision, self.owner)
        self.policy = GroupPolicy(self.root)
        # 这两个测试验的是生成与发送本身，不是预筛：把它关掉，回到"先成后判"的老顺序，
        # 否则这里每条消息都会先花一次判断模型。预筛另有 test_group_prescreen.py。
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True,
                                          'debounce_s': 0, 'read': True, 'prescreen_enabled': False})
        self.loop = QQAgentLoop(self.root)
        self.channel = FakeChannel()
        self.loop.channel = self.channel
        self.judge = Mock(return_value={'speak': True, 'reason': '值得说', 'judge_calls': 1})
        self.generate = Mock(return_value={'engine': 'fish', 'text': '回一句', 'tools': []})
        self.audit = Mock()
        # Keep the patcher itself: .start() hands back the replacement, and stopping that
        # would leave the module patched for whatever runs next in the same process.
        for patcher in (patch('qq_agent_loop.connect', side_effect=AssertionError('真实连接')),
                        patch('model_gateway.ModelGateway.chat',
                              side_effect=AssertionError('真实模型调用')),
                        patch.object(group_decide, 'worth_speaking', self.judge, create=True),
                        patch.object(group_decide, 'fish_candidate', self.generate),
                        patch.object(self.loop.governance, 'audit', self.audit)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def payload(self, text='这句怎么回', mid=None):
        return {'chat_type': 'group', 'chat_id': self.chat,
                'message_id': mid or 'm-' + uuid.uuid4().hex[:8],
                'sender': {'id': '333333333', 'display_name': '群友甲'},
                'reply_to': '', 'mentions_bot': False, 'reply_to_bot': False,
                'segments': [{'type': 'text', 'text': text}]}

    def drive(self, coro):
        return asyncio.run(coro)

    def audits(self):
        return [call.args[0] for call in self.audit.call_args_list]

    def reservations(self):
        with database(self.policy.path) as db:
            return [dict(row) for row in db.execute('SELECT cursor,state FROM reservations ORDER BY cursor')]

    def age_last_observation(self, seconds):
        """把最新一条观察推老：它发生在窗口之前，不是我们这句话的下一句。"""
        with database(self.policy.path) as db:
            row = db.execute('SELECT seq,data FROM observations WHERE chat=? ORDER BY seq DESC LIMIT 1',
                             (self.chat,)).fetchone()
            data = json.loads(row['data']); data['ts'] -= seconds
            db.execute('UPDATE observations SET data=?,created=created-? WHERE chat=? AND seq=?',
                       (json.dumps(data, ensure_ascii=False), seconds, self.chat, row['seq']))

    async def until(self, condition, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            await asyncio.sleep(0.02)
        raise AssertionError('等待超时')

    async def settle(self):
        while self.loop.group_tasks:
            await asyncio.gather(*list(self.loop.group_tasks), return_exceptions=True)

    # 1
    def test_default_zero_ends_the_turn_without_any_wait(self):
        slept = []
        real = asyncio.sleep

        async def spy(delay, *args, **kwargs):
            slept.append(delay)
            return await real(delay, *args, **kwargs)

        async def go():
            with patch('qq_agent_loop.asyncio.sleep', spy):
                self.loop._fish_start(self.payload('第一句', mid='fz-1'), self.chat)
                await self.settle()

        self.drive(go())
        self.assertEqual([d for d in slept if d >= 0.5], [])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.audits(), [])
        self.assertEqual(self.loop._followups, set())

    # 2
    def test_one_new_message_in_the_window_is_answered_exactly_once(self):
        # 主动冷却设 0：这条只验"窗口内补答一次"本身；默认 60 秒冷却下的续答豁免，
        # 由下面的 test_a_continuation_past_the_active_cooldown_is_answered 单独验。
        self.policy.configure(self.chat, {'followup_s': 1, 'active_cooldown_s': 0})

        async def go():
            self.loop._fish_start(self.payload('第一句', mid='fw-1'), self.chat)
            await self.until(lambda: len(self.channel.sent) >= 1)
            # 对方紧接着补的一句：只进策略库，不触发独立事件——正是回访窗口要补上的那一句。
            self.policy.observe(self.payload('再补一句', mid='fw-2'))
            await self.settle()

        self.drive(go())
        self.assertEqual(len(self.channel.sent), 2)
        self.assertEqual([s['reply_to'] for s in self.channel.sent], ['fw-1', 'fw-2'])
        audits = self.audits()
        self.assertEqual(audits.count('group_fish_followup_wait'), 1)
        self.assertEqual(audits.count('group_fish_followup_sent'), 1)
        self.assertEqual(self.loop._followups, set())

    # 3
    def test_nothing_new_in_the_window_is_silent_and_unaudited_as_a_send(self):
        self.policy.configure(self.chat, {'followup_s': 1})

        async def go():
            self.loop._fish_start(self.payload('唯一的一条', mid='fq-1'), self.chat)
            await self.settle()

        self.drive(go())
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.audits(), ['group_fish_followup_wait'])
        self.assertEqual(self.loop._followups, set())

    # 4
    def test_a_second_event_in_the_window_never_queues_a_second_waiter(self):
        self.policy.configure(self.chat, {'followup_s': 1, 'active_cooldown_s': 0})

        async def go():
            self.loop._fish_start(self.payload('第一句', mid='fs-1'), self.chat)
            await self.until(lambda: len(self.channel.sent) >= 1)
            await self.until(lambda: self.loop._followups == {self.chat})
            self.loop._fish_start(self.payload('第二句', mid='fs-2'), self.chat)
            await self.until(lambda: len(self.channel.sent) >= 2)
            await self.settle()

        self.drive(go())
        self.assertEqual(self.audits().count('group_fish_followup_wait'), 1)
        self.assertEqual(len(self.channel.sent), 2)  # 第二条由它自己的事件回掉，补答不再来第三次
        self.assertEqual(self.loop._followups, set())


    # 5
    def test_a_continuation_past_the_active_cooldown_is_answered(self):
        # 冷却用默认的 60 秒：普通路径此刻是"主动回复冷却中"，只有续答能过。
        self.policy.configure(self.chat, {'followup_s': 1})
        self.assertEqual(self.policy.config(self.chat)['active_cooldown_s'], 60)

        async def go():
            self.loop._fish_start(self.payload('第一句', mid='fk-1'), self.chat)
            await self.until(lambda: len(self.channel.sent) >= 1)
            self.policy.observe(self.payload('接着说下一句', mid='fk-2'))
            self.assertEqual(self.policy.candidate(self.chat)['reason'], '主动回复冷却中')
            await self.settle()

        self.drive(go())
        self.assertEqual([s['reply_to'] for s in self.channel.sent], ['fk-1', 'fk-2'])
        reasons = [call.args[1] for call in self.audit.call_args_list
                   if call.args[0] == 'group_fish_followup_sent']
        self.assertEqual(len(reasons), 1)
        self.assertIn('continuation', reasons[0])

    # 6
    def test_the_same_message_without_the_continuation_flag_is_still_cooled_down(self):
        self.policy.observe(self.payload('第一句', mid='fd-1'))
        token = self.policy.reserve(self.chat, 1, 'active')
        self.policy.finish(token, sent=True)
        self.policy.observe(self.payload('第二句', mid='fd-2'))

        cooled = self.policy.candidate(self.chat)
        self.assertEqual((cooled['action'], cooled['reason']), ('wait', '主动回复冷却中'))
        self.assertEqual(self.policy.reserve(self.chat, 2, 'active'), '')
        self.assertEqual(self.policy.candidate(self.chat, continuation=True)['action'], 'candidate')
        followup = self.policy.reserve(self.chat, 2, 'active', continuation=True)
        self.assertTrue(followup)
        self.assertFalse(self.policy.valid(followup))
        self.assertTrue(self.policy.valid(followup, continuation=True))
        self.policy.finish(followup, sent=True)
        # 只豁免"每小时上限"和"冷却"：一分钟总量上限对续答照样生效。
        self.policy.configure(self.chat, {'max_total_min': 1})
        self.policy.observe(self.payload('第三句', mid='fd-3'))
        limited = self.policy.candidate(self.chat, continuation=True)
        self.assertEqual((limited['action'], limited['reason']), ('wait', '全部回复达到每分钟上限'))

    # 7
    def test_a_topic_older_than_the_window_is_not_a_continuation(self):
        self.policy.configure(self.chat, {'followup_s': 1})

        async def go():
            self.loop._fish_start(self.payload('第一句', mid='fc-1'), self.chat)
            await self.until(lambda: len(self.channel.sent) >= 1)
            self.policy.observe(self.payload('窗口之前的旧话题', mid='fc-2'))
            self.age_last_observation(120)
            await self.settle()

        self.drive(go())
        self.assertEqual(len(self.channel.sent), 1)
        self.assertNotIn('group_fish_followup_sent', self.audits())
        self.assertEqual([(r['cursor'], r['state']) for r in self.reservations()], [(1, 'sent')])


if __name__ == '__main__':
    unittest.main()
