"""Archived-web adapter for the canonical doc_store resource index.

Existing sidecars are read once as migration input. New writes, versions,
organization and retrieval share document_docs; no parallel index is maintained.
"""
from pathlib import Path
import re
from urllib.parse import urlparse
from web_knowledge import WebKnowledge
from doc_store import library_root, ingest, doc_detail, list_docs, import_legacy_web

TEXT_SUFFIXES = ('.md', '.txt', '.html', '.htm', '.json')
# Executables, archives, media and other binary containers never become a document.
BLOCKED_SUFFIXES = (
    '.exe', '.bat', '.cmd', '.ps1', '.dll', '.sh', '.py', '.js', '.mjs', '.cjs', '.msi', '.msix',
    '.zip', '.7z', '.rar', '.gz', '.tgz', '.bz2', '.xz', '.tar', '.jar', '.class', '.vbs', '.vbe',
    '.com', '.scr', '.cpl', '.sys', '.so', '.dylib', '.bin', '.dat', '.iso', '.dmg', '.apk',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico', '.svg', '.tif', '.tiff',
    '.mp3', '.mp4', '.wav', '.avi', '.mkv', '.mov', '.flac', '.ogg', '.webm',
)
READ_CHARS = 20000
MAX_BYTES = 2 * 1024 * 1024  # Legacy export metadata only; resource storage has no such cap.
MAX_CHARS = 200000  # Legacy metadata only; new writes retain the provided text.


def library_dir(root):
    return library_root(root)


def shelf_dir(root, chat=''):
    value = str(chat or '')
    scope = ('chat:' + value if value.startswith('group:') else value) if value else 'owner'
    return library_root(root, scope)


def _binary_problem(body) -> str:
    if '\x00' in body:
        return '正文含二进制字节（NUL），不是文本资料，这里不保存'
    sample = body[:4096]
    unprintable = sum(1 for char in sample if ord(char) < 32 and char not in '\n\r\t')
    if sample and unprintable / len(sample) > 0.05:
        return '正文含大量不可打印字节，看起来是二进制内容，这里不保存'
    return ''


def _sniff_suffix(body) -> str:
    head = body.lstrip()[:200].lower()
    if head.startswith(('<!doctype html', '<html')) or '<html' in head:
        return '.html'
    if head.startswith(('{', '[')):
        return '.json'
    return '.txt'


def _choose_suffix(url, body):
    """Return (suffix, problem). A refused type yields a reason, never a guess."""
    suffix = Path(urlparse(url).path or '').suffix.lower()
    if suffix in BLOCKED_SUFFIXES:
        return '', '拒绝保存：该地址是二进制、可执行或媒体类型（' + suffix + '），这里只存文本资料'
    if suffix in TEXT_SUFFIXES:
        return suffix, ''
    # Unknown or missing suffix: an ordinary public page often ends in .php/.shtml,
    # so the body itself decides whether this is text.
    return _sniff_suffix(body), ''



def _scope(root, shelf):
    base = library_root(root)
    if shelf is None or Path(shelf).resolve() == base:
        return 'owner'
    path = Path(shelf).resolve()
    if not path.is_relative_to(base):
        raise ValueError('资料分类目录不在Fish资料根')
    group = re.search(r'([0-9]{5,15})$', path.name)
    if group is None:
        raise ValueError('无法识别资料范围')
    return 'chat:group:' + group.group(1)


def save_public_page(root, url, title, text, *, source='fish_web_fetch', fetched_at=None,
                     upstream_truncated=False, shelf=None):
    try:
        target = WebKnowledge.public_target(url)
        if not isinstance(text, str) or not text.strip():
            raise ValueError('没有取得可保存的网页文本')
        problem = _binary_problem(text)
        suffix, type_problem = _choose_suffix(target, text)
        if problem or type_problem:
            raise ValueError(problem or type_problem)
        saved = ingest(root, str(title or target)[:120], text, kind='html' if suffix in ('.html', '.htm') else suffix.lstrip('.'), scope=_scope(root, shelf),
                       source='web_source', url=target, fetched_at=fetched_at or '',
                       metadata={'provider': source, 'upstream_truncated': bool(upstream_truncated)})
        return {'ok': True, **saved, 'path': saved['original_path'], 'title': saved['name'],
                'truncated': bool(upstream_truncated), 'upstream_truncated': bool(upstream_truncated), 'local_truncated': False}
    except (ValueError, OSError) as exc:
        return {'ok': False, 'error': str(exc)}


def list_documents(root, limit=20):
    return [{**row, 'path': row['original_path'], 'title': row['name']} for row in list_docs(root, limit=limit)]


def read_document(root, doc_id):
    try:
        import_legacy_web(root)
        row = doc_detail(root, doc_id)
        original = Path(row['original_path'])
        text = original.read_text(encoding='utf-8')
        return {'ok': True, **row, 'path': str(original), 'title': row['name'], 'text': text[:READ_CHARS],
                'text_chars': len(text), 'read_truncated': len(text) > READ_CHARS}
    except (ValueError, OSError) as exc:
        return {'ok': False, 'error': str(exc)}
