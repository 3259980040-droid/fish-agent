"""QQ command and pause regressions over a real local WebSocket transport."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
import dependencies
from websockets.asyncio.server import serve
from fish_desktop.bootstrap import initialize
from qq_agent_loop import QQAgentLoop
from storage import read_json, write_json


class QQControlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fish-qq-controls-')
        self.root = initialize(Path(self.temp.name) / 'personal')
        self.owner = '123456789'
        path = self.root / 'channels/fish_qq/config.json'
        config = read_json(path)
        config['allowlist']['owner_id'] = self.owner
        write_json(path, config)
        self.loop = QQAgentLoop(self.root)
        self.loop.conversation = self.loop.store.get_or_create_conversation('private:' + self.owner, 'qq')
        self.connected = asyncio.Event()
        self.actions = []
        self.connections = 0
        self.seq = 0
        self.acknowledge = True

        async def connection(ws):
            self.connections += 1
            await ws.recv()
            await ws.send(json.dumps({'type': 'hello.ok', 'payload': {'epoch': 'fixture', 'seq': 0}}))
            self.server_ws = ws
            self.connected.set()
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get('type') == 'action':
                    self.actions.append(frame)
                    if self.acknowledge:
                        await ws.send(json.dumps({'type': 'action.result', 'id': frame['id'], 'ok': True,
                                                  'payload': {'message_id': 'fixture-' + frame['id']}}))

        self.server = await serve(connection, '127.0.0.1', 0)
        self.loop.channel.url = 'ws://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
        await self.loop.channel.start()
        await asyncio.wait_for(self.connected.wait(), 2)

    async def asyncTearDown(self):
        await self.loop.channel.stop()
        self.server.close()
        await self.server.wait_closed()
        self.temp.cleanup()

    async def send(self, text):
        self.seq += 1
        await self.server_ws.send(json.dumps({'type': 'event', 'event': 'message.received', 'seq': self.seq,
            'payload': {'chat_type': 'private', 'chat_id': 'private:' + self.owner,
                        'sender': {'id': self.owner}, 'message_id': 'fixture-' + str(self.seq),
                        'segments': [{'type': 'text', 'text': text}]}}))

    async def until(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), 2)

    async def flush(self):
        await self.until(lambda: self.loop.channel.last_seq == self.seq)
        await asyncio.wait_for(self.loop.channel._events.join(), 2)

    def messages(self):
        with closing(self.loop.store.connect()) as db:
            return [dict(row) for row in db.execute('SELECT text,status FROM messages ORDER BY id')]

    async def test_command_receipt_does_not_block_following_events(self):
        await self.send('/状态')
        await self.send('after-status')
        await self.flush()
        self.assertEqual(len(self.actions), 1)
        self.assertEqual(self.connections, 1)
        self.assertEqual(self.loop.queue.get_nowait()['text'], 'after-status')
        self.loop.queue.task_done()
        self.assertEqual(self.loop.channel.futures, {})

    async def test_paused_message_stays_skipped_after_immediate_resume(self):
        # Back-to-back frames exercise the race without relying on human delays.
        await self.send('/暂停')
        await self.send('during-pause')
        await self.send('/继续')
        await self.send('after-resume')
        await self.flush()
        self.assertFalse(self.loop.governance.config()['paused'])
        self.assertEqual(len(self.actions), 2)
        self.assertEqual(self.connections, 1)
        self.assertEqual(self.messages(), [{'text': 'during-pause', 'status': 'paused'},
                                           {'text': 'after-resume', 'status': 'queued'}])
        self.assertEqual(self.loop.queue.qsize(), 1)
        self.assertEqual(self.loop.queue.get_nowait()['text'], 'after-resume')
        self.loop.queue.task_done()

    async def test_pause_drops_previously_queued_work(self):
        await self.send('waiting-task')
        await self.send('/暂停')
        await self.send('/继续')
        await self.flush()
        self.assertEqual(self.messages(), [{'text': 'waiting-task', 'status': 'paused'}])
        self.assertTrue(self.loop.queue.empty())
        await asyncio.wait_for(self.loop.queue.join(), 0.5)

    async def test_disconnect_releases_inflight_action(self):
        self.acknowledge = False
        task = asyncio.create_task(self.loop.channel.action('send_message', {}, timeout=5))
        await self.until(lambda: bool(self.actions))
        await self.server_ws.close()
        with self.assertRaisesRegex(RuntimeError, 'disconnected'):
            await asyncio.wait_for(task, 1)
        self.assertEqual(self.loop.channel.futures, {})


if __name__ == '__main__':
    unittest.main()
