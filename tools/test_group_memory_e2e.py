"""One real inbound group message, end to end, into that group's own memory.

No model call and no QQ message leave the machine: the transport is a fake channel,
the worth-speaking judge is a mock and the extraction model is a fake gateway passed
straight to learn_one. Everything after that - learning_jobs, apply_extraction, the
memories row and the group's own recall - is the real code on a temporary root, so
what is asserted here is what a later turn in that group would really read back.

The chain under test:
    inbound payload -> _fish_remember -> learning_jobs -> learn_one
    -> apply_extraction -> memories (chat:group:<id>, candidate) -> same-group recall

Missing message_id is exercised through _fish_remember directly: GroupPolicy.observe
already rejects an id-less payload before the fish branch runs, so the helper is the
only place where a source could still be built without one, and that is exactly the
place the shared-source bug would have lived.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from test_social_base import Fixture
from group_policy import GroupPolicy
from group_tests import GroupTests
from memory_store import MemoryStore
from social_context import SocialContext
from storage import database
from qq_agent_loop import QQAgentLoop
import group_decide


class FakeChannel:
    """The only transport the fish branch may reach; it records instead of sending."""

    def __init__(self):
        self.bot = {'id': '20001'}
        self.sent = []

    async def action(self, name, args, timeout=0):
        self.sent.append({'name': name, **args})
        return {'ok': True, 'payload': {'message_id': 'out-' + str(len(self.sent))}}


class FakeGateway:
    """Stands in for the extraction model: fixed JSON, and it records what it saw."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def json(self, prompt, text):
        self.calls.append((prompt, text))
        return self.result


# A grounded extraction: the quote is verbatim in the message, so the fact is really
# backed by what the group said. A group scope can only ever be evidence='observed',
# so apply_extraction must still store it as a candidate.
EXTRACTION = {'memories': [{'text': '群友甲最近都在夜钓鲫鱼', 'category': '事件', 'kind': 'event',
                            'evidence': 'self_report', 'source_quote': '我最近都在夜钓鲫鱼',
                            'subject': 'qq:333333333', 'confidence': 0.9}]}


