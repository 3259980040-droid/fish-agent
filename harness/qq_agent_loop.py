"""Phase 3C：owner 私聊 reactive agent loop。

fish-qq（Transport）→ Fish Core（本进程）→ QQ DSH Runtime → canonical persona → QQ。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
import threading
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from core import HarnessConfig, RuntimeManager
from dsh_web import DSHWebClient
from qq_store import QQStore
from multimodal import MediaService
from governance import Governance, control_token


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class ChannelClient:
    def __init__(self, url: str, token: str, on_event):
        self.url = url
        self.token = token
        self.on_event = on_event
        self.ws = None
        self.epoch = None
        self.last_seq = 0
        self.bot = None
        self.futures: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._task: asyncio.Task | None = None
        self._event_task: asyncio.Task | None = None
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._event_task = asyncio.create_task(self._dispatch_events(), name="fish-qq-core-events")
        self._task = asyncio.create_task(self._run(), name="fish-qq-core-client")

    async def stop(self) -> None:
        self._stop.set()
        if self.ws is not None:
            await self.ws.close()
        tasks = [task for task in (self._task, self._event_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _dispatch_events(self) -> None:
        # Keep event order while the receiver continues resolving action receipts.
        # An event handler may itself send a message and await its receipt.
        while not self._stop.is_set():
            frame = await self._events.get()
            try:
                await self.on_event(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"fish-qq event failed: {type(exc).__name__}: {exc}")
            finally:
                self._events.task_done()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with connect(
                    self.url,
                    additional_headers={"Authorization": f"Bearer {self.token}"},
                    max_size=1024 * 1024,
                    open_timeout=5,
                ) as ws:
                    self.ws = ws
                    await ws.send(json.dumps({
                        "v": 1, "type": "hello", "id": f"core-{uuid.uuid4().hex[:8]}", "ts": "",
                        "payload": {"client": "fish-core", "last_seq": self.last_seq,
                                    "epoch": self.epoch if self.last_seq else None},
                    }))
                    hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if hello.get("type") != "hello.ok":
                        raise RuntimeError(f"hello failed: {hello}")
                    self.epoch = hello["payload"].get("epoch")
                    self.last_seq = int(hello["payload"].get("seq", 0))
                    self.bot = hello["payload"].get("bot")
                    log(f"fish-qq connected epoch={self.epoch} seq={self.last_seq}")
                    async for raw in ws:
                        try:
                            frame = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if frame.get("type") == "event":
                            self.last_seq = max(self.last_seq, int(frame.get("seq", 0)))
                            self._events.put_nowait(frame)
                        elif frame.get("type") == "action.result" and frame.get("id") in self.futures:
                            future = self.futures.pop(frame["id"])
                            if not future.done():
                                future.set_result(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log(f"fish-qq disconnected: {type(exc).__name__}: {exc}")
            finally:
                self.ws = None
                for future in self.futures.values():
                    if not future.done():
                        future.set_exception(RuntimeError("fish-qq disconnected"))
                self.futures.clear()
            if not self._stop.is_set():
                await asyncio.sleep(1)

    async def action(self, action: str, payload: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        if self.ws is None:
            raise RuntimeError("fish-qq not connected")
        self._seq += 1
        action_id = f"core-{self._seq}-{uuid.uuid4().hex[:4]}"
        future = asyncio.get_running_loop().create_future()
        self.futures[action_id] = future
        try:
            await self.ws.send(json.dumps({
                "v": 1, "type": "action", "id": action_id, "action": action,
                "ts": "", "payload": payload,
            }))
            return await asyncio.wait_for(future, timeout)
        finally:
            self.futures.pop(action_id, None)


class QQAgentLoop:
    def __init__(self, root: Path, owner: str | None = None, agent_id: str = "qq"):
        self.root = root
        self.governance = Governance(root, agent_id)
        self.owner = str(owner) if owner is not None else self.governance.owner_id()
        self.agent_id = agent_id
        self.config = HarnessConfig(root)
        self.runtime = RuntimeManager(self.config)
        self.store = QQStore(root)
        self.channel = ChannelClient(
            os.environ.get("FISH_QQ_URL", "ws://127.0.0.1:3101/ws"),
            os.environ.get("FISH_QQ_TOKEN", ""),
            self.on_event,
        )
        self.dsh: DSHWebClient | None = None
        self.session_id: str | None = None
        self.conversation: dict[str, Any] | None = None
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.cancel_event = asyncio.Event()
        self.active_turn: str | None = None
        self.current_text = ""
        self.control_server = None
        self._stop = asyncio.Event()
        self.group_ids: set[str] = set()
        self.group_mode = os.environ.get("FISH_QQ_GROUP_REPLY", "off").lower()
        try:
            cfg = json.loads((root / "channels" / "fish_qq" / "config.json").read_text(encoding="utf-8"))
            self.group_ids = {str(x) for x in (cfg.get("allowlist", {}).get("group_ids", []) or [])}
        except Exception:
            pass
        self.group_locks: dict[str, asyncio.Lock] = {}
        self.group_tasks: set[asyncio.Task] = set()
        self.control_token = control_token(root)
        self._runtime_lock = asyncio.Lock()
        self.active_test_session = None
        from group_workflow import GroupWorkflow
        self.social_workflow = GroupWorkflow(root)

    async def start(self) -> None:
        if not self.owner:
            raise ValueError("尚未配置主人账号，请在系统设置中填写")
        self.conversation = self.store.get_or_create_conversation(f"private:{self.owner}", self.agent_id)
        self.session_id = self.conversation.get("runtime_session_id")
        await self.channel.start()
        self.worker = asyncio.create_task(self._worker(), name="qq-agent-worker")
        self.control_server = await asyncio.start_server(self._control, "127.0.0.1", 3102)
        try:
            await self.ensure_runtime()
        except Exception as exc:  # noqa: BLE001
            log(f"QQ runtime unavailable at startup: {type(exc).__name__}: {exc}")
        log(f"QQ agent loop ready conversation={self.conversation['id']} session={self.session_id}")

    async def stop(self) -> None:
        self._stop.set()
        self.cancel_event.set()
        tasks = list(self.group_tasks)
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        if self.control_server is not None:
            self.control_server.close()
            await self.control_server.wait_closed()
        if self.worker is not None:
            self.worker.cancel()
            try:
                await self.worker
            except (asyncio.CancelledError, Exception):
                pass
        await self.channel.stop()
        self.runtime.stop(self.agent_id)

    async def ensure_runtime(self) -> None:
        from runtime_adapter import DSHRuntimeAdapter
        async with self._runtime_lock:
            client = await asyncio.to_thread(DSHRuntimeAdapter(self.root, self.agent_id, self.runtime).connect)
            await client.auth()
            self.dsh = client
            if self.session_id:
                try:
                    await client.baseline(self.session_id)
                except Exception:
                    log("stored session unavailable; creating a new runtime session")
                    self.session_id = None
            if not self.session_id:
                self.session_id = await client.create_session()
                self.store.set_session(self.conversation["id"], self.session_id)
            log(f"QQ runtime ready session={self.session_id}")

    async def on_event(self, frame: dict[str, Any]) -> None:
        if frame.get("event") != "message.received":
            return
        payload = frame.get("payload") or {}
        sender = str((payload.get("sender") or {}).get("id", ""))
        from owner_tests import command
        if await command(self, payload, sender):
            return
        if await self._admin_command(payload, sender):
            return
        if payload.get("chat_type") == "group":
            await self._on_group(payload)
            return
        if payload.get("chat_type") != "private":
            return
        if sender != self.owner or payload.get("chat_id") != f"private:{self.owner}":
            return
        text = "".join(
            seg.get("text", "") for seg in payload.get("segments", [])
            if seg.get("type") == "text"
        ).strip()
        if not text:
            text = "".join({"image":"[图片]", "video":"[视频]", "audio":"[语音]"}.get(s.get("type"), "")
                           for s in payload.get("segments", [])) or "[附件消息]"
        from social_context import SocialContext
        test_space = SocialContext(self.root).owner_space(sender)
        conversation = self.store.get_or_create_conversation('test:' + test_space['id'], self.agent_id) if test_space else self.conversation
        paused = self.governance.config()['paused']
        try:
            message_id = self.store.record_message(
                conversation["id"], payload["chat_id"], self.owner, "inbound",
                str(payload.get("message_id", "")), payload.get("reply_to"),
                payload.get("segments", []), text, "paused" if paused else "queued",
            )
        except Exception as exc:  # duplicate channel_message_id or DB error
            log(f"inbound duplicate/error: {type(exc).__name__}: {exc}")
            return
        if paused:
            self.store.update_message(message_id, error="主人已暂停回复，消息已跳过")
            log(f"owner inbound skipped while paused message_id={message_id}")
            return
        await self.queue.put({"message_id": message_id, "payload": payload, "text": text,
                              'space_id': test_space['id'] if test_space else '', 'conversation': conversation})
        log(f"owner inbound queued message_id={message_id} text_len={len(text)}")

    async def _on_group(self, payload: dict[str, Any]) -> None:
        await self.social_workflow.observe(self, payload)

    async def _worker(self) -> None:
        while not self._stop.is_set():
            item = await self.queue.get()
            try:
                await self._process(item)
            except Exception as exc:  # noqa: BLE001
                log(f"turn error: {type(exc).__name__}: {exc}")
            finally:
                self.queue.task_done()

    async def _process(self, item: dict[str, Any]) -> None:
        if self.governance.config()["paused"]:
            self.store.update_message(item["message_id"], status="paused", error="主人已暂停回复")
            return
        if item.get('space_id'):
            from owner_tests import process
            await process(self, item)
            return
        try:
            await self.ensure_runtime()
        except Exception as exc:  # noqa: BLE001
            self.store.update_message(item["message_id"], status="failed", error=str(exc))
            log(f"runtime unavailable for message {item['message_id']}: {exc}")
            return
        assert self.dsh is not None and self.session_id is not None
        client = self.dsh
        turn_id = f"turn-{uuid.uuid4().hex[:12]}"
        self.active_turn = turn_id
        self.current_text = item["text"]
        self.cancel_event = asyncio.Event()
        self.store.create_turn(turn_id, self.conversation["id"], self.session_id, turn_id)
        try:
            baseline = await client.baseline(self.session_id)
            prompt = await asyncio.to_thread(MediaService(self.root, "qq").prepare_content,
                                             item["payload"].get("segments", []), item["text"], cancelled=self.cancel_event)
            if self.cancel_event.is_set():
                self.store.update_message(item["message_id"], status="cancelled", cancelled=True,
                                          runtime_turn_id=turn_id)
                self.store.finish_turn(turn_id, "cancelled", error="cancelled during media processing")
                self.active_turn = None
                return
            from tool_host import bind_session
            from memory_store import MemoryStore
            await asyncio.to_thread(bind_session, self.root, 'qq', self.session_id, 'owner', self.owner)
            prompt[0]['text'] = self.governance.identity_prompt(self.owner) + MemoryStore(self.root, 'qq').context(item['text'], 'owner') + prompt[0]['text']
            self.store.update_message(item['message_id'], segments=item['payload'].get('segments', []))
            self.governance.consume("calls")
            await client.prompt(self.session_id, prompt)
            result = await client.wait_response(
                self.session_id, baseline, timeout=180, cancel_event=self.cancel_event,
            )
        except Exception as exc:  # noqa: BLE001
            status = 'cancelled' if isinstance(exc, InterruptedError) or self.cancel_event.is_set() else 'failed'
            self.store.update_message(item["message_id"], status=status, error=str(exc), runtime_turn_id=turn_id)
            self.store.finish_turn(turn_id, status, error=str(exc))
            self.active_turn = None
            return
        if result.get("cancelled"):
            self.store.update_message(item["message_id"], status="cancelled", cancelled=True,
                                      runtime_turn_id=turn_id)
            self.store.finish_turn(turn_id, "cancelled", error="cancelled")
            self.active_turn = None
            return
        if not result.get("ok"):
            self.store.update_message(item["message_id"], status="failed", error=result.get("error", ""),
                                      runtime_turn_id=turn_id)
            self.store.finish_turn(turn_id, "failed", error=result.get("error", ""))
            self.active_turn = None
            return
        response = str(result["text"]).strip()
        try:
            if self.governance.config()['paused']:
                raise PermissionError('主人已暂停，回复未发送')
            self.governance.check_output(response)
        except (PermissionError, OSError, ValueError) as exc:
            self.store.update_message(item["message_id"], status="blocked", error=str(exc), runtime_turn_id=turn_id)
            self.store.finish_turn(turn_id, "blocked", error=str(exc))
            self.active_turn = None
            return
        if self.cancel_event.is_set():
            self.store.update_message(item["message_id"], status="cancelled", cancelled=True,
                                      runtime_turn_id=turn_id)
            self.store.finish_turn(turn_id, "cancelled", error="cancelled before send")
            self.active_turn = None
            return
        response_segments = self._reply_segments(self.session_id, response)
        outbound_id = self.store.record_message(
            self.conversation["id"], f"private:{self.owner}", "bot", "outbound", "",
            None, response_segments, response, "pending",
            runtime_session_id=self.session_id, runtime_turn_id=turn_id,
        )
        turn = await self._deliver_reply(f'private:{self.owner}', response_segments, f'qq-agent:{turn_id}', self.cancel_event)
        if turn.get("ok"):
            MemoryStore(self.root, 'qq').enqueue('qq:' + str(item['message_id']), item['text'], 'owner',subject='qq:'+self.owner)
            payload = turn.get("payload") or {}
            self.store.update_message(outbound_id, status="sent",
                                      channel_message_id=str(payload.get("message_id", "")))
            self.store.update_message(item["message_id"], status="processed",
                                      runtime_session_id=self.session_id, runtime_turn_id=turn_id)
            self.store.finish_turn(turn_id, "done", assistant_message_id=str(payload.get("message_id", "")))
            log(f"owner outbound sent message_id={payload.get('message_id')} turn={turn_id}")
        else:
            error = (turn.get("error") or {}).get("code", "send_failed")
            self.store.update_message(outbound_id, status="failed", error=error)
            self.store.update_message(item["message_id"], status="processed", error=error,
                                      runtime_session_id=self.session_id, runtime_turn_id=turn_id)
            self.store.finish_turn(turn_id, "send_failed", error=error)
            log(f"outbound failed: {error}")
        self.active_turn = None

    async def cancel(self) -> dict[str, Any]:
        self.cancel_event.set()
        for task in list(self.group_tasks):
            task.cancel()
        if self.active_turn:
            result = await asyncio.to_thread(self.runtime.cancel, self.agent_id, self.active_test_session or self.session_id)
            log(f"cancel requested turn={self.active_turn} result={result.get('ok')}")
            return {"ok": result.get("ok", False), "turn": self.active_turn, "result": result}
        return {"ok": False, "error": "no active turn"}

    async def _deliver_reply(self, chat_id, segments, key, cancelled=None, validate=None):
        text = ''.join(s.get('text', '') for s in segments if s['type'] == 'text')
        self.governance.check_output(text)  # All parts must pass before the first send.
        parts = []
        while len(text) > 1800:
            cut = max(text.rfind('\n', 900, 1800), text.rfind('。', 900, 1800))
            cut = cut + 1 if cut >= 900 else 1800
            parts.append(text[:cut]); text = text[cut:]
        if text: parts.append(text)
        media = [s for s in segments if s['type'] != 'text']
        if not parts: parts = ['']
        sent = []
        for index, part in enumerate(parts):
            if validate is not None and not validate():
                return {'ok':False,'error':{'code':'群配置、人格发布或频率已改变，未继续发送','may_have_sent':bool(sent)}}
            if self.governance.config()['paused'] or (cancelled and cancelled.is_set()):
                return {'ok': False, 'error': {'code': f'已暂停或取消，已发送 {len(sent)}/{len(parts)} 段', 'may_have_sent': bool(sent)}}
            chunk = ([{'type': 'text', 'text': part}] if part else []) + (media if index == len(parts)-1 else [])
            result = await self.channel.action('send_message', {'chat_id': chat_id, 'segments': chunk,
                'idempotency_key': key if len(parts) == 1 else f'{key}:part:{index+1}'}, timeout=30)
            if not result.get('ok'):
                detail = result.get('error') or {}
                return {'ok': False, 'error': {**detail, 'code': f"第 {index+1}/{len(parts)} 段发送失败：{detail.get('code','未知')}；已发送 {len(sent)} 段，未自动重试"}}
            sent.append(str((result.get('payload') or {}).get('message_id', '')))
        return {'ok': True, 'payload': {'message_id': sent[-1], 'message_ids': sent}}

    def _reply_segments(self, session, text):
        result = [{'type': 'text', 'text': text}]
        try:
            from tool_host import request
            asset = request(self.root, '/emoji/take', {'session': session}, role='qq').get('asset')
            if asset:
                result.append({'type': 'image', 'asset_id': asset})
        except Exception:
            pass
        return result

    async def _admin_command(self, payload: dict, sender: str) -> bool:
        text = "".join(s.get("text", "") for s in payload.get("segments", []) if s.get("type") == "text").strip()
        commands = {"/状态": "status", "/暂停": "pause", "/继续": "resume", "/取消": "cancel",
                    "停": "pause", "别说了": "pause", "闭嘴": "pause", "安静": "pause", "继续": "resume", "出来吧": "resume"}
        capability = commands.get(text)
        if not capability or payload.get("chat_type") != "private" or sender != self.owner or not self.governance.is_owner(sender):
            return False
        # An explicit test owner still uses the same permission allow-list.
        if capability not in self.governance.config()["owner_permissions"]:
            self.governance.audit("denied", "主人账号未启用权限：" + capability)
            return True
        if capability in ("pause", "resume"):
            self.governance.save({"paused": capability == "pause"})
            if capability == "pause":
                while not self.queue.empty():
                    item = self.queue.get_nowait()
                    self.store.update_message(item['message_id'], status='paused', error='主人已暂停回复，消息已跳过')
                    self.queue.task_done()
                await self.cancel()
            response = "已暂停回复。发 /继续 恢复。" if capability == "pause" else "已恢复回复。"
        elif capability == "cancel":
            await self.cancel()
            response = "已取消当前任务。"
        else:
            cfg = self.governance.snapshot()
            permissions = "、".join(self.governance.owner_capabilities().values()) or "无"
            response = f"主人权限已核验。当前 Fish 管理权限：{permissions}。{'已暂停' if cfg['config']['paused'] else '可回复'}；等待任务 {self.queue.qsize()}；今日处理轮数 {cfg['usage'].get('calls', 0)}。"
        self.governance.audit("owner_command", capability)
        await self.channel.action("send_message", {"chat_id": f"private:{self.owner}",
            "segments": [{"type": "text", "text": response}], "idempotency_key": "admin:" + str(payload.get("message_id", uuid.uuid4().hex))})
        return True

    async def _control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.read(4096), 5)
            request_line = raw.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            method, path = (request_line.split(" ") + ["", ""])[:2]
            headers = {}
            for line in raw.split(b"\r\n")[1:]:
                if b":" in line:
                    key, value = line.split(b":", 1)
                    headers[key.strip().lower()] = value.strip().decode("latin-1")
            import hmac
            if not hmac.compare_digest(headers.get(b"x-fish-token", ""), self.control_token):
                body, status = b'{"ok":false,"error":"unauthorized"}', "401 Unauthorized"
            elif path == "/cancel" and method == "POST":
                body = json.dumps(await self.cancel(), ensure_ascii=False).encode()
                status = "200 OK"
            elif path == "/status":
                body = json.dumps({
                    "conversation": self.conversation["id"] if self.conversation else None,
                    "session_id": self.session_id,
                    "active_turn": self.active_turn,
                    "queue": self.queue.qsize(),
                    "bot": self.channel.bot,
                    "counts": self.store.counts(),
                }, ensure_ascii=False).encode()
                status = "200 OK"
            elif path == "/shutdown" and method == "POST":
                body = b'{"ok":true}'
                status = "200 OK"
                self._stop.set()
            else:
                body = b'{"ok":false}'
                status = "404 Not Found"
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def main_async() -> int:
    root = Path(os.environ.get("FISH_HARNESS_HOME", str(Path(__file__).resolve().parent.parent)))
    loop = QQAgentLoop(root)
    await loop.start()
    try:
        await loop._stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await loop.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ private reactive agent loop")
    parser.parse_args()
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
