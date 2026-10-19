"""Owner-authorized initial roster import while the old Fish transport runs.

Only get_group_member_list is sent to the existing local NapCat; never messages.
The production GUI uses the authenticated Fish channel after the upgrade.
"""
import asyncio
import json
import sys
import uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
import dependencies
from fish_desktop.services import load_user_env
from secrets_store import SecretStore
from social_roster import SocialRoster
from social_members import allowed_group
from storage import read_json,write_json
from websockets.asyncio.client import connect
from urllib.parse import urlparse


async def main():
    load_user_env();cfg=read_json(ROOT/'channels/fish_qq/config.json')
    groups=[str(g) for g in cfg.get('allowlist',{}).get('group_ids',[])]
    if len(groups)!=1:raise ValueError('需要恰好一个已配置白名单群；请在新版成员管理中选择')
    gid=groups[0];url=cfg.get('napcat',{}).get('url','ws://127.0.0.1:3001')
    if urlparse(url).hostname not in ('127.0.0.1','localhost','::1'):raise ValueError('初次导入仅连接本机 NapCat')
    key=SecretStore(ROOT).get('channel-napcat',cfg.get('napcat',{}).get('token_env','NAPCAT_TOKEN'))
    echo='roster-read-'+uuid.uuid4().hex
    async with connect(url,additional_headers={'Authorization':'Bearer '+key} if key else {},open_timeout=5,max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({'action':'get_group_member_list','params':{'group_id':int(gid),'no_cache':True},'echo':echo}))
        async def receive():
            async for raw in ws:
                data=json.loads(raw)
                if data.get('echo')==echo:return data
            raise ConnectionError('NapCat closed')
        result=await asyncio.wait_for(receive(),20)
    if result.get('status')!='ok' or result.get('retcode',0)!=0:raise ValueError('NapCat 未返回成员列表；原档案保留')
    raw=result.get('data')
    if not isinstance(raw,list):raise ValueError('成员列表格式不符')
    members=[{'user_id':str(m['user_id']),'nickname':str(m.get('nickname') or '')[:200],'card':str(m.get('card') or '')[:200]} for m in raw]
    allowed_group(ROOT,gid);roster=SocialRoster(ROOT);settings=roster.settings()
    if settings['group_id'] and settings['group_id']!=gid:raise ValueError('当前管理群不同；请在客户端选择')
    roster.configure(gid,settings['auto_affinity']);counts=roster.sync_members(gid,members)
    evidence={'ok':True,'action':'get_group_member_list','group_id':gid,'counts':counts,'model_calls':0,'qq_messages_sent':0,'member_details_logged':False}
    write_json(ROOT/'artifacts/social-members/initial-roster-sync.json',evidence)
    print(json.dumps(evidence))
if __name__=='__main__':asyncio.run(main())
