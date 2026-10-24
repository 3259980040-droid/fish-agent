r"""Replay real group messages through the candidate and the judge, and report both.

The judge prompt and the material it reads are the only place a group goes
silent, so a change there is measured, not assumed. Each message is read back
out of the group's own store, generated with fish_candidate exactly as the loop
does, and then judged; nothing is sent, because the judge only decides.

    C:\Python313\python.exe tools/probe_judge.py --direct --limit 12 --json

A sample is one generation call plus one judge call, so twelve samples cost
twenty-four paid calls. --direct asks the judge itself instead of worth_speaking,
so the group's judge allowance and its cooldown are not spent; everything else is
the real judge on the real material. The summary is twenty lines at most, and the
per-sample detail is written to the JSON file the summary names.
"""
import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]

import group_decide

CHAT = 'group:1043230169'
LIMIT = 12
LIMIT_CAP = 24
MIN_CHARS = 8
STAMP = '%Y-%m-%d %H:%M:%S'

# A refusal is one sentence of the judge's own words. These buckets are how a
# month of runs is read back; the words are matched where the judge can only
# mean one thing, and anything unmatched stays visible as 其它 instead of being
# forced into a bucket that would invent a pattern.
CATEGORIES = (
    ('不针对我', ('不针对', '不是对我', '没提到我', '没有提到我', '不是问我', '没有问我',
                 '没人问我', '不是对我说', '没点名', '泛指', '对所有人')),
    ('私事', ('私事', '私人', '私下', '甲对乙', '两人', '不关我', '私聊')),
    ('已被人接上', ('已经有人', '有人接', '已经接上', '已经回', '已回', '已经答', '已答',
                   '已有回应', '已被回应')),
    ('太热闹', ('热闹', '连聊', '一句接一句', '节奏', '刷屏', '插不上', '太快', '密集', '连发')),
    ('只在重复', ('重复', '复述', '照搬', '同一句', '一样')),
)
OTHER = '其它'
REASONS = [name for name, _ in CATEGORIES] + [OTHER]


def read_message(root, chat, message_id):
    """One stored message rebuilt as the payload the group loop hands over."""
    path = Path(root) / 'data/qq/qq.db'
    if not path.is_file():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute('SELECT channel_message_id,user_id,sender_id,text,segments,reply_to '
                         'FROM messages WHERE chat_id=? AND channel_message_id=? '
                         'ORDER BY id DESC LIMIT 1', (chat, message_id)).fetchone()
    finally:
        db.close()
    if row is None:
        return None
    try:
        segments = json.loads(row['segments'] or '[]')
    except (TypeError, ValueError):
        segments = []
    if not isinstance(segments, list) or not segments:
        segments = [{'type': 'text', 'text': str(row['text'] or '')}]
    return {'chat_type': 'group', 'chat_id': chat,
            'message_id': str(row['channel_message_id']),
            'sender': {'id': str(row['sender_id'] or row['user_id'] or ''), 'display_name': ''},
            'segments': segments, 'reply_to': str(row['reply_to'] or ''),
            'mentions_bot': False, 'reply_to_bot': False}


def stored(root, chat, min_chars):
    """Every stored inbound line long enough to be a real remark, oldest first.

    A one-word '嗯' or a bare emoji is not a message with a topic in it, so it is
    filtered here rather than being handed to the generator and counted as a
    refusal.
    """
    path = Path(root) / 'data/qq/qq.db'
    if not path.is_file():
        return []
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute('SELECT channel_message_id,ts,text FROM messages '
                          'WHERE chat_id=? AND direction=? AND length(trim(text))>=? '
                          'ORDER BY id', (chat, 'inbound', int(min_chars))).fetchall()
    finally:
        db.close()
    return [{'message_id': str(row['channel_message_id'] or ''),
             'ts': str(row['ts'] or ''), 'text': str(row['text'] or '').strip()}
            for row in rows if str(row['channel_message_id'] or '')]


def sampled(rows, limit, mode):
    """Which stored lines to measure: the newest ones, or an even spread.

    The newest twelve are all from one evening's conversation, so a warm burst
    of two people reads as the group's normal state; the spread walks the whole
    stored history and samples it evenly, which is what a rate has to average.
    """
    if limit <= 0 or not rows:
        return []
    if limit >= len(rows):
        return list(rows)
    if mode == 'latest':
        return rows[-limit:]
    step = (len(rows) - 1) / (limit - 1)
    picks, seen = [], set()
    for index in range(limit):
        at = round(index * step)
        if at not in seen:
            seen.add(at)
            picks.append(rows[at])
    return picks


