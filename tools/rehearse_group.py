#!/usr/bin/env python3
"""群聊彩排：真实模型、真实判断、真实流水线，但一条消息都不发。

把 qq.db 里真实的入站消息重新喂给 QQAgentLoop._on_group，走完整 fish 分支
（observe -> 记忆 -> 候选生成 -> worth_speaking -> 预留 -> 发送前复检 ->
_deliver_reply）。唯一的传输层是 FakeChannel，它只记录 action，不打开任何
websocket、不碰 NapCat。

隔离：真实的群配置被抄进一个临时根，只强制 listen/reply=True（其余保持真实
值），记忆、账本、审计、模型用量全部写临时根。运行期间任何试图打开或写入
线上根（本仓库目录）的 sqlite / 文件写操作都会直接抛错，所以"没碰线上 data/"
是运行期强制出来的，不是事后自述。跑完再对线上三个库做 sha256 前后比对。

不发送、不调除"生成 / 值不值得说"以外的模型、不改 harness 下任何文件。

用法：
  python tools/rehearse_group.py                       # 最近 10 条
  python tools/rehearse_group.py --limit 6             # 先看句子是否合理
  python tools/rehearse_group.py --limit 12 --json out.json
  python tools/rehearse_group.py --since "2026-10-22 13:00:00"
  python tools/rehearse_group.py --ids 3055,3072       # 指定消息号复跑
  python tools/rehearse_group.py --gap 2               # 每条之间停 2 秒
  python tools/rehearse_group.py --wake 1.0            # 覆盖唤醒概率（默认用真实值）
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import pathlib
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]

DEFAULT_CHAT = 'group:1043230169'
# 线上三个核心库：跑完必须与跑前逐字节一致。
ONLINE_DBS = ('data/qq/qq.db', 'data/qq/memory.db', 'data/social/group-policy.db')
# 顺带一起校验的账本/审计库，只是多一层证据。
EXTRA_DBS = ('data/qq/governance.db', 'data/social/group-shadow.db', 'data/qq/model-usage.db')
MAX_LIMIT = 20

REASON_LABELS = (
    ('本次未被唤醒', '未被唤醒（唤醒概率未命中）'),
    ('主动回复冷却中', '主动回复冷却中'),
    ('主动回复达到每小时上限', '主动回复每小时上限'),
    ('全部回复达到每分钟上限', '全部回复每分钟上限'),
    ('正在合并连续消息', '合并窗口内被后一条取代'),
    ('群策略先放行、紧接着的预留又拒了', '同一游标二次判定未通过'),
    ('cooldown', '主动回复冷却中'),
    ('主动回复冷却中', '主动回复冷却中'),
    ('潜水到', '潜水期间不开口'),
    ('无人接续', '上限：没人接话，继续旁听'),
    ('当前仅回应提及', '仅回应提及模式'),
    ('旁听或发言未开启', '群开关关闭'),
    ('没有新消息', '没有新消息'),
    ('没有上下文', '没有可读上下文'),
    ('正在处理候选', '另一条候选正在处理'),
    ('paused', '主人暂停'),
    ('cancel', '本轮被取消'),
)


def say(*parts):
    print(*parts, flush=True)


def adopt_user_env():
    """真实服务从 User 环境读 key（fish_desktop.services.load_user_env），这里照做。

    只取模型与通道 token 这几个名字，值既不打印也不写盘。
    """
    if os.name != 'nt':
        return []
    import winreg
    names = ('FISH_QQ_DEEPSEEK_API_KEY', 'FISH_RESEARCH_DEEPSEEK_API_KEY')
    loaded = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            for name in names:
                if os.environ.get(name):
                    continue
                try:
                    value, _kind = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if value:
                    os.environ[name] = str(value)
                    loaded.append(name)
    except OSError:
        return loaded
    return loaded


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- 参数与选条


def parse_stamp(value):
    """'2026-10-22 13:00:00' / '13:00:00' / '13:00' / epoch 秒。"""
    text = str(value).strip()
    if not text:
        raise SystemExit('--since 需要一个时间')
    if text.replace('.', '', 1).isdigit():
        return float(text)
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S'):
        try:
            return time.mktime(time.strptime(text, pattern))
        except ValueError:
            continue
    day = time.strftime('%Y-%m-%d')
    for pattern in ('%H:%M:%S', '%H:%M'):
        try:
            return time.mktime(time.strptime(day + ' ' + text, '%Y-%m-%d ' + pattern))
        except ValueError:
            continue
    raise SystemExit('看不懂的时间：' + text)


def stamp_epoch(value):
    return time.mktime(time.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S'))


def inbound_rows(root, chat, *, limit, since, ids):
    """qq.db 里的真实入站消息，按时间正序；limit 取最后 N 条。"""
    store = root / 'data/qq/qq.db'
    if not store.exists():
        raise SystemExit('找不到线上群消息库：' + str(store))
    names = {}
    people = root / 'data/social/people.db'
    if people.exists():
        with contextlib.closing(sqlite3.connect(str(people))) as db:
            db.row_factory = sqlite3.Row
            names = {str(row['user_id']): str(row['card'] or row['nickname'] or '')
                     for row in db.execute('SELECT user_id, nickname, card FROM members')}
    with contextlib.closing(sqlite3.connect(str(store))) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            "SELECT id, ts, chat_id, user_id, text, reply_to, channel_message_id, segments "
            "FROM messages WHERE chat_id=? AND direction='inbound' ORDER BY ts, id", (chat,))]
        bot_ids = {str(row['channel_message_id']) for row in db.execute(
            "SELECT channel_message_id FROM messages WHERE chat_id=? AND direction='outbound' "
            "AND status='sent'", (chat,))}
    for row in rows:
        row['ts_epoch'] = stamp_epoch(row['ts'])
        row['name'] = names.get(str(row['user_id']), '')
        try:
            row['segment_list'] = json.loads(row['segments'] or '[]')
        except ValueError:
            row['segment_list'] = [{'type': 'text', 'text': str(row['text'] or '')}]
    if ids:
        wanted = {str(item).strip() for item in ids if str(item).strip()}
        picked = [row for row in rows
                  if str(row['id']) in wanted or str(row['channel_message_id']) in wanted]
        if not picked:
            raise SystemExit('--ids 没有匹配到任何入站消息：' + ','.join(sorted(wanted)))
        picked.sort(key=lambda row: (row['ts_epoch'], row['id']))
        return picked, bot_ids
    if since is not None:
        rows = [row for row in rows if row['ts_epoch'] >= since]
        if not rows:
            raise SystemExit('--since 之后没有入站消息')
    return rows[-limit:], bot_ids


# ---------------------------------------------------------------- 临时根


def copy_sqlite(source, target):
    """用备份接口拿一致快照：线上服务可能正在写这个库。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    try:
        with contextlib.closing(sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True)) as src:
            with contextlib.closing(sqlite3.connect(str(target))) as dst:
                src.backup(dst)
        return
    except sqlite3.Error:
        pass
    shutil.copy2(source, target)


