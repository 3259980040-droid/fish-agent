"""Read-only replay of recent real group messages through the live policy layer.

Answers one question: if the newest real messages of a group were pushed through
today's GroupPolicy gates, how often would she speak and why would she not. Every
message goes through the real GroupPolicy.observe / candidate / reserve / finish of
harness/group_policy.py, so cooldown, the hourly and per-minute caps, the debounce
merge window, a dive, the wake roll and the unanswered limit behave exactly as they
do live. Nothing is sent, no model is called, and the live stores are opened
read-only: the replay runs in a throw-away root whose group-policy.db holds a copy
of the real configuration.

The timeline is replayed as events, not as a straight loop, because the fish turn
itself is asynchronous (qq_agent_loop._fish_reply): an inbound message is observed
when it arrives, then an "active" turn waits out the debounce window
(min(debounce_s, 10)) before it reserves, and a reservation made for an older cursor
is retargeted to the newest message. Replaying arrivals and wake-ups in time order
is what makes the merge window and the cooldowns land on the same instants the live
loop hits.

Two things are deliberately not the live store state, and both are reported in the
output:

  * the chat's inbound history is fed from the replay's own message list, not from
    the live observations/handled/cursor rows, so the ledger is empty at the start
    of the pass and every message is new;
  * listen and reply are forced on. The live group is currently silent, and with
    listen off GroupPolicy returns "旁听或发言未开启" for every message, which would
    answer the question with nothing. Every other gate keeps the real value.

    python tools/replay_group_policy.py --root <工程根> --chat group:1043230169 \
        --limit 200 --since '2026-10-19 00:00' --out <json 路径>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]

from group_policy import DEFAULTS, GroupPolicy  # noqa: E402

TS_FORMAT = '%Y-%m-%d %H:%M:%S'
COLUMNS = 'ts,user_id,sender_id,text,channel_message_id,reply_to,segments'
# reason text -> bucket; anything else lands in 其它 with its raw text kept beside it.
BUCKETS = (('主动回复冷却中', '冷却'), ('主动回复达到每小时上限', '每小时上限'),
           ('全部回复达到每分钟上限', '每分钟上限'), ('无人接续', '无人接续'),
           ('正在合并连续消息', '合并窗口'), ('潜水到', '潜水'),
           ('本次未被唤醒', '未被唤醒'), ('当前仅回应提及', '仅回应提及'))
BUCKET_NAMES = tuple(name for _, name in BUCKETS) + ('其它',)
DETAIL_LIMIT = 20
PREVIEW_CHARS = 30
# The fish turn's own copy of this bound: it sleeps at most this long, not debounce_s.
MERGE_CAP_S = 10.0
# The replay must be reproducible: the wake roll is seeded per message, and the same
# seed is replayed right before reserve(), whose own _candidate() rolls a second time.
SEED = 'replay-group-policy'
# The config keys the replay changes, reported so a reader never has to guess which
# gate was real.
FORCED = {'listen': True, 'reply': True}


def sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1 << 20), b''):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return ''


def _open_ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def real_config(root: Path, chat: str) -> tuple[dict, bool]:
    """The chat's stored settings, read without letting GroupPolicy create anything."""
    path = root / 'data' / 'social' / 'group-policy.db'
    try:
        with _open_ro(path) as db:
            row = db.execute('SELECT config FROM groups WHERE chat=?', (chat,)).fetchone()
    except sqlite3.Error:
        return dict(DEFAULTS), False
    stored = json.loads(row['config']) if row and row['config'] else {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}, bool(row)


def _segments(raw, text) -> list:
    try:
        segments = json.loads(raw or '[]')
    except (TypeError, ValueError):
        segments = []
    if isinstance(segments, list) and segments:
        return [item for item in segments if isinstance(item, dict)]
    return [{'type': 'text', 'text': str(text or '')}]


def read_messages(root: Path, chat: str, limit: int, since: str) -> dict:
    """Only reads the message store: the group's inbound rows inside the window."""
    with _open_ro(root / 'data' / 'qq' / 'qq.db') as db:
        sql = ('SELECT ' + COLUMNS + " FROM messages WHERE chat_id=? AND direction='inbound'")
        args = [chat]
        if since:
            sql += ' AND ts>=?'
            args.append(since)
        rows = db.execute(sql + ' ORDER BY ts ASC, id ASC', args).fetchall()
    window = rows[-limit:] if limit > 0 else rows
    kept, dropped_empty, skipped = [], 0, []
    for row in window:
        ts = str(row['ts'] or '').strip()
        mid = str(row['channel_message_id'] or '').strip()
        try:
            stamp = datetime.strptime(ts, TS_FORMAT)
        except ValueError:
            skipped.append({'ts': ts, 'message_id': mid, 'why': '时间戳无法解析'})
            continue
        if not mid:
            skipped.append({'ts': ts, 'message_id': '', 'why': '缺少消息号'})
            continue
        if not str(row['text'] or '').strip():
            dropped_empty += 1
            continue
        kept.append({'ts': ts, 'ts_epoch': stamp.timestamp(), 'message_id': mid,
                     'sender': str(row['sender_id'] or row['user_id'] or ''),
                     'text': str(row['text'] or ''), 'reply_to': str(row['reply_to'] or ''),
                     'segments': _segments(row['segments'], row['text'])})
    return {'messages': kept, 'read': len(window), 'dropped_empty': dropped_empty,
            'skipped': skipped, 'since': since}


