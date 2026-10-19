"""Fake NapCat OneBot 11 forward-WS server，用于离线契约测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.asyncio.server import ServerConnection, serve


class FakeNapCat:
    def __init__(self, token: str = "", host: str = "127.0.0.1", port: int = 0):
        self.token = token
        self.host = host
        self.port = port
        self.url = ""
        self.server = None
        self.clients: list[ServerConnection] = []
        self.actions: list[dict[str, Any]] = []
        self.connected = asyncio.Event()
        self.drop_send = False
        self.fail_send = False
        self._message_seq = 1000

    async def start(self) -> None:
        self.server = await serve(self._handler, self.host, self.port, max_size=1024 * 1024)
        self.port = int(self.server.sockets[0].getsockname()[1])
        self.url = f"ws://{self.host}:{self.port}"

    async def stop(self) -> None:
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception:
                pass
        self.clients.clear()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handler(self, ws: ServerConnection) -> None:
        self.clients.append(ws)
        self.connected.set()
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                action = frame.get("action")
                if not action:
                    continue
                self.actions.append(frame)
                echo = frame.get("echo")
                if action == "get_login_info":
                    await ws.send(json.dumps({
                        "status": "ok", "retcode": 0, "echo": echo,
                        "data": {"user_id": 900001, "nickname": "大肥鱼"},
                    }))
                elif action in ("send_group_msg", "send_private_msg"):
                    if self.drop_send:
                        continue
                    if self.fail_send:
                        await ws.send(json.dumps({
                            "status": "failed", "retcode": 100, "echo": echo,
                            "message": "fake fail",
                        }))
                        continue
                    self._message_seq += 1
                    await ws.send(json.dumps({
                        "status": "ok", "retcode": 0, "echo": echo,
                        "data": {"message_id": self._message_seq},
                    }))
                elif action == 'get_group_member_list':
                    await ws.send(json.dumps({'status':'ok','retcode':0,'echo':echo,'data':[
                        {'user_id':200000001,'nickname':'Owner','card':'Owner card','age':25,'sex':'unknown'},
                        {'user_id':123456789,'nickname':'Member','card':'','unrelated_secret':'omit'}]}))
                elif action in ("get_group_msg_history", "get_friend_msg_history"):
                    target = frame.get("params", {}).get("group_id") or frame.get("params", {}).get("user_id")
                    messages = [
                        {
                            "post_type": "message",
                            "message_type": "group" if action.startswith("get_group") else "private",
                            "group_id": target if action.startswith("get_group") else None,
                            "user_id": 200000001,
                            "message_id": 5000 + i,
                            "sender": {"user_id": 200000001, "nickname": "主人"},
                            "message": [{"type": "text", "data": {"text": f"history-{i}"}}],
                        }
                        for i in range(30)
                    ]
                    await ws.send(json.dumps({
                        "status": "ok", "retcode": 0, "echo": echo,
                        "data": {"messages": messages},
                    }))
                else:
                    await ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": echo, "data": {}}))
        except Exception:
            pass
        finally:
            if ws in self.clients:
                self.clients.remove(ws)

    async def push_event(self, event: dict[str, Any]) -> None:
        if not self.clients:
            raise RuntimeError("FakeNapCat 没有连接")
        await self.clients[-1].send(json.dumps(event, ensure_ascii=False))

    async def close_client(self) -> None:
        if self.clients:
            await self.clients[-1].close()
