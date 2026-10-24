"""她的话只有调用发送工具才会出去：没调用就一条不发，调用了就按顺序发。

全程 FakeChannel 记账：不打开 websocket、不连模型、不发真实 QQ。这一组验的是
"她的输出不再等于消息"这件事本身，不是判断层，所以预筛关掉、worth_speaking 换成
一个固定的"值得说"，判断层另有 test_group_prescreen / test_worth_speaking。
"""
import asyncio
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from test_social_base import Fixture
from test_owner_private import OwnerPrivateFixture, said, stopped, spoken, tool_call
from governance import Governance
from group_policy import GroupPolicy
from group_tests import GroupTests
from model_gateway import ChatResult
from qq_agent_loop import QQAgentLoop
from social_context import SocialContext
from storage import database
from emoji_store import EmojiStore
from PIL import Image
import group_decide


class FakeChannel:
    """The only transport these tests may reach; it records instead of sending."""

    def __init__(self, receipt=None):
        self.bot = {'id': '20001'}
        self.sent = []
        self.receipt = receipt

    async def action(self, name, args, timeout=0):
        self.sent.append({'name': name, **args})
        if self.receipt:
            return self.receipt(len(self.sent) - 1)
        return {'ok': True, 'payload': {'message_id': 'out-' + str(len(self.sent))}}


def audits(kind=None):
    """Capture every audit row of one test: her decisions have to be readable."""
    calls = Mock()
    return calls


class GroupTurn(Fixture):
    """一个群的生成与发送架子；用例放在下面，真模型彩排只借用这个架子。"""

    # 这一组验的是"她的话怎么出去"，判断层和审计各自另有主：默认把它们换成替身，
    # 真模型彩排（RealModelRehearsal）才换成真的。
    JUDGE = True
    MOCK_AUDIT = True

    def setUp(self):
        super().setUp()
        contexts = SocialContext(self.root)
        self.revision = contexts.save_persona('qq', '主人私聊人格', '主人私聊里的 Fish')['id']
        contexts.set_default('qq', self.revision)
        GroupTests(self.root).create(self.group, self.revision, self.owner)
        self.policy = GroupPolicy(self.root)
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True,
                                          'debounce_s': 0, 'read': True, 'prescreen_enabled': False})
        self.loop = QQAgentLoop(self.root)
        self.channel = FakeChannel()
        self.loop.channel = self.channel
        for target in (patch('qq_agent_loop.connect', side_effect=AssertionError('真实连接')),):
            target.start()
            self.addCleanup(target.stop)
        observe = patch('group_dialogue.GroupDialogue.observe', new=AsyncMock())
        self.observed = observe.start()
        self.addCleanup(observe.stop)
        if self.JUDGE:
            judge = patch.object(group_decide, 'worth_speaking',
                                 Mock(return_value={'speak': True, 'reason': '值得说',
                                                    'judge_calls': 1}), create=True)
            judge.start()
            self.addCleanup(judge.stop)
        self.audit = Mock()
        if self.MOCK_AUDIT:
            audit = patch.object(Governance, 'audit', self.audit)
            audit.start()
            self.addCleanup(audit.stop)
        image = self.root / 'fixture.png'
        Image.new('RGB', (24, 24), 'blue').save(image)
        self.asset = EmojiStore(self.root).add(image, '蓝色表情')['id']

    def model(self, *turns):
        """Pin the model's turns; a turn that already called a tool just ends."""
        queue = list(turns)

        def answer(messages, **kwargs):
            if spoken(messages):
                return stopped()
            return queue.pop(0) if len(queue) > 1 else queue[0]
        client = MagicMock(base='https://api.deepseek.com', model='test-model',
                           chat=Mock(side_effect=answer))
        started = patch('model_gateway.ModelGateway', MagicMock(return_value=client))
        started.start()
        self.addCleanup(started.stop)

    def payload(self, text='这句怎么回', mid=None):
        return {'chat_type': 'group', 'chat_id': self.chat,
                'message_id': mid or 'm-' + uuid.uuid4().hex[:8],
                'sender': {'id': '333333333', 'display_name': '群友甲'},
                'reply_to': '', 'mentions_bot': False, 'reply_to_bot': False,
                'segments': [{'type': 'text', 'text': text}]}

    async def settle(self):
        while self.loop.group_tasks:
            await asyncio.gather(*list(self.loop.group_tasks), return_exceptions=True)

    def turn(self, mid):
        async def go():
            await self.loop._on_group(self.payload(mid=mid))
            await self.settle()
        asyncio.run(go())

    def kinds(self):
        return [call.args[0] for call in self.audit.call_args_list]

    def reasons(self, kind):
        return [call.args[1] for call in self.audit.call_args_list if call.args[0] == kind]

    def texts(self):
        return [segment.get('text') for sent in self.channel.sent
                for segment in sent['segments'] if segment.get('type') == 'text']

