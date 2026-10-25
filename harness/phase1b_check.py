"""Phase 1B 验收：pinned CLI、profile、overlay、key、effective、cancel。"""

from __future__ import annotations
from locations import project_path

import json
import os
import shutil
import sys
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import (
    DatabaseRouter,
    HarnessConfig,
    PersonaManager,
    RuntimeManager,
    init_databases,
    read_text,
    resolve,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        FAILS.append(name)


def make_root() -> Path:
    base = Path(__file__).resolve().parent / ".phase1b-tmp"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    root = base / "run"
    root.mkdir(parents=True, exist_ok=True)
    (project_path(root, "harness")).mkdir(parents=True, exist_ok=True)
    (project_path(root, "harness", "global")).mkdir(parents=True, exist_ok=True)
    (project_path(root, "harness", "agents")).mkdir(parents=True, exist_ok=True)
    (root / "homes" / "research").mkdir(parents=True, exist_ok=True)
    (root / "knowledge").mkdir(parents=True, exist_ok=True)
    (project_path(root, "qq-work")).mkdir(parents=True, exist_ok=True)
    (project_path(root, "harness", "global", "AGENTS.md")).write_text("GLOBAL RULES\n", encoding="utf-8")
    (project_path(root, "harness", "agents", "research.persona.md")).write_text("RESEARCH PERSONA\n", encoding="utf-8")
    (project_path(root, "harness", "agents", "qq.persona.md")).write_text("QQ PERSONA\n", encoding="utf-8")
    (root / "knowledge" / "AGENTS.md").write_text("RESEARCH WORKSPACE\n", encoding="utf-8")
    (project_path(root, "qq-work", "AGENTS.md")).write_text("QQ WORKSPACE\n", encoding="utf-8")

    (root / "fake_dsh.py").write_text(
        "\n".join([
            "import os, sys, pathlib",
            "args = sys.argv[1:]",
            "home = pathlib.Path(os.environ['DSH_HOME'])",
            "if '--version' in args:",
            "    print('0.2.1-rc.2')",
            "    sys.exit(0)",
            "if 'plugin' in args:",
            "    (home/'profiles'/'web'/'node_modules'/'fish-control').mkdir(parents=True, exist_ok=True)",
            "    sys.exit(0)",
            "if '--dump-config' in args:",
            "    (home/'profiles'/'web').mkdir(parents=True, exist_ok=True)",
            "    (home/'profiles'/'web'/'package.json').write_text('{}')",
            "    print(\"- id: agent-default-model\")",
            "    print(\"  name: '@deepseek-ai/dsh-agent-default-model'\")",
            "    print('  config:')",
            "    print('    provider: deepseek-official')",
            "    print('    model: deepseek-flash')",
            "    sys.exit(0)",
            "if '--port' in args:",
            "    (home/'key.txt').write_text(os.environ.get('DEEPSEEK_API_KEY', ''))",
            "    (home/'token.txt').write_text(os.environ.get('FISH_CONTROL_TOKEN', ''))",
            "    sys.exit(1)",
            "sys.exit(1)",
        ]) + "\n",
        encoding="utf-8",
    )
    (root / "fake_dsh.cmd").write_bytes(
        ('@echo off\r\npython "%~dp0fake_dsh.py" %*\r\n').encode("ascii")
    )
    return root


class CancelHandler(BaseHTTPRequestHandler):
    token = "test-token"
    last_path = ""

    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802
        CancelHandler.last_path = self.path
        if not self.path.startswith("/fish/cancel"):
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("x-fish-token") != self.token:
            self.send_response(401)
            self.end_headers()
            return
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    root = make_root()
    server = ThreadingHTTPServer(("127.0.0.1", 0), CancelHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    config_data = {
        "runtime": {
            "kind": "dsh",
            "enabled": True,
            "cli": str(root / "fake_dsh.cmd"),
            "version": "0.2.1-rc.2",
            "profile": "web",
            "homes": {"research": "homes/research"},
            "ports": {"research": port},
            "patches": {"research": "harness/runtime/research.patch.yml"},
            "control_token_env": "FISH_CONTROL_TOKEN",
            "control_plugin": "harness/runtime/fish-control",
        },
        "agents": [
            {
                "id": "research",
                "name": "研究实例",
                "workspace": "knowledge",
                "db": "data/research/research.db",
                "persona": "harness/agents/research.persona.md",
                "provider": "deepseek-official",
                "model": "deepseek-flash",
                "apiKeyEnv": "FISH_RESEARCH_DEEPSEEK_API_KEY",
            },
            {
                "id": "qq",
                "name": "大肥鱼",
                "workspace": "qq-work",
                "db": "data/qq/qq.db",
                "persona": "harness/agents/qq.persona.md",
                "provider": "deepseek-official",
                "model": "deepseek-flash",
                "apiKeyEnv": "FISH_QQ_DEEPSEEK_API_KEY",
            },
        ],
        "channels": [],
    }
    (project_path(root, "harness", "agents.json")).write_text(
        json.dumps(config_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # control plugin path is under the project, but this temp root has none; create a dummy
    (project_path(root, "harness", "runtime", "fish-control")).mkdir(parents=True, exist_ok=True)

    os.environ["FISH_RESEARCH_DEEPSEEK_API_KEY"] = "test-key"
    os.environ["FISH_CONTROL_TOKEN"] = "test-token"

    config = HarnessConfig(root)
    runtime = RuntimeManager(config)

    personas = PersonaManager(config)
    preview = personas.preview("research")
    check(
        "P0-5 投影包含 global/persona/workspace",
        all(marker in preview["text"] for marker in ("GLOBAL RULES", "RESEARCH PERSONA", "RESEARCH WORKSPACE")),
        preview["text"],
    )
    projected = personas.project("research")
    target = root / "homes" / "research" / "AGENTS.md"
    check("P0-5 投影写入 home", projected["ok"] and target.exists())
    check("P0-5 投影可重建", "RESEARCH PERSONA" in read_text(target))
    target.unlink()
    personas.project("research")
    check("P0-5 删除后可重建", target.exists() and "RESEARCH PERSONA" in read_text(target))

    paths = init_databases(root)
    router = DatabaseRouter(config, paths)
    check("P0-7 research 路由", router.path_for("research") == paths["research"])
    check("P0-7 qq 路由", router.path_for("qq") == paths["qq"])
    check("P0-7 schema_version", router.schema_version("research") == 1 and router.schema_version("qq") == 1)
    denied = False
    try:
        router.assert_owner("research", paths["qq"])
    except PermissionError:
        denied = True
    check("P0-7 跨库访问被拒绝", denied)
    with closing(router.connect("research")) as conn:
        conn.execute(
            "INSERT INTO sources(type,url,bvid,captured_at,status) VALUES(?,?,?,?,?)",
            ("test", "https://example.com", "BV1", "now", "new"),
        )
    with closing(router.connect("qq")) as conn:
        qq_tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    check("P0-7 research 写入 qq 不可见", "sources" not in qq_tables, str(qq_tables))

    check("pinned CLI：status stopped", runtime.status("research")["state"] == "stopped",
          runtime.status("research"))

    prepared = runtime.ensure_profile("research")
    check("profile 初始化", prepared["ok"] and runtime.profile_ready("research"), prepared)

    overlay = runtime.write_overlay("research")
    text = read_text(overlay)
    check("overlay 写入 model", 'model: "deepseek-flash"' in text)
    check("overlay 禁用 account", "id: llm-deepseek-account" in text and "disabled: true" in text)

    effective = runtime.effective_config("research")
    check("effective provider/model", effective.get("ok") and effective.get("matches"), effective)

    key = runtime.api_key_status("research")
    check("API key 已识别", key["set"] is True, key)

    started = runtime.start("research")
    check("启动失败返回 failed", started["state"] == "failed", started)
    home = root / "homes" / "research"
    check("API key 注入子进程", read_text(home / "key.txt") == "test-key", read_text(home / "key.txt"))
    check("control token 注入", read_text(home / "token.txt") == "test-token", read_text(home / "token.txt"))

    cancelled = runtime.cancel("research", session_id="test-session")
    check("Cancel Current Turn HTTP", cancelled.get("ok") is True, cancelled)
    check("Cancel 携带 session_id", "session_id=test-session" in CancelHandler.last_path,
          CancelHandler.last_path)

    paths["research"].unlink()
    with closing(router.connect("qq")) as conn:
        qq_after_delete = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    check("P0-7 删除 research 不影响 qq", qq_after_delete == 0)

    server.shutdown()
    shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


