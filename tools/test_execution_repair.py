"""R02: public workbench/plan/QQ seams on disposable instance records.

Only the model and QQ transport are substituted. Budget, trace, input records,
tools, clock and result reading remain the real implementations.
"""
import asyncio
import json
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from fish_agent import get_runtime, reset_runtime
from governance import Governance
from locations import project_path
from model_gateway import ChatResult
from social_context import SocialContext
from storage import write_json
from test_unified_execution import FakeGateway, tool_call
from workbench import Workbench

OWNER = '3259980040'


class Gateway(FakeGateway):
    model = 'deepseek-v4-test'

    def chat(self, messages, **kwargs):
        kwargs['on_event']('reasoning', '替代模型流的实际思考片段')
        return super().chat(messages, **kwargs)


class RecordingChannel:
    """QQ boundary replacement; any operation notice is observable as an action."""
    ws = object()
    bot = {'id': 'bot-r02'}

    def __init__(self):
        self.sent = []

    async def action(self, action, payload, timeout=30):
        self.sent.append((action, payload))
        return {'ok': True, 'payload': {'message_id': 'offline-receipt'}}


class ExecutionRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.host_root = Path(temporary.name)
        write_json(self.host_root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'name': 'Fish', 'model': Gateway.model,
                        'base_url': Gateway.base, 'apiKeyEnv': 'FISH_TEST_KEY',
                        'persona': 'harness/agents/qq.persona.md'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.host_root / 'channels/fish_qq/config.json',
                   {'allowlist': {'owner_id': OWNER, 'group_ids': ['123456']}})
        tokenizer = self.host_root / 'vendor/tokenizers/deepseek-v4/tokenizer.json'
        tokenizer.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / 'vendor/tokenizers/deepseek-v4/tokenizer.json', tokenizer)
        SocialContext(self.host_root).ensure_default('qq')
        Governance(self.host_root).save({'max_tool_calls_per_turn': 0})
        self.runtime = get_runtime(self.host_root)
        self.addCleanup(reset_runtime, self.host_root)
        self.root = self.runtime.root
        write_json(project_path(self.root, 'data/social/model-metadata.json'), {
            Gateway.base + '|' + Gateway.model: {
                'id': Gateway.model, 'context_window': 1000000,
                'fetched_at': time.time(), 'source': 'offline-provider-metadata'}})
        from tool_host import ToolHost
        self.host = ToolHost(self.host_root)
        self.host.runtime = self.runtime
        self.host.serve(background_workers=False, listen_port=0)
        self.addCleanup(self.close_host)
        config = json.loads((self.host_root / 'harness/agents.json').read_text(encoding='utf-8'))
        config['runtime']['tool_host_port'] = self.host.server.server_address[1]
        write_json(self.host_root / 'harness/agents.json', config)
        self.wb = Workbench(self.host_root)

    def close_host(self):
        self.runtime.stop()
        self.host.stop.set()
        self.host.server.shutdown()
        self.host.server.server_close()

    def gateway(self, replies):
        fake = Gateway(replies)
        replacement = patch('model_gateway.ModelGateway', return_value=fake)
        replacement.start()
        self.addCleanup(replacement.stop)
        return fake

    def wait_view(self, execution, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.wb.dispatch('turn.view', {'instance_id': 'main', 'execution': execution})
            if view['status'] not in ('queued', 'running') and not self.runtime.execution.active_conversation():
                return view
            time.sleep(.03)
        self.fail('回合未结清：' + str(view))

    def evidence(self, case, **facts):
        print(json.dumps({'case': case, **facts}, ensure_ascii=False), flush=True)

    def test_unavailable_debug_copy_does_not_block_tools_or_erase_real_records(self):
        # A diagnostic-only file cannot be opened as SQLite. No real record is removed.
        project_path(self.root, 'data/social/request-inspector.db').mkdir()
        fake = self.gateway([
            ChatResult('', {'prompt_tokens': 10, 'completion_tokens': 2}, 'tool_calls',
                       (tool_call('fish_workspace', {}, 'workspace-r02'),)),
            ChatResult('已经查看工作区，这轮安静结束。', {'prompt_tokens': 12, 'completion_tokens': 3}, 'stop')])
        conversation = self.wb.dispatch('chat.new', {'title': 'R02原文'})['id']
        job = self.wb.dispatch('chat.send', {'instance_id': 'main', 'id': conversation,
            'text': '看看工作区，可以不回复。', 'request_id': uuid.uuid4().hex})
        submitted = self.wb.dispatch('chat.job', {'job_id': job['job_id']})
        view = self.wait_view(submitted['execution'])
        self.assertEqual(view['raw_status'], 'silent', view)
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(view['trace']['reasoning'], '替代模型流的实际思考片段' * 2)
        results = [e['value'] for e in view['trace']['events'] if e['kind'] == 'tool_result']
        self.assertEqual(results[0]['name'], 'fish_workspace')
        self.assertNotIn('error', results[0]['result'])
        self.assertEqual(self.wb.dispatch('chat.snapshot', {'id': conversation})['messages'][0]['text'],
                         '看看工作区，可以不回复。')
        from context_budget import snapshot
        self.assertEqual(snapshot(self.root)['latest']['turn_requests'], 2)
        self.assertEqual(snapshot(self.root)['latest']['output_tokens'], 5)
        self.assertTrue(view['inputs'])
        self.assertIsNone(view['delivery_times']['sending'])
        self.assertEqual(view['delivery_times']['outcome'], 'silent')
        self.evidence('debug_copy_unavailable', turn=view, usage=snapshot(self.root)['latest'])

    def test_plan_followup_and_clock_keep_the_real_private_place_and_common_deadline(self):
        self.gateway([
            ChatResult('', {}, 'tool_calls', (tool_call('fish_think_again',
                {'interest': .7, 'topic': '继续想刚才的计划'}, 'plan-followup-r02'),)),
            ChatResult('计划已考虑，这轮不发言。', {}, 'stop'),
            ChatResult('三秒续醒也可以安静。', {}, 'stop'),
            ChatResult('到点自主思考也正常。', {}, 'stop')])
        plan = self.wb.dispatch('schedules.create', {'title': '自主计划',
            'prompt': '想想最近的经历，可以不说话。', 'purpose': 'autonomy', 'after_seconds': 3600})
        self.wb.dispatch('schedules.run', {'id': plan['id']})
        clock = [time.time()]
        with patch('time.time', side_effect=lambda: clock[0]):
            submitted = self.runtime.plans.dispatch()[0]
            view = self.wait_view(submitted['execution'])
            self.assertEqual(view['raw_status'], 'silent', view)
            self.assertEqual(float(self.runtime.state.get_state('auto_deadline')), clock[0] + 2700)
            self.assertEqual(float(self.runtime.state.get_state('followup_due')), clock[0] + 3)
            clock[0] += 4
            followup = self.wait_self_view()
            self.assertEqual(followup['raw_status'], 'silent', followup)
            self.assertEqual(followup['trigger']['kind'], 'followup')
            self.assertEqual(followup['trigger']['source'], 'qq-private')
            self.assertEqual(followup['trigger']['place'], 'private:' + OWNER)
            self.assertEqual(float(self.runtime.state.get_state('auto_deadline')), clock[0] + 2700)
            clock[0] += 2701
            timer = self.wait_self_view(exclude=followup['execution'])
            self.assertEqual(timer['raw_status'], 'silent', timer)
            self.assertEqual(timer['trigger']['kind'], 'timer')
            self.assertEqual(timer['trigger']['source'], 'qq-private')
            self.assertEqual(timer['trigger']['place'], 'private:' + OWNER)
            self.assertEqual(float(self.runtime.state.get_state('auto_deadline')), clock[0] + 2700)
            self.evidence('plan_followup_timer', plan=view, followup=followup, timer=timer,
                          clock_note='本用例仅在时间边界使用替代时钟，运行记录为离线事实')

    def wait_self_view(self, exclude=''):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            thoughts = self.wb.dispatch('chat.thoughts', {})['thoughts']
            for thought in thoughts:
                reference = thought['reference']
                if reference.startswith('self:') and reference[5:] != exclude:
                    return self.wait_view(reference[5:])
            time.sleep(.05)
        self.fail('计划后的自主回合未进入实际模型链：' + str(thoughts))

    def test_group_intake_watches_the_actual_failure_without_operations_messages(self):
        from group_policy import GroupPolicy
        from qq_agent_loop import QQAgentLoop
        GroupPolicy(self.root).configure('group:123456',
            {'listen': True, 'reply': True, 'engine': 'fish'})
        self.gateway([RuntimeError('offline provider interrupted before a final answer')])
        loop = QQAgentLoop(self.host_root)
        loop.channel = RecordingChannel()
        loop.conversation = loop.store.get_or_create_conversation('private:' + OWNER, 'qq')

        async def receive():
            await loop.on_event({'event': 'message.received', 'payload': {
                'chat_type': 'group', 'chat_id': 'group:123456', 'message_id': 'group-r02',
                'sender': {'id': '2100000001', 'display_name': '测试群友'},
                'segments': [{'type': 'text', 'text': '接着想想。'}], 'mentions_bot': True}})
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                items = loop.governance.fault_snapshot()['items']
                if any(row['source'] == 'group-fish:group:123456' and row['stage'] == 'model'
                       for row in items):
                    break
                await asyncio.sleep(.05)
            else:
                self.fail('已失败回合没有被原结果监听记录到管理员日志')
            self.assertEqual(loop.channel.sent, [])
            self.assertEqual(loop.governance.fault_snapshot()['notices'], [])
            run = self.runtime.experience.read()[0]['run']
            self.evidence('group_failure_watched', turn=self.wb.dispatch('turn.view', {'execution': run}),
                          faults=loop.governance.fault_snapshot(), channel_actions=loop.channel.sent)
        asyncio.run(receive())

    def test_private_intake_schedules_its_silent_result_on_the_event_loop(self):
        from qq_agent_loop import QQAgentLoop
        self.gateway([ChatResult('想过了，这轮不发聊天。', {}, 'stop')])
        loop = QQAgentLoop(self.host_root)
        loop.channel = RecordingChannel()
        loop.conversation = loop.store.get_or_create_conversation('private:' + OWNER, 'qq')

        async def receive():
            await loop.on_event({'event': 'message.received', 'payload': {
                'chat_type': 'private', 'chat_id': 'private:' + OWNER, 'message_id': 'private-r02',
                'sender': {'id': OWNER}, 'segments': [{'type': 'text', 'text': '随便想想最近的事。'}]}})
            inbound = self.runtime.client_qq.inbound(1)
            self.assertNotEqual(inbound['status'], 'failed', inbound)
            view = await asyncio.to_thread(self.wait_view, inbound['runtime_turn_id'])
            self.assertEqual(view['raw_status'], 'silent', view)
            deadline = time.monotonic() + 5
            while not any(item['outcome'] == 'silent' for item in loop.governance.fault_snapshot()['latest']):
                self.assertLess(time.monotonic(), deadline, '私聊结束监听未记录正常静默')
                await asyncio.sleep(.05)
            self.assertEqual(loop.governance.fault_snapshot()['items'], [])
            self.assertEqual(loop.channel.sent, [])
            self.evidence('private_silent_watched', turn=view,
                          faults=loop.governance.fault_snapshot(), channel_actions=loop.channel.sent)
        asyncio.run(receive())

    def test_explicit_watcher_keeps_the_submitting_instance_and_its_error_log(self):
        from qq_agent_loop import QQAgentLoop
        self.gateway([RuntimeError('offline original instance failure')])
        conversation = self.wb.dispatch('chat.new', {'title': '固定实例'})['id']
        job = self.wb.dispatch('chat.send', {'id': conversation, 'text': '思考这一轮。'})
        submitted = self.wb.dispatch('chat.job', {'job_id': job['job_id']})
        view = self.wait_view(submitted['execution'])
        self.assertEqual(view['raw_status'], 'failed')
        other = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(other.cleanup)
        # The connection adapter defaults to a different host. The submitted
        # execution's explicit instance controls both its status read and log.
        loop = QQAgentLoop(Path(other.name))
        async def watch():
            await loop.watch_turn(view['execution'], source='explicit-instance-r02',
                                  instance=self.runtime.context)
        asyncio.run(watch())
        own = Governance(self.root).fault_snapshot()['items']
        self.assertTrue(any(row['source'] == 'explicit-instance-r02' for row in own))
        foreign = loop.governance.fault_snapshot()['items']
        self.assertFalse(any(row['source'] == 'explicit-instance-r02' for row in foreign))
        self.evidence('explicit_instance_watched', turn=view, own_faults=own, foreign_faults=foreign)

    def test_delivery_denial_returns_its_real_receipt_without_an_extra_chat_notice(self):
        from qq_agent_loop import QQAgentLoop
        class DeniedChannel(RecordingChannel):
            async def action(self, action, payload, timeout=30):
                self.sent.append((action, payload))
                return {'ok': False, 'error': {'code': 'offline permission denied'}}
        loop = QQAgentLoop(self.host_root)
        loop.channel = DeniedChannel()
        loop.conversation = loop.store.get_or_create_conversation('private:' + OWNER, 'qq')
        from experience import ExperienceTurn
        ExperienceTurn(self.root, loop.store.db_path, loop.conversation['id'], channel='qq')
        result = asyncio.run(loop.private_deliver({'target': 'private:' + OWNER,
            'bubbles': [{'kind': 'text', 'text': '实际打算说的话'}], 'key': 'delivery-r02'}))
        self.assertFalse(result['sent'], result)
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('offline permission denied', result['error'])
        self.assertEqual(len(loop.channel.sent), 1, '发送失败后不能再往普通聊天发一条技术通知')
        self.evidence('delivery_denied', receipt=result, channel_actions=loop.channel.sent)


if __name__ == '__main__':
    unittest.main()
