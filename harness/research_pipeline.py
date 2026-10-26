"""Research 收藏夹最小闭环：同步、字幕优先、无字幕才视频理解，失败可重试。"""

from __future__ import annotations
from locations import project_path

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bilibili_source import FixtureBilibiliAdapter, SourceError
from research_store import ResearchStore, now_text


def has_substantive_subtitles(text: str) -> bool:
    """Accept even short speech/lyrics, but not captions made only of sound cues."""
    cues = {"音乐", "背景音乐", "纯音乐", "伴奏", "无声", "静音", "掌声", "笑声", "欢呼声", "噪音", "杂音",
            "music", "background music", "instrumental", "silence", "applause", "laughter", "cheering", "noise", "inaudible"}
    for line in str(text or "").splitlines():
        line = re.sub(r"^\s*\[\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\]\s*", "", line)
        line = line.strip(" \t[]()（）【】<>♪♫♬♩🎵🎶.,，。!！…:：-—")
        if line and line.casefold() not in cues:
            return True
    return False


class LocalVideoAnalyzer:
    """Use the same cancelable media pipeline as normal chat."""

    def __init__(self, root: Path, command: str | None = None):
        self.root = root
        self.command = command or self._resolve()
        self.out_dir = project_path(root, "data", "research", "frames")

    @staticmethod
    def _resolve() -> str | None:
        for name in ("mcp-video-analyzer.cmd", "mcp-video-analyzer.exe", "mcp-video-analyzer"):
            found = shutil.which(name)
            if found:
                return found
        return None

    def analyze(self, url: str, bvid: str, cancelled=None) -> dict[str, Any]:
        from multimodal import MediaService
        media = MediaService(self.root, "research")
        if media.config["research_enabled"]:
            try:
                observed = media.analyze(url, "video", cancelled=cancelled)
                return {**observed, "modality": "frames" if "frames" in observed["modalities"] else "transcript" if "transcript" in observed["modalities"] else "ocr"}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        return {'ok': False, 'error': '研究角色的媒体输入已被主人关闭'}


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(p.strip() for p in parts if p.strip())
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "").strip()
    return str(value).strip()


