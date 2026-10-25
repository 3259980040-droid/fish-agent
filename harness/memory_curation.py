"""One repeatable memory curation program for every entry point.

The daily automatic window, the Fish tool and the client's "整理" button all
submit here. A submission returns a real run reference immediately; a single
background worker (outside the main thinking lock) claims it and executes the
same ordered steps: read the new/specified range, normalize and deduplicate
sources, extract facts/events with Fish's configured model, save memories and
revisions, tidy duplicates/expiry, write the daily digest, curate expressions,
then reconcile FTS5 and the existing vector indexes.

A failed step is recorded with its range and reason; it never advances the
daily window or pretends unfinished material was processed. Re-running the same
range is allowed and unchanged material is skipped by the learning-job state.
"""
from __future__ import annotations
from locations import project_path

import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage import database, role_dir

TZ = timezone(timedelta(hours=8))
DAY_HOUR = 4
MAX_MODEL_CALLS = 60  # Existing per-batch call budget, never a whole-run completion rule.
MAX_FAILURE_RETRIES = 2
LEASE_SECONDS = 1800
TIDY_HALF_LIFE_HOURS = 24 * 7
TIDY_DECAY_THRESHOLD = 0.1
TERMINAL = ('done', 'skipped')
STEPS = ('range', 'learn', 'tidy', 'digest', 'emoji', 'index')
STATUS_LABELS = {'queued': '已受理', 'running': '整理中', 'done': '已完成',
                 'partial': '部分完成', 'failed': '失败'}

RUNS = '''CREATE TABLE IF NOT EXISTS memory_curate_runs(
    id TEXT PRIMARY KEY, role TEXT NOT NULL, range_key TEXT NOT NULL,
    requested_by TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
    range_start REAL NOT NULL, range_end REAL NOT NULL, day TEXT NOT NULL,
    request TEXT NOT NULL DEFAULT '{}', progress TEXT NOT NULL DEFAULT '[]',
    result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, started REAL, finished REAL)'''
RUNS_INDEX = ('CREATE INDEX IF NOT EXISTS memory_curate_runs_status '
              'ON memory_curate_runs(status,created)')
STATE = 'CREATE TABLE IF NOT EXISTS memory_curate_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)'

_WORKERS, _EVENTS, _GATEWAYS, _CANCELLED = {}, {}, {}, {}
_LOCK = threading.RLock()


# ------------------------------------------------------------------ time/range
def _local(ts):
    return datetime.fromtimestamp(float(ts), TZ)


def _day(day=None):
    if not day:
        return _local(time.time()).strftime('%Y-%m-%d')
    text = str(day).strip()
    try:
        time.strptime(text, '%Y-%m-%d')
    except ValueError:
        raise ValueError('日期需为 YYYY-MM-DD') from None
    return text


def _day_bounds(day):
    start = datetime.strptime(_day(day), '%Y-%m-%d').replace(tzinfo=TZ)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _daily_window(now):
    current = _local(now)
    window = current.replace(hour=DAY_HOUR, minute=0, second=0, microsecond=0)
    if window > current:
        window -= timedelta(days=1)
    return window.timestamp()


def _epoch(value, default=None):
    if value in (None, ''):
        return default
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if re.fullmatch(r'\d+(\.\d+)?', text):
        return float(text)
    parsed = datetime.fromisoformat(text)
    return (parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed).timestamp()


def _iso(ts):
    return _local(ts).strftime('%Y-%m-%d %H:%M')


# ------------------------------------------------------------------ persistence
def _path(root, role='qq'):
    return role_dir(Path(root), role) / 'memory.db'


def _prepare(root, role='qq'):
    path = _path(root, role)
    with database(path) as db:
        db.execute(RUNS)
        db.execute(RUNS_INDEX)
        db.execute(STATE)
    return path


def _record(row):
    item = dict(row)
    for field, default in (('request', {}), ('progress', []), ('result', {})):
        try:
            item[field] = json.loads(item.get(field) or ('[]' if field == 'progress' else '{}'))
        except (TypeError, ValueError):
            item[field] = default
    item['reference'] = item['id']
    item['range'] = {'start': float(item.get('range_start') or 0),
                     'end': float(item.get('range_end') or 0),
                     'start_text': _iso(item.get('range_start') or 0),
                     'end_text': _iso(item.get('range_end') or 0), 'day': item.get('day')}
    item['status_label'] = STATUS_LABELS.get(str(item.get('status')), str(item.get('status')))
    result = item.get('result') or {}
    if isinstance(result, dict):
        item.setdefault('day', item.get('day'))
        item['cleaned'] = result.get('tidy') or {}
        item['digest'] = result.get('digest') or {}
        item['emoji'] = result.get('emoji') or {}
    return item


def _load(root, run_id):
    for role in ('qq', 'research'):
        path = _path(root, role)
        if not path.is_file():
            continue
        with database(path) as db:
            db.execute(RUNS)
            row = db.execute('SELECT * FROM memory_curate_runs WHERE id=?', (str(run_id),)).fetchone()
        if row is not None:
            return _record(row)
    return None


