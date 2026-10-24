"""Read-only projection of stored QQ group history for the main chat.

QQStore migrates on open and GroupHistory creates tables, so both are avoided
here: every store is opened with mode=ro and nothing is inserted, updated or
created. A group is readable only when the channel allowlist lists it *and* the
owner's own stored read switch is on; no caller-supplied identity counts.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from group_policy import DEFAULTS

GROUP_RE = re.compile(r'[0-9]{5,15}')
MAX_REFERENCES = 10
TS_FORMAT = '%Y-%m-%d %H:%M:%S'
_COLUMNS = ('id,ts,chat_id,user_id,sender_id,text,direction,channel_message_id,'
            'reply_to,segments,status')

__all__ = ['authorized_groups', 'read_group', 'recent_lines']


def _read_db(path):
    """Open an existing SQLite file read-only; a missing file yields None."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    except sqlite3.Error:
        return None
    db.row_factory = sqlite3.Row
    return db


def _rows(db, sql, args=()):
    if db is None:
        return []
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _query(path, sql, args=()):
    db = _read_db(path)
    try:
        return _rows(db, sql, args)
    finally:
        if db is not None:
            db.close()


def _object(value):
    try:
        data = json.loads(value or '{}')
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _channel(root):
    try:
        return json.loads((Path(root) / 'channels/fish_qq/config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}


def _allowed(root):
    allow = _channel(root).get('allowlist')
    if not isinstance(allow, dict):
        return set()
    return {str(group) for group in allow.get('group_ids') or []}


def _policies(root):
    """Stored group rows, read directly: GroupPolicy.config() would insert one."""
    rows = _query(Path(root) / 'data/social/group-policy.db', 'SELECT chat,config FROM groups')
    return {str(row['chat']): _object(row['config']) for row in rows}


def _state(root, chat):
    path = Path(root) / 'data/social/group-policy.db'
    rows = _query(path, 'SELECT config FROM groups WHERE chat=?', (chat,))
    config = {**DEFAULTS, **_object(rows[0]['config'])} if rows else dict(DEFAULTS)
    seen = _query(path, 'SELECT COUNT(*) n FROM observations WHERE chat=?', (chat,))
    return {'config': config, 'observations': int((seen[0]['n'] if seen else 0) or 0)}


def _bounded(name, value, low, high, default):
    if value is None:
        return default
    if type(value) is not int or not low <= value <= high:
        raise ValueError(name + ' 需为 ' + str(low) + '..' + str(high) + ' 的整数')
    return int(value)


def _group_id(value):
    text = str('' if value is None else value).strip()
    if not GROUP_RE.fullmatch(text):
        raise ValueError('群号需为 5-15 位数字')
    return text


def _attachments(value):
    try:
        segments = json.loads(value or '[]')
    except (TypeError, ValueError):
        return []
    kinds = []
    for segment in segments if isinstance(segments, list) else []:
        if isinstance(segment, dict):
            kind = str(segment.get('type') or '')
            if kind and kind not in kinds:
                kinds.append(kind)
    return kinds


def _observed(path, chat, message_ids):
    """Names recorded while listening in one policy store, keyed by the QQ message id."""
    message_ids = [mid for mid in message_ids if mid]
    if not message_ids:
        return {}
    marks = ','.join('?' * len(message_ids))
    rows = _query(path, 'SELECT id,data FROM observations WHERE chat=? AND id IN (' + marks + ')',
                  (chat, *message_ids))
    names = {}
    for row in rows:
        sender = _object(row['data']).get('sender')
        if not isinstance(sender, dict):
            continue
        name = str(sender.get('name') or sender.get('nickname') or sender.get('card') or '').strip()
        if name:
            names[str(row['id'])] = name
    return names


def _observed_count(path, chat):
    rows = _query(path, 'SELECT COUNT(*) n FROM observations WHERE chat=?', (chat,))
    return int((rows[0]['n'] if rows else 0) or 0)


def _space_stores(root, group, chat, message_ids):
    """Group-test windows that recorded this group, the routed window first.

    New group messages reach qq.db through a test window's own policy store, so
    a snapshot may live outside data/social/group-policy.db. Every store here is
    opened read-only; a missing or unreadable one simply contributes nothing.
    """
    routed = ''
    rows = _query(Path(root) / 'data/social/context.db',
                  'SELECT space FROM group_test_routes WHERE group_id=?', (str(group),))
    if rows and rows[0]['space']:
        routed = str(rows[0]['space'])
    windows = [str(row['space']) for row
               in _query(Path(root) / 'data/social/context.db',
                         'SELECT space FROM group_test_spaces WHERE group_id=? ORDER BY rowid DESC',
                         (str(group),))
               if str(row['space'] or '')]
    windows.sort(key=lambda sid: sid != routed)
    stores = []
    for space in windows:
        path = Path(root) / 'data/qq/spaces' / space / 'group-policy.db'
        count = _observed_count(path, chat)
        if not count:
            continue
        stores.append({'space': space, 'label': 'space:' + space[:8], 'routed': space == routed,
                       'count': count, 'names': _observed(path, chat, message_ids)})
    return stores


def _roster_names(root, chat):
    """Group roster names: the group card first, then the QQ nickname."""
    rows = _query(Path(root) / 'data/social/people.db',
                  'SELECT user_id,nickname,card FROM members WHERE group_id=?', (chat[6:],))
    names = {}
    for row in rows:
        name = str(row['card'] or row['nickname'] or '').strip()
        if name:
            names[str(row['user_id'])] = name
    return names


def _names(root, chat, stores, message_ids):
    bot = _channel(root).get('bot')
    snapshots = {}
    for store in stores:
        for mid, name in store['names'].items():
            snapshots.setdefault(mid, (name, store['label']))
    return {'message': _observed(Path(root) / 'data/social/group-policy.db', chat, message_ids),
            'snapshot': snapshots, 'sender': _roster_names(root, chat),
            'bot': str((bot or {}).get('display_name') or '').strip() if isinstance(bot, dict) else ''}


def _sender_name(row, names):
    """The name plus its provenance: only an observation is a snapshot of that moment."""
    mid = str(row['channel_message_id'] or '')
    exact = names['message'].get(mid)
    if exact:
        return exact, 'observed_at_message', 'main'
    window = names['snapshot'].get(mid)
    if window:
        return window[0], 'observed_at_message', window[1]
    sender = str(row['sender_id'] or '')
    outbound = str(row['direction'] or '') != 'inbound'
    if outbound and names['bot']:
        return names['bot'], 'bot_display', ''
    roster = names['sender'].get(sender)
    if roster:
        return roster, 'current_roster', ''
    if sender == 'bot' and names['bot']:
        return names['bot'], 'bot_display', ''
    return '', 'unknown', ''


def _names_note(main_count, stores):
    """Say plainly which names are snapshots, where each came from, and which are roster only."""
    note = ('sender_name 的来源见 name_source：observed_at_message 是旁听时按该消息编号记下的当时名称，'
            '只有它是当条消息的快照；current_roster 取自 people.db 的当前名册，只是现在的名字，'
            '不保证是发消息当时的昵称；bot_display 是机器人自己出站消息的显示名；'
            'unknown 表示本地没有该发送者的名字记录。')
    if main_count:
        note += '主策略库有 ' + str(main_count) + ' 条旁听记录，命中的消息带 observed_from=main。'
    for store in stores:
        where = '活动的群测试窗口 ' if store['routed'] else '群测试窗口 '
        note += (where + store['label'] + ' 有 ' + str(store['count']) + ' 条旁听记录，'
                 '命中的消息带 observed_from=' + store['label'] + '。')
    if stores and not main_count:
        note += '本次返回的 observed_at_message 来自群测试窗口的旁听快照，不是主策略旁听。'
    if not main_count and not stores:
        note += '本地该群没有任何旁听记录，本次返回不会出现 observed_at_message，名字都只是当前名册。'
    return note


def _window_gaps(stores, listening):
    """Name the test window that is really collecting, instead of blaming the main policy."""
    gaps = []
    for store in stores:
        if not store['routed']:
            gaps.append('该群另有群测试窗口 ' + store['label'] + ' 的历史旁听记录')
        elif listening:
            gaps.append('该群另有活动的群测试窗口 ' + store['label'] + ' 的旁听记录')
        else:
            gaps.append('该群消息来自活动的群测试窗口 ' + store['label'] + '，主策略未开启旁听')
    return gaps


def _provenance(root, chat, message_id):
    """Public receipt facts only; the policy owner keeps private request ids."""
    if not message_id:
        return None
    try:
        from send_policy import outbound_source
        row = outbound_source(root, chat, message_id)
    except Exception:  # an unreadable receipt ledger never blocks reading history
        return None
    if not isinstance(row, dict):
        return None
    return {key: row[key] for key in ('origin_kind', 'origin_description', 'confirmed_at') if key in row}


def _message(root, chat, row, names):
    mid = str(row['channel_message_id'] or '')
    inbound = str(row['direction'] or '') == 'inbound'
    sender_name, name_source, observed_from = _sender_name(row, names)
    item = {'message_id': mid, 'row_id': int(row['id']), 'time': str(row['ts'] or ''),
            'sender_id': str(row['sender_id'] or ''), 'sender_name': sender_name,
            'name_source': name_source, 'observed_from': observed_from,
            'direction': 'inbound' if inbound else 'bot_outbound',
            'reply_to': str(row['reply_to'] or ''), 'text': str(row['text'] or ''),
            'attachment_types': _attachments(row['segments']), 'source': 'qqstore'}
    if not inbound and str(row['status'] or '') == 'sent':
        source = _provenance(root, chat, mid)
        if source:
            item['provenance'] = source
    return item


def _page(root, chat, start, end, before, limit):
    sql = 'SELECT ' + _COLUMNS + ' FROM messages WHERE chat_id=? AND ts>=? AND ts<=?'
    args = [chat, start, end]
    if before is not None:
        sql += ' AND id<?'
        args.append(before)
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(limit + 1)
    return _query(Path(root) / 'data/qq/qq.db', sql, tuple(args))


def _quoted(root, chat, wanted):
    """Bounded lookup of quoted messages inside the same group."""
    found = {}
    for mid in wanted[:MAX_REFERENCES]:
        rows = _query(Path(root) / 'data/qq/qq.db',
                      'SELECT ' + _COLUMNS + ' FROM messages WHERE chat_id=? AND channel_message_id=? '
                      'ORDER BY id DESC LIMIT 1', (chat, mid))
        if rows:
            found[mid] = rows[0]
    return found


def _unknown_names(items):
    missing = []
    for item in items:
        sender = item['sender_id']
        if not item['sender_name'] and sender and sender not in missing:
            missing.append(sender)
    return missing


def _runtime_digest(root):
    """The same digest GroupTests checks, without SocialContext's table-creating init."""
    from social_context import SocialContext
    return SocialContext.runtime_digest(SimpleNamespace(root=Path(root)), 'qq')


def _current_space(root, group):
    """The group-test window that really handles this group, resolved read-only.

    Mirrors GroupTests.current(): routed space, same owner and digest, a qq group
    space. Nothing is created or opened for writing, so listing permissions can
    never write.
    """
    contexts = Path(root) / 'data/social/context.db'
    if not contexts.is_file():
        return ''
    routes = _query(contexts, 'SELECT space,owner FROM group_test_routes WHERE group_id=?', (str(group),))
    if not routes or not routes[0]['space']:
        return ''
    space, owner = str(routes[0]['space']), str(routes[0]['owner'] or '')
    rows = _query(contexts, 'SELECT owner,runtime_digest FROM group_test_spaces WHERE space=? AND group_id=?',
                  (space, str(group)))
    if not rows or str(rows[0]['owner'] or '') != owner:
        return ''
    if str(rows[0]['runtime_digest'] or '') != _runtime_digest(root):
        return ''
    found = _query(contexts, 'SELECT role,origin FROM spaces WHERE id=?', (space,))
    if not found or str(found[0]['role']) != 'qq' or str(found[0]['origin']) != 'group':
        return ''
    return space


def _participation_note(space, main_on, engine='dsh'):
    if engine == 'fish':
        # engine=fish answers from the main policy and bypasses any group-test
        # window, so "a window is talking" would be a false statement here.
        if not main_on:
            return '当前由主决策 Fish 接管（engine=fish），旁听/发言已关闭：这个群不会自动说话。'
        return '当前由主决策 Fish 接管（engine=fish）；群测试窗口不在链路上，不会替它说话。'
    if not space:
        return '当前按主策略生效。'
    if main_on:
        return '当前由活动的群测试窗口 space:' + space[:8] + ' 接管旁听/发言；主策略的旁听/发言也在开，窗口结束后回到主策略。'
    return '当前由活动的群测试窗口 space:' + space[:8] + ' 接管旁听/发言；主策略的旁听/发言是关的，窗口结束后会回到主策略。'


def authorized_groups(root) -> list:
    """Groups the owner has separately authorised for reading, with stored sizes.

    listen/learn/reply are the switches that are actually in force: an active
    group-test window takes them over from the main policy. read is an owner
    authorisation and never comes from a window.
    """
    root = Path(root)
    allowed = _allowed(root)
    if not allowed:
        return []
    groups = []
    for chat, config in sorted(_policies(root).items()):
        group_id = chat[6:] if chat.startswith('group:') else ''
        if group_id not in allowed:
            continue
        merged = {**DEFAULTS, **config}
        if merged.get('read') is not True:
            continue
        engine = str(merged.get('engine') or 'dsh')
        space = _current_space(root, group_id)
        routed = bool(space) and engine != 'fish'
        effective = merged
        if routed:
            rows = _query(root / 'data/qq/spaces' / space / 'group-policy.db',
                          'SELECT config FROM groups WHERE chat=?', (chat,))
            # A routed window without a row for this group runs on DEFAULTS, which
            # GroupPolicy would also report; the main switches stay out of play.
            effective = {**DEFAULTS, **_object(rows[0]['config'])} if rows else dict(DEFAULTS)
        stat = _query(root / 'data/qq/qq.db',
                      'SELECT COUNT(*) n,MAX(ts) last FROM messages WHERE chat_id=?', (chat,))
        row = stat[0] if stat else None
        main_on = bool(merged['listen'] or merged['learn'] or merged['reply'])
        groups.append({'group_id': group_id, 'chat': chat, 'read': True,
                       'listen': bool(effective['listen']), 'learn': bool(effective['learn']),
                       'reply': bool(effective['reply']),
                       'participation_source': ('space:' + space[:8] if routed
                                                else 'fish' if engine == 'fish' else 'main'),
                       'engine': engine,
                       'main_listen': bool(merged['listen']), 'main_learn': bool(merged['learn']),
                       'main_reply': bool(merged['reply']),
                       'participation_note': _participation_note(space, main_on, engine),
                       'messages_stored': int((row['n'] if row else 0) or 0),
                       'last_message_at': str((row['last'] if row else '') or '')})
    return groups


def _span_minutes(first, last):
    """Real elapsed minutes between two stored timestamps; 0 when they cannot be read."""
    try:
        begin = datetime.strptime(str(first), TS_FORMAT)
        end = datetime.strptime(str(last), TS_FORMAT)
    except (TypeError, ValueError):
        return 0
    return max(0, int(round((end - begin).total_seconds() / 60)))


def _coverage_source(messages):
    """Where the returned names were observed: main, one window, mixed, or nothing."""
    origins = {item['observed_from'] for item in messages if item['observed_from']}
    if not origins:
        return 'none'
    return origins.pop() if len(origins) == 1 else 'mixed'


def _coverage_reason(messages, stores, listening, main_count):
    if not messages:
        return '该时间窗内没有记录到群消息，这段时间没有可读资料'
    if stores and not main_count:
        where = '活动的群测试窗口 ' if any(store['routed'] for store in stores) else '群测试窗口 '
        return '消息来自' + where + stores[0]['label'] + '，主策略未开启旁听，未监听时段没有记录'
    if not listening:
        return '该群未开启旁听或学习，未监听时段没有记录'
    return '本地只有旁听时段的消息记录，未监听时段没有记录'


def _coverage(messages, minutes, stores, listening, main_count, gaps):
    """Structured extent of this batch: what it really covers and what it cannot claim.

    complete stays False in every case on purpose: qq.db only holds what some
    listening window happened to record, this read is one bounded page, and no
    subscription exists that could make a batch the whole conversation.
    """
    times = sorted(item['time'] for item in messages if item['time'])
    first = times[0] if times else ''
    last = times[-1] if times else ''
    return {'requested_minutes': minutes, 'first_message_time': first, 'last_message_time': last,
            'span_minutes': _span_minutes(first, last), 'source': _coverage_source(messages),
            'complete': False,
            'reason': _coverage_reason(messages, stores, listening, main_count),
            'gaps_count': len(gaps)}


def _coverage_note(coverage, count):
    """One line for the model: covered time, source, completeness, gap count.

    Times come from the stored messages themselves, so the note never restates the
    requested window as a duration word ('一小时') the records do not support.
    """
    if not coverage['first_message_time']:
        note = ('覆盖范围：该窗口内没有记录（请求 ' + str(coverage['requested_minutes']) + ' 分钟，来源 '
                + coverage['source'] + '）；不完整：' + coverage['reason'] + '；缺口 '
                + str(coverage['gaps_count']) + ' 条。')
    else:
        note = ('覆盖范围：已读 ' + str(count) + ' 条，时间 ' + coverage['first_message_time'] + ' 至 '
                + coverage['last_message_time'] + '，实际跨度 ' + str(coverage['span_minutes']) + ' 分钟，来源 '
                + coverage['source'] + '；不完整：' + coverage['reason'] + '；缺口 '
                + str(coverage['gaps_count']) + ' 条。')
    return note[:200]


def read_group(root, group_id, *, minutes=30, limit=50, before=None) -> dict:
    """One bounded read of an authorised group; raises PermissionError otherwise."""
    root = Path(root)
    group = _group_id(group_id)
    minutes = _bounded('minutes', minutes, 1, 120, 30)
    limit = _bounded('limit', limit, 1, 50, 50)
    if before is not None and (type(before) is not int or before < 1):
        raise ValueError('before 需为消息序号')
    chat = 'group:' + group
    state = _state(root, chat)
    if group not in _allowed(root) or state['config'].get('read') is not True:
        raise PermissionError('群未获主人读取授权：' + chat)
    now = datetime.now()
    start = (now - timedelta(minutes=minutes)).strftime(TS_FORMAT)
    end = now.strftime(TS_FORMAT)
    fetched = _page(root, chat, start, end, before, limit)
    more = len(fetched) > limit
    page = list(reversed(fetched[:limit]))
    wanted, seen = [], {str(row['channel_message_id'] or '') for row in page}
    for row in page:
        target = str(row['reply_to'] or '')
        if target and target not in seen and target not in wanted:
            wanted.append(target)
    quoted = _quoted(root, chat, wanted)
    ordered = [quoted[mid] for mid in sorted(quoted, key=lambda key: int(quoted[key]['id']))]
    ids = [str(row['channel_message_id'] or '') for row in page]
    ids += [str(row['channel_message_id'] or '') for row in ordered]
    stores = _space_stores(root, group, chat, ids)
    names = _names(root, chat, stores, ids)
    messages = [_message(root, chat, row, names) for row in page]
    references = [_message(root, chat, row, names) for row in ordered]
    gaps = []
    if not (root / 'data/qq/qq.db').is_file():
        gaps.append('本地没有 QQ 消息数据库，无法读取任何记录')
    if not messages:
        gaps.append('该时间窗内没有记录到群消息')
    listening = bool(state['config']['listen'] or state['config']['learn'])
    if stores:
        gaps.extend(_window_gaps(stores, listening))
    elif not listening:
        gaps.append('该群未开启旁听或学习，未监听时段的资料不完整')
    elif not state['observations']:
        gaps.append('本地没有该群的旁听记录，未监听时段的资料不完整')
    for mid in wanted:
        if mid not in quoted:
            gaps.append('引用的消息 ' + mid + ' 不在本地记录中')
    if len(wanted) > MAX_REFERENCES:
        gaps.append('另有 ' + str(len(wanted) - MAX_REFERENCES) + ' 条引用消息未补读')
    missing = _unknown_names(messages + references)
    for sender in missing[:6]:
        gaps.append('本地没有该发送者的名称记录：' + sender)
    if len(missing) > 6:
        gaps.append('另有 ' + str(len(missing) - 6) + ' 个发送者本地没有名称记录')
    coverage = _coverage(messages, minutes, stores, listening, state['observations'], gaps)
    return {'group_id': group, 'chat': chat, 'authorized': True,
            'window': {'minutes': minutes, 'from': start, 'to': end},
            'messages': messages, 'references': references, 'more': more,
            'next_before': int(page[0]['id']) if page else None,
            'counts': {'returned': len(messages),
                       'inbound': sum(1 for m in messages if m['direction'] == 'inbound'),
                       'bot_outbound': sum(1 for m in messages if m['direction'] == 'bot_outbound')},
            'gaps': gaps, 'names_note': _names_note(state['observations'], stores),
            'coverage': coverage, 'coverage_note': _coverage_note(coverage, len(messages))}


# ------------------------------------------------------------------ one stream
# Her private chat and every read-authorised group are the same experience: one
# timeline ordered by real time, and no window she is refused to look at. The
# surface decides what she says, never what she may read, so this reader takes no
# scope at all.
ACTIVITY_KINDS = ('private', 'group', 'all')
ACTIVITY_DEFAULT_HOURS = 6
ACTIVITY_MAX_LINES = 60
MEDIA_LABELS = {'image': '[图片]', 'face': '[表情]', 'mface': '[表情]', 'video': '[视频]',
                'audio': '[语音]', 'file': '[文件]'}
DEAD_STATUSES = ('failed', 'blocked', 'cancelled')


def _allowlist(root):
    allow = _channel(root).get('allowlist')
    return allow if isinstance(allow, dict) else {}


def _bot_name(root):
    bot = _channel(root).get('bot')
    return str(bot.get('display_name') or '').strip() if isinstance(bot, dict) else ''


def _owner_chat(root):
    """The owner's own private chat, private:<owner QQ>; empty when nobody is named."""
    owner = str(_allowlist(root).get('owner_id') or '').strip()
    return 'private:' + owner if owner else ''


def _readable(text, segments):
    """What a message actually says: the words plus one mark per attachment.

    A sticker or a picture carries no text of its own, so without the mark the line
    would read as empty and she could not tell that anything arrived at all.
    """
    try:
        rows = json.loads(segments or '[]')
    except (TypeError, ValueError):
        rows = []
    marks = []
    for row in rows if isinstance(rows, list) else []:
        mark = MEDIA_LABELS.get(str(row.get('type') or '')) if isinstance(row, dict) else ''
        if mark and mark not in marks:
            marks.append(mark)
    parts = [str(text or '').strip(), *marks]
    return ' '.join(part for part in parts if part).strip() or '[附件消息]'


def _newest(root, chat, start, end, limit):
    """The newest rows of one chat inside a window, newest first."""
    rows = _query(Path(root) / 'data/qq/qq.db',
                  'SELECT ' + _COLUMNS + ' FROM messages WHERE chat_id=? AND ts>=? AND ts<=? '
                  'ORDER BY id DESC LIMIT ?', (chat, start, end, limit + 1))
    return [row for row in rows if str(row['status'] or '') not in DEAD_STATUSES]


def _stream_names(root, chat, rows):
    """Group speaker names, resolved exactly as fish_group_history resolves them."""
    if not str(chat).startswith('group:'):
        return {}
    ids = [str(row['channel_message_id'] or '') for row in rows]
    return _names(root, chat, _space_stores(root, chat[6:], chat, ids), ids)


def _line(chat, row, bot, names):
    inbound = str(row['direction'] or '') == 'inbound'
    if str(chat).startswith('group:'):
        name, _source, _from = _sender_name(row, names)
        who = name or ('她' if not inbound else str(row['sender_id'] or '未知发言人'))
        where = str(chat)
    else:
        # A private chat has exactly two speakers and both of them are known.
        who = '主人' if inbound else (bot or '她')
        where = 'private'
    return {'ts': str(row['ts'] or ''), 'where': where, 'who': who,
            'direction': 'inbound' if inbound else 'outbound',
            'text': _readable(row['text'], row['segments']), 'seq': int(row['id'])}


def recent_lines(root, *, kind='all', hours=ACTIVITY_DEFAULT_HOURS, limit=ACTIVITY_MAX_LINES) -> dict:
    """One bounded read of the whole stream: her private chat and every authorised group.

    Each line is one message as she lived it: ts is the real send time, where is
    private or group:<群号>, who is the speaker with her own lines included, and
    text is the readable form. Lines come back oldest first and only the newest
    limit survive; truncated then says older lines exist, so this batch can never be
    reported as everything that happened.
    """
    root = Path(root)
    kind = str(kind or 'all')
    if kind not in ACTIVITY_KINDS:
        raise ValueError('kind 需为 private、group 或 all')
    hours = _bounded('hours', hours, 1, 168, ACTIVITY_DEFAULT_HOURS)
    limit = _bounded('limit', limit, 1, ACTIVITY_MAX_LINES, ACTIVITY_MAX_LINES)
    chats = []
    private = _owner_chat(root)
    if kind in ('private', 'all') and private:
        chats.append(private)
    if kind in ('group', 'all'):
        chats.extend(group['chat'] for group in authorized_groups(root))
    now = datetime.now()
    start = (now - timedelta(hours=hours)).strftime(TS_FORMAT)
    end = now.strftime(TS_FORMAT)
    bot = _bot_name(root)
    lines = []
    for chat in chats:
        rows = _newest(root, chat, start, end, limit)
        names = _stream_names(root, chat, rows)
        lines.extend(_line(chat, row, bot, names) for row in rows)
    lines.sort(key=lambda line: (line['ts'], line['seq']))
    return {'kind': kind, 'hours': hours,
            'lines': [{key: line[key] for key in ('ts', 'where', 'who', 'direction', 'text')}
                      for line in lines[-limit:]],
            'truncated': len(lines) > limit}

