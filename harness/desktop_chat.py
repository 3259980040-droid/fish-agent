"""Persistent Fish chat; legacy work sessions retain their DSH execution path."""
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
from storage import locked_settings


class DesktopChat:
    def __init__(self, root: Path, role: str, runtime):
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
        """Switch this conversation between chat and work without a new conversation.

        The conversation id, space, runtime session, persona revision, messages
        and attachments never change. The native working-directory service
        applies an updated workspace before the next work turn.
        """
        if self.role=='qq':
            raise ValueError('Fish已使用自己的Agent工具，不再切换DSH工作模式；直接交办任务即可')
        if mode not in ('chat', 'work'):
            raise ValueError('未知对话模式')
        with self._state_lock:
            if self.active_cid == cid:
                raise RuntimeError('请等待当前回复完成或取消后再切换模式')
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone()
            if not row:
                raise ValueError('请选择对话')
            target = row['workspace']
            if mode == 'work':
                if workspace:
                    target = workspace
                elif not target:
                    from owner_computer import workspace as owner_workspace
                    target = str(owner_workspace(self.root))
                directory = Path(target).resolve()
                if not directory.is_dir():
                    raise ValueError('请选择存在的工作目录')
                target = str(directory)
            db.execute('UPDATE conversations SET mode=?,workspace=?,updated_at=? WHERE id=?',
                       (mode, target, time.strftime("%Y-%m-%d %H:%M:%S"), cid))
            db.commit()
            settings = dict(db.execute('SELECT mode,workspace,revision,permission FROM conversations WHERE id=?', (cid,)).fetchone())
        return {'ok': True, 'conversation_id': cid, 'session_id': row['session_id'] or '',
                'space_id': row['space_id'] or '', 'settings': settings,
                'note': '已切换到' + ('工作模式' if mode == 'work' else '聊天模式') + '；同一对话、人格与历史保持不变。'}

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
                'space': context, 'settings':dict(settings) if settings else {},'activity':getattr(self.active_client,'activity',{}) if cid==self.active_cid else {}}

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

    def send(self, cid: str, text: str, attachments: list[dict], *, cancelled=None, message_id=None) -> dict:
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("这个角色正在回复，请等待或取消")
        with self._state_lock:
            self.cancel_event = cancelled if cancelled is not None else threading.Event()
            self.active_cid = cid
            self._committed = False
        mid = message_id
        try:
            if self.policy.config()["paused"]:
                raise PermissionError("主人已暂停任务，请在系统设置或主人私聊恢复")
            if not text.strip() and not attachments:
                raise ValueError("请输入文字或添加照片")
            with closing(self.connect()) as conn:
                conversation = conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if conversation is None:
                raise ValueError("对话不存在")
            space = None if self.role == 'qq' else self.conversation_context(cid)
            # Resolve the queued turn's route from storage. An active turn must
            # finish or cancel before its conversation can switch routes.
            work = self.role != 'qq' and self._mode(cid)['mode'] == 'work'
            self._isolation_requested = bool(space)
            scope = 'work:'+cid if work else space['scope'] if space else 'owner'
            mid = mid if mid is not None else self.receive(cid, text, attachments)
            with closing(self.connect()) as db:
                inbound = db.execute('SELECT * FROM messages WHERE id=? AND conversation_id=?', (mid, cid)).fetchone()
            if not inbound:
                raise ValueError('已接收的原始消息不存在，未重复建立消息')
            from experience import was_considered
            if was_considered(self.root, 'desktop:' + str(mid)):
                self._status(mid, 'considered', '')
                return self.snapshot(cid)
            imported = json.loads(inbound['segments'] or '[]')
            self._status(mid, 'processing')
            media = MediaService(self.root, self.role)
            if space:
                from social_context import SocialContext
                media.directory = SocialContext(self.root).directory(space['id']) / 'media'
            content = media.prepare_content(imported, text or "请查看附件。", local=True,
                                          cancelled=self.cancel_event, include_sources=not work, source_reference='desktop:' + str(mid))
            from memory_store import MemoryStore
            memory = MemoryStore(self.root, self.role)
            if self.cancel_event.is_set():
                raise InterruptedError("已取消")
            self.policy.consume("calls")
            if work:
                # Only the DSH work prompt is pre-searched; the chat turn leaves
                # web results to the model's own tools.
                from web_knowledge import WebKnowledge
                web = WebKnowledge(self.root).resolve(cid, text, self.cancel_event)
                content[0]['text'] = self._prompt_text(cid, text, memory, scope) + web + content[0]['text']
                client = self._client()
                result = asyncio.run(self._turn(client, conversation['session_id'], cid, content))
            else:
                result = self._chat_turn(cid, mid, content, imported, media, memory, scope, space)
            if result.get("cancelled") or self.cancel_event.is_set():
                raise InterruptedError("已取消")
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "没有取得完整回复"))
            if result.get('silent'):
                self._status(mid, 'considered', '')
                return self.snapshot(cid)
            if not work:self.policy.check_output(result["text"])
            from chat_output import decode_reply,local_segments
            bubbles = decode_reply(self.root,result['text']) if self.role=='qq' else [{'kind':'text','text':result['text']}]
            with self._state_lock:
                if self.cancel_event.is_set():
                    raise InterruptedError('本地已取消等待；晚到结果未保存为成功回复')
                # Cancellation and successful delivery share one commit boundary.
                from experience import delivery
                delivery(self.root, 'desktop:' + str(mid), sending=True)
                with closing(self.connect()) as conn:
                    for bubble in bubbles:
                        conn.execute('INSERT INTO messages(conversation_id,direction,text,segments,status,error,created_at) VALUES(?,?,?,?,?,?,?)',
                            (cid,'outbound',str(bubble.get('text') or ''),json.dumps(local_segments(self.root,bubble),ensure_ascii=False),'done','',time.strftime('%Y-%m-%d %H:%M:%S')))
                    conn.execute("UPDATE messages SET status='done',error='' WHERE id=?", (mid,))
                    conn.execute('UPDATE conversations SET updated_at=? WHERE id=?', (time.strftime('%Y-%m-%d %H:%M:%S'), cid))
                    conn.commit()
                self._committed = True
                delivery(self.root, 'desktop:' + str(mid), outcome='sent')
            if not work:
                try:
                    memory.enqueue('desktop:' + str(mid), text, scope)
                except Exception:
                    self.policy.audit('desktop_memory_enqueue_failed', '回复已交付；后续记忆提取未排队')
            return self.snapshot(cid)
        except Exception as exc:
            if mid is not None:
                status = "cancelled" if isinstance(exc, InterruptedError) else "blocked" if isinstance(exc, PermissionError) else "failed"
                self._status(mid, status, str(exc))
            raise
        finally:
            with self._state_lock:
                self.active_session = None
                self.active_client = None
                self.active_cid = ''
            self.lock.release()

    def _chat_turn(self, cid, mid, content, imported, media, memory, scope, space):
        from desktop_context import DesktopContext
        from model_gateway import ModelGateway
        from social_context import SocialContext
        from conversation_style import private_context
        from fish_chat import respond
        from tool_host import bind_session
        # Unreadable attachments keep the turn alive, with the same note QQ uses.
        from owner_private import note_media_gaps
        note_media_gaps(imported, content, self.policy)
        context = DesktopContext(self.root, self.db)
        context.remember_input(mid, content)
        with closing(self.connect()) as db:
            db.execute('UPDATE messages SET segments=? WHERE id=?', (json.dumps(imported, ensure_ascii=False), mid))
            db.commit()
        gateway = ModelGateway(self.root, self.role)
        self.active_client = gateway
        from experience import ExperienceTurn
        experience = ExperienceTurn(self.root, self.db, cid, mid)
        revision = self._mode(cid)['revision']
        contexts = SocialContext(self.root)
        persona = contexts.active_persona('qq') if self.role == 'qq' else space['persona'] if space else contexts.persona(revision) if revision else contexts.ensure_default(self.role)
        session = 'desktop:' + cid
        bind_session(self.root, self.role, session, 'owner', desktop=True)
        tool_context = 'owner-desktop' if self.role == 'qq' else ''
        from chat_output import TurnSilent
        try:
            answer = respond(self.root, self.role, persona=persona, history=[], history_source={},
            memory=memory.context(content[0]['text'], scope), scope=scope,
            references=private_context(self.root, content[0]['text'], conversation=cid, desktop=True),
            current=media.chat_completions_content(content), session=session,
            cancelled=self.cancel_event, origin='desktop-chat:' + cid, tool_context=tool_context, experience=experience,
            output='bubbles' if self.role=='qq' else 'text',emoji_state={'previewed':set(),'tools':[]})
        except TurnSilent:
            return {'ok': True, 'silent': True, 'text': ''}
        return {'ok': True, 'text': answer}

    def _mode(self, cid):
        """Current persisted settings of one conversation (mode read per phase)."""
        with closing(self.connect()) as db:
            row = db.execute('SELECT mode,workspace,revision,permission FROM conversations WHERE id=?', (cid,)).fetchone()
        if not row:
            raise ValueError("对话不存在")
        return dict(row)

    def _prompt_text(self, cid, text, memory, scope):
        """Program prefix for one turn; the user's own words are left raw."""
        from conversation_style import private_context
        style=private_context(self.root,text,conversation=cid,desktop=True) if self._mode(cid)['mode']=='chat' else ''
        return memory.context(text, scope)+style

    async def _turn(self, client, session, cid, content):
        self.active_client=client
        await client.auth()
        settings=self._mode(cid)
        if session:
            try:
                baseline = await client.baseline(session)
            except Exception:
                # Preserve the desktop conversation when the runtime history
                # cannot be resumed. Require an explicit new conversation.
                raise RuntimeError("运行时会话无法恢复，请新建对话；原记录仍保留")
        else:
            context = self.conversation_context(cid)
            if settings['mode']=='work':
                session=await client.create_session(cwd=settings['workspace'])
            elif context:
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
        settings = self._mode(cid)  # set_mode() may have run while awaiting the client.
        if settings['mode']=='work':
            bind_session(self.root,self.role,session,scope='work:'+cid,desktop=True)
            port=self.runtime.config.runtime_port(self.role)
            response=await asyncio.to_thread(requests.post,f'http://127.0.0.1:{port}/fish/work-permission',
                json={'session':session,'preset':settings['permission']},headers={'x-fish-token':control_token(self.root)},timeout=10,
                proxies={'http':None,'https':None,'all':None})
            if not response.ok:raise RuntimeError(response.json().get('error','工作工具权限未能应用'))
        elif space:
            from social_context import SocialContext
            SocialContext(self.root).bind_runtime(space['id'], session)
            bind_session(self.root, self.role, session, scope=space['scope'], desktop=True)
        else:
            bind_session(self.root, self.role, session, desktop=True)
        if self.cancel_event.is_set():
            return {"cancelled": True}
        await client.prompt(session, content)
        return await client.wait_response(session, baseline, 1800 if self._mode(cid)['mode']=='work' else 180, cancel_event=self.cancel_event)

    def cancel(self):
        with self._state_lock:
            if self._committed:
                return {'ok': True, 'note': '本轮回复已完成保存'}
            self.cancel_event.set()
            if self.active_session:
                return self.runtime.cancel(self.role, self.active_session)
            return {'ok': True, 'note': '已请求取消本地等待；未确认服务商是否停止计算'}
