"""The one wake/run ledger and the short claim that starts a turn.

Everything that decides *when* Fish thinks lives behind this file: the unified
receive order (``agent_inputs``), the turn ledger (``agent_runs``) and the pending
wake set (``agent_wakes``). Claiming a turn is one short transaction - the ordered
inputs and the running row change together - so a message that arrives while she is
thinking is registered but cannot slip into the turn already started.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from storage import database

from .. import schema

QUEUED = 'queued'
RUNNING = 'running'
COMPLETE = 'complete'
SILENT = 'silent'
FAILED = 'failed'
CANCELLED = 'cancelled'
MERGED = 'merged'

OPEN_STATES = (QUEUED, RUNNING)


class AgentState:
    """One writer for the agent's own state; callers use these methods, not SQL."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = schema.ensure(self.root)

    # ---------------------------------------------------------------- inputs
    def record_input(self, *, reference, kind='message', source, conversation='', message='',
                     place='', text='', payload=None):
        """Register one received event in the unified order; a repeat returns the first row.

        The original record is already saved by its own adapter. This row keeps only
        the reference, the place it came from and the order it arrived in, so the same
        source is never counted twice.
        """
        now = time.time()
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM agent_inputs WHERE reference=?', (str(reference),)).fetchone()
            if row is not None:
                return dict(row)
            db.execute('INSERT INTO agent_inputs(reference,kind,source,conversation,message,place,text,payload,received)'
                       ' VALUES(?,?,?,?,?,?,?,?,?)',
                       (str(reference), kind, source, str(conversation), str(message), place,
                        str(text)[:20000], body, now))
            row = db.execute('SELECT * FROM agent_inputs WHERE reference=?', (str(reference),)).fetchone()
        return dict(row)

    def inputs_for(self, run_id):
        with database(self.path) as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM agent_inputs WHERE run=? ORDER BY seq', (run_id,))]

    def finish_inputs(self, references, *, considered, cancelled=False):
        """Record what actually happened to each claimed input; nothing is implied."""
        if not references:
            return
        with database(self.path) as db:
            for reference in references:
                db.execute('UPDATE agent_inputs SET considered=?,cancelled=? WHERE reference=?',
                           (1 if considered else 0, 1 if cancelled else 0, str(reference)))

    def cancel_conversation(self, conversation):
        """Drop queued turns of one conversation and flag their inputs as cancelled."""
        now = time.time()
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            rows = [dict(row) for row in db.execute(
                "SELECT id,status,request FROM agent_runs WHERE conversation=? AND status IN ('queued','running')",
                (str(conversation),))]
            if not rows:
                return {'queued': 0, 'running': [], 'requests': []}
            queued = [row for row in rows if row['status'] == QUEUED]
            running = [row['id'] for row in rows if row['status'] == RUNNING]
            for row in queued:
                db.execute("UPDATE agent_runs SET status=?,finished=?,error=?,error_kind=? WHERE id=?",
                           (CANCELLED, now, '主人取消了这次提交，未开始生成', 'InterruptedError', row['id']))
                db.execute('UPDATE agent_inputs SET cancelled=1 WHERE run=?', (row['id'],))
            # Unclaimed inputs of this conversation belong to a submission the owner
            # has just cancelled; they must not start a later turn on their own.
            db.execute("UPDATE agent_inputs SET cancelled=1 WHERE conversation=? AND run=''", (str(conversation),))
        return {'queued': len(queued), 'running': running, 'requests': [row['request'] for row in rows if row['request']]}

    def pending_inputs(self, *, source=None):
        with database(self.path) as db:
            if source is None:
                return int(db.execute("SELECT COUNT(*) FROM agent_inputs WHERE run='' AND cancelled=0").fetchone()[0])
            return int(db.execute("SELECT COUNT(*) FROM agent_inputs WHERE run='' AND cancelled=0 AND source=?",
                                  (source,)).fetchone()[0])

    # ------------------------------------------------------------------ runs
    def create_run(self, *, kind, source, conversation='', request='', input_reference=''):
        run_id = uuid.uuid4().hex
        now = time.time()
        with database(self.path) as db:
            db.execute('INSERT INTO agent_runs(id,kind,source,conversation,request,status,created,inputs)'
                       ' VALUES(?,?,?,?,?,?,?,?)',
                       (run_id, kind, source, str(conversation), str(request), QUEUED, now,
                        json.dumps([input_reference] if input_reference else [], ensure_ascii=False)))
        return run_id

    def run(self, run_id):
        """The real state of one turn; a merged turn answers with the run that covered it."""
        with database(self.path) as db:
            row = db.execute('SELECT * FROM agent_runs WHERE id=?', (str(run_id),)).fetchone()
            seen = set()
            while row is not None and row['status'] == MERGED and row['merged_into'] and row['id'] not in seen:
                seen.add(row['id'])
                row = db.execute('SELECT * FROM agent_runs WHERE id=?', (row['merged_into'],)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['elapsed_ms'] = max(0, int(((result['finished'] or time.time()) - (result['started'] or result['created'])) * 1000))
        return result

    def queued_run(self, *, source=None, conversation=None):
        clauses, values = ["status='queued'"], []
        if source is not None:
            clauses.append('source=?'); values.append(source)
        if conversation is not None:
            clauses.append('conversation=?'); values.append(str(conversation))
        with database(self.path) as db:
            row = db.execute('SELECT * FROM agent_runs WHERE ' + ' AND '.join(clauses) + ' ORDER BY created,rowid LIMIT 1',
                             values).fetchone()
        return dict(row) if row else None

    def claim_next(self):
        """Start the oldest queued turn and merge same-place turns waiting behind it.

        Returns ``(run, inputs)`` or ``None``. The claim records the turn as running
        and takes every input already registered for that place in one commit; inputs
        registered after this commit stay for the next turn (the snapshot rule).
        """
        now = time.time()
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM agent_runs WHERE status='queued' ORDER BY created,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            run = dict(row)
            db.execute("UPDATE agent_runs SET status=?,started=? WHERE id=? AND status='queued'",
                       (RUNNING, now, run['id']))
            others = [dict(item) for item in db.execute(
                "SELECT id FROM agent_runs WHERE status='queued' AND source=? AND conversation=? AND id!=?"
                " ORDER BY created,rowid", (run['source'], run['conversation'], run['id']))]
            for other in others:
                db.execute("UPDATE agent_runs SET status=?,merged_into=?,finished=? WHERE id=?",
                           (MERGED, run['id'], now, other['id']))
            inputs = [dict(item) for item in db.execute(
                "SELECT * FROM agent_inputs WHERE run='' AND cancelled=0 AND source=? AND conversation=? ORDER BY seq",
                (run['source'], run['conversation']))]
            if inputs:
                db.execute("UPDATE agent_inputs SET run=? WHERE run='' AND cancelled=0 AND source=? AND conversation=?",
                           (run['id'], run['source'], run['conversation']))
                db.execute('UPDATE agent_runs SET inputs=? WHERE id=?',
                           (json.dumps([item['reference'] for item in inputs], ensure_ascii=False), run['id']))
            run.update(status=RUNNING, started=now,
                       inputs=[item['reference'] for item in inputs])
        return run, inputs

    def finish_run(self, run_id, status, *, error='', error_kind='', result=None):
        now = time.time()
        body = json.dumps(result, ensure_ascii=False, default=str) if result is not None else ''
        with database(self.path) as db:
            db.execute('UPDATE agent_runs SET status=?,finished=?,error=?,error_kind=?,result=? WHERE id=?',
                       (status, now, str(error)[:500], error_kind, body, str(run_id)))

    def fail_stale(self):
        """A stopped host leaves no turn pretending to run; records and inputs stay readable."""
        now = time.time()
        with database(self.path) as db:
            return int(db.execute(
                "UPDATE agent_runs SET status=?,finished=?,error=?,error_kind=? WHERE status IN ('queued','running')",
                (FAILED, now, '服务已重启，这次提交没有完成；原记录保留', 'InterruptedError')).rowcount)


def receive_reference(source, message):
    """The reference of one original record, kept in that store's own numbering."""
    return str(source) + ':' + str(message)