def _recent(root, limit=10, role='qq'):
    path = _prepare(root, role)
    with database(path) as db:
        rows = [dict(row) for row in db.execute(
            'SELECT * FROM memory_curate_runs ORDER BY created DESC LIMIT ?', (max(1, int(limit)),))]
    return [_record(row) for row in rows]


def _write_progress(root, role, run_id, progress, stage):
    with database(_path(root, role)) as db:
        db.execute('UPDATE memory_curate_runs SET progress=?,stage=? WHERE id=?',
                   (json.dumps(progress, ensure_ascii=False), str(stage), run_id))


def _finish(root, role, run_id, status, progress, result, error=''):
    now = time.time()
    with database(_path(root, role)) as db:
        db.execute('UPDATE memory_curate_runs SET status=?,stage=?,progress=?,result=?,error=?,finished=? WHERE id=?',
                   (status, status, json.dumps(progress, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False), str(error)[:500], now, run_id))
        window = result.get('range', {}).get('window') if isinstance(result, dict) else None
        if window:
            key = ('daily_done:' if status == 'done' else 'daily_attempt:') + str(int(float(window)))
            db.execute('INSERT OR REPLACE INTO memory_curate_state VALUES(?,?)',
                       (key, json.dumps({'run': run_id, 'status': status, 'at': now}, ensure_ascii=False)))


# ------------------------------------------------------------------ submission
def _last_end(root, role='qq'):
    path = _path(root, role)
    if not path.is_file():
        return 0.0
    with database(path) as db:
        db.execute(RUNS)
        row = db.execute("SELECT MAX(range_end) FROM memory_curate_runs WHERE status='done'").fetchone()
    return float(row[0] or 0) if row else 0.0


def _revision(root, role='qq'):
    """Changing material keeps a different revision; unchanged range can be reused."""
    from memory_store import MemoryStore
    store = MemoryStore(Path(root), role)
    with database(store.path) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()
        memory_revision = int(row[0] or 0) if row else 0
    source_revision = 0
    for path in (project_path(Path(root), 'data/qq/qq.db'), project_path(Path(root), 'data/qq/desktop-chat.db')):
        if not path.is_file():
            continue
        try:
            with database(path) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
                    count, newest = db.execute(
                        'SELECT COUNT(*), COALESCE(MAX(id),0) FROM messages').fetchone()
                    source_revision += int(count) * 1000000 + int(newest)
        except sqlite3.Error:
            continue
    return str(memory_revision) + ':' + str(source_revision)


def _source_limits(root, role):
    """Freeze original-store high water marks; the run never follows new arrivals."""
    limits = {}
    for name, path, table, key in (
            ('qq.db', project_path(root, 'data/qq/qq.db'), 'messages', 'id'),
            ('desktop-chat.db', project_path(root, 'data', role, 'desktop-chat.db'), 'messages', 'id'),
            ('learning_jobs', _path(root, role), 'learning_jobs', 'rowid')):
        limits[name] = 0
        if path.is_file():
            with database(path) as db:
                if db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone():
                    limits[name] = int(db.execute('SELECT COALESCE(MAX(' + key + '),0) FROM ' + table).fetchone()[0])
    return limits


