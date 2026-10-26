"""04-02: one graded FTS5 retrieval entry, overview and original expansion.

No model and no network: a temporary root with the real stores.
"""
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]

import doc_store
import history_index
import knowledge
from memory_digest import _store as store_digest
from memory_store import MemoryStore
from locations import InstanceContext, project_path
from workbench import Workbench

QQ_SCHEMA = (
    'CREATE TABLE messages (id INTEGER PRIMARY KEY, ts TEXT, chat_id TEXT, user_id TEXT, '
    'text TEXT, conversation_id TEXT, sender_id TEXT, direction TEXT, channel_message_id TEXT, '
    "reply_to TEXT, segments TEXT, status TEXT)")
DESKTOP_SCHEMA = (
    'CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id TEXT, direction TEXT, '
    'text TEXT, segments TEXT, status TEXT, error TEXT, created_at TEXT)')
CATALOG_SCHEMA = (
    'CREATE TABLE chat_catalog (id INTEGER PRIMARY KEY, checkpoint_id INTEGER, source TEXT NOT NULL, '
    'conversation TEXT NOT NULL, place TEXT NOT NULL, from_message_id INTEGER NOT NULL, '
    'to_message_id INTEGER NOT NULL, start_time TEXT, end_time TEXT, topics TEXT NOT NULL, '
    'keywords TEXT NOT NULL, gist TEXT NOT NULL, created REAL NOT NULL)')


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project_path(self.root, 'harness/memory-embeddings.json').write_text('{"enabled": false}', encoding='utf-8')
        self.qq = project_path(self.root, 'data/qq/qq.db')
        with closing(sqlite3.connect(self.qq)) as db:
            db.execute(QQ_SCHEMA)
            db.executemany(
                'INSERT INTO messages(id,ts,chat_id,user_id,text,conversation_id,sender_id,'
                "direction,channel_message_id,reply_to,segments,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(1, '2026-09-01 10:00:00', 'private:20001', '20001',
                  '我们下周三去北门那家面馆吃面', 'c1', '20001', 'inbound', '9001', '',
                  json.dumps([{'type': 'image', 'path': 'C:/fish/photo.jpg'}], ensure_ascii=False),
                  'processed'),
                 (2, '2026-09-02 10:00:00', 'private:20001', '20001',
                  '很久以前看过一场电影票，一直记得', 'c1', '20001', 'inbound', '9002', '',
                  '[]', 'processed')])
            db.commit()
        self.desktop = project_path(self.root, 'data/qq/desktop-chat.db')
        with closing(sqlite3.connect(self.desktop)) as db:
            db.execute(DESKTOP_SCHEMA)
            db.execute('CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, mode TEXT)')
            db.execute("INSERT INTO conversations VALUES('d1','本地聊天','chat')")
            db.commit()
        self.experience = project_path(self.root, 'data/social/experience.db')
        with closing(sqlite3.connect(self.experience)) as db:
            db.execute(CATALOG_SCHEMA)
            db.execute(
                'INSERT INTO chat_catalog VALUES(1,1,?,?,?,?,?,?,?,?,?,?,?)',
                ('qq.db', 'c1', 'private:20001', 1, 2, '2026-09-01', '2026-09-02',
                 '面馆', '北门,面馆,电影票', '约好去北门面馆吃面，也提过电影票', time.time()))
            db.commit()
        self.doc = doc_store.ingest(self.root, '北门面馆资料',
                                    '北门那家面馆周末排队很久，最好提前去。', kind='txt')
        self.memory = MemoryStore(self.root, 'qq').save(
            '主人喜欢北门那家面馆', category='偏好', scope='owner', source='qq:1',
            source_text='主人原话：北门那家面馆好吃')
        store_digest(self.root, 'day', '2026-09-02',
                     '今天约好去北门面馆吃面，也提过电影票。', ['事件'], {'memories': 2},
                     time.time(), 1)

    # -- helpers -----------------------------------------------------------
    def insert_message(self, message_id, text, *, conversation='c1', ts='2026-09-03 10:00:00'):
        with closing(sqlite3.connect(self.qq)) as db:
            db.execute(
                'INSERT INTO messages(id,ts,chat_id,user_id,text,conversation_id,sender_id,'
                "direction,channel_message_id,reply_to,segments,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, ts, 'private:20001', '20001', text, conversation, '20001',
                 'inbound', str(9000 + message_id), '', '[]', 'processed'))
            db.commit()

    def references(self, query, limit=20):
        return [hit['reference'] for hit in knowledge.search(self.root, query, limit=limit)['hits']]

    # -- three levels ------------------------------------------------------
    def test_message_hit_exposes_index_overview_and_original(self):
        result = knowledge.search(self.root, '北门面馆')
        hit = next(item for item in result['hits'] if item['reference'] == 'message:qq:1')
        self.assertEqual(hit['level'], 'index')
        self.assertEqual(hit['levels']['index']['reference'], 'message:qq:1')
        self.assertEqual(hit['levels']['overview']['kind'], 'catalog')
        self.assertEqual(hit['levels']['overview']['reference'], 'catalog:1')
        self.assertEqual(hit['levels']['original']['reference'], 'message:qq:1')
        opened = knowledge.open(self.root, 'message:qq:1')
        self.assertEqual(opened['level'], 'original')
        self.assertEqual(opened['message']['text'], '我们下周三去北门那家面馆吃面')
        self.assertEqual(opened['attachments'][0]['path'], 'C:/fish/photo.jpg')
        self.assertTrue(opened['context'])
        original = knowledge.open(self.root, 'catalog:1', level='original')
        self.assertEqual(original['level'], 'original')
        self.assertEqual([row['id'] for row in original['messages']], [1, 2])

    def test_document_memory_and_digest_hits_open_their_own_store(self):
        refs = self.references('北门面馆')
        doc_ref = next(ref for ref in refs if ref.startswith('doc:'))
        memory_ref = next(ref for ref in refs if ref.startswith('memory:'))
        digest_ref = next(ref for ref in refs if ref.startswith('digest:'))

        opened_doc = knowledge.open(self.root, doc_ref)
        self.assertTrue(opened_doc['original_path'])
        self.assertTrue(Path(opened_doc['original_path']).is_file())
        self.assertEqual(opened_doc['chunk']['ord'], 1)

        opened_memory = knowledge.open(self.root, memory_ref)
        self.assertEqual(opened_memory['memory']['id'], self.memory['id'])
        self.assertGreaterEqual(len(opened_memory['revisions']), 1)
        self.assertIn('北门', opened_memory['memory']['text'])

        opened_digest = knowledge.open(self.root, digest_ref)
        self.assertEqual(opened_digest['digest']['day'], '2026-09-02')
        self.assertIn('面馆', opened_digest['digest']['text'])

    def test_long_query_keeps_head_and_tail_and_old_rows(self):
        # Many newer rows that do not match; the old matching rows must remain.
        for index in range(200, 230):
            self.insert_message(index, '今天只是普通闲聊，没有要找的内容', ts='2026-10-01 10:00:00')
        refs = self.references('北门面馆 很久以前 电影票')
        self.assertIn('message:qq:1', refs)
        self.assertIn('message:qq:2', refs)
        result = knowledge.search(self.root, '北门面馆 很久以前 电影票')
        self.assertGreater(result['retrieval']['terms']['count'], 3)
        self.assertFalse(result['retrieval']['semantic']['used'])

    # -- index follows the real store -------------------------------------
    def test_new_changed_and_deleted_messages_change_later_results(self):
        self.assertNotIn('message:qq:7', self.references('新开的书店'))
        self.insert_message(7, '新开的书店在河边')
        self.assertIn('message:qq:7', self.references('新开的书店'))
        with closing(sqlite3.connect(self.qq)) as db:
            db.execute("UPDATE messages SET text='新开的书店搬去了山上' WHERE id=7")
            db.commit()
        self.assertNotIn('message:qq:7', self.references('河边'))
        self.assertIn('message:qq:7', self.references('山上'))
        with closing(sqlite3.connect(self.qq)) as db:
            db.execute('DELETE FROM messages WHERE id=7')
            db.commit()
        self.assertNotIn('message:qq:7', self.references('山上'))
        # The explicit sync is what notices the deletion; it is not MAX-only.
        self.assertEqual(history_index.sync(self.root)['deleted'], 0)

    def test_memory_correction_and_deletion_change_results(self):
        self.assertIn('memory:' + self.memory['id'], self.references('北门'))
        MemoryStore(self.root, 'qq').save(
            '主人喜欢南门那家面馆', category='偏好', scope='owner', source='qq:1',
            memory_id=self.memory['id'], source_text='主人更正：是南门')
        refs = self.references('北门')
        self.assertNotIn('memory:' + self.memory['id'], refs)
        self.assertIn('memory:' + self.memory['id'], self.references('南门'))
        MemoryStore(self.root, 'qq').delete(self.memory['id'])
        self.assertNotIn('memory:' + self.memory['id'], self.references('南门'))

    def test_document_deletion_is_forgotten(self):
        saved = doc_store.ingest(self.root, '绝密码头计划', '绝密码头计划在周三凌晨执行。', kind='txt')
        self.assertIn('doc:' + saved['doc_id'], ' '.join(self.references('绝密码头计划')))
        doc_store.delete_doc(self.root, saved['doc_id'])
        refs = self.references('绝密码头计划')
        self.assertNotIn('doc:' + saved['doc_id'], refs)

    def test_workbench_delete_conversations_forgets_the_index(self):
        with closing(sqlite3.connect(self.desktop)) as db:
            db.execute(
                "INSERT INTO messages(id,conversation_id,direction,text,segments,status,error,created_at) "
                "VALUES(11,'d1','inbound','本地约定周六去看展览','[]','done','','2026-10-01 10:00:00')")
            db.commit()
        self.assertIn('message:desktop:11', self.references('本地约定周六去看展览'))

        class FakeChat:
            _state_lock = threading.RLock()
            active_cid = ''

            def __init__(self, path):
                self.db = path

            def connect(self):
                return sqlite3.connect(self.db)

        class FakeRuntime:
            class execution:
                @staticmethod
                def active_conversation():
                    return ''

        page = Workbench(self.root)
        page._chat_instance = FakeChat(self.desktop)
        page.runtime = lambda: FakeRuntime()
        removed = page.delete_conversations({'conversation_ids': ['d1']})
        self.assertEqual(removed['messages'], 1)
        self.assertNotIn('message:desktop:11', self.references('本地约定周六去看展览'))

    # -- honest lexical status --------------------------------------------
    def test_lexical_only_status_has_reason_and_still_returns_hits(self):
        result = knowledge.search(self.root, '北门面馆')
        retrieval = result['retrieval']
        self.assertTrue(retrieval['fts5'])
        self.assertEqual(retrieval['mode'], 'lexical')
        self.assertIn('仅词法', retrieval['label'])
        self.assertFalse(retrieval['semantic']['used'])
        self.assertTrue(retrieval['semantic']['reason'])
        self.assertTrue(result['hits'])
        for hit in result['hits']:
            self.assertIsNone(hit['levels']['index'].get('semantic'))

    # -- entries -----------------------------------------------------------
    def test_group_document_is_searchable_and_readable_from_other_places(self):
        saved = doc_store.ingest(self.root, '群甲观测手册', '星环观测站在周五开放。',
                                 scope='chat:group:12345', source='qq-attachment',
                                 metadata={'sender_id': '30001', 'source_reference': 'message:qq:31'})
        page = Workbench(self.root)
        result = page.dispatch('knowledge.search', {'query': '星环观测站', 'limit': 20})
        hit = next(item for item in result['hits'] if item['kind'] == 'document')
        self.assertEqual(hit['reference'], 'doc:' + saved['doc_id'] + ':1')
        opened = page.dispatch('knowledge.open', {'reference': hit['reference']})
        self.assertEqual(opened['original_path'], saved['original_path'])

    def test_library_search_can_open_the_group_original_file(self):
        saved = doc_store.ingest(self.root, '群甲星环手册', '星环观测站在周五开放。',
                                 scope='chat:group:12345')
        page = Workbench(self.root)
        with patch('os.startfile'):
            opened = page.dispatch('library.open', {'doc_id': saved['doc_id']})
        self.assertEqual(opened['path'], saved['original_path'])
        found = page.dispatch('library.search', {'query': '星环观测站'})
        self.assertEqual(found['results'][0]['doc_id'], saved['doc_id'])

    def test_legacy_and_unified_tools_find_the_same_group_document(self):
        from governance import Governance
        from tool_host import ToolHost
        Governance(self.root).save({}, owner_id='20001')
        saved = doc_store.ingest(self.root, '群甲星环手册', '星环观测站在周五开放。',
                                 scope='chat:group:12345')
        host = ToolHost(self.root)
        for session, scope, sender in (('owner-check', 'owner', '20001'),
                                       ('group-fish:group:54321', 'chat:group:54321', '30002')):
            host.bind('qq', session, scope, sender=sender)
            unified = host.execute('qq', session, 'fish_knowledge_search', {'query': '星环观测站'}, 'search')
            self.assertEqual(unified['hits'][0]['reference'], 'doc:' + saved['doc_id'] + ':1')
            legacy = host.execute('qq', session, 'fish_doc_search', {'query': '星环观测站'}, 'doc-search')
            self.assertEqual(legacy['results'][0]['doc_id'], saved['doc_id'])
            opened = host.execute('qq', session, 'fish_doc_read', {'doc_id': saved['doc_id']}, 'read')
            self.assertEqual(opened['scope'], 'chat:group:12345')
            self.assertEqual(opened['original_path'], saved['original_path'])

    def test_document_source_and_revision_survive_search_and_open(self):
        prior = doc_store.ingest(self.root, '群甲手册', '星环观测站原定周五开放。',
                                 scope='chat:group:12345', source='qq-attachment',
                                 metadata={'sender_id': '30001', 'source_reference': 'message:qq:31'})
        saved = doc_store.ingest(self.root, '群甲手册', '星环观测站更正为周六开放。',
                                 scope='chat:group:12345', source='qq-attachment',
                                 metadata={'sender_id': '30001', 'source_reference': 'message:qq:32'})
        page = Workbench(self.root)
        found = page.dispatch('knowledge.search', {'query': '星环观测站', 'scope': 'chat:group:54321'})
        hit = next(item for item in found['hits'] if item['kind'] == 'document')
        self.assertEqual(hit['scope'], 'chat:group:12345')
        self.assertEqual(hit['source'], 'qq-attachment')
        self.assertEqual(hit['metadata']['sender_id'], '30001')
        self.assertEqual(hit['version'], 2)
        self.assertEqual(hit['parent_version'], prior['doc_id'])
        self.assertEqual(hit['links'][0]['reference'], 'message:qq:32')
        opened = page.dispatch('knowledge.open', {'reference': hit['reference']})
        self.assertEqual(opened['scope'], hit['scope'])
        self.assertEqual(opened['metadata'], hit['metadata'])
        self.assertEqual(opened['links'], hit['links'])
        self.assertEqual(opened['parent_version'], prior['doc_id'])
        old = page.dispatch('knowledge.open', {'reference': 'doc:' + prior['doc_id']})
        self.assertEqual(old['version'], 1)
        self.assertIn('原定周五', old['preview'][0]['text'])

    def test_document_query_reports_unindexed_and_missing_original_separately(self):
        original = self.root / 'unparsed.bin'
        original.write_bytes(b'original-bytes')
        saved = doc_store.ingest(self.root, '尚未解析的群文件', '', kind='file',
                                 scope='chat:group:12345', original_path=str(original))
        page = Workbench(self.root)
        found = page.dispatch('knowledge.search', {'query': '不存在的术语'})
        state = found['retrieval']['semantic']['channels']['documents']
        self.assertEqual(state['status'], 'no-match')
        self.assertEqual(state['unindexed'], 1)
        self.assertFalse(state['ready'])
        original.unlink()
        opened = page.dispatch('knowledge.open', {'reference': 'doc:' + saved['doc_id']})
        self.assertEqual(opened['original_status'], 'missing')
        self.assertIn('原件读取失败', opened['note'])

    def test_legacy_group_reference_still_opens_the_registered_original(self):
        shelf = doc_store.library_root(self.root, 'chat:group:12345')
        original = shelf / 'web' / 'old.md'
        original.parent.mkdir(parents=True)
        original.write_text('历史星环观测手册', encoding='utf-8')
        index = shelf / 'web' / 'index' / 'old.json'
        index.parent.mkdir()
        index.write_text(json.dumps({'doc_id': 'legacy-star', 'rel_path': 'web/old.md',
                                     'title': '原有群书架', 'url': 'https://example.invalid/star'}), encoding='utf-8')
        doc_store.import_legacy_web(self.root)
        opened = Workbench(self.root).dispatch('knowledge.open', {'reference': 'doc:legacy-star'})
        self.assertEqual(opened['original_path'], str(original.resolve()))
        self.assertEqual(opened['scope'], 'chat:group:12345')
        self.assertNotEqual(opened['doc_id'], 'legacy-star')

    def test_search_and_original_stay_in_the_requested_instance(self):
        # W01's fixture registers the second physical instance; W02 owns its creation UI.
        directory = self.root / 'data/instances/other'
        directory.mkdir()
        (directory / 'instance.json').write_text(json.dumps({'id': 'other', 'host_root': str(self.root)}), encoding='utf-8')
        catalog = self.root / 'data/host/instances.json'
        entries = json.loads(catalog.read_text(encoding='utf-8'))
        entries['instances'].append({'id': 'other', 'name': '另一份Fish'})
        catalog.write_text(json.dumps(entries), encoding='utf-8')
        context = InstanceContext(self.root, 'other', directory)
        config = project_path(context, 'harness/memory-embeddings.json')
        config.parent.mkdir()
        config.write_text('{"enabled": false}', encoding='utf-8')
        mine = doc_store.ingest(self.root, '星环手册', '星环手册属于初始Fish。', scope='chat:group:12345')
        theirs = doc_store.ingest(context.root, '星环手册', '星环手册属于另一份Fish。', scope='chat:group:12345')
        page = Workbench(self.root)
        other = Workbench(context)
        found = page.dispatch('knowledge.search', {'instance_id': 'main', 'query': '星环手册'})
        foreign = other.dispatch('knowledge.search', {'instance_id': 'other', 'query': '星环手册'})
        self.assertEqual([hit['reference'] for hit in found['hits']], ['doc:' + mine['doc_id'] + ':1'])
        self.assertEqual([hit['reference'] for hit in foreign['hits']], ['doc:' + theirs['doc_id'] + ':1'])
        opened = other.dispatch('knowledge.open', {'instance_id': 'other', 'reference': foreign['hits'][0]['reference']})
        self.assertTrue(Path(opened['original_path']).is_relative_to(directory))
        with self.assertRaises(ValueError):
            page.dispatch('knowledge.open', {'reference': foreign['hits'][0]['reference']})
        with self.assertRaises(ValueError):
            other.dispatch('knowledge.open', {'reference': found['hits'][0]['reference']})

    def test_workbench_read_actions_use_the_same_entry(self):
        page = Workbench(self.root)
        result = page.dispatch('knowledge.search', {'query': '北门面馆', 'limit': 10})
        self.assertTrue(result['hits'])
        self.assertTrue(result['retrieval']['fts5'])
        opened = page.dispatch('knowledge.open', {'reference': 'message:qq:1'})
        self.assertEqual(opened['message']['text'], '我们下周三去北门那家面馆吃面')

    def test_fish_tools_are_registered_and_use_the_entry(self):
        from tool_host import ToolHost, builtins
        names = {item['name'] for item in builtins('qq')}
        self.assertIn('fish_knowledge_search', names)
        self.assertIn('fish_knowledge_open', names)
        host = ToolHost(self.root)
        host.identity = lambda role, session: {
            'role': role, 'session': session, 'scope': 'owner', 'owner': 1,
            'desktop': 1, 'sender': '20001'}
        found = host.execute('qq', 'knowledge-check', 'fish_knowledge_search',
                             {'query': '北门面馆'}, 'call-knowledge-1')
        self.assertTrue(found['hits'])
        opened = host.execute('qq', 'knowledge-check', 'fish_knowledge_open',
                              {'reference': 'message:qq:1'}, 'call-knowledge-2')
        self.assertIn('面馆', opened['message']['text'])


if __name__ == '__main__':
    unittest.main()
