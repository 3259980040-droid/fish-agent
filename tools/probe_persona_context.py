"""Local mock Harness acceptance for one persona context node.

Uses the bundled Harness and a synthetic local model endpoint. No paid model,
no NapCat, no QQ sends. Writes only under a temporary fixture root.
"""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "harness")]
import dependencies
from fish_desktop.bootstrap import initialize
from storage import read_json, write_json
from core import HarnessConfig, RuntimeManager
from secrets_store import SecretStore
from social_context import SocialContext
from desktop_chat import DesktopChat
from request_inspector import RequestInspector


def free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    fixture = ROOT / "artifacts/social-base/persona-context-probe" / str(int(time.time()))
    root = initialize(fixture)
    received = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            received.append(data)
            message = {
                "id": "mock_" + str(len(received)),
                "type": "message",
                "role": "assistant",
                "model": "deepseek-flash",
                "content": [{"type": "text", "text": "本地模拟回复"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 30, "output_tokens": 6},
            }
            if data.get("stream"):
                events = [
                    ("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "本地模拟回复"}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 6}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                body = "".join("event: " + kind + "\ndata: " + json.dumps(value, ensure_ascii=False) + "\n\n" for kind, value in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            else:
                body = json.dumps(message, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    raw = read_json(root / "harness/agents.json")
    raw["runtime"]["tool_host_port"] = free_port()
    raw["runtime"]["ports"]["qq"] = free_port()
    for agent in raw["agents"]:
        if agent["id"] == "qq":
            agent.update(base_url="http://127.0.0.1:" + str(server.server_port), model="deepseek-flash", provider="deepseek-official")
    write_json(root / "harness/agents.json", raw)
    SecretStore(root).put("agent-qq", "synthetic-local-provider-key")
    manager = RuntimeManager(HarnessConfig(root))
    result = {"root": str(root), "paid_requests": 0, "qq_messages_sent": 0}
    try:
        contexts = SocialContext(root)
        a = contexts.save_persona("qq", "A", "ISOLATED_PERSONA_A", global_rules="GLOBAL_A", workspace_rules="WORKSPACE_A")
        b = contexts.save_persona("qq", "B", "ISOLATED_PERSONA_B", global_rules="GLOBAL_B", workspace_rules="WORKSPACE_B")
        chat = DesktopChat(root, "qq", manager)
        ca = chat.new("A", revision=a["id"])
        cb = chat.new("B", revision=b["id"])
        checks = []
        for turn in range(1, 4):
            chat.send(ca, f"CANARY_A_{turn}", [])
            ctx = chat.conversation_context(ca)
            cap = RequestInspector(root).latest("qq", ctx["session"])
            messages = cap["payload"]["messages"]
            sources = {}
            for message in messages:
                key = (message.get("role"), message.get("source"))
                sources[str(key)] = sources.get(str(key), 0) + 1
            text = json.dumps(messages, ensure_ascii=False)
            checks.append({"turn": turn, "scope": ctx["scope"], "persona_copies": sources.get("('user', 'fish-persona-context')", 0), "a_marker": text.count("ISOLATED_PERSONA_A"), "b_marker": text.count("ISOLATED_PERSONA_B"), "sources": sources})
        chat.send(cb, "CANARY_B_1", [])
        ctxb = chat.conversation_context(cb)
        capb = RequestInspector(root).latest("qq", ctxb["session"])
        textb = json.dumps(capb["payload"]["messages"], ensure_ascii=False)
        bcheck = {"scope": ctxb["scope"], "a_marker": textb.count("ISOLATED_PERSONA_A"), "b_marker": textb.count("ISOLATED_PERSONA_B")}
        assert checks[0]["persona_copies"] == 1, checks
        assert all(c["persona_copies"] == 1 for c in checks), checks
        assert all(c["a_marker"] == 1 and c["b_marker"] == 0 for c in checks), checks
        assert bcheck["b_marker"] == 1 and bcheck["a_marker"] == 0, bcheck
        # A live session alone cannot validate its persisted format. Tear down
        # the native runtime, then resume the exact A and B sessions from disk.
        sessions = {cid: chat.conversation_context(cid)['session'] for cid in (ca, cb)}
        manager.stop('qq')
        manager = RuntimeManager(HarnessConfig(root))
        chat = DesktopChat(root, 'qq', manager)
        restored = []
        for cid, marker, forbidden in ((ca, 'ISOLATED_PERSONA_A', 'CANARY_B_1'),
                                        (cb, 'ISOLATED_PERSONA_B', 'CANARY_A_1')):
            chat.send(cid, 'COLD_RESTORE_CHECK', [])
            context = chat.conversation_context(cid)
            assert context['session'] == sessions[cid]
            payload = RequestInspector(root).latest('qq', context['session'])['payload']
            history = json.dumps(payload['messages'], ensure_ascii=False)
            assert history.count(marker) == 1 and forbidden not in history
            assert ('CANARY_A_1' if cid == ca else 'CANARY_B_1') in history
            restored.append({'session': context['session'], 'same_session': True, 'history_kept': True})
        result.update(ok=True, turns=checks, b=bcheck, cold_restores=restored, provider_requests=len(received))
    except Exception as exc:
        result.update(ok=False, error=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        manager.stop("qq")
        server.shutdown()
        server.server_close()
        from tool_host import _hosts
        host = _hosts.get(str(root.resolve()))
        if host:
            host.stop.set()
        write_json(fixture / "probe-result.json", result)
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