def submit(root, *, day=None, start=None, end=None, force=False, requested_by='tool',
           role='qq', gateway=None, autostart=True, steps=None, range_key=None, window=None):
    """Accept one run and return its real reference without running the model.

    The exact same range and revision already queued/running returns that same
    reference. A wider range is a new queued run; it is not silently folded into
    or dropped from the active one.
    """
    from locations import instance_context
    root = instance_context(root).root
    role = 'research' if str(role) == 'research' else 'qq'
    now = time.time()
    path = _prepare(root, role)
    if not force:
        with database(path) as db:
            candidates = db.execute("SELECT * FROM memory_curate_runs WHERE role=? "
                                    "AND status IN ('queued','running','partial','failed') ORDER BY created",
                                    (role,)).fetchall()
            for candidate in candidates:
                same = (start is None and not day) or (
                    start is not None and end is not None
                    and float(candidate['range_start']) == _epoch(start)
                    and float(candidate['range_end']) == _epoch(end))
                if not same:
                    continue
                previous = _record(candidate)
                if previous['status'] in ('partial', 'failed'):
                    progress = previous['progress'] + [{'name': 'resume', 'state': 'done', 'at': now,
                                                       'previous_status': previous['status'],
                                                       'previous_error': previous['error']}]
                    db.execute("UPDATE memory_curate_runs SET status='queued',stage='accepted',finished=NULL,"
                               "error='',progress=? WHERE id=?", (json.dumps(progress, ensure_ascii=False), candidate['id']))
                    previous.update(status='queued', stage='accepted', finished=None, error='', progress=progress)
                previous.update(reused=True, note='接续同一固定范围；已完成来源沿原学习状态跳过')
                if gateway is not None:
                    _GATEWAYS[previous['id']] = gateway
                if autostart:
                    ensure_worker(root)
                return previous
    limits = _source_limits(root, role)
    if start is not None:
        first = _epoch(start, None)
        last = _epoch(end, now)
        if first is None:
            raise ValueError('指定范围需要开始时间')
        if last <= first:
            last = first + 1
    elif day:
        first, last = _day_bounds(_day(day))
    else:
        last = _epoch(end, now)
        first = _next_start(root, role, last, limits)
    day_value = _day(day) if day else _local(last).strftime('%Y-%m-%d')
    chosen_steps = [step for step in (steps or STEPS) if step in STEPS]
    if 'learn' in chosen_steps and 'range' not in chosen_steps:
        chosen_steps.insert(0, 'range')
    if not chosen_steps:
        raise ValueError('整理步骤为空')
    key = str(range_key or '') or (role + ':' + str(int(first)) + ':' + str(int(last)) +
                                   ':' + _revision(root, role))
    request = {'day': day_value, 'start': float(first), 'end': float(last), 'force': bool(force),
               'steps': chosen_steps, 'requested_by': requested_by,
               'source_limits': limits}
    if window:
        request['window'] = float(window)
    path = _prepare(root, role)
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM memory_curate_runs WHERE role=? AND range_key=? "
                         "AND status IN ('queued','running') ORDER BY created DESC LIMIT 1",
                         (role, key)).fetchone()
        if row is not None:
            result = _record(row)
            result.update(reused=True,
                          note='相同范围/修订正在整理，返回同一执行引用')
            return result
        run_id = 'curate:' + uuid.uuid4().hex[:12]
        db.execute('INSERT INTO memory_curate_runs(id,role,range_key,requested_by,status,stage,'
                   'range_start,range_end,day,request,progress,result,created) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (run_id, role, key, str(requested_by), 'queued', 'accepted',
                    float(first), float(last), day_value,
                    json.dumps(request, ensure_ascii=False), '[]', '{}', now))
        row = db.execute('SELECT * FROM memory_curate_runs WHERE id=?', (run_id,)).fetchone()
    if gateway is not None:
        with _LOCK:
            _GATEWAYS[run_id] = gateway
    if autostart:
        ensure_worker(root)
        event = _EVENTS.get(str(root.resolve()))
        if event is not None:
            event.set()
    record = _record(row)
    record['note'] = ('已受理，后台整理中；用同一引用查询阶段和结果，不等待模型'
                      if autostart else '已受理；未启动后台线程')
    return record


def _next_start(root, role, end, limits):
    """An end timestamp alone cannot prove every earlier source was processed."""
    from memory_store import MemoryStore
    store = MemoryStore(root, role)
    with database(store.path) as db:
        finished = {row[0] for row in db.execute("SELECT source FROM learning_jobs WHERE status IN ('done','skipped')")}
        finished.update(row[0] for row in db.execute('SELECT source FROM exclusions'))
    rows, _, _ = _collect(root, role, 0, end, limits)
    pending = [row['ts'] for row in rows
               if not any(reference in finished for reference in [row['source'], *row.get('source_aliases', [])])]
    return min(pending) if pending else min(_last_end(root, role) or
                                           _day_bounds(_local(end).strftime('%Y-%m-%d'))[0], end)


def status(root, run_id=None, *, limit=10, role='qq'):
    """Read one run or the recent runs; progress and the real result are separate."""
    root = Path(root)
    if run_id:
        record = _load(root, run_id)
        if record is None:
            raise ValueError('整理执行不存在或已删除')
        return {'run': record, 'latest': record, 'runs': [record],
                'note': 'progress 是阶段记录；result 只包含实际完成和实际失败的内容'}
    runs = _recent(root, limit, role)
    latest = next((item for item in runs if item.get('status') in ('done', 'partial', 'failed')), None)
    return {'run': runs[0] if runs else None, 'latest': latest, 'runs': runs,
            'note': '每日自动、工具和客户端按钮共用这些执行记录'}


def latest(root):
    """Newest settled run, flattened enough for the existing memory overview."""
    record = status(root, limit=20).get('latest')
    return record


# ------------------------------------------------------------------ worker
def _claim(root):
    path = _prepare(root, 'qq')
    now = time.time()
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        stale = db.execute("SELECT id,role FROM memory_curate_runs WHERE status='running' "
                           'AND COALESCE(started,0)<?', (now - LEASE_SECONDS,)).fetchall()
        for item in stale:
            db.execute("UPDATE memory_curate_runs SET status='failed',stage='failed',error=?,finished=? "
                       'WHERE id=?', ('上次整理在执行中退出，未标记完成；可重新启动', now, item['id']))
        row = db.execute("SELECT * FROM memory_curate_runs WHERE status='queued' "
                         'ORDER BY created LIMIT 1').fetchone()
        if row is None:
            return None
        db.execute("UPDATE memory_curate_runs SET status='running',stage='running',started=? WHERE id=?",
                   (now, row['id']))
        row = db.execute('SELECT * FROM memory_curate_runs WHERE id=?', (row['id'],)).fetchone()
    return _record(row)


