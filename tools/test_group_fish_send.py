"""engine=fish: one Fish reply per group cursor, delivered by the loop's one sender.

Everything here runs on a fake channel that records what would have been sent: no
websocket is opened, no model call is attempted, no QQ message leaves the machine.
"""
import asyncio
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from test_social_base import Fixture
from governance import Governance
from group_policy import GroupPolicy
from group_tests import GroupTests
from social_context import SocialContext
from storage import database
from qq_agent_loop import QQAgentLoop
import group_decide


class FakeChannel:
    """The only transport the fish branch may reach; it records instead of sending."""

    def __init__(self, receipt=None):
        self.bot = {'id': '20001'}
        self.sent = []
        self.receipt = receipt

    async def action(self, name, args, timeout=0):
        self.sent.append({'name': name, **args})
        if self.receipt:
            return self.receipt(len(self.sent) - 1)
        return {'ok': True, 'payload': {'message_id': 'out-' + str(len(self.sent))}}


class RollbackChannel(FakeChannel):
    """Records sends, and lets the test change the chat's settings right after one lands."""

    def __init__(self, after_send=None):
        super().__init__()
        self.after_send = after_send

    async def action(self, name, args, timeout=0):
        result = await super().action(name, args, timeout)
        if self.after_send:
            self.after_send(len(self.sent))
        return result


