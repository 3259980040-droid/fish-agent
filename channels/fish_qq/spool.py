"""轻量 outbox / lock；不持久化入站消息。"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


class LockHeld(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Lock:
    def __init__(self, path: Path):
        self.path = path
        self.held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                pid = int(data.get("pid", 0))
            except (OSError, ValueError):
                pid = 0
            if _pid_alive(pid):
                raise LockHeld(f"fish-qq 已在运行 (pid={pid})")
            self.path.unlink(missing_ok=True)
        payload = {"pid": os.getpid(), "started_at": time.time()}
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.held = True

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


class Outbox:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "outbox.jsonl"

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def _write(self, records: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + ("\n" if records else ""),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def add(self, action: str, payload: dict[str, Any], ttl_s: float = 600) -> dict[str, Any]:
        now = time.time()
        record = {
            "id": f"ob-{uuid.uuid4().hex[:12]}",
            "action": action,
            "payload": payload,
            "created_at": now,
            "expires_at": now + ttl_s,
            "status": "queued",
        }
        records = self._read()
        records.append(record)
        self._write(records)
        return record

    def set_status(self, record_id: str, status: str) -> None:
        records = self._read()
        for record in records:
            if record.get("id") == record_id:
                record["status"] = status
                record["updated_at"] = time.time()
                break
        self._write(records)

    def get(self, record_id: str) -> dict[str, Any] | None:
        return next((r for r in self._read() if r.get("id") == record_id), None)

    def recover(self) -> dict[str, list[dict[str, Any]]]:
        """inflight → unknown；queued 保留，返回给调用方决定是否重试。"""
        records = self._read()
        queued = []
        changed = False
        for record in records:
            if record.get("status") == "inflight":
                record["status"] = "unknown"
                record["recovered_at"] = time.time()
                changed = True
            elif record.get("status") == "queued":
                if record.get("expires_at", 0) < time.time():
                    record["status"] = "expired"
                    changed = True
                else:
                    queued.append(record)
        if changed:
            self._write(records)
        return {
            "queued": queued,
            "inflight_to_unknown": [r for r in records if r.get("recovered_at")],
        }

    def expire(self) -> int:
        records = self._read()
        now = time.time()
        count = 0
        for record in records:
            if record.get("status") == "queued" and record.get("expires_at", 0) < now:
                record["status"] = "expired"
                count += 1
        if count:
            self._write(records)
        return count

    def all(self) -> list[dict[str, Any]]:
        return self._read()
