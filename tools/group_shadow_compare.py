"""Read-only A/B view: the old DSH group reply next to the Fish shadow candidate.

Nothing here writes to the live stores: every database is opened with mode=ro and
the output is a Markdown file under the batch folder (or stdout with --stdout).

An outbound reply is only shown as this message's A/B partner when the pairing is
checkable: QQ's own quote (outbound.reply_to == inbound channel_message_id), or
the group reply ledger that recorded which remote message a job produced. Being
close in time is not evidence, so a reply nobody can be proven to have answered
stays "unmatched" and is never rendered as the actual answer.

    python tools/group_shadow_compare.py --root <fish root> [--out <md path>] [--limit 20]
"""
from __future__ import annotations
import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# Only used to report that unrelated outbound traffic exists around a message.
WINDOW = timedelta(seconds=180)
UNCONFIRMED = '未能确认匹配（无精确关联）'
STATUSES = ('matched', 'unmatched', 'shadow_only', 'dsh_only')


def _db(path: Path):
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql, args=()):
    if conn is None:
        return []
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _clip(text, limit=180):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit] + '…'


def _key(row):
    remote = str(row['channel_message_id'] or '')
    return remote or 'ts:' + str(row['ts'] or '')


def _exact_outbound(qq, jobs, chat, message_id):
    """Outbound rows tied to this inbound by QQ's quote or by the reply ledger.

    Returns pairs of (row, source) with one entry per distinct outbound message.
    """
    found = {}
    for row in _rows(qq, 'SELECT ts,text,channel_message_id FROM messages WHERE chat_id=? '
                         'AND direction=? AND reply_to=? ORDER BY id', (chat, 'outbound', message_id)):
        found.setdefault(_key(row), (row, 'reply_to'))
    roots = (message_id, 'active:' + message_id)
    for row in _rows(jobs, 'SELECT j.remote FROM jobs j LEFT JOIN inputs i ON i.job=j.id '
                           'WHERE j.chat=? AND j.remote<>? AND (j.root IN (?,?) OR i.message=?)',
                     (chat, '', roots[0], roots[1], message_id)):
        remote = str(row['remote'])
        for sent in _rows(qq, 'SELECT ts,text,channel_message_id FROM messages WHERE chat_id=? '
                              'AND direction=? AND channel_message_id=?', (chat, 'outbound', remote)):
            found.setdefault(remote, (sent, 'job'))
    return list(found.values())


def _nearby_outbound(qq, chat, created):
    """How many outbound messages sit around this time, whoever they answered."""
    low = (created - WINDOW).strftime('%Y-%m-%d %H:%M:%S')
    high = (created + WINDOW).strftime('%Y-%m-%d %H:%M:%S')
    return len(_rows(qq, 'SELECT id FROM messages WHERE chat_id=? AND direction=? AND ts>=? AND ts<=?',
                     (chat, 'outbound', low, high)))


def _status(exact, nearby, answered):
    if exact:
        return 'matched' if answered else 'dsh_only'
    if nearby:
        return 'unmatched'
    return 'shadow_only' if answered else 'unmatched'


def collect(root: Path, limit=20):
    root = Path(root)
    shadow = _db(root / 'data/social/group-shadow.db')
    qq = _db(root / 'data/qq/qq.db')
    jobs = _db(root / 'data/social/group-replies.db')
    try:
        rows = _rows(shadow, 'SELECT * FROM shadows ORDER BY id DESC LIMIT ?', (max(1, int(limit)),))
        records = []
        for row in rows:
            item = dict(row)
            chat = str(item.get('chat') or '')
            mid = str(item.get('message_id') or '')
            created = datetime.fromtimestamp(float(item.get('created') or 0))
            inbound = _rows(qq, 'SELECT ts,sender_id,text FROM messages WHERE chat_id=? '
                                'AND channel_message_id=? LIMIT 1', (chat, mid))
            exact = _exact_outbound(qq, jobs, chat, mid)
            nearby = 0 if exact else _nearby_outbound(qq, chat, created)
            shadow_text = _clip(item.get('text'))
            error = str(item.get('error') or '')
            answered = bool(shadow_text) and not error
            records.append({
                'at': created.strftime('%H:%M:%S'),
                'chat': chat,
                'message_id': mid,
                'inbound': _clip(inbound[0]['text']) if inbound else '',
                'sender': str(inbound[0]['sender_id']) if inbound else '',
                'status': _status(exact, nearby, answered),
                'actual': '；'.join(_clip(sent['text']) for sent, _ in exact),
                'actual_ids': [str(sent['channel_message_id'] or '') for sent, _ in exact],
                'sources': sorted({source for _, source in exact}),
                'multiple': len(exact) > 1,
                'nearby': nearby,
                'shadow': shadow_text,
                'elapsed_ms': item.get('elapsed_ms'),
                'error': error,
                'engine': item.get('engine'),
            })
    finally:
        for conn in (shadow, qq, jobs):
            if conn:
                conn.close()
    return records


