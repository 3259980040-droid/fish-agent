"""主动开口：安静够久的群，给她一次自己决定要不要开头说话的机会。

和参考实现（qq-bridge 的 scheduleProactiveCheckV2/sendWakePromptV2）同一个思路，但
调度器是这一轮维护循环里的一次检查，不是每个群一个定时器：一个会话一条记录，
下次检查时间落在既有的 data/social/group-policy.db 里，重启只会接着用它，不会
重新掷骰子打乱节奏。

三件事分得很开，各自可以单独验证：
  1. 到期（due/claim）：这张表说了算，不看进程内存；
  2. 判定（evaluate）：群状态、安静时长、冷却与上限、概率及其修正，全部只读；
  3. 生成与发送（draft/_open）：生成调 fish_chat.respond，发送只走既有 _deliver_reply。

她说完不说、说不说，都由她自己+概率决定：草稿窗口和群回复窗口一样绑了工具身份，
所以她的开场白也要调用 fish_say 才会出去——没调用就是这一轮想了不发，只记审计，
一条消息也不发。她的正文从此只是她的思考，和回复路径是同一个规矩。
"""
from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from group_policy import DEFAULTS, GroupPolicy
from storage import database

# One table, in the store the group already has. No new database file: the policy a
# group runs on and the clock this module keeps for it must not be able to disagree
# about which group they are talking about.
SCHEMA = ('CREATE TABLE IF NOT EXISTS group_proactive(chat TEXT PRIMARY KEY,'
          'next_check REAL,last_attempt REAL,last_sent REAL,streak INTEGER NOT NULL DEFAULT 0)')


def _ensure_schema(db):
    """Existing tables predate the streak column; add it once, never rewrite rows."""
    db.execute(SCHEMA)
    if 'streak' not in {row[1] for row in db.execute('PRAGMA table_info(group_proactive)')}:
        db.execute('ALTER TABLE group_proactive ADD COLUMN streak INTEGER NOT NULL DEFAULT 0')


def _self_wake_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS group_self_wake(chat TEXT PRIMARY KEY, at REAL NOT NULL,'
               'chain INTEGER NOT NULL DEFAULT 0, interest REAL NOT NULL DEFAULT 0,'
               "topic TEXT NOT NULL DEFAULT '', created REAL NOT NULL)")


def request_self_wake(root, chat, interest, topic, delay=3.0, now=None):
    """She asked to look again after this turn; nothing here interrupts thinking."""
    now = time.time() if now is None else float(now)
    try:
        score = float(interest)
    except (TypeError, ValueError):
        raise ValueError('兴趣值需为 0–1 的数字')
    if not 0.0 <= score <= 1.0:
        raise ValueError('兴趣值需为 0–1 的数字')
    if score < 0.7:
        raise ValueError('兴趣不足 0.7，不需要再看一眼')
    wait = max(3.0, min(600.0, float(delay or 3.0)))
    with database(_path(root)) as db:
        _self_wake_schema(db)
        row = db.execute('SELECT chain FROM group_self_wake WHERE chat=?', (str(chat),)).fetchone()
        chain = int(row[0] or 0) if row else 0
        if chain >= 3:
            raise ValueError('连续自唤醒已达 3 次，这一轮不再找你')
        db.execute('INSERT INTO group_self_wake(chat,at,chain,interest,topic,created) VALUES(?,?,?,?,?,?) '
                   'ON CONFLICT(chat) DO UPDATE SET at=excluded.at,chain=excluded.chain,'
                   'interest=excluded.interest,topic=excluded.topic,created=excluded.created',
                   (str(chat), now + wait, chain + 1, score, str(topic or '')[:120], now))
    return {'scheduled_at': now + wait, 'chain': chain + 1, 'interest': score}


