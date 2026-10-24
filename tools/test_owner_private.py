"""C-batch route/identity/history and archived QQ file receipt checks."""
import asyncio
import copy
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from test_social_base import Fixture
from model_gateway import ChatResult
from owner_delegation import OwnerDelegation
from owner_intent import route
from qq_agent_loop import QQAgentLoop
from social_context import SocialContext
from storage import database
from task_delegation import TaskDelegation


def tool_call(name, arguments, call_id='call-1'):
    """One function call the fake model makes, in the shape fish_chat reads it."""
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


def said(*texts, call_id='call-1'):
    """The turn where she speaks: fish_say is the only reason her words leave."""
    return ChatResult('', {}, 'tool_calls', (tool_call('fish_say', {'texts': list(texts)}, call_id),))


def stopped(thinking=''):
    """The end of a turn: she wrote this much and called nothing else."""
    return ChatResult(thinking, {}, 'stop')


def spoken(messages):
    """True once this turn already called a sending tool, so the next answer just stops."""
    return any(isinstance(message, dict) and message.get('role') == 'tool' for message in messages)


def bubble_result(text, usage, finish):
    """A reply that really reaches QQ, i.e. one that calls the sending tool.

    The name stays because the tests keep pinning the same words; what changed is that
    the words now leave only because fish_say was called.
    """
    return said(text)


class OwnerPrivateFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.loop = QQAgentLoop(self.root)
        self.loop.conversation = self.loop.store.get_or_create_conversation('private:' + self.owner, 'qq')
        self.loop.channel = MagicMock(action=AsyncMock(return_value={'ok': True, 'payload': {'message_id': ''}}))
        self.requests = []
        gateway = MagicMock(model='test-model', base='https://api.deepseek.com')
        def answer(messages, **kwargs):
            self.requests.append(copy.deepcopy(messages))
            return stopped() if spoken(messages) else said('好，记住暗号蓝鲸了。')
        gateway.chat.side_effect = answer
        self.gateway = gateway
        for name, value in [('model_gateway.ModelGateway', MagicMock(return_value=gateway)),
                            ('web_knowledge.WebKnowledge.resolve', MagicMock(return_value='')),
                            ('conversation_style.private_context', MagicMock(return_value='')),
                            ('memory_store.MemoryStore.context', MagicMock(return_value='')),
                            ('memory_store.MemoryStore.enqueue', MagicMock())]:
            p = patch(name, value);p.start();self.addCleanup(p.stop)

    def item(self, text, *, sender=None, space=None):
        chat = 'private:' + (sender or self.owner)
        conv = self.loop.store.get_or_create_conversation('test:' + space['id'] if space else chat, 'qq')
        mid = self.loop.store.record_message(conv['id'], chat, sender or self.owner, 'inbound', '', None,
            [{'type': 'text', 'text': text}], text, 'queued')
        return {'message_id': mid, 'conversation': conv, 'space_id': space['id'] if space else '',
                'text': text, 'payload': {'chat_id': chat, 'chat_type': 'private', 'segments': [{'type': 'text', 'text': text}]}}

    def tearDown(self):
        if hasattr(self, 'tasks'):
            self.tasks.close()
        import gc
        gc.collect()
        super().tearDown()


