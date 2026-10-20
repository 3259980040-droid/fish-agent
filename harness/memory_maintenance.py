"""Scheduled memory maintenance: daily/weekly jobs in Asia/Shanghai (UTC+8).

``MemoryMaintenance(root).tick(now)`` is cheap and idempotent and is meant to
be called from the ToolHost background worker; it catches up a missed daily
04:00 or weekly Sunday 04:30 window once, then only processes *new* source
messages (per-role incremental cursors). ``run(kind, now=...)`` executes one
job on demand.

Sources are the real private/local histories already on disk: inbound QQ
messages in ``data/qq/qq.db`` and inbound desktop chats in
``data/<role>/desktop-chat.db``. Only user messages are read -- never the
bot's own replies or model-written summaries -- and every stored fact keeps
its ``source`` reference. A job with no new eligible message makes no model
request (the call itself is refused in that case).

Weekly maintenance does not delete memory files: it deduplicates automatic
facts, evaluates expiry/inactivity with the upstream A_memorix lifecycle policy
(via ``memory_source``) and only moves inactive automatic facts to the
``candidate`` status so they are no longer recalled as active facts. Owner
(newest copy of an auto fact) writes are never touched. No user notification
is produced by any run; failures are recorded in the maintenance state.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import memory_source
from storage import database, role_dir

TZ = timezone(timedelta(hours=8))
DAILY_TIME = (4, 0)
WEEKLY_WEEKDAY = 6          # Sunday (Monday=0)
WEEKLY_TIME = (4, 30)
DAILY_MAX_MESSAGES = 120    # per source per day (oldest first; rest on next day)
CATCH_UP_SCAN_LIMIT = 400   # rows inspected per source when a window was missed
MAX_CATCH_UP_MESSAGES = 400  # messages replayed once by a missed window
MAX_FAILURES_RETRIED = 1
HALF_LIFE_HOURS = 24 * 7
DECAY_THRESHOLD = 0.1
TERMINAL = ('done', 'empty')

_NAMESPACE = 'memory-maintenance'
# Process-wide (not thread-local): every thread/instance of a root shares the
# same run lock, so two maintenance threads never learn the same window twice.
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class _RootLock:
    """Thread lock (all threads of this process) + advisory file lock (processes).

    ``threading.local`` is never used here: the lock object must be identical
    for every thread, and the file lock makes a separately started process
    (e.g. a child spawned outside this process) wait instead of interleaving.
    """

    def __init__(self, root):
        self.thread = threading.RLock()
        self.path = Path(root).resolve() / 'data' / '.memory-maintenance.lock'
        self.handle = None

    def __enter__(self):
        self.thread.acquire()
        try:
            import msvcrt
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.path, 'a+b')
            for _ in range(600):            # up to ~60 s, then proceed
                try:
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        except (ImportError, OSError):
            self.handle = None              # file lock unavailable: thread lock only
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            try:
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            except (ImportError, OSError):
                pass
            self.handle.close()
            self.handle = None
        self.thread.release()
        return False


def _shared_lock(name):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(name, threading.RLock())


def _shared_memory(key, factory):
    """Process-wide mutable dict, safe to create from several threads."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault('shared:' + key, factory())


