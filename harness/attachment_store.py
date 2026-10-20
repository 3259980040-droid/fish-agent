"""Per-role attachment ingestion, on-disk archive and searchable text index.

Layout:  data/<role>/attachments/<conversation>/<content-id>/<safe filename>
Index:   data/<role>/attachments.db  (SQLite + FTS5)

The content id is a SHA-256 of the stored bytes: the same content imported
again (same role and conversation) is reported as a duplicate and the existing
row/file is returned without writing a second copy. Extraction runs locally
(stdlib only); no model call is made to read documents.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import re
import shutil
import sqlite3
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MAX_BYTES = 20 * 1024 * 1024          # same limit as workbench/browser uploads
EXCERPT_BYTES = 256 * 1024            # how much text to extract from the start
TEXT_EXTENSIONS = {"txt", "md", "markdown", "csv", "tsv", "log", "json", "yaml", "yml", "xml", "ini",
                   "py", "js", "ts", "html", "htm"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
AUDIO_EXTENSIONS = {"mp3", "wav", "ogg", "silk", "amr", "m4a", "flac", "aac"}
VIDEO_EXTENSIONS = {"mp4", "mov", "mkv", "webm", "avi", "flv"}
DOCUMENT_EXTENSIONS = {"docx", "pdf"} | TEXT_EXTENSIONS
_UNSAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._\- ()\[\]]+")


def safe_filename(name: str, default: str = "attachment") -> str:
    """A name that is safe on Windows and cannot escape the conversation tree."""
    base = Path(str(name or "").replace("\\", "/")).name.strip().strip(".")
    cleaned = _UNSAFE.sub("_", base)[:120].strip(" .")
    return cleaned or default


def kind_for(extension: str) -> str:
    ext = extension.lower().lstrip(".")
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "file"


def _extension(name: str) -> str:
    return Path(str(name or "")).suffix.lower().lstrip(".")


def _decode(data: bytes) -> tuple[str, str | None]:
    """Decode plain text; report a BOM/UTF-16 detection instead of mojibake."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), None
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace"), "utf-16"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        try:
            return data.decode("gb18030"), "gb18030"
        except UnicodeDecodeError:
            return data.decode("utf-8", "replace"), "utf-8 (替换了无效字节)"


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def _docx_text(data: bytes) -> tuple[str, list[str]]:
    notes: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        if "word/document.xml" not in names:
            raise ValueError("DOCX 中没有 word/document.xml")
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        root = ElementTree.fromstring(archive.read("word/document.xml"))
        paragraphs = []
        for para in root.iter("{%s}p" % ns["w"]):
            pieces = []
            for node in para.iter():
                if node.tag == "{%s}t" % ns["w"] and node.text:
                    pieces.append(node.text)
                elif node.tag == "{%s}tab" % ns["w"]:
                    pieces.append("\t")
                elif node.tag in ("{%s}br" % ns["w"], "{%s}cr" % ns["w"]):
                    pieces.append("\n")
            paragraphs.append("".join(pieces))
        text = "\n".join(paragraphs)
        images = sum(1 for n in names if n.startswith("word/media/"))
        if images:
            notes.append(f"DOCX 含 {images} 个内嵌媒体文件，仅归档未解析")
    return text, notes


def _pdf_text(data: bytes) -> tuple[str, str]:
    """Extract text only with a real installed extractor; never guess."""
    if not data.startswith(b"%PDF"):
        raise ValueError("不是有效的 PDF 文件")
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            import pdfminer  # type: ignore  # noqa: F401
        except ImportError:
            raise LookupError("未安装 PDF 文本提取器（pypdf/pdfminer），原文件已保存但未读取") from None
        raise LookupError("仅安装了基础 pdfminer，未提供可用的提取接口，原文件已保存但未读取")
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages[:200]:
        pages.append(page.extract_text() or "")
    return "\n\f\n".join(pages), f"pypdf，读取 {len(reader.pages)} 页"