class OwnerPrivateTests(OwnerPrivateFixture):
    def test_owner_chat_reuses_fish_persona_history_and_never_dsh(self):
        contexts = SocialContext(self.root)
        persona = contexts.save_persona('qq', '独立鱼', 'FISH_ONLY', global_rules='DSH_GLOBAL', workspace_rules='DSH_WORKSPACE')
        space = contexts.create_space('qq', revision=persona['id'], origin='owner-qq')
        contexts.select_owner_space(space['id'], self.owner)
        asyncio.run(self.loop._process(self.item('记住暗号蓝鲸', space=space)))
        asyncio.run(self.loop._process(self.item('我刚说什么', space=space)))
        raw = json.dumps(self.requests[-1], ensure_ascii=False)
        self.assertIn('FISH_ONLY', raw);self.assertIn('记住暗号蓝鲸', raw)
        self.assertEqual(raw.count('我刚说什么'), 1)
        self.assertNotIn('DSH_GLOBAL', raw);self.assertNotIn('DSH_WORKSPACE', raw)
        self.assertIsNone(self.loop.dsh)
        self.assertEqual(contexts.space(space['id'])['session'], '')

    def test_gateway_failure_has_notice_without_native_fallback(self):
        self.gateway.chat.side_effect = RuntimeError('API unavailable')
        item = self.item('你好')
        asyncio.run(self.loop._process(item))
        with database(self.loop.store.db_path) as db:
            self.assertEqual(db.execute('SELECT status FROM messages WHERE id=?', (item['message_id'],)).fetchone()[0], 'failed')
        self.assertIsNone(self.loop.dsh)
        self.assertTrue(self.loop.channel.action.called)

    def test_window_switch_during_generation_cancels_old_reply(self):
        contexts = SocialContext(self.root)
        a = contexts.create_space('qq', origin='owner-qq')
        b = contexts.create_space('qq', origin='owner-qq')
        contexts.select_owner_space(a['id'], self.owner)
        item = self.item('等一下再回复我', space=a)
        entered, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test did not release generation')
            return stopped() if spoken(args[0]) else said('旧窗口生成的回复')
        self.gateway.chat.side_effect = delayed
        async def run():
            task = asyncio.create_task(self.loop._process(item))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                from owner_tests import command
                await command(self.loop, {'chat_type': 'private', 'chat_id': 'private:' + self.owner,
                    'message_id': 'switch-test', 'segments': [{'type': 'text', 'text': '/切换测试 ' + b['id']}]}, self.owner)
            finally:
                release.set()
                await asyncio.wait_for(task, 3)
        asyncio.run(run())
        self.assertEqual(contexts.owner_space(self.owner)['id'], b['id'])
        sent = [c.args[1]['segments'][0]['text'] for c in self.loop.channel.action.call_args_list]
        self.assertEqual(len(sent), 1)
        self.assertIn('当前独立测试', sent[0])
        self.assertNotIn('旧窗口生成的回复', ''.join(sent))
        with database(self.loop.store.db_path) as db:
            inbound = db.execute('SELECT status,cancelled FROM messages WHERE id=?', (item['message_id'],)).fetchone()
            turn = db.execute('SELECT status FROM runtime_turns WHERE conversation_id=?', (item['conversation']['id'],)).fetchone()
            self.assertEqual(tuple(inbound), ('cancelled', 1))
            self.assertEqual(turn['status'], 'cancelled')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages WHERE conversation_id=?', ('qq:test:' + b['id'] + ':qq',)).fetchone()[0], 0)
        self.assertIsNone(self.loop.dsh)

    def test_same_window_persona_reload_also_discards_generated_reply(self):
        from tool_host import host_here, port
        # Exercise the real tools on this temporary root's own random port. The
        # case needs to reach generation, not a different root's shared-port auth.
        host_here(self.root)
        contexts = SocialContext(self.root)
        a = contexts.create_space('qq', origin='owner-qq')
        contexts.select_owner_space(a['id'], self.owner)
        changed = contexts.save_persona('qq', '新人格', '这是一份新设定')
        def answer(*args, **kwargs):
            contexts.change_persona(a['id'], changed['id'])
            return stopped() if spoken(args[0]) else said('上一份人格的回复')
        self.gateway.chat.side_effect = answer
        item = self.item('你好', space=a)
        asyncio.run(self.loop._process(item))
        self.assertGreaterEqual(self.gateway.chat.call_count, 1, '未进入模型，不能声称已验收人格切换')
        self.assertEqual(contexts.space(a['id'])['revision'], changed['id'])
        self.loop.channel.action.assert_not_called()
        with database(self.loop.store.db_path) as db:
            self.assertEqual(db.execute('SELECT status FROM messages WHERE id=?', (item['message_id'],)).fetchone()[0], 'cancelled')
        print('PERSONA_RELOAD_FIXTURE private_port=' + str(port(self.root)) +
              ' fake_gateway_calls=' + str(self.gateway.chat.call_count) +
              ' revision_changed=True outbound_calls=0', flush=True)

    def test_generation_failure_after_window_switch_does_not_notify_new_window(self):
        contexts = SocialContext(self.root)
        a = contexts.create_space('qq', origin='owner-qq')
        contexts.select_owner_space(a['id'], self.owner)
        def fail(*args, **kwargs):
            contexts.leave_owner_space(self.owner)
            raise TimeoutError('旧窗口生成失败')
        self.gateway.chat.side_effect = fail
        asyncio.run(self.loop._process(self.item('你好', space=a)))
        self.loop.channel.action.assert_not_called()

    def test_final_channel_checks_pause_content_and_owner_reply_setting(self):
        original = self.gateway.chat.side_effect
        for changes in ({'paused': True}, {'blocked_text': ['GENERATED_BLOCKED']},
                        {'send_policy': {'owner_private': {'owner_reply': False}}}):
            with self.subTest(changes=changes):
                self.loop.governance.save({'paused': False, 'blocked_text': [],
                    'send_policy': {'owner_private': {'owner_reply': True}}})
                self.loop.channel.action.reset_mock()
                def generated(*args, **kwargs):
                    self.loop.governance.save(changes)
                    return stopped() if spoken(args[0]) else said('GENERATED_BLOCKED')
                self.gateway.chat.side_effect = generated
                with patch.object(self.loop.governance, 'check_output', wraps=self.loop.governance.check_output) as check:
                    asyncio.run(self.loop._process(self.item('生成后再变更设置')))
                    inspected = [c.args[0] for c in check.call_args_list]
                sent = [c.args[1]['segments'][0]['text'] for c in self.loop.channel.action.call_args_list]
                self.assertNotIn('GENERATED_BLOCKED', sent)
                # A failure notice is a different message, not a second decision
                # over the original generated reply.
                self.assertLessEqual(inspected.count('GENERATED_BLOCKED'), 1)
        self.gateway.chat.side_effect = original

    def test_task_notices_use_the_same_final_channel_checks(self):
        from owner_private import deliveries
        for changes, expected in (({}, 'sent'), ({'paused': True}, 'failed'),
                                  ({'blocked_text': ['TASK_NOTICE_BLOCKED']}, 'blocked')):
            with self.subTest(changes=changes):
                self.loop._stop.clear()
                self.loop.governance.save({'paused': False, 'blocked_text': []} | changes)
                self.loop.channel.action.reset_mock()
                task = str(time.time_ns())
                value = {'id': task, 'status': 'failed', 'error': 'TASK_NOTICE_BLOCKED',
                         'source_conversation': self.loop.conversation['id']}
                def pending(*args):
                    self.loop._stop.set()
                    return {'tasks': [value]}
                with patch('owner_private.task_call', side_effect=pending), \
                     patch('owner_private.asyncio.sleep', new=AsyncMock()), \
                     patch.object(self.loop.governance, 'check_output', wraps=self.loop.governance.check_output) as check:
                    asyncio.run(deliveries(self.loop))
                    self.assertEqual(check.call_count, 1)
                self.assertEqual(self.loop.channel.action.call_count, 1 if expected == 'sent' else 0)
                with database(self.loop.store.db_path) as db:
                    self.assertEqual(db.execute('SELECT state FROM task_notices WHERE task=?', (task,)).fetchone()[0], expected)

    def test_duplicate_channel_event_has_one_consumer_and_nonowner_is_ignored(self):
        payload = {'chat_type': 'private', 'chat_id': 'private:' + self.owner, 'sender': {'id': self.owner},
                   'message_id': 'same-inbound', 'segments': [{'type': 'text', 'text': 'hello'}]}
        async def run():
            for _ in range(2):
                await self.loop.on_event({'event': 'message.received', 'payload': payload})
            self.assertEqual(self.loop.queue.qsize(), 1)
            payload['sender']['id'] = '111111111';payload['chat_id'] = 'private:111111111'
            await self.loop.on_event({'event': 'message.received', 'payload': payload})
            self.assertEqual(self.loop.queue.qsize(), 1)
        asyncio.run(run())

    def test_nonowner_stored_input_cannot_enter_chat_or_task(self):
        with self.assertRaises(PermissionError):
            asyncio.run(self.loop._process(self.item('创建文件发给我', sender='111111111')))
        self.gateway.chat.assert_not_called()

    def test_worker_marks_rejected_task_terminal_instead_of_leaving_it_queued(self):
        item = self.item('帮我创建文件发给我')
        async def run():
            with patch.object(self.loop, '_process', new=AsyncMock(side_effect=PermissionError('任务权限已关闭'))):
                worker = asyncio.create_task(self.loop._worker())
                await self.loop.queue.put(item)
                await self.loop.queue.join()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        asyncio.run(run())
        with database(self.loop.store.db_path) as db:
            self.assertEqual(db.execute('SELECT status FROM messages WHERE id=?', (item['message_id'],)).fetchone()[0], 'blocked')

    def test_clear_file_request_and_joke_are_distinct(self):
        for text in ('帮我写一篇文章，保存为article.md文件发给我', '在电脑上写一个文章出来', '/电脑任务 写文章保存为a.md'):
            self.assertEqual(route(text)['kind'], 'DSH_TASK', text)
        for text in ('帮我摘月亮', '帮我写一篇文章', '你是一个串子'):
            self.assertEqual(route(text)['kind'], 'CHAT')
        self.assertEqual(route('帮我修改现有项目并保存文件')['kind'], 'CLARIFY')
        self.assertEqual(route('/任务状态')['kind'], 'FISH_TASK')