class GroupSending(GroupTurn):
    # ① 她写了一整段，但没调用任何发送工具
    def test_a_turn_that_calls_no_sending_tool_sends_nothing(self):
        self.model(stopped('这句我想了想，还是别说了，群里现在在聊别的。'))
        self.turn('silent-1')
        self.assertEqual(self.channel.sent, [])
        self.assertIn('turn_no_send', self.kinds())
        self.assertIn('group_fish_no_send', self.kinds())
        # 她当时在想什么留得住：审计里记着她的思考开头
        self.assertIn('这句我想了想', ''.join(self.reasons('turn_no_send')))
        self.assertEqual(self.state(), ['skipped'])

    # ② 她调用 fish_say，两句就是两条，顺序不变
    def test_two_submitted_bubbles_leave_as_two_ordered_messages(self):
        self.model(said('草', '笑死'))
        self.turn('said-1')
        self.assertEqual(self.texts(), ['草', '笑死'])
        self.assertEqual(len({sent['idempotency_key'] for sent in self.channel.sent}), 2)
        self.assertEqual(self.channel.sent[0]['reply_to'], 'said-1')
        self.assertNotIn('reply_to', self.channel.sent[1])
        self.assertEqual(self.state(), ['sent'])
        said_row = ''.join(self.reasons('turn_said'))
        self.assertIn('2条', said_row)
        self.assertIn('草笑死', said_row)

    # ① 用户裁决：她明确调了发送工具，判断层不得再否决
    def test_her_own_send_is_not_judged_even_when_the_judge_says_no(self):
        judge = Mock(return_value={'speak': False, 'reason': '别人已经答了'})
        self.model(said('草', '笑死'))
        with patch.object(group_decide, 'worth_speaking', judge, create=True):
            self.turn('said-guard')
        self.assertEqual(self.texts(), ['草', '笑死'])
        self.assertEqual(judge.call_count, 0)
        self.assertIn('group_fish_said_by_herself', self.kinds())
        self.assertNotIn('group_fish_silent', self.kinds())

    # 她的理由进审计，不进群里
    def test_the_reason_she_gave_for_speaking_stays_in_the_audit(self):
        self.model(ChatResult('', {}, 'tool_calls',
                              (tool_call('fish_say', {'texts': ['蓝色大肥鱼在此'],
                                                      'reason': '被点名了，得应一声'}),)))
        self.turn('why-1')
        self.assertEqual(self.texts(), ['蓝色大肥鱼在此'])
        row = ''.join(self.reasons('group_fish_said_by_herself'))
        self.assertIn('被点名了', row)
        self.assertNotIn('被点名了', ''.join(self.texts()))

    # ② 表情走同一条通道，单张 image
    def test_a_sticker_leaves_as_its_own_image_segment(self):
        self.model(ChatResult('', {}, 'tool_calls',
                              (tool_call('fish_send_sticker', {'emoji_id': self.asset}),)))
        self.turn('sticker-1')
        self.assertEqual([sent['segments'] for sent in self.channel.sent],
                         [[{'type': 'image', 'asset_id': self.asset}]])
        self.assertIn('turn_sticker', self.kinds())

    # ② 文字加表情同轮：先文字后表情
    def test_words_and_a_sticker_keep_the_order_she_gave_them(self):
        self.model(ChatResult('', {}, 'tool_calls',
                              (tool_call('fish_say', {'texts': ['先说这一句']}),
                               tool_call('fish_send_sticker', {'emoji_id': self.asset}, 'call-2'))))
        self.turn('both-1')
        self.assertEqual([sent['segments'] for sent in self.channel.sent],
                         [[{'type': 'text', 'text': '先说这一句'}],
                          [{'type': 'image', 'asset_id': self.asset}]])

    # 契约还在：fish_say 只收 1 至 3 项
    def test_fish_say_refuses_more_than_three_bubbles(self):
        self.model(ChatResult('', {}, 'tool_calls',
                              (tool_call('fish_say', {'texts': ['一', '二', '三', '四']}),)))
        self.turn('over-1')
        self.assertEqual(self.channel.sent, [])
        self.assertIn('turn_no_send', self.kinds())

    def state(self):
        with database(self.policy.path) as db:
            return [row['state'] for row in
                    db.execute('SELECT state FROM reservations ORDER BY cursor')]

    def rows(self):
        """The audit trail as it was actually written, not as this test remembers it."""
        with database(self.root / 'data/qq/governance.db') as db:
            return [dict(row) for row in db.execute('SELECT ts,kind,reason FROM audit ORDER BY rowid')]


