"""看图：看一眼这个聊天里的图，看过就记住，别人的图拿不到。

The vision call is canned, the picture is a real PNG on disk, and the scope is the
production one: the chat comes from the session binding, so another conversation's
message id is not a row here at all. The cache is asserted the only way it can mean
anything - the second look does not reach the model.
"""
import sqlite3
import sys
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from PIL import Image
from test_social_base import Fixture
from fish_chat import CATALOGUES, TOOL_TIMEOUTS, tool_definitions
from tool_host import ToolHost, bind_session, builtins

DESCRIPTION = '一张赛马娘游戏截图，角色站在赛道边，屏幕右上角有倒计时。'


class ImageLookTests(Fixture):
    def setUp(self):
        super().setUp()
        from qq_store import QQStore
        self.qq = QQStore(self.root)
        self.private = 'private:' + self.owner
        self.group_chat = 'group:' + self.group
        self.blue = self.picture('blue')
        self.green = self.picture('green')
        self.host = ToolHost(self.root)
        self.private_session = 'qq-private:' + self.owner
        self.group_scope = 'chat:group:' + self.group
        self.group_session = 'group-fish:' + self.group_scope[5:]
        bind_session(self.root, 'qq', self.private_session, 'owner', self.owner, False, chat_mode='owner')
        bind_session(self.root, 'qq', self.group_session, self.group_scope, '1834434041')

    def picture(self, colour):
        path = self.root / (colour + '.png')
        Image.new('RGB', (32, 32), colour).save(path)
        return path

    def record(self, chat_id, channel_id, path, text=''):
        conversation = self.qq.get_or_create_conversation(chat_id, 'qq')['id']
        return self.qq.record_message(conversation, chat_id, self.owner, 'inbound', channel_id, None,
                                      [{'type': 'image', 'path': str(path), 'alt': '[图片]'}],
                                      text, 'done')

    def look(self, session, arguments, call_id='look'):
        return self.host.execute('qq', session, 'fish_image_look', arguments, call_id)

    def vision(self, answer=DESCRIPTION):
        """One canned vision call per picture; the fake counts the calls it served."""
        model = Mock()
        model.call = Mock(return_value=answer)
        return model, patch('model_gateway.ModelGateway', lambda *a, **k: model)

    def audit(self, table='tool_events', column='cached'):
        path = self.root / 'data/qq/model-usage.db'
        with closing(sqlite3.connect(path)) as db:
            return [row[0] for row in db.execute(
                'SELECT ' + column + ' FROM ' + table + " WHERE tool='image_look' ORDER BY created")] \
                if table == 'tool_events' else [row[0] for row in db.execute('SELECT ' + column + ' FROM ' + table)]

    # 1
    def test_the_tool_is_published_on_both_surfaces_with_a_when_to_use_it_description(self):
        published = {definition['name']: definition for definition in builtins('qq')}
        self.assertIn('fish_image_look', CATALOGUES['qq'])
        self.assertIn('fish_image_look', published)
        description = published['fish_image_look']['description']
        self.assertIn('看一眼', description)
        self.assertIn('from_cache', description)
        self.assertIn('只能看当前会话里出现过的图', description)
        self.assertEqual({'message_id', 'count'},
                         set(published['fish_image_look']['parameters']['properties']))
        for context in ('owner-private', 'owner-desktop', 'group'):
            self.assertIn('fish_image_look',
                          [item['function']['name'] for item in tool_definitions('qq', context)], context)
        # 一次看图是一次下载加一次视觉调用，不能按默认 30 秒的只读工具预算给。
        self.assertEqual(90, TOOL_TIMEOUTS['fish_image_look'])

    # 2
    def test_the_newest_picture_is_described_once_by_the_vision_model(self):
        row = self.record(self.private, '2222', self.blue)
        model, gateway = self.vision()
        with gateway:
            result = self.look(self.private_session, {})
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['from_cache'])
        self.assertEqual([{'message_id': '2222', 'description': DESCRIPTION, 'from_cache': False}],
                         result['looks'])
        self.assertEqual(1, model.call.call_count)
        self.assertEqual([0], self.audit())          # 一次真调用，cached=0
        self.assertIn('不要补充描述里没有的东西', result['note'])

    # 3
    def test_the_same_message_id_answers_from_the_cache_without_a_second_model_call(self):
        self.record(self.private, '2222', self.blue)
        model, gateway = self.vision()
        with gateway:
            first = self.look(self.private_session, {'message_id': '2222'}, 'look-1')
            second = self.look(self.private_session, {'message_id': '2222'}, 'look-2')
        self.assertEqual(first['looks'][0]['description'], second['looks'][0]['description'])
        self.assertFalse(first['from_cache'])
        self.assertTrue(second['from_cache'])
        self.assertEqual(DESCRIPTION, second['looks'][0]['description'])
        # 第二次一眼都没有再看模型：这就是缓存唯一的含义。
        self.assertEqual(1, model.call.call_count)
        self.assertEqual([0, 1], self.audit())

    # 4
    def test_count_reads_two_pictures_and_each_one_is_cached_on_its_own(self):
        self.record(self.private, '2222', self.blue)
        self.record(self.private, '3333', self.green)
        model, gateway = self.vision()
        with gateway:
            result = self.look(self.private_session, {'count': 2})
            again = self.look(self.private_session, {'count': 2}, 'look-again')
        self.assertEqual(['3333', '2222'], [look['message_id'] for look in result['looks']])
        self.assertEqual([DESCRIPTION, DESCRIPTION], [look['description'] for look in result['looks']])
        self.assertEqual(2, model.call.call_count)
        self.assertTrue(again['from_cache'])
        self.assertEqual(2, model.call.call_count)   # 两张都命中了，模型没有被再叫一次

    # 5
    def test_a_message_id_from_another_conversation_is_not_a_picture_she_can_reach(self):
        self.record(self.private, '2222', self.blue)
        model, gateway = self.vision()
        with gateway:
            with self.assertRaisesRegex(PermissionError, '这张图不在这个会话里，我拿不到'):
                self.look(self.group_session, {'message_id': '2222'}, 'look-foreign')
            with self.assertRaisesRegex(PermissionError, '还没有带图片的消息'):
                self.look(self.group_session, {}, 'look-empty')
            with self.assertRaisesRegex(PermissionError, '这条消息里没有图片'):
                self.record(self.private, '4444', self.blue)
                self.qq.get_or_create_conversation(self.private, 'qq')
                with closing(sqlite3.connect(self.root / 'data/qq/qq.db')) as db:
                    db.execute("UPDATE messages SET segments='[]' WHERE channel_message_id='4444'")
                    db.commit()
                self.look(self.private_session, {'message_id': '4444'}, 'look-not-image')
        self.assertEqual(0, model.call.call_count)

    # 6
    def test_a_window_without_a_chat_cannot_look_at_anybody_picture(self):
        self.record(self.private, '2222', self.blue)
        bind_session(self.root, 'qq', 'owner-desktop', 'owner', self.owner, desktop=True)
        model, gateway = self.vision()
        with gateway:
            with self.assertRaisesRegex(PermissionError, '这个窗口没有 QQ 聊天记录'):
                self.look('owner-desktop', {}, 'look-desktop')
        self.assertEqual(0, model.call.call_count)

    # 7
    def test_more_than_two_pictures_at_once_is_refused_instead_of_quietly_cut(self):
        self.record(self.private, '2222', self.blue)
        model, gateway = self.vision()
        with gateway:
            for count in (0, 3):
                with self.assertRaisesRegex(ValueError, 'count 只能是 1 或 2'):
                    self.look(self.private_session, {'count': count}, 'look-' + str(count))
        self.assertEqual(0, model.call.call_count)

    # 9
    def test_a_cached_look_answers_even_when_the_vision_service_is_unavailable(self):
        self.record(self.private, '2222', self.blue)
        model, gateway = self.vision()
        with gateway:
            self.look(self.private_session, {}, 'look-warm')
        # 视觉服务这会儿不可用（没配置模型、图又过期了）：上次描述仍然读得到。
        with patch('multimodal.MediaService', side_effect=RuntimeError('视觉服务不可用')):
            cached = self.look(self.private_session, {'message_id': '2222'}, 'look-cold')
        self.assertTrue(cached['from_cache'])
        self.assertEqual(DESCRIPTION, cached['looks'][0]['description'])
        self.assertEqual(1, model.call.call_count)

    # 8
    def test_a_picture_that_cannot_be_read_is_reported_and_not_cached(self):
        self.record(self.private, '2222', self.blue)
        model, gateway = self.vision()
        with gateway, patch('multimodal.MediaService.attachment_path',
                            side_effect=OSError('下载失败')):
            result = self.look(self.private_session, {})
        self.assertFalse(result['ok'])
        self.assertIn('这张图没看清', result['error'])
        self.assertEqual(0, model.call.call_count)
        with closing(sqlite3.connect(self.root / 'data/tool-host.db')) as db:
            self.assertEqual([], list(db.execute('SELECT * FROM image_looks')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