def summarize(records):
    counts = {name: 0 for name in STATUSES}
    for item in records:
        counts[item['status']] += 1
    return counts


def _actual_cell(item):
    if item['actual']:
        ids = '、'.join(item['actual_ids']) or '-'
        cell = item['actual'] + '（' + ids + '）'
        return cell + (' ⚠ 多条疑似' if item['multiple'] else '')
    return UNCONFIRMED if item['status'] == 'unmatched' else '（未回复）'


def _note(item):
    note = item['error'] or ''
    if item['engine'] == 'fish':
        note = (note + ' ' if note else '') + 'engine=fish（旧记录，按 shadow 读取，从未真发）'
    if item['status'] == 'unmatched' and item['nearby']:
        note = (note + ' ' if note else '') + '同群另有 {} 条未确认归属的出站'.format(item['nearby'])
    if item['sources']:
        note = (note + ' ' if note else '') + '关联依据：' + '、'.join(item['sources'])
    return note or ('engine=' + str(item['engine']))


def render(root: Path, records, *, generated_at=None):
    lines = ['# 影子 A/B 对比：同一批群消息，旧 DSH 回复 vs Fish 影子候选', '',
             '生成时间：' + (generated_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
             '工程根：' + str(root), '',
             '说明：影子模式只算不发。左边是群里真实发出去的 DSH 回复，右边是同一时刻用 fish_chat 主决策算出来的候选。',
             '只有能被精确关联上的出站才会出现在"实际回复"列：QQ 的引用（reply_to）或群回复账本记录的回执号；',
             '仅时间接近的回复不再参与配对，找不到关联就标 unmatched 并显示"未能确认匹配"，绝不冒充实际回复。',
             '影子当前不接群工具（tool_context 为空），所以候选会比将来的真 Fish 朴素，这是已知差异。', '']
    if not records:
        lines += ['## 暂无影子记录', '',
                  '原因判断（按顺序排查）：',
                  '1. 重载之后群里还没有新消息（影子只在一条群消息真正进入 DSH 生成分支时触发）；',
                  '2. 该群 engine 不是 shadow（engine=fish 的旧配置按 shadow 读取并在备注里标注；engine=dsh 不跑影子）；',
                  '3. 消息没有进入生成分支（未开 listen、主人暂停、测试窗口未路由等，此时旧路径不生成，也不会有影子）；',
                  '4. 影子当天额度用完或同一群已有影子在跑，这次被跳过（查 group-shadow.db 的 shadow_usage 与审计 group_shadow_skipped）。',
                  '',
                  '出现记录后重跑本脚本即可生成对比表。', '']
        return '\n'.join(lines)
    counts = summarize(records)
    lines += ['## 统计', '',
              '- 总记录：{}'.format(len(records)),
              '- matched（有精确关联，可对比）：{}'.format(counts['matched']),
              '- unmatched（无精确关联，不参与对比）：{}'.format(counts['unmatched']),
              '- shadow_only（旧路径没回，只有影子候选）：{}'.format(counts['shadow_only']),
              '- dsh_only（旧路径回了，影子没有可用候选）：{}'.format(counts['dsh_only']), '']
    lines += ['| 时间 | 状态 | 群消息 | 旧 DSH 实际回复（回执号） | Fish 影子候选 | 影子耗时 | 备注 |',
              '|---|---|---|---|---|---|---|']
    for item in records:
        lines.append('| {} | {} | {} | {} | {} | {} | {} |'.format(
            item['at'], item['status'], item['inbound'] or '（图片/表情）', _actual_cell(item),
            item['shadow'] or '（空）', item['elapsed_ms'] or '-', _note(item)))
    lines += ['', '## 怎么读这张表', '',
              '- 只有 matched 行可以做 A/B 判断；unmatched 行只说明"这条没找到可核实的回复"，',
              '  想看那段时间群里回了什么，请直接查 qq.db，本表不给未经确认的配对；',
              '- 判"该不该开口"：旧路径开口而影子沉默（dsh_only，或反之 shadow_only），先看群消息本身值不值得接；',
              '- 判"质量"：同一条消息两种回复，看引用是否准确、有没有编内容、语气是否贴合；',
              '- 判"成本"：影子耗时列是这次多花的钱（一次模型调用）与延迟；影子受 shadow_daily_limit 约束，',
              '  当天超额后只会跳过、不再产生记录；',
              '- 判"下一步"：连续若干条影子都比旧路径自然 → 再谈灰度；明显更差 → 先补群侧工具与上下文。', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--out')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--stdout', action='store_true')
    args = parser.parse_args()
    root = Path(args.root)
    text = render(root, collect(root, args.limit))
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
        print('written: ' + args.out)
    if args.stdout or not args.out:
        print(text)


if __name__ == '__main__':
    main()
