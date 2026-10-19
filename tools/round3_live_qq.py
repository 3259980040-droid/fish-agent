"""Authorized owner-only photo acceptance using the delivered Fish.exe."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
from fish_desktop.services import load_user_env, tcp_open
from governance import control_token
from secrets_store import SecretStore
from storage import read_json, write_json
from core import root_id
import requests


def main():
    sys.stdout.reconfigure(encoding='utf-8'); load_user_env()
    out=ROOT/'artifacts/round3'; out.mkdir(parents=True,exist_ok=True)
    owner=str(read_json(ROOT/'channels/fish_qq/config.json')['allowlist']['owner_id'])
    env=os.environ.copy();env.update(FISH_HARNESS_HOME=str(ROOT),FISH_QQ_GROUP_REPLY='off')
    for name,ref in [('FISH_QQ_TOKEN','channel-core'),('NAPCAT_TOKEN','channel-napcat')]:
        value=SecretStore(ROOT).get(ref,name)
        if value:env[name]=value
    exe=ROOT/'dist-round2/Fish/Fish.exe'; procs=[]
    with closing(sqlite3.connect(ROOT/'data/qq/qq.db')) as db: baseline=db.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0]
    try:
        for kind in ['fish-qq','qq-agent-loop']:
            args=[str(exe),'--backend',kind]
            if kind=='fish-qq':args+=['--config',str(ROOT/'channels/fish_qq/config.json')]
            with (out/(kind+'-launch.log')).open('a',encoding='utf-8') as log:
                procs.append(subprocess.Popen(args,cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)))
        deadline=time.monotonic()+100
        while time.monotonic()<deadline:
            if any(p.poll() is not None for p in procs):raise RuntimeError('新版后台提前退出，请查看后台日志')
            try:
                r=requests.get('http://127.0.0.1:3090/fish/connect',headers={'x-fish-token':control_token(ROOT)},timeout=2)
                d=r.json()
                if r.ok and d.get('root_id')==root_id(ROOT) and d.get('control_version')==3 and tcp_open('127.0.0.1',3102):break
            except (requests.RequestException,ValueError):pass
            time.sleep(.3)
        else:raise TimeoutError('QQ 角色未在等待时间内就绪')
        write_json(out/'live-qq-ready.json',{'ready':True,'owner_only':True,'group_reply':'off','executable':str(exe),'backend_pids':[p.pid for p in procs],'baseline_id':baseline,'started':time.time()})
        print('READY: owner-only QQ photo test; group replies off.',flush=True)
        deadline=time.monotonic()+900
        while time.monotonic()<deadline:
            with closing(sqlite3.connect(ROOT/'data/qq/qq.db')) as db:
                db.row_factory=sqlite3.Row
                rows=[dict(r) for r in db.execute("SELECT * FROM messages WHERE id>? AND chat_id=? ORDER BY id",(baseline,'private:'+owner))]
            inbound=[r for r in rows if r['direction']=='inbound' and any(s.get('type')=='image' for s in json.loads(r['segments'] or '[]'))]
            if inbound:
                first=inbound[0]
                replies=[r for r in rows if r['direction']=='outbound' and r.get('runtime_turn_id') and r['runtime_turn_id']==first.get('runtime_turn_id')]
                if replies:
                    if first['status'] in ('queued','processing') or replies[0]['status']=='pending':
                        time.sleep(.3);continue
                    media=[{k:s[k] for k in ('type','local_path','preview_path','error') if k in s} for s in json.loads(first['segments'] or '[]')]
                    result={'ok':first['status']=='processed' and replies[0]['status']=='sent','real_owner_private_photo':True,'inbound_id':first['id'],'outbound_id':replies[0]['id'],'inbound_status':first['status'],'outbound_status':replies[0]['status'],'reply':replies[0]['text'],'segments':media,'group_reply':'off','executable':str(exe)}
                    write_json(out/'live-owner-photo-check.json',result)
                    print(json.dumps({k:v for k,v in result.items() if k not in ('reply','segments')},ensure_ascii=False),flush=True);return
                if first['status'] in ('failed','blocked','cancelled'):
                    write_json(out/'live-owner-photo-check.json',{'ok':False,'inbound_id':first['id'],'status':first['status'],'error':first.get('error'),'real_owner_private_photo':True})
                    print('Photo received but processing did not complete; diagnostic saved.',flush=True);return
            time.sleep(1)
        print('Owner photo not observed within this test window.',flush=True)
    except Exception:
        for proc in procs:
            if proc.poll() is None:
                from process_tools import stop_process_tree
                stop_process_tree(proc)
        raise


if __name__=='__main__':main()
