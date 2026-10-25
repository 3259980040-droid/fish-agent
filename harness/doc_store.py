"""One resource/version/chunk index for imported texts and saved web pages.

The existing document tables remain authoritative. Originals live as files;
sections/child chunks and genuine local vectors are derived data in the same DB.
No newest-first coarse shortlist or query-tail truncation precedes ranking.
"""
from __future__ import annotations
from locations import project_path
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from pathlib import Path
from storage import database, role_dir
from fts5_index import bigrams, digest as fts_digest, expressions

CHUNK_MIN, CHUNK_MAX, CHUNK_OVERLAP = 300, 600, 50
MAX_TEXT_CHARS, MAX_NAME_CHARS = 2_000_000, 120
KINDS = ('txt', 'md', 'pdf', 'chat', 'html', 'json', 'image', 'file')
KIND_ALIASES = {'text': 'txt', 'markdown': 'md', 'plain': 'txt'}
CJK_RUN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]+')
LATIN_WORD = re.compile(r'[a-zA-Z0-9_]{2,}')
SENTENCE_ENDS = '。！？!?；;'


def _db(root):
    return role_dir(Path(root), 'qq') / 'memory.db'


def _prepare(root):
    path = _db(root)
    with database(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS document_docs(doc_id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,chars INTEGER NOT NULL,chunks INTEGER NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS document_chunks(id TEXT PRIMARY KEY,doc_id TEXT NOT NULL,doc_name TEXT NOT NULL,ord INTEGER NOT NULL,text TEXT NOT NULL,chars INTEGER NOT NULL,created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS document_chunks_doc ON document_chunks(doc_id,ord);
        CREATE TABLE IF NOT EXISTS document_sections(id TEXT PRIMARY KEY,doc_id TEXT NOT NULL,heading TEXT NOT NULL,ord INTEGER NOT NULL,text TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS document_embeddings(id TEXT PRIMARY KEY,model TEXT NOT NULL,dim INTEGER NOT NULL,digest TEXT NOT NULL,vector BLOB NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS document_aliases(alias TEXT,scope TEXT,doc_id TEXT,PRIMARY KEY(alias,scope));
        CREATE TABLE IF NOT EXISTS document_search_state(scope TEXT PRIMARY KEY,state TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS document_imports(source TEXT PRIMARY KEY,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS document_links(doc_id TEXT,reference TEXT,created REAL,PRIMARY KEY(doc_id,reference));
        CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(bigram, digest UNINDEXED, tokenize='unicode61');
        CREATE TABLE IF NOT EXISTS document_index_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        db.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in db.execute('PRAGMA table_info(document_docs)')}
        for name, decl in (('scope', "TEXT NOT NULL DEFAULT 'owner'"), ('resource_key', "TEXT NOT NULL DEFAULT ''"),
            ('digest', "TEXT NOT NULL DEFAULT ''"), ('original_path', "TEXT NOT NULL DEFAULT ''"),
            ('source', "TEXT NOT NULL DEFAULT 'upload'"), ('url', "TEXT NOT NULL DEFAULT ''"),
            ('fetched_at', "TEXT NOT NULL DEFAULT ''"), ('parent_version', "TEXT NOT NULL DEFAULT ''"),
            ('active', 'INTEGER NOT NULL DEFAULT 1'), ('metadata', "TEXT NOT NULL DEFAULT '{}'"),
            ('folder', "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                db.execute(f'ALTER TABLE document_docs ADD COLUMN {name} {decl}')
        columns = {row[1] for row in db.execute('PRAGMA table_info(document_chunks)')}
        if 'parent' not in columns:
            db.execute("ALTER TABLE document_chunks ADD COLUMN parent TEXT NOT NULL DEFAULT ''")
        db.execute('CREATE INDEX IF NOT EXISTS document_scope_resource ON document_docs(scope,resource_key,active)')
        db.execute("UPDATE document_docs SET resource_key='document:'||name WHERE resource_key=''")
        db.execute('CREATE TABLE IF NOT EXISTS document_import_errors(source TEXT PRIMARY KEY,error TEXT,updated REAL)')
    return path


def _chunk_haystack(row):
    return str(row['text'] or '')


def _signature(db):
    row = db.execute('SELECT COUNT(*), COALESCE(MAX(c.rowid),0) FROM document_chunks c '
                     'JOIN document_docs d ON d.doc_id=c.doc_id WHERE d.active=1').fetchone()
    return str(int(row[0])) + ':' + str(int(row[1]))


def _index_chunks(db, doc_id):
    """Write this document's active chunks into the derived FTS5 index."""
    rows = db.execute('SELECT rowid, id, text FROM document_chunks WHERE doc_id=?', (str(doc_id),)).fetchall()
    for row in rows:
        value = bigrams(_chunk_haystack(row))
        stamp = fts_digest(value)
        db.execute('DELETE FROM document_chunks_fts WHERE rowid=?', (int(row['rowid']),))
        db.execute('INSERT INTO document_chunks_fts(rowid, bigram, digest) VALUES(?,?,?)',
                   (int(row['rowid']), value, stamp))
    db.execute("INSERT OR REPLACE INTO document_index_state VALUES('signature',?)",
               (_signature(db),))


def _forget_chunks(db, doc_id):
    for row in db.execute('SELECT rowid FROM document_chunks WHERE doc_id=?', (str(doc_id),)).fetchall():
        db.execute('DELETE FROM document_chunks_fts WHERE rowid=?', (int(row[0]),))
    db.execute("INSERT OR REPLACE INTO document_index_state VALUES('signature',?)",
               (_signature(db),))


def sync_index(root, *, force=False):
    """Reconcile the document chunk index with the active owning rows.

    Documents are ingested and deleted through this module, so both paths
    update the index directly; this reconciliation also repairs a first-run or
    externally changed table. Inactive versions and deleted documents never
    survive in the index.
    """
    path = _prepare(root)
    with database(path) as db:
        state = db.execute("SELECT value FROM document_index_state WHERE key='signature'").fetchone()
        if state is not None and not force and str(state[0]) == _signature(db):
            rows = db.execute('SELECT COUNT(*) FROM document_chunks c JOIN document_docs d '
                              'ON d.doc_id=c.doc_id WHERE d.active=1').fetchone()[0]
            return {'indexed': int(rows), 'added': 0, 'updated': 0, 'deleted': 0, 'skipped': 'unchanged'}
        rows = [dict(row) for row in db.execute(
            'SELECT c.rowid, c.id, c.text FROM document_chunks c JOIN document_docs d '
            'ON d.doc_id=c.doc_id WHERE d.active=1')]
        indexed = {int(row[0]): str(row[1] or '') for row in db.execute(
            'SELECT rowid, digest FROM document_chunks_fts')}
        ids = {int(row['rowid']) for row in rows}
        deletes = [key for key in indexed if key not in ids]
        for key in deletes:
            db.execute('DELETE FROM document_chunks_fts WHERE rowid=?', (key,))
        added = updated = 0
        for row in rows:
            value = bigrams(_chunk_haystack(row))
            stamp = fts_digest(value)
            previous = indexed.get(int(row['rowid']))
            if previous is None:
                added += 1
            elif previous != stamp:
                updated += 1
            else:
                continue
            db.execute('DELETE FROM document_chunks_fts WHERE rowid=?', (int(row['rowid']),))
            db.execute('INSERT INTO document_chunks_fts(rowid, bigram, digest) VALUES(?,?,?)',
                       (int(row['rowid']), value, stamp))
        db.execute("INSERT OR REPLACE INTO document_index_state VALUES('signature',?)",
                   (_signature(db),))
    return {'indexed': len(rows), 'added': added, 'updated': updated, 'deleted': len(deletes),
            'skipped': ''}


def forget(root, doc_id):
    """Drop one deleted document's chunks from the derived index."""
    path = _prepare(root)
    with database(path) as db:
        _forget_chunks(db, doc_id)
    return {'ok': True, 'doc_id': str(doc_id)}


def _fts_candidates(root, query):
    expr = expressions(query)[1]
    if not expr:
        return set()
    path = _prepare(root)
    try:
        with database(path) as db:
            rows = db.execute(
                'SELECT c.id FROM document_chunks_fts f JOIN document_chunks c ON c.rowid=f.rowid '
                'WHERE document_chunks_fts MATCH ?', (expr,)).fetchall()
    except sqlite3.Error:
        return None
    return {str(row[0]) for row in rows}


def library_root(root, scope='owner'):
    base = project_path(Path(root), 'data/qq/library')
    if scope != 'owner':
        if not re.fullmatch(r'chat:group:[0-9]{5,15}', scope):
            raise ValueError('资源范围无效')
        base /= 'groups/' + scope.split(':')[-1]
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def _text(value):
    return str(value or '').replace('\r\n', '\n').replace('\r', '\n')


def _units(body):
    units, buffer = [], ''
    for char in body:
        buffer += char
        if char == '\n' or char in SENTENCE_ENDS:
            if buffer.strip():
                units.append(buffer.strip())
            buffer = ''
    if buffer.strip():
        units.append(buffer.strip())
    return [unit[pos:pos + CHUNK_MAX] for unit in units for pos in range(0, len(unit), CHUNK_MAX)]


def chunk_text(body):
    chunks, current = [], ''
    for unit in _units(body):
        if current and len(current) + len(unit) + 1 > CHUNK_MAX:
            chunks.append(current.strip())
            tail = current[-CHUNK_OVERLAP:].strip()
            current = tail if len(tail) + len(unit) + 1 <= CHUNK_MAX else ''
        current += ('\n' if current else '') + unit
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _index_text(body, kind):
    if kind == 'json':
        try:
            return json.dumps(json.loads(body), ensure_ascii=False, indent=2)
        except ValueError:
            return body
    if kind != 'html':
        return body
    from html.parser import HTMLParser
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts, self.hidden = [], 0
        def handle_starttag(self, tag, attrs):
            if tag in ('script', 'style'):
                self.hidden += 1
            if tag in ('p','div','li','tr','br','section','article'):
                self.parts.append('\n')
            if re.fullmatch('h[1-6]', tag):
                self.parts.append('\n' + '#' * int(tag[1]) + ' ')
        def handle_endtag(self, tag):
            if tag in ('script','style'):
                self.hidden = max(0, self.hidden-1)
            if tag in ('p','div','li','tr','section','article') or re.fullmatch('h[1-6]', tag):
                self.parts.append('\n')
        def handle_data(self, data):
            if not self.hidden:
                self.parts.append(data)
    parsed = Text()
    parsed.feed(body)
    return ''.join(parsed.parts)


def _sections(body):
    heading, lines, result = '', [], []
    for line in body.splitlines(keepends=True):
        match = re.match(r'^\s{0,3}#{1,6}\s+(.+)', line)
        if match:
            if lines:
                result.append((heading, ''.join(lines)))
            heading, lines = match.group(1).strip(), [line]
        else:
            lines.append(line)
    if lines:
        result.append((heading, ''.join(lines)))
    return result or [('', body)]


def _row(record):
    item = dict(record)
    return {**item, 'doc_id': str(item['doc_id']), 'chars': int(item['chars']), 'chunks': int(item['chunks']),
            'created': float(item['created']), 'metadata': json.loads(item.get('metadata') or '{}')}


def _versions(db):
    counters, result = {}, {}
    for row in db.execute('SELECT doc_id,name,scope,resource_key FROM document_docs ORDER BY created,rowid'):
        key = (row['scope'], row['resource_key'] or row['name'])
        counters[key] = counters.get(key, 0) + 1
        result[row['doc_id']] = counters[key]
    return result


def ingest(root, name, body, *, kind='txt', scope='owner', source='upload', url='', fetched_at='',
           resource_key='', original_path='', metadata=None, created_at=None):
    name = ' '.join(str(name or '').split())
    if not name or len(name) > MAX_NAME_CHARS:
        raise ValueError('请填写资料名称，最多120字')
    text = _text(body)
    if not text.strip() and not original_path:
        raise ValueError('资料正文为空，且没有原件')
    kind = KIND_ALIASES.get(str(kind).lower(), str(kind).lower())
    if kind not in KINDS:
        kind = 'txt'
    path = _prepare(root)
    key = resource_key or ('web:' + url if url else 'document:' + name)
    if original_path:
        original = Path(original_path).resolve()
        if not original.is_file():
            raise ValueError('资源原件不存在')
        hasher = hashlib.sha256()
        with original.open('rb') as data:
            for block in iter(lambda: data.read(1024 * 1024), b''):
                hasher.update(block)
        digest = hasher.hexdigest()
    else:
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    with database(path) as db:
        prior = db.execute('SELECT * FROM document_docs WHERE scope=? AND resource_key=? AND created<=? ORDER BY created DESC,rowid DESC',
                           (scope, key, float(created_at) if created_at is not None else time.time())).fetchone()
        if prior and prior['digest'] == digest and prior['active']:
            combined = {**json.loads(prior['metadata'] or '{}'), **(metadata or {})}
            db.execute('UPDATE document_docs SET metadata=? WHERE doc_id=?', (json.dumps(combined, ensure_ascii=False), prior['doc_id']))
            if (metadata or {}).get('source_reference'):
                db.execute('INSERT OR IGNORE INTO document_links VALUES(?,?,?)', (prior['doc_id'], metadata['source_reference'], time.time()))
            versions = _versions(db)
            return {**_row(prior), 'metadata': combined, 'version': versions[prior['doc_id']], 'deduplicated': True}
    doc_id, created = uuid.uuid4().hex, float(created_at) if created_at is not None else time.time()
    if original_path:
        original = Path(original_path).resolve()
        if not original.is_file():
            raise ValueError('资源原件不存在')
    else:
        suffix = '.' + kind if kind in ('md','html','json') else '.txt'
        original = library_root(root, scope) / 'originals' / hashlib.sha256(key.encode()).hexdigest()[:16] / (doc_id + suffix)
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(text, encoding='utf-8')
    # Parse in sections; parent context is separate from the small matching child.
    sections, chunks = [], []
    for number, (heading, section) in enumerate(_sections(_index_text(text, kind)), 1):
        parent = doc_id + ':section:' + str(number)
        sections.append((parent, doc_id, heading, number, section))
        for child in chunk_text(section):
            ordinal = len(chunks) + 1
            chunks.append((doc_id + ':' + str(ordinal), doc_id, name, ordinal, child, len(child), created, parent))
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO document_docs(doc_id,name,kind,chars,chunks,created,scope,resource_key,digest,original_path,source,url,fetched_at,parent_version,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (doc_id, name, kind, len(text), len(chunks), created, scope, key, digest, str(original), source, url,
                    str(fetched_at), prior['doc_id'] if prior else '', json.dumps(metadata or {}, ensure_ascii=False)))
        successor = db.execute('SELECT doc_id FROM document_docs WHERE scope=? AND resource_key=? AND created>? ORDER BY created,rowid LIMIT 1', (scope, key, created)).fetchone()
        if successor:
            db.execute('UPDATE document_docs SET parent_version=? WHERE doc_id=?', (doc_id, successor['doc_id']))
        db.executemany('INSERT INTO document_sections VALUES(?,?,?,?,?)', sections)
        db.executemany('INSERT INTO document_chunks(id,doc_id,doc_name,ord,text,chars,created,parent) VALUES(?,?,?,?,?,?,?,?)', chunks)
        _index_chunks(db, doc_id)
        if (metadata or {}).get('source_reference'):
            db.execute('INSERT OR IGNORE INTO document_links VALUES(?,?,?)', (doc_id, metadata['source_reference'], created))
        version = _versions(db)[doc_id]
        row = db.execute('SELECT * FROM document_docs WHERE doc_id=?', (doc_id,)).fetchone()
    return {**_row(row), 'version': version, 'deduplicated': False}


def _resolve(db, doc_id, scope):
    if db.execute('SELECT 1 FROM document_docs WHERE doc_id=?', (doc_id,)).fetchone():
        return doc_id
    rows = db.execute('SELECT DISTINCT doc_id FROM document_aliases WHERE alias=?' +
                      (' AND scope=?' if scope is not None else ''),
                      (doc_id, scope) if scope is not None else (doc_id,)).fetchall()
    if len(rows) > 1:
        raise ValueError('旧资料别名对应多个来源，请用检索返回的真实资料编号读取')
    return rows[0]['doc_id'] if rows else doc_id


def list_docs(root, *, limit=200, scope='owner'):
    import_legacy_web(root)
    with database(_prepare(root)) as db:
        rows = db.execute('SELECT * FROM document_docs WHERE active=1 AND scope=? ORDER BY created DESC,rowid DESC LIMIT ?', (scope, int(limit))).fetchall()
        versions = _versions(db)
        return [{**_row(row), 'version': versions[row['doc_id']]} for row in rows]


def doc_detail(root, doc_id, *, scope='owner', start=1, count=3):
    with database(_prepare(root)) as db:
        doc_id = _resolve(db, str(doc_id), scope)
        row = db.execute('SELECT * FROM document_docs WHERE doc_id=? AND active=1' + (' AND scope=?' if scope is not None else ''),
                         (doc_id, scope) if scope is not None else (doc_id,)).fetchone()
        if row is None:
            raise ValueError('此范围没有这份资料，或已由主人删除')
        chunks = db.execute('SELECT ord,text,chars,parent FROM document_chunks WHERE doc_id=? AND ord>=? ORDER BY ord LIMIT ?',
                            (doc_id, int(start), max(1, int(count)))).fetchall()
        return {**_row(row), 'version': _versions(db)[doc_id], 'preview': [dict(r) for r in chunks],
                'links': [dict(link) for link in db.execute('SELECT reference,created FROM document_links WHERE doc_id=?', (doc_id,))],
                'next_ord': int(chunks[-1]['ord']) + 1 if chunks and int(chunks[-1]['ord']) < row['chunks'] else None}


def delete_doc(root, doc_id, *, scope='owner'):
    # Retain the original and tombstone: an old sidecar must never restore a
    # document the owner deliberately removed from retrieval.
    with database(_prepare(root)) as db:
        doc_id = _resolve(db, str(doc_id), scope)
        row = db.execute('SELECT chunks FROM document_docs WHERE doc_id=? AND scope=? AND active=1', (doc_id, scope)).fetchone()
        if row is None:
            raise ValueError('没有这份可删除的资料')
        db.execute('UPDATE document_docs SET active=0 WHERE doc_id=?', (doc_id,))
        _forget_chunks(db, doc_id)
    return {'ok': True, 'doc_id': doc_id, 'chunks': int(row['chunks']), 'original_retained': True}


def tokens_of(query):
    phrase = ' '.join(str(query or '').split()).casefold()
    tokens = []
    for run in CJK_RUN.findall(phrase):
        tokens.extend([run] if len(run) == 1 else [run[i:i+2] for i in range(len(run)-1)])
    tokens.extend(LATIN_WORD.findall(phrase))
    return list(dict.fromkeys(tokens)), phrase.replace(' ', '')


def _score(text, tokens, phrase):
    value = sum(min(text.count(token), 4) * (1.0 + min(len(token), 6) * .25) for token in tokens)
    return value + (6.0 if phrase and phrase in text else 0.0)


def _pool(root, scope):
    with database(_prepare(root)) as db:
        versions = _versions(db)
        return [{**dict(row), 'metadata': json.loads(row['metadata'] or '{}'), 'version': versions[row['doc_id']]}
                for row in db.execute('SELECT c.*,d.source,d.url,d.original_path,d.scope,d.metadata,d.parent_version,d.resource_key,d.digest AS document_digest,d.fetched_at,s.heading '
                'FROM document_chunks c JOIN document_docs d ON c.doc_id=d.doc_id '
                'LEFT JOIN document_sections s ON s.id=c.parent WHERE d.active=1 '
                + ('AND d.scope=? ' if scope is not None else '') +
                'AND NOT EXISTS(SELECT 1 FROM document_docs newer WHERE newer.scope=d.scope AND newer.resource_key=d.resource_key '
                'AND (newer.created>d.created OR (newer.created=d.created AND newer.rowid>d.rowid)))',
                (scope,) if scope is not None else ())]


def retrieval_status(root, *, scope='owner'):
    from memory_engine import _worker, _config, text_digest
    worker = _worker(Path(root), 'qq')
    pool = _pool(root, scope)
    model = _config(Path(root))['model']
    with database(_prepare(root)) as db:
        vectors = {r['id']: dict(r) for r in db.execute('SELECT id,model,dim,digest FROM document_embeddings WHERE model=?', (model,))}
        last = db.execute('SELECT state FROM document_search_state WHERE scope=?', (scope if scope is not None else '*',)).fetchone()
        counts = db.execute('SELECT COUNT(*),COALESCE(SUM(d.chunks=0),0) FROM document_docs d WHERE d.active=1 '
                            + ('AND d.scope=? ' if scope is not None else '') +
                            'AND NOT EXISTS(SELECT 1 FROM document_docs newer WHERE newer.scope=d.scope AND newer.resource_key=d.resource_key '
                            'AND (newer.created>d.created OR (newer.created=d.created AND newer.rowid>d.rowid)))',
                            (scope,) if scope is not None else ()).fetchone()
    valid = sum(1 for row in pool if row['id'] in vectors and vectors[row['id']]['digest'] == text_digest(row['text']) and
                worker is not None and vectors[row['id']]['dim'] == worker.dim)
    encoder = bool(worker and worker.status == 'ready' and worker.alive())
    ready = encoder and valid > 0
    reason = ('' if ready else '资料索引尚无有效向量' if encoder else worker.detail or '本地语义模型未就绪' if worker else '语义配置已关闭')
    return {'ready': ready, 'encoder_ready': encoder, 'model': model, 'indexed': valid, 'eligible': len(pool),
            'documents': int(counts[0]), 'unindexed': int(counts[1]),
            'pending': len(pool) - valid, 'mode': 'hybrid' if ready else 'lexical', 'detail': reason,
            'fts5': True, 'last_query': json.loads(last['state']) if last else None}


def maintain(root, *, batch=64):
    import_legacy_web(root)
    from memory_engine import _worker, _config, text_digest, _unpack
    worker = _worker(Path(root), 'qq')
    if worker is None or worker.status != 'ready':
        return {'count': 0, 'status': 'model-not-ready'}
    with database(_prepare(root)) as db:
        rows = [dict(r) for r in db.execute('SELECT c.id,c.text FROM document_chunks c JOIN document_docs d ON d.doc_id=c.doc_id WHERE d.active=1')]
        stored = {r['id']: dict(r) for r in db.execute('SELECT id,model,digest FROM document_embeddings')}
    pending = [r for r in rows if stored.get(r['id'], {}).get('model') != worker.model or stored.get(r['id'], {}).get('digest') != text_digest(r['text'])][:batch]
    if not pending:
        return {'count': 0, 'status': 'ready'}
    reply = worker.request({'op': 'embed', 'texts': [r['text'] for r in pending]}, 60)
    if not reply.get('ok') or len(reply.get('vectors', [])) != len(pending):
        return {'count': 0, 'status': 'error', 'error': reply.get('error') or '嵌入返回数量不匹配'}
    with database(_prepare(root)) as db:
        for row, value in zip(pending, reply['vectors']):
            blob = bytes.fromhex(value)
            db.execute('INSERT OR REPLACE INTO document_embeddings VALUES(?,?,?,?,?,?)',
                       (row['id'], worker.model, len(_unpack(blob)), text_digest(row['text']), blob, time.time()))
    return {'count': len(pending), 'status': 'ready'}


def search(root, query, *, limit=5, scope='owner', with_status=False, document_ids=None):
    """Search current revisions; None reads every source in this physical instance."""
    import_legacy_web(root)
    tokens, phrase = tokens_of(query)
    sync_index(root)
    rows = _pool(root, scope)
    if document_ids is not None:
        rows = [row for row in rows if row['doc_id'] in document_ids]
    candidates = _fts_candidates(root, query) if tokens else {row['id'] for row in rows}
    fts5 = candidates is not None
    if candidates is None:
        # No usable FTS index: stay lexical over the same pool, never blank.
        candidates = {row['id'] for row in rows}
    lexical = {r['id']: _score(r['text'].casefold(), tokens, phrase) for r in rows
               if r['id'] in candidates}
    lexical = {key: value for key, value in lexical.items() if value > 0}
    status = retrieval_status(root, scope=scope)
    semantic, reason = {}, status['detail']
    if status['ready'] and str(query).strip():
        try:
            from memory_engine import embed_query, _cosine, _unpack, text_digest
            vector = embed_query(Path(root), str(query))
            if vector is None:
                raise ValueError('本地语义查询没有返回有效向量')
            with database(_prepare(root)) as db:
                stored = {r['id']: dict(r) for r in db.execute('SELECT * FROM document_embeddings WHERE model=?', (status['model'],))}
            for row in rows:
                item = stored.get(row['id'])
                if item and item['dim'] == len(vector) and item['digest'] == text_digest(row['text']):
                    score = _cosine(vector, _unpack(item['vector']))
                    if score >= .15:
                        semantic[row['id']] = score
        except Exception as exc:
            reason = '语义查询失败：' + str(exc)[:200]
    # Reciprocal rank fusion combines genuine independent channels, never a
    # lexical score relabelled semantic. Every eligible old chunk can compete.
    channels = [lexical] + ([semantic] if semantic else [])
    fused = {}
    for channel in channels:
        for rank, ident in enumerate(sorted(channel, key=lambda key: (-channel[key], key)), 1):
            fused[ident] = fused.get(ident, 0.) + 1. / (60 + rank)
    hits = []
    by_id = {r['id']: r for r in rows}
    for ident in sorted(fused, key=lambda key: (-fused[key], key))[:max(1, min(50, int(limit)))]:
        row = by_id[ident]
        with database(_prepare(root)) as db:
            nearby = db.execute('SELECT ord,text FROM document_chunks WHERE doc_id=? AND ord BETWEEN ? AND ? ORDER BY ord',
                                (row['doc_id'], row['ord']-1, row['ord']+1)).fetchall()
            links = [dict(link) for link in db.execute('SELECT reference,created FROM document_links WHERE doc_id=?', (row['doc_id'],))]
        hits.append({'doc_id': row['doc_id'], 'doc_name': row['doc_name'], 'ord': row['ord'], 'text': row['text'],
                     'score': lexical.get(ident, 0.), 'semantic': semantic.get(ident), 'fused': fused[ident],
                     'matched_by': ('fts5' if fts5 else 'lexical') + ('+semantic' if ident in semantic else ''),
                     'heading': row.get('heading') or '', 'parent': row['parent'], 'context': [dict(r) for r in nearby],
                     'source': row['source'], 'scope': row['scope'], 'metadata': row['metadata'],
                     'version': row['version'], 'parent_version': row['parent_version'], 'resource_key': row['resource_key'],
                     'links': links, 'url': row['url'], 'original_path': row['original_path'], 'fetched_at': row['fetched_at']})
    used = bool(semantic)
    label = '词法＋语义' if used else '仅词法：本次语义未命中' if status['ready'] else '仅词法：语义未就绪'
    state = {'mode': 'hybrid' if used else 'lexical', 'label': label,
             'ready': status['ready'], 'model': status['model'], 'detail': '' if semantic else reason or '本次语义未命中',
             'status': 'matched' if hits else 'not-indexed' if status['documents'] and not rows else 'no-match',
             'documents': status['documents'], 'eligible': status['eligible'], 'unindexed': status['unindexed'],
             'indexed': status['indexed'], 'pending': status['pending'], 'query_used_semantic': bool(semantic),
             'fts5': fts5, 'lexical_candidates': len(candidates)}
    with database(_prepare(root)) as db:
        db.execute('INSERT OR REPLACE INTO document_search_state VALUES(?,?,?)', (scope if scope is not None else '*', json.dumps(state, ensure_ascii=False), time.time()))
    return {'results': hits, 'retrieval': state} if with_status else hits


def organize(root, action, *, scope='owner', name='', doc_id=''):
    base = library_root(root, scope)
    if action == 'list':
        folder = base / 'folders'
        return {'ok': True, 'shelf': str(base), 'folders': [p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_dir()] if folder.exists() else [],
                'documents': list_docs(root, limit=10000, scope=scope), 'scope': scope}
    parts = str(name).replace('\\', '/').split('/') if name else []
    if any(not part or part in ('.','..') or any(c in part for c in ':*?"<>|\x00') for part in parts):
        raise ValueError('分类目录包含无效名称，请按层级给出文件夹名')
    folder = base / 'folders' / Path(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    if action == 'create':
        return {'ok': True, 'folder': str(folder), 'scope': scope}
    if action != 'move':
        raise ValueError('整理动作应为create、list或move')
    row = doc_detail(root, doc_id, scope=scope)
    original = Path(row['original_path'])
    if not original.is_file() or not original.is_relative_to(base):
        raise ValueError('原件不在当前分类根，请先归档资源；未移动外部文件')
    target = folder / original.name
    if target != original:
        if target.exists():
            raise ValueError('分类目录已有同名文件，未覆盖')
        original.replace(target)
    with database(_prepare(root)) as db:
        db.execute('UPDATE document_docs SET original_path=?,folder=? WHERE doc_id=?', (str(target), '/'.join(parts), row['doc_id']))
    return {'ok': True, 'doc_id': row['doc_id'], 'path': str(target), 'folder': '/'.join(parts)}


def import_legacy_web(root):
    """Register existing archived originals once; sidecars are historical inputs."""
    base = library_root(root)
    path = _prepare(root)
    with database(path) as db:
        imported = {row[0] for row in db.execute('SELECT source FROM document_imports')}
    for index in base.glob('**/web/index/*.json'):
        key = str(index.relative_to(base))
        if key in imported:
            continue
        try:
            record = json.loads(index.read_text(encoding='utf-8'))
            shelf = index.parent.parent.parent
            scope = 'owner'
            if shelf != base:
                group = re.search(r'([0-9]{5,15})$', shelf.name)
                if group is None:
                    raise ValueError('历史群资料目录无法确定所属群')
                scope = 'chat:group:' + group.group(1)
            original = (shelf / str(record['rel_path'])).resolve()
            if not original.is_relative_to(base) or not original.is_file():
                raise ValueError('历史资料原件缺失或路径不在Fish资料根')
            text = original.read_text(encoding='utf-8')
            from datetime import datetime
            stamp = record.get('saved_at') or record.get('fetched_at')
            created = datetime.fromisoformat(str(stamp)).timestamp() if stamp else original.stat().st_mtime
            saved = ingest(root, str(record.get('title') or original.name)[:MAX_NAME_CHARS], text, scope=scope, created_at=created,
                           source='web_source', url=str(record.get('url') or ''), fetched_at=str(record.get('fetched_at') or ''),
                           original_path=str(original), metadata={'legacy_index': key, 'truncated': bool(record.get('truncated'))})
            with database(path) as db:
                db.execute('INSERT OR IGNORE INTO document_aliases VALUES(?,?,?)', (str(record['doc_id']), scope, saved['doc_id']))
                db.execute('INSERT OR IGNORE INTO document_imports VALUES(?,?)', (key, time.time()))
                db.execute('DELETE FROM document_import_errors WHERE source=?', (key,))
        except (OSError, ValueError, KeyError) as exc:
            with database(path) as db:
                db.execute('INSERT OR REPLACE INTO document_import_errors VALUES(?,?,?)', (key, str(exc)[:400], time.time()))
