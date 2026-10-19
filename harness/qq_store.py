"""QQ Domain persistence：Fish conversation / messages / runtime turns。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class QQStore:
    def __init__(self, root: Path, db_path: Path | None = None):
        self.root = root
        self.db_path = db_path or (root / "data" / "qq" / "qq.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS conversations (
                  id TEXT PRIMARY KEY,
                  channel TEXT NOT NULL,
                  chat_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  created_at TEXT,
                  updated_at TEXT,
                  status TEXT DEFAULT 'active',
                  runtime_session_id TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT, chat_id TEXT, user_id TEXT, text TEXT,
                  replied INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS runtime_turns (
                  id TEXT PRIMARY KEY,
                  conversation_id TEXT NOT NULL,
                  runtime_session_id TEXT,
                  request_id TEXT,
                  status TEXT,
                  started_at TEXT,
                  finished_at TEXT,
                  error TEXT,
                  assistant_message_id TEXT
                );
                CREATE TABLE IF NOT EXISTS incidents (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, detail TEXT
                );
                """
            )
            for column, decl in (
                ("conversation_id", "TEXT"), ("sender_id", "TEXT"),
                ("direction", "TEXT"), ("channel_message_id", "TEXT"),
                ("reply_to", "TEXT"), ("segments", "TEXT"),
                ("status", "TEXT"), ("runtime_session_id", "TEXT"),
                ("runtime_turn_id", "TEXT"), ("error", "TEXT"),
                ("cancelled", "INTEGER DEFAULT 0"),
            ):
                self._ensure_column(conn, "messages", column, decl)
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version(version) VALUES(2)")
            else:
                conn.execute("UPDATE schema_version SET version=2")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_channel_id "
                "ON messages(channel_message_id) WHERE channel_message_id IS NOT NULL AND channel_message_id != ''"
            )
            conn.commit()

    @staticmethod
    def make_conversation_id(chat_id: str, agent_id: str) -> str:
        return f"qq:{chat_id}:{agent_id}"

    def get_or_create_conversation(self, chat_id: str, agent_id: str) -> dict[str, Any]:
        conversation_id = self.make_conversation_id(chat_id, agent_id)
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO conversations(id, channel, chat_id, agent_id, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (conversation_id, "qq", chat_id, agent_id, now_text(), now_text()),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        return dict(row)

    def set_session(self, conversation_id: str, session_id: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE conversations SET runtime_session_id=?, updated_at=? WHERE id=?",
                (session_id, now_text(), conversation_id),
            )
            conn.commit()

    def record_message(
        self, conversation_id: str, chat_id: str, sender_id: str, direction: str,
        channel_message_id: str, reply_to: str | None, segments: list[dict[str, Any]],
        text: str, status: str, runtime_session_id: str = "",
        runtime_turn_id: str = "", error: str = "", cancelled: bool = False,
    ) -> int:
        with closing(self.connect()) as conn:
            cur = conn.execute(
                "INSERT INTO messages(ts, chat_id, user_id, text, replied, conversation_id, "
                "sender_id, direction, channel_message_id, reply_to, segments, status, "
                "runtime_session_id, runtime_turn_id, error, cancelled) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    now_text(), chat_id, sender_id, text, 1 if direction == "outbound" else 0,
                    conversation_id, sender_id, direction, channel_message_id, reply_to,
                    json.dumps(segments, ensure_ascii=False), status,
                    runtime_session_id, runtime_turn_id, error, 1 if cancelled else 0,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def update_message(self, message_id: int, **updates: Any) -> None:
        allowed = {"status", "error", "cancelled", "channel_message_id", "runtime_session_id", "runtime_turn_id", "segments"}
        if isinstance(updates.get('segments'), list):
            updates['segments'] = json.dumps(updates['segments'], ensure_ascii=False)
        fields = {k: v for k, v in updates.items() if k in allowed}
        if not fields:
            return
        with closing(self.connect()) as conn:
            assignments = ", ".join(f"{key}=?" for key in fields)
            conn.execute(
                f"UPDATE messages SET {assignments} WHERE id=?",
                (*fields.values(), int(message_id)),
            )
            conn.commit()

    def create_turn(self, turn_id: str, conversation_id: str, session_id: str, request_id: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT INTO runtime_turns(id, conversation_id, runtime_session_id, request_id, "
                "status, started_at) VALUES(?,?,?,?,?,?)",
                (turn_id, conversation_id, session_id, request_id, "running", now_text()),
            )
            conn.commit()

    def finish_turn(self, turn_id: str, status: str, error: str = "", assistant_message_id: str = "") -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE runtime_turns SET status=?, finished_at=?, error=?, assistant_message_id=? WHERE id=?",
                (status, now_text(), error[-1000:], assistant_message_id, turn_id),
            )
            conn.commit()

    def counts(self) -> dict[str, int]:
        with closing(self.connect()) as conn:
            return {
                "conversations": conn.execute("SELECT count(*) FROM conversations").fetchone()[0],
                "messages": conn.execute("SELECT count(*) FROM messages").fetchone()[0],
                "runtime_turns": conn.execute("SELECT count(*) FROM runtime_turns").fetchone()[0],
            }
