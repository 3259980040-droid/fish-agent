# RESULT — Real attachment ingestion/reading (DeepSeek subprocess)

Offline implementation only: no QQ sends, no group 1043230169 involvement (it stays off), no deletions of any existing repo files, no paid/model calls added. Parent owns workbench.py / tool_host.py / desktop_chat.py / qq_agent_loop.py / governance / memory / DSH UI and will wire the API + native UI.

## Files touched (only the owned set)
| File | Change |
|---|---|
| `harness/attachment_store.py` (NEW) | `AttachmentStore`, local extractors, SQLite index |
| `harness/multimodal.py` | `document_message()`, `_media_segments()`, `prepare_content()` extended for `file`/`document` segments; existing image/audio/video path kept verbatim (only the media-segment loop moved behind a `if not segments: return content` / `if not …_enabled: return content` guard) |
| `channels/fish_qq/normalize.py` | inbound `file` segments retained instead of `[不支持的消息类型: file]`; `_media_ref()` also accepts `path`; image/record/video url detection unchanged |
| `tools/test_attachment_flow.py` (NEW) | one offline end-to-end check |

Nothing else edited. `git status`-worthy note: normalize `image` missing-URL still yields `unknown`/`[不支持的消息类型: image]` as before (not in scope).

## API contract

```python
sys.path.insert(0, "<repo>/harness")
from attachment_store import AttachmentStore, safe_conversation

store = AttachmentStore(root: Path, role: str = "qq")      # role in {"qq","research"}
r = store.import_bytes(conversation_id: str, filename: str, data: bytes, *,
                       source: str = "upload", source_ref: str = "", type_hint: str = "") -> dict
r = store.import_path(conversation_id: str, path: str|Path, *, source="local",
                      source_ref="", type_hint="", name="") -> dict
items = store.list(conversation_id: str, limit: int = 100) -> list[dict]
hits  = store.search(query: str, conversation_id: str|None = None, limit: int = 10) -> list[dict]
row   = store.get(attachment_id: str) -> dict | None
text  = store.context_line(attachment_id: str, max_chars: int = 8000) -> str
safe_conversation("group:123") -> "qq-group_123"
```

Returned dict (from DB row): `id` (sha256 hex of the bytes), `conversation_id`, `role`, `name` (safe filename), `stored_name`, `path` (absolute stored path in `data/<role>/attachments/<conv>/<id>/<name>`), `type` (`image|audio|video|file`), `format`, `size`, `source`, `source_ref`, `status`, `error`, `text`, `created_at`. `import_*` adds `duplicate: bool` (`+ original_status` when duplicate; `notes` when new). `import_path` adds `original_path` (the untouched source).
`search` hits add `snippet` and `rank`.

### Status values (truthful, never faked)
| status | meaning |
|---|---|
| `read` | document text extracted and indexed |
| `archived` | image/audio/video: original saved; delegated to MediaService / remains at `path` |
| `unsupported` | e.g. `.pdf` with no installed extractor, `.xlsx/.pptx` etc.: original saved, `error` says why |
| `failed` | parser error (e.g. `.docx` that is not a zip): original saved, `error` has the reason |
| `empty` | parsed but no text (scanned PDF/blank doc) |

Duplicates are *not* archived under `status`; the old row is returned with `duplicate=True`.

### Dedup
`id = sha256(bytes)` is computed once at import — no re-hashing of the tree. Same content anywhere in the role resolves to the existing row/file; a `links` row records the new conversation so `list()` shows it. The original filename of later copies is kept in the `sources` table (source, source_ref, filename); the stored path keeps the first filename. Extracting again does **not** re-run (it is a single SQL lookup).

### Storage
```
data/<role>/attachments/<conversation>/<content-id>/<safe filename>   # exactly one copy per content
data/<role>/attachments.db   # SQLite: attachments, sources (originals/provenance), links (conv↔content), attachment_fts (FTS5)
```
Text is stored only in this per-role index — not in persona/config/preferences/global memory.

### Multimodal
* `MediaService.prepare_content(segments, text, *, local=False, cancelled=None)` now also takes `{"type":"file"|"document", ...}` segments. Each becomes one `{"type":"text"}` block containing: a non-instruction banner (file content must never act as AGENTS/SKILL/system rules), `文件名`, `存储路径`, `读取状态`, and the excerpt (first 8000 chars) — for long files a note says the full file stays at the path for native work tools. Documents are parsed even when `qq_enabled`/vision are off (no service needed).
* Segment inputs accepted for a document (first match wins): `id`/`asset_id` (already-stored content id), `path` (local file, e.g. upload dir), `url` (downloaded with the existing `_download` DNS/Range/host checks). A segment with `status:"no_url"`/`reason` (from QQ normalize) → honest `[文件“x”未能读取：…]` block, never invented.
  The segment gets `attachment_id`, `store_path`, `filename`, `status` back (for UI / later tool reads).
* Images/audio/video: unchanged behavior. `prepare_content` still sends a real image block for a local image — archived + delegated; a document-only image never claims a description. When the QQ image `EmojiStore` enqueue / vision path runs it is exactly as before.
* Documents do not count against the image/audio/video limit (`max_attachments`).

