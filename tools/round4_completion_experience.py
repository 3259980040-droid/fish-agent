"""Completion audit: embedded release code, real models, no real QQ sends."""
import argparse
import asyncio
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/round4/completion-audit'
FOLDER = ROOT / 'dist-round4-audit/Fish'
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT/'vendor/python'))
from PyInstaller.archive.readers import CArchiveReader
pyz = CArchiveReader(str(FOLDER/'Fish.exe')).open_embedded_archive('PYZ.pyz')
owned = {p.stem for p in (ROOT/'harness').glob('*.py')}


class EmbeddedModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if name in pyz.toc and (name.split('.')[0] in owned or name.startswith(('fish_desktop', 'harness', 'channels.fish_qq'))):
            return importlib.util.spec_from_loader(name, self, is_package=pyz.toc[name][0] == 1)
    def create_module(self, spec): return None
    def exec_module(self, module):
        module.__file__ = str(FOLDER/'_internal'/Path(*module.__name__.split('.')).with_suffix('.py'))
        exec(pyz.extract(module.__name__), module.__dict__)


sys.meta_path.insert(0, EmbeddedModules())
sys._MEIPASS = str(FOLDER/'_internal'); sys.frozen = True; sys.executable = str(FOLDER/'Fish.exe')
from storage import read_json, write_json, database
from model_gateway import ModelLedger
from governance import Governance
from core import HarnessConfig, RuntimeManager
from fish_desktop.services import load_user_env
os.environ['FISH_HARNESS_HOME'] = str(ROOT)
load_user_env()


def owner_experience():
    from fish_desktop.bootstrap import initialize
    from secrets_store import SecretStore
    from memory_store import MemoryStore
    from emoji_store import EmojiStore
    from desktop_chat import DesktopChat
    from qq_agent_loop import QQAgentLoop
    from tool_host import shutdown_host
    root = OUT/('owner-data-'+str(time.time_ns()))
    initialize(root)
    cfg = read_json(root/'harness/agents.json')
    cfg['runtime']['ports'] = {'qq':3494, 'research':3495}; cfg['runtime']['tool_host_port'] = 3496
    cfg['channels'][0]['enabled'] = False
    actual = HarnessConfig(ROOT).agent('qq')
    for item in cfg['agents']:
        if item['id'] == 'qq': item.update(model=actual.model, base_url=actual.base_url)
    write_json(root/'harness/agents.json', cfg)
    key = SecretStore(ROOT).get('agent-qq', actual.apiKeyEnv)
    if not key: raise RuntimeError('Protected QQ model credential unavailable')
    SecretStore(root).put('agent-qq', key)
    policy = Governance(root)
    policy.save({'owner_permissions':['status','memory.write'], 'max_model_requests_daily':6}, owner_id='12345678')
    MemoryStore(root,'qq').configure(False); MemoryStore(root,'research').configure(False)
    EmojiStore(root).configure({'auto_collect':False})
    write_json(root/'harness/setup.json', {'completed':True, 'fixture':True})
    ledger = ModelLedger(ROOT,'qq'); reservations = []
    for _ in range(6): reservations.append(ledger.reserve('round4-completion-owner-photo'))
    loop = QQAgentLoop(root)
    loop.conversation = loop.store.get_or_create_conversation('private:12345678','qq')
    loop.channel = Mock(); loop.channel.bot = {'id':'fixture-bot'}
    async def receipt(*args, **kwargs):
        return {'ok':True,'payload':{'message_id':'fixture-receipt-'+str(time.time_ns())}}
    loop.channel.action = AsyncMock(side_effect=receipt)
    result = {'ok':False,'real_qq_sends':0, 'channel':'local mock; no connection to QQ/NapCat', 'checks':{}}
    async def ask(number, text):
        # Use the real inbound dispatcher and owner processing path.
        await loop.on_event({'event':'message.received','payload':{'chat_type':'private','chat_id':'private:12345678',
            'sender':{'id':'12345678'},'message_id':f'completion-{number}','segments':[{'type':'text','text':text}]}})
        item = loop.queue.get_nowait(); await loop._process(item); loop.queue.task_done()
        with loop.store.connect() as db:
            row = db.execute("SELECT text,status,error FROM messages WHERE direction='outbound' ORDER BY id DESC LIMIT 1").fetchone()
        if not row or row['status'] != 'sent': raise AssertionError('Owner flow failed: '+str(dict(row) if row else None))
        return row['text']
    try:
        answer = asyncio.run(ask(1,'我现在账号是不是有部分管理权限？请根据本轮程序核验说明我已启用哪些权限，别只说你看不到。不要执行任何管理操作。'))
        result['answers'] = {'initial_permissions':answer}
        assert all(term in answer for term in ('查看状态','保存记忆'))
        assert not any(term in answer for term in ('没有查权限的接口','无法确认你的权限','看不到权限'))
        result['checks']['owner_natural_question_has_verified_permissions'] = True
        policy.save({'owner_permissions':['status']})
        answer = asyncio.run(ask(2,'我刚修改了权限。只根据本轮清单说明现在可用哪些权限，以及上一轮的保存记忆权限现在还能不能用。不要执行管理操作。'))
        result['answers']['after_permission_change'] = answer
        assert '查看状态' in answer and any(term in answer for term in ('不能','未启用','已关闭','不可用','没有','取消','不再'))
        result['checks']['same_conversation_uses_changed_permissions'] = True
        chat = DesktopChat(root,'qq',loop.runtime); cid = chat.new('0.3.3 默认照片复验')
        snapshot = chat.send(cid,'请读出图片文字、数字，描述图形和背景颜色。',
                             [{'type':'image','path':str(ROOT/'artifacts/round2/plugin-image-fixture.png')}])
        answer = snapshot['messages'][-1]['text']; result['answers']['ordinary_photo'] = answer
        assert '4092' in answer and '蓝' in answer and '白' in answer
        result['checks']['ordinary_send_photo_understood'] = True
        # This attempt must hit the hard quota before any provider call.
        count = ModelLedger(root,'qq').snapshot()['requests']
        assert count > 0
        policy.save({'max_model_requests_daily':count})
        try: chat.send(cid,'这是额度耗尽提示验收，请勿发起模型请求。',[])
        except RuntimeError as exc:
            message = str(exc); assert '上限' in message and 'turn ended' not in message
            result['quota_error'] = message
        else: raise AssertionError('Exhausted quota did not fail')
        assert ModelLedger(root,'qq').snapshot()['requests'] == count
        result['checks']['quota_failure_actionable_and_no_provider_request'] = True
        result['ok'] = len(result['checks']) == 4
        result['fixture_root'] = str(root); result['chat_conversation'] = cid
    finally:
        loop.runtime.stop('qq'); shutdown_host(root)
        path = root/'data/qq/model-usage.db'
        with sqlite3.connect(path) as db: rows = list(db.execute('SELECT status,input_tokens,output_tokens FROM requests ORDER BY created'))
        for index, rid in enumerate(reservations):
            row = rows[index] if index < len(rows) else None
            ledger.finish(rid,{'inputTokens':row[1],'outputTokens':row[2]} if row else {},row[0] if row else 'unused_test_budget_reservation')
        result['model_requests'] = len(rows)
        result['executable_sha256'] = hashlib.sha256((FOLDER/'Fish.exe').read_bytes()).hexdigest()
        result['method'] = 'Embedded final release QQ/desktop code under test Python, actual bundled Node/DSH and provider'
        destination=OUT/'owner-photo-experience.json'
        if destination.exists(): destination.rename(OUT/('owner-photo-experience-attempt-'+str(time.time_ns())+'.json'))
        write_json(destination,result)
        print(json.dumps(result,ensure_ascii=True),flush=True)


