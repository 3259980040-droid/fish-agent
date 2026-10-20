"""Local embedding worker: JSON-lines over stdin/stdout, run by the memory venv.

This file is started by ``MemoryEngine`` with the interpreter in
``vendor/runtime/memory-python`` so the UI/host process never imports
fastembed/onnxruntime/faiss. It imports the standard library only until a
``load`` command arrives; ``faiss`` is used only when both sides agree on it.

Contract (one JSON object per line, UTF-8):
  stdin  {"id":int,"op":"hello"}                          -> {"id","ok","pid","python","faiss"}
         {"id":int,"op":"load","model","cache_dir","dim","faiss","local_files_only"}
         {"id":int,"op":"embed","texts":[str]}
         {"id":int,"op":"ping"} | {"op":"shutdown"}
  stdout {"id":int,"ok":bool,"status":str,...}            exactly one reply per id
  stderr free-form diagnostics (the manager appends it to a log file).

Without ``faiss`` the worker returns float32 vector BLOBs built from plain
``struct``; the manager keeps them in SQLite and searches them in Python.
With ``faiss`` it also builds an IndexFlatIP over the manager's copies so the
manager can search through the worker (one long-lived process, no re-import).
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
import traceback

_embedder = None
_faiss = None
_index = None
_ids: list[str] = []
_state = {'status': 'new', 'model': '', 'dim': 0, 'detail': '', 'loaded_at': 0.0}


def _log(message, **fields):
    print(json.dumps({'t': time.strftime('%H:%M:%S'), 'msg': str(message)[:800], **fields}, ensure_ascii=False),
          file=sys.stderr, flush=True)


def _reply(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def _sanitize_proxy_env():
    # A malformed NO_PROXY entry (e.g. an unbracketed IPv6 suffix) makes httpx
    # raise InvalidURL inside huggingface_hub even for a fully cached model.
    for name in ('NO_PROXY', 'no_proxy'):
        raw = os.environ.get(name, '')
        if raw:
            parts = [part.strip() for part in raw.split(',') if part.strip()]
            os.environ[name] = ','.join(part for part in parts if part.count(':') == 0 or part.count(':') == 1
                                        or (part.startswith('[') and part.endswith(']')))


def _pack(vector):
    if _faiss is not None:
        import numpy as np
        return np.asarray(vector, dtype='float32').tobytes()
    values = [float(value) for value in vector]
    return struct.pack('<%df' % len(values), *values)


def _unpack(blob):
    if _faiss is not None:
        import numpy as np
        return np.frombuffer(blob, dtype='float32').copy()
    count = len(blob) // 4
    return list(struct.unpack('<%df' % count, blob))


def _embed_texts(texts):
    if _embedder is None:
        raise RuntimeError('模型尚未加载')
    outputs = list(_embedder.passage_embed(list(texts), batch_size=16))
    return [_pack(vector) for vector in outputs]


def op_hello(request):
    info = {'pid': os.getpid(), 'python': sys.version.split()[0], 'faiss': ''}
    try:
        import faiss
        info['faiss'] = faiss.__version__
    except Exception:
        pass
    return {'status': 'ready', **info}


def op_load(request):
    global _embedder, _faiss, _index, _ids
    _sanitize_proxy_env()
    model = str(request.get('model') or '')
    if not model or '/' not in model or any(token in model for token in ('http:', 'https:', '..', '\\')):
        raise ValueError('模型名不合法')
    cache_dir = os.path.abspath(str(request.get('cache_dir') or ''))
    if not cache_dir:
        raise ValueError('缺少模型缓存目录')
    os.makedirs(cache_dir, exist_ok=True)
    _state.update(status='loading', model=model, detail='', dim=int(request.get('dim') or 0))
    _log('loading model', model=model)
    if request.get('faiss'):
        import faiss
        _faiss = faiss
    import fastembed
    options = {'model_name': model, 'cache_dir': cache_dir, 'threads': 2, 'show_download_progress': False}
    try:
        _embedder = fastembed.TextEmbedding(providers=['CPUExecutionProvider'], **options)
    except Exception as exc:
        if 'Could not load model' not in str(exc) and 'Could not download' not in str(exc):
            raise
        if request.get('local_files_only'):
            raise
        _log('online load failed, retrying with cache only', error=str(exc)[:300])
        _embedder = fastembed.TextEmbedding(**options)
    probe = list(_embedder.passage_embed(['记忆向量自检'], batch_size=1))[0]
    dim = len(probe)
    if _faiss is not None:
        _index = _faiss.IndexFlatIP(dim)
        _ids = []
    _state.update(status='ready', dim=dim, loaded_at=time.time())
    _log('model ready', model=model, dim=dim)
    return {'status': 'ready', 'model': model, 'dim': dim}


def op_embed(request):
    texts = request.get('texts') or []
    if not isinstance(texts, list) or not texts or len(texts) > 64 or any(not isinstance(t, str) for t in texts):
        raise ValueError('texts 需为 1–64 条字符串')
    blobs = _embed_texts([text[:4000] for text in texts])
    return {'status': 'ok', 'vectors': [blob.hex() for blob in blobs], 'dim': _state['dim']}


def op_emplace(request):
    """Store vectors in the worker's FAISS index (manager-provided ids)."""
    if _index is None:
        raise RuntimeError('此进程没有 FAISS')
    ids = request.get('ids') or []
    vectors = [bytes.fromhex(str(value)) for value in request.get('vectors') or []]
    if len(ids) != len(vectors):
        raise ValueError('ids 与 vectors 数量不一致')
    import numpy as np
    matrix = np.vstack([_unpack(blob) for blob in vectors]).astype('float32')
    _index.add(matrix)
    _ids.extend(str(value) for value in ids)
    return {'status': 'ok', 'count': len(_ids)}


