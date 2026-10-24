#!/usr/bin/env python3
"""主人私聊彩排：真模型、真工具、真记忆，但一条消息都不发。

把 qq.db 里主人私聊的真实入站消息重新喂给 QQAgentLoop.on_event 的私聊分支，
走完整 owner_private.process：权限复检 -> 人格/历史/记忆上下文 -> fish_chat.respond
（owner-private 目录、真实 ToolHost、真实模型）-> decode_reply -> 分条发送。
唯一的传输层是 FakeChannel，它只记录 action，不打开 websocket、不碰 NapCat。

隔离：真实 harness/ 与 data/ 整棵被抄进临时根，ToolHost 用临时根在自己的随机端口
起一份（不会复用线上 3103 那份），记忆、账本、审计、请求快照全部写临时根。运行期间
任何试图打开或写入线上根的 sqlite / 写盘调用都会当场抛错——"没碰线上 data/" 是运行期
强制出来的，不是事后自述。跑完再对线上几个库做 sha256 前后比对。

时间：时钟钉在每条消息的真实时刻并按真实速度走，冷却、提醒、24 小时过期这类
逻辑读到的就是它们在那刻的真实值，而不是被重放速度压缩出来的假象。

用法：
  python tools/rehearse_private.py                    # 最近 8 条
  python tools/rehearse_private.py --limit 4          # 先看句子是否合理
  python tools/rehearse_private.py --limit 8 --json out.json
  python tools/rehearse_private.py --since "2026-10-22 14:00:00"
  python tools/rehearse_private.py --ids 3084,3086    # 指定消息号复跑
  python tools/rehearse_private.py --gap 1            # 每条之间额外停 1 秒
  python tools/rehearse_private.py --keep             # 保留临时根，便于翻账本
  python tools/rehearse_private.py --no-learn         # 只生成，不跑记忆提取
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]

# 线上核心库：跑完必须与跑前逐字节一致。
ONLINE_DBS = ('data/qq/qq.db', 'data/qq/memory.db', 'data/qq/governance.db',
              'data/social/context.db', 'data/qq/model-usage.db')
# 顺带一起校验的账本/审计/请求快照，只是多一层证据。
EXTRA_DBS = ('data/social/request-inspector.db', 'data/qq/tool-host.db',
             'data/qq/emoji/library.db', 'data/social/people.db')
MAX_LIMIT = 20
COPY_IGNORE = ('*-wal', '*-shm', '*.lock', '*.log')


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


def default_chat(root):
    """主线私聊就是主人那个号：先从通道配置取，取不到再用入站最多的私聊。"""
    channel = root / 'channels/fish_qq/config.json'
    if channel.exists():
        try:
            owner = str(json.loads(channel.read_text(encoding='utf-8'))
                        .get('allowlist', {}).get('owner_id', '') or '')
        except ValueError:
            owner = ''
        if owner:
            return 'private:' + owner
    store = root / 'data/qq/qq.db'
    with contextlib.closing(sqlite3.connect('file:' + str(store) + '?mode=ro', uri=True)) as db:
        row = db.execute("SELECT chat_id FROM messages WHERE chat_id LIKE 'private:%' "
                         "AND direction='inbound' GROUP BY chat_id ORDER BY count(*) DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit('qq.db 里没有任何主人私聊入站消息')
    return str(row[0])


def inbound_rows(root, chat, *, limit, since, ids):
    """qq.db 里的真实入站消息，按时间正序；limit 取最后 N 条。"""
    store = root / 'data/qq/qq.db'
    if not store.exists():
        raise SystemExit('找不到线上私聊消息库：' + str(store))
    with contextlib.closing(sqlite3.connect('file:' + str(store) + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            "SELECT id, ts, chat_id, user_id, text, reply_to, channel_message_id, conversation_id, segments "
            "FROM messages WHERE chat_id=? AND direction='inbound' ORDER BY ts, id", (chat,))]
    for row in rows:
        row['ts_epoch'] = stamp_epoch(row['ts'])
        try:
            row['segment_list'] = json.loads(row['segments'] or '[]')
        except ValueError:
            row['segment_list'] = [{'type': 'text', 'text': str(row['text'] or '')}]
    if not rows:
        raise SystemExit('这个私聊没有入站消息：' + chat)
    if ids:
        wanted = {str(item).strip() for item in ids if str(item).strip()}
        picked = [row for row in rows
                  if str(row['id']) in wanted or str(row['channel_message_id']) in wanted]
        if not picked:
            raise SystemExit('--ids 没有匹配到任何入站消息：' + ','.join(sorted(wanted)))
        picked.sort(key=lambda row: (row['ts_epoch'], row['id']))
        return picked
    if since is not None:
        rows = [row for row in rows if row['ts_epoch'] >= since]
        if not rows:
            raise SystemExit('--since 之后没有入站消息')
    return rows[-limit:]


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
    """真实 harness/ 与整棵 data/ 抄进临时根；只读线上，不动线上。"""
    shutil.copytree(real_root / 'harness', temp_root / 'harness',
                    ignore=shutil.ignore_patterns('runtime', 'tmp', '__pycache__', 'settings.lock'),
                    dirs_exist_ok=True)
    channel = real_root / 'channels/fish_qq/config.json'
    if channel.exists():
        (temp_root / 'channels/fish_qq').mkdir(parents=True, exist_ok=True)
        shutil.copy2(channel, temp_root / 'channels/fish_qq/config.json')
    data = real_root / 'data'
    if data.is_dir():
        shutil.copytree(data, temp_root / 'data',
                        ignore=shutil.ignore_patterns(*COPY_IGNORE), dirs_exist_ok=True)
        for db_file in sorted(data.rglob('*.db')):
            if any(part.endswith('-wal') for part in db_file.parts):
                continue
            copy_sqlite(db_file, temp_root / db_file.relative_to(real_root))
    for sub in ('qq', 'social', 'shared', 'system'):
        (temp_root / 'data' / sub).mkdir(parents=True, exist_ok=True)
    (temp_root / 'harness/runtime').mkdir(parents=True, exist_ok=True)


def reset_budget(temp_root):
    """把"今天已花掉的调用额度"在临时库里重开，并说清真实值是多少。

    额度是计费预算，不是她的性格：照抄过来的话整段彩排只会得到
    "今天的调用上限已到"，respond 根本跑不到。所以临时库按新的一天重开，
    真实用量照原样写进报告。
    """
    path = temp_root / 'data/qq/governance.db'
    day = time.strftime('%Y-%m-%d')
    report = {'day': day, 'real': {}, 'reopened': True}
    if not path.exists():
        return report
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        for kind in ('calls', 'replies'):
            row = db.execute('SELECT count FROM usage WHERE day=? AND kind=?', (day, kind)).fetchone()
            report['real'][kind] = int(row[0]) if row and row[0] is not None else 0
            db.execute('DELETE FROM usage WHERE day=? AND kind=?', (day, kind))
        db.commit()
    return report


def force_switches(temp_root):
    """彩排要跑到生成那一步：暂停/私聊发送关掉时在临时根里打开，并如实报告。"""
    path = temp_root / 'harness/control.json'
    if not path.exists():
        return {'forced': {}, 'note': '无 control.json，沿用默认值'}
    raw = json.loads(path.read_text(encoding='utf-8'))
    before = {'paused_qq': bool((raw.get('paused_roles') or {}).get('qq', raw.get('paused', False))),
              'owner_reply': bool((raw.get('send_policy') or {}).get('owner_private', {}).get('owner_reply', True))}
    forced = {}
    if before['paused_qq']:
        raw.setdefault('paused_roles', {})['qq'] = False
        forced['paused_roles.qq'] = False
    if not before['owner_reply']:
        raw.setdefault('send_policy', {}).setdefault('owner_private', {})['owner_reply'] = True
        forced['send_policy.owner_private.owner_reply'] = True
    if forced:
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'stored': before, 'forced': forced}


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

    彩排是在几秒内把几小时的主聊重放一遍。如果时钟就是"现在"，24 小时过期、
    冷却、提醒、到期事件读到的全是重放这一刻的值，看起来就像时间被压缩了。
    钉到这条消息的真实时间戳后，这些逻辑读到的就是它们在那一刻的真实值；
    time.monotonic 不动，所以真实的等待窗口仍是真实等待。
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
        self.sent.append({'name': name, 'args': args,
                          'key': str(args.get('idempotency_key') or '')})
        return {'ok': True, 'payload': {'message_id': 'rehearsal-' + str(len(self.sent))}}


# ---------------------------------------------------------------- 展示


def flat(value):
    return ''.join(str(value or '').split())


def short(value, limit):
    body = flat(value)
    if not body:
        return ''
    return body[:limit] + ('…' if len(body) > limit else '')


def inbound_display(row):
    body = flat(row['text'])
    if body:
        return body
    marks = {'image': '[图片]', 'emoji': '[表情]', 'face': '[表情]', 'audio': '[语音]',
             'video': '[视频]', 'file': '[文件]', 'document': '[文件]'}
    marks = ''.join(marks.get(str(s.get('type')), '') for s in row['segment_list'] if isinstance(s, dict))
    return marks or '[空消息]'


def send_text(send):
    parts = []
    for segment in (send['args'].get('segments') or []):
        if not isinstance(segment, dict):
            continue
        if segment.get('type') == 'text':
            parts.append(str(segment.get('text') or ''))
        elif segment.get('asset_id') or segment.get('type') == 'emoji':
            parts.append('[表情:' + str(segment.get('asset_id') or segment.get('emoji_id') or '?') + ']')
    return ''.join(parts)


def tool_line(tools):
    if not tools:
        return '（未调用工具）'
    return '；'.join('%s %s' % (item['name'] or '(空)', 'ok' if item['ok'] else 'false')
                     for item in tools)


# ---------------------------------------------------------------- 记忆快照


def memory_snapshot(path):
    mark = {'memories': 0, 'jobs': 0}
    if not path.exists():
        return mark
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        db.row_factory = sqlite3.Row
        # id 是十六进制串，插入顺序只能看 rowid。
        mark['memories'] = int(db.execute('SELECT COALESCE(MAX(rowid),0) FROM memories').fetchone()[0] or 0)
        try:
            mark['jobs'] = int(db.execute('SELECT COALESCE(MAX(rowid),0) FROM learning_jobs').fetchone()[0] or 0)
        except sqlite3.Error:
            pass
    return mark


def new_memories(path, after):
    if not path.exists():
        return []
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            'SELECT id,text,kind,category,scope,subject,status,due,created,source '
            'FROM memories WHERE rowid>? ORDER BY rowid', (after,))]


def new_jobs(path, after):
    if not path.exists():
        return []
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        db.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in db.execute(
                'SELECT * FROM learning_jobs WHERE rowid>? ORDER BY rowid', (after,))]
        except sqlite3.Error:
            return []


def stored_row(path, channel_id):
    """这条入站消息在临时库里最后是什么状态：真实失败原因在这里，不在异常里。"""
    if not path.exists():
        return {}
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT id,status,error,runtime_turn_id FROM messages '
                         'WHERE channel_message_id=? ORDER BY id DESC LIMIT 1',
                         (channel_id,)).fetchone()
    return dict(row) if row else {}


# ---------------------------------------------------------------- 主流程


async def rehearse(*, rows, chat, temp_root, args, clock, channel, real_calls,
                   audits, generated, logs, memory_path):
    import fish_chat
    import qq_agent_loop
    from governance import Governance
    from qq_agent_loop import ChannelClient, QQAgentLoop

    loop = QQAgentLoop(temp_root)
    loop.channel = channel
    loop.conversation = loop.store.get_or_create_conversation('private:' + loop.owner, loop.agent_id)

    def blocked_async(*_args, **_kwargs):
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
        """真实 respond；只旁听它这一轮公布了什么、调用了什么。"""
        state = fkwargs.get('emoji_state') if isinstance(fkwargs.get('emoji_state'), dict) else {}
        persona = fkwargs.get('persona') or {}
        started = time.monotonic()
        text, error = None, ''
        try:
            text = original_respond(*fargs, **fkwargs)
            return text
        except Exception as exc:  # noqa: BLE001 - 生成失败也是结论
            error = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            generated.append({
                'text': text if isinstance(text, str) else '',
                'error': error,
                'ms': int((time.monotonic() - started) * 1000),
                'persona': str(persona.get('id') or ''),
                'scope': str(fkwargs.get('scope') or ''),
                'tools': [{'name': str(call.get('name') or ''), 'ok': bool(call.get('ok'))}
                          for call in (state.get('tools') or [])],
            })

    store_path = memory_path.parent / 'qq.db'
    learned = []
    memory_store = gateway = None
    if args.learn:
        # 真实私聊里这条队列由 ToolHost 的学习线程消费；彩排自己把本轮那条抽出来，
        # 否则"她记下了什么"只会看到队列，看不到记忆条目。
        from memory_store import MemoryStore
        from model_gateway import ModelGateway
        memory_store, gateway = MemoryStore(temp_root, 'qq'), ModelGateway(temp_root, 'qq')

    items = []
    import unittest.mock as mock
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(ChannelClient, 'action', blocked))
        stack.enter_context(mock.patch.object(ChannelClient, 'start', blocked_async))
        stack.enter_context(mock.patch.object(qq_agent_loop, 'connect', blocked, create=True))
        stack.enter_context(mock.patch.object(Governance, 'audit', audit_wrap))
        stack.enter_context(mock.patch.object(fish_chat, 'respond', respond_wrap))
        stack.enter_context(mock.patch.object(qq_agent_loop, 'log', logs.append))

        loop.worker = asyncio.create_task(loop._worker(), name='rehearse-private-worker')
        try:
            for index, row in enumerate(rows, 1):
                clock.set(row['ts_epoch'])
                mark_sent, mark_gen = len(channel.sent), len(generated)
                mark_audit, mark_memory = len(audits), memory_snapshot(memory_path)
                # 重放用的是演练消息号：线上那条已经在临时库里，不能重复入站。
                mid = 'rehearse-%s-%s' % (row['id'], uuid.uuid4().hex[:8])
                payload = {
                    'event': 'message.received',
                    'payload': {
                        'chat_type': 'private',
                        'chat_id': chat,
                        'message_id': mid,
                        'sender': {'id': str(row['user_id'])},
                        'reply_to': str(row['reply_to'] or ''),
                        'segments': row['segment_list'],
                    },
                }
                error = ''
                started = time.monotonic()
                try:
                    await loop.on_event(payload)
                    await asyncio.wait_for(loop.queue.join(), timeout=float(args.timeout))
                except Exception as exc:  # noqa: BLE001 - 一条崩了也要出一行结论
                    error = f'{type(exc).__name__}: {exc}'
                elapsed = int((time.monotonic() - started) * 1000)
                stored = stored_row(store_path, mid)
                if memory_store is not None and stored.get('id'):
                    # 入站记录的学习源是这条消息的库内行号，不是通道消息号。
                    for _attempt in range(5):
                        try:
                            if not memory_store.learn_one(gateway, source='qq:%s' % stored['id']):
                                break
                            learned.append({'source': 'qq:%s' % stored['id']})
                        except Exception as exc:  # noqa: BLE001 - 提取失败也是结论
                            learned.append({'source': 'qq:%s' % stored['id'],
                                            'error': f'{type(exc).__name__}: {exc}'})
                            break

                sends = channel.sent[mark_sent:]
                replies = [send for send in sends if not send['key'].endswith(':notice')]
                notices = [send for send in sends if send['key'].endswith(':notice')]
                reply = ''.join(send_text(send) for send in replies)
                call = generated[mark_gen] if len(generated) > mark_gen else {}
                item = {
                    'index': index,
                    'ts': row['ts'],
                    'row_id': row['id'],
                    'channel_message_id': str(row['channel_message_id'] or ''),
                    'raw_text': str(row['text'] or ''),
                    'display': inbound_display(row),
                    'segments': row['segment_list'],
                    'reply': reply,
                    'raw_reply': (call.get('text') or '')[:800],
                    'notices': [send_text(send) for send in notices],
                    'tools': call.get('tools') or [],
                    'persona': call.get('persona', ''),
                    'respond_ms': call.get('ms', 0),
                    'elapsed_ms': elapsed,
                    'generate_error': call.get('error', ''),
                    'audits': audits[mark_audit:],
                    'memories': new_memories(memory_path, mark_memory['memories']),
                    'jobs': new_jobs(memory_path, mark_memory['jobs']),
                    'stored': stored,
                    'error': error,
                }
                items.append(item)
                if args.gap > 0 and index < len(rows):
                    await asyncio.sleep(float(args.gap))
        finally:
            loop.worker.cancel()
            await asyncio.gather(loop.worker, return_exceptions=True)
    return items, learned


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='主人私聊彩排：真实模型、真实工具链，但绝不发送')
    parser.add_argument('--chat', default='', help='私聊范围，默认从通道配置的主人号推出')
    parser.add_argument('--limit', type=int, default=8, help='取最近几条入站消息，默认 8，上限 20')
    parser.add_argument('--since', default='', help='只看这个时间之后的消息（YYYY-MM-DD HH:MM[:SS] 或 HH:MM[:SS]）')
    parser.add_argument('--ids', default='', help='指定消息号复跑（qq.db 行号或 QQ 消息号，逗号分隔）')
    parser.add_argument('--json', default='', help='明细 json 路径，默认 artifacts/rehearse-private-<时间>.json')
    parser.add_argument('--gap', type=float, default=0.0, help='每条之间额外等待的秒数，默认 0')
    parser.add_argument('--timeout', type=float, default=300.0, help='单条最多等多久，默认 300 秒')
    parser.add_argument('--keep', action='store_true', help='保留临时根，便于事后翻记忆与请求快照')
    parser.add_argument('--learn', action=argparse.BooleanOptionalAction, default=True,
                        help='把本条入站的学习任务真的提取成记忆（真实模型），默认开')
    args = parser.parse_args(argv)

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    limit = max(1, min(MAX_LIMIT, int(args.limit)))
    ids = [part for part in str(args.ids or '').replace('，', ',').split(',') if part.strip()]
    since = parse_stamp(args.since) if args.since else None

    hashes_before = {name: sha256(ROOT / name) for name in ONLINE_DBS + EXTRA_DBS if (ROOT / name).exists()}
    chat = str(args.chat).strip() or default_chat(ROOT)
    rows = inbound_rows(ROOT, chat, limit=limit, since=since, ids=ids)

    import dependencies  # noqa: F401  与真实进程一致的运行时准备
    loaded_env = adopt_user_env()

    temp_root = Path(tempfile.mkdtemp(prefix='fish-rehearse-private-'))
    try:
        stage_root(ROOT, temp_root)
        budget = reset_budget(temp_root)
        switches = force_switches(temp_root)
        memory_path = temp_root / 'data/qq/memory.db'

        clock = VirtualClock()
        channel = FakeChannel('')
        real_calls, audits, generated, logs = [], [], [], []

        started = time.time()
        with isolation(ROOT, temp_root) as touched:
            clock.install()
            try:
                items, learned = asyncio.run(rehearse(
                    rows=rows, chat=chat, temp_root=temp_root, args=args, clock=clock,
                    channel=channel, real_calls=real_calls, audits=audits,
                    generated=generated, logs=logs, memory_path=memory_path))
            finally:
                clock.restore()
    finally:
        hashes_after = {name: sha256(ROOT / name) for name in ONLINE_DBS + EXTRA_DBS if (ROOT / name).exists()}

    # ---- 结论
    answered = [item for item in items if item['reply']]
    unanswered = [item for item in items if not item['reply']]
    errors = [item for item in items if item['error'] or item['generate_error']]
    counters = {}
    for item in items:
        for tool in item['tools']:
            slot = counters.setdefault(tool['name'], {'ok': 0, 'false': 0})
            slot['ok' if tool['ok'] else 'false'] += 1
    fresh = [memory for item in items for memory in item['memories']]
    queued = [job for item in items for job in item['jobs']]
    total_ms = sum(item['elapsed_ms'] for item in items)
    personas = sorted({item['persona'] for item in items if item['persona']})

    say('私聊彩排：真模型、真工具、真记忆，一条消息都不发')
    say('私聊：%s ｜ %d 条真实入站 ｜ %s → %s' % (
        chat, len(items), items[0]['ts'] if items else '-', items[-1]['ts'] if items else '-'))
    say('人格：%s ｜ 工具目录：owner-private（16 个）' % ('、'.join(personas) or '（未取到）'))
    media = [item for item in items
             if any(isinstance(s, dict) and s.get('type') in ('image', 'audio', 'video', 'file', 'document')
                    for s in item['segments'])]
    if media:
        say('附件消息 %d 条：重放要按原 URL 重新下载，QQ 的 rkey 过期就读不到（真实链路对私聊不用本地归档取图）'
            % len(media))
    say('临时根：%s' % temp_root)
    say('线上根：%s' % ROOT)
    say('真实通道：ChannelClient.start %d 次 / ChannelClient.action %d 次／假通道记录 %d 条 action'
        % (real_calls.count('start'), real_calls.count('action'), len(channel.sent)))
    say('密钥：从 User 环境读入 %d 个（%s，值不打印）'
        % (len(loaded_env), ','.join(loaded_env) if loaded_env else '无（沿用当前进程环境）'))
    say('调用预算：真实今天已用 %s 次，临时库按新的一天重开'
        % (budget['real'].get('calls', 0)))
    say('开关：%s' % ('沿用真实值' if not switches['forced'] else
                     '临时根里打开 ' + ','.join(switches['forced'])))
    say('隔离检查：线上路径上的任何 sqlite/写盘调用都会当场抛错，本次拦截 %d 次'
        % len(touched['blocked']))
    say('记忆提取：' + ('临时根里真实跑 %d 次学习' % len(learned) if args.learn else '已跳过（--no-learn）'))
    say('')
    say('  # 时间      主人原话（前24字）                    ｜ 她的回复（≤120字） ｜ 工具（ok/false）')
    for item in items:
        body = (('「%s」' % short(item['display'], 24)) if item['display'] else '')
        why = (item['generate_error'] or item['stored'].get('error') or item['error']
               or item['stored'].get('status') or '')
        reply = short(item['reply'], 120) or ('（未生成：%s）' % short(why, 64) if why else '（没有回复）')
        say('%2d %s  %s ｜ %s ｜ %s' % (item['index'], item['ts'][11:19], body, reply,
                                        tool_line(item['tools'])))
        for notice in item['notices']:
            say('        └ 失败提示（未发出）：%s' % short(notice, 70))
    say('')
    say('总条数 %d ｜ 有回复 %d 条 ｜ 未回复 %d 条 ｜ 抛异常 %d 条 ｜ 平均耗时 %.1f 秒/条（生成 %.1f 秒/条）' % (
        len(items), len(answered), len(unanswered), len(errors),
        total_ms / max(1, len(items)) / 1000.0,
        sum(item['respond_ms'] for item in items) / max(1, len(items)) / 1000.0))
    if counters:
        say('工具调用统计：' + ' ｜ '.join(
            '%s %d 次（成功 %d / 失败 %d）' % (name, slot['ok'] + slot['false'], slot['ok'], slot['false'])
            for name, slot in sorted(counters.items(), key=lambda kv: -(kv[1]['ok'] + kv[1]['false']))))
    else:
        say('工具调用统计：本轮没有任何工具调用')
    say('')
    say('这一轮她记下了什么：')
    if not fresh and not queued:
        say('  没有新增记忆，也没有新的学习条目')
    for memory in fresh:
        say('  记忆 #%s [%s/%s] %s' % (
            memory['id'], memory.get('kind') or '-', memory.get('status') or '-',
            short(memory.get('text'), 80)))
        say('      分类=%s 范围=%s 对象=%s 到期=%s 来源=%s' % (
            memory.get('category') or '-', memory.get('scope') or '-',
            memory.get('subject') or '-', memory.get('due') or '-', memory.get('source') or '-'))
    if queued:
        waiting = [job for job in queued if (job.get('status') or '') != 'done']
        say('  学习队列（本轮新增 %d 条：提取完成 %d / 待提取 %d）：'
            % (len(queued), len(queued) - len(waiting), len(waiting)))
        for job in queued:
            say('    #%s [%s] %s' % (job.get('id'), job.get('status') or '-', short(job.get('text'), 70)))
    say('')
    changed = [name for name in ONLINE_DBS + EXTRA_DBS
               if name in hashes_before and hashes_before[name] != hashes_after.get(name)]
    say('线上库 sha256 前后：' + ('全部一致' if not changed else '有变动 ' + ','.join(changed)))
    for name in ONLINE_DBS + EXTRA_DBS:
        if name in hashes_before:
            mark = '一致' if hashes_before[name] == hashes_after.get(name) else '不一致'
            say('  %-32s %s  %s' % (name, hashes_before[name][:16], mark))
    if changed:
        say('  （线上服务同时在跑；隔离检查拦截 %d 次说明本脚本没有写过线上根）' % len(touched['blocked']))

    payload = {
        'meta': {
            'chat': chat,
            'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'elapsed_s': round(time.time() - started, 1),
            'personas': personas,
            'rows_driver': {'limit': limit, 'since': args.since, 'ids': ids, 'gap': args.gap},
            'temp_root': str(temp_root),
            'real_channel_calls': real_calls,
            'fake_channel_actions': len(channel.sent),
            'isolation_blocked': touched['blocked'],
            'isolation_checked': touched['checked'],
            'env_loaded': loaded_env,
            'budget': budget,
            'switches': switches,
            'harness_logs': logs,
            'sha256_before': hashes_before,
            'sha256_after': hashes_after,
            'tool_counters': counters,
            'learning': {'ran': len(learned), 'items': learned},
            'answered': len(answered),
            'unanswered': len(unanswered),
            'errors': len(errors),
            'avg_ms': round(total_ms / max(1, len(items)), 1),
            'memories_new': len(fresh),
            'learning_jobs_new': len(queued),
        },
        'items': items,
    }
    target = Path(args.json) if args.json else (
        ROOT / 'artifacts' / ('rehearse-private-%s.json' % time.strftime('%Y%m%d-%H%M%S')))
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