class GroupMemoryEndToEndTests(Fixture):
    def setUp(self):
        super().setUp()
        contexts = SocialContext(self.root)
        self.revision = contexts.save_persona('qq', '主人私聊人格', '主人私聊里的 Fish')['id']
        contexts.set_default('qq', self.revision)
        GroupTests(self.root).create(self.group, self.revision, self.owner)
        self.policy = GroupPolicy(self.root)
        # 这两个测试验的是记忆，不是预筛：关掉预筛，别让每条消息先花一次判断模型。
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True,
                                          'learn': True, 'debounce_s': 0, 'read': True, 'prescreen_enabled': False})
        self.loop = QQAgentLoop(self.root)
        self.loop.channel = FakeChannel()
        self.store = MemoryStore(self.root, 'qq')
        for target in (patch('qq_agent_loop.connect', side_effect=AssertionError('真实连接')),
                       patch('model_gateway.ModelGateway.chat',
                             side_effect=AssertionError('真实模型调用')),
                       # A vector warm is a daemon thread still holding memory.db when
                       # the temporary root is deleted: Windows then cannot clean up.
                       patch('memory_engine.enqueue_warm', return_value=False),
                       patch.object(group_decide, 'worth_speaking',
                                    Mock(return_value={'speak': True, 'reason': '值得说'}),
                                    create=True)):
            target.start()
            self.addCleanup(target.stop)

    def payload(self, text='我最近都在夜钓鲫鱼', mid='m-1', sender='333333333', **extra):
        data = {'chat_type': 'group', 'chat_id': self.chat, 'message_id': mid,
                'sender': {'id': sender, 'display_name': '群友甲'},
                'reply_to': '', 'mentions_bot': False, 'reply_to_bot': False,
                'segments': [{'type': 'text', 'text': text}]}
        data.update(extra)
        return data

    def jobs(self):
        with database(self.store.path) as db:
            return [dict(row) for row in db.execute(
                'SELECT id,scope,source,subject,text,status,error FROM learning_jobs ORDER BY created')]

    def memories(self):
        with database(self.store.path) as db:
            return [dict(row) for row in db.execute(
                'SELECT id,text,scope,source,subject,status,evidence FROM memories ORDER BY created')]

    def run_group(self, payload, reply='好呀，这就说'):
        """One account-level turn through the loop's fish branch, audit captured."""
        audit = Mock()

        async def go():
            with patch.object(group_decide, 'fish_candidate',
                              Mock(return_value={'engine': 'fish', 'text': reply, 'tools': []})), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(payload)
                while self.loop.group_tasks:
                    await asyncio.gather(*list(self.loop.group_tasks), return_exceptions=True)
        asyncio.run(go())
        return [call.args[0] for call in audit.call_args_list]

    def learned_group(self, mid='e2e-1', text='我最近都在夜钓鲫鱼'):
        """The whole chain once: inbound message, job, fake extraction, recallable row."""
        self.run_group(self.payload(text, mid=mid))
        source = self.jobs()[0]['source']
        gateway = FakeGateway(EXTRACTION)
        self.assertTrue(self.store.learn_one(gateway, source=source))
        self.assertEqual(len(gateway.calls), 1, '提取模型必须被真实调用一次')
        return source, gateway

    # 1
    def test_inbound_message_becomes_a_recallable_group_candidate(self):
        source = 'qq-group:' + self.chat + ':e2e-full'
        self.run_group(self.payload('我最近都在夜钓鲫鱼', mid='e2e-full'))
        rows = self.jobs()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['source'], source)
        self.assertEqual(rows[0]['scope'], 'chat:' + self.chat)
        self.assertEqual(rows[0]['subject'], 'qq:333333333')
        self.assertEqual(rows[0]['status'], 'pending')

        gateway = FakeGateway(EXTRACTION)
        self.assertTrue(self.store.learn_one(gateway, source=source))
        self.assertEqual(gateway.calls[0][1], '我最近都在夜钓鲫鱼')  # the real message text
        done = self.jobs()[0]
        self.assertEqual((done['status'], done['error']), ('done', ''))

        saved = self.memories()
        self.assertEqual(len(saved), 1, saved)
        self.assertEqual(saved[0]['scope'], 'chat:' + self.chat)
        self.assertEqual(saved[0]['status'], 'candidate')
        self.assertEqual(saved[0]['source'], source)
        self.assertEqual(saved[0]['subject'], 'qq:333333333')
        self.assertIn('夜钓鲫鱼', saved[0]['text'])

        block = self.store.context('夜钓', scope='chat:' + self.chat)
        self.assertIn('夜钓鲫鱼', block)
        self.assertIn('群内记录·候选', block)

    # 2
    def test_group_learn_off_writes_no_job_and_says_so(self):
        self.policy.configure(self.chat, {'learn': False})
        payload = self.payload('我最近都在夜钓鲫鱼', mid='e2e-learn-off')
        audits = self.run_group(payload)
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.memories(), [])
        self.assertNotIn('group_fish_memory_failed', audits)
        self.assertEqual(len(self.loop.channel.sent), 1)  # the reply path is untouched
        outcome = self.loop._fish_remember(self.chat, dict(payload), '我最近都在夜钓鲫鱼')
        self.assertEqual((outcome['learned'], outcome['reason']), (False, 'learn-off'))
        self.assertEqual(self.jobs(), [])

    # 3
    def test_global_auto_learning_off_queues_nothing_and_is_not_a_save(self):
        self.policy.configure(self.chat, {'learn': True})
        self.store.configure(False)
        payload = self.payload('我最近都在夜钓鲫鱼', mid='e2e-global-off')
        audits = self.run_group(payload)
        self.assertEqual(self.jobs(), [])  # the group switch alone is not enough
        # Audited, so "nothing was learned" is visible instead of looking like a save.
        self.assertIn('group_fish_memory_off', audits)
        self.assertNotIn('group_fish_memory_failed', audits)
        outcome = self.loop._fish_remember(self.chat, dict(payload), '我最近都在夜钓鲫鱼')
        self.assertEqual((outcome['learned'], outcome['reason']), (False, 'auto-learning-off'))
        self.assertNotIn('source', outcome)
        self.assertEqual(self.jobs(), [])
        # And the learner refuses too, so nothing can be learned behind the switch.
        self.assertFalse(self.store.learn_one(FakeGateway(EXTRACTION)))
        self.assertFalse(self.store.learning_status()['enabled'])
        self.assertEqual(self.memories(), [])

    # 4
    def test_the_candidate_mark_is_added_by_group_recall_only(self):
        self.learned_group(mid='e2e-mark')
        block = self.store.context('夜钓', scope='chat:' + self.chat)
        self.assertIn('群内记录·候选，未经确认', block)
        # The widening is the recall path's alone: a plain read (tools, management)
        # still refuses an unconfirmed row, so nothing else started trusting it.
        plain = self.store.search(scope='chat:' + self.chat)
        self.assertEqual(plain, [])
        self.assertEqual(len(self.store.candidates('chat:' + self.chat)), 1)
        # The same query in an owner scope must not carry the group mark either way.
        self.assertNotIn('群内记录·候选', self.store.context('夜钓', scope='owner'))

    # 5
    def test_another_group_and_the_owner_cannot_recall_it(self):
        self.learned_group(mid='e2e-isolation')
        mine = self.store.context('夜钓', scope='chat:' + self.chat)
        self.assertIn('夜钓鲫鱼', mine)
        owner = self.store.context('夜钓', scope='owner')
        self.assertNotIn('夜钓鲫鱼', owner)
        self.assertNotIn('群友甲', owner)
        other = 'group:111222333'
        elsewhere = self.store.context('夜钓', scope='chat:' + other)
        self.assertNotIn('夜钓鲫鱼', elsewhere)
        self.assertEqual(self.store.search(scope='chat:' + other), [])
        self.assertEqual([row['scope'] for row in self.memories()], ['chat:' + self.chat])

    # 6
    def test_a_missing_message_id_uses_its_own_local_id_and_never_shares_a_source(self):
        first = self.loop._fish_remember(
            self.chat, self.payload('第一条没有平台编号', mid=None, local_id=41), '第一条没有平台编号')
        second = self.loop._fish_remember(
            self.chat, self.payload('第二条没有平台编号', mid=None, local_id=42), '第二条没有平台编号')
        self.assertEqual((first['learned'], first['id_kind']), (True, 'local_id'))
        self.assertEqual((second['learned'], second['id_kind']), (True, 'local_id'))
        self.assertNotEqual(first['source'], second['source'])
        rows = self.jobs()
        self.assertEqual(len(rows), 2, rows)  # the second message is not swallowed
        self.assertEqual([row['source'] for row in rows], [first['source'], second['source']])
        for row in rows:
            self.assertTrue(row['source'].startswith('qq-group:' + self.chat + ':local_id:'), row)
        # Each local id learns on its own, into this group's scope only: the quote is
        # this job's own text, so a swallowed job would show up as a missing memory.
        for row in rows:
            self.assertTrue(self.store.learn_one(FakeGateway(
                {'memories': [{'text': row['text'], 'category': '事实', 'kind': 'fact',
                               'evidence': 'self_report', 'source_quote': row['text'],
                               'subject': 'qq:333333333', 'confidence': 0.9}]}),
                source=row['source']))
        self.assertEqual(len(self.memories()), 2)
        self.assertEqual({row['scope'] for row in self.memories()}, {'chat:' + self.chat})

    # 7
    def test_a_message_with_no_id_at_all_is_skipped_and_audited(self):
        bare = self.payload('没有编号的消息')
        bare.pop('message_id')
        audit = Mock()
        with patch.object(self.loop.governance, 'audit', audit):
            outcomes = [self.loop._fish_remember(self.chat, dict(bare), '没有编号的消息'),
                        self.loop._fish_remember(self.chat, dict(bare), '没有编号的消息'),
                        self.loop._fish_remember(self.chat, self.payload('空编号', mid=''), '空编号')]
        for outcome in outcomes:
            self.assertEqual((outcome['learned'], outcome['reason']), (False, 'missing-message-id'))
            self.assertNotIn('source', outcome)
        kinds = [call.args[0] for call in audit.call_args_list]
        self.assertEqual(kinds, ['group_fish_memory_skipped'] * 3)
        self.assertTrue(all('未学习' in call.args[1] for call in audit.call_args_list))
        # Nothing was queued, so there is no shared source for a redelivery to swallow.
        self.assertEqual(self.jobs(), [])


if __name__ == '__main__':
    unittest.main()
