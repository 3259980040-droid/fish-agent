"""Background owner services for the native DSH Fish workspace; no Qt UI."""
from pathlib import Path
import argparse
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]


def _stop():
    """Ask the running loopback service to stop without importing its runtime deps."""
    import urllib.request
    try:
        config = json.loads((ROOT / 'harness/agents.json').read_text(encoding='utf-8'))
        port = int(config.get('runtime', {}).get('tool_host_port', 3103))
        token = (ROOT / 'harness/runtime/control.token').read_text(encoding='utf-8').strip()
        request = urllib.request.Request(
            f'http://127.0.0.1:{port}/system/stop', data=b'{}', method='POST',
            headers={'x-fish-token': token, 'x-fish-role': 'qq'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            return 0 if response.status == 200 else 1
    except Exception as exc:
        print(f'停止后台失败：{exc}', file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    if args.stop:
        return _stop()
    from fish_desktop.services import FishServices
    from tool_host import ToolHost, request
    from storage import write_json
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
        state.update(status='stopping')
        write_json(state_path,state)
        try:
            services.shutdown()
        finally:
            if getattr(host, '_workbench', None):
                host._workbench.close()
            if getattr(host,'scheduler_thread',None):
                host.scheduler_thread.join()
            host.computer.interaction.close()
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
