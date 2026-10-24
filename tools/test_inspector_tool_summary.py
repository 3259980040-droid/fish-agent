"""Offline checks for the structured summary of tool results in request snapshots."""
import json
import tempfile
import unittest
from pathlib import Path

from request_inspector import RequestInspector

BODY = '今晚八点开组会，记得带笔记本'
GROUP = '7400010000'
CANARY = 'CANARY-群消息正文-不得出现在摘要里'


def history_payload():
    return {
        'group_id': GROUP,
        'chat': 'group:' + GROUP,
        'authorized': True,
        'window': {'minutes': 60, 'from': '2026-10-22 00:56:56', 'to': '2026-10-22 01:56:56'},
        'messages': [{'message_id': str(7400010001 + i), 'text': BODY + str(i),
                      'sender_id': '2100000001', 'sender_name': '成员', 'direction': 'inbound'}
                     for i in range(4)],
        'references': [],
        'counts': {'returned': 4, 'inbound': 4, 'bot_outbound': 0},
        'gaps': ['该群未开启旁听或学习，未监听时段的资料不完整', '本地没有该发送者的名称记录：2100000001'],
        'observed_from': ['main', 'space:' + 'a' * 10],
        'source': 'deepseek-search',
        'text': 'x' * 3000,
    }


def coverage_payload():
    """One group read shaped exactly as group_read.read_group returns it."""
    return {
        'group_id': GROUP,
        'chat': 'group:' + GROUP,
        'authorized': True,
        'window': {'minutes': 120, 'from': '2026-02-12 08:40:01', 'to': '2026-02-12 10:40:01'},
        'messages': [{'message_id': str(7400010001 + i), 'text': CANARY + str(i),
                      'sender_id': '2100000001', 'sender_name': '成员', 'direction': 'inbound'}
                     for i in range(2)],
        'references': [], 'more': False, 'next_before': None,
        'counts': {'returned': 2, 'inbound': 2, 'bot_outbound': 0},
        'gaps': ['该群未开启旁听或学习，未监听时段的资料不完整'],
        'names_note': '本地没有该发送者的名称记录：2100000001',
        'coverage': {'requested_minutes': 120,
                     'first_message_time': '2026-02-12 10:00:01',
                     'last_message_time': '2026-02-12 10:40:01',
                     'span_minutes': 40, 'source': 'none', 'complete': False,
                     'reason': '该群未开启旁听或学习，未监听时段没有记录',
                     'gaps_count': 1},
        'coverage_note': ('覆盖范围：已读 2 条，时间 2026-02-12 10:00:01 至 2026-02-12 10:40:01，'
                          '实际跨度 40 分钟，来源 none；不完整。'),
    }


class InspectorToolSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inspector = RequestInspector(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, content):
        self.inspector.capture('qq', {'session': 's1', 'scope': 'owner'}, {
            'provider': 'stub', 'model': 'stub-model', 'purpose': 'owner-private',
            'messages': [{'role': 'user', 'content': '查一下群里刚才说了什么'},
                         {'role': 'tool', 'tool_call_id': 'c1', 'content': content}]})
        return self.inspector.latest('qq', 's1')['payload']['messages'][-1]

    # 1. a JSON tool result becomes a fixed-size structure, not a wall of text
    def test_tool_result_becomes_structured_summary(self):
        captured = self.capture(json.dumps(history_payload(), ensure_ascii=False))
        self.assertEqual((captured['role'], captured['tool_call_id']), ('tool', 'c1'))
        summary = captured['content']
        self.assertIsInstance(summary, dict)
        self.assertEqual(summary['gaps_count'], 2)
        self.assertEqual(summary['window'], {'minutes': 60, 'from': '2026-10-22 00:56:56',
                                            'to': '2026-10-22 01:56:56'})
        self.assertEqual(summary['counts'], {'returned': 4, 'inbound': 4, 'bot_outbound': 0})
        self.assertEqual(summary['message_ids'], ['7400010001', '7400010002', '7400010003', '7400010004'])
        self.assertIs(summary['authorized'], True)
        self.assertEqual(summary['source'], ['deepseek-search'])
        self.assertEqual(summary['observed_from'], ['main', 'space:' + 'a' * 10])
        self.assertEqual(summary['text_chars'], 3000)
        self.assertIs(summary['truncated_original'], True)
        self.assertIn('messages', summary['keys'])
        self.assertEqual(len(summary['gaps']), 2)

    # 2. no group message body survives the summary
    def test_summary_keeps_no_message_body(self):
        payload = history_payload()
        payload['messages'][0]['text'] = BODY[:4] + '机密正文'
        captured = self.capture(json.dumps(payload, ensure_ascii=False))
        dumped = json.dumps(captured, ensure_ascii=False)
        self.assertNotIn('机密正文', dumped)
        self.assertNotIn(BODY, dumped)
        self.assertNotIn('x' * 100, dumped)
        self.assertNotIn('sender_name', dumped)
        # Ids and counts are the evidence that survives.
        self.assertIn('7400010001', dumped)
        self.assertIn('gaps_count', dumped)

    # 3. a fetched page keeps its shape fields, only the body becomes a length
    def test_fetched_page_is_summarized_without_its_text(self):
        page = {'ok': True, 'url': 'https://example.com/a?b=1&c=2', 'title': '一个页面',
                'text': '正' * 13000, 'truncated': True, 'source': 'deepseek-fetch'}
        captured = self.capture(json.dumps(page, ensure_ascii=False))
        summary = captured['content']
        self.assertIsInstance(summary, dict)
        self.assertIs(summary['ok'], True)
        self.assertEqual(summary['url'], 'https://example.com/a?[查询参数已隐藏]')
        self.assertEqual(summary['title'], '一个页面')
        self.assertEqual(summary['text_chars'], 13000)
        self.assertIs(summary['truncated'], True)
        self.assertNotIn('正' * 100, json.dumps(captured, ensure_ascii=False))

    # 4. the summary stays inside its fixed budget whatever the result size
    def test_summary_size_is_bounded(self):
        payload = {'gaps': ['缺口' + '很长的说明' * 40 for _ in range(9)],
                   'messages': [{'message_id': str(700000000 + i), 'text': BODY * 5} for i in range(80)],
                   'counts': {str(i): i for i in range(20)},
                   'keys_extra': 'k' * 500, 'url': 'https://example.com/' + 'p' * 900,
                   'title': 't' * 900, 'error': 'e' * 900, 'text': 'y' * 5000}
        captured = self.capture(json.dumps(payload, ensure_ascii=False))
        self.assertIsInstance(captured['content'], dict)
        self.assertLessEqual(len(json.dumps(captured['content'], ensure_ascii=False)), 1200)
        self.assertLessEqual(len(captured['content']['gaps']), 3)
        self.assertLessEqual(len(captured['content']['message_ids']), 5)

    # 5. plain tool text keeps the previous truncation
    def test_non_json_tool_content_still_truncated(self):
        captured = self.capture('自由文本' * 15000)
        self.assertIsInstance(captured['content'], str)
        self.assertEqual(len(captured['content']), 40000)
        self.assertTrue(captured['content'].startswith('自由文本'))
        listed = self.capture(json.dumps([{'message_id': '1', 'text': BODY}], ensure_ascii=False))
        self.assertIsInstance(listed['content'], str)
        self.assertIn(BODY, listed['content'])

    # 6. non-tool messages are untouched by the tool summary
    def test_user_message_text_is_not_summarized(self):
        captured = self.capture('{"ok": true, "text": "普通结果"}')
        messages = self.inspector.latest('qq', 's1')['payload']['messages']
        self.assertEqual(messages[0]['content'], '查一下群里刚才说了什么')
        self.assertIsInstance(captured['content'], dict)
        self.assertEqual(captured['content']['text_chars'], len('普通结果'))

    # 7. the nested coverage object survives: real first/last time, span, source, completeness
    def test_nested_coverage_reaches_the_summary(self):
        captured = self.capture(json.dumps(coverage_payload(), ensure_ascii=False))
        summary = captured['content']
        self.assertIsInstance(summary, dict)
        coverage = summary['coverage']
        self.assertEqual(coverage['requested_minutes'], 120)
        self.assertEqual(coverage['first_message_time'], '2026-02-12 10:00:01')
        self.assertEqual(coverage['last_message_time'], '2026-02-12 10:40:01')
        self.assertEqual(coverage['span_minutes'], 40)
        self.assertEqual(coverage['source'], 'none')
        self.assertIs(coverage['complete'], False)
        self.assertEqual(coverage['gaps_count'], 1)
        self.assertIn('未监听时段没有记录', coverage['reason'])
        # The requested window and the real span stay two different facts.
        self.assertEqual(summary['window']['minutes'], 120)
        self.assertEqual(summary['counts'], {'returned': 2, 'inbound': 2, 'bot_outbound': 0})
        dumped = json.dumps(captured, ensure_ascii=False)
        self.assertNotIn(CANARY, dumped)
        self.assertNotIn('sender_name', dumped)

    # 8. a long coverage payload loses keys and the reason long before its core scalars
    def test_oversized_coverage_keeps_its_core_scalars(self):
        payload = coverage_payload()
        payload['messages'] = [{'message_id': str(7400010000 + i), 'text': BODY}
                               for i in range(60)]
        payload['counts'] = {('count_%d' % i): i for i in range(20)}
        payload['counts']['returned'] = 60
        payload['gaps'] = ['缺口' + '很长的说明' * 40 for _ in range(9)]
        payload['coverage']['reason'] = '缺口说明' * 200
        for index in range(30):
            payload['extra_%d' % index] = 'k' * 40
        payload['url'] = 'https://example.com/' + 'p' * 900
        captured = self.capture(json.dumps(payload, ensure_ascii=False))
        summary = captured['content']
        self.assertIsInstance(summary, dict)
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False)), 1200)
        coverage = summary['coverage']
        self.assertEqual(coverage['first_message_time'], '2026-02-12 10:00:01')
        self.assertEqual(coverage['last_message_time'], '2026-02-12 10:40:01')
        self.assertEqual(coverage['span_minutes'], 40)
        self.assertEqual(coverage['source'], 'none')
        self.assertIs(coverage['complete'], False)
        self.assertEqual(coverage['gaps_count'], 1)
        self.assertNotIn(CANARY, json.dumps(captured, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
