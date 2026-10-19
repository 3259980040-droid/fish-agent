"""One real DSH turn proving native tool registration, owner binding and usage.
Uses synthetic data in the research role; never connects the QQ transport.
"""
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
from fish_desktop.services import FishServices, tcp_open
from plugin_registry import PluginRegistry
from memory_store import MemoryStore
from model_gateway import ModelLedger


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    if tcp_open('127.0.0.1', 3080):
        raise RuntimeError('研究角色正在使用，本次测试未替换现有进程')
    services = FishServices(ROOT)
    out = ROOT / 'artifacts/round2'; out.mkdir(parents=True, exist_ok=True)
    script = out / 'echo_plugin.py'
    script.write_text("import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({'receipt':'FISH-PLUGIN-REAL-2741','text':x['arguments']['text']}))\n", encoding='utf-8')
    registry = PluginRegistry(ROOT)
    pid = 'round2_echo'
    registry.save({'id': pid, 'name': '第二轮验收回声工具', 'kind': 'cli', 'command': sys.executable, 'args': [str(script)], 'roles': ['research'], 'owner_only': True,
        'tools': [{'name': 'echo', 'description': '返回验收回执，只有执行工具才能取得回执。', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}}]})
    registry.discover(pid); registry.toggle(pid, True)
    chat = services.chat('research'); cid = chat.new(); marker = '第二轮验收偏好-' + uuid.uuid4().hex[:8]
    before = ModelLedger(ROOT, 'research').snapshot()
    try:
        result = chat.send(cid, f'这是主人要求的功能验收。请认真完成两项：先实际调用 fishp__round2_echo__echo，text 参数填“工具验收”；再调用 fish_memory_save，把“{marker}：喜欢蓝色”保存为约定。最后报告两个工具的实际返回值。不要只复述计划。', [])
        answer = next((r['text'] for r in reversed(result['messages']) if r['direction'] == 'outbound'), '')
        rows = MemoryStore(ROOT, 'research').search(marker)
        logged = [e for e in registry.snapshot()['events'] if e['plugin'] == pid and e['action'] == 'echo' and e['ok']]
        evidence = {'ok': bool(rows and logged), 'conversation_id': cid, 'answer': answer,
            'memory_saved': any(marker in r['text'] for r in rows), 'plugin_executed': bool(logged),
            'usage_before': before, 'usage_after': ModelLedger(ROOT, 'research').snapshot(), 'real_qq_sends': 0}
        (out / 'runtime-tools-check.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(evidence, ensure_ascii=False))
        return 0 if evidence['ok'] else 1
    finally:
        services.runtime.stop('research')
        registry.remove(pid)
        for row in MemoryStore(ROOT, 'research').search(marker):
            if marker in row['text']: MemoryStore(ROOT, 'research').delete(row['id'])


if __name__ == '__main__': raise SystemExit(main())
