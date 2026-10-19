"""Prepare isolated data for native packaged maintenance acceptance; no transport."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
from fish_desktop.bootstrap import initialize
from storage import read_json, write_json
from memory_store import MemoryStore
from desktop_chat import DesktopChat


def main():
    out = ROOT / 'artifacts/round4'
    out.mkdir(exist_ok=True)
    root = out / 'native-data'
    if root.exists():
        raise RuntimeError('Fixture already exists; preserve it for review')
    initialize(root)
    cfg = read_json(root / 'harness/agents.json')
    cfg['runtime']['ports'] = {'qq':3294, 'research':3295}
    cfg['runtime']['tool_host_port'] = 3296
    cfg['channels'][0]['enabled'] = False
    write_json(root / 'harness/agents.json', cfg)
    write_json(root / 'harness/setup.json', {'completed': True, 'fixture': True})
    for role in ('qq', 'research'):
        store = MemoryStore(root, role)
        store.configure(False)
        store.save('第四轮维护基准 ' + role + ' R4-BASE', source='manual', source_text='独立测试数据，不是用户兴趣')
        chat = DesktopChat(root, role, None)
        cid = chat.new('第四轮维护基准 ' + role)
        chat._record(cid, 'inbound', 'R4-BASE-' + role, [], 'done')
    for name in ('qq-work/round4-marker.txt', 'knowledge/round4-marker.txt'):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text('R4-BASE', encoding='utf-8')
    env = os.environ.copy()
    for key in list(env):
        if any(word in key.upper() for word in ('API_KEY', 'TOKEN', 'SECRET', 'PYTHONPATH', 'PYTHONHOME')):
            env.pop(key)
    env.update(FISH_HARNESS_HOME=str(root), LOCALAPPDATA=str(out / 'localappdata'), FISH_QQ_GROUP_REPLY='off')
    exe = ROOT / 'dist-round3/Fish/Fish.exe'
    process = subprocess.Popen([str(exe)], cwd=root, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    result = {'pid':process.pid, 'exe':str(exe), 'root':str(root), 'localappdata':env['LOCALAPPDATA'], 'real_qq_transport':False, 'real_model_calls':0}
    write_json(out / 'native-fixture.json', result)
    progress = read_json(ROOT / 'FISH-PROGRESS.json')
    progress.update(current_round=4, round_status='round 4 in progress', next_action='Native packaged maintenance restart and final daily-use acceptance', open_gui_version='round3 native isolated acceptance')
    progress['rounds']['4'] = 'in progress'
    write_json(ROOT / 'FISH-PROGRESS.json', progress)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
