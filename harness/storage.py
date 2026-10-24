"""Small cross-process transactions for configuration and role-private SQLite."""
from __future__ import annotations
import contextlib
import json
import os
import sqlite3
import threading
import time
import uuid
import functools
from pathlib import Path

_mutex = threading.RLock()
_local = threading.local()


def locked_settings(function):
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        with settings_lock(self.root):
            return function(self, *args, **kwargs)
    return wrapped


@contextlib.contextmanager
def settings_lock(root: Path, timeout: float = 12):
    key = str(root.resolve())
    with _mutex:
        held = getattr(_local, 'held', {})
        if key in held:
            yield
            return
        path = root / 'harness' / 'settings.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+b') as handle:
            if path.stat().st_size == 0:
                handle.write(b'0'); handle.flush()
            end = time.monotonic() + timeout
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > end:
                        raise TimeoutError('其他进程正在保存设置，请稍后重试')
                    time.sleep(.05)
            held[key] = True
            _local.held = held
            try:
                yield
            finally:
                del held[key]
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)


def read_json(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def commit_files(root: Path, files: dict):
    """Validate and stage all settings before replacing any current file."""
    root = root.resolve()
    prepared, previous = {}, {}
    with settings_lock(root):
        try:
            for path, value in files.items():
                path = Path(path).resolve()
                if not path.is_relative_to(root) or path == root:
                    raise ValueError('配置路径超出数据目录')
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
                temporary.write_bytes(value)
                prepared[path] = temporary
                previous[path] = path.read_bytes() if path.exists() else None
            changed = []
            try:
                for path, temporary in prepared.items():
                    temporary.replace(path); changed.append(path)
            except Exception:
                for path in reversed(changed):
                    old = previous[path]
                    if old is None: path.unlink(missing_ok=True)
                    else: path.write_bytes(old)
                raise
        finally:
            for path in prepared.values(): path.unlink(missing_ok=True)


def role_dir(root: Path, role: str) -> Path:
    if role not in ('qq', 'research'):
        raise ValueError('未知角色')
    path = root / 'data' / role
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextlib.contextmanager
def database(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def thinking_slot(root, role, cancelled=None, *, key=None):
    """Serialize the same Fish's main turns across desktop and QQ processes."""
    path = Path(root) / 'data/social' / (str(key or role) + '-thinking.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        waited = False
        started = time.monotonic()
        while True:
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError('主人已取消等待中的回合')
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if not waited:
                    from governance import Governance
                    Governance(Path(root), role).audit('chat.queued', '主思考正在处理上一轮，当前输入继续等待')
                    waited = True
                time.sleep(.05)
        try:
            if waited:
                from governance import Governance
                Governance(Path(root), role).audit('chat.dequeued', '已进入主思考；实际等待 %.3f 秒' % (time.monotonic() - started))
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


def serial_thinking(function):
    @functools.wraps(function)
    def run(root, role, *args, **kwargs):
        with thinking_slot(root, role, kwargs.get('cancelled'), key=kwargs.get('execution_key')):
            return function(root, role, *args, **kwargs)
    return run
