"""Owner-console member sync and narrowly selected, model-safe person context."""
from locations import project_path
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
    groups = read_json(project_path(Path(root), 'channels/fish_qq/config.json')).get('allowlist', {}).get('group_ids', [])
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
    config = read_json(project_path(Path(root), 'channels/fish_qq/config.json'))
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


def participants(payload, snapshot, bot_id='', reference=None):
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
    if reference:
        add((reference.get('author') or {}).get('id'))
        add(reference.get('reply_target_id'))
    for item in payload.get('segments', []):
        if item.get('type') == 'at': add(item.get('user_id'))
    for item in reversed(snapshot.get('messages', [])[-12:]):
        add((item.get('sender') or {}).get('id'))
    return ids[:8]


def reply_context(root, payload, snapshot, bot_id='', reference=None):
    group = str(payload.get('chat_id', '')).removeprefix('group:')
    roster = SocialRoster(root)
    managed = roster.settings()['group_id'] == group
    allowed_group(root, group)
    ids = participants(payload, snapshot, bot_id, reference)
    profiles = []
    from memory_store import MemoryStore
    memory = MemoryStore(root, 'qq'); facts = []
    for uid in ids:
        p = roster.public_profile(group, uid) if managed else {'user_id': uid, 'nickname': '', 'card': '', 'aliases': [], 'preferences': ''}
        p['preferences'] = p['preferences'][:600]; p['aliases'] = p['aliases'][:8]
        # Real message names override an older roster snapshot. QQ ID remains
        # the key even when a card changes or another member uses the same name.
        observed = [reference.get('author') or {}] if reference else []
        observed.extend(m.get('sender') or {} for m in snapshot.get('messages', []))
        observed.append(payload.get('sender') or {})
        live = next((s for s in reversed(observed) if str(s.get('id')) == uid), {})
        for key in ('nickname', 'card'):
            if key in live: p[key] = str(live[key])[:80]
        p['display_name'] = str(live.get('display_name') or live.get('name') or p['card'] or p['nickname'])
        p['group_id'] = group
        profiles.append(p)
        for row in memory.profile('qq:' + uid, scope='chat:group:' + group)[:3]:
            facts.append({'subject': uid, 'text': row['text'][:350], 'source': row['source'][:100]})
    target = str((payload.get('sender') or {}).get('id', ''))
    behavior = roster.behavior(group, target) if managed and re.fullmatch(r'[0-9]{5,15}', target) else {'max_chars': MAX_CHARS, 'active_probability': 1.0, 'cooldown_multiplier': 1.0}
    # Relationship state still controls proactive participation, never how
    # thoroughly a direct question deserves to be answered.
    behavior = {**behavior, 'max_chars': MAX_CHARS}
    names = {}
    for p in profiles:
        for name in {p['display_name'], p['nickname'], p['card'], *p['aliases']} - {''}:
            names.setdefault(name, []).append(p['user_id'])
    collisions = {name: sorted(set(users)) for name, users in names.items() if len(set(users)) > 1}
    return {'profiles': profiles, 'facts': facts[:8], 'reply_target': target, 'quoted_message': reference,
            'ambiguous_names': collisions, 'min_chars': MIN_CHARS, 'max_chars': MAX_CHARS}, behavior


def active_admitted(chat, cursor, behavior, last_sent, cooldown, now):
    if last_sent and now - last_sent < cooldown * behavior['cooldown_multiplier']: return False
    value = int.from_bytes(hashlib.sha256(f'{chat}:{cursor}'.encode()).digest()[:8], 'big') / 2**64
    return value < behavior['active_probability']