def stage_root(real_root, temp_root):
    """把真实配置/记忆/账本抄进临时根；只要读，不动线上。"""
    harness = real_root / 'harness'
    shutil.copytree(harness, temp_root / 'harness',
                    ignore=shutil.ignore_patterns('runtime', 'tmp', '__pycache__', 'settings.lock'),
                    dirs_exist_ok=True)
    channel = real_root / 'channels/fish_qq/config.json'
    if channel.exists():
        (temp_root / 'channels/fish_qq').mkdir(parents=True, exist_ok=True)
        shutil.copy2(channel, temp_root / 'channels/fish_qq/config.json')
    for sub in ('qq', 'social', 'system'):
        source = real_root / 'data' / sub
        if not source.is_dir():
            continue
        for item in sorted(source.iterdir()):
            if not item.is_file():
                continue
            target = temp_root / 'data' / sub / item.name
            if item.name.endswith('.db'):
                copy_sqlite(item, target)
            elif item.name.endswith('.json'):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
    # 表情库的图片是散文件：库表抄过来了，原件也得跟着来，否则临时根里每个 ID 都
    # 读不出原件，她也就永远挑不出一张真能发的表情。
    emoji = real_root / 'data/qq/emoji'
    if emoji.is_dir():
        shutil.copytree(emoji, temp_root / 'data/qq/emoji', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('*.db', '__pycache__'))
        if (emoji / 'library.db').is_file():
            (temp_root / 'data/qq/emoji').mkdir(parents=True, exist_ok=True)
            copy_sqlite(emoji / 'library.db', temp_root / 'data/qq/emoji/library.db')
    shared = real_root / 'data/shared'
    if shared.is_dir():
        shutil.copytree(shared, temp_root / 'data/shared', dirs_exist_ok=True)
    for sub in ('qq', 'social', 'shared', 'system'):
        (temp_root / 'data' / sub).mkdir(parents=True, exist_ok=True)
    (temp_root / 'harness/runtime').mkdir(parents=True, exist_ok=True)


