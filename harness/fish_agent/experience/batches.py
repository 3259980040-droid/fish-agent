"""Consecutive 3-second file batches, owned by the common experience.

Files are grouped by fixed three-second windows from the moment the first file of a
round is registered. A new file never extends a window, a file exactly on a boundary
belongs to the next one, and a long idle gap starts a new round instead of inventing
empty batches. A sealed batch is handed to the next turn's materials as a whole, with
each item keeping its own sender/place/time/reference order.

Registration time decides the batch; download/parse time only updates each
resource's own state. Nothing here starts a thought: the batch only supplies the
references a turn was already going to use.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from storage import database

from .. import schema

WINDOW_S = 3.0
# A gap of more than one whole window after the last used window starts a new round.
MAX_WINDOWS_GAP = 2


class BatchLog:
    def __init__(self, root):
        self.root = Path(root)
        self.path = schema.ensure(self.root)
        self.db_path = self.root / 'data/qq/attachment.db'

    # ------------------------------------------------------------- registration
    def register(self, *, place, reference, resource='', received=None):
        """Add one registered file to its place's fixed window; returns the batch id."""
        now = time.time() if received is None else float(received)
        place = str(place)
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM agent_batch_places WHERE place=?', (place,)).fetchone()
            if row is None:
                anchor, last = now, -1
            else:
                anchor, last = float(row['anchor']), int(row['last_index'])
            index = int((now - anchor) // WINDOW_S) if now >= anchor else 0
            if index < 0 or index > last + MAX_WINDOWS_GAP:
                # A long idle gap is a new reception round, not empty batches.
                anchor, index = now, 0
            opens = anchor + index * WINDOW_S
            closes = opens + WINDOW_S
            batch_id = 'batch-' + uuid.uuid5(uuid.NAMESPACE_URL, '%s|%s|%s' % (place, anchor, index)).hex
            db.execute('INSERT OR IGNORE INTO agent_batches(id,place,anchor,window_index,opens,closes,state,run) '
                       "VALUES(?,?,?,?,?,?,'open','')", (batch_id, place, anchor, index, opens, closes))
            seq = int(db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM agent_batch_items WHERE batch=?',
                                 (batch_id,)).fetchone()[0])
            db.execute('INSERT OR IGNORE INTO agent_batch_items(batch,seq,reference,resource,received,state) '
                       "VALUES(?,?,?,?,?,'registered')", (batch_id, seq, str(reference), str(resource), now))
            db.execute('INSERT INTO agent_batch_places(place,anchor,last_index) VALUES(?,?,?) '
                       'ON CONFLICT(place) DO UPDATE SET anchor=excluded.anchor,last_index=excluded.last_index',
                       (place, anchor, index))
        return batch_id

    # ---------------------------------------------------------------- sealing
    def seal_due(self, place, now=None):
        now = time.time() if now is None else float(now)
        with database(self.path) as db:
            rows = [dict(r) for r in db.execute(
                "SELECT id FROM agent_batches WHERE place=? AND state='open' AND closes<=?", (str(place), now))]
            for row in rows:
                db.execute("UPDATE agent_batches SET state='sealed' WHERE id=? AND state='open'", (row['id'],))
        return [row['id'] for row in rows]

    def take(self, place, *, run='', wait_for_close=False, window=WINDOW_S + 0.5):
        """Seal what is due and hand the place's sealed batches to one turn.

        ``wait_for_close`` is used only when this turn's own input carries a file: it
        waits at most one window for that batch to close. Ordinary text never waits,
        and a slow download never extends a window.
        """
        place = str(place)
        if wait_for_close:
            open_row = self._latest_open(place)
            if open_row is not None:
                wait = min(window, max(0.0, float(open_row['closes']) - time.time()))
                if wait:
                    time.sleep(wait)
        self.seal_due(place)
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            batches = [dict(r) for r in db.execute(
                "SELECT * FROM agent_batches WHERE place=? AND state='sealed' ORDER BY opens,window_index", (place,))]
            handled = []
            for batch in batches:
                items = [dict(r) for r in db.execute(
                    'SELECT * FROM agent_batch_items WHERE batch=? ORDER BY seq', (batch['id'],))]
                if not items:
                    db.execute("UPDATE agent_batches SET state='consumed' WHERE id=?", (batch['id'],))
                    continue
                db.execute("UPDATE agent_batches SET state='handed',run=? WHERE id=?", (str(run), batch['id']))
                handled.append({'id': batch['id'], 'opens': batch['opens'], 'closes': batch['closes'],
                                'place': batch['place'], 'items': items})
        return handled

    def finish(self, batch_ids, *, ok):
        """A handed batch becomes consumed only after its turn really produced a result."""
        if not batch_ids:
            return
        with database(self.path) as db:
            for batch_id in batch_ids:
                db.execute("UPDATE agent_batches SET state=? WHERE id=? AND state='handed'",
                           ('consumed' if ok else 'sealed', str(batch_id)))
                if ok:
                    # The turn that received the batch really read these files; each item
                    # keeps that fact instead of only the batch's aggregate state.
                    db.execute("UPDATE agent_batch_items SET state='read' WHERE batch=?", (str(batch_id),))
                else:
                    db.execute("UPDATE agent_batch_items SET state='registered' WHERE batch=?", (str(batch_id),))

    def _latest_open(self, place):
        with database(self.path) as db:
            row = db.execute("SELECT * FROM agent_batches WHERE place=? AND state='open' ORDER BY closes DESC LIMIT 1",
                             (str(place),)).fetchone()
        return dict(row) if row else None

    def covered(self, references):
        """Whether these input references were already handed to an earlier turn."""
        refs = [str(value) for value in references if value]
        if not refs:
            return False
        holes = ','.join('?' * len(refs))
        with database(self.path) as db:
            row = db.execute(
                'SELECT 1 FROM agent_batch_items i JOIN agent_batches b ON b.id=i.batch '
                "WHERE i.reference IN (" + holes + ") AND b.state IN ('handed','consumed') LIMIT 1", refs).fetchone()
        return row is not None

    # ------------------------------------------------------------------- reads
    def state(self, place=None, limit=50):
        """What each batch and item actually is: window, state, source reference."""
        with database(self.path) as db:
            if place is None:
                batches = [dict(r) for r in db.execute(
                    'SELECT * FROM agent_batches ORDER BY opens DESC LIMIT ?', (int(limit),))]
            else:
                batches = [dict(r) for r in db.execute(
                    'SELECT * FROM agent_batches WHERE place=? ORDER BY opens DESC LIMIT ?', (str(place), int(limit)))]
            for batch in batches:
                batch['items'] = [dict(r) for r in db.execute(
                    'SELECT * FROM agent_batch_items WHERE batch=? ORDER BY seq', (batch['id'],))]
                batch['opens_text'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(batch['opens']))
                batch['closes_text'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(batch['closes']))
        return batches


def load_segments(root, reference):
    """Segments of one stored message by its reference, or [] when it is gone."""
    source, _, ident = str(reference).partition(':')
    if not ident.isdigit():
        return []
    name = 'desktop-chat.db' if source == 'desktop' else 'qq.db'
    path = Path(root) / 'data/qq' / name
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT segments FROM messages WHERE id=?', (int(ident),)).fetchone()
    except sqlite3.Error:
        return []
    if not row:
        return []
    try:
        value = json.loads(row[0] or '[]')
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def file_segments(segments):
    return [item for item in segments if str(item.get('type') or '') in ('image', 'file', 'video', 'audio')]


def resource_of(segment):
    return str(segment.get('resource_id') or segment.get('path') or segment.get('url')
               or segment.get('file_id') or segment.get('asset_id') or '')
