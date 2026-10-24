"""接力点名绕过主动冷却：短话 + 同一个人接着喊，一段接力最多连续放行两次。

六条最小验证，不连网、不调模型、不发真实 QQ 消息：前五条跑群策略自己，第六条
用 fish_judge_daily_limit=0 验判断层那道同名冷却有没有跟着让路。
qq.db 里的入站/出站账由测试直接写（线上是 _fish_record 写的），所以"读的是真账"
这件事和线上同一份代码路径。
"""
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]

from group_policy import GroupPolicy  # noqa: E402 - 需要 harness 在路径上
import group_decide  # noqa: E402 - 判断层那道同名冷却也要验

STAMP = '%Y-%m-%d %H:%M:%S'
# 真实事故那一刻：她在 04:50:53 说完话，04:50:56 起同一个人开始喊她。
T0 = time.mktime(time.strptime('2026-10-23 04:50:53', STAMP))
COOLDOWN = 20


class RelayTests(unittest.TestCase):
    chat = 'group:900000001'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='relay-bypass-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'data/qq').mkdir(parents=True)
        self.store = sqlite3.connect(str(self.root / 'data/qq/qq.db'))
        self.addCleanup(self.store.close)
        self.store.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,chat_id TEXT,'
                           'user_id TEXT,text TEXT,direction TEXT,status TEXT)')
        self.store.commit()
        self.audits = []
        self.policy = GroupPolicy(self.root, audit=lambda kind, reason: self.audits.append((kind, reason)))
        # 冷却 20 秒、合并窗口关掉、别的上限放宽：要验的只有冷却这一道门，别的门不该插手。
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True, 'read': True,
                                          'mode': 'familiar', 'debounce_s': 0,
                                          'active_cooldown_s': COOLDOWN,
                                          'max_active_hour': 8, 'max_unanswered_active': 10})

    # ---------------------------------------------------------------- 铺场景

    def at(self, seconds):
        return T0 + seconds

    def ledger(self, who, text, seconds, direction='inbound', status='observed', chat=None):
        """qq.db 里的一行：入站是别人说的，出站是她真的发出去的。"""
        self.store.execute('INSERT INTO messages(ts,chat_id,user_id,text,direction,status) VALUES(?,?,?,?,?,?)',
                           (time.strftime(STAMP, time.localtime(self.at(seconds))),
                            chat or self.chat, who, text, direction, status))
        self.store.commit()

    def observe(self, who, text, seconds, chat=None):
        payload = {'chat_id': chat or self.chat, 'message_id': 'm-' + uuid.uuid4().hex[:8],
                   'sender': {'id': who}, 'reply_to': '', 'mentions_bot': False,
                   'reply_to_bot': False, 'segments': [{'type': 'text', 'text': text}]}
        return int(self.policy.observe(payload, now=self.at(seconds))['cursor'])

    def hear(self, who, text, seconds, chat=None):
        """一条入站：群自己的账 qq.db 一行，加上群策略的观察窗口。"""
        cursor = self.observe(who, text, seconds, chat=chat)
        self.ledger(who, text, seconds, chat=chat)
        return cursor

    def reply(self, cursor, seconds, chat=None):
        """真的开口一次：预留 -> 通道收下了 -> 结账，冷却从这一刻重新开始。"""
        chat = chat or self.chat
        token = self.policy.reserve(chat, cursor, 'active', now=self.at(seconds))
        self.assertTrue(token, '这一段应该能拿到预留')
        self.policy.finish(token, sent=True, now=self.at(seconds))
        self.ledger('bot', '她回的那一句', seconds, direction='outbound', status='sent', chat=chat)

    def speaks(self, who, text, seconds, chat=None):
        """先像平时那样答她一句，把 20 秒冷却真正启动起来。"""
        self.reply(self.hear(who, text, seconds, chat=chat), seconds, chat=chat)

    def summon(self, who, text, seconds, chat=None):
        """一次召唤：入站落地 -> 判定 -> 放行就真的说出去；返回这一条的判定。"""
        chat = chat or self.chat
        cursor = self.hear(who, text, seconds, chat=chat)
        verdict = self.policy.candidate(chat, now=self.at(seconds))
        if verdict['action'] == 'candidate':
            self.reply(cursor, seconds, chat=chat)
        return verdict

    # ---------------------------------------------------------------- 四条

    def test_two_of_three_relayed_calls_pass_and_the_third_is_cooled(self):
        """同一段接力：前两条短召唤越冷却放行，第三条仍然按冷却沉默（防刷屏）。"""
        self.speaks('111', '先把这句话说完', 0)
        self.assertEqual(self.audits, [])

        first = self.summon('111', '干嘛', 5)
        self.assertEqual((first['action'], first.get('relay')), ('candidate', '干嘛'))
        # 预筛跳过读的就是这一个事实：这一票是接力点名给的（没放行过的游标一律 False）。
        self.assertTrue(self.policy.relay_granted(self.chat, first['cursor']))
        second = self.summon('111', '你现在上号', 8)
        self.assertEqual((second['action'], second.get('relay')), ('candidate', '你现在上号'))

        third = self.summon('111', '来', 11)
        self.assertEqual(third['action'], 'wait')
        self.assertIn('冷却', third['reason'])
        self.assertFalse(self.policy.relay_granted(self.chat, third['cursor']))
        self.assertEqual([kind for kind, _ in self.audits], ['group_relay_bypass'] * 2)
        self.assertIn('干嘛', self.audits[0][1])
        self.assertIn(self.chat, self.audits[0][1])

    def test_a_line_over_ten_words_does_not_pass(self):
        """超过 10 个字的召唤不算"短话"：冷却原样挡着。"""
        long_text = '你现在上号来看看这个啊'
        self.assertEqual(len(long_text), 11)
        self.chat = 'group:900000002'
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True, 'read': True,
                                          'mode': 'familiar', 'debounce_s': 0,
                                          'active_cooldown_s': COOLDOWN,
                                          'max_active_hour': 8, 'max_unanswered_active': 10})
        self.speaks('111', '先把这句话说完', 0)
        verdict = self.summon('111', long_text, 5)
        self.assertEqual(verdict['action'], 'wait')
        self.assertIn('冷却', verdict['reason'])
        self.assertEqual(self.audits, [])

    def test_ten_words_is_still_short_enough(self):
        """边界：去空白后正好 10 个字，仍然算短话。"""
        words = '你现在上号来看看这个'
        self.assertEqual(len(words), 10)
        self.chat = 'group:900000003'
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True, 'read': True,
                                          'mode': 'familiar', 'debounce_s': 0,
                                          'active_cooldown_s': COOLDOWN,
                                          'max_active_hour': 8, 'max_unanswered_active': 10})
        self.speaks('111', '先把这句话说完', 0)
        verdict = self.summon('111', '\t' + words[:4] + ' ' + words[4:] + '\n', 5)
        self.assertEqual(verdict['action'], 'candidate')
        self.assertEqual(verdict.get('relay'), words)

    def test_two_people_calling_is_not_a_relay(self):
        """她说完之后两个人都在喊：这不叫接力，冷却原样挡着。"""
        self.chat = 'group:900000004'
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True, 'read': True,
                                          'mode': 'familiar', 'debounce_s': 0,
                                          'active_cooldown_s': COOLDOWN,
                                          'max_active_hour': 8, 'max_unanswered_active': 10})
        self.speaks('111', '先把这句话说完', 0)
        self.hear('111', '干嘛', 2)
        self.hear('222', '来', 3)
        verdict = self.summon('111', '在吗', 4)
        self.assertEqual(verdict['action'], 'wait')
        self.assertIn('冷却', verdict['reason'])
        self.assertEqual(self.audits, [])

    def test_an_unreadable_ledger_never_bypasses(self):
        """读不到 qq.db 一律不绕：冷却宁可多挡一次，也不凭猜放行。"""
        self.chat = 'group:900000005'
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True, 'read': True,
                                          'mode': 'familiar', 'debounce_s': 0,
                                          'active_cooldown_s': COOLDOWN,
                                          'max_active_hour': 8, 'max_unanswered_active': 10})
        self.speaks('111', '先把这句话说完', 0)
        self.store.close()
        (self.root / 'data/qq/qq.db').unlink()
        self.observe('111', '干嘛', 5)
        verdict = self.policy.candidate(self.chat, now=self.at(5))
        self.assertEqual(verdict['action'], 'wait')
        self.assertIn('冷却', verdict['reason'])
        self.assertEqual(self.audits, [])


