"""Memory digests: one short first-person summary of what changed in a period.

Three levels share one meaning: a day, an ISO week, and a calendar month. Every
level reads the same existing records (the owner memories in data/qq/memory.db
and the expression library in data/social/group-language.db); day summaries stay
in the memory_digest table and week/month summaries live in memory_digest_period,
both inside that same memory database, so no new file is created and the memory
tables themselves are untouched.

A period is summarized once and then kept. force=True rewrites the same period (a
redone period replaces its row, it never appends a second one), and a model
failure is stored as a "今天没整理成：原因" record so a page can show what
happened instead of raising into the caller. revise() is the opposite case: the
owner asked for that one paragraph to be rewritten, so its failure is raised to
whoever asked.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date
from pathlib import Path

from storage import database, role_dir
from fts5_index import bigrams, digest as fts_digest, expressions

TABLE = '''CREATE TABLE IF NOT EXISTS memory_digest(
    day TEXT PRIMARY KEY, text TEXT NOT NULL, kinds TEXT NOT NULL,
    counts TEXT NOT NULL, created REAL NOT NULL, model_calls INTEGER NOT NULL)'''
# Week and month summaries: same columns plus the scope they belong to. The day
# table above keeps its own rows so no existing digest has to be migrated.
PERIOD_TABLE = '''CREATE TABLE IF NOT EXISTS memory_digest_period(
    scope TEXT NOT NULL, period TEXT NOT NULL, text TEXT NOT NULL, kinds TEXT NOT NULL,
    counts TEXT NOT NULL, created REAL NOT NULL, model_calls INTEGER NOT NULL,
    PRIMARY KEY(scope, period))'''
# Derived lexical indexes: the digest rows above stay the one stored copy.
DIGEST_FTS = '''CREATE VIRTUAL TABLE IF NOT EXISTS memory_digest_fts
    USING fts5(bigram, digest UNINDEXED, tokenize='unicode61')'''
PERIOD_FTS = '''CREATE VIRTUAL TABLE IF NOT EXISTS memory_digest_period_fts
    USING fts5(bigram, digest UNINDEXED, tokenize='unicode61')'''
SYSTEM = ('把今天新增和改动的记忆、表达写成一段给自己看的状态小结。第一人称，写具体的人和事，'
          '不要罗列条目、不要写客套话或建议。输出 JSON {"digest":"…"}，'
          'digest 是 150 到 400 字的中文段落。')
WEEKLY_SYSTEM = ('把这一周新增和改动的记忆、表达写成一段给自己看的状态小结。第一人称，写具体的人和事，'
                 '突出这一周里最重要的一两件事，不要罗列条目、不要写客套话或建议。'
                 '输出 JSON {"digest":"…"}，digest 是 200 到 500 字的中文段落。')
MONTHLY_SYSTEM = ('把这一个月新增和改动的记忆、表达写成一段给自己看的状态小结。第一人称，写具体的人和事，'
                  '只留下这个月真正重要的变化与关系，不要罗列条目、不要写客套话或建议。'
                  '输出 JSON {"digest":"…"}，digest 是 250 到 600 字的中文段落。')
REVISE_SYSTEM = ('主人指出你写的这段小结里有不对的地方。按主人说的改：只改主人指出的事实，'
                 '其余人物、时间、口气保持原样，不要新增没发生过的事、不要写建议或客套话。'
                 '输出 JSON {"digest":"…"}，digest 是改写后完整的一段中文小结。')
SYSTEMS = {'day': SYSTEM, 'week': WEEKLY_SYSTEM, 'month': MONTHLY_SYSTEM}
NOTHING = '今天没有新增或改动的记忆与表达，没什么可整理的。'
NOTHINGS = {'day': NOTHING,
            'week': '这一周没有新增或改动的记忆与表达，没什么可整理的。',
            'month': '这一个月没有新增或改动的记忆与表达，没什么可整理的。'}
FAILED = '今天没整理成：'
MAX_TEXT = 1200
MATERIAL = 40
MATERIALS = {'day': MATERIAL, 'week': 80, 'month': 120}
SCOPES = ('day', 'week', 'month')
STATUS_LABELS = {'active': '有效', 'candidate': '待核实', 'archived': '已停用'}


def _labels():
    """Kind labels taken from the memory store's own table, never re-invented."""
    from memory_store import _LABELS
    return _LABELS


