"""Scheduled-state readers for the one memory curation program.

The daily actual work lives in :mod:`memory_curation`: its automatic 04:00
window, the Fish tool and the client button all submit the same run. This module
keeps the historical readers (QQ/desktop messages), the persisted schedule
status and the compatibility entry points; it no longer owns a second learning
loop, a second daily tick or an independent dedup pass.
"""
from __future__ import annotations
from locations import project_path

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
        config = json.loads((project_path(Path(root), 'channels', 'fish_qq', 'config.json')).read_text(encoding='utf-8'))
        return str(config.get('bot_id') or config.get('self_id') or '').strip()
    except (OSError, ValueError, TypeError):
        return ''


def read_qq_messages(root: Path, role: str, start: float, after_id=0, *,
                     end=None, through_id=None, limit=READ_LIMIT, strict=False) -> list:
    """Inbound user messages of the qq store with ``ts >= start``.

    ``start`` (epoch seconds) bounds the scan as ``ts >= start`` so it never
    walks more history than the session needs; rows are returned even when
    their id is at or below the cursor, so a message whose ``ts`` reads
    earlier than its neighbours is still seen. The caller filters by both ts
    and id, which keeps this reader idempotent.
    """
    path = project_path(Path(root), 'data', 'qq', 'qq.db')
    if not path.is_file():
        return []
    bot_id = _qq_bot_id(root)
    from governance import Governance
    owner_id = Governance(root).owner_id()
    with closing(sqlite3.connect(path, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        try:
            bounds, values = '', [_strftime(float(start)), int(after_id)]
            if end is not None:
                bounds += ' AND ts<=?'
                values.append(_strftime(float(end)))
            if through_id is not None:
                bounds += ' AND id<=?'
                values.append(int(through_id))
            if limit is not None:
                bounds += ' ORDER BY id LIMIT ?'
                values.append(int(limit))
            else:
                bounds += ' ORDER BY id'
            rows = db.execute(
                "SELECT id,ts,chat_id,user_id,text,conversation_id,sender_id,direction,channel_message_id,status "
                "FROM messages WHERE direction='inbound' AND ts>=? AND id>?" + bounds, values).fetchall()
        except sqlite3.Error:
            if strict:
                raise
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
            # Match the live loop and owner evidence tools: qq:<local row id>.
            # Keep the old platform reference as an alias so previous learning
            # jobs/exclusions remain effective after this change.
            normal.append({'origin': 'qq.db', 'row_id': int(row['id']), 'ts': _parse_iso(row['ts']), 'scope': scope,
                           'source': 'qq:' + str(row['id']), 'source_aliases': ['qq:' + message_id],
                           'subject': 'qq:' + sender, 'sender': sender,
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


def read_desktop_messages(root: Path, role: str, start: float, after_id=0, *,
                          end=None, through_id=None, limit=READ_LIMIT, strict=False) -> list:
    """Inbound user desktop chat messages of one role with ``ts >= start``.

    Ordinary chats and work chats share the owner scope; owner private test
    spaces are read only for recall (their queue is not written).
    """
    path = project_path(Path(root), 'data', role, 'desktop-chat.db')
    if not path.is_file():
        return []
    out = []
    # A normal connection (the two processes may share the same WAL database;
    # a second process never needs a read-only handle). Missing columns/tables
    # yield no messages instead of failing the whole maintenance run.
    with closing(sqlite3.connect(path, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        try:
            bounds, values = '', [_strftime(float(start)), int(after_id)]
            if end is not None:
                bounds += ' AND m.created_at<=?'
                values.append(_strftime(float(end)))
            if through_id is not None:
                bounds += ' AND m.id<=?'
                values.append(int(through_id))
            if limit is not None:
                bounds += ' ORDER BY m.id LIMIT ?'
                values.append(int(limit))
            else:
                bounds += ' ORDER BY m.id'
            rows = db.execute(
                'SELECT m.id,m.conversation_id,m.direction,m.text,m.status,m.created_at,c.mode,c.space_id '
                'FROM messages m LEFT JOIN conversations c ON c.id=m.conversation_id '
                "WHERE m.direction='inbound' AND m.created_at>=? AND m.id>?" + bounds, values).fetchall()
        except sqlite3.Error:
            if strict:
                raise
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
        found = [role for role in ('qq', 'research') if (project_path(Path(self.root), 'data', role, 'desktop-chat.db')).is_file()]
        if (project_path(Path(self.root), 'data', 'qq', 'qq.db')).is_file() and 'qq' not in found:
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
        return project_path(self.root, 'data', 'qq' if store == 'qq.db' else role, store)

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
        record = dict((state.get('job:' + kind) or {}).get('record') or {})
        # Older successful maintenance rows stored attempted_window only.
        # Treat that completed window as successful without rerunning history.
        if not record.get('last_window') and record.get('status') in YES:
            record['last_window'] = record.get('attempted_window', 0)
        return record

    def _save_job(self, state: _State, kind, record: dict):
        state.set('job:' + kind, {'record': record})

    def _roles(self):
        return [role for role in ('qq', 'research') if (project_path(self.root, 'data', role)).is_dir()]

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
            'sources': {'qq.db': (project_path(self.root, 'data/qq/qq.db')).is_file(),
                        'desktop': {role: (project_path(self.root, 'data', role, 'desktop-chat.db')).is_file()
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
        """Compatibility entry into the one curation program's daily window."""
        from memory_curation import tick as curation_tick
        return curation_tick(self.root, now)


    # -- run ----------------------------------------------------------------

    @staticmethod
    def _earlier_window(kind, window) -> float:
        period = 86400 if kind == 'daily' else 7 * 86400
        return window - period


    @staticmethod
    def _skip_missed(record, kind, window) -> bool:
        """True when this missed window was already caught up once."""
        return float(record.get('attempted_window') or 0) >= window


    def run(self, kind, now=None, *, role='qq', gateway_factory=None):
        """Compatibility entry into the one curation program.

        Daily learning/digest/emoji and the duplicate/expiry pass are no longer
        executed here; this class keeps status readers and the historical
        readers, while every write goes through ``memory_curation``.
        """
        if kind not in ('daily', 'weekly'):
            raise ValueError('维护任务只能是 daily 或 weekly')
        if role not in ('qq', 'research'):
            raise ValueError('未知角色')
        now = to_timestamp(now)
        window = due_windows(now)[kind]
        if kind == 'weekly':
            from memory_curation import tidy_memories
            summary = tidy_memories(self.root, role, now)
            return {'kind': kind, 'role': role, 'window': _iso(window), 'status': 'done',
                    'summary': summary}
        from memory_curation import run_now
        gateway = gateway_factory(role) if gateway_factory is not None else None
        record = run_now(self.root, role=role, gateway=gateway, requested_by='daily',
                         start=self._earlier_window(kind, window), end=now,
                         day=_iso(window)[:10])
        return {'kind': kind, 'role': role, 'window': _iso(window),
                'status': record.get('status'), 'summary': record.get('result') or {}}

    # -- daily --------------------------------------------------------------


    @staticmethod



    # -- weekly -------------------------------------------------------------
    @staticmethod
    def _auto(row) -> bool:
        """Automatic facts only: owner/manual saves are never touched.

        Facts an owner correction conflicted with (``mark_conflict``) are
        excluded; a confirmation turns the row into a manual fact itself.
        """
        return (row['source'] != 'manual' and row['evidence'] not in ('manual','owner_quote')
                and not str(row['scope']).startswith('chat:group:') and not row.get('conflict'))



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


def start_background(host, root=None, *, interval: float = 60.0, embed_interval: float = 300.0, stop=None):
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
    stop = stop if stop is not None else getattr(host, 'stop', None)
    if stop is None:
        stop = threading.Event()
    from memory_curation import ensure_worker
    ensure_worker(root, stop=stop)
    last_embed = 0.0

    def warm_embeddings():
        """Local model worker only (no model API); readiness on the first pass."""
        if not (project_path(root, 'harness', 'agents.json')).is_file():
            return
        try:
            from memory_engine import MemoryEngine
            for role in ('qq', 'research'):
                if (project_path(root, 'data', role, 'memory.db')).is_file():
                    MemoryEngine(root, role).maintenance()
            from doc_store import maintain as maintain_documents
            maintain_documents(root)
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
                # The one automatic daily trigger: it submits the same curation
                # program the tool and the client button use, then returns.
                from memory_curation import tick as curation_tick
                curation_tick(root)
            except Exception:  # noqa: BLE001 - persisted in the curation run record
                pass
            if time.time() - last_embed >= embed_interval:
                last_embed = time.time()
                warm_embeddings()
            stop.wait(interval)
    thread = threading.Thread(target=loop, daemon=True, name='fish-memory-maintenance')
    started['thread'] = thread
    thread.start()
    return thread
