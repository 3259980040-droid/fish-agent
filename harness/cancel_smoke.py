"""真实 /fish/cancel 路由验收：临时 Session，不碰正式大肥鱼人格。

不需要 API key；只创建/取消临时 session，不触发模型生成。
"""

from __future__ import annotations

import re
import sys
import time

import requests

from core import HarnessConfig, RuntimeManager

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def rpc(session: requests.Session, origin: str, method: str, args: dict) -> dict:
    body = {
        "type": "client-request",
        "rpcId": f"cancel-smoke-{method}",
        "method": method,
        "payload": {"args": args},
    }
    response = session.post(f"{origin}/api/{method}", json=body, timeout=15)
    response.raise_for_status()
    return response.json()


def main() -> int:
    config = HarnessConfig()
    runtime = RuntimeManager(config)
    agent_id = "research"
    started = runtime.start(agent_id)
    if not started.get("ok"):
        print(f"启动失败：{started}")
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
        auth = session.get(launch, allow_redirects=False, timeout=5)
        if auth.status_code != 303:
            print(f"认证失败：HTTP {auth.status_code}")
            return 1
        origin = launch.split("/?")[0]

        sessions = [
            rpc(session, origin, "session/create", {"request": {}})["result"]["value"]["sessionId"]
            for _ in range(2)
        ]
        headers = {"x-fish-token": runtime.control_token()}

        status = requests.get(f"{origin}/fish/status", headers=headers, timeout=5)
        print("status:", status.status_code, status.text)
        no_id = requests.post(f"{origin}/fish/cancel", headers=headers, json={}, timeout=5)
        print("cancel without session_id:", no_id.status_code, no_id.text)
        one = requests.post(
            f"{origin}/fish/cancel?session_id={sessions[0]}",
            headers=headers, json={"session_id": sessions[0]}, timeout=5,
        )
        print("cancel with session_id:", one.status_code, one.text)

        ok = (
            status.status_code == 200
            and no_id.status_code == 409
            and one.status_code == 200
            and one.json().get("session_id") == sessions[0]
        )
        print("\nPASS" if ok else "\nFAIL")
        return 0 if ok else 1
    finally:
        runtime.stop(agent_id)


if __name__ == "__main__":
    raise SystemExit(main())
