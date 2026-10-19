"""Research 业务库：来源 / observation / derived memory 分开存。

只碰 data/research/research.db；不碰 qq.db / harness.db。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from storage import locked_settings, write_json


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ResearchStore:
    def __init__(self, root: Path, db_path: Path | None = None, config_path: Path | None = None):
        self.root = root
        self.db_path = db_path or (root / "data" / "research" / "research.db")
        self.config_path = config_path or (root / "data" / "research" / "config.json")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
        if column not in self._columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def migrate(self) -> None:
        with closing(self.connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  type TEXT, url TEXT, bvid TEXT, captured_at TEXT, status TEXT,
                  media_id INTEGER, title TEXT, author TEXT, duration INTEGER,
                  cid INTEGER, cover_url TEXT,
                  available_modalities TEXT DEFAULT '[]',
                  processed_modalities TEXT DEFAULT '[]',
                  observation_level TEXT,
                  warnings TEXT DEFAULT '[]',
                  retry_count INTEGER DEFAULT 0,
                  last_error TEXT,
                  updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS observations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_id INTEGER, level TEXT, notes TEXT, created_at TEXT,
                  modality TEXT, warnings TEXT DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS memories (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_id INTEGER, summary TEXT, path TEXT, created_at TEXT,
                  kind TEXT DEFAULT 'video_summary'
                );
                CREATE TABLE IF NOT EXISTS favorites (
                  media_id INTEGER PRIMARY KEY,
                  title TEXT,
                  selected_at TEXT,
                  last_sync_at TEXT,
                  last_sync_status TEXT
                );
                CREATE TABLE IF NOT EXISTS sync_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  media_id INTEGER, started_at TEXT, finished_at TEXT,
                  total INTEGER DEFAULT 0, processed INTEGER DEFAULT 0,
                  skipped INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
                  status TEXT, note TEXT
                );
                """
            )
            for column, decl in (
                ("media_id", "INTEGER"), ("title", "TEXT"), ("author", "TEXT"),
                ("duration", "INTEGER"), ("cid", "INTEGER"), ("cover_url", "TEXT"),
                ("available_modalities", "TEXT DEFAULT '[]'"),
                ("processed_modalities", "TEXT DEFAULT '[]'"),
                ("observation_level", "TEXT"), ("warnings", "TEXT DEFAULT '[]'"),
                ("retry_count", "INTEGER DEFAULT 0"), ("last_error", "TEXT"),
                ("updated_at", "TEXT"),
            ):
                self._ensure_column(conn, "sources", column, decl)
            for column, decl in (
                ("modality", "TEXT"), ("warnings", "TEXT DEFAULT '[]'"),
            ):
                self._ensure_column(conn, "observations", column, decl)
            for column, decl in (("kind", "TEXT DEFAULT 'video_summary'"),):
                self._ensure_column(conn, "memories", column, decl)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_bvid ON sources(bvid) "
                "WHERE bvid IS NOT NULL AND bvid != ''"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone() is None:
                conn.execute("INSERT INTO schema_version(version) VALUES(2)")
            else:
                conn.execute("UPDATE schema_version SET version=2")
            conn.commit()

    def load_config(self) -> dict[str, Any]:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_config(self, data: dict[str, Any]) -> None:
        write_json(self.config_path, data)

    @locked_settings
    def select_favorite(self, media_id: int, title: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT INTO favorites(media_id, title, selected_at) VALUES(?,?,?) "
                "ON CONFLICT(media_id) DO UPDATE SET title=excluded.title, selected_at=excluded.selected_at",
                (int(media_id), title, now_text()),
            )
            conn.commit()
        config = self.load_config()
        config["favorite"] = {
            "media_id": int(media_id),
            "title": title,
            "selected_at": now_text(),
        }
        self.save_config(config)

    def selected_favorite(self) -> dict[str, Any] | None:
        fav = self.load_config().get('favorite') or {}
        if fav.get('media_id'): return fav
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT media_id, title, selected_at FROM favorites ORDER BY selected_at DESC LIMIT 1"
            ).fetchone()
        if row is not None:
            return dict(row)
        fav = self.load_config().get("favorite") or {}
        if fav.get("media_id"):
            return fav
        return None

    def upsert_source(self, video: dict[str, Any], media_id: int) -> tuple[int, dict[str, Any]]:
        bvid = str(video.get("bvid") or "")
        if not bvid:
            raise ValueError("video.bvid 为空")
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM sources WHERE bvid=?", (bvid,)).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO sources(type,url,bvid,captured_at,status,media_id,title,author,"
                    "duration,cover_url,available_modalities,processed_modalities,warnings,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "bilibili_video", video.get("url", ""), bvid, now_text(), "new",
                        int(media_id), video.get("title", ""), video.get("author", ""),
                        int(video.get("duration", 0) or 0), video.get("cover", ""),
                        json.dumps(["metadata"], ensure_ascii=False), "[]", "[]", now_text(),
                    ),
                )
                source_id = int(cur.lastrowid)
                row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            else:
                conn.execute(
                    "UPDATE sources SET media_id=?, title=?, author=?, duration=?, cover_url=?, "
                    "updated_at=? WHERE id=?",
                    (
                        int(media_id), video.get("title", row["title"]),
                        video.get("author", row["author"]),
                        int(video.get("duration", 0) or row["duration"] or 0),
                        video.get("cover", row["cover_url"]), now_text(), int(row["id"]),
                    ),
                )
                source_id = int(row["id"])
                row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            conn.commit()
        return source_id, dict(row)

    def update_video_details(self, source_id: int, details: dict[str, Any]) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE sources SET cid=?, duration=?, author=?, title=?, cover_url=?, updated_at=? "
                "WHERE id=?",
                (
                    int(details.get("cid", 0) or 0), int(details.get("duration", 0) or 0),
                    details.get("author", ""), details.get("title", ""),
                    details.get("cover", ""), now_text(), int(source_id),
                ),
            )
            conn.commit()

    def should_process(self, row: dict[str, Any], max_retries: int = 3) -> tuple[bool, str]:
        try:
            processed = json.loads(row.get("processed_modalities") or "[]")
        except ValueError:
            processed = []
        if any(item in processed for item in ("subtitle", "transcript", "frames", "video")):
            return False, "already_processed"
        if row.get("status") == "done":
            return False, "done"
        if row.get("status") == "failed" and int(row.get("retry_count") or 0) >= max_retries:
            return False, "retry_exhausted"
        return True, "new"

    def mark_processed(
        self, source_id: int, level: str, available: list[str],
        processed: list[str], warnings: list[str], status: str = "done",
    ) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE sources SET status=?, observation_level=?, available_modalities=?, "
                "processed_modalities=?, warnings=?, last_error=NULL, updated_at=? WHERE id=?",
                (
                    status, level, json.dumps(available, ensure_ascii=False),
                    json.dumps(processed, ensure_ascii=False),
                    json.dumps(warnings, ensure_ascii=False), now_text(), int(source_id),
                ),
            )
            conn.commit()

    def mark_failed(
        self, source_id: int, available: list[str], processed: list[str],
        warnings: list[str], error: str,
    ) -> None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT retry_count FROM sources WHERE id=?", (source_id,)).fetchone()
            retry = int(row["retry_count"] or 0) + 1 if row else 1
            conn.execute(
                "UPDATE sources SET status='failed', observation_level='metadata_only', "
                "available_modalities=?, processed_modalities=?, warnings=?, retry_count=?, "
                "last_error=?, updated_at=? WHERE id=?",
                (
                    json.dumps(available, ensure_ascii=False),
                    json.dumps(processed, ensure_ascii=False),
                    json.dumps(warnings, ensure_ascii=False), retry, error[-1000:],
                    now_text(), int(source_id),
                ),
            )
            conn.commit()

    def record_observation(
        self, source_id: int, level: str, modality: str,
        notes: str, warnings: list[str],
    ) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT INTO observations(source_id, level, modality, notes, warnings, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    int(source_id), level, modality, notes,
                    json.dumps(warnings, ensure_ascii=False), now_text(),
                ),
            )
            conn.commit()

    def record_memory(self, source_id: int, summary: str, path: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT INTO memories(source_id, summary, path, kind, created_at) VALUES(?,?,?,?,?)",
                (int(source_id), summary, path, "video_summary", now_text()),
            )
            conn.commit()

    def start_sync(self, media_id: int, total: int) -> int:
        with closing(self.connect()) as conn:
            cur = conn.execute(
                "INSERT INTO sync_runs(media_id, started_at, total, status) VALUES(?,?,?,?)",
                (int(media_id), now_text(), int(total), "running"),
            )
            conn.commit()
            return int(cur.lastrowid)

    def finish_sync(
        self, run_id: int, processed: int, skipped: int, failed: int,
        status: str, note: str = "",
    ) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at=?, processed=?, skipped=?, failed=?, "
                "status=?, note=? WHERE id=?",
                (now_text(), processed, skipped, failed, status, note, int(run_id)),
            )
            conn.commit()

    def list_sources(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with closing(self.connect()) as conn:
            return {
                "sources": conn.execute("SELECT count(*) FROM sources").fetchone()[0],
                "observations": conn.execute("SELECT count(*) FROM observations").fetchone()[0],
                "memories": conn.execute("SELECT count(*) FROM memories").fetchone()[0],
            }
