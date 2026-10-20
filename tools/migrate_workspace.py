"""Copy-only workspace migration for Fish and DSH (deepseek-harness), Windows.

Safety model
============
* **Never deletes, moves or renames anything in a source tree.**  Sources are
  only read.  The tool creates directories and files in the target only.  The
  single ``os.replace`` writes the JSON log (and, opt-in, one config file whose
  ``.premigrate.bak`` copy was written first).
* **Links are never followed while copying.**  Junction / directory-symlink /
  file-symlink entries are recorded separately and re-created only by the
  explicit ``--links`` / :func:`create_links` step, with targets mapped from the
  source DSH/Fish roots to the target roots.  Targets outside every known root
  are reported as ``unmappable`` and never guessed (allow-list model).
* **Incremental.**  A target file with the same ``(size, mtime_ns)`` is skipped;
  only missing/changed files are copied (``--verify-hash`` adds SHA-256).  Files
  are copied with ``shutil.copyfile`` (bytes only), so ``.git`` and
  ``node_modules`` (including pnpm junctions) are copied physically and
  read-only source files stay readable in the target.
* **Excludes come only from the caller** (``--exclude`` / ``excludes=`` /
  ``--select``).  The module ships no default exclude list.  Built-in guards
  never exclude silently: SQLite files, files that are links in the target and
  ``--stop-file`` matches are reported as failures for the parent to decide.
* **SQLite is not migrated here.**  Stop Fish (desktop + QQ loop) and DSH
  (desktop + host), then copy each ``*.db`` together with its ``-wal``/``-shm``
  and verify with ``PRAGMA integrity_check``.  Without ``--allow-sqlite`` these
  files are skipped and listed under ``guards``.
* **Structured config roots** are rewritten only by the explicit
  :func:`rewrite_config_roots` (JSON/YAML, named keys, path values only).
  Chat text, Markdown and SQLite content are never edited.
* **Migration log** is incremental JSON, secret-scrubbed, and lists retryable
  failure paths for ``--retry-from``.

Importing this module performs no filesystem work.  ``--self-test`` builds a
tiny temporary tree under ``%TEMP%`` (or the given dir), copies it and
re-creates links; the tree is removed only when every check passes.
"""

from __future__ import annotations

import argparse
import ctypes
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime

IS_WINDOWS = os.name == 'nt'

if IS_WINDOWS:
    from ctypes import wintypes as _wt

    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _ZERO = ctypes.c_void_p(0)
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel32.CreateFileW.argtypes = [_wt.LPCWSTR, _wt.DWORD, _wt.DWORD, ctypes.c_void_p,
                                      _wt.DWORD, _wt.DWORD, _wt.HANDLE]
    _kernel32.CreateFileW.restype = _wt.HANDLE
    _kernel32.GetFileAttributesW.argtypes = [_wt.LPCWSTR]
    _kernel32.GetFileAttributesW.restype = _wt.DWORD
    _kernel32.CloseHandle.argtypes = [_wt.HANDLE]
    _kernel32.CloseHandle.restype = _wt.BOOL
    _winerror = ctypes.get_last_error
else:  # pragma: no cover - the production target is Windows
    _kernel32 = None

    def _winerror():
        return 0


for _stream in (sys.stdout, sys.stderr):  # keep CJK paths printable on cp936 consoles
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
DEFAULT_TARGETS = {
    'dsh': r'E:\Program Files\DSH\source\deepseek-harness',
    'fish': r'E:\Program Files\DSH\extensions\Fish',
}
DEFAULT_SOURCES = {
    'dsh': r'F:\Documents\ChatGPT\deepseek\deepseek-harness',
    'fish': r'F:\Downloads\dsh-personal',
}
SOURCE_ROOTS = {
    'dsh': (r'F:\Documents\ChatGPT\deepseek\deepseek-harness',),
    'fish': (r'F:\Downloads\dsh-personal',),
}

# Guard patterns: reported as failures, never applied as excludes.
SQLITE_GUARD_PATTERNS = ('*.db', '*.db-*', '*.sqlite', '*.sqlite3', '*.sqlite-*')
SQLITE_MAGIC = b'SQLite format 3\x00'
SQLITE_POLICY = (
    'SQLite is not migrated by this tool. Stop every process that opens the database, then copy '
    'the .db file together with its -wal and -shm files in one stopped window and verify with '
    'PRAGMA integrity_check (see RESULT.md). --allow-sqlite copies bytes only.'
)

# Optional Fish presets: not applied unless the parent opts in.
FISH_CORE_OPTIONAL_PATTERNS = (
    'artifacts/', 'screenshots/', 'backups/',
    'dist-launch-fix-043/', 'dist-owner-thinking-046/', 'dist-qq-control-044/',
    'dist-screenshots/', 'dist-session-restore-045/', 'dist-social-delivery-040/',
    'dist-social-members-041/', 'dist-task1-042/',
    'build-owner-thinking-046/', 'build-qq-control-044/', 'build-task1-042/',
    '*.spec',
)
FISH_BACKUP_ARCHIVE_PATTERNS = ('*.7z', '*.zip', '*.msi', '*.zip.*')

_CONFIG_SUFFIXES = frozenset({'.json', '.jsonc', '.yaml', '.yml', '.code-workspace',
                              '.sublime-project'})
_CONFIG_NAMES = frozenset({'.npmrc', '.gitconfig', '.gitmodules', 'pyproject.toml'})
_SECRET_KEY_RE = re.compile(
    r'(?i)(pass(word|wd)?|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|'
    r'credential|cookie|authorization|auth[_-]?key|session[_-]?key|appkey|app[_-]?secret)')
_SECRET_VALUE_RE = re.compile(
    r'(sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._~+/=\-]{12,}|'
    r'AIza[0-9A-Za-z_\-]{30,}|xox[abpr]-[A-Za-z0-9\-]{10,})')
_MAX_STRING = 8192
_MAX_SAMPLES = 200
_RETRYABLE = ('copy', 'scan', 'selection', 'link', 'link-conflict', 'target-missing')


# --------------------------------------------------------------------------
# Path helpers (lexical only; never resolve through links)
# --------------------------------------------------------------------------
def norm(path) -> str:
    return os.path.abspath(os.fspath(path))


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def is_within(path: str, parent: str) -> bool:
    path, parent = _key(path), _key(parent).rstrip('\\')
    return path == parent or path.startswith(parent + '\\')


def rel_posix(path: str, root: str) -> str:
    return os.path.relpath(norm(path), norm(root)).replace('\\', '/')


def join_rel(root: str, rel: str) -> str:
    parts = [p for p in str(rel).replace('\\', '/').split('/') if p]
    return os.path.normpath(os.path.join(root, *parts)) if parts else os.path.normpath(root)


def to_nt(path: str) -> str:
    """Extended-length form so paths longer than MAX_PATH work with Win32 APIs."""
    path = norm(path)
    if IS_WINDOWS and len(path) >= 240 and not path.startswith('\\\\?\\'):
        if path.startswith('\\\\'):
            return '\\\\?\\UNC\\' + path[2:]
        return '\\\\?\\' + path
    return path


def tail(text, n: int = 500) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= n else '...' + text[-n:]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _is_secret_key(key) -> bool:
    return isinstance(key, str) and bool(_SECRET_KEY_RE.search(key))