def run_pending(root, *, gateway=None, limit=10, cancelled=None):
    """Claim and execute up to ``limit`` queued runs (the worker's one function)."""
    executed = []
    for _ in range(max(1, int(limit))):
        if cancelled is not None and cancelled.is_set():
            break
        row = _claim(Path(root))
        if row is None:
            break
        executed.append(execute(root, row['id'], gateway=gateway, cancelled=cancelled))
    return executed


def ensure_worker(root, *, stop=None):
    """One lazy background worker per data root; it never runs inside a turn."""
    from locations import instance_context
    root = instance_context(root).root
    key = str(root)
    with _LOCK:
        if stop is not None:
            _CANCELLED[key] = stop
        thread = _WORKERS.get(key)
        if thread is not None and thread.is_alive():
            return thread
        _EVENTS.setdefault(key, threading.Event())
        if stop is None and (key not in _CANCELLED or _CANCELLED[key].is_set()):
            _CANCELLED[key] = threading.Event()

        def loop():
            while not _CANCELLED[key].is_set():
                try:
                    run_pending(root, cancelled=_CANCELLED[key])
                except Exception:  # noqa: BLE001 - every run keeps its own failure
                    pass
                _EVENTS[key].wait(5)
                _EVENTS[key].clear()

        thread = threading.Thread(target=loop, daemon=True, name='fish-memory-curation')
        _WORKERS[key] = thread
        thread.start()
        return thread


# ------------------------------------------------------------------ one program
def _count_rows(rows):
    counts = {}
    for row in rows:
        key = str(row.get('origin') or 'unknown')
        counts[key] = counts.get(key, 0) + 1
    return counts


def _collect(root, role, start, end, source_limits=None):
    """Read new/specified inbound material from the real owning stores."""
    from memory_maintenance import read_desktop_messages, read_qq_messages
    rows = []
    limits = source_limits if source_limits is not None else _source_limits(root, role)
    rows.extend(read_qq_messages(Path(root), role, float(start), 0, end=end,
                                through_id=limits.get('qq.db', 0), limit=None, strict=True))
    rows.extend(read_desktop_messages(Path(root), role, float(start), 0, end=end,
                                     through_id=limits.get('desktop-chat.db', 0), limit=None, strict=True))
    # The live group learner already owns its provenance and gate. Consume its
    # existing queue as well; do not turn group text into owner-private evidence.
    if limits.get('learning_jobs'):
        known = {reference for row in rows for reference in [row['source'], *row.get('source_aliases', [])]}
        with database(_path(root, role)) as db:
            queued = db.execute('SELECT rowid AS seq,* FROM learning_jobs WHERE rowid<=? '
                                'AND created>=? AND created<=? ORDER BY rowid',
                                (limits['learning_jobs'], float(start), float(end))).fetchall()
        for job in queued:
            if job['source'] in known or str(job['scope']).startswith('space:') or ':test:' in str(job['scope']):
                continue
            rows.append({'origin': 'learning_jobs', 'row_id': job['seq'], 'ts': job['created'],
                         'source': job['source'], 'scope': job['scope'], 'subject': job['subject'],
                         'text': job['text'], 'source_aliases': []})
    rows = [row for row in rows if float(start) <= float(row['ts']) <= float(end)]
    rows.sort(key=lambda item: (float(item['ts']), str(item.get('origin') or ''),
                                int(item.get('row_id') or 0)))
    total = len(rows)
    return rows, total, False


def _clean(text):
    return re.sub(r'\s+', '', str(text or '')).lower()


def _groups(rows):
    """Exact duplicate texts in one place become one job with source aliases."""
    groups = {}
    for row in rows:
        text = ' '.join(str(row.get('text') or '').split())
        if not text:
            continue
        key = (str(row.get('scope') or ''), str(row.get('subject') or ''), _clean(text))
        group = groups.setdefault(key, {'text': text, 'scope': str(row.get('scope') or ''),
                                        'subject': str(row.get('subject') or ''), 'sources': []})
        group['sources'].append(row)
    return list(groups.values())


def _gateway(root, role):
    from model_gateway import ModelGateway
    try:
        gateway = ModelGateway(Path(root), role)
    except (ValueError, KeyError, OSError) as exc:
        raise ValueError('模型未配置，整理未调用模型：' + type(exc).__name__ + ': ' + str(exc)[:160]) from None
    if not str(getattr(gateway, 'key', '') or ''):
        raise ValueError('模型未配置，整理未调用模型；材料仍保留待下次')
    return gateway


