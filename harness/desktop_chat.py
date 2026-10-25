"""Persistent Fish chat records for the native client; the turn itself is the runtime's.

This module owns the client's conversation store: conversations, inbound records and
the outbound bubbles the page reads. It no longer generates a reply - a submission
goes to the one main execution, and the runtime's delivery writes the answer here
through the same atomic commit the old path used.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

from governance import Governance
from storage import locked_settings


class DesktopChat:
    def __init__(self, root: Path, role: str, runtime=None):
        if role not in ("qq", "research"):
            raise ValueError("未知角色")
        self.root, self.role, self.runtime = root, role, runtime
        self.db = root / "data" / role / "desktop-chat.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._committed = False
        self.active_session = None
        self.active_client = None
        self.active_cid = ''
        self.policy = Governance(root, role)
        with closing(self.connect()) as conn:
            conn.executescript("""CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,title TEXT,session_id TEXT,updated_at TEXT);
                CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,conversation_id TEXT,direction TEXT,text TEXT,
                segments TEXT,status TEXT,error TEXT,created_at TEXT);""")
            conn.commit()
            columns = {row[1] for row in conn.execute('PRAGMA table_info(conversations)')}
            if 'space_id' not in columns:
                conn.execute("ALTER TABLE conversations ADD COLUMN space_id TEXT NOT NULL DEFAULT ''")
            for name, default in (('mode', 'chat'), ('workspace', ''), ('revision', ''), ('permission','workspace-write')):
                if name not in columns:
                    conn.execute(f"ALTER TABLE conversations ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
            conn.commit()

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @locked_settings
    def new(self, title: str = "新对话", *, isolated=None, revision='', mode='chat', workspace='',permission='danger-full-access') -> str:
        cid = uuid.uuid4().hex
        if mode not in ('chat', 'work'): raise ValueError('未知对话模式')
        if permission not in ('workspace-write','danger-full-access'):raise ValueError('未知工具权限')
        if mode == 'work':
            isolated = False
            directory = Path(workspace or self.root).resolve()
            if not directory.is_dir(): raise ValueError('请选择存在的工作目录')
            workspace = str(directory)
        if isolated is None:
            isolated = False
        space = None
        if isolated:
            from social_context import SocialContext
            space = SocialContext(self.root).create_space(self.role, title, revision=revision)
        with closing(self.connect()) as conn:
            conn.execute("INSERT INTO conversations(id,title,session_id,updated_at,space_id,mode,workspace,revision,permission) VALUES(?,?,?,?,?,?,?,?,?)", (cid, title[:80], None, time.strftime("%Y-%m-%d %H:%M:%S"), space['id'] if space else '',mode,workspace,revision,permission))
            conn.commit()
        return cid

    def set_mode(self, cid: str, mode: str, workspace: str = '') -> dict:
        """Retired for Fish: she already uses her own Agent tools in one main execution."""
        raise ValueError('Fish已使用自己的Agent工具，不再切换DSH工作模式；直接交办任务即可')

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
        with closing(self.connect()) as conn:
            settings = conn.execute('SELECT mode,workspace,revision,permission FROM conversations WHERE id=?',(cid,)).fetchone()
        return {"conversation_id": cid, "conversations": [dict(row) for row in conversations], "messages": result, 'has_more': total > len(result), 'search_hits': hits, 'focus_id': around,
                'space': context, 'settings':dict(settings) if settings else {}}

    @locked_settings
    def reload_persona(self, cid, revision):
        """Keep ordinary history while changing the selected persona context."""
        from social_context import SocialContext
        store=SocialContext(self.root);store.persona(revision)
        with closing(self.connect()) as db:
            row=db.execute('SELECT * FROM conversations WHERE id=?',(cid,)).fetchone()
            if not row: raise ValueError('请选择对话')
            if row['space_id']:store.change_persona(row['space_id'],revision)
            db.execute('UPDATE conversations SET revision=? WHERE id=?',(revision,cid));db.commit()
        return {'ok':True,'note':'人格已重载；保留本窗口历史，后续回复使用新设定。'}

    def set_work_permission(self,cid,permission):
        if permission not in ('workspace-write','danger-full-access'):raise ValueError('未知工具权限')
        with closing(self.connect()) as db:
            row=db.execute("SELECT id FROM conversations WHERE id=? AND mode='work'",(cid,)).fetchone()
            if not row:raise ValueError('请选择本地工作对话')
            db.execute('UPDATE conversations SET permission=? WHERE id=?',(permission,cid));db.commit()

    def conversation_context(self, cid):
        if self.role == 'qq':
            return None
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT space_id FROM conversations WHERE id=?', (cid,)).fetchone()
        if not row or not row['space_id']:
            return None
        from social_context import SocialContext
        return SocialContext(self.root).space(row['space_id'])

    # ---------------------------------------------------------------- records
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

    def inbound(self, cid, mid):
        """The inbound record that was saved before submission, or None."""
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM messages WHERE id=? AND conversation_id=?', (int(mid), cid)).fetchone()
        return dict(row) if row else None

    def status(self, mid, status, error=""):
        """Record what actually happened to one inbound message."""
        self._status(mid, status, error)

    def update_segments(self, mid, segments):
        with closing(self.connect()) as db:
            db.execute('UPDATE messages SET segments=? WHERE id=?', (json.dumps(segments, ensure_ascii=False), mid))
            db.commit()

    def commit_reply(self, cid, mid, bubbles):
        """The answer and its delivery share one commit: a late result cannot half-land."""
        from chat_output import local_segments
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._state_lock:
            with closing(self.connect()) as conn:
                for bubble in bubbles:
                    conn.execute('INSERT INTO messages(conversation_id,direction,text,segments,status,error,created_at) VALUES(?,?,?,?,?,?,?)',
                                 (cid, 'outbound', str(bubble.get('text') or ''),
                                  json.dumps(local_segments(self.root, bubble), ensure_ascii=False),
                                  'done', '', stamp))
                if mid not in (None, 0, '0', ''):
                    conn.execute("UPDATE messages SET status='done',error='' WHERE id=?", (mid,))
                conn.execute('UPDATE conversations SET updated_at=? WHERE id=?', (stamp, cid))
                conn.commit()
            self._committed = True

    def mode(self, cid):
        """Current persisted settings of one conversation (mode read per phase)."""
        with closing(self.connect()) as db:
            row = db.execute('SELECT mode,workspace,revision,permission FROM conversations WHERE id=?', (cid,)).fetchone()
        if not row:
            raise ValueError("对话不存在")
        return dict(row)

    def receive(self, cid, text, attachments):
        """Persist an inbound immediately, even while this Fish is thinking."""
        if self.policy.config()['paused']:
            raise PermissionError('主人已暂停任务，请在系统设置或主人私聊恢复')
        if not str(text).strip() and not attachments:
            raise ValueError('请输入文字或添加附件')
        with closing(self.connect()) as db:
            if not db.execute('SELECT 1 FROM conversations WHERE id=?', (cid,)).fetchone():
                raise ValueError('对话不存在')
        from attachment_store import AttachmentStore
        store = AttachmentStore(self.root, self.role)
        imported = []
        for attachment in attachments:
            source = Path(attachment['path']).resolve()
            if not source.is_file():
                raise ValueError('附件原件不存在')
            record = store.import_path(cid, source, name=attachment.get('name', source.name),
                                       type_hint=attachment.get('type', ''), source='desktop')
            segment = {'id': record['id'], 'type': record['type'], 'path': record['path'],
                       'name': record['name'], 'conversation_id': cid, 'size': record['size'],
                       'resource_id': record['resource_id'], 'processing': record['processing']}
            if record['type'] == 'image':
                from image_views import ImageViews
                resource = ImageViews(self.root).register(record['path'], reference='attachment:' + record['id'])
                segment.update(resource_id=resource['doc_id'], resource_scope='owner')
            imported.append(segment)
        mid = self._record(cid, 'inbound', text or '[附件]', imported, 'queued')
        with closing(self.connect()) as db:
            db.execute("UPDATE conversations SET title=CASE WHEN title='新对话' THEN ? ELSE title END WHERE id=?",
                       ((text or '附件对话')[:40], cid))
            db.commit()
        return mid

    # ------------------------------------------------------------- submission
    def send(self, cid: str, text: str, attachments: list[dict], *, cancelled=None, message_id=None) -> dict:
        """Blocking submission kept for the old desktop callers.

        The turn runs in the same one main execution the client uses; this call only
        waits for its real result. Nothing is generated here.
        """
        from fish_agent import get_runtime
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("这个角色正在回复，请等待或取消")
        mid = message_id
        previous_cid = self.active_cid
        try:
            if self.policy.config()["paused"]:
                raise PermissionError("主人已暂停任务，请在系统设置或主人私聊恢复")
            if not text.strip() and not attachments:
                raise ValueError("请输入文字或添加照片")
            with closing(self.connect()) as conn:
                if conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone() is None:
                    raise ValueError("对话不存在")
            mid = mid if mid is not None else self.receive(cid, text, attachments)
            self.active_cid = cid
            runtime = get_runtime(self.root)
            submission = runtime.execution.submit(
                source='desktop', conversation=cid, message=mid, text=text,
                reference='desktop:' + str(mid), request='')
            run = runtime.execution.wait(submission['execution'], cancelled=cancelled)
            if run['status'] == 'cancelled':
                raise InterruptedError(run['error'] or '已取消')
            if run['status'] == 'failed':
                kind = run['error_kind'] or 'RuntimeError'
                message = run['error'] or '没有取得完整回复'
                if kind in ('TimeoutError', 'InterruptedError'):
                    raise (TimeoutError if kind == 'TimeoutError' else InterruptedError)(message)
                raise RuntimeError(message)
            return self.snapshot(cid)
        except Exception as exc:
            if mid is not None and not isinstance(exc, InterruptedError):
                self._status(mid, "cancelled" if isinstance(exc, InterruptedError) else "blocked"
                             if isinstance(exc, PermissionError) else "failed", str(exc))
            raise
        finally:
            self.active_cid = previous_cid
            with self._state_lock:
                self.active_session = None
                self.active_client = None
            self.lock.release()

    def cancel(self):
        """Cancel the real work: this conversation's queued submissions and its turn."""
        from fish_agent import get_runtime
        if self._committed:
            return {'ok': True, 'note': '本轮回复已完成保存'}
        conversation = self.active_cid
        if not conversation:
            return {'ok': True, 'note': '没有正在进行的回合'}
        info = get_runtime(self.root).execution.cancel(conversation)
        return {'ok': True, 'note': info['note'], **info}
