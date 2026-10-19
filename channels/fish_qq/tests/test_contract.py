from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from channels.fish_qq.config import FishQQConfig
from channels.fish_qq.server import FishQQApp
from channels.fish_qq.spool import Outbox
from channels.fish_qq.tests.fake_napcat import FakeNapCat

TOKEN = "test-core-token"
OWNER = "200000001"
GROUP = "123456"


class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ["FISH_QQ_TOKEN"] = TOKEN
        os.environ["NAPCAT_TOKEN"] = "test-napcat-token"
        self.tmp = Path(tempfile.mkdtemp(prefix="fishqq-contract-"))
        self.fake = FakeNapCat("test-napcat-token")
        await self.fake.start()
        cfg = FishQQConfig.from_dict({
            "listen": {"host": "127.0.0.1", "port": 0, "path": "/ws", "token_env": "FISH_QQ_TOKEN"},
            "napcat": {
                "url": self.fake.url, "token_env": "NAPCAT_TOKEN",
                "send_timeout_s": 0.5, "reconnect_delays": [0.05, 0.05],
            },
            "allowlist": {"owner_id": OWNER, "group_ids": [GROUP], "private_mode": "owner_only"},
            "spool_dir": str(self.tmp / "channel-state"),
            "bot": {"id": "900001", "display_name": "大肥鱼"},
        }, self.tmp)
        self.app = FishQQApp(cfg)
        await self.app.start()
        await asyncio.wait_for(self.app.ready_event.wait(), 5)
        self.core, self.hello = await self.connect_core()
        self.stopped = False

    async def asyncTearDown(self) -> None:
        try:
            if not self.stopped:
                await self.app.stop()
        finally:
            await self.fake.stop()
            shutil.rmtree(self.tmp, ignore_errors=True)

    async def connect_core(self, last_seq: int = 0, epoch: str | None = None, token: str = TOKEN):
        ws = await connect(
            f"ws://127.0.0.1:{self.app.port}/ws",
            additional_headers={"Authorization": f"Bearer {token}"},
        )
        await ws.send(json.dumps({
            "v": 1, "type": "hello", "id": "h1", "ts": "x",
            "payload": {"client": "fish-core", "last_seq": last_seq, "epoch": epoch},
        }))
        hello = json.loads(await asyncio.wait_for(ws.recv(), 3))
        return ws, hello

    async def recv(self, timeout: float = 3) -> dict:
        return json.loads(await asyncio.wait_for(self.core.recv(), timeout))

    async def recv_event(self, name: str, timeout: float = 3) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = await self.recv(max(0.1, deadline - time.monotonic()))
            if frame.get("type") == "event" and frame.get("event") == name:
                return frame
        raise TimeoutError(name)

    async def action(self, action: str, payload: dict, action_id: str = "a1") -> dict:
        await self.core.send(json.dumps({
            "v": 1, "type": "action", "id": action_id, "action": action, "payload": payload,
        }))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            frame = await self.recv(max(0.1, deadline - time.monotonic()))
            if frame.get("type") == "action.result" and frame.get("id") == action_id:
                return frame
        raise TimeoutError(action)

    def make_event(self, text: str, message_id: str, *, user_id: str = OWNER,
                   group_id: str = GROUP, message: list | None = None) -> dict:
        return {
            "post_type": "message", "message_type": "group",
            "group_id": int(group_id), "user_id": int(user_id),
            "message_id": message_id,
            "sender": {"user_id": int(user_id), "nickname": "主人"},
            "message": message or [{"type": "text", "data": {"text": text}}],
        }

    async def test_member_sync_is_read_only_and_allowlisted(self):
        result=await self.action('get_group_members',{'group_id':GROUP})
        self.assertTrue(result['ok'],result)
        self.assertEqual(len(result['payload']['members']),2)
        self.assertEqual(set(result['payload']['members'][0]),{'user_id','nickname','card'})
        before=len(self.fake.actions)
        denied=await self.action('get_group_members',{'group_id':'987654321'},action_id='denied')
        self.assertFalse(denied['ok']);self.assertEqual(len(self.fake.actions),before)
        self.assertFalse(any(a['action'].startswith('send_') for a in self.fake.actions))

    async def test_01_auth_rejects_bad_token(self) -> None:
        ws = await connect(
            f"ws://127.0.0.1:{self.app.port}/ws",
            additional_headers={"Authorization": "Bearer wrong"},
        )
        frame = json.loads(await asyncio.wait_for(ws.recv(), 3))
        self.assertEqual(frame.get("error", {}).get("code"), "unauthorized")
        with self.assertRaises(ConnectionClosed) as ctx:
            await asyncio.wait_for(ws.recv(), 3)
        self.assertEqual(ctx.exception.code, 4401)

    async def test_02_allowlist_no_leak(self) -> None:
        await self.fake.push_event(self.make_event("bad-group", "1", group_id="999"))
        await self.fake.push_event({
            "post_type": "message", "message_type": "private", "user_id": 111,
            "message_id": "2", "sender": {"user_id": 111, "nickname": "陌生人"},
            "message": [{"type": "text", "data": {"text": "secret"}}],
        })
        await asyncio.sleep(0.1)
        status = (await self.action("get_status", {}))["payload"]
        self.assertGreaterEqual(status["counters"]["received"], 0)
        # 允许的主人私聊应当进来；前两条不应产生 message.received
        await self.fake.push_event({
            "post_type": "message", "message_type": "private", "user_id": int(OWNER),
            "message_id": "3", "sender": {"user_id": int(OWNER), "nickname": "主人"},
            "message": [{"type": "text", "data": {"text": "hello"}}],
        })
        event = await self.recv_event("message.received")
        self.assertEqual(event["payload"]["chat_id"], f"private:{OWNER}")
        status = (await self.action("get_status", {}))["payload"]
        self.assertGreaterEqual(self.app.counters.dropped_allowlist, 2)
        self.assertEqual(status["state"], "ready")

    async def test_03_normalize_segments(self) -> None:
        await self.fake.push_event(self.make_event("", "10", message=[
            {"type": "text", "data": {"text": "在吗"}},
            {"type": "image", "data": {"url": "https://example.com/a.jpg", "summary": "图"}},
            {"type": "at", "data": {"qq": "900001"}},
            {"type": "reply", "data": {"id": "m-1"}},
            {"type": "face", "data": {"id": "14", "text": "笑"}},
            {"type": "video", "data": {"url": "https://example.com/v.mp4"}},
        ]))
        event = await self.recv_event("message.received")
        payload = event["payload"]
        types = [seg["type"] for seg in payload["segments"]]
        self.assertEqual(types, ["text", "image", "at", "face", "video"])
        self.assertEqual(payload["segments"][-1]["url"], "https://example.com/v.mp4")
        self.assertNotIn("data", payload["segments"][-1])
        self.assertTrue(payload["mentions_bot"])
        self.assertEqual(payload["reply_to"], "m-1")
        self.assertNotIn("raw_message", payload)
        self.assertNotIn("data", payload["segments"][0])

    async def test_04_send_text_image_reply(self) -> None:
        await self.core.send(json.dumps({"v": 1, "type": "ping", "id": "p1", "ts": "x"}))
        pong = await self.recv()
        self.assertEqual(pong["type"], "pong")
        self.assertEqual(pong["id"], "p1")
        for i, segments in enumerate((
            [{"type": "text", "text": "在，刚翻了个身。"}],
            [{"type": "image", "url": "https://example.com/a.jpg"}],
            [{"type": "text", "text": "收到"}],
        )):
            result = await self.action("send_message", {
                "chat_id": f"group:{GROUP}", "segments": segments,
                "reply_to": "12345" if i == 2 else None,
                "idempotency_key": f"send-{i}",
            }, f"s{i}")
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["payload"]["message_id"])
        last = self.fake.actions[-1]
        self.assertEqual(last["action"], "send_group_msg")
        self.assertEqual(last["params"]["message"][0]["type"], "reply")
        self.assertEqual(last["params"]["message"][1]["type"], "text")

    async def test_05_history_30(self) -> None:
        result = await self.action("get_history", {"chat_id": f"group:{GROUP}", "limit": 30})
        self.assertTrue(result["ok"])
        messages = result["payload"]["messages"]
        self.assertEqual(len(messages), 30)
        self.assertEqual(messages[0]["segments"][0]["text"], "history-0")
        self.assertEqual(messages[-1]["segments"][0]["text"], "history-29")
        self.assertNotIn("raw_message", messages[0])

    async def test_06_inbound_60_no_drop(self) -> None:
        for i in range(60):
            await self.fake.push_event(self.make_event(f"m{i}", f"60-{i}"))
        seen = 0
        while seen < 60:
            event = await self.recv_event("message.received", timeout=5)
            self.assertTrue(event["payload"]["message_id"].startswith("60-"))
            seen += 1
        self.assertEqual(seen, 60)
        self.assertEqual(self.app.counters.dropped_dup, 0)

    async def test_07_outbound_technical_limit(self) -> None:
        results = []
        for i in range(21):
            results.append((await self.action("send_message", {
                "chat_id": f"group:{GROUP}",
                "segments": [{"type": "text", "text": f"limit-{i}"}],
                "idempotency_key": f"limit-{i}",
            }, f"l{i}")))
        self.assertTrue(all(frame["ok"] for frame in results[:20]))
        self.assertFalse(results[20]["ok"])
        self.assertEqual(results[20]["error"]["code"], "rate_limited")
        self.assertGreater(results[20]["error"]["retry_after_ms"], 0)
        self.assertEqual(len([a for a in self.fake.actions if a["action"] == "send_group_msg"]), 20)

    async def test_08_disconnect_degraded_and_reconnect(self) -> None:
        await self.fake.close_client()
        event = await self.recv_event("channel.state")
        while event["payload"]["state"] != "degraded":
            event = await self.recv_event("channel.state")
        result = await self.action("send_message", {
            "chat_id": f"group:{GROUP}", "segments": [{"type": "text", "text": "x"}],
            "idempotency_key": "degraded-1",
        }, "d1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "channel_unavailable")
        await asyncio.wait_for(self.app.ready_event.wait(), 5)
        self.assertEqual(self.app.state, "ready")
        self.assertGreaterEqual(self.app.counters.reconnects, 1)
        ready = await self.recv_event("channel.state")
        while not (ready["payload"]["state"] == "ready" and ready["payload"].get("reconnected")):
            ready = await self.recv_event("channel.state")

    async def test_09_core_replay_and_gap(self) -> None:
        await self.fake.push_event(self.make_event("one", "r1"))
        first = await self.recv_event("message.received")
        first_seq = first["seq"]
        await self.core.close()
        await asyncio.sleep(0.1)
        await self.fake.push_event(self.make_event("two", "r2"))
        await self.fake.push_event(self.make_event("three", "r3"))
        await asyncio.sleep(0.1)
        ws2, _ = await self.connect_core(last_seq=first_seq, epoch=self.app.epoch)
        self.core = ws2
        replayed = []
        for _ in range(2):
            replayed.append(await self.recv_event("message.received"))
        self.assertEqual([e["payload"]["message_id"] for e in replayed], ["r2", "r3"])
        await ws2.close()
        ws3, _ = await self.connect_core(last_seq=first_seq, epoch="wrong-epoch")
        self.core = ws3
        gap = await self.recv_event("channel.state")
        self.assertTrue(gap["payload"].get("gap"))

    async def test_10_idempotency(self) -> None:
        payload = {
            "chat_id": f"group:{GROUP}", "segments": [{"type": "text", "text": "idem"}],
            "idempotency_key": "same-key",
        }
        r1 = await self.action("send_message", payload, "i1")
        r2 = await self.action("send_message", payload, "i2")
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertEqual(r1["payload"]["message_id"], r2["payload"]["message_id"])
        sends = [a for a in self.fake.actions if a["action"] == "send_group_msg"]
        self.assertEqual(len(sends), 1)

    async def test_11_timeout_unknown_no_retry(self) -> None:
        self.fake.drop_send = True
        result = await self.action("send_message", {
            "chat_id": f"group:{GROUP}", "segments": [{"type": "text", "text": "timeout"}],
            "idempotency_key": "timeout-1",
        }, "t1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "timeout")
        self.assertTrue(result["error"]["may_have_sent"])
        await asyncio.sleep(0.2)
        record = next(r for r in self.app.outbox.all() if r.get("payload", {}).get("idempotency_key") == "timeout-1")
        self.assertEqual(record["status"], "unknown")
        sends = [a for a in self.fake.actions if a["action"] == "send_group_msg"]
        self.assertEqual(len(sends), 1)

    async def test_12_unknown_placeholder_and_unsupported_outbound(self) -> None:
        await self.fake.push_event(self.make_event("", "u1", message=[
            {"type": "mystery", "data": {"secret": "raw"}},
        ]))
        event = await self.recv_event("message.received")
        self.assertEqual(event["payload"]["segments"][0]["type"], "unknown")
        self.assertNotIn("secret", json.dumps(event["payload"], ensure_ascii=False))
        result = await self.action("send_message", {
            "chat_id": f"group:{GROUP}",
            "segments": [{"type": "text", "text": "x"}, {"type": "at", "user_id": OWNER}],
            "idempotency_key": "unsupported-1",
        }, "x1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unsupported_segment")

    async def test_14_sigterm_process_releases_lock(self) -> None:
        root = Path(__file__).resolve().parents[3]
        state = self.tmp / "sig-state"
        config_path = self.tmp / "sig-config.json"
        config_path.write_text(json.dumps({
            "listen": {"host": "127.0.0.1", "port": 0, "path": "/ws", "token_env": "FISH_QQ_TOKEN"},
            "napcat": {"url": self.fake.url, "token_env": "NAPCAT_TOKEN",
                       "send_timeout_s": 1, "reconnect_delays": [0.05]},
            "allowlist": {"owner_id": OWNER, "group_ids": [GROUP]},
            "spool_dir": str(state),
            "bot": {"id": "900001"},
        }), encoding="utf-8")
        env = os.environ.copy()
        env["FISH_QQ_TOKEN"] = TOKEN
        env["NAPCAT_TOKEN"] = "test-napcat-token"
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "channels.fish_qq", "--config", str(config_path),
            cwd=str(root), env=env, creationflags=flags,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        lock = state / "fish-qq.lock"
        try:
            for _ in range(50):
                if lock.exists():
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(lock.exists(), "lock 未创建")
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
            self.assertEqual(proc.returncode, 0)
            self.assertFalse(lock.exists(), "SIGTERM 后 lock 未释放")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def test_15_queued_recovery_auto_retries_inflight_unknown(self) -> None:
        state = self.tmp / "recover-app"
        outbox = Outbox(state)
        payload = {
            "chat_id": f"group:{GROUP}",
            "segments": [{"type": "text", "text": "recovered"}],
            "idempotency_key": "recover-q",
        }
        queued = outbox.add("send_message", payload)
        inflight = outbox.add("send_message", payload)
        outbox.set_status(inflight["id"], "inflight")
        before = len([a for a in self.fake.actions if a["action"] == "send_group_msg"])
        cfg = FishQQConfig.from_dict({
            "listen": {"host": "127.0.0.1", "port": 0, "path": "/ws", "token_env": "FISH_QQ_TOKEN"},
            "napcat": {"url": self.fake.url, "token_env": "NAPCAT_TOKEN",
                       "send_timeout_s": 1, "reconnect_delays": [0.05]},
            "allowlist": {"owner_id": OWNER, "group_ids": [GROUP]},
            "spool_dir": str(state),
            "bot": {"id": "900001"},
        }, self.tmp)
        app2 = FishQQApp(cfg)
        await app2.start()
        try:
            await asyncio.wait_for(app2.ready_event.wait(), 5)
            await asyncio.sleep(0.3)
            self.assertEqual(app2.outbox.get(queued["id"])["status"], "confirmed")
            self.assertEqual(app2.outbox.get(inflight["id"])["status"], "unknown")
            after = len([a for a in self.fake.actions if a["action"] == "send_group_msg"])
            self.assertEqual(after - before, 1)
        finally:
            await app2.stop()

    async def test_13_outbox_recovery_and_graceful_stop(self) -> None:
        outbox = Outbox(self.tmp / "recovery")
        queued = outbox.add("send_message", {"chat_id": f"group:{GROUP}"})
        inflight = outbox.add("send_message", {"chat_id": f"group:{GROUP}"})
        outbox.set_status(inflight["id"], "inflight")
        recovered = outbox.recover()
        self.assertEqual(len(recovered["queued"]), 1)
        self.assertEqual(outbox.get(inflight["id"])["status"], "unknown")
        self.assertEqual(outbox.get(queued["id"])["status"], "queued")
        start = time.monotonic()
        await self.app.stop()
        self.stopped = True
        self.assertLess(time.monotonic() - start, 5)
        self.assertFalse(self.app.lock.path.exists())