def bot_identity(root: Path, chat: str, override: str = '') -> tuple[str, str]:
    """Who "me" is in this group: the id an @-segment must name, and how we know.

    channels/fish_qq/config.json carries bot.id, but the live one is the literal
    string "unknown", so the fallback counts the at-targets of this group's whole
    inbound history and takes the most frequent one - the account the group @s when
    it addresses her. --bot-id overrides both.
    """
    if override:
        return str(override), 'override'
    try:
        data = json.loads((root / 'channels/fish_qq/config.json').read_text(encoding='utf-8'))
        bot = str((data.get('bot') or {}).get('id') or '')
        if bot.isdigit():
            return bot, 'config'
    except (OSError, ValueError):
        pass
    with _open_ro(root / 'data/qq/qq.db') as db:
        rows = db.execute("SELECT segments FROM messages WHERE chat_id=? AND direction='inbound'",
                          (chat,)).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        for segment in _segments(row['segments'], ''):
            if segment.get('type') == 'at':
                target = str(segment.get('user_id') or '')
                if target:
                    counts[target] = counts.get(target, 0) + 1
    if not counts:
        return '', 'none'
    return max(counts, key=lambda key: counts[key]), 'most-mentioned'


def outbound_ids(root: Path, chat: str) -> set[str]:
    """Her own delivered message ids: a reply to one of them is a reply to her."""
    with _open_ro(root / 'data' / 'qq' / 'qq.db') as db:
        rows = db.execute("SELECT channel_message_id FROM messages WHERE chat_id=? "
                          "AND direction='outbound'", (chat,)).fetchall()
    return {str(row['channel_message_id']) for row in rows if row['channel_message_id']}


def payload_of(message: dict, chat: str, bot_id: str, bot_ids: set[str]) -> dict:
    """The inbound shape the channel adapter hands the policy layer (normalize.py)."""
    segments = message['segments']
    mentions = any(segment.get('type') == 'at' and str(segment.get('user_id') or '') == bot_id
                   for segment in segments)
    reply_to = message['reply_to']
    return {'chat_id': chat, 'message_id': message['message_id'],
            'sender': {'id': message['sender'], 'display_name': ''},
            'segments': segments, 'reply_to': reply_to,
            'mentions_bot': mentions, 'reply_to_bot': bool(reply_to and reply_to in bot_ids)}


def classify(reason: str) -> str:
    for prefix, name in BUCKETS:
        if reason.startswith(prefix):
            return name
    return '其它'


def _preview(text: str) -> str:
    return ' '.join(str(text).split())[:PREVIEW_CHARS]


