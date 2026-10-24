"""Offline checks for Fish's own document library: real files, sidecar index, tool wiring."""
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import fish_library
from fish_chat import CATALOGUES, call_tool, tool_definitions
from fish_library import library_dir, list_documents, read_document, save_public_page
from governance import Governance
from storage import write_json
from tool_host import ToolHost, builtins
from web_knowledge import WebKnowledge

OWNER = '10001'
# A public address literal: the real guard runs, and no name lookup or request happens.
PAGE = 'http://93.184.216.34/notes.md'
BODY = '# 公开资料\n\n第一段正文。\n第二段正文。\n'
REQUIRED = {'ok', 'doc_id', 'path', 'rel_path', 'url', 'title', 'sha256', 'bytes', 'truncated'}


class LibraryFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.web = library_dir(self.root) / 'web'

    def stored(self):
        return [item for item in self.web.iterdir() if item.is_file()]

    # 1
    def test_saves_one_plain_markdown_page(self):
        saved = save_public_page(self.root, PAGE, '公开笔记', BODY)
        self.assertTrue(saved['ok'], saved)
        self.assertTrue(REQUIRED.issubset(set(saved)))
        path = Path(saved['path'])
        self.assertTrue(path.is_file())
        self.assertEqual(BODY, path.read_text(encoding='utf-8'))
        self.assertEqual('.md', path.suffix)
        self.assertEqual(hashlib.sha256(BODY.encode('utf-8')).hexdigest(), saved['sha256'])
        self.assertEqual(len(BODY.encode('utf-8')), saved['bytes'])
        self.assertFalse(saved['truncated'])
        self.assertTrue(path.resolve().is_relative_to(library_dir(self.root).resolve()))
        record = json.loads((self.web / 'index' / (saved['doc_id'] + '.json')).read_text(encoding='utf-8'))
        for key in ('doc_id', 'rel_path', 'url', 'title', 'source', 'fetched_at', 'sha256',
                    'bytes', 'chars', 'truncated', 'saved_at'):
            self.assertIn(key, record)
        self.assertEqual(saved['sha256'], record['sha256'])
        self.assertEqual(saved['rel_path'], record['rel_path'])
        self.assertEqual(path.name, Path(record['rel_path']).name)
        self.assertTrue(record['rel_path'].startswith('web/'))
        self.assertEqual(PAGE, record['url'])
        self.assertEqual('fish_web_fetch', record['source'])
        self.assertEqual('公开笔记', record['title'])
        self.assertEqual(len(BODY), record['chars'])
        self.assertTrue(record['fetched_at'])
        self.assertEqual(BODY, read_document(self.root, saved['doc_id'])['text'])

    # 2
    def test_the_same_page_twice_writes_no_second_file(self):
        first = save_public_page(self.root, PAGE, '公开笔记', BODY)
        second = save_public_page(self.root, PAGE, '公开笔记', BODY)
        self.assertTrue(second['ok'], second)
        self.assertTrue(second['deduplicated'])
        self.assertEqual(first['doc_id'], second['doc_id'])
        self.assertEqual(first['path'], second['path'])
        self.assertEqual(1, len(self.stored()))
        self.assertEqual(1, len(list(self.web.joinpath('index').glob('*.json'))))

    # 3
    def test_an_overlong_body_is_truncated_and_marked(self):
        saved = save_public_page(self.root, PAGE, '长文', 'a' * 250000)
        self.assertTrue(saved['ok'], saved)
        self.assertTrue(saved['truncated'])
        self.assertTrue(saved['local_truncated'])
        self.assertFalse(saved['upstream_truncated'])
        self.assertEqual(fish_library.MAX_CHARS, len(Path(saved['path']).read_text(encoding='utf-8')))
        record = json.loads((self.web / 'index' / (saved['doc_id'] + '.json')).read_text(encoding='utf-8'))
        self.assertTrue(record['truncated'])
        self.assertEqual(fish_library.MAX_CHARS, record['chars'])

    # 4
    def test_a_body_over_the_disk_ceiling_is_refused_with_no_partial_file(self):
        refused = save_public_page(self.root, PAGE, '超大', 'a' * (fish_library.MAX_BYTES + 16))
        self.assertFalse(refused['ok'])
        self.assertIn('2 MiB', refused['error'])
        self.assertEqual([], self.stored())
        self.assertEqual([], list(self.web.joinpath('index').glob('*.json')))

    # 5
    def test_binary_content_is_refused_and_never_lands(self):
        refused = save_public_page(self.root, PAGE, '二进制', 'head\x00tail')
        self.assertFalse(refused['ok'])
        self.assertIn('二进制', refused['error'])
        self.assertEqual([], self.stored())
        self.assertEqual([], list_documents(self.root))

    # 6
    def test_blocked_suffixes_are_refused(self):
        for name in ('setup.exe', 'run.ps1', 'bundle.js', 'archive.zip', 'clip.mp4'):
            refused = save_public_page(self.root, 'http://93.184.216.34/' + name, '可疑', 'just text')
            self.assertFalse(refused['ok'], name)
            self.assertIn('拒绝保存', refused['error'])
        self.assertEqual([], self.stored())

    # 7
    def test_a_traversing_title_still_lands_inside_the_library(self):
        base = library_dir(self.root).resolve()
        for title in ('../../../../evil', '..\\..\\..\\evil', '../../etc/passwd'):
            saved = save_public_page(self.root, PAGE, title, BODY)
            self.assertTrue(saved['ok'], saved)
            path = Path(saved['path'])
            self.assertTrue(path.resolve().is_relative_to(base), title)
            self.assertEqual(base / 'web', path.resolve().parent)
            self.assertTrue(saved['rel_path'].startswith('web/'))
        for item in self.stored():
            escaped = [part for part in item.relative_to(base).parts if part in ('..',)]
            self.assertEqual([], escaped)

    # 8
    def test_loopback_and_cloud_metadata_never_become_a_document(self):
        with patch('requests.post') as post, patch('requests.get') as get:
            refused = save_public_page(self.root, 'http://127.0.0.1/x', '本机', 'hello')
            metadata = save_public_page(self.root, 'http://169.254.169.254/latest/meta-data', '元数据', 'hi')
        self.assertFalse(refused['ok'])
        self.assertIn('本机', refused['error'])
        self.assertFalse(metadata['ok'])
        post.assert_not_called()
        get.assert_not_called()
        with patch.object(WebKnowledge, 'public_target',
                          side_effect=ValueError('不允许访问本机、内网或云元数据域名')):
            guarded = save_public_page(self.root, 'https://example.com/a.md', 't', 'hello')
        self.assertFalse(guarded['ok'])
        self.assertIn('本机', guarded['error'])
        self.assertEqual([], self.stored())

    # 9
    def test_list_and_read_cover_present_and_missing_documents(self):
        first = save_public_page(self.root, PAGE, '第一篇', BODY)
        second = save_public_page(self.root, 'http://93.184.216.34/other.txt', '第二篇', 'other text')
        self.assertTrue(first['ok'] and second['ok'])
        listed = list_documents(self.root)
        self.assertEqual(2, len(listed))
        self.assertEqual({first['doc_id'], second['doc_id']}, {item['doc_id'] for item in listed})
        index = self.web / 'index' / (first['doc_id'] + '.json')
        record = json.loads(index.read_text(encoding='utf-8'))
        record['saved_at'] = '2020-01-01T00:00:00+00:00'
        index.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(second['doc_id'], list_documents(self.root)[0]['doc_id'])
        self.assertEqual(1, len(list_documents(self.root, limit=1)))
        read = read_document(self.root, second['doc_id'])
        self.assertTrue(read['ok'], read)
        self.assertEqual('other text', read['text'])
        self.assertEqual('http://93.184.216.34/other.txt', read['url'])
        missing = read_document(self.root, '0' * 16)
        self.assertFalse(missing['ok'])
        self.assertIn('没有这篇资料', missing['error'])
        self.assertFalse(read_document(self.root, '../../etc/passwd')['ok'])
        self.assertFalse(read_document(self.root, '')['ok'])
        Path(first['path']).unlink()
        gone = read_document(self.root, first['doc_id'])
        self.assertFalse(gone['ok'])
        self.assertIn('已不存在', gone['error'])

    # 14
    def test_the_same_body_from_another_address_reports_its_own_source(self):
        first = save_public_page(self.root, PAGE, '第一篇', BODY)
        second = save_public_page(self.root, 'http://93.184.216.34/copy.md', '第二篇', BODY)
        self.assertTrue(second['ok'], second)
        self.assertTrue(second['deduplicated'])
        self.assertEqual('http://93.184.216.34/copy.md', second['url'])
        self.assertEqual('第二篇', second['title'])
        self.assertTrue(second['fetched_at'])
        self.assertEqual(first['doc_id'], second['content_reused_from_doc_id'])
        self.assertEqual(first['path'], second['path'])
        self.assertEqual(1, len(self.stored()))
        record = json.loads((self.web / 'index' / (first['doc_id'] + '.json')).read_text(
            encoding='utf-8'))
        self.assertEqual(hashlib.sha256(BODY.encode('utf-8')).hexdigest(), record['sha256'])
        self.assertEqual(PAGE, record['url'])
        self.assertEqual(['http://93.184.216.34/notes.md', 'http://93.184.216.34/copy.md'],
                         [item['url'] for item in record['sources']])
        self.assertTrue(all(item['fetched_at'] for item in record['sources']))
        self.assertEqual(1, len(list(self.web.joinpath('index').glob('*.json'))))

    # 15
    def test_the_same_address_twice_does_not_grow_the_source_list(self):
        first = save_public_page(self.root, PAGE, '公开笔记', BODY)
        second = save_public_page(self.root, PAGE, '公开笔记', BODY)
        self.assertTrue(second['deduplicated'])
        self.assertEqual(PAGE, second['url'])
        self.assertEqual(first['doc_id'], second['content_reused_from_doc_id'])
        record = json.loads((self.web / 'index' / (first['doc_id'] + '.json')).read_text(
            encoding='utf-8'))
        self.assertEqual(1, len(record['sources']))
        self.assertEqual(PAGE, record['sources'][0]['url'])
        self.assertEqual(1, len(self.stored()))

    # 10
    def test_the_library_has_no_process_or_shell_route(self):
        source = Path(fish_library.__file__).read_text(encoding='utf-8')
        for token in ('subprocess', 'os.system', 'shutil.which', 'Popen'):
            self.assertNotIn(token, source)


class LibraryToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'harness').mkdir(parents=True, exist_ok=True)
        write_json(self.root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'model': 'fake-model', 'base_url': 'https://fake.invalid',
                        'apiKeyEnv': 'FISH_TEST_QQ_KEY'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.root / 'channels/fish_qq/config.json', {
            'allowlist': {'owner_id': OWNER, 'group_ids': []},
            'bot': {'display_name': '大肥鱼'}})
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        self.host = ToolHost(self.root)
        self.host.serve(background_workers=False, listen_port=0)
        self.addCleanup(self.stop_host)
        config = json.loads((self.root / 'harness/agents.json').read_text(encoding='utf-8'))
        config['runtime']['tool_host_port'] = int(self.host.server.server_address[1])
        write_json(self.root / 'harness/agents.json', config)
        self.session = 'qq-private:' + uuid.uuid4().hex
        self.host.bind('qq', self.session, 'owner', OWNER, False, chat_mode='owner')

    def stop_host(self):
        self.host.stop.set()
        self.host.server.shutdown()
        self.host.server.server_close()

    def stored(self):
        web = library_dir(self.root) / 'web'
        return [item for item in web.iterdir() if item.is_file()]

    def call(self, name, arguments):
        return call_tool(self.root, 'qq', self.session, name, arguments, uuid.uuid4().hex, 30)

    # 11
    def test_the_one_table_offers_saving_and_organizing_on_every_surface(self):
        self.assertEqual(1, len(CATALOGUES))
        for context in ('owner-private', 'owner-desktop', 'group'):
            offered = [item['function']['name'] for item in tool_definitions('qq', context)]
            self.assertIn('fish_doc_save', offered, context)
            self.assertIn('fish_doc_organize', offered, context)
        definition = next(row for row in builtins('qq') if row['name'] == 'fish_doc_save')
        self.assertEqual(['url'], definition['parameters']['required'])
        self.assertIn('url', definition['parameters']['properties'])
        self.assertIn('title', definition['parameters']['properties'])
        organize = next(row for row in builtins('qq') if row['name'] == 'fish_doc_organize')
        self.assertEqual(['action'], organize['parameters']['required'])
        self.assertEqual(['create', 'list', 'move'],
                         organize['parameters']['properties']['action']['enum'])

    def group_session(self, chat='group:1043230169'):
        session = 'group-fish:' + chat
        self.host.bind('qq', session, 'chat:' + chat, '2100000001', False)
        return session, chat

    def group_root(self, chat):
        """The group's own shelf under the library: one directory per bound scope."""
        return library_dir(self.root) / 'groups' / ('chat:' + chat).replace(':', '_')

    # 17
    def test_a_group_save_lands_in_the_group_shelf_and_never_the_owner_one(self):
        session, chat = self.group_session()
        page = {'ok': True, 'url': PAGE, 'title': '群里读到的页', 'text': BODY,
                'truncated': False, 'source': 'deepseek-fetch'}
        with patch.object(WebKnowledge, 'fetch_tool', return_value=page):
            value = call_tool(self.root, 'qq', session, 'fish_doc_save', {'url': PAGE},
                              uuid.uuid4().hex, 30)
        self.assertTrue(value['ok'], value)
        path = Path(value['path'])
        self.assertTrue(path.is_relative_to(self.group_root(chat)), path)
        self.assertTrue(path.is_file())
        self.assertEqual(BODY, path.read_text(encoding='utf-8'))
        # 主人的资料目录还是空的：群里的保存碰不到它。
        self.assertEqual([], [item for item in (library_dir(self.root) / 'web').iterdir()
                              if item.is_file()])

    # 18
    def test_she_keeps_her_own_folders_and_a_traversal_never_becomes_a_path(self):
        page = {'ok': True, 'url': PAGE, 'title': '网页标题', 'text': BODY,
                'truncated': False, 'source': 'deepseek-fetch'}
        with patch.object(WebKnowledge, 'fetch_tool', return_value=page):
            saved = self.call('fish_doc_save', {'url': PAGE})
        created = self.call('fish_doc_organize', {'action': 'create', 'name': '网页笔记'})
        self.assertTrue(created['ok'], created)
        self.assertEqual('folders/网页笔记', created['folder'])
        moved = self.call('fish_doc_organize',
                          {'action': 'move', 'name': '网页笔记', 'doc_id': saved['doc_id']})
        self.assertTrue(moved['ok'], moved)
        self.assertTrue(moved['rel_path'].startswith('folders/网页笔记/'))
        listing = self.call('fish_doc_organize', {'action': 'list'})
        self.assertEqual(['网页笔记'], listing['folders'])
        self.assertEqual([saved['doc_id']], [row['doc_id'] for row in listing['documents']])
        self.assertEqual('网页笔记', listing['documents'][0]['folder'])
        # 归档之后原来那份资料仍然读得回来：旁注里的路径跟着文件走。
        self.assertEqual(BODY, read_document(self.root, saved['doc_id'])['text'])
        escaped = self.call('fish_doc_organize', {'action': 'create', 'name': '../' + 'escape'})
        self.assertTrue(escaped['folder'].startswith('folders/'))
        self.assertNotIn('..', escaped['folder'])

    # 12
    def test_a_model_call_saves_the_real_file_and_reports_it(self):
        page = {'ok': True, 'url': 'http://93.184.216.34/notes.md', 'title': '网页标题',
                'text': BODY, 'truncated': False, 'source': 'deepseek-fetch'}
        with patch.object(WebKnowledge, 'fetch_tool', return_value=page):
            value = self.call('fish_doc_save', {'url': 'http://93.184.216.34/notes.md'})
        self.assertTrue(value['ok'], value)
        path = Path(value['path'])
        self.assertTrue(path.is_file())
        self.assertEqual(BODY, path.read_text(encoding='utf-8'))
        self.assertEqual('http://93.184.216.34/notes.md', value['url'])
        self.assertEqual(hashlib.sha256(BODY.encode('utf-8')).hexdigest(), value['sha256'])
        self.assertEqual('网页标题', value['title'])
        with patch.object(WebKnowledge, 'fetch_tool', return_value=page):
            again = self.call('fish_doc_save', {'url': 'http://93.184.216.34/notes.md', 'title': '指定标题'})
        self.assertTrue(again['deduplicated'])
        self.assertEqual(path, Path(again['path']))
        self.assertEqual(1, len(self.stored()))

    # 13
    def test_a_failed_read_returns_the_error_fact_and_saves_nothing(self):
        failed = {'ok': False, 'url': 'http://93.184.216.34/notes.md', 'error': '网络请求失败（Timeout）'}
        with patch.object(WebKnowledge, 'fetch_tool', return_value=failed):
            value = self.call('fish_doc_save', {'url': 'http://93.184.216.34/notes.md'})
        self.assertFalse(value['ok'])
        self.assertIn('网络请求失败', value['error'])
        self.assertEqual([], self.stored())
        self.assertEqual([], list_documents(self.root))

    def sidecar(self, doc_id):
        return json.loads((library_dir(self.root) / 'web/index' / (doc_id + '.json')).read_text(
            encoding='utf-8'))

    # 16
    def test_a_page_the_reader_already_cut_keeps_that_level_through_the_tool(self):
        page = {'ok': True, 'url': 'http://93.184.216.34/long.md', 'title': '长页',
                'text': 'x' * 12000, 'truncated': True, 'source': 'deepseek-fetch'}
        with patch.object(WebKnowledge, 'fetch_tool', return_value=page):
            value = self.call('fish_doc_save', {'url': 'http://93.184.216.34/long.md'})
        self.assertTrue(value['ok'], value)
        self.assertTrue(value['upstream_truncated'])
        self.assertFalse(value['local_truncated'])
        self.assertTrue(value['truncated'])
        record = self.sidecar(value['doc_id'])
        self.assertTrue(record['upstream_truncated'])
        self.assertFalse(record['local_truncated'])
        self.assertTrue(record['truncated'])
        self.assertEqual(12000, record['chars'])
        self.assertEqual(12000, len(Path(value['path']).read_text(encoding='utf-8')))

    # 17
    def test_a_page_within_both_limits_stays_untruncated(self):
        page = {'ok': True, 'url': 'http://93.184.216.34/notes.md', 'title': '网页标题',
                'text': BODY, 'truncated': False, 'source': 'deepseek-fetch'}
        with patch.object(WebKnowledge, 'fetch_tool', return_value=page):
            value = self.call('fish_doc_save', {'url': 'http://93.184.216.34/notes.md'})
        self.assertTrue(value['ok'], value)
        self.assertFalse(value['upstream_truncated'])
        self.assertFalse(value['local_truncated'])
        self.assertFalse(value['truncated'])
        record = self.sidecar(value['doc_id'])
        self.assertFalse(record['upstream_truncated'])
        self.assertFalse(record['local_truncated'])
        self.assertFalse(record['truncated'])

    # 18
    def test_a_body_this_write_cuts_is_marked_local_only(self):
        page = {'ok': True, 'url': 'http://93.184.216.34/huge.md', 'title': '超长',
                'text': 'a' * 250000, 'truncated': False, 'source': 'deepseek-fetch'}
        with patch.object(WebKnowledge, 'fetch_tool', return_value=page):
            value = self.call('fish_doc_save', {'url': 'http://93.184.216.34/huge.md'})
        self.assertTrue(value['ok'], value)
        self.assertTrue(value['local_truncated'])
        self.assertFalse(value['upstream_truncated'])
        self.assertTrue(value['truncated'])
        record = self.sidecar(value['doc_id'])
        self.assertTrue(record['local_truncated'])
        self.assertFalse(record['upstream_truncated'])
        self.assertEqual(fish_library.MAX_CHARS, record['chars'])


if __name__ == '__main__':
    unittest.main()
