"""Focused A-batch checks: local route, media, history, real cancellation and jobs."""
import asyncio
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
from PIL import Image
from desktop_chat import DesktopChat
from model_gateway import ModelGateway, ChatOptions, ChatResult
from social_context import SocialContext
from storage import database
from workbench import Workbench


def fixture(root):
    (root / 'harness/agents').mkdir(parents=True)
    (root / 'harness/agents.json').write_text(json.dumps({'agents': [
        {'id': 'qq', 'name': 'Fish', 'model': 'configured-model',
         'apiKeyEnv': 'FISH_TEST_ONLY_KEY', 'persona': 'harness/agents/qq.persona.md'}]}), encoding='utf-8')
    (root / 'harness/agents/qq.persona.md').write_text('FISH_PERSONA_ONLY', encoding='utf-8')


class DirectChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture(self.root)
        self.chat = DesktopChat(self.root, 'qq', MagicMock())
        persona = SocialContext(self.root).save_persona('qq', 'test', 'FISH_PERSONA_ONLY',
            global_rules='DSH_GLOBAL_MUST_NOT_ENTER', workspace_rules='DSH_WORKSPACE_MUST_NOT_ENTER')
        self.cid = self.chat.new(revision=persona['id'])
        self.requests = []
        self.gateway = MagicMock(model='configured-model', base='https://api.deepseek.com', activity={})
        def respond(messages, **kwargs):
            self.requests.append(copy.deepcopy(messages))
            return ChatResult('好的，我记住了这轮对话。', {}, 'stop')
        self.gateway.chat.side_effect = respond
        for name, value in [
            ('model_gateway.ModelGateway', MagicMock(return_value=self.gateway)),
            ('web_knowledge.WebKnowledge.resolve', MagicMock(return_value='')),
            ('conversation_style.private_context', MagicMock(return_value='')),
        ]:
            p = patch(name, value)
            p.start()
            self.addCleanup(p.stop)
        self.memory = MagicMock()
        self.memory.context.return_value = '[test memory]'
        p = patch('memory_store.MemoryStore', return_value=self.memory)
        p.start()
        self.addCleanup(p.stop)
        self.chat._client = MagicMock(side_effect=AssertionError('ordinary chat must not enter DSH'))

    def test_multiturn_persona_scope_and_single_current_message(self):
        self.chat.send(self.cid, '这一轮记住暗号蓝鲸', [])
        self.chat.send(self.cid, '上轮暗号是什么', [])
        self.assertEqual([m['role'] for m in self.requests[1]], ['system', 'user', 'assistant', 'user'])
        raw = json.dumps(self.requests[1], ensure_ascii=False)
        self.assertEqual(raw.count('上轮暗号是什么'), 1)
        self.assertIn('这一轮记住暗号蓝鲸', raw)
        self.assertIn('FISH_PERSONA_ONLY', raw)
        self.assertNotIn('DSH_GLOBAL_MUST_NOT_ENTER', raw)
        self.assertNotIn('DSH_WORKSPACE_MUST_NOT_ENTER', raw)
        self.assertNotIn('其他群友', raw)
        self.assertEqual(self.memory.context.call_args.args[1], self.chat.conversation_context(self.cid)['scope'])
        self.chat._client.assert_not_called()
        self.assertEqual(len(self.chat.snapshot(self.cid)['messages']), 4)

    def test_image_wire_and_history_are_preserved(self):
        path = self.root / 'image.png'
        Image.new('RGB', (48, 32), 'red').save(path)
        self.chat.send(self.cid, '图里是什么颜色', [{'path': str(path), 'type': 'image'}])
        image = next(b for b in self.requests[0][-1]['content'] if b['type'] == 'image_url')
        self.assertTrue(image['image_url']['url'].startswith('data:image/png;base64,'))
        self.chat.send(self.cid, '上一张图呢', [])
        self.assertEqual(image, next(b for b in self.requests[1][1]['content'] if b['type'] == 'image_url'))
        with database(self.chat.db) as db:
            cached = db.execute('SELECT content FROM chat_inputs ORDER BY message LIMIT 1').fetchone()[0]
        self.assertNotIn('base64', cached)
        self.assertIn('_fish_path', cached)

    def test_legacy_history_and_persona_reload_keep_ids(self):
        before = self.chat._record(self.cid, 'inbound', '旧版已说过的话', [], 'done')
        self.chat._record(self.cid, 'outbound', '旧版的回复', [], 'done')
        newer = SocialContext(self.root).save_persona('qq', 'new', 'NEW_PERSONA')
        self.chat.reload_persona(self.cid, newer['id'])
        self.chat.send(self.cid, '继续旧话题', [])
        raw = json.dumps(self.requests[0], ensure_ascii=False)
        self.assertIn('旧版已说过的话', raw)
        self.assertIn('NEW_PERSONA', raw)
        self.assertEqual(self.chat.snapshot(self.cid)['messages'][0]['id'], before)

    def test_late_result_cannot_commit_then_next_turn_works(self):
        def late(*args, **kwargs):
            self.chat.cancel()
            return ChatResult('晚到回复', {}, 'stop')
        self.gateway.chat.side_effect = late
        with self.assertRaises(InterruptedError):
            self.chat.send(self.cid, '取消这一轮', [])
        self.assertEqual(self.chat.snapshot(self.cid)['messages'][-1]['status'], 'cancelled')
        self.gateway.chat.side_effect = lambda *a, **kw: ChatResult('下一轮正常', {}, 'stop')
        self.chat.send(self.cid, '下一轮', [])
        rows = self.chat.snapshot(self.cid)['messages']
        self.assertEqual([r['text'] for r in rows if r['direction'] == 'outbound'], ['下一轮正常'])

    def test_api_failure_does_not_fallback_or_complete(self):
        self.gateway.chat.side_effect = TimeoutError('测试超时')
        with self.assertRaises(TimeoutError):
            self.chat.send(self.cid, '这一轮失败', [])
        self.chat._client.assert_not_called()
        self.assertEqual(self.chat.snapshot(self.cid)['messages'][-1]['status'], 'failed')

    def test_old_work_branch_remains_separate(self):
        self.chat.set_mode(self.cid, 'work', str(self.root))
        self.chat._client.side_effect = None
        async def work(*args):
            return {'ok': True, 'text': '旧工作会话结果'}
        self.chat._turn = work
        self.chat.send(self.cid, '旧工作任务', [])
        self.chat._client.assert_called_once()
        self.gateway.chat.assert_not_called()

    def test_compaction_keeps_raw_history_and_current_once(self):
        (self.root / 'harness/control.json').write_text('{"desktop_context_chars":8000}', encoding='utf-8')
        for index in range(6):
            self.chat._record(self.cid, 'inbound', '旧问题' + str(index) + '文' * 1500, [], 'done')
            self.chat._record(self.cid, 'outbound', '旧回答' + str(index) + '字' * 1500, [], 'done')
        self.gateway.call.return_value = '此前讨论的简短摘要'
        self.chat.send(self.cid, '本轮继续', [])
        raw = json.dumps(self.requests[0], ensure_ascii=False)
        self.assertIn('此前讨论的简短摘要', raw)
        self.assertEqual(raw.count('本轮继续'), 1)
        self.assertEqual(len(self.chat.snapshot(self.cid)['messages']), 14)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture(self.root)
        with patch('secrets_store.SecretStore.get', return_value='test-key'):
            self.gateway = ModelGateway(self.root, 'qq')

    def transport(self, response):
        original = httpx.AsyncClient
        def make(**kwargs):
            return original(**kwargs, transport=httpx.MockTransport(response))
        p = patch('model_gateway.httpx.AsyncClient', side_effect=make)
        p.start()
        self.addCleanup(p.stop)

    def test_actual_wire_options_reasoning_usage_and_text(self):
        sent = []
        def handle(request):
            sent.append(json.loads(request.content))
            events = [
                {'choices': [{'delta': {'reasoning_content': 'private reasoning must not leak'}}]},
                {'choices': [{'delta': {'content': '完整回复'}, 'finish_reason': 'stop'}]},
                {'choices': [], 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}]
            data = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events) + 'data: [DONE]\n\n'
            return httpx.Response(200, text=data)
        self.transport(handle)
        result = self.gateway.chat([{'role': 'user', 'content': 'hello'}], options=ChatOptions('high', 8192, 2))
        self.assertEqual(result.text, '完整回复')
        self.assertEqual(result.usage['prompt_tokens'], 11)
        self.assertEqual(sent[0]['thinking'], {'type': 'enabled'})
        self.assertEqual(sent[0]['reasoning_effort'], 'high')
        self.assertEqual(sent[0]['model'], 'configured-model')
        self.assertEqual(sent[0]['max_tokens'], 8192)

    def test_truncated_stream_is_a_failure(self):
        self.transport(lambda request: httpx.Response(200, text='data: {"choices":[{"delta":{"content":"片段"},"finish_reason":"length"}]}\n\n'))
        with self.assertRaisesRegex(RuntimeError, '输出 token 上限'):
            self.gateway.chat([{'role': 'user', 'content': 'x'}])

    def test_semantic_reply_uses_existing_stream_and_thinking_without_native_json_mode(self):
        sent = []
        def handle(request):
            sent.append(json.loads(request.content))
            raw = json.dumps({'bubbles': [{'kind': 'text', 'text': '第一条'}, {'kind': 'text', 'text': '第二条'}]})
            event = {'choices': [{'delta': {'content': raw}, 'finish_reason': 'stop'}]}
            return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n')
        self.transport(handle)
        answer = self.gateway.chat([{'role': 'user', 'content': '分两条消息'}], options=ChatOptions('high', 8192, 2))
        self.assertEqual(len(json.loads(answer.text)['bubbles']), 2)
        self.assertNotIn('response_format', sent[0])
        self.assertEqual(sent[0]['reasoning_effort'], 'high')
        self.assertTrue(sent[0]['stream'])

    def test_http_failure_keeps_image_and_does_not_retry(self):
        count = []
        def handle(request):
            count.append(json.loads(request.content))
            return httpx.Response(400, text='unsupported image')
        self.transport(handle)
        image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,TEST'}}
        with self.assertRaisesRegex(RuntimeError, 'HTTP 400'):
            self.gateway.chat([{'role': 'user', 'content': [image]}])
        self.assertEqual(len(count), 1)
        self.assertEqual(count[0]['messages'][0]['content'], [image])

    def test_cancellation_closes_local_task_promptly(self):
        closed = threading.Event()
        async def waiting(*args):
            try:
                await asyncio.sleep(10)
            finally:
                closed.set()
        self.gateway._chat_stream = waiting
        event = threading.Event()
        timer = threading.Timer(.03, event.set)
        timer.start()
        start = time.monotonic()
        with self.assertRaises(InterruptedError):
            self.gateway.chat([], cancelled=event)
        timer.join()
        self.assertTrue(closed.is_set())
        self.assertLess(time.monotonic() - start, 1)

    def test_total_timeout_is_truthful(self):
        async def waiting(*args):
            await asyncio.sleep(10)
        self.gateway._chat_stream = waiting
        with self.assertRaises(TimeoutError):
            self.gateway.chat([], options=ChatOptions('off', 8192, .03))

    def test_missing_fish_key_never_reads_dsh_credentials(self):
        with patch('secrets_store.SecretStore.get', return_value=''), patch('model_gateway.Path.home', side_effect=AssertionError('DSH credential access')):
            gateway = ModelGateway(self.root, 'qq')
            with self.assertRaisesRegex(ValueError, 'Fish'):
                gateway.chat([])

    def test_background_call_keeps_its_original_options(self):
        response = MagicMock(ok=True)
        response.json.return_value = {'choices': [{'message': {'content': 'background'}}], 'usage': {'prompt_tokens': 2}}
        with patch('model_gateway.requests.post', return_value=response) as post:
            self.assertEqual(self.gateway.call([{'role': 'user', 'content': 'x'}]), 'background')
        self.assertEqual(post.call_args.kwargs['json']['thinking'], {'type': 'disabled'})
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 4096)


class WorkbenchTests(unittest.TestCase):
    def test_cancellation_status_and_atomic_job_claim(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            fixture(root)
            wb = Workbench(root)
            wb._chat_instance = MagicMock()
            wb._chat_instance.send.side_effect = InterruptedError('本地已取消等待')
            with database(wb.path) as db:
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', ('a'*32, 'cid', 'queued', '', 0, 0))
            wb._send('a'*32, 'cid', 'x', [])
            wb._send('a'*32, 'cid', 'x', [])
            wb._chat_instance.send.assert_called_once()
            self.assertEqual(wb.dispatch('chat.job', {'job_id': 'a'*32})['status'], 'cancelled')
            wb.close()

    def test_reused_submission_id_does_not_schedule_twice(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            fixture(root)
            wb = Workbench(root)
            wb._chat_instance = MagicMock()
            with patch.object(wb._executor, 'submit') as submit:
                args = {'id': 'cid', 'text': '相同提交', 'request_id': 'b'*32}
                first = wb.dispatch('chat.send', args)
                second = wb.dispatch('chat.send', args)
                self.assertEqual(first, second)
                submit.assert_called_once()
            wb.close()


if __name__ == '__main__':
    unittest.main()
