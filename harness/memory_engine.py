"""Owner-aware memory retrieval engine: lexical + true semantic, fused by rank.

Design
------
* The heavy embedding stack (fastembed/onnxruntime, optionally faiss) lives
  only in the worker subprocess ``memory_embeddings_worker.py`` started with
  ``vendor/runtime/memory-python/Scripts/python.exe``. This module is imported
  by the UI/ToolHost process and imports the standard library only.
* Vectors are stored in the role's ``memory.db`` (``memory_embeddings``) as
  float32 BLOBs keyed by memory id plus a text digest, so stale vectors are
  ignored rather than deleted. FAISS is used inside the worker when both the
  worker and the manager can import it; even then SQLite is the source of
  truth and can rebuild the index at any time.
* There is exactly one long-lived worker per (root, model), guarded by a
  process mutex plus a short request lock. Loading happens only through
  ``warm()``/``maintenance()`` in a background thread; a recall never starts a
  load and only waits ``RECALL_LOAD_WAIT_S`` on an already-warming worker,
  otherwise it returns lexical results immediately.
* Semantic readiness is reported honestly: ``semantic.status`` is one of
  ``ready``, ``loading``, ``model-missing``, ``disabled`` or ``error`` and
  ``semantic.mode`` is ``hybrid`` only when vectors were actually used.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import math
import os
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import memory_source
from storage import database, read_json, role_dir

DEFAULT_MODEL = 'BAAI/bge-small-zh-v1.5'
DEFAULT_DIM = 512
CACHE_RELATIVE = 'data/shared/models'
WORKER_RELATIVE = ('harness', 'memory_embeddings_worker.py')
VENV_PYTHON_RELATIVE = ('vendor', 'runtime', 'memory-python', 'Scripts', 'python.exe')
CONFIG_RELATIVE = ('harness', 'memory-embeddings.json')
REQUEST_TIMEOUT_S = 10.0        # embedding / query call while the model is ready
LOAD_HANDSHAKE_TIMEOUT_S = 120  # start the process + `hello` (python import time)
RECALL_LOAD_WAIT_S = 1.5        # how long one recall waits for a warming model
WARM_INTERVAL_S = 20            # retry interval after model-missing / error
MAX_PENDING_BATCH = 64
VECTOR_WEIGHTS = {'lexical': 0.35, 'semantic': 0.65}
RRF_K = 60

_workers: Dict[Tuple[str, str], 'EmbeddingWorker'] = {}
_workers_lock = threading.Lock()
_threads: Dict[Tuple[str, str], threading.Thread] = {}
_scheduler: Dict[str, float] = {}
_scheduler_lock = threading.Lock()


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def text_digest(text: str) -> str:
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def embedding_text(row) -> str:
    """Text embedded for one memory: identity context + content."""
    parts = [str(row['text'] or '')]
    if row.get('category'):
        parts.append('类别：' + str(row['category']))
    if row.get('kind'):
        parts.append('类型：' + str(row['kind']))
    if row.get('subject'):
        parts.append('主体：' + str(row['subject']))
    return '\n'.join(parts)[:4000]


def _config(root: Path) -> dict:
    base = {'enabled': True, 'model': DEFAULT_MODEL, 'dim': 0, 'cache_dir': CACHE_RELATIVE,
            'venv': 'vendor/runtime/memory-python', 'faiss': True}
    try:
        base.update(read_json(Path(root).joinpath(*CONFIG_RELATIVE), {}))
    except (OSError, ValueError):
        pass
    return base


def _role_memory_path(root: Path, role: str) -> Path:
    return role_dir(root, role) / 'memory.db'


def faiss_available() -> bool:
    """Only meaningful in the UI process: used to decide where searches run."""
    try:
        import faiss  # noqa: F401
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def _python_for(root: Path, config: dict) -> Optional[Path]:
    candidates = []
    venv = config.get('venv')
    if venv:
        base = Path(root) / str(venv)
        candidates += [base / 'Scripts' / 'python.exe', base / 'bin' / 'python']
    candidates.append(Path(root).joinpath(*VENV_PYTHON_RELATIVE))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _worker_script(root: Path) -> Path:
    return Path(root).joinpath(*WORKER_RELATIVE)


def _pack(vector: List[float]) -> bytes:
    values = [float(value) for value in vector]
    return struct.pack('<%df' % len(values), *values)


def _unpack(blob: bytes) -> List[float]:
    return list(struct.unpack('<%df' % (len(blob) // 4), blob))


def _cosine(left: List[float], right: List[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    na = math.sqrt(sum(a * a for a in left))
    nb = math.sqrt(sum(b * b for b in right))
    return dot / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------------------
# worker process manager
# --------------------------------------------------------------------------

class EmbeddingWorker:
    """One long-lived embedding subprocess with a JSON-lines handshake."""

    def __init__(self, root: Path, role: str, *, python: Path, script: Path, model: str,
                 cache_dir: Path, want_faiss: bool, log_path: Path):
        self.root, self.role = Path(root), role
        self.python, self.script, self.model = Path(python), Path(script), model
        self.cache_dir, self.want_faiss = Path(cache_dir), bool(want_faiss)
        self.log_path = Path(log_path)
        self.mutex = threading.RLock()     # request + lifecycle serialisation
        self.lock = threading.Lock()
        self.process: Optional[subprocess.Popen] = None
        self.status = 'stopped'
        self.detail = ''
        self.dim = 0
        self.faiss = False
        self.started_at = 0.0
        self.ready_at = 0.0
        self.next_attempt = 0.0
        self.request_id = 0
        self.available = True
        self.index_synced = False
        self._stderr_tail: List[str] = []

    # -- logging -----------------------------------------------------------
    def log(self, message: str, **fields) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({'t': time.strftime('%Y-%m-%d %H:%M:%S'), 'role': self.role,
                               'msg': str(message)[:2000], **fields}, ensure_ascii=False)
            with self.log_path.open('a', encoding='utf-8') as handle:
                handle.write(line + '\n')
        except OSError:
            pass

    def _start_stderr_pump(self, process) -> None:
        def pump():
            try:
                for raw in iter(process.stderr.readline, b''):
                    text = raw.decode('utf-8', 'replace').rstrip()
                    if not text:
                        continue
                    self._stderr_tail = (self._stderr_tail + [text])[-40:]
                    self.log('worker', stderr=text[:1500])
            except (OSError, ValueError):
                return
        thread = threading.Thread(target=pump, daemon=True, name='fish-memory-emb-stderr')
        thread.start()

    # -- lifecycle ----------------------------------------------------------
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _spawn(self) -> None:
        problem = ''
        if not self.python.is_file():
            problem = f'模型环境不存在：{self.python}'
        elif not self.script.is_file():
            problem = f'嵌入工作进程不存在：{self.script}'
        if problem:
            self.status, self.detail = 'model-missing', problem
            self.next_attempt = time.time() + WARM_INTERVAL_S
            self.log('cannot start', error=problem)
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUTF8'] = '1'
        env['HF_HUB_DISABLE_TELEMETRY'] = '1'
        env['HF_HOME'] = str(self.cache_dir / 'hf')
        env['HF_XET_CACHE'] = str(self.cache_dir / 'xet')
        env['HF_HUB_DISABLE_XET'] = '1'
        env['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'
        try:
            self.process = subprocess.Popen(
                [str(self.python), str(self.script)], cwd=str(self.root),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as exc:
            self.status, self.detail = 'error', f'无法启动嵌入工作进程：{exc}'
            self.next_attempt = time.time() + WARM_INTERVAL_S
            self.log('spawn failed', error=str(exc))
            return
        self.started_at = time.time()
        self._start_stderr_pump(self.process)
        hello = self._roundtrip({'op': 'hello'}, LOAD_HANDSHAKE_TIMEOUT_S)
        if not hello.get('ok'):
            self._fail(hello.get('error') or '嵌入工作进程未响应')
            return
        self.log('worker hello', pid=hello.get('pid'), python=hello.get('python'), faiss=hello.get('faiss'))
        self.status, self.detail = 'loading', ''
        reply = self._roundtrip({'op': 'load', 'model': self.model, 'cache_dir': str(self.cache_dir),
                                 'dim': DEFAULT_DIM, 'faiss': bool(hello.get('faiss')) and self.want_faiss,
                                 'local_files_only': False}, LOAD_HANDSHAKE_TIMEOUT_S)
        if not reply.get('ok'):
            detail = reply.get('error') or '模型加载失败'
            self._fail(detail)
            lowered = detail.lower()
            if 'could not load model' in lowered or 'could not download' in lowered:
                self.status = 'model-missing'
                self.detail = ('模型文件不可用（需要下载或网络）：' + detail)[:500]
            return
        self.dim = int(reply.get('dim') or 0)
        self.faiss = bool(hello.get('faiss')) and self.want_faiss
        self.index_synced = False
        self.status, self.detail, self.ready_at = 'ready', '', time.time()
        self.log('model ready', model=self.model, dim=self.dim, faiss=self.faiss)

    def _roundtrip(self, payload: dict, timeout: float) -> dict:
        process = self.process
        if process is None or process.poll() is not None:
            return {'ok': False, 'error': '嵌入工作进程未运行'}
        with self.lock:
            self.request_id += 1
            request_id = self.request_id
            try:
                process.stdin.write((json.dumps({'id': request_id, **payload}, ensure_ascii=False) + '\n').encode('utf-8'))
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                return {'ok': False, 'error': f'写入工作进程失败：{exc}'}
            result: dict = {}

            def read():
                try:
                    line = process.stdout.readline()
                    result['reply'] = json.loads(line.decode('utf-8')) if line else {'ok': False, 'error': '工作进程已退出'}
                except Exception as exc:  # noqa: BLE001 - reported to caller
                    result['reply'] = {'ok': False, 'error': f'读取工作进程失败：{exc}'}
            reader = threading.Thread(target=read, daemon=True, name='fish-memory-emb-read')
            reader.start()
            reader.join(timeout)
            if reader.is_alive():
                self.log('request timeout', op=payload.get('op'), timeout=timeout)
                self._kill()
                return {'ok': False, 'error': f'嵌入工作进程响应超时（{payload.get("op")}）'}
            reply = result.get('reply') or {'ok': False, 'error': '无响应'}
            if reply.get('id') != request_id:
                return {'ok': False, 'error': '嵌入工作进程应答错位'}
            return reply

    def _fail(self, detail: str) -> None:
        self.status, self.detail = 'error', str(detail)[:500]
        self.next_attempt = time.time() + WARM_INTERVAL_S
        self.log('worker error', error=self.detail)
        self._kill()

    def _kill(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001 - best effort shutdown
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def ensure_started(self, *, force: bool = False) -> bool:
        with self.mutex:
            if self.alive() and self.status in ('ready', 'loading'):
                return True
            if not force and time.time() < self.next_attempt and self.status in ('model-missing', 'error'):
                return False
            if self.alive():
                self._kill()
            self._spawn()
            return self.alive()

    def request(self, payload: dict, timeout: float = REQUEST_TIMEOUT_S) -> dict:
        if not self.ensure_started():
            return {'ok': False, 'error': self.detail or '嵌入模型未就绪', 'status': self.status}
        reply = self._roundtrip(payload, timeout)
        if not reply.get('ok') and not self.alive():
            self._fail(reply.get('error') or '嵌入工作进程已退出')
        return reply

    def shutdown(self) -> None:
        with self.mutex:
            if self.alive() and self.status == 'ready':
                try:
                    self._roundtrip({'op': 'shutdown'}, 2.0)
                except Exception:  # noqa: BLE001
                    pass
            self._kill()
            self.status = 'stopped'

    def state(self) -> dict:
        return {'status': self.status, 'detail': self.detail, 'model': self.model, 'dim': self.dim,
                'faiss': self.faiss, 'pid': self.process.pid if self.alive() else 0,
                'stderr_tail': self._stderr_tail[-3:]}


def _worker(root: Path, role: str) -> Optional[EmbeddingWorker]:
    config = _config(root)
    if not config.get('enabled', True):
        return None
    python = _python_for(root, config)
    script = _worker_script(root)
    cache = (Path(root) / str(config.get('cache_dir') or CACHE_RELATIVE)).resolve()
    model = str(config.get('model') or DEFAULT_MODEL).strip()
    key = (str(Path(root).resolve()), model)
    with _workers_lock:
        worker = _workers.get(key)
        created = worker is None
        if created:
            worker = EmbeddingWorker(root, role, python=python or Path('python'), script=script, model=model,
                                     cache_dir=cache, want_faiss=bool(config.get('faiss', True)),
                                     log_path=Path(root) / 'data' / 'memory-embeddings.log')
            worker.available = python is not None and script.is_file()
            _workers[key] = worker
        return worker


def _shutdown_all() -> None:
    with _workers_lock:
        workers = list(_workers.values())
    for worker in workers:
        try:
            worker.shutdown()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_shutdown_all)


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------

class MemoryEngine:
    def __init__(self, root: Path, role: str):
        self.root, self.role = Path(root), role
        from memory_store import MemoryStore
        MemoryStore(self.root, role)
        self.path = _role_memory_path(root, role)
        self.config = _config(root)

    # -- vector storage -----------------------------------------------------
    def _stored(self) -> Dict[str, dict]:
        with database(self.path) as db:
            return {row['memory_id']: dict(row) for row in db.execute(
                'SELECT memory_id,model,dim,digest,vector,source,kind,evidence,scope,observed,created FROM memory_embeddings')}

    def _model_vectors(self, stored=None) -> Dict[str, dict]:
        stored = self._stored() if stored is None else stored
        return {key: value for key, value in stored.items() if value['model'] == self.config['model']}

    def embedding_status(self) -> dict:
        """Honest readiness of the semantic channel (no loading side effects)."""
        worker = _worker(self.root, self.role)
        stored = self._model_vectors()
        dims = {value['dim'] for value in stored.values()}
        if worker is None:
            ready, status, detail = False, 'disabled', '语义检索已关闭'
        else:
            status = worker.status if worker.available else 'model-missing'
            ready = status == 'ready' and worker.dim > 0
            detail = worker.detail if worker.available else f'未找到模型环境 {_python_for(self.root, self.config)}'
        with database(self.path) as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM memories WHERE status IN ('active','candidate') AND (expires=0 OR expires>?)", (time.time(),))]
        eligible = len(rows)
        embedded = sum(1 for row in rows if (stored.get(row['id']) or {}).get('digest') == text_digest(embedding_text(row)))
        return {'mode': 'hybrid' if ready else 'lexical', 'status': status, 'ready': ready,
                'model': self.config['model'], 'dim': worker.dim if worker else 0, 'faiss': bool(worker and worker.faiss),
                'stored_vectors': len(stored), 'stored_dims': sorted(dims), 'embedded_memories': embedded,
                'eligible_memories': eligible, 'pending': max(0, eligible - embedded),
                'detail': detail[:400] if detail else '', 'cache_dir': str(Path(self.root) / str(self.config.get('cache_dir') or CACHE_RELATIVE)),
                'log': str(Path(self.root) / 'data' / 'memory-embeddings.log')}

    def semantic_status(self) -> dict:
        status = self.embedding_status()
        return {key: status[key] for key in ('status', 'ready', 'mode', 'model', 'pending', 'detail')}

    # -- warming / backfill -------------------------------------------------
    def warm(self, *, wait: float = 0.0) -> bool:
        """Start (or wait briefly for) the model worker. Safe to call often."""
        worker = _worker(self.root, self.role)
        if worker is None or not worker.available:
            return False
        started = worker.ensure_started()
        if wait > 0 and worker.status == 'loading':
            end = time.time() + wait
            while time.time() < end and worker.status == 'loading':
                time.sleep(0.05)
        return started and worker.status == 'ready'

    def backfill(self, limit: int = MAX_PENDING_BATCH) -> dict:
        """Embed up to ``limit`` memories lacking a current vector."""
        worker = _worker(self.root, self.role)
        if worker is None or worker.status != 'ready':
            return {'ok': False, 'status': worker.status if worker else 'disabled', 'count': 0}
        stored = self._model_vectors()
        with database(self.path) as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM memories WHERE status IN ('active','candidate') AND (expires=0 OR expires>?) "
                "ORDER BY updated DESC LIMIT 4000", (time.time(),))]
        pending = [row for row in rows if (stored.get(row['id']) or {}).get('digest') != text_digest(embedding_text(row))]
        if worker.faiss:
            # The worker's FAISS index may have just started (or restarted):
            # rebuild it from SQLite so query searches cover every vector.
            ids = [row['id'] for row in rows if row['id'] in stored]
            worker.request({'op': 'rebuild', 'dim': self.config['dim'] if self.config.get('dim') else 0,
                            'ids': ids, 'vectors': [stored[key]['vector'].hex() for key in ids]})
            worker.index_synced = True
        if not pending:
            return {'ok': True, 'status': 'ready', 'count': 0}
        pending = pending[:max(1, min(MAX_PENDING_BATCH, int(limit)))]
        reply = worker.request({'op': 'embed', 'texts': [embedding_text(row) for row in pending]}, REQUEST_TIMEOUT_S * 6)
        if not reply.get('ok'):
            return {'ok': False, 'status': 'error', 'count': 0, 'error': reply.get('error', '')[:300]}
        self._store_vectors(pending, [bytes.fromhex(value) for value in reply.get('vectors', [])], worker)
        return {'ok': True, 'status': 'ready', 'count': len(pending)}

    def _store_vectors(self, rows: List[dict], blobs: List[bytes], worker: EmbeddingWorker) -> None:
        now = time.time()
        vectors = []
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            for row, blob in zip(rows, blobs):
                vector = _unpack(blob)
                dim = len(vector)
                db.execute('INSERT INTO memory_embeddings(memory_id,model,dim,digest,vector,source,kind,evidence,scope,observed,created) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET '
                           'model=excluded.model,dim=excluded.dim,digest=excluded.digest,vector=excluded.vector,'
                           'source=excluded.source,kind=excluded.kind,evidence=excluded.evidence,scope=excluded.scope,'
                           'observed=excluded.observed,created=excluded.created',
                           (row['id'], self.config['model'], dim, text_digest(embedding_text(row)), _pack(vector),
                            row.get('source', ''), row.get('kind', ''), row.get('evidence', ''), row.get('scope', ''),
                            float(row.get('observed') or 0), now))
                vectors.append((row['id'], blob))
        # Keep the worker's FAISS index (when the worker process owns it) in step.
        if worker is not None and worker.faiss:
            worker.request({'op': 'emplace', 'ids': [key for key, _ in vectors],
                            'vectors': [blob.hex() for _, blob in vectors]}, REQUEST_TIMEOUT_S * 3)

    def maintenance(self) -> dict:
        """Warm the model and backfill a bounded batch; returns a summary."""
        warmed = self.warm()
        if not warmed:
            driver = _worker(self.root, self.role)
            return {'warmed': False, 'backfilled': 0, 'status': driver.status if driver else 'disabled'}
        result = self.backfill()
        return {'warmed': True, 'backfilled': result.get('count', 0), 'status': result.get('status')}

    # -- retrieval ------------------------------------------------------------
    def query_vector(self, query: str) -> Optional[List[float]]:
        """Embed one query in the ready worker; None when it is not available."""
        worker = _worker(self.root, self.role)
        if worker is None or worker.status != 'ready':
            return None
        reply = worker.request({'op': 'embed', 'texts': [query[:1000]]}, REQUEST_TIMEOUT_S * 3)
        self._faiss_hits = None
        if not reply.get('ok'):
            return None
        vector = _unpack(bytes.fromhex(reply['vectors'][0]))
        stored = self._model_vectors()
        if worker.faiss and not worker.index_synced:
            # Vectors whose digest does not match the current text are stale
            # and must not enter the worker's search index.
            current = [key for key, row in stored.items()
                       if row['digest'] == text_digest(embedding_text(self._memory_row(key)))]
            rebuilt = worker.request({'op': 'rebuild', 'dim': len(vector), 'ids': current,
                                      'vectors': [stored[key]['vector'].hex() for key in current]})
            worker.index_synced = bool(rebuilt.get('ok'))
        if worker.faiss:
            hits = worker.request({'op': 'search', 'vector': bytes(_pack(vector)).hex(), 'limit': 300},
                                  REQUEST_TIMEOUT_S * 3)
            if hits.get('ok') and 'hits' in hits:
                self._faiss_hits = {key: round(float(score), 6) for score, key in hits['hits']}
        return vector

    def _memory_row(self, memory_id):
        with database(self.path) as db:
            row = db.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
        return dict(row) if row else {}

    def _cosine_scores(self, query_vector: List[float], rows: List[dict]) -> Dict[str, float]:
        # The worker's FAISS index already holds current vectors (it is
        # rebuilt from SQLite after a restart); otherwise compute cosine in
        # Python. Vectors from another dimension (model changed) are skipped;
        # only vectors matching this query's dimension are comparable, the
        # rest wait for backfill. Stale digests (edited text) are ignored too.
        hits = getattr(self, '_faiss_hits', None)
        if hits:
            return {row['id']: hits[row['id']] for row in rows if row['id'] in hits}
        stored = self._model_vectors()
        scores = {}
        for row in rows:
            vector = stored.get(row['id'])
            if not vector or vector['digest'] != text_digest(embedding_text(row)) or vector['dim'] != len(query_vector):
                continue
            scores[row['id']] = round(_cosine(query_vector, _unpack(vector['vector'])), 6)
        return scores

    def recall(self, store, query='', *, scope, limit=8, subject=None, min_relevance=0.15, with_status=False):
        """Hybrid recall. Returns a list of records, or a dict with status when asked."""
        query = '' if query is None else str(query).strip()
        status = self.embedding_status()
        query_vector = None
        if query and status['ready'] and self.warm(wait=RECALL_LOAD_WAIT_S):
            # Only wait briefly on an already-warming worker; never start a
            # (possibly downloading) model from the recall path.
            query_vector = self.query_vector(query)
            if query_vector is None:
                status = {**status, **self.embedding_status()}
        semantic_used = query_vector is not None
        lexical = store.search(query, scope=scope, limit=60, subject=subject)
        lexical_scores = {row['id']: float(row['lexical']) for row in lexical}
        candidate_rows = lexical
        semantic_scores: Dict[str, float] = {}
        if semantic_used:
            # Semantic recall must be able to surface paraphrases that share no
            # token with the query, so compare against the whole visible pool.
            with database(self.path) as db:
                visible = store._rows_for_scope(db, scope, subject)
            by_id = {row['id']: row for row in lexical}
            now = time.time()
            for raw in visible:
                row = dict(raw)
                if row['id'] in by_id or row['status'] != 'active' or (row['expires'] and row['expires'] <= now):
                    continue
                if subject is not None and row['subject'] != subject:
                    continue
                row['lexical'] = 0.0
                by_id[row['id']] = row
            candidate_rows = list(by_id.values())[:3000]
            semantic_scores = self._cosine_scores(query_vector, candidate_rows)
        channels = {'lexical': lexical_scores}
        used = {'lexical': VECTOR_WEIGHTS['lexical']}
        if semantic_used:
            channels['semantic'] = semantic_scores
            used['semantic'] = VECTOR_WEIGHTS['semantic']
        total = max(used.values())
        weights = {key: value / total for key, value in used.items()}
        fused, _calibrated, upstream = memory_source.fuse(self.root, channels, weights, rrf_k=RRF_K)
        ranks = {row_id: index + 1 for index, row_id in enumerate(sorted(fused, key=lambda key: (-fused[key], key)))}
        lexical_pct = memory_source.percentile(lexical_scores)
        results = []
        for row in candidate_rows:
            if row['id'] not in fused:
                continue
            lexical_value = lexical_pct.get(row['id'], 0.0)
            semantic_value = semantic_scores.get(row['id']) if semantic_used else None
            if row['id'] in lexical_scores:
                relevance = ((0.5 * lexical_value + 0.5 * max(0.0, semantic_value))
                             if semantic_value is not None else lexical_value)
            else:
                relevance = max(0.0, semantic_value or 0.0)
            if row['id'] not in lexical_scores and relevance < min_relevance:
                continue
            item = dict(row)
            item.update(score=round(lexical_value, 4), lexical=round(row['lexical'], 4),
                        semantic=round(semantic_value, 4) if semantic_value is not None else None,
                        relevance=round(relevance, 4), fused=round(fused[row['id']], 6), rank=ranks[row['id']],
                        matched_by='+'.join(name for name in ('lexical', 'semantic') if row['id'] in channels.get(name, {})),
                        observed_at=item.get('observed_at') or '')
            results.append(item)
        results.sort(key=lambda item: (item['rank'], -item['relevance'], -item['updated']))
        results = results[:max(1, min(200, int(limit)))]
        channel_status = {'status': 'fused' if semantic_used else 'lexical-only',
                          'mode': 'hybrid' if semantic_used else 'lexical',
                          'model': status['model'], 'ready': status['ready'],
                          'embedding_status': status['status'], 'pending': status['pending'],
                          'vectors': status['stored_vectors'], 'detail': status.get('detail', ''),
                          'fusion': 'weighted_rrf', 'weights': weights, 'upstream': upstream}
        if with_status:
            return {'results': results, 'semantic': channel_status}
        return results


# --------------------------------------------------------------------------
# background warming (no file log spam: state is kept in-process)
# --------------------------------------------------------------------------

def _schedule_reason(root: Path, role: str) -> str:
    key = str(Path(root).resolve()) + '\x00' + role
    with _scheduler_lock:
        last = _scheduler.get(key, 0.0)
        if time.time() - last < 30:
            return ''
        _scheduler[key] = time.time()
        return key


def request_embedding_warm(root: Path, role: str, *, background: bool = True) -> bool:
    """Ensure a worker exists and backfill one bounded batch, preferably async."""
    key = str(Path(root).resolve()) + '\x00' + role
    with _workers_lock:
        if background and key in _threads and _threads[key].is_alive():
            return True

    def work():
        try:
            MemoryEngine(root, role).maintenance()
        except Exception:  # noqa: BLE001 - readiness is reported via status()
            pass
        finally:
            with _workers_lock:
                _threads.pop(key, None)
    if not background:
        work()
        return True
    thread = threading.Thread(target=work, daemon=True, name='fish-memory-emb-warm')
    with _workers_lock:
        _threads[key] = thread
    thread.start()
    return True


def enqueue_warm(root: Path, role: str) -> bool:
    """Rate-limited background warm; returns False when a warm just ran."""
    if not _schedule_reason(root, role):
        return False
    return request_embedding_warm(root, role)


def status(root: Path, role: str = 'qq') -> dict:
    """Public status helper: readiness, pending vectors and upstream source use."""
    return {**MemoryEngine(root, role).embedding_status(), 'source': memory_source.source_status(root)}


def warm_thread(root: Path) -> threading.Thread:
    """Long-lived, low-frequency warmer a host can start once at boot.

    It only talks to the local model worker; it makes no network or model API
    requests. Roles are discovered lazily so later roles are picked up too.
    """
    def loop():
        while True:
            for role in ('qq', 'research'):
                try:
                    if _role_memory_path(root, role).is_file():
                        MemoryEngine(root, role).maintenance()
                except Exception:  # noqa: BLE001 - status() reports the reason
                    pass
            time.sleep(300)
    thread = threading.Thread(target=loop, daemon=True, name='fish-memory-emb-warm-loop')
    thread.start()
    return thread
