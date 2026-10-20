"""Open Fish as a native workspace inside the existing DSH Electron client."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'harness'), str(ROOT / 'vendor/python')]


def main():
    from tool_host import request
    config = json.loads((ROOT / 'harness/integration.json').read_text(encoding='utf-8'))
    dsh = Path(config['dsh_root'])
    desktop = dsh / 'apps/desktop'
    env = os.environ.copy()
    env.update(FISH_HARNESS_HOME=str(ROOT),
               PYTHONUTF8='1', PYTHONNOUSERSITE='1',
               PYTHONPATH=str(ROOT / 'vendor/python'),
               FISH_NATIVE_UI_ENTRY=str(Path(config['native_ui_package']) / 'lib/index.js'),
               DSH_HOME=config['desktop_home'],
               DSH_DESKTOP_OPEN_DEVTOOLS='0',
               DSH_DESKTOP_PRIMARY_RUNTIME_DIR=str(desktop / '.desktop-build/targets/win-x64/runtime/primary-runtime'))
    env.pop('ELECTRON_RUN_AS_NODE', None)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    logs = ROOT / 'data/system/logs'
    logs.mkdir(parents=True, exist_ok=True)
    try:
        health = request(ROOT, '/health', timeout=1)
    except Exception:
        health = None
    if health and not health.get('workbench_version'):
        raise RuntimeError('旧后台仍在运行，请先结束旧版 Fish 后再打开统一客户端')
    if not health:
        with (logs / 'backend.log').open('a', encoding='utf-8') as output:
            subprocess.Popen([config.get('python_executable',sys.executable), str(ROOT / 'harness/unified_service.py')],
                             cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
                             creationflags=flags)
        for _ in range(60):
            try:
                if request(ROOT, '/health', timeout=1).get('workbench_version'):
                    break
            except Exception:
                pass
            time.sleep(.25)
        else:
            raise RuntimeError('Fish 后台启动失败，请查看 data/system/logs/backend.log')
    electron_package = desktop / 'node_modules/electron'
    electron = electron_package / 'dist' / (electron_package / 'path.txt').read_text().strip()
    if not electron.is_file() or not (desktop / 'lib/main.js').is_file():
        raise RuntimeError('DSH 原生桌面运行文件不完整，请检查 integration.json 中的 dsh_root')
    with (logs / 'desktop.log').open('a', encoding='utf-8') as output:
        subprocess.Popen([str(electron), '--user-data-dir=' + str(desktop / '.desktop-build/development/electron-user-data'), str(desktop)], cwd=desktop, env=env,
                         stdout=output, stderr=subprocess.STDOUT, creationflags=flags)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
