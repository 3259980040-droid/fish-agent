"""Apply the owner's approved complete QQ MCP grant through Tulpa's own API."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'harness'))
import dependencies
import requests
from storage import read_json,write_json,settings_lock
from secrets_store import SecretStore

FLAGS = ('media','prepare','voice','onebot','send','manage','chat','chat_images','chat_sticker_send','chat_sticker_collect',
         'chat_reactions','chat_voice','chat_video','chat_files','qq_messages','qq_interactions','qq_friends','qq_profile','qq_qzone','qq_albums')


def plan(root):
    path = root/'data/integrations/tulpa/app/data/mcp-access.sqlite3'
    route = read_json(root/'data/integrations/tulpa/owner-route.json')
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        row = db.execute('SELECT scope,revoked,revision FROM grants WHERE id=?',(route['grant_id'],)).fetchone()
    if row is None or row[1]: raise ValueError('现有QQ MCP授权不存在或已撤销，没有偷偷换账号')
    scope = json.loads(row[0])
    return {'grant_id':route['grant_id'],'account':scope.get('chat_account'),'all_conversations':not scope['conversations'],
            'missing_flags':[k for k in FLAGS if not scope.get(k)],'revision':row[2],
            'scope_replacement_required':bool(scope['conversations'] or scope.get('start') or scope.get('end'))}


def apply(root,proposed):
    from tulpa_owner import ensure_service
    import os
    state=read_json(root/'data/system/unified-service.json',{})
    if state.get('status') not in ('running','partial'):raise RuntimeError('请先由当前后台承载QQ MCP，避免服务挂在一次性迁移脚本下面')
    os.environ['FISH_LOOP_PARENT_PID']=str(state['pid'])
    ensure_service(root)
    backup = root/'backups/agent-update'/('qq-grant-'+time.strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True,exist_ok=False)
    for relative in ('data/integrations/tulpa/app/data/mcp-access.sqlite3',):
        source = root/relative
        with sqlite3.connect(source) as old,sqlite3.connect(backup/source.name) as new: old.backup(new)
    path = root/'data/integrations/tulpa/owner-route.json'
    route = read_json(path)
    write_json(backup/'owner-route.json',route)
    session = requests.Session();session.trust_env=False
    session.headers.update({'x-chatweave-ui':'1'})
    url = 'http://127.0.0.1:3120/api/mcp/connections'
    if proposed['scope_replacement_required']:
        reply = session.post(url,json={'name':'Fish 自主Agent · 主人完整QQ授权','platforms':['qq'],'all_conversations':True,
                                       **{k:True for k in FLAGS}},timeout=30)
        reply.raise_for_status();created=reply.json()
        SecretStore(root).put('plugin-tulpa_owner-http',created['token'])
        route['grant_id']=created['id']
    elif proposed['missing_flags']:
        reply = session.put(url+'/'+route['grant_id'],json={**{k:True for k in FLAGS},'expected_revision':proposed['revision']},timeout=30)
        reply.raise_for_status()
    for key in ('space_id','persona_revision','revision','runtime_session','test_space_id','test_runtime_session','chat_session_id','conversation_id'):
        route.pop(key,None)
    write_json(path,route)
    return {'backup':str(backup),'current':plan(root),'account_note':'授权接口能力，不声称QQ服务实现所有动作；本脚本不发送消息或动态'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    proposed=plan(ROOT)
    if args.apply:
        with settings_lock(ROOT): result=apply(ROOT,proposed)
    else: result={'plan':proposed,'applied':False}
    print(json.dumps(result,ensure_ascii=False,indent=2))