class FishSendTests(Fixture):
    def setUp(self):
        super().setUp()
        contexts = SocialContext(self.root)
        self.revision = contexts.save_persona('qq', '主人私聊人格', '主人私聊里的 Fish')['id']
        contexts.set_default('qq', self.revision)
        GroupTests(self.root).create(self.group, self.revision, self.owner)
        self.policy = GroupPolicy(self.root)
        # 这两个测试验的是生成与发送本身，不是预筛：把它关掉，回到"先成后判"的老顺序，
        # 否则这里每条消息都会先花一次判断模型。预筛另有 test_group_prescreen.py。
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True,
                                          'debounce_s': 0, 'read': True, 'prescreen_enabled': False})
        self.loop = QQAgentLoop(self.root)
        self.channel = FakeChannel()
        self.loop.channel = self.channel
        # Nothing here may dial out: no websocket, no model provider, no second sender.
        for target in (patch('qq_agent_loop.connect', side_effect=AssertionError('真实连接')),
                       patch('model_gateway.ModelGateway.chat',
                             side_effect=AssertionError('真实模型调用'))):
            target.start()
            self.addCleanup(target.stop)
        observe = patch('group_dialogue.GroupDialogue.observe', new=AsyncMock())
        self.observed = observe.start()
        self.addCleanup(observe.stop)
        # The 'worth speaking' judge is another module's work, so these tests stand in
        # for it: the gate is exercised, its implementation and its budget are not.
        self.judge = Mock(return_value={'speak': True, 'reason': '值得说', 'judge_calls': 1})
        judge = patch.object(group_decide, 'worth_speaking', self.judge, create=True)
        judge.start()
        self.addCleanup(judge.stop)

    def payload(self, text='这句怎么回', mid=None, *, reply_to='', mentions=False, sender='333333333'):
        return {'chat_type': 'group', 'chat_id': self.chat,
                'message_id': mid or 'm-' + uuid.uuid4().hex[:8],
                'sender': {'id': sender, 'display_name': '群友甲'},
                'reply_to': reply_to, 'mentions_bot': mentions, 'reply_to_bot': False,
                'segments': [{'type': 'text', 'text': text}]}

    async def settle(self):
        """Await the group tasks a call scheduled, still inside the caller's patches."""
        while self.loop.group_tasks:
            await asyncio.gather(*list(self.loop.group_tasks), return_exceptions=True)

    def drive(self, coro):
        return asyncio.run(coro)

    def reservations(self):
        with database(self.policy.path) as db:
            return [dict(row) for row in
                    db.execute('SELECT token,chat,cursor,kind,state FROM reservations ORDER BY cursor')]

    def holding_reservation(self, cursor=0, kind='active'):
        with database(self.policy.path) as db:
            db.execute('INSERT INTO reservations(token,chat,cursor,kind,state,created) '
                       'VALUES(?,?,?,?,?,?)',
                       ('held-' + uuid.uuid4().hex[:6], self.chat, cursor, kind, 'active', time.time()))

    def audits(self, audit):
        return [call.args[0] for call in audit.call_args_list]

    # 1
    def test_a_held_reservation_generates_once_and_sends_once(self):
        respond = Mock(return_value='好呀，这就说')
        audit = Mock()
        generate = Mock(wraps=group_decide.fish_candidate)

        async def go():
            with patch.object(group_decide, 'fish_candidate', generate), \
                    patch('fish_chat.respond', respond), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(self.payload('这句怎么回', mid='fish-1'))
                await self.settle()
        self.drive(go())

        self.assertEqual(generate.call_count, 1)
        self.assertEqual(generate.call_args.kwargs['payload']['message_id'], 'fish-1')
        self.assertEqual(respond.call_count, 1)
        options = respond.call_args.kwargs
        self.assertEqual((options['history_source'], options['session'], options['origin']),
                         ('group-fish', 'group-fish:' + self.chat, 'group-fish:' + self.chat))
        self.assertIn(options['tool_context'], {'group'})  # 群侧目录已接线（4 个只读工具）
        self.assertEqual(options['persona']['body'], '主人私聊里的 Fish')
        self.assertEqual(len(self.channel.sent), 1)
        sent = self.channel.sent[0]
        self.assertEqual((sent['name'], sent['chat_id'], sent['reply_to'], sent['idempotency_key']),
                         ('send_message', self.chat, 'fish-1', 'group-fish:fish-1'))
        self.assertEqual(sent['segments'], [{'type': 'text', 'text': '好呀，这就说'}])
        self.assertEqual([(r['state'], r['kind'], r['cursor']) for r in self.reservations()],
                         [('sent', 'active', 1)])
        self.assertEqual(self.audits(audit), [])

    # 2
    def test_a_lost_reservation_generates_nothing_and_is_audited(self):
        self.holding_reservation()
        generate = Mock()
        audit = Mock()

        async def go():
            with patch.object(group_decide, 'fish_candidate', generate), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(self.payload(mid='fish-2'))
                await self.settle()
        self.drive(go())
        self.assertEqual(generate.call_count, 0)
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.audits(audit), ['group_fish_skipped'])
        self.assertEqual([r['state'] for r in self.reservations()], ['active'])

    # 3
    def test_two_inbound_in_one_window_produce_exactly_one_send(self):
        generate = Mock(return_value={'engine': 'fish', 'text': '一次就够', 'tools': []})
        audit = Mock()

        async def go():
            with patch.object(group_decide, 'fish_candidate', generate), \
                    patch.object(self.loop.governance, 'audit', audit):
                await asyncio.gather(self.loop._on_group(self.payload('第一句', mid='c-1')),
                                     self.loop._on_group(self.payload('第二句', mid='c-2')))
                await self.settle()
        self.drive(go())
        self.assertEqual(len(self.channel.sent), 1)
        self.assertLessEqual(generate.call_count, 1)
        self.assertEqual([r['state'] for r in self.reservations()], ['sent'])
        self.assertIn(self.channel.sent[0]['reply_to'], ('c-1', 'c-2'))

    # 4
    def test_the_reply_quotes_the_real_inbound_id_not_the_newest_row(self):
        conversation = self.loop.store.get_or_create_conversation(self.chat, 'qq')
        self.loop.store.record_message(conversation['id'], self.chat, '444444444', 'inbound',
                                       'm-later', None, [], '后到的另一条', 'observed')
        audit = Mock()

        async def go():
            with patch.object(group_decide, 'fish_candidate',
                              Mock(return_value={'engine': 'fish', 'text': '回先发的那条', 'tools': []})), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(self.payload('先发的那条', mid='m-first'))
                await self.settle()
        self.drive(go())
        self.assertEqual([s['reply_to'] for s in self.channel.sent], ['m-first'])
        self.assertNotEqual(self.channel.sent[0]['reply_to'], 'm-later')

    # 5
    def test_a_cancelled_run_generates_and_sends_nothing(self):
        cancel = threading.Event()
        cancel.set()
        generate = Mock()
        audit = Mock()

        async def go():
            with patch.object(group_decide, 'fish_candidate', generate), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._fish_reply(self.payload(mid='x-1'), self.chat, cancel)
                await self.settle()
        self.drive(go())
        self.assertEqual(generate.call_count, 0)
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.reservations(), [])
        self.assertEqual(self.audits(audit), ['group_fish_skipped'])

    # 6
    def test_paused_governance_generates_and_sends_nothing(self):
        Governance(self.root, 'qq').save({'paused': True})
        generate, audit = Mock(), Mock()

        async def go():
            with patch.object(group_decide, 'fish_candidate', generate), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(self.payload(mid='p-1'))
                await self.settle()
        self.drive(go())
        self.assertEqual((generate.call_count, len(self.channel.sent)), (0, 0))
        self.assertEqual(self.reservations(), [])
        self.assertEqual(self.audits(audit), ['group_fish_skipped'])

    # 7
    def test_an_unknown_receipt_is_recorded_uncertain_and_never_resent(self):
        deliver = AsyncMock(return_value={'ok': False,
                                          'error': {'code': '回执未取得', 'may_have_sent': True}})
        audit = Mock()

        async def go():
            with patch.object(group_decide, 'fish_candidate',
                              Mock(return_value={'engine': 'fish', 'text': '状态不明的一条', 'tools': []})), \
                    patch.object(QQAgentLoop, '_deliver_reply', deliver), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(self.payload('这句', mid='u-1'))
                await self.settle()
                await self.loop._on_group(self.payload('这句', mid='u-1'))
                await self.settle()
        self.drive(go())
        self.assertEqual(deliver.await_count, 1)   # one attempt, no second send
        self.assertEqual(self.channel.sent, [])    # the transport was never reached at all
        self.assertEqual([r['state'] for r in self.reservations()], ['uncertain'])
        self.assertEqual(self.audits(audit), ['group_fish_uncertain'])

    # 8
    def test_a_failed_generation_sends_nothing_and_leaves_the_old_path_alone(self):
        audit = Mock()
        generate = Mock(side_effect=RuntimeError('模型不可用'))
        # This case injects a generation failure. Give it an explicit successful
        # identity prerequisite, rather than depending on an unrelated local host.
        binding = Mock(return_value={'ok': True})

        async def go():
            with patch('tool_host.bind_session', binding), \
                    patch.object(group_decide, 'fish_candidate', generate), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(self.payload(mid='e-1'))
                await self.settle()
        self.drive(go())
        binding.assert_called_once_with(self.root, 'qq', 'group-fish:' + self.chat,
                                        'chat:' + self.chat, '333333333')
        self.assertEqual(generate.call_count, 1, audit.call_args_list)
        self.judge.assert_not_called()
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.observed.await_count, 0)  # no DSH dialogue for a fish group
        self.assertEqual(self.audits(audit), ['group_fish_failed'], audit.call_args_list)
        self.assertEqual([r['state'] for r in self.reservations()], ['skipped'])
        print('GENERATION_FAILURE_FIXTURE binding_calls=1 generation_calls=1 audit=' +
              repr([call.args for call in audit.call_args_list]), flush=True)

    # 9
    def test_engine_dsh_runs_none_of_the_fish_branch(self):
        self.policy.configure(self.chat, {'engine': 'dsh'})
        start, generate = Mock(), Mock()

        async def go():
            with patch.object(QQAgentLoop, '_fish_start', start), \
                    patch.object(group_decide, 'fish_candidate', generate):
                await self.loop._on_group(self.payload(mid='d-1'))
                await self.settle()
        self.drive(go())
        self.assertEqual((start.call_count, generate.call_count, len(self.channel.sent)), (0, 0, 0))
        self.assertEqual(self.reservations(), [])
        self.assertEqual(self.observed.await_count, 1)  # the DSH path is unchanged

    # 10
    def test_engine_shadow_still_only_records_a_candidate(self):
        self.policy.configure(self.chat, {'engine': 'shadow'})
        start = Mock()
        respond = Mock(return_value='候选回复')

        async def go():
            with patch.object(QQAgentLoop, '_fish_start', start), \
                    patch('fish_chat.respond', respond):
                await self.loop._on_group(self.payload(mid='s-1'))
                await self.settle()
        self.drive(go())
        self.assertEqual((start.call_count, respond.call_count), (0, 1))
        self.assertEqual(self.channel.sent, [])
        rows = group_decide.shadows(self.root, self.chat)
        self.assertEqual([(r['text'], r['engine']) for r in rows], [('候选回复', 'shadow')])
        self.assertEqual(self.observed.await_count, 1)

    # -- helpers for the rollback cases: a generation that is still in flight when the
    #    owner changes the chat's settings, which is exactly when a stale send would land.
    def switch_mid_generation(self, apply_switch, *, mid, text='生成中的一条'):
        """Generate, weaken the chat's authorisation, then let the reply continue."""
        entered, release = threading.Event(), threading.Event()
        audit = Mock()

        def slow(*_args, **_kwargs):
            entered.set()
            release.wait(10)
            return {'engine': 'fish', 'text': text, 'tools': []}

        async def go():
            with patch.object(group_decide, 'fish_candidate', slow), \
                    patch.object(self.loop.governance, 'audit', audit):
                task = asyncio.create_task(self.loop._on_group(self.payload(mid=mid)))
                self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                apply_switch()
                release.set()
                await task
                await self.settle()
        self.drive(go())
        return audit

    def states(self):
        return [row['state'] for row in self.reservations()]

    # 11
    def test_an_engine_rollback_to_dsh_stops_a_generation_in_flight(self):
        audit = self.switch_mid_generation(
            lambda: self.policy.configure(self.chat, {'engine': 'dsh'}), mid='rb-dsh')

        self.assertEqual(self.channel.sent, [])            # nothing left the machine
        self.assertNotIn('sent', self.states())            # the reservation lost its right to send
        self.assertEqual(self.judge.call_count, 0)         # and no paid judgement was spent on it
        self.assertIn('group_fish_skipped', self.audits(audit))

    # 12
    def test_an_engine_rollback_to_shadow_stops_a_generation_in_flight(self):
        audit = self.switch_mid_generation(
            lambda: self.policy.configure(self.chat, {'engine': 'shadow'}), mid='rb-shadow')

        self.assertEqual(self.channel.sent, [])
        self.assertNotIn('sent', self.states())
        self.assertIn('group_fish_skipped', self.audits(audit))

    # 13
    def test_withdrawing_reply_stops_a_generation_in_flight(self):
        audit = self.switch_mid_generation(
            lambda: self.policy.configure(self.chat, {'reply': False}), mid='rb-reply')

        self.assertEqual(self.channel.sent, [])
        self.assertNotIn('sent', self.states())
        self.assertIn('group_fish_skipped', self.audits(audit))

    # 14
    def test_withdrawing_listen_stops_a_generation_in_flight(self):
        audit = self.switch_mid_generation(
            lambda: self.policy.configure(self.chat, {'listen': False}), mid='rb-listen')

        self.assertEqual(self.channel.sent, [])
        self.assertNotIn('sent', self.states())
        self.assertIn('group_fish_skipped', self.audits(audit))
        # the same call also withdrew reply, so the stored pair is consistent again
        self.assertIs(self.policy.config(self.chat)['reply'], False)

    # 15
    def test_pausing_governance_stops_a_generation_in_flight(self):
        audit = self.switch_mid_generation(
            lambda: Governance(self.root, 'qq').save({'paused': True}), mid='rb-paused')

        self.assertEqual(self.channel.sent, [])
        self.assertNotIn('sent', self.states())
        self.assertIn('group_fish_skipped', self.audits(audit))

    # 16
    def test_a_rollback_between_two_segments_stops_the_rest(self):
        body = '第一段' * 700  # over one segment's 1800 characters, so it is split in two
        audit = Mock()

        def flip(index):
            if index == 1:  # the first segment landed; the owner rolls back right after it
                self.policy.configure(self.chat, {'engine': 'dsh'})

        async def go():
            with patch.object(group_decide, 'fish_candidate',
                              Mock(return_value={'engine': 'fish', 'text': body, 'tools': []})), \
                    patch.object(self.loop.governance, 'audit', audit):
                self.loop.channel = RollbackChannel(flip)
                await self.loop._on_group(self.payload(mid='rb-seg'))
                await self.settle()
        self.drive(go())

        sent = self.loop.channel.sent
        self.assertEqual(len(sent), 1)                     # the second segment was refused
        self.assertEqual(sent[0]['segments'], [{'type': 'text', 'text': body[:1800]}])
        self.assertEqual(self.states(), ['disabled'])      # retired mid-send, never 'sent'
        self.assertEqual(self.audits(audit), ['group_fish_uncertain'])

    # 17
    def test_a_judged_silent_reply_is_finished_unsent_and_audited(self):
        self.judge.return_value = {'speak': False, 'reason': '别人已经答了', 'judge_calls': 1}
        audit = Mock()

        async def go():
            with patch.object(group_decide, 'fish_candidate',
                              Mock(return_value={'engine': 'fish', 'text': '其实不必说', 'tools': []})), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(self.payload('这句', mid='j-1'))
                await self.settle()
        self.drive(go())

        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.states(), ['skipped'])       # finished, never 'sent'
        self.assertEqual(self.audits(audit), ['group_fish_silent'])
        self.assertIn('别人已经答了', audit.call_args.args[1])
        self.assertEqual(self.judge.call_args.kwargs['candidate_text'], '其实不必说')
        self.assertEqual(self.judge.call_args.kwargs['kind'], 'active')
        self.assertEqual(self.observed.await_count, 0)

    # 18
    def test_the_judge_is_asked_only_after_a_generation_succeeded(self):
        audit = Mock()

        async def go():
            with patch.object(group_decide, 'fish_candidate', Mock(side_effect=RuntimeError('模型不可用'))), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(self.payload(mid='j-2'))
                await self.settle()
        self.drive(go())
        self.assertEqual(self.judge.call_count, 0)
        self.assertEqual(self.states(), ['skipped'])

    # 19
    def test_a_generation_that_times_out_sends_nothing(self):
        audit = Mock()

        def slow(*_args, **_kwargs):
            time.sleep(0.3)
            return {'engine': 'fish', 'text': '太慢的一条', 'tools': []}

        async def go():
            with patch.object(group_decide, 'fish_candidate', slow), \
                    patch('qq_agent_loop.FISH_TIMEOUT_S', 0.05), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(self.payload('这句', mid='t-1'))
                await self.settle()
        self.drive(go())

        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.states(), ['skipped'])       # the reservation ended, unsent
        self.assertEqual(self.audits(audit), ['group_fish_timeout'])
        self.assertEqual(self.judge.call_count, 0)         # no reply, nothing to judge


if __name__ == '__main__':
    unittest.main()