def _learn(root, role, rows, *, gateway, force, on_batch=None, cancelled=None):
    from memory_store import MemoryStore
    store = MemoryStore(Path(root), role)
    with database(store.path) as db:
        jobs = {row['source']: dict(row) for row in db.execute(
            'SELECT source,scope,status,attempts FROM learning_jobs')}
        excluded = {row[0] for row in db.execute('SELECT source FROM exclusions')}
    pending, skipped = [], 0
    for group in _groups(rows):
        references = [row['source'] for row in group['sources']]
        references.extend(alias for row in group['sources'] for alias in row.get('source_aliases') or [])
        if any(reference in excluded for reference in references):
            skipped += 1
            continue
        primary = group['sources'][0]['source']
        job = next((jobs[reference] for reference in references if reference in jobs), None)
        if job is not None and job['status'] in TERMINAL and not force:
            skipped += 1
            continue
        if job is not None:
            primary = job['source']
        group['primary'] = primary
        exhausted = bool(job is not None and job['status'] == 'failed'
                         and int(job.get('attempts') or 0) >= MAX_FAILURE_RETRIES and not force)
        group['blocked'] = '原学习任务已达失败重试次数；保留未完成，需明确重做' if exhausted else ''
        aliases = [row['source'] for row in group['sources'][1:]]
        aliases.extend(alias for row in group['sources'] for alias in row.get('source_aliases') or [])
        with database(store.path) as db:
            if job is not None and job['status'] not in TERMINAL:
                db.execute('UPDATE learning_jobs SET text=? WHERE source=?', (group['text'], primary))
            if force and job is not None:
                db.execute("UPDATE learning_jobs SET status='pending',error='',started=0,attempts=0 "
                           'WHERE source=?', (primary,))
            elif job is not None and job['status'] == 'failed' and not exhausted:
                db.execute("UPDATE learning_jobs SET status='pending',error='',started=0 WHERE source=?",
                           (primary,))
        store.enqueue(primary, group['text'], group['scope'], subject=group['subject'],
                      source_aliases=aliases)
        pending.append(group)
    if not pending:
        return {'state': 'done', 'new_messages': 0, 'saved': [], 'saved_count': 0,
                'failures': [], 'failure_count': 0, 'model_calls': 0,
                'skipped_existing': skipped, 'deferred': 0, 'remaining': 0,
                'completed': skipped, 'total': skipped, 'batch_count': 0, 'batches': [],
                'note': '没有新的可学习内容，未调用模型'}
    session = gateway
    saved, failures, calls, deferred, processed = [], [], 0, 0, 0
    batches, batch_calls, batch_start = [], 0, 0

    def finish_batch():
        nonlocal batch_calls, batch_start
        batches.append({'number': len(batches) + 1, 'processed': processed - batch_start,
                        'model_calls': batch_calls, 'remaining': len(pending) - processed + len(failures)})
        batch_calls, batch_start = 0, processed
        if on_batch is not None:
            on_batch({'batch_count': len(batches), 'batches': list(batches),
                      'completed': skipped + processed - len(failures), 'total': skipped + len(pending),
                      'remaining': len(pending) - processed + len(failures),
                      'model_calls': calls, 'saved_count': len(saved)})

    for group in pending:
        if batch_calls >= MAX_MODEL_CALLS:
            finish_batch()
        primary = group['primary']
        processed += 1
        try:
            _check_running(root, role, cancelled)
            if group['blocked']:
                raise InterruptedError(group['blocked'])
            if session is None:
                session = _gateway(root, role)
            outcome = store.extract(session, primary)
        except Exception as exc:  # noqa: BLE001 - one bad source keeps its own failure
            failures.append({'source': primary, 'scope': group['scope'],
                             'reason': type(exc).__name__ + ': ' + str(exc)[:200]})
            # No next source/model request after an actual interruption.
            break
        calls += int(outcome.get('model_calls') or 0)
        batch_calls += int(outcome.get('model_calls') or 0)
        if outcome.get('status') == 'done':
            for item in outcome.get('saved') or []:
                try:
                    row = store.get(item['id'], scope=None)
                except ValueError:
                    continue
                saved.append({'id': row['id'], 'text': row['text'], 'category': row['category'],
                              'kind': row['kind'], 'status': row['status'], 'scope': row['scope'],
                              'source': row['source']})
        elif outcome.get('status') == 'failed':
            failures.append({'source': primary, 'scope': group['scope'],
                             'reason': outcome.get('error') or '模型提炼失败'})
            break
        elif outcome.get('status') not in TERMINAL:
            failures.append({'source': primary, 'scope': group['scope'],
                             'reason': outcome.get('reason') or outcome.get('error') or '来源未完成处理'})
            break
    if processed > batch_start:
        finish_batch()
    deferred = len(pending) - processed + len(failures)
    return {'state': 'partial' if failures or deferred else 'done', 'new_messages': len(pending),
            'saved': saved[:60], 'saved_count': len(saved), 'failures': failures[:60],
            'failure_count': len(failures), 'model_calls': calls, 'skipped_existing': skipped,
            'deferred': deferred, 'remaining': deferred, 'completed': skipped + processed - len(failures),
            'total': skipped + len(pending),
            'batch_count': len(batches), 'batches': batches,
            'note': ('保存 %d 条，失败 %d 条' % (len(saved), len(failures))) if failures
                    else ('保存 %d 条' % len(saved))}


