"""Deliver real local task files through the existing owner's QQ connection."""
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
    config = read_json(Path(root)/'channels/fish_qq/config.json', {})['napcat']
    name = config.get('token_env', 'NAPCAT_TOKEN')
    token = os.environ.get(name) or SecretStore(root).get('channel-napcat', name)
    echo = 'owner-file-' + uuid.uuid4().hex
    async with connect(config['url'], additional_headers={'Authorization': 'Bearer '+token}, open_timeout=5) as ws:
        await ws.send(json.dumps({'action': action, 'params': params, 'echo': echo}))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 60))
            if frame.get('echo') == echo: return frame


def send_file(root, target, path, request_id):
    root = Path(root)
    from bot_control import BotControl
    target = BotControl(root).validate_target(target)
    ok, reason = send_policy.decide(root, target, action='group_send' if target.startswith('group:') else 'owner_request', context='owner')
    if not ok: raise PermissionError(reason)
    source = Path(path).expanduser().resolve()
    if not source.is_file(): raise ValueError('待发送文件不存在')
    db_path = root/'data/social/owner-control.db'
    with database(db_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS file_sends(id TEXT PRIMARY KEY,target TEXT,path TEXT,state TEXT,receipt TEXT,updated REAL)')
        prior = db.execute('SELECT * FROM file_sends WHERE id=?', (request_id,)).fetchone()
        if prior: return dict(prior)
        db.execute('INSERT INTO file_sends VALUES(?,?,?,?,?,?)', (request_id,target,str(source),'sending','{}',time.time()))
    kind, peer = target.split(':',1)
    params = {'user_id' if kind=='private' else 'group_id': int(peer), 'file': str(source), 'name': source.name}
    try:
        receipt = asyncio.run(onebot(root, 'upload_private_file' if kind=='private' else 'upload_group_file', params))
        state = 'sent' if receipt.get('status')=='ok' and receipt.get('retcode',0)==0 else 'failed'
    except Exception as exc:
        state, receipt = 'unknown', {'error': type(exc).__name__, 'note': '未自动重发，请核对QQ结果'}
    with database(db_path) as db:
        db.execute('UPDATE file_sends SET state=?,receipt=?,updated=? WHERE id=?',
                   (state,json.dumps(receipt,ensure_ascii=False),time.time(),request_id))
    return {'state':state,'target':target,'path':str(source),'name':source.name,'receipt':receipt}
