"""One sending boundary for every Fish-authored outbound: owner reply, timer result, manual send.

The channel (channels/fish_qq/server.py) already enforces the hard floor: owner
private or a whitelisted group, no arbitrary recipients, output guard rules.
``channel_allows`` mirrors exactly those checks so callers fail fast with a
readable reason, and the channel stays the final gate. This module adds the
owner-editable *sending settings* that sit between "who may receive" and
"what was sent", acting per destination and per action:

* ``owner_reply``               -- program's own final reply in the owner's private chat
* ``owner_private.scheduled``  -- a scheduled task result sent to ``private:<owner>``
* ``group_reply``              -- public (non-owner) auto-participation in a whitelisted group
* ``group_owner_request``      -- owner's explicit one-off / scheduled send to a group

Defaults: owner reply and owner private scheduled result allowed; group relay
only on the owner's explicit request; public auto-reply off and not exposed as
a setting here (it stays in GroupPolicy listen/reply).

Supported transport: QQ text segments on the fish-qq WebSocket channel only
(``send_message`` -> NapCat ``send_private_msg`` / ``send_group_msg``). There
is no configured Qzone / Moment / mass-send transport, so those are reported
unsupported rather than emulated.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DISPLAY_ZONE = 'Asia/Shanghai'
ACTIONS = ('owner_reply', 'group_reply', 'group_send', 'owner_private_send')
BUCKETS = ('owner_private', 'group')
SETTABLE = ('owner_reply', 'group_owner_request')
DEFAULT_SEND_POLICY = {
    'version': 1,
    'owner_private': {'owner_reply': True, 'scheduled_result': True},
    'group': {'owner_request': True, 'public_auto_reply': False},
    'require_explicit_owner': True,
}
CAPABILITIES = (
    {'action': 'owner_reply', 'destination': 'owner_private', 'supported': True,
     'default': 'allowed', 'reason': '主人在程序核验的私聊里等待回复；未授予此权限时主人主动询问也不会收到答案'},
    {'action': 'scheduled_result', 'destination': 'owner_private', 'supported': True,
     'default': 'allowed', 'reason': '主人创建定时任务即是对结果回执的明确请求'},
    {'action': 'owner_request', 'destination': 'group', 'supported': True,
     'default': 'allowed_when_explicit', 'reason': '仅当主人在当前会话明确要求发往该白名单群；公开群自动插话仍由独立群开关控制且默认关闭'},
    {'action': 'public_auto_reply', 'destination': 'group', 'supported': False,
     'default': 'off', 'reason': '公开群自动回复属于群参与策略（监听/回复开关默认关闭），不在发送设置中开放，避免绕过群开关'},
    {'action': 'qzone_publish', 'destination': 'qzone', 'supported': False,
     'default': 'off', 'reason': '当前运行时没有 QQ 空间/说说发布接口（fish-qq 通道只实现私聊与群消息），不伪造发送'},
    {'action': 'mass_send', 'destination': 'any', 'supported': False,
     'default': 'off', 'reason': '通道按白名单收件人发送，不支持批量群发或陌生人私聊'},
)


def normalize_policy(raw) -> dict:
    """Accept ``send_policy`` or a whole governance config; fill unknown/old keys with defaults."""
    if isinstance(raw, dict) and isinstance(raw.get('send_policy'), dict):
        raw = raw['send_policy']
    raw = raw if isinstance(raw, dict) else {}
    result = {'version': DEFAULT_SEND_POLICY['version']}
    owner = raw.get('owner_private') if isinstance(raw.get('owner_private'), dict) else {}
    group = raw.get('group') if isinstance(raw.get('group'), dict) else {}
    new_owner = {k: owner.get(k, d) for k, d in DEFAULT_SEND_POLICY['owner_private'].items()}
    new_group = dict(DEFAULT_SEND_POLICY['group'])
    new_group['owner_request'] = group.get('owner_request', group.get('owner_request_allowed', True))
    for k in DEFAULT_SEND_POLICY['owner_private']:
        new_owner[k] = new_owner[k] is not False
    new_group['owner_request'] = new_group['owner_request'] is not False
    new_group['public_auto_reply'] = False
    result.update(owner_private=new_owner, group=new_group, require_explicit_owner=True)
    return result


def validate_updates(updates) -> dict:
    """Validate a partial ``send_policy`` update; raise ValueError in Chinese for the UI."""
    if not isinstance(updates, dict):
        raise ValueError('发送设置必须是对象')
    unknown = set(updates) - set(DEFAULT_SEND_POLICY)
    if unknown:
        raise ValueError('未知发送设置项：' + '、'.join(sorted(unknown)))
    result = {}
    for bucket in ('owner_private', 'group'):
        if bucket not in updates:
            continue
        values = updates[bucket]
        if not isinstance(values, dict):
            raise ValueError('发送设置 ' + bucket + ' 必须按目标分项设置')
        allowed = set(DEFAULT_SEND_POLICY[bucket])
        bad = set(values) - allowed
        if bad:
            raise ValueError('未知发送设置项：' + '、'.join(sorted(bad)))
        for key, value in values.items():
            if key == 'public_auto_reply':
                raise ValueError('公开群自动回复请在群聊设置中控制，发送设置不开放此项')
            if type(value) is not bool:
                raise ValueError('发送设置开关必须为开启或关闭')
        result[bucket] = dict(values)
    if 'require_explicit_owner' in updates:
        if updates['require_explicit_owner'] is not True:
            raise ValueError('跨群和陌生目标必须由主人明确要求，不能关闭')
    return result


def owner_private_chat(root) -> str:
    from governance import Governance
    return 'private:' + Governance(root).owner_id()


def channel_allows(root, chat_id: str) -> tuple[bool, str]:
    """Mirror channels/fish_qq server ``_check_delivery`` recipient checks."""
    try:
        from storage import read_json
        allow = read_json(Path(root) / 'channels/fish_qq/config.json', {}).get('allowlist', {})
        owner = owner_private_chat(root)
        if chat_id == owner:
            return True, ''
        if str(chat_id).startswith('group:') and str(chat_id)[6:] in [str(g) for g in allow.get('group_ids', [])]:
            return True, ''
    except (OSError, ValueError):
        return False, '频道配置不可读'
    return False, '目标不是程序核验的主人私聊或白名单群，频道会拒绝发送'


def policy_check(root, chat_id: str, *, action: str, scheduled: bool = False, context: str = '') -> tuple[bool, str]:
    """Policy decision for one Fish outbound (not a transport). Returns (allowed, reason)."""
    from governance import Governance
    cfg = Governance(root).config()
    if cfg.get('paused'):
        return False, '主人已暂停'
    policy = cfg['send_policy']
    owner = owner_private_chat(root)
    if chat_id == owner:
        if action == 'owner_reply':
            ok = policy['owner_private']['owner_reply']
            return (True, '') if ok else (False, '发送设置关闭了主人私聊回复（定时结果与主人明确交办通知不受影响）')
        if action == 'scheduled_result' and scheduled:
            ok = policy['owner_private']['scheduled_result']
            return (True, '') if ok else (False, '发送设置关闭了主人私聊定时结果')
        return True, ''
    if str(chat_id).startswith('group:'):
        if action in ('group_send', 'scheduled_result'):
            if not policy['group']['owner_request']:
                return False, '发送设置关闭了主人在群聊的明确交办发送'
            if not (str(context).startswith('owner') or str(context).startswith('control:')
                    or (str(context).startswith('chat:') and not str(context).startswith('chat:group:'))):
                return False, '跨群发送只接受主人在当前会话的明确要求'
            return True, ''
        return False, '公开群自动回复不走此发送边界；请在群聊设置中控制'
    return False, '不支持的目标'


def decide(root, chat_id: str, *, action: str, scheduled: bool = False, context: str = '') -> tuple[bool, str]:
    ok, reason = channel_allows(root, chat_id)
    if not ok:
        return ok, reason
    return policy_check(root, chat_id, action=action, scheduled=scheduled, context=context)


def _slot(chat_id: str) -> str:
    return 'owner_private' if chat_id.startswith('private:') else 'group' if chat_id.startswith('group:') else 'other'


def record_send(root, *, request_id: str, chat_id: str, text: str, action: str, state: str, receipt: dict | None = None,
                source: str = '', target_label: str = '') -> None:
    """Append one row to the adaptive ledger (actual sends only; ledger failures never block a send)."""
    try:
        from storage import database
        path = Path(root) / 'data/social/owner-control.db'
        with database(path) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS send_log(id INTEGER PRIMARY KEY,request_id TEXT,slot TEXT,chat TEXT,
                action TEXT,source TEXT,state TEXT,receipt TEXT,text_hash TEXT,created REAL);''')
            import hashlib, time
            db.execute('INSERT INTO send_log(request_id,slot,chat,action,source,state,receipt,text_hash,created) VALUES(?,?,?,?,?,?,?,?,?)',
                       (request_id, _slot(chat_id), chat_id, action, source[:120], state,
                        repr(('OK ' if (receipt or {}).get('ok') else 'ERR ') + str((receipt or {}).get('error') or ''))[:300],
                        hashlib.sha256(str(text or '').encode('utf-8')).hexdigest()[:32], time.time()))
    except Exception:
        pass


def summary(root, *, limit: int = 500) -> dict:
    """Per-slot/action counts of actual sends, so the owner can adjust settings from facts."""
    result = {'window': limit, 'slots': {}, 'unavailable_slots': {}}
    try:
        from storage import database
        path = Path(root) / 'data/social/owner-control.db'
        if not path.exists():
            return result
        with database(path) as db:
            for row in db.execute('SELECT slot,action,state,COUNT(*) n,MAX(created) last FROM send_log GROUP BY slot,action,state'):
                result['slots'].setdefault(row['slot'], {}).setdefault(row['action'], {})[row['state']] = {'count': row['n'], 'last': row['last']}
    except Exception:
        pass
    configured = {'owner_private', 'group'}
    result['unavailable_slots'] = {c['action']: c['reason'] for c in CAPABILITIES
                                   if not c['supported'] and c['destination'] in configured}
    return result


def display(value, zone: str = DISPLAY_ZONE) -> str:
    """ISO timestamp (aware or naive UTC) -> 'YYYY-MM-DD HH:mm:ss Asia/Shanghai'."""
    if not value:
        return ''
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value, ZoneInfo('UTC'))
        else:
            moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo('UTC'))
        return moment.astimezone(ZoneInfo(zone)).strftime('%Y-%m-%d %H:%M:%S') + ' ' + zone
    except (ValueError, TypeError):
        return str(value)


def settings_view(root) -> dict:
    """Everything the owner UI needs for the sending-settings card."""
    from governance import Governance
    policy = Governance(root).config()['send_policy']
    return {'send_policy': policy, 'defaults': normalize_policy({}), 'capabilities': [dict(c) for c in CAPABILITIES],
            'editable': list(SETTABLE), 'display_zone': DISPLAY_ZONE, 'actual_sends': summary(root),
            'owner_chat': owner_private_chat(root)}


__all__ = ['ACTIONS', 'BUCKETS', 'SETTABLE', 'DEFAULT_SEND_POLICY', 'CAPABILITIES', 'normalize_policy', 'validate_updates',
           'channel_allows', 'policy_check', 'decide', 'record_send', 'summary', 'display', 'settings_view', 'owner_private_chat']