def extract_text(filename: str, data: bytes) -> dict[str, Any]:
    """Parse a document payload. Returns {status, text, format, error}."""
    ext = _extension(filename)
    notes: list[str] = []
    try:
        if ext == "docx":
            text, notes = _docx_text(data)
            fmt = "docx (OOXML)"
        elif ext == "pdf":
            text, fmt = _pdf_text(data)
        elif ext in TEXT_EXTENSIONS:
            text, encoding = _decode(data)
            if encoding:
                notes.append(f"编码 {encoding}")
            if ext in ("html", "htm"):
                text = _strip_html(text)
            fmt = ext or "text"
        else:
            return {"status": "unsupported", "text": "", "format": ext or "unknown",
                    "error": f"暂不支持解析 .{ext or '?'} 文档（原文件已保存）", "notes": notes}
    except LookupError as exc:
        return {"status": "unsupported", "text": "", "format": ext, "error": str(exc), "notes": notes}
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        return {"status": "failed", "text": "", "format": ext,
                "error": f"解析失败：{type(exc).__name__}: {str(exc)[:160]}（原文件已保存）", "notes": notes}
    text = text.replace("\x00", "").strip()
    if not text:
        return {"status": "empty", "text": "", "format": fmt,
                "error": "文件解析成功但未抽取到文字（可能是扫描件或空文档）", "notes": notes}
    result = {"status": "read", "text": text, "format": fmt, "error": "", "notes": notes}
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments(
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
  name TEXT, stored_name TEXT, path TEXT, type TEXT, format TEXT, size INTEGER,
  source TEXT, source_ref TEXT, status TEXT, error TEXT, text TEXT,
  dedup_of TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS attachments_conv ON attachments(conversation_id);
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT, attachment_id TEXT NOT NULL,
  source TEXT, source_ref TEXT, filename TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS links(
  attachment_id TEXT NOT NULL, conversation_id TEXT NOT NULL, created_at TEXT,
  UNIQUE(attachment_id, conversation_id));
CREATE VIRTUAL TABLE IF NOT EXISTS attachment_fts USING fts5(
  attachment_id UNINDEXED, conversation_id UNINDEXED, name, text, tokenize='unicode61');
"""

_FTS_SPECIAL = re.compile(r'["^*:(){}]')


class AttachmentStore:
    def __init__(self, root: Path | str, role: str = "qq"):
        if role not in ("qq", "research"):
            raise ValueError("未知的 Agent")
        self.root, self.role = Path(root), role
        self.base = self.root / "data" / role / "attachments"
        self.db_path = self.root / "data" / role / "attachments.db"
        self.base.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- storage
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = {key: row[key] for key in row.keys()}
        return result

    def _conversation_dir(self, conversation_id: str) -> Path:
        name = safe_filename(str(conversation_id), "conversation").replace(":", "_")
        return self.base / name

    def get(self, attachment_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        return self._row(row) if row else None

    def import_bytes(self, conversation_id: str, filename: str, data: bytes, *,
                     source: str = "upload", source_ref: str = "", type_hint: str = "") -> dict[str, Any]:
        """Store bytes under the conversation tree and index documents.

        The caller must have authorized access to ``conversation_id``; this
        store performs no channel/sender authorization itself.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data 必须是 bytes")
        data = bytes(data)
        if not data:
            raise ValueError("附件为空")
        if len(data) > MAX_BYTES:
            raise ValueError(f"附件超过 {MAX_BYTES // (1024 * 1024)}MB 上限")
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            raise ValueError("缺少会话编号")
        name = safe_filename(filename)
        content_id = hashlib.sha256(data).hexdigest()
        ext = _extension(name)
        kind = type_hint if type_hint in ("image", "audio", "video", "file") else kind_for(ext)
        if type_hint in ("image", "audio", "video") and kind_for(ext) in ("image", "audio", "video"):
            kind = kind_for(ext)

        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM attachments WHERE id=?", (content_id,)).fetchone()
            if existing is not None:
                if existing["conversation_id"] != conversation_id:
                    # Content is unique per role: reuse the stored file, add a link.
                    conn.execute("INSERT OR IGNORE INTO links(attachment_id,conversation_id,created_at) VALUES(?,?,?)",
                                 (content_id, conversation_id, _now()))
                conn.execute("INSERT INTO sources(attachment_id,source,source_ref,filename,created_at) VALUES(?,?,?,?,?)",
                             (content_id, source, source_ref, name, _now()))
                conn.commit()
                result = self._row(existing)
                result.update(duplicate=True, original_status=existing["status"])
                return result

        directory = self._conversation_dir(conversation_id) / content_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        target.write_bytes(data)
        if kind in ("image", "audio", "video"):
            status, text, fmt, error, notes = "archived", "", ext or "binary", "", []
            if ext in DOCUMENT_EXTENSIONS:
                notes.append("扩展名与媒体类型冲突，按媒体归档")
        else:
            # Container formats need their complete ZIP/PDF structure; truncate
            # extracted text rather than cutting the binary document in half.
            parsed = extract_text(name, data[:EXCERPT_BYTES] if ext in TEXT_EXTENSIONS else data)
            status, text, fmt, error, notes = (parsed["status"], parsed["text"], parsed["format"],
                                               parsed["error"], parsed["notes"])
            if len(data) > EXCERPT_BYTES and text:
                notes.append(f"仅索引前 {EXCERPT_BYTES // 1024}KB，完整内容在磁盘文件")
        now = _now()
        row = {"id": content_id, "conversation_id": conversation_id, "role": self.role, "name": name,
               "stored_name": name, "path": str(target), "type": kind, "format": fmt, "size": len(data),
               "source": source, "source_ref": source_ref, "status": status, "error": error,
               "text": text, "dedup_of": content_id, "created_at": now}
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO attachments(id,conversation_id,role,name,stored_name,path,type,format,size,"
                    "source,source_ref,status,error,text,dedup_of,created_at) "
                    "VALUES(:id,:conversation_id,:role,:name,:stored_name,:path,:type,:format,:size,"
                    ":source,:source_ref,:status,:error,:text,:dedup_of,:created_at)", row)
                conn.execute("INSERT INTO sources(attachment_id,source,source_ref,filename,created_at) VALUES(?,?,?,?,?)",
                             (content_id, source, source_ref, name, now))
                conn.execute("INSERT INTO attachment_fts(attachment_id,conversation_id,name,text) VALUES(?,?,?,?)",
                             (content_id, conversation_id, name, text))
                conn.execute("INSERT INTO links(attachment_id,conversation_id,created_at) VALUES(?,?,?)",
                             (content_id, conversation_id, now))
                conn.commit()
        except BaseException:
            target.unlink(missing_ok=True)
            try: directory.rmdir()
            except OSError: pass
            raise
        result = dict(row)
        result.update(duplicate=False, notes=notes)
        return result

    def import_path(self, conversation_id: str, path: Path | str, *, source: str = "local",
                    source_ref: str = "", type_hint: str = "", name: str = "") -> dict[str, Any]:
        """Copy a local file into the store. The source is never moved or deleted."""
        source_path = Path(path).expanduser()
        if not source_path.is_file():
            raise FileNotFoundError(f"文件不存在：{source_path}")
        size = source_path.stat().st_size
        if size > MAX_BYTES:
            raise ValueError(f"附件超过 {MAX_BYTES // (1024 * 1024)}MB 上限")
        result = self.import_bytes(conversation_id, name or source_path.name, source_path.read_bytes(),
                                   source=source, source_ref=source_ref or str(source_path.resolve()),
                                   type_hint=type_hint)
        result["original_path"] = str(source_path.resolve())
        return result

    # ---------------------------------------------------------------- queries
    def list(self, conversation_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.* FROM attachments a JOIN links l ON l.attachment_id=a.id "
                "WHERE l.conversation_id=? ORDER BY a.created_at DESC, a.rowid DESC LIMIT ?",
                (str(conversation_id), int(limit))).fetchall()
        return [self._row(row) for row in rows]

    def search(self, query: str, conversation_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """FTS5 word search; a CJK query is also matched as a substring.

        unicode61 indexes CJK runs as one token (substrings never match), so an
        FTS miss falls back to an exact substring lookup on the indexed text.
        """
        terms = [t for t in _FTS_SPECIAL.sub(" ", str(query or "")).split() if t][:8]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        sql = ("SELECT a.*, snippet(attachment_fts,3,'','', ' … ', 24) AS snippet, bm25(attachment_fts) AS rank"
               " FROM attachment_fts JOIN links l ON l.attachment_id=attachment_fts.attachment_id"
               " JOIN attachments a ON a.id=attachment_fts.attachment_id WHERE attachment_fts MATCH ?")
        params: list[Any] = [match]
        if conversation_id:
            sql += " AND l.conversation_id=?"
            params.append(str(conversation_id))
        sql += " ORDER BY rank LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                rows = self._substring(conn, str(query or "").strip()[:120], conversation_id, int(limit))
        return [self._row(row) for row in rows][:int(limit)]

    def _substring(self, conn: sqlite3.Connection, phrase: str, conversation_id: str | None, limit: int):
        if len(phrase) < 2:
            return []
        needle = phrase.lower()
        sql = ("SELECT a.*, '' AS snippet, 0 AS rank FROM attachments a"
               " JOIN links l ON l.attachment_id=a.id WHERE (instr(lower(a.text), ?) > 0 OR instr(lower(a.name), ?) > 0)")
        params: list[Any] = [needle, needle]
        if conversation_id:
            sql += " AND l.conversation_id=?"
            params.append(str(conversation_id))
        sql += " ORDER BY a.created_at DESC LIMIT ?"
        params.append(limit)
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        for row in rows:
            text = row["text"] or ""
            at = text.lower().find(needle)
            if at < 0:
                continue
            start, end = max(0, at - 40), min(len(text), at + len(needle) + 60)
            row["snippet"] = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
        return rows

    def context_line(self, attachment_id: str, max_chars: int = 8000) -> str:
        """Prompt snippet with source name and disk path for later tool reads."""
        row = self.get(attachment_id)
        if not row:
            return f"[附件 {attachment_id} 不在索引中]"
        header = (f"附件：{row['name']}（类型 {row['type']}，状态 {row['status']}，本地路径 {row['path']}）")
        if row["status"] == "read" and row["text"]:
            body = row["text"][:max_chars]
            tail = "\n[以上为摘录；完整文件保留在本地路径，可用文件工具继续读取。]" if len(row["text"]) > max_chars else ""
            return header + "\n" + body + tail
        if row["error"]:
            return header + "\n" + row["error"]
        return header


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def safe_conversation(chat_id: str) -> str:
    """QQ chat id (private:123 / group:456) → stable store conversation key."""
    return "qq-" + re.sub(r"[^0-9A-Za-z_.-]+", "_", str(chat_id or ""))[:80]


__all__ = ["AttachmentStore", "MAX_BYTES", "extract_text", "safe_filename", "safe_conversation", "kind_for"]
