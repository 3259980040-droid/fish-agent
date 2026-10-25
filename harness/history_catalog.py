"""Two-level recall over frozen history: catalogue first, originals by range second.

The catalogue is a derived summary view written by context compaction. Its own
FTS5 index makes topic lookup a real lexical query; the original message rows
are read from the owning store and are never copied into this module.
"""
from __future__ import annotations
from locations import project_path

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from fts5_index import bigrams, digest as fts_digest, expressions

CATALOG_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts "
               "USING fts5(bigram, digest UNINDEXED, tokenize='unicode61')")


def _ledger(root):
    return project_path(Path(root), 'data/social/experience.db')


def _open(path):
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _write(path):
    conn = sqlite3.connect(str(path), timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def _has_catalog(db):
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_catalog'").fetchone())


def _haystack(row):
    return ' '.join(str(row[key] or '') for key in
                    ('place', 'conversation', 'topics', 'keywords', 'gist'))


def _sync_catalog(root):
    """Reconcile catalog_fts with chat_catalog; returns the row count or 0."""
    path = _ledger(root)
    if not path.is_file():
        return 0
    with closing(_write(path)) as db:
        if not _has_catalog(db):
            return 0
        db.execute(CATALOG_FTS)
        rows = [dict(row) for row in db.execute(
            'SELECT id, place, conversation, topics, keywords, gist FROM chat_catalog')]
        indexed = {int(row[0]): str(row[1] or '') for row in db.execute(
            'SELECT rowid, digest FROM catalog_fts')}
        ids = {int(row['id']) for row in rows}
        for catalog_id in [key for key in indexed if key not in ids]:
            db.execute('DELETE FROM catalog_fts WHERE rowid=?', (catalog_id,))
        for row in rows:
            value = bigrams(_haystack(row))
            stamp = fts_digest(value)
            if indexed.get(int(row['id'])) == stamp:
                continue
            db.execute('DELETE FROM catalog_fts WHERE rowid=?', (int(row['id']),))
            db.execute('INSERT INTO catalog_fts(rowid, bigram, digest) VALUES(?,?,?)',
                       (int(row['id']), value, stamp))
        db.commit()
    return len(rows)


def search_catalog(root, query, *, place='', limit=5):
    """Find which stretch of history talked about this; returns ranges, not text."""
    path = _ledger(root)
    if not path.is_file():
        return {'entries': [], 'note': '还没有任何压缩记录；旧消息仍可按关键词直接检索'}
    try:
        _sync_catalog(root)
    except sqlite3.Error as exc:
        return {'entries': [], 'note': '目录索引无法读取：' + str(exc)[:120]}
    expr = expressions(query)[1]
    if not expr:
        return {'entries': [], 'note': '请给一个要找的主题词'}
    with closing(_open(path)) as db:
        if not _has_catalog(db):
            return {'entries': [], 'note': '还没有任何压缩记录'}
        sql = ('SELECT c.id, c.source, c.place, c.conversation, c.from_message_id, c.to_message_id,'
               ' c.start_time, c.end_time, c.topics, c.keywords, c.gist FROM catalog_fts f '
               'JOIN chat_catalog c ON c.id=f.rowid WHERE catalog_fts MATCH ?')
        args = [expr]
        if place:
            sql += ' AND c.place=?'
            args.append(place)
        sql += ' ORDER BY bm25(catalog_fts), c.to_message_id DESC LIMIT ?'
        args.append(max(1, min(50, int(limit))))
        try:
            rows = [dict(row) for row in db.execute(sql, args)]
        except sqlite3.Error as exc:
            return {'entries': [], 'note': '目录索引查询失败：' + str(exc)[:120]}
    return {'entries': rows, 'mode': 'fts5',
            'note': '这些是目录条目：先用 gist 判断是否相关，需要原话再按 from/to 取原文'}


def entry(root, catalog_id):
    """One catalog row by its stable id, or None."""
    path = _ledger(root)
    if not path.is_file():
        return None
    with closing(_open(path)) as db:
        if not _has_catalog(db):
            return None
        row = db.execute('SELECT * FROM chat_catalog WHERE id=?', (int(catalog_id),)).fetchone()
    return dict(row) if row else None


def find_range(root, source, conversation, message_id):
    """The newest catalogue entry whose range covers this message, or None."""
    path = _ledger(root)
    if not path.is_file():
        return None
    try:
        with closing(_open(path)) as db:
            if not _has_catalog(db):
                return None
            row = db.execute(
                'SELECT * FROM chat_catalog WHERE source=? AND conversation=? '
                'AND from_message_id<=? AND to_message_id>=? '
                'ORDER BY to_message_id DESC, id DESC LIMIT 1',
                (str(source), str(conversation), int(message_id), int(message_id))).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def _shape(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
    return {
        'stamp': 'ts' if 'ts' in columns else 'created_at',
        'place': 'chat_id' if 'chat_id' in columns else 'conversation_id',
        'sender': 'sender_id' if 'sender_id' in columns else "''",
        'quote': 'reply_to' if 'reply_to' in columns else "''",
        'conversation': 'conversation_id' if 'conversation_id' in columns else
                        ('chat_id' if 'chat_id' in columns else "''"),
        'segments': 'segments' if 'segments' in columns else "'[]'",
    }


def _source_path(root, source):
    name = 'desktop-chat.db' if str(source) == 'desktop-chat.db' else 'qq.db'
    return project_path(Path(root), 'data/qq', name)


def read_message(root, source, message_id):
    """One complete original message, including its attachment segments."""
    path = _source_path(root, source)
    if not path.is_file():
        return {'messages': [], 'note': '该来源库不存在'}
    with closing(_open(path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'messages' not in tables:
            return {'messages': [], 'note': '该来源库没有消息表'}
        shape = _shape(db)
        row = db.execute(
            'SELECT id, ' + shape['stamp'] + ', ' + shape['place'] + ', ' + shape['sender'] +
            ', direction, text, status, ' + shape['quote'] + ', ' + shape['conversation'] +
            ', ' + shape['segments'] + ' FROM messages WHERE id=?', (int(message_id),)).fetchone()
    if row is None:
        return {'messages': [], 'note': '这条原始消息已不存在'}
    return {'messages': [_message(row)], 'note': '原始消息按编号读取；未送达或未知的记录不能当作已经说过'}


def context(root, source, conversation, message_id, *, before=3, after=3):
    """Nearby original messages around one message in the same conversation."""
    path = _source_path(root, source)
    if not path.is_file():
        return {'messages': [], 'note': '该来源库不存在'}
    start = max(1, int(message_id) - max(0, int(before)))
    end = int(message_id) + max(0, int(after))
    with closing(_open(path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'messages' not in tables:
            return {'messages': [], 'note': '该来源库没有消息表'}
        shape = _shape(db)
        place = shape['place']
        rows = db.execute(
            'SELECT id, ' + shape['stamp'] + ', ' + place + ', ' + shape['sender'] +
            ', direction, text, status, ' + shape['quote'] + ', ' + shape['conversation'] +
            ', ' + shape['segments'] + ' FROM messages WHERE ' + place + '=? AND id>=? AND id<=? ORDER BY id',
            (str(conversation), start, end)).fetchall()
    return {'messages': [_message(row) for row in rows],
            'note': '上下文按同一条对话的真实消息号取回；没有返回的消息是本地没有记录'}


def _message(row):
    try:
        segments = json.loads(row[9] or '[]')
    except (TypeError, ValueError):
        segments = []
    return {'id': row[0], 'time': row[1], 'place': row[2], 'sender': row[3],
            'direction': row[4], 'text': str(row[5] or ''), 'status': row[6],
            'quote': row[7], 'conversation': row[8],
            'attachments': [part for part in segments if isinstance(part, dict)
                            and part.get('type') not in ('text', 'at')]}


def read_range(root, *, source, conversation, from_id, to_id, limit=40, offset=0, truncate=400):
    """Fetch originals for one catalogue range; the store is read-only here."""
    path = _source_path(root, source)
    if not path.is_file():
        return {'messages': [], 'note': '该来源库不存在'}
    with closing(_open(path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'messages' not in tables:
            return {'messages': [], 'note': '该来源库没有消息表'}
        shape = _shape(db)
        rows = db.execute(
            'SELECT id, ' + shape['stamp'] + ', ' + shape['place'] + ', ' + shape['sender'] +
            ', direction, text, status, ' + shape['quote'] + ' FROM messages '
            'WHERE ' + shape['conversation'] + '=? AND id>=? AND id<=? ORDER BY id LIMIT ? OFFSET ?',
            (conversation, int(from_id), int(to_id), max(1, int(limit)), max(0, int(offset)))).fetchall()
    messages = []
    for row in rows:
        text = str(row[5] or '')
        messages.append({'id': row[0], 'time': row[1], 'place': row[2], 'sender': row[3],
                         'direction': row[4],
                         'text': text if int(truncate or 0) <= 0 else text[:int(truncate)],
                         'status': row[6], 'reply_to': row[7]})
    return {'messages': messages,
            'note': '原文按消息号取回；未送达或未知的记录不能当作已经说过'}
