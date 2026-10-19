"""Phase 1A 验收：Fish Core 在 Runtime 缺失/错误/启动失败时仍可用。"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from core import (
    ChannelManager,
    HarnessConfig,
    RuntimeManager,
    init_databases,
    read_text,
    save_agent_settings,
    write_text,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        FAILS.append(name)


def make_root() -> Path:
    base = Path(__file__).resolve().parent / ".phase1a-tmp"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    root = base / "run"
    root.mkdir(parents=True, exist_ok=True)
    (root / "harness").mkdir(parents=True, exist_ok=True)
    (root / "homes" / "research").mkdir(parents=True, exist_ok=True)
    (root / "homes" / "qq").mkdir(parents=True, exist_ok=True)
    (root / "knowledge").mkdir(parents=True, exist_ok=True)
    (root / "qq-work").mkdir(parents=True, exist_ok=True)
    return root


def base_config(root: Path, runtime: dict | None = None) -> None:
    data = {
        "agents": [
            {
                "id": "research",
                "name": "研究实例",
                "runtime": "dsh",
                "workspace": "knowledge",
                "db": "data/research/research.db",
                "persona": "harness/agents/research.persona.md",
                "model": "deepseek-flash",
                "apiKeyEnv": "FISH_RESEARCH_DEEPSEEK_API_KEY",
            },
            {
                "id": "qq",
                "name": "大肥鱼",
                "runtime": "dsh",
                "workspace": "qq-work",
                "db": "data/qq/qq.db",
                "persona": "harness/agents/qq.persona.md",
                "model": "deepseek-flash",
                "apiKeyEnv": "FISH_QQ_DEEPSEEK_API_KEY",
            },
        ],
        "channels": [
            {
                "id": "qq",
                "name": "QQ 通道",
                "kind": "fish-qq",
                "enabled": False,
                "listen": "ws://127.0.0.1:3101/ws",
                "command": "",
                "config": "channels/fish_qq/config.json",
            }
        ],
    }
    if runtime is not None:
        data["runtime"] = runtime
    (root / "harness" / "agents.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def fake_cli(root: Path) -> Path:
    path = root / "fake-dsh.cmd"
    path.write_bytes(
        (
            "@echo off\r\n"
            'if "%~1"=="--version" (\r\n'
            "  echo 0.2.1-rc.2\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            "exit /b 1\r\n"
        ).encode("ascii")
    )
    return path


def main() -> int:
    root = make_root()
    base_config(root)  # 完全没有 runtime 块
    config = HarnessConfig(root)
    check("无 runtime：配置可加载", config.error == "", config.error)
    check("无 runtime：Agent 仍存在", len(config.agents) == 2)
    check("无 runtime：Channel 仍存在", len(config.channels) == 1)

    runtime = RuntimeManager(config)
    state = runtime.status("research")
    check("无 runtime：状态 unconfigured", state["state"] == "unconfigured", state)

    dbs = init_databases(root)
    check("无 runtime：三个数据库存在", all(p.exists() for p in dbs.values()), str(dbs))
    import sqlite3

    with sqlite3.connect(dbs["research"]) as conn:
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    check("无 runtime：research.db 可访问", {"sources", "observations", "memories"} <= tables, str(tables))

    persona = root / "harness" / "agents" / "research.persona.md"
    write_text(persona, "初始人格")
    check("无 runtime：人格可写可读", read_text(persona) == "初始人格")
    save_agent_settings(config.path, "research", {"model": "deepseek-v4-pro"})
    reloaded = HarnessConfig(root)
    check("无 runtime：Agent 设置可保存", reloaded.agent("research").model == "deepseek-v4-pro")

    # runtime.cli 为空
    base_config(root, {"kind": "dsh", "enabled": True, "cli": "", "profile": "web"})
    config = HarnessConfig(root)
    check("cli 为空：unconfigured", RuntimeManager(config).status("research")["state"] == "unconfigured")

    # runtime.cli 不存在
    base_config(root, {"kind": "dsh", "enabled": True, "cli": str(root / "missing.exe"), "profile": "web"})
    config = HarnessConfig(root)
    check("cli 不存在：unavailable", RuntimeManager(config).status("research")["state"] == "unavailable")

    # home 缺失
    base_config(root, {
        "kind": "dsh", "enabled": True, "cli": str(fake_cli(root)),
        "version": "0.2.1-rc.2", "profile": "web",
        "homes": {"research": "homes/not-there"}, "ports": {"research": 3999},
    })
    config = HarnessConfig(root)
    check("home 缺失：unavailable", RuntimeManager(config).status("research")["state"] == "unavailable")

    # 版本匹配但启动失败
    base_config(root, {
        "kind": "dsh", "enabled": True, "cli": str(fake_cli(root)),
        "version": "0.2.1-rc.2", "profile": "web",
        "homes": {"research": "homes/research"}, "ports": {"research": 3999},
    })
    config = HarnessConfig(root)
    runtime = RuntimeManager(config)
    check("版本匹配：status stopped", runtime.status("research")["state"] == "stopped", runtime.status("research"))
    result = runtime.start("research")
    check("启动失败：返回 failed 不抛异常", result["state"] == "failed", result)

    # Channel 启停
    base_config(root, None)
    raw = json.loads((root / "harness" / "agents.json").read_text(encoding="utf-8"))
    raw["channels"][0]["command"] = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    (root / "harness" / "agents.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    config = HarnessConfig(root)
    channels = ChannelManager(config)
    check("Channel 配置可读", config.channel("qq").listen == "ws://127.0.0.1:3101/ws")
    started = channels.start("qq")
    check("Channel 可启动", started["state"] == "running", started)
    time.sleep(0.3)
    stopped = channels.stop("qq")
    check("Channel 可停止", stopped["state"] == "stopped", stopped)

    # 损坏配置
    (root / "harness" / "agents.json").write_text("{broken", encoding="utf-8")
    broken = HarnessConfig(root)
    check("损坏配置：不抛异常且记录 error", bool(broken.error) and broken.agents == [])

    shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