def reset_budgets(temp_root, chat):
    """把"今天已花掉的付费调用额度"在临时库里重开，并说清真实值是多少。

    判断额度和影子额度是计费预算，不是她的性格：今天 80 次判断已经花光，照抄
    过来的话整段彩排只会得到 judge-daily-limit，值不值得说这一步根本跑不到。
    所以临时库按新的一天重开，真实用量照原样写进报告。
    """
    path = temp_root / 'data/social/group-shadow.db'
    day = time.strftime('%Y-%m-%d')
    report = {'day': day, 'real': {}, 'reopened': True}
    if not path.exists():
        return report
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        for table in ('fish_judge_usage', 'shadow_usage'):
            try:
                row = db.execute('SELECT calls FROM ' + table + ' WHERE chat=? AND day=?',
                                 (chat, day)).fetchone()
            except sqlite3.Error:
                continue
            report['real'][table] = int(row[0]) if row and row[0] is not None else 0
            db.execute('DELETE FROM ' + table + ' WHERE chat=? AND day=?', (chat, day))
        db.commit()
    return report


@contextlib.contextmanager
def isolation(real_root, temp_root):
    """运行期强制隔离：任何碰线上根的 sqlite 或写盘调用都直接抛错。"""
    real = real_root.resolve()
    temp = temp_root.resolve()
    touched = {'blocked': [], 'checked': 0}

    def refuse(path, action):
        touched['checked'] += 1
        try:
            resolved = Path(os.fspath(path)).resolve()
        except (OSError, ValueError, TypeError):
            return
        if resolved == temp or temp in resolved.parents:
            return
        if resolved == real or real in resolved.parents:
            touched['blocked'].append(f'{action}: {resolved}')
            raise AssertionError(f'彩排拒绝{action}线上路径：{resolved}')

    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        refuse(database, '打开数据库')
        return real_connect(database, *args, **kwargs)

    def wrap(method, action, *, mode_arg=False):
        original = getattr(pathlib.Path, method)

        def patched(self, *args, **kwargs):
            if mode_arg:
                mode = str(kwargs.get('mode') or (args[0] if args else 'r'))
                if any(flag in mode for flag in ('w', 'a', 'x', '+')):
                    refuse(self, action)
            else:
                refuse(self, action)
            return original(self, *args, **kwargs)

        return original, patched

    saved = []
    try:
        sqlite3.connect = connect
        for method, action, mode_arg in (
                ('write_text', '写文件', False), ('write_bytes', '写文件', False),
                ('open', '写打开文件', True), ('mkdir', '建目录', False),
                ('replace', '替换文件', False), ('rename', '改名', False),
                ('unlink', '删除文件', False)):
            original, patched = wrap(method, action, mode_arg=mode_arg)
            saved.append((method, original))
            setattr(pathlib.Path, method, patched)
        yield touched
    finally:
        sqlite3.connect = real_connect
        for method, original in saved:
            setattr(pathlib.Path, method, original)


# ---------------------------------------------------------------- 虚拟时钟


class VirtualClock:
    """把 time.time() 钉在每条消息的真实时刻，让它再按真实速度走。

    彩排是在几秒内把二十分钟的聊天重放一遍。如果时钟就是"现在"，那么 180 秒
    主动冷却、每小时 3 条、每分钟 6 条这些真实节奏限制全部会被压缩成"刚说完
    就冷却"，沉默原因就不是她的判断而是重放速度了。钉到消息的真实时间戳后，
    这些限制读到的就是它们在那一刻的真实值；4 秒合并窗口、5 秒回后续答窗口
    仍然是真实等待（虚拟时间在睡梦里照常前进）。
    """

    def __init__(self):
        self._real = time.time
        self._offset = 0.0
        self.active = False

    def now(self):
        return self._real() + self._offset if self.active else self._real()

    def set(self, stamp):
        self._offset = float(stamp) - self._real()

    def install(self):
        time.time = self.now
        self.active = True

    def restore(self):
        time.time = self._real
        self.active = False


# ---------------------------------------------------------------- 假通道


class FakeChannel:
    """唯一的传输层：记录 action，从不发送。"""

    def __init__(self, bot_id):
        self.bot = {'id': bot_id}
        self.sent = []

    async def action(self, name, args, timeout=0):
        self.sent.append({'name': name, **args})
        return {'ok': True, 'payload': {'message_id': 'rehearsal-' + str(len(self.sent))}}


# ---------------------------------------------------------------- 展示


def flat(value):
    return ''.join(str(value or '').split())


def short(value, limit):
    body = flat(value)
    if not body:
        return ''
    return body[:limit] + ('…' if len(body) > limit else '')


def inbound_display(row, bot_id):
    body = flat(row['text'])
    if body:
        return body
    marks = {'image': '[图片]', 'emoji': '[表情]', 'face': '[表情]', 'audio': '[语音]',
             'video': '[视频]', 'file': '[文件]'}
    parts = []
    for segment in row['segment_list']:
        if not isinstance(segment, dict):
            continue
        kind = str(segment.get('type') or '')
        if kind == 'text':
            continue
        if kind == 'unknown':
            note = str(segment.get('placeholder') or '').strip()
            raw = str(segment.get('raw_type') or '未知')
            parts.append(note or ('[不支持:' + raw + ']'))
        elif kind == 'at':
            who = '我' if str(segment.get('user_id') or '') == bot_id else '某人'
            parts.append('[@' + who + ']')
        else:
            parts.append(marks.get(kind, '[' + kind + ']'))
    return ''.join(parts) or '（空消息）'


