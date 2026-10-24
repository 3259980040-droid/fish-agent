"""/群设置：六个回合参数的读、写与范围校验。

命令改的是群通道真正在用的那一份 GroupPolicy，所以这里的断言只看两件事：
命令回执里说的是不是她要的值，配置里存的数是不是同一个值；以及非法输入有没有
给一句中文说明而不是把异常抛进消息循环。
"""
import asyncio
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]

import dependencies  # noqa: F401
import life_commands
from group_policy import GroupPolicy

OWNER = '10001'
GROUP = '1043230169'
CHAT = 'group:' + GROUP


class FakeGovernance:
    def __init__(self):
        self.audits = []

    def is_owner(self, sender_id, **_kwargs):
        return str(sender_id) == OWNER

    def audit(self, kind, reason, text=''):
        self.audits.append((kind, reason))


class FakeLoop:
    """够 /群设置 用的最小 loop：配置目录、主人编号和一条回复通道。"""

    def __init__(self, root):
        self.root = root
        self.owner = OWNER
        self.group_ids = [GROUP]
        self.governance = FakeGovernance()
        self.sent = []

    async def _deliver_reply(self, chat_id, segments, request_id, reply_to=None):
        self.sent.append({'chat_id': chat_id, 'request_id': request_id,
                          'text': ''.join(str(s.get('text') or '') for s in segments)})


class GroupSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.loop = FakeLoop(self.root)
        self.policy = GroupPolicy(self.root)

    def config(self):
        return GroupPolicy(self.root).config(CHAT)

    def ask(self, text, *, chat_type='private', sender=OWNER):
        payload = {'chat_type': chat_type,
                   'chat_id': 'private:' + OWNER if chat_type == 'private' else CHAT,
                   'sender': {'id': sender}, 'message_id': 'mid-' + uuid.uuid4().hex[:6],
                   'segments': [{'type': 'text', 'text': text}]}
        handled = asyncio.run(life_commands.handle(self.loop, payload, sender))
        return handled, (self.loop.sent[-1] if self.loop.sent else None)

    def reply(self, text):
        handled, sent = self.ask(text)
        self.assertTrue(handled, text)
        self.assertEqual('private:' + OWNER, sent['chat_id'])
        return sent['text']

    # 1
    def test_the_panel_reads_all_six_values(self):
        text = self.reply('/群设置')
        self.assertIn('群 %s 的回合参数' % GROUP, text)
        for expected in ('回合（自主回合节拍）：180秒', '预算（回合读入预算）：1200字',
                         '每小时（每小时判断调用上限）：40次', '每天（每天判断调用上限）：400次',
                         '潜水（单次潜水上限）：240分钟', '潜水全天（一天累计潜水上限）：480分钟'):
            self.assertIn(expected, text)
        self.assertIn('/群设置 回合 180', text)
        # 读的真是群通道那一份配置，不是面板自己编的数。
        self.assertEqual(180, self.config()['round_interval_s'])

    # 2
    def test_each_value_round_trips_and_is_restored(self):
        cases = [('回合', 300, 'round_interval_s', 300), ('预算', 900, 'round_budget_chars', 900),
                 ('每小时', 20, 'round_hourly_calls', 20), ('每天', 120, 'round_daily_calls', 120),
                 ('潜水', 120, 'dive_max_s', 7200), ('潜水全天', 360, 'dive_daily_max_s', 21600)]
        for name, value, key, stored in cases:
            with self.subTest(name=name):
                before = self.config()[key]
                text = self.reply('/群设置 %s %s' % (name, value))
                after = self.config()[key]
                # 配置真的改了，而且换一个新实例（跨进程视角）读到的也是新值。
                self.assertEqual(stored, after)
                self.assertIn(str(value), text)
                self.assertIn('%g' % (before / life_commands.GROUP_SETTINGS[name][6]), text)
                back = self.reply('/群设置 %s %g' % (name, before / life_commands.GROUP_SETTINGS[name][6]))
                self.assertEqual(before, self.config()[key])
                self.assertIn('%g' % (after / life_commands.GROUP_SETTINGS[name][6]), back)
        # 全部还原之后和一开始一模一样。
        self.assertEqual({'round_interval_s': 180, 'round_budget_chars': 1200,
                          'round_hourly_calls': 40, 'round_daily_calls': 400,
                          'dive_max_s': 14400, 'dive_daily_max_s': 28800},
                         {key: self.config()[key] for key in
                          ('round_interval_s', 'round_budget_chars', 'round_hourly_calls',
                           'round_daily_calls', 'dive_max_s', 'dive_daily_max_s')})

    # 3
    def test_an_optional_unit_word_is_accepted(self):
        self.reply('/群设置 潜水 200 分钟')
        self.assertEqual(12000, self.config()['dive_max_s'])
        self.reply('/群设置 回合 180 秒')
        self.assertEqual(180, self.config()['round_interval_s'])
        text = self.reply('/群设置 回合 900 分钟')  # 单位写错了就不是这条命令
        self.assertIn('改法', text)
        self.assertEqual(180, self.config()['round_interval_s'])

    # 4
    def test_illegal_values_are_refused_in_chinese_and_change_nothing(self):
        before = self.config()
        cases = {'/群设置 回合 10': '回合需在 60 到 3600 之间',
                 '/群设置 回合 99999': '回合需在 60 到 3600 之间',
                 '/群设置 预算 abc': '预算要写成 200 到 4000 的整数',
                 '/群设置 潜水 9999': '潜水需在 0 到 1440 之间',
                 '/群设置 每天 -1': '每天要写成 0 到 5000 的整数',
                 '/群设置 频率 3': '/群设置 回合 180',
                 '/群设置 预算': '预算（回合读入预算）'}
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertIn(expected, self.reply(command))
        self.assertEqual(before, self.config())

    # 5
    def test_one_name_only_reads_that_item(self):
        text = self.reply('/群设置 回合')
        self.assertIn('回合（自主回合节拍）：180秒', text)
        self.assertNotIn('潜水全天', text)

    # 6
    def test_the_habits_list_points_at_the_new_command(self):
        text = self.reply('/习惯')
        self.assertIn('/群设置', text)
        self.assertIn('回合', text)

    # 7
    def test_only_the_owner_private_chat_may_use_it(self):
        handled, sent = self.ask('/群设置', chat_type='group', sender='2100000001')
        self.assertFalse(handled)
        self.assertIsNone(sent)
        handled, sent = self.ask('/群设置', sender='2100000001')
        self.assertFalse(handled)
        self.assertIsNone(sent)
        # 主人私聊里只读一项也算命中这条命令，别落到"未知命令"的分支里。
        handled, sent = self.ask('/群设置 回合')
        self.assertTrue(handled)
        self.assertIn('回合（自主回合节拍）', sent['text'])

    # 8
    def test_a_command_hit_is_audited_like_the_other_life_commands(self):
        self.reply('/群设置 回合 240')
        self.assertIn(('life_command', '/群设置'), self.loop.governance.audits)
        self.reply('/群设置 回合 180')


if __name__ == '__main__':
    unittest.main()