def judge_direct(root, chat, payload):
    """The judge's own verdict on this round, without worth_speaking's gates.

    The group's daily allowance is spend the live loop needs, and a measurement
    run must not eat it; everything measured here is still the real judge on the
    real material, so only the quota and the cooldown are out of the picture.
    """
    try:
        decision = group_decide._judge(root, chat, group_decide._judge_material(root, chat, payload),
                                       None)
    except Exception as exc:  # noqa: BLE001 - a failed judge is a refusal, as in the gate
        return {'speak': False, 'reason': 'judge-error:' + type(exc).__name__, 'judge_calls': 1}
    if not isinstance(decision, dict) or 'should_speak' not in decision:
        return {'speak': False, 'reason': 'judge-error:malformed', 'judge_calls': 1}
    return {'speak': decision.get('should_speak') is True,
            'reason': str(decision.get('reason') or ''),
            'judge_calls': 1}


def category(reason):
    """Which refusal bucket the judge's own sentence falls in."""
    text = str(reason or '')
    if text.startswith('judge-error'):
        return OTHER
    for name, words in CATEGORIES:
        if any(word in text for word in words):
            return name
    return OTHER


def wilson(spoken, total):
    """The 95% interval for a rate measured on this many samples.

    Twelve samples pin a rate only loosely, and a bare 3/12 reads as more certain
    than it is; the interval is what says so.
    """
    if total <= 0:
        return (0.0, 0.0)
    z = 1.96
    p = spoken / total
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    centre = p + z * z / (2 * total)
    bound = 1 + z * z / total
    return (max(0.0, (centre - spread) / bound), min(1.0, (centre + spread) / bound))