def label_of(reason):
    text = str(reason or '').strip()
    for prefix, label in REASON_LABELS:
        if text.startswith(prefix):
            return label
    return text.split('：')[0].split(':')[0][:24] or '未说明'


def trim_chat(reason, chat):
    text = str(reason or '').strip()
    if text.endswith(chat):
        text = text[: -len(chat)].strip()
    return text


# ---------------------------------------------------------------- 主流程


def build_payload(row, bot_id, bot_message_ids):
    reply_to = str(row['reply_to'] or '')
    segments = row['segment_list']
    mentions = any(isinstance(s, dict) and s.get('type') == 'at'
                   and str(s.get('user_id') or '') == bot_id for s in segments)
    return {'chat_type': 'group', 'chat_id': row['chat_id'],
            'message_id': str(row['channel_message_id']),
            'sender': {'id': str(row['user_id']), 'display_name': row['name'] or '群友'},
            'reply_to': reply_to, 'mentions_bot': mentions,
            'reply_to_bot': bool(reply_to and reply_to in bot_message_ids),
            'segments': segments}


def reservations_of(path, chat):
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        db.row_factory = sqlite3.Row
        return {row['token']: dict(row) for row in
                db.execute('SELECT token,cursor,kind,state,finished FROM reservations WHERE chat=?', (chat,))}


def observe_map(policy_path, chat):
    with contextlib.closing(sqlite3.connect(str(policy_path))) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT seq, id FROM observations WHERE chat=?', (chat,)).fetchall()
        cursor = db.execute('SELECT cursor, handled, unanswered FROM groups WHERE chat=?', (chat,)).fetchone()
    return {str(row['id']): int(row['seq']) for row in rows}, (cursor['cursor'] if cursor else 0)


def rewind(policy_path, chat, start_seq, ids):
    """把群状态退回到第一条被重放消息之前，让上下文自然重建。"""
    keep = max(0, int(start_seq) - 1)
    with contextlib.closing(sqlite3.connect(str(policy_path))) as db:
        db.execute('BEGIN IMMEDIATE')
        dropped_obs = db.execute('DELETE FROM observations WHERE chat=? AND seq>?', (chat, keep)).rowcount
        dropped_dedup = sum(db.execute('DELETE FROM dedup WHERE chat=? AND id=?', (chat, mid)).rowcount
                            for mid in ids)
        last = db.execute('SELECT seq FROM observations WHERE chat=? ORDER BY seq DESC LIMIT 1',
                          (chat,)).fetchone()
        cursor = min(int(last[0]), keep) if last and last[0] is not None else 0
        # handled 必须一起退回来：它领先 cursor 时每一条都会被读成"没有新消息"。
        db.execute('UPDATE groups SET cursor=?, handled=? WHERE chat=?', (cursor, cursor, chat))
        dropped_res = db.execute('DELETE FROM reservations WHERE chat=? AND cursor>?',
                                 (chat, cursor)).rowcount
        db.commit()
    return {'cursor': cursor, 'observations_dropped': dropped_obs,
            'dedup_cleared': dropped_dedup, 'reservations_dropped': dropped_res}


def model_calls(usage_path, origins=('group-fish:', 'group-judge')):
    out = {}
    if not Path(usage_path).exists():
        return out
    with contextlib.closing(sqlite3.connect(str(usage_path))) as db:
        for row in db.execute('SELECT origin, COUNT(*) FROM requests GROUP BY origin'):
            origin = str(row[0] or '')
            for prefix in origins:
                if origin.startswith(prefix):
                    out[prefix] = out.get(prefix, 0) + int(row[1] or 0)
    return out


# audit 只说得出这几句笼统的跳过原因，真正的理由在群策略的 gate 里。
GENERIC_SKIPS = {
    'limits': '群频率限制（冷却 / 每小时 / 每分钟 / 合并窗口）',
    'not-woken': '本次未被唤醒（唤醒概率未命中）',
    'dive': '潜水期间被叫到也没开口',
    'cursor-moved': '这条被更新的消息顶掉了（游标已移动）',
}