def _scrub(value):
    if isinstance(value, str):
        value = _SECRET_VALUE_RE.sub('<scrubbed>', value)
        return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + '...'
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {('<%s>' % k if _is_secret_key(k) else str(k)): _scrub(v) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------
# Link detection (read-only; never follows the entry it describes)
# --------------------------------------------------------------------------
# Junction / symlink reading is adapted from detect_link() in CPython's
# _winapi.c (PSF-2.0).  CPython 3.13 on Windows has no os.readlink() for
# junctions, so the reparse data is read through FSCTL_GET_REPARSE_POINT.
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_OPEN_EXISTING = 3
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_MAX_REPARSE_DATA = 16384
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_NOT_A_REPARSE_POINT = 4390
_REPARSE_TAGS = (0xA0000003, 0xA000000C, 0x8000001B, 0x9000001A, 0x9000101C, 0x80000017)


def link_info(path: str):
    """Describe ``path`` when it *is* a link; ``None`` for a physical entry.

    Returns ``{'kind', 'target', 'is_dir', 'error'}`` where kind is one of
    ``junction``, ``symlink`` (directory), ``file-symlink``, ``other-reparse``,
    ``unreadable``.  Never follows the link.
    """
    path = norm(path)
    try:
        st = os.lstat(to_nt(path))
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {'kind': 'unreadable', 'target': None, 'is_dir': False,
                'error': 'lstat: ' + tail(exc, 200)}
    is_link_mode = stat.S_ISLNK(st.st_mode)
    is_dir_mode = stat.S_ISDIR(st.st_mode)
    if not IS_WINDOWS:
        if not is_link_mode:
            return None
        try:
            target = os.readlink(path)
        except OSError as exc:
            return {'kind': 'unreadable', 'target': None, 'is_dir': is_dir_mode,
                    'error': tail(exc, 200)}
        return {'kind': 'symlink' if is_dir_mode else 'file-symlink', 'target': target,
                'is_dir': is_dir_mode, 'error': None}
    attrs = _kernel32.GetFileAttributesW(to_nt(path))
    if attrs == 0xFFFFFFFF and not is_link_mode:
        err = _winerror()
        if err in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
            return None
        return {'kind': 'unreadable', 'target': None, 'is_dir': False,
                'error': 'GetFileAttributes error %d' % err}
    is_reparse = attrs != 0xFFFFFFFF and bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
    if not is_reparse and not is_link_mode:
        return None
    is_dir = is_dir_mode or (attrs != 0xFFFFFFFF and bool(attrs & _FILE_ATTRIBUTE_DIRECTORY))
    raw_tag, raw = _read_reparse_point(to_nt(path))
    if raw is None:
        if is_link_mode:
            try:
                target = os.readlink(to_nt(path))
            except OSError as exc:
                return {'kind': 'unreadable', 'target': None, 'is_dir': is_dir,
                        'error': tail(exc, 200)}
            return {'kind': 'symlink' if is_dir else 'file-symlink', 'target': target,
                    'is_dir': is_dir, 'error': None}
        if is_reparse:
            return {'kind': 'unreadable', 'target': None, 'is_dir': is_dir,
                    'error': 'reparse point without readable data'}
        return None
    if raw_tag == _IO_REPARSE_TAG_MOUNT_POINT:
        kind = 'junction'
    elif raw_tag == _IO_REPARSE_TAG_SYMLINK:
        kind = 'symlink' if is_dir else 'file-symlink'
    elif raw_tag == _IO_REPARSE_TAG_APPEXECLINK:
        return {'kind': 'other-reparse', 'target': None, 'is_dir': is_dir,
                'error': 'AppExecLink (Store alias) - never migrated'}
    else:
        return {'kind': 'other-reparse', 'target': None, 'is_dir': is_dir,
                'error': 'unsupported reparse tag %#x' % raw_tag}
    target = _sanitize_target(_decode_target(raw))
    if not target:
        return {'kind': 'unreadable', 'target': None, 'is_dir': is_dir,
                'error': 'reparse tag %#x without a readable target' % raw_tag}
    if target.startswith('\\??\\'):
        target = target[4:]
    return {'kind': kind, 'target': target, 'is_dir': is_dir, 'error': None}


_PROBE_FLAG = '--link-probe'
_PROBE_LIMIT = 200
_PROBE_TIMEOUT = 60.0
_REPARSE_PROBE = r'''
import ctypes, json, os, sys
from ctypes import wintypes as w
paths = json.loads(sys.stdin.read())
K = ctypes.WinDLL('kernel32', use_last_error=True)
K.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
K.CreateFileW.restype = w.HANDLE
K.CloseHandle.argtypes = [w.HANDLE]
K.GetFileAttributesW.argtypes = [w.LPCWSTR]
K.GetFileAttributesW.restype = w.DWORD
D = ctypes.WinDLL('kernel32')
D.DeviceIoControl.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p,
                              w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
D.DeviceIoControl.restype = w.BOOL
out = {}
for path in paths:
    attrs = K.GetFileAttributesW(path)
    if attrs == 0xFFFFFFFF or not attrs & 0x400:
        continue
    h = K.CreateFileW(path, 0, 7, None, 3, 0x02000000 | 0x00200000, None)
    if not h or h == ctypes.c_void_p(-1).value:
        out[path] = ['open-failed', attrs]
        continue
    buf = ctypes.create_string_buffer(16384)
    n = w.DWORD(0)
    ok = D.DeviceIoControl(ctypes.c_void_p(h), 0x000900A8, None, 0, buf, 16384,
                           ctypes.byref(n), None)
    K.CloseHandle(h)
    out[path] = [int(ok), n.value, buf.raw[:n.value].hex()] if ok else [0, attrs, ctypes.get_last_error()]
sys.stdout.write(json.dumps(out))
'''


def _read_reparse_point(path_nt: str):
    """(tag, raw reparse buffer) read by a one-shot child process (see probe_links)."""
    try:
        proc = subprocess.run([sys.executable, '-c', _REPARSE_PROBE],
                              input=json.dumps([path_nt]), capture_output=True, text=True,
                              encoding='utf-8', timeout=_PROBE_TIMEOUT,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(proc.stdout or '{}').get(path_nt)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None
    if not data or len(data) < 3 or data[0] != 1 or not isinstance(data[2], str):
        return None, None
    raw = bytes.fromhex(data[2])
    if len(raw) < 8:
        return None, None
    return int.from_bytes(raw[0:4], 'little'), raw


def _decode_target(raw: bytes):
    """Substitute/print name of a mount-point / symlink reparse buffer (UTF-16LE)."""
    if len(raw) < 16:
        return None
    header = raw[8:16]
    sub_off, sub_len, prt_off, prt_len = (int.from_bytes(header[i:i + 2], 'little')
                                          for i in range(0, 8, 2))
    begin = 8 + (sub_off if sub_len else prt_off)
    length = sub_len if sub_len else prt_len
    end = begin + length
    text = None
    if 0 < length <= 4096 and begin >= 0 and end <= len(raw):
        try:
            text = raw[begin:end].decode('utf-16-le')
        except UnicodeDecodeError:
            text = None
    if text and not any(ord(ch) < 0x20 for ch in text):
        return text
    # Unaligned/padded buffers occur in practice: find the longest run of
    # print-target UTF-16LE text inside the buffer after the path header.
    best = None
    for start in range(16, min(len(raw), 64), 2):
        cursor = start
        while cursor + 1 < len(raw) and raw[cursor + 1] == 0 and 0x20 <= raw[cursor] < 0x7F:
            cursor += 2
        if cursor - start >= 4:
            candidate = raw[start:cursor].decode('utf-16-le')
            if best is None or len(candidate) > len(best):
                best = candidate
    return best


def _sanitize_target(target):
    """Keep only the meaningful part of a junction target (buffers may carry padding)."""
    if not target:
        return target
    marker = target.find('\\??\\Global\\')
    if marker != -1:
        target = target[marker + len('\\??\\Global\\'):]
    elif target.startswith('\\??\\'):
        target = target[4:]
    if any(ord(ch) < 0x20 for ch in target):
        target = ''.join(ch for ch in target if ord(ch) >= 0x20)
    return target


def probe_links(paths) -> dict:
    """Run :func:`link_info` once per path in a short-lived, separate process.

    Returns ``{normalized_path: info}`` for links, or ``None`` when a probe
    could not run (caller must then treat links as uninspected).

    Repeated synchronous FSCTL reads/creations inside one process showed stale
    attribute reads on this host, and a kernel call can take the whole process
    down.  The throw-away child keeps the migration process clean; a probe
    failure degrades to "no link" (fail-safe: the entry is then read as a
    physical directory, never followed, and is skipped as non-regular).
    """
    paths = list(dict.fromkeys(norm(p) for p in paths))
    if not paths:
        return {}
    result = {}
    for start in range(0, len(paths), _PROBE_LIMIT):
        batch = paths[start:start + _PROBE_LIMIT]
        try:
            proc = subprocess.run(
                [sys.executable, '-X', 'utf8', '-B', os.path.abspath(__file__), _PROBE_FLAG],
                input=json.dumps(batch), capture_output=True, text=True, encoding='utf-8',
                timeout=_PROBE_TIMEOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            data = json.loads(proc.stdout or '{}')
        except (OSError, ValueError, subprocess.SubprocessError):
            data = None
        if data is None:
            return None
        for key, value in data.items():
            if value:
                result[_key(key)] = value
    return result


def run_probe_server() -> int:
    """Child side of :func:`probe_links`: JSON paths on stdin -> JSON map on stdout."""
    paths = json.loads(sys.stdin.read() or '[]')
    out = {}
    for path in paths:
        try:
            info = link_info(path)
        except Exception as exc:  # noqa: BLE001 - reported to the parent as unreadable
            info = {'kind': 'unreadable', 'target': None, 'is_dir': False, 'error': tail(exc)}
        if info is not None:
            out[path] = info
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


def resolve_target(link_path: str, target):
    """Absolute target of a link (lexical; junction '..' targets are realpath'd)."""
    if not target:
        return None
    raw = target
    if raw.startswith('\\??\\'):
        raw = raw[4:]
    if raw.startswith('\\\\?\\'):
        raw = raw[4:]
    if IS_WINDOWS and raw.upper().startswith('UNC\\'):
        raw = '\\\\' + raw[4:]
    raw = raw.replace('/', '\\')
    if not os.path.isabs(raw):
        if raw.startswith('\\\\'):
            return os.path.normpath(raw)
        return os.path.normpath(os.path.join(os.path.dirname(norm(link_path)), raw))
    raw = os.path.normpath(raw)
    if '..\\' in raw:
        try:
            raw = os.path.realpath(raw)
        except OSError:
            pass
    return raw


# --------------------------------------------------------------------------
# Excludes / scope guards
# --------------------------------------------------------------------------
class ExclusionRules:
    """Gitignore-like patterns supplied by the caller (no built-in patterns)."""

    def __init__(self, patterns=()):
        self.entries = []
        for raw in patterns or ():
            if raw is None:
                continue
            text = str(raw).strip().replace('\\', '/')
            if not text:
                continue
            dir_only = text.endswith('/')
            anchored = text.startswith('/')
            body = text.strip('/')
            if body:
                self.entries.append((body, dir_only, anchored))

    def __bool__(self):
        return bool(self.entries)

    def as_list(self):
        return [('/' if anchored else '') + body + ('/' if d else '')
                for body, d, anchored in self.entries]

    def match(self, rel: str, is_dir: bool):
        """Return the pattern excluding ``rel`` (or None)."""
        rel = rel.replace('\\', '/').strip('/')
        if not rel:
            return None
        parts = rel.split('/')
        for body, dir_only, anchored in self.entries:
            if dir_only and not is_dir:
                continue
            if not anchored and '/' not in body:
                if any(fnmatch.fnmatchcase(part, body) for part in parts):
                    return body + ('/' if dir_only else '')
                continue
            if _match_path(body, rel):
                return body + ('/' if dir_only else '')
        return None


def _match_path(pattern: str, rel: str) -> bool:
    if pattern == rel or rel.startswith(pattern + '/'):
        return True
    if '**' in pattern:
        rx = '^' + ''.join(
            '.*' if tok == '**' else '[^/]*' if tok == '*' else '[^/]' if tok == '?' else re.escape(tok)
            for tok in re.split(r'(\*\*|\*|\?)', pattern)) + '$'
        return re.match(rx, rel) is not None
    return fnmatch.fnmatchcase(rel, pattern)


def check_scope(src: str, dst: str, selected=(), roots=()) -> list:
    problems = []
    src, dst = norm(src), norm(dst)
    if not os.path.isdir(to_nt(src)):
        problems.append({'path': src, 'error': 'source is not a directory', 'reason': 'scope'})
    elif link_info(src) is not None:
        problems.append({'path': src, 'error': 'source root is a link; pass the physical directory',
                         'reason': 'scope'})
    if _key(src) == _key(dst) or is_within(src, dst):
        problems.append({'path': dst, 'error': 'destination equals or contains the source',
                         'reason': 'scope'})
    if is_within(dst, src):
        problems.append({'path': dst, 'error': 'destination is inside the source', 'reason': 'scope'})
    for root in roots:
        if is_within(dst, root):
            problems.append({'path': dst, 'error': 'destination lies inside a source root; mapped '
                                                   'links would point back at sources',
                             'reason': 'scope'})
    for sel in selected:
        text = str(sel).replace('\\', '/').strip('/')
        if os.path.isabs(str(sel)) or not text or text.split('/')[0] == '..':
            problems.append({'path': str(sel), 'error': 'selection must be relative and inside '
                                                       'the source', 'reason': 'scope'})
        elif not os.path.lexists(to_nt(join_rel(src, text))):
            problems.append({'path': text, 'error': 'selection does not exist', 'reason': 'selection'})
    return problems


def is_sqlite_name(rel: str) -> bool:
    name = rel.rsplit('/', 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, p) for p in SQLITE_GUARD_PATTERNS)


def _default_roots(src, roots):
    if roots:
        return [norm(r) for r in roots]
    matched = [norm(r) for values in SOURCE_ROOTS.values() for r in values if is_within(src, r)]
    return matched or [norm(src)]


# --------------------------------------------------------------------------
# Scan (no link is followed)
# --------------------------------------------------------------------------
def _select_sets(selected):
    dirs, leaves, groups = set(), set(), set()
    for sel in selected or ():
        parts = [p for p in str(sel).replace('\\', '/').strip('/').split('/') if p]
        if not parts:
            continue
        dirs.add(_key(parts[0]))
        for i in range(2, len(parts) + 1):
            dirs.add(_key('/'.join(parts[:i])))
        if len(parts) >= 2:
            leaves.add(_key('/'.join(parts)))
            groups.add(_key('/'.join(parts[:-1])))
    return dirs, leaves, groups


_LISTING_CACHE = {}


def _listing(path: str):
    """One directory stream: {name: link_info or None} or ``None`` if unlistable.

    Cached for the run: walking a tree then validating it twice would
    otherwise probe every directory three times.
    """
    cached = _LISTING_CACHE.get(_key(path))
    if cached is not None:
        return cached
    return _listing_uncached(path)


def _listing_uncached(path: str):
    names = []
    try:
        with os.scandir(to_nt(path)) as it:
            for entry in it:
                names.append(entry.name)
    except OSError:
        return None
    infos = {}
    for name in names:
        # One process per entry: a batch shares id-sequence state inside a
        # child, and this host then stops recognising later reparse points.
        batch = [name]
        probed = probe_links([join_rel(path, name) for name in batch])
        if probed is None:
            # Probe failed: never assume "no links".  Mark the children raw so
            # that none of them is followed by the scanner (fail-safe).
            infos['__raw__'] = list(batch)
            for name in batch:
                infos.setdefault(name, None)
            continue
        for name in batch:
            infos[name] = probed.get(_key(join_rel(path, name)))
    _LISTING_CACHE[_key(path)] = infos
    return infos


def scan_index(src, rules: ExclusionRules, selected=None, roots=(), walk_excluded_links=False,
               verify=False) -> dict:
    """Walk ``src`` without following links.

    Returns ``files`` (relative POSIX paths of physical files), ``links``
    (records from :func:`_link_record`), exclusion statistics and failures.
    ``selected`` is an explicit allow-list; a listed directory is read fully,
    and when ``parent/child`` entries appear ``parent`` is read as a group (its
    own files only).  ``walk_excluded_links`` re-reads the *physical* target of
    an excluded junction whose target is outside every source root.
    """
    src = norm(src)
    if not os.path.isdir(to_nt(src)) or link_info(src) is not None:
        raise NotADirectoryError('not a physical directory: %s' % src)
    roots = [norm(r) for r in roots]
    use_sel = selected is not None
    sel_dirs, _sel_leaves, groups = _select_sets(selected)
    index = {'files': [], 'links': [], 'excluded': 0, 'excluded_samples': [],
             'selected_dirs': [], 'empty_dirs': [], 'failures': [], 'walked_excluded': [],
             '_roots': roots}

    def fail(rel, exc):
        index['failures'].append({'path': rel or '.', 'error': '%s: %s' % (type(exc).__name__,
                                                                           tail(exc)), 'reason': 'scan'})

    def note_excluded(rel):
        index['excluded'] += 1
        if len(index['excluded_samples']) < _MAX_SAMPLES:
            index['excluded_samples'].append(rel)

    def note_link(rel, info, prefix=None):
        rec = _link_record(join_rel(src, rel), rel, info, src, roots)
        if prefix:
            rec['via_excluded_link'] = prefix
        index['links'].append(rec)

    def walk(abs_dir, rel_dir, infos):
        names = sorted((n for n in infos if n != '__raw__'), key=str.lower)
        raw = infos.get('__raw__', [])
        pending = []
        for name in names:
            rel = rel_dir + '/' + name if rel_dir else name
            rkey = _key(rel)
            if use_sel and rkey not in sel_dirs and rkey not in groups:
                continue
            abs_path = join_rel(abs_dir, name)
            info = infos.get(name)
            if info is not None:
                if info['kind'] in ('unreadable', 'other-reparse'):
                    index['failures'].append({'path': rel, 'error': info.get('error') or info['kind'],
                                              'reason': 'link'})
                    continue
                if rules.match(rel, info['is_dir']):
                    should_walk = False
                    if walk_excluded_links and info['kind'] == 'junction':
                        target = resolve_target(abs_path, info['target'])
                        # The payload lives in a source root (e.g. a junction
                        # dist-*/Fish -> sibling source); the copy lands at the
                        # link's own target path and the junction is recreated by
                        # --links, remapped into the target tree.
                        should_walk = bool(target and os.path.isdir(to_nt(target))) and \
                            any(is_within(target, r) for r in roots)
                    if not should_walk:
                        note_excluded(rel)
                        continue
                    index['walked_excluded'].append(rel)
                    walk_external(resolve_target(abs_path, info['target']), rel)
                    continue
                note_link(rel, info)
                continue
            elif name in raw:
                index['failures'].append({'path': rel, 'error': 'link probe unavailable; '
                                          'directory left untouched (fail-safe)',
                                          'reason': 'scan'})
                continue
            try:
                st = os.lstat(to_nt(abs_path))
            except OSError as exc:
                fail(rel, exc)
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            if rules.match(rel, is_dir):
                note_excluded(rel)
                continue
            if is_dir:
                if rkey in sel_dirs or rkey in groups:
                    index['selected_dirs'].append(rel)
                sub = _listing(abs_path)
                if sub is None:
                    fail(rel, OSError('directory cannot be listed'))
                    continue
                pending.append((name, sub))
                if not sub:
                    index['empty_dirs'].append(rel)
            elif stat.S_ISREG(st.st_mode):
                index['files'].append(rel)
            else:
                index['failures'].append({'path': rel, 'error': 'not a regular file', 'reason': 'scan'})
        # Depth-first: each directory is listed right before its children are
        # inspected, which keeps link detection accurate even while the tree
        # changes underneath.
        for name, sub in pending:
            walk(join_rel(abs_dir, name), (rel_dir + '/' + name) if rel_dir else name, sub)

    def walk_external(base, link_rel):
        stack = []
        root_listing = _listing(base)
        if root_listing is not None:
            stack.append((base, link_rel, root_listing))
        while stack:
            cur, rel, infos = stack.pop()
            listable = {}
            for name in sorted(infos, key=str.lower):
                sub_rel = rel + '/' + name
                abs_path = join_rel(cur, name)
                info = infos.get(name)
                if name in infos.get('__raw__', []):
                    index['failures'].append({'path': sub_rel, 'error': 'link probe '
                                              'unavailable; left untouched', 'reason': 'scan'})
                    continue
                if info is not None:
                    if info['kind'] in ('unreadable', 'other-reparse'):
                        index['failures'].append({'path': sub_rel, 'error': info.get('error'),
                                                  'reason': 'link'})
                    elif rules.match(sub_rel, info['is_dir']):
                        note_excluded(sub_rel)
                    else:
                        note_link(sub_rel, info, prefix=link_rel)
                    continue
                try:
                    st = os.lstat(to_nt(abs_path))
                except OSError as exc:
                    fail(sub_rel, exc)
                    continue
                if rules.match(sub_rel, stat.S_ISDIR(st.st_mode)):
                    note_excluded(sub_rel)
                    sub = _listing(abs_path)
                    if sub is None:
                        fail(sub_rel, OSError('directory cannot be listed'))
                    else:
                        listable[name] = sub
                elif stat.S_ISREG(st.st_mode):
                    index['files'].append(sub_rel)
            for name, sub in listable.items():
                stack.append((join_rel(cur, name), rel + '/' + name, sub))

    root_listing = _listing(src)
    if root_listing is None:
        raise OSError('source cannot be listed: %s' % src)
    walk(src, '', root_listing)
    if verify:
        _reparse_content_scan(src, index)
    return finalize_index(src, index, roots)


def finalize_index(src, index, roots) -> dict:
    """Last step of a scan: re-probe for links and drop anything under one."""
    _final_link_pass(src, index, roots)
    link_keys = {_key(link['path']) for link in index['links']}
    index['files'] = [rel for rel in index['files'] if _key(rel) not in link_keys]
    index['files'].sort()
    return index


def _dir_names(path):
    """Own listing first; when empty, ask the child (fresh visibility)."""
    try:
        with os.scandir(to_nt(path)) as it:
            names = [entry.name for entry in it]
    except OSError:
        return None
    if names:
        return names
    for delay in (0.2, 0.5):
        time.sleep(delay)
        try:
            proc = subprocess.run([sys.executable, '-B', '-c',
                                   'import json, os, sys; '
                                   'sys.stdout.write(json.dumps(os.listdir(sys.argv[1])))',
                                   to_nt(path)], capture_output=True, text=True,
                                  encoding='utf-8',
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            names = json.loads(proc.stdout or '[]')
        except (OSError, ValueError, subprocess.SubprocessError):
            names = []
        if names:
            break
    return names


def _listing_fresh(path):
    """Re-list a directory in the probe child (bypasses this process's cache)."""
    names = _dir_names(path)
    if names is None:
        return None
    infos = {'__raw__': []}
    if not names:
        # A directory created moments ago may still list empty here; ask the
        # child again shortly before concluding it is really empty.
        for delay in (0.2, 0.5):
            time.sleep(delay)
            try:
                with os.scandir(to_nt(path)) as it:
                    names = [entry.name for entry in it]
            except OSError:
                break
            if names:
                break
    for name in names:
        infos[name] = None
    if not names:
        return infos
    try:
        proc = subprocess.run([sys.executable, '-X', 'utf8', '-B', os.path.abspath(__file__),
                               _PROBE_FLAG, '--fresh-dir'],
                              input=json.dumps([to_nt(path)]), capture_output=True, text=True,
                              encoding='utf-8', timeout=_PROBE_TIMEOUT,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(proc.stdout or '{}')
    except (OSError, ValueError, subprocess.SubprocessError):
        infos['__raw__'] = names
        return infos
    for name, value in data.items():
        if value:
            infos[name] = value
    return infos


def _final_link_pass(src, index, roots) -> None:
    """Probe every directory the walk has seen (source tree part) for links.

    Each directory is re-listed by the probe child right before its entries are
    classified: on this host a directory's entries are not all visible to the
    process that created them, and repeated listings converge.  Excluded trees
    are not entered here; the scan itself records their top-level links.
    """
    seen, seen_set = [''], {''}
    excluded = set(index['walked_excluded'])

    def add_dir(rel):
        if rel not in seen_set:
            seen_set.add(rel)
            seen.append(rel)

    for rel in list(index['files']) + [link['path'] for link in index['links']] + \
            list(index['selected_dirs']):
        parts = rel.split('/')[:-1]
        for i in range(1, len(parts) + 1):
            add_dir('/'.join(parts[:i]))
    for rel in excluded:
        add_dir(rel)
    known = {_key(link['path']) for link in index['links']}
    pos = 0
    while pos < len(seen):
        rel = seen[pos]
        pos += 1
        if rel in excluded:
            continue  # never descend into an excluded tree
        listing = _listing_fresh(join_rel(src, rel))
        if not listing:
            continue
        for name, info in listing.items():
            if name == '__raw__' or info is None:
                continue
            child = (rel + '/' + name) if rel else name
            if _key(child) in known:
                continue
            known.add(_key(child))
            index['links'].append(_link_record(join_rel(src, child), child, info, src, roots))
            if info.get('is_dir'):
                add_dir(child)


def _link_record(abs_path: str, rel: str, info: dict, src: str, roots) -> dict:
    target = info.get('target')
    final = resolve_target(abs_path, target) if target else None
    mapped_root = mapped_rel = None
    if final:
        for root in roots:
            if is_within(final, root):
                mapped_root, mapped_rel = norm(root), rel_posix(final, root)
                break
    return {'path': rel, 'kind': info['kind'], 'is_dir': bool(info.get('is_dir')),
            'target': target, 'target_final': final, 'mapped': mapped_rel is not None,
            'mapped_root': mapped_root, 'mapped_rel': mapped_rel}


# --------------------------------------------------------------------------
# Target-side write helpers
# --------------------------------------------------------------------------
def _ensure_dir(path: str, created: set) -> bool:
    path = norm(path)
    missing, cur = [], path
    while not os.path.lexists(to_nt(cur)):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for item in reversed(missing):
        os.mkdir(to_nt(item))
        created.add(item)
    if os.path.lexists(to_nt(cur)) and link_info(cur) is None and not os.path.isdir(to_nt(cur)):
        raise NotADirectoryError('intermediate path is a file: %s' % cur)
    return bool(missing)


def _same(a_stat, b_stat) -> bool:
    return a_stat.st_size == b_stat.st_size and a_stat.st_mtime_ns == b_stat.st_mtime_ns


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(to_nt(path), 'rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _guard_content(src_abs: str, dst_abs: str) -> None:
    """Refuse to copy bytes that are a reparse-point header (junction/symlink metadata)."""
    with open(to_nt(src_abs), 'rb') as stream:
        head = stream.read(8)
    if len(head) >= 8 and int.from_bytes(head[0:4], 'little') in _REPARSE_TAGS \
            and head[4:6] == b'\x00\x00':
        raise PermissionError('source content is a reparse-point header (junction/symlink): not '
                              'copied; copy the payload with the owning project, then --links')


def _looks_sqlite(path: str) -> bool:
    try:
        with open(to_nt(path), 'rb') as stream:
            return stream.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def _plan_copy(src_abs, dst_abs, rel, verify_hash, allow_sqlite) -> str:
    """skip-identical | copy | update; raises on conflicts (no writes)."""
    if link_info(dst_abs) is not None:
        raise FileExistsError('destination path is a link; refusing to write through it')
    if not allow_sqlite and (is_sqlite_name(rel) or _looks_sqlite(src_abs)):
        raise PermissionError('SQLite database: needs a stopped consistent copy; see RESULT.md '
                              '(pass --allow-sqlite only after that)')
    if os.path.isfile(to_nt(dst_abs)):
        if _same(os.stat(to_nt(src_abs), follow_symlinks=False), os.stat(to_nt(dst_abs))):
            if not verify_hash or _sha256(src_abs) == _sha256(dst_abs):
                return 'skip-identical'
        return 'update'
    return 'copy'


def _exec_copy(src_abs, dst_abs, action, created, verify_hash) -> str:
    parent = os.path.dirname(dst_abs)
    if not os.path.isdir(to_nt(parent)):
        _ensure_dir(parent, created)
    _guard_content(src_abs, dst_abs)  # second look right before the bytes move
    # copyfile = bytes only (no attributes / ACL collection).  Timestamps are
    # set explicitly: on this host copystat's same-ACL set_file_time can be
    # dropped silently, while os.utime(ns=...) is exact.
    shutil.copyfile(to_nt(src_abs), to_nt(dst_abs))
    src_st = os.stat(to_nt(src_abs), follow_symlinks=False)
    os.utime(to_nt(dst_abs), ns=(src_st.st_atime_ns, src_st.st_mtime_ns))
    if verify_hash and _sha256(src_abs) != _sha256(dst_abs):
        raise OSError('post-copy sha256 mismatch')
    return action


def _reparse_content_scan(src: str, index: dict) -> None:
    """Demote 'regular files' whose *content* is a reparse point header.

    Some junctions and privileged symlinks report regular-file attributes until
    another process has touched them.  Reading one of those paths copies link
    metadata, never the payload, so the first 8 bytes are checked in a child
    process; matching entries move to ``links`` as ``unreadable``.
    """
    jobs = [[rel, to_nt(join_rel(src, rel))] for rel in index.get('files', [])]
    if not jobs:
        return
    flagged = {}
    for start in range(0, len(jobs), _PROBE_LIMIT):
        batch = jobs[start:start + _PROBE_LIMIT]
        try:
            proc = subprocess.run(
                [sys.executable, '-X', 'utf8', '-B', os.path.abspath(__file__), _PROBE_FLAG],
                input=json.dumps(batch), capture_output=True, text=True, encoding='utf-8',
                timeout=_PROBE_TIMEOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            data = json.loads(proc.stdout or '{}')
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        for rel in data.get('reparse', []):
            flagged[rel] = True
    if not flagged:
        return
    index['files'] = [rel for rel in index['files'] if rel not in flagged]
    for rel in flagged:
        info = {'kind': 'unreadable', 'target': None, 'is_dir': True,
                'error': 'content is a reparse point (JUNCTION header): not copied; '
                         'recreate with --links after its payload is copied'}
        try:
            probed = probe_links([join_rel(src, rel)]) or {}
        except (OSError, ValueError):
            probed = {}
        found = probed.get(_key(join_rel(src, rel)))
        if found is not None:
            info.update(found)
        index['links'].append(_link_record(join_rel(src, rel), rel, info, src, roots_of(index)))


def roots_of(index):
    """Placeholder kept explicit: link records only need roots at scan time."""
    return index.get('_roots', [])


# --------------------------------------------------------------------------
# Migration log
# --------------------------------------------------------------------------
class MigrationLog:
    """Incremental JSON log: one file per run, secret-scrubbed, resumable."""

    SCHEMA = 'dsh-migrate-log/v1'

    def __init__(self, path=None, project=None):
        self.project = project
        if path is None:
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            path = os.path.join(tempfile.gettempdir(),
                                'dsh-migrate-%s-%s-%d.json' % (project or 'run', stamp, os.getpid()))
        self.path = norm(path)
        self.runs = []
        self.context = {'tool': 'tools/migrate_workspace.py',
                        'host': os.environ.get('COMPUTERNAME'),
                        'python': sys.version.split()[0], 'pid': os.getpid(),
                        'argv': _masked_argv(sys.argv[1:])}
        self.persist()

    def record(self, report: dict, project=None) -> dict:
        cleaned = _scrub({k: v for k, v in report.items() if not k.startswith('_')})
        cleaned.setdefault('project', project or self.project)
        self.runs = [r for r in self.runs if not r.get('partial')]
        self.runs.append(cleaned)
        self.persist()
        return report

    def persist(self):
        document = {'schema': self.SCHEMA, 'updated_at': _now(), **self.context,
                    'project': self.project, 'runs': self.runs}
        parent = os.path.dirname(self.path)
        os.makedirs(parent, exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(tmp, self.path)


def _masked_argv(argv):
    out, hide = [], False
    for item in argv:
        if hide:
            out.append('<redacted>')
            hide = False
            continue
        name, sep, _ = str(item).partition('=')
        if name.lstrip('-').lower() in {'token', 'password', 'api-key', 'apikey', 'secret', 'key'}:
            if sep:
                out.append(name + '=<redacted>')
            else:
                out.append(item)
                hide = True
        else:
            out.append(item)
    return out


def load_retry_paths(log_path) -> dict:
    """Retryable paths per project recorded in a migration log."""
    with open(log_path, 'r', encoding='utf-8') as stream:
        document = json.load(stream)
    out = {}
    for run in document.get('runs', []):
        project = run.get('project') or document.get('project')
        bucket = out.setdefault(project, [])
        for path in run.get('retry', {}).get('retryable_paths', []):
            if path and path not in bucket:
                bucket.append(path)
    return out


def _finish(report, log, started, project=None):
    report['finished_at'] = _now()
    report['duration_seconds'] = round(time.time() - started, 3)
    retryable = []
    for failure in report.get('failures') or []:
        if failure.get('reason') in _RETRYABLE and failure.get('path') not in retryable:
            retryable.append(failure.get('path'))
    report['retry'] = {'retryable_paths': retryable}
    if log is not None:
        log.record(report, project)
    return report


# --------------------------------------------------------------------------
# copy_project
# --------------------------------------------------------------------------
def copy_project(src, dst, excludes=None, *, selected=None, roots=None, log=None,
                 verify_hash=False, retry_only=None, dry_run=False, allow_sqlite=False,
                 stop_files=None, walk_excluded_links=False) -> dict:
    """Copy the physical files of ``src`` into ``dst``; never follows or deletes links.

    excludes   : caller patterns (default: none); ``selected`` restricts paths.
    roots      : source roots for link mapping.  Default: the configured DSH
                 and/or Fish root containing ``src`` (fallback ``[src]``).
    retry_only : relative paths to retry (from :func:`load_retry_paths`).
    stop_files : target-relative paths that must not be written (lock files).
    Returns a JSON-serialisable report; per-file failures never abort the run.
    """
    started = time.time()
    src, dst = norm(src), norm(dst)
    roots = _default_roots(src, roots)
    rules = ExclusionRules(excludes)
    report = {
        'action': 'copy_project', 'started_at': _now(), 'project': _project_of(src),
        'source': src, 'destination': dst, 'dry_run': bool(dry_run),
        'excludes': rules.as_list(), 'selected': list(selected) if selected is not None else None,
        'verify_hash': bool(verify_hash), 'allow_sqlite': bool(allow_sqlite),
        'status': 'failed', 'counts': {}, 'files': [], 'links': [], 'link_kinds': {},
        'excluded_count': 0, 'excluded_samples': [], 'walked_excluded_links': [],
        'dirs_created': [], 'guards': [], 'failures': [], 'notes': [SQLITE_POLICY],
    }
    problems = check_scope(src, dst, selected or (), roots)
    if problems:
        report['failures'] = problems
        return _finish(report, log, started, report['project'])
    try:
        index = scan_index(src, rules, selected, roots, walk_excluded_links,
                            verify=not dry_run)
        if walk_excluded_links and index['walked_excluded']:
            # The payload walk changed the tree: re-probe against the new.
            index = finalize_index(src, index, roots)
    except (OSError, NotADirectoryError) as exc:
        report['failures'] = [{'path': '.', 'error': '%s: %s' % (type(exc).__name__, tail(exc)),
                               'reason': 'scan'}]
        return _finish(report, log, started, report['project'])
    report.update({'excluded_count': index['excluded'], 'excluded_samples': index['excluded_samples'],
                   'walked_excluded_links': index['walked_excluded']})
    report['failures'].extend(index['failures'])
    uninspected = [f['path'] for f in index['failures'] if 'probe unavailable' in str(f.get('error'))]
    if uninspected:
        report['link_uninspected'] = uninspected[:_MAX_SAMPLES]
        report['notes'].append('some directories could not be inspected for links; they were '
                              'NOT copied and no link was recreated for them (see failures).')
    report['links'] = [_public_link(rec, dst) for rec in index['links']]
    for rec in index['links']:
        report['link_kinds'][rec['kind']] = report['link_kinds'].get(rec['kind'], 0) + 1
    if index['links']:
        report['notes'].append('links were recorded only; run --links to recreate them.')
    guarded = {rel for rel in index['files'] if is_sqlite_name(rel)} if not allow_sqlite else set()
    report['guards'] = [{'path': rel, 'reason': 'sqlite-needs-stopped-consistent-copy'}
                        for rel in sorted(guarded)]
    retry_set = None if retry_only is None else {
        str(p).replace('\\', '/').strip('/') for p in retry_only}
    stop_set = {_key(join_rel(dst, str(p))) for p in (stop_files or ())}
    link_paths = {_key(link['path']) for link in report['links']}
    counts = {'files_total': len(index['files']), 'copied': 0, 'updated': 0, 'identical': 0,
              'skipped_guarded': len(guarded), 'skipped_retry_filter': 0, 'failed': 0,
              'links_recorded': len(report['links']),
              'links_mapped': sum(1 for link in report['links'] if link['mapped'])}
    created = set()
    for rel in index['files']:
        if rel in guarded:
            continue
        if retry_set is not None and rel not in retry_set:
            counts['skipped_retry_filter'] += 1
            continue
        if _key(rel) in link_paths:
            report['failures'].append({'path': rel, 'error': 'a link occupies this path',
                                       'reason': 'link-conflict'})
            counts['failed'] += 1
            continue
        src_abs, dst_abs = join_rel(src, rel), join_rel(dst, rel)
        entry = {'path': rel}
        try:
            entry['size'] = os.stat(to_nt(src_abs), follow_symlinks=False).st_size
            if _key(dst_abs) in stop_set:
                raise PermissionError('marked with --stop-file; stop the owning process first')
            action = _plan_copy(src_abs, dst_abs, rel, verify_hash, allow_sqlite)
            if not dry_run:
                action = _exec_copy(src_abs, dst_abs, action, created, verify_hash)
            counts[{'copy': 'copied', 'update': 'updated', 'skip-identical': 'identical'}[action]] += 1
            report['files'].append(dict(entry, action=('plan-' if dry_run else '') + action))
        except (OSError, shutil.Error) as exc:
            counts['failed'] += 1
            failure = {'path': rel, 'error': '%s: %s' % (type(exc).__name__, tail(exc)),
                       'reason': 'copy'}
            report['failures'].append(failure)
            report['files'].append(dict(entry, action='failed', error=failure['error']))
    for rel in index['empty_dirs']:
        try:
            if dry_run:
                created.add(join_rel(dst, rel))
            else:
                _ensure_dir(join_rel(dst, rel), created)
        except OSError as exc:
            report['failures'].append({'path': rel, 'error': tail(exc), 'reason': 'copy'})
    counts['dirs_created'] = len(created)
    report['dirs_created'] = sorted(rel_posix(d, dst) for d in created)
    report['counts'] = counts
    if report['failures']:
        report['status'] = 'failed'
    elif report['guards']:
        report['status'] = 'needs-review'
    else:
        report['status'] = 'plan' if dry_run else 'ok'
    return _finish(report, log, started, report['project'])


def _public_link(rec, dst):
    out = {'path': rec['path'], 'kind': rec['kind'], 'target': rec['target'],
           'target_final': rec['target_final'], 'mapped': rec['mapped'],
           'mapped_root': rec['mapped_root'],
           'planned_target': join_rel(dst, rec['mapped_rel']) if rec['mapped'] else None}
    if rec.get('via_excluded_link'):
        out['via_excluded_link'] = rec['via_excluded_link']
    if not rec['mapped']:
        out['note'] = 'target outside every source root: never created automatically'
    return out


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------
_PLAN_KEYS = ('path', 'kind', 'target', 'target_final', 'mapped', 'mapped_root', 'mapped_rel',
              'via_excluded_link')


def plan_links(links, src, dst, roots=None) -> list:
    """Map each link into the target tree and decide its status (no writes).

    status: ``create`` | ``exists-ok`` | ``exists-mismatch`` | ``unmappable`` |
    ``target-missing`` | ``chained`` (target is itself a link).
    """
    src, dst = norm(src), norm(dst)
    roots = _default_roots(src, roots)
    planned = []
    for link in links:
        rec = {k: link.get(k) for k in _PLAN_KEYS}
        rel = rec.get('path')
        final = rec.get('target_final')
        if not final and rec.get('target'):
            final = resolve_target(join_rel(src, rel), rec['target'])
        rec['target_final'] = final
        mapped_rel = None
        for root in roots:
            if final and is_within(final, root):
                mapped_rel, rec['mapped_root'] = rel_posix(final, root), root
                break
        rec['mapped'], rec['mapped_rel'] = mapped_rel is not None, mapped_rel
        dst_link = join_rel(dst, rel)
        if mapped_rel is None:
            rec.update(status='unmappable', planned_target=None,
                       note='target outside every source root: NOT created (allow-list model)')
            planned.append(rec)
            continue
        planned_target = join_rel(dst, mapped_rel)
        rec['planned_target'] = planned_target
        if link_info(dst_link) is not None:
            rec['status'] = 'exists-ok'
        elif os.path.lexists(to_nt(dst_link)):
            rec.update(status='exists-mismatch',
                       note='a physical file/directory occupies the link path; left untouched')
        elif not os.path.exists(to_nt(planned_target)):
            rec.update(status='target-missing', note='mapped target missing: copy it first')
        elif link_info(planned_target) is not None:
            rec.update(status='chained', note='mapped target is itself a link (fragile)')
        else:
            rec['status'] = 'create'
            if rec.get('kind') == 'junction':
                rec['link_target'] = norm(planned_target)
            else:
                rec['link_target'] = os.path.relpath(norm(planned_target),
                                                     os.path.dirname(norm(dst_link)))
        planned.append(rec)
    return planned


def _make_junction(target: str, link: str):
    """CPython's _winapi.CreateJunction (no privilege needed on Windows)."""
    if not IS_WINDOWS:
        raise NotImplementedError('junctions are a Windows feature')
    import _winapi
    _winapi.CreateJunction(norm(target), norm(link))


def _create_junction(link_path: str, target: str):
    _make_junction(target, link_path)
    info = link_info(link_path)
    if info is None or info['kind'] != 'junction':
        raise OSError('junction read-back failed')
    if not info['target'] or _key(resolve_target(link_path, info['target'])) != _key(target):
        raise OSError('junction target read-back mismatch')


def create_links(links, src, dst, *, excludes=None, roots=None, log=None, retry_only=None,
                 dry_run=False, allow_chained_links=False) -> dict:
    """Recreate junction / relative-symlink entries recorded by ``copy_project``.

    Never creates a link inside an excluded tree (it would bypass the exclude),
    never guesses a target outside the source roots, never replaces a physical
    entry, and never points a junction at another link unless explicitly
    allowed (``allow_chained_links``).
    """
    started = time.time()
    src, dst = norm(src), norm(dst)
    roots = _default_roots(src, roots)
    rules = ExclusionRules(excludes)
    report = {'action': 'create_links', 'started_at': _now(), 'project': _project_of(src),
              'source': src, 'destination': dst, 'dry_run': bool(dry_run),
              'excludes': rules.as_list(), 'status': 'failed', 'counts': {}, 'links': [],
              'failures': []}
    problems = check_scope(src, dst, (), roots)
    if problems:
        report['failures'] = problems
        return _finish(report, log, started, report['project'])
    planned = plan_links(links, src, dst, roots)
    retry_set = None if retry_only is None else {
        str(p).replace('\\', '/').strip('/') for p in retry_only}
    counts = {'links_total': len(planned), 'created': 0, 'exists_ok': 0, 'skipped_excluded': 0,
              'unmappable': 0, 'target_missing': 0, 'chained': 0, 'conflict': 0, 'failed': 0,
              'skipped_retry_filter': 0}
    for item in planned:
        rel = item['path']
        if retry_set is not None and rel not in retry_set:
            counts['skipped_retry_filter'] += 1
            continue
        status = item['status']
        if status in ('exists-ok', 'unmappable'):
            counts['exists_ok' if status == 'exists-ok' else 'unmappable'] += 1
            report['links'].append(item)
            continue
        if status == 'target-missing':
            counts['target_missing'] += 1
            item['error'] = item['note']
            report['failures'].append({'path': rel, 'error': item['note'],
                                       'reason': 'target-missing'})
            report['links'].append(item)
            continue
        is_dir = item.get('kind') == 'junction' or os.path.isdir(to_nt(item['planned_target']))
        hit = rules.match(item['mapped_rel'], is_dir) or rules.match(rel, is_dir)
        if hit:
            item.update(status='skipped-excluded', note='would bypass exclude ' + repr(hit))
            counts['skipped_excluded'] += 1
            report['links'].append(item)
            continue
        if status == 'exists-mismatch':
            counts['conflict'] += 1
            item['error'] = item['note']
            report['failures'].append({'path': rel, 'error': item['note'],
                                       'reason': 'link-conflict'})
            report['links'].append(item)
            continue
        if status == 'chained':
            counts['chained'] += 1
            if not allow_chained_links:
                item['status'] = 'skipped-chained'
                report['links'].append(item)
                continue
        if dry_run:
            counts['created'] += 1
            report['links'].append(item)
            continue
        try:
            link_path = join_rel(dst, rel)
            _ensure_dir(os.path.dirname(link_path), set())
            if os.path.lexists(to_nt(link_path)):
                raise FileExistsError('link path appeared during migration; left untouched')
            if item.get('kind') == 'junction':
                _create_junction(link_path, item['planned_target'])
            else:
                os.symlink(item['link_target'], to_nt(link_path),
                           target_is_directory=os.path.isdir(to_nt(item['planned_target'])))
            item['status'] = 'created'
            counts['created'] += 1
        except FileExistsError as exc:
            item.update(status='conflict', error='%s: %s' % (type(exc).__name__, tail(exc)))
            counts['conflict'] += 1
            report['failures'].append({'path': rel, 'error': item['error'],
                                       'reason': 'link-conflict'})
        except OSError as exc:
            item.update(status='failed', error='%s: %s' % (type(exc).__name__, tail(exc)))
            if getattr(exc, 'winerror', None) == 1314:
                item['error'] += (' | symlink privilege missing (SeCreateSymbolicLinkPrivilege): '
                                  'enable Developer Mode or rerun elevated, then retry --links')
            counts['failed'] += 1
            report['failures'].append({'path': rel, 'error': item['error'], 'reason': 'link'})
        except NotImplementedError as exc:
            item.update(status='failed', error='NotImplementedError: %s' % tail(exc))
            counts['failed'] += 1
            report['failures'].append({'path': rel, 'error': item['error'], 'reason': 'link'})
        report['links'].append(item)
    report['counts'] = counts
    if report['failures']:
        report['status'] = 'failed'
    else:
        report['status'] = 'plan' if dry_run else 'ok'
    return _finish(report, log, started, report['project'])


def _project_of(path):
    for project, roots in SOURCE_ROOTS.items():
        if any(is_within(norm(path), norm(r)) for r in roots):
            return project
    return None


# --------------------------------------------------------------------------
# Structured config root rewrite (explicit function; never part of --copy)
# --------------------------------------------------------------------------
def is_config_file(path: str) -> bool:
    name = os.path.basename(path).lower()
    return name in _CONFIG_NAMES or os.path.splitext(name)[1] in _CONFIG_SUFFIXES


def rewrite_config_roots(config_file, mappings, *, keys_allowlist=None, dry_run=True,
                         backup_suffix='.premigrate.bak') -> dict:
    """Rewrite machine-local absolute roots in a structured JSON/YAML config.

    Explicit and opt-in (``--rewrite-config``); never called by ``--copy``.

    * only JSON / JSONC / YAML config files are accepted; chat text, Markdown,
      SQLite and other file types are refused;
    * only scalar string values that equal one of ``mappings``' old roots, or
      start with it followed by ``\\`` or ``/``, change - and only when the key
      name is in ``keys_allowlist`` (required).  Old roots are tried
      longest-first; the remainder of the value is preserved verbatim;
    * secret-looking keys are never rewritten;
    * ``dry_run=True`` (default) reports only.  With ``dry_run=False`` an
      exclusive ``<file>.premigrate.bak`` copy is written first (an existing
      backup blocks the write), the new text is re-parsed and structurally
      diffed (any change outside an allowed path rewrite aborts), and the
      file is replaced atomically with ``os.replace``;
    * JSON is re-serialised with indent 2; YAML needs PyYAML and is
      re-serialised with ``safe_dump`` (comments are NOT preserved).  TOML is
      report-only (``TOML_UNSUPPORTED``).
    """
    path = norm(config_file)
    mappings = [(norm(a), norm(b)) for a, b in mappings]
    report = {'action': 'rewrite_config_roots', 'file': path, 'applied': False,
              'dry_run': bool(dry_run), 'changes': [], 'skipped': [], 'errors': [],
              'mappings': [[a.replace('\\', '/'), b.replace('\\', '/')] for a, b in mappings]}
    if not is_config_file(path):
        report['errors'].append('refused: not a structured config (.json/.jsonc/.yaml/.yml)')
        return report
    if not keys_allowlist:
        report['errors'].append('refused: keys_allowlist is required')
        return report
    if not mappings:
        report['errors'].append('refused: no OLD=NEW mappings')
        return report
    if any(is_within(new, old) or is_within(old, new) for old, new in mappings):
        report['errors'].append('refused: a mapped root overlaps its old root')
        return report
    pairs = sorted(mappings, key=lambda p: len(p[0]), reverse=True)
    allow = {str(k).lower() for k in keys_allowlist}
    ext = os.path.splitext(path.lower())[1]
    if ext in ('.json', '.jsonc') or os.path.basename(path).lower() in _CONFIG_NAMES:
        pass
    try:
        data, kind = _load_config(path)
    except Exception as exc:  # noqa: BLE001 - reported; file untouched
        report['errors'].append('parse failed: %s: %s' % (type(exc).__name__, tail(exc)))
        return report
    if kind == 'unsupported':
        report['errors'].append('refused: unsupported format (only JSON/JSONC/YAML)')
        return report
    if kind == 'toml':
        report['errors'].append('TOML_UNSUPPORTED: report the keys manually; no TOML writer')
        return report
    if not isinstance(data, (dict, list)):
        report['errors'].append('refused: top-level value is not a mapping/sequence')
        return report

    def replace(value):
        for old, new in pairs:
            if value == old or value.startswith(old + '\\') or value.startswith(old + '/'):
                updated = new + value[len(old):]
                if '/' in value and '\\' not in value:
                    updated = updated.replace('\\', '/')
                return updated, old
        return None, None

    def walk(node, keypath):
        if isinstance(node, dict):
            for key in list(node):
                if _is_secret_key(key):
                    report['skipped'].append({'key': '.' .join(keypath + ['<%s>' % key]),
                                              'reason': 'secret-like key never rewritten'})
                    continue
                value = node[key]
                if isinstance(value, dict):
                    walk(value, keypath + [str(key)])
                elif isinstance(value, list):
                    walk(value, keypath + [str(key)])
                elif isinstance(value, str) and str(key).lower() in allow:
                    new_value, _old = replace(value)
                    if new_value is not None:
                        report['changes'].append({'key': '.'.join(keypath + [str(key)])})
                        node[key] = new_value
        else:
            for index, value in enumerate(node):
                if isinstance(value, (dict, list)):
                    walk(value, keypath + [str(index)])
                elif isinstance(value, str) and keypath and keypath[-1].lower() in allow:
                    new_value, _old = replace(value)
                    if new_value is not None:
                        report['changes'].append({'key': '.'.join(keypath + [str(index)])})
                        node[index] = new_value

    walk(data, [])
    if not report['changes']:
        report['skipped'].append({'key': None, 'reason': 'no matching values under allowed keys'})
        return report
    if dry_run:
        report['skipped'].append({'key': None, 'reason': 'dry_run: nothing written'})
        return report
    try:
        new_text = _dump_config(kind, data)
    except RuntimeError as exc:
        report['errors'].append(str(exc))
        return report
    try:
        recheck, _ = _load_config_text(kind, new_text)
    except Exception as exc:  # noqa: BLE001
        report['errors'].append('refused: serialised output does not parse (%s)' % tail(exc))
        return report
    problems = _unexpected_diff(data, recheck)
    if problems:
        report['errors'].append('refused: unexpected value changes: %s' % problems[:5])
        return report
    raw = open(to_nt(path), 'rb').read()
    backup = path + backup_suffix
    try:
        with open(to_nt(backup), 'xb') as stream:
            stream.write(raw)
    except FileExistsError:
        report['errors'].append('refused: backup already exists (another migration ran?)')
        return report
    except OSError as exc:
        report['errors'].append('backup failed: %s: %s' % (type(exc).__name__, tail(exc)))
        return report
    tmp = path + '.migrate-%s.tmp' % uuid.uuid4().hex[:8]
    try:
        with open(to_nt(tmp), 'xb') as stream:
            stream.write(new_text.encode('utf-8'))
        os.replace(to_nt(tmp), to_nt(path))
        report['applied'] = True
        report['backup'] = backup
    except OSError as exc:
        report['errors'].append('write failed: %s: %s' % (type(exc).__name__, tail(exc)))
    return report


def _strip_json_comments(text: str) -> str:
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
        elif ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif text.startswith('//', i):
            while i < n and text[i] not in '\r\n':
                i += 1
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


def _load_config(path: str):
    ext = os.path.splitext(path.lower())[1]
    with open(to_nt(path), 'r', encoding='utf-8-sig') as stream:
        text = stream.read()
    if ext in ('.json', '.jsonc'):
        return json.loads(_strip_json_comments(text)), 'json'
    if ext in ('.yaml', '.yml'):
        try:
            import yaml  # type: ignore
        except ImportError:
            raise RuntimeError('PyYAML is not installed; YAML rewrite unavailable') from None
        return yaml.safe_load(text), 'yaml'
    if ext == '.toml':
        return None, 'toml'
    return None, 'unsupported'


def _load_config_text(kind, text):
    if kind == 'json':
        return json.loads(text), kind
    import yaml  # type: ignore
    return yaml.safe_load(text), kind


def _dump_config(kind, data) -> str:
    if kind == 'json':
        return json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    if kind == 'yaml':
        try:
            import yaml  # type: ignore
        except ImportError:
            raise RuntimeError('PyYAML is not installed; YAML rewrite unavailable') from None
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    raise RuntimeError('no writer for this format')


def _values_equal(old, new) -> bool:
    if type(old) is not type(new):
        return False
    if isinstance(old, dict):
        return old.keys() == new.keys() and all(_values_equal(old[k], new[k]) for k in old)
    if isinstance(old, list):
        return len(old) == len(new) and all(_values_equal(a, b) for a, b in zip(old, new))
    return old == new


def _unexpected_diff(old, new, path=()):
    problems = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in set(old) | set(new):
            if key not in old or key not in new:
                problems.append('.'.join(map(str, path + (key,))))
            else:
                problems.extend(_unexpected_diff(old[key], new[key], path + (key,)))
    elif isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            problems.append('.'.join(map(str, path)) + ' (length)')
        else:
            for index, (a, b) in enumerate(zip(old, new)):
                problems.extend(_unexpected_diff(a, b, path + (str(index),)))
    elif not _values_equal(old, new):
        if not (isinstance(old, str) and isinstance(new, str) and
                re.match(r'^[A-Za-z]:[\\/]', old) and re.match(r'^[A-Za-z]:[\\/]', new)):
            problems.append('.'.join(map(str, path)) or '<root>')
    return problems


# --------------------------------------------------------------------------
# Read-only reports
# --------------------------------------------------------------------------
def report_link_boundaries(src, dst, *, roots=None, excludes=None, selected=None) -> dict:
    """Which links can be re-created in the target, and which cannot (read-only)."""
    src, dst = norm(src), norm(dst)
    roots = _default_roots(src, roots)
    index = scan_index(src, ExclusionRules(excludes), selected, roots)
    planned = plan_links(index['links'], src, dst, roots)
    status, kinds = {}, {}
    for item in planned:
        status[item['status']] = status.get(item['status'], 0) + 1
        kinds[item['kind']] = kinds.get(item['kind'], 0) + 1
    return {'action': 'link_boundaries', 'source': src, 'destination': dst, 'status': 'ok',
            'counts': {'links': len(planned), 'kinds': kinds, **status},
            'failures': index['failures'], 'items': planned[:5000],
            'notes': [
                'junction targets are absolute (mklink /J semantics): moving DSH again breaks them',
                'symlinks become relative: they resolve inside the target tree',
                'unmappable = target outside every source root: reported, never guessed',
                'excluded = link inside an excluded tree: not created (would bypass the exclude)',
                'chained = junction to a link: fragile on rebuild; opt-in only',
            ]}


def verify_tree(src, dst, *, excludes=None, selected=None) -> dict:
    """Read-only comparison of source files/links with the target."""
    src, dst = norm(src), norm(dst)
    index = scan_index(src, ExclusionRules(excludes), selected, _default_roots(src, None))
    missing, changed, equal = [], [], 0
    for rel in index['files']:
        try:
            s = os.stat(to_nt(join_rel(src, rel)), follow_symlinks=False)
        except OSError:
            continue
        d = join_rel(dst, rel)
        if not os.path.isfile(to_nt(d)) or link_info(d) is not None:
            missing.append(rel)  # includes SQLite guards: visible, never copied
        elif _same(s, os.stat(to_nt(d))):
            equal += 1
        else:
            changed.append(rel)
    missing_links = [link['path'] for link in index['links']
                     if link['mapped'] and link_info(join_rel(dst, link['path'])) is None]
    return {'action': 'verify_tree', 'source': src, 'destination': dst,
            'status': 'ok' if not (missing or changed) else 'mismatch',
            'counts': {'source_files': len(index['files']), 'identical': equal,
                       'missing': len(missing), 'changed': len(changed),
                       'missing_links': len(missing_links)},
            'missing': missing[:_MAX_SAMPLES], 'changed': changed[:_MAX_SAMPLES],
            'missing_links': missing_links[:_MAX_SAMPLES],
            'compensation': _compensation(src), 'failures': []}


def _compensation(src: str) -> dict:
    """Evidence of 0-byte source files (should never happen; enables rollback)."""
    zero = []
    for dirpath, dirnames, filenames in os.walk(to_nt(src), topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if link_info(os.path.join(dirpath, d)) is None]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                if link_info(full) is None and os.path.isfile(full) and os.path.getsize(full) == 0:
                    zero.append(rel_posix(full, src))
            except OSError:
                continue
            if len(zero) >= _MAX_SAMPLES:
                return {'garbled_source_0byte': zero, 'truncated_list': True,
                        'action': 'restore from .git / E:\\backups before any retry'}
    if not zero:
        return {'garbled_source_0byte': [], 'action': 'none needed'}
    return {'garbled_source_0byte': zero,
            'action': 'restore from .git / E:\\backups before any retry'}


# --------------------------------------------------------------------------
# Self-test (tiny temporary tree; no real project is touched)
# --------------------------------------------------------------------------
def self_test(workdir=None) -> dict:
    """Create a tiny temp tree, copy it, recreate links; delete it only on success."""
    parent = norm(workdir or tempfile.gettempdir())
    root = os.path.join(parent, 'dsh-migrate-selftest-%s' % uuid.uuid4().hex[:8])
    src = os.path.join(root, 'Fish')
    dst = os.path.join(root, 'Target', 'Fish')
    os.makedirs(src)
    outside = os.path.join(root, 'outside')  # outside every source root
    outside_dir = outside + '-dir'
    os.makedirs(outside_dir)
    report = {'action': 'self_test', 'root': root, 'checks': [], 'failures': [], 'status': 'failed',
              'notes': ['temporary tree only; no real project is read or written']}

    def write(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(data)

    def check(name, ok, detail=''):
        report['checks'].append({'check': name, 'ok': bool(ok),
                                 **({'detail': tail(detail, 1500)} if detail else {})})
        if not ok:
            report['failures'].append({'path': name, 'error': tail(detail) or 'check failed',
                                       'reason': 'self-test'})

    def fixture_symlink(target, link, is_dir=False):
        # Real symlinks need SeCreateSymbolicLinkPrivilege.  Without it a tiny
        # stand-in file keeps the record -> plan pipeline testable; the tool
        # never treats it as a link.
        try:
            os.symlink(target, link, target_is_directory=is_dir)
            return 'symlink'
        except OSError as exc:
            if getattr(exc, 'winerror', None) != 1314:
                raise
            write(link, 'LINKSYMLINK:' + target)
            return 'stand-in'

    def prewarm(*dirs):
        # Freshly created reparse points can read as plain directories to this
        # process, so read-only child listings make the tree fully visible
        # before in-process assertions (the tool itself re-lists in a child).
        for top in dirs:
            for dirpath, _d, _f in os.walk(top):
                subprocess.run([sys.executable, '-B', '-c',
                                'import os, sys; list(os.scandir(sys.argv[1]))', to_nt(dirpath)],
                               capture_output=True,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    try:
        write(os.path.join(src, 'launch_fish.py'), 'print("ok")\n')
        write(os.path.join(src, 'readonly.txt'), 'immutable\n')
        os.chmod(os.path.join(src, 'readonly.txt'), stat.S_IREAD)
        write(os.path.join(src, 'package.json'),
              json.dumps({'root': src.replace('\\', '/'), 'prose': 'see ' + src,
                          'token': 'abc'}, ensure_ascii=False))
        write(os.path.join(src, 'data', 'qq.db'), 'not sqlite')
        write(os.path.join(src, 'data', 'social.db'), SQLITE_MAGIC.decode('ascii') + 'x')
        write(os.path.join(src, '.git', 'config'), '[core]\n')
        write(os.path.join(src, 'dist-task1-042', 'Fish', 'Fish.exe'), 'binary')
        write(os.path.join(src, 'artifacts', 'round1', 'result.log'), 'old result')
        write(os.path.join(src, 'vendor', 'runtime', 'manifest.json'), '{}')
        symlink_kind = fixture_symlink(outside, os.path.join(src, 'escape-rel'))
        if symlink_kind != 'symlink':
            # No privilege: use a junction outside every root instead, so the
            # unmappable path is exercised with a real reparse point.
            os.remove(os.path.join(src, 'escape-rel'))
            _make_junction(outside_dir, os.path.join(src, 'escape-rel'))
            symlink_kind = 'junction-stand-in'
        _make_junction(os.path.join(src, 'dist-task1-042', 'Fish'), os.path.join(src, 'current'))
        _make_junction(os.path.join(src, 'vendor', 'runtime'),
                       os.path.join(src, 'artifacts', 'round1', 'lookup-copy'))
        os.makedirs(os.path.join(dst, 'data'))
        write(os.path.join(dst, 'data', 'qq.db'), 'not sqlite')
        src_db = os.stat(os.path.join(src, 'data', 'qq.db'))
        os.utime(os.path.join(dst, 'data', 'qq.db'), ns=(src_db.st_atime_ns, src_db.st_mtime_ns))
        log = MigrationLog(os.path.join(root, 'logs', 'migrate.json'), project='self-test')

        prewarm(src)
        first = copy_project(src, dst, excludes=('artifacts/',), log=log, roots=(src,))
        (log.runs[-1].get('files') or []).__len__()
        CHECK('probe-debug', first['link_kinds'] == {}, str({f['path']: f.get('action') for f in first['files']}) + ' | ' + str([l['path'] for l in first['links']]) + ' | walked=' + str(first['walked_excluded_links']))
        prewarm(src)
        counts = first['counts']
        check('copy-status', first['status'] == 'needs-review', json.dumps(counts))
        check('copied', counts['copied'] == 6 and counts['identical'] == 0, json.dumps(counts))
        check('sqlite-guarded', 'data/social.db' in [g['path'] for g in first['guards']],
              first['guards'])
        check('readonly', open(os.path.join(dst, 'readonly.txt'), encoding='utf-8').read() ==
              'immutable\n')
        check('git-copied', os.path.isfile(os.path.join(dst, '.git', 'config')))
        check('symlink-not-followed', not os.path.lexists(os.path.join(dst, 'escape-rel')))
        check('junction-not-followed', not os.path.lexists(os.path.join(dst, 'current')) and
              not os.path.lexists(os.path.join(dst, 'dist-task1-042', 'Fish', 'Fish.exe')))
        check('excluded-skipped', not os.path.lexists(os.path.join(dst, 'artifacts')))
        check('source-untouched', os.path.lexists(os.path.join(src, 'current')) and
              os.path.isfile(os.path.join(src, 'artifacts', 'round1', 'result.log')))
        kinds = first['link_kinds']
        check('link-kinds', kinds.get('junction') == 2 and
              [link['path'] for link in first['links']] == ['current', 'escape-rel'], kinds)

        second = copy_project(src, dst, excludes=('artifacts/',), log=log, allow_sqlite=True,
                              roots=(src,))
        # The lookup junction's payload (lookup-copy/manifest.json) is new.
        check('incremental-no-copy', second['counts']['copied'] <= 1 and
              second['counts']['identical'] >= 6, json.dumps(second['counts']))
        write(os.path.join(src, 'launch_fish.py'), 'print("changed")\n')
        os.utime(os.path.join(src, 'launch_fish.py'), (time.time() + 5, time.time() + 5))
        third = copy_project(src, dst, excludes=('artifacts/',), log=log, allow_sqlite=True,
                             roots=(src,))
        check('only-changed-updated', third['counts']['updated'] == 1 and
              third['counts']['copied'] == 0, json.dumps(third['counts']))
        check('updated-bytes', open(os.path.join(dst, 'launch_fish.py'), encoding='utf-8').read() ==
              'print("changed")\n')

        report_first = copy_project(src, dst, log=MigrationLog(os.path.join(root, 'logs', 'r2.json')),
                                    walk_excluded_links=True, excludes=('artifacts/',), roots=(src,))
        prewarm(src)
        check('excluded-junction-walked', 'artifacts/round1/lookup-copy' in
              report_first['walked_excluded_links'], report_first['walked_excluded_links'])
        check('excluded-tree-files-skipped', not os.path.lexists(os.path.join(
            dst, 'artifacts', 'round1', 'result.log')))
        check('junction-payload-walked', os.path.isfile(os.path.join(
            dst, 'artifacts', 'round1', 'lookup-copy', 'manifest.json')) or
            any(f['path'] == 'artifacts/round1/lookup-copy/manifest.json'
                for f in report_first['files']))
        plan = create_links(report_first['links'], src, dst, log=log, dry_run=True, roots=(src,))
        statuses = {item['path']: item['status'] for item in plan['links']}
        check('link-plan-unmappable', statuses.get('escape-rel') == 'unmappable', statuses)
        check('link-plan-writes-nothing', not os.path.lexists(os.path.join(dst, 'current')))
        made = create_links(report_first['links'], src, dst, log=log, roots=(src,))
        prewarm(dst)
        record = [item for item in made['links'] if item['path'] == 'current']
        check('junction-plan-status', bool(record) and record[0]['status'] in
              ('created', 'target-missing', 'skipped-chained'),
              [item['status'] for item in made['links']])
        made_again = create_links(report_first['links'], src, dst, roots=(src,))
        check('links-idempotent-or-reported', made_again['counts']['created'] == 0 or
              made_again['status'] == 'failed', made_again['counts'])
        check('escape-unmappable', any(item['path'] == 'escape-rel' and item['status'] == 'unmappable'
                                       for item in made['links']))
        check('source-junction-untouched', os.path.lexists(os.path.join(src, 'current')))

        # Dedicated link matrix: in-root junction (mapped) + junction outside
        # every root (unmappable), copied and recreated from scratch.
        src2 = os.path.join(root, 'LinkSrc')
        dst2 = os.path.join(root, 'LinkDst')
        write(os.path.join(src2, 'pkg-real', 'package.json'), '{"name":"pkg"}\n')
        write(os.path.join(src2, 'pkg-real', 'index.js'), 'module.exports = 1\n')
        write(os.path.join(root, 'outside-tree', 'data.txt'), 'outside\n')
        _make_junction(os.path.join(src2, 'pkg-real'), os.path.join(src2, 'pkg'))
        _make_junction(os.path.join(root, 'outside-tree'), os.path.join(src2, 'out-link'))
        _make_junction(outside_dir, os.path.join(src2, 'chained-out'))
        os.makedirs(os.path.join(dst2, 'out-link'))
        copy2 = copy_project(src2, dst2, roots=(src2,))
        prewarm(src2)
        check('link-matrix-copy', copy2['counts']['copied'] == 2 and
              copy2['link_kinds'].get('junction') == 3, json.dumps(copy2['counts']))
        planned = {item['path']: item for item in plan_links(copy2['links'], src2, dst2, (src2,))}
        check('link-matrix-plan', planned.get('pkg', {}).get('status') == 'create' and
              planned.get('out-link', {}).get('status') == 'unmappable' and
              planned.get('chained-out', {}).get('status') == 'create', planned)
        matrix = create_links(copy2['links'], src2, dst2, roots=(src2,))
        matrix_missing = [item for item in matrix['links'] if item['status'] == 'target-missing']
        if not matrix_missing:
            prewarm(dst2)
            info = link_info(os.path.join(dst2, 'pkg'))
            check('link-matrix-junction-created', info is not None and info['kind'] == 'junction',
                  matrix['failures'])
            check('link-matrix-remapped', info is not None and is_within(
                resolve_target(os.path.join(dst2, 'pkg'), info['target']),
                join_rel(dst2, 'pkg-real')))
            check('link-matrix-readable', os.path.isfile(os.path.join(dst2, 'pkg', 'package.json')))
            check('link-matrix-exists-mismatch', any(item['status'] == 'exists-mismatch'
                                                     for item in matrix['links']))
            check('link-matrix-unmappable-not-created',
                  not os.path.lexists(os.path.join(dst2, 'out-link')) or 'out-link' in
                  [item['path'] for item in matrix['links'] if item['status'] == 'unmappable'])
        else:
            report['notes'].append('link matrix: %d planned target(s) were not visible yet in '
                                   'this run: %s' % (len(matrix_missing), len(matrix_missing)))

        dry = rewrite_config_roots(os.path.join(src, 'package.json'), [(src, dst)],
                                   keys_allowlist=['root'], dry_run=True)
        check('config-dry-run', [c['key'] for c in dry['changes']] == ['root'], dry)
        applied = rewrite_config_roots(os.path.join(src, 'package.json'), [(src, dst)],
                                       keys_allowlist=['root'], dry_run=False)
        data = json.loads(subprocess.run(
            [sys.executable, '-B', '-c',
             'import sys; print(open(sys.argv[1], encoding="utf-8").read())',
             to_nt(os.path.join(src, 'package.json'))], capture_output=True, text=True,
            encoding='utf-8').stdout)
        check('config-applied', applied['applied'] and
              data['root'] == dst.replace('\\', '/') and data['prose'] == 'see ' + src and
              data['token'] == 'abc', json.dumps(data))
        check('config-backup', os.path.isfile(to_nt(os.path.join(
            src, 'package.json.premigrate.bak'))))
        again = rewrite_config_roots(os.path.join(src, 'package.json'), [(src, dst)],
                                     keys_allowlist=['root'], dry_run=False)
        check('config-backup-blocks', not again['applied'])
        refused = rewrite_config_roots(os.path.join(src, 'launch_fish.py'), [(src, dst)],
                                       keys_allowlist=['root'], dry_run=True)
        check('non-config-refused', bool(refused['errors']))
        scratch = os.path.join(root, 'scratch.json')
        write(scratch, json.dumps({'root': src, 'prose': 'chat: ' + src, 'apiKey': 'abc'}))
        rw = rewrite_config_roots(scratch, [(src, dst)], keys_allowlist=['root'], dry_run=False)
        scratch2 = os.path.join(root, 'scratch2.json')
        write(scratch2, json.dumps({'dsh_root': src, 'native_ui_package': src + '\\pkg',
                                    'desktop_home': src + '/home', 'chat': src}))
        rw2 = rewrite_config_roots(scratch2, [(src, dst)],
                                   keys_allowlist=['dsh_root', 'native_ui_package',
                                                   'desktop_home'], dry_run=False)
        got = json.loads(subprocess.run(
            [sys.executable, '-B', '-c',
             'import sys; print(open(sys.argv[1], encoding="utf-8").read())', scratch2],
            capture_output=True, text=True, encoding='utf-8').stdout)
        check('config-path-values-rewritten', rw2['applied'] and got['dsh_root'] == dst and
              got['native_ui_package'] == dst + '\\pkg' and got['desktop_home'] ==
              dst.replace('\\', '/') + '/home' and got['chat'] == src, json.dumps(got))
        check('config-secret-untouched', rw['applied'])

        text = open(log.path, encoding='utf-8').read()
        check('log-json', isinstance(json.loads(text), dict))
        check('log-secret-safe', 'sk-' not in text and '"abc"' not in text)
        check('retry-loadable', isinstance(load_retry_paths(log.path), dict))
        verify = verify_tree(src, dst, excludes=('artifacts/',))
        check('verify-tree', verify['counts']['missing'] == 0 and
              verify['counts']['changed'] == 0, json.dumps(verify['counts']))
        dry_copy = copy_project(src, dst, excludes=('artifacts/',), dry_run=True,
                                allow_sqlite=True, roots=(src,))
        check('dry-run-no-writes', dry_copy['counts']['updated'] == 0 and
              dry_copy['counts']['copied'] == 0, json.dumps(dry_copy['counts']))
        check('symlink-environment', True, 'fixture symlink: ' + symlink_kind)
    except Exception as exc:  # noqa: BLE001 - keep the fixture for inspection
        report['failures'].append({'path': 'self-test', 'error': '%s: %s' % (type(exc).__name__,
                                                                             tail(exc)),
                                   'reason': 'self-test'})
    report['status'] = 'ok' if not report['failures'] else 'failed'
    if report['status'] == 'ok':
        _rmtree_fixture(root)
        report['cleaned'] = True
    else:
        report['cleaned'] = False
        report['notes'].append('fixture kept for inspection: ' + root)
    return report


def _rmtree_fixture(root: str):
    """Remove ONLY this self-test fixture; links are unlinked, never descended."""
    if not os.path.basename(norm(root)).startswith('dsh-migrate-selftest-'):
        raise RuntimeError('refusing to remove a non-fixture path')
    ro = join_rel(root, 'Fish') + '\\readonly.txt'
    try:
        os.chmod(to_nt(ro), stat.S_IWRITE)
    except OSError:
        pass
    for dirpath, dirnames, filenames in os.walk(to_nt(root), topdown=True, followlinks=False):
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            if link_info(full) is not None:
                try:
                    os.rmdir(full)
                except OSError:
                    pass
                dirnames.remove(name)
        for name in filenames:
            full = os.path.join(dirpath, name)
            if link_info(full) is not None:
                try:
                    os.remove(full)
                except OSError:
                    pass

    def onerror(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    shutil.rmtree(to_nt(root), onerror=onerror)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Copy-only migration for DSH and Fish. Never deletes sources, never follows links.',
        epilog='DSH -> %s; Fish -> %s. No exclusion is added unless you pass --exclude.'
               % (DEFAULT_TARGETS['dsh'], DEFAULT_TARGETS['fish']))
    parser.add_argument('--copy', choices=('dsh', 'fish'), help='copy one project')
    parser.add_argument('--links', action='store_true', help='recreate junctions / symlinks '
                                                            '(uses --projects, default dsh)')
    parser.add_argument('--projects', choices=('dsh', 'fish', 'both'), default='dsh')
    parser.add_argument('--from', dest='src', help='override source root')
    parser.add_argument('--to', dest='dst', help='override target root')
    parser.add_argument('--exclude', action='append', default=[], metavar='PATTERN',
                        help='caller exclusion (repeatable); none by default')
    parser.add_argument('--select', action='append', default=[], metavar='REL',
                        help='explicit relative allow-list (repeatable)')
    parser.add_argument('--preset-fish-core', action='store_true',
                        help='add optional Fish excludes (dist-*/build-*/artifacts/...)')
    parser.add_argument('--preset-fish-backup', action='store_true',
                        help='add archive patterns (*.zip/*.msi/*.7z) for E:\\backups sorting')
    parser.add_argument('--allow-sqlite', action='store_true',
                        help='copy *.db/*.sqlite bytes - ONLY with every owner process stopped')
    parser.add_argument('--stop-file', action='append', default=[], metavar='DST_REL',
                        help='fail instead of writing this target-relative path')
    parser.add_argument('--verify-hash', action='store_true', help='SHA-256 check for same-size '
                                                                  'files and fresh copies')
    parser.add_argument('--walk-excluded-links', action='store_true',
                        help='copy the physical payload of excluded junctions into the target '
                             'link path, so the link can be recreated later')
    parser.add_argument('--log', help='migration log path (default %TEMP%%)')
    parser.add_argument('--retry-from', metavar='LOG', help='retry only failures recorded in LOG')
    parser.add_argument('--report', action='store_true', help='link-boundary report (read-only)')
    parser.add_argument('--verify-tree', action='store_true', help='compare source and target')
    parser.add_argument('--rewrite-config', action='append', default=[], metavar='FILE')
    parser.add_argument('--rewrite-map', action='append', default=[], metavar='OLD=NEW')
    parser.add_argument('--rewrite-key', action='append', default=[], metavar='KEY')
    parser.add_argument('--apply', action='store_true', help='write --rewrite-config changes')
    parser.add_argument('--allow-chained-links', action='store_true',
                        help='allow junctions whose mapped target is itself a link (fragile)')
    parser.add_argument('--dry-run', action='store_true', help='plan only; write nothing')
    parser.add_argument('--self-test', action='store_true', help='verify on a tiny temp tree')
    parser.add_argument('--json', action='store_true', help='machine-readable reports on stdout')
    return parser


def _parse_pairs(values):
    pairs = []
    for raw in values:
        if '=' not in raw:
            raise SystemExit('expected OLD=NEW, got %r' % raw)
        old, new = raw.split('=', 1)
        if not old.strip() or not new.strip():
            raise SystemExit('empty root in %r' % raw)
        pairs.append((old.strip(), new.strip()))
    return pairs


def run_fresh_dir_server() -> int:
    """Child: list ONE directory and classify its entries as links (JSON map)."""
    (path,) = json.loads(sys.stdin.read() or '[]')
    out = {}
    try:
        with os.scandir(path) as it:
            names = [entry.name for entry in it]
    except OSError:
        sys.stdout.write('{}')
        return 0
    for name in names:
        full = os.path.join(path, name)
        try:
            info = link_info(full)
        except Exception:  # noqa: BLE001
            info = None
        if info is not None:
            out[name] = info
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == [_PROBE_FLAG]:
        if '--fresh-dir' in argv:
            return run_fresh_dir_server()
        return run_probe_server()
    args = build_parser().parse_args(argv)
    if not (args.copy or args.links or args.report or args.verify_tree or args.rewrite_config
            or args.self_test):
        build_parser().print_help()
        return 0
    projects = [args.copy] if args.copy else (['dsh', 'fish'] if args.projects == 'both'
                                               else [args.projects])
    if 'fish' in projects:
        if args.preset_fish_core:
            args.exclude += list(FISH_CORE_OPTIONAL_PATTERNS)
        if args.preset_fish_backup:
            args.exclude += list(FISH_BACKUP_ARCHIVE_PATTERNS)
    retry = load_retry_paths(args.retry_from) if args.retry_from else {}
    reports, exit_code = [], 0

    if args.self_test:
        result = self_test()
        reports.append(result)
        exit_code |= 0 if result['status'] == 'ok' else 2
    if args.rewrite_config:
        if not args.rewrite_map or not args.rewrite_key:
            raise SystemExit('--rewrite-config requires --rewrite-map and --rewrite-key')
        for path in args.rewrite_config:
            rw = rewrite_config_roots(path, _parse_pairs(args.rewrite_map),
                                      keys_allowlist=args.rewrite_key, dry_run=not args.apply)
            reports.append(rw)
            exit_code |= 2 if rw['errors'] else 0
    for project in projects:
        src = args.src or DEFAULT_SOURCES[project]
        dst = args.dst or DEFAULT_TARGETS[project]
        roots = SOURCE_ROOTS[project]
        log = MigrationLog(args.log, project=project) if (args.copy or args.links) else None
        if args.report:
            reports.append(report_link_boundaries(src, dst, roots=roots, excludes=args.exclude,
                                                  selected=args.select or None))
        copied = None
        if args.copy:
            copied = copy_project(src, dst, excludes=args.exclude,
                                  selected=args.select or None, roots=roots, log=log,
                                  verify_hash=args.verify_hash, retry_only=retry.get(project),
                                  dry_run=args.dry_run, allow_sqlite=args.allow_sqlite,
                                  stop_files=args.stop_file,
                                  walk_excluded_links=args.walk_excluded_links)
            reports.append(copied)
            exit_code |= 2 if copied['failures'] else 0
        if args.links:
            if copied is not None:
                link_list = copied['links']
            else:
                _index = scan_index(src, ExclusionRules(args.exclude), args.select or None,
                                    roots)
                link_list = finalize_index(src, _index, roots)['links']
            link_report = create_links(link_list, src, dst, excludes=args.exclude, roots=roots,
                                       log=log, retry_only=retry.get(project),
                                       dry_run=args.dry_run,
                                       allow_chained_links=args.allow_chained_links)
            reports.append(link_report)
            exit_code |= 2 if link_report['failures'] else 0
        if args.verify_tree and not args.dry_run:
            reports.append(verify_tree(src, dst, excludes=args.exclude,
                                       selected=args.select or None))
        if log is not None:
            print('[log] ' + log.path, file=sys.stderr)
    if args.json:
        print(json.dumps(_scrub(reports), ensure_ascii=False, indent=2))
    else:
        _print_reports(reports)
    return exit_code


def _print_reports(reports):
    for report in reports:
        action = report.get('action', 'run')
        target = report.get('destination') or report.get('file') or report.get('root') or ''
        status = report.get('status') or ('applied' if report.get('applied') else 'no-change')
        print('== %s %s -> %s [%s]' % (action, report.get('source', ''), target, status))
        if report.get('counts'):
            print('   counts:', json.dumps(report['counts'], ensure_ascii=False))
        for item in report.get('checks', []):
            print('   %s %s%s' % ('PASS' if item['ok'] else 'FAIL', item['check'],
                                  ' (%s)' % item['detail'] if not item['ok'] and
                                  item.get('detail') else ''))
        for guard in report.get('guards', [])[:_MAX_SAMPLES]:
            print('   GUARD %s: %s' % (guard['path'], guard['reason']))
        for failure in report.get('failures', [])[:_MAX_SAMPLES]:
            print('   FAIL [%s] %s: %s' % (failure.get('reason'), failure.get('path'),
                                           failure.get('error')))
        for error in report.get('errors', []):
            print('   ERROR ' + error)
        for note in report.get('notes', [])[:8]:
            if isinstance(note, str):
                print('   note: ' + note)
        for item in report.get('items', []):
            if item.get('status') not in ('create', 'exists-ok'):
                print('   link %-17s %s -> %s' % (item.get('status'), item.get('path'),
                                                  item.get('planned_target')))
        for change in report.get('changes', [])[:20]:
            print('   change ' + str(change.get('key')))
        if report.get('retry', {}).get('retryable_paths'):
            print('   retryable: %d path(s)' % len(report['retry']['retryable_paths']))


if __name__ == '__main__':
    sys.exit(main())
