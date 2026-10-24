"""H1 fault flow on disposable SQLite stores, fake model and fake QQ transport.

These checks do not start services, call an API or send a real message. A failure
is retained by the runner; this module never retries a failing test to turn it green.
"""
import asyncio
import json
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
import group_round
import owner_private
from chat_output import TurnSilent
from group_policy import GroupPolicy
from qq_agent_loop import QQAgentLoop
from storage import read_json, write_json
from test_social_base import Fixture


class FaultFlowTests(Fixture):
    def setUp(self):
        super().setUp()
        self.loop = QQAgentLoop(self.root)
        self.loop.conversation = self.loop.store.get_or_create_conversation('private:' + self.owner, 'qq')
        self.loop.channel.ws = object()  # A connected-state marker; no socket is opened.
        self.governance = self.loop.governance
        self.delivery = AsyncMock(return_value={'ok': True, 'payload': {'message_id': 'fault-notice-1'}})
        self.raw_channel = AsyncMock(side_effect=AssertionError('不能绕过既有私聊回复链'))
        for patcher in (
            patch.object(self.loop, '_deliver_reply', self.delivery),
            patch.object(self.loop.channel, 'action', self.raw_channel),
            patch('qq_agent_loop.connect', side_effect=AssertionError('测试不能建立真实连接')),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reply = patch.object(owner_private, 'reply', wraps=owner_private.reply)
        self.reply_spy = self.reply.start()
        self.addCleanup(self.reply.stop)

    def _snapshot(self):
        return self.governance.fault_snapshot()

    def _private_item(self, text):
        conversation = self.loop.conversation
        mid = self.loop.store.record_message(conversation['id'], 'private:' + self.owner, self.owner,
            'inbound', '', None, [{'type': 'text', 'text': text}], text, 'queued')
        return {'message_id': mid, 'conversation': conversation, 'space_id': '', 'text': text,
                'payload': {'chat_id': 'private:' + self.owner, 'chat_type': 'private',
                            'segments': [{'type': 'text', 'text': text}]}}

    def _sent_text(self, call):
        return ''.join(str(segment.get('text') or '')
                       for bubble in call.kwargs.get('bubble_plan', [])
                       for segment in bubble.get('segments', []) if segment.get('type') == 'text')

    def _assert_system_private_chain(self):
        self.assertEqual(self.delivery.await_count, self.reply_spy.await_count)
        self.raw_channel.assert_not_awaited()
        for call in self.delivery.await_args_list:
            self.assertEqual(call.args[0], 'private:' + self.owner)
            self.assertIn('系统故障通知', self._sent_text(call))

    def test_three_real_think_failures_aggregate_and_only_actual_model_success_recovers(self):
        async def go():
            with patch('model_gateway.ModelGateway.__init__', return_value=None), \
                    patch('model_gateway.ModelGateway.json', side_effect=RuntimeError('model service 503')) as model:
                for _ in range(3):
                    await group_round._think(self.loop, self.root, self.chat, '一段群上下文')
                self.assertEqual(model.call_count, 3, '故障不能偷偷增加模型重试')
            failed = self._snapshot()
            self.assertEqual(len(failed['items']), 1)
            card = failed['items'][0]
            self.assertEqual(card['stage'], 'model')
            self.assertEqual((card['consecutive'], card['total']), (3, 3))
            self.assertFalse(card['auto_retry'])
            self.assertEqual(self.delivery.await_count, 1, '同类故障应合并为一条告警')
            # Merely reading the fault card must never mark an outage recovered.
            self.assertEqual(self._snapshot()['items'][0]['consecutive'], 3)
            with patch('model_gateway.ModelGateway.__init__', return_value=None), \
                    patch('model_gateway.ModelGateway.json', return_value={
                        'decision': 'speak', 'reason': '可以接话', 'minutes': 0}) as model:
                answer = await group_round._think(self.loop, self.root, self.chat, '这次调用真实返回成功')
                self.assertEqual(answer['decision'], 'speak')
                self.assertEqual(model.call_count, 1)
            recovered = self._snapshot()
            same = next(row for row in recovered['items'] if row['source'] == card['source'])
            self.assertEqual((same['consecutive'], same['total']), (0, 3))
            self.assertGreater(same['recovered_at'], 0)
            self.assertTrue(any(row['source'] == card['source'] and row['outcome'] == 'success'
                                for row in recovered['latest']))
        asyncio.run(go())
        self._assert_system_private_chain()
        print('H1 模型故障：3次注入失败→1张卡/consecutive=3→实际成功后=0；通知走主人私聊原链。', flush=True)

    def test_connection_recovery_does_not_wait_for_a_receipt_on_the_websocket_reader(self):
        self.governance.fault('qq-channel', ConnectionError('disconnected'), stage='connection')
        async def go():
            entered, release = asyncio.Event(), asyncio.Event()
            async def notice(*args, **kwargs):
                entered.set()
                await release.wait()
            with patch.object(self.loop, '_fault_notice', side_effect=notice):
                await asyncio.wait_for(self.loop._channel_health('connection', None), 1)
                await asyncio.wait_for(entered.wait(), 1)
                release.set()
                await asyncio.gather(*tuple(self.loop._notice_tasks))
        asyncio.run(go())
        self.assertEqual(self._snapshot()['items'][0]['consecutive'], 0)

    def test_fault_notice_storage_error_is_uncertain_and_never_resent(self):
        async def go():
            with patch.object(owner_private, 'fault_notice', side_effect=RuntimeError('receipt storage lost')) as notify:
                await self.loop._fault('lost-notice', RuntimeError('model failed'))
                await self.loop._fault('lost-notice', RuntimeError('model failed'))
                self.assertEqual(notify.await_count, 1)
        asyncio.run(go())
        self.assertEqual(self._snapshot()['notices'][0]['uncertain'], 1)

    def test_turn_silent_records_a_normal_outcome_without_a_fault_card_or_notice(self):
        asyncio.run(self.loop._fault('normal-silence', TurnSilent('本轮没有调用发送工具')))
        self.loop._silent('chosen-silence', stage='model')
        snapshot = self._snapshot()
        self.assertEqual(snapshot['items'], [])
        self.assertEqual({row['outcome'] for row in snapshot['latest']}, {'silent'})
        self.delivery.assert_not_awaited()
        self.reply_spy.assert_not_awaited()

    def test_uncertain_delivery_is_visible_and_never_automatically_resent(self):
        self.delivery.return_value = {'ok': False, 'error': {'code': 'timeout', 'may_have_sent': True}}
        error = {'error': {'code': 'timeout', 'may_have_sent': True}}
        async def go():
            await self.loop._fault('group-delivery:' + self.chat, error, stage='delivery', uncertain=True)
            await self.loop._fault('group-delivery:' + self.chat, error, stage='delivery', uncertain=True)
        asyncio.run(go())
        snapshot = self._snapshot()
        self.assertEqual(len(snapshot['items']), 1)
        card = snapshot['items'][0]
        self.assertTrue(card['uncertain'])
        self.assertFalse(card['auto_retry'])
        self.assertEqual(card['consecutive'], 2)
        self.assertEqual(self.delivery.await_count, 1, '未知结果不能自动重发故障通知')
        self.assertEqual(len(snapshot['notices']), 1)
        self.assertEqual(snapshot['notices'][0]['uncertain'], 1)
        self.assertEqual(snapshot['notices'][0]['sent'], 0)
        self._assert_system_private_chain()

    def test_paused_or_unbound_owner_keeps_the_fault_visible_without_sending(self):
        cfg = self.governance.config()
        cfg['paused_roles']['qq'] = True
        write_json(self.governance.path, cfg)
        asyncio.run(self.loop._fault('paused-model', RuntimeError('model service failed')))
        self.assertEqual(len(self._snapshot()['items']), 1)
        self.delivery.assert_not_awaited()
        cfg['paused_roles']['qq'] = False
        write_json(self.governance.path, cfg)
        channel = read_json(self.root / 'channels/fish_qq/config.json')
        channel['allowlist']['owner_id'] = ''
        write_json(self.root / 'channels/fish_qq/config.json', channel)
        self.loop.owner = ''
        asyncio.run(self.loop._fault('unbound-model', RuntimeError('model service failed')))
        self.assertEqual(len(self._snapshot()['items']), 2)
        self.delivery.assert_not_awaited()
        self.raw_channel.assert_not_awaited()

    def test_notice_cooldown_is_thirty_minutes_and_recovery_is_once_per_shanghai_day(self):
        start = datetime(2026, 10, 23, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()

        async def at(moment, function, *args, **kwargs):
            with patch('governance.time.time', return_value=moment):
                await function(*args, **kwargs)

        async def go():
            await at(start, self.loop._fault, 'model-A', TimeoutError('timeout'))
            self.assertEqual(self.delivery.await_count, 1)
            await at(start + 1799, self.loop._fault, 'model-A', TimeoutError('timeout'))
            self.assertEqual(self.delivery.await_count, 1)
            await at(start + 1800, self.loop._fault, 'model-A', TimeoutError('timeout'))
            self.assertEqual(self.delivery.await_count, 2)
            await at(start + 1801, self.loop._recovered, 'model-A', stage='model')
            self.assertEqual(self.delivery.await_count, 3)
            await at(start + 1900, self.loop._fault, 'model-B', TimeoutError('timeout'))
            self.assertEqual(self.delivery.await_count, 4)
            await at(start + 1901, self.loop._recovered, 'model-B', stage='model')
            self.assertEqual(self.delivery.await_count, 4, '同一天不再发送第二条恢复提示')
            await at(start + 86400, self.loop._fault, 'model-B', TimeoutError('timeout'))
            self.assertEqual(self.delivery.await_count, 5)
            await at(start + 86401, self.loop._recovered, 'model-B', stage='model')
            self.assertEqual(self.delivery.await_count, 6)
        asyncio.run(go())
        notices = self._snapshot()['notices']
        recovered = [row for row in notices if row['recovered']]
        self.assertEqual(len(recovered), 2)
        self.assertEqual(len({row['day'] for row in recovered}), 2)
        self._assert_system_private_chain()

    def test_a_clarification_ack_does_not_recover_a_model_that_was_not_called(self):
        self.governance.fault('owner-private', ValueError('既有模型执行故障'), stage='model')
        item = self._private_item('这个任务是指哪一项？')
        with patch('owner_intent.route', return_value={'kind': 'CLARIFY', 'text': '请说明要处理的文件。'}), \
                patch.object(owner_private, 'generate', side_effect=AssertionError('澄清回执不能调用模型')) as model:
            asyncio.run(self.loop._process(item))
        model.assert_not_called()
        card = next(row for row in self._snapshot()['items'] if row['source'] == 'owner-private')
        self.assertEqual((card['stage'], card['consecutive']), ('model', 1))
        self.assertFalse(any(row['source'] == 'owner-private' and row['stage'] == 'model'
                             and row['outcome'] == 'success' for row in self._snapshot()['latest']))
        self.assertEqual(self.delivery.await_count, 1, '澄清回执应真实走私聊回复链')
        self.assertEqual(self.delivery.await_args.args[0], 'private:' + self.owner)
        print('H1 澄清回执：原私聊回复链成功，但模型未被调用，既有model故障不清零。', flush=True)

    def test_real_private_process_preserves_permission_exception_classification(self):
        item = self._private_item('测试一条私聊消息')
        with patch('owner_intent.route', return_value={'kind': 'CHAT'}), \
                patch.object(owner_private, 'generate', side_effect=PermissionError(
                    '主人已关闭私聊自动回复发送')) as model:
            asyncio.run(self.loop._process(item))
        self.assertEqual(model.call_count, 1)
        cards = self._snapshot()['items']
        self.assertEqual(len(cards), 1)
        self.assertEqual((cards[0]['source'], cards[0]['stage'], cards[0]['code']),
                         ('owner-private', 'model', 'permission'))
        self.assertEqual(cards[0]['consecutive'], 1)
        self.assertFalse(cards[0]['auto_retry'])
        self._assert_system_private_chain()
        print('H1 私聊权限故障：真实process保留原PermissionError类型→permission卡，不降级为普通执行失败。', flush=True)

    def test_existing_judge_and_prescreen_error_dicts_stay_failures_instead_of_recovery(self):
        def check_active(source, stage):
            cards = [row for row in self._snapshot()['items'] if row['source'] == source and row['stage'] == stage]
            self.assertEqual(sum(row['consecutive'] for row in cards), 2)
            self.assertFalse(any(row['source'] == source and row['stage'] == stage and row['outcome'] == 'success'
                                 for row in self._snapshot()['latest']))

        async def go():
            for branch in ('group-fish', 'group-round'):
                with self.subTest(branch=branch):
                    source = branch + ':' + self.chat
                    self.governance.fault(source, ValueError('既有判断故障'), stage='judge')
                    with patch('group_decide.worth_speaking', return_value={
                        'speak': False, 'reason': 'judge-error:PermissionError', 'judge_calls': 1}) as model:
                        if branch == 'group-fish':
                            answer = await self.loop._fish_verdict(self.chat, {}, '一条候选', 'active', threading.Event())
                        else:
                            answer = await group_round._verdict(self.loop, self.chat, {}, '一条候选', 'active')
                        self.assertFalse(answer.get('speak'))
                        self.assertEqual(model.call_count, 1)
                    check_active(source, 'judge')
            policy = GroupPolicy(self.root)
            for index, reason in enumerate(('判断材料读不到：PermissionError', '判断失败，按 0 分处理')):
                with self.subTest(prescreen_reason=reason):
                    chat = 'group:' + str(int(self.group) + index + 1)
                    source = 'group-fish:' + chat
                    policy.configure(chat, {'engine': 'fish', 'listen': True, 'reply': True,
                                            'prescreen_enabled': True, 'energy_enabled': False,
                                            'target_replies_per_hour': 0})
                    self.governance.fault(source, ValueError('既有预筛故障'), stage='prescreen')
                    with patch('group_prescreen.score', return_value={
                        'score': 0, 'dims': {}, 'reason': reason, 'calls': 0 if index == 0 else 3}) as model:
                        answer = await self.loop._fish_prescreen(policy, chat, {
                            'segments': [{'type': 'text', 'text': '普通群消息'}]}, 'active', threading.Event(),
                            self.governance.audit)
                        self.assertFalse(answer)
                        self.assertEqual(model.call_count, 1)
                    check_active(source, 'prescreen')
        asyncio.run(go())
        self._assert_system_private_chain()
        print('H1 旧错误dict：judge两个入口与预筛两种失败理由都保留故障，连续数1→2，不误报恢复。', flush=True)

    def test_s2_historical_observations_keep_real_logs_and_counts_without_inventing_a_probability(self):
        log_root = Path(r'F:\AIWork\DSH\项目\DSH-Fish\审计\模块优化\2026-10-23\实施记录\S2-验证')
        case = ('test_group_memory.GroupMemoryTests.'
                'test_a_memory_failure_is_audited_and_the_reply_still_happens')
        names = ('定点验收-初次.log', '定点验收-扩围前.log', '定点验收-完成.log', '定点验收-诊断.log')
        observations = []
        for index, name in enumerate(names):
            source = log_root / name
            self.assertTrue(source.is_file(), '历史原始记录必须存在：' + str(source))
            rows = [json.loads(line) for line in source.read_text(encoding='utf-8').splitlines()
                    if line.startswith('{')]
            actual = [row for row in rows if row.get('case') == 'cleanup_restores_original' and row.get('test') == case]
            self.assertEqual(len(actual), 1)
            observations.append({'log': str(source), 'passed': actual[0]['successful']})
            self.governance.record_test_observation(case, actual[0]['successful'], now=100 + index)
        self.assertEqual([row['passed'] for row in observations], [True, True, False, True])
        snapshot = self._snapshot()
        self.assertEqual(snapshot['items'], [], '测试波动不冒充模型运行故障')
        self.assertEqual(len(snapshot['tests']), 1)
        observed = snapshot['tests'][0]
        self.assertEqual((observed['runs'], observed['failures'], observed['consecutive']), (4, 1, 0))
        self.assertEqual((observed['last_failed'], observed['last_passed']), (102, 103))
        caution = '这4次历史观察的脚本版本和执行条件不同，1次失败不能推算稳定失败概率；原失败栈未保存，根因未知。'
        print('H1 测试观测：' + json.dumps({'case': case, 'observations': observations,
              'runs': observed['runs'], 'failures': observed['failures'], 'caution': caution}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    unittest.main()
