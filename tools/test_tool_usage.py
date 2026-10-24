"""Offline checks for external tool-usage events and the per-day tool snapshot."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
import web_knowledge
from model_gateway import ModelLedger
from storage import database
from web_knowledge import WebKnowledge

SEARCH = '/v1/tools/search'
FETCH = '/v1/tools/fetch'


class _StubProvider(BaseHTTPRequestHandler):
    """Local stand-in for the provider tools endpoints."""
    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length:
            self.rfile.read(length)
        with self.server.lock:
            self.server.hits[self.path] = self.server.hits.get(self.path, 0) + 1
        status, payload = self.server.routes.get(self.path, (404, {'error': 'unknown path'}))
        data = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class ToolUsageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), _StubProvider)
        cls.server.daemon_threads = True
        cls.server.lock = threading.Lock()
        cls.server.hits = {}
        cls.server.routes = {}
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def setUp(self):
        self.server.hits.clear()
        self.server.routes.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.route(SEARCH, {'results': [{'title': '标题' + str(i), 'url': 'https://example.invalid/' + str(i),
                                        'summary': '摘要' + str(i)} for i in range(5)]})
        self.route(FETCH, {'title': 'Stub Page', 'content': 'x' * 200})

    def tearDown(self):
        self.temp.cleanup()

    def route(self, path, payload, status=200):
        self.server.routes[path] = (status, payload)

    def gateway(self, base=None):
        return patch.object(web_knowledge, 'ModelGateway',
            return_value=SimpleNamespace(key='synthetic-key', base=base or self.base))

    def allow_public(self):
        return patch.object(WebKnowledge, 'public_target', staticmethod(lambda url: str(url).strip()))

    def events(self):
        with database(ModelLedger(self.root, 'qq').path) as db:
            return [dict(row) for row in db.execute('SELECT * FROM tool_events ORDER BY created,id').fetchall()]

    # 1. a real search records one measured event
    def test_search_records_one_event_with_real_numbers(self):
        with self.gateway():
            result = WebKnowledge(self.root).search_tool('DeepSeek 最新版本', chat='')
        self.assertIs(result['ok'], True)
        rows = self.events()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['tool'], 'fish_web_search')
        self.assertEqual(row['provider'], 'deepseek-search')
        self.assertEqual(row['origin'], 'web-tool')
        self.assertEqual(row['ok'], 1)
        self.assertEqual(row['cached'], 0)
        self.assertEqual(row['result_count'], len(result['results']))
        self.assertEqual(row['text_chars'], sum(len(item['text']) for item in result['results']))
        self.assertGreater(row['text_chars'], 0)
        self.assertGreaterEqual(row['elapsed_ms'], 0)
        self.assertEqual(row['meter'], 'unknown')
        self.assertEqual(row['detail'], '')
        self.assertRegex(row['day'], r'^\d{4}-\d{2}-\d{2}$')
        # The event never carries the query string or the snippet bodies.
        dumped = json.dumps(row, ensure_ascii=False)
        self.assertNotIn('DeepSeek 最新版本', dumped)
        self.assertNotIn('摘要0', dumped)

    # 2. the cache hit is its own event and reports no provider metering
    def test_cache_hit_records_its_own_event(self):
        with self.gateway():
            service = WebKnowledge(self.root)
            service.search_tool('版本', chat='')
            second = service.search_tool('版本', chat='')
        self.assertIs(second['cached'], True)
        rows = self.events()
        self.assertEqual([row['cached'] for row in rows], [0, 1])
        self.assertEqual([row['ok'] for row in rows], [1, 1])
        self.assertEqual(self.server.hits.get(SEARCH, 0), 1)
        self.assertEqual(rows[1]['meter'], 'unknown')

    # 3. provider failures land as events holding only an error category
    def test_failures_record_only_the_error_category(self):
        self.route(SEARCH, {'error': 'boom'}, status=500)
        with self.gateway():
            result = WebKnowledge(self.root).search_tool('机密查询词', chat='')
        self.assertIs(result['ok'], False)
        row = self.events()[-1]
        self.assertEqual((row['tool'], row['ok'], row['cached']), ('fish_web_search', 0, 0))
        self.assertEqual(row['detail'], 'http_500')
        self.assertEqual(row['result_count'], 0)
        self.assertEqual(row['text_chars'], 0)
        self.assertNotIn('机密查询词', json.dumps(row, ensure_ascii=False))
        self.assertNotIn('boom', row['detail'])

        with self.gateway(), patch.object(web_knowledge.requests, 'post', side_effect=requests.Timeout('timed out')):
            result = WebKnowledge(self.root).search_tool('超时查询词', chat='')
        self.assertIs(result['ok'], False)
        row = self.events()[-1]
        self.assertEqual((row['ok'], row['detail'], row['meter']), (0, 'timeout', 'unknown'))
        self.assertNotIn('超时查询词', json.dumps(row, ensure_ascii=False))

    # 4. a refused page target is an event too, without the URL or its query string
    def test_refused_fetch_records_target_rejection(self):
        self.route(FETCH, {'error': 'nope'}, status=500)
        with self.gateway(), self.allow_public():
            result = WebKnowledge(self.root).fetch_tool('https://example.com/page?token=abcdef123')
        self.assertIs(result['ok'], False)
        row = self.events()[-1]
        self.assertEqual((row['tool'], row['provider'], row['ok']), ('fish_web_fetch', 'deepseek-fetch', 0))
        self.assertEqual(row['detail'], 'http_500')
        self.assertNotIn('example.com', json.dumps(row, ensure_ascii=False))
        self.assertNotIn('abcdef123', json.dumps(row, ensure_ascii=False))

    def test_fetch_success_records_result_and_characters(self):
        self.route(FETCH, {'title': '长页面', 'content': '正' * 13000})
        with self.gateway(), self.allow_public():
            result = WebKnowledge(self.root).fetch_tool('https://example.com/page')
        self.assertIs(result['ok'], True)
        row = self.events()[-1]
        self.assertEqual((row['tool'], row['provider'], row['ok'], row['cached']), ('fish_web_fetch', 'deepseek-fetch', 1, 0))
        self.assertEqual(row['result_count'], 1)
        self.assertEqual(row['text_chars'], len(result['text']))
        self.assertEqual(row['meter'], 'unknown')

    # 5. a provider reported metering field is kept verbatim; nothing is estimated
    def test_provider_metering_is_kept_verbatim(self):
        self.route(SEARCH, {'results': [{'title': 't', 'url': 'https://example.invalid/a', 'summary': 's'}],
                            'usage': {'prompt_tokens': 3, 'completion_tokens': 1}})
        with self.gateway():
            WebKnowledge(self.root).search_tool('版本', chat='')
        row = self.events()[-1]
        self.assertNotEqual(row['meter'], 'unknown')
        self.assertEqual(json.loads(row['meter']), {'prompt_tokens': 3, 'completion_tokens': 1})
        # No amount is ever derived from latency or result counts.
        self.assertNotIn('cost', row['meter'].lower())
        snapshot = ModelLedger(self.root, 'qq').tool_snapshot()
        self.assertEqual((snapshot['calls'], snapshot['meter_unknown'], snapshot['meter_reported']), (1, 0, 1))

    # 6. the snapshot aggregates exactly what the events recorded
    def test_tool_snapshot_matches_the_events(self):
        with self.gateway(), self.allow_public():
            service = WebKnowledge(self.root)
            service.search_tool('版本', chat='')
            service.search_tool('版本', chat='')
            service.fetch_tool('https://example.com/page')
            self.route(SEARCH, {'error': 'boom'}, status=500)
            service.search_tool('失败查询', chat='')
        rows = self.events()
        snapshot = ModelLedger(self.root, 'qq').tool_snapshot()
        self.assertEqual(snapshot['calls'], len(rows))
        self.assertEqual(snapshot['ok'], sum(row['ok'] for row in rows))
        self.assertEqual(snapshot['failed'], sum(1 for row in rows if not row['ok']))
        self.assertEqual(snapshot['cached'], sum(row['cached'] for row in rows))
        self.assertEqual(snapshot['elapsed_ms'], sum(row['elapsed_ms'] for row in rows))
        self.assertEqual(snapshot['result_count'], sum(row['result_count'] for row in rows))
        self.assertEqual(snapshot['text_chars'], sum(row['text_chars'] for row in rows))
        self.assertEqual(snapshot['meter_unknown'], sum(1 for row in rows if row['meter'] == 'unknown'))
        self.assertEqual({item['tool'] for item in snapshot['by_tool']}, {'fish_web_search', 'fish_web_fetch'})
        self.assertEqual((snapshot['calls'], snapshot['failed']), (4, 1))

    # 7. the main chat usage keeps its own table and meaning
    def test_request_ledger_semantics_are_unchanged(self):
        ledger = ModelLedger(self.root, 'qq')
        rid = ledger.reserve('desktop-chat')
        ledger.finish(rid, {'prompt_tokens': 5, 'completion_tokens': 7}, 'done')
        with self.gateway():
            WebKnowledge(self.root).search_tool('版本', chat='')
        snapshot = ledger.snapshot()
        self.assertEqual(set(snapshot), {'requests', 'input_tokens', 'output_tokens', 'unreported',
                                         'daily_limits_enabled', 'note'})
        self.assertEqual((snapshot['requests'], snapshot['input_tokens'], snapshot['output_tokens']), (1, 5, 7))
        self.assertEqual(snapshot['unreported'], 0)
        with database(ledger.path) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(requests)').fetchall()]
        self.assertEqual(columns, ['id', 'day', 'origin', 'status', 'input_tokens', 'output_tokens', 'created'])
        self.assertEqual(ledger.tool_snapshot()['calls'], 1)

    # 8. a broken ledger never breaks the tool call
    def test_ledger_failure_does_not_fail_the_tool(self):
        with self.gateway(), patch.object(ModelLedger, 'record_tool', side_effect=RuntimeError('db locked')):
            result = WebKnowledge(self.root).search_tool('版本', chat='')
        self.assertIs(result['ok'], True)
        self.assertTrue(result['results'])


if __name__ == '__main__':
    unittest.main()
