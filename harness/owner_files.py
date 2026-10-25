"""Deliver real local task files through the existing owner's QQ connection."""
from locations import project_path
import asyncio
import json
import os
from pathlib import Path
import time
import uuid

from storage import database, read_json
from governance import Governance
from secrets_store import SecretStore
import send_policy


async def onebot(root, action, params):
    from websockets.asyncio.client import connect
    config = read_json(project_path(Path(root), 'channels/fish_qq/config.json'), {})['napcat']
    name = config.get('token_env', 'NAPCAT_TOKEN')
    token = os.environ.get(name) or SecretStore(root).get('channel-napcat', name)
    echo = 'owner-file-' + uuid.uuid4().hex
    async with connect(config['url'], additional_headers={'Authorization': 'Bearer '+token}, open_timeout=5) as ws:
        await ws.send(json.dumps({'action': action, 'params': params, 'echo': echo}))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 60))
            if frame.get('echo') == echo: return frame


def send_file(root, target, path, request_id, *, session=''):
    root = Path(root)
    from bot_control import BotControl
    target = BotControl(root).validate_target(target)
    ok, reason = send_policy.decide(root, target, action='group_send' if target.startswith('group:') else 'owner_request', context='owner')
    if not ok: raise PermissionError(reason)
    source = Path(path).expanduser().resolve()
    if not source.is_file(): raise ValueError('待发送文件不存在')
    db_path = project_path(root, 'data/social/owner-control.db')
    with database(db_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS file_sends(id TEXT PRIMARY KEY,target TEXT,path TEXT,state TEXT,receipt TEXT,updated REAL)')
        prior = db.execute('SELECT * FROM file_sends WHERE id=?', (request_id,)).fetchone()
        if prior: return dict(prior)
        db.execute('INSERT INTO file_sends VALUES(?,?,?,?,?,?)', (request_id,target,str(source),'sending','{}',time.time()))
    kind, peer = target.split(':',1)
    params = {'user_id' if kind=='private' else 'group_id': int(peer), 'file': str(source), 'name': source.name}
    from qq_action_record import begin,finish
    record = begin(root,'upload_private_file' if kind=='private' else 'upload_group_file',params,session)
    async def upload():
        history_action = 'get_friend_msg_history' if kind=='private' else 'get_group_msg_history'
        history_args = {'user_id' if kind=='private' else 'group_id':int(peer),'count':20}
        def messages(frame):
            data = frame.get('data') or {}
            return data.get('messages',[]) if isinstance(data,dict) else []
        try:
            before = {str(m.get('message_id')) for m in messages(await onebot(root,history_action,history_args))}
        except Exception:
            before = None
        receipt = await onebot(root,'upload_private_file' if kind=='private' else 'upload_group_file',params)
        if receipt.get('status')!='ok' or receipt.get('retcode',0)!=0:
            return 'failed',receipt
        data = receipt.get('data') or {}
        if isinstance(data,dict) and data.get('message_id'):
            return 'sent',receipt
        # An accepted upload is not a delivered chat message. Read back once;
        # never repeat the upload when confirmation is unavailable.
        if before is not None:
            await asyncio.sleep(1)
            try:
                for message in messages(await onebot(root,history_action,history_args)):
                    if str(message.get('message_id')) in before:
                        continue
                    for segment in message.get('message') or []:
                        file = segment.get('data') or {}
                        returned_name = str(file.get('name') or file.get('file_name') or file.get('file') or '')
                        if segment.get('type')=='file' and returned_name==source.name:
                            receipt['readback'] = {'message_id':message.get('message_id'),'file':file}
                            return 'sent',receipt
            except Exception as exc:
                receipt['readback_error'] = type(exc).__name__
        receipt['note'] = 'QQ接受上传请求，但未取得消息送达凭据；未自动重发'
        return 'unknown',receipt
    try:
        state,receipt = asyncio.run(upload())
    except Exception as exc:
        state, receipt = 'unknown', {'error': type(exc).__name__, 'note': '未自动重发，请核对QQ结果'}
    finish(record,receipt,error=receipt.get('note') or receipt.get('error'))
    with database(db_path) as db:
        db.execute('UPDATE file_sends SET state=?,receipt=?,updated=? WHERE id=?',
                   (state,json.dumps(receipt,ensure_ascii=False),time.time(),request_id))
    return {'state':state,'target':target,'path':str(source),'name':source.name,'receipt':receipt}
