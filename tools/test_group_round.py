"""群的自主回合：看 → 想 → 判 → 做。

九条最小验证，全部跑在假模型和假通道上：不开 websocket、不调真实 API、不发真实 QQ。
① 有话说：一次便宜调用、一条 group_round、字数不超预算、走的是唯一发送链。
② 刹车：最近消息一模一样时第二回合直接跳过，一次调用都不花，但留下一行 skipped。
③ 立刻插队：她说完之后有人接着说话，不等三分钟。
④ 不插队：一条更早的普通消息仍按三分钟节拍等，理由说得清。
⑤ 潜水：写进 dive_until，回合不动，普通消息不开口，@ 仍然回（走回复通道）。
⑥ 潜水上限：当天累计到上限后不再允许潜水。
⑦ 调用配额：这一小时想够了就不再主动想，只应答。
⑧ 判不跳过：worth_speaking 说不值得，就不发。
⑨ 看：图片读成可读行，整段不超预算。
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
from storage import database
from qq_agent_loop import QQAgentLoop
import group_decide
import group_round


class RoundTests(Fixture):
    def setUp(self):
        super().setUp()
        self.policy = GroupPolicy(self.root)
        # 回合验的是"要不要开口"和它的刹车，不是预筛：预筛关掉，生成与判断都换成假的。
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True,
                                          'debounce_s': 0, 'read': True, 'prescreen_enabled': False})
        self.loop = QQAgentLoop(self.root)
        self.channel = FakeChannel()
        self.loop.channel = self.channel
        self.think = Mock(return_value=json.dumps({'decision': 'speak', 'reason': '这句接得上',
                                                   'minutes': 0}, ensure_ascii=False))
        self.judge = Mock(return_value={'speak': True, 'reason': '值得说', 'judge_calls': 1})
        self.generate = Mock(return_value={'engine': 'fish', 'text': '我也这么想', 'tools': [],
                                           'segments': [{'type': 'text', 'text': '我也这么想'}]})
        self.audit = Mock()
        for patcher in (patch('qq_agent_loop.connect', side_effect=AssertionError('真实连接')),
                        patch('model_gateway.ModelGateway.call', self.think),
                        patch.object(group_decide, 'worth_speaking', self.judge, create=True),
                        patch.object(group_decide, 'fish_candidate', self.generate),
                        patch.object(self.loop.governance, 'audit', self.audit)):
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- 夹具 ----
    def feed(self, text='今晚还打吗', direct=False, name='群友甲', segments=None):
        """One inbound message through the real listening store, and its id."""
        mid = 'm-' + uuid.uuid4().hex[:10]
        self.policy.observe({'chat_id': self.chat, 'message_id': mid,
                             'sender': {'id': '333333333', 'display_name': name},
                             'segments': segments or [{'type': 'text', 'text': text}],
                             'mentions_bot': direct, 'reply_to_bot': False})
        return mid

    def payload(self, mid, text='在吗', direct=True):
        return {'chat_type': 'group', 'chat_id': self.chat, 'message_id': mid,
                'sender': {'id': '333333333', 'display_name': '群友甲'}, 'reply_to': '',
                'mentions_bot': direct, 'reply_to_bot': False,
                'segments': [{'type': 'text', 'text': text}]}

    def tick(self):
        return asyncio.run(group_round.tick(self.loop))

    def rounds(self):
        with database(group_round._path(self.root)) as db:
            return [dict(row) for row in
                    db.execute('SELECT * FROM group_round WHERE chat=? ORDER BY id', (self.chat,))]

    def state(self):
        with database(group_round._path(self.root)) as db:
            row = db.execute('SELECT * FROM group_round_state WHERE chat=?', (self.chat,)).fetchone()
            return dict(row) if row else {}

    def audits(self, kind=None):
        items = [call.args for call in self.audit.call_args_list]
        return [args for args in items if kind is None or args[0] == kind]

    def age_round(self, seconds):
        """把上一次真回合推老：节拍与底线都按它的时间算。"""
        with database(group_round._path(self.root)) as db:
            db.execute('UPDATE group_round_state SET at=at-? WHERE chat=?', (seconds, self.chat))

    def age_message(self, mid, seconds):
        """把一条群消息推老：它发生在她说那句话之前，不是"接着说她的"。"""
        with database(group_round._path(self.root)) as db:
            row = db.execute('SELECT seq,data FROM observations WHERE chat=? ORDER BY seq DESC LIMIT 1',
                             (self.chat,)).fetchone()
            data = json.loads(row['data'])
            data['ts'] = float(data.get('ts') or time.time()) - seconds
            db.execute('UPDATE observations SET data=? WHERE chat=? AND seq=?',
                       (json.dumps(data, ensure_ascii=False), self.chat, row['seq']))
        return mid

    # ①
    def test_a_round_thinks_once_and_speaks_through_the_only_send_path(self):
        self.feed('今晚还打吗')
        result = self.tick()
        self.assertEqual('speak', result['decision'])
        self.assertEqual(1, self.think.call_count)           # 每回合最多一次便宜调用
        self.assertEqual(1, self.judge.call_count)           # 判没有被跳过
        self.assertEqual(1, result['spoken'])
        self.assertTrue(self.channel.sent)                   # 走的是唯一发送链
        row = self.rounds()[-1]
        self.assertEqual(('speak', 1, 1), (row['decision'], row['calls'], row['spoken']))
        self.assertTrue(0 < row['chars'] <= 1200)             # 看的预算
        self.assertEqual('interval', row['trigger'])
        self.assertEqual(1, len(self.audits('group_round')))

    # ②
    def test_the_same_messages_skip_the_next_round_without_a_call(self):
        self.feed('今晚还打吗')
        self.tick()
        before = self.think.call_count
        second = self.tick()
        self.assertFalse(second['round'])
        self.assertEqual(before, self.think.call_count)       # 0 调用
        row = self.rounds()[-1]
        self.assertEqual('skipped', row['decision'])
        self.assertEqual(0, row['calls'])
        self.assertIn('一样', row['reason'])
        self.assertIn('没人提她', row['reason'])
        self.assertEqual('skipped', self.state()['decision'])
        audit = self.audits('group_round')[-1][1]
        self.assertIn('skipped', audit)
        self.assertIn('没人提她', audit)
        self.assertIn('调用 0（本小时 1/40，今天 1/400）', audit)  # 预算用量一起留痕

    # ③
    def test_a_line_after_her_own_opens_the_next_round_at_once(self):
        self.feed('今晚还打吗')
        self.tick()
        self.age_round(45)                     # 过了底线，还没到三分钟
        self.feed('那我先开一局')
        second = self.tick()
        self.assertTrue(second['round'])
        self.assertEqual('after-me', second['trigger'])       # 不等定时器
        self.assertEqual(2, self.think.call_count)

    # ④
    def test_a_plain_message_still_waits_for_the_three_minute_rhythm(self):
        self.feed('今晚还打吗')
        self.tick()
        self.age_round(45)
        self.age_message(self.feed('顺便问下昨天那个'), 600)
        waited = self.tick()
        self.assertFalse(waited['round'])
        self.assertIn('不到 180 秒', waited['reason'])
        self.assertEqual(1, self.think.call_count)
        self.age_round(200)
        self.assertTrue(self.tick()['round'])
        self.assertEqual(2, self.think.call_count)

    # ⑤
    def test_a_dive_is_written_and_only_a_mention_still_gets_an_answer(self):
        self.think.return_value = json.dumps({'decision': 'dive', 'reason': '他们在吵，我不插手',
                                              'minutes': 20}, ensure_ascii=False)
        self.feed('你们别吵了')
        began = time.time()
        result = self.tick()
        self.assertEqual('dive', result['decision'])
        dive = float(self.policy.config(self.chat)['dive_until'])
        self.assertTrue(began + 1150 <= dive <= time.time() + 1200)
        self.assertIn('潜 20 分钟', self.rounds()[-1]['reason'])
        # 回合在潜水期间不动
        calls = self.think.call_count
        quiet = self.tick()
        self.assertFalse(quiet['round'])
        self.assertIn('潜水到', quiet['reason'])
        self.assertEqual(calls, self.think.call_count)
        # 普通消息不开口
        self.feed('哈哈')
        self.assertEqual('silent', self.policy.candidate(self.chat)['action'])
        self.assertIn('潜水到', self.policy.candidate(self.chat)['reason'])
        # @ 仍然回：direct 不受潜水限制，回复通道的门是开的……
        self.feed('@大肥鱼 你说句话', direct=True)
        self.assertEqual('candidate', self.policy.candidate(self.chat)['action'])
        # ……而且真的发得出去（_fish_reply 自己 observe，所以这里给一条没听过的消息）
        asyncio.run(self.loop._fish_reply(self.payload('m-' + uuid.uuid4().hex[:10]), self.chat))
        self.assertTrue(self.channel.sent)
        self.assertIn('我也这么想', json.dumps(self.channel.sent, ensure_ascii=False))

    # ⑥
    def test_the_daily_dive_budget_refuses_another_dive(self):
        cap = int(self.policy.config(self.chat)['dive_daily_max_s'])
        day = time.strftime('%Y-%m-%d', time.localtime())
        with database(group_round._path(self.root)) as db:
            for statement in group_round.SCHEMA:
                db.execute(statement)
            db.execute('INSERT INTO group_dive(chat,day,seconds) VALUES(?,?,?)',
                       (self.chat, day, float(cap - 60)))
        self.think.return_value = json.dumps({'decision': 'dive', 'reason': '插不上话',
                                              'minutes': 30}, ensure_ascii=False)
        self.feed('你们继续')
        result = self.tick()
        self.assertEqual('silent', result['decision'])        # 想潜水，被当天上限挡住
        self.assertIn('不能潜水', result['reason'])
        self.assertEqual(0.0, float(self.policy.config(self.chat)['dive_until']))
        self.assertEqual(cap - 60, group_round.dived_today(self.root, self.chat))

    # ⑦
    def test_the_hourly_call_budget_only_answers(self):
        now = time.time()
        for _ in range(40):
            group_round.record(self.root, self.chat, now, trigger='interval', decision='silent',
                               reason='配额测试', calls=1, chars=10, cursor=0)
        self.feed('今晚还打吗')
        result = self.tick()
        self.assertFalse(result['round'])
        self.assertIn('这一小时已经想够了', result['reason'])
        self.assertEqual(0, self.think.call_count)
        self.assertEqual('skipped', self.rounds()[-1]['decision'])

    # ⑧
    def test_the_judge_is_still_asked_before_she_speaks(self):
        self.judge.return_value = {'speak': False, 'reason': '别人在聊自己的事', 'judge_calls': 1}
        self.feed('今晚还打吗')
        result = self.tick()
        self.assertEqual('speak', result['decision'])         # 她的决定记下来了
        self.assertEqual(0, result['spoken'])                 # 但没发出去
        self.assertIn('别人在聊自己的事', result['reason'])
        self.assertFalse(self.channel.sent)
        self.assertEqual(1, self.think.call_count)            # 便宜调用仍只有一次

    # ⑨
    def test_the_look_fits_its_budget_and_reads_pictures(self):
        self.feed('今晚还打吗')
        self.feed('', segments=[{'type': 'image', 'ref': 'asset-1'}])
        view = group_round.survey(self.root, self.chat)
        text = group_round.look(self.root, self.chat, view)
        self.assertLessEqual(len(text), 1200)
        self.assertIn('[图片]', text)
        self.assertIn('距我上次开口', text)
        self.assertIn('群友甲', text)
        # 预算真的会截断：把预算压到很小，只留下最旧的那几条之外的部分
        self.policy.configure(self.chat, {'round_budget_chars': 200})
        tight = group_round.look(self.root, self.chat,
                                 group_round.survey(self.root, self.chat))
        self.assertLessEqual(len(tight), 200)


if __name__ == '__main__':
    unittest.main()
