"""One synthetic-image request through the real Research DSH; never sends QQ."""
from __future__ import annotations
import asyncio
import base64
import json
import re
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parent.parent
sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
from core import HarnessConfig, RuntimeManager
from dsh_web import DSHWebClient
from fish_desktop.services import load_user_env, tcp_open

async def run():
    output=ROOT/'artifacts/takeover-20261018';output.mkdir(parents=True,exist_ok=True)
    image=Image.new('RGB',(400,240),'white');draw=ImageDraw.Draw(image)
    draw.rectangle((24,100,180,215),fill='#2266dd')
    font=ImageFont.truetype(r'C:\Windows\Fonts\consolab.ttf',44)
    draw.text((24,25),'FISH 4827',fill='black',font=font)
    path=output/'native-vision-fixture.png';image.save(path)
    load_user_env();manager=RuntimeManager(HarnessConfig(ROOT))
    if tcp_open('127.0.0.1',3080):
        raise RuntimeError('Research runtime already listening; smoke will not replace an active runtime')
    try:
        result=manager.start('research')
        if not result.get('ok'): raise RuntimeError(result.get('reason','runtime start failed'))
        launch=None
        for _ in range(80):
            await asyncio.sleep(.25)
            for line in manager.logs['research']:
                match=re.search(r'dsh web: (http://\S+)',line)
                if match: launch=match.group(1);break
            if launch: break
        if not launch: raise RuntimeError('No launch URL emitted')
        client=DSHWebClient(launch);await client.auth()
        session=await client.create_session();baseline=await client.baseline(session)
        await client.prompt(session,[{'type':'text','text':'这是自动化验收用的合成图片。只回答图片中的字母数字，以及色块的颜色；不要调用工具或扩展任务。'},
            {'type':'image','mediaType':'image/png','data':base64.b64encode(path.read_bytes()).decode(),'name':'vision-test.png'}])
        result=await client.wait_response(session,baseline,90)
        if not result.get('ok'):
            cursor=await client._cursor(session);page=await client._page(session,cursor)
            result['page_event_types']=[(r.get('event') or {}).get('type',r.get('type')) for r in page.get('records',[])]
            result['record_example_keys']=[list(r.keys()) for r in page.get('records',[])[:2]]
            result['event_example_keys']=[list((r.get('event') or {}).keys()) for r in page.get('records',[])[:2]]
        result['fixture_read']=bool(result.get('ok') and '4827' in result.get('text',''))
        (output/'native-vision-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result['fixture_read'] else 1
    finally:
        manager.stop('research')

if __name__=='__main__':
    try: code=asyncio.run(run())
    except Exception as exc:
        print(type(exc).__name__,re.sub(r'https?://\S+','<url>',str(exc)));code=1
    raise SystemExit(code)