def stage_report(item, chat, wake=1.0):
    """每条的结论：她说了什么，或者为什么不说。"""
    if item['sent']:
        return '说', '', item['reply']
    judge = item['judge']
    if judge and judge.get('speak') is False:
        reason = trim_chat(judge.get('reason'), chat)
        if reason == 'model-no-reply':
            return '沉默', '模型自己选择不回（空回复或沉默标记）', ''
        if reason.startswith('cooldown'):
            left = reason.split(':', 1)[1].replace('s-left', '') if ':' in reason else ''
            return '沉默', '主动回复冷却中（还剩 %s 秒）' % left, ''
        if reason.startswith('judge-'):
            return '沉默', '判断环节没走完：' + reason, ''
        return '沉默', '判断后决定不说：' + (reason or 'judge-declined'), ''
    candidate = item['candidate']
    if candidate.get('error'):
        return '沉默', '生成失败：' + short(candidate['error'], 60), ''
    audits = item['audits']
    for audit in reversed(audits):
        kind = audit['kind']
        reason = trim_chat(audit['reason'], chat)
        if kind == 'group_fish_timeout':
            return '沉默', '生成或判断超时（视为不说）', ''
        if kind == 'group_fish_uncertain':
            return '沉默', '回执不明（真实路径不会重发）', ''
        if kind == 'group_fish_failed':
            return '沉默', '生成失败：' + (reason or kind), ''
    for audit in reversed(audits):
        kind = audit['kind']
        reason = trim_chat(audit['reason'], chat)
        if kind in ('group_fish_skipped', 'group_fish_dived') and reason not in GENERIC_SKIPS:
            return '沉默', reason or kind, ''
    gates = item.get('gates') or []
    refused = [gate for gate in gates
               if gate.get('stage') == 'candidate' and gate.get('action') not in (None, 'candidate')]
    if refused:
        reason = trim_chat(refused[-1].get('reason'), chat)
        if reason:
            return '沉默', reason, ''
    # 群策略先放行过一次候选、紧接着的预留又是拒的：同一个游标在两次判定里结论不同，
    # 这里只报事实（两边的原始理由都在 json 的 gates 里），不替它猜是哪一道门槛。
    if (any(gate.get('action') == 'candidate' for gate in gates)
            and any(gate.get('stage') == 'reserve' and gate.get('granted') is False for gate in gates)):
        return '沉默', '群策略先放行、紧接着的预留又拒了（同一游标二次判定未通过）', ''
    if refused:
        return '沉默', '群策略再次拒绝（游标或上限变动）', ''
    for audit in reversed(audits):
        reason = trim_chat(audit['reason'], chat)
        if reason in GENERIC_SKIPS:
            return '沉默', GENERIC_SKIPS[reason], ''
    return '沉默', '未进入生成（未监听、重复或自己发的）', ''


async def settle(loop):
    while loop.group_tasks:
        await asyncio.gather(*list(loop.group_tasks), return_exceptions=True)


