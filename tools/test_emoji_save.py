"""把一张图存进表情库：一条下载路径、一条入库路径，拒绝逐条钉住。

The transport here is canned, but the address gate is the production one:
web_knowledge.public_target decides every hop, so a loopback URL is refused by
the same rule the page tools use, not by a mock. A picture from the chat takes
the same inspect+add path; only the chat it may be read out of is settled by the
session binding, so another conversation's id is not a row here at all.
"""
import io
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from PIL import Image
from test_social_base import Fixture
import requests
from emoji_store import EmojiStore
from fish_chat import CATALOGUES, tool_definitions
from multimodal import MediaService
from social_context import SocialContext
from tool_host import ToolHost, builtins
from web_knowledge import WebKnowledge

IMAGE = {'Content-Type': 'image/png'}
TEST_HOST = 'img.example.com'
# 打桩前先取到生产用的地址判定，桩里才能继续用它判断真实的地址。
PUBLIC_TARGET = WebKnowledge.public_target


def png(colour='blue'):
    buffer = io.BytesIO()
    Image.new('RGB', (24, 24), colour).save(buffer, 'PNG')
    return buffer.getvalue()


class FakeResponse:
    """One canned HTTP reply with the two members the downloader actually reads."""

    def __init__(self, body=b'', *, status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = headers or {}
        self.ok = 200 <= status < 300
        self.is_redirect = 300 <= status < 400

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(str(self.status_code))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    """A session-shaped transport: no socket, one recorded URL per request."""

    def __init__(self, *replies):
        self.replies, self.calls, self.trust_env = list(replies), [], True

    def get(self, url, **_options):
        self.calls.append(url)
        return self.replies.pop(0)

    def close(self):
        pass


class EmojiSaveTests(Fixture):
    def store(self):
        return EmojiStore(self.root)

    @staticmethod
    def gate(url):
        """No DNS in a unit test: the test host is public by fiat, everything else is the real gate."""
        return url if (urlparse(str(url)).hostname or '') == TEST_HOST else PUBLIC_TARGET(url)

    def download(self, *replies):
        session = FakeSession(*replies)
        return session, patch('network_config.session', lambda root: session), \
            patch('web_knowledge.WebKnowledge.public_target', staticmethod(self.gate))

    # 1
    def test_a_real_png_is_saved_with_its_note_and_turns_up_in_search(self):
        body = png()
        session, transport, gate = self.download(
            FakeResponse(body, headers={**IMAGE, 'Content-Length': str(len(body))}))
        with transport, gate:
            saved = self.store().save_url('https://' + TEST_HOST + '/blue.png', '蓝色圆脸微笑')
        self.assertTrue(saved['ok'], saved)
        self.assertEqual('蓝色圆脸微笑', saved['note'])
        self.assertEqual(len('蓝色圆脸微笑'), saved['chars'])
        self.assertEqual('web:' + TEST_HOST, saved['source'])
        self.assertEqual(len(body), saved['bytes'])
        self.assertFalse(saved['duplicate'])
        rows = self.store().list()
        stored = next(row for row in rows if row['id'] == saved['asset'])
        self.assertTrue(Path(stored['path']).is_file())
        self.assertEqual('蓝色圆脸微笑', stored['description'])
        self.assertEqual('web:' + TEST_HOST, stored['source'])
        self.assertEqual(saved['asset'], self.store().candidates('蓝色圆脸微笑')[0]['id'])
        # 下载用的临时文件不留在表情目录里
        self.assertEqual([], list(self.root.glob('data/qq/emoji/*.download')))

    # 2
    def test_a_web_page_is_refused_before_it_reaches_the_library(self):
        session, transport, gate = self.download(
            FakeResponse(b'<html></html>', headers={'Content-Type': 'text/html; charset=utf-8'}))
        with transport, gate:
            refused = self.store().save_url('https://' + TEST_HOST + '/index.html', '不是图片')
        self.assertFalse(refused['ok'])
        self.assertIn('目标不是图片', refused['error'])
        self.assertIn('text/html', refused['error'])
        self.assertEqual([], self.store().list())

    # 3
    def test_loopback_is_refused_by_the_shared_address_gate(self):
        refused = self.store().save_url('http://127.0.0.1:3104/health', '本机')
        self.assertFalse(refused['ok'])
        self.assertIn('图片地址被拒绝', refused['error'])
        self.assertIn('只允许访问 80 或 443 端口', refused['error'])
        loopback = self.store().save_url('http://127.0.0.1/health', '本机')
        self.assertFalse(loopback['ok'])
        self.assertIn('本机地址', loopback['error'])
        self.assertEqual([], self.store().list())

    # 4
    def test_a_redirect_into_the_private_network_dies_at_that_hop(self):
        session, transport, gate = self.download(
            FakeResponse(status=302, headers={'Location': 'http://127.0.0.1/health'}), FakeResponse(png()))
        with transport, gate:
            refused = self.store().save_url('https://' + TEST_HOST + '/jump.png', '重定向')
        self.assertFalse(refused['ok'])
        self.assertIn('本机地址', refused['error'])
        self.assertEqual(['https://' + TEST_HOST + '/jump.png'], session.calls)  # 第二跳没有被请求
        self.assertEqual([], self.store().list())

    # 5
    def test_the_five_megabyte_ceiling_holds_on_the_header_and_in_the_stream(self):
        session, transport, gate = self.download(
            FakeResponse(png(), headers={**IMAGE, 'Content-Length': str(6 * 1024 * 1024)}))
        with transport, gate:
            refused = self.store().save_url('https://' + TEST_HOST + '/big.png', '太大')
        self.assertFalse(refused['ok'])
        self.assertIn('5MB', refused['error'])
        session, transport, gate = self.download(FakeResponse(b'0' * (6 * 1024 * 1024), headers=IMAGE))
        with transport, gate:
            streamed = self.store().save_url('https://' + TEST_HOST + '/streamed.png', '太大')
        self.assertFalse(streamed['ok'])
        self.assertIn('5MB', streamed['error'])
        self.assertEqual([], self.store().list())

    # 6
    def test_a_payload_that_only_claims_to_be_an_image_never_enters_the_library(self):
        session, transport, gate = self.download(FakeResponse(b'not an image at all', headers=IMAGE))
        with transport, gate:
            refused = self.store().save_url('https://' + TEST_HOST + '/lie.png', '假装是图')
        self.assertFalse(refused['ok'])
        self.assertIn('不是可用的表情图片', refused['error'])
        self.assertEqual([], self.store().list())

    # 7
    def test_the_one_executor_answers_on_all_three_surfaces(self):
        published = {definition['name']: definition for definition in builtins('qq')}
        self.assertIn('把一张图存进自己的表情库', published['fish_emoji_save']['description'])
        self.assertIn('聊天里刚发的图可以直接存', published['fish_emoji_save']['description'])
        self.assertEqual({'url', 'message_id', 'latest', 'note'},
                         set(published['fish_emoji_save']['parameters']['properties']))
        self.assertEqual([], published['fish_emoji_save']['parameters']['required'])
        self.assertEqual(1, len(CATALOGUES))
        self.assertIn('fish_emoji_save', CATALOGUES['qq'])
        for context in ('owner-private', 'owner-desktop', 'group'):
            self.assertIn('fish_emoji_save',
                          [item['function']['name'] for item in tool_definitions('qq', context)],
                          context)
        contexts = SocialContext(self.root)
        revision = contexts.save_persona('qq', 'chat', 'chat')
        space = contexts.create_space('qq', revision=revision['id'], origin='owner-qq')
        contexts.select_owner_space(space['id'], self.owner)
        host = ToolHost(self.root)
        host.bind('qq', 'owner-window', space['scope'], self.owner)
        session, transport, gate = self.download(
            FakeResponse(png('red'), headers=IMAGE), FakeResponse(png('green'), headers=IMAGE))
        with transport, gate:
            private = host.execute('qq', 'owner-window', 'fish_emoji_save',
                                   {'url': 'https://' + TEST_HOST + '/a.png', 'note': '私聊里存的'}, 'save-private')
        self.assertTrue(private['ok'], private)
        group_scope = 'chat:group:' + self.group
        host.bind('qq', 'group-fish:' + group_scope[5:], group_scope, self.owner)
        with transport, gate:
            group = host.execute('qq', 'group-fish:' + group_scope[5:], 'fish_emoji_save',
                                 {'url': 'https://' + TEST_HOST + '/b.png', 'note': '群里存的'}, 'save-group')
        self.assertTrue(group['ok'], group)
        self.assertEqual({'私聊里存的', '群里存的'},
                         {row['description'] for row in self.store().list()} & {'私聊里存的', '群里存的'})


class EmojiChatSaveTests(Fixture):
    """聊天里的图：来源只能是本会话，落库仍是同一条 inspect+add 路径。

    这里造的是入库前的事实：一条文字、一条带本地 png 的 image 段、一条更新的文字，
    于是 latest 必须往前翻到那条带图的消息，message_id 必须能指名取到同一张。
    """

    def setUp(self):
        super().setUp()
        from qq_store import QQStore
        self.picture = self.root / 'fixture.png'
        Image.new('RGB', (24, 24), 'blue').save(self.picture)
        self.qq = QQStore(self.root)
        self.private = 'private:' + self.owner
        conversation = self.qq.get_or_create_conversation(self.private, 'qq')['id']
        self.text_row = self.record(conversation, '1111', [{'type': 'text', 'text': '看这个'}], '看这个')
        self.image_row = self.record(conversation, '2222', [{'type': 'image', 'path': str(self.picture),
                                                             'alt': '[图片]'}], '')
        self.after_row = self.record(conversation, '3333', [{'type': 'text', 'text': '收到了吗'}], '收到了吗')
        self.host = ToolHost(self.root)
        self.host.bind('qq', 'qq-private:' + self.owner, 'owner', self.owner)

    def record(self, conversation, channel_id, segments, text):
        return self.qq.record_message(conversation, self.private, self.owner, 'inbound',
                                     channel_id, None, segments, text, 'done')

    def save(self, session, arguments, call_id):
        return self.host.execute('qq', session, 'fish_emoji_save', arguments, call_id)

    # 8
    def test_latest_walks_back_to_the_newest_message_that_has_a_picture(self):
        saved = self.save('qq-private:' + self.owner, {'latest': True, 'note': '主人刚发的蓝脸'}, 'save-latest')
        self.assertTrue(saved['ok'], saved)
        self.assertEqual('qq:' + self.private + ':2222', saved['source'])
        self.assertEqual('2222', saved['message_id'])
        self.assertEqual(self.picture.stat().st_size, saved['bytes'])
        stored = next(row for row in self.store().list() if row['id'] == saved['asset'])
        self.assertEqual('主人刚发的蓝脸', stored['description'])
        self.assertEqual('qq:' + self.private + ':2222', stored['source'])
        self.assertEqual(self.picture.stat().st_size, Path(stored['path']).stat().st_size)

    # 9
    def test_a_named_message_id_hands_over_the_same_picture_and_dedupes_it(self):
        named = self.save('qq-private:' + self.owner, {'message_id': '2222', 'note': '主人刚发的蓝脸'}, 'save-named')
        self.assertTrue(named['ok'], named)
        self.assertEqual('2222', named['message_id'])
        by_row_id = self.save('qq-private:' + self.owner,
                              {'message_id': str(self.image_row), 'note': '主人刚发的蓝脸'}, 'save-row')
        self.assertTrue(by_row_id['ok'], by_row_id)
        self.assertTrue(by_row_id['duplicate'])
        self.assertEqual(named['asset'], by_row_id['asset'])
        self.assertEqual(1, len(self.store().list()))

    # 13
    def test_a_qq_attachment_url_is_read_by_the_media_service_not_a_second_downloader(self):
        body = png()
        url = 'https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc'
        conversation = self.qq.get_or_create_conversation(self.private, 'qq')['id']
        self.record(conversation, '4444', [{'type': 'image', 'url': url, 'alt': '[动画表情]'}], '')
        session = FakeSession(FakeResponse(body, headers={**IMAGE, 'Content-Length': str(len(body))}))
        # 只放开 DNS 与域名判定：真实库里 QQ 的图片就在 multimedia.nt.qq.com.cn 上。
        with patch('network_config.session', lambda root: session), \
                patch.object(MediaService, '_validate_media_url', staticmethod(lambda target: None)):
            saved = self.save('qq-private:' + self.owner, {'message_id': '4444', 'note': '从 QQ 地址取的'}, 'save-url-segment')
        self.assertTrue(saved['ok'], saved)
        self.assertEqual([url], session.calls)
        self.assertEqual('qq:' + self.private + ':4444', saved['source'])
        self.assertEqual(len(body), saved['bytes'])
        stored = next(row for row in self.store().list() if row['id'] == saved['asset'])
        self.assertTrue(Path(stored['path']).is_file())

    # 10
    def test_another_conversation_id_is_not_a_picture_she_can_reach(self):
        group_scope = 'chat:group:' + self.group
        group_session = 'group-fish:' + group_scope[5:]
        # 群里的发起人是一个群友，不是主人：他取不到主人私聊里的任何一张图。
        self.host.bind('qq', group_session, group_scope, '1834434041')
        with self.assertRaisesRegex(PermissionError, '这张图不在这个会话里，我拿不到'):
            self.save(group_session, {'message_id': '2222', 'note': '别的会话的图'}, 'save-foreign')
        with self.assertRaisesRegex(PermissionError, '还没有带图片的消息'):
            self.save(group_session, {'latest': True, 'note': '本群还没人发图'}, 'save-empty')
        self.assertEqual([], self.store().list())

    # 11
    def test_the_owner_chat_cannot_be_reached_from_a_window_without_one(self):
        self.host.bind('qq', 'owner-desktop', 'owner', self.owner, desktop=True)
        with self.assertRaisesRegex(PermissionError, '这个窗口没有 QQ 聊天记录'):
            self.save('owner-desktop', {'latest': True}, 'save-desktop')
        self.assertEqual([], self.store().list())

    # 12
    def test_two_sources_at_once_are_refused_instead_of_guessed(self):
        with self.assertRaisesRegex(ValueError, '只能给一个'):
            self.save('qq-private:' + self.owner,
                      {'url': 'https://' + TEST_HOST + '/a.png', 'latest': True}, 'save-both')
        self.assertEqual([], self.store().list())

    def store(self):
        return EmojiStore(self.root)

class OwnerAcrossWindowsTests(Fixture):
    """主人的号跨窗口是同一个人；别人的号不是。

    用户的裁决：QQ 发送消息绑定真实账号，所以身份按账号判定——主人账号是用户本人，
    其余账号是群友角色。于是主人在私聊里发过的图，主人本人在群里让她发时她拿得到
    （from=owner-private，并留一条 tool.scope.allowed 审计）；群友拿不到任何人的私聊图；
    主人私聊也倒不过来群里的图。
    """

    STRANGER = '1834434041'

    def setUp(self):
        super().setUp()
        from qq_store import QQStore
        self.qq = QQStore(self.root)
        self.private = 'private:' + self.owner
        self.group_chat = 'group:' + self.group
        self.picture = self.root / 'owner-private.png'
        Image.new('RGB', (24, 24), 'green').save(self.picture)
        self.private_row = self.record(self.private, '5001', self.owner, self.picture)
        self.host = ToolHost(self.root)
        self.group_scope = 'chat:group:' + self.group
        self.group_session = 'group-fish:' + self.group_scope[5:]
        self.private_session = 'qq-private:' + self.owner

    def record(self, chat_id, channel_id, sender, path):
        conversation = self.qq.get_or_create_conversation(chat_id, 'qq')['id']
        return self.qq.record_message(conversation, chat_id, sender, 'inbound', channel_id, None,
                                      [{'type': 'image', 'path': str(path), 'alt': '[图片]'}],
                                      '', 'done')

    def store(self):
        return EmojiStore(self.root)

    def save(self, session, arguments, call_id):
        return self.host.execute('qq', session, 'fish_emoji_save', arguments, call_id)

    def audit_notes(self, kind='tool.scope.allowed'):
        with closing(sqlite3.connect(self.root / 'data/qq/governance.db')) as db:
            return [str(row[0]) for row in db.execute(
                'SELECT reason FROM audit WHERE kind=?', (kind,))]

    # 1 — 主人在私聊存过图，主人在群里要，她拿得到，并且取的是主人私聊那一张
    def test_the_owner_reaches_his_own_private_picture_from_a_group(self):
        self.host.bind('qq', self.group_session, self.group_scope, self.owner)
        saved = self.save(self.group_session,
                          {'latest': True, 'note': '主人在群里要的私聊图'}, 'owner-cross')
        self.assertTrue(saved['ok'], saved)
        self.assertEqual('owner-private', saved['from'])
        self.assertEqual('qq:' + self.private + ':5001', saved['source'])
        self.assertEqual('5001', saved['message_id'])
        self.assertEqual('主人在群里要的私聊图', saved['note'])
        stored = next(row for row in self.store().list() if row['id'] == saved['asset'])
        self.assertEqual(self.picture.stat().st_size, Path(stored['path']).stat().st_size)
        self.assertEqual('qq:' + self.private + ':5001', stored['source'])
        self.assertIn('主人跨会话取图：当前会话没有这张图，改取主人私聊图库 ' + self.private,
                      '\n'.join(self.audit_notes()))

    # 2 — 同一个群、同一个 latest，换成群友的号就一张也拿不到
    def test_a_group_member_gets_no_private_picture_at_all(self):
        self.host.bind('qq', self.group_session, self.group_scope, self.STRANGER)
        with self.assertRaisesRegex(PermissionError, '还没有带图片的消息'):
            self.save(self.group_session, {'latest': True, 'note': '群友要主人私聊的图'}, 'mate-latest')
        with self.assertRaisesRegex(PermissionError, '这张图不在这个会话里'):
            self.save(self.group_session, {'message_id': '5001', 'note': '群友点名主人私聊那条'}, 'mate-named')
        self.assertEqual([], self.store().list())
        self.assertEqual([], self.audit_notes())

    # 3 — 群友取本群自己的图照旧，且落的是本群那条，不是主人私聊那条
    def test_a_group_member_still_reaches_this_group_and_only_this_group(self):
        group_picture = self.root / 'group.png'
        Image.new('RGB', (24, 24), 'red').save(group_picture)
        self.record(self.group_chat, '6001', self.STRANGER, group_picture)
        self.host.bind('qq', self.group_session, self.group_scope, self.STRANGER)
        saved = self.save(self.group_session, {'latest': True, 'note': '群友存的群图'}, 'mate-group')
        self.assertTrue(saved['ok'], saved)
        self.assertEqual('chat', saved['from'])
        self.assertEqual('qq:' + self.group_chat + ':6001', saved['source'])

    # 4 — 反向不通：主人私聊里拿不到群里的图
    def test_the_owner_private_chat_still_cannot_reach_a_group_picture(self):
        group_picture = self.root / 'group.png'
        Image.new('RGB', (24, 24), 'red').save(group_picture)
        self.record(self.group_chat, '6001', self.STRANGER, group_picture)
        self.host.bind('qq', self.private_session, 'owner', self.owner)
        with self.assertRaisesRegex(PermissionError, '这张图不在这个会话里'):
            self.save(self.private_session, {'message_id': '6001', 'note': '主人私聊要群里的图'}, 'owner-group')
        own = self.save(self.private_session, {'latest': True, 'note': '主人私聊自己的图'}, 'owner-own')
        self.assertTrue(own['ok'], own)
        self.assertEqual('chat', own['from'])
        self.assertEqual('qq:' + self.private + ':5001', own['source'])

    # 5 — 主人私聊本身不是回落出来的，它取的就是自己那间聊天
    def test_the_owner_private_chat_reads_its_own_chat_first(self):
        self.host.bind('qq', self.private_session, 'owner', self.owner)
        saved = self.save(self.private_session, {'message_id': '5001', 'note': '主人私聊自己的图'}, 'owner-named')
        self.assertTrue(saved['ok'], saved)
        self.assertEqual('chat', saved['from'])
        self.assertEqual([], self.audit_notes())


if __name__ == '__main__':
    unittest.main()
