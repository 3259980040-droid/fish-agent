"""OneBot 事件/段 → fish-qq 规范化事件；出站只允许 text/image。"""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class NormalizeError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_]+)((?:,[^\]]*)?)\]")
CQ_ESCAPES = (("&#44;", ","), ("&#91;", "["), ("&#93;", "]"), ("&amp;", "&"))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _cq_unescape(value: str) -> str:
    for old, new in CQ_ESCAPES:
        value = value.replace(old, new)
    return value


def _cq_params(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in raw.lstrip(",").split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            result[key] = _cq_unescape(value)
    return result


def _image_url(data: dict[str, Any]) -> str | None:
    return _media_ref(data)


def _media_ref(data: dict[str, Any]) -> str | None:
    """Any HTTP(S) transport the adapter gave us (url / path / file as URL)."""
    for key in ("url", "path", "file"):
        value = str(data.get(key) or "")
        if value.startswith("http://") or value.startswith("https://"):
            return value
    return None


def _file_segment(data: dict[str, Any]) -> dict[str, Any]:
    """Keep every identifier a file segment carries; never invent a local file."""
    raw_file = str(data.get("file") or data.get("file_id") or "")
    name = str(data.get("name") or data.get("file_name") or Path(raw_file.replace("\\", "/")).name or "")
    url = _media_ref(data)
    file_id = str(data.get("file_id") or data.get("id") or data.get("fid") or "")
    seg: dict[str, Any] = {"type": "file", "name": name or "file", "url": url, "file": raw_file,
                           "file_id": file_id, "file_size": str(data.get("file_size") or ""),
                           "source_ref": url or file_id or raw_file}
    local = raw_file if raw_file and not url and (Path(raw_file).is_absolute() or raw_file.startswith("file:")) else ""
    if local and Path(local.removeprefix("file:")).is_file():
        seg["path"] = str(Path(local.removeprefix("file:")).resolve())
    if url is None and not seg.get("path"):
        # Honest status for the model/UI: identifiers were kept for a later retry.
        reason = "附件段没有可下载地址" + (f"（仅有文件标识 {file_id[:40]}）" if file_id else "")
        seg["status"] = "no_url"
        seg["reason"] = reason
        seg["placeholder"] = f"[文件 {name or '未命名'}：{reason}]"
    return seg


def _segment(kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if kind == "text":
        text = str(data.get("text", ""))
        return {"type": "text", "text": text} if text != "" else None
    if kind == "image":
        url = _image_url(data)
        if url is None:
            return {"type": "unknown", "raw_type": "image", "placeholder": "[不支持的消息类型: image]"}
        seg: dict[str, Any] = {"type": "image", "url": url}
        alt = data.get("summary") or data.get("alt")
        if alt:
            seg["alt"] = str(alt)
        return seg
    if kind == "file":
        return _file_segment(data)
    if kind == "at":
        qq = str(data.get("qq", ""))
        return {"type": "at", "user_id": qq} if qq else None
    if kind in ("record", "video"):
        url = _image_url(data)
        if not url:
            return {"type": "unknown", "raw_type": kind, "placeholder": f"[附件没有可读取的地址: {kind}]"}
        return {"type": "audio" if kind == "record" else "video", "url": url,
                "file_id": str(data.get("file", ""))}
    if kind == "reply":
        return None  # 归一化为消息级 reply_to
    if kind == "face":
        seg = {"type": "face", "id": str(data.get("id", ""))}
        if data.get("text"):
            seg["text"] = str(data["text"])
        return seg
    return {"type": "unknown", "raw_type": kind, "placeholder": f"[不支持的消息类型: {kind}]"}


def _from_cq(text: str) -> tuple[list[dict[str, Any]], str | None]:
    segments: list[dict[str, Any]] = []
    reply_to: str | None = None
    cursor = 0
    for match in CQ_RE.finditer(text):
        if match.start() > cursor:
            plain = _cq_unescape(text[cursor:match.start()])
            if plain:
                segments.append({"type": "text", "text": plain})
        kind = match.group(1)
        params = _cq_params(match.group(2) or "")
        if kind == "reply":
            reply_to = params.get("id")
        else:
            seg = _segment(kind, params)
            if seg is not None:
                segments.append(seg)
        cursor = match.end()
    if cursor < len(text):
        plain = _cq_unescape(text[cursor:])
        if plain:
            segments.append({"type": "text", "text": plain})
    return segments, reply_to


def normalize_message(
    raw: dict[str, Any], bot_id: str, bot_message_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    if raw.get("post_type") != "message":
        return None
    message_type = raw.get("message_type")
    if message_type == "group":
        chat_type = "group"
        target = raw.get("group_id")
        chat_id = f"group:{target}"
    elif message_type == "private":
        chat_type = "private"
        target = raw.get("user_id")
        chat_id = f"private:{target}"
    else:
        return None
    if target is None:
        return None

    segments: list[dict[str, Any]] = []
    reply_to: str | None = None
    message = raw.get("message")
    if isinstance(message, list):
        for item in message:
            if not isinstance(item, dict):
                segments.append({"type": "unknown", "raw_type": "malformed", "placeholder": "[不支持的消息类型: malformed]"})
                continue
            kind = str(item.get("type", "unknown"))
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            if kind == "reply":
                reply_to = str(data.get("id") or "") or None
                continue
            seg = _segment(kind, data)
            if seg is not None:
                segments.append(seg)
    elif isinstance(message, str):
        segments, reply_to = _from_cq(message)
    else:
        segments.append({"type": "unknown", "raw_type": "empty", "placeholder": "[不支持的消息类型: empty]"})

    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    for segment in segments:
        if segment.get('type') in ('file', 'image', 'video', 'audio'):
            segment['chat_id'] = chat_id
    sender_id = str(sender.get("user_id", raw.get("user_id", "")))
    display_name = str(sender.get("card") or sender.get("nickname") or "")
    bot_ids = bot_message_ids or set()
    return {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_id": str(raw.get("message_id", "")),
        "sender": {"id": sender_id, "display_name": display_name},
        "segments": segments,
        "mentions_bot": any(seg.get("type") == "at" and seg.get("user_id") == bot_id for seg in segments),
        "reply_to_bot": bool(reply_to and reply_to in bot_ids),
        "reply_to": reply_to,
        "received_at": _now(),
    }


def normalize_outbound(
    segments: Any, max_text_chars: int, max_segments: int,
) -> list[dict[str, Any]]:
    if not isinstance(segments, list) or not segments:
        raise NormalizeError("bad_request")
    if len(segments) > max_segments:
        raise NormalizeError("payload_too_large")
    result: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            raise NormalizeError("bad_request")
        kind = seg.get("type")
        if kind == "text":
            text = seg.get("text")
            if not isinstance(text, str):
                raise NormalizeError("bad_request")
            if len(text) > max_text_chars:
                raise NormalizeError("payload_too_large")
            result.append({"type": "text", "text": text})
        elif kind == "image":
            if seg.get('asset_id'):
                import re
                if not re.fullmatch('[a-f0-9]{64}', str(seg['asset_id'])):
                    raise NormalizeError('unsupported_segment')
                result.append({'type': 'image', 'asset_id': seg['asset_id']})
                continue
            url = seg.get("url")
            if not isinstance(url, str):
                raise NormalizeError("bad_request")
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise NormalizeError("unsupported_segment")
            result.append({"type": "image", "url": url})
        else:
            raise NormalizeError("unsupported_segment")
    return result


def to_onebot_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for seg in segments:
        if seg["type"] == "text":
            result.append({"type": "text", "data": {"text": seg["text"]}})
        elif seg["type"] == "image":
            result.append({"type": "image", "data": {"file": seg["url"]}})
    return result


def cq_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;").replace(",", "&#44;")


def text_to_cq(segments: list[dict[str, Any]]) -> str:
    parts = []
    for seg in segments:
        if seg["type"] == "text":
            parts.append(cq_escape(seg["text"]))
        elif seg["type"] == "image":
            parts.append(f"[CQ:image,file={cq_escape(seg['url'])}]")
    return "".join(parts)
