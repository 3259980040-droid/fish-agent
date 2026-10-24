"""一条命令看清她现在的状态：能不能说话、说话什么节奏、记住了什么、提醒开不开。

只读。不发送、不调模型，真实 group-policy.db / qq.db / memory.db 在读完前后各算一次
sha256 并直接打印，好让人不必相信"我没写库"这句话。

    python tools/fish_habits.py --root <工程根> --chat group:1043230169 --replay-limit 200

说话节奏一节复用 tools/replay_group_policy.py 的读库与回放函数（listen/reply 由关闭
强制为开启，否则策略层对每条消息都返回"旁听或发言未开启"，问什么都答成 0），因此这里
的开口数与 python tools/replay_group_policy.py 的命令行结果一致——不存在第二份判定
逻辑，策略层改了只改一处。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / 'harness', ROOT / 'tools'):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import replay_group_policy as replay  # noqa: E402  同一份回放逻辑
from group_read import authorized_groups  # noqa: E402

TS = replay.TS_FORMAT
SOCIAL_KINDS = ('topic', 'thought', 'impression')
ALL_KINDS = ('fact', 'preference', 'relationship', 'event', 'promise',
             'topic', 'thought', 'impression')
TEXT_LIMIT = 40
REASON_LIMIT = 16
REASONS_SHOWN = 3
SOCIAL_SHOWN = 3
REMINDER_SWITCH = 'FISH_MEMORY_REMINDER'
ON_VALUES = ('1', 'on', 'true', 'yes')
HASHED = (('group-policy.db', ('data', 'social', 'group-policy.db')),
          ('qq.db', ('data', 'qq', 'qq.db')),
          ('memory.db', ('data', 'qq', 'memory.db')))


def _clock(epoch: float, now: float) -> str:
    moment = datetime.fromtimestamp(float(epoch))
    return moment.strftime('%H:%M' if moment.date() == datetime.fromtimestamp(now).date()
                           else '%m-%d %H:%M')


def _minutes(first: str, last: str) -> float:
    try:
        return (datetime.strptime(last, TS) - datetime.strptime(first, TS)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return 0.0


def _trim(text, limit: int = TEXT_LIMIT) -> str:
    flat = ' '.join(str(text or '').split())
    return flat if len(flat) <= limit else flat[:limit] + '…'


def hashes(root: Path) -> dict:
    return {name: replay.sha256(root.joinpath(*parts)) for name, parts in HASHED}


def registry_switch() -> str:
    """The user-level value that a restart would inherit; '' when unset or not Windows."""
    if sys.platform != 'win32':
        return ''
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            return str(winreg.QueryValueEx(key, REMINDER_SWITCH)[0])
    except (OSError, ImportError):
        return ''


def reminder_state() -> dict:
    """effective is env-first, then the user-level value a restart would inherit.

    memory_reminder.enabled() itself reads only os.environ, so a registry-only 1 is
    a promise about the next start, not about the process that is running now.
    """
    env = str(os.environ.get(REMINDER_SWITCH, '')).strip()
    registry = registry_switch().strip()
    # What the running service does is the only answer that matters; the env of
    # this diagnostic process says nothing about it. /health reports the serving
    # process (checked above), so ask it first and fall back only when it is down.
    live = live_switch()
    effective = live if live in ('on', 'off') else (env or registry)
    on = str(effective).strip().lower() in ON_VALUES
    source = ('正在运行的后台' if live in ('on', 'off')
              else '进程环境变量' if env else ('用户级注册表' if registry else '未设置'))
    note = '' if live in ('on', 'off') else (
        '注册表已置 %s，但当前进程没继承到；memory_reminder 只读进程 env，重启服务后才生效' % registry
        if on else '')
    return {'env': env, 'registry': registry, 'live': live, 'on': on, 'source': source, 'note': note}


def live_switch():
    """'on'/'off' as the serving process reports it, or '' when it cannot be asked."""
    try:
        from tool_host import request
        return str(request(Path(ROOT), '/health', timeout=3).get('memory_reminder', '') or '')
    except Exception:  # noqa: BLE001 - an unreachable service is reported, not raised
        return ''


def group_tool_count(root: Path) -> int:
    """How many tools a group turn is actually handed, read from the live catalogue.

    Counting fish_group_* names in tool_host.py described the executor, not what a
    group turn publishes: capabilities, history, web, the social notes and the dive
    switches all reach the group without that prefix. The catalogue is the fact.
    """
    try:
        import sys
        harness = str(Path(root) / 'harness')
        if harness not in sys.path:
            sys.path.insert(0, harness)
        from fish_chat import tool_definitions
        return len(tool_definitions('qq', 'group', emoji_state={'previewed': set(), 'tools': []}))
    except Exception:  # noqa: BLE001 - a diagnostic must print a number, not raise
        return 0


def memory_facts(root: Path) -> dict:
    """The qq memory store read straight from SQLite: MemoryStore() would create tables."""
    path = root / 'data' / 'qq' / 'memory.db'
    facts = {'path': str(path), 'found': path.is_file(), 'kinds': {}, 'active': 0,
             'social': [], 'social_counts': {kind: 0 for kind in SOCIAL_KINDS},
             'open_events': 0, 'last_open_due': 0.0, 'reminders': 0,
             'last_reminder': None, 'error': ''}
    if not facts['found']:
        return facts
    db = None
    try:
        db = replay._open_ro(path)
        for row in db.execute('SELECT kind,status,COUNT(*) n FROM memories GROUP BY kind,status'):
            bucket = facts['kinds'].setdefault(str(row['kind'] or 'fact'), {'active': 0, 'other': 0})
            count = int(row['n'] or 0)
            if str(row['status'] or '') == 'active':
                bucket['active'] += count
                facts['active'] += count
            else:
                bucket['other'] += count
        for kind in SOCIAL_KINDS:
            facts['social_counts'][kind] = int(db.execute(
                "SELECT COUNT(*) n FROM memories WHERE kind=? AND status='active'",
                (kind,)).fetchone()['n'] or 0)
        facts['social'] = [dict(row) for row in db.execute(
            "SELECT kind,text,updated FROM memories WHERE kind IN (?,?,?) AND status='active' "
            "ORDER BY updated DESC LIMIT ?", (*SOCIAL_KINDS, SOCIAL_SHOWN))]
        events = db.execute("SELECT COUNT(*) n,MAX(due) due FROM memories WHERE kind='event' "
                            "AND action='open'").fetchone()
        facts['open_events'] = int(events['n'] or 0)
        facts['last_open_due'] = float(events['due'] or 0)
        try:
            facts['reminders'] = int(db.execute('SELECT COUNT(*) n FROM memory_reminders'
                                                ).fetchone()['n'] or 0)
            last = db.execute('SELECT memory_id,due,state,receipt,updated,attempts '
                              'FROM memory_reminders ORDER BY updated DESC LIMIT 1').fetchone()
            facts['last_reminder'] = dict(last) if last else None
        except Exception as exc:  # noqa: BLE001 - the ledger appears with the first reminder
            facts['error'] = 'memory_reminders 读不到: ' + type(exc).__name__ + ': ' + str(exc)[:80]
    except Exception as exc:  # noqa: BLE001 - a locked or older store must not end the report
        facts['error'] = 'memory.db 读不到: ' + type(exc).__name__ + ': ' + str(exc)[:80]
    finally:
        if db is not None:
            db.close()
    return facts


def group_state(root: Path, chat: str) -> tuple[dict, dict, bool]:
    stored, found = replay.real_config(root, chat)
    routed = {str(row['chat']): row for row in authorized_groups(root)}
    return stored, routed.get(chat) or {}, found


def pacing(root: Path, chat: str, config: dict, limit: int) -> dict:
    """One replay of real recent messages; the gates stay real except listen/reply."""
    feed = replay.read_messages(root, chat, limit, '')
    messages = feed['messages']
    bot_id, bot_source = replay.bot_identity(root, chat, '')
    outcome = replay.replay_pass(chat, messages, bot_id, replay.outbound_ids(root, chat),
                                 {**config, **replay.FORCED}, replay.SEED)
    first = messages[0]['ts'] if messages else ''
    last = messages[-1]['ts'] if messages else ''
    span = _minutes(first, last)
    candidates = outcome['candidates']
    return {'messages': len(messages), 'rows_read': feed['read'],
            'dropped_empty': feed['dropped_empty'], 'skipped': len(feed['skipped']),
            'first_ts': first, 'last_ts': last, 'span_minutes': round(span, 1),
            'candidates': candidates, 'silent': outcome['silent'],
            'by_reason': outcome['by_reason'], 'by_reason_raw': outcome['by_reason_raw'],
            'errors': len(outcome['errors']), 'bot_id': bot_id, 'bot_source': bot_source,
            'minutes_per_line': round(span / candidates, 1) if candidates and span > 0 else 0.0,
            'forced': dict(replay.FORCED)}


def top_reasons(pacing_result: dict) -> list:
    ranked = sorted(((count, name) for name, count in pacing_result['by_reason'].items() if count),
                    key=lambda item: (-item[0], item[1]))
    parts = []
    for count, name in ranked[:REASONS_SHOWN]:
        if name != '其它':
            parts.append(f'{name} {count}')
            continue
        raw = sorted((text for text in pacing_result['by_reason_raw']
                      if replay.classify(text) == '其它'),
                     key=lambda text: -pacing_result['by_reason_raw'][text])
        inner = '、'.join(f'{_trim(text, REASON_LIMIT)} {pacing_result["by_reason_raw"][text]}'
                          for text in raw[:2])
        parts.append(f'其它 {count}（{inner}）')
    return parts


def build(root: Path, chat: str, limit: int, now: float) -> dict:
    config, entry, stored_found = group_state(root, chat)
    facts = memory_facts(root)
    reminder = reminder_state()
    tools = group_tool_count(root)
    rhythm = pacing(root, chat, config, limit)

    listen, reply = bool(config.get('listen')), bool(config.get('reply'))
    dive_until = float(config.get('dive_until') or 0)
    limits_on = bool(config.get('reply_limits'))
    caps = (config.get('max_active_hour'), config.get('max_total_min'),
            config.get('max_unanswered_active'))

    if not dive_until:
        dive = '未潜水'
    elif dive_until <= now:
        dive = f'未潜水（上次潜水已于 {_clock(dive_until, now)} 到点）'
    else:
        dive = f'潜水到 {_clock(dive_until, now)}（还剩 {(dive_until - now) / 60:.0f} 分钟）'

    section1 = [
        f"engine={config.get('engine')} · read={str(bool(config.get('read'))).lower()} · "
        f"listen={str(listen).lower()} · learn={str(bool(config.get('learn'))).lower()} · "
        f"reply={str(reply).lower()}" + ('' if stored_found else '（库里没有这个群，按默认值）'),
        f"潜水：{dive} · 唤醒概率 {float(config.get('wake_probability') or 0):g} · "
        f"主动冷却 {float(config.get('active_cooldown_s') or 0):g}s · "
        f"回访窗口 {float(config.get('followup_s') or 0):g}s",
        f"回复闸门：reply_limits={str(limits_on).lower()} → 每小时主动 {caps[0]} 条 · "
        f"每分钟全部 {caps[1]} 条 · 无人接续 {caps[2]} 条"
        + ('' if limits_on else '（三条上限不参与判定）'),
        ('路由：' + str(entry.get('participation_source')) + ' — ' + str(entry.get('participation_note')))
        if entry else '路由：该群不在 group_read.authorized_groups 里（主人未授权读取，或 read 未开）',
        '能不能说话：' + ('能，listen/reply 都开' if listen and reply else
                     '不能，旁听和发言都关着' if not (listen or reply) else
                     '半开：listen 或 reply 只有一个开，策略层要求两个都开才算能说')
        + ('，但现在正在潜水' if dive_until > now else ''),
    ]

    reasons = top_reasons(rhythm)
    if rhythm['messages'] and rhythm['candidates']:
        rate = (f"约每 {rhythm['minutes_per_line']:g} 分钟开口一句"
                f"（这段真实消息覆盖 {rhythm['span_minutes']:g} 分钟）")
    elif rhythm['messages']:
        rate = '按这套闸门一次都不会开口（全沉默）'
    else:
        rate = '没有可回放的真实消息'
    section2 = [
        (f"回放最近 {rhythm['messages']} 条真实入站（{rhythm['first_ts']} → {rhythm['last_ts']}），"
         f"listen/reply 强制开启作为对照") if rhythm['messages']
        else '回放：这个群没有任何可回放的真实入站',
        f"开口 {rhythm['candidates']} 次 · 沉默 {rhythm['silent']} 次"
        + (f" · 异常 {rhythm['errors']} 条" if rhythm['errors'] else ''),
        '主要沉默原因：' + (' · '.join(reasons) if reasons
                        else ('（没有消息）' if not rhythm['messages'] else '（无，每条都开口）')),
        '折算：' + rate,
    ]

    kinds = ' · '.join(
        f"{kind} {facts['kinds'][kind]['active']}" +
        (f"（另有 {facts['kinds'][kind]['other']} 条非活跃）" if facts['kinds'][kind]['other'] else '')
        for kind in ALL_KINDS if kind in facts['kinds']) or '没有记忆'
    section3 = [f"按 kind：{kinds}",
                '社交：' + ' · '.join(f'{kind} {facts["social_counts"][kind]}' for kind in SOCIAL_KINDS)]
    if facts['social']:
        section3 += [f"  最近一条 {item['kind']}：{_trim(item['text'])}" for item in facts['social']]
    else:
        section3.append('  最近 3 条社交记忆：没有')
    section3.append(
        f"event(action=open) {facts['open_events']} 条"
        + (f"，最近一条 due {datetime.fromtimestamp(facts['last_open_due']).strftime(TS)}"
           if facts['last_open_due'] else '，没有到期时间')
        + (f" · {facts['error']}" if facts['error'] else ''))

    last = facts['last_reminder']
    section4 = [
        f"【{REMINDER_SWITCH}】" + ('开' if reminder['on'] else '关')
        + f"（来源：{reminder['source']}；进程 env={reminder['env'] or '未设'}，"
          f"用户级注册表={reminder['registry'] or '未设'}）",
        f"memory_reminders 台账 {facts['reminders']} 条"
        + (f" · 最近一条 state={last['state']} · due="
           f"{datetime.fromtimestamp(float(last['due'])).strftime(TS)} · "
           f"receipt={last['receipt'] or '—'} · 重试 {last['attempts']} 次" if last else ' · 台账为空'),
    ]
    if reminder['note']:
        section4.append('  ' + reminder['note'])

    social_total = sum(facts['social_counts'].values())
    opening = '现在群是' + ('开的' if listen and reply else '关的')
    if dive_until > now:
        opening += f'（潜水到 {_clock(dive_until, now)}）'
    estimate = ('约每 %g 分钟开口一次' % rhythm['minutes_per_line']) if rhythm['candidates'] \
        else '一次都不会开口'
    conclusion = (opening + '；打开后按最近 %d 条真实消息估算，%s；她手上有 %d 个群工具、'
                  '%d 条社交记忆（活跃记忆 %d 条）；提醒开关%s。'
                  % (rhythm['messages'], estimate, tools, social_total, facts['active'],
                     '已开' if reminder['on'] and not reminder['note'] else
                     '注册表已开、当前进程没继承' if reminder['on'] else '未开'))

    return {'chat': chat, 'root': str(root), 'stored_row_found': stored_found,
            'policy': {key: config.get(key) for key in
                       ('engine', 'read', 'listen', 'learn', 'reply', 'dive_until',
                        'wake_probability', 'active_cooldown_s', 'followup_s',
                        'reply_limits', 'max_active_hour', 'max_total_min',
                        'max_unanswered_active')},
            'participation': entry, 'pacing': rhythm, 'memory': facts,
            'reminder': reminder, 'group_tools': tools,
            'sections': {'1': section1, '2': section2, '3': section3, '4': section4},
            'conclusion': conclusion}


def report_text(report: dict, before: dict, after: dict) -> str:
    lines = []
    for key, title in (('1', '她能不能说话'), ('2', '说话节奏'),
                       ('3', '她记住了什么'), ('4', '提醒')):
        lines.append(f'【{key} {title}】')
        lines += ['  ' + row for row in report['sections'][key]]
    lines.append('【5 一句话】')
    lines.append('  ' + report['conclusion'])
    evidence = ' · '.join(
        f'{name} {before[name][:16] or "（缺失）"}{("→" + after[name][:16] + " 一致") if before[name] == after[name] else ("→" + after[name][:16] + " 变了")}'
        for name, _ in HASHED)
    lines.append('【只读证据】' + ('未发送、未调模型；'
                                 if before == after else '有库文件变化，需人工确认；') + evidence)
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='一条命令看她的生活状态：能不能说话、说话节奏、记住了什么、提醒开不开')
    parser.add_argument('--root', default=str(ROOT), help='工程根')
    parser.add_argument('--chat', default='group:1043230169', help='群范围，如 group:1043230169')
    parser.add_argument('--replay-limit', type=int, default=200, help='回放最近多少条真实入站（默认 200）')
    parser.add_argument('--json', default='', help='把结构化结果另存为 JSON')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'group:[0-9]{5,15}', str(args.chat)):
        parser.error('群范围格式应为 group:群号')

    root = Path(args.root).expanduser().resolve()
    before = hashes(root)
    report = build(root, args.chat, max(0, args.replay_limit), time.time())
    after = hashes(root)
    report['safety'] = {'sent': 0, 'model_calls': 0, 'before': before, 'after': after,
                        'unchanged': before == after}
    print(report_text(report, before, after))
    if args.json:
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('JSON：' + str(out))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