def _check_running(root, role, cancelled=None):
    from governance import Governance
    from memory_store import MemoryStore
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError('后台正在停止；已完成和剩余材料保留')
    if Governance(root, role).config()['paused']:
        raise InterruptedError('主人已暂停；已完成和剩余材料保留')
    if not MemoryStore(root, role).options()['auto_learning']:
        raise InterruptedError('自动学习已关闭；未处理材料保留')


def _auto(row):
    return (row['source'] != 'manual' and row['evidence'] not in ('manual', 'owner_quote')
            and not str(row['scope']).startswith('chat:group:') and not row.get('conflict'))


def tidy_memories(root, role='qq', now=None):
    """Exact duplicate collapse and expiry/decay of automatic facts; no deletion."""
    from memory_maintenance import _State, _clusters
    from memory_store import MemoryStore
    import memory_source
    now = time.time() if now is None else float(now)
    store = MemoryStore(Path(root), role)          # schema is created idempotently
    state = _State(role_dir(Path(root), role) / 'memory.db')
    lifecycle = state.get('lifecycle') or {}
    conflict = state.get('conflicts') or {}
    try:
        upstream = bool(memory_source.source_status(Path(root))['lifecycle']['loaded'])
    except Exception:  # noqa: BLE001 - upstream policy is reported, not fatal
        upstream = False
    with database(store.path) as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM memories')]
    for row in rows:
        row['conflict'] = row['id'] in conflict
    auto = [row for row in rows if _auto(row)]
    summary = {'evaluated': len(auto), 'skipped_manual': len(rows) - len(auto),
               'collapsed': [], 'deactivated': [], 'kept': 0, 'upstream': upstream,
               'deleted': 0, 'model_calls': 0}
    kept = set()
    for cluster in _clusters(auto):
        if len(cluster['rows']) < 2:
            continue
        keep = max(cluster['rows'], key=lambda row: (float(row['created'] or 0), float(row['updated'] or 0)))
        kept.add(keep['id'])
        for row in cluster['rows']:
            if row['id'] == keep['id'] or row['status'] != 'active':
                continue
            _deactivate(store, state, row, 'duplicate', now, upstream)
            summary['collapsed'].append({'id': row['id'], 'duplicate_of': keep['id'],
                                         'source': row['source']})
    for row in auto:
        if row['status'] != 'active' or lifecycle.get(row['id'], {}).get('status') == 'inactive':
            continue
        if row['id'] in kept and not (row['expires'] and row['expires'] <= now):
            summary['kept'] += 1
            continue
        reason = None
        if row['expires'] and row['expires'] <= now:
            reason = 'expired'
        else:
            anchor = float(row['observed'] or row['created'])
            strength = min(1.0, max(0.0, float(row['confidence']) or 1.0))
            try:
                decision = memory_source.evaluate_due(
                    Path(root), strength=strength, anchor=anchor, now=now,
                    half_life_hours=TIDY_HALF_LIFE_HOURS, threshold=TIDY_DECAY_THRESHOLD)
            except Exception:  # noqa: BLE001 - decay is optional to the tidy step
                summary['kept'] += 1
                continue
            lifecycle[row['id']] = {'status': 'inactive' if decision['inactive'] else 'active',
                                    'reason': decision['reason'], 'checked_at': now,
                                    'next_at': decision['next_at'], 'anchor': anchor,
                                    'upstream': decision['upstream']}
            if decision['inactive']:
                reason = str(decision['reason'] or 'decayed')
        if reason:
            _deactivate(store, state, row, reason, now, upstream)
            summary['deactivated'].append({'id': row['id'], 'reason': reason})
        else:
            summary['kept'] += 1
    state.set('lifecycle', lifecycle)
    summary['note'] = ('只整理自动学习的事实：重复的合并成候选、过期的停用；'
                       '没有删除任何记忆记录，人工保存或确认的事实未改动')
    return summary


def _deactivate(store, state, row, reason, now, upstream):
    with database(store.path) as db:
        if row['status'] == 'active':
            db.execute("UPDATE memories SET status='candidate',updated=? WHERE id=? AND status='active'",
                       (now, row['id']))
            db.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")
    lifecycle = state.get('lifecycle') or {}
    lifecycle[row['id']] = {'status': 'inactive', 'reason': reason, 'since': now,
                            'upstream': upstream, 'anchor': float(row['observed'] or row['created'])}
    state.set('lifecycle', lifecycle)


