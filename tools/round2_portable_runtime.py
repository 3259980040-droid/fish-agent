"""Start the bundled Node/DSH from an empty private Fish data directory."""
import json
import os
from pathlib import Path
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
from fish_desktop.bootstrap import initialize
from core import HarnessConfig, RuntimeManager
from storage import read_json, write_json
from runtime_adapter import DSHRuntimeAdapter
from tool_host import shutdown_host


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    out = ROOT / 'artifacts/round2'; out.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='portable-runtime-', dir=out)
    root = Path(temporary.name).resolve()
    manager = None
    try:
        initialize(root)
        cfg = read_json(root / 'harness/agents.json')
        cfg['runtime']['ports'] = {'qq': 3191, 'research': 3192}; cfg['runtime']['tool_host_port'] = 3193
        write_json(root / 'harness/agents.json', cfg)
        manager = RuntimeManager(HarnessConfig(root))
        client = DSHRuntimeAdapter(root, 'research', manager).connect()
        result = {'ok': client.origin == 'http://127.0.0.1:3192', 'new_data_directory': True,
            'bundled_cli': cfg['runtime']['cli'], 'bundled_node': cfg['runtime']['node'],
            'runtime_version': manager._version(), 'control_plugin_created': (root / 'harness/runtime/fish-control/lib/index.js').is_file(),
            'model_calls': 0, 'real_qq_sends': 0}
        (out / 'portable-runtime-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['ok'] else 1
    except Exception as exc:
        if manager:
            logs = manager.logs.get('research', [])
            (out / 'portable-runtime-errors.txt').write_text('\n'.join(logs), encoding='utf-8')
        print(type(exc).__name__, str(exc)[:500])
        return 1
    finally:
        if manager: manager.stop_all()
        shutdown_host(root)
        if root.is_relative_to(out.resolve()): temporary.cleanup()


if __name__ == '__main__': raise SystemExit(main())
