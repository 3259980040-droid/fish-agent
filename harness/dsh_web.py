"""最小 DSH web 客户端：只做 session/create / prompt / page / follow。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from websockets.asyncio.client import connect


def safe_runtime_error(value: str) -> str:
    """Keep actionable error details without exposing request credentials."""
    text = str(value)
    for name, secret in os.environ.items():
        if any(word in name.upper() for word in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD')) and len(secret) >= 6:
            text = text.replace(secret, '[已隐藏]')
    def public_url(match):
        try:
            url = urlsplit(match.group())
            return urlunsplit((url.scheme, url.netloc.rsplit('@', 1)[-1], url.path, '', ''))
        except ValueError:
            return '[服务地址已隐藏]'
    text = re.sub(r'https?://[^\s<>"\']+', public_url, text)
    text = re.sub(r'(?i)\bBearer\s+[^\s,;"\']+', 'Bearer [已隐藏]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[已隐藏]', text)
    text = re.sub(r'(?i)(["\']?(?:api[_-]?key|token|secret|password|authorization)["\']?\s*[:=]\s*)["\']?[^\s,;"\'}]+', r'\1[已隐藏]', text)
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', text)
    return text.strip()[:600]


class DSHWebClient:
    def __init__(self, launch_url: str):
        self.launch_url = launch_url
        self.session = requests.Session()
        self.session.trust_env = False
        self.origin = launch_url.split("/?")[0]
        self.cookie = ""
        self._baseline_seq: dict[str, int] = {}
        self.ws_url = self.origin.replace("http://", "ws://").replace("https://", "wss://") + "/api/remote.mux"

    async def auth(self) -> None:
        def do_auth():
            response = self.session.get(self.launch_url, allow_redirects=False, timeout=10)
            return response.status_code, self.session.cookies.get_dict()

        status, cookies = await asyncio.to_thread(do_auth)
        if status != 303:
            raise RuntimeError(f"DSH auth failed: HTTP {status}")
        self.cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())

    async def _rpc(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        body = {
            "type": "client-request",
            "rpcId": f"fish-{method}-{uuid.uuid4().hex[:8]}",
            "method": method,
            "payload": {"args": args},
        }

        def post():
            return self.session.post(f"{self.origin}/api/{method}", json=body, timeout=30)

        response = await asyncio.to_thread(post)
        response.raise_for_status()
        data = response.json()
        result = data.get("result") or {}
        if not result.get("ok"):
            raise RuntimeError(f"{method}: {result.get('error')}")
        return result.get("value", {})

    async def create_session(self, *, cwd: str = '') -> str:
        # Public SessionCreateRequest owns an optional cwd. Tests must start in
        # their own directory rather than inheriting the role's working files.
        value = await self._rpc("session/create", {"request": {"cwd": cwd} if cwd else {}})
        return str(value["sessionId"])

    async def prompt(self, session_id: str, text: str | list[dict[str, Any]]) -> str:
        request_id = str(uuid.uuid4())
        await self._rpc("session/prompt", {"request": {
            "requestId": request_id,
            "sessionId": session_id,
            "mode": "queue",
            "content": text if isinstance(text, list) else [{"type": "text", "text": text}],
        }})
        return request_id

    async def _cursor(self, session_id: str) -> int:
        stream_id = f"fish-follow-{uuid.uuid4().hex[:8]}"
        async with connect(self.ws_url, additional_headers={"Cookie": self.cookie}, open_timeout=10, proxy=None) as ws:
            await ws.send(json.dumps({
                "type": "open",
                "streamId": stream_id,
                "endpoint": "session/follow",
                "payload": {"args": {"request": {"address": {"kind": "session", "sessionId": session_id}}}},
            }))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), max(0.1, deadline - time.monotonic()))
                frame = json.loads(raw)
                if frame.get("streamId") != stream_id:
                    continue
                value = frame.get("value")
                if frame.get("type") == "item" and isinstance(value, dict) \
                        and value.get("type") == "snapshot" and isinstance(value.get("cursor"), int):
                    return value["cursor"]
        raise TimeoutError("DSH session follow cursor timeout")

    async def _page(self, session_id: str, through_seq: int) -> dict[str, Any]:
        return await self._rpc("session/page", {"request": {
            "address": {"kind": "session", "sessionId": session_id},
            "throughSeq": through_seq,
            "maxMessages": 100,
        }})

    @staticmethod
    def _assistant_texts(page: dict[str, Any]) -> list[str]:
        texts = []
        for record in page.get("records", []):
            if record.get("type") != "event":
                continue
            event = record.get("event") or {}
            if event.get("type") != "assistant/message":
                continue
            message = (event.get("data") or {}).get("message") or {}
            parts = []
            for block in message.get("content", []) or []:
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            text = "\n".join(parts).strip()
            if text:
                texts.append(text)
        return texts

    async def baseline(self, session_id: str) -> int:
        cursor = await self._cursor(session_id)
        self._baseline_seq[session_id] = cursor
        page = await self._page(session_id, cursor)
        return len(self._assistant_texts(page))

    async def wait_response(
        self, session_id: str, baseline: int, timeout: float,
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_count = baseline
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return {"ok": False, "cancelled": True, "error": "cancelled"}
            try:
                cursor = await self._cursor(session_id)
                page = await self._page(session_id, cursor)
            except Exception as exc:  # noqa: BLE001
                await asyncio.sleep(1)
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            texts = self._assistant_texts(page)
            start = self._baseline_seq.get(session_id, 0)
            recent = [record for record in page.get("records", [])
                      if record.get("type") == "event" and
                      int((record.get("event") or {}).get("seq", 0)) > start]
            ended = next(((record.get("event") or {}) for record in reversed(recent)
                          if (record.get("event") or {}).get("type") == "turn/end"), None)
            if ended:
                detail = (ended.get("data") or {}).get("reason") or {}
                reason = detail.get("kind")
                if reason and reason != "completed":
                    labels = {'error': '回复失败', 'cancelled': '已取消', 'interrupted': '回复已中断', 'user': '已停止'}
                    error = detail.get('error') or {}
                    message = error.get('message', '') if isinstance(error, dict) else error if isinstance(error, str) else ''
                    message = safe_runtime_error(message)
                    label = labels.get(reason, '回复未完成')
                    return {"ok": False, "cancelled": reason in ("cancelled", "interrupted", "user"),
                            "error": label + ('：' + message if message else '；请查看诊断记录后重试')}
                final = self._assistant_texts({"records": recent})
                if final:
                    return {"ok": True, "text": final[-1]}
            last_count = len(texts)
            await asyncio.sleep(1)
        return {"ok": False, "error": f"timeout (assistant_count={last_count})"}
