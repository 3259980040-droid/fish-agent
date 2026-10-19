"""OneBot 11 正向 WebSocket 客户端：只暴露需要的 action。"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect


class OneBotUnavailable(RuntimeError):
    pass


class OneBotTimeout(RuntimeError):
    pass


class OneBotError(RuntimeError):
    pass


class OneBotClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        send_timeout_s: float = 15.0,
        reconnect_delays: list[float] | None = None,
        heartbeat_s: float = 30.0,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        on_state: Callable[[str, str, bool, int], Any] | None = None,
    ):
        self.url = url
        self.token = token
        self.send_timeout_s = send_timeout_s
        self.reconnect_delays = reconnect_delays or [1, 2, 4, 8, 16, 30, 60]
        self.heartbeat_s = heartbeat_s
        self.on_event = on_event
        self.on_state = on_state
        self.ws = None
        self.ready = asyncio.Event()
        self.bot: dict[str, str] = {"id": "unknown", "display_name": ""}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._futures: dict[str, asyncio.Future] = {}
        self._echo = 0
        self._ever_connected = False
        self._disconnected_at = 0.0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="fish-qq-onebot")

    async def stop(self) -> None:
        self._stop.set()
        if self.ws is not None:
            try:
                await self.ws.close(code=1000)
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.ready.clear()

    async def _emit_state(self, state: str, reason: str, reconnected: bool = False, gap_ms: int = 0) -> None:
        if self.on_state is not None:
            result = self.on_state(state, reason, reconnected, gap_ms)
            if asyncio.iscoroutine(result):
                await result

    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            await self._emit_state(
                "connecting",
                "napcat_reconnecting" if self._ever_connected else "napcat_connecting",
            )
            try:
                async with connect(
                    self.url,
                    additional_headers={"Authorization": f"Bearer {self.token}"} if self.token else None,
                    open_timeout=5,
                    proxy=None if __import__('urllib.parse', fromlist=['urlparse']).urlparse(self.url).hostname in ('localhost', '127.0.0.1', '::1') else True,
                    max_size=1024 * 1024,
                ) as ws:
                    self.ws = ws
                    self.ready.set()
                    reader = asyncio.create_task(self._read_loop(ws))
                    attempt = 0
                    try:
                        info = await self.send_action("get_login_info", {}, timeout=5)
                        data = info.get("data") if isinstance(info, dict) else {}
                        if isinstance(data, dict):
                            self.bot = {
                                "id": str(data.get("user_id", "unknown")),
                                "display_name": str(data.get("nickname", "")),
                            }
                    except Exception:
                        pass
                    gap_ms = int((time.time() - self._disconnected_at) * 1000) if self._disconnected_at else 0
                    await self._emit_state("ready", "napcat_connected", self._ever_connected, gap_ms)
                    self._ever_connected = True
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    try:
                        await reader
                    finally:
                        heartbeat.cancel()
                        try:
                            await heartbeat
                        except (asyncio.CancelledError, Exception):
                            pass
                        reader.cancel()
                        try:
                            await reader
                        except (asyncio.CancelledError, Exception):
                            pass
                    if self.ws is ws and not self._stop.is_set():
                        self.ws = None
                        self.ready.clear()
                        self._disconnected_at = time.time()
                        self._fail_pending()
                        await self._emit_state("degraded", "napcat_disconnected")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ws = None
                self.ready.clear()
                self._disconnected_at = time.time()
                self._fail_pending()
                await self._emit_state("degraded", "napcat_disconnected")
            if self._stop.is_set():
                break
            delay = self.reconnect_delays[min(attempt, len(self.reconnect_delays) - 1)]
            attempt += 1
            await asyncio.sleep(delay * (1 + random.random() * 0.2))

    async def _read_loop(self, ws) -> None:
        async for message in ws:
            self._handle_message(message)

    async def _heartbeat(self, ws) -> None:
        while self.ws is ws and not self._stop.is_set():
            await asyncio.sleep(self.heartbeat_s)
            if self.ws is not ws:
                return
            try:
                await self.send_action("get_status", {}, timeout=5)
            except asyncio.CancelledError:
                raise
            except Exception:
                try:
                    await ws.close()
                except Exception:
                    pass
                return

    def _handle_message(self, message: str | bytes) -> None:
        try:
            data = json.loads(message)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        echo = data.get("echo")
        if echo is not None and str(echo) in self._futures:
            future = self._futures.pop(str(echo))
            if not future.done():
                future.set_result(data)
            return
        if data.get("post_type") and self.on_event is not None:
            result = self.on_event(data)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)

    def _fail_pending(self) -> None:
        for future in self._futures.values():
            if not future.done():
                future.set_exception(OneBotUnavailable("NapCat 连接已断开"))
        self._futures.clear()

    async def send_action(self, action: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if self.ws is None:
            raise OneBotUnavailable("NapCat 未连接")
        self._echo += 1
        echo = f"fish-{self._echo}"
        future = asyncio.get_running_loop().create_future()
        self._futures[echo] = future
        frame = {"action": action, "params": params, "echo": echo}
        await self.ws.send(json.dumps(frame, ensure_ascii=False))
        try:
            result = await asyncio.wait_for(future, timeout=timeout or self.send_timeout_s)
        except asyncio.TimeoutError:
            self._futures.pop(echo, None)
            raise OneBotTimeout(f"{action} timeout")
        if result.get("status") == "failed" or result.get("retcode", 0) != 0:
            raise OneBotError(f"{action} failed: {result.get('message') or result.get('wording') or result.get('retcode')}")
        return result

    async def get_history(self, chat_type: str, chat_id: str, limit: int = 30) -> list[dict[str, Any]]:
        target = chat_id.split(":", 1)[1]
        if chat_type == "group":
            result = await self.send_action("get_group_msg_history", {"group_id": int(target), "count": limit})
        else:
            result = await self.send_action("get_friend_msg_history", {"user_id": int(target), "count": limit})
        data = result.get("data") or {}
        return list(data.get("messages") or [])