def _day(day=None):
    """The day being summarized, YYYY-MM-DD, defaulting to today."""
    if not day:
        return time.strftime('%Y-%m-%d')
    text = str(day).strip()
    try:
        time.strptime(text, '%Y-%m-%d')
    except ValueError:
        raise ValueError('日期需为 YYYY-MM-DD') from None
    return text


def _bounds(day):
    """Local-midnight epoch range of one day."""
    start = time.mktime(time.strptime(day, '%Y-%m-%d'))
    return start, start + 86400


def _current(scope):
    """The period happening now, in that scope's own key format."""
    now = time.localtime()
    if scope == 'day':
        return time.strftime('%Y-%m-%d', now)
    if scope == 'week':
        return time.strftime('%G-W%V', now)
    return time.strftime('%Y-%m', now)


def _scope(scope):
    value = str(scope or 'day').strip()
    if value not in SCOPES:
        raise ValueError('小结范围需为 day、week 或 month')
    return value


def _period(scope, period=None):
    """The period key of one scope, validated, defaulting to the current one."""
    scope = _scope(scope)
    text = str(period or '').strip() or _current(scope)
    if scope == 'day':
        return _day(text)
    if scope == 'week':
        if not re.fullmatch(r'\d{4}-W\d{2}', text):
            raise ValueError('周小结需为 YYYY-Www，例如 2024-W23')
        year, week = int(text[:4]), int(text[6:])
        # 53 is only a real week in a long year; fromisocalendar is the authority.
        try:
            date.fromisocalendar(year, week, 1)
        except ValueError:
            raise ValueError('这一周不存在') from None
        return text
    if not re.fullmatch(r'\d{4}-\d{2}', text):
        raise ValueError('月小结需为 YYYY-MM，例如 2024-06')
    if not 1 <= int(text[5:7]) <= 12:
        raise ValueError('月份需在 1 到 12 之间')
    return text


def _period_bounds(scope, period):
    """Local epoch range one period covers: one day, one ISO week, one month."""
    if scope == 'day':
        return _bounds(period)
    if scope == 'week':
        monday = date.fromisocalendar(int(period[:4]), int(period[6:]), 1)
        start = time.mktime((monday.year, monday.month, monday.day, 0, 0, 0, 0, 0, -1))
        return start, start + 7 * 86400
    year, month = int(period[:4]), int(period[5:7])
    start = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
    end = (time.mktime((year + 1, 1, 1, 0, 0, 0, 0, 0, -1)) if month == 12
           else time.mktime((year, month + 1, 1, 0, 0, 0, 0, 0, -1)))
    return start, end


def _memory_path(root):
    return role_dir(Path(root), 'qq') / 'memory.db'


def _language_path(root):
    return Path(root) / 'data/social/group-language.db'


def _readonly(path):
    """A read-only connection, or None when the database is absent or unreadable."""
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _tally(pairs, labels, order=None):
    """Rows of {label, count}, sorted by order first and count second."""
    counts = {}
    for key, value in pairs:
        name = str(key or '')
        counts[name] = counts.get(name, 0) + int(value or 0)
    rows = [{'label': labels.get(key, key), 'count': counts[key]} for key in counts]
    rank = {name: index for index, name in enumerate(order or ())}
    return sorted(rows, key=lambda row: (rank.get(row['label'], len(rank)), -row['count'], row['label']))


def _material(root, day):
    """What changed on this day: updated memories and newly written expressions."""
    start, end = _bounds(day)
    return _material_range(root, start, end, MATERIAL)


def _material_range(root, start, end, limit=MATERIAL):
    """What changed inside one epoch range, capped at limit rows of each kind."""
    labels = _labels()
    with database(_memory_path(root)) as db:
        db.execute(TABLE)
        memories = [dict(row) for row in db.execute(
            'SELECT id,text,category,kind,status,created,updated FROM memories '
            'WHERE updated>=? AND updated<? ORDER BY updated DESC', (start, end))]
    conn = _readonly(_language_path(root))
    expressions = []
    if conn is not None:
        try:
            expressions = [dict(row) for row in conn.execute(
                'SELECT id,kind,trigger,content,status,created FROM entries '
                'WHERE created>=? AND created<? ORDER BY created DESC', (start, end))]
        except sqlite3.Error:
            expressions = []
        finally:
            conn.close()
    return {
        'memories': [{'kind': labels.get(str(row.get('kind') or ''), ''), 'category': row.get('category') or '',
                      'status': STATUS_LABELS.get(str(row.get('status') or ''), ''),
                      'text': str(row.get('text') or '')[:160]} for row in memories[:limit]],
        'expressions': [{'kind': row.get('kind') or '', 'trigger': row.get('trigger') or '',
                         'content': str(row.get('content') or '')[:160]} for row in expressions[:limit]],
        'totals': {'memories': len(memories), 'expressions': len(expressions)},
    }


