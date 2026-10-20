"""Background owner services for the native DSH Fish workspace; no Qt UI."""
from pathlib import Path
import argparse
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]


def main():
    from fish_desktop.services import FishServices
    from tool_host import ToolHost, request
    from storage import write_json
    parser = argparse.ArgumentParser()
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    if args.stop:
        request(ROOT, '/system/stop')
        return 0
    try:
        health = request(ROOT, '/health', timeout=1)
    except Exception:
        health = None
    if health:
        raise RuntimeError('已有后台正在运行；请通过客户端重载后台后重试')
    services = FishServices(ROOT)
    host = ToolHost(ROOT)
    host.serve()
    state_path = ROOT / 'data/system/unified-service.json'
    state = {'pid': os.getpid(), 'started': time.time(), 'status': 'starting', 'version': 1}
    write_json(state_path, state)
    try:
        result = services.start_fish()
        state.update(status='running' if result.get('ok') else 'partial', qq=result)
        write_json(state_path, state)
        while not host.stop.wait(2):
            pass
    finally:
        host.stop.set()
        services.shutdown()
        if getattr(host, '_workbench', None):
            host._workbench.close()
        host.server.shutdown()
        host.server.server_close()
        state.update(status='stopped', stopped=time.time())
        write_json(state_path, state)
    return 0


if __name__ == '__main__':
    if sys.stdout is None or sys.stderr is None:
        log_path = ROOT / 'data/system/logs/backend.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = log_path.open('a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = stream
    raise SystemExit(main())
