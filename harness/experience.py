"""Preparation and considered coverage over original records, shared by all entry points."""
import json
import time
import uuid
from pathlib import Path
from storage import database
from desktop_context import DesktopContext


class ExperienceTurn:
    def __init__(self, root, path, conversation, message=None, *, channel='desktop', event=None):
        self.root, self.path = Path(root), Path(path)
        self.conversation, self.message, self.channel = conversation, int(message) if message is not None else None, channel
        self.event = event
        self.id = uuid.uuid4().hex
        self.positions = {}
        self.covered = set()
        self.ledger = self.root / 'data/social/experience.db'
        with database(self.ledger) as db:
            db.execute('CREATE TABLE IF NOT EXISTS considered(reference TEXT PRIMARY KEY,turn TEXT NOT NULL,at REAL NOT NULL,outcome TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS experience_turns(id TEXT PRIMARY KEY,session TEXT NOT NULL,reference TEXT NOT NULL,arrival TEXT NOT NULL,started REAL,first_output REAL,sending REAL,receipt REAL,outcome TEXT NOT NULL,error TEXT NOT NULL)')

    def prepare(self, cancelled=None):
        self.context = DesktopContext(self.root, self.path, channel=self.channel)
        history, source = self.context.history(self.conversation, self.message, None, None, cancelled, event=self.event)
        self.positions = source['positions']
        # Each entry keeps its own source file and conversation: a bare message id is
        # only meaningful inside one store, so they are never mixed into one maximum.
        self.older = [dict(item) for item in source.get('older') or []
                      if isinstance(item, dict) and str(item.get('id', '')).isdigit()]
        self.reference = source['current_reference']
        with database(self.ledger) as db:
            db.execute('INSERT INTO experience_turns(id,session,reference,arrival,started,first_output,sending,receipt,outcome,error) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (self.id, self.session, self.reference, source['current_event']['time'], None, None, None, None, 'running', ''))
        return history, source

    def requested(self, report):
        with database(self.ledger) as db:
            db.execute('UPDATE experience_turns SET started=COALESCE(started,?) WHERE id=?', (time.time(), self.id))
        self.pending = set(report['covered'])
        compacted = [int(ref.split(':')[1]) for item in report['trimmed'] if item['layer'] == 1
                     for ref in item['references'] if ref.startswith(('qq:', 'desktop:')) and ref.split(':')[1].isdigit()]
        # Records kept out of the verbatim window are the summary's business, whether or
        # not the single token cap also trimmed something this turn.
        from context_compaction import enqueue
        if compacted:
            enqueue(self.root, self.path, self.conversation, max(compacted))
        by_place = {}
        for item in getattr(self, 'older', []):
            key = (str(item.get('source') or ''), str(item.get('conversation') or ''))
            by_place[key] = max(by_place.get(key, 0), int(item['id']))
        for (name, conversation), through in by_place.items():
            if not name or not conversation:
                continue
            enqueue(self.root, self.root / 'data/qq' / name, conversation, through)

    def received(self, first_output_at=None):
        self.covered.update(self.pending)
        self.covered.add(self.reference)
        if first_output_at is not None:
            with database(self.ledger) as db:
                db.execute('UPDATE experience_turns SET first_output=COALESCE(first_output,?) WHERE id=?', (first_output_at, self.id))

    def finish(self, outcome, error=''):
        with database(self.ledger) as db:
            db.execute('UPDATE experience_turns SET outcome=CASE WHEN receipt IS NULL THEN ? ELSE outcome END,error=CASE WHEN ?=\'\' THEN error ELSE ? END WHERE id=?', (outcome,str(error)[:500],str(error)[:500],self.id))
            if outcome in ('considered', 'silent'):
                db.executemany('INSERT OR REPLACE INTO considered VALUES(?,?,?,?)',
                               [(ref, self.id, time.time(), outcome) for ref in self.covered])


def was_considered(root, reference, *, session=None):
    """Coverage is shared; consuming a queued wake is local to its actual session."""
    path = Path(root) / 'data/social/experience.db'
    if not path.exists():
        return False
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='considered'").fetchone():
            return False
        if session is None:
            return db.execute('SELECT 1 FROM considered WHERE reference=?', (reference,)).fetchone() is not None
        # A different place may have read the input as background. It must not
        # consume this place's pending wake. Preserve coalescing within a place.
        return db.execute('SELECT 1 FROM considered c JOIN experience_turns t ON t.id=c.turn '
                          'WHERE c.reference=? AND t.session=? UNION ALL '
                          'SELECT 1 FROM experience_turns WHERE reference=? AND session=? '
                          "AND outcome IN ('considered','silent','sent','uncertain') LIMIT 1",
                          (reference, session, reference, session)).fetchone() is not None


def delivery(root, reference, *, sending=False, outcome=None,error=''):
    """Stamp real transport actions, including unknown receipts; never resend here."""
    path = Path(root) / 'data/social/experience.db'
    if not path.exists():
        return
    with database(path) as db:
        if sending:
            db.execute('UPDATE experience_turns SET sending=COALESCE(sending,?) WHERE reference=?', (time.time(), reference))
        if outcome:
            db.execute('UPDATE experience_turns SET receipt=CASE WHEN sending IS NULL THEN receipt ELSE ? END,outcome=?,error=? WHERE reference=?',
                       (time.time(),outcome,str(error)[:500],reference))


def reference_for_session(root,session):
    import sqlite3
    path=Path(root)/'data/social/experience.db'
    if not session or not path.is_file():return ''
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='experience_turns'").fetchone():return ''
        row=db.execute('SELECT reference FROM experience_turns WHERE session=? ORDER BY rowid DESC LIMIT 1',(session,)).fetchone()
    return row[0] if row else ''