def _index(root, role):
    from doc_store import maintain as maintain_documents
    from doc_store import sync_index as sync_documents
    from history_index import sync as sync_messages
    from memory_digest import sync_index as sync_digests
    from memory_engine import MemoryEngine
    from memory_store import MemoryStore
    result = {'messages': sync_messages(Path(root)), 'memories': MemoryStore(Path(root), role).sync_index(),
              'documents': sync_documents(Path(root)), 'digests': sync_digests(Path(root)), 'vector': {}}
    try:
        result['vector']['memories'] = MemoryEngine(Path(root), role).backfill()
    except Exception as exc:  # noqa: BLE001 - vector readiness is reported, not fatal
        result['vector']['memories'] = {'ok': False, 'status': 'error',
                                        'error': type(exc).__name__ + ': ' + str(exc)[:160]}
    try:
        result['vector']['documents'] = maintain_documents(Path(root))
    except Exception as exc:  # noqa: BLE001
        result['vector']['documents'] = {'status': 'error',
                                         'error': type(exc).__name__ + ': ' + str(exc)[:160]}
    return result


def execute(root, run_id, *, gateway=None, gateway_factory=None, cancelled=None):
    """Execute one accepted run to its real result; progress is written per stage."""
    root = Path(root)
    record = _load(root, run_id)
    if record is None:
        raise ValueError('整理执行不存在或已删除')
    if record['status'] not in ('queued', 'running'):
        return record
    role = record['role']
    with database(_path(root, role)) as db:
        db.execute("UPDATE memory_curate_runs SET status='running',started=COALESCE(started,?) WHERE id=?",
                   (time.time(), run_id))
    request = record.get('request') or {}
    if 'source_limits' not in request:
        request['source_limits'] = _source_limits(root, role)
        with database(_path(root, role)) as db:
            db.execute('UPDATE memory_curate_runs SET request=? WHERE id=?',
                       (json.dumps(request, ensure_ascii=False), run_id))
    steps = request.get('steps') or list(STEPS)
    force = bool(request.get('force'))
    progress = list(record.get('progress') or []) or [{'name': 'accepted', 'state': 'done', 'at': record.get('created')}]
    _write_progress(root, role, run_id, progress, 'running')
    runtime = {'rows': [], 'range': {}}
    result, failures = {}, []

    def stage(name, call, optional=False):
        progress.append({'name': name, 'state': 'running', 'at': time.time()})
        _write_progress(root, role, run_id, progress, name)
        try:
            if name in ('digest', 'emoji'):
                _check_running(root, role, cancelled)
            detail = call() or {}
        except Exception as exc:  # noqa: BLE001 - recorded, then the run may fail
            progress[-1] = {'name': name, 'state': 'failed', 'at': time.time(),
                            'error': type(exc).__name__ + ': ' + str(exc)[:300]}
            _write_progress(root, role, run_id, progress, name)
            failures.append({'stage': name, 'reason': progress[-1]['error']})
            if optional:
                return {'state': 'failed', 'error': progress[-1]['error']}
            raise
        progress[-1] = {'name': name, 'state': 'done', 'at': time.time(), **detail}
        _write_progress(root, role, run_id, progress, name)
        return detail

    def read_range():
        rows, total, truncated = _collect(root, role, request['start'], request['end'],
                                         request.get('source_limits'))
        runtime['rows'] = rows
        runtime['range'] = {'start': request['start'], 'end': request['end'],
                            'day': request.get('day'), 'window': request.get('window')}
        return {'messages': len(rows), 'scanned': total, 'truncated': truncated,
                'sources': _count_rows(rows)}
    try:
        if 'range' in steps:
            result['range'] = stage('range', read_range)
        session = None
        if 'learn' in steps:
            def learn():
                nonlocal session
                if not runtime['rows']:
                    return {'new_messages': 0, 'saved': [], 'failures': [], 'model_calls': 0,
                            'skipped_existing': 0, 'note': '本次范围没有可学习的新消息'}
                session = gateway or _GATEWAYS.pop(run_id, None)
                if session is None and gateway_factory is not None:
                    session = gateway_factory(role)
                def batch_progress(detail):
                    progress[-1].update(detail)
                    _write_progress(root, role, run_id, progress, 'learn')
                return _learn(root, role, runtime['rows'], gateway=session, force=force,
                              on_batch=batch_progress, cancelled=cancelled)
            result['learn'] = stage('learn', learn)
            failures.extend({'stage': 'learn', **item} for item in result['learn'].get('failures') or [])
        learning_incomplete = bool((result.get('learn') or {}).get('remaining') or
                                   (result.get('learn') or {}).get('failure_count'))
        if 'tidy' in steps and not learning_incomplete:
            result['tidy'] = stage('tidy', lambda: tidy_memories(root, role))
        if 'digest' in steps:
            if learning_incomplete:
                # A digest written from a partially failed extraction would mark
                # the day finished and hide the missing material from the retry.
                result['digest'] = {'state': 'skipped',
                                    'reason': '学习阶段有失败，未写小结，等同一范围重试'}
            else:
                def digest():
                    from memory_digest import daily
                    day = str(request.get('day') or _local(time.time()).strftime('%Y-%m-%d'))
                    digest_gateway = session
                    if digest_gateway is None:
                        digest_gateway = gateway or _GATEWAYS.pop(run_id, None)
                    record_row = daily(root, day, force=force or bool((result.get('learn') or {}).get('saved_count')),
                                       gateway=digest_gateway, raise_errors=True)
                    text = str(record_row.get('text') or '')
                    calls = 0 if record_row.get('skipped') else int(record_row.get('model_calls') or 0)
                    return {'day': day, 'skipped': bool(record_row.get('skipped')),
                            'model_calls': calls,
                            'chars': len(text), 'error': ''}
                result['digest'] = stage('digest', digest, optional=True)
        if 'emoji' in steps and role == 'qq' and not learning_incomplete:
            def emoji():
                from daily_curate import _emoji
                day = str(request.get('day') or _local(time.time()).strftime('%Y-%m-%d'))
                return _emoji(root, day, gateway=session or gateway, messages_db=_path(root, 'qq').parent / 'qq.db')
            result['emoji'] = stage('emoji', emoji, optional=True)
        if 'index' in steps and not learning_incomplete:
            result['index'] = stage('index', lambda: _index(root, role))
    except Exception as exc:  # noqa: BLE001 - a failed range/learn writes a real failure
        result['range'] = {**runtime.get('range', {}),
                           'start': request.get('start'), 'end': request.get('end'),
                           'day': request.get('day'), 'window': request.get('window')}
        result['range_requested'] = result['range']
        result['failures'] = failures + [{'stage': 'run', 'reason': type(exc).__name__ + ': ' + str(exc)[:300]}]
        result['model_calls'] = int((result.get('learn') or {}).get('model_calls') or 0)
        result['note'] = '整理失败，未标记完成；' + result['failures'][-1]['reason']
        _finish(root, role, run_id, 'failed', progress, result, result['failures'][-1]['reason'])
        return _load(root, run_id)

    learn_result = result.get('learn') or {}
    optional_failed = any(
        isinstance(result.get(name), dict) and result[name].get('state') == 'failed'
        for name in ('tidy', 'digest', 'emoji', 'index'))
    status = 'partial' if failures or optional_failed or learning_incomplete else 'done'
    result.update({'run': run_id,
                   'range': {**(result.get('range') or {}), 'start': request.get('start'),
                             'end': request.get('end'), 'day': request.get('day'),
                             'window': request.get('window')},
                   'new_messages': int(learn_result.get('new_messages') or 0),
                   'saved': learn_result.get('saved') or [],
                   'saved_count': int(learn_result.get('saved_count') or len(learn_result.get('saved') or [])),
                   'failures': failures, 'failure_count': len(failures),
                   'skipped_existing': int(learn_result.get('skipped_existing') or 0),
                   'completed': int(learn_result.get('completed') or 0),
                   'remaining': int(learn_result.get('remaining') or 0),
                   'batch_count': int(learn_result.get('batch_count') or 0),
                   'model_calls': int(sum(int((result.get(name) or {}).get('model_calls') or 0)
                                          for name in ('learn', 'digest'))),
                   'truncated': bool((result.get('range') or {}).get('truncated')),
                   'note': ('整理完成' if status == 'done' else '整理中断，未完成材料保留') +
                           '：已处理%d份，剩余%d份，本次%d批' % (
                               int(learn_result.get('completed') or 0), int(learn_result.get('remaining') or 0),
                               int(learn_result.get('batch_count') or 0))})
    _finish(root, role, run_id, status, progress, result,
            '' if status == 'done' else '；'.join(str(item.get('reason') or '') for item in failures)[:500])
    return _load(root, run_id)