class PublishedTools(Fixture):
    """一张表对所有面公布，能不能发由执行器按场合答：窗口不该有第二张能力表。"""

    def test_every_surface_is_offered_the_same_table(self):
        from fish_chat import tool_definitions, CATALOGUES
        for context in ('group', 'owner-private', 'owner-desktop'):
            with self.subTest(context=context):
                names = [definition['function']['name']
                         for definition in tool_definitions('qq', context)]
                self.assertEqual(set(CATALOGUES['qq']), set(names))
                self.assertIn('fish_say', names)
                self.assertIn('fish_send_sticker', names)

    def test_a_window_without_a_sender_is_refused_in_chinese(self):
        from tool_host import ToolHost
        host = ToolHost(self.root)
        host.bind('qq', 'desk-1', 'owner', self.owner, True)
        with self.assertRaises(PermissionError) as caught:
            host.execute('qq', 'desk-1', 'fish_say', {'texts': ['这里没有通道']}, 'say-1')
        self.assertIn('发送工具只在主人私聊和群聊里可用', str(caught.exception))


class OwnerSending(OwnerPrivateFixture):
    def model(self, *turns):
        queue = list(turns)

        def answer(messages, **kwargs):
            self.requests.append(messages)
            if spoken(messages):
                return stopped()
            return queue.pop(0) if len(queue) > 1 else queue[0]
        self.gateway.chat.side_effect = answer

    def sent_texts(self):
        return [segment.get('text') for call in self.loop.channel.action.call_args_list
                for segment in call.args[1]['segments'] if segment.get('type') == 'text']

    def kinds(self, audit):
        return [call.args[0] for call in audit.call_args_list]

    # ① 主人私聊：她这一轮没调用发送工具，就一个气泡也不发
    def test_a_quiet_private_turn_sends_nothing_and_is_not_a_failure(self):
        audit = Mock()
        self.model(stopped('他刚才那句话我其实不太想接。'))
        item = self.item('在吗')
        with patch.object(Governance, 'audit', audit):
            asyncio.run(self.loop._process(item))
        self.loop.channel.action.assert_not_called()
        self.assertIn('turn_no_send', self.kinds(audit))
        self.assertIn('不太想接', ''.join(call.args[1] for call in audit.call_args_list
                                        if call.args[0] == 'turn_no_send'))
        with database(self.loop.store.db_path) as db:
            self.assertEqual(db.execute('SELECT status FROM messages WHERE id=?',
                                        (item['message_id'],)).fetchone()[0], 'processed')
            self.assertEqual(db.execute('SELECT status FROM runtime_turns WHERE conversation_id=?',
                                        (item['conversation']['id'],)).fetchone()[0], 'silent')
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE direction='outbound'")
                             .fetchone()[0], 0)

    # ② 主人私聊：她调用 fish_say 的两句各自成了一条消息
    def test_private_bubbles_leave_in_the_order_she_submitted_them(self):
        self.model(said('在的', '刚在发呆'))
        asyncio.run(self.loop._process(self.item('在吗')))
        self.assertEqual(self.sent_texts(), ['在的', '刚在发呆'])
        with database(self.loop.store.db_path) as db:
            rows = [dict(row) for row in db.execute(
                "SELECT text,status,channel_message_id FROM messages WHERE direction='outbound' ORDER BY id")]
        self.assertEqual([row['text'] for row in rows], ['在的', '刚在发呆'])
        self.assertEqual([row['status'] for row in rows], ['sent', 'sent'])

    # 契约还在：第二轮再补第 4 条会被拒绝，已经说出去的三条不受影响
    def test_a_fourth_bubble_is_refused_inside_the_turn(self):
        self.gateway.chat.side_effect = [
            ChatResult('', {}, 'tool_calls', (tool_call('fish_say', {'texts': ['一', '二', '三']}),)),
            ChatResult('', {}, 'tool_calls', (tool_call('fish_say', {'texts': ['四']}, 'call-2'),)),
            stopped()]
        asyncio.run(self.loop._process(self.item('说三句')))
        self.assertEqual(self.sent_texts(), ['一', '二', '三'])

    # 没有备注、又没看过的表情不许发
    def test_an_unpreviewed_sticker_without_a_note_is_refused(self):
        image = self.root / 'plain.png'
        Image.new('RGB', (24, 24), 'red').save(image)
        asset = EmojiStore(self.root).add(image, '')['id']
        self.model(ChatResult('', {}, 'tool_calls',
                              (tool_call('fish_send_sticker', {'emoji_id': asset}),)))
        asyncio.run(self.loop._process(self.item('发个表情')))
        self.loop.channel.action.assert_not_called()


