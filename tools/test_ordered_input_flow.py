"""R01: observable ordered turns, fixed claims and independent plan results.

Reuse the existing temporary-host/fake-provider fixtures; observe the original
workbench/runtime interfaces rather than a second queue or private SQL layout.
"""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools'), str(ROOT / 'vendor/python')]

import dependencies
import test_unified_execution as client_fixture
import test_plan_execution as plan_fixture
from workbench import Workbench

final = client_fixture.final


class VisibleCoverageBudget(client_fixture.FakeBudget):
    """The provider boundary reports the background references it actually saw."""
    def fit(self, messages, tools, output_reserve):
        references = [ref for item in messages for ref in item.get('_fish_refs', [])]
        return {**super().fit(messages, tools, output_reserve), 'covered': references}


class OrderedInputFlowTests(unittest.TestCase):
    setUp = client_fixture.UnifiedExecutionTests.setUp
    stop_host = client_fixture.UnifiedExecutionTests.stop_host
    gateway = client_fixture.UnifiedExecutionTests.gateway
    conversation = client_fixture.UnifiedExecutionTests.conversation
    submit = client_fixture.UnifiedExecutionTests.submit
    job = client_fixture.UnifiedExecutionTests.job

    def test_waiting_places_keep_arrival_order_and_only_adjacent_inputs_merge(self):
        bench = Workbench(self.root)
        first_place, other_place = self.conversation(bench), self.conversation(bench)
        fake = self.gateway([final('') for _ in range(4)])
        fake.gate.clear()
        blocker = self.submit(bench, first_place, '先完成正在思考的一轮')
        self.assertTrue(fake.called.wait(10))
        first = self.submit(bench, first_place, '连续材料一')
        adjacent = self.submit(bench, first_place, '连续材料二')
        other = self.submit(bench, other_place, '中间另一场所的来信')
        last = self.submit(bench, first_place, '最后才到，不越过中间来信')
        fake.gate.set()
        settled = [self.job(bench, item['job_id']) for item in (blocker, first, adjacent, other, last)]
        self.assertEqual([item['status'] for item in settled], ['complete'] * 5, settled)
        views = [bench.dispatch('turn.view', {'execution': item['execution']}) for item in settled]
        self.assertEqual(views[1]['execution'], views[2]['execution'])
        self.assertNotEqual(views[1]['execution'], views[4]['execution'])
        self.assertEqual([item['text'] for item in views[1]['materials']], ['连续材料一', '连续材料二'])
        self.assertEqual(views[1]['reference'], views[1]['materials'][-1]['reference'])
        self.assertEqual([item['source'] for item in views[1]['materials']], ['desktop', 'desktop'])
        self.assertLessEqual(views[1]['finished'], views[3]['started'])
        self.assertLessEqual(views[3]['finished'], views[4]['started'])
        self.assertEqual(len(fake.requests), 4)

    def test_late_file_material_is_visible_but_its_new_input_still_runs_next(self):
        bench = Workbench(self.root)
        conversation = self.conversation(bench)
        fake = self.gateway([final(''), final('')])
        fake.gate.clear()
        coverage = patch('context_budget.ContextBudget', VisibleCoverageBudget)
        coverage.start()
        self.addCleanup(coverage.stop)
        first_file, second_file = self.root / 'first.txt', self.root / 'second.txt'
        first_file.write_text('alpha fixed window material', encoding='utf-8')
        second_file.write_text('beta late file material', encoding='utf-8')
        first = bench.dispatch('chat.send', {'id': conversation, 'text': '先认领这句',
                                           'attachments': [{'path': str(first_file), 'type': 'file'}]})
        deadline = time.time() + 10
        while time.time() < deadline:
            first_job = bench.dispatch('chat.job', {'job_id': first['job_id']})
            if first_job['status'] == 'running':
                break
            time.sleep(.02)
        self.assertEqual(first_job['status'], 'running')
        second = bench.dispatch('chat.send', {'id': conversation, 'text': '晚到文本必须下一轮',
                                            'attachments': [{'path': str(second_file), 'type': 'file'}]})
        self.assertTrue(fake.called.wait(10))
        fake.gate.set()
        first_job, second_job = self.job(bench, first['job_id']), self.job(bench, second['job_id'])
        self.assertEqual([first_job['status'], second_job['status']], ['complete', 'complete'])
        first_view = bench.dispatch('turn.view', {'execution': first_job['execution']})
        second_view = bench.dispatch('turn.view', {'execution': second_job['execution']})
        self.assertEqual(len(first_view['inputs']), 1)
        self.assertEqual(len(second_view['inputs']), 1)
        self.assertEqual(len(fake.requests), 2)
        self.assertIn('alpha fixed window material', fake.renders(0))
        self.assertIn('beta late file material', fake.renders(0))
        self.assertIn('晚到文本必须下一轮', fake.renders(1))
        self.assertFalse(second_view['merged'])

    def test_two_plan_occurrences_for_one_target_each_keep_their_commission_and_result(self):
        from bot_control import BotControl
        bench = Workbench(self.root)
        conversation = self.conversation(bench)
        runtime = bench.runtime()
        control = BotControl(runtime.root)
        plan_fixture.FakeTransport.sent = []
        transport = patch.object(BotControl, '_channel_transport', plan_fixture.FakeTransport())
        transport.start()
        self.addCleanup(transport.stop)
        fake = self.gateway([final(''), final('喝水提醒已到'), final('休息提醒已到')])
        fake.gate.clear()
        blocker = self.submit(bench, conversation, '先完成当前轮，再处理两项独立委托')
        self.assertTrue(fake.called.wait(10))
        plans = [control.create({'target': 'owner', 'title': title, 'prompt': prompt,
                                 'after_seconds': 60, 'purpose': 'reminder'})
                 for title, prompt in [('第一项', '独立委托甲：检查喝水安排'), ('第二项', '独立委托乙：检查休息安排')]]
        slots = [control.plans.run_now(plan['id'])['slot'] for plan in plans]
        submitted = runtime.plans.dispatch()
        self.assertEqual(len(submitted), 2)
        fake.gate.set()
        self.assertEqual(self.job(bench, blocker['job_id'])['status'], 'complete')
        deadline = time.time() + 20
        views = []
        while time.time() < deadline:
            views = [bench.dispatch('turn.view', {'execution': item['execution']}) for item in submitted]
            occurrences = [control.plans.occurrence(plan['id'], slot) for plan, slot in zip(plans, slots)]
            if all(item['status'] == 'complete' for item in views) and all(
                    item and item['state'] == 'sent' for item in occurrences):
                break
            time.sleep(.03)
        self.assertEqual([item['status'] for item in views], ['complete', 'complete'])
        self.assertEqual([item['state'] for item in occurrences], ['sent', 'sent'])
        self.assertEqual([item['text'] for item in plan_fixture.FakeTransport.sent], ['喝水提醒已到', '休息提醒已到'])
        self.assertEqual(len({item['receipt_id'] for item in occurrences}), 2)
        self.assertNotEqual(views[0]['execution'], views[1]['execution'])
        self.assertEqual([view['plan']['schedule_id'] for view in views], [plan['id'] for plan in plans])
        self.assertEqual([view['inputs'] for view in views],
                         [['plan:' + plan['id'] + ':' + slot] for plan, slot in zip(plans, slots)])
        self.assertEqual(len(fake.requests), 3)
        self.assertIn('独立委托甲：检查喝水安排', fake.renders(1))
        self.assertIn('独立委托乙：检查休息安排', fake.renders(2))


if __name__ == '__main__':
    unittest.main()
