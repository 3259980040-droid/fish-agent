"""Full-text index over original message stores; originals are never rewritten.

Chinese has no word delimiters, so text is indexed as character bigrams and
queries are expanded the same way. One row per message, rowid mirrors the
message id. Index maintenance is an explicit reconciliation: new rows are
added, edited rows are replaced, and rows whose original was deleted are
removed, so the index never depends on "append after MAX(rowid)" alone.
"""
from __future__ import annotations
from locations import project_path

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from fts5_index import bigrams, digest as fts_digest, expressions

WORD = re.compile(r'[a-z0-9_]{2,}')

SCHEMA = ("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
          "USING fts5(bigram, digest UNINDEXED, tokenize='unicode61')")


def _open(path):
    conn = sqlite3.connect(str(path), timeout=20)
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def _targets(root, path=None):
    if path:
        return [Path(path)]
    return [project_path(Path(root), 'data/qq/qq.db'), project_path(Path(root), 'data/qq/desktop-chat.db')]


def _short(source):
    name = str(source)
    if name == 'desktop-chat.db' or name.endswith('/desktop-chat.db') or name.endswith('\\desktop-chat.db'):
        return 'desktop'
    if name == 'qq.db' or name.endswith('/qq.db') or name.endswith('\\qq.db'):
        return 'qq'
    return Path(name).stem


def _target(root, source):
    name = Path(str(source)).name
    if name == 'desktop-chat.db' or str(source) in ('desktop', 'desktop-chat'):
        return project_path(Path(root), 'data/qq/desktop-chat.db')
    return project_path(Path(root), 'data/qq/qq.db')


def _schema(db):
    """Ensure the current FTS5 shape; an older derived index is rebuilt."""
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    if 'messages' not in tables:
        return False
    if 'messages_fts' in tables:
        columns = {row[1] for row in db.execute('PRAGMA table_info(messages_fts)')}
        if 'digest' not in columns:
            # Index only: the original messages are untouched and are re-read below.
            db.execute('DROP TABLE messages_fts')
    db.execute(SCHEMA)
    return True


def _sync_one(target):
    if not target.is_file():
        return {'source': target.name, 'indexed': 0, 'added': 0, 'updated': 0,
                'deleted': 0, 'skipped': '来源库不存在'}
    with closing(_open(target)) as db:
        if not _schema(db):
            return {'source': target.name, 'indexed': 0, 'added': 0, 'updated': 0,
                    'deleted': 0, 'skipped': '来源库没有消息表'}
        try:
            source_rows = db.execute('SELECT id, text FROM messages').fetchall()
        except sqlite3.Error as exc:
            return {'source': target.name, 'indexed': 0, 'added': 0, 'updated': 0,
                    'deleted': 0, 'skipped': type(exc).__name__ + ': ' + str(exc)[:120]}
        indexed = {int(row[0]): str(row[1] or '') for row in db.execute(
            'SELECT rowid, digest FROM messages_fts')}
        ids, inserts, updates = set(), [], []
        for raw_id, text in source_rows:
            message_id = int(raw_id)
            ids.add(message_id)
            value = bigrams(text)
            stamp = fts_digest(value)
            previous = indexed.get(message_id)
            if previous is None:
                inserts.append((message_id, value, stamp))
            elif previous != stamp:
                updates.append((message_id, value, stamp))
        deletes = [message_id for message_id in indexed if message_id not in ids]
        try:
            for message_id in deletes:
                db.execute('DELETE FROM messages_fts WHERE rowid=?', (message_id,))
            for message_id, value, stamp in inserts:
                db.execute('INSERT INTO messages_fts(rowid, bigram, digest) VALUES(?,?,?)',
                           (message_id, value, stamp))
            for message_id, value, stamp in updates:
                db.execute('DELETE FROM messages_fts WHERE rowid=?', (message_id,))
                db.execute('INSERT INTO messages_fts(rowid, bigram, digest) VALUES(?,?,?)',
                           (message_id, value, stamp))
            db.commit()
        except sqlite3.Error as exc:
            db.rollback()
            return {'source': target.name, 'indexed': len(ids), 'added': 0, 'updated': 0,
                    'deleted': 0, 'skipped': type(exc).__name__ + ': ' + str(exc)[:120]}
    return {'source': target.name, 'indexed': len(ids), 'added': len(inserts),
            'updated': len(updates), 'deleted': len(deletes), 'skipped': ''}


def sync(root, path=None):
    """Reconcile every message index with its original rows.

    This reads all messages once so an edited or deleted old row is noticed;
    it does not stop at the newest row. The index is derived and can be rebuilt,
    while the original message rows are only ever read.
    """
    stores = [_sync_one(target) for target in _targets(root, path)]
    return {'stores': stores,
            'added': sum(item['added'] for item in stores),
            'updated': sum(item['updated'] for item in stores),
            'deleted': sum(item['deleted'] for item in stores),
            'indexed': sum(item['indexed'] for item in stores)}