LIVE_KEY = 'FISH_QQ_DEEPSEEK_API_KEY'
LIVE_OPT_IN = 'FISH_RUN_LIVE_REHEARSAL'


@unittest.skipUnless(os.environ.get(LIVE_OPT_IN) == '1' and os.environ.get(LIVE_KEY),
                     '真模型彩排需显式设置 FISH_RUN_LIVE_REHEARSAL=1 并提供 ' + LIVE_KEY)
class RealModelRehearsal(GroupTurn):
    """③ 真模型彩排：同一句入站的"群里有人喊蓝色大肥鱼"，看她选择说还是不说。

    临时根 + FakeChannel + 真模型（不 patch ModelGateway）：不碰真实 QQ，也不碰真实
    数据目录。仅 FISH_RUN_LIVE_REHEARSAL=1 且有密钥时运行，持有密钥不会默认触发。
    密钥只从环境变量来，不落盘、不写进报告。
    """

    JUDGE = False
    MOCK_AUDIT = False
    QUESTION = '群里有人喊蓝色大肥鱼'

    def setUp(self):
        super().setUp()
        live = Path(os.environ.get('FISH_HARNESS_HOME') or ROOT)
        # 用和线上同一份模型配置，临时根才有同一只 Fish；密钥仍走环境变量。
        for name in ('harness/agents.json', 'harness/control.json'):
            source = live / name
            if source.is_file():
                (self.root / name).parent.mkdir(parents=True, exist_ok=True)
                (self.root / name).write_text(source.read_text(encoding='utf-8'), encoding='utf-8')

    def rows(self):
        """The audit trail of this rehearsal, read from the temp root it wrote to."""
        with database(self.root / 'data/qq/governance.db') as db:
            return [dict(row) for row in db.execute('SELECT ts,kind,reason FROM audit ORDER BY rowid')]

    def speak(self, text, mid):
        async def go():
            await self.loop._on_group(self.payload(text, mid=mid))
            await self.settle()
        asyncio.run(go())

    def report(self, label, text):
        self.speak(text, label)
        row = {'inbound': text,
               'sent': [sent['segments'] for sent in self.channel.sent],
               'audit': [item for item in self.rows()
                         if item['kind'] in ('turn_said', 'turn_sticker', 'turn_no_send',
                                             'group_fish_silent', 'group_fish_no_send',
                                             'group_fish_failed', 'group_fish_timeout',
                                             'group_fish_said_by_herself')]}
        print(json.dumps(row, ensure_ascii=False, indent=1))
        return row

    def test_her_own_decision_is_what_the_channel_sees(self):
        row = self.report('rehearsal-1', self.QUESTION)
        if not self.channel.sent:
            # 没发就是她（或判断层）没开口：必须有一条说清楚原因的行，不能是哑的。
            self.assertTrue({'turn_no_send', 'group_fish_silent', 'group_fish_no_send'}
                            & {item['kind'] for item in row['audit']})
        else:
            # 真发了：这句话是她自己调发送工具交出来的，判断层没有否决过它。
            self.assertIn('turn_said', {item['kind'] for item in row['audit']})
            self.assertIn('group_fish_said_by_herself', {item['kind'] for item in row['audit']})

    def test_a_turn_where_she_calls_no_sending_tool_sends_nothing(self):
        # 同一句入站，另加一句彩排说明：这一轮她只是听着。真模型照旧自己决定不调工具，
        # 走的是同一条 turn_no_send 的路——0 条，也有一条说得清原因的审计。
        row = self.report('rehearsal-2', self.QUESTION
                          + '\n[彩排说明：这一轮你只是在听，不用发言，也不要调用任何发送工具]')
        self.assertEqual(self.channel.sent, [])
        kinds = {item['kind'] for item in row['audit']}
        self.assertIn('turn_no_send', kinds)
        self.assertIn('group_fish_no_send', kinds)
        self.assertNotIn('group_fish_silent', kinds)
