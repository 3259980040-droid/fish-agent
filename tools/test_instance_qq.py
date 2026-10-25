"""W03: one account ingress, two real instance records and their own receipts."""
import asyncio
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies
from test_social_base import Fixture
from test_unified_execution import FakeBudget
from locations import InstanceCatalog, project_path
from workbench import Workbench
from tool_host import ToolHost
from fish_agent import get_runtime, reset_runtime
from model_gateway import ChatResult
from storage import read_json, write_json


class InstanceQQ(Fixture):
    def tearDown(self):
        self.doCleanups()
        super().tearDown()

    def test_shared_group_ingress_keeps_background_instances_and_outgoing_receipts_independent(self):
        self.group = '765432198'
        self.chat = 'group:' + self.group
        bench = Workbench(self.root)
        from types import SimpleNamespace
        status = patch('requests.get', return_value=SimpleNamespace(json=lambda: {'bot': {'id': '99999'}, 'connected': True}))
        status.start(); self.addCleanup(status.stop)
        bench.dispatch('channels.update', {'receive': False})
        entries = [bench.dispatch('instances.create', {'name': name,
            'persona': {'name': name, 'body': name + '自己的性格'}, 'running': False}) for name in ('甲', '乙')]
        ids = [entry['id'] for entry in entries]
        catalog = InstanceCatalog(self.root)
        self.addCleanup(lambda: [reset_runtime(catalog.resolve(row['id'])) for row in catalog.records()])
        for iid in ids:
            bench.dispatch('channels.update', {'instance_id': iid, 'connection_id': 'qq', 'receive': True,
                                               'private_receive': True, 'group_id': self.group, 'listen': True})
        self.assertTrue(bench.dispatch('channels.status', {'instance_id': ids[1]})['private_receive'])
        self.assertEqual(bench.dispatch('channels.status', {'instance_id': ids[1]})['groups'][0]['id'], self.group)
        host = ToolHost(self.root)
        host.serve(background_workers=False, listen_port=0)
        self.addCleanup(lambda: (host.stop.set(), host.server.shutdown(), host.server.server_close(), host.close_instances()))
        config = read_json(project_path(self.root, 'harness/agents.json'))
        config['runtime']['tool_host_port'] = host.server.server_address[1]
        write_json(project_path(self.root, 'harness/agents.json'), config)
        requests = []
        class Gateway:
            base = 'https://fake.invalid'
            model = 'fake-model'
            def __init__(self, *args): pass
            def chat(self, messages, **options):
                encoded = json.dumps(messages, ensure_ascii=False)
                requests.append(encoded)
                if messages[-1]['role'] == 'tool': return ChatResult('', {}, 'stop', first_output_at=time.time())
                if '主人私聊测试' in encoded:
                    words = '甲私聊答复' if '甲自己的性格' in encoded else '乙私聊答复'
                    return ChatResult('', {}, 'tool_calls', ({'id': 'same-private-call', 'type': 'function',
                        'function': {'name': 'fish_say', 'arguments': json.dumps({'texts': [words]}, ensure_ascii=False)}},),
                        first_output_at=time.time())
                return ChatResult('', {}, 'stop', first_output_at=time.time())
        from context_budget import _tokenizer
        tokenizer = _tokenizer(str(ROOT / 'vendor/tokenizers/deepseek-v4/tokenizer.json'))
        from qq_agent_loop import QQAgentLoop
        from channels.fish_qq.config import FishQQConfig
        from channels.fish_qq.server import FishQQApp
        loop = QQAgentLoop(self.root)
        channel = FishQQApp(FishQQConfig(root=self.root, owner_id=self.owner, group_ids=[], state_dir=self.root / 'offline-channel'))
        async def exercise():
            received = []
            async def emit(event, payload):
                received.append(payload)
                await loop.on_event({'event': event, 'payload': payload})
            channel._emit = emit
            from types import SimpleNamespace
            sent = []
            async def send(action, payload, **kwargs):
                sent.append({'action': action, 'payload': payload})
                return {'data': {'message_id': str(7000 + len(sent))}}
            channel.onebot = SimpleNamespace(send_action=send)
            channel.state = 'ready'
            send_errors = []
            async def action(name, payload, **kwargs):
                self.assertEqual(name, 'send_message')
                try: return await channel._send_message(payload)
                except Exception as error:
                    send_errors.append(type(error).__name__ + ': ' + str(error))
                    raise
            loop.channel.action = action
            loop.channel.bot = {'id': '99999'}
            for iid in ids:
                bench.dispatch('instances.set_running', {'id': iid, 'running': True})
            bench.dispatch('tools.settings.save', {'instance_id': ids[1], 'patch': {'send': {'group': False}}})
            raw = {'post_type': 'message', 'message_type': 'group', 'group_id': int(self.group),
                   'user_id': 222222222, 'message_id': 101, 'time': int(time.time()),
                   'sender': {'user_id': 222222222, 'nickname': '群友'},
                   'message': [{'type': 'at', 'data': {'qq': '99999'}}, {'type': 'text', 'data': {'text': '两窗同收'}}]}
            channel.config.bot_id = '99999'
            await channel._on_onebot_event(raw)
            await channel._on_onebot_event(raw)
            await loop.on_event({'event': 'message.received', 'payload': received[-1]})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                events = [get_runtime(catalog.resolve(iid)).experience.read() for iid in ids]
                if all(len(rows) == 1 and rows[0]['considered'] for rows in events) and not loop._private_watches:
                    break
                await asyncio.sleep(.03)
            for iid in ids:
                runtime = get_runtime(catalog.resolve(iid))
                events = runtime.experience.read()
                self.assertEqual(len(events), 1)
                self.assertTrue(events[0]['considered'])
                view = bench.dispatch('turn.view', {'instance_id': iid, 'execution': events[0]['run']})
                self.assertTrue(view['silent'])
                self.assertEqual(view['materials'][0]['original']['channel_message_id'], '101')
            self.assertEqual(len(requests), 2, '关闭发送仍须进入自己的思考')
            bench.dispatch('instances.set_running', {'id': 'main', 'running': False})
            first = await loop.group_deliver({'instance_id': ids[0], 'target': self.chat,
                'bubbles': [{'kind': 'text', 'text': '甲的后台出站'}], 'key': 'same-call', 'turn': 'call-a'})
            self.assertEqual(send_errors, [])
            self.assertTrue(first['sent'], first)
            blocked = await loop.group_deliver({'instance_id': ids[1], 'target': self.chat,
                'bubbles': [{'kind': 'text', 'text': '乙未启用发送'}], 'key': 'same-call', 'turn': 'call-b'})
            self.assertEqual(blocked['state'], 'blocked')
            bench.dispatch('tools.settings.save', {'instance_id': ids[1], 'patch': {'send': {'group': True}}})
            second = await loop.group_deliver({'instance_id': ids[1], 'target': self.chat,
                'bubbles': [{'kind': 'text', 'text': '乙的后台出站'}], 'key': 'same-call', 'turn': 'call-b'})
            self.assertTrue(second['sent'], second)
            self.assertNotEqual(first['receipt']['message_id'], second['receipt']['message_id'])
            self.assertEqual(len(sent), 2)
            # Replace only the local HTTP transport, preserving the real adapter's
            # encoded instance envelope and the real QQ private delivery receiver.
            import requests as http
            original_post = http.post
            event_loop = asyncio.get_running_loop()
            def post(url, *args, **kwargs):
                if str(url) != 'http://127.0.0.1:3102/deliver': return original_post(url, *args, **kwargs)
                body = json.loads(kwargs['data'])
                coroutine = loop.group_deliver(body) if body['target'].startswith('group:') else loop.private_deliver(body)
                result = asyncio.run_coroutine_threadsafe(coroutine, event_loop).result(10)
                return SimpleNamespace(json=lambda: result, status_code=200)
            with patch('requests.post', post):
                private = {'post_type': 'message', 'message_type': 'private', 'sub_type': 'friend',
                    'user_id': int(self.owner), 'message_id': 102, 'time': int(time.time()),
                    'sender': {'user_id': int(self.owner), 'nickname': '主人'},
                    'message': [{'type': 'text', 'data': {'text': '主人私聊测试'}}]}
                await channel._on_onebot_event(private)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    events = [get_runtime(catalog.resolve(iid)).experience.read() for iid in ids]
                    if all(len(rows) == 2 and rows[0]['considered'] for rows in events) and not loop._private_watches: break
                    await asyncio.sleep(.03)
            self.assertEqual(len(sent), 4, send_errors)
            from bot_control import BotControl
            from locations import instance_context
            from tool_host import request
            def native_transport(control, target, text, key, reply_to='', *, segments=None, source_tools=()):
                body = {'instance_id': instance_context(control.root).instance_id, 'chat_id': target,
                        'segments': segments or [{'type': 'text', 'text': text}],
                        'idempotency_key': key, 'source_tools': list(source_tools)}
                return asyncio.run_coroutine_threadsafe(channel._send_message(body), event_loop).result(10)
            with patch.object(BotControl, '_channel_transport', native_transport):
                context = catalog.resolve(ids[0])
                await asyncio.to_thread(request, context.root, '/bind',
                    {'role': 'qq', 'session': 'direct-tool', 'scope': 'owner', 'sender': self.owner, 'desktop': True}, role='qq')
                direct = await asyncio.to_thread(request, context.root, '/execute',
                    {'session': 'direct-tool', 'name': 'fish_group_send', 'arguments': {'chat_id': self.chat, 'text': '甲的明确工具出站'},
                     'call_id': 'original-call'}, role='qq', admin=False)
            self.assertEqual(direct['state'], 'SUCCEEDED', direct)
            self.assertEqual(len(sent), 5)
            views = []
            for iid, words in zip(ids, ('甲的后台出站', '乙的后台出站')):
                runtime = get_runtime(catalog.resolve(iid))
                history = bench.dispatch('chat.snapshot', {'instance_id': iid, 'id': 'qq:' + self.chat + ':qq'})
                self.assertEqual([row['text'] for row in history['messages'] if row['direction'] == 'fish'],
                                 [words, '甲的明确工具出站'] if iid == ids[0] else [words])
                latest = runtime.experience.read()[0]
                view = bench.dispatch('turn.view', {'instance_id': iid, 'execution': latest['run']})
                self.assertTrue(view['delivery']['sent'], view)
                self.assertEqual(view['materials'][0]['original']['status'], 'processed')
                self.assertTrue(view['delivery_times']['receipt'])
                trace = json.dumps(view['trace'], ensure_ascii=False)
                self.assertIn('甲私聊答复' if iid == ids[0] else '乙私聊答复', trace)
                self.assertNotIn('乙私聊答复' if iid == ids[0] else '甲私聊答复', trace)
                views.append(view)
            self.evidence = {'instances': ids, 'private_turns': views, 'outbox': channel.outbox.all(),
                             'direct_tool_result': direct, 'native_sent': sent, 'model_requests': requests}
            loop._stop.set()
            watches = list(loop._private_watches)
            for task in watches: task.cancel()
            await asyncio.gather(*watches, return_exceptions=True)
        with patch('model_gateway.ModelGateway', Gateway), patch('context_budget.ContextBudget', FakeBudget), \
                patch('desktop_context._tokens', lambda root, text: len(tokenizer.encode(str(text or '')).ids)):
            asyncio.run(exercise())


if __name__ == '__main__': unittest.main()