def ensure(root, path=None):
    """Compatibility name used by older callers: returns changed row count."""
    result = sync(root, path)
    return result['added'] + result['updated']


def _query(db, sql, expression, limit):
    if not expression:
        return []
    try:
        return db.execute(sql, (expression, int(limit))).fetchall()
    except sqlite3.Error:
        return []


def _search_one(target, query, limit):
    if not target.is_file():
        return []
    with closing(_open(target)) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if 'messages_fts' not in tables or 'messages' not in tables:
            return []
        columns = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
        if 'chat_id' in columns:
            place, sender = 'chat_id', 'sender_id'
            conversation = 'conversation_id' if 'conversation_id' in columns else 'chat_id'
        else:
            # Desktop conversations keep created_at and conversation_id only.
            place, sender, conversation = 'conversation_id', "''", 'conversation_id'
        stamp = 'ts' if 'ts' in columns else 'created_at'
        quote = 'reply_to' if 'reply_to' in columns else "''"
        quote_expr = 'm.' + quote if quote != "''" else "''"
        sender_expr = 'm.' + sender if sender != "''" else "''"
        sql = ('SELECT m.id, m.' + place + ', ' + sender_expr + ', m.' + stamp + ', '
               'm.direction, m.text, m.status, ' + quote_expr + ', m.' + conversation + ' '
               'FROM messages_fts f JOIN messages m ON m.id=f.rowid '
               'WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT ?')
        rows = _query(db, sql, expressions(query)[0], limit)
        if not rows:
            # A long question rarely matches every bigram: any term keeps the
            # first and the last keyword in play, and bm25 still ranks them.
            rows = _query(db, sql, expressions(query)[1], limit)
    source = target.name
    store = _short(source)
    hits = []
    for rank, row in enumerate(rows, 1):
        message_id = int(row[0])
        hits.append({
            'id': message_id, 'place': row[1], 'sender': row[2], 'time': row[3],
            'direction': row[4], 'text': str(row[5] or '')[:120], 'status': row[6],
            'quote': row[7], 'conversation': row[8],
            'source': source, 'store': store,
            'reference': 'message:' + store + ':' + str(message_id),
            'rank': rank, 'fts5': True,
        })
    return hits


def search(root, query, *, limit=8, path=None, source=None):
    """Return matching original rows with a short excerpt, best match first.

    Every store's index is reconciled first, so new, edited and deleted rows
    all change this result. Multiple stores are fused by rank, never by a raw
    score that would compare unrelated indexes.
    """
    targets = _targets(root, path)
    if source:
        wanted = _short(source)
        targets = [target for target in targets if _short(target.name) == wanted]
    sync(root, path)
    per_store = [_search_one(target, query, max(2, int(limit))) for target in targets]
    if len(per_store) == 1:
        return per_store[0][:max(1, int(limit))]
    by_reference, fused = {}, {}
    for hits in per_store:
        for rank, hit in enumerate(hits, 1):
            by_reference[hit['reference']] = hit
            fused[hit['reference']] = fused.get(hit['reference'], 0.0) + 1.0 / (60 + rank)
    ordered = sorted(fused, key=lambda key: (-fused[key], key))[:max(1, int(limit))]
    return [{**by_reference[key], 'fused': round(fused[key], 6)} for key in ordered]


def forget_messages(root, source, ids, path=None):
    """Drop index rows whose originals were deliberately deleted.

    The owning delete path calls this before/after removing its real row; a
    stale index must not keep returning text the owner removed. Missing source
    databases and a never-built index are both no-ops.
    """
    target = Path(path) if path else _target(root, source)
    if not target.is_file():
        return 0
    wanted = [int(value) for value in (ids or [])]
    if not wanted:
        return 0
    with closing(_open(target)) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if 'messages_fts' not in tables:
            return 0
        removed = 0
        for message_id in wanted:
            removed += db.execute('DELETE FROM messages_fts WHERE rowid=?', (message_id,)).rowcount
        db.commit()
    return removed


def forget(root, reference, path=None):
    """Drop one stable ``message:<store>:<id>`` reference from its index."""
    parts = str(reference or '').split(':')
    if len(parts) != 3 or parts[0] != 'message':
        return 0
    source = 'desktop-chat.db' if parts[1] in ('desktop', 'desktop-chat.db') else 'qq.db'
    try:
        message_id = int(parts[2])
    except (TypeError, ValueError):
        return 0
    return forget_messages(root, source, [message_id], path=path)
