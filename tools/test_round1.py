"""Round one checks for owner capabilities and default photo chat."""
from __future__ import annotations
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "harness")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PIL import Image
from governance import Governance, control_token, write_json
from desktop_chat import DesktopChat
from qq_agent_loop import QQAgentLoop
from guard import Guard
_QT_APP = None


class RoundOneTests(unittest.TestCase):
    def setUp(self):
        # These tests replace DSH HTTP/session clients; the real tool host has
        # separate second-round HTTP and ownership integration contracts.
        for name in ('ensure_host', 'bind_session'):
            patcher = patch('tool_host.' + name)
            patcher.start(); self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="fish-round1-")
        self.root = Path(self.temp.name)
        write_json(self.root / "channels/fish_qq/config.json", {"allowlist": {"owner_id":"12345678"}})
        self.policy = Governance(self.root)
        self.photo = self.root / "photo.png"
        Image.new("RGB", (64, 64), "blue").save(self.photo)

    def tearDown(self):
        self.temp.cleanup()

    def test_owner_identity_uses_real_sender_and_narrow_permissions(self):
        self.assertTrue(self.policy.permitted("12345678", "cancel"))
        self.assertFalse(self.policy.permitted("99999999", "cancel"))
        self.assertFalse(self.policy.permitted("12345678", "cancel", private=False))
        self.assertFalse(self.policy.permitted("12345678", "install_shell"))
        self.assertIn("认真", self.policy.identity_prompt("12345678"))
        self.assertIn("无主人管理权限", self.policy.identity_prompt("nickname:主人"))
        self.policy.save({"owner_permissions":["status"]})
        self.assertFalse(self.policy.permitted("12345678", "cancel"))

    def test_adapter_reuses_authenticated_process_and_rejects_wrong_role(self):
        from runtime_adapter import DSHRuntimeAdapter
        manager = SimpleNamespace(config=SimpleNamespace(runtime_port=lambda role: 1234), start=unittest.mock.Mock())
        response = SimpleNamespace(status_code=200, raise_for_status=lambda: None,
            json=lambda: {"url":"http://127.0.0.1:1234/?launch=fixture", "role":"qq", "guard_active":True, "control_version":3, 'root_id':__import__('core').root_id(self.root)})
        with patch("runtime_adapter.requests.get", return_value=response):
            client = DSHRuntimeAdapter(self.root, "qq", manager).connect()
        self.assertEqual(client.origin, "http://127.0.0.1:1234")
        manager.start.assert_not_called()
        with patch("runtime_adapter.requests.get", return_value=response):
            with self.assertRaises(PermissionError): DSHRuntimeAdapter(self.root, "research", manager).connect()

    def test_adapter_waits_for_control_plugin_during_startup(self):
        import requests
        from runtime_adapter import DSHRuntimeAdapter
        manager = SimpleNamespace(config=SimpleNamespace(runtime_port=lambda role: 1234), start=unittest.mock.Mock(return_value={"ok":True}))
        missing = SimpleNamespace(status_code=404)
        ready = SimpleNamespace(status_code=200, raise_for_status=lambda:None,
            json=lambda:{"url":"http://127.0.0.1:1234/?launch=fixture", "role":"qq", "guard_active":True, "control_version":3, 'root_id':__import__('core').root_id(self.root)})
        with patch("runtime_adapter.requests.get", side_effect=[requests.ConnectionError(), missing, ready]), patch("runtime_adapter.time.sleep"):
            client = DSHRuntimeAdapter(self.root, "qq", manager).connect()
        self.assertIn("1234", client.origin)
        manager.start.assert_called_once_with("qq")

    def test_rule_validation_preserves_config(self):
        before = self.policy.config()
        with self.assertRaises(Exception): self.policy.save({"block_patterns":["["]})
        self.assertEqual(self.policy.config(), before)

    def test_output_blocking_is_real_and_records_no_message_body(self):
        self.policy.save({"blocked_text":["BLOCK_THIS"]})
        self.policy.check_output("普通照片中有蓝色方块。")
        with self.assertRaises(PermissionError): self.policy.check_output("B L O C K _ T H I S")
        self.assertEqual(self.policy.snapshot()["audit"][0]["kind"], "blocked")
        self.assertNotIn("B L O C K", json.dumps(self.policy.snapshot()["audit"]))

    def test_invalid_builtin_rule_fails_closed(self):
        path = self.root / "rules.json"
        write_json(path, {"categories":[{"id":"bad", "action":"block", "patterns":["["]}]})
        with self.assertRaises(ValueError): Guard(path)

    def test_daily_budget_atomic_and_role_isolated(self):
        self.policy.save({"max_daily_calls":5})
        def consume(_):
            try:
                self.policy.consume("calls")
                return True
            except PermissionError: return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(consume, range(30)))
        self.assertEqual(sum(results), 5)
        Governance(self.root, "research").consume("calls")
        self.assertEqual(self.policy.snapshot()["usage"]["calls"], 5)

    def test_owner_admin_commands_bypass_model_and_respect_identity(self):
        loop = QQAgentLoop(self.root)
        loop.channel = SimpleNamespace(action=AsyncMock(return_value={"ok":True}))
        payload = {"chat_type":"private", "chat_id":"private:12345678", "message_id":"test-1", "segments":[{"type":"text","text":"/暂停"}]}
        self.assertFalse(asyncio.run(loop._admin_command(payload, "99999999")))
        self.assertFalse(self.policy.config()["paused"])
        self.assertTrue(asyncio.run(loop._admin_command(payload, "12345678")))
        self.assertTrue(self.policy.config()["paused"])
        self.assertFalse(Governance(self.root, "research").config()["paused"])
        payload["segments"][0]["text"] = "/继续"
        asyncio.run(loop._admin_command(payload, "12345678"))
        self.assertFalse(self.policy.config()["paused"])
        self.assertEqual(loop.channel.action.await_count, 2)

    def test_control_endpoint_requires_token(self):
        async def run():
            loop = QQAgentLoop(self.root)
            server = await asyncio.start_server(loop._control, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                for token, expected in (("", b"401 Unauthorized"), (control_token(self.root), b"200 OK")):
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.write(f"GET /status HTTP/1.1\r\nHost: localhost\r\nx-fish-token: {token}\r\n\r\n".encode())
                    await writer.drain()
                    self.assertIn(expected, await reader.read())
                    writer.close(); await writer.wait_closed()
            finally:
                server.close(); await server.wait_closed()
        asyncio.run(run())

    def _chat_fixture(self, result="我看到了蓝色方块。"):
        client = SimpleNamespace(auth=AsyncMock(), create_session=AsyncMock(return_value="test-session"),
            baseline=AsyncMock(return_value=0), prompt=AsyncMock(), wait_response=AsyncMock(return_value={"ok":True,"text":result}))
        runtime = SimpleNamespace(cancel=lambda *a: {"ok":True})
        chat = DesktopChat(self.root, "qq", runtime)
        return chat, client

    def test_regular_send_submits_native_photo_without_recognition_action(self):
        chat, client = self._chat_fixture()
        cid = chat.new(isolated=False)  # Existing daily conversations retain their legacy scope.
        with patch.object(chat, "_client", return_value=client):
            result = chat.send(cid, "看一下照片", [{"type":"image","path":str(self.photo)}])
        blocks = client.prompt.await_args.args[1]
        self.assertIn("来自主人账号", blocks[0]["text"])
        self.assertTrue(blocks[0]["text"].endswith("看一下照片"))
        self.assertEqual(blocks[1]["type"], "image")
        self.assertGreater(len(blocks[1]["data"]), 100)
        self.assertEqual(result["messages"][-1]["text"], "我看到了蓝色方块。")
        other = DesktopChat(self.root, "research", SimpleNamespace())
        self.assertEqual(other.snapshot()["messages"], [])

    def test_blocked_chat_output_never_appears_as_assistant_reply(self):
        self.policy.save({"blocked_text":["BLOCK_THIS"]})
        chat, client = self._chat_fixture("BLOCK_THIS")
        cid = chat.new(isolated=False)
        with patch.object(chat, "_client", return_value=client):
            with self.assertRaises(PermissionError): chat.send(cid, "你好", [])
        rows = chat.snapshot(cid)["messages"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "blocked")

    def test_qt_paste_photo_enters_normal_composer(self):
        global _QT_APP
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QImage
        from fish_desktop.services import FishServices
        from fish_desktop.conversation import ChatPage
        _QT_APP = QApplication.instance() or QApplication([])
        app = _QT_APP
        page = ChatPage(FishServices(self.root))
        mime = QMimeData(); mime.setImageData(QImage(str(self.photo)))
        page.editor.insertFromMimeData(mime)
        self.assertEqual(len(page.attachments), 1)
        self.assertEqual(page.attachments[0]["type"], "image")
        self.assertEqual(page.send_btn.text(), "发送")
        page.close(); app.processEvents()

    def test_qq_transport_blocks_before_network_send(self):
        from channels.fish_qq.config import FishQQConfig
        from channels.fish_qq.server import FishQQApp
        self.policy.save({"blocked_text":["BLOCK_THIS"]})
        cfg = FishQQConfig(root=self.root, owner_id="12345678", state_dir=self.root/"spool")
        app = FishQQApp(cfg)
        app.state = "ready"
        app.onebot = SimpleNamespace(send_action=AsyncMock(return_value={"data":{"message_id":"1"}}))
        for target, text in (("private:12345678", "BLOCK_THIS"), ("private:99999999", "普通文字")):
            result = asyncio.run(app._send_message({"chat_id":target,"segments":[{"type":"text","text":text}]}))
            self.assertFalse(result["ok"])
        app.onebot.send_action.assert_not_awaited()

    def test_restart_projection_includes_owner_diligence(self):
        from core import HarnessConfig, PersonaManager
        write_json(self.root/"harness/agents.json", {"agents":[{"id":"qq","persona":"harness/agents/qq.persona.md","workspace":"qq-work"}]})
        text = PersonaManager(HarnessConfig(self.root)).render("qq")
        self.assertIn("不得假装已执行或已完成", text)
        self.assertIn("普通消息中提供的图片是默认输入能力", text)


if __name__ == "__main__": unittest.main(verbosity=2)