def replay_pass(chat: str, messages: list, bot_id: str, bot_ids: set[str],
                config: dict, seed: str) -> dict:
    """One full timeline through a fresh temp store; nothing outside temp is written."""
    temp = Path(tempfile.mkdtemp(prefix='fish-replay-'))
    outcome = {'total': len(messages), 'candidates': 0, 'silent': 0,
               'by_reason': {name: 0 for name in BUCKET_NAMES}, 'by_reason_raw': {},
               'details': [], 'errors': [], 'merge_wait_s': 0.0}
    try:
        store = GroupPolicy(temp)
        effective = store.configure(chat, config)
        outcome['effective_config'] = effective
        merge = min(float(effective['debounce_s']), MERGE_CAP_S)
        outcome['merge_wait_s'] = merge
        plan, results = [], [None] * len(messages)
        for index, message in enumerate(messages):
            payload = payload_of(message, chat, bot_id, bot_ids)
            direct = bool(payload['mentions_bot'] or payload['reply_to_bot'])
            plan.append({'index': index, 'message': message, 'payload': payload,
                         'direct': direct,
                         'wake': message['ts_epoch'] + (0.0 if direct else merge)})
        # Arrivals first at equal times, so a wake-up sees exactly the messages that
        # had arrived by then: the same order the live loop observes them in.
        events = [(item['message']['ts_epoch'], 0, item['index']) for item in plan]
        events += [(item['wake'], 1, item['index']) for item in plan]
        events.sort()
        turns = {}
        mid_by_cursor = {}
        for when, phase, index in events:
            item = plan[index]
            message = item['message']
            record = {'ts': message['ts'], 'message_id': message['message_id'],
                      'sender': message['sender'], 'text': _preview(message['text']),
                      'direct': item['direct'], 'answered_message_id': ''}
            if phase == 0:
                try:
                    random.seed(f'{seed}:{message["message_id"]}')
                    seen = store.observe(item['payload'], now=when)
                except Exception as exc:  # noqa: BLE001 - one bad row must not end the replay
                    turns[index] = {'alive': False,
                                    'reason': '回放异常: ' + type(exc).__name__ + ': ' + str(exc)[:160]}
                    outcome['errors'].append({'ts': message['ts'], 'stage': 'observe',
                                              'message_id': message['message_id'],
                                              'error': str(exc)[:200]})
                    continue
                if not seen.get('accepted'):
                    turns[index] = {'alive': False, 'reason': '消息未被接受（重复投递或旁听未开启）'}
                    continue
                mid_by_cursor[int(seen['cursor'])] = message['message_id']
                turns[index] = {'alive': True, 'cursor': int(seen['cursor']),
                                'kind': 'direct' if item['direct'] else 'active'}
                continue
            turn = turns.get(index) or {'alive': False, 'reason': '未观察到该消息'}
            if not turn.get('alive'):
                action, reason = 'silent', turn.get('reason') or ''
            else:
                try:
                    random.seed(f'{seed}:{message["message_id"]}')
                    candidate = store.candidate(chat, now=when)
                    action = candidate.get('action', '')
                    reason = str(candidate.get('reason') or '')
                    token = ''
                    if action == 'candidate':
                        # The same seed, so reserve()'s second _candidate() rolls the
                        # wake probability to the same answer instead of a new one.
                        random.seed(f'{seed}:{message["message_id"]}')
                        token = store.reserve(chat, int(candidate['cursor']),
                                              candidate['kind'], now=when)
                        if not token:
                            action, reason = 'silent', '预留未通过（reserve 二次判定否决）'
                    if token:
                        store.finish(token, True, now=when)
                        record['answered_message_id'] = mid_by_cursor.get(
                            int(candidate['cursor']), '')
                except Exception as exc:  # noqa: BLE001 - one bad row must not end the replay
                    action, reason = 'error', '回放异常: ' + type(exc).__name__ + ': ' + str(exc)[:160]
                    outcome['errors'].append({'ts': message['ts'], 'stage': 'wake',
                                              'message_id': message['message_id'],
                                              'error': str(exc)[:200]})
            record.update(action=action, reason=reason)
            results[index] = record
            if action == 'candidate':
                outcome['candidates'] += 1
            else:
                outcome['silent'] += 1
                outcome['by_reason'][classify(reason)] += 1
                outcome['by_reason_raw'][reason] = outcome['by_reason_raw'].get(reason, 0) + 1
        for record in results:
            if record is None:
                continue
            if len(outcome['details']) < DETAIL_LIMIT:
                outcome['details'].append(record)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return outcome


