#!/usr/bin/env python3
"""真模型复跑：只比"先判后生成"的调用次数，一条消息都不发。

tools/rehearse_group.py 照抄真实群配置，而线上这一小时她已经说过 16 到 18 条、超过新的
目标 8 条，于是滑动窗口把 8 条里的 6 条直接按下，预筛根本没被问到。这个脚本做同一件事，
只把 target_replies_per_hour 强制为 0（不限），让每一条普通消息都真的走一次预筛，好看出
"预筛若干 + 生成少量 + 判断少量"到底是什么数量级。

隔离沿用 rehearse_group：临时根、假通道、虚拟时钟、线上库前后 sha256 比对。
用法：python tools/verify_group_prescreen_live.py --limit 8
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]

import rehearse_group as R  # noqa: E402

CHAT = 'group:1043230169'
BOT_ID = '1417334937'
ORIGINS = ('group-prescreen', 'group-fish:', 'group-judge')


def usage(root):
    path = Path(root) / 'data/qq/model-usage.db'
    out = {}
    if not path.exists():
        return out
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        for origin, count in db.execute('SELECT origin, COUNT(*) FROM requests GROUP BY origin'):
            for prefix in ORIGINS:
                if str(origin).startswith(prefix):
                    out[prefix] = out.get(prefix, 0) + int(count)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=8)
    parser.add_argument('--chat', default=CHAT)
    args = parser.parse_args(argv)
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    limit = max(1, min(R.MAX_LIMIT, int(args.limit)))
    chat = args.chat
    before = {name: R.sha256(ROOT / name) for name in R.ONLINE_DBS + R.EXTRA_DBS if (ROOT / name).exists()}
    rows, bot_ids = R.inbound_rows(ROOT, chat, limit=limit, since=None, ids=[])
    R.adopt_user_env()
    from group_policy import GroupPolicy

    temp_root = Path(tempfile.mkdtemp(prefix='fish-prescreen-'))
    try:
        R.stage_root(ROOT, temp_root)
        R.reset_budgets(temp_root, chat)
        policy = GroupPolicy(temp_root)
        effective = policy.configure(chat, {'listen': True, 'reply': True, 'target_replies_per_hour': 0})
        policy_path = policy.path
        mapped, stored_cursor = R.observe_map(policy_path, chat)
        first = next((mapped[str(row['channel_message_id'])] for row in rows
                      if str(row['channel_message_id']) in mapped), None)
        wanted = [str(row['channel_message_id']) for row in rows]
        rewound = R.rewind(policy_path, chat, first, wanted) if first is not None else {
            'cursor': stored_cursor, 'observations_dropped': 0, 'dedup_cleared': 0, 'reservations_dropped': 0}
        clock, channel = R.VirtualClock(), R.FakeChannel(BOT_ID)
        real_calls, audits, generated, verdicts, gates, logs = [], [], [], [], [], []
        calls_before = usage(temp_root)
        started = time.time()
        with R.isolation(ROOT, temp_root):
            clock.install()
            try:
                _, items = asyncio.run(R.rehearse(
                    rows=rows, chat=chat, bot_ids=bot_ids, temp_root=temp_root,
                    args=types.SimpleNamespace(gap=0), clock=clock, channel=channel,
                    real_calls=real_calls, audits=audits, generated=generated, verdicts=verdicts,
                    gates=gates, logs=logs, policy_path=policy_path,
                    wake=float(effective['wake_probability'])))
            finally:
                clock.restore()
        calls_after = usage(temp_root)
    finally:
        after = {name: R.sha256(ROOT / name) for name in R.ONLINE_DBS + R.EXTRA_DBS if (ROOT / name).exists()}

    delta = {key: calls_after.get(key, 0) - calls_before.get(key, 0) for key in ORIGINS}
    print('群：%s ｜ %d 条真实入站（目标条数强制为 0，让每条都真的走预筛）' % (chat, len(items)))
    print('生效配置：' + ' '.join('%s=%s' % (key, effective[key]) for key in (
        'prescreen_enabled', 'prescreen_threshold', 'prescreen_timeout_s', 'energy_enabled',
        'energy_cost', 'target_replies_per_hour', 'active_cooldown_s', 'max_active_hour')))
    print('回退：cursor=%s 丢弃观察 %d 清去重 %d 清预留 %d' % (
        rewound['cursor'], rewound['observations_dropped'], rewound['dedup_cleared'], rewound['reservations_dropped']))
    print('真实通道：start %d 次 / action %d 次' % (real_calls.count('start'), real_calls.count('action')))
    print('模型调用（真实 API）：预筛 %d 次 / 生成 %d 次 / 判断 %d 次' % (
        delta['group-prescreen'], delta['group-fish:'], delta['group-judge']))
    print('旧顺序同样这批消息至少要生成 %d 次（每条一次），再判断最多 %d 次' % (len(items), len(items)))
    print('')
    for item in items:
        mark = ('→ 说：「%s」' % R.short(item['reply'], 60)) if item['sent'] else ('→ 沉默：%s' % R.short(item['reason'], 60))
        print('%2d %s 「%s」  %s' % (item['index'], item['ts'][11:19], R.short(item['display'], 18), mark))
        for audit in item['audits']:
            if audit['kind'].startswith(('group_prescreen', 'group_energy')):
                print('      账：%s %s' % (audit['kind'], R.trim_chat(audit['reason'], chat)[:110]))
    print('')
    print('她说 %d 句 ｜ 沉默 %d 句 ｜ 用时 %.1fs' % (
        sum(1 for item in items if item['sent']), sum(1 for item in items if not item['sent']),
        time.time() - started))
    changed = [name for name in R.ONLINE_DBS + R.EXTRA_DBS
               if name in before and before[name] != after.get(name)]
    print('线上库 sha256 前后：' + ('一致' if not changed else '有变动 ' + ','.join(changed) +
          '（彩排只读线上库；这段时间线上服务自己在收发，变动来自它）'))
    print('临时根：%s' % temp_root)
    return 0


if __name__ == '__main__':
    sys.exit(main())