def op_rebuild(request):
    global _index, _ids
    if _faiss is None:
        raise RuntimeError('此进程没有 FAISS')
    ids = [str(value) for value in request.get('ids') or []]
    vectors = [bytes.fromhex(str(value)) for value in request.get('vectors') or []]
    if len(ids) != len(vectors):
        raise ValueError('ids 与 vectors 数量不一致')
    dim = int(request.get('dim') or _state['dim'])
    _index = _faiss.IndexFlatIP(dim)
    _ids = []
    if ids:
        import numpy as np
        matrix = np.vstack([_unpack(blob) for blob in vectors]).astype('float32')
        _index.add(matrix)
        _ids = ids
    return {'status': 'ok', 'count': len(_ids)}


def op_search(request):
    if _index is None or not _ids:
        return {'status': 'ok', 'hits': []}
    blob = bytes.fromhex(str(request.get('vector') or ''))
    vector = _unpack(blob)
    import numpy as np
    query = np.asarray(vector, dtype='float32').reshape(1, -1)
    limit = max(1, min(500, int(request.get('limit') or 10)))
    scores, indexes = _index.search(query, min(limit, len(_ids)))
    hits = [(float(score), _ids[int(index)]) for score, index in zip(scores[0], indexes[0]) if int(index) >= 0]
    return {'status': 'ok', 'hits': hits, 'count': len(_ids)}


def op_stats(request):
    return {'status': 'ok', 'count': len(_ids) if _index is not None else 0, 'faiss': bool(_faiss is not None)}


HANDLERS = {'hello': op_hello, 'load': op_load, 'embed': op_embed, 'emplace': op_emplace,
            'rebuild': op_rebuild, 'search': op_search, 'stats': op_stats}


def main():
    _log('worker started', python=sys.version.split()[0])
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = {}
        try:
            request = json.loads(line)
            request_id = request.get('id')
            op = str(request.get('op'))
            if op == 'shutdown':
                _reply({'id': request_id, 'ok': True, 'status': 'shutdown'})
                return 0
            if op == 'ping':
                _reply({'id': request_id, 'ok': True, 'status': _state['status'], 'model': _state['model'], 'dim': _state['dim']})
                continue
            handler = HANDLERS.get(op)
            if handler is None:
                raise ValueError('未知操作')
            result = handler(request)
            _reply({'id': request_id, 'ok': True, **result})
        except Exception as exc:
            _log('error', op=request.get('op'), error=type(exc).__name__ + ': ' + str(exc)[:300], where=traceback.format_exc(limit=2)[-400:])
            if request.get('op') == 'load':
                _state.update(status='error', detail=type(exc).__name__ + ': ' + str(exc)[:300])
            try:
                _reply({'id': request.get('id'), 'ok': False, 'status': 'error',
                        'error': type(exc).__name__ + ': ' + str(exc)[:400]})
            except Exception:
                return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