def run(args) -> dict:
    root = Path(args.root).expanduser().resolve()
    chat = args.chat
    real_path = root / 'data' / 'social' / 'group-policy.db'
    qq_path = root / 'data' / 'qq' / 'qq.db'
    before = {'group_policy_db_sha256': sha256(real_path), 'qq_db_sha256': sha256(qq_path)}

    stored, stored_found = real_config(root, chat)
    feed = read_messages(root, chat, args.limit, args.since)
    bot_id, bot_source = bot_identity(root, chat, args.bot_id)
    bot_ids = outbound_ids(root, chat)
    messages = feed['messages']
    config = {k: v for k, v in stored.items() if k in DEFAULTS}
    config.update(FORCED)

    baseline = replay_pass(chat, messages, bot_id, bot_ids, config, SEED)
    contrast = replay_pass(chat, messages, bot_id, bot_ids,
                           {**config, 'active_cooldown_s': 0}, SEED + ':cooldown-0')
    after = {'group_policy_db_sha256': sha256(real_path), 'qq_db_sha256': sha256(qq_path)}

    limits_on = bool(stored.get('reply_limits'))
    # The cooldown gate is not behind reply_limits (only the hourly cap, the
    # per-minute total and the unanswered limit are), so the contrast always has a
    # real effect; the note says which of the other gates were even reading.
    note = ('冷却闸门不读 reply_limits，始终生效；reply_limits=%s 时'
            '每小时上限、每分钟上限、无人接续%s'
            % ('true' if limits_on else 'false', '才参与判定' if limits_on else '全部关闭'))
    report = {
        'tool': 'replay_group_policy', 'generated_at': datetime.now().strftime(TS_FORMAT),
        'root': str(root), 'chat': chat, 'limit': args.limit, 'since': args.since,
        'read': {'rows_in_window': feed['read'], 'replayed': len(messages),
                 'dropped_empty_text': feed['dropped_empty'], 'skipped': feed['skipped'],
                 'first_ts': messages[0]['ts'] if messages else '',
                 'last_ts': messages[-1]['ts'] if messages else ''},
        'bot': {'id': bot_id, 'source': bot_source, 'own_message_ids': len(bot_ids)},
        'timeline': {'mode': 'event-ordered arrivals and wake-ups',
                     'merge_wait_s': baseline['merge_wait_s'],
                     'note': '主动消息在到达后按 min(debounce_s, 10) 秒醒来再预留，'
                             '与 qq_agent_loop._fish_reply 的等待一致；提及立即醒来'},
        'policy': {'real_config': stored, 'stored_row_found': stored_found,
                   'replay_overrides': dict(FORCED),
                   'note': '时间线状态（observations/handled/cursor）从空白开始，'
                           '其余设置均为真实库中的值'},
        'result': {'total': baseline['total'], 'candidates': baseline['candidates'],
                   'silent': baseline['silent'], 'by_reason': baseline['by_reason'],
                   'by_reason_raw': baseline['by_reason_raw'],
                   'details': baseline['details'], 'errors': baseline['errors']},
        'contrast': {'change': {'active_cooldown_s': 0},
                     'candidates': contrast['candidates'],
                     'delta': contrast['candidates'] - baseline['candidates'],
                     'by_reason': contrast['by_reason'], 'note': note},
        'safety': {'sent': 0, 'model_calls': 0, 'real_stores_written': 0,
                   'temp_roots_removed': 2,
                   'before': before, 'after': after, 'unchanged': before == after},
    }
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    report['out'] = str(out)
    return report


def summary(report: dict) -> str:
    read, result = report['read'], report['result']
    reason_text = ' · '.join(f'{name} {count}'
                             for name, count in result['by_reason'].items() if count)
    lines = [
        f"群 {report['chat']} · 回放 {result['total']} 条真实入站"
        f"（{read['first_ts']} → {read['last_ts']}）",
        f"窗口读取 {read['rows_in_window']} 条，丢弃纯图/空文本 {read['dropped_empty_text']} 条"
        + (f"，跳过无效 {len(read['skipped'])} 条" if read['skipped'] else ''),
        f"策略：真实配置抄入临时库；listen/reply 由关闭强制为开启（否则策略层永不候选）"
        f"；reply_limits={report['policy']['real_config'].get('reply_limits')}",
        f"时间轴：到达与醒来按时间排序，主动消息醒于到达后 {report['timeline']['merge_wait_s']:g} 秒",
        f"她开口 {result['candidates']} 次，沉默 {result['silent']} 次",
        f"沉默原因：{reason_text or '（无）'}",
        f"对照 active_cooldown_s=0：候选 {result['candidates']} → "
        f"{report['contrast']['candidates']}（{report['contrast']['delta']:+d}）",
        f"对照说明：{report['contrast']['note']}",
    ]
    for item in result['details'][:4]:
        lines.append(f"  {item['ts']} {item['sender']} {item['text']} → "
                     f"{item['action']} {item['reason'][:24]}")
    lines.append("安全：未发送、未调模型；真实 group-policy.db / qq.db sha256 "
                 + ('前后一致' if report['safety']['unchanged'] else '有变化（需人工确认）'))
    lines.append(f"JSON：{report['out']}")
    return '\n'.join(lines[:15])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='只读回放真实群消息经过策略层的结果（不发不调模型）')
    parser.add_argument('--root', default=str(ROOT), help='工程根')
    parser.add_argument('--chat', required=True, help='群范围，如 group:1043230169')
    parser.add_argument('--limit', type=int, default=200, help='最近多少条入站消息（默认 200）')
    parser.add_argument('--since', default='', help="起始时间 'YYYY-MM-DD HH:MM'，留空为不限")
    parser.add_argument('--out', required=True, help='输出 JSON 路径')
    parser.add_argument('--bot-id', default='', help='她的 QQ 号（默认从配置或 @ 目标推断）')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'group:[0-9]{5,15}', str(args.chat)):
        parser.error('群范围格式应为 group:群号')
    report = run(args)
    print(summary(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
