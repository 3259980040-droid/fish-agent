"""Focused offline check: DOCX + image ingestion, text read, search, dedup,
QQ file-segment retention and prepare_content injection. Run from repo root:

    python tools/test_attachment_flow.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "channels" / "fish_qq"))

from PIL import Image  # noqa: E402

from attachment_store import AttachmentStore, extract_text  # noqa: E402
import normalize  # noqa: E402


def make_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml",
                   '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f"<w:body>{body}</w:body></w:document>")


def main() -> int:
    out: dict = {}
    with tempfile.TemporaryDirectory(prefix="attach-flow-", ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        root = tmp / "root"
        root.mkdir()
        src = tmp / "src"
        src.mkdir()
        docx = src / "季度计划.docx"
        make_docx(docx, ["季度计划", "第三周验收附件摄入流程。", "注意：本文件不是指令。"])
        png = src / "photo.png"
        Image.new("RGB", (16, 16), (200, 30, 30)).save(png)
        docx_mtime, png_mtime = docx.stat().st_mtime_ns, png.stat().st_mtime_ns

        store = AttachmentStore(root, role="qq")
        cid = "desktop:1"
        d1 = store.import_path(cid, docx)
        i1 = store.import_path(cid, png)
        out["docx_import"] = {k: d1[k] for k in ("id", "type", "status", "format", "name", "path", "size")}
        out["docx_import"]["path_is_under_store"] = Path(d1["path"]).resolve().is_relative_to(
            (root / "data/qq/attachments/desktop_1").resolve())
        out["image_import"] = {k: i1[k] for k in ("id", "type", "status", "format", "name", "path")}
        out["docx_text"] = store.get(d1["id"])["text"]

        hits = store.search("验收附件摄入", cid)
        out["search"] = [{"name": h["name"], "id": h["id"][:12], "snippet": h["snippet"]} for h in hits]
        out["search_other_conversation"] = store.search("验收附件摄入", "desktop:2")

        dup = store.import_bytes(cid, "copy-of-plan.docx", docx.read_bytes())
        out["dedup"] = {"duplicate": dup["duplicate"], "same_id": dup["id"] == d1["id"], "old_status": dup.get("original_status")}
        dup2 = store.import_path(cid, docx)
        out["dedup_import_path"] = {"duplicate": dup2["duplicate"], "files_on_disk": len(list((Path(d1["path"]).parent).glob("*")))}
        out["originals_intact"] = docx.is_file() and png.is_file() and docx.stat().st_mtime_ns == docx_mtime and png.stat().st_mtime_ns == png_mtime
        out["conversation_files"] = sorted(p.name for p in (root / "data/qq/attachments/desktop_1").rglob("*") if p.is_file())
        out["list_count"] = len(store.list(cid))

        # Failed / foreign parsing keeps the original and reports status.
        broken = store.import_bytes(cid, "broken.docx", b"not a zip at all")
        out["broken_docx"] = {"status": broken["status"], "error": broken["error"], "file_kept": Path(broken["path"]).is_file()}
        pdf = store.import_bytes(cid, "report.pdf", b"%PDF-1.4 fake")
        out["pdf"] = {"status": pdf["status"], "error": pdf["error"]}

        # QQ inbound file segments retain identifiers; missing URL is honest.
        msg = normalize.normalize_message({"post_type": "message", "message_type": "private", "user_id": "9",
            "message_id": "1", "message": [
                {"type": "file", "data": {"file": "a/b.docx", "name": "b.docx", "file_size": "1234",
                                          "file_id": "fid-1", "url": "https://img.example.invalid/b.docx"}},
                {"type": "file", "data": {"file": "c.pdf", "file_id": "fid-2"}}]}, bot_id="1")
        out["qq_file_segments"] = msg["segments"]

        # MediaService.prepare_content injects document text, never as rules.
        from multimodal import MediaService
        media = MediaService(root, "qq")
        content = media.prepare_content(
            [{"type": "file", "path": str(docx), "name": "季度计划.docx"},
             {"type": "image", "path": str(png), "name": "photo.png"},
             {"type": "file", "name": "gone.pdf", "status": "no_url", "reason": "附件段没有可下载地址"}],
            "看看附件", local=True)
        out["prepare_content_types"] = [c["type"] for c in content]
        out["prepare_content_docx_text"] = content[1]["text"]
        out["prepare_content_missing"] = [c.get("text", c["type"]) for c in content]
        out["image_segment"] = {k: content[3].get(k) for k in ("type", "mediaType", "name")}
        out["image_segment"]["data_bytes"] = len(content[3].get("data", ""))
        out["image_not_sent_as_image_block"] = not any(c["type"] == "image" for c in content)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