def take_self_wake(root, chat, now=None):
    """Consume a due self-wake; only counts when something new actually arrived."""
    now = time.time() if now is None else float(now)
    with database(_path(root)) as db:
        _self_wake_schema(db)
        row = db.execute('SELECT * FROM group_self_wake WHERE chat=?', (str(chat),)).fetchone()
        if row is None or float(row['at'] or 0.0) > now:
            return None
        last_attempt = None
        attempt = db.execute('SELECT last_attempt FROM group_proactive WHERE chat=?', (str(chat),)).fetchone()
        last_attempt = float(attempt[0] or 0.0) if attempt else 0.0
        db.execute('DELETE FROM group_self_wake WHERE chat=?', (str(chat),))
    try:
        import sqlite3 as _sqlite3
        from contextlib import closing as _closing
        with _closing(_sqlite3.connect((Path(root) / 'data/qq/qq.db').resolve().as_uri() + '?mode=ro', uri=True)) as _db:
            fresh = int(_db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND direction='inbound' "
                                    "AND ts > datetime(?,'unixepoch','localtime')", (str(chat), last_attempt)).fetchone()[0])
    except Exception:
        fresh = 0
    return {'topic': str(row['topic'] or ''), 'interest': float(row['interest'] or 0.0), 'fresh': fresh}


def _backoff(root):
    """Owner-tunable pacing: base/cap/start_after plus a busy-chat bypass."""
    from storage import read_json
    settings = dict(read_json(Path(root) / 'harness/control.json'))
    settings.update(read_json(Path(root) / 'harness/context-settings.json', {}) or {})
    return (float(settings.get('round_backoff_base', 15)),
            float(settings.get('round_backoff_cap', 300)),
            int(settings.get('round_backoff_start_after', 2)),
            int(settings.get('round_bypass_messages', 6)),
            float(settings.get('round_min_interval', 60)))
# A generation is a paid model call on a maintenance thread; an overrun is audited
# and never sent, exactly like the group reply path's own bound.
TIMEOUT_S = 60.0
# The probability modifiers, each one read off the reference implementation's numbers:
# something she wanted to say is a reason to speak, having just spoken is a reason not
# to, the small hours are nobody's hour, and a group she has been filling is already
# answered.
THOUGHT_FACTOR = 1.4
JUST_SPOKE_FACTOR = 0.5
NIGHT_FACTOR = 0.3
BUSY_FACTOR = 0.3
JUST_SPOKE_S = 1800
BUSY_SENT = 5
NIGHT_START, NIGHT_END = 23, 8
# What she is asked to do, in her own turn. Both halves matter: the invitation, and
# the plain sentence that makes "no" a first-class answer - a turn that calls no
# sending tool says nothing, so declining needs no sentinel and no empty reply.
PROMPT = ('【主动开口】这个群已经安静了 %d 分钟，没有新消息进来。'
          '你可以主动开个头：接住刚才聊到一半的事、提一个大家都接得上的话题，或者说一句你自己想说的话。'
          '想开口就用发送工具把话交出去；不想说就什么都不做，不要勉强，也不要复述聊天记录。')
# 这个窗口的会话名：和群回复窗口（group-fish:<chat>）共用同一个群面，但是另一条待发
# 队列，所以草稿不会被正在生成的回复吞掉，回复也改不了草稿。
SESSION = 'group-proactive:%s'
NOTES = ('这一轮没有谁在说话，是她自己想开口。别假装有人刚问过她什么，'
         '别逐条补答聊天记录，一句话就够。')


def _path(root):
    return Path(root) / 'data/social/group-policy.db'


def _read(path, sql, args=()):
    """One read-only query against a store the service may be writing."""
    path = Path(path)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        return [dict(row) for row in conn.execute(sql, args)]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _stamp(value):
    """A stored ISO timestamp as epoch seconds; an unreadable one is not a time."""
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _interval(config, root=None):
    """The random check interval, clamped so a broken value cannot become a flood.

    The owner's pacing in context-settings.json wins over the per-chat policy, so the
    number edited in the settings page is the number that actually paces her.
    """
    override = {}
    if root is not None:
        from storage import read_json
        merged = dict(read_json(Path(root) / 'harness/control.json'))
        merged.update(read_json(Path(root) / 'harness/context-settings.json', {}) or {})
        override = {k: v for k, v in merged.items() if k in ('proactive_check_min_s', 'proactive_check_max_s')}
    source = {**config, **override}
    low = float(source.get('proactive_check_min_s') or DEFAULTS['proactive_check_min_s'])
    high = float(source.get('proactive_check_max_s') or DEFAULTS['proactive_check_max_s'])
    low, high = min(low, high), max(low, high)
    return max(60.0, low), max(60.0, high)


def next_check(root, chat, now=None):
    """When this chat is looked at again, or 0.0 when it has never been scheduled."""
    rows = _read(_path(root), 'SELECT next_check FROM group_proactive WHERE chat=?', (str(chat),))
    try:
        return float(rows[0]['next_check'] or 0.0) if rows else 0.0
    except (KeyError, TypeError, ValueError):
        return 0.0


def due(root, chat, now=None):
    """Is this chat's check due? An unscheduled chat is due the first time it is asked."""
    now = time.time() if now is None else float(now)
    stored = next_check(root, chat)
    return stored <= now


def claim(root, chat, now=None):
    """Take the due check, or return None when it is not this chat's turn yet.

    The next check is rolled *before* anything else happens, so an attempt that then
    fails, is declined, or dies with the process has still consumed the slot: a
    restart cannot re-roll a fresh interval and a broken send cannot tighten the pace.
    """
    now = time.time() if now is None else float(now)
    config = GroupPolicy(root).config(str(chat))
    low, high = _interval(config, root)
    with database(_path(root)) as db:
        db.execute('BEGIN IMMEDIATE')
        _ensure_schema(db)
        row = db.execute('SELECT * FROM group_proactive WHERE chat=?', (str(chat),)).fetchone()
        base, cap, start_after, bypass, floor_s = _backoff(root)
        streak = int(row['streak'] or 0) if row is not None else 0
        last_attempt = float(row['last_attempt'] or 0.0) if row is not None else 0.0
        burst = 0
        if last_attempt > 0:
            try:
                import sqlite3 as _sqlite3
                from contextlib import closing as _closing
                with _closing(_sqlite3.connect((Path(root) / 'data/qq/qq.db').resolve().as_uri() + '?mode=ro', uri=True)) as _db:
                    burst = int(_db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND direction='inbound' "
                                            "AND ts > datetime(?,'unixepoch','localtime')", (str(chat), last_attempt)).fetchone()[0])
            except Exception:
                burst = 0
        pending = take_self_wake(root, chat, now)
        if pending is not None and int(pending.get('fresh') or 0) > 0:
            # She asked for this look, and something new did arrive: allow it now.
            streak = 0
        elif burst >= bypass:
            # A busy group wins over any backoff: worth looking immediately, streak reset.
            streak = 0
        elif row is not None and float(row['next_check'] or 0.0) > now:
            return None
        wait = random.uniform(low, high)
        if streak >= start_after:
            wait = max(wait, min(cap, base * (2 ** (streak - start_after))))
        wait = max(wait, floor_s)
        scheduled = now + wait
        db.execute('INSERT INTO group_proactive(chat,next_check,last_attempt,last_sent,streak) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(chat) DO UPDATE SET next_check=excluded.next_check,'
                   'last_attempt=excluded.last_attempt,streak=excluded.streak',
                   (str(chat), scheduled, now, float(row['last_sent'] or 0.0) if row is not None else 0.0, streak + 1))
    return scheduled


def mark_sent(root, chat, now=None):
    """The slot the cooldown is measured from: only a delivered opener is a send."""
    now = time.time() if now is None else float(now)
    with database(_path(root)) as db:
        db.execute('BEGIN IMMEDIATE')
        _ensure_schema(db)
        db.execute('INSERT INTO group_proactive(chat,next_check,last_attempt,last_sent,streak) VALUES(?,?,?,?,0) '
                   'ON CONFLICT(chat) DO UPDATE SET last_sent=excluded.last_sent,streak=0',
                   (str(chat), 0.0, now, now))


def _quiet_since(root, chat, now):
    """Seconds since the group's last inbound line, or None when it has none."""
    rows = _read(Path(root) / 'data/qq/qq.db',
                 "SELECT ts FROM messages WHERE chat_id=? AND direction='inbound' ORDER BY id DESC LIMIT 1",
                 (str(chat),))  # 入站时间只从群消息账本读：安静多久是 qq.db 的事实
    if not rows:
        return None
    stamp = _stamp(rows[0]['ts'])
    return None if stamp <= 0 else max(0.0, now - stamp)


def _her_hour(root, chat, now):
    """(how many lines she delivered this hour, when the last one was)."""
    rows = _read(Path(root) / 'data/qq/qq.db',
                 "SELECT ts FROM messages WHERE chat_id=? AND direction='outbound' "
                 "AND COALESCE(status,'')='sent' ORDER BY id DESC LIMIT 40", (str(chat),))
    stamps = [stamp for stamp in (_stamp(row['ts']) for row in rows) if stamp > 0]
    recent = [stamp for stamp in stamps if now - 3600 <= stamp <= now]
    return len(recent), (max(stamps) if stamps else 0.0)


def _busy(root, chat):
    """A reply is already being generated or sent in this chat: she is not free to chat."""
    rows = _read(_path(root), "SELECT 1 FROM reservations WHERE chat=? AND state='active'", (str(chat),))
    return bool(rows)


def _pending_thought(root, chat):
    """Anything she wanted to say and has not said yet (the 2-hour thought note)."""
    try:
        from memory_store import MemoryStore
        return bool((MemoryStore(root, 'qq').social_context('chat:' + str(chat)) or {}).get('thought'))
    except Exception:  # noqa: BLE001 - a memory miss is a modifier that did not apply
        return False


def probability(root, chat, config, now, *, sent_hour=0, last_say=0.0):
    """The chance this check opens with, plus which modifiers moved it.

    Capped at 1.0: the modifiers can only ever make an opener more or less likely than
    the group's own setting, never turn it into a guarantee the setting did not ask for.
    """
    value = float(config.get('proactive_probability') or 0.0)
    factors = []
    if value <= 0:
        return 0.0, factors
    if _pending_thought(root, chat):
        value *= THOUGHT_FACTOR
        factors.append('有待用想说 ×%s' % THOUGHT_FACTOR)
    if last_say and now - last_say < JUST_SPOKE_S:
        value *= JUST_SPOKE_FACTOR
        factors.append('刚说过话 ×%s' % JUST_SPOKE_FACTOR)
    if time.localtime(now).tm_hour >= NIGHT_START or time.localtime(now).tm_hour < NIGHT_END:
        value *= NIGHT_FACTOR
        factors.append('夜间 ×%s' % NIGHT_FACTOR)
    if sent_hour >= BUSY_SENT:
        value *= BUSY_FACTOR
        factors.append('一小时内已发 %d 条 ×%s' % (sent_hour, BUSY_FACTOR))
    return min(1.0, value), factors


def evaluate(root, chat, now=None, *, roll=None):
    """Every gate between a due check and a paid model call, in one read-only answer.

    Order is the reference implementation's: authorisation first, then "is this group
    actually quiet", then the limits that pace her initiative, and the probability
    last, so a refusal always names the first reason it met.
    """
    now = time.time() if now is None else float(now)
    chat = str(chat)
    config = GroupPolicy(root).config(chat)
    answer = {'chat': chat, 'attempt': False, 'reason': '', 'quiet_s': 0.0,
              'probability': 0.0, 'factors': []}

    def no(reason, **extra):
        return {**answer, 'reason': reason, **extra}
    if config['engine'] != 'fish':
        return no('引擎不是 fish')
    if not (config['listen'] is True and config['reply'] is True):
        return no('旁听或发言未开启')
    dive = float(config['dive_until'] or 0)
    if now < dive:
        return no('潜水到 ' + time.strftime('%H:%M', time.localtime(dive)))
    if _busy(root, chat):
        return no('正在处理候选')
    quiet = _quiet_since(root, chat, now)
    if quiet is None:
        return no('没有入站消息')
    idle = float(config['proactive_idle_s'] or 0)
    if quiet < idle:
        return no('群里还热闹（安静 %.0f 分钟 < %.0f 分钟）' % (quiet / 60, idle / 60),
                  quiet_s=quiet)
    rows = _read(_path(root), 'SELECT last_sent FROM group_proactive WHERE chat=?', (chat,))
    last_sent = float(rows[0]['last_sent'] or 0.0) if rows else 0.0
    cooldown = float(config['active_cooldown_s'] or 0)
    if last_sent and now - last_sent < cooldown:
        return no('主动开口冷却中（还剩 %d 分钟）' % max(1, int((cooldown - (now - last_sent)) / 60)),
                  quiet_s=quiet)
    sent_hour, last_say = _her_hour(root, chat, now)
    # The same per-hour ceiling the reply path reads (a reply is refused at
    # len(active) >= max_active_hour), so 0 blocks this too instead of meaning "unlimited".
    cap = int(config['max_active_hour'] or 0)
    if sent_hour >= cap:
        return no('主动发送达到每小时上限（%d/%d）' % (sent_hour, cap), quiet_s=quiet)
    chance, factors = probability(root, chat, config, now, sent_hour=sent_hour, last_say=last_say)
    answer.update(quiet_s=quiet, probability=chance, factors=factors,
                  last_sent=last_sent, sent_hour=sent_hour, last_say=last_say)
    draw = random.random() if roll is None else float(roll)
    if draw >= chance:
        return {**answer, 'reason': '本次未被抽中（%.3f ≥ %.2f）' % (draw, chance)}
    return {**answer, 'attempt': True, 'reason': '可以试试开口', 'roll': draw}


def _declined(text):
    """A submitted sentinel is still silence, not a reply to post.

    Since this window sends through fish_say, "no" is normally a turn that calls
    nothing (TurnSilent). This keeps the wire-level sentinels dead too, so a
    NO_REPLY that got submitted as a text bubble is never posted into the group.
    """
    from group_decide import SILENT_WORDS, _bare
    body = str(text or '')
    return not body.strip() or _bare(body) in SILENT_WORDS


def draft(root, chat, *, quiet_s=0.0, cancelled=None):
    """Ask the same decision core she answers with what she would say unprompted.

    No message is being answered, so the history is the group's recent talk as ambient
    context and the request is the invitation itself. The invitation is still a turn:
    this window is bound to the group's tool identity before the model runs, so her
    opener leaves through the same fish_say queue a reply does, and a turn that calls
    no sending tool is a turn she kept quiet. Nothing here sends - the queue is read
    back as a candidate and _open stays the only sender.
    """
    from group_decide import (HISTORY_LIMIT, HISTORY_WINDOW, _bubbles, _dialogue,
                              _group_memory, _names, _owner_revision, _persona)
    from chat_output import TurnSilent
    from fish_chat import respond
    from tool_host import bind_session, take_said
    chat = str(chat)
    session = SESSION % chat
    now = time.time()
    names = _names(root, chat)
    rows = [row for row in _dialogue(root, chat, names) if row['created'] >= now - HISTORY_WINDOW]
    history = [json.loads(row['message']) for row in rows][-HISTORY_LIMIT:]
    persona = _persona(root, _owner_revision(root))
    body = PROMPT % max(1, int(round(float(quiet_s) / 60)))
    # 没有入站消息可回，但她说的话一样要经过发送工具：先把这一轮绑成这个群的主动
    # 开口会话。绑定不上就不开口——绝不退回"正文直接当消息"那条老路，那条路等于给
    # 这个窗口一个不需要她决定的发送权。绑定本身放宽了哪些判断，tool_host.bind 里
    # 记了一条 tool.identity.proactive 审计。
    try:
        bind_session(Path(root), 'qq', session, 'chat:' + chat, '')
    except Exception as exc:  # noqa: BLE001 - 没有身份就没有发言权，这一轮不开口
        return {'text': '', 'segments': [], 'declined': True, 'raw': '', 'revision': persona['id'],
                'said': False, 'reason': '', 'error': 'no-tool-identity: ' + type(exc).__name__
                + ': ' + str(exc)[:160]}
    state = {'previewed': set(), 'tools': []}
    try:
        raw = respond(root, 'qq', persona=persona, history=history, history_source='group-proactive',
                      memory=_group_memory(root, chat, body), scope='chat:' + chat,
                      references=NOTES, current=[{'type': 'text', 'text': body}],
                      session=session, cancelled=cancelled,
                      origin=session, output='bubbles', emoji_state=state,
                      tool_context='group')
    except TurnSilent:
        # 她这一轮想了但没发：turn_no_send 已经由决策层写进审计，这里只是不开口。
        return {'text': '', 'segments': [], 'declined': True, 'raw': '', 'revision': persona['id'],
                'said': False, 'reason': ''}
    said = take_said(root, session)
    raw = str(raw or '').strip()
    bubbles = _bubbles(root, chat, raw, state)
    if _declined(bubbles.get('text')):
        return {'text': '', 'segments': [], 'declined': True, 'raw': raw, 'revision': persona['id'],
                'said': bool(said['said']), 'reason': said['reason']}
    return {'text': bubbles['text'], 'segments': bubbles['segments'], 'declined': False,
            'raw': raw, 'revision': persona['id'], 'said': bool(said['said']),
            'reason': said['reason']}


def _chats(loop):
    """The groups she may speak in at all: the channel's own allowlist, nothing else."""
    numbers = {str(number).strip() for number in (getattr(loop, 'group_ids', ()) or ())}
    return sorted({number if number.startswith('group:') else 'group:' + number
                   for number in numbers if number})


def _audit(loop, kind, reason):
    try:
        loop.governance.audit(kind, str(reason)[:250])
    except Exception:  # noqa: BLE001 - an audit miss never breaks the maintenance round
        pass


async def tick(loop):
    """One maintenance round: look at the due chats, try at most one of them.

    At most one attempt per round is the whole point: a generation takes seconds to
    minutes, and this runs inside the loop's 300-second heartbeat, so a second group
    would turn one slow model call into a stalled maintenance cycle.
    """
    if loop._stop.is_set():
        return {'attempted': False, 'reason': 'stopped'}
    if loop.governance.config()['paused']:
        return {'attempted': False, 'reason': 'paused'}
    root = Path(loop.root)
    for chat in _chats(loop):
        try:
            if not GroupPolicy(root).config(chat)['proactive_enabled']:
                continue
            scheduled = claim(root, chat)
            if scheduled is None:
                continue
            verdict = evaluate(root, chat)
            if not verdict['attempt']:
                _audit(loop, 'group_proactive_skipped', verdict['reason'] + ' ' + chat)
                continue
            _audit(loop, 'group_proactive_attempt',
                   '安静 %.0f 分钟 · 概率 %.2f%s %s' % (
                       verdict['quiet_s'] / 60, verdict['probability'],
                       ('（' + '，'.join(verdict['factors']) + '）') if verdict['factors'] else '',
                       chat))
            return await _open(loop, chat, verdict)
        except Exception as exc:  # noqa: BLE001 - one broken group never stops the others
            _audit(loop, 'group_proactive_failed', type(exc).__name__ + ': ' + str(exc)[:200] + ' ' + chat)
    return {'attempted': False}


async def _open(loop, chat, verdict):
    """Generate one opener, and either send it through the group's only send path or not."""
    root = Path(loop.root)
    try:
        made = await asyncio.wait_for(
            asyncio.to_thread(draft, root, chat, quiet_s=verdict['quiet_s']), TIMEOUT_S)
    except TimeoutError:
        _audit(loop, 'group_proactive_failed', '生成超过 %d 秒，未发送 %s' % (TIMEOUT_S, chat))
        return {'attempted': True, 'sent': False, 'reason': 'timeout', 'chat': chat}
    made = made if isinstance(made, dict) else {}
    text = str(made.get('text') or '')
    if made.get('declined') or _declined(text):
        # 两种情况分开记：她压根没调用发送工具（想了不发），和她交了话却被判成沉默哨兵。
        _audit(loop, 'group_proactive_declined',
               (('她这一轮没有调用发送工具，想了不发 ' + str(made.get('error') or '')[:80]).strip()
                if not made.get('said')
                else '她交了话但内容是沉默哨兵：' + str(made.get('raw') or '')[:100])
               + ' ' + chat)
        return {'attempted': True, 'sent': False, 'reason': 'declined', 'chat': chat}
    if loop._stop.is_set():
        return {'attempted': True, 'sent': False, 'reason': 'stopped', 'chat': chat}
    key = 'group-proactive:' + str(int(time.time()))
    try:
        delivered = await loop._deliver_reply(
            chat, made.get('segments') or [{'type': 'text', 'text': text}], key)
    except Exception as exc:  # noqa: BLE001 - a failed send is recorded, never retried
        _audit(loop, 'group_proactive_failed', type(exc).__name__ + ': ' + str(exc)[:200] + ' ' + chat)
        return {'attempted': True, 'sent': False, 'reason': 'send-failed', 'chat': chat, 'text': text}
    delivered = delivered if isinstance(delivered, dict) else {}
    if not delivered.get('ok'):
        error = delivered.get('error') or {}
        _audit(loop, 'group_proactive_failed',
               str(error.get('code') or 'send-failed')[:200] + ' ' + chat)
        return {'attempted': True, 'sent': False, 'reason': 'send-failed', 'chat': chat, 'text': text}
    mark_sent(root, chat)
    reason = str(made.get('reason') or '').strip()
    _audit(loop, 'group_proactive_sent',
           ('她说的（' + reason + '）：' if reason else '她说的：') + text[:150] + ' ' + chat)
    return {'attempted': True, 'sent': True, 'chat': chat, 'text': text, 'key': key}
