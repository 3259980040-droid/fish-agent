"""Preparation and considered coverage over original records, shared by all entry points.

The turn also carries the one automatic compression trigger: the request budget reports
this turn's measured pressure together with the compression numbers resolved from the
same policy, and this module selects the complete older range. It only queues that
range; the summary itself runs outside the main thinking lock.
"""
from locations import project_path
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
        self.claimed_references = frozenset()
        self.surface = []
        self.ledger = project_path(self.root, 'data/social/experience.db')
        with database(self.ledger) as db:
            db.execute('CREATE TABLE IF NOT EXISTS considered(reference TEXT PRIMARY KEY,turn TEXT NOT NULL,at REAL NOT NULL,outcome TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS experience_turns(id TEXT PRIMARY KEY,session TEXT NOT NULL,reference TEXT NOT NULL,arrival TEXT NOT NULL,started REAL,first_output REAL,sending REAL,receipt REAL,outcome TEXT NOT NULL,error TEXT NOT NULL)')

    def prepare(self, cancelled=None):
        self.context = DesktopContext(self.root, self.path, channel=self.channel)
        history, source = self.context.history(
            self.conversation, self.message, None, None, cancelled, event=self.event,
            claimed_references=self.claimed_references)
        self.positions = source['positions']
        # The ordered original material of this place: compression selects complete older
        # ranges from exactly this order, and each entry keeps its own source and id.
        self.surface = [dict(node) for node in source.get('surface') or []
                        if isinstance(node, dict) and str(node.get('id', '')).isdigit()]
        self.reference = source['current_reference']
        with database(self.ledger) as db:
            db.execute('INSERT INTO experience_turns(id,session,reference,arrival,started,first_output,sending,receipt,outcome,error) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (self.id, self.session, self.reference, source['current_event']['time'], None, None, None, None, 'running', ''))
        return history, source

    def requested(self, report, tokens=None):
        with database(self.ledger) as db:
            db.execute('UPDATE experience_turns SET started=COALESCE(started,?) WHERE id=?', (time.time(), self.id))
        self.pending = set(report.get('covered') or ())
        self._request_compaction(report, tokens)

    def _request_compaction(self, report, tokens):
        """One pressure trigger over the range selection; it queues, it never calls a model.

        The budget report already carries the pressure it measured and the trigger and
        retention numbers resolved from the one policy, so no second configuration read
        and no character or message-count estimate happens here.
        """
        if getattr(self, '_compaction_decided', False):
            return
        pressure = report.get('pressure_tokens')
        threshold = report.get('threshold_tokens')
        retain = report.get('retain_tokens')
        if not isinstance(pressure, (int, float)) or not isinstance(threshold, (int, float)):
            return
        if not report.get('compaction_auto', True):
            return
        if pressure < threshold:
            return
        self._compaction_decided = True
        try:
            from fish_agent.experience.compaction import request, select_regions
            regions = select_regions(self.surface, tokens, int(retain or 0))
        except Exception as exc:
            # A configuration or storage problem must not turn the owner's turn into a
            # failure; it is audited and this range stays unscheduled.
            from governance import Governance
            try:
                Governance(self.root).audit('context.compaction.request',
                                            '自动压缩没有入队，本轮对话继续', str(exc)[:200])
            except Exception:
                pass
            return
        queued = []
        for region in regions:
            # Each region belongs to its own original store and conversation: its
            # progress must not be written under the asking place's database.
            try:
                queued.append(request(self.root, source=region['source'],
                                      conversation=region['conversation'] or self.conversation,
                                      through=region['through'], pressure=int(pressure),
                                      threshold=int(threshold), retain=int(retain or 0),
                                      reason='pressure'))
            except Exception as exc:
                from governance import Governance
                try:
                    Governance(self.root).audit('context.compaction.request',
                                                '自动压缩没有入队，本轮对话继续', str(exc)[:200])
                except Exception:
                    pass
        self.compaction_request = queued

    def received(self, first_output_at=None):
        # A budget report describes what was visible, including background and
        # late arrivals. Only the fixed claim is eligible to settle input work.
        self.covered.update(self.pending.intersection(self.claimed_references))
        self.covered.add(self.reference)
        if first_output_at is not None:
            with database(self.ledger) as db:
                db.execute('UPDATE experience_turns SET first_output=COALESCE(first_output,?) WHERE id=?', (first_output_at, self.id))

    def finish(self, outcome, error=''):
        with database(self.ledger) as db:
            db.execute('UPDATE experience_turns SET outcome=CASE WHEN receipt IS NULL THEN ? ELSE outcome END,error=CASE WHEN ?=\'\' THEN error ELSE ? END WHERE id=?', (outcome,str(error)[:500],str(error)[:500],self.id))
            if outcome in ('considered', 'silent'):
                actual = self.covered.intersection(self.claimed_references | {getattr(self, 'reference', '')})
                db.executemany('INSERT OR REPLACE INTO considered VALUES(?,?,?,?)',
                               [(ref, self.id, time.time(), outcome) for ref in actual])


def was_considered(root, reference, *, session=None):
    """Coverage is shared; consuming a queued wake is local to its actual session."""
    path = project_path(Path(root), 'data/social/experience.db')
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
    path = project_path(Path(root), 'data/social/experience.db')
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
    path=project_path(Path(root), 'data/social/experience.db')
    if not session or not path.is_file():return ''
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='experience_turns'").fetchone():return ''
        row=db.execute('SELECT reference FROM experience_turns WHERE session=? ORDER BY rowid DESC LIMIT 1',(session,)).fetchone()
    return row[0] if row else ''