def _extract_visual(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for frame in data.get("frames") or []:
        if isinstance(frame, dict):
            value = frame.get("description") or frame.get("text") or frame.get("ocr")
            if value:
                parts.append(str(value))
    for item in data.get("ocrResults") or []:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            value = item.get("text") or item.get("content")
            if value:
                parts.append(str(value))
    return "\n".join(p.strip() for p in parts if p.strip())


def _write_note(
    root: Path, bvid: str, title: str, url: str, author: str,
    level: str, text: str,
) -> Path:
    path = root / "knowledge" / "notes" / "video" / f"{bvid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    excerpt = text[:1200].strip()
    path.write_text(
        f"# {title}\n\n"
        f"- bvid: {bvid}\n"
        f"- url: {url}\n"
        f"- author: {author}\n"
        f"- observation_level: {level}\n"
        f"- captured_at: {now_text()}\n\n"
        f"## 资料节选（历史兼容入口，未生成语义摘要）\n\n{text[:240].strip()}\n\n"
        f"## 原文节选\n\n{excerpt}\n",
        encoding="utf-8",
    )
    return path


def _process_one(
    store: ResearchStore, adapter, analyzer, video: dict[str, Any],
) -> dict[str, Any]:
    bvid = video["bvid"]
    source_id, _row = store.upsert_source(video, store.selected_favorite()["media_id"])
    details = adapter.get_video(bvid)
    store.update_video_details(source_id, details)
    title = details.get("title") or video.get("title", bvid)
    url = details.get("url") or video.get("url", "")
    author = details.get("author") or video.get("author", "")
    cid = int(details.get("cid", 0) or 0)

    available = ["metadata"]
    processed = ["metadata"]
    warnings: list[str] = []

    subtitle = adapter.get_subtitle(bvid, cid) if cid else None
    if subtitle and has_substantive_subtitles(subtitle.get("text", "")):
        text = str(subtitle["text"]).strip()
        available.append("subtitle")
        processed.append("subtitle")
        level = "subtitle_read"
        store.mark_processed(source_id, level, available, processed, warnings)
        store.record_observation(source_id, level, "subtitle", text[:20000], warnings)
        note = _write_note(store.root, bvid, title, url, author, level, text)
        store.record_memory(source_id, f"{title} — {text[:180].strip()}", str(note))
        return {"status": "done", "level": level, "bvid": bvid}

    if subtitle and str(subtitle.get("text", "")).strip():
        warnings.append("subtitle_nonlinguistic")
    available.append("video")
    analysis: dict[str, Any]
    if hasattr(adapter, "download_video"):
        try:
            local = adapter.download_video(bvid, cid)
        except Exception as exc:  # noqa: BLE001
            local = None
            analysis = {"ok": False, "error": f"video_download_failed: {type(exc).__name__}: {exc}"}
            warnings.append("video_download_failed")
        else:
            if local is not None:
                analysis = analyzer.analyze(str(local), bvid) if analyzer is not None else {"ok": False, "error": "无 analyzer"}
            else:
                analysis = {"ok": False, "error": "没有可下载的 playurl"}
    else:
        analysis = analyzer.analyze(url, bvid) if analyzer is not None else {"ok": False, "error": "无 analyzer"}
    if analysis.get("ok") and str(analysis.get("text", "")).strip():
        text = str(analysis["text"]).strip()
        modality = str(analysis.get("modality", "transcript"))
        if modality == "frames":
            processed.append("frames")
            level = "video_sampled"
        else:
            processed.append("transcript")
            level = "video_analyzed"
        warnings.extend(str(w) for w in analysis.get("warnings", []) or [])
        store.mark_processed(source_id, level, available, processed, warnings)
        store.record_observation(
            source_id, level, modality,
            text[:20000], warnings,
        )
        note = _write_note(store.root, bvid, title, url, author, level, text)
        store.record_memory(source_id, f"{title} — {text[:180].strip()}", str(note))
        return {"status": "done", "level": level, "bvid": bvid}

    warnings.extend(["subtitle_missing", "video_analyzer_failed"])
    error = str(analysis.get("error", "video analyzer failed"))
    warnings.extend(str(w) for w in analysis.get("warnings", []) or [])
    store.mark_failed(source_id, available, processed, warnings, error)
    store.record_observation(
        source_id, "metadata_only", "metadata",
        "仅获取元数据，未读字幕、未看视频；不作为已看。", warnings,
    )
    return {"status": "failed", "level": "metadata_only", "bvid": bvid, "error": error}


def sync_favorites(
    store: ResearchStore,
    adapter,
    analyzer=None,
    limit: int | None = None,
) -> dict[str, Any]:
    favorite = store.selected_favorite()
    if favorite is None:
        raise SourceError("尚未选择收藏夹；先用 --select-media-id 保存稳定 media_id")
    media_id = int(favorite["media_id"])
    items = adapter.list_items(media_id)
    if limit is not None:
        items = items[:limit]
    max_retries = int(store.load_config().get("max_retries", 3))
    run_id = store.start_sync(media_id, len(items))
    processed = skipped = failed = 0
    results = []
    for video in items:
        _source_id, row = store.upsert_source(video, media_id)
        ok, reason = store.should_process(row, max_retries)
        if not ok:
            skipped += 1
            results.append({"bvid": video["bvid"], "status": "skipped", "reason": reason})
            continue
        try:
            result = _process_one(store, adapter, analyzer, video)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            source_id, row = store.upsert_source(video, media_id)
            store.mark_failed(
                source_id, ["metadata"], ["metadata"],
                [f"exception:{type(exc).__name__}"], str(exc),
            )
            store.record_observation(
                source_id, "metadata_only", "metadata",
                f"处理异常，未看视频：{type(exc).__name__}", [str(exc)],
            )
            results.append({"bvid": video["bvid"], "status": "failed", "error": str(exc)})
            continue
        if result["status"] == "done":
            processed += 1
        else:
            failed += 1
        results.append(result)
    status = "completed" if failed == 0 else "partial"
    store.finish_sync(run_id, processed, skipped, failed, status)
    return {
        "media_id": media_id,
        "total": len(items),
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "status": status,
        "results": results,
    }
