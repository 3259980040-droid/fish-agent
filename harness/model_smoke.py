"""每个 Agent 跑一次真实模型调用（headless，不启动 web）。

用法：
  $env:FISH_RESEARCH_DEEPSEEK_API_KEY = "..."
  $env:FISH_QQ_DEEPSEEK_API_KEY = "..."
  python harness/model_smoke.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from core import HarnessConfig, RuntimeManager

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    config = HarnessConfig()
    runtime = RuntimeManager(config)
    missing = []

    for agent in config.agents:
        key = os.environ.get(agent.apiKeyEnv) if agent.apiKeyEnv else None
        if not key:
            print(f"{agent.id}: SKIP，未设置 {agent.apiKeyEnv or 'apiKeyEnv'}")
            missing.append(agent.id)
            continue
        home = config.runtime_home(agent.id)
        if home is None:
            print(f"{agent.id}: SKIP，runtime.homes 未配置")
            missing.append(agent.id)
            continue
        overlay = runtime.write_overlay(agent.id)
        projected = runtime.personas.project(agent.id)
        if not projected["ok"]:
            print(f"{agent.id}: 投影失败：{projected.get('reason')}")
        cmd = runtime._cmd([
            "--profile", "headless",
            "--patch", str(overlay),
            "Reply with exactly one word: OK",
        ])
        if cmd is None:
            print(f"{agent.id}: SKIP，找不到 Runtime CLI")
            missing.append(agent.id)
            continue
        env = os.environ.copy()
        env["DSH_HOME"] = str(home)
        env["DEEPSEEK_API_KEY"] = key
        env["FISH_AGENT_ID"] = agent.id
        print(f"{agent.id}: 调用中…")
        try:
            result = subprocess.run(
                cmd, cwd=str(config.root), env=env, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=180,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{agent.id}: FAIL {type(exc).__name__}: {exc}")
            missing.append(agent.id)
            continue
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        print(f"{agent.id}: exit={result.returncode}")
        if out:
            print(f"  stdout: {out[-500:]}")
        if err:
            print(f"  stderr: {err[-500:]}")
        if result.returncode != 0:
            missing.append(agent.id)

    if missing:
        print(f"\n未完成：{', '.join(missing)}")
        return 2
    print("\nALL MODEL CALLS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
