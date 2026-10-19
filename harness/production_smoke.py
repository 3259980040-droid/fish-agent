"""Production-path smoke：走 Fish 当前真实的 web Runtime，而不是 headless。

流程：
1. 用 RuntimeManager 启动 research 的 web Runtime（3080）。
2. 通过 dsh web 的 /api/session/create + /api/session/prompt 发真实请求。
3. 等 assistant 文本。
4. 新建临时 Session，发长生成，按 session_id 调 /fish/cancel。
5. 确认进程仍 running；同一 Session 再发一条短请求，确认还能响应。

需要：FISH_RESEARCH_DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import requests
from websockets.sync.client import connect

from core import HarnessConfig, RuntimeManager

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

API_OK = "FISH_PRODUCTION_OK"
AFTER_CANCEL = "FISH_SESSION_AFTER_CANCEL_OK"


def rpc(session: requests.Session, origin: str, method: str, args: dict) -> dict:
    body = {
        "type": "client-request",
        "rpcId": f"prod-{method}-{uuid.uuid4().hex[:8]}",
        "method": method,
        "payload": {"args": args},
    }
    response = session.post(f"{origin}/api/{method}", json=body, timeout=30)
    response.raise_for_status()
    data = response.json()
    result = data.get("result", {})
    if not result.get("ok"):
        raise RuntimeError(f"{method}: {result.get('error')}")
    return result.get("value", {})


def session_cursor(ws_url: str, cookie: str, session_id: str) -> int:
    stream_id = f"prod-follow-{uuid.uuid4().hex[:8]}"
    with connect(ws_url, additional_headers={"Cookie": cookie}, open_timeout=10) as ws:
        ws.send(json.dumps({
            "type": "open",
            "streamId": stream_id,
            "endpoint": "session/follow",
            "payload": {"args": {"request": {"address": {"kind": "session", "sessionId": session_id}}}},
        }))
        deadline = time.time() + 10
        while time.time() < deadline:
            raw = ws.recv(timeout=max(0.1, deadline - time.time()))
            frame = json.loads(raw)
            if frame.get("streamId") != stream_id:
                continue
            if frame.get("type") == "error":
                raise RuntimeError(f"session/follow: {frame.get('error')}")
            value = frame.get("value")
            if frame.get("type") == "item" and isinstance(value, dict) \
                    and value.get("type") == "snapshot" and isinstance(value.get("cursor"), int):
                return value["cursor"]
    raise TimeoutError("session/follow 没有返回 snapshot cursor")


def page_text(page: dict) -> str:
    chunks = []
    for record in page.get("records", []):
        if record.get("type") != "event":
            continue
        event = record.get("event", {})
        if event.get("type") != "assistant/message":
            continue
        message = event.get("data", {}).get("message", {})
        for block in message.get("content", []):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                chunks.append(block["text"])
    return "\n".join(chunks)


def wait_for_text(session, origin, ws_url, cookie, session_id, marker, timeout=180) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        cursor = session_cursor(ws_url, cookie, session_id)
        page = rpc(session, origin, "session/page", {
            "request": {
                "address": {"kind": "session", "sessionId": session_id},
                "throughSeq": cursor,
                "maxMessages": 50,
            },
        })
        last = page_text(page)
        if marker in last:
            return {"ok": True, "text": last}
        time.sleep(1)
    return {"ok": False, "text": last}


def main() -> int:
    config = HarnessConfig()
    runtime = RuntimeManager(config)
    agent_id = "research"
    key = os.environ.get(config.agent(agent_id).apiKeyEnv)
    if not key:
        print(f"未设置 {config.agent(agent_id).apiKeyEnv}；production smoke 跳过。")
        return 2

    started = runtime.start(agent_id)
    if not started.get("ok"):
        print("启动失败：", started)
        return 1
    try:
        launch = None
        for _ in range(30):
            time.sleep(0.5)
            for line in runtime.logs.get(agent_id, []):
                match = re.search(r"dsh web: (http://\S+)", line)
                if match:
                    launch = match.group(1)
                    break
            if launch:
                break
        if not launch:
            print("未拿到 dsh web 启动 URL")
            return 1

        session = requests.Session()
        auth = session.get(launch, allow_redirects=False, timeout=10)
        if auth.status_code != 303:
            print(f"认证失败：HTTP {auth.status_code}")
            return 1
        origin = launch.split("/?")[0]
        cookie = "; ".join(f"{k}={v}" for k, v in session.cookies.get_dict().items())
        ws_url = origin.replace("http://", "ws://").replace("https://", "wss://") + "/api/remote.mux"

        # 1) production-path 真实调用
        created = rpc(session, origin, "session/create", {"request": {}})
        sid = created["sessionId"]
        rpc(session, origin, "session/prompt", {
            "request": {
                "requestId": str(uuid.uuid4()),
                "sessionId": sid,
                "mode": "queue",
                "content": [{"type": "text", "text": f"Reply with exactly: {API_OK}"}],
            },
        })
        result = wait_for_text(session, origin, ws_url, cookie, sid, API_OK)
        print("production web call:", "PASS" if result["ok"] else "FAIL")
        if not result["ok"]:
            print("last text:", result["text"][-800:])
            return 1

        # 2) 临时 Session 长生成 -> Cancel
        cancel_sid = rpc(session, origin, "session/create", {"request": {}})["sessionId"]
        rpc(session, origin, "session/prompt", {
            "request": {
                "requestId": str(uuid.uuid4()),
                "sessionId": cancel_sid,
                "mode": "queue",
                "content": [{
                    "type": "text",
                    "text": "Write a very long, detailed 3000-word essay about the history of computing. Do not stop early.",
                }],
            },
        })
        time.sleep(1.0)
        cancelled = runtime.cancel(agent_id, session_id=cancel_sid)
        print("cancel:", cancelled.get("ok"), cancelled.get("status"), cancelled.get("body"))
        alive = runtime.status(agent_id)["state"] == "running"
        print("runtime after cancel:", runtime.status(agent_id)["state"])

        # 3) 同一 Session 继续响应
        rpc(session, origin, "session/prompt", {
            "request": {
                "requestId": str(uuid.uuid4()),
                "sessionId": cancel_sid,
                "mode": "queue",
                "content": [{"type": "text", "text": f"Reply with exactly: {AFTER_CANCEL}"}],
            },
        })
        after = wait_for_text(session, origin, ws_url, cookie, cancel_sid, AFTER_CANCEL)
        print("session after cancel:", "PASS" if after["ok"] else "FAIL")
        if not alive or not after["ok"]:
            print("last text:", after["text"][-800:])
            return 1
        print("\nPRODUCTION SMOKE PASS")
        return 0
    finally:
        runtime.stop(agent_id)


if __name__ == "__main__":
    raise SystemExit(main())