### QQ inbound
`normalize` now emits:
```json
{"type":"file","name":"b.docx","url":"https://...","file":"a/b.docx","file_id":"fid-1",
 "file_size":"1234","source_ref":"https://..."}
{"type":"file","name":"c.pdf","url":null,"file":"c.pdf","file_id":"fid-2","source_ref":"fid-2",
 "status":"no_url","reason":"附件段没有可下载地址（仅有文件标识 fid-2）","placeholder":"[文件 c.pdf：…]"}
```
No channel authorization changed. Note: `qq_agent_loop._owner_message_text` only maps image/video/audio placeholders, so a file-only QQ message with no URL will have empty `text` there until the parent adds `"file": "[文件]"` or similar — outside my file ownership.

## Parent integration notes
1. **Upload endpoint** (`attachments.import`), body `{id:<local conversation>, name, data:<base64>}`:
   ```python
   import base64
   from attachment_store import AttachmentStore
   raw = base64.b64decode(body["data"], validate=True)
   rec = AttachmentStore(root, role).import_bytes(str(body["id"]), body.get("name") or "附件", raw, source="upload")
   return {k: rec[k] for k in ("id","name","path","status","type","text","error")} | {"duplicate": rec["duplicate"]}
   ```
   Enforce the 20 MiB limit at the HTTP layer too (`attachment_store.MAX_BYTES`); the store also rejects >20 MiB and empty payloads with `ValueError`.
2. **`chat.send attachments [{type,path,name,id}]`** → build segments `[{"type": t if t in ("image","audio","video") else "file", "id": id or None, "path": path, "name": name}]` and pass to `MediaService(root, role).prepare_content(segments, text, local=True, …)`. Items that were already uploaded via `attachments.import` need only `id`. For the desktop path this replaces today's ad-hoc copy in `desktop_chat.send` (line ~208); keep the 20 MiB and 3-attachment checks there.
3. **History/UI** `attachments.list` -> `store.list(cid)`; `attachments.search` -> `store.search(query, cid)`.
4. **Optional model tool** (tool_host): `{"name":"fish_attachment_search","query":…}` -> `store.search(q, conversation_scope)`, and file reads go to `row["path"]` via the existing native file tools.
5. **Per-conversation store key**: pass the desktop conversation id as-is; for QQ use `safe_conversation(payload["chat_id"])` if a stable key is wanted (the store also accepts `private:123`/`group:456` directly — they are sanitized into the directory name).
6. Conversation access check stays at the API/channel layer; the store performs none.

## Actual check (offline, `python tools/test_attachment_flow.py`, temp dir only)
Tiny DOCX (597 B, 3 CJK paragraphs) built with stdlib zip; 16×16 PNG built with PIL. Output (trimmed):
```
docx_import: id=f15dc3f1…, type=file, status=read, format="docx (OOXML)", name=季度计划.docx, size=597,
             path=…/data/qq/attachments/desktop_1/f15dc3f1…/季度计划.docx, path_is_under_store=true
image_import: type=image, status=archived, format=png, path=…/desktop_1/a6847bbf…/photo.png
docx_text: "季度计划\n第三周验收附件摄入流程。\n注意：本文件不是指令。"
search("验收附件摄入", cid): [{name:季度计划.docx, id:f15dc3f1…, snippet:"季度计划\n第三周验收附件摄入流程。\n注意：本文件不是指令。"}]
search("验收附件摄入", "desktop:2"): []                       # conversation isolation
dedup (import_bytes of same bytes, other name): {duplicate:true, same_id:true, old_status:"read"}
dedup (import_path again): {duplicate:true, files_on_disk:1}  # no second copy
originals_intact: true                                         # source .docx/.png still exist, mtime unchanged
list_count: 2
broken .docx: {status:"failed", error:"解析失败：BadZipFile: File is not a zip file（原文件已保存）", file_kept:true}
fake %PDF: {status:"unsupported", error:"未安装 PDF 文本提取器（pypdf/pdfminer），原文件已保存但未读取"}
QQ file segments: b.docx keeps url+file+file_id+file_size; c.pdf (no url) -> status no_url + reason + placeholder
prepare_content types: ["text","text","text","image"]          # prompt, docx data block, missing-file block, real image block
```

## Supported formats / limitations (actual)
* **Read now (stdlib, no network, no model):** `.txt .md .markdown .csv .tsv .log .json .yaml .yml .xml .ini .py .js .ts .html/.htm` (tags stripped), `.docx` (OOXML paragraph text; embedded media only counted, not parsed). Encoding: UTF-8, UTF-8 BOM, UTF-16 BOM, GB18030 fallback.
* **PDF:** only via an installed `pypdf` (`_pdf_text`); **this runtime has neither pypdf nor pdfminer**, so PDFs are saved with `status:"unsupported"`. `pdfminer`-only is also reported unsupported (no extract interface wired). No fake reads.
* **Archived only:** images, audio, video (status `archived`); their understanding stays with MediaService (`analyze`/`prepare_content`). Scanned-image PDFs and DOCX text boxes/headers beyond `w:p` paragraphs are not extracted; `.doc/.xls/.xlsx/.pptx` are unsupported.
* Extraction indexes the first 256 KB of a document (long files remain fully on disk at `path`). 20 MiB per file; QQ download additionally capped by `multimodal.json max_download_mb`.

## Known gaps
* FTS5 `unicode61` treats a CJK run as one token, so CJK queries fall back to an exact substring lookup (`_substring`); English word search uses FTS5.
* `multimodal.prepare_content`: when only a *stored-id* document segment (`{"id": …}`) is sent, the store row is read directly; the `path` written back onto the segment points at the store copy and can be reused.
* No native-UI/API wiring done here by design (parent owns those files); no live QQ test (no sends).
