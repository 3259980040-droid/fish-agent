"""One bounded real Bilibili cycle with the owner's explicitly chosen directions."""
import hashlib,json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness'),str(ROOT/'vendor/python')]
os.environ['FISH_HARNESS_HOME']=str(ROOT)
from fish_desktop.services import load_user_env
from research_autonomy import ResearchControl,ResearchWorker
from storage import write_json,database
from memory_store import MemoryStore
load_user_env();out=ROOT/'artifacts/round4';control=ResearchControl(ROOT)
before={r['id'] for r in control.snapshot()['jobs']}
worker=ResearchWorker(ROOT);result={'ok':False,'real_bilibili':True,'real_qq_sends':0,'account_write_actions':0,'started_at':time.time()}
try:
    result['discovery']=worker.discover()
    jobs=[j for j in control.snapshot()['jobs'] if j['id'] not in before and j['status']=='queued']
    if not jobs:raise RuntimeError('No new eligible videos; inspect discovery skip and duplicate records')
    exe=ROOT/'dist-round4-delivery/Fish/Fish.exe';sys.frozen=True;sys.executable=str(exe);sys._MEIPASS=str(exe.parent/'_internal')
    result['jobs']=[]
    # Claim only this cycle's candidates, preserving any older pending jobs.
    from datetime import date
    import uuid
    for job in jobs[:2]:
        with database(control.path) as db:
            db.execute('BEGIN IMMEDIATE')
            count=db.execute('SELECT COUNT(*) FROM attempts WHERE day=?',(date.today().isoformat(),)).fetchone()[0]
            if count>=control.config()['daily_limit']:raise PermissionError('Research daily content limit reached')
            changed=db.execute("UPDATE jobs SET status='running',stage='读取视频资料',attempts=attempts+1,lease=?,updated=? WHERE id=? AND status='queued'",(time.time(),time.time(),job['id'])).rowcount
            if not changed:continue
            db.execute('INSERT INTO attempts VALUES(?,?,?)',(uuid.uuid4().hex,date.today().isoformat(),job['id']))
        worker.run_job(job)
        snapshot=next(j for j in control.snapshot()['jobs'] if j['id']==job['id'])
        result['jobs'].append({k:snapshot[k] for k in ('id','bvid','title','origin','status','stage','error')})
        if snapshot['status']=='done':
            detail=control.detail(job['bvid']);report=detail['report']
            result.setdefault('reviews',[]).append({'bvid':job['bvid'],'url':job['url'],'report':report,'observed_characters':len(detail['body'])})
            assert MemoryStore(ROOT,'research').search(job['bvid']) or MemoryStore(ROOT,'research').search(report['title'])
    result['ok']=bool(result.get('reviews')) and all(j['status']=='done' for j in result['jobs'])
    result['settings']={k:control.config()[k] for k in ('directions','keywords','exclude','auto','sources')}
    result['executable_sha256']=hashlib.sha256(exe.read_bytes()).hexdigest()
except Exception as exc:
    result['error']=type(exc).__name__+': '+str(exc).split('?')[0][:500]
finally:
    result['finished_at']=time.time();write_json(out/'bilibili-experience-check.json',result)
    print(json.dumps(result,ensure_ascii=True))