def seconds(a, b):
    try:
        return (datetime.strptime(b, STAMP) - datetime.strptime(a, STAMP)).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def summary_lines(report, args, detail_path):
    """The whole result in twenty lines or fewer, for a terminal and a chat."""
    rows = [item for item in report if item.get('candidate')]
    refused = [item for item in report if item.get('candidate') and not item.get('speak')]
    spoken = sum(1 for item in report if item.get('speak'))
    spans = [item['ts'] for item in report if item.get('ts')]
    lines = ['probe_judge %s chat=%s limit=%d min-chars=%d sample=%s'
             % ('direct' if args.direct else 'worth_speaking', args.chat, len(report),
                args.min_chars, args.sample)]
    if spans:
        gap = seconds(min(spans), max(spans))
        lines.append('样本 %d 条：%s → %s（跨度 %.1f 小时）'
                     % (len(report), min(spans)[5:16], max(spans)[5:16], gap / 3600))
    else:
        lines.append('样本 %d 条：无可用的时间戳' % len(report))
    costs = [item['generate_ms'] for item in rows if item.get('generate_ms') is not None]
    if costs:
        ordered = sorted(costs)
        lines.append('出候选 %d/%d 条，平均生成 %.1f 秒（中位 %.1f，最长 %.1f）'
                     % (len(rows), len(report), sum(costs) / len(costs) / 1000,
                        ordered[len(ordered) // 2] / 1000, ordered[-1] / 1000))
    else:
        lines.append('出候选 0/%d 条，没有可测的生成耗时' % len(report))
    low, high = wilson(spoken, len(report))
    lines.append('判断通过 %d/%d = %.1f%%，Wilson 95%% 区间 %.1f%%–%.1f%%'
                 % (spoken, len(report), 100.0 * spoken / len(report) if report else 0.0,
                    100.0 * low, 100.0 * high))
    errors = [item for item in report if str(item.get('reason') or '').startswith('judge-error')]
    failed = [item for item in report if item.get('error') and not item.get('candidate')]
    if errors or failed:
        lines.append('异常：判断失败 %d 条，未出候选 %d 条' % (len(errors), len(failed)))
    lines.append('被拒 %d 条，按原因归类：' % len(refused))
    for name in REASONS:
        group = [item for item in refused if item.get('category') == name]
        if not group:
            lines.append('  · %s 0' % name)
            continue
        example = str(group[0].get('reason') or '')[:48]
        lines.append('  · %s %d：“%s”（%s）' % (name, len(group), example, group[0]['message_id']))
    if detail_path:
        lines.append('明细 %s' % detail_path)
    return lines[:20]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(ROOT))
    parser.add_argument('--chat', default=CHAT)
    parser.add_argument('--limit', type=int, default=LIMIT,
                        help='messages to measure; default %d, capped at %d '
                             '(each one is one generation and one judge call)' % (LIMIT, LIMIT_CAP))
    parser.add_argument('--min-chars', type=int, default=MIN_CHARS,
                        help='skip stored lines shorter than this; default %d' % MIN_CHARS)
    parser.add_argument('--sample', choices=('latest', 'spread'), default='spread',
                        help='latest takes the newest messages, spread walks the stored history '
                             'evenly; default spread')
    parser.add_argument('--ids', nargs='*', default=None,
                        help='replay exactly these message ids instead of sampling')
    parser.add_argument('--direct', action='store_true',
                        help='ask the judge itself instead of worth_speaking: the verdict is '
                             'measured without spending the group daily judge allowance')
    parser.add_argument('--candidates', default='',
                        help='JSON file of {message_id: candidate text} to judge instead of '
                             'generating again: the judge layer alone then costs one call each')
    parser.add_argument('--json', nargs='?', const='', default=None,
                        help='write the per-sample detail to this JSON file (default: '
                             'artifacts/probe_judge-<stamp>.json); the summary still prints')
    args = parser.parse_args(argv)
    args.limit = max(0, min(LIMIT_CAP, args.limit))
    args.min_chars = max(0, args.min_chars)
    root, chat = Path(args.root), args.chat
    started_at = datetime.now()
    names = group_decide._names(root, chat)['sender']
    reused = json.loads(Path(args.candidates).read_text(encoding='utf-8')) if args.candidates else {}
    if args.ids:
        chosen = [{'message_id': str(mid), 'ts': '', 'text': ''} for mid in args.ids][:args.limit]
    else:
        chosen = sampled(stored(root, chat, args.min_chars), args.limit, args.sample)
    if not chosen:
        print('没有可测的消息：chat=%s 下没有长度 >= %d 的入站消息，'
              '或 --limit 为 0（明细未写）' % (chat, args.min_chars), file=sys.stderr)
        return 2
    report = []
    for index, item in enumerate(chosen, 1):
        message_id = item['message_id']
        payload = read_message(root, chat, message_id)
        if payload is None:
            report.append({'message_id': message_id, 'ts': item['ts'], 'error': 'not-stored'})
            print('[%d/%d] %s 未存库' % (index, len(chosen), message_id), file=sys.stderr)
            continue
        sender = payload['sender']
        sender['display_name'] = names.get(sender['id'], sender['id'] or '群友')
        record = {'message_id': message_id, 'ts': item['ts'] or '', 'sender': sender['display_name'],
                  'text': item['text']}
        if str(reused.get(message_id) or '').strip():
            text, generate_ms = str(reused[message_id]).strip(), None  # saved: only the judge is paid
        else:
            began = time.monotonic()
            candidate = group_decide.fish_candidate(root, chat, payload=payload) or {}
            text = str(candidate.get('text') or '').strip()
            generate_ms = candidate.get('elapsed_ms')
            if not isinstance(generate_ms, int):
                generate_ms = int((time.monotonic() - began) * 1000)
        if not text:
            record['error'] = str(candidate.get('error') or candidate.get('skipped')
                                  or 'empty-candidate')[:200]
            report.append(record)
            print('[%d/%d] %s 无候选：%s' % (index, len(chosen), message_id, record['error']),
                  file=sys.stderr)
            continue
        if args.direct:
            verdict = judge_direct(root, chat, payload)
        else:
            verdict = group_decide.worth_speaking(root, chat, payload=payload, candidate_text=text)
        record.update(candidate=text, generate_ms=generate_ms,
                      speak=bool(verdict.get('speak')), reason=str(verdict.get('reason') or ''),
                      judge_calls=int(verdict.get('judge_calls') or 0))
        record['category'] = None if record['speak'] else category(record['reason'])
        report.append(record)
        print('[%d/%d] %s 生成 %s 秒 → %s %s'
              % (index, len(chosen), message_id,
                 '-' if generate_ms is None else '%.1f' % (generate_ms / 1000),
                 '通过' if record['speak'] else '拒绝', record['reason'][:40]), file=sys.stderr)
    detail_path = ''
    if args.json is not None:
        target = Path(args.json) if args.json else \
            root / 'artifacts' / ('probe_judge-%s.json' % started_at.strftime('%Y%m%d-%H%M%S'))
        if not target.is_absolute():
            target = root / target
        target.parent.mkdir(parents=True, exist_ok=True)
        detail_path = str(target)
    lines = summary_lines(report, args, detail_path)
    if detail_path:
        Path(detail_path).write_text(json.dumps(
            {'chat': chat, 'root': str(root), 'started': started_at.strftime(STAMP),
             'finished': datetime.now().strftime(STAMP), 'direct': bool(args.direct),
             'limit': args.limit, 'min_chars': args.min_chars, 'sample': args.sample,
             'ids': args.ids or None, 'summary': lines, 'samples': report},
            ensure_ascii=False, indent=1), encoding='utf-8')
    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
