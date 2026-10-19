"""Core-side 真实运输探针：连接 fish-qq，发 hello / get_status / ping / send_message。

用法（fish-qq 已运行）：
  $env:FISH_QQ_TOKEN = "..."
  python -m channels.fish_qq.real_check --status
  python -m channels.fish_qq.real_check --wait-private
  python -m channels.fish_qq.real_check --send-private "transport test"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from typing import Any

from websockets.asyncio.client import connect


class CoreProbe:
    def __init__(self, url: str, token: str, last_seq: int = 0, epoch: str | None = None):
        self.url = url
        self.token = token
        self.last_seq = last_seq
        self.epoch = epoch
        self.ws = None
        self._action_seq = 0

    async def connect(self) -> dict[str, Any]:
        self.ws = await connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            max_size=1024 * 1024,
        )
        await self.ws.send(json.dumps({
            "v": 1, "type": "hello", "id": "probe-hello", "ts": "",
            "payload": {"client": "fish-core", "last_seq": self.last_seq, "epoch": self.epoch},
        }))
        frame = json.loads(await asyncio.wait_for(self.ws.recv(), 10))
        if frame.get("type") != "hello.ok":
            raise RuntimeError(f"hello failed: {frame}")
        return frame

    async def action(self, action: str, payload: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
        assert self.ws is not None
        self._action_seq += 1
        action_id = f"probe-{self._action_seq}-{uuid.uuid4().hex[:4]}"
        await self.ws.send(json.dumps({
            "v": 1, "type": "action", "id": action_id, "action": action,
            "ts": "", "payload": payload,
        }))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = json.loads(await asyncio.wait_for(
                self.ws.recv(), max(0.1, deadline - time.monotonic()),
            ))
            if frame.get("type") == "action.result" and frame.get("id") == action_id:
                return frame
        raise TimeoutError(action)

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()


async def main_async(args: argparse.Namespace) -> int:
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"未设置 {args.token_env}", flush=True)
        return 2
    probe = CoreProbe(args.url, token, args.last_seq, args.epoch)
    hello = await probe.connect()
    print(json.dumps(hello, ensure_ascii=False, indent=2))
    try:
        if args.status:
            print(json.dumps(await probe.action("get_status", {}), ensure_ascii=False, indent=2))
            await probe.ws.send(json.dumps({"v": 1, "type": "ping", "id": "p1", "ts": ""}))
            pong = json.loads(await asyncio.wait_for(probe.ws.recv(), 5))
            print(json.dumps(pong, ensure_ascii=False, indent=2))
            return 0
        if args.send_private:
            result = await probe.action("send_message", {
                "chat_id": f"private:{args.owner}",
                "segments": _segments(args.send_private, args.image),
                "reply_to": args.reply_to or None,
                "idempotency_key": args.idempotency_key or f"real-private-{uuid.uuid4().hex[:8]}",
            })
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1
        if args.send_group:
            result = await probe.action("send_message", {
                "chat_id": f"group:{args.group}",
                "segments": _segments(args.send_group, args.image),
                "reply_to": args.reply_to or None,
                "idempotency_key": args.idempotency_key or f"real-group-{uuid.uuid4().hex[:8]}",
            })
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1
        if args.history:
            result = await probe.action("get_history", {
                "chat_id": f"private:{args.owner}", "limit": 30,
            })
            if not result.get("ok"):
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
            messages = result["payload"]["messages"]
            print(json.dumps({
                "ok": True, "count": len(messages),
                "first": messages[0]["message_id"] if messages else None,
                "last": messages[-1]["message_id"] if messages else None,
            }, ensure_ascii=False, indent=2))
            return 0
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            try:
                frame = json.loads(await asyncio.wait_for(
                    probe.ws.recv(), max(0.1, deadline - time.monotonic()),
                ))
            except (asyncio.TimeoutError, TimeoutError):
                break
            print(json.dumps(frame, ensure_ascii=False, indent=2))
            if args.wait_private and frame.get("type") == "event" \
                    and frame.get("event") == "message.received" \
                    and frame.get("payload", {}).get("chat_type") == "private" \
                    and frame.get("payload", {}).get("sender", {}).get("id") == args.owner:
                return 0
        return 0 if not args.wait_private else 1
    finally:
        await probe.close()


def _segments(text: str, image: str) -> list[dict[str, str]]:
    segments = []
    if text:
        segments.append({"type": "text", "text": text})
    if image:
        segments.append({"type": "image", "url": image})
    return segments


def main() -> int:
    parser = argparse.ArgumentParser(description="fish-qq Core-side 真实运输探针")
    parser.add_argument("--url", default="ws://127.0.0.1:3101/ws")
    parser.add_argument("--token-env", default="FISH_QQ_TOKEN")
    parser.add_argument("--last-seq", type=int, default=0)
    parser.add_argument("--epoch", default=None)
    parser.add_argument("--owner", default=os.environ.get("FISH_OWNER_QQ", ""))
    parser.add_argument("--group", default="")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--wait-private", action="store_true")
    parser.add_argument("--send-private", default="")
    parser.add_argument("--send-group", default="")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--image", default="")
    parser.add_argument("--reply-to", default="")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