def _kind_tally(root, day):
    """Memory kinds touched on this day, for the digest header."""
    start, end = _bounds(day)
    return _kind_tally_range(root, start, end)


def _kind_tally_range(root, start, end):
    """Memory kinds touched inside one epoch range."""
    with database(_memory_path(root)) as db:
        db.execute(TABLE)
        rows = db.execute('SELECT kind,COUNT(*) n FROM memories WHERE updated>=? AND updated<? '
                          'GROUP BY kind', (start, end)).fetchall()
    return [(row['kind'], row['n']) for row in rows]


def _text(reply):
    """The model's paragraph, whichever documented key it used."""
    for key in ('digest', 'text', 'summary'):
        value = reply.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:MAX_TEXT]
    raise RuntimeError('模型没有返回小结正文')


def _reason(error):
    return (str(error).strip() or type(error).__name__)[:200]


def _record(day, text, kinds, counts, created, model_calls, scope='day'):
    """One digest as the pages read it; the period key rides in day for both levels.

    start and end are the real epoch range the period covers, so a page can nest
    a week inside the month it begins in without reimplementing ISO weeks.
    """
    try:
        start, end = _period_bounds(scope, day)
    except (TypeError, ValueError):
        start = end = 0.0
    return {'scope': scope, 'day': day, 'text': text, 'kinds': kinds, 'counts': counts,
            'created': created, 'model_calls': model_calls,
            'start': float(start), 'end': float(end)}


def _row(row, scope='day'):
    try:
        kinds = json.loads(row['kinds'] or '[]')
    except ValueError:
        kinds = []
    try:
        counts = json.loads(row['counts'] or '{}')
    except ValueError:
        counts = {}
    period = str(row['day'] if 'day' in row.keys() else row['period'])
    return _record(period, row['text'], kinds if isinstance(kinds, list) else [],
                   counts if isinstance(counts, dict) else {},
                   float(row['created'] or 0), int(row['model_calls'] or 0), scope)


def _stored(root, scope, period):
    """The row one period already has, or None."""
    path = _memory_path(root)
    with database(path) as db:
        db.execute(TABLE); db.execute(PERIOD_TABLE)
        if scope == 'day':
            row = db.execute('SELECT * FROM memory_digest WHERE day=?', (period,)).fetchone()
        else:
            row = db.execute('SELECT * FROM memory_digest_period WHERE scope=? AND period=?',
                             (scope, period)).fetchone()
    return None if row is None else _row(dict(row), scope)


def _fts_haystack(text, labels):
    return ' '.join([str(text or '')] + [str(label or '') for label in (labels or [])])


def _fts_put(db, table, rowid, text, labels):
    value = bigrams(_fts_haystack(text, labels))
    db.execute('DELETE FROM ' + table + ' WHERE rowid=?', (int(rowid),))
    db.execute('INSERT INTO ' + table + '(rowid, bigram, digest) VALUES(?,?,?)',
               (int(rowid), value, fts_digest(value)))


def _prepare_fts(db):
    db.execute(DIGEST_FTS)
    db.execute(PERIOD_FTS)


def _store(root, scope, period, text, labels, counts, created, model_calls):
    """Write one period's digest; a redone period replaces its own row."""
    path = _memory_path(root)
    with database(path) as db:
        db.execute(TABLE); db.execute(PERIOD_TABLE); _prepare_fts(db)
        if scope == 'day':
            db.execute('INSERT INTO memory_digest(day,text,kinds,counts,created,model_calls) VALUES(?,?,?,?,?,?) '
                       'ON CONFLICT(day) DO UPDATE SET text=excluded.text,kinds=excluded.kinds,'
                       'counts=excluded.counts,created=excluded.created,model_calls=excluded.model_calls',
                       (period, text, json.dumps(labels, ensure_ascii=False),
                        json.dumps(counts, ensure_ascii=False), created, model_calls))
            row = db.execute('SELECT rowid FROM memory_digest WHERE day=?', (period,)).fetchone()
            _fts_put(db, 'memory_digest_fts', row[0], text, labels)
            return
        db.execute('INSERT INTO memory_digest_period(scope,period,text,kinds,counts,created,model_calls) '
                   'VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,period) DO UPDATE SET text=excluded.text,'
                   'kinds=excluded.kinds,counts=excluded.counts,created=excluded.created,'
                   'model_calls=excluded.model_calls',
                   (scope, period, text, json.dumps(labels, ensure_ascii=False),
                    json.dumps(counts, ensure_ascii=False), created, model_calls))
        row = db.execute('SELECT rowid FROM memory_digest_period WHERE scope=? AND period=?',
                         (scope, period)).fetchone()
        _fts_put(db, 'memory_digest_period_fts', row[0], text, labels)