class OwnerTaskTests(OwnerPrivateFixture):
    def setUp(self):
        super().setUp()
        self.runs = []
        runs = self.runs
        class Run:
            def __init__(self, root, directory, session, config, cancelled):
                self.directory = directory;self.stopped = threading.Event();runs.append(self)
            def run(self, prompt, notify):
                notify('running', {})
                work = self.directory / 'workspace';work.mkdir(parents=True)
                (work / 'article.md').write_text('归档的文章', encoding='utf-8')
                (work / 'fish-artifacts.json').write_text('{"files":["article.md"]}', encoding='utf-8')
                return SimpleNamespace(finish_reason='completed', final_response='完成', events=[])
            def close(self):self.stopped.set()
        for name, value in [('task_delegation.DSHDelegatedRun', Run),
                            ('task_delegation.execution_config', MagicMock(return_value={'timeout_seconds': 30}))]:
            p = patch(name, value);p.start();self.addCleanup(p.stop)
        self.tasks = TaskDelegation(self.root)
        self.adapter = OwnerDelegation(self.root, self.tasks)

    def task(self):
        item = self.item('帮我写一篇文章，保存为article.md文件发给我')
        value = self.adapter.dispatch('submit', {'message': item['message_id']})
        for _ in range(100):
            row = self.tasks.get(value['id'])
            if row['status'] == 'completed':return item, row
            time.sleep(.02)
        self.fail('task did not complete')

    def test_retry_failed_upload_resends_archive_without_execution(self):
        item, task = self.task()
        self.assertEqual(self.adapter.dispatch('submit', {'message': item['message_id']})['id'], task['id'])
        Path(task['artifacts'][0]['source_path']).write_text('源文件被改变', encoding='utf-8')
        sent_bytes = []
        def send(root, target, path, request):
            sent_bytes.append(Path(path).read_text(encoding='utf-8'))
            return {'state': 'failed' if len(sent_bytes) == 1 else 'sent', 'receipt': {'status': 'failed' if len(sent_bytes) == 1 else 'ok'}}
        params = {'message': item['message_id'], 'task': task['id']}
        with patch('owner_files.send_file', side_effect=send):
            first = self.adapter.dispatch('deliver', params)
            self.adapter.dispatch('deliver', params)
            self.assertEqual(len(sent_bytes), 1)
            final = self.adapter.dispatch('deliver', params | {'retry': True})
        self.assertEqual(sent_bytes, ['归档的文章', '归档的文章'])
        self.assertEqual([d['state'] for d in final['deliveries']], ['failed', 'sent'])
        self.assertEqual(len(self.runs), 1)

    def test_unknown_upload_never_repeats_and_permission_is_program_owned(self):
        item, task = self.task()
        params = {'message': item['message_id'], 'task': task['id']}
        with patch('owner_files.send_file', return_value={'state': 'unknown', 'receipt': {}}) as send:
            self.adapter.dispatch('deliver', params)
            self.adapter.dispatch('deliver', params | {'retry': True})
            self.assertEqual(send.call_count, 1)
        foreign = self.item('创建文件发给我', sender='111111111')
        with self.assertRaises(PermissionError):self.adapter.dispatch('submit', {'message': foreign['message_id']})
        cfg = self.loop.governance.config()
        self.loop.governance.save({'owner_permissions': [p for p in cfg['owner_permissions'] if p != 'computer.work']})
        with self.assertRaises(PermissionError):self.adapter.dispatch('deliver', params)
