"""A real MCP image result goes through DSH attachments into DeepSeek vision."""
import base64
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
from PIL import Image, ImageDraw, ImageFont
from fish_desktop.services import FishServices, tcp_open
from plugin_registry import PluginRegistry
from model_gateway import ModelLedger
from memory_store import MemoryStore
from tool_host import shutdown_host


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    if tcp_open('127.0.0.1', 3080): raise RuntimeError('研究角色正在运行，本验收没有替换现有角色')
    out = ROOT / 'artifacts/round2'; out.mkdir(parents=True, exist_ok=True)
    im = Image.new('RGB', (520, 320), 'white'); draw = ImageDraw.Draw(im)
    draw.text((20, 20), 'VISION TOOL 4092', font=ImageFont.truetype(r'C:\Windows\Fonts\consolab.ttf', 40), fill='black')
    draw.ellipse((50, 100, 240, 290), fill='#2464dc'); output = io.BytesIO(); im.save(output, 'PNG'); im.save(out / 'plugin-image-fixture.png')
    encoded = base64.b64encode(output.getvalue()).decode()
    script = out / 'image_mcp_fixture.py'
    script.write_text('import sys,json\nIMAGE=' + repr(encoded) + '''
for line in sys.stdin:
 x=json.loads(line); m=x.get('method'); i=x.get('id')
 if i is None: continue
 if m=='initialize': r={'protocolVersion':x['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'synthetic-image','version':'1'}}
 elif m=='tools/list': r={'tools':[{'name':'analyze','description':'Return a synthetic image for visual inspection','inputSchema':{'type':'object','properties':{},'additionalProperties':False},'annotations':{'readOnlyHint':True}}]}
 elif m=='tools/call': r={'content':[{'type':'image','data':IMAGE,'mimeType':'image/png'}],'isError':False}
 else: r={}
 print(json.dumps({'jsonrpc':'2.0','id':i,'result':r}),flush=True)
''', encoding='utf-8')
    services = FishServices(ROOT); registry = PluginRegistry(ROOT, services.runtime); pid = 'round2_image'
    registry.save({'id': pid, 'kind': 'mcp_stdio', 'name': '图片回传验收', 'command': sys.executable, 'args': [str(script)], 'roles': ['research'], 'owner_only': True})
    registry.discover(pid); registry.toggle(pid, True)
    chat = services.chat('research'); cid = chat.new(); before = ModelLedger(ROOT, 'research').snapshot()
    try:
        with patch.object(MemoryStore, 'enqueue'):
            snapshot = chat.send(cid, '请实际调用 fishp__round2_image__analyze 一次。直接理解它返回的图片，抄录图片上的全部字母和数字，并描述下方图形的形状与颜色。不要根据工具名称猜测，不需要调用其他工具。', [])
        answer = next(r['text'] for r in reversed(snapshot['messages']) if r['direction'] == 'outbound')
        invoked = any(e['plugin'] == pid and e['action'] == 'analyze' and e['ok'] for e in registry.snapshot()['events'])
        result = {'ok': bool(invoked and '4092' in answer and '蓝' in answer and '圆' in answer), 'answer': answer,
            'real_mcp_invocation': invoked, 'image_marker_was_not_in_prompt': True, 'usage_before': before,
            'usage_after': ModelLedger(ROOT, 'research').snapshot(), 'auto_learning_disabled_for_synthetic_fixture': True, 'real_qq_sends': 0}
        (out / 'plugin-image-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False)); return 0 if result['ok'] else 1
    finally:
        services.runtime.stop('research'); registry.remove(pid); shutdown_host(ROOT)


if __name__ == '__main__': raise SystemExit(main())
