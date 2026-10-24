"""engine=fish writes a group's own experience, in that group's scope only.

No model call and no QQ message leave the machine: the transport is a fake channel
and the candidate is a mock. The memory store under test is a real MemoryStore on
the temporary root, so what is asserted is what a later turn would recall.
"""
import ast
import asyncio
import sys
import unittest
import uuid
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
import memory_store


class FakeChannel:
    """The only transport the fish branch may reach; it records instead of sending."""

    def __init__(self):
        self.bot = {'id': '20001'}
        self.sent = []

    async def action(self, name, args, timeout=0):
        self.sent.append({'name': name, **args})
        return {'ok': True, 'payload': {'message_id': 'out-' + str(len(self.sent))}}


class GroupMemoryTests(Fixture):
    def setUp(self):
        super().setUp()
        contexts = SocialContext(self.root)
        self.revision = contexts.save_persona('qq', '主人私聊人格', '主人私聊里的 Fish')['id']
        contexts.set_default('qq', self.revision)
        GroupTests(self.root).create(self.group, self.revision, self.owner)
        self.policy = GroupPolicy(self.root)
        # 这两个测试验的是记忆，不是预筛：关掉预筛，别让每条消息先花一次判断模型。
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True,
                                          'debounce_s': 0, 'read': True, 'prescreen_enabled': False})
        self.loop = QQAgentLoop(self.root)
        self.loop.channel = FakeChannel()
        self.store = MemoryStore(self.root, 'qq')
        for target in (patch('qq_agent_loop.connect', side_effect=AssertionError('真实连接')),
                       patch('model_gateway.ModelGateway.chat',
                             side_effect=AssertionError('真实模型调用')),
                       # The "worth speaking" judge is another module's work; here it
                       # only has to allow the reply so a send is observable.
                       patch.object(group_decide, 'worth_speaking',
                                    Mock(return_value={'speak': True, 'reason': '值得说'}),
                                    create=True)):
            target.start()
            self.addCleanup(target.stop)

    def payload(self, text='我最近都在夜钓鲫鱼', mid=None, sender='333333333'):
        return {'chat_type': 'group', 'chat_id': self.chat,
                'message_id': mid or 'm-' + uuid.uuid4().hex[:8],
                'sender': {'id': sender, 'display_name': '群友甲'},
                'reply_to': '', 'mentions_bot': False, 'reply_to_bot': False,
                'segments': [{'type': 'text', 'text': text}]}

    def jobs(self):
        with database(self.store.path) as db:
            return [dict(row) for row in db.execute(
                'SELECT scope,source,subject,text,status FROM learning_jobs ORDER BY created')]

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

    # 1
    def test_learn_on_stores_one_job_in_this_group_scope(self):
        self.policy.configure(self.chat, {'learn': True})
        audits = self.run_group(self.payload('我最近都在夜钓鲫鱼', mid='g-mem-1'))
        rows = self.jobs()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['scope'], 'chat:' + self.chat)
        self.assertEqual(rows[0]['source'], 'qq-group:' + self.chat + ':g-mem-1')
        self.assertEqual(rows[0]['subject'], 'qq:333333333')
        self.assertEqual(rows[0]['status'], 'pending')
        self.assertIn('夜钓鲫鱼', rows[0]['text'])
        self.assertNotIn('group_fish_memory_failed', audits)
        # The outbound is history, not a fact about the group: nothing learns its text.
        self.assertEqual([row for row in rows if '好呀' in row['text']], [])
        self.assertEqual([row for row in rows if 'out-' in row['source']], [])

    # 2
    def test_learn_off_writes_nothing_at_all(self):
        self.policy.configure(self.chat, {'learn': False})
        audits = self.run_group(self.payload('我最近都在夜钓鲫鱼', mid='g-mem-2'))
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.loop.channel.sent[0]['segments'],
                         [{'type': 'text', 'text': '好呀，这就说'}])  # the reply still happens
        self.assertNotIn('group_fish_memory_failed', audits)

    # 3
    def test_a_redelivered_message_enqueues_once(self):
        self.policy.configure(self.chat, {'learn': True})
        payload = self.payload('我最近都在夜钓鲫鱼', mid='g-mem-3')
        self.run_group(payload)
        self.run_group(dict(payload))  # the transport redelivers the same message_id
        # ...and even an unconditional second write of the same source stays one job.
        self.loop._fish_remember(self.chat, dict(payload), '我最近都在夜钓鲫鱼')
        self.loop._fish_remember(self.chat, dict(payload), '我最近都在夜钓鲫鱼')
        self.assertEqual(len(self.jobs()), 1, self.jobs())

    # 4
    def test_group_scope_recall_does_not_reach_owner_recall(self):
        self.policy.configure(self.chat, {'learn': True})
        self.run_group(self.payload('我最近都在夜钓鲫鱼', mid='g-recall-1'))
        job = self.jobs()[0]
        # A real save, but without its background vector warm: a daemon thread still
        # holding memory.db would make the temporary root undeletable on Windows.
        with patch('memory_engine.enqueue_warm', return_value=False):
            self.store.save('群友甲最近都在夜钓鲫鱼', category='事件', scope='chat:' + self.chat,
                            source=job['source'], source_text=job['text'], evidence='observed')
        mine = self.store.context('夜钓', scope='chat:' + self.chat)
        self.assertIn('夜钓鲫鱼', mine)
        owner = self.store.context('夜钓', scope='owner')
        self.assertNotIn('夜钓鲫鱼', owner)
        self.assertNotIn('群友甲', owner)

    # 5
    def test_the_enqueue_call_site_can_only_use_this_chat_scope(self):
        tree = ast.parse((ROOT / 'harness/qq_agent_loop.py').read_text(encoding='utf-8'))
        helper = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef) and node.name == '_fish_remember')
        calls = [node for node in ast.walk(helper) if isinstance(node, ast.Call)
                 and getattr(node.func, 'attr', '') == 'enqueue']
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual([kw.arg for kw in call.keywords], ['subject'])
        scope = call.args[2]
        # scope must be exactly 'chat:' + chat, built from the chat under test.
        self.assertIsInstance(scope, ast.BinOp)
        self.assertIsInstance(scope.op, ast.Add)
        self.assertEqual((scope.left.value, scope.right.id), ('chat:', 'chat'))
        literals = {node.value for node in ast.walk(helper)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertNotIn('owner', literals)
        self.assertNotIn('public', literals)
        self.assertFalse([value for value in literals if value.startswith('chat:group:')])

    # 6
    def test_a_memory_failure_is_audited_and_the_reply_still_happens(self):
        self.policy.configure(self.chat, {'learn': True})
        audit = Mock()

        async def go():
            with patch.object(group_decide, 'fish_candidate',
                              Mock(return_value={'engine': 'fish', 'text': '照常回一句', 'tools': []})), \
                    patch.object(memory_store.MemoryStore, 'enqueue',
                                 side_effect=RuntimeError('记忆库不可写')), \
                    patch.object(self.loop.governance, 'audit', audit):
                await self.loop._on_group(self.payload('我最近都在夜钓鲫鱼', mid='g-boom-1'))
                while self.loop.group_tasks:
                    await asyncio.gather(*list(self.loop.group_tasks), return_exceptions=True)
        asyncio.run(go())
        kinds = [call.args[0] for call in audit.call_args_list]
        self.assertIn('group_fish_memory_failed', kinds)
        self.assertIn('记忆库不可写', [call.args[1] for call in audit.call_args_list
                                     if call.args[0] == 'group_fish_memory_failed'][0])
        self.assertEqual([s['segments'] for s in self.loop.channel.sent],
                         [[{'type': 'text', 'text': '照常回一句'}]])


if __name__ == '__main__':
    unittest.main()
