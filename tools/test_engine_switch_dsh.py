"""engine=fish must take the old DSH dialogue's sending rights away with the switch.

A group that switched to fish still has the dialogue the previous engine started:
it generates on its own and asks only its own window's listen/reply switches, so a
stale segment could leave beside the new engine's reply. These cases drive that
window on a fake channel and a fake runtime: no websocket is opened, no model call
is attempted, no QQ message leaves the machine.
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
from group_dialogue import GroupDialogue
from group_policy import GroupPolicy
from group_tests import GroupTests
from group_workflow import GroupWorkflow
from qq_agent_loop import QQAgentLoop
import group_decide

# The unpatched sender, so a case can drive the real per-segment checks itself.
REAL_DELIVER = QQAgentLoop._deliver_reply


class SwitchTests(Fixture):
    def setUp(self):
        super().setUp()
        # The deployment's own switches: engine lives here, not in the test window.
        self.main = GroupPolicy(self.root)
        self.tests = GroupTests(self.root)
        persona = self.tests.contexts.save_persona('qq', '同一个人格', '引擎切换测试用的人格')
        self.trial = self.tests.create(self.group, persona['id'], self.owner)
        self.loop = QQAgentLoop(self.root)
        self.sent = []

        async def action(name, args, timeout=0):
            self.sent.append({'name': name, **args})
            return {'ok': True, 'payload': {'message_id': 'out-' + str(len(self.sent))}}

        self.loop.channel = Mock(bot={'id': '20001'}, action=AsyncMock(side_effect=action))
        self.workflow = GroupWorkflow(self.root, trial=self.trial)
        self.workflow.policy.configure(self.chat, {'debounce_s': 0})
        self.dialogue = GroupDialogue(self.loop, self.workflow, self.chat, merge_s=.01)
        self.key = self.trial['id'] + ':' + self.trial['revision']
        self.loop.group_dialogues[self.key] = self.dialogue
        self.loop.group_test_workflows[self.key] = self.workflow
        self.clients = []
        self.content = None
        self.reply_text = 'DSH 的一条回复'
        self.answer_error = ''
        # The one sender under test, counted instead of delivered.
        self.deliver = AsyncMock(return_value={'ok': True, 'payload': {'message_id': 'out-1'}})
        self.judge = Mock(return_value={'speak': True, 'reason': '值得说'})
        for target in (patch('qq_agent_loop.connect', side_effect=AssertionError('真实连接')),
                       patch('model_gateway.ModelGateway.chat', side_effect=AssertionError('真实模型调用')),
                       patch('runtime_adapter.DSHRuntimeAdapter.connect', side_effect=self._client),
                       patch('tool_host.bind_session'),
                       patch('tool_host.set_group_context'),
                       patch.object(self.loop.runtime, 'cancel', return_value={'ok': True}),
                       patch.object(QQAgentLoop, '_deliver_reply', new=self.deliver),
                       patch.object(group_decide, 'worth_speaking', self.judge)):
            target.start()
            self.addCleanup(target.stop)   # .start() hands back the mock, not the patcher

    # -- the fake runtime the DSH path talks to: one client per generated reply.
    def _client(self, *args, **kwargs):
        client = Mock(auth=AsyncMock(), create_session=AsyncMock(return_value='s-' + uuid.uuid4().hex),
                      baseline=AsyncMock(return_value=0), prompt=AsyncMock(side_effect=self._prompt),
                      wait_response=AsyncMock(side_effect=self._answer))
        self.clients.append(client)
        return client

    async def _prompt(self, session, content):
        self.content = content
        if self.on_prompt:
            await self.on_prompt()

    async def _answer(self, *args, **kwargs):
        if self.answer_error:
            return {'ok': False, 'error': self.answer_error}
        return {'ok': True, 'text': self.reply_text, 'reasoning': ''}

    def _switching_deliver(self, switch, seen):
        """The real sender, with the owner's switch landing after the entry check."""
        async def deliver(loop, *args, **kwargs):
            seen['kwargs'] = kwargs
            switch()
            return await REAL_DELIVER(loop, *args, **kwargs)
        return deliver

    def payload(self, text='这句怎么回', mid=None, *, sender='333333333', mentions=True):
        return {'chat_type': 'group', 'chat_id': self.chat,
                'message_id': mid or 'm-' + uuid.uuid4().hex[:8],
                'sender': {'id': sender, 'display_name': '群友甲'},
                'reply_to': '', 'mentions_bot': mentions, 'reply_to_bot': False,
                'segments': [{'type': 'text', 'text': text}]}

    async def drain(self):
        for _ in range(6):
            tasks = [t for t in self.loop.group_tasks if not t.done()]
            if not tasks:
                return
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)

    def fish(self, **extra):
        return {'engine': 'fish', 'listen': True, 'reply': True, **extra}

    def generate(self, text='新引擎的一条'):
        return Mock(return_value={'engine': 'fish', 'text': text, 'tools': []})

    # 1
    def test_a_fish_switch_stops_a_generating_dsh_reply_before_it_sends(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold():
            entered.set()
            await release.wait()

        self.on_prompt = hold

        async def go():
            await self.dialogue.observe(self.payload('生成中的一条', mid='dsh-1'))
            self.assertTrue(self.dialogue.enabled())          # engine is still dsh here
            await asyncio.wait_for(entered.wait(), 5)
            self.main.configure(self.chat, self.fish())
            self.assertFalse(self.dialogue.enabled())         # generation no longer admits a send
            task_id = next(iter(self.dialogue.tasks))
            job = self.dialogue.jobs.get(task_id)
            self.assertEqual(job['state'], 'generating')       # it really is mid-reply
            self.assertFalse(self.dialogue.valid(job))
            release.set()
            await self.drain()
            self.assertEqual(self.dialogue.jobs.get(task_id)['state'], 'cancelled')

        asyncio.run(go())
        self.assertEqual(self.deliver.await_count, 0)
        self.assertEqual(self.sent, [])

    # 2
    def test_a_fish_inbound_cancels_and_forgets_the_chats_dsh_dialogue(self):
        self.main.configure(self.chat, self.fish())
        cancel = AsyncMock()

        async def go():
            with patch.object(GroupDialogue, 'cancel', cancel), \
                    patch.object(group_decide, 'fish_candidate', self.generate('新引擎的一条')):
                await self.loop._on_group(self.payload('切过来了', mid='flip-1'))
                await self.drain()

        asyncio.run(go())
        self.assertEqual(cancel.await_count, 1)               # awaited, not just scheduled
        self.assertNotIn(self.key, self.loop.group_dialogues)  # and forgotten with the engine
        self.assertEqual(self.deliver.await_count, 1)         # fish is the only sender left

    # 3
    def test_a_shadow_engine_leaves_the_dsh_dialogue_working(self):
        self.main.configure(self.chat, {'engine': 'shadow', 'listen': True, 'reply': True})

        async def go():
            self.assertTrue(self.dialogue.enabled())
            await self.dialogue.observe(self.payload('shadow 下照旧', mid='sd-1'))
            await self.drain()

        asyncio.run(go())
        self.assertEqual(self.deliver.await_count, 1)         # shadow constrains fish only
        self.assertTrue(self.dialogue.enabled())

    # 4
    def test_a_switch_back_to_dsh_restores_dsh_and_never_revives_the_fish_token(self):
        self.main.configure(self.chat, self.fish())
        self.assertFalse(self.dialogue.enabled())
        seen = self.main.observe(self.payload('fish 时代的一条', mid='tok-1'))
        token = self.main.reserve(self.chat, int(seen['cursor']), 'direct')
        self.assertTrue(token)
        self.assertTrue(self.main.live(token))                # the fish engine could have sent

        self.main.configure(self.chat, {'engine': 'dsh'})
        self.assertFalse(self.main.valid(token))              # the retired reservation stays spent
        self.assertFalse(self.main.live(token))
        self.assertTrue(self.dialogue.enabled())              # dsh answers this group again

        async def go():
            await self.dialogue.observe(self.payload('切回 dsh 后的一条', mid='back-1'))
            await self.drain()

        asyncio.run(go())
        self.assertEqual(self.deliver.await_count, 1)

    # 5
    def test_a_stuck_judge_never_speaks_and_is_audited_as_a_possible_charge(self):
        self.main.configure(self.chat, self.fish())
        audit = Mock()

        def stuck(*_args, **_kwargs):
            time.sleep(0.3)                                   # longer than the patched bound
            return {'speak': True, 'reason': '太慢但想说'}

        async def go():
            with patch.object(group_decide, 'worth_speaking', stuck), \
                    patch('qq_agent_loop.JUDGE_TIMEOUT_S', 0.05), \
                    patch.object(group_decide, 'fish_candidate', self.generate('不该发的一条')), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(self.payload('这句', mid='judge-slow'))
                await self.drain()

        asyncio.run(go())
        kinds = [call.args[0] for call in audit.call_args_list]
        self.assertEqual(self.deliver.await_count, 0)
        self.assertEqual(self.sent, [])
        self.assertIn('group_fish_judge_timeout', kinds)
        timeout = next(call.args[1] for call in audit.call_args_list
                       if call.args[0] == 'group_fish_judge_timeout')
        self.assertIn('请求可能仍在计费', timeout)             # wait_for stops the await, not the thread
        self.assertEqual([t for t in self.loop.group_tasks if not t.done()], [])

    # 6
    def test_a_switch_window_sends_once_and_the_in_flight_dsh_reply_is_the_casualty(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold():
            entered.set()
            await release.wait()

        self.on_prompt = hold
        held = {}

        async def go():
            await self.dialogue.observe(self.payload('旧 DSH 在途的一条', mid='win-dsh'))
            await asyncio.wait_for(entered.wait(), 5)
            held['job'] = next(iter(self.dialogue.tasks))
            held['stale'] = self.dialogue.tasks[held['job']]
            self.main.configure(self.chat, self.fish())
            with patch.object(group_decide, 'fish_candidate', self.generate('新 Fish 的一条')):
                await self.loop._on_group(self.payload('新引擎入站', mid='win-fish'))
                others = [t for t in self.loop.group_tasks
                          if not t.done() and t is not held['stale']]
                await asyncio.wait_for(asyncio.gather(*others, return_exceptions=True), 5)
                held['during'] = self.deliver.await_count   # both routes are alive right here
            release.set()                                    # the stale generation finishes anyway
            await self.drain()
            held['after'] = self.deliver.await_count
            held['state'] = self.dialogue.jobs.get(held['job'])['state']

        asyncio.run(go())
        self.assertEqual(held['during'], 1)                    # the fish route spoke, the DSH did not
        self.assertEqual(held['after'], 1)                     # and the stale one still never speaks
        self.assertEqual(self.deliver.await_args.args[2], 'group-fish:win-fish')
        self.assertEqual(held['state'], 'cancelled')            # reason: the switch stopped it
        self.assertEqual(self.sent, [])                        # no transport was reached at all

    # -- the two short auxiliaries the old path sends beside a reply. Both check the
    #    engine at their entry, so the only thing standing between a switch and the
    #    channel is the predicate the sender asks again, once per segment.
    # 7
    def test_the_cancel_ack_is_refused_when_the_engine_turns_fish_before_it_sends(self):
        seen = {}

        def flip():
            self.main.configure(self.chat, self.fish())        # the switch lands after the entry check

        async def go():
            with patch.object(QQAgentLoop, '_deliver_reply', self._switching_deliver(flip, seen)):
                await self.dialogue.observe(self.payload('取消', mid='ack-1'))

        asyncio.run(go())
        self.assertEqual(self.sent, [])                        # the channel was never reached
        self.assertIn('validate', seen['kwargs'])              # but the sender asked again
        self.assertFalse(seen['kwargs']['validate']())

    # 8
    def test_the_cancel_ack_still_sends_while_the_engine_is_dsh_or_shadow(self):
        for engine in ('dsh', 'shadow'):
            with self.subTest(engine=engine):
                self.main.configure(self.chat, {'engine': engine, 'listen': True, 'reply': True})
                self.sent.clear()
                seen = {}

                async def go():
                    with patch.object(QQAgentLoop, '_deliver_reply',
                                      self._switching_deliver(lambda: None, seen)):
                        await self.dialogue.observe(self.payload('取消', mid='ack-' + engine))

                asyncio.run(go())
                self.assertEqual([s['segments'][0]['text'] for s in self.sent],
                                 ['你目前没有等待中的请求。'])
                self.assertTrue(seen['kwargs']['validate']())

    # 9
    def test_the_failure_notice_is_refused_when_the_engine_turns_fish_before_it_sends(self):
        self.answer_error = '模型不可用'
        seen = {}

        def flip():
            self.main.configure(self.chat, self.fish())

        async def go():
            with patch.object(QQAgentLoop, '_deliver_reply', self._switching_deliver(flip, seen)):
                await self.dialogue.observe(self.payload('生成会失败的一条', mid='fail-1'))
                await self.drain()

        asyncio.run(go())
        self.assertEqual(self.sent, [])                        # the failure notice stopped too
        self.assertIn('validate', seen['kwargs'])
        self.assertFalse(seen['kwargs']['validate']())

    # 10
    def test_the_failure_notice_still_sends_while_the_engine_is_shadow(self):
        self.main.configure(self.chat, {'engine': 'shadow', 'listen': True, 'reply': True})
        self.answer_error = '模型不可用'
        seen = {}

        async def go():
            with patch.object(QQAgentLoop, '_deliver_reply',
                              self._switching_deliver(lambda: None, seen)):
                await self.dialogue.observe(self.payload('生成会失败的一条', mid='fail-2'))
                await self.drain()

        asyncio.run(go())
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['reply_to'], 'fail-2')
        self.assertIn('没有正常交付', self.sent[0]['segments'][0]['text'])
        self.assertTrue(seen['kwargs']['validate']())


if __name__ == '__main__':
    unittest.main()
