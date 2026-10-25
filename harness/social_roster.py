"""Per-group QQ member roster plus an internal relationship-state database.

Standard library only. All data lives in ``<root>/data/social/people.db``.
Internal numeric state (affinity, events, deltas) must never be handed to a
model: only :meth:`SocialRoster.public_profile` and
:meth:`SocialRoster.behavior` are model-safe projections.
"""

from __future__ import annotations
from locations import project_path

import json
import math
import re
import sqlite3
import time
import uuid
from pathlib import Path

__all__ = ["SocialRoster", "RosterError"]

_GROUP_RE = re.compile(r"[0-9]{5,15}")
_USER_RE = re.compile(r"[0-9]{5,15}")
_KIND_DELTA = {"helpful": 2, "warm": 1, "hostile": -2, "boundary": -1, "neutral": 0}
_DAY_SECONDS = 86400
_COOLDOWN_SECONDS = 600
_DAILY_CAP = 6
_INITIAL_AFFINITY = 30
_MAX_MEMBERS = 10000

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        group_id TEXT NOT NULL,
        auto_affinity INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS members (
        group_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        nickname TEXT NOT NULL DEFAULT '',
        card TEXT NOT NULL DEFAULT '',
        present INTEGER NOT NULL DEFAULT 1,
        affinity INTEGER NOT NULL,
        locked INTEGER NOT NULL DEFAULT 0,
        preferences TEXT NOT NULL DEFAULT '',
        aliases TEXT NOT NULL DEFAULT '[]',
        created REAL NOT NULL,
        updated REAL NOT NULL,
        PRIMARY KEY (group_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        event_id TEXT NOT NULL,
        group_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        delta INTEGER NOT NULL,
        message_id TEXT NOT NULL DEFAULT '',
        auto INTEGER NOT NULL,
        undone INTEGER NOT NULL DEFAULT 0,
        undo_of TEXT,
        created REAL NOT NULL,
        UNIQUE (group_id, event_id)
    )""",
    "CREATE INDEX IF NOT EXISTS events_member ON events (group_id, user_id, created)",
)

_MEMBER_COLUMNS = (
    "user_id, nickname, card, present, affinity, locked, preferences, aliases, updated"
)


class RosterError(ValueError):
    """Validation / scope / state error raised to the caller."""


def _is_digits(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _text(value, field, limit):
    if not isinstance(value, str):
        raise RosterError(f"{field} must be a string")
    if len(value) > limit:
        raise RosterError(f"{field} must be at most {limit} characters")
    return value


def _require_group(group_id):
    if not _is_digits(group_id, _GROUP_RE):
        raise RosterError("group_id must be a 5-15 digit string")
    return group_id


def _require_user(user_id):
    if not _is_digits(user_id, _USER_RE):
        raise RosterError("user_id must be a 5-15 digit string")
    return user_id


def _row_to_member(row):
    return {
        "user_id": row["user_id"],
        "nickname": row["nickname"],
        "card": row["card"],
        "present": bool(row["present"]),
        "affinity": int(row["affinity"]),
        "locked": bool(row["locked"]),
        "preferences": row["preferences"],
        "aliases": list(json.loads(row["aliases"])),
        "updated": float(row["updated"]),
    }


class SocialRoster:
    """Single-group roster with internal relationship state."""

    def __init__(self, root):
        self.root = Path(root)
        self.db_path = project_path(self.root, "data", "social", "people.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._setup()

    # ------------------------------------------------------------------ db
    def _setup(self):
        conn = self._connect()
        try:
            with conn:
                for statement in _SCHEMA:
                    conn.execute(statement)
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _tx(self, fn, immediate=True):
        """Run ``fn(conn)`` inside one transaction; commit or roll back."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                result = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result
        finally:
            conn.close()

    def _selected(self, conn):
        row = conn.execute("SELECT group_id, auto_affinity FROM settings WHERE id = 1").fetchone()
        if row is None:
            raise RosterError("no group configured")
        return row["group_id"], bool(row["auto_affinity"])

    def _scope(self, conn, group_id):
        """Validate group id and that it is the selected group."""
        _require_group(group_id)
        selected, auto = self._selected(conn)
        if group_id != selected:
            raise RosterError("group is not the selected group")
        return selected, auto

    @staticmethod
    def _load(conn, group_id, user_id):
        return conn.execute(
            "SELECT " + _MEMBER_COLUMNS + " FROM members WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()

    @classmethod
    def _require_member(cls, conn, group_id, user_id):
        row = cls._load(conn, group_id, user_id)
        if row is None:
            raise RosterError("unknown member")
        return row

    # -------------------------------------------------------------- settings
    def settings(self):
        def run(conn):
            row = conn.execute(
                "SELECT group_id, auto_affinity FROM settings WHERE id = 1"
            ).fetchone()
            if row is None:
                return {"group_id": "", "auto_affinity": True}
            return {"group_id": row["group_id"], "auto_affinity": bool(row["auto_affinity"])}

        return self._tx(run, immediate=False)

    def configure(self, group_id, auto_affinity=True):
        _require_group(group_id)
        if not isinstance(auto_affinity, bool):
            raise RosterError("auto_affinity must be a bool")

        def run(conn):
            conn.execute(
                "INSERT INTO settings (id, group_id, auto_affinity) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET group_id = excluded.group_id, "
                "auto_affinity = excluded.auto_affinity",
                (group_id, int(auto_affinity)),
            )
            return {"group_id": group_id, "auto_affinity": auto_affinity}

        return self._tx(run)

    # --------------------------------------------------------------- roster
    def sync_members(self, group_id, members):
        _require_group(group_id)
        if not isinstance(members, (list, tuple)) or not members:
            raise RosterError("members must be a nonempty list")
        if len(members) > _MAX_MEMBERS:
            raise RosterError(f"members must have at most {_MAX_MEMBERS} rows")
        # Pass 1: validate the entire list (nothing is written on failure).
        cleaned, seen = [], set()
        for entry in members:
            if not isinstance(entry, dict):
                raise RosterError("each member must be a dictionary")
            user_id = _require_user(entry.get("user_id"))
            nickname = _text(entry.get("nickname", ""), "nickname", 200)
            card = _text(entry.get("card", ""), "card", 200)
            if user_id in seen:
                raise RosterError(f"duplicate user_id {user_id}")
            seen.add(user_id)
            cleaned.append((user_id, nickname, card))

        def run(conn):
            self._scope(conn, group_id)
            now = time.time()
            existing = {
                r["user_id"]: r["present"]
                for r in conn.execute(
                    "SELECT user_id, present FROM members WHERE group_id = ?", (group_id,)
                )
            }
            added = 0
            for user_id, nickname, card in cleaned:
                if user_id not in existing:
                    conn.execute(
                        "INSERT INTO members (group_id, user_id, nickname, card, present, "
                        "affinity, locked, preferences, aliases, created, updated) "
                        "VALUES (?, ?, ?, ?, 1, ?, 0, '', '[]', ?, ?)",
                        (group_id, user_id, nickname, card, _INITIAL_AFFINITY, now, now),
                    )
                    added += 1
                else:
                    conn.execute(
                        "UPDATE members SET nickname = ?, card = ?, present = 1, updated = ? "
                        "WHERE group_id = ? AND user_id = ?",
                        (nickname, card, now, group_id, user_id),
                    )
            placeholders = ",".join("?" for _ in seen)
            left = conn.execute(
                "SELECT COUNT(*) FROM members WHERE group_id = ? AND present = 1 "
                f"AND user_id NOT IN ({placeholders})",
                (group_id, *seen),
            ).fetchone()[0]
            conn.execute(
                "UPDATE members SET present = 0, updated = ? WHERE group_id = ? AND present = 1 "
                f"AND user_id NOT IN ({placeholders})",
                (now, group_id, *seen),
            )
            return {"total": len(cleaned), "added": added, "left": left}

        return self._tx(run)

    def members(self, group_id, query=""):
        _require_group(group_id)
        if not isinstance(query, str):
            raise RosterError("query must be a string")

        def run(conn):
            self._scope(conn, group_id)
            sql = "SELECT " + _MEMBER_COLUMNS + " FROM members WHERE group_id = ?"
            params = [group_id]
            if query:
                sql += (
                    " AND (instr(user_id, ?) > 0 OR instr(nickname, ?) > 0"
                    " OR instr(card, ?) > 0)"
                )
                params += [query, query, query]
            sql += " ORDER BY user_id LIMIT ?"
            params.append(_MAX_MEMBERS)
            return [_row_to_member(r) for r in conn.execute(sql, params)]

        return self._tx(run, immediate=False)

    def member(self, group_id, user_id):
        _require_group(group_id)
        _require_user(user_id)

        def run(conn):
            self._scope(conn, group_id)
            return _row_to_member(self._require_member(conn, group_id, user_id))

        return self._tx(run, immediate=False)

    def save_member(self, group_id, user_id, *, affinity, locked, preferences, aliases):
        _require_group(group_id)
        _require_user(user_id)
        if not isinstance(affinity, int) or isinstance(affinity, bool) or not 0 <= affinity <= 100:
            raise RosterError("affinity must be an int in 0..100")
        if not isinstance(locked, bool):
            raise RosterError("locked must be a bool")
        if not isinstance(preferences, str) or len(preferences) > 2000:
            raise RosterError("preferences must be a string of at most 2000 characters")
        if not isinstance(aliases, (list, tuple)) or len(aliases) > 20:
            raise RosterError("aliases must be a list of at most 20 strings")
        for alias in aliases:
            if not isinstance(alias, str) or not alias or len(alias) > 80:
                raise RosterError("each alias must be a nonempty string of at most 80 chars")
        alias_json = json.dumps(list(aliases), ensure_ascii=False)

        def run(conn):
            self._scope(conn, group_id)
            row = self._require_member(conn, group_id, user_id)
            now = time.time()
            conn.execute(
                "UPDATE members SET affinity = ?, locked = ?, preferences = ?, aliases = ?, "
                "updated = ? WHERE group_id = ? AND user_id = ?",
                (affinity, int(locked), preferences, alias_json, now, group_id, user_id),
            )
            if locked != bool(row['locked']):
                self._insert_event(conn, group_id, user_id, kind='manual_lock', delta=0, auto=False, now=now)
            if affinity != int(row['affinity']):
                self._insert_event(conn, group_id, user_id, kind='manual',
                                   delta=affinity-int(row['affinity']), auto=False, now=now)
            return _row_to_member(self._load(conn, group_id, user_id))

        return self._tx(run)

    # --------------------------------------------------------------- events
    @staticmethod
    def _insert_event(conn, group_id, user_id, *, kind, delta, message_id="", auto=False,
                      now=None, undo_of=None, source_id=None, undone=False):
        return conn.execute(
            "INSERT INTO events (id, event_id, group_id, user_id, kind, delta, message_id, "
            "auto, undone, undo_of, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), source_id or str(uuid.uuid4()), group_id, user_id, kind, delta,
             message_id, int(auto), int(undone), undo_of, time.time() if now is None else now),
        ).lastrowid

    def _auto_state(self, conn, group_id, user_id, now):
        """Return (last_nonzero_auto_created, absolute_auto_sum_in_window)."""
        last = conn.execute(
            "SELECT MAX(created) FROM events WHERE group_id = ? AND user_id = ? "
            "AND auto = 1 AND delta != 0 ORDER BY created DESC",
            (group_id, user_id),
        ).fetchone()[0]
        window_start = now - _DAY_SECONDS
        total = conn.execute(
            "SELECT COALESCE(SUM(ABS(delta)), 0) FROM events WHERE group_id = ? AND user_id = ? "
            "AND auto = 1 AND delta != 0 AND created > ? AND created <= ?",
            (group_id, user_id, window_start, now),
        ).fetchone()[0]
        return last, int(total)

    def apply_event(self, group_id, user_id, *, event_id, kind, message_id, now=None):
        _require_group(group_id)
        _require_user(user_id)
        _text(event_id, "event_id", 200)
        if not event_id:
            raise RosterError("event_id must be nonempty")
        if not isinstance(kind, str) or kind not in _KIND_DELTA:
            raise RosterError("kind must be one of " + ", ".join(_KIND_DELTA))
        _text(message_id, "message_id", 200)
        if now is not None and (
            not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now) or now < 0
        ):
            raise RosterError("now must be a number")
        ts = float(now) if now is not None else time.time()

        def run(conn):
            _, auto = self._scope(conn, group_id)
            if conn.execute(
                "SELECT 1 FROM events WHERE group_id = ? AND event_id = ?", (group_id, event_id)
            ).fetchone():
                # Duplicate source event: no mutation, report current state.
                dup = self._load(conn, group_id, user_id)
                current = dup["affinity"] if dup is not None else _INITIAL_AFFINITY
                return {"applied": False, "delta": 0, "affinity": current}

            row = self._load(conn, group_id, user_id)
            delta = 0
            if row is not None and row['present'] and auto and not row["locked"]:
                score = int(row["affinity"])
                delta = _KIND_DELTA[kind]
                last, used = self._auto_state(conn, group_id, user_id, ts)
                if delta and last is not None and ts - last < _COOLDOWN_SECONDS:
                    delta = 0
                allowance = max(0, _DAILY_CAP - used)
                delta = max(-allowance, min(allowance, delta))
                new_score = max(0, min(100, score + delta))
                delta = new_score - score
                if delta:
                    conn.execute(
                        "UPDATE members SET affinity = ?, updated = ? "
                        "WHERE group_id = ? AND user_id = ?",
                        (new_score, time.time(), group_id, user_id),
                    )
                    score = new_score
            else:
                score = None if row is None else int(row["affinity"])

            self._insert_event(
                conn, group_id, user_id, kind=kind, delta=delta, message_id=message_id,
                auto=row is not None, now=ts, source_id=event_id,
            )
            affinity = score if score is not None else _INITIAL_AFFINITY
            return {"applied": delta != 0, "delta": delta, "affinity": affinity}

        return self._tx(run)

    def events(self, group_id, user_id, limit=30):
        _require_group(group_id)
        _require_user(user_id)
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise RosterError("limit must be an int")
        limit = max(1, min(100, limit))

        def run(conn):
            self._scope(conn, group_id)
            self._require_member(conn, group_id, user_id)
            rows = conn.execute(
                "SELECT id, kind, delta, message_id, created, undone FROM events "
                "WHERE group_id = ? AND user_id = ? ORDER BY seq DESC LIMIT ?",
                (group_id, user_id, limit),
            )
            return [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "delta": int(r["delta"]),
                    "message_id": r["message_id"],
                    "created": float(r["created"]),
                    "undone": bool(r["undone"]),
                }
                for r in rows
            ]

        return self._tx(run, immediate=False)

    def undo_event(self, group_id, event_id):
        _require_group(group_id)
        _text(event_id, "event_id", 200)
        if not event_id:
            raise RosterError("event_id must be nonempty")

        def run(conn):
            self._scope(conn, group_id)
            row = conn.execute(
                "SELECT * FROM events WHERE group_id = ? AND id = ?", (group_id, event_id)
            ).fetchone()
            if row is None:
                raise RosterError("no such event in this group")
            if row["undone"]:
                raise RosterError("event already undone")
            if row["undo_of"] is not None:
                raise RosterError("cannot undo a reversal")
            if row['kind'] == 'manual_lock' or not row['delta']:
                raise RosterError('这项记录没有可撤销的数值变化')
            member_row = self._load(conn, group_id, row["user_id"])
            if member_row is None:
                raise RosterError("member no longer exists")
            now = time.time()
            delta = -int(row["delta"])
            if delta:
                score = max(0, min(100, int(member_row["affinity"]) + delta))
                conn.execute(
                    "UPDATE members SET affinity = ?, updated = ? "
                    "WHERE group_id = ? AND user_id = ?",
                    (score, now, group_id, row["user_id"]),
                )
                self._insert_event(
                    conn, group_id, row["user_id"], kind="undo", delta=score - int(member_row["affinity"]),
                    auto=False, now=now, undo_of=row["id"],
                )
            conn.execute("UPDATE events SET undone = 1 WHERE id = ?", (row["id"],))
            return _row_to_member(self._load(conn, group_id, row["user_id"]))

        return self._tx(run)

    # ----------------------------------------------------- model-safe views
    def observe_sender(self, group_id, sender):
        """Refresh transport names by account; preserve owner aliases/preferences."""
        uid = str(sender.get('id') or '')
        if not _is_digits(str(group_id), _GROUP_RE) or not _is_digits(uid, _USER_RE):
            return False
        def run(conn):
            selected = conn.execute('SELECT group_id FROM settings WHERE id=1').fetchone()
            if not selected or selected['group_id'] != str(group_id):
                return False
            old = self._load(conn, str(group_id), uid)
            # An old transport's display_name cannot distinguish card/nickname.
            nickname = str(sender.get('nickname') if 'nickname' in sender else (old['nickname'] if old else sender.get('display_name') or sender.get('name') or ''))[:80]
            card = str(sender.get('card') if 'card' in sender else (old['card'] if old else ''))[:80]
            now = time.time()
            conn.execute('''INSERT INTO members(group_id,user_id,nickname,card,present,affinity,created,updated)
                VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET
                nickname=excluded.nickname,card=excluded.card,present=1,updated=excluded.updated''',
                (str(group_id), uid, nickname, card, _INITIAL_AFFINITY, now, now))
            return True
        return self._tx(run)

    def public_profile(self, group_id, user_id):
        _require_group(group_id)
        _require_user(user_id)

        def run(conn):
            self._scope(conn, group_id)
            row = self._load(conn, group_id, user_id)
            if row is None:
                return {
                    "user_id": user_id,
                    "nickname": "",
                    "card": "",
                    "aliases": [],
                    "preferences": "",
                }
            # Strict allowlist projection.
            return {
                "user_id": row["user_id"],
                "nickname": row["nickname"],
                "card": row["card"],
                "aliases": list(json.loads(row["aliases"])),
                "preferences": row["preferences"],
            }

        return self._tx(run, immediate=False)

    def behavior(self, group_id, user_id):
        _require_group(group_id)
        _require_user(user_id)

        def run(conn):
            self._scope(conn, group_id)
            row = self._load(conn, group_id, user_id)
            score = _INITIAL_AFFINITY if row is None else int(row["affinity"])
            if score < 40:
                max_chars, active, cooldown = 120, 0.2, 2.0
            elif score < 75:
                max_chars, active, cooldown = 240, 0.5, 1.5
            else:
                max_chars, active, cooldown = 480, 0.8, 1.0
            return {
                "max_chars": max_chars,
                "active_probability": active,
                "cooldown_multiplier": cooldown,
            }

        return self._tx(run, immediate=False)