def sync_index(root):
    """Reconcile both digest indexes with their stored rows."""
    path = _memory_path(root)
    counts = {'days': 0, 'periods': 0}
    if not path.is_file():
        return counts
    with database(path) as db:
        db.execute(TABLE); db.execute(PERIOD_TABLE); _prepare_fts(db)
        for key, table, sql in (
                ('days', 'memory_digest_fts', 'SELECT rowid, text, kinds FROM memory_digest'),
                ('periods', 'memory_digest_period_fts',
                 'SELECT rowid, text, kinds FROM memory_digest_period')):
            rows = [dict(row) for row in db.execute(sql)]
            counts[key] = len(rows)
            indexed = {int(row[0]): str(row[1] or '') for row in db.execute(
                'SELECT rowid, digest FROM ' + table)}
            ids = {int(row['rowid']) for row in rows}
            for ident in [ident for ident in indexed if ident not in ids]:
                db.execute('DELETE FROM ' + table + ' WHERE rowid=?', (ident,))
            for row in rows:
                try:
                    labels = json.loads(row['kinds'] or '[]')
                except (TypeError, ValueError):
                    labels = []
                value = bigrams(_fts_haystack(row['text'], labels))
                stamp = fts_digest(value)
                if indexed.get(int(row['rowid'])) == stamp:
                    continue
                db.execute('DELETE FROM ' + table + ' WHERE rowid=?', (int(row['rowid']),))
                db.execute('INSERT INTO ' + table + '(rowid, bigram, digest) VALUES(?,?,?)',
                           (int(row['rowid']), value, stamp))
    return counts


def search_digests(root, query, *, limit=5, scope=None):
    """FTS5 lookup over day/week/month digests; returns overview-level rows."""
    expr = expressions(query)[1]
    if not expr:
        return {'results': [], 'mode': 'fts5', 'note': '请给一个要找的主题词'}
    root = Path(root)
    sync_index(root)
    path = _memory_path(root)
    results = []
    with database(path) as db:
        db.execute(TABLE); db.execute(PERIOD_TABLE); _prepare_fts(db)
        try:
            days = db.execute(
                'SELECT d.day, d.text, d.kinds, d.counts FROM memory_digest_fts f '
                'JOIN memory_digest d ON d.rowid=f.rowid WHERE memory_digest_fts MATCH ? '
                'ORDER BY bm25(memory_digest_fts) LIMIT ?', (expr, int(limit))).fetchall()
            periods = db.execute(
                'SELECT p.scope, p.period, p.text, p.kinds, p.counts FROM memory_digest_period_fts f '
                'JOIN memory_digest_period p ON p.rowid=f.rowid WHERE memory_digest_period_fts MATCH ? '
                'ORDER BY bm25(memory_digest_period_fts) LIMIT ?', (expr, int(limit))).fetchall()
        except sqlite3.Error as exc:
            return {'results': [], 'mode': 'fts5', 'note': '小结索引查询失败：' + str(exc)[:120]}
    for rank, row in enumerate(days, 1):
        results.append({'scope': 'day', 'period': row['day'], 'text': row['text'],
                        'reference': 'digest:day:' + str(row['day']), 'rank': rank,
                        'matched_by': 'fts5'})
    for rank, row in enumerate(periods, 1):
        if scope not in (None, '', 'all') and str(row['scope']) != scope:
            continue
        results.append({'scope': row['scope'], 'period': row['period'], 'text': row['text'],
                        'reference': 'digest:' + str(row['scope']) + ':' + str(row['period']),
                        'rank': rank, 'matched_by': 'fts5'})
    return {'results': results[:max(1, min(50, int(limit)) * 2)], 'mode': 'fts5', 'note': ''}


