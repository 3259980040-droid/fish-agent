"""Owner quotes from the desktop client are verified in the desktop chat store.

A desktop turn calls the same fish_memory_save tool, but its messages live in
data/<role>/desktop-chat.db, which has no sender_id column and a different id
space. Reading the QQ database there could never match, and accepting a QQ
message id would be a forged source. Each store is therefore checked on its own.
"""
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from governance import Governance
from memory_store import MemoryStore
from storage import write_json
from tool_host import ToolHost

OWNER = '10001'
GROUP = '1043230169'
QQ_TEXT = '主人说喜欢蓝色'


def open_desktop(root):
    path = root / 'data' / 'qq' / 'desktop-chat.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,title TEXT,session_id TEXT,
            updated_at TEXT,space_id TEXT NOT NULL DEFAULT '',mode TEXT NOT NULL DEFAULT 'chat',
            workspace TEXT NOT NULL DEFAULT '',revision TEXT NOT NULL DEFAULT '',
            permission TEXT NOT NULL DEFAULT 'workspace-write');
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,conversation_id TEXT,direction TEXT,text TEXT,
            segments TEXT,status TEXT,error TEXT,created_at TEXT);""")
        db.commit()
    return path


def conversation(root, cid, session='', space=''):
    with closing(sqlite3.connect(root / 'data' / 'qq' / 'desktop-chat.db')) as db:
        db.execute('INSERT OR REPLACE INTO conversations(id,title,session_id,updated_at,space_id,mode) '
                   'VALUES(?,?,?,?,?,?)', (cid, cid, session, '2026-10-22 06:00:00', space, 'chat'))
        db.commit()


def message(root, cid, text, direction='inbound'):
    with closing(sqlite3.connect(root / 'data' / 'qq' / 'desktop-chat.db')) as db:
        cursor = db.execute('INSERT INTO messages(conversation_id,direction,text,status,created_at) '
                            'VALUES(?,?,?,?,?)', (cid, direction, text, 'done', '2026-10-22 06:00:00'))
        db.commit()
        return cursor.lastrowid


def qq_row(root, text=QQ_TEXT, mid=777):
    path = root / 'data' / 'qq' / 'qq.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT, '
                   'user_id TEXT, text TEXT, replied INTEGER DEFAULT 0, conversation_id TEXT, sender_id TEXT, '
                   'direction TEXT, channel_message_id TEXT, reply_to TEXT, segments TEXT, status TEXT, '
                   'runtime_session_id TEXT, runtime_turn_id TEXT, error TEXT, cancelled INTEGER DEFAULT 0)')
        db.execute('INSERT OR REPLACE INTO messages(id,ts,chat_id,user_id,text,conversation_id,sender_id,direction,'
                   'channel_message_id,status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (mid, '2026-10-22 06:00:00', 'private:' + OWNER, OWNER, text, 'qq:private:' + OWNER,
                    OWNER, 'inbound', str(900000 + mid), 'processed'))
        db.commit()
    return mid


class DesktopMemorySourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'harness').mkdir(parents=True, exist_ok=True)
        (self.root / 'data' / 'qq').mkdir(parents=True, exist_ok=True)
        write_json(self.root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'model': 'fake-model', 'base_url': 'https://fake.invalid',
                        'apiKeyEnv': 'FISH_TEST_QQ_KEY'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.root / 'channels/fish_qq/config.json',
                   {'allowlist': {'owner_id': OWNER, 'group_ids': [int(GROUP)]},
                    'bot': {'display_name': '大肥鱼'}})
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        self.host = ToolHost(self.root)
        self.addCleanup(lambda: self.host.stop.set())
        open_desktop(self.root)
        self.session = 'desktop-session:' + uuid.uuid4().hex
        conversation(self.root, 'c1', session=self.session)
        conversation(self.root, 'c2', session='desktop-session:other')
        self.owner_text = '主人说周三交报告，别忘了'
        self.mid = message(self.root, 'c1', self.owner_text)
        self.host.bind('qq', self.session, 'owner', '', True)

    def save(self, mid=None, quote='主人说周三交报告', session=None, evidence='owner_quote', **extra):
        body = {'text': '主人周三要交报告。', 'category': '约定', 'evidence': evidence,
                'source_quote': quote, 'source_message_id': str(self.mid if mid is None else mid)}
        body.update(extra)
        return self.host.execute('qq', session or self.session, 'fish_memory_save', body,
                                 'call-' + uuid.uuid4().hex[:8])

    def stored(self, memory_id):
        return next(item for item in MemoryStore(self.root, 'qq').search(
            '', scope='owner', limit=200, include_candidates=True) if item['id'] == memory_id)

    # 1
    def test_a_real_desktop_message_can_be_the_owner_quote(self):
        saved = self.save()
        self.assertTrue(saved['ok'])
        row = self.stored(saved['id'])
        self.assertEqual('desktop:' + str(self.mid), row['source'])
        self.assertEqual(self.owner_text, row['source_text'])
        self.assertEqual('active', row['status'])
        self.assertEqual('主人说周三交报告', row['text'])

    # 2
    def test_a_quote_that_is_not_in_the_desktop_message_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.save(quote='主人说周四交报告')
        self.assertIn('桌面消息不匹配', str(caught.exception))
        self.assertEqual([], MemoryStore(self.root, 'qq').search('周四', scope='owner', include_candidates=True))

    # 3
    def test_another_conversations_message_id_is_refused(self):
        other = message(self.root, 'c2', '别的对话里说的话')
        with self.assertRaises(ValueError):
            self.save(mid=other, quote='别的对话里说的话')
        self.assertEqual([], MemoryStore(self.root, 'qq').search('别的对话', scope='owner', include_candidates=True))

    # 4
    def test_a_qq_message_id_never_passes_as_a_desktop_message_id(self):
        qq_row(self.root)
        with self.assertRaises(ValueError) as caught:
            self.save(mid=777, quote=QQ_TEXT)
        self.assertIn('桌面消息号', str(caught.exception))
        # The very same row is a valid quote for a QQ private chat, so the
        # refusal above is the desktop store refusing, not a missing message.
        qq_session = 'qq-private:' + uuid.uuid4().hex
        self.host.bind('qq', qq_session, 'owner', OWNER, False, chat_mode='owner')
        saved = self.save(mid=777, quote=QQ_TEXT, session=qq_session)
        self.assertEqual('qq:777', self.stored(saved['id'])['source'])

    # 5
    def test_a_window_scoped_desktop_conversation_must_be_this_space(self):
        from social_context import SocialContext
        space = SocialContext(self.root).create_space('qq', '桌面窗口')
        other_session = 'desktop-session:' + uuid.uuid4().hex
        conversation(self.root, 'c3', session=other_session, space=space['id'])
        inside = message(self.root, 'c3', '这个窗口里的原话')
        self.host.bind('qq', other_session, 'space:' + space['id'], '', True)
        saved = self.save(mid=inside, quote='这个窗口里的原话', session=other_session)
        self.assertEqual('desktop:' + str(inside), self.stored(saved['id'])['source'])
        # The same session, but a conversation that is not this window.
        conversation(self.root, 'c4', session=other_session, space='')
        outside = message(self.root, 'c4', '窗口外的原话')
        with self.assertRaises(ValueError):
            self.save(mid=outside, quote='窗口外的原话', session=other_session)

    # 6
    def test_a_desktop_reply_is_not_the_owners_quote(self):
        reply = message(self.root, 'c1', '好啊，那周三见', 'outbound')
        with self.assertRaises(ValueError):
            self.save(mid=reply, quote='好啊，那周三见')

    # 7
    def test_group_scope_still_cannot_save_memory(self):
        group_session = 'group-fish:' + GROUP
        self.host.bind('qq', group_session, 'chat:group:' + GROUP, '20002', False)
        for evidence in ('owner_quote', 'model_inference'):
            with self.assertRaises(PermissionError):
                self.host.execute('qq', group_session, 'fish_memory_save',
                                  {'text': '群里的话', 'evidence': evidence,
                                   'source_quote': '主人说周三交报告',
                                   'source_message_id': str(self.mid)}, 'call-group-' + evidence)


if __name__ == '__main__':
    unittest.main()