async def rehearse(*, rows, chat, bot_ids, temp_root, args, clock, channel, real_calls,
                   audits, generated, verdicts, gates, logs, policy_path, wake):
    import qq_agent_loop
    import group_decide
    import fish_chat
    from governance import Governance
    from group_policy import GroupPolicy
    from qq_agent_loop import QQAgentLoop

    loop = QQAgentLoop(temp_root)
    loop.channel = channel

    original_candidate = GroupPolicy.candidate
    original_reserve = GroupPolicy.reserve

    def candidate_wrap(self, chat_id, now=None, *, continuation=False):
        result = original_candidate(self, chat_id, now=now, continuation=continuation)
        gates.append({'stage': 'candidate', 'continuation': bool(continuation),
                      'action': result.get('action'), 'reason': result.get('reason'),
                      'kind': result.get('kind'), 'cursor': result.get('cursor')})
        return result

    def reserve_wrap(self, chat_id, cursor, kind, now=None, *, continuation=False):
        token = original_reserve(self, chat_id, cursor, kind, now=now, continuation=continuation)
        gates.append({'stage': 'reserve', 'cursor': cursor, 'kind': kind,
                      'granted': bool(token), 'continuation': bool(continuation)})
        return token

    async def blocked_async(*_args, **_kwargs):
        real_calls.append('start')
        raise AssertionError('彩排里真实通道被启动了')

    def blocked(*_args, **_kwargs):
        real_calls.append('action')
        raise AssertionError('彩排里真实通道被调用了')

    original_audit = Governance.audit

    def audit_wrap(self, kind, reason, text=''):
        audits.append({'kind': str(kind), 'reason': str(reason)})
        return original_audit(self, kind, reason, text)

    original_respond = fish_chat.respond

    def respond_wrap(*fargs, **fkwargs):
        started = time.monotonic()
        try:
            text = original_respond(*fargs, **fkwargs)
        except Exception as exc:  # noqa: BLE001 - 生成失败也是结论
            generated.append({'text': '', 'error': f'{type(exc).__name__}: {exc}',
                              'ms': int((time.monotonic() - started) * 1000)})
            raise
        generated.append({'text': str(text or ''), 'error': '',
                          'ms': int((time.monotonic() - started) * 1000)})
        return text

    original_judge = group_decide.worth_speaking

    def judge_wrap(root, chat_id, *, payload, candidate_text, kind='active', cancelled=None):
        verdict = original_judge(root, chat_id, payload=payload, candidate_text=candidate_text,
                                 kind=kind, cancelled=cancelled)
        verdicts.append({'candidate': str(candidate_text or ''), 'kind': str(kind),
                         **(verdict if isinstance(verdict, dict) else {'speak': None})})
        return verdict

    items = []
    import unittest.mock as mock
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(qq_agent_loop.ChannelClient, 'action', blocked))
        stack.enter_context(mock.patch.object(qq_agent_loop.ChannelClient, 'start', blocked_async))
        stack.enter_context(mock.patch.object(qq_agent_loop, 'connect', blocked))
        stack.enter_context(mock.patch.object(Governance, 'audit', audit_wrap))
        stack.enter_context(mock.patch.object(fish_chat, 'respond', respond_wrap))
        stack.enter_context(mock.patch.object(group_decide, 'worth_speaking', judge_wrap))
        stack.enter_context(mock.patch.object(GroupPolicy, 'candidate', candidate_wrap))
        stack.enter_context(mock.patch.object(GroupPolicy, 'reserve', reserve_wrap))
        stack.enter_context(mock.patch.object(qq_agent_loop, 'log', logs.append))

        for index, row in enumerate(rows, 1):
            clock.set(row['ts_epoch'])
            mark_audits, mark_gen, mark_judge = len(audits), len(generated), len(verdicts)
            mark_gates = len(gates)
            mark_sent = len(channel.sent)
            before = reservations_of(policy_path, chat)
            payload = build_payload(row, channel.bot['id'], bot_ids)
            error = ''
            try:
                await loop._on_group(payload)
            except Exception as exc:  # noqa: BLE001 - 一条崩了也要出一行结论
                error = f'{type(exc).__name__}: {exc}'
            await settle(loop)
            after = reservations_of(policy_path, chat)
            sends = channel.sent[mark_sent:]
            reply = ''.join(str(segment.get('text') or '')
                            for send in sends
                            for segment in (send.get('segments') or [])
                            if isinstance(segment, dict) and segment.get('type') == 'text')
            fresh = [{'cursor': row2['cursor'], 'kind': row2['kind'], 'state': row2['state']}
                     for token, row2 in after.items() if token not in before]
            changed = [{'cursor': row2['cursor'], 'kind': row2['kind'], 'state': row2['state']}
                       for token, row2 in after.items()
                       if token in before and before[token]['state'] != row2['state']]
            item = {
                'index': index,
                'ts': row['ts'],
                'message_id': str(row['channel_message_id']),
                'row_id': row['id'],
                'sender_id': str(row['user_id']),
                'sender_tail': str(row['user_id'])[-4:],
                'sender_name': row['name'],
                'mentioned': payload['mentions_bot'],
                'reply_to': payload['reply_to'],
                'raw_text': str(row['text'] or ''),
                'display': inbound_display(row, channel.bot['id']),
                'sent': bool(sends),
                'reply': reply,
                # 表情段在通道里是 image(asset_id)：这里只记录，不解释，方便看到“她选了哪张”。
                'stickers': [str(segment.get('asset_id') or segment.get('emoji_id') or '')
                             for send in sends
                             for segment in (send.get('segments') or [])
                             if isinstance(segment, dict) and segment.get('type') in ('image', 'sticker')],
                'sends': [{'reply_to': send.get('reply_to'),
                           'key': send.get('idempotency_key'),
                           'text': ''.join(str(s.get('text') or '') for s in send.get('segments') or []
                                           if isinstance(s, dict) and s.get('type') == 'text')}
                          for send in sends],
                'candidate': (generated[mark_gen] if len(generated) > mark_gen else {}),
                'judge': (verdicts[mark_judge] if len(verdicts) > mark_judge else {}),
                'audits': audits[mark_audits:],
                'gates': gates[mark_gates:],
                'reservations': fresh + changed,
                'error': error,
            }
            verdict, reason, _ = stage_report(item, chat, wake)
            item['outcome'] = verdict
            item['reason'] = reason
            item['reason_label'] = '' if verdict == '说' else (label_of(reason) if reason else '')
            items.append(item)

            if args.gap > 0 and index < len(rows):
                await asyncio.sleep(float(args.gap))
    return loop, items


