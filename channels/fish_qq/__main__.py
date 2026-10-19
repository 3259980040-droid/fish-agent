"""python -m channels.fish_qq --config channels/fish_qq/config.json"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from .config import FishQQConfig
from .server import FishQQApp
from .spool import LockHeld


async def _run(config: FishQQConfig) -> int:
    app = FishQQApp(config)
    loop = asyncio.get_running_loop()

    def request_stop(*_args) -> None:
        if not app.stopped_event.is_set():
            loop.create_task(app.stop())

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGTERM)):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_a: request_stop())

    try:
        await app.start()
    except LockHeld as exc:
        print(f"fish-qq: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"fish-qq: 启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    await app.stopped_event.wait()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="fish-qq Channel Adapter")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    args = parser.parse_args()
    root = Path(os.environ.get("FISH_HARNESS_HOME", str(Path(__file__).resolve().parents[2])))
    try:
        config = FishQQConfig.from_file(Path(args.config), root)
    except Exception as exc:
        print(f"fish-qq: 配置错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(_run(config))


if __name__ == "__main__":
    raise SystemExit(main())
