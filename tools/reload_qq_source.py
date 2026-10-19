"""Drain/replay the QQ worker while leaving its desktop and transport running."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
import dependencies
from websockets.asyncio.client import connect
from fish_desktop.services import http_json,tcp_open
from secrets_store import SecretStore
from storage import write_json


async def reload(parent):
    token=SecretStore(ROOT).get('channel-core','FISH_QQ_TOKEN')
    if not token:raise RuntimeError('QQ channel credential unavailable')
    deadline=time.monotonic()+45
    async with connect('ws://127.0.0.1:3101/ws',additional_headers={'Authorization':'Bearer '+token},max_size=1024*1024) as ws:
        await ws.send(json.dumps({'v':1,'type':'hello','id':'worker-reload-observer','payload':{'client':'fish-update-observer'}}))
        hello=json.loads(await ws.recv())
        if hello.get('type')!='hello.ok':raise RuntimeError('Transport handshake failed')
        epoch=hello['payload']['epoch'];seq=hello['payload']['seq']
        while time.monotonic()<deadline:
            try:
                frame=json.loads(await asyncio.wait_for(ws.recv(),.8))
                if frame.get('type')=='event':seq=max(seq,int(frame['seq']))
                continue
            except asyncio.TimeoutError:pass
            status=http_json('http://127.0.0.1:3102/status',root=ROOT)
            if status and not any(status.get(k) for k in ('active_turn','queue','group_tasks','event_queue','event_busy')):break
        else:raise RuntimeError('QQ stayed busy; worker was not stopped')
        checkpoint=ROOT/'harness/runtime/qq-channel-cursor.json'
        if checkpoint.exists():
            saved=json.loads(checkpoint.read_text(encoding='utf-8'))
            if saved.get('epoch')==epoch:seq=int(saved['seq'])
        write_json(checkpoint,{'epoch':epoch,'seq':seq})
        before=status
        if not http_json('http://127.0.0.1:3102/shutdown',method='POST',timeout=3,root=ROOT):raise RuntimeError('Worker did not accept shutdown')
        for _ in range(60):
            if not tcp_open('127.0.0.1',3102):break
            await asyncio.sleep(.1)
        else:raise RuntimeError('Old worker has not exited; new worker not launched')
        env=os.environ.copy();env.update(FISH_HARNESS_HOME=str(ROOT),FISH_QQ_TOKEN=token,FISH_LOOP_PARENT_PID=str(parent))
        log=(ROOT/'harness/qq-agent-loop.log').open('a',encoding='utf-8')
        process=subprocess.Popen([r'C:\Python313\pythonw.exe',str(ROOT/'launch_fish.py'),'--backend','qq-agent-loop'],
            cwd=str(ROOT),env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        log.close()
        for _ in range(60):
            after=http_json('http://127.0.0.1:3102/status',root=ROOT)
            if after and after.get('language_layer_version')==1 and after.get('bot'):break
            await asyncio.sleep(.25)
        else:raise RuntimeError('New source worker did not become ready')
        result={'ok':True,'worker_pid':process.pid,'desktop_pid':parent,'desktop_restarted':False,
            'transport_restarted':False,'replay_seed':{'epoch':epoch,'seq':seq},'after':after}
        out=ROOT/'artifacts/group-language';out.mkdir(parents=True,exist_ok=True);write_json(out/'activation.json',result)
        print(json.dumps({k:v for k,v in result.items() if k!='after'},ensure_ascii=False))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser();parser.add_argument('--desktop-pid',required=True,type=int)
    asyncio.run(reload(parser.parse_args().desktop_pid))