def process_lock(root):
    """Process-shared lock for one root (see ``_RootLock``)."""
    root = str(Path(root).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get('root:' + root)
        if lock is None:
            lock = _LOCKS['root:' + root] = _RootLock(root)
        return lock


# --------------------------------------------------------------------------
# time helpers (pure)
# --------------------------------------------------------------------------

def to_timestamp(now) -> float:
    """Accept None (wall clock), epoch seconds, naive/aware datetime, ISO text."""
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return float(now)
    text = str(now).strip()
    if re.fullmatch(r'\d+(\.\d+)?', text):
        return float(text)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.timestamp()


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


def due_windows(now) -> dict:
    """Most recent daily/weekly occurrence at or before ``now``."""
    now = to_timestamp(now)
    current = local_dt(now)
    daily = current.replace(hour=DAILY_TIME[0], minute=DAILY_TIME[1], second=0, microsecond=0)
    if daily > current:
        daily -= timedelta(days=1)
    weekly = current.replace(hour=WEEKLY_TIME[0], minute=WEEKLY_TIME[1], second=0, microsecond=0)
    weekly -= timedelta(days=(current.weekday() - WEEKLY_WEEKDAY) % 7)
    if weekly > current:
        weekly -= timedelta(days=7)
    return {'daily': daily.timestamp(), 'weekly': weekly.timestamp()}


def next_windows(now) -> dict:
    """The first daily/weekly window strictly after ``now``."""
    now_ts = to_timestamp(now)
    base = local_dt(now_ts).replace(hour=0, minute=0, second=0, microsecond=0)
    result = {}
    for kind, (hour, minute) in (('daily', DAILY_TIME), ('weekly', WEEKLY_TIME)):
        for offset in range(15):
            candidate = base + timedelta(days=offset, hours=hour, minutes=minute)
            if candidate.timestamp() <= now_ts:
                continue
            if kind == 'weekly' and candidate.weekday() != WEEKLY_WEEKDAY:
                continue
            result[kind] = candidate.timestamp()
            break
    return result


def _iso(ts):
    return local_dt(ts).strftime('%Y-%m-%d %H:%M')


def _strftime(ts: float) -> str:
    """Stored message timestamp format (naive Shanghai wall-clock text)."""
    return local_dt(ts).strftime('%Y-%m-%d %H:%M:%S')


def _parse_iso(text) -> float:
    try:
        parsed = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.timestamp()


# --------------------------------------------------------------------------
# persisted state
# --------------------------------------------------------------------------

class _State:
    """Maintenance state rows in the role's memory.db (schema tolerant)."""

    def __init__(self, path: Path):
        self.path = path
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS memory_maintenance_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def get(self, key, default=None):
        with database(self.path) as db:
            row = db.execute('SELECT value FROM memory_maintenance_state WHERE key=?', (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row['value'])
        except (TypeError, ValueError):
            return default

    def set(self, key, value):
        with database(self.path) as db:
            db.execute('INSERT INTO memory_maintenance_state(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, json.dumps(value, ensure_ascii=False)))


# --------------------------------------------------------------------------
# source readers (real private/local histories, user messages only)
# --------------------------------------------------------------------------

READ_BEHIND = 20          # rows re-read before the cursor id (ts-skew safety)
READ_LIMIT = 1000         # rows fetched per store per run


def _qq_chat_scope(chat_id: str, conversation_id: str) -> str:
    """Map a real qq.db chat to the memory scope the live pipeline enqueues."""
    if re.fullmatch(r'chat:group:[0-9]{5,15}', str(chat_id or '')):
        return str(chat_id)               # matched real group chat scope
    if re.search(r':test:[a-f0-9]{32}', str(conversation_id or '')):
        return ''                          # isolated test window: never learned
    return 'owner'                         # owner private chat


def _qq_bot_id(root: Path) -> str:
    try:
        config = json.loads((Path(root) / 'channels' / 'fish_qq' / 'config.json').read_text(encoding='utf-8'))
        return str(config.get('bot_id') or config.get('self_id') or '').strip()
    except (OSError, ValueError, TypeError):
        return ''


def read_qq_messages(root: Path, role: str, start: float, after_id=0) -> list:
    """Inbound user messages of the qq store with ``ts >= start``.

    ``start`` (epoch seconds) bounds the scan as ``ts >= start`` so it never
    walks more history than the session needs; rows are returned even when
    their id is at or below the cursor, so a message whose ``ts`` reads
    earlier than its neighbours is still seen. The caller filters by both ts
    and id, which keeps this reader idempotent.
    """
    path = Path(root) / 'data' / 'qq' / 'qq.db'
    if not path.is_file():
        return []
    bot_id = _qq_bot_id(root)
    from governance import Governance
    owner_id = Governance(root).owner_id()
    with closing(sqlite3.connect(path, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT id,ts,chat_id,user_id,text,conversation_id,sender_id,direction,channel_message_id,status "
                "FROM messages WHERE direction='inbound' AND ts>=? AND id>? ORDER BY id LIMIT ?",
                (_strftime(float(start)), int(after_id), READ_LIMIT)).fetchall()
        except sqlite3.Error:
            return []
        normal = []
        for row in rows:
            text = str(row['text'] or '').strip()
            sender = str(row['sender_id'] or row['user_id'] or '')
            if str(row['chat_id']) != 'private:' + owner_id or sender != owner_id:
                continue
            scope = _qq_chat_scope(row['chat_id'], row['conversation_id'])
            if not text or not re.fullmatch(r'[0-9]{5,15}', sender) or sender == bot_id:
                continue
            if row['status'] in ('failed', 'cancelled'):
                continue
            # Group chats need an explicitly bonded ("matched") group; anything
            # else (other groups, bonded group test windows) is not learned.
            if scope.startswith('chat:'):
                if role != 'qq' or not _matched_group(root, scope[5:]):
                    continue
            message_id = str(row['channel_message_id'] or row['id'])
            normal.append({'origin': 'qq.db', 'row_id': int(row['id']), 'ts': _parse_iso(row['ts']), 'scope': scope,
                           'source': 'qq:' + message_id, 'subject': 'qq:' + sender, 'sender': sender,
                           'text': text, 'channel_message_id': message_id,
                           'conversation': str(row['conversation_id'] or '')})
    return normal


def _matched_group(root: Path, group: str) -> bool:
    """Is this real group chat the single bonded group of the roster?"""
    group = str(group)
    if not re.fullmatch(r'[0-9]{5,15}', group):
        return False
    try:
        from social_roster import SocialRoster
        return str(SocialRoster(Path(root)).settings().get('group_id') or '') == group
    except Exception:  # noqa: BLE001 - no roster means nothing is bonded yet
        return False


def read_desktop_messages(root: Path, role: str, start: float, after_id=0) -> list:
    """Inbound user desktop chat messages of one role with ``ts >= start``.

    Ordinary chats and work chats share the owner scope; owner private test
    spaces are read only for recall (their queue is not written).
    """
    path = Path(root) / 'data' / role / 'desktop-chat.db'
    if not path.is_file():
        return []
    out = []
    # A normal connection (the two processes may share the same WAL database;
    # a second process never needs a read-only handle). Missing columns/tables
    # yield no messages instead of failing the whole maintenance run.
    with closing(sqlite3.connect(path, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                'SELECT m.id,m.conversation_id,m.direction,m.text,m.status,m.created_at,c.mode,c.space_id '
                'FROM messages m LEFT JOIN conversations c ON c.id=m.conversation_id '
                "WHERE m.direction='inbound' AND m.created_at>=? AND m.id>? ORDER BY m.id LIMIT ?",
                (_strftime(float(start)), int(after_id), READ_LIMIT)).fetchall()
        except sqlite3.Error:
            return []
    for row in rows:
        text = str(row['text'] or '').strip()
        if not text or text == '[图片]':
            continue
        if str(row['status'] or '') in ('failed', 'cancelled', 'blocked'):
            continue
        mode = str(row['mode'] or 'chat')
        space = str(row['space_id'] or '')
        if mode == 'work':
            scope, origin = 'owner', 'desktop-work'              # work and chat share facts
        elif space:
            scope, origin = 'owner', 'desktop-chat'
        else:
            scope, origin = 'owner', 'desktop-chat'
        out.append({'origin': f'{role}/{origin}', 'row_id': int(row['id']), 'ts': _parse_iso(row['created_at']),
                    # Same source string the live desktop pipeline queues.
                    'scope': scope, 'source': f'desktop:{row["id"]}', 'subject': '', 'sender': '', 'text': text,
                    'channel_message_id': str(row['id']), 'conversation': str(row['conversation_id'] or '')})
    return out


# --------------------------------------------------------------------------
# history cursor (incremental: only new messages)
# --------------------------------------------------------------------------

class HistoryCursor:
    """Incremental reader over qq.db and both desktop-chat stores.

    A message is new when it lies inside the session window (``start`` ..
    ``end``; the previous scheduled window, or the last successful run for a
    catch-up) and its row id is above the last handled id of its store
    (``seen``). ``seen`` never moves backwards, so a manually re-run older
    window cannot re-learn what was already handled, and the stored sources
    guard keeps a run idempotent even when it stops early. ``qq_cursor``
    records the highest ``qq.db`` user row scanned, to bound the next scans.
    On first use ``initialize`` records the current newest rows as *history*
    (no bulk import of old history); only rows newer than those are learned.
    Callers initialize before reading, and a missing ``seen`` never inherits
    the just-written baseline. Only user messages are read.
    """

    STORES = ('qq.db', 'desktop-chat.db')
    RETAIN_SOURCES = 5000      # remembered handled sources (idempotency guard)

    def __init__(self, root: Path):
        self.root = Path(root)

    def roles(self):
        found = [role for role in ('qq', 'research') if (Path(self.root) / 'data' / role / 'desktop-chat.db').is_file()]
        if (Path(self.root) / 'data' / 'qq' / 'qq.db').is_file() and 'qq' not in found:
            found.append('qq')
        return found

    def _state(self, role: str) -> _State:
        return _State(role_dir(self.root, role) / 'memory.db')

    @staticmethod
    def _max_id(path: Path) -> int:
        if not path.is_file():
            return 0
        try:
            with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=5)) as db:
                return int(db.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0])
        except sqlite3.Error:
            return 0

    def _path(self, role, store):
        return self.root / 'data' / ('qq' if store == 'qq.db' else role) / store

    @staticmethod
    def _store(row) -> str:
        return 'qq.db' if row['origin'] == 'qq.db' else 'desktop-chat.db'

    def initialize(self, role: str, now: float) -> dict:
        """First use only: rows existing now are history, never learned.

        The baseline records both the current newest id per store and the
        creation instant. A row is in this session when it arrived after that
        instant; the id keeps rows written in the same second (with an
        already-higher id) from being skipped.
        """
        state = self._state(role)
        if state.get('cursor_initialized'):
            return state.get('cursor') or {}
        seen = {'qq.db': self._max_id(self._path(role, 'qq.db')),
                'desktop-chat.db': self._max_id(self._path(role, 'desktop-chat.db'))}
        cursors = {'qq_cursor': seen['qq.db']}
        state.set('cursor', cursors)
        state.set('seen', seen)
        state.set('cursor_initialized', True)
        state.set('cursor_initialized_at', now)
        return cursors

    def collect(self, role: str, start: float, end: float, *, limit: int = DAILY_MAX_MESSAGES) -> dict:
        """User messages with ``start <= ts <= end`` that were not handled before.

        All read rows are returned for cursor bookkeeping; ``messages`` (what
        the learning queue receives) keeps only those newer than the last
        handled id. On first use the current rows are the baseline.
        """
        state = self._state(role)
        start, end = float(start), float(end)
        if not state.get('cursor_initialized'):
            self.initialize(role, end)
        seen = self._seen(state)
        start = min(start, float(state.get('cursor_initialized_at') or start))
        rows = [row for row in read_qq_messages(self.root, role, start, seen.get('qq.db', 0)) if start <= row['ts'] <= end]
        rows += [row for row in read_desktop_messages(self.root, role, start, seen.get('desktop-chat.db', 0)) if start <= row['ts'] <= end]
        rows.sort(key=lambda item: (item['ts'], item['origin'], item['row_id']))
        # New = inside this session's window (past the first-use baseline, so
        # old history is never bulk-imported) and past the last handled id of
        # its store. Rows already picked up by the live queue are dropped by
        # the caller, which keeps the remaining backlog for a later run.
        since = float(state.get('cursor_initialized_at') or 0)
        fresh = [row for row in rows
                 if row['row_id'] > seen[self._store(row)] and (start >= since or row['ts'] >= since)]
        return {'role': role, 'messages': fresh[:max(1, limit)], 'seen': len(fresh), 'rows': rows,
                'cursors': dict(state.get('cursor') or {}), 'start': start}

    @staticmethod
    def _seen(state) -> dict:
        stored = state.get('seen') or {}
        return {'qq.db': int(stored.get('qq.db') or 0), 'desktop-chat.db': int(stored.get('desktop-chat.db') or 0)}

    def advance(self, collected: dict) -> dict:
        """Move ``seen`` past the ids this run learned or recognised."""
        role = collected['role']
        state = self._state(role)
        seen = self._seen(state)
        for row in collected.get('rows') or []:
            seen[self._store(row)] = max(seen[self._store(row)], int(row['row_id']))
        cursors = dict(state.get('cursor') or collected.get('cursors') or {})
        cursors['qq_cursor'] = max(int(cursors.get('qq_cursor') or 0), seen['qq.db'])
        state.set('cursor', cursors)
        state.set('seen', seen)
        state.set('cursor_initialized', True)
        return cursors


# --------------------------------------------------------------------------
# maintenance
# --------------------------------------------------------------------------

YES = ('ok', 'done', 'skipped', 'empty')
NO = ('pending', 'running', 'failed')


def _learning(state: _State) -> dict:
    return state.get('learning') or {'processed': {}, 'failures': {}}


def _known_sources(store) -> set:
    """Every source the role already has a learning job for (live queue)."""
    with database(store.path) as db:
        return {row[0] for row in db.execute('SELECT source FROM learning_jobs').fetchall()}


def _clean_source(text: str) -> str:
    return re.sub(r'[\s\W_]+', '', str(text)).lower()


def _clusters(rows, threshold=0.8):
    """Greedy exact-text clustering over near-identical normalized texts."""
    groups = {}
    for row in rows:
        key = _clean_source(row['text'])
        if len(key) < 6:
            continue
        groups.setdefault(key, []).append(row)
    if not groups:
        return []
    clusters = []
    for key, members in groups.items():
        placed = False
        for cluster in clusters:
            left, right = key, cluster['key']
            short, long_ = (left, right) if len(left) <= len(right) else (right, left)
            # Prefix + character-multiset overlap: "…在北门" vs "…在北门了！"
            # are one fact with punctuation, not a fragment of a longer text.
            prefix = sum(1 for a, b in zip(short, long_) if a == b) / max(1, len(short))
            overlap = len(set(short) & set(long_)) / max(1, len(set(long_)))
            if prefix >= threshold and overlap >= 0.6:
                cluster['keys'].append(key)
                cluster['rows'].extend(members)
                placed = True
                break
        if not placed:
            clusters.append({'key': key, 'keys': [key], 'rows': list(members)})
    return clusters


class MemoryMaintenance:
    """Scheduled extraction (daily) and dedup/expiry (weekly) per role."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.history = HistoryCursor(self.root)
        # One process-wide lock per root so tick(), the ToolHost background
        # thread and a manual run never overlap (jobs per kind are serialized
        # by the job records; the lock closes the read-then-write race).
        self.lock = process_lock(self.root)
        with self.lock:
            # First use of this role: the messages present now are history (no
            # bulk import); anything arriving after this point is new.
            for role in self._roles() or ('qq',):
                self.history.initialize(role, time.time())

    # -- state --------------------------------------------------------------
    def _state(self, role='qq') -> _State:
        return _State(role_dir(self.root, role) / 'memory.db')

    def _job(self, state: _State, kind) -> dict:
        return dict((state.get('job:' + kind) or {}).get('record') or {})

    def _save_job(self, state: _State, kind, record: dict):
        state.set('job:' + kind, {'record': record})

    def _roles(self):
        return [role for role in ('qq', 'research') if (self.root / 'data' / role).is_dir()]

    def status(self) -> dict:
        """Persisted schedule/run state, cursors, readiness; read-only."""
        now = time.time()
        windows = due_windows(now)
        upcoming = next_windows(now)
        jobs, cursors = {}, {}
        for role in self._roles():
            state = self._state(role)
            for kind in ('daily', 'weekly'):
                record = self._job(state, kind)
                last = float(record.get('last_window') or 0)
                jobs[f'{role}:{kind}'] = {
                    'last_window': _iso(last) if last else '',
                    'status': record.get('status', 'never'),
                    'started_at': _iso(record['started_at']) if record.get('started_at') else '',
                    'finished_at': _iso(record['finished_at']) if record.get('finished_at') else '',
                    'summary': record.get('summary') or {},
                    'last_error': str(record.get('last_error') or '')[:300],
                    'due': not last or last < windows[kind],
                }
            cursors[role] = {'initialized': bool(state.get('cursor_initialized')),
                             **(state.get('cursor') or {})}
        try:
            from memory_engine import MemoryEngine
            semantic = MemoryEngine(self.root, 'qq').semantic_status()
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            semantic = {'status': 'error', 'ready': False, 'detail': type(exc).__name__}
        try:
            source = memory_source.source_status(self.root)
        except Exception:  # noqa: BLE001
            source = {}
        return {
            'timezone': 'Asia/Shanghai (UTC+8)',
            'daily': {'at': '04:00', 'due_window': _iso(windows['daily']), 'next': _iso(upcoming['daily'])},
            'weekly': {'at': 'Sunday 04:30', 'due_window': _iso(windows['weekly']), 'next': _iso(upcoming['weekly'])},
            'jobs': jobs,
            'cursors': cursors,
            'sources': {'qq.db': (self.root / 'data/qq/qq.db').is_file(),
                        'desktop': {role: (self.root / 'data' / role / 'desktop-chat.db').is_file()
                                    for role in ('qq', 'research')}},
            'semantic': semantic,
            'upstream': {'commit': memory_source.MAIBOT_COMMIT,
                         'lifecycle_loaded': bool((source.get('lifecycle') or {}).get('loaded')),
                         'calibration_loaded': bool((source.get('calibration') or {}).get('loaded'))},
            'limits': {'daily_max_messages': DAILY_MAX_MESSAGES, 'half_life_hours': HALF_LIFE_HOURS,
                       'decay_threshold': DECAY_THRESHOLD, 'max_failure_retries': MAX_FAILURES_RETRIED},
        }

    def due(self, now=None) -> list:
        """[(role, kind)] whose most recent scheduled window has not run yet.

        A run counts as done when its scheduled window is newer than the last
        successful window and newer than the last attempt (failed runs retry
        on the next tick, but never in a loop), or when the earlier window
        already ran successfully (the catch-up happened once).
        """
        windows = due_windows(now)
        result = []
        for role in self._roles():
            state = self._state(role)
            if not state.path.is_file():
                continue
            for kind in ('daily', 'weekly'):
                record = self._job(state, kind)
                last = float(record.get('last_window') or 0)
                if last >= windows[kind]:
                    continue
                if self._skip_missed(record, kind, windows[kind]):
                    continue
                if (record.get('status') != 'error'
                        and float(record.get('attempted_window') or 0) >= windows[kind]):
                    continue
                result.append((role, kind))
        return result

    def tick(self, now=None):
        """Cheap, idempotent: run each due job once; never raises for a job."""
        now = to_timestamp(now)
        with self.lock:
            for role in self._roles():
                # First use: rows existing now are history, not backlog.
                self.history.initialize(role, now)
            return self._tick(now)

    def _tick(self, now):
        summary = {'now': _iso(now), 'ran': [], 'errors': [], 'status': 'ok'}
        due = self.due(now)
        # A model-less role (no key yet) stays due until a key exists; it is
        # retried at most hourly so it never delays or re-runs the other role.
        attention = _shared_memory('attention', dict)
        key = str(self.root.resolve())
        last = attention.get(key) or {}
        waiting = [(role, kind) for role, kind in due
                   if self._job(self._state(role), kind).get('status') == 'pending']
        if waiting and now - float(last.get('at') or 0) < 3600 and set(waiting) == set(last.get('items') or []):
            summary['status'] = 'idle'
            summary['deferred'] = [f'{role}:{kind}' for role, kind in waiting]
            return summary
        for role, kind in due:
            try:
                result = self.run(kind, now=now, role=role)
                summary['ran'].append({'role': role, 'kind': kind, 'status': result['status'],
                                       'summary': result['summary']})
            except Exception as exc:  # noqa: BLE001 - recorded in state, chat continues
                summary['errors'].append({'role': role, 'kind': kind,
                                          'error': type(exc).__name__ + ': ' + str(exc)[:200]})
        attention[key] = {'at': now, 'items': [f'{role}:{kind}' for role, kind in waiting]}
        if not due:
            summary['status'] = 'idle'
        elif summary['errors']:
            summary['status'] = 'partial'
        return summary

    # -- run ----------------------------------------------------------------

    @staticmethod
    def _earlier_window(kind, window) -> float:
        period = 86400 if kind == 'daily' else 7 * 86400
        return window - period

    def _missed(self, record, kind, window) -> bool:
        """Was the previous window missed (last successful run before it)?

        For the daily job this is checked against the previous *daily* window
        (Fish may have been off for days); because a missed window is caught
        up only once, a run cost of at most one extra bounded catch-up is
        guaranteed per long gap.
        """
        return float(record.get('last_window') or 0) < self._earlier_window(kind, window)

    @staticmethod
    def _skip_missed(record, kind, window) -> bool:
        """True when this missed window was already caught up once."""
        return float(record.get('attempted_window') or 0) >= window

    def _run_once(self, kind, now, role, gateway_factory, catch_up):
        """One attempt for the window of ``now``; returns (status, summary)."""
        window = due_windows(now)[kind]
        state = self._state(role)
        previous = self._job(state, kind)
        record = {**previous, 'status': 'running', 'started_at': time.time(), 'last_error': ''}
        self._save_job(state, kind, record)
        try:
            if kind == 'daily':
                # A catch-up replays since the last successful run; a regular
                # run covers since the previous daily window. Weekly never
                # replays history: its review only looks at current memory.
                if catch_up:
                    start = max(float(previous.get('last_window') or 0), self._earlier_window(kind, window) - 6 * 86400)
                else:
                    start = self._earlier_window(kind, window)
                summary = self._daily(role, window, now, gateway_factory, start=start, catch_up=catch_up,
                                      budget=MAX_CATCH_UP_MESSAGES if catch_up else DAILY_MAX_MESSAGES)
            else:
                summary = self._weekly(role, window, now)
        except Exception as exc:
            self._save_job(state, kind, {**record, 'status': 'error', 'finished_at': time.time(),
                                         'last_error': type(exc).__name__ + ': ' + str(exc)[:300],
                                         'attempted_window': window})
            raise
        record.update(status=summary.pop('status'), finished_at=time.time(), attempted_window=window,
                      summary=summary, last_error=str(summary.get('error') or '')[:300])
        if record['status'] in YES:
            # Successful (or empty) run: this is the last successful window; a
            # catch-up computes the next start from it.
            record['last_window'] = window
        self._save_job(state, kind, record)
        return record['status'], summary

    def run(self, kind, now=None, *, role='qq', gateway_factory=None):
        """Execute 'daily' or 'weekly' once for a role and persist the result.

        If the previous window was missed, the messages since the last run are
        collected once (``caught_up``) before the current window runs, so a
        long downtime never silently drops history.
        """
        if kind not in ('daily', 'weekly'):
            raise ValueError('维护任务只能是 daily 或 weekly')
        if role not in ('qq', 'research'):
            raise ValueError('未知角色')
        now = to_timestamp(now)
        window = due_windows(now)[kind]
        with self.lock:
            state = self._state(role)
            # First real use of this role's state: without a baseline the whole
            # pre-existing history (and the live queue built from it) looked
            # "new". Record the current rows as history; only later messages
            # are learned (`collect()` applies the same rule as a backstop).
            record = self._job(state, kind)
            self.history.initialize(role, now)
            if float(record.get('last_window') or 0) >= window:
                return {'kind': kind, 'role': role, 'window': _iso(window), 'status': 'already-done',
                        'summary': record.get('summary') or {}}
            caught_up = None
            if kind == 'daily' and self._missed(record, kind, window):
                # Fish was off over at least one window: replay what arrived
                # since the last successful run, once. (A run already done for
                # today is not repeated by the normal run below.)
                status, caught_up = self._run_once(kind, now, role, gateway_factory, True)
                return {'kind': kind, 'role': role, 'window': _iso(window), 'status': status,
                        'summary': caught_up, 'caught_up': caught_up}
            if (record.get('status') != 'error' and float(record.get('attempted_window') or 0) >= window
                    and not caught_up):
                return {'kind': kind, 'role': role, 'window': _iso(window), 'status': 'already-done',
                        'summary': record.get('summary') or {}}
            status, summary = self._run_once(kind, now, role, gateway_factory, False)
            try:
                from memory_engine import enqueue_warm
                enqueue_warm(self.root, role)
            except Exception:  # noqa: BLE001 - backfill is best effort
                pass
            result = {'kind': kind, 'role': role, 'window': _iso(window), 'status': status, 'summary': summary}
            if caught_up is not None:
                result['caught_up'] = caught_up
            return result

    # -- daily --------------------------------------------------------------
    def _gateway(self, role, factory):
        if factory is not None:
            return factory(role)
        from model_gateway import ModelGateway
        try:
            return ModelGateway(self.root, role)
        except (ValueError, KeyError, OSError):
            return None

    def _daily(self, role, window, now, factory=None, *, start, catch_up=False, budget=DAILY_MAX_MESSAGES) -> dict:
        """Learn the messages of this session (``start`` .. ``now``).

        A regular run takes messages since the previous window; a catch-up
        run (a missed window) takes the ones since the last successful run,
        bounded, so nothing that arrived while Fish was off is lost. Messages
        already in the role's learning history (live mode queues them as they
        arrive) are never extracted again.
        """
        from memory_store import MemoryStore
        store = MemoryStore(self.root, role)
        today = _iso(window)[:10]
        state = self._state(role)
        learning = _learning(state)
        window_key = f'{role}:{today}' + (':catch-up' if catch_up else '')
        if not catch_up and learning.get('window') == window_key and not (
                'error' in (self._job(state, 'daily') or {})):
            # Already final for this Shanghai day (a manual re-run never re-learns).
            return {'status': 'empty', 'window': window_key, 'new_messages': 0, 'model_calls': 0,
                    'note': '今日窗口已完成，未调用模型'}
        collected = self.history.collect(role, start, now, limit=budget)
        queue = self._learning_queue(role)
        processed = learning.get('processed') or {}
        failures = learning.get('failures') or {}
        # Only rows this run learns (or the live pipeline already queued):
        # others stay below 'seen' and are picked up by a later run.
        known = set(queue)
        fresh = []
        for message in collected['messages']:
            source = message['source']
            if source in known or source in processed or int(failures.get(source, 0)) > MAX_FAILURES_RETRIED:
                continue
            fresh.append(message)
        summary = {'window': window_key, 'new_messages': len(fresh), 'scanned': collected['seen'],
                   'skipped_existing': collected['seen'] - len(fresh),
                   'catch_up_from': _iso(start) if catch_up else '',
                   'sources': sorted({message['origin'] for message in fresh}),
                   'queued': 0, 'model_calls': 0}
        if not fresh:
            # Nothing new to learn: no model request at all. A catch-up keeps
            # the day resumable so the current window still learns normally.
            learning.update(processed=processed, failures=failures, updated_at=now,
                            window=learning.get('window') if catch_up else window_key)
            state.set('learning', learning)
            self.history.advance(self._handled(role, collected['messages']))
            summary.update(status='empty', note='没有需要学习的新内容，未调用模型')
            return summary
        gateway = self._gateway(role, factory)
        if gateway is None or not getattr(gateway, 'key', ''):
            # Not a lost window: nothing is advanced, so the same messages are
            # retried once a model key exists (no backlog is lost).
            summary.update(status='pending', note='模型未配置，今日任务保留待重试；未调用模型')
            return summary
        mine = set()
        for message in fresh:
            scope = message['scope']
            if store.is_test_scope(scope):
                continue
            job = queue.get(message['source'])
            if job and job['scope'] == scope and job['status'] != 'failed':
                continue              # already queued/learned by the live pipeline
            store.enqueue(message['source'], message['text'], scope, subject=message['subject'])
            job = self._learning_queue(role).get(message['source'])
            if job and job['scope'] == scope and job['status'] in ('pending', 'running'):
                mine.add(message['source'])
        summary['queued'] = len(mine)
        summary['model_calls'] = self._drain(role, mine, gateway, processed, failures, budget)
        summary['failed'] = sorted(source for source, count in failures.items() if count)
        learning.update(window=window_key, processed=processed, failures=failures, updated_at=time.time())
        state.set('learning', learning)
        self.history.advance(self._handled(role, collected['messages']))
        summary['status'] = 'partial' if summary['failed'] else 'done'
        return summary

    @staticmethod
    def _handled(role, fresh):
        """Collected view limited to ``fresh``: only these rows move ``seen``.

        A row already in the live queue, or one beyond the per-run budget,
        keeps its old position and is picked up by a later run.
        """
        return {'role': role, 'messages': list(fresh), 'rows': list(fresh)}

    def _learning_queue(self, role):
        with database(role_dir(self.root, role) / 'memory.db') as db:
            return {row['source']: dict(row) for row in db.execute(
                "SELECT source,scope,status FROM learning_jobs WHERE scope NOT LIKE 'space:%' "
                "AND scope NOT LIKE 'chat:group:%:test:%'")}

    def _drain(self, role, mine, gateway, processed, failures, budget=DAILY_MAX_MESSAGES):
        """Learn this window's queued messages (existing ``learn_one``), capped."""
        from memory_store import MemoryStore
        store = MemoryStore(self.root, role)
        calls, no_progress = 0, 0
        with database(store.path) as db:
            ordered = [row['source'] for row in db.execute(
                "SELECT source FROM learning_jobs WHERE status IN ('pending','running') "
                "AND scope NOT LIKE 'space:%' AND scope NOT LIKE 'chat:group:%:test:%' ORDER BY created")]
        # Only this run's messages; a live-pipeline job before them is left to
        # the live worker (model_calls counts our own calls only).
        for source in [source for source in ordered if source in mine]:
            if calls >= int(budget) or no_progress >= 2:
                break
            learned = store.learn_one(gateway, source)
            calls += 1
            with database(store.path) as db:
                status = db.execute('SELECT status FROM learning_jobs WHERE source=?', (source,)).fetchone()
            if not learned or status is None:
                no_progress += 1
                continue
            state = status['status']
            if status is not None and state in TERMINAL:
                processed[source] = state
                failures.pop(source, None)
                no_progress = 0
            else:
                processed[source] = state
                failures[source] = int(failures.get(source, 0)) + 1
                no_progress += 1
        return calls

    # -- weekly -------------------------------------------------------------
    @staticmethod
    def _auto(row) -> bool:
        """Automatic facts only: owner/manual saves are never touched.

        Facts an owner correction conflicted with (``mark_conflict``) are
        excluded; a confirmation turns the row into a manual fact itself.
        """
        return (row['source'] != 'manual' and row['evidence'] != 'manual'
                and not str(row['scope']).startswith('chat:group:') and not row.get('conflict'))

    def _weekly(self, role, window, now) -> dict:
        from memory_store import MemoryStore
        MemoryStore(self.root, role)          # schema is created idempotently
        state = self._state(role)
        upstream = bool(memory_source.source_status(self.root)['lifecycle']['loaded'])
        summary = {'evaluated': 0, 'collapsed': [], 'deactivated': [], 'kept': 0,
                   'skipped_manual': 0, 'upstream': upstream, 'deleted': 0, 'model_calls': 0}
        conflict = state.get('conflicts') or {}
        with database(role_dir(self.root, role) / 'memory.db') as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM memories')]
        for row in rows:
            row['conflict'] = row['id'] in conflict
        auto = [row for row in rows if self._auto(row)]
        summary['skipped_manual'] = len(rows) - len(auto)
        summary['evaluated'] = len(auto)
        # 1) duplicates of the same learned text: keep the newest copy, move the
        #    others to candidates with their source preserved (no deletion).
        kept = set()
        for cluster in _clusters(auto):
            if len(cluster['rows']) < 2:
                continue
            keep = max(cluster['rows'], key=lambda row: (float(row['created'] or 0), float(row['updated'] or 0)))
            kept.add(keep['id'])
            for row in cluster['rows']:
                if row['id'] == keep['id'] or row['status'] != 'active':
                    continue
                self._deactivate(role, row, 'duplicate', now, upstream)
                summary['collapsed'].append({'id': row['id'], 'duplicate_of': keep['id'], 'source': row['source']})
        # 2) expiry + upstream lifecycle inactivity (no hard deletion)
        lifecycle = state.get('lifecycle') or {}
        for row in auto:
            if row['status'] != 'active' or lifecycle.get(row['id'], {}).get('status') == 'inactive':
                continue
            if row['id'] in kept and not (row['expires'] and row['expires'] <= now):
                # The surviving copy of a duplicate set stays searchable; its
                # inactive twins already hold that set's lifecycle verdict.
                summary['kept'] += 1
                continue
            reason = None
            if row['expires'] and row['expires'] <= now:
                reason = 'expired'
            else:
                anchor = float(row['observed'] or row['created'])
                strength = min(1.0, max(0.0, float(row['confidence']) or 1.0))
                decision = memory_source.evaluate_due(self.root, strength=strength, anchor=anchor, now=now,
                                                      half_life_hours=HALF_LIFE_HOURS, threshold=DECAY_THRESHOLD)
                lifecycle[row['id']] = {'status': 'inactive' if decision['inactive'] else 'active',
                                        'reason': decision['reason'], 'checked_at': now,
                                        'next_at': decision['next_at'], 'anchor': anchor,
                                        'upstream': decision['upstream']}
                if decision['inactive']:
                    reason = str(decision['reason'] or 'decayed')
            if reason:
                self._deactivate(role, row, reason, now, upstream)
                summary['deactivated'].append({'id': row['id'], 'reason': reason})
            else:
                summary['kept'] += 1
        state.set('lifecycle', lifecycle)
        # The semantic index follows the facts it serves (bounded backfill).
        try:
            from memory_engine import MemoryEngine
            summary['backfill'] = MemoryEngine(self.root, role).backfill().get('count', 0)
        except Exception as exc:  # noqa: BLE001 - shown in status, not fatal
            summary['backfill_error'] = type(exc).__name__
        summary['status'] = 'done'
        summary['note'] = '只整理自动学习的事实；人工保存或确认的事实未改动，未删除任何记忆文件或记录'
        return summary

    def _deactivate(self, role, row, reason, now, upstream):
        """Move a row to candidate (never deletes) and record its lifecycle state."""
        with database(role_dir(self.root, role) / 'memory.db') as db:
            db.execute('BEGIN IMMEDIATE')
            if row['status'] == 'active':
                db.execute("UPDATE memories SET status='candidate', updated=? WHERE id=? AND status='active'",
                           (now, row['id']))
                db.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")
        state = self._state(role)
        lifecycle = state.get('lifecycle') or {}
        lifecycle[row['id']] = {'status': 'inactive', 'reason': reason, 'since': now, 'upstream': upstream,
                                'anchor': float(row['observed'] or row['created'])}
        state.set('lifecycle', lifecycle)

    # -- confirmations ------------------------------------------------------
    def confirm(self, memory_id, *, scope='owner', role='qq', source='manual'):
        """Owner-confirmed fact: active, manual evidence, and excluded from dedup/decay.

        The confirmation is recorded as a new revision, so the learned text,
        source and the review that preceded it stay in history.
        """
        from memory_store import MemoryStore
        store = MemoryStore(self.root, role)
        with database(store.path) as db:
            row = db.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
        row = dict(row) if row is not None else None
        if row is None:
            raise ValueError('记忆不存在')
        if store.is_test_scope(row['scope']):
            raise PermissionError('独立测试空间长期记忆只读')
        if self._auto(row):
            marker = 'maint:confirmed:' + memory_id
            with database(store.path) as db:
                db.execute('INSERT OR IGNORE INTO exclusions VALUES(?,?,?)',
                           (MemoryStore.digest(marker), marker, time.time()))
        result = store.save(row['text'], category=row['category'], scope=row['scope'], source=source,
                            source_text='主人在本地确认了这条自动学习的记忆', confidence=1.0,
                            memory_id=memory_id, subject=row['subject'],
                            aliases=json.loads(row['aliases'] or '[]'), kind=row['kind'], evidence='manual',
                            status='active', observed=time.time(), expires=row['expires'])
        state = self._state(role)
        lifecycle = state.get('lifecycle') or {}
        if memory_id in lifecycle:
            lifecycle[memory_id].update(status='active', confirmed_at=time.time())
            state.set('lifecycle', lifecycle)
        return {'ok': True, **result, 'confirmed': True}

    def mark_conflict(self, memory_id, *, role='qq'):
        """Record that an owner correction conflicts with this automatic fact.

        The row is left untouched (it may still be visible where it was
        written); it is only excluded from weekly dedup/decay so a human
        correction is never silently re-bucketed.
        """
        with database(role_dir(self.root, role) / 'memory.db') as db:
            row = db.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
        if row is None:
            raise ValueError('记忆不存在')
        state = self._state(role)
        conflicts = state.get('conflicts') or {}
        conflicts[memory_id] = {'source': row['source'], 'text': row['text'][:1000], 'at': time.time()}
        state.set('conflicts', conflicts)
        return {'ok': True, 'conflicts': len(conflicts)}


def start_background(host, root=None, *, interval: float = 60.0, embed_interval: float = 300.0):
    """Host hook: one daemon thread running ``tick`` and embedding top-ups.

    ``host`` is the ToolHost (its ``stop`` event ends the loop). ``tick``
    checks the schedule every minute but only acts on a due window; the
    embedding warm-up only talks to the local model worker and never calls a
    model API. Returns the thread.
    """
    root = Path(root or host.root)
    started = _shared_memory('thread:' + str(root.resolve()), dict)
    existing = started.get('thread')
    if existing is not None and existing.is_alive():
        return existing                      # exactly one warm thread per root
    stop = getattr(host, 'stop', None)
    if stop is None:
        stop = threading.Event()
    maintenance = MemoryMaintenance(root)
    last_embed = 0.0

    def warm_embeddings():
        """Local model worker only (no model API); readiness on the first pass."""
        if not (root / 'harness' / 'agents.json').is_file():
            return
        try:
            from memory_engine import MemoryEngine
            for role in ('qq', 'research'):
                if (root / 'data' / role / 'memory.db').is_file():
                    MemoryEngine(root, role).maintenance()
        except Exception:  # noqa: BLE001 - readiness is reported by status()
            pass

    def loop():
        nonlocal last_embed
        first = True
        while not stop.is_set():
            if first:
                first = False
                warm_embeddings()
                last_embed = time.time()
            if stop.is_set():
                break
            try:
                maintenance.tick()
            except Exception:  # noqa: BLE001 - persisted in the job record
                pass
            if time.time() - last_embed >= embed_interval:
                last_embed = time.time()
                warm_embeddings()
            stop.wait(interval)
    thread = threading.Thread(target=loop, daemon=True, name='fish-memory-maintenance')
    started['thread'] = thread
    thread.start()
    return thread
