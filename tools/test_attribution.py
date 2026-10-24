"""Attribution: every group line carries who spoke and who it was said to.

The failing case these tests pin down is a real one, group 1043230169:
04:49:54 the owner wrote "不陪我我恨你" with an @ on 撕咬, and at 04:50:42, asked
why her logic was tangled, she answered "恨我就恨我" — the earlier "你" had come
back to her as a complaint about herself. Nothing in the prompt said who that
"你" had been said to: the rebuilt history row had dropped its at segment, and a
later turn without a quote got no references block at all.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from test_social_base import Fixture
from qq_store import QQStore
from social_context import SocialContext
from social_roster import SocialRoster
from storage import read_json, write_json
import group_decide

OWNER = '3259980040'
BITE = '3497685841'
OTHER = '1834434041'
BOT = '1417334937'
GROUP = '1043230169'
CHAT = 'group:' + GROUP
BOT_NAME = '大肥鱼'


class AttributionTests(Fixture):
    def setUp(self):
        super().setUp()
        config = read_json(self.root / 'channels/fish_qq/config.json')
        config['bot'] = {'id': 'unknown', 'display_name': BOT_NAME}
        write_json(self.root / 'channels/fish_qq/config.json', config)
        SocialRoster(self.root).configure(GROUP)
        SocialRoster(self.root).sync_members(GROUP, [
            {'user_id': BOT, 'nickname': BOT_NAME, 'card': BOT_NAME},
            {'user_id': BITE, 'nickname': '撕咬', 'card': '撕咬'},
            {'user_id': OTHER, 'nickname': '有纪宁', 'card': '有纪宁'},
            {'user_id': OWNER, 'nickname': 'Eternal清涩', 'card': 'Eternal清涩'}])
        self.store = QQStore(self.root)
        self.conversation = self.store.get_or_create_conversation(CHAT, 'qq')['id']

    def say(self, text, mid, sender=OWNER, *, at='', reply_to=None, direction='inbound',
            status='sent'):
        segments = ([{'type': 'at', 'user_id': at}] if at else []) + [{'type': 'text', 'text': text}]
        self.store.record_message(self.conversation, CHAT, sender, direction, mid, reply_to,
                                  segments, text, status)

    def payload(self, text, mid, sender=OWNER, *, at='', reply_to=''):
        return {'chat_type': 'group', 'chat_id': CHAT, 'message_id': mid,
                'sender': {'id': sender, 'display_name': ''}, 'reply_to': reply_to,
                'mentions_bot': bool(at) and at == BOT,
                'segments': ([{'type': 'at', 'user_id': at}] if at else [])
                            + [{'type': 'text', 'text': text}]}

    def focus(self, text, mid, **kwargs):
        names = group_decide._names(self.root, CHAT)
        payload = self.payload(text, mid, **kwargs)
        return group_decide._focus(self.root, CHAT, payload, names) + (names, payload)

    def notes(self, text, mid, **kwargs):
        current, history, quotes, view, names, payload = self.focus(text, mid, **kwargs)
        return group_decide._speaking_notes(
            quotes, True, group_decide._attribution(
                self.root, CHAT, payload, names, view.get('addressees') or []))

    # 1
    def test_a_history_row_keeps_who_its_at_pointed_at(self):
        self.say('不陪我我恨你', 'm-3944', at=BITE)
        rows = group_decide._dialogue(self.root, CHAT, group_decide._names(self.root, CHAT))
        message = json.loads(rows[0]['message'])
        header = json.loads(message['content'][0]['text'].split('】')[0]
                            .removeprefix('【群消息 '))
        self.assertEqual(header['mentions'], [BITE])
        self.assertEqual(header['at_names'], ['撕咬（QQ ' + BITE + '）'])
        self.assertEqual(header['sender_id'], OWNER)
        self.assertEqual(header['name'], 'Eternal清涩')

    # 2
    def test_the_current_message_names_both_ends_of_the_address(self):
        self.say('不陪我我恨你', 'm-3944', at=BITE)
        self.say('你给我看蒙了，你这肥鱼前后逻辑咋这么混乱', 'm-3946')
        current, history, quotes, view, names, payload = self.focus(
            '你给我看蒙了，你这肥鱼前后逻辑怎么这么混乱', 'm-3946')
        self.assertIn('"name": "Eternal清涩"', current)
        self.assertIn(OWNER, current)
        self.assertIn('你这肥鱼', current)

    # 3
    def test_an_at_is_named_in_the_current_message(self):
        current, history, quotes, view, names, payload = self.focus('你个猪', 'm-3930', at=BITE)
        self.assertIn('这句 @ 的是 撕咬（QQ ' + BITE + '）', current)

    # 4 — the failing turn: a pronoun line with no @ is never assumed to be hers
    def test_a_pronoun_without_an_at_is_marked_as_not_necessarily_her(self):
        notes = self.notes('你觉得他刚才那句话啥意思', 'm-x')
        self.assertIn('[这句的"你"未必是我]', notes)
        self.assertIn('没有 @ 你', notes)
        self.assertIn(CLAIM := '别人之间的对话、互骂、抱怨第三个人，都不是在骂你', notes)

    # 5
    def test_a_message_that_ats_her_is_marked_as_hers(self):
        notes = self.notes('你个猪', 'm-x', at=BOT)
        self.assertIn('[这句是冲你说的]', notes)
        self.assertNotIn('[这句的"你"未必是我]', notes)

    # 6
    def test_her_identity_is_written_on_its_own_line(self):
        notes = self.notes('随便说一句', 'm-x')
        identity = [line for line in notes.splitlines() if line.startswith('[你是谁]')][0]
        self.assertIn(BOT_NAME, identity)
        self.assertIn(GROUP, identity)
        self.assertIn('Eternal清涩（QQ ' + OWNER + '）', identity)

    # 7 — the digest is what a later turn reads the earlier "你" through
    def test_the_addressee_digest_says_who_each_line_was_said_to(self):
        self.say('380掉到350了', 'm-left', sender=BITE)
        self.say('不陪我我恨你', 'm-3944', at=BITE)
        self.say('你恨他别带上我，人家又不是不陪你', 'm-3945', direction='outbound',
                 reply_to='m-3944')
        current, history, quotes, view, names, payload = self.focus(
            '你给我看蒙了，你这肥鱼前后逻辑咋这么混乱', 'm-3946')
        digest = view['addressees']
        self.assertEqual(len(digest), 2)
        self.assertIn('Eternal清涩（QQ ' + OWNER + '） → 撕咬（QQ ' + BITE + '）：不陪我我恨你',
                      digest[0])
        self.assertIn('你（大肥鱼）→ Eternal清涩（QQ ' + OWNER + '）', digest[1])
        self.assertIn('你恨他别带上我', digest[1])
        notes = self.notes('你给我看蒙了，你这肥鱼前后逻辑咋这么混乱', 'm-3946')
        self.assertIn('不陪我我恨你', notes)
        self.assertIn('这句的"你"未必是我', notes)

    # 8 — nothing after the answered message may be read as history
    def test_the_future_of_the_group_is_not_history(self):
        self.say('你给我看蒙了，你这肥鱼前后逻辑咋混乱', 'm-3946')
        self.say('后面才说的话就不该出现在上下文里', 'm-later', sender=OTHER)
        current, history, quotes, view, names, payload = self.focus(
            '你给我看蒙了，你这肥鱼前后逻辑咋混乱', 'm-3946')
        seen = json.dumps([m for m in history], ensure_ascii=False)
        self.assertNotIn('后面才说的话', seen)
        self.assertNotIn('后面才说的话', json.dumps(view, ensure_ascii=False))

    # 9
    def test_a_shadow_turn_is_still_the_bare_comparison_it_was(self):
        self.assertEqual(group_decide._speaking_notes([], False, '不该进影子'), '')
        self.assertEqual(group_decide._prompt_notes([], False), '')

    # 10 — the wiring: the block reaches a speaking turn and never a shadow
    def test_the_block_reaches_the_fish_turn_only(self):
        revision = SocialContext(self.root).save_persona('qq', '主人私聊人格', '私聊里的那个人格')
        self.say('不陪我我恨你', 'm-3944', at=BITE)
        self.say('你给我看蒙了，你这肥鱼前后逻辑咋这么混乱', 'm-3946')
        seen = {}

        def captured(root, role, **kwargs):
            seen.update(kwargs)
            return json.dumps({'bubbles': [{'type': 'text', 'text': '我哪句乱了呀'}]},
                              ensure_ascii=False)

        with patch('fish_chat.respond', captured):
            group_decide.fish_candidate(self.root, CHAT,
                                        payload=self.payload('你给我看蒙了，你这肥鱼前后逻辑咋乱',
                                                             'm-3946'),
                                        revision=revision['id'])
        self.assertIn('[你是谁]', seen['references'])
        self.assertIn('这句的"你"未必是我', seen['references'])
        self.assertIn('不陪我我恨你', seen['references'])
        self.assertIn('撕咬（QQ ' + BITE + '）', seen['references'])

        seen.clear()
        with patch('fish_chat.respond', captured), \
                patch.object(group_decide, '_admit', return_value=''):
            group_decide.respond_as_group(self.root, CHAT,
                                          payload=self.payload('随便聊聊', 'm-shadow'))
        self.assertEqual(seen['references'], '')

    # 11 — 群友发的图/表情没有字：一张都没被当成"这条消息是空的"丢掉
    def show(self, mid, segments, sender=BITE, *, text='', reply_to=None):
        self.store.record_message(self.conversation, CHAT, sender, 'inbound', mid, reply_to,
                                  segments, text, 'sent')

    def picture_payload(self, mid, segments, sender=BITE, *, reply_to=''):
        return {'chat_type': 'group', 'chat_id': CHAT, 'message_id': mid,
                'sender': {'id': sender, 'display_name': ''}, 'reply_to': reply_to,
                'mentions_bot': False, 'segments': segments}

    def test_a_picture_message_is_not_dropped_from_the_group_history(self):
        self.show('m-img', [{'type': 'image', 'url': 'https://x/1'}], text='')
        self.show('m-sticker', [{'type': 'image', 'url': 'https://x/2', 'alt': '[动画表情]'}], text='')
        rows = group_decide._dialogue(self.root, CHAT, group_decide._names(self.root, CHAT))
        self.assertEqual(2, len(rows))
        bodies = [json.loads(row['message'])['content'][0]['text'].split('】\n')[-1] for row in rows]
        self.assertRegex(bodies[0], r'^\[图片\] 撕咬 发了一张图 \d\d:\d\d:\d\d$')
        self.assertRegex(bodies[1], r'^\[表情\] 撕咬 发了个表情包 \d\d:\d\d:\d\d$')

    # 12 — 不带 @ 的纯图同样读得到：这是她"看不见图"的那一半
    def test_a_picture_without_words_or_an_at_is_readable_here_and_by_the_judge(self):
        segments = [{'type': 'image', 'url': 'https://x/3'}]
        self.show('m-img', segments, text='')
        payload = self.picture_payload('m-img', segments)
        names = group_decide._names(self.root, CHAT)
        current = group_decide._focus(self.root, CHAT, payload, names)[0]
        self.assertIn('[图片] 撕咬 发了一张图', current)
        judge = group_decide._judge_context(self.root, CHAT, payload)
        self.assertIn('[图片] 撕咬 发了一张图', judge['current']['text'])
        self.assertFalse(judge['current']['mentions_me'])
        # 带 @ 的纯图不多出"没有写正文"那句假话，两边的正文是同一条可读的行
        direct = {**payload, 'mentions_bot': True,
                  'segments': [{'type': 'at', 'user_id': BOT}, *segments]}
        self.assertIn('[图片] 撕咬 发了一张图', group_decide._focus(self.root, CHAT, direct, names)[0])

    # 13 — 被引用的也是一张图时，引用链照样说得出那是什么
    def test_a_quoted_picture_says_what_it_was(self):
        self.show('m-img', [{'type': 'image', 'url': 'https://x/4', 'alt': '[动画表情]'}], text='')
        self.show('m-ask', [{'type': 'text', 'text': '这个表情哪来的'}], sender=OTHER,
                  text='这个表情哪来的', reply_to='m-img')
        payload = self.picture_payload('m-ask', [{'type': 'text', 'text': '这个表情哪来的'}],
                                       sender=OTHER, reply_to='m-img')
        current = group_decide._focus(self.root, CHAT, payload,
                                      group_decide._names(self.root, CHAT))[0]
        self.assertIn('这句话引用了 m-img 撕咬：[表情] 撕咬 发了个表情包', current)

    # 14 — 唯一还该跳过的，是连字和附件都没有的那一条
    def test_a_message_with_neither_words_nor_an_attachment_is_still_skipped(self):
        self.show('m-nothing', [], text='')
        rows = group_decide._dialogue(self.root, CHAT, group_decide._names(self.root, CHAT))
        self.assertEqual([], rows)


if __name__ == '__main__':
    unittest.main()
