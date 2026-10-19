"""allowlist、去重、入站 ring buffer、出站技术限频。"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class Allowlist:
    def __init__(self, owner_id: str, group_ids: list[str], private_mode: str = "owner_only"):
        self.owner_id = str(owner_id)
        self.group_ids = {str(group) for group in group_ids}
        self.private_mode = private_mode

    def is_allowed(self, chat_type: str, chat_id: str, sender_id: str) -> bool:
        if chat_type == "private":
            return self.private_mode == "owner_only" and str(sender_id) == self.owner_id
        if chat_type == "group":
            return chat_id.removeprefix("group:") in self.group_ids
        return False


class Dedup:
    def __init__(self, window_s: float = 300):
        self.window_s = window_s
        self._seen: dict[str, float] = {}

    def seen(self, message_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        cutoff = now - self.window_s
        for key, stamp in list(self._seen.items()):
            if stamp < cutoff:
                del self._seen[key]
        if not message_id:
            return False
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False


class OutboundLimiter:
    def __init__(self, per_chat: int = 20, global_per_min: int = 60, window_s: float = 60):
        self.per_chat = per_chat
        self.global_per_min = global_per_min
        self.window_s = window_s
        self._chat: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()

    def check(self, chat_id: str, now: float | None = None) -> tuple[bool, int]:
        now = time.time() if now is None else now
        cutoff = now - self.window_s
        while self._global and self._global[0] < cutoff:
            self._global.popleft()
        queue = self._chat.setdefault(chat_id, deque())
        while queue and queue[0] < cutoff:
            queue.popleft()
        if len(self._global) >= self.global_per_min:
            retry = int((self._global[0] + self.window_s - now) * 1000)
            return False, max(1, retry)
        if len(queue) >= self.per_chat:
            retry = int((queue[0] + self.window_s - now) * 1000)
            return False, max(1, retry)
        return True, 0

    def hit(self, chat_id: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._global.append(now)
        self._chat.setdefault(chat_id, deque()).append(now)


class RingBuffer:
    def __init__(self, max_items: int = 500, window_s: float = 300):
        self.max_items = max_items
        self.window_s = window_s
        self._items: deque[tuple[float, str, int, dict[str, Any]]] = deque()
        self.overflow = 0

    def append(self, epoch: str, seq: int, event: dict[str, Any], now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - self.window_s
        while self._items and self._items[0][0] < cutoff:
            self._items.popleft()
        self._items.append((now, epoch, seq, event))
        while len(self._items) > self.max_items:
            self._items.popleft()
            self.overflow += 1

    def replay(self, epoch: str, last_seq: int, now: float | None = None) -> tuple[list[dict[str, Any]], bool]:
        now = time.time() if now is None else now
        cutoff = now - self.window_s
        while self._items and self._items[0][0] < cutoff:
            self._items.popleft()
        if not self._items:
            return [], last_seq >= 0
        oldest_seq = self._items[0][2]
        gap = epoch != self._items[0][1] or last_seq < oldest_seq - 1
        result = [event for _stamp, item_epoch, seq, event in self._items
                  if item_epoch == epoch and seq > last_seq]
        return result, gap


@dataclass
class Counters:
    received: int = 0
    dropped_allowlist: int = 0
    dropped_dup: int = 0
    buffer_overflow: int = 0
    unknown_segments: int = 0
    sent: int = 0
    failed: int = 0
    reconnects: int = 0
    started_at: float = field(default_factory=time.time)

    def metrics(self) -> dict[str, int]:
        return {
            "received": self.received,
            "dropped_allowlist": self.dropped_allowlist,
            "dropped_dup": self.dropped_dup,
            "buffer_overflow": self.buffer_overflow,
            "unknown_segments": self.unknown_segments,
            "sent": self.sent,
            "failed": self.failed,
            "reconnects": self.reconnects,
            "uptime_s": int(time.time() - self.started_at),
        }