# ---------------------------------------------------------------- 入口


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='群聊彩排：真实模型、真实判断、真实流水线，但绝不发送')
    parser.add_argument('--chat', default=DEFAULT_CHAT, help='群范围，默认 ' + DEFAULT_CHAT)
    parser.add_argument('--limit', type=int, default=10, help='取最近几条入站消息，默认 10，上限 20')
    parser.add_argument('--since', default='', help='只看这个时间之后的消息（YYYY-MM-DD HH:MM[:SS] 或 HH:MM[:SS]）')
    parser.add_argument('--ids', default='', help='指定消息号复跑（qq.db 行号或 QQ 消息号，逗号分隔）')
    parser.add_argument('--json', default='', help='明细 json 路径，默认 artifacts/rehearse-group-<时间>.json')
    parser.add_argument('--gap', type=float, default=0.0, help='每条之间等待的秒数，默认 0')
    parser.add_argument('--wake', type=float, default=None, help='覆盖唤醒概率（默认用群的真实配置）')
    parser.add_argument('--keep', action='store_true', help='保留临时根，便于事后翻账本')
    args = parser.parse_args(argv)

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    limit = max(1, min(MAX_LIMIT, int(args.limit)))
    ids = [part for part in str(args.ids or '').replace('，', ',').split(',') if part.strip()]
    since = parse_stamp(args.since) if args.since else None
    chat = str(args.chat)
    bot_id = '1417334937'  # 她自己：people.db 里 nickname=大肥鱼 的那个号

    hashes_before = {name: sha256(ROOT / name) for name in ONLINE_DBS + EXTRA_DBS if (ROOT / name).exists()}
    rows, bot_ids = inbound_rows(ROOT, chat, limit=limit, since=since, ids=ids)

    sys.path[:0] = [str(ROOT / 'harness')]
    import dependencies  # noqa: F401  与真实进程一致的运行时准备
    loaded_env = adopt_user_env()
    from group_policy import GroupPolicy
    from group_decide import group_engine

    temp_root = Path(tempfile.mkdtemp(prefix='fish-rehearse-'))
    try:
        stage_root(ROOT, temp_root)
        budgets = reset_budgets(temp_root, chat)
        policy = GroupPolicy(temp_root)
        stored = policy.config(chat)
        forced = {'listen': True, 'reply': True}
        if args.wake is not None:
            forced['wake_probability'] = max(0.0, min(1.0, float(args.wake)))
        effective = policy.configure(chat, forced)
        if group_engine(temp_root, chat) != 'fish':
            raise SystemExit('这个群的 engine 不是 fish（当前 %r），fish 分支根本不会跑；'
                             '请先在真实群配置里把 engine 改成 fish。' % effective.get('engine'))

        policy_path = policy.path
        mapped, stored_cursor = observe_map(policy_path, chat)
        first = next((mapped[str(row['channel_message_id'])] for row in rows
                      if str(row['channel_message_id']) in mapped), None)
        wanted = [str(row['channel_message_id']) for row in rows]
        rewound = rewind(policy_path, chat, first, wanted) if first is not None else {
            'cursor': stored_cursor, 'observations_dropped': 0, 'dedup_cleared': 0, 'reservations_dropped': 0}

        clock = VirtualClock()
        channel = FakeChannel(bot_id)
        real_calls, audits, generated, verdicts, gates, logs = [], [], [], [], [], []
        usage_path = temp_root / 'data/qq/model-usage.db'
        calls_before = model_calls(usage_path)

        random.seed(1043230169)  # 唤醒概率的骰子可以复现，但不改真实概率

        started = time.time()
        with isolation(ROOT, temp_root) as touched:
            clock.install()
            try:
                loop, items = asyncio.run(rehearse(
                    rows=rows, chat=chat, bot_ids=bot_ids, temp_root=temp_root, args=args,
                    clock=clock, channel=channel, real_calls=real_calls, audits=audits,
                    generated=generated, verdicts=verdicts, gates=gates, logs=logs,
                    policy_path=policy_path, wake=float(effective['wake_probability'])))
            finally:
                clock.restore()
        calls_after = model_calls(usage_path)
    finally:
        hashes_after = {name: sha256(ROOT / name) for name in ONLINE_DBS + EXTRA_DBS if (ROOT / name).exists()}

    generated_calls = calls_after.get('group-fish:', 0) - calls_before.get('group-fish:', 0)
    judge_calls = calls_after.get('group-judge', 0) - calls_before.get('group-judge', 0)

    # ---- 结论
    spoke = [item for item in items if item['sent']]
    mute = [item for item in items if not item['sent']]
    labels = {}
    for item in mute:
        labels[item['reason_label'] or '未说明'] = labels.get(item['reason_label'] or '未说明', 0) + 1

    say('群聊彩排：真模型、真判断、真流水线，一条消息都不发')
    say('群：%s ｜ %d 条真实入站 ｜ %s → %s' % (
        chat, len(items), items[0]['ts'][11:19] if items else '-', items[-1]['ts'][11:19] if items else '-'))
    say('生效配置：' + ' '.join('%s=%s' % (key, effective[key]) for key in (
        'engine', 'listen', 'reply', 'learn', 'read', 'debounce_s', 'active_cooldown_s',
        'max_active_hour', 'max_total_min', 'max_unanswered_active', 'wake_probability',
        'followup_s', 'fish_judge_daily_limit')) + ('  （wake 由 --wake 覆盖）' if args.wake is not None else ''))
    say('临时根：%s' % temp_root)
    say('线上根：%s' % ROOT)
    say('回退：cursor=%s ｜ 丢观察 %d ｜ 清去重 %d ｜ 清预留 %d' % (
        rewound['cursor'], rewound['observations_dropped'], rewound['dedup_cleared'],
        rewound['reservations_dropped']))
    say('真实通道：ChannelClient.start %d 次 / ChannelClient.action %d 次 / qq_agent_loop.connect %d 次'
        % (real_calls.count('start'), real_calls.count('action'), 0))
    say('密钥：从 User 环境读入 %d 个（%s，值不打印）'
        % (len(loaded_env), ','.join(loaded_env) if loaded_env else '无（沿用当前进程环境）'))
    say('判断预算：真实今天已用 %s 次，临时库按新的一天重开（否则只会得到 judge-daily-limit）'
        % (budgets['real'] or '0'))
    say('模型调用：生成 %d 次 / 判断 %d 次（真实 API）' % (generated_calls, judge_calls))
    say('隔离检查：线上路径上的任何 sqlite/写盘调用都会当场抛错，本次拦截 %d 次'
        % len(touched['blocked']))
    say('')
    say('  # 时间     群友  原话（前20字）                       她的回复（≤120字）/ 沉默原因')
    for item in items:
        head = '%2d %s  …%s  「%s」' % (item['index'], item['ts'][11:19],
                                        item['sender_tail'], short(item['display'], 20))
        tail = (('→ 说：「%s」' % short(item['reply'], 120)) if item['sent']
                else ('→ 沉默：%s' % short(item['reason'], 70)))
        if item.get('stickers'):
            tail += '  [表情 ' + ','.join(sticker[:8] for sticker in item['stickers']) + ']'
        say(head + '  ' + tail)
    say('')
    say('总条数 %d ｜ 她说 %d 句 ｜ 沉默 %d 句' % (len(items), len(spoke), len(mute)))
    if labels:
        say('沉默原因分布：' + ' ｜ '.join('%s %d' % (key, value)
                                          for key, value in sorted(labels.items(), key=lambda kv: -kv[1])))
    say('')
    changed = [name for name in ONLINE_DBS + EXTRA_DBS
               if name in hashes_before and hashes_before[name] != hashes_after.get(name)]
    say('线上库 sha256 前后：' + ('三个核心库一致' if not changed else '有变动 ' + ','.join(changed)))
    for name in ONLINE_DBS + EXTRA_DBS:
        if name in hashes_before:
            mark = '一致' if hashes_before[name] == hashes_after.get(name) else '不一致'
            say('  %-28s %s  %s' % (name, hashes_before[name][:16], mark))

    payload = {
        'meta': {
            'chat': chat,
            'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'elapsed_s': round(time.time() - started, 1),
            'bot_id': bot_id,
            'config': effective,
            'config_forced': forced,
            'config_stored': stored,
            'rewind': rewound,
            'rows_driver': {'limit': limit, 'since': args.since, 'ids': ids, 'gap': args.gap},
            'temp_root': str(temp_root),
            'real_channel_calls': real_calls,
            'isolation_blocked': touched['blocked'],
            'harness_logs': logs,
            'env_loaded': loaded_env,
            'budgets': budgets,
            'model_calls': {'generate': generated_calls, 'judge': judge_calls},
            'sha256_before': hashes_before,
            'sha256_after': hashes_after,
            'sent_count': len(spoke),
            'silent_count': len(mute),
            'silent_reasons': labels,
        },
        'items': items,
    }
    target = Path(args.json) if args.json else (
        ROOT / 'artifacts' / ('rehearse-group-%s.json' % time.strftime('%Y%m%d-%H%M%S')))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    say('明细已写入：%s' % target)

    problems = []
    if real_calls:
        problems.append('真实通道被调用 ' + ','.join(real_calls))
    if changed:
        problems.append('线上库被改动 ' + ','.join(changed))
    if not args.keep:
        shutil.rmtree(temp_root, ignore_errors=True)
    else:
        say('临时根已保留：%s' % temp_root)
    if problems:
        say('！验收失败：' + '；'.join(problems))
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
