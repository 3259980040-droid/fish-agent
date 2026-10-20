"""QQ-space drafts and the real NapCat publishing adapter.

API source: NapNeko/NapCatQQ SendQzoneMsg.ts. No public test post is automatic.
"""
import asyncio
import json
from pathlib import Path
import time
import uuid
from storage import read_json, write_json


def _image_reference(value):
    """NapCat expects local images as file URIs; retain supported remote/data forms."""
    value = str(value).strip()
    if value.startswith(('file:', 'http://', 'https://', 'base64://')):
        return value
    return Path(value).expanduser().resolve().as_uri()


def execute(root, action, params):
    root = Path(root)
    folder = root/'outputs/social/qzone'
    if action == 'qzone_draft':
        text = str(params.get('content', '')).strip()
        if not text: raise ValueError('动态正文为空')
        draft = {'id': uuid.uuid4().hex, 'content': text,
                 'images': [_image_reference(value) for value in params.get('images', [])],
                 'ugc_right': int(params.get('ugc_right', 4)), 'target_uins': params.get('target_uins', []),
                 'state': 'draft', 'created': time.time()}
        if draft['ugc_right'] not in (1,4,16,64,128): raise ValueError('不支持的动态可见范围')
        write_json(folder/(draft['id']+'.json'), draft)
        return {**draft, 'path': str(folder/(draft['id']+'.json')), 'published': False}
    if action != 'qzone_publish': raise ValueError('未知动态操作')
    if not read_json(root/'harness/integration.json', {}).get('qzone_enabled', False):
        raise PermissionError('空间发布开关未开启；草稿仍保留，可在客户端设置中开启')
    draft_id = str(params.get('draft_id', ''))
    if len(draft_id)!=32 or not all(c in '0123456789abcdef' for c in draft_id): raise ValueError('请先创建动态草稿并使用实际编号')
    path = folder/(draft_id+'.json')
    draft = read_json(path, {})
    if not draft: raise ValueError('动态草稿不存在')
    if draft['state'] != 'draft': return draft
    draft.update(state='sending',updated=time.time());write_json(path,draft)
    from owner_files import onebot
    try:
        payload = {k:draft[k] for k in ('content','images','ugc_right','target_uins')}
        payload['images'] = [_image_reference(value) for value in payload['images']]
        result = asyncio.run(onebot(root,'send_qzone_msg',payload))
        tid = (result.get('data') or {}).get('tid')
        state = 'sent' if result.get('status')=='ok' and tid else 'failed' if result.get('status')=='failed' else 'unknown'
    except Exception as exc: state,result='unknown',{'error':type(exc).__name__,'note':'未自动重试，请核对空间动态'}
    draft.update(state=state,receipt=result,updated=time.time());write_json(path,draft)
    return draft
