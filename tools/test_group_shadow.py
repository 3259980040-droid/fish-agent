"""Group shadow decision: engine switch, recorded candidates, and no delivery."""
import asyncio
import hashlib
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from test_social_base import Fixture
from group_policy import GroupPolicy
from group_tests import GroupTests
from social_context import SocialContext
from qq_agent_loop import QQAgentLoop
import group_decide


class ShadowTests(Fixture):
    def setUp(self):
        super().setUp()
        persona = SocialContext(self.root).save_persona('qq', '影子人格', '群里的同一个人格')
        self.revision = persona['id']
        GroupTests(self.root).create(self.group, self.revision, self.owner)
        # A shadow may only read a group the owner still authorises.
        GroupPolicy(self.root).configure(self.chat, {'read': True})
        self.loop = QQAgentLoop(self.root)
        self.loop.channel = Mock(bot={'id': '20001'},
                                 action=AsyncMock(return_value={'ok': True, 'payload': {'message_id': 'out'}}))
        dialogue_observe = patch('group_dialogue.GroupDialogue.observe', new=AsyncMock())
        self.observed = dialogue_observe.start()
        self.addCleanup(dialogue_observe.stop)

    def payload(self, text='现在这句', mid='shadow-1', reply_to=''):
        return {'chat_type': 'group', 'chat_id': self.chat, 'message_id': mid,
                'sender': {'id': '333333333', 'display_name': '群友甲'},
                'reply_to': reply_to, 'mentions_bot': False,
                'segments': [{'type': 'text', 'text': text}]}

    def store(self, text, mid, *, direction='inbound', status='sent', sender='333333333'):
        conversation = self.loop.store.get_or_create_conversation(self.chat, 'qq')
        self.loop.store.record_message(conversation['id'], self.chat, sender, direction, mid,
                                       None, [], text, status)

    def digest(self, *paths):
        return [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths]

    # 1
    def test_default_engine_runs_no_shadow(self):
        self.assertEqual(GroupPolicy(self.root).config(self.chat)['engine'], 'dsh')
        respond, record = Mock(return_value={'text': 'x'}), Mock()

        async def run():
            with patch.object(group_decide, 'respond_as_group', respond), \
                    patch.object(group_decide, 'record_shadow', record):
                await self.loop._on_group(self.payload())
        asyncio.run(run())
        self.assertEqual(self.observed.await_count, 1)
        self.assertEqual((respond.call_count, record.call_count), (0, 0))
        self.assertEqual(group_decide.shadows(self.root, self.chat), [])

    # 2
    def test_shadow_engine_records_candidate_and_no_history_body(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow'})
        self.store('HISTORY_SECRET 之前说过的话', 'old-1')
        self.store('UNSENT_SECRET 没能发出去的', 'old-2', direction='outbound', status='pending')
        respond = Mock(return_value='候选回复')

        async def run():
            with patch('fish_chat.respond', respond):
                await self.loop._on_group(self.payload('现在这句', mid='shadow-2'))
        asyncio.run(run())
        self.assertEqual(respond.call_count, 1)
        options = respond.call_args.kwargs
        self.assertEqual(options['history_source'], 'group-shadow')
        self.assertEqual(options['scope'], 'chat:' + self.chat)
        self.assertEqual(options['session'], 'group-shadow:' + self.chat)
        self.assertEqual(options['origin'], 'group-shadow:' + self.chat)
        self.assertEqual((options['memory'], options['references'], options['tool_context']), ('', '', ''))
        self.assertEqual(options['output'], 'text')
        self.assertEqual(options['persona']['body'], '群里的同一个人格')
        history = json.dumps(options['history'], ensure_ascii=False)
        self.assertIn('HISTORY_SECRET', history)
        self.assertNotIn('UNSENT_SECRET', history)
        self.assertIn('现在这句', json.dumps(options['current'], ensure_ascii=False))
        self.assertIn('群友甲', json.dumps(options['current'], ensure_ascii=False))
        rows = group_decide.shadows(self.root, self.chat)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['engine'], 'shadow')
        self.assertEqual(row['text'], '候选回复')
        self.assertEqual(row['message_id'], 'shadow-2')
        self.assertEqual(row['source'], 'engine-shadow')
        self.assertEqual(row['tools'], [])
        self.assertFalse(row['error'])
        self.assertIsInstance(row['elapsed_ms'], int)
        self.assertNotIn('HISTORY_SECRET', json.dumps(rows, ensure_ascii=False))
        self.assertNotIn('HISTORY_SECRET', json.dumps(row, ensure_ascii=False))

    # 3
    def test_respond_failure_stays_in_the_record_and_old_path_still_runs(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow'})

        async def run():
            with patch('fish_chat.respond', Mock(side_effect=RuntimeError('模型不可用'))):
                await self.loop._on_group(self.payload(mid='shadow-3'))
        asyncio.run(run())
        self.assertEqual(self.observed.await_count, 1)
        rows = group_decide.shadows(self.root, self.chat)
        self.assertEqual(len(rows), 1)
        self.assertIn('RuntimeError', rows[0]['error'])
        self.assertIn('模型不可用', rows[0]['error'])
        self.assertEqual(rows[0]['text'], '')

    # 4
    def test_shadow_reads_qq_and_policy_stores_without_writing(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow'})
        self.store('只读检查用的旧消息', 'old-4')
        watched = (self.root / 'data/qq/qq.db', self.root / 'data/social/group-policy.db')
        before = self.digest(*watched)
        with patch('fish_chat.respond', Mock(return_value='只读候选')):
            plan = group_decide.shadow_ready(self.root, self.chat)
            candidate = group_decide.respond_as_group(self.root, self.chat, payload=self.payload(mid='shadow-4'))
            group_decide.record_shadow(self.root, self.chat, payload=self.payload(mid='shadow-4'),
                                       candidate=candidate)
        self.assertEqual(before, self.digest(*watched))
        self.assertEqual(plan['engine'], 'shadow')
        self.assertEqual(candidate['text'], '只读候选')
        self.assertEqual(group_decide.shadows(self.root, self.chat)[0]['engine'], 'shadow')
        self.assertTrue((self.root / 'data/social/group-shadow.db').is_file())

    # 5
    def test_engine_switch_validates_and_does_not_change_other_switches(self):
        policy = GroupPolicy(self.root)
        self.assertEqual(policy.config(self.chat)['engine'], 'dsh')
        for engine in ('dsh', 'shadow'):
            stored = policy.configure(self.chat, {'engine': engine, 'listen': True, 'reply': True, 'read': True})
            self.assertEqual(stored['engine'], engine)
            current = policy.config(self.chat)
            self.assertEqual(current['engine'], engine)
            self.assertIs(True, current['listen'])
            self.assertIs(True, current['reply'])
            self.assertIs(True, current['read'])
        # engine=fish is a real send engine now, so it is configurable here; it is
        # dispatched by the group loop, not by the shadow block.
        self.assertEqual(policy.configure(self.chat, {'engine': 'fish'})['engine'], 'fish')
        self.assertEqual(policy.config(self.chat)['engine'], 'fish')
        with self.assertRaises(ValueError):
            policy.configure(self.chat, {'engine': 'model'})
        self.assertEqual(policy.configure(self.chat, {'engine': 'shadow'})['engine'], 'shadow')
        self.assertEqual(policy.config(self.chat)['engine'], 'shadow')
        off = policy.configure(self.chat, {'listen': False, 'reply': True, 'engine': 'shadow'})
        self.assertIs(False, off['reply'])
        self.assertIs(False, off['listen'])
        self.assertEqual(off['engine'], 'shadow')

    # 6
    def test_candidate_is_never_delivered(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow'})
        delivered = AsyncMock()

        async def run():
            with patch.object(QQAgentLoop, '_deliver_reply', delivered), \
                    patch('fish_chat.respond', Mock(return_value='这个不该发出去')):
                await self.loop._on_group(self.payload(mid='shadow-6'))
        asyncio.run(run())
        self.assertEqual(delivered.await_count, 0)
        self.assertEqual(self.loop.channel.action.await_count, 0)
        self.assertEqual(group_decide.shadows(self.root, self.chat)[0]['text'], '这个不该发出去')
        # No transport may be importable from the shadow module at all.
        source = (ROOT / 'harness/group_decide.py').read_text(encoding='utf-8')
        for banned in ('send_policy', 'group_dialogue', 'qq_agent_loop', '.action(', '_deliver_reply('):
            self.assertNotIn(banned, source)

    # 7
    def test_quoted_message_enters_current_and_history_with_its_id(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow'})
        self.store('A 的原始问题：滑窗怎么写', 'q-a', sender='444444444')
        payload = self.payload('那这个怎么改', mid='q-b', reply_to='q-a')
        respond = Mock(return_value='候选回复')
        with patch('fish_chat.respond', respond):
            candidate = group_decide.respond_as_group(self.root, self.chat, payload=payload)
        self.assertFalse(candidate.get('error'))
        current = candidate['current']
        self.assertIn('q-a', current)
        self.assertIn('A 的原始问题：滑窗怎么写', current)
        history = json.dumps(candidate['history'], ensure_ascii=False)
        self.assertIn('q-a', history)
        self.assertIn('A 的原始问题：滑窗怎么写', history)
        self.assertEqual(candidate['request_view']['related_history'][0]['reason'], 'explicit_reply_chain')
        # The same quote is handed over as a verifiable reference, not as ''.
        references = respond.call_args.kwargs['references']
        self.assertIn('q-a', references)
        self.assertIn('444444444', references)
        self.assertIn('A 的原始问题：滑窗怎么写', references)
        self.assertIn('q-a', json.dumps(candidate['request_view']['current_request']['quoted_references'],
                                        ensure_ascii=False))

    # 8
    def test_message_outside_the_time_window_is_excluded_with_a_reason(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow'})
        self.store('OLD_SECRET 三小时前的旧话题', 'q-old', sender='333333333')
        stamp = (datetime.now() - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S')
        with closing(sqlite3.connect(self.root / 'data/qq/qq.db')) as db:
            db.execute('UPDATE messages SET ts=? WHERE channel_message_id=?', (stamp, 'q-old'))
            db.commit()
        with patch('fish_chat.respond', Mock(return_value='候选回复')):
            candidate = group_decide.respond_as_group(self.root, self.chat, payload=self.payload(mid='q-new'))
        self.assertNotIn('OLD_SECRET', json.dumps(candidate['history'], ensure_ascii=False))
        self.assertIn({'source': 'in:q-old', 'message_id': 'q-old', 'reason': 'outside_time_window'},
                      candidate['request_view']['excluded'])
        self.assertEqual(candidate['request_view']['window']['excluded_rows'], 1)

    # 9
    def test_projection_is_reused_without_building_a_dialogue_window(self):
        source = (ROOT / 'harness/group_decide.py').read_text(encoding='utf-8')
        self.assertNotIn('GroupHistory(', source)
        self.assertIn('focus_projection(', source)

    # 10
    def test_persona_basis_is_the_owner_private_persona_not_the_test_window(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow'})
        contexts = SocialContext(self.root)
        main = contexts.save_persona('qq', '主人私聊人格', '私聊里的那个人格')
        contexts.set_default('qq', main['id'])
        respond = Mock(return_value='候选回复')
        with patch('fish_chat.respond', respond):
            candidate = group_decide.respond_as_group(self.root, self.chat, payload=self.payload(mid='f4-1'))
        self.assertNotEqual(main['id'], self.revision)
        self.assertEqual(candidate['revision'], main['id'])
        self.assertEqual(candidate['test_window_revision'], self.revision)
        options = respond.call_args.kwargs
        self.assertEqual(options['persona']['id'], main['id'])
        self.assertEqual(options['persona']['body'], '私聊里的那个人格')

    # 11
    def test_owner_selected_private_space_outranks_the_default_persona(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow'})
        contexts = SocialContext(self.root)
        default = contexts.save_persona('qq', '默认人格', '不是主人正在聊的那个')
        contexts.set_default('qq', default['id'])
        live = contexts.save_persona('qq', '主人当前窗口人格', '主人当前在用的人格')
        space = contexts.create_space('qq', revision=live['id'], origin='owner-qq')
        contexts.select_owner_space(space['id'], self.owner)
        respond = Mock(return_value='候选回复')
        with patch('fish_chat.respond', respond):
            candidate = group_decide.respond_as_group(self.root, self.chat, payload=self.payload(mid='f4-2'))
        self.assertEqual(candidate['revision'], live['id'])
        self.assertNotIn(candidate['revision'], (default['id'], self.revision))
        self.assertEqual(respond.call_args.kwargs['persona']['body'], '主人当前在用的人格')
        self.assertEqual(candidate['test_window_revision'], self.revision)


if __name__ == '__main__':
    unittest.main()