def video_experience(recorded=False):
    from research_autonomy import ResearchControl,ResearchWorker
    from memory_store import MemoryStore
    from datetime import date
    import uuid
    control=ResearchControl(ROOT); bvid='BV1pRn36oERL'
    result={'ok':False,'bvid':bvid,'real_bilibili':True,'real_qq_sends':0,'account_write_actions':0}
    try:
        with database(control.path) as db:
            db.execute('BEGIN IMMEDIATE')
            count=db.execute('SELECT COUNT(*) FROM attempts WHERE day=?',(date.today().isoformat(),)).fetchone()[0]
            if count>=control.config()['daily_limit']: raise PermissionError('Research daily limit reached')
            row=db.execute('SELECT * FROM jobs WHERE bvid=?',(bvid,)).fetchone()
            if not row or row['status']=='running': raise RuntimeError('Research candidate unavailable or already running')
            job=dict(row)
            # Atomic, bounded retry of this existing owner-approved candidate.
            db.execute("UPDATE jobs SET status='running',stage='重试音乐字幕的视频画面理解',attempts=attempts+1,lease=?,updated=? WHERE id=?",(time.time(),time.time(),job['id']))
            db.execute('INSERT INTO attempts VALUES(?,?,?)',(uuid.uuid4().hex,date.today().isoformat(),job['id']))
        result['before_report']=control.detail(bvid)['report']
        ResearchWorker(ROOT).run_job(job)
        finished=next(j for j in control.snapshot()['jobs'] if j['id']==job['id'])
        result['job']={k:finished[k] for k in ('id','status','stage','error','attempts')}
        if finished['status']!='done': raise AssertionError('Video retry did not finish: '+finished['error'])
        detail=control.detail(bvid);result['report']=detail['report']
        assert detail['report']['observation_level'] in ('subtitle_read','video_sampled')
        memories=[r for r in MemoryStore(ROOT,'research').search(bvid,limit=200) if r['source']==job['url']]
        assert len(memories)==1 and memories[0]['text']==detail['report']['summary']
        result['updated_active_memory_count']=len(memories)
        result['ok']=True
    finally:
        result['executable_sha256']=hashlib.sha256((FOLDER/'Fish.exe').read_bytes()).hexdigest()
        write_json(OUT/'bilibili-video-fallback.json',result)
        print(json.dumps(result,ensure_ascii=True),flush=True)