def run_now(root, *, gateway=None, **kwargs):
    """Submit and execute synchronously (compatibility/manual tests only)."""
    accepted = submit(root, autostart=False, gateway=gateway, **kwargs)
    return execute(root, accepted['id'], gateway=gateway)


def tick(root, now=None):
    """The one automatic daily trigger: at most one run per 04:00 window."""
    root = Path(root)
    now = _epoch(now, time.time())
    window = _daily_window(now)
    path = _prepare(root, 'qq')
    with database(path) as db:
        rows = [dict(row) for row in db.execute(
            'SELECT * FROM memory_curate_runs WHERE role=? ORDER BY created DESC', ('qq',))
                if float(json.loads(row['request'] or '{}').get('window') or 0) == float(window)]
        attempt = db.execute('SELECT value FROM memory_curate_state WHERE key=?',
                             ('daily_attempt:' + str(int(window)),)).fetchone()
    for row in rows:
        record = _record(row)
        if record['status'] == 'done':
            return {'submitted': False, 'skipped': '今天已经整理过', 'run': record}
        if record['status'] in ('queued', 'running'):
            return {'submitted': False, 'skipped': '同一范围正在整理，返回同一执行引用', 'run': record}
    if attempt is not None:
        try:
            previous = json.loads(attempt[0])
        except (TypeError, ValueError):
            previous = {}
        if now - float(previous.get('at') or 0) < 3600:
            return {'submitted': False, 'skipped': '上次整理失败，一个小时内不重复自动重试',
                    'run': _record(rows[0]) if rows else None}
    record = submit(root, end=now,
                    requested_by='daily', window=window, autostart=True)
    return {'submitted': True, 'run': record}