def get_digest(root, scope, period):
    """One stored digest by its scope and period, or None."""
    return _stored(Path(root), _scope(scope), _period(scope, period))


def build(root, scope='day', period=None, *, force=False, gateway=None, raise_errors=False):
    """Summarize one period once; force rewrites the summary it already has.

    ``gateway`` lets the curation program pass Fish's configured model seam so
    the daily digest is one step of that program, not a second model owner.
    ``raise_errors`` is for that program: a failed call is its own recorded
    failure and must not leave a "failed" digest row that later looks finished.
    """
    root = Path(root)
    scope = _scope(scope)
    period = _period(scope, period)
    existing = _stored(root, scope, period)
    if existing is not None and not force:
        return dict(existing, skipped=True)
    start, end = _period_bounds(scope, period)
    material = _material_range(root, start, end, MATERIALS[scope])
    kinds = _tally(_kind_tally_range(root, start, end), _labels())
    counts = {'memories': material['totals']['memories'],
              'expressions': material['totals']['expressions'], 'kinds': kinds}
    model_calls = 0
    if counts['memories'] == 0 and counts['expressions'] == 0:
        text = NOTHINGS[scope]
    else:
        model_calls = 1
        try:
            if gateway is None:
                from model_gateway import ModelGateway
                gateway = ModelGateway(root, 'qq')
            reply = gateway.json(
                SYSTEMS[scope], json.dumps({'scope': scope, 'period': period, **material},
                                           ensure_ascii=False), origin='memory-digest')
            text = _text(reply)
        except Exception as exc:  # A page must never see a background failure.
            if raise_errors:
                raise
            text = (FAILED + _reason(exc))[:MAX_TEXT]
    created = time.time()
    labels = [row['label'] for row in kinds]
    _store(root, scope, period, text, labels, counts, created, model_calls)
    return dict(_record(period, text, labels, counts, created, model_calls, scope), skipped=False)


def daily(root, day=None, *, force=False, gateway=None, raise_errors=False):
    """Summarize one day once; force rewrites the summary it already has."""
    return build(root, 'day', day, force=force, gateway=gateway, raise_errors=raise_errors)


def weekly(root, week=None, *, force=False, gateway=None, raise_errors=False):
    """Summarize one ISO week once; force rewrites the summary it already has."""
    return build(root, 'week', week, force=force, gateway=gateway, raise_errors=raise_errors)


def monthly(root, month=None, *, force=False, gateway=None, raise_errors=False):
    """Summarize one calendar month once; force rewrites it when force is set."""
    return build(root, 'month', month, force=force, gateway=gateway, raise_errors=raise_errors)


def revise(root, scope, period, note):
    """Rewrite one stored period's summary as the owner's note says, and keep it.

    The paragraph the owner is correcting is what the model receives together
    with the note, so a correction changes the sentence it points at instead of
    summarizing the period again from scratch. A missing period is summarized
    first, because a correction needs something to correct. Unlike the nightly
    build, a model failure here is raised: the owner asked for this rewrite.
    """
    root = Path(root)
    scope = _scope(scope)
    period = _period(scope, period)
    text = ' '.join(str(note or '').split())
    if not text:
        raise ValueError('请写一句哪里不对')
    if len(text) > 500:
        raise ValueError('批注最多 500 字')
    before = _stored(root, scope, period) or build(root, scope, period, force=True)
    start, end = _period_bounds(scope, period)
    material = _material_range(root, start, end, MATERIALS[scope])
    payload = {'scope': scope, 'period': period, 'owner_note': text,
               'current_digest': before['text'], **material}
    from model_gateway import ModelGateway
    reply = ModelGateway(root, 'qq').json(REVISE_SYSTEM, json.dumps(payload, ensure_ascii=False),
                                          origin='memory-digest-revise')
    after = _text(reply)
    created = time.time()
    _store(root, scope, period, after, before['kinds'], before['counts'], created, 1)
    return {'scope': scope, 'period': period, 'note': text, 'before': before['text'],
            'after': after, 'digest': _stored(root, scope, period)}


