"""Owner-console member sync and narrowly selected, model-safe person context."""
import asyncio
import hashlib
import json
import re
import uuid
from pathlib import Path
from storage import read_json
from social_roster import SocialRoster
from group_reply_style import MIN_CHARS, MAX_CHARS


def allowed_group(root, group):
    groups = read_json(Path(root) / 'channels/fish_qq/config.json').get('allowlist', {}).get('group_ids', [])
    if str(group) not in {str(g) for g in groups}:
        raise PermissionError('请选择白名单中的目标群')


def sync_from_channel(root, group):
    """Read-only channel action; never send a QQ message or invoke a model."""
    allowed_group(root, group)
    roster = SocialRoster(root)
    if roster.settings()['group_id'] != str(group):
        raise ValueError('请先保存此群为当前管理群')
    from secrets_store import SecretStore
    from websockets.asyncio.client import connect
    config = read_json(Path(root) / 'channels/fish_qq/config.json')
    token = SecretStore(root).get('channel-core', config.get('listen', {}).get('token_env', 'FISH_QQ_TOKEN'))
    if not token:
        raise ValueError('请先配置 QQ 频道并启动 QQ 角色')
    async def run():
        port = int(config.get('listen', {}).get('port', 3101))
        path = config.get('listen', {}).get('path', '/ws')
        async with connect(f'ws://127.0.0.1:{port}{path}', additional_headers={'Authorization': 'Bearer ' + token},
                           max_size=8*1024*1024, open_timeout=5) as ws:
            await ws.send(json.dumps({'v': 1, 'type': 'hello', 'id': 'roster', 'payload': {'client': 'fish-roster'}}))
            hello = json.loads(await asyncio.wait_for(ws.recv(), 8))
            if hello.get('type') != 'hello.ok': raise PermissionError('QQ 频道身份核验失败')
            action = 'roster-' + uuid.uuid4().hex
            await ws.send(json.dumps({'v': 1, 'type': 'action', 'id': action, 'action': 'get_group_members', 'payload': {'group_id': str(group)}}))
            async def receive():
                async for raw in ws:
                    frame = json.loads(raw)
                    if frame.get('id') == action and frame.get('type') == 'action.result': return frame
                raise ConnectionError('QQ 频道已断开')
            result = await asyncio.wait_for(receive(), 25)
            if not result.get('ok'): raise ValueError(result.get('error', {}).get('message', '同步失败；请重启新版 QQ 频道'))
            payload = result.get('payload', {})
            if payload.get('group_id') != str(group): raise PermissionError('返回的群范围不匹配')
            # Revalidate the whitelist and selected group after an in-flight sync.
            allowed_group(root, group)
            return roster.sync_members(str(group), payload.get('members'))
    return asyncio.run(run())


def participants(payload, snapshot, bot_id=''):
    ids = []
    def add(value):
        value = str(value or '')
        if re.fullmatch(r'[0-9]{5,15}', value) and value != str(bot_id) and value not in ids:
            ids.append(value)
    add((payload.get('sender') or {}).get('id'))
    reply = str(payload.get('reply_to') or '')
    if reply:
        for item in snapshot.get('messages', []):
            if str(item.get('message_id')) == reply: add((item.get('sender') or {}).get('id'))
    for item in payload.get('segments', []):
        if item.get('type') == 'at': add(item.get('user_id'))
    return ids[:4]


def reply_context(root, payload, snapshot, bot_id=''):
    group = str(payload.get('chat_id', '')).removeprefix('group:')
    roster = SocialRoster(root)
    if roster.settings()['group_id'] != group:
        return {'profiles': [], 'facts': [], 'reply_target': '', 'min_chars': MIN_CHARS, 'max_chars': MAX_CHARS}, {'active_probability': 1.0, 'cooldown_multiplier': 1.0, 'max_chars': MAX_CHARS}
    allowed_group(root, group)
    ids = participants(payload, snapshot, bot_id)
    profiles = []
    from memory_store import MemoryStore
    memory = MemoryStore(root, 'qq'); facts = []
    for uid in ids:
        p = roster.public_profile(group, uid)
        p['preferences'] = p['preferences'][:600]; p['aliases'] = p['aliases'][:8]
        profiles.append(p)
        for row in memory.profile('qq:' + uid, scope='chat:group:' + group)[:3]:
            facts.append({'subject': uid, 'text': row['text'][:350], 'source': row['source'][:100]})
    target = str((payload.get('sender') or {}).get('id', ''))
    behavior = roster.behavior(group, target) if re.fullmatch(r'[0-9]{5,15}', target) else {'max_chars': 120, 'active_probability': .2, 'cooldown_multiplier': 2.0}
    # Relationship state still controls proactive participation, never how
    # thoroughly a direct question deserves to be answered.
    behavior = {**behavior, 'max_chars': MAX_CHARS}
    return {'profiles': profiles, 'facts': facts[:8], 'reply_target': target, 'min_chars': MIN_CHARS, 'max_chars': MAX_CHARS}, behavior


def active_admitted(chat, cursor, behavior, last_sent, cooldown, now):
    if last_sent and now - last_sent < cooldown * behavior['cooldown_multiplier']: return False
    value = int.from_bytes(hashlib.sha256(f'{chat}:{cursor}'.encode()).digest()[:8], 'big') / 2**64
    return value < behavior['active_probability']
