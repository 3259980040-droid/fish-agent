"""主动开口走发送工具：她想开口才发，想了不发就一条都不发。

这个窗口原来把她的正文直接当消息发。现在它和群回复窗口一样绑了工具身份，所以
她的开场白也要调用 fish_say 才会出去。这一组盯三件事：
  ① 没调用发送工具 → 0 条，审计写清"想了不发"；
  ② 调用了 → 那几句话原样发出去，她的理由进审计；
  ③ 绑定放宽了哪些判断：本群范围的工具可用，潜水/账号/主人/跨群照旧拒绝。
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from test_owner_private import stopped, tool_call
from test_turn_sending import FakeChannel, GroupTurn
from model_gateway import ChatResult
from storage import database
import group_proactive


def said_with_reason(*texts, reason=''):
    """一次真的开口：fish_say 带着她自己的理由。"""
    arguments = {'texts': list(texts)}
    if reason:
        arguments['reason'] = reason
    return ChatResult('', {}, 'tool_calls', (tool_call('fish_say', arguments),))


class ProactiveSending(GroupTurn):
    """她这一轮要不要开口，由她自己调不调发送工具决定。"""

    JUDGE = False

    def open(self):
        async def go():
            return await group_proactive._open(self.loop, self.chat, {'quiet_s': 3600.0})
        return asyncio.run(go())

    def identity(self, session):
        with database(self.root / 'data/tool-host.db') as db:
            row = db.execute('SELECT * FROM identities WHERE role=? AND session=?',
                             ('qq', session)).fetchone()
        return dict(row) if row else {}

    # ① 想了不发
    def test_a_draft_she_never_sends_is_no_message_at_all(self):
        self.model(stopped('这个群安静半天了，我也没什么想说的。'))
        outcome = self.open()
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(outcome['reason'], 'declined')
        self.assertIn('turn_no_send', self.kinds())
        self.assertIn('group_proactive_declined', self.kinds())
        self.assertIn('想了不发', ''.join(self.reasons('group_proactive_declined')))

    # ② 说了就一条，原话不加工，理由只进审计
    def test_her_draft_leaves_through_the_same_send_tool(self):
        self.model(said_with_reason('都睡着了？那我先说一句', reason='安静太久了，起个头'))
        outcome = self.open()
        self.assertEqual(self.texts(), ['都睡着了？那我先说一句'])
        self.assertTrue(outcome['sent'])
        self.assertIn('group_proactive_sent', self.kinds())
        self.assertIn('安静太久了', ''.join(self.reasons('group_proactive_sent')))
        self.assertNotIn('安静太久了', ''.join(self.texts()))

    # ③ 绑定：这个窗口确实是这个群的会话，而且放宽有审计
    def test_the_draft_window_is_bound_to_this_group_and_the_widening_is_audited(self):
        self.model(said_with_reason('说一句'))
        self.open()
        row = self.identity(group_proactive.SESSION % self.chat)
        self.assertEqual((row.get('scope'), row.get('owner')), ('chat:' + self.chat, 0))
        self.assertIn('tool.identity.proactive', self.kinds())
        self.assertIn('放宽点', ''.join(self.reasons('tool.identity.proactive')))


class DraftSurface(unittest.TestCase):
    """草稿窗口是这个群的群面，不是这个账号的面。"""

    def setUp(self):
        from test_social_base import Fixture
        self.fixture = Fixture(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.chat = self.fixture.chat
        self.owner = self.fixture.owner

    def test_the_surface_is_the_group_and_nothing_else(self):
        from tool_host import ToolHost
        host = ToolHost(self.root)
        session = group_proactive.SESSION % self.chat
        host.bind('qq', session, 'chat:' + self.chat, '')
        self.assertEqual(host.surface(host.identity('qq', session), session, 'qq'), 'group')
        # 发话可以，越界照旧拒绝：潜水、账号开关、跨群代发都不认这个会话。
        for name, arguments in (('fish_group_dive', {'minutes': 10}),
                                ('fish_self_control', {'action': 'pause'}),
                                ('fish_group_send', {'chat_id': self.chat, 'text': '代发'})):
            with self.subTest(name=name):
                with self.assertRaises(PermissionError):
                    host.execute('qq', session, name, arguments, 'call-' + name)

    def test_a_staged_bubble_is_not_sent_by_the_draft_itself(self):
        from tool_host import ToolHost, take_said
        host = ToolHost(self.root)
        session = group_proactive.SESSION % self.chat
        host.bind('qq', session, 'chat:' + self.chat, '')
        host.execute('qq', session, 'fish_say', {'texts': ['草稿一句'], 'reason': '想说'}, 'c1')
        # 队列只说明她决定了说什么；发不发由 _open 那唯一的发送链决定。
        self.assertEqual(take_said(self.root, session), {'said': 1, 'reason': '想说'})
        self.assertEqual(take_said(self.root, session), {'said': 0, 'reason': ''})


LIVE_KEY = 'FISH_QQ_DEEPSEEK_API_KEY'


@unittest.skipUnless(os.environ.get('FISH_RUN_LIVE_REHEARSAL') == '1' and os.environ.get(LIVE_KEY),
                     '真模型彩排需显式设置 FISH_RUN_LIVE_REHEARSAL=1 并提供 ' + LIVE_KEY)
class RealModelProactive(GroupTurn):
    """③ 真模型彩排：同一套主动开口，她选择说就是 1 条，选择不说就是 0 条。"""

    JUDGE = False
    MOCK_AUDIT = False

    def setUp(self):
        super().setUp()
        live = Path(os.environ.get('FISH_HARNESS_HOME') or ROOT)
        for name in ('harness/agents.json', 'harness/control.json'):
            source = live / name
            if source.is_file():
                (self.root / name).parent.mkdir(parents=True, exist_ok=True)
                (self.root / name).write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
        self.audit_rows = []

    def rows(self):
        with database(self.root / 'data/qq/governance.db') as db:
            return [dict(row) for row in db.execute('SELECT ts,kind,reason FROM audit ORDER BY rowid')]

    def open(self, prompt):
        async def go():
            with patch.object(group_proactive, 'PROMPT', prompt):
                return await group_proactive._open(self.loop, self.chat, {'quiet_s': 3600.0})
        return asyncio.run(go())

    def test_a_real_turn_that_says_nothing_sends_nothing(self):
        outcome = self.open('【彩排】这个群已经安静了 %d 分钟。这一轮你不想说话：'
                            '只思考，不要调用任何发送工具。')
        rows = self.rows()
        print(json.dumps({'sent': [sent['segments'] for sent in self.channel.sent],
                          'outcome': outcome,
                          'audit': [row for row in rows if row['kind'].startswith('group_proactive')
                                    or row['kind'] in ('turn_no_send', 'turn_said')]},
                         ensure_ascii=False, indent=1))
        self.assertEqual(self.channel.sent, [])
        kinds = {row['kind'] for row in rows}
        self.assertIn('turn_no_send', kinds)
        self.assertIn('group_proactive_declined', kinds)

    def test_a_real_turn_that_speaks_sends_exactly_what_she_submitted(self):
        outcome = self.open('【彩排】这个群已经安静了 %d 分钟。现在主动说一句话，'
                            '用发送工具把它交出去，不要写别的东西。')
        rows = self.rows()
        print(json.dumps({'sent': [sent['segments'] for sent in self.channel.sent],
                          'outcome': outcome,
                          'audit': [row for row in rows if row['kind'].startswith('group_proactive')
                                    or row['kind'] in ('turn_no_send', 'turn_said')]},
                         ensure_ascii=False, indent=1))
        if not outcome.get('sent'):
            self.skipTest('这次真模型选择不说：' + str(outcome.get('reason')))
        self.assertGreaterEqual(len(self.channel.sent), 1)
        kinds = {row['kind'] for row in rows}
        self.assertIn('turn_said', kinds)
        self.assertIn('group_proactive_sent', kinds)


if __name__ == '__main__':
    unittest.main(verbosity=2)