def recorded_video_experience():
    """Replay the verified cue condition; download/analyze the actual Bili video."""
    from fish_desktop.bootstrap import initialize
    from secrets_store import SecretStore
    from bilibili_source import BilibiliSourceAdapter
    from research_autonomy import ResearchControl,ResearchWorker
    from research_store import ResearchStore
    from memory_store import MemoryStore
    from tool_host import shutdown_host
    root=OUT/('video-data-'+str(time.time_ns()));initialize(root)
    cfg=read_json(root/'harness/agents.json');actual=HarnessConfig(ROOT).agent('research')
    for item in cfg['agents']:
        if item['id']=='research':item.update(model=actual.model,base_url=actual.base_url)
    cfg['runtime']['ports']={'qq':3594,'research':3595};cfg['runtime']['tool_host_port']=3596
    write_json(root/'harness/agents.json',cfg)
    SecretStore(root).put('agent-research',SecretStore(ROOT).get('agent-research',actual.apiKeyEnv))
    Governance(root,'research').save({'max_model_requests_daily':3})
    MemoryStore(root,'research').configure(False)
    control=ResearchControl(root);current=ResearchControl(ROOT).config()
    control.configure({k:current[k] for k in ('directions','keywords','exclude')})
    adapter=BilibiliSourceAdapter(ROOT);bvid='BV1pRn36oERL';video=adapter.get_video(bvid)
    # Use exactly the original observation stored before this audit, not invented text.
    original=ResearchStore(ROOT)
    with original.connect() as db:
        old=db.execute("SELECT o.notes FROM observations o JOIN sources s ON s.id=o.source_id WHERE s.bvid=? AND o.modality='subtitle' ORDER BY o.id LIMIT 1",(bvid,)).fetchone()['notes']
    from research_pipeline import has_substantive_subtitles
    assert not has_substantive_subtitles(old)
    adapter.get_subtitle=lambda *args:{'text':old,'recorded_condition':True}
    store=ResearchStore(root);sid,_=store.upsert_source(video,0);store.record_observation(sid,'subtitle_read','subtitle',old,[])
    control.enqueue(video);job=control.claim();ledger=ModelLedger(ROOT,'research')
    reservations=[ledger.reserve('round4-recorded-cue-real-video') for _ in range(3)]
    result={'ok':False,'real_bilibili_download':True,'subtitle_condition':'replayed original stored music-only observation',
            'all_live_online_cycle':False,'real_provider':True,'real_qq_sends':0,'account_write_actions':0,'fixture_root':str(root)}
    try:
        processed=ResearchWorker(root,adapter=adapter).process(job)
        if not processed.get('ok'): raise AssertionError('Video processing failed: '+str(processed.get('error')))
        detail=control.detail(bvid);result['report']=detail['report']
        assert detail['report']['observation_level']=='video_sampled'
        assert MemoryStore(root,'research').search(bvid)
        result['ok']=True
    finally:
        with sqlite3.connect(root/'data/research/model-usage.db') as db:rows=list(db.execute('SELECT status,input_tokens,output_tokens FROM requests ORDER BY created'))
        for index,rid in enumerate(reservations):
            row=rows[index] if index<len(rows) else None
            ledger.finish(rid,{'inputTokens':row[1],'outputTokens':row[2]} if row else {},row[0] if row else 'unused_test_budget_reservation')
        result['model_requests']=len(rows)
        result['executable_sha256']=hashlib.sha256((FOLDER/'Fish.exe').read_bytes()).hexdigest()
        result['method']='Embedded final release research code; recorded subtitle condition, actual Bili metadata/download and bundled extractor/provider'
        write_json(OUT/'recorded-cue-real-video.json',result);shutdown_host(root)
        print(json.dumps(result,ensure_ascii=True),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('owner','video','recorded-video'));args=parser.parse_args()
    {'owner':owner_experience,'video':video_experience,'recorded-video':recorded_video_experience}[args.mode]()
