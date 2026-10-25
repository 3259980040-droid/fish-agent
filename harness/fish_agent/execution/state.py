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

    def cancel_conversation(self, conversation=None):
        """Drop queued turns (of one conversation, or all) and flag their inputs.

        ``conversation=None`` is the owner's explicit global stop: every place's
        queued work and the running turn are named in the result so the caller can
        stop what it can actually reach.
        """
        now = time.time()
        where, values = '', []
        if conversation is not None:
            where, values = ' AND conversation=?', [str(conversation)]
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            rows = [dict(row) for row in db.execute(
                "SELECT id,status,request,conversation FROM agent_runs WHERE status IN ('queued','running')" + where,
                values)]
            if not rows:
                return {'queued': 0, 'running': [], 'requests': []}
            queued = [row for row in rows if row['status'] == QUEUED]
            running = [row['id'] for row in rows if row['status'] == RUNNING]
            for row in queued:
                db.execute("UPDATE agent_runs SET status=?,finished=?,error=?,error_kind=? WHERE id=?",
                           (CANCELLED, now, '主人取消了这次提交，未开始生成', 'InterruptedError', row['id']))
                db.execute('UPDATE agent_inputs SET cancelled=1 WHERE run=?', (row['id'],))
            # Unclaimed inputs of a cancelled submission must not start a later turn.
            if conversation is None:
                db.execute("UPDATE agent_inputs SET cancelled=1 WHERE run=''")
            else:
                db.execute("UPDATE agent_inputs SET cancelled=1 WHERE conversation=? AND run=''", (str(conversation),))
        return {'queued': len(queued), 'running': running, 'requests': [row['request'] for row in rows if row['request']]}

    def pending_inputs(self, *, source=None, conversation=None):
        clauses, values = ["run=''", 'cancelled=0'], []
        if source is not None:
            clauses.append('source=?'); values.append(source)
        if conversation is not None:
            clauses.append('conversation=?'); values.append(str(conversation))
        with database(self.path) as db:
            return int(db.execute('SELECT COUNT(*) FROM agent_inputs WHERE ' + ' AND '.join(clauses),
                                  values).fetchone()[0])

    # ------------------------------------------------------------------ runs
    def create_run(self, *, kind, source, conversation='', request='', input_reference='',
                   reason='', place=''):
        run_id = uuid.uuid4().hex
        now = time.time()
        with database(self.path) as db:
            db.execute('INSERT INTO agent_runs(id,kind,source,conversation,request,status,created,inputs,reason,place)'
                       ' VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (run_id, kind, source, str(conversation), str(request), QUEUED, now,
                        json.dumps([input_reference] if input_reference else [], ensure_ascii=False),
                        str(reason)[:500], str(place)))
        return run_id

    # ------------------------------------------------------------- agent clock
    def get_state(self, key, default=''):
        with database(self.path) as db:
            row = db.execute('SELECT value FROM agent_state WHERE key=?', (str(key),)).fetchone()
        return row['value'] if row is not None else default

    def set_state(self, key, value):
        with database(self.path) as db:
            db.execute('INSERT INTO agent_state(key,value,updated) VALUES(?,?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated=excluded.updated',
                       (str(key), str(value), time.time()))

    def queued_or_running(self):
        with database(self.path) as db:
            return db.execute("SELECT 1 FROM agent_runs WHERE status IN ('queued','running') LIMIT 1").fetchone() is not None

    def latest_place(self):
        with database(self.path) as db:
            row = db.execute('SELECT source,conversation,place FROM agent_inputs ORDER BY seq DESC LIMIT 1').fetchone()
        return dict(row) if row is not None else None

    def run(self, run_id, *, resolve_merged=True):
        """The real state of one turn; a merged turn answers with the run that covered it."""
        with database(self.path) as db:
            row = db.execute('SELECT * FROM agent_runs WHERE id=?', (str(run_id),)).fetchone()
            seen = set()
            while resolve_merged and row is not None and row['status'] == MERGED and row['merged_into'] and row['id'] not in seen:
                seen.add(row['id'])
                row = db.execute('SELECT * FROM agent_runs WHERE id=?', (row['merged_into'],)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['elapsed_ms'] = max(0, int(((result['finished'] or time.time()) - (result['started'] or result['created'])) * 1000))
        return result

    def list_runs(self, *, limit=20, cursor=''):
        """Stable pages of the original ledger, including quiet and merged rows."""
        import base64
        import math
        limit = max(1, min(50, int(limit)))
        where, args = '', []
        if cursor:
            try:
                created, identifier = json.loads(base64.urlsafe_b64decode(str(cursor)).decode())
                if isinstance(created, bool) or not isinstance(created, (float, int)) or not math.isfinite(created) \
                        or not isinstance(identifier, str) or not identifier:
                    raise ValueError('invalid cursor')
            except Exception as exc:
                raise ValueError('回合分页位置无效') from exc
            where, args = ' WHERE (created<? OR (created=? AND id<?))', [created, created, identifier]
        with database(self.path) as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM agent_runs' + where +
                    ' ORDER BY created DESC,id DESC LIMIT ?', (*args, limit + 1))]
        page, following = rows[:limit], None
        if len(rows) > limit:
            last = page[-1]
            following = base64.urlsafe_b64encode(json.dumps([last['created'], last['id']]).encode()).decode()
        return page, following

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
        """Claim the next eligible turn and only its adjacent ordinary materials.

        Returns ``(run, inputs)`` or ``None``. The claim records the turn as running.
        Trigger references retain reception order, including when submitters finish
        registering their wakes in a different order. Untriggered group chatter does
        not block an immediate wake. A different queued place or an independent plan
        ends a merge; later input remains for its own turn.
        """
        now = time.time()
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            queued = [dict(row) for row in db.execute(
                "SELECT * FROM agent_runs WHERE status='queued' ORDER BY created,rowid")]
            if not queued:
                return None
            pending = [dict(row) for row in db.execute(
                "SELECT * FROM agent_inputs WHERE run='' AND cancelled=0 ORDER BY seq")]
            by_reference = {item['reference']: item for item in pending}
            next_seq = max((item['seq'] for item in pending), default=0) + 1

            def trigger(item):
                references = json.loads(item['inputs'] or '[]')
                return by_reference.get(references[0]) if references else None

            def arrival_order(item):
                event = trigger(item)
                if event:
                    return event['seq'], item['created']
                # A clock has no input reference; retain its actual position among
                # arrivals rather than moving every later message ahead of it.
                before = max((event['seq'] for event in pending if event['received'] <= item['created']), default=0)
                return before + .5, item['created']

            queued.sort(key=arrival_order)
            run = queued[0]
            selected = [run]
            boundary = next_seq
            ordinary = run['kind'] == 'message' and trigger(run) is not None
            if ordinary:
                for other in queued[1:]:
                    if (other['kind'] != 'message' or other['source'] != run['source']
                            or other['conversation'] != run['conversation']):
                        boundary = arrival_order(other)[0]
                        break
                    selected.append(other)
            if ordinary:
                inputs = [item for item in pending if item['kind'] == 'message'
                          and item['source'] == run['source']
                          and item['conversation'] == run['conversation'] and item['seq'] < boundary]
            else:
                # An occurrence is its own commission. Never let the last plan
                # payload replace other commissions addressed to the same target.
                references = set(json.loads(run['inputs'] or '[]'))
                inputs = [item for item in pending if item['reference'] in references]
            db.execute("UPDATE agent_runs SET status=?,started=? WHERE id=? AND status='queued'",
                       (RUNNING, now, run['id']))
            for other in selected[1:]:
                db.execute("UPDATE agent_runs SET status=?,merged_into=?,finished=? WHERE id=?",
                           (MERGED, run['id'], now, other['id']))
            db.executemany("UPDATE agent_inputs SET run=? WHERE seq=? AND run='' AND cancelled=0",
                           [(run['id'], item['seq']) for item in inputs])
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

    def fail_stale(self,*,before=None):
        """A stopped host leaves no turn pretending to run; records and inputs stay readable."""
        now = time.time()
        with database(self.path) as db:
            return int(db.execute(
                "UPDATE agent_runs SET status=?,finished=?,error=?,error_kind=? WHERE status IN ('queued','running') AND created<?",
                (FAILED, now, '服务已重启，这次提交没有完成；原记录保留', 'InterruptedError',float(before) if before is not None else now)).rowcount)


def receive_reference(source, message):
    """The reference of one original record, kept in that store's own numbering."""
    return str(source) + ':' + str(message)