def list_digests(root, limit=60, scope=None):
    """Stored summaries, newest period first; one scope or all three together."""
    scope = None if scope in (None, '', 'all') else _scope(scope)
    bound = max(1, min(365, int(limit)))
    path = _memory_path(root)
    with database(path) as db:
        db.execute(TABLE); db.execute(PERIOD_TABLE)
        rows = ([] if scope not in (None, 'day') else
                [_row(dict(row), 'day') for row in db.execute(
                    'SELECT * FROM memory_digest ORDER BY day DESC LIMIT ?', (bound,)).fetchall()])
        if scope in (None, 'week', 'month'):
            rows.extend(_row(dict(row), str(row['scope'])) for row in db.execute(
                'SELECT * FROM memory_digest_period ORDER BY period DESC LIMIT ?', (bound,)).fetchall()
                if scope is None or str(row['scope']) == scope)
    # Newest period first; a month and a week can share a prefix, so the scope
    # order (month, then week, then day) breaks the tie the way the tree nests them.
    rank = {'month': 0, 'week': 1, 'day': 2}
    rows.sort(key=lambda row: (row['day'], -rank.get(row['scope'], 3)), reverse=True)
    return rows[:bound]


def recent_expressions(root, limit=10):
    """Newest expression-library entries, read only."""
    from language_workbench import KINDS, STATUSES
    conn = _readonly(_language_path(root))
    if conn is None:
        return []
    try:
        rows = [dict(row) for row in conn.execute(
            'SELECT id,kind,trigger,content,status,updated FROM entries ORDER BY updated DESC LIMIT ?',
            (max(1, min(50, int(limit))),))]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [{'id': row['id'], 'source_type': 'group_language', 'kind': row['kind'] or '',
             'kind_label': KINDS.get(row['kind'], row['kind'] or ''), 'trigger': row['trigger'] or '',
             'content': row['content'] or '', 'status': row['status'] or '',
             'status_label': STATUSES.get(row['status'], row['status'] or ''),
             'editable': True, 'changed_at': float(row['updated'] or 0)} for row in rows]


def _counts(root):
    """Whole-library totals; the page labels them as library-wide, not per day."""
    labels = _labels()
    path = _memory_path(root)
    with database(path) as db:
        db.execute(TABLE)
        total = db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
        kinds = _tally([(row['kind'], row['n']) for row in db.execute(
            'SELECT kind,COUNT(*) n FROM memories GROUP BY kind').fetchall()], labels)
        statuses = _tally([(row['status'], row['n']) for row in db.execute(
            'SELECT status,COUNT(*) n FROM memories GROUP BY status').fetchall()], STATUS_LABELS)
    from language_workbench import STATUSES
    library, expression_statuses = 0, []
    conn = _readonly(_language_path(root))
    if conn is not None:
        try:
            library = conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
            expression_statuses = _tally([(row['status'], row['n']) for row in conn.execute(
                'SELECT status,COUNT(*) n FROM entries GROUP BY status').fetchall()],
                STATUSES, order=['active', 'candidate', 'disabled'])
        except sqlite3.Error:
            library, expression_statuses = 0, []
        finally:
            conn.close()
    return {'memories': {'total': int(total or 0), 'kinds': kinds, 'statuses': statuses},
            'expressions': {'total': int(library or 0), 'statuses': expression_statuses}}


def open_events(root, limit=10):
    """Still-open owner events; the same rows the life page shows."""
    path = _memory_path(root)
    if not path.is_file():
        return []
    from memory_store import MemoryStore
    conn = _readonly(path)
    try:
        scopes, _owner = MemoryStore(root, 'qq')._visible_scopes(conn, 'owner')
        holes = ','.join('?' * len(scopes))
        rows = conn.execute(
            "SELECT id,text,action,due,created FROM memories WHERE scope IN (%s) "
            "AND kind='event' AND status='active' AND action='open' "
            "ORDER BY CASE WHEN due=0 THEN 1 ELSE 0 END, due ASC, created DESC LIMIT ?" % holes,
            (*scopes, max(1, min(50, int(limit))))).fetchall()
    finally:
        conn.close()
    return [{'id': str(row['id']), 'text': ' '.join(str(row['text'] or '').split())[:80],
             'action': 'open', 'state': '待办', 'due': float(row['due'] or 0),
             'created': float(row['created'] or 0)} for row in rows]


def overview(root):
    """One read for the memory page: timeline, totals, open events, recent expressions."""
    return {'digests': list_digests(root), 'counts': _counts(root),
            'open_events': open_events(root), 'expressions': recent_expressions(root)}
