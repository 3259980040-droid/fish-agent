"""One graded retrieval entry: FTS5 index hit -> overview -> original.

Fish and the owner both read the same three levels. A hit names the owning
store and a stable reference; the overview is a catalogue entry, a document
chunk, a memory or a digest; the original is read back from that store on
demand. Nothing here builds a second corpus: the indexes are derived and the
originals stay authoritative.

Lexical FTS5 is always present. Semantic scores are added only when the
existing document/memory vector path actually returned one; otherwise the
answer says "仅词法" with the real reason.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import doc_store
import history_catalog
import history_index
import memory_digest
from fts5_index import terms
from memory_store import MemoryStore


def message_reference(source, message_id):
    store = 'desktop' if str(source) in ('desktop', 'desktop-chat.db') else 'qq'
    return 'message:' + store + ':' + str(int(message_id))


def catalog_reference(catalog_id):
    return 'catalog:' + str(int(catalog_id))


def doc_reference(doc_id, ordinal=None):
    value = 'doc:' + str(doc_id)
    return value if ordinal in (None, '') else value + ':' + str(int(ordinal))


def memory_reference(memory_id):
    return 'memory:' + str(memory_id)


def digest_reference(scope, period):
    return 'digest:' + str(scope) + ':' + str(period)


def parse(reference):
    """Split one stable reference into its owning store and identifiers."""
    parts = str(reference or '').split(':')
    if len(parts) < 2:
        raise ValueError('引用格式无效：' + str(reference)[:80])
    kind = parts[0]
    if kind == 'message' and len(parts) == 3:
        return {'kind': 'message', 'store': parts[1], 'id': int(parts[2])}
    if kind == 'catalog' and len(parts) == 2:
        return {'kind': 'catalog', 'id': int(parts[1])}
    if kind == 'doc' and len(parts) in (2, 3):
        return {'kind': 'doc', 'id': parts[1],
                'ord': int(parts[2]) if len(parts) == 3 else None}
    if kind == 'memory' and len(parts) == 2:
        return {'kind': 'memory', 'id': parts[1]}
    if kind == 'digest' and len(parts) == 3:
        return {'kind': 'digest', 'scope': parts[1], 'period': parts[2]}
    raise ValueError('未知引用：' + str(reference)[:80])


def _open_tool(reference, level='original'):
    return {'tool': 'fish_knowledge_open', 'arguments': {'reference': reference, 'level': level}}


def _message_item(root, hit):
    overview = history_catalog.find_range(root, hit['source'], hit['conversation'], hit['id'])
    reference = hit['reference']
    if overview:
        overview_level = {
            'level': 'overview', 'kind': 'catalog', 'available': True,
            'reference': catalog_reference(overview['id']),
            'text': overview.get('gist') or '',
            'topics': overview.get('topics') or '', 'keywords': overview.get('keywords') or '',
            'from_message_id': overview.get('from_message_id'),
            'to_message_id': overview.get('to_message_id'),
            'start_time': overview.get('start_time'), 'end_time': overview.get('end_time'),
            'open': _open_tool(catalog_reference(overview['id'])),
        }
    else:
        overview_level = {
            'level': 'overview', 'kind': 'none', 'available': False,
            'reason': '这段消息不在已压缩的目录范围内；可直接展开原文',
            'open': _open_tool(reference, 'overview'),
        }
    return {
        'reference': reference, 'kind': 'message', 'level': 'index', 'channel': 'messages',
        'snippet': hit['text'], 'store': hit['source'], 'place': hit['place'],
        'time': hit['time'], 'direction': hit['direction'], 'score': hit.get('rank'),
        'levels': {
            'index': {'level': 'index', 'kind': 'message', 'reference': reference,
                      'snippet': hit['text'], 'store': hit['source'], 'place': hit['place'],
                      'time': hit['time'], 'matched_by': 'fts5'},
            'overview': overview_level,
            'original': {'level': 'original', 'kind': 'message', 'reference': reference,
                         'available': True, 'open': _open_tool(reference)},
        },
    }


def _catalog_item(entry):
    reference = catalog_reference(entry['id'])
    return {
        'reference': reference, 'kind': 'catalog', 'level': 'overview', 'channel': 'catalogs',
        'snippet': entry.get('gist') or '', 'store': entry.get('source') or '',
        'time': entry.get('start_time') or '', 'score': None,
        'levels': {
            'index': {'level': 'index', 'kind': 'catalog', 'reference': reference,
                      'snippet': entry.get('gist') or '', 'matched_by': 'fts5'},
            'overview': {'level': 'overview', 'kind': 'catalog', 'reference': reference,
                         'text': entry.get('gist') or '', 'topics': entry.get('topics') or '',
                         'keywords': entry.get('keywords') or '',
                         'from_message_id': entry.get('from_message_id'),
                         'to_message_id': entry.get('to_message_id'),
                         'start_time': entry.get('start_time'), 'end_time': entry.get('end_time'),
                         'open': _open_tool(reference)},
            'original': {'level': 'original', 'kind': 'catalog', 'reference': reference,
                         'available': True,
                         'range': {'source': entry.get('source'),
                                   'conversation': entry.get('conversation'),
                                   'from': entry.get('from_message_id'),
                                   'to': entry.get('to_message_id')},
                         'open': _open_tool(reference)},
        },
    }


def _document_item(hit):
    ordinal = hit['ord']
    chunk_reference = doc_reference(hit['doc_id'], ordinal)
    doc_reference_value = doc_reference(hit['doc_id'])
    matched = 'fts5+semantic' if hit.get('semantic') is not None else 'fts5'
    return {
        'reference': chunk_reference, 'kind': 'document', 'level': 'index', 'channel': 'documents',
        'snippet': str(hit.get('text') or '')[:200], 'store': 'doc:' + str(hit['doc_id']),
        'time': hit.get('fetched_at') or '', 'score': hit.get('fused'),
        'levels': {
            'index': {'level': 'index', 'kind': 'document_chunk', 'reference': chunk_reference,
                      'snippet': str(hit.get('text') or '')[:200], 'matched_by': matched},
            'overview': {'level': 'overview', 'kind': 'document_chunk', 'reference': chunk_reference,
                         'text': hit.get('text') or '', 'heading': hit.get('heading') or '',
                         'context': hit.get('context') or [], 'doc_name': hit.get('doc_name'),
                         'open': _open_tool(chunk_reference)},
            'original': {'level': 'original', 'kind': 'document', 'reference': doc_reference_value,
                         'available': bool(hit.get('original_path')),
                         'original_path': hit.get('original_path') or '', 'url': hit.get('url') or '',
                         'open': _open_tool(doc_reference_value)},
        },
    }


def _memory_item(hit):
    reference = memory_reference(hit['id'])
    matched = str(hit.get('matched_by') or 'lexical')
    return {
        'reference': reference, 'kind': 'memory', 'level': 'index', 'channel': 'memories',
        'snippet': str(hit.get('text') or '')[:200], 'store': 'memory',
        'time': hit.get('observed_at') or '', 'score': hit.get('relevance'),
        'levels': {
            'index': {'level': 'index', 'kind': 'memory', 'reference': reference,
                      'snippet': str(hit.get('text') or '')[:200], 'matched_by': matched,
                      'semantic': hit.get('semantic')},
            'overview': {'level': 'overview', 'kind': 'memory', 'reference': reference,
                         'text': hit.get('text') or '', 'category': hit.get('category') or '',
                         'kind': hit.get('kind') or '', 'status': hit.get('status') or '',
                         'source': hit.get('source') or '', 'scope': hit.get('scope') or '',
                         'open': _open_tool(reference)},
            'original': {'level': 'original', 'kind': 'memory', 'reference': reference,
                         'available': True, 'open': _open_tool(reference)},
        },
    }


def _digest_item(hit):
    reference = hit['reference']
    return {
        'reference': reference, 'kind': 'digest', 'level': 'overview', 'channel': 'digests',
        'snippet': str(hit.get('text') or '')[:200], 'store': 'memory_digest',
        'time': hit.get('period') or '', 'score': hit.get('rank'),
        'levels': {
            'index': {'level': 'index', 'kind': 'digest', 'reference': reference,
                      'snippet': str(hit.get('text') or '')[:200], 'matched_by': 'fts5'},
            'overview': {'level': 'overview', 'kind': 'digest', 'reference': reference,
                         'text': hit.get('text') or '', 'scope': hit.get('scope'),
                         'period': hit.get('period'), 'open': _open_tool(reference)},
            'original': {'level': 'original', 'kind': 'digest', 'reference': reference,
                         'available': True, 'open': _open_tool(reference)},
        },
    }


def _document_hits(root, query, limit, scope):
    scopes = ['owner']
    if scope and str(scope).startswith('chat:group:'):
        scopes.append(str(scope))
    hits, retrieval = [], []
    for current in dict.fromkeys(scopes):
        try:
            result = doc_store.search(root, query, limit=limit, scope=current, with_status=True)
        except Exception as exc:  # noqa: BLE001 - a broken library channel is reported, not fatal
            retrieval.append({'scope': current, 'mode': 'lexical', 'ready': False,
                              'detail': type(exc).__name__ + ': ' + str(exc)[:160], 'fts5': True})
            continue
        for hit in result.get('results') or []:
            if hit['doc_id'] not in {item['doc_id'] for item in hits}:
                hits.append(hit)
        state = result.get('retrieval') or {}
        retrieval.append({'scope': current, **state})
    used = any(state.get('query_used_semantic') for state in retrieval)
    reason = '；'.join(str(state.get('detail') or '') for state in retrieval if state.get('detail'))
    mode = 'hybrid' if used else 'lexical'
    return hits, {'mode': mode, 'used': used, 'scope': scopes,
                  'indexed': sum(int(state.get('indexed') or 0) for state in retrieval),
                  'pending': sum(int(state.get('pending') or 0) for state in retrieval),
                  'detail': '' if used else reason or '资料语义向量未就绪',
                  'fts5': True}


def _memory_hits(root, query, limit):
    store = MemoryStore(root, 'qq')
    try:
        recalled = store.recall(query, scope=None, limit=limit, with_status=True)
        results = recalled.get('results') or []
        semantic = recalled.get('semantic') or {}
        used = semantic.get('status') == 'fused' and any(row.get('semantic') is not None for row in results)
        return results, {'mode': 'hybrid' if used else 'lexical', 'used': used,
                         'status': semantic.get('status') or 'lexical-only',
                         'detail': semantic.get('detail') or ('' if used else '记忆语义向量未就绪'),
                         'pending': semantic.get('pending') or 0, 'fts5': True}
    except Exception as exc:  # noqa: BLE001 - lexical memory remains usable
        rows = store.search(query, scope=None, limit=limit)
        return rows, {'mode': 'lexical', 'used': False, 'status': 'lexical-only',
                      'detail': '记忆语义查询失败：' + type(exc).__name__ + ': ' + str(exc)[:160],
                      'pending': 0, 'fts5': True}


def _semantic_summary(documents, memories):
    used = bool(documents.get('used') or memories.get('used'))
    reasons = []
    if not documents.get('used'):
        reasons.append('资料：' + str(documents.get('detail') or '语义未就绪'))
    if not memories.get('used'):
        reasons.append('记忆：' + str(memories.get('detail') or '语义未就绪'))
    return {
        'used': used, 'status': 'fused' if used else 'lexical-only',
        'mode': 'hybrid' if used else 'lexical',
        'label': '词法＋语义' if used else '仅词法：语义未就绪',
        'reason': '' if used else '；'.join(reasons),
        'query_used_semantic': used,
        'channels': {
            'messages': {'mode': 'lexical', 'used': False, 'fts5': True,
                         'detail': '原始消息只有 FTS5 词法索引，没有向量索引'},
            'catalogs': {'mode': 'lexical', 'used': False, 'fts5': True},
            'documents': documents,
            'memories': memories,
            'digests': {'mode': 'lexical', 'used': False, 'fts5': True},
        },
    }


def search(root, query, *, limit=12, scope=None):
    """Search every owning store, then present index/overview/original levels."""
    root = Path(root)
    query = ' '.join(str(query or '').split())
    limit = max(1, min(50, int(limit)))
    if not query:
        return {'query': '', 'hits': [], 'retrieval': {
            'mode': 'lexical', 'label': '仅词法：语义未就绪', 'fts5': True,
            'reason': '查询为空', 'levels': ['index', 'overview', 'original'],
            'semantic': {'used': False, 'status': 'lexical-only', 'reason': '查询为空'},
            'channels': {}, 'truncated': False, 'limit': limit}}
    message_hits = history_index.search(root, query, limit=max(6, limit * 2))
    catalogs = history_catalog.search_catalog(root, query, limit=max(3, limit // 2))
    documents, document_state = _document_hits(root, query, max(3, limit), scope)
    memories, memory_state = _memory_hits(root, query, max(3, limit))
    digests = memory_digest.search_digests(root, query, limit=max(2, limit // 3)).get('results') or []

    channels = {
        'messages': [_message_item(root, hit) for hit in message_hits],
        'catalogs': [_catalog_item(entry) for entry in (catalogs.get('entries') or [])],
        'documents': [_document_item(hit) for hit in documents],
        'memories': [_memory_item(hit) for hit in memories],
        'digests': [_digest_item(hit) for hit in digests],
    }
    by_reference, fused = {}, {}
    for name, items in channels.items():
        for rank, item in enumerate(items, 1):
            by_reference[item['reference']] = item
            fused[item['reference']] = fused.get(item['reference'], 0.0) + 1.0 / (60 + rank)
    ordered = sorted(fused, key=lambda key: (
        -fused[key],
        0 if by_reference[key].get('level') == 'index' else 1,
        {'messages': 0, 'documents': 1, 'memories': 2, 'catalogs': 3, 'digests': 4}.get(
            by_reference[key].get('channel'), 5),
        key))
    hits = []
    for reference in ordered[:limit]:
        item = dict(by_reference[reference])
        item['fused'] = round(fused[reference], 6)
        hits.append(item)
    semantic = _semantic_summary(document_state, memory_state)
    total = sum(len(items) for items in channels.values())
    return {
        'query': query,
        'hits': hits,
        'retrieval': {
            'mode': semantic['mode'], 'label': semantic['label'], 'fts5': True,
            'semantic': semantic, 'levels': ['index', 'overview', 'original'],
            'channels': {name: len(items) for name, items in channels.items()},
            'terms': {'count': len(terms(query)), 'sample': terms(query)[:12]},
            'limit': limit, 'truncated': total > len(hits), 'matched_total': total,
            'note': '命中是索引引用；概述来自目录/资料块/记忆/小结，原文仍按 reference 回原库展开',
        },
    }


def _open_message(root, ref, *, level, page, count):
    source = 'desktop-chat.db' if ref['store'] in ('desktop', 'desktop-chat.db') else 'qq.db'
    found = history_catalog.read_message(root, source, ref['id'])
    messages = found.get('messages') or []
    if not messages:
        raise ValueError(found.get('note') or '这条原始消息已不存在')
    message = messages[0]
    if level == 'index':
        return {'reference': message_reference(ref['store'], ref['id']), 'kind': 'message',
                'level': 'index', 'snippet': str(message.get('text') or '')[:200]}
    if level == 'overview':
        catalog = history_catalog.find_range(root, source, message.get('conversation'), ref['id'])
        return {'reference': message_reference(ref['store'], ref['id']), 'kind': 'message',
                'level': 'overview', 'catalog': catalog, 'message': message}
    before, after = max(0, int(count) // 2), max(1, int(count) - max(0, int(count) // 2) - 1)
    nearby = history_catalog.context(root, source, message.get('place'), ref['id'],
                                     before=before, after=after)
    return {'reference': message_reference(ref['store'], ref['id']), 'kind': 'message',
            'level': 'original', 'message': message, 'context': nearby.get('messages') or [],
            'attachments': message.get('attachments') or [],
            'note': '原文已回到所属消息库读取；附件只是原件引用，未把正文复制到索引'}


def _open_catalog(root, ref, *, level, page, count):
    entry = history_catalog.entry(root, ref['id'])
    if entry is None:
        raise ValueError('目录条目不存在或已删除')
    reference = catalog_reference(ref['id'])
    if level in ('index', 'overview'):
        return {'reference': reference, 'kind': 'catalog', 'level': level, 'entry': entry}
    result = history_catalog.read_range(
        root, source=entry.get('source'), conversation=entry.get('conversation'),
        from_id=entry.get('from_message_id') or 1, to_id=entry.get('to_message_id') or 1,
        limit=count, offset=(max(1, int(page)) - 1) * count, truncate=0)
    messages = result.get('messages') or []
    return {'reference': reference, 'kind': 'catalog', 'level': 'original',
            'entry': entry, 'messages': messages, 'page': int(page),
            'next_page': int(page) + 1 if len(messages) >= int(count) else None,
            'note': '原文按目录条目的真实消息号区间读取；没有返回的消息是本地没有记录'}


def _open_document(root, ref, *, level, page, count):
    start = ref['ord'] if ref.get('ord') is not None else max(1, int(page))
    try:
        detail = doc_store.doc_detail(root, ref['id'], scope=None, start=start, count=count)
    except ValueError:
        raise ValueError('资料不存在或已由主人删除') from None
    reference = doc_reference(ref['id'], ref.get('ord')) if ref.get('ord') is not None else doc_reference(ref['id'])
    original = Path(str(detail.get('original_path') or ''))
    payload = {'reference': reference, 'kind': 'document', 'level': level,
               'doc_id': detail['doc_id'], 'doc_name': detail.get('name'),
               'version': detail.get('version'), 'chunks': detail.get('chunks'),
               'chars': detail.get('chars'), 'url': detail.get('url') or '',
               'original_path': str(original) if detail.get('original_path') else '',
               'original_exists': bool(detail.get('original_path') and original.is_file()),
               'preview': detail.get('preview') or [], 'next_ord': detail.get('next_ord')}
    chunk = (detail.get('preview') or [None])[0]
    if chunk is not None:
        payload['chunk'] = chunk
    if level in ('index', 'overview'):
        payload['note'] = '资料块是索引命中；需要完整上下文时按 doc_id 继续读取'
    else:
        payload['note'] = ('原件保存在资料目录，按切片分页读取；本回答不声称已读完整文件'
                           if detail.get('original_path') else '这份资料只保存了切片，没有可打开的原件')
    return payload


def _open_memory(root, ref, *, level):
    store = MemoryStore(root, 'qq')
    row = store.get(ref['id'], scope=None)
    reference = memory_reference(ref['id'])
    if level == 'index':
        return {'reference': reference, 'kind': 'memory', 'level': 'index',
                'snippet': str(row.get('text') or '')[:200]}
    history = store.history(ref['id'])
    return {'reference': reference, 'kind': 'memory', 'level': level,
            'memory': row, 'revisions': history,
            'note': '记忆正文、来源和修订都在同一个记忆库；原文出现在 history 的 source_text'}


def open(root, reference, *, level='original', page=1, count=5, scope=None):
    """Expand one stable reference to overview or original, with real paging."""
    root = Path(root)
    ref = parse(reference)
    level = str(level or 'original')
    if level not in ('index', 'overview', 'original'):
        raise ValueError('读取层次需为 index、overview 或 original')
    page, count = max(1, int(page)), max(1, min(20, int(count)))
    if ref['kind'] == 'message':
        return _open_message(root, ref, level=level, page=page, count=count)
    if ref['kind'] == 'catalog':
        return _open_catalog(root, ref, level=level, page=page, count=count)
    if ref['kind'] == 'doc':
        return _open_document(root, ref, level=level, page=page, count=count)
    if ref['kind'] == 'memory':
        return _open_memory(root, ref, level=level)
    if ref['kind'] == 'digest':
        stored = memory_digest.get_digest(root, ref['scope'], ref['period'])
        if stored is None:
            raise ValueError('这条小结不存在')
        return {'reference': reference, 'kind': 'digest', 'level': level, 'digest': stored}
    raise ValueError('未知引用：' + str(reference)[:80])
