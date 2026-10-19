"""fish-qq 主进程：Core WS Server + NapCat 客户端 + 规范化/策略/outbox。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .config import FishQQConfig
from .normalize import NormalizeError, normalize_message, normalize_outbound, to_onebot_segments
from .onebot import OneBotClient, OneBotError, OneBotTimeout, OneBotUnavailable
from .policy import Allowlist, Counters, Dedup, OutboundLimiter, RingBuffer
from .spool import Lock, Outbox
from harness.governance import Governance


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class FishQQApp:
    def __init__(self, config: FishQQConfig):
        self.config = config
        self.governance = Governance(config.root, "qq")
        self.epoch = uuid.uuid4().hex
        self.seq = 0
        self.state = "starting"
        self.state_reason = "starting"
        self.state_since = now_iso()
        self.port = config.listen_port
        self.server = None
        self.onebot: OneBotClient | None = None
        self.clients: set[ServerConnection] = set()
        self.allowlist = Allowlist(config.owner_id, config.group_ids, config.private_mode)
        self.dedup = Dedup(config.dedup_window_s)
        self.limiter = OutboundLimiter(config.outbound_per_min, config.outbound_global_per_min)
        self.ring = RingBuffer(500, 300)
        self.counters = Counters()
        self.lock = Lock(config.state_dir / "fish-qq.lock")
        self.outbox = Outbox(config.state_dir)
        self.idempotency: dict[str, tuple[float, dict[str, Any]]] = {}
        self.bot_message_ids: set[str] = set()
        self.ready_event = asyncio.Event()
        self.stopped_event = asyncio.Event()
        self.recovered_queued: list[dict[str, Any]] = []
        self._retry_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.config.validate()
        self.lock.acquire()
        recovered = self.outbox.recover()
        self.recovered_queued = recovered["queued"]
        self.onebot = OneBotClient(
            self.config.napcat_url,
            self.config.napcat_token,
            send_timeout_s=self.config.send_timeout_s,
            reconnect_delays=self.config.reconnect_delays,
            heartbeat_s=self.config.heartbeat_s,
            on_event=self._on_onebot_event,
            on_state=self._on_onebot_state,
        )
        self.server = await serve(
            self._core_handler,
            self.config.listen_host,
            self.config.listen_port,
            max_size=self.config.max_frame_bytes,
        )
        sockets = getattr(self.server, "sockets", None)
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        await self.onebot.start()
        self._retry_task = asyncio.create_task(self._retry_queued())

    async def stop(self) -> None:
        if self.state == "stopped":
            return
        self.state = "stopping"
        self.state_reason = "stopping"
        self.state_since = now_iso()
        await self._emit("channel.state", {
            "state": "stopping", "reason": "stopping", "since": self.state_since,
            "napcat": {"connected": self.onebot is not None and self.onebot.ws is not None,
                       "url": self.config.napcat_url},
        })
        if self._retry_task is not None:
            self._retry_task.cancel()
        if self.onebot is not None:
            await self.onebot.stop()
        for ws in list(self.clients):
            try:
                await ws.close(code=1001)
            except Exception:
                pass
        self.clients.clear()
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
        self.lock.release()
        self.state = "stopped"
        self.stopped_event.set()

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        frame = {
            "v": 1, "type": "event", "event": event, "seq": self.seq,
            "epoch": self.epoch, "ts": now_iso(), "payload": payload,
        }
        self.ring.append(self.epoch, self.seq, frame)
        if self.clients:
            await asyncio.gather(*(self._safe_send(ws, frame) for ws in list(self.clients)),
                                 return_exceptions=True)

    async def _safe_send(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        try:
            await ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception:
            self.clients.discard(ws)

    async def _on_onebot_state(self, state: str, reason: str, reconnected: bool = False, gap_ms: int = 0) -> None:
        self.state = state
        self.state_reason = reason
        self.state_since = now_iso()
        if state == "ready":
            self.ready_event.set()
            if self.onebot is not None and self.onebot.bot.get("id") not in ("", "unknown"):
                self.config.bot_id = self.onebot.bot["id"]
                self.config.display_name = self.onebot.bot.get("display_name") or self.config.display_name
            if reconnected:
                self.counters.reconnects += 1
        elif state in ("degraded", "connecting"):
            self.ready_event.clear()
        payload = {
            "state": state,
            "reason": reason,
            "since": self.state_since,
            "napcat": {
                "connected": self.onebot is not None and self.onebot.ws is not None,
                "url": self.config.napcat_url,
            },
            "reconnected": reconnected,
        }
        if gap_ms:
            payload["gap_ms"] = gap_ms
        await self._emit("channel.state", payload)

    async def _on_onebot_event(self, raw: dict[str, Any]) -> None:
        event = normalize_message(raw, self.config.bot_id, self.bot_message_ids)
        if event is None:
            return
        if not self.allowlist.is_allowed(event["chat_type"], event["chat_id"], event["sender"]["id"]):
            self.counters.dropped_allowlist += 1
            return
        if self.dedup.seen(event["message_id"]):
            self.counters.dropped_dup += 1
            return
        self.counters.received += 1
        self.counters.unknown_segments += sum(1 for seg in event["segments"] if seg["type"] == "unknown")
        await self._emit("message.received", event)

    async def _core_handler(self, ws: ServerConnection) -> None:
        auth = ""
        try:
            auth = ws.request.headers.get("Authorization", "")
        except Exception:
            auth = ""
        if auth != f"Bearer {self.config.core_token}":
            await self._safe_send(ws, {
                "v": 1, "type": "error", "ts": now_iso(),
                "error": {"code": "unauthorized", "message": "bad token"},
            })
            await ws.close(code=4401)
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
        except asyncio.TimeoutError:
            await ws.close(code=4408)
            return
        except ConnectionClosed:
            return
        try:
            hello = json.loads(raw)
        except (TypeError, ValueError):
            await self._send_error_and_close(ws, "bad_request", 1008)
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello" or hello.get("v") != 1:
            await self._send_error_and_close(ws, "bad_request", 1008)
            return
        payload = hello.get("payload") if isinstance(hello.get("payload"), dict) else {}
        await self._safe_send(ws, {
            "v": 1, "type": "hello.ok", "id": hello.get("id"), "ts": now_iso(),
            "payload": {
                "channel": "qq",
                "state": self.state,
                "bot": {"id": self.config.bot_id, "display_name": self.config.display_name},
                "epoch": self.epoch,
                "seq": self.seq,
                "capabilities": ["send_text", "send_image", "reply", "history", "native_face_probe"],
                "limits": {
                    "max_text_chars": self.config.max_text_chars,
                    "max_segments": self.config.max_segments,
                    "max_frame_bytes": self.config.max_frame_bytes,
                },
            },
        })
        self.clients.add(ws)

        last_seq = payload.get("last_seq")
        hello_epoch = payload.get("epoch")
        if hello_epoch and isinstance(last_seq, int) and last_seq > 0:
            events, gap = self.ring.replay(str(hello_epoch or ""), last_seq)
            if gap:
                await self._emit("channel.state", {
                    "state": self.state, "reason": "replay_gap", "since": self.state_since,
                    "napcat": {"connected": self.onebot is not None and self.onebot.ws is not None,
                               "url": self.config.napcat_url},
                    "reconnected": False, "gap": True,
                })
            for event in events:
                await self._safe_send(ws, event)

        try:
            async for raw in ws:
                await self._handle_core_frame(ws, raw)
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)

    async def _send_error_and_close(self, ws: ServerConnection, code: str, close_code: int) -> None:
        await self._safe_send(ws, {
            "v": 1, "type": "error", "ts": now_iso(),
            "error": {"code": code, "message": code},
        })
        await ws.close(code=close_code)

    async def _handle_core_frame(self, ws: ServerConnection, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            await self._send_error_and_close(ws, "bad_request", 1008)
            return
        if not isinstance(frame, dict) or frame.get("v") != 1:
            await self._send_result(ws, frame.get("id") if isinstance(frame, dict) else None,
                                    False, error={"code": "bad_request", "message": "bad frame"})
            return
        if frame.get("type") == "ping":
            await self._safe_send(ws, {"v": 1, "type": "pong", "id": frame.get("id"), "ts": now_iso()})
            return
        if frame.get("type") != "action":
            await self._send_result(ws, frame.get("id"), False, error={"code": "bad_request", "message": "not action"})
            return
        action = str(frame.get("action", ""))
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        if action == "send_message":
            result = await self._send_message(payload)
        elif action == "get_status":
            result = {"ok": True, "payload": self._status_payload()}
        elif action == "get_history":
            result = await self._get_history(payload)
        elif action == 'get_group_members':
            result = await self._get_group_members(payload)
        elif action == "probe_capabilities":
            result = {"ok": True, "payload": {"native_face": {
                "fetch": None, "detail": None, "add": None, "delete": None,
                "set_desc": None, "send": None,
            }}}
        else:
            result = {"ok": False, "error": {"code": "bad_request", "message": f"unknown action {action}"}}
        await self._send_result(ws, frame.get("id"), result["ok"],
                                payload=result.get("payload"), error=result.get("error"))

    async def _send_result(
        self, ws: ServerConnection, frame_id: Any, ok: bool,
        payload: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
    ) -> None:
        frame: dict[str, Any] = {
            "v": 1, "type": "action.result", "id": frame_id, "ok": ok, "ts": now_iso(),
        }
        if payload is not None:
            frame["payload"] = payload
        if error is not None:
            frame["error"] = error
        await self._safe_send(ws, frame)

    def _status_payload(self) -> dict[str, Any]:
        queue = len([r for r in self.outbox.all() if r.get("status") in ("queued", "inflight")])
        return {
            "state": self.state,
            "bot": self.onebot.bot if self.onebot else {"id": "unknown", "display_name": ""},
            "napcat": {"connected": self.onebot is not None and self.onebot.ws is not None,
                       "url": self.config.napcat_url},
            "epoch": self.epoch,
            "seq": self.seq,
            "uptime_s": int(time.time() - self.counters.started_at),
            "version": "0.1.0",
            "queue": {"outbox": queue},
            "counters": {"received": self.counters.received, "sent": self.counters.sent,
                         "failed": self.counters.failed},
        }

    async def _send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.state != "ready":
            return {"ok": False, "error": {
                "code": "channel_unavailable", "message": "NapCat not ready", "may_have_sent": False,
            }}
        try:
            segments = normalize_outbound(payload.get("segments"), self.config.max_text_chars,
                                          self.config.max_segments)
        except NormalizeError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": exc.code, "may_have_sent": False}}
        chat_id = str(payload.get("chat_id", ""))
        if not (chat_id.startswith("group:") or chat_id.startswith("private:")):
            return {"ok": False, "error": {"code": "bad_request", "message": "bad chat_id"}}
        allowed, retry_after = self.limiter.check(chat_id)
        if not allowed:
            return {"ok": False, "error": {
                "code": "rate_limited", "message": "outbound technical limit",
                "retry_after_ms": retry_after, "may_have_sent": False,
            }}
        key = payload.get("idempotency_key")
        if key:
            cached = self.idempotency.get(str(key))
            if cached and cached[0] > time.time():
                return {"ok": True, "payload": cached[1]}
        try:
            self._check_delivery(chat_id, segments)
            self.governance.consume("replies")
        except (PermissionError, OSError, ValueError) as exc:
            return {"ok": False, "error": {"code": "policy_blocked", "message": str(exc), "may_have_sent": False}}
        self.limiter.hit(chat_id)
        record = self.outbox.add("send_message", payload, ttl_s=600)
        self.outbox.set_status(record["id"], "inflight")
        chat_type, target = chat_id.split(":", 1)
        try:
            segments = self._resolve_images(segments, chat_id)
        except (ValueError, PermissionError):
            self.outbox.set_status(record['id'], 'failed')
            return {'ok': False, 'error': {'code': 'emoji_unavailable', 'may_have_sent': False}}
        message = to_onebot_segments(segments)
        if payload.get("reply_to"):
            message = [{"type": "reply", "data": {"id": str(payload["reply_to"])}}] + message
        if chat_type == "group":
            action = "send_group_msg"
            params = {"group_id": int(target), "message": message}
        else:
            action = "send_private_msg"
            params = {"user_id": int(target), "message": message}
        try:
            assert self.onebot is not None
            result = await self.onebot.send_action(action, params, timeout=self.config.send_timeout_s)
        except OneBotTimeout as exc:
            self.outbox.set_status(record["id"], "unknown")
            self.counters.failed += 1
            return {"ok": False, "error": {
                "code": "timeout", "message": str(exc), "may_have_sent": True,
            }}
        except OneBotUnavailable as exc:
            self.outbox.set_status(record["id"], "unknown")
            self.counters.failed += 1
            return {"ok": False, "error": {
                "code": "channel_unavailable", "message": str(exc), "may_have_sent": False,
            }}
        except OneBotError as exc:
            self.outbox.set_status(record["id"], "unknown")
            self.counters.failed += 1
            return {"ok": False, "error": {
                "code": "send_failed", "message": str(exc), "may_have_sent": False,
            }}
        data = result.get("data") if isinstance(result, dict) else {}
        message_id = str((data or {}).get("message_id", ""))
        self.outbox.set_status(record["id"], "confirmed")
        if message_id:
            self.bot_message_ids.add(message_id)
            if len(self.bot_message_ids) > 1000:
                self.bot_message_ids = set(list(self.bot_message_ids)[-500:])
        result_payload = {"chat_id": chat_id, "message_id": message_id, "sent_at": now_iso()}
        if key:
            self.idempotency[str(key)] = (time.time() + 600, result_payload)
        self.counters.sent += 1
        return {"ok": True, "payload": result_payload}

    def _resolve_images(self, segments, chat_id):
        from harness.emoji_store import EmojiStore
        result = []
        for segment in segments:
            if segment.get('asset_id'):
                try:
                    asset = EmojiStore(self.governance.root).reserve_send(segment['asset_id'], chat_id)
                    result.append({'type': 'image', 'url': asset['file']})
                except (ValueError, PermissionError):
                    self.governance.audit('emoji_skipped', '表情不可用或仍在冷却；保留已通过检查的文字回复')
            else:
                result.append(segment)
        if not result:
            raise ValueError('没有可发送的内容')
        return result

    def _check_delivery(self, chat_id: str, segments: list[dict]) -> None:
        kind, target = chat_id.split(":", 1)
        if kind == "private" and target != (self.governance.owner_id() or self.config.owner_id):
            raise PermissionError("不在允许发送的主人私聊范围")
        if kind == "group" and target not in self.allowlist.group_ids:
            raise PermissionError("群不在允许发送范围")
        self.governance.check_output("\n".join(str(s.get("text", "")) for s in segments))

    async def _get_group_members(self, payload: dict[str, Any]) -> dict[str, Any]:
        group = str(payload.get('group_id', ''))
        if not group.isascii() or not group.isdigit() or not 5 <= len(group) <= 15:
            return {'ok': False, 'error': {'code': 'bad_request', 'message': '无效群号'}}
        if group not in self.allowlist.group_ids:
            return {'ok': False, 'error': {'code': 'forbidden', 'message': '群不在白名单'}}
        if self.state != 'ready' or self.onebot is None:
            return {'ok': False, 'error': {'code': 'channel_unavailable', 'message': '请先连接 NapCat'}}
        try:
            result = await self.onebot.send_action('get_group_member_list', {'group_id': int(group), 'no_cache': True})
            raw = result.get('data')
            if not isinstance(raw, list) or not 1 <= len(raw) <= 10000:
                raise ValueError('成员列表不完整，保留原档案')
            members = [{'user_id': str(m['user_id']), 'nickname': str(m.get('nickname') or '')[:200],
                        'card': str(m.get('card') or '')[:200]} for m in raw]
            return {'ok': True, 'payload': {'group_id': group, 'members': members}}
        except (OneBotUnavailable, OneBotError, OneBotTimeout, ValueError, KeyError, TypeError):
            return {'ok': False, 'error': {'code': 'member_sync_failed', 'message': '群成员读取失败，原档案保留；请检查 NapCat 连接'}}

    async def _get_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        chat_id = str(payload.get("chat_id", ""))
        if not (chat_id.startswith("group:") or chat_id.startswith("private:")):
            return {"ok": False, "error": {"code": "bad_request", "message": "bad chat_id"}}
        limit = int(payload.get("limit", 30) or 30)
        chat_type = chat_id.split(":", 1)[0]
        try:
            assert self.onebot is not None
            raw_messages = await self.onebot.get_history(chat_type, chat_id, min(limit, 30))
        except (OneBotUnavailable, OneBotError, OneBotTimeout) as exc:
            return {"ok": False, "error": {"code": "channel_unavailable", "message": str(exc)}}
        messages = []
        for raw in raw_messages:
            event = normalize_message(raw, self.config.bot_id, self.bot_message_ids)
            if event is not None:
                messages.append(event)
        return {"ok": True, "payload": {"messages": messages[-limit:]}}

    async def _retry_queued(self) -> None:
        await self.ready_event.wait()
        for record in self.recovered_queued:
            payload = record.get("payload") or {}
            try:
                chat_id = str(payload.get("chat_id", ""))
                segments = normalize_outbound(payload.get("segments"), self.config.max_text_chars,
                                              self.config.max_segments)
                self._check_delivery(chat_id, segments)
                self.governance.consume("replies")
                chat_type, target = chat_id.split(":", 1)
                message = to_onebot_segments(self._resolve_images(segments, chat_id))
                if payload.get("reply_to"):
                    message = [{"type": "reply", "data": {"id": str(payload["reply_to"])}}] + message
                if chat_type == "group":
                    action, params = "send_group_msg", {"group_id": int(target), "message": message}
                else:
                    action, params = "send_private_msg", {"user_id": int(target), "message": message}
                self.outbox.set_status(record["id"], "inflight")
                await self.onebot.send_action(action, params, timeout=self.config.send_timeout_s)
                self.outbox.set_status(record["id"], "confirmed")
            except Exception:
                self.outbox.set_status(record["id"], "unknown")
