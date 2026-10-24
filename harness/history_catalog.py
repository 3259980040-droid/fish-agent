"""Two-level recall over frozen history: catalogue first, originals by range second."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def _ledger(root):
    return Path(root) / 'data/social/experience.db'


def _open(path):
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def search_catalog(root, query, *, place='', limit=5):
    """Find which stretch of history talked about this; returns ranges, not text."""
    path = _ledger(root)
    if not path.is_file():
        return {'entries': [], 'note': '还没有任何压缩记录；旧消息仍可按关键词直接检索'}
    terms = [term for term in str(query or '').replace('，', ',').split(',') if term.strip()]
    with closing(_open(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_catalog'").fetchone():
            return {'entries': [], 'note': '还没有任何压缩记录'}
        # source is required by fish_history_read, so it must travel with every entry.
        sql = ('SELECT id, source, place, conversation, from_message_id, to_message_id, start_time, end_time,'
               ' topics, keywords, gist FROM chat_catalog WHERE 1=1')
        args = []
        if place:
            sql += ' AND place=?'
            args.append(place)
        for term in terms:
            sql += ' AND (keywords LIKE ? OR gist LIKE ? OR topics LIKE ?)'
            args.extend(['%' + term.strip() + '%'] * 3)
        sql += ' ORDER BY to_message_id DESC LIMIT ?'
        args.append(int(limit))
        rows = [dict(row) for row in db.execute(sql, args)]
    return {'entries': rows,
            'note': '这些是目录条目：先用 gist 判断是否相关，需要原话再按 from/to 取原文'} 


def read_range(root, *, source, conversation, from_id, to_id, limit=40):
    """Fetch originals for one catalogue range; the store is read-only here."""
    name = 'desktop-chat.db' if str(source) == 'desktop-chat.db' else 'qq.db'
    path = Path(root) / 'data/qq' / name
    if not path.is_file():
        return {'messages': [], 'note': '该来源库不存在'}
    with closing(_open(path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'messages' not in tables:
            return {'messages': [], 'note': '该来源库没有消息表'}
        columns = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
        stamp = 'ts' if 'ts' in columns else 'created_at'
        place = 'chat_id' if 'chat_id' in columns else 'conversation_id'
        sender = 'sender_id' if 'sender_id' in columns else "''"
        quote = 'reply_to' if 'reply_to' in columns else "''"
        rows = db.execute('SELECT id, ' + stamp + ', ' + place + ', ' + sender + ', direction, text, status, ' + quote + ' '
                          'FROM messages WHERE conversation_id=? AND id>=? AND id<=? ORDER BY id LIMIT ?',
                          (conversation, int(from_id), int(to_id), int(limit))).fetchall()
    return {'messages': [{'id': row[0], 'time': row[1], 'place': row[2], 'sender': row[3],
                          'direction': row[4], 'text': (row[5] or '')[:400],
                          'status': row[6], 'reply_to': row[7]} for row in rows],
            'note': '原文按消息号取回；未送达或未知的记录不能当作已经说过'}
