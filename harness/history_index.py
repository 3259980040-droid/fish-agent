"""Full-text index over original message stores; originals are never rewritten.

Chinese has no word delimiters, so text is indexed as character bigrams and queries
are expanded the same way. One row per message, rowid mirrors the message id.
"""
import re
import sqlite3
from contextlib import closing
from pathlib import Path

HAN = re.compile(r'[\u4e00-\u9fff]')
WORD = re.compile(r'[a-z0-9_]{2,}')

SCHEMA = ("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(bigram, tokenize='unicode61')")


def bigrams(text):
    """Character bigrams for CJK plus latin words, so 整理方案 can be found."""
    value = str(text or '').casefold()
    terms = []
    for run in re.findall(r'[\u4e00-\u9fff]+', value):
        terms.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
        if len(run) == 1:
            terms.append(run)
    terms.extend(WORD.findall(value))
    return ' '.join(terms)


def _open(path):
    conn = sqlite3.connect(str(path), timeout=20)
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def ensure(root, path=None):
    """Create the index and add any messages that are not indexed yet."""
    paths = [Path(path)] if path else [Path(root) / 'data/qq/qq.db', Path(root) / 'data/qq/desktop-chat.db']
    added = 0
    for target in paths:
        if not target.is_file():
            continue
        with closing(_open(target)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            if 'messages' not in tables:
                continue
            db.execute(SCHEMA)
            # Rows whose original message is gone must leave the index too; the join in
            # search already hides them, but a stale index keeps paying for storage.
            db.execute('DELETE FROM messages_fts WHERE rowid NOT IN (SELECT id FROM messages)')
            db.commit()  # the prune must survive even when nothing new arrives
            last = db.execute('SELECT COALESCE(MAX(rowid),0) FROM messages_fts').fetchone()[0]
            rows = db.execute('SELECT id, text FROM messages WHERE id>? ORDER BY id', (int(last),)).fetchall()
            if not rows:
                continue
            db.executemany('INSERT INTO messages_fts(rowid, bigram) VALUES(?,?)',
                           [(int(mid), bigrams(text)) for mid, text in rows])
            db.commit()
            added += len(rows)
    return added


def search(root, query, *, limit=8, path=None):
    """Return matching original rows with a short excerpt, best match first."""
    target = Path(path) if path else Path(root) / 'data/qq/qq.db'
    terms = bigrams(query)
    if not terms:
        return []
    with closing(_open(target)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if 'messages_fts' not in tables:
            return []
        sql = ('SELECT m.id, m.chat_id, m.sender_id, m.ts, m.direction, m.text, m.status '
               'FROM messages_fts f JOIN messages m ON m.id=f.rowid '
               'WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT ?')
        columns = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
        if 'chat_id' not in columns:
            # Desktop conversations keep created_at and conversation_id only.
            sql = ('SELECT m.id, m.conversation_id, \'\', m.created_at, m.direction, m.text, m.status '
                   'FROM messages_fts f JOIN messages m ON m.id=f.rowid '
                   'WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT ?')
        rows = db.execute(sql, (terms, int(limit))).fetchall()
        if not rows:
            # A long question rarely matches every bigram: fall back to any-term.
            any_term = ' OR '.join(part for part in terms.split(' ') if part)
            if any_term:
                rows = db.execute(sql, (any_term, int(limit))).fetchall()
    return [{'id': row[0], 'place': row[1], 'sender': row[2], 'time': row[3],
             'direction': row[4], 'text': (row[5] or '')[:120], 'status': row[6]} for row in rows]
