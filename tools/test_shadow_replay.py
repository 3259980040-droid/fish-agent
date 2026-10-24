"""Shadow isolation replay: eight decision shapes, each one case, all offline.

The fixture root is disposable and the model is a script, so these tests prove the
decision path (who is being answered, what is linked, what is excluded, why a run
was skipped) without a real model, a real group, or a real send. The evidence
files the replay writes are asserted field by field in test_evidence_files.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from test_social_base import Fixture
from group_policy import GroupPolicy
from group_tests import GroupTests
from social_context import SocialContext
import group_decide
import group_shadow_compare
import shadow_replay

OWNER = '123456789'
GROUP = '987654321'
CHAT = 'group:' + GROUP
A, B, C, D = '2100000001', '2100000002', '2100000003', '2100000004'
POLICY = {'engine': 'shadow', 'read': True, 'listen': True, 'learn': True, 'reply': True,
          'shadow_daily_limit': 60}


def message(mid, text, *, sender=A, name='群友甲', minutes_ago=2, reply_to='', direction='inbound',
            status='observed'):
    return {'message_id': mid, 'ts': '', 'minutes_ago': minutes_ago,
            'sender': {'id': sender, 'display_name': name}, 'text': text, 'reply_to': reply_to,
            'direction': direction, 'status': status}


class ReplayFixture(Fixture):
    """Shared root, owner persona, and the one helper every case speaks through."""

    def setUp(self):
        super().setUp()
        self.contexts = SocialContext(self.root)
        self.persona = self.contexts.save_persona('qq', '主人私聊人格', '私聊里的那个人格')
        self.contexts.set_default('qq', self.persona['id'])
        self.chat = CHAT

    def fixture(self, messages, *, name='case', policy=None, model='候选回复', **extra):
        data = {'name': name, 'chat': self.chat,
                'policy': {**POLICY, **(policy or {})},
                'model': {'text': model},
                'messages': messages}
        data.update(extra)
        return data

    def run_case(self, data, *, timeout=None, ledger=False):
        return shadow_replay.replay(self.root, data, timeout=timeout, ledger=ledger)

    def record(self, data, *, timeout=None, ledger=False):
        payload = self.run_case(data, timeout=timeout, ledger=ledger)
        self.assertEqual(1, len(payload['records']))
        record = payload['records'][0]
        self.assertFalse(record['real_model_called'])
        self.assertFalse(record['sent'])
        return record

    def targets(self, data, target):
        return {**data, 'targets': [target]}

    # 1
    def test_one_clear_question_gets_a_candidate_and_zero_allowance_goes_silent(self):
        data = self.fixture([message('q-1', '滑窗的右指针什么时候停', minutes_ago=2)], name='clear-question')
        record = self.record(data)
        self.assertEqual('', record['skipped'])
        self.assertEqual('候选回复', record['candidate'])
        self.assertEqual(1, record['model_calls'])
        self.assertIn('q-1', record['current'])
        self.assertEqual([], record['tools'])
        # The same fixture with no allowance left is a determinate silence, not a model judgement.
        silent = self.record(self.fixture([message('q-2', '再来一句', minutes_ago=1)],
                                          name='not-worth-a-call', policy={'shadow_daily_limit': 0}))
        self.assertEqual('daily-limit', silent['skipped'])
        self.assertEqual('', silent['candidate'])
        self.assertEqual(0, silent['model_calls'])

    # 2
    def test_interleaved_speakers_keep_the_right_quote_chain(self):
        data = self.fixture([
            message('a-1', '甲的问题：滑窗怎么写', sender=A, name='群友甲', minutes_ago=8),
            message('b-1', '乙插一句：我不太懂', sender=B, name='群友乙', minutes_ago=6),
            message('a-2', '甲补充：先写右指针', sender=A, name='群友甲', minutes_ago=4),
            message('b-2', '乙回甲：那我按你说的试', sender=B, name='群友乙', minutes_ago=1, reply_to='a-1'),
        ], name='interleaved', targets=['b-2'])
        record = self.record(data)
        self.assertEqual('b-2', record['request_view']['current_request']['focus_message_id'])
        self.assertEqual(B, record['request_view']['current_request']['focus_sender_id'])
        self.assertIn('群友乙', record['current'])
        self.assertIn('a-1', record['current'])
        self.assertEqual(['a-1'], [item['message_id'] for item in record['references']])
        self.assertEqual(A, record['references'][0]['sender_id'])
        self.assertEqual('甲的问题：滑窗怎么写', record['references'][0]['text'])
        self.assertEqual('explicit_reply_chain', record['request_view']['related_history'][0]['reason'])
        self.assertEqual('a-1', record['request_view']['related_history'][0]['message_id'])
        linked = json.dumps(record['request_view']['related_history'], ensure_ascii=False)
        self.assertNotIn('b-1', linked)

    # 3
    def test_two_inbound_messages_are_replayed_without_crossing(self):
        data = self.fixture([
            message('p-1', '丙问：滑窗的边界怎么定', sender=C, name='群友丙', minutes_ago=9),
            message('p-2', '丁问：打印机卡纸了怎么办', sender=D, name='群友丁', minutes_ago=7),
            message('m-1', '甲回丙：先把右指针走到头', sender=A, name='群友甲', minutes_ago=2, reply_to='p-1'),
            message('m-2', '乙回丁：先打开后盖', sender=B, name='群友乙', minutes_ago=2, reply_to='p-2'),
        ], name='simultaneous')
        first = self.record(self.targets(data, 'm-1'))
        second = self.record(self.targets(data, 'm-2'))
        self.assertEqual('m-1', first['request_view']['current_request']['focus_message_id'])
        self.assertEqual('m-2', second['request_view']['current_request']['focus_message_id'])
        self.assertIn('m-1', first['current'])
        self.assertNotIn('m-2', first['current'])
        self.assertIn('m-2', second['current'])
        self.assertNotIn('m-1', second['current'])
        self.assertEqual(['p-1'], [item['message_id'] for item in first['references']])
        self.assertEqual(['p-2'], [item['message_id'] for item in second['references']])
        self.assertEqual(['p-1'], [item['message_id'] for item in first['request_view']['related_history']
                                   if item['reason'] == 'explicit_reply_chain'])
        self.assertEqual(['p-2'], [item['message_id'] for item in second['request_view']['related_history']
                                   if item['reason'] == 'explicit_reply_chain'])
        # Unrelated rows are allowed to appear as excluded; they may not be linked.
        for record, foreign in ((first, 'p-2'), (second, 'p-1')):
            linked = json.dumps({'history': record['request_view']['related_history'],
                                 'quotes': record['request_view']['current_request']['quoted_references'],
                                 'references': record['references']}, ensure_ascii=False)
            self.assertNotIn(foreign, linked)

    # 4
    def test_a_topic_three_hours_old_is_excluded_with_a_reason(self):
        data = self.fixture([
            message('old-1', '三小时前的旧话题：周末去哪', sender=A, name='群友甲', minutes_ago=180),
            message('new-1', '现在这句：滑窗怎么写', sender=A, name='群友甲', minutes_ago=1),
        ], name='outside-window', targets=['new-1'])
        record = self.record(data)
        self.assertIn({'source': 'in:old-1', 'message_id': 'old-1', 'reason': 'outside_time_window'},
                      record['request_view']['excluded'])
        self.assertEqual(1, record['request_view']['window']['excluded_rows'])
        self.assertNotIn('周末去哪', json.dumps(record['history'], ensure_ascii=False))

    # 5
    def test_no_exact_link_stays_unmatched_and_never_reads_as_a_real_reply(self):
        data = self.fixture([
            message('in-1', '这句没人引用，也没回执', sender=A, name='群友甲', minutes_ago=1),
            message('out-1', '机器人刚刚说了别的', sender='bot', name='大肥鱼', minutes_ago=0,
                    direction='outbound', status='sent'),
        ], name='unmatched')
        record = self.record(data, ledger=True)
        self.assertFalse(record['error'], record['error'])
        self.assertEqual('候选回复', record['candidate'])
        rows = [item for item in group_shadow_compare.collect(self.root) if item['message_id'] == 'in-1']
        self.assertEqual(1, len(rows))
        self.assertEqual('unmatched', rows[0]['status'])
        self.assertEqual('', rows[0]['actual'])
        self.assertEqual([], rows[0]['actual_ids'])
        self.assertEqual('候选回复', rows[0]['shadow'])
        self.assertEqual(group_shadow_compare.UNCONFIRMED, group_shadow_compare._actual_cell(rows[0]))
        rendered = group_shadow_compare.render(self.root, rows)
        self.assertIn(group_shadow_compare.UNCONFIRMED, rendered)
        self.assertNotIn('机器人刚刚说了别的', rendered)

    # 6
    def test_the_candidate_runs_on_the_owner_private_persona_not_the_test_window(self):
        window = self.contexts.save_persona('qq', '旧测试窗口人格', '群测试窗口里的那个人格')
        GroupTests(self.root).create(GROUP, window['id'], OWNER)
        record = self.record(self.fixture([message('w-1', '在吗', minutes_ago=1)], name='persona-basis'))
        self.assertEqual(self.persona['id'], record['revision'])
        self.assertEqual(window['id'], record['test_window_revision'])
        self.assertNotEqual(record['revision'], record['test_window_revision'])

    # 7
    def test_window_switches_are_reported_but_never_decide_the_shadow(self):
        window = self.contexts.save_persona('qq', '群测试窗口人格', '窗口人格')
        space = GroupTests(self.root).create(GROUP, window['id'], OWNER)
        # The window takes over listen/reply, and even names another engine: the
        # shadow's own gate reads the main policy only, so neither may flip it.
        GroupTests(self.root).policy(space).configure(CHAT, {'listen': True, 'reply': True,
                                                             'learn': False, 'engine': 'dsh'})
        GroupPolicy(self.root).configure(CHAT, {'engine': 'shadow', 'read': True,
                                               'listen': False, 'reply': False})
        record = self.record(self.fixture([message('s-1', '在吗', minutes_ago=1)], name='window-switches',
                                          policy={'listen': False, 'reply': False}))
        listed = record['permissions']
        self.assertTrue(listed['authorized'])
        self.assertIs(True, listed['read'])
        self.assertEqual('space:' + space['id'][:8], listed['participation_source'])
        self.assertEqual((True, False, True), (listed['listen'], listed['learn'], listed['reply']))
        self.assertEqual((False, False), (listed['main_listen'], listed['main_reply']))
        self.assertIn('群测试窗口 space:' + space['id'][:8], listed['participation_note'])
        self.assertEqual('shadow', group_decide.shadow_ready(self.root, CHAT)['engine'])
        self.assertEqual('', record['skipped'])
        self.assertEqual(1, record['model_calls'])
        self.assertFalse(record['sent'])

    # 8
    def test_every_non_call_path_skips_without_a_model_call(self):
        data = self.fixture([message('x-1', '在吗', minutes_ago=1)], name='skip-paths')
        dsh = self.record(self.fixture([message('x-1', '在吗', minutes_ago=1)], name='engine-dsh',
                                       policy={'engine': 'dsh'}))
        self.assertEqual('engine:dsh', dsh['skipped'])
        self.assertEqual(0, dsh['model_calls'])
        self.assertEqual('', dsh['candidate'])
        self.assertEqual({}, dsh['request_view'])
        group_decide._inflight.add(CHAT)
        self.addCleanup(group_decide._inflight.discard, CHAT)
        busy = self.record(data)
        self.assertEqual('busy', busy['skipped'])
        self.assertEqual(0, busy['model_calls'])
        group_decide._inflight.discard(CHAT)
        slow = self.fixture([message('x-2', '慢一点也行', minutes_ago=1)], name='timeout')
        slow['model'] = {'text': '迟到的候选', 'sleep_s': 0.3}
        with patch.object(shadow_replay, 'SHADOW_TIMEOUT_S', 0.05):
            timed = self.record(slow)
        self.assertTrue(timed['timed_out'])
        self.assertIn('超时', timed['error'])
        self.assertEqual('', timed['candidate'])
        time.sleep(0.4)
        spent = self.record(self.fixture([message('x-3', '额度用完了', minutes_ago=1)], name='no-allowance',
                                         policy={'shadow_daily_limit': 0}))
        self.assertEqual('daily-limit', spent['skipped'])
        self.assertEqual(0, spent['model_calls'])
        paused = self.record(self.fixture([message('x-4', '暂停了', minutes_ago=1)], name='paused',
                                          paused=True))
        self.assertEqual('paused', paused['skipped'])
        self.assertEqual(0, paused['model_calls'])
        self.assertEqual('', paused['candidate'])

    # 9
    def test_evidence_files_carry_every_field_and_only_fixture_claims(self):
        records = [self.record(self.fixture([message('e-1', '在吗', minutes_ago=1)], name='evidence-a')),
                   self.record(self.fixture([message('e-2', '在吗', minutes_ago=1)], name='evidence-b',
                                            policy={'shadow_daily_limit': 0}))]
        payload = {'records': records}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            written = shadow_replay.write_evidence(
                payload, Path(folder) / shadow_replay.EVIDENCE_JSON,
                generated_at='2026-01-01 00:00:00', source='tools/fixtures/shadow_replay_basic.json')
            names = [Path(item).name for item in written]
            self.assertEqual([shadow_replay.EVIDENCE_JSON, shadow_replay.EVIDENCE_MD,
                              shadow_replay.LEVELS_MD], names)
            for path in written:
                self.assertTrue(Path(path).is_file(), path)
            body = json.loads(Path(written[0]).read_text(encoding='utf-8'))
            self.assertEqual('fixture', body['evidence_level'])
            self.assertIs(False, body['real_model_called'])
            self.assertIs(False, body['sent'])
            self.assertEqual(2, len(body['records']))
            for record in body['records']:
                for field in shadow_replay.RECORD_FIELDS:
                    self.assertIn(field, record)
                self.assertIs(False, record['real_model_called'])
                self.assertIs(False, record['sent'])
                self.assertEqual('fixture', record['fixture']['evidence_level'])
            report = Path(written[1]).read_text(encoding='utf-8')
            self.assertIn('evidence-a', report)
            self.assertIn('真实模型调用：false', report)
            self.assertIn('daily-limit', report)
            levels = Path(written[2]).read_text(encoding='utf-8')
            self.assertIn('fixture', levels)
            self.assertIn('recorded-history', levels)
            self.assertIn('real-model-shadow', levels)
            self.assertIn('real_model_called）：false', levels)
        source = (ROOT / 'tools/shadow_replay.py').read_text(encoding='utf-8')
        for banned in ('send_policy', 'group_dialogue', 'qq_agent_loop', '.action(', '_deliver_reply('):
            self.assertNotIn(banned, source)


if __name__ == '__main__':
    unittest.main()
