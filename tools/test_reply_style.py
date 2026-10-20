"""Offline checks for the shared reply length and expression baseline."""
import unittest

from group_reply_style import MAX_CHARS, MIN_CHARS, reply_style_prompt


class ReplyStyleTests(unittest.TestCase):
    def test_baseline_contains_length_and_action_rules(self):
        text = reply_style_prompt()
        self.assertIn(f'{MIN_CHARS}–{MAX_CHARS}', text)
        self.assertIn('按本轮问题的复杂程度', text)
        self.assertIn('用户明确指定字数、格式或只需一句时，优先遵从', text)
        self.assertIn('不要每次都加动作', text)
        self.assertIn('普通聊天默认一段连续文本', text)

    def test_shape_is_bounded(self):
        self.assertEqual((MIN_CHARS, MAX_CHARS), (20, 500))


if __name__ == '__main__':
    unittest.main()