class JudgeCooldownTests(unittest.TestCase):
    """判断层那道同名的冷却也得跟着让路，否则策略层放行了等于白放。

    这里不调模型：把"值不值得说"的每日上限设成 0，冷却一放行就会走到
    judge-disabled 而不是 cooldown——两种 reason 的差别就是这道闸门开没开。
    """

    chat = 'group:900000101'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='relay-judge-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'data/qq').mkdir(parents=True)
        self.policy = GroupPolicy(self.root)
        self.policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True,
                                          'active_cooldown_s': COOLDOWN, 'fish_judge_daily_limit': 0})

    def store(self, *statements):
        """直接改群策略那本账：这两行是它自己的表，测试只补状态、不绕过它的判定。"""
        conn = sqlite3.connect(str(self.root / 'data/social/group-policy.db'))
        try:
            for statement in statements:
                conn.execute(statement[0], statement[1])
            conn.commit()
        finally:
            conn.close()

    def sent_now(self):
        """她刚说过一句：判断层用 reservations 里 state='sent' 的最后时刻算冷却。"""
        now = time.time()
        self.store(('INSERT INTO reservations(token,chat,cursor,kind,state,created,finished) '
                    "VALUES(?,?,?,?,?,?,?)", ('t-' + uuid.uuid4().hex[:6], self.chat, 1,
                                              'active', 'sent', now - 1, now)))
        return now

    def bypass_row(self, ts):
        self.store(('INSERT INTO relay_bypass(chat,ts) VALUES(?,?)', (self.chat, ts)))

    def verdict(self):
        payload = {'chat_id': self.chat, 'message_id': 'm-1', 'sender': {'id': '111'},
                   'segments': [{'type': 'text', 'text': '干嘛'}]}
        return group_decide.worth_speaking(self.root, self.chat, payload=payload,
                                           candidate_text='上号干嘛呀，先说清楚嘛', kind='active')

    def test_the_judge_cooldown_stands_down_only_for_an_open_relay(self):
        now = self.sent_now()
        self.assertTrue(self.verdict()['reason'].startswith('cooldown'))
        self.bypass_row(now + 1)
        self.assertEqual(self.verdict()['reason'], 'judge-disabled')
        # 窗口外的旧放行不算：接力已经结束了，冷却照旧。
        self.store(('DELETE FROM relay_bypass', ()),
                   ('INSERT INTO relay_bypass(chat,ts) VALUES(?,?)', (self.chat, now - 600)))
        self.assertTrue(self.verdict()['reason'].startswith('cooldown'))


if __name__ == '__main__':
    unittest.main(verbosity=2)