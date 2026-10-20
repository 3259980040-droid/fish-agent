"""Focused checks for owner memory, hybrid recall, embeddings and maintenance.

Runs against a temporary ``root`` only; never touches data/ of the real Fish
root. Usage::

    python tools/test_memory_workflow.py            # focused checks (no model)
    python tools/test_memory_workflow.py --semantic # also probe the real model
                                                    # worker (downloads the model
                                                    # into data/shared/models)

No network calls except the optional ``--semantic`` model download through the
vendored fastembed worker, and no notifications of any kind.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent / 'harness'
sys.path.insert(0, str(HARNESS))

import memory_maintenance as mm  # noqa: E402
import memory_source  # noqa: E402
from memory_store import MemoryStore  # noqa: E402

TZ = timezone(timedelta(hours=8))
RESULTS = []


def check(name, condition, detail=''):
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (('  -- ' + str(detail)) if detail and not condition else ''))


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def make_root():
    root = Path(tempfile.mkdtemp(prefix='fish-memory-check-'))
    (root / 'harness').mkdir()
    (root / 'channels' / 'fish_qq').mkdir(parents=True)
    (root / 'channels' / 'fish_qq' / 'config.json').write_text('{"bot_id":"10001","allowlist":{"owner_id":"20001"}}', encoding='utf-8')
    # Model worker disabled for the deterministic checks (no venv/model use).
    (root / 'harness' / 'memory-embeddings.json').write_text('{"enabled": false}\n', encoding='utf-8')
    for role in ('qq', 'research'):
        (root / 'data' / role).mkdir(parents=True)
    return root


def qq_db(root, rows, start_id=1):
    path = root / 'data' / 'qq' / 'qq.db'
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT, "
                   "user_id TEXT, text TEXT, replied INTEGER DEFAULT 0, conversation_id TEXT, sender_id TEXT, "
                   "direction TEXT, channel_message_id TEXT, reply_to TEXT, segments TEXT, status TEXT, "
                   "runtime_session_id TEXT, runtime_turn_id TEXT, error TEXT, cancelled INTEGER DEFAULT 0)")
        next_id = max([start_id - 1, db.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0]])
        for index, (ts, chat, sender, text, direction) in enumerate(rows):
            next_id += 1
            db.execute('INSERT INTO messages(id,ts,chat_id,user_id,text,conversation_id,sender_id,direction,'
                       'channel_message_id,status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (next_id, ts, chat, sender, text, 'qq:conversation:' + chat, sender, direction,
                        str(900000 + next_id), 'processed' if direction == 'inbound' else 'sent'))
        db.commit()
    return next_id


def desktop_db(root, role, conversations):
    """Create the real desktop-chat schema; ``conversations`` maps id -> mode."""
    path = root / 'data' / role / 'desktop-chat.db'
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,title TEXT,session_id TEXT,"
                   "updated_at TEXT,space_id TEXT NOT NULL DEFAULT '',mode TEXT NOT NULL DEFAULT 'chat',"
                   "workspace TEXT NOT NULL DEFAULT '',revision TEXT NOT NULL DEFAULT '',permission TEXT NOT NULL DEFAULT 'workspace-write')")
        db.execute('CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,conversation_id TEXT,direction TEXT,'
                   'text TEXT,segments TEXT,status TEXT,error TEXT,created_at TEXT)')
        for key, mode in conversations.items():
            db.execute('INSERT OR REPLACE INTO conversations(id,title,mode) VALUES(?,?,?)', (key, key, mode))
        db.commit()


def desktop_message(root, role, conversation_id, ts, text, direction='inbound', status='done'):
    path = root / 'data' / role / 'desktop-chat.db'
    with closing(sqlite3.connect(path)) as db:
        cursor = db.execute('INSERT INTO messages(conversation_id,direction,text,status,created_at) VALUES(?,?,?,?,?)',
                            (conversation_id, direction, text, status, ts))
        db.commit()
        return cursor.lastrowid


class FakeGateway:
    key = 'fake'

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def json(self, system, user, **kwargs):
        self.calls.append(user)
        if not self.replies:
            raise AssertionError('unexpected model call: ' + user[:60])
        return self.replies.pop(0)


# ---------------------------------------------------------------------------
# 1. owner facts: save / update / recall
# ---------------------------------------------------------------------------

def test_owner_facts(root):
    store = MemoryStore(root, 'qq')
    result = store.save('主人喜欢喝柠檬水', category='偏好', scope='owner', source='manual',
                        source_text='主人亲自说喜欢柠檬水', kind='preference', evidence='self_report',
                        subject='qq:20001')
    first = result['id']
    check('owner fact saved', result.get('ok') and first, result)
    again = store.save('主人喜欢喝柠檬水', category='偏好', scope='owner', subject='qq:20001')
    check('same owner fact not duplicated', again.get('duplicate') and again['id'] == first, again)
    with closing(sqlite3.connect(root / 'data/qq/memory.db')) as db:
        count = db.execute("SELECT COUNT(*) FROM memories WHERE text='主人喜欢喝柠檬水'").fetchone()[0]
    check('owner fact stored once', count == 1, count)
    # Work and owner chat share facts; private spaces are only used for owner recall.
    store.save('主人周末在学日语', scope='owner', source='manual', kind='fact', subject='qq:20001')
    social = root / 'data' / 'social'
    social.mkdir(parents=True, exist_ok=True)
    # SocialContext records space origins in data/social/context.db (not social.db).
    with closing(sqlite3.connect(social / 'context.db')) as db:
        db.execute('CREATE TABLE spaces(id TEXT PRIMARY KEY, role TEXT, origin TEXT)')
        db.executemany('INSERT INTO spaces VALUES(?,?,?)', [
            ('a' * 32, 'qq', 'desktop'), ('b' * 32, 'qq', 'owner-qq'), ('c' * 32, 'qq', 'group')])
        db.commit()
    # Real space memories are read-only for this store; seed them like the
    # original owner test windows were written by the earlier pipeline.
    with closing(sqlite3.connect(root / 'data/qq/memory.db')) as db:
        for sid, text in (('a' * 32, '主人在测试窗口里的验证词是青柠纸船'),
                          ('b' * 32, '主人 QQ 私聊窗口的旧代号是蓝鲸'),
                          ('c' * 32, '群测试窗口里的口号是青椒炒蛋')):
            db.execute("INSERT INTO memories(id,text,category,scope,source,source_text,confidence,created,updated,kind,evidence,status) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (sid, text, '事实', 'space:' + sid, 'desktop:1', text,
                                                         1.0, time.time(), time.time(), 'fact', 'self_report', 'active'))
        db.commit()
    store.save('群里有人说主人喜欢喝乌龙茶', scope='chat:group:1043230169', source='qq-group:1043230169:1',
               subject='qq:30003', kind='preference', evidence='self_report', status='active')
    owner_rows = store.search('柠檬水', scope='owner', limit=20)
    space_rows = store.recall('验证词 青柠纸船', scope='owner', limit=20)
    owner_qq_rows = store.recall('旧代号 蓝鲸', scope='owner', limit=20)
    group_test_rows = store.recall('口号 青椒炒蛋', scope='owner', limit=20)
    group_rows = store.recall('柠檬水', scope='chat:group:1043230169', limit=20)
    check('owner recall reads owner/public facts', any('柠檬水' in row['text'] for row in owner_rows), [r['text'] for r in owner_rows])
    check('owner recall reads owner-desktop space memories', any(r['scope'] == 'space:' + 'a' * 32 for r in space_rows),
          [(r['text'], r['scope']) for r in space_rows])
    check('owner recall reads owner-qq space memories', any(r['scope'] == 'space:' + 'b' * 32 for r in owner_qq_rows),
          [r['scope'] for r in owner_qq_rows])
    check('owner recall never reads group test windows', not any(r['scope'] == 'space:' + 'c' * 32 for r in group_test_rows),
          [r['scope'] for r in group_test_rows])
    role_space = store.search('验证词', scope='space:' + 'a' * 32, limit=20)
    check('a space scope only reads that space', [r['scope'] for r in role_space] == ['space:' + 'a' * 32], [r['scope'] for r in role_space])
    check('group scope never reads owner facts', not any('柠檬水' in row['text'] for row in group_rows), [r['text'] for r in group_rows])
    work_rows = store.search('日语', scope='owner', limit=20)
    check('work/owner chat share owner facts', any('日语' in r['text'] for r in work_rows), [r['text'] for r in work_rows])
    check('non-owner search does not read owner scope', not any('柠檬水' in r['text'] for r in group_rows))

    # Correction keeps id, source and full history.
    corrected = store.save('主人最近改喝柠檬茶', memory_id=first, scope='owner', source='manual',
                           source_text='主人更正：现在喝柠檬茶', category='偏好', kind='preference',
                           subject='qq:20001')
    with closing(sqlite3.connect(root / 'data/qq/memory.db')) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute('SELECT * FROM memories WHERE id=?', (first,)).fetchone())
        history = [dict(r) for r in db.execute('SELECT * FROM memory_history WHERE memory_id=? ORDER BY revision', (first,))]
    check('correction updates same active fact', corrected.get('id') == first and row['text'] == '主人最近改喝柠檬茶', row['text'])
    check('correction preserves source', row['source'] == 'manual' and row['source_text'] == '主人亲自说喜欢柠檬水', row['source_text'])
    check('correction keeps history', [h['text'] for h in history] == ['主人喜欢喝柠檬水', '主人最近改喝柠檬茶'],
          [h['text'] for h in history])
    check('history revision records operation', history[-1]['operation'] == 'correction', history[-1]['operation'])
    check('store.history API', [h['revision'] for h in store.history(first)] == [2, 1])
    old = store.recall('柠檬水', scope='owner')
    check('recall shows only corrected active text', not any('喜欢喝柠檬水' in r['text'] for r in old), [r['text'] for r in old])
    check('recall result carries source/time metadata',
          all({'source', 'kind', 'observed_at', 'rank', 'relevance', 'scope'} <= set(r) for r in old), [r.get('matched_by') for r in old])

    # Auto extraction includes confirmed events/relationships, but not roleplay.
    job = {'scope': 'owner', 'source': 'qq:555', 'subject': 'qq:20001', 'text': '上周和妈妈去看了海，还约了大学室友下个月吃饭'}
    applied = store.apply_extraction(job, {'memories': [
        {'text': '主人上周和妈妈去看了海', 'category': '事件', 'kind': 'event', 'evidence': 'observed', 'confidence': 0.9},
        {'text': '主人的大学室友约了下个月一起吃饭', 'category': '关系', 'kind': 'relationship', 'evidence': 'self_report', 'confidence': 0.9},
        {'text': '主人是魔法学院的星使鱼', 'category': '关系', 'kind': 'relationship', 'evidence': 'roleplay', 'confidence': 0.95},
    ]})
    with closing(sqlite3.connect(root / 'data/qq/memory.db')) as db:
        db.row_factory = sqlite3.Row
        statuses = {r['text']: (r['kind'], r['status'], r['evidence']) for r in db.execute(
            "SELECT * FROM memories WHERE source='qq:555'")}
    check('confirmed event stored active', statuses.get('主人上周和妈妈去看了海') == ('event', 'active', 'observed'), statuses)
    check('confirmed relationship stored active', statuses.get('主人的大学室友约了下个月一起吃饭') == ('relationship', 'active', 'self_report'), statuses)
    check('roleplay stays candidate', statuses.get('主人是魔法学院的星使鱼', (None, ''))[1] == 'candidate', statuses)
    check('extraction results reported', len(applied) == 3, applied)
    prompt = store.context('柠檬茶', scope='owner')
    check('context announces lexical-only mode honestly', '语义向量未就绪' in prompt, prompt[:120])
    check('context marks source', '来源：' in prompt, prompt)


# ---------------------------------------------------------------------------
# 2. hybrid retrieval with an honest semantic channel
# ---------------------------------------------------------------------------

def test_semantic_honesty(root):
    from memory_engine import MemoryEngine
    MemoryStore(root, 'qq').save('主人喜欢柠檬茶', scope='owner', source='manual')
    engine = MemoryEngine(root, 'qq')
    status = engine.embedding_status()
    check('semantic status disabled when the worker is off', status['status'] == 'disabled' and status['mode'] == 'lexical', status['status'])
    recalled = engine.recall(MemoryStore(root, 'qq'), '柠檬茶', scope='owner', with_status=True)
    check('recall works without embeddings', recalled['results'] and recalled['semantic']['status'] == 'lexical-only',
          recalled['semantic'])
    check('no vectors stored while disabled', status['stored_vectors'] == 0, status['stored_vectors'])
    source = memory_source.source_status(root)
    check('upstream lifecycle module loaded from pinned commit',
          source['lifecycle']['loaded'] and source['commit'] == memory_source.MAIBOT_COMMIT, source['lifecycle'])
    check('upstream calibration module loaded', source['calibration']['loaded'], source['calibration'])


def test_fusion(root):
    """Fusion ranking: a paraphrase found only semantically outranks noise."""
    from memory_engine import MemoryEngine
    store = MemoryStore(root, 'qq')
    rows = []
    for text in ('主人最近改喝柠檬茶', '主人周末在学日语', '主人养了一只叫豆豆的猫'):
        store.save(text, scope='owner', source='manual', category='事实', observed=time.time())
    with closing(sqlite3.connect(root / 'data/qq/memory.db')) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT * FROM memories WHERE status='active'")]
    engine = MemoryEngine(root, 'qq')
    engine._model_vectors = lambda stored=None: {}
    fake = {row['id']: 0.1 for row in rows}
    fake[[row['id'] for row in rows if '猫' in row['text']][0]] = 0.9
    engine.query_vector = lambda query: [1.0, 0.0]
    engine._cosine_scores = lambda vector, candidates: {key: value for key, value in fake.items() if any(c['id'] == key for c in candidates)}
    engine.embedding_status = lambda: {'status': 'ready', 'ready': True, 'mode': 'hybrid', 'model': 'fake', 'pending': 0,
                                       'stored_vectors': len(rows), 'detail': ''}
    engine.warm = lambda wait=0: True
    recalled = engine.recall(store, '植物', scope='owner', with_status=True)
    top = recalled['results'][0] if recalled['results'] else {}
    check('semantic paraphrase can surface without lexical overlap', '豆豆' in top.get('text', ''), recalled['results'][:2])
    check('fused mode reported as hybrid', recalled['semantic']['mode'] == 'hybrid' and recalled['semantic']['status'] == 'fused',
          recalled['semantic'])
    check('semantic match labelled', top.get('matched_by') == 'semantic' and top.get('semantic') is not None, top.get('matched_by'))
    check('fusion metadata exposed', set(recalled['semantic']['weights']) == {'lexical', 'semantic'}, recalled['semantic']['weights'])


# ---------------------------------------------------------------------------
# 3. maintenance schedule and sources
# ---------------------------------------------------------------------------

def fill_windows(root, now, *, roles=('qq', 'research')):
    """Pretend both windows already ran so only new content matters.

    ``last_window`` is the previous window with a successful run (here the one
    just before the due window), which is what a regular re-start looks like.
    """
    for role in roles:
        state = mm._State(root / 'data' / role / 'memory.db')
        windows = mm.due_windows(now)
        for kind in ('daily', 'weekly'):
            earlier = windows[kind] - (86400 if kind == 'daily' else 7 * 86400)
            state.set('job:' + kind, {'record': {'status': 'done', 'last_window': earlier,
                                                 'attempted_window': earlier, 'summary': {}}})


def test_schedule():
    windows = mm.due_windows(at(21, 3, 59))
    check('daily window before 04:00 is previous day', mm.local_dt(windows['daily']).day == 20, mm._iso(windows['daily']))
    windows = mm.due_windows(at(21, 4, 0))
    check('daily window at exactly 04:00 is today', mm._iso(windows['daily']) == '2026-10-21 04:00', mm._iso(windows['daily']))
    windows = mm.due_windows(at(25, 4, 29))       # Sunday 2026-10-25
    check('weekly window before Sunday 04:30 is previous Sunday', mm._iso(windows['weekly']) == '2026-10-18 04:30',
          mm._iso(windows['weekly']))
    windows = mm.due_windows(at(25, 4, 30))
    check('weekly window on Sunday 04:30 is that Sunday', mm._iso(windows['weekly']) == '2026-10-25 04:30', mm._iso(windows['weekly']))
    windows = mm.due_windows(at(20, 23, 0))
    check('weekly window on Tuesday is previous Sunday', mm._iso(windows['weekly']) == '2026-10-18 04:30', mm._iso(windows['weekly']))
    upcoming = mm.next_windows(at(21, 5, 0))
    check('next daily after 04:00 is tomorrow', mm._iso(upcoming['daily']) == '2026-10-22 04:00', mm._iso(upcoming['daily']))
    check('next weekly after Sunday 04:30 is next Sunday', mm._iso(mm.next_windows(at(25, 5, 0))['weekly']) == '2026-11-01 04:30')
    check('UTC input converted to Shanghai windows',
          mm._iso(mm.due_windows(datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc))['daily']) == '2026-10-21 04:00')


def test_due_and_empty(root):
    now = at(21, 5, 0)
    qq_db(root, [(mm._iso(mm.to_timestamp(now) - 3600), 'private:20001', '20001', '早上好', 'inbound')])
    desktop_db(root, 'qq', {'work1': 'work', 'chat1': 'chat'})
    desktop_db(root, 'research', {'r1': 'chat'})
    gateway = FakeGateway([])
    maintenance = mm.MemoryMaintenance(root)
    due = {key for key in maintenance.due(now)}
    check('both jobs due on first tick', due == {('qq', 'daily'), ('qq', 'weekly'), ('research', 'daily'), ('research', 'weekly')}, due)
    # Seeded messages predate the cursor -> nothing "new".
    summary = maintenance.tick(now)
    kinds = {(item['role'], item['kind']) for item in summary['ran']}
    check('tick runs all due jobs', kinds == due, summary)
    daily_qq = next(item for item in summary['ran'] if item['kind'] == 'daily' and item['role'] == 'qq')
    check('first run starts at the newest message (no history backfill)', daily_qq['summary']['new_messages'] == 0,
          daily_qq['summary'])
    check('empty source makes no model call', daily_qq['summary'].get('model_calls', 0) == 0, daily_qq['summary'])
    check('idle tick after catch-up', maintenance.tick(now)['status'] == 'idle', maintenance.tick(now))
    status = maintenance.status()
    check('status exposes schedule and jobs', status['jobs']['qq:daily']['status'] == 'empty' and status['timezone'].startswith('Asia/Shanghai'),
          status['jobs']['qq:daily']['status'])
    check('status reports semantic readiness', status['semantic']['status'] == 'disabled', status['semantic'])


def test_new_messages_with_fake_gateway(root):
    """Regular dailies ran up to 2026-10-21; the run on 10-25 (would be a catch-up
    day) replays 10-22 onward while the live pipeline already queued the older
    message, which must not be extracted twice."""
    now = at(25, 5, 0)
    qq_db(root, [('2026-10-20 23:00:00', 'private:20001', '20001', '昨天在加班', 'inbound')])
    fill_windows(root, at(20, 5, 0), roles=('qq',))   # regular daily of 2026-10-19/20
    # Live mode (qq_agent_loop) already queued the 10-20 message; the store is
    # created once (first use baselines the history present *then*).
    MemoryStore(root, 'qq').enqueue('qq:900001', '昨天在加班', 'owner', subject='qq:20001')
    maintenance = mm.MemoryMaintenance(root)
    # One new owner message (plus the bot's own reply, which must never be read).
    new_id = qq_db(root, [('2026-10-22 02:00:00', 'private:20001', '20001', '我上周和妈妈去看了海', 'inbound'),
                          ('2026-10-22 02:00:05', 'private:20001', 'bot', '哇，海边好玩吗？', 'outbound')])
    gateway = FakeGateway([{'memories': [
        {'text': '主人上周和妈妈去看了海', 'category': '事件', 'kind': 'event', 'evidence': 'observed', 'confidence': 0.9}]}])
    result = maintenance.run('daily', now=now, role='qq', gateway_factory=lambda role: gateway)
    check('new message triggers one extraction call', result['summary'].get('model_calls') == 1, result['summary'])
    check('only the new unqueued user message is sent to the model', gateway.calls == ['我上周和妈妈去看了海'], gateway.calls)
    with closing(sqlite3.connect(root / 'data/qq/memory.db')) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM memories WHERE text='主人上周和妈妈去看了海'").fetchone()
    expected = [r[0] for r in sqlite3.connect(root / 'data/qq/qq.db').execute("SELECT channel_message_id FROM messages WHERE text='我上周和妈妈去看了海' AND direction='inbound'")][0]
    check('learned fact keeps source reference', row is not None and row['source'] == 'qq:' + expected, dict(row) if row else None)
    check('learned event is active in owner scope', row is not None and row['scope'] == 'owner' and row['status'] == 'active',
          dict(row) if row else None)
    again = maintenance.run('daily', now=now, role='qq', gateway_factory=lambda role: gateway)
    check('same window is not run twice', again['status'] == 'already-done' and not gateway.replies, again['status'])
    later = at(26, 5, 0)
    empty = maintenance.run('daily', now=later, role='qq', gateway_factory=lambda role: gateway)
    check('no new content -> no model call', empty['status'] == 'empty' and empty['summary'].get('model_calls') == 0, empty)
    check('nothing left in the fake gateway', len(gateway.calls) == 1, gateway.calls)


def test_missed_window_catch_up(edge):
    # Isolated root so the scenario does not depend on earlier test state.
    root = edge
    now = at(24, 6, 0)       # 04:00 was missed while "offline"
    qq_db(root, [])
    fill_windows(root, at(23, 5, 0))             # regular runs until 2026-10-23
    mm.MemoryMaintenance(root)
    last_run = mm.due_windows(at(23, 5, 0))['daily']
    mm._State(root / 'data/qq/memory.db').set('cursor_initialized_at', last_run)
    qq_db(root, [('2026-10-23 12:00:00', 'private:20001', '20001', '我家的猫叫豆豆', 'inbound'),
                 ('2026-10-24 04:30:00', 'private:20001', '20001', '今天出门忘带伞了', 'inbound')])
    gateway = FakeGateway([{'memories': []}, {'memories': []}])
    try:
        result = mm.MemoryMaintenance(root).run('daily', now=now, role='qq', gateway_factory=lambda role: gateway)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    caught = result.get('caught_up') or {}
    check('missed window caught up once with its new messages', caught.get('new_messages') == 2
          and caught.get('catch_up_from') == '2026-10-22 04:00', {k: caught.get(k) for k in ('new_messages', 'catch_up_from')})
    check('catch-up learned the messages that arrived after the last run', len(gateway.calls) == 2, gateway.calls)
    # Role 'research' has no API key configured in the test root: its window
    # stays pending without any model request and is retried hourly at most.
    wait = make_root()
    try:
        qq_db(wait, [])
        fill_windows(wait, at(23, 5, 0), roles=('research',))
        mm.MemoryMaintenance(wait)
        qq_db(wait, [('2026-10-23 12:00:00', 'private:20001', '20001', '今天也有新消息', 'inbound')])
        pending = mm.MemoryMaintenance(wait).tick(now)
        later = mm.MemoryMaintenance(wait).tick(at(24, 8, 0))
    finally:
        shutil.rmtree(wait, ignore_errors=True)
    check('missing model keeps the research window pending, not lost',
          any(item['role'] == 'research' and item['status'] == 'pending' for item in pending['ran']), pending['ran'])
    check('pending window is not retried at once',
          later.get('deferred') == ['research:daily'] or not any(i['role'] == 'research' for i in later['ran']), later)


def test_weekly(root):
    now = at(25, 5, 0)
    qq_db(root, [])
    fill_windows(root, now, roles=('qq',))
    store = MemoryStore(root, 'qq')
    old = time.time() - 200 * 86400
    a = store.save('主人以前常去的那家面馆在北门', scope='owner', source='qq:7001', source_text='北门那家面馆', observed=old)
    b = store.save('主人以前常去的那家面馆在北门了！', scope='owner', source='qq:7002', source_text='北门面馆',
                   observed=old - 3600)
    manual = store.save('主人不吃香菜', scope='owner', source='manual', evidence='manual')
    expired = store.save('主人的临时停车位是三号', scope='owner', source='qq:7003', observed=time.time(), expires=time.time() - 10)
    fresh = store.save('主人最近在学日语', scope='owner', source='qq:7004', observed=time.time())
    store.save('主人去年出差时临时住在南城', scope='owner', source='qq:7005', observed=old)
    gateway = FakeGateway([])
    summary = mm.MemoryMaintenance(root).run('weekly', now=now, role='qq', gateway_factory=lambda role: gateway)['summary']
    with closing(sqlite3.connect(root / 'data/qq/memory.db')) as db:
        db.row_factory = sqlite3.Row
        rows = {r['id']: dict(r) for r in db.execute('SELECT * FROM memories')}
    check('weekly makes no model call', not gateway.calls and summary.get('model_calls', 0) == 0, gateway.calls)
    check('weekly keeps exactly one duplicate active', sum(1 for key in (a['id'], b['id']) if rows[key]['status'] == 'active') == 1,
          {k: rows[k]['status'] for k in (a['id'], b['id'])})
    check('dedup reported', len(summary['collapsed']) == 1, summary.get('collapsed'))
    check('expired fact deactivated to candidate', rows[expired['id']]['status'] == 'candidate', rows[expired['id']]['status'])
    check('decayed fact deactivated via lifecycle', any(item['reason'] in ('decayed', 'decay') for item in summary['deactivated']),
          summary.get('deactivated'))
    check('upstream lifecycle used', summary['upstream'] is True, summary.get('upstream'))
    check('manual owner fact untouched', rows[manual['id']]['status'] == 'active', rows[manual['id']]['status'])
    check('fresh fact kept', rows[fresh['id']]['status'] == 'active', rows[fresh['id']]['status'])
    check('nothing deleted', len(rows) >= 5 and summary.get('deleted') == 0, len(rows))
    with closing(sqlite3.connect(root / 'data/qq/memory.db')) as db:
        sources = [r[0] for r in db.execute('SELECT source FROM memories WHERE id IN (?,?)', (a['id'], b['id']))]
    check('dedup preserves source', sorted(sources) == ['qq:7001', 'qq:7002'], sources)
    confirm = mm.MemoryMaintenance(root).confirm(b['id'], role='qq')
    with closing(sqlite3.connect(root / 'data/qq/memory.db')) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute('SELECT * FROM memories WHERE id=?', (b['id'],)).fetchone())
        excluded = db.execute("SELECT * FROM exclusions WHERE source=?", ('maint:confirmed:' + b['id'],)).fetchone()
        history = db.execute('SELECT operation,source FROM memory_history WHERE memory_id=? ORDER BY revision', (b['id'],)).fetchall()
    check('owner confirmation makes the fact active manual', confirm['ok'] and row['status'] == 'active' and row['evidence'] == 'manual', row)
    check('confirmation recorded in history with the original source',
          [h['operation'] for h in history] == ['original', 'update'] and history[1]['source'] == 'qq:7002', [dict(h) for h in history])
    check('confirmed fact excluded from later cleanup', excluded is not None)


def test_manual_last_window():
    """A manual run must never consume a newer due window; catch-up only once."""
    root = make_root()
    qq_db(root, [])
    maintenance = mm.MemoryMaintenance(root)
    stale = mm.to_timestamp(at(10, 5, 0))
    result = maintenance.run('weekly', now=stale, role='research')
    now = at(25, 5, 0)
    due = maintenance.due(now)
    check('old manual window does not block the current one', ('research', 'weekly') in {(r, k) for r, k in due},
          (mm._iso(stale), result['status'], due))
    # Missed by several weeks: caught up once, then the current window runs.
    result = maintenance.run('weekly', now=now, role='research')
    catch_up = result.get('caught_up') or {}
    check('missed weekly window caught up once', catch_up.get('catch_up_from') != '' and result['status'] in ('empty', 'done'),
          {k: result.get(k) for k in ('status', 'caught_up')})
    maintenance.run('weekly', now=now, role='research')
    check('weekly window itself is recorded', ('research', 'weekly') not in {(r, k) for r, k in maintenance.due(now)}, maintenance.due(now))
    week_later = at(25, 5, 0)
    later = maintenance.due(mm.to_timestamp(week_later) + 8 * 86400)
    check('a later window after downtime is caught up once and is due', ('research', 'weekly') in {(r, k) for r, k in later}, later)


# ---------------------------------------------------------------------------
# 4. optional real model probe
# ---------------------------------------------------------------------------

def test_semantic_probe(real_root):
    """Probe the real venv worker; its model cache lives in the real root."""
    from memory_engine import MemoryEngine
    temp_root = make_root()
    (temp_root / 'harness' / 'memory-embeddings.json').write_text(json.dumps({
        'enabled': True, 'model': 'BAAI/bge-small-zh-v1.5',
        'venv': str((Path(real_root) / 'vendor' / 'runtime' / 'memory-python').resolve()),
        'cache_dir': str((Path(real_root) / 'data' / 'shared' / 'models').resolve())}), encoding='utf-8')
    engine = MemoryEngine(temp_root, 'qq')
    store = MemoryStore(temp_root, 'qq')
    for text in ('主人喜欢喝柠檬水', '主人养了一只叫豆豆的猫'):
        store.save(text, scope='owner', source='manual', category='事实')
    started = time.time()
    ready = engine.warm(wait=0)
    check('model worker started', ready, engine.embedding_status()['detail'])
    status = engine.embedding_status()
    check('model ready with real dimension', status['ready'] and status['dim'] > 0, status)
    backfill = engine.backfill()
    check('backfill stored vectors in SQLite', backfill.get('count', 0) == 2, backfill)
    recalled = engine.recall(store, '小猫', scope='owner', with_status=True)
    check('hybrid recall runs with the model', recalled['semantic']['status'] == 'fused', recalled['semantic'])
    top = recalled['results'][0] if recalled['results'] else {}
    check('semantic hit is the cat fact', '豆豆' in top.get('text', ''), [r['text'] for r in recalled['results'][:2]])
    engine._worker_status = None
    print('semantic probe seconds: %.1f' % (time.time() - started))
    shutil.rmtree(temp_root, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--semantic', action='store_true', help='also probe the real local model worker')
    parser.add_argument('--real-root', default=str(Path(__file__).resolve().parent.parent),
                        help='Fish root whose vendored venv/model cache is used by --semantic')
    args = parser.parse_args()
    for scenario in (test_owner_facts, test_semantic_honesty, test_fusion, test_due_and_empty,
                     test_new_messages_with_fake_gateway, test_missed_window_catch_up, test_weekly):
        root = make_root()
        try:
            scenario(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
    test_schedule()
    test_manual_last_window()
    if args.semantic:
        test_semantic_probe(Path(args.real_root))
    failed = [name for name, ok, _ in RESULTS if not ok]
    print('\n%d checks, %d failed' % (len(RESULTS), len(failed)))
    for name in failed:
        print('FAILED:', name)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
