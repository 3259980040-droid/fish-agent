"""群里的表情气泡：同一条回复契约、同一份表情库、同一条发送链。

这里没有第二个发送器：群侧把 sticker 交给 qq_agent_loop._deliver_reply，它按通道
既有的段格式换成 image(asset_id) 发出；表情那一段失败时文字已经发出，只留一条
emoji_failed_in_group 审计，不重发，也不把已经发出去的文字报成没发。
"""
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from PIL import Image
from test_social_base import Fixture
from storage import database
from emoji_store import EmojiStore
import group_decide
from qq_agent_loop import QQAgentLoop


class GroupStickerTests(Fixture):
    def setUp(self):
        super().setUp()
        image = self.root / 'fixture.png'
        Image.new('RGB', (24, 24), 'blue').save(image)
        self.asset = EmojiStore(self.root).add(image, '蓝色圆脸')['id']

    def loop(self, *replies):
        loop = QQAgentLoop(self.root)
        loop.channel = Mock(action=AsyncMock(side_effect=list(replies)))
        return loop

    def reply(self, asset=None):
        return [{'type': 'text', 'text': '就这张'},
                {'type': 'sticker', 'emoji_id': asset or self.asset}]

    # 1
    def test_text_and_sticker_both_go_out_and_text_goes_first(self):
        loop = self.loop({'ok': True, 'payload': {'message_id': 'text-ok'}},
                         {'ok': True, 'payload': {'message_id': 'sticker-ok'}})
        result = asyncio.run(loop._deliver_reply(self.chat, self.reply(), 'group-fish:k'))
        self.assertTrue(result['ok'], result)
        sent = [call.args[1] for call in loop.channel.action.await_args_list]
        # 通道只认既有段格式：sticker 在发送链里换成 image(asset_id)，与私聊一致。
        self.assertEqual([v['segments'] for v in sent],
                         [[{'type': 'text', 'text': '就这张'}],
                          [{'type': 'image', 'asset_id': self.asset}]])
        self.assertEqual([v['chat_id'] for v in sent], [self.chat, self.chat])
        self.assertEqual(result['payload']['message_ids'], ['text-ok', 'sticker-ok'])
        self.assertEqual([bubble['state'] for bubble in result['payload']['bubbles']], ['sent'])

    # 2
    def test_a_failed_sticker_never_undoes_sent_text_and_is_not_resent(self):
        loop = self.loop({'ok': True, 'payload': {'message_id': 'text-ok'}},
                         {'ok': False, 'error': {'code': '表情发送过于频繁'}})
        audit = Mock()
        with patch.object(loop.governance, 'audit', audit):
            result = asyncio.run(loop._deliver_reply(self.chat, self.reply(), 'group-fish:k'))
        self.assertTrue(result['ok'], result)  # 文字真的发出去了，不报成没发
        self.assertEqual(loop.channel.action.await_count, 2)  # 不重发，也不多一条失败通知
        self.assertEqual(loop.channel.action.await_args_list[0].args[1]['segments'],
                         [{'type': 'text', 'text': '就这张'}])
        self.assertEqual([call.args[0] for call in audit.call_args_list], ['emoji_failed_in_group'])
        self.assertEqual([bubble['state'] for bubble in result['payload']['bubbles']], ['partial'])

    # 3
    def test_private_text_and_sticker_stay_in_one_action(self):
        loop = self.loop({'ok': True, 'payload': {'message_id': 'both'}})
        result = asyncio.run(loop._deliver_reply('private:' + self.owner, self.reply(), 'private:k'))
        self.assertTrue(result['ok'], result)
        sent = [call.args[1] for call in loop.channel.action.await_args_list]
        self.assertEqual([v['segments'] for v in sent],
                         [[{'type': 'text', 'text': '就这张'}, {'type': 'image', 'asset_id': self.asset}]])

    # 4
    def test_generation_puts_the_words_before_a_single_sticker(self):
        raw = json.dumps({'bubbles': [{'kind': 'sticker', 'emoji_id': self.asset},
                                      {'kind': 'text', 'text': '就这张'}]}, ensure_ascii=False)
        audit = Mock()
        with patch.object(group_decide, '_audit', audit):
            reply = group_decide._bubbles(self.root, self.chat, raw, {'previewed': set()})
        self.assertEqual(reply['text'], '就这张')
        self.assertEqual(reply['segments'], [{'type': 'text', 'text': '就这张'},
                                             {'type': 'sticker', 'emoji_id': self.asset}])
        self.assertEqual([call.args[1] for call in audit.call_args_list], [])

    def test_a_second_sticker_in_one_turn_keeps_her_words_and_not_the_extra_image(self):
        """一轮只有一张表情的位置：多出来的那张不合契约，她的字照样留着。"""
        raw = json.dumps({'bubbles': [{'kind': 'sticker', 'emoji_id': self.asset},
                                      {'kind': 'sticker', 'emoji_id': self.asset},
                                      {'kind': 'text', 'text': '就这张'}]}, ensure_ascii=False)
        audit = Mock()
        with patch.object(group_decide, '_audit', audit):
            reply = group_decide._bubbles(self.root, self.chat, raw, {'previewed': set()})
        self.assertEqual(reply['segments'], [{'type': 'text', 'text': '就这张'}])
        self.assertEqual([call.args[1] for call in audit.call_args_list], ['group_fish_reply_format'])

    # 5
    def test_a_sticker_outside_the_library_never_goes_and_the_words_still_do(self):
        raw = json.dumps({'bubbles': [{'kind': 'text', 'text': '嗯'},
                                      {'kind': 'sticker', 'emoji_id': 'fake'}]})
        audit = Mock()
        with patch.object(group_decide, '_audit', audit):
            reply = group_decide._bubbles(self.root, self.chat, raw, {'previewed': set()})
        self.assertEqual(reply['segments'], [{'type': 'text', 'text': '嗯'}])
        self.assertEqual([call.args[1] for call in audit.call_args_list], ['group_fish_reply_format'])

    # 6
    def test_an_unpreviewed_undescribed_sticker_needs_the_preview(self):
        image = self.root / 'plain.png'
        Image.new('RGB', (24, 24), 'green').save(image)
        asset = EmojiStore(self.root).add(image)['id']
        raw = json.dumps({'bubbles': [{'kind': 'text', 'text': '嗯'},
                                      {'kind': 'sticker', 'emoji_id': asset}]})
        self.assertEqual(group_decide._bubbles(self.root, self.chat, raw, {'previewed': set()})['segments'],
                         [{'type': 'text', 'text': '嗯'}])
        self.assertEqual(group_decide._bubbles(self.root, self.chat, raw, {'previewed': {asset}})['segments'],
                         [{'type': 'text', 'text': '嗯'}, {'type': 'sticker', 'emoji_id': asset}])

    # 7
    def test_one_sticker_rides_beside_three_bubbles(self):
        """文字 1 至 3 条加 1 张表情就是一轮的满额：第 4 条文字或第 2 张表情都不合契约。"""
        raw = json.dumps({'bubbles': [{'kind': 'text', 'text': '第一句'},
                                      {'kind': 'sticker', 'emoji_id': self.asset},
                                      {'kind': 'text', 'text': '第二句'},
                                      {'kind': 'text', 'text': '第三句'}]}, ensure_ascii=False)
        audit = Mock()
        with patch.object(group_decide, '_audit', audit):
            reply = group_decide._bubbles(self.root, self.chat, raw, {'previewed': {self.asset}})
        self.assertEqual(reply['segments'], [{'type': 'text', 'text': '第一句' + chr(10) + '第二句' + chr(10) + '第三句'},
                                             {'type': 'sticker', 'emoji_id': self.asset}])
        self.assertEqual([call.args[1] for call in audit.call_args_list], [])

    def test_an_off_contract_bubble_count_keeps_her_words_and_drops_the_rest(self):
        raw = json.dumps({'bubbles': [{'kind': 'text', 'text': '第一句'},
                                      {'kind': 'text', 'text': '第二句'},
                                      {'kind': 'text', 'text': '第三句'},
                                      {'kind': 'text', 'text': '第四句'}]}, ensure_ascii=False)
        audit = Mock()
        with patch.object(group_decide, '_audit', audit):
            reply = group_decide._bubbles(self.root, self.chat, raw, {'previewed': set()})
        self.assertEqual(reply['segments'],
                         [{'type': 'text', 'text': '第一句' + chr(10) + '第二句' + chr(10) + '第三句' + chr(10) + '第四句'}])
        self.assertEqual([call.args[1] for call in audit.call_args_list], ['group_fish_reply_format'])

    # 8
    def test_an_emoji_stored_in_the_owner_private_chat_goes_out_in_a_group(self):
        """私聊里收进库的表情，群里走同一条链发：档位只在这个聊天自己的节奏上。"""
        image = self.root / 'private-collected.jpg'
        Image.new('RGB', (24, 24), 'purple').save(image, 'JPEG')
        asset = EmojiStore(self.root).add(image, '私聊里收到的紫色圆脸', '无语',
                                          source='qq-attachment:private-1')['id']
        loop = self.loop({'ok': True, 'payload': {'message_id': 'text-ok'}},
                         {'ok': True, 'payload': {'message_id': 'sticker-ok'}})
        result = asyncio.run(loop._deliver_reply(self.chat, self.reply(asset), 'group-fish:private'))
        self.assertTrue(result['ok'], result)
        sent = [call.args[1] for call in loop.channel.action.await_args_list]
        self.assertEqual([v['segments'] for v in sent],
                         [[{'type': 'text', 'text': '就这张'}],
                          [{'type': 'image', 'asset_id': asset}]])
        # 通道那一步（server._resolve_images）只问 EmojiStore 要原图，不问这张是哪个聊天存的。
        reserved = EmojiStore(self.root).reserve_send(asset, self.chat, request_id='private-collected')
        self.assertEqual(reserved['asset_id'], asset)
        self.assertTrue(reserved['file'].startswith('base64://'))

    # 9
    def test_the_owner_private_chat_cannot_spend_the_group_emoji_budget(self):
        """每日表情额度按聊天算：私聊发过的表情不花群里的额度，也不用群的冷却。"""
        store = EmojiStore(self.root)
        store.configure({'max_daily_sends': 1, 'cooldown_seconds': 30, 'same_meme_seconds': 60})
        with database(store.path) as db:
            db.execute('INSERT INTO sends(asset,chat,ts,request_id,state) VALUES(?,?,?,?,?)',
                       (self.asset, 'private:' + self.owner, time.time(), 'private-1', 'sent'))
        self.assertEqual(store.send_problem(self.asset, self.chat), '')
        self.assertEqual(store.reserve_send(self.asset, self.chat, request_id='group-1')['asset_id'], self.asset)
        self.assertEqual(store.send_problem(self.asset, 'group:1043230169'), '')
        with database(store.path) as db:  # 这个群今天发过一条，冷却已过，但额度是 1，已经用完
            db.execute('UPDATE sends SET ts=? WHERE request_id=?', (time.time() - 90, 'group-1'))
        self.assertEqual(store.send_problem(self.asset, self.chat), '今日表情发送达到上限')

    # 10
    def test_a_refused_group_sticker_audits_why_it_was_refused(self):
        """通道只回一个 emoji_unavailable；审计要写清楚是这张还在冷却，不是图不能用。"""
        store = EmojiStore(self.root)
        with database(store.path) as db:
            db.execute('INSERT INTO sends(asset,chat,ts,request_id,state) VALUES(?,?,?,?,?)',
                       (self.asset, self.chat, time.time() - 300, 'earlier', 'sent'))
        loop = self.loop({'ok': True, 'payload': {'message_id': 'text-ok'}},
                         {'ok': False, 'error': {'code': 'emoji_unavailable'}})
        audit = Mock()
        with patch.object(loop.governance, 'audit', audit):
            result = asyncio.run(loop._deliver_reply(self.chat, self.reply(), 'group-fish:refused'))
        self.assertTrue(result['ok'], result)
        self.assertEqual([call.args[0] for call in audit.call_args_list], ['emoji_failed_in_group'])
        reason = audit.call_args_list[0].args[1]
        self.assertIn('emoji_unavailable', reason)
        self.assertIn('同一表情仍在冷却', reason)


if __name__ == '__main__':
    unittest.main()
