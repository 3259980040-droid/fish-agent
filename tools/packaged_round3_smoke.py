"""Run the actual packaged QQ backend against a local OneBot simulator."""
import asyncio
import json
import os
import socket
import sys
import tempfile
import shutil
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from channels.fish_qq.tests.fake_napcat import FakeNapCat
from websockets.asyncio.client import connect


async def main():
    exe = ROOT / "dist-round3-final/Fish/Fish.exe"
    fake = FakeNapCat("packaged-napcat-fixture")
    await fake.start()
    proc = None
    temp_parent = (ROOT / "artifacts/round3").resolve()
    temp_workspace = tempfile.TemporaryDirectory(prefix="fish-packaged-round3-", dir=temp_parent)
    try:
        with nullcontext(temp_workspace.name) as temp:
            root = Path(temp)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            config = root / "channels/fish_qq/config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"listen":{"port":port}, "napcat":{"url":fake.url},
                "allowlist":{"owner_id":"12345678"}}), encoding="utf-8")
            settings = root / "harness/control.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({"blocked_text":["BLOCK_THIS"]}), encoding="utf-8")
            env = dict(os.environ, FISH_HARNESS_HOME=str(root), FISH_QQ_TOKEN="packaged-core-fixture", NAPCAT_TOKEN="packaged-napcat-fixture")
            proc = await asyncio.create_subprocess_exec(str(exe), "--backend", "fish-qq", "--config", str(config),
                env=env, cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            ws = None
            for _ in range(70):
                if proc.returncode is not None:
                    detail = (await proc.stdout.read()).decode("utf-8", errors="replace")
                    (ROOT / "artifacts/round3/packaged-bootloader.log").write_text(detail, encoding="utf-8")
                    raise RuntimeError(f"打包后端提前退出：{proc.returncode} {detail[-2500:]}")
                try:
                    ws = await connect(f"ws://127.0.0.1:{port}/ws", additional_headers={"Authorization":"Bearer packaged-core-fixture"}, open_timeout=1)
                    break
                except OSError:
                    await asyncio.sleep(.2)
            if ws is None:
                raise TimeoutError("打包后端未就绪")
            async with ws:
                await ws.send(json.dumps({"v":1,"type":"hello","id":"hello","payload":{"client":"packaged-test"}}))
                await ws.recv()
                await asyncio.wait_for(fake.connected.wait(), 5)
                await asyncio.sleep(.3)
                results = []
                for seq, text in enumerate(("BLOCK_THIS", "打包后端普通消息验收")):
                    request_id = f"fixture-{seq}"
                    await ws.send(json.dumps({"v":1,"type":"action","id":request_id,"action":"send_message",
                        "payload":{"chat_id":"private:12345678","segments":[{"type":"text","text":text}]}}))
                    while True:
                        reply = json.loads(await asyncio.wait_for(ws.recv(), 5))
                        if reply.get("id") == request_id:
                            results.append(reply)
                            break
                sends = [a for a in fake.actions if a["action"] == "send_private_msg"]
                record = {"ok": not results[0]["ok"] and results[0]["error"]["code"] == "policy_blocked" and results[1]["ok"] and len(sends) == 1,
                          "blocked_before_transport": not results[0]["ok"], "allowed_fixture_sends": len(sends),
                          "real_qq_messages_sent": 0, "bundled_rules_created": (root / "harness/safety/rules.json").is_file(), "executable": str(exe)}
                (ROOT / "artifacts/round3/packaged-backend-check.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(record, ensure_ascii=False))
                return 0 if record["ok"] and record["bundled_rules_created"] else 1
    finally:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        await fake.stop()
        log = Path(temp_workspace.name) / "harness/backend-bootstrap.log"
        if log.exists():
            shutil.copy2(log, ROOT / "artifacts/round3/packaged-backend.log")
        task_temp = Path(temp_workspace.name).resolve()
        assert task_temp.parent == temp_parent and task_temp.name.startswith("fish-packaged-round3-")
        temp_workspace.cleanup()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
