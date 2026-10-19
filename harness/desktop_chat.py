"""Persistent role-local desktop conversation, using the same native DSH input."""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

import requests
from dsh_web import DSHWebClient
from governance import Governance, control_token
from multimodal import MediaService


class DesktopChat:
    def __init__(self, root: Path, role: str, runtime):
        if role not in ("qq", "research"):
            raise ValueError("未知角色")
        self.root, self.role, self.runtime = root, role, runtime
        self.db = root / "data" / role / "desktop-chat.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()
        self.active_session = None
        self.policy = Governance(root, role)
        with closing(self.connect()) as conn:
            conn.executescript("""CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,title TEXT,session_id TEXT,updated_at TEXT);
                CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,conversation_id TEXT,direction TEXT,text TEXT,
                segments TEXT,status TEXT,error TEXT,created_at TEXT);""")
            conn.commit()
            columns = {row[1] for row in conn.execute('PRAGMA table_info(conversations)')}
            if 'space_id' not in columns:
                conn.execute("ALTER TABLE conversations ADD COLUMN space_id TEXT NOT NULL DEFAULT ''")
            conn.commit()

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def new(self, title: str = "新对话", *, isolated=None, revision='') -> str:
        cid = uuid.uuid4().hex
        if isolated is None:
            isolated = self.role == 'qq'
        space = None
        if isolated:
            from social_context import SocialContext
            space = SocialContext(self.root).create_space(self.role, title, revision=revision)
        with closing(self.connect()) as conn:
            conn.execute("INSERT INTO conversations(id,title,session_id,updated_at,space_id) VALUES(?,?,?,?,?)", (cid, title[:80], None, time.strftime("%Y-%m-%d %H:%M:%S"), space['id'] if space else ''))
            conn.commit()
        return cid

    def snapshot(self, cid: str = "", search: str = "", after='', before='', limit=100, *, around=0) -> dict:
        with closing(self.connect()) as conn:
            limit = min(2000, max(20, int(limit)))
            hits = []
            if search.strip():
                conditions, args = ['m.text LIKE ?'], ['%' + search.strip()[:300] + '%']
                if after: conditions.append('m.created_at>=?'); args.append(after + ' 00:00:00')
                if before: conditions.append('m.created_at<=?'); args.append(before + ' 23:59:59')
                hits = [dict(r) for r in conn.execute('SELECT m.id,m.conversation_id,m.text,m.created_at,c.title FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE ' + ' AND '.join(conditions) + ' ORDER BY m.id DESC LIMIT 100', args)]
                if not around:
                    hit = next((r for r in hits if not cid or r['conversation_id']==cid), None)
                    if hit: cid, around = hit['conversation_id'], hit['id']
            clauses, values = [], []
            if search:
                clauses.append('(c.title LIKE ? OR EXISTS(SELECT 1 FROM messages m WHERE m.conversation_id=c.id AND m.text LIKE ?))'); values += ['%' + search + '%', '%' + search + '%']
            if after: clauses.append('c.updated_at>=?'); values.append(after + ' 00:00:00')
            if before: clauses.append('c.updated_at<=?'); values.append(before + ' 23:59:59')
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            conversations = conn.execute('SELECT c.* FROM conversations c' + where + ' ORDER BY c.updated_at DESC LIMIT 100', values).fetchall()
            if not cid and conversations:
                cid = conversations[0]["id"]
            focused = conn.execute('SELECT 1 FROM messages WHERE id=? AND conversation_id=?', (around, cid)).fetchone() if around else None
            if focused:
                preceding = conn.execute('SELECT * FROM messages WHERE conversation_id=? AND id<=? ORDER BY id DESC LIMIT ?', (cid, around, limit//2)).fetchall()
                following = conn.execute('SELECT * FROM messages WHERE conversation_id=? AND id>? ORDER BY id LIMIT ?', (cid, around, limit-len(preceding))).fetchall()
                messages = list(reversed(following)) + list(preceding)
            else:
                around = 0
                messages = conn.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?", (cid, limit)).fetchall()
            total = conn.execute('SELECT COUNT(*) FROM messages WHERE conversation_id=?', (cid,)).fetchone()[0]
        result = [dict(row) for row in reversed(messages)]
        for row in result:
            row["segments"] = json.loads(row["segments"] or "[]")
        context = self.conversation_context(cid) if cid else None
        return {"conversation_id": cid, "conversations": [dict(row) for row in conversations], "messages": result, 'has_more': total > len(result), 'search_hits': hits, 'focus_id': around,
                'space': context}

    def conversation_context(self, cid):
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT space_id FROM conversations WHERE id=?', (cid,)).fetchone()
        if not row or not row['space_id']:
            return None
        from social_context import SocialContext
        return SocialContext(self.root).space(row['space_id'])

    def _record(self, cid, direction, text, segments, status, error=""):
        with closing(self.connect()) as conn:
            cur = conn.execute("INSERT INTO messages(conversation_id,direction,text,segments,status,error,created_at) VALUES(?,?,?,?,?,?,?)", (cid, direction, text, json.dumps(segments, ensure_ascii=False), status, error[:500], time.strftime("%Y-%m-%d %H:%M:%S")))
            conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (time.strftime("%Y-%m-%d %H:%M:%S"), cid))
            conn.commit()
            return cur.lastrowid

    def _status(self, mid, status, error=""):
        with closing(self.connect()) as conn:
            conn.execute("UPDATE messages SET status=?,error=? WHERE id=?", (status, error[:500], mid))
            conn.commit()

    def _client(self):
        from runtime_adapter import DSHRuntimeAdapter
        return DSHRuntimeAdapter(self.root, self.role, self.runtime).connect(self.cancel_event,
            require_isolation=bool(getattr(self, '_isolation_requested', False)))

    def send(self, cid: str, text: str, attachments: list[dict]) -> dict:
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("这个角色正在回复，请等待或取消")
        self.cancel_event.clear()
        mid = None
        try:
            if self.policy.config()["paused"]:
                raise PermissionError("主人已暂停任务，请在系统设置或主人私聊恢复")
            if not text.strip() and not attachments:
                raise ValueError("请输入文字或添加照片")
            with closing(self.connect()) as conn:
                conversation = conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if conversation is None:
                raise ValueError("对话不存在")
            space = self.conversation_context(cid)
            self._isolation_requested = bool(space)
            scope = space['scope'] if space else 'owner'
            imported = []
            for attachment in attachments[:3]:
                source = Path(attachment["path"]).resolve()
                if not source.is_file() or source.stat().st_size > 20 * 1024 * 1024:
                    raise ValueError("附件不存在或超过 20MB")
                directory = self.db.parent / ("spaces/" + space['id'] if space else "chat-attachments") / uuid.uuid4().hex
                directory.mkdir(parents=True)
                target = directory / ("attachment" + source.suffix.lower())
                shutil.copy2(source, target)
                imported.append({"type": attachment["type"], "path": str(target)})
            mid = self._record(cid, "inbound", text or "[图片]", imported, "processing")
            with closing(self.connect()) as conn:
                conn.execute("UPDATE conversations SET title=CASE WHEN title='新对话' THEN ? ELSE title END WHERE id=?", ((text or "图片对话")[:40], cid))
                conn.commit()
            media = MediaService(self.root, self.role)
            if space:
                from social_context import SocialContext
                media.directory = SocialContext(self.root).directory(space['id']) / 'media'
            content = media.prepare_content(imported, text or "请理解这张图片。", local=True, cancelled=self.cancel_event)
            from memory_store import MemoryStore
            content[0]["text"] = self.policy.identity_prompt(desktop=True) + MemoryStore(self.root, self.role).context(text, scope) + content[0]['text']
            if space:
                content[0]['text'] = SocialContext(self.root).prompt(space['id']) + content[0]['text']
            if self.cancel_event.is_set():
                raise InterruptedError("已取消")
            self.policy.consume("calls")
            client = self._client()
            result = asyncio.run(self._turn(client, conversation["session_id"], cid, content))
            if result.get("cancelled") or self.cancel_event.is_set():
                raise InterruptedError("已取消")
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "没有取得完整回复"))
            self.policy.check_output(result["text"])
            self._record(cid, "outbound", result["text"], [], "done")
            MemoryStore(self.root, self.role).enqueue('desktop:' + str(mid), text, scope)
            self._status(mid, "done")
            return self.snapshot(cid)
        except Exception as exc:
            if mid is not None:
                status = "cancelled" if isinstance(exc, InterruptedError) else "blocked" if isinstance(exc, PermissionError) else "failed"
                self._status(mid, status, str(exc))
            raise
        finally:
            self.active_session = None
            self.lock.release()

    async def _turn(self, client, session, cid, content):
        await client.auth()
        if session:
            try:
                baseline = await client.baseline(session)
            except Exception:
                # Preserve the desktop conversation when the runtime history
                # cannot be resumed. Require an explicit new conversation.
                raise RuntimeError("运行时会话无法恢复，请新建对话；原记录仍保留")
        else:
            context = self.conversation_context(cid)
            if context:
                from social_context import SocialContext
                session = await client.create_session(cwd=str(SocialContext(self.root).directory(context['id'])))
            else:
                session = await client.create_session()
            with closing(self.connect()) as conn:
                conn.execute("UPDATE conversations SET session_id=? WHERE id=?", (session, cid))
                conn.commit()
            baseline = await client.baseline(session)
        self.active_session = session
        from tool_host import bind_session
        space = self.conversation_context(cid)
        if space:
            from social_context import SocialContext
            SocialContext(self.root).bind_runtime(space['id'], session)
            bind_session(self.root, self.role, session, scope=space['scope'], desktop=True)
        else:
            bind_session(self.root, self.role, session, desktop=True)
        if self.cancel_event.is_set():
            return {"cancelled": True}
        await client.prompt(session, content)
        return await client.wait_response(session, baseline, 180, cancel_event=self.cancel_event)

    def cancel(self):
        self.cancel_event.set()
        if self.active_session:
            return self.runtime.cancel(self.role, self.active_session)
        return {"ok": True}
