"""Native DSH Fish panel operations, backed by the existing Fish services."""
from __future__ import annotations
from locations import project_path, integration_settings, save_integration_option

from pathlib import Path
import json
import os
import re
import sqlite3
import threading
import time
import uuid

from storage import database

# The life page reads her own state; every count below is a SELECT on a
# connection that may not write, so opening the page never changes a ledger.
LIFE_KINDS = ('fact', 'preference', 'relationship', 'event', 'promise', 'meme', 'emotion')
LIFE_SOCIAL = ('topic', 'thought', 'impression')
LIFE_REMINDER_ON = {'1', 'on', 'true', 'yes'}
LIFE_EVENT_STATE = {'open': '待办', 'completed': '已完成', 'cancelled': '已取消'}


def _query_only(path):
    conn = sqlite3.connect(str(path), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _reminder_user_switch():
    """The User-level FISH_MEMORY_REMINDER, read without touching this process."""
    if os.name != 'nt':
        return ''
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            value, _type = winreg.QueryValueEx(key, 'FISH_MEMORY_REMINDER')
        return str(value or '')
    except OSError:
        return ''


def _life_reminder(root):
    process = str(os.environ.get('FISH_MEMORY_REMINDER', '')).strip()
    on = process.lower() in LIFE_REMINDER_ON
    user = _reminder_user_switch()
    if on:
        source = '进程环境变量 FISH_MEMORY_REMINDER=' + process
    elif user.lower() in LIFE_REMINDER_ON:
        source = '用户环境变量已设为 ' + user + '，当前后台进程还没读到，重启后生效'
    else:
        source = '未开启（用户环境变量 FISH_MEMORY_REMINDER 未设置）'
    state = {'on': on, 'source': source, 'ledger_count': 0, 'last_state': '', 'last_receipt': ''}
    path = project_path(root, 'data/qq/memory.db')
    if not path.is_file():
        return state
    conn = _query_only(path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_reminders'").fetchone():
            return state
        state['ledger_count'] = int(conn.execute('SELECT COUNT(*) FROM memory_reminders').fetchone()[0])
        row = conn.execute('SELECT state,receipt FROM memory_reminders ORDER BY updated DESC,id DESC LIMIT 1').fetchone()
        if row:
            state['last_state'] = str(row['state'] or '')
            state['last_receipt'] = str(row['receipt'] or '')
    finally:
        conn.close()
    return state


def _life_memory(root):
    state = {'kinds': {kind: 0 for kind in LIFE_KINDS},
             'social': {kind: 0 for kind in LIFE_SOCIAL},
             'open_events': 0, 'next_due': 0.0, 'next_due_text': ''}
    path = project_path(root, 'data/qq/memory.db')
    if not path.is_file():
        return state
    now = time.time()
    conn = _query_only(path)
    try:
        for row in conn.execute("SELECT kind,COUNT(*) AS n FROM memories WHERE status='active' GROUP BY kind"):
            if str(row['kind'] or '') in state['kinds']:
                state['kinds'][str(row['kind'])] = int(row['n'])
        for row in conn.execute("SELECT kind,COUNT(*) AS n FROM memories WHERE kind IN ('topic','thought','impression') "
                                "AND status IN ('active','candidate') AND scope='owner' GROUP BY kind"):
            state['social'][str(row['kind'])] = int(row['n'])
        state['open_events'] = int(conn.execute(
            "SELECT COUNT(*) FROM memories WHERE kind='event' AND status='active' AND action='open'").fetchone()[0])
        due = conn.execute("SELECT MIN(due) FROM memories WHERE kind='event' AND status='active' "
                           "AND action='open' AND due>? AND due>0", (now,)).fetchone()[0]
    finally:
        conn.close()
    if due:
        state['next_due'] = float(due)
        state['next_due_text'] = time.strftime('%Y-%m-%d %H:%M', time.localtime(float(due)))
    return state


# ---------------------------------------------------------------- chat traces
# The audit kinds that record what happened to one group message: the
# pre-screen score, the judge's verdict, or the reason no turn started at all.
TRACE_KINDS = ('group_prescreen_skip', 'group_prescreen', 'group_fish_silent', 'group_fish_skipped')
# English keys qq_agent_loop audits, in the owner's words. A key that is not
# here is dropped rather than printed raw: the panel never shows a reason it
# cannot read (no "unknown", no bare token).
TRACE_REASON_TEXT = {
    'not-woken': '本次未被唤醒', 'cooldown': '冷却中', 'limits': '达到上限', 'dive': '潜水中',
    'not-worth-speaking': '判断层认为不值得开口', 'cursor-moved': '话头已被别人接走',
    'paused': 'Fish 已暂停', 'cancelled': '本轮被取消', 'own-or-missing-sender': '没有识别到发言人',
}
# The five pre-screen dimensions, in scoring order.
TRACE_DIMS = ('content', 'will', 'social', 'timing', 'coherence')
# How far an audit row may sit from the message it decided.
TRACE_WINDOW_S = 120.0


def _epoch(text):
    """Seconds of a stored 'YYYY-MM-DD HH:MM:SS' row, or None when it is not one."""
    try:
        return time.mktime(time.strptime(str(text)[:19], '%Y-%m-%d %H:%M:%S'))
    except (TypeError, ValueError):
        return None


def _trace_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trace_reason(text):
    """Chinese phrase of one audited reason, or '' when only an unknown key is left."""
    text = ' '.join(str(text or '').split())
    if not text:
        return ''
    head, _, rest = text.partition(':')
    key = head.strip()
    if key in TRACE_REASON_TEXT:
        detail = rest.strip()
        return TRACE_REASON_TEXT[key] + ('：' + detail if detail and not detail.isascii() else '')
    if re.fullmatch(r'[A-Za-z][A-Za-z\- ]*', text):
        return ''
    return text


def _group_trace(kind, reason):
    """One group audit row as the panel's trace: score, five dimensions, reason."""
    text = ' '.join(str(reason or '').split())
    # The loop appends the chat to every audit reason; the panel shows the decision only.
    text = re.sub(r'\s+group:[0-9A-Za-z:_\-]+$', '', text)
    trace = {'kind': 'group', 'event': str(kind)}
    score = re.search(r'预筛\s*([0-9]+(?:\.[0-9]+)?)', text)
    threshold = re.search(r'阈值\s*([0-9]+(?:\.[0-9]+)?)', text)
    dims = {key: float(value) for key, value in re.findall(r'([a-z]+)=([0-9]+(?:\.[0-9]+)?)', text)
            if key in TRACE_DIMS}
    if score:
        trace['score'] = float(score.group(1))
    if threshold:
        trace['threshold'] = float(threshold.group(1))
    if dims:
        trace['dims'] = dims
    judgement = text.partition('：')[2].partition('（')[0].strip() if '：' in text else text
    phrase = _trace_reason(judgement)
    if phrase:
        trace['reason'] = phrase[:200]
    return trace


def _segment_kinds(segments):
    """Distinct segment types of one stored message, in stored order."""
    try:
        data = json.loads(segments or '[]')
    except ValueError:
        return []
    kinds = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get('type') or '').strip()
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def _payload_tools(payload):
    """Tool names one inspected request carried, in call order."""
    try:
        data = json.loads(payload)
    except ValueError:
        return ()
    names = []
    for message in (data.get('messages') or []) if isinstance(data, dict) else []:
        if not isinstance(message, dict) or str(message.get('role')) != 'assistant':
            continue
        for call in message.get('tool_calls') or []:
            function = call.get('function') if isinstance(call, dict) else None
            name = str((function or {}).get('name') or '').strip() if isinstance(function, dict) else ''
            if name and name not in names:
                names.append(name)
    return tuple(names)


class Workbench:
    def __init__(self, root):
        from locations import instance_context
        self.context = instance_context(root)
        self.instance_id = self.context.instance_id
        self.root = self.context.root
        self.path = project_path(self.root, 'data/social/workbench.db')
        self._chat_instance = None
        self._delegation_instance = None
        self._selected_conversation = ''
        self._lock = threading.Lock()
        with database(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, conversation TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL)''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'input_message' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN input_message INTEGER')
            if 'execution' not in columns:
                # The client's request map only keeps the reference; the real status is
                # always read back from the one main execution's own turn ledger.
                db.execute("ALTER TABLE jobs ADD COLUMN execution TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE jobs SET status='failed',error='服务已重启，原记录保留，请继续任务',updated=? WHERE status IN ('queued','running')", (time.time(),))

    def runtime(self):
        from fish_agent import get_runtime
        return get_runtime(self.root)

    def chat(self):
        with self._lock:
            if self._chat_instance is None:
                from core import HarnessConfig, RuntimeManager
                from desktop_chat import DesktopChat
                self._chat_instance = DesktopChat(self.root, 'qq', RuntimeManager(HarnessConfig(self.root)))
            return self._chat_instance

    def delegation(self):
        with self._lock:
            if self._delegation_instance is None:
                from task_delegation import TaskDelegation
                self._delegation_instance = TaskDelegation(self.root, on_result=self._task_result)
            return self._delegation_instance

    def _task_result(self,row):
        if row['config'].get('notify_target')!='desktop':
            return
        chat = self.chat()
        with database(chat.db) as db:
            exists = db.execute('SELECT 1 FROM conversations WHERE id=?',(row['source_conversation'],)).fetchone()
        if not exists:
            from governance import Governance
            Governance(self.root).audit('task.result_saved','原客户端记录已删除；结果保留任务页，没有恢复被删对话',row['id'])
            return
        from chat_output import local_segments
        bubbles = row['config'].get('result_bubbles') if row['status']=='completed' else None
        if not bubbles:
            bubbles = [{'kind':'text','text':'系统任务结果：'+row['title']+'\n状态：'+row['status']+'\n'+(row['error'] or row['result'])}]
        from attachment_store import AttachmentStore
        archive = AttachmentStore(self.root,'qq')
        for index,bubble in enumerate(bubbles):
            parts = [{**part,'task_id':row['id'],'origin':'fish-task' if row['status']=='completed' else 'system-task'} for part in local_segments(self.root,bubble)]
            if index==0:
                for artifact in row['artifacts']:
                    file = archive.get(artifact['id'])
                    if file:
                        parts.append({'type':'file','id':file['id'],'name':file['name'],'path':file['path'],'resource_id':file['resource_id'],'task_id':row['id']})
            chat._record(row['source_conversation'],'outbound',str(bubble.get('text') or ''),parts,'done')

    def settings(self):
        from governance import Governance
        from vision_identification import settings as image_settings
        cfg = Governance(self.root).config()
        used = int(Governance(self.root).snapshot()['usage'].get('calls', 0))
        limit = int(cfg['max_daily_calls'])
        return {'paused': cfg['paused'], 'daily_limits': {
                    'limit': limit, 'used': used, 'remaining': max(0, limit - used),
                    'unlimited': not cfg['daily_usage_limits']['qq']},
                'owner_permissions': cfg['owner_permissions'],
                'max_tool_calls_per_turn': cfg['max_tool_calls_per_turn'],
                'chat_reasoning_effort': cfg['chat_reasoning_effort'], 'reply_display': cfg['reply_display'],
                'image_identification_enabled':image_settings(self.root)['enabled']}

    def status(self):
        from governance import Governance
        from owner_mode import OwnerMode
        from social_context import SocialContext
        policy = Governance(self.root)
        contexts = SocialContext(self.root)
        persona = contexts.active_persona('qq')
        mode = OwnerMode(self.root).get()
        from governance import CAPABILITIES
        from storage import read_json
        return {**self.settings(), **mode, 'owner': policy.owner_id(), 'owner_capabilities': CAPABILITIES,
                'keep_background_on_close': integration_settings(self.root).get('keep_background_on_close', True),
                'qzone_enabled': integration_settings(self.root).get('qzone_enabled', False),
                'send_policy': policy.config()['send_policy'],
                'permissions': '、'.join(policy.owner_capabilities(desktop=True).values()),
                'persona': {k: persona[k] for k in ('id', 'name', 'body')},
                'paths': {'root': str(self.root), 'workspace': mode['workspace']}}

    # ---------------------------------------------------------------- chat view
    def _role_db(self, name):
        """One database of the role this workbench answers for."""
        return project_path(self.root, 'data', self.chat().role, name)

    def _group_ids(self):
        from storage import read_json
        config = read_json(project_path(self.root, 'channels/fish_qq/config.json'), {})
        return [str(value) for value in config.get('allowlist', {}).get('group_ids', [])]

    @staticmethod
    def _group_conversation_id(group_id):
        return 'qq:group:%s:qq' % group_id

    def _group_conversations(self):
        """Whitelisted QQ groups as read-only conversations, newest activity first."""
        rows = []
        path = self._role_db('qq.db')
        if path.is_file():
            conn = _query_only(path)
            try:
                for group_id in self._group_ids():
                    cid = self._group_conversation_id(group_id)
                    row = conn.execute('SELECT updated_at FROM conversations WHERE id=?', (cid,)).fetchone()
                    last = conn.execute('SELECT MAX(ts) FROM messages WHERE conversation_id=?', (cid,)).fetchone()[0]
                    if row is None and not last:
                        continue
                    rows.append({'id': cid, 'title': '', 'kind': 'group', 'group_id': group_id,
                                 'updated_at': str(last or row['updated_at'] or ''), 'read_only': True})
            finally:
                conn.close()
        rows.sort(key=lambda row: row['updated_at'], reverse=True)
        return rows

    def _group_messages(self, group):
        """The stored QQ thread of one group, oldest first, as chat rows."""
        path = self._role_db('qq.db')
        if not path.is_file():
            return []
        conn = _query_only(path)
        try:
            stored = [dict(row) for row in conn.execute(
                'SELECT id,ts,direction,text,segments FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 200',
                (group['id'],)).fetchall()]
        finally:
            conn.close()
        messages = []
        for row in reversed(stored):
            bot = str(row['direction'] or '') == 'outbound'
            # A picture or a voice note carries no text; its segment types are the
            # only honest label this page can show.
            messages.append({'id': 'q%s' % row['id'], 'created_at': str(row['ts'] or ''),
                             'direction': 'fish' if bot else 'owner', 'text': str(row['text'] or ''),
                             'kinds': _segment_kinds(row['segments']), 'status': 'complete' if bot else ''})
        return messages

    def _group_traces(self, group, messages):
        """The audit row that decided each group message, keyed by message id."""
        stamps = [(message, _epoch(message['created_at'])) for message in messages]
        stamps = [(message, stamp) for message, stamp in stamps if stamp is not None]
        path = self._role_db('governance.db')
        if not stamps or not path.is_file():
            return {}
        low = min(stamp for _, stamp in stamps) - TRACE_WINDOW_S
        high = max(stamp for _, stamp in stamps) + TRACE_WINDOW_S
        conn = _query_only(path)
        try:
            rows = conn.execute(
                'SELECT ts,kind,reason FROM audit WHERE kind IN (%s) AND reason LIKE ? AND ts>=? AND ts<=? ORDER BY ts,id'
                % ','.join('?' * len(TRACE_KINDS)),
                (*TRACE_KINDS, '%group:' + group['group_id'],
                 time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(low)),
                 time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(high)))).fetchall()
        finally:
            conn.close()
        traces = {}
        for row in rows:
            stamp = _epoch(row['ts'])
            if stamp is None:
                continue
            decided = None
            for message, when in stamps:
                if when > stamp + 1.0:
                    break
                decided = (message, when)
            if decided is None or stamp - decided[1] > TRACE_WINDOW_S:
                continue
            trace = _group_trace(row['kind'], row['reason'])
            if len(trace) <= 2:
                continue
            known = traces.get(decided[0]['id'])
            # A scored pre-screen explains more than the budget check that ran first.
            if known is None or ('score' not in known and 'score' in trace):
                traces[decided[0]['id']] = trace
        return traces

    def _persona_name(self, revision):
        if not revision:
            return ''
        path = project_path(self.root, 'data/social/context.db')
        if not path.is_file():
            return ''
        conn = _query_only(path)
        try:
            row = conn.execute('SELECT name FROM personas WHERE id=?', (revision,)).fetchone()
        finally:
            conn.close()
        return str(row['name'] or '') if row else ''

    # -------------------------------------------------------------- personas page
    def _current_persona(self):
        """The persona the owner's private chat is answering as right now."""
        from governance import Governance
        from social_context import SocialContext
        return SocialContext(self.root).active_persona('qq')

    def _persona_preview(self, text, revision_b=''):
        """One line answered twice: by the live persona and by a comparison version.

        Both sides are real model calls, and each side reports its own failure so a
        broken comparison never hides the answer that did come back.
        """
        from social_context import SocialContext
        line = str(text or '').strip()
        if not line:
            raise ValueError('请输入要预览的一句话')
        if len(line) > 500:
            raise ValueError('预览输入最多 500 字')
        contexts = SocialContext(self.root)
        live = self._current_persona()
        versions = contexts.personas('qq')
        if revision_b:
            other = contexts.persona(contexts.resolve(revision_b) if len(revision_b) < 32 else revision_b)
        else:
            # "上一版" is the version created just before the live one; a live default
            # that is missing from the history falls back to any other version.
            index = next((position for position, row in enumerate(versions) if row['id'] == live['id']), None)
            other = (versions[index + 1] if index is not None and index + 1 < len(versions)
                     else next((row for row in versions if row['id'] != live['id']), live))
        return {'model_calls': 2, 'text': line,
                'results': [self._persona_reply(live, line), self._persona_reply(other, line)]}

    def _persona_reply(self, persona, line):
        """One preview answer, or that side's own error and how long it took."""
        from fish_chat import respond
        started = time.monotonic()
        row = {'revision': str(persona['id']), 'name': str(persona['name'])}
        try:
            answer = respond(self.root, 'qq', persona=persona, history=[], history_source='persona-preview',
                             memory='', scope='owner', references='', current=[{'type': 'text', 'text': line}],
                             session='persona-preview:' + uuid.uuid4().hex, cancelled=threading.Event(),
                             origin='persona-preview', output='text', tool_context='')
        except Exception as exc:
            return {**row, 'error': str(exc)[:400], 'elapsed_ms': int((time.monotonic() - started) * 1000)}
        return {**row, 'text': str(answer or ''), 'elapsed_ms': int((time.monotonic() - started) * 1000)}

    def _private_events(self, cid, space):
        """Request starts of one private conversation, each with the tools it carried."""
        role = self.chat().role
        events = []
        usage = project_path(self.root, 'data', role, 'model-usage.db')
        if usage.is_file():
            conn = _query_only(usage)
            try:
                for row in conn.execute('SELECT created FROM requests WHERE origin=?', ('desktop-chat:' + cid,)):
                    stamp = _trace_number(row['created'])
                    if stamp is not None:
                        events.append((stamp, ()))
                for row in conn.execute('SELECT created,tool FROM tool_events WHERE origin LIKE ?', ('%' + cid + '%',)):
                    stamp = _trace_number(row['created'])
                    if stamp is not None:
                        events.append((stamp, (str(row['tool'] or ''),)))
            finally:
                conn.close()
        inspector = project_path(self.root, 'data/social/request-inspector.db')
        if inspector.is_file():
            conn = _query_only(inspector)
            try:
                rows = conn.execute(
                    'SELECT created,payload FROM requests WHERE session=? OR (scope=? AND ?<>\'\')',
                    ('desktop:' + cid, 'space:' + space, space)).fetchall()
            finally:
                conn.close()
            for row in rows:
                stamp = _trace_number(row['created'])
                if stamp is not None:
                    events.append((stamp, _payload_tools(str(row['payload'] or ''))))
        events.sort(key=lambda item: item[0])
        return events

    @staticmethod
    def _turn_trace(events, start, end, persona):
        """Tools, elapsed time and persona of one reply, or None when nothing is known."""
        # This turn's request starts fall after the previous message; only when a
        # single-request turn began before that message row was stored (the usual
        # case for a fast reply) does the window reach back a moment for it.
        window = [(stamp, tools) for stamp, tools in events if start < stamp <= end]
        if not window:
            window = [(stamp, tools) for stamp, tools in events if start - 2.0 <= stamp <= end]
        trace = {'kind': 'private'}
        tools = []
        for _, names in window:
            for name in names:
                if name and name not in tools:
                    tools.append(name)
        if tools:
            trace['tools'] = tools[:12]
        if window:
            elapsed = int(round((end - min(stamp for stamp, _ in window)) * 1000))
            if 50 <= elapsed <= 600000:
                trace['elapsed_ms'] = elapsed
        if persona:
            trace['persona'] = persona
        return trace if len(trace) > 1 else None

    def _private_traces(self, cid, messages):
        """What each private reply cost: tools, elapsed milliseconds, persona name."""
        persona = ''
        space = ''
        path = self._role_db('desktop-chat.db')
        if path.is_file():
            conn = _query_only(path)
            try:
                row = conn.execute('SELECT revision,space_id FROM conversations WHERE id=?', (cid,)).fetchone()
            finally:
                conn.close()
            if row:
                persona = self._persona_name(str(row['revision'] or ''))
                space = str(row['space_id'] or '')
        events = self._private_events(cid, space)
        traces = {}
        previous = None
        for message in messages:
            stamp = _epoch(message.get('created_at'))
            if stamp is None:
                continue
            assistant = str(message.get('direction') or '') in ('outbound', 'fish', 'out')
            if assistant:
                trace = self._turn_trace(events, stamp if previous is None else previous, stamp, persona)
                if trace is not None:
                    traces[str(message['id'])] = trace
            previous = stamp
        return traces

    def _chat_view(self, cid):
        """One conversation plus the why-chain of every message that has one.

        Private conversations live in this role's desktop-chat.db; a group
        conversation is one whitelisted QQ thread in qq.db, read only.
        """
        cid = str(cid or '')
        groups = self._group_conversations()
        group = next((row for row in groups if row['id'] == cid), None)
        result = self.chat().snapshot('' if group is not None else cid)
        for row in result['conversations']:
            row['kind'] = 'private'
        result['conversations'] = list(result['conversations']) + groups
        if group is None:
            result['kind'] = 'private'
            traces = self._private_traces(result['conversation_id'], result['messages'])
        else:
            result.update({'conversation_id': group['id'], 'messages': self._group_messages(group),
                           'settings': {}, 'kind': 'group', 'group_id': group['group_id']})
            traces = self._group_traces(group, result['messages'])
        for message in result['messages']:
            message['trace'] = traces.get(str(message['id']), {})
        from context_budget import snapshot as context_usage
        session = ('group-fish:group:' + group['group_id']) if group else ''
        result['context_usage'] = context_usage(self.root, session)['latest']
        # 有效预算、最近整理范围和它真实的结果：页面读这里，不需要自己再解析配置。
        from fish_agent.experience.compaction import status as compaction_status
        store = 'desktop-chat.db' if group is None else 'qq.db'
        result['context_compaction'] = compaction_status(
            self.root, store + ':' + str(result['conversation_id']))
        from turn_trace import latest as latest_thought
        result['thought'] = latest_thought(self.root)
        task_path = project_path(self.root, 'data/delegation/tasks.db')
        result['active_tasks'] = 0
        if task_path.is_file():
            with database(task_path) as db:
                result['active_tasks'] = db.execute("SELECT COUNT(*) FROM tasks WHERE source_conversation=? AND status IN ('queued','starting','running','cancel_requested')",(result['conversation_id'],)).fetchone()[0]
        return result

    def delete_conversations(self, params):
        """Delete private conversations and their messages; memory and QQ threads stay."""
        raw = params.get('conversation_ids')
        if raw is None:
            single = params.get('conversation_id')
            raw = [] if single in (None, '') else [single]
        if not isinstance(raw, list) or not raw:
            raise ValueError('请选择要删除的会话')
        if len(raw) > 100:
            raise ValueError('一次最多删除 100 个会话')
        wanted = []
        for value in raw:
            if not isinstance(value, str):
                raise ValueError('会话编号无效')
            if value:
                wanted.append(value)
        chat = self.chat()
        removed = {'conversations': 0, 'messages': 0, 'conversation_ids': []}
        removed_messages = []
        # The main execution owns the running turn; ask it, not a page-local flag.
        active = self.runtime().execution.active_conversation() or chat.active_cid
        if active and active in wanted:
            raise ValueError('这个会话正在回复，请先取消再删除')
        with chat._state_lock:
            conn = chat.connect()
            try:
                for cid in wanted:
                    if cid.startswith('qq:group:'):
                        raise ValueError('群聊记录属于 QQ 侧，这里只能删除私人会话')
                    if conn.execute('SELECT 1 FROM conversations WHERE id=?', (cid,)).fetchone() is None:
                        continue
                    removed_messages.extend(row[0] for row in conn.execute(
                        'SELECT id FROM messages WHERE conversation_id=?', (cid,)))
                    removed['messages'] += conn.execute('DELETE FROM messages WHERE conversation_id=?', (cid,)).rowcount
                    conn.execute('DELETE FROM conversations WHERE id=?', (cid,))
                    removed['conversations'] += 1
                    removed['conversation_ids'].append(cid)
                conn.commit()
            finally:
                conn.close()
        if removed_messages:
            # The FTS index is derived: forget these originals in the same delete path.
            from history_index import forget_messages
            forget_messages(self.root, 'desktop-chat.db', removed_messages)
        if self._selected_conversation in removed['conversation_ids']:
            self._selected_conversation = ''
        return {'ok': True, **removed}

    def dispatch(self, action, params):
        from locations import InstanceCatalog
        if not isinstance(params, dict):
            raise ValueError('操作参数必须是对象')
        if action == 'instances.list':
            return InstanceCatalog(self.context.host_root).list()
        selected = str(params.get('instance_id') or self.instance_id)
        if selected != self.instance_id:
            raise ValueError('工作台不属于所选Fish窗口：' + selected)
        params = {key: value for key, value in params.items() if key != 'instance_id'}
        return self._dispatch(action, params)

    def _dispatch(self, action, params):
        from bot_control import BotControl
        from governance import Governance
        from memory_store import MemoryStore
        from persona_library import PersonaLibrary
        from social_context import SocialContext
        if not isinstance(params, dict):
            raise ValueError('操作参数必须是对象')
        if action == 'status':
            return self.status()
        if action == 'faults.status':
            return self.fault_status()
        if action == 'activity.recent':
            # The same one stream the tool reads: private chat and every authorised
            # group merged by real time. Read only, and the same bounds, so the folded
            # panel can never show more than she could have answered from.
            from group_read import recent_lines
            bounded = {}
            for key in ('hours', 'limit'):
                if params.get(key) in (None, ''):
                    continue
                try:
                    bounded[key] = int(params[key])
                except (TypeError, ValueError):
                    raise ValueError(key + ' 需为整数')
            got = recent_lines(self.root, kind=str(params.get('kind') or 'all'), **bounded)
            # rows is the array name the folded panel already reads; kind, hours and
            # truncated ride along so the panel can say when it only shows the newest.
            return {'kind': got['kind'], 'hours': got['hours'], 'truncated': got['truncated'],
                    'rows': got['lines']}
        if action.startswith('delegations.'):
            service = self.delegation()
            if action == 'delegations.list':
                return service.list()
            if action == 'delegations.submit':
                cid = str(params.get('conversation_id') or '')
                source = str(params.get('message_id') or '')
                if cid:
                    self.chat()._mode(cid)
                    if source:
                        with database(self.chat().db) as db:
                            if not db.execute('SELECT 1 FROM messages WHERE id=? AND conversation_id=?', (source, cid)).fetchone():
                                raise ValueError('来源消息不属于当前对话')
                elif source:
                    raise ValueError('来源消息需要绑定对话')
                return service.submit(params.get('request_id'), params.get('title', ''), params.get('prompt', ''), cid, source)
            if action == 'delegations.get':
                return service.get(params['id'])
            if action == 'delegations.cancel':
                return service.cancel(params['id'])
            if action == 'delegations.download':
                return service.download(params['id'], params['artifact_id'], offset=params.get('offset', 0))
            if action == 'delegations.open':
                result = service.download(params['id'], params['artifact_id'])
                os.startfile(result['path'])
                return {'opened': True, 'path': result['path']}
            raise ValueError('未知委托操作')
        if action == 'mode.set':
            from owner_mode import OwnerMode
            OwnerMode(self.root).set(params['mode'], params.get('workspace'))
            return self.status()
        if action == 'settings.get':
            result = self.status()
            # The pacing numbers are read straight from where they are written, so the
            # settings page shows the value that actually paces her.
            import json as _json
            path = project_path(self.root, 'harness/context-settings.json')
            pacing = _json.loads(path.read_text(encoding='utf-8') or '{}') if path.is_file() else {}
            keys = ('proactive_check_min_s', 'proactive_check_max_s', 'round_backoff_base',
                    'round_backoff_cap', 'round_backoff_start_after', 'round_bypass_messages',
                    'round_min_interval')
            result['pacing'] = {k: int(pacing[k]) for k in keys if k in pacing}
            return result
        if action == 'settings.save' and any(k in params for k in (
                'proactive_check_min_s', 'proactive_check_max_s', 'round_backoff_base',
                'round_backoff_cap', 'round_backoff_start_after', 'round_bypass_messages',
                'round_min_interval', 'maintenance_interval_s')):
            # Autonomous pacing lives in context-settings.json: governance rewrites
            # control.json, and a number the owner typed must survive that.
            import json as _json
            path = project_path(self.root, 'harness/context-settings.json')
            current = {}
            if path.is_file():
                current = _json.loads(path.read_text(encoding='utf-8') or '{}')
            limits = {'proactive_check_min_s': (30, 3600), 'proactive_check_max_s': (30, 3600),
                      'round_backoff_base': (15, 3600), 'round_backoff_cap': (30, 3600),
                      'round_backoff_start_after': (1, 20), 'round_bypass_messages': (1, 200),
                      'round_min_interval': (30, 3600), 'maintenance_interval_s': (20, 300)}
            for key, (low, high) in limits.items():
                if key not in params:
                    continue
                value = int(params[key])
                if value < low or value > high:
                    raise ValueError('%s 需在 %d–%d 之间' % (key, low, high))
                current[key] = value
            path.write_text(_json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
            return {'saved': True, 'pacing': {k: current[k] for k in limits if k in current}}
        if action == 'settings.save':
            changes = {}
            if 'image_identification_enabled' in params:
                from vision_identification import settings as image_settings
                from storage import write_json
                value=params['image_identification_enabled']
                if type(value) is not bool:raise ValueError('识图资料核对需为开关值')
                write_json(project_path(self.root, 'harness/vision-identification.json'),{**image_settings(self.root),'enabled':value})
            for option in ('keep_background_on_close', 'qzone_enabled'):
                if option not in params: continue
                from storage import read_json, write_json
                if type(params[option]) is not bool: raise ValueError('开关必须为布尔值')
                save_integration_option(self.root, option, params[option])
            if 'paused' in params:
                if type(params['paused']) is not bool:
                    raise ValueError('暂停开关必须是布尔值')
                changes['paused'] = params['paused']
            for key in ('owner_permissions', 'send_policy', 'max_tool_calls_per_turn', 'chat_reasoning_effort', 'reply_display'):
                if key in params:
                    changes[key] = params[key]
            if 'daily_limits' in params:
                value = params['daily_limits']
                if type(value) is bool:
                    changes['daily_usage_limits'] = {'qq': value}
                elif isinstance(value, dict) and type(value.get('unlimited')) is bool:
                    changes['daily_usage_limits'] = {'qq': not value['unlimited']}
                    if value.get('limit') is not None:
                        changes['max_daily_calls'] = value['limit']
                else:
                    raise ValueError('请设置每日次数或选择无上限')
            Governance(self.root).save(changes)
            return self.status()
        if action == 'send.settings':
            return BotControl(self.root).execute('send_get')
        if action == 'memory.status':
            from memory_maintenance import MemoryMaintenance
            from memory_engine import MemoryEngine
            return {**MemoryMaintenance(self.root).status(), **MemoryEngine(self.root, 'qq').semantic_status()}
        if action.startswith('language.'):
            from language_workbench import LanguageWorkbench
            language = LanguageWorkbench(self.root)
            if action == 'language.status': return language.status()
            if action == 'language.list': return language.list(**params)
            if action == 'language.save': return language.save(**params, owner=Governance(self.root).owner_id())
            if action == 'language.manage': return language.manage(params['id'], params['status'])
        if action == 'channels.status':
            import requests
            from core import HarnessConfig
            from governance import control_token
            from group_policy import GroupPolicy
            from storage import read_json
            config = read_json(project_path(self.root, 'channels/fish_qq/config.json'), {})
            headers = {'x-fish-token': control_token(self.root)}
            loop = {}
            try:
                loop = requests.get('http://127.0.0.1:3102/status', headers=headers, timeout=2,
                                    proxies={'http': None, 'https': None, 'all': None}).json()
            except (requests.RequestException, ValueError):
                pass
            ready = False
            try:
                port = HarnessConfig(self.root).runtime_port('qq')
                ready = bool(requests.get(f'http://127.0.0.1:{port}/fish/status', headers=headers, timeout=2,
                                         proxies={'http': None, 'https': None, 'all': None}).json().get('ok'))
            except (requests.RequestException, ValueError):
                pass
            groups = []
            for group_id in config.get('allowlist', {}).get('group_ids', []):
                from group_tests import GroupTests
                from conversation_policy import community
                # The switches that actually decide whether this group hears and answers live
                # in the main policy: engine=fish reads it on every message. A group-test
                # window keeps its own copy and used to be what this page showed, so the
                # toggles wrote to a policy nobody was reading and appeared to do nothing.
                value = GroupPolicy(self.root).config('group:' + str(group_id))
                trial = GroupTests(self.root).current(str(group_id))
                groups.append({'id': str(group_id), 'listen': bool(value.get('listen')), 'reply': bool(value.get('reply')),
                    'topic': community(self.root, str(group_id)).get('topic',''),
                    'route': 'normal', 'window': trial['id'] if trial else '',
                    'engine': str(value.get('engine') or ''), 'learn': bool(value.get('learn')),
                    'read': bool(value.get('read'))})
            bot = loop.get('bot') or config.get('bot') or {}
            return {'owner': Governance(self.root).owner_id(), 'bot': str(bot.get('id', 'unknown')),
                    'private_route': 'private:' + Governance(self.root).owner_id(), 'groups': groups,
                    'services': [{'name': '主人消息处理', 'ready': bool(loop), 'detail': '已连接' if loop else '未连接'},
                                 {'name': '原生 DSH 工作环境', 'ready': ready, 'detail': '可用' if ready else '未启动'},
                                 {'name': '工具与记忆', 'ready': True, 'detail': '已连接'}],
                    'note': '主人交办的单次群发送与群聊自动参与分开设置。'}
        if action == 'channels.update':
            from group_policy import GroupPolicy
            from storage import read_json
            group_id = str(params.get('group_id', ''))
            config = read_json(project_path(self.root, 'channels/fish_qq/config.json'), {})
            allowed = {str(value) for value in config.get('allowlist', {}).get('group_ids', [])}
            if group_id not in allowed:
                raise ValueError('只能修改白名单群')
            changes = {key: params[key] for key in ('listen', 'read', 'reply') if key in params}
            if not changes and 'topic' not in params:
                raise ValueError('没有要保存的群设置')
            if any(type(value) is not bool for value in changes.values()):
                raise ValueError('群设置必须是开关值')
            from group_tests import GroupTests
            from conversation_policy import set_community
            if 'topic' in params: set_community(self.root, group_id, params['topic'])
            # Always the main policy: writing into a test window changed a copy that the
            # fish engine never reads, which is why the switch looked broken.
            policy = GroupPolicy(self.root)
            return policy.configure('group:' + group_id, changes) if changes else policy.config('group:' + group_id)
        if action == 'personas.list':
            contexts = SocialContext(self.root)
            return [{**row, 'body': contexts.persona(row['id'])['body']} for row in PersonaLibrary(self.root).list()]
        if action == 'personas.current':
            persona = self._current_persona()
            return {'id': persona['id'], 'revision': persona['id'],
                    'name': persona['name'], 'body': persona['body']}
        if action == 'personas.preview':
            return self._persona_preview(params.get('text', ''), str(params.get('revision_b') or ''))
        if action == 'personas.save':
            return PersonaLibrary(self.root).add(params['name'], params['body'], source='DSH Fish 人格编辑')
        if action == 'personas.create':
            return PersonaLibrary(self.root).create(params.get('name', ''), params.get('body', ''))
        if action == 'personas.clear':
            # Deleting old versions is destructive: clear() writes backups/ first
            # and keeps every persona a live window still points at.
            return PersonaLibrary(self.root).clear()
        if action == 'personas.import':
            rows = PersonaLibrary(self.root).import_text(params['text'], name=params.get('name', '导入人格'), source=params.get('source', 'DSH Fish 导入'))
            return [{'id': row['id'], 'name': row['name'], 'body': row['body']} for row in rows]
        if action == 'personas.apply':
            return PersonaLibrary(self.root).apply(params['revision'])
        if action == 'context.compact':
            # The owner's immediate compression uses the same selection/commit chain as
            # the automatic one; it is the only entry that ignores the automatic switch.
            from fish_agent.experience.compaction import compact_now
            source = str(params.get('source') or 'desktop-chat.db')
            conversation = str(params.get('conversation') or '')
            if not conversation:
                raise ValueError('缺少要整理的会话编号')
            return compact_now(self.root, source=source, conversation=conversation)
        if action == 'context.status':
            # 上下文管理的只读接口：有效预算、触发/保留比例，最近一次整理的完整范围和结果。
            # 09-06 接页面动作时读这里，不再自己解析 harness 配置或猜窗口容量。
            from context_budget import snapshot as context_usage
            from fish_agent.experience.compaction import status as compaction_status
            budget = context_usage(self.root)
            return {'effective_budget': budget['effective'],
                    'compaction': compaction_status(self.root, params.get('context_key')),
                    'latest_request': budget['latest']}
        if action == 'memory.layers':
            # The three layers in one read: what she remembers, what got digested,
            # what is still raw. Read-only; it answers "is the pipeline alive".
            import sqlite3 as _s
            from contextlib import closing as _c
            def _one(path, sql, args=()):
                if not path.is_file():
                    return None
                with _c(_s.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                    row = db.execute(sql, args).fetchone()
                    return row[0] if row else None
            memory_db = project_path(self.root, 'data/qq/memory.db')
            qq_db = project_path(self.root, 'data/qq/qq.db')
            exp = project_path(self.root, 'data/social/experience.db')
            layers = {
                'retrieval': {
                    'active': _one(memory_db, "SELECT COUNT(*) FROM memories WHERE status='active'"),
                    'archived': _one(memory_db, "SELECT COUNT(*) FROM memories WHERE status='archived'"),
                    'candidate': _one(memory_db, "SELECT COUNT(*) FROM memories WHERE status='candidate'"),
                },
                'overview': {
                    'digest_days': _one(memory_db, 'SELECT COUNT(*) FROM memory_digest'),
                    'latest_digest_day': _one(memory_db, 'SELECT MAX(day) FROM memory_digest'),
                    'checkpoints': _one(exp, 'SELECT COUNT(*) FROM context_checkpoints'),
                    'catalog_entries': _one(exp, 'SELECT COUNT(*) FROM chat_catalog'),
                },
                'detail': {
                    'messages': _one(qq_db, 'SELECT COUNT(*) FROM messages'),
                    'indexed': _one(qq_db, 'SELECT COUNT(*) FROM messages_fts'),
                },
            }
            return {'layers': layers,
                    'note': '检索层=她记得的事；概述层=每天的摘要与段落目录；细化层=原始消息与索引'}
        if action == 'memory.overview':
            from memory_digest import overview
            from daily_curate import latest
            # 最近一次整理：每日、工具和这个按钮共用同一条执行记录；
            # 升级前旧表行只在没有新记录时作为迁移读取。
            return {**overview(self.root), 'daily_curate': latest(self.root)}
        if action == 'memory.curate.run':
            from memory_curation import submit
            force = params.get('force', False)
            if type(force) is not bool:
                raise ValueError('重做标记需为开关值')
            return submit(self.root, day=params.get('day'), start=params.get('start'),
                          end=params.get('end'), force=force, requested_by='client')
        if action == 'memory.curate.status':
            from memory_curation import status as curation_status
            return curation_status(self.root, params.get('run_id'), limit=10)
        if action.startswith('memory.digest.'):
            from memory_digest import build, list_digests, revise
            if action == 'memory.digest.list':
                limit = params.get('limit', 60)
                if type(limit) is not int or not 1 <= limit <= 365:
                    raise ValueError('小结条数需为 1 到 365 的整数')
                return list_digests(self.root, limit, params.get('scope'))
            if action == 'memory.digest.run':
                force = params.get('force', False)
                if type(force) is not bool:
                    raise ValueError('重写标记需为开关值')
                # scope picks the level: one day, one ISO week, or one calendar month.
                period = params.get('period') or params.get('day') or None
                written = build(self.root, params.get('scope', 'day'), period, force=force)
                return {'digest': written, 'digests': list_digests(self.root, 60)}
            if action == 'memory.digest.revise':
                # The owner says what is wrong with one paragraph; the model rewrites
                # that paragraph alone and the rewrite replaces the stored text.
                period = params.get('period') or params.get('day') or ''
                return revise(self.root, params.get('scope', 'day'), period, params.get('note', ''))
        if action == 'memory.history':
            store = MemoryStore(self.root, 'qq')
            store.get(str(params['id']), scope='owner')
            return store.history(str(params['id']))
        if action in ('memory.disable', 'memory.enable'):
            # Recall only reads status='active'; disabling keeps the record and its
            # history but stops her from bringing it up again.
            import time as _time
            status = 'disabled' if action == 'memory.disable' else 'active'
            store = MemoryStore(self.root, 'qq')
            store.get(str(params['id']), scope='owner')
            from storage import database as _db_factory
            with _db_factory(store.path) as db:
                db.execute('UPDATE memories SET status=?,updated=? WHERE id=?', (status, _time.time(), str(params['id'])))
            return {'id': str(params['id']), 'status': status}
        if action == 'memory.merge':
            # Merging never deletes: the dropped record is marked and keeps its history.
            import time as _time
            keep, drop = str(params['keep']), str(params['drop'])
            if keep == drop:
                raise ValueError('同一条记忆不需要合并')
            store = MemoryStore(self.root, 'qq')
            store.get(keep, scope='owner')
            store.get(drop, scope='owner')
            from storage import database as _db_factory
            with _db_factory(store.path) as db:
                db.execute("UPDATE memories SET status='merged',action=?,updated=? WHERE id=?",
                           ('merged_into:' + keep, _time.time(), drop))
            return {'keep': keep, 'drop': drop, 'drop_status': 'merged'}
        if action == 'memory.search':
            # The page reads the one memory; each row keeps its source scope.
            return MemoryStore(self.root, 'qq').search(params.get('query', ''), scope=None, limit=60,
                                                      include_candidates=True)
        if action in ('knowledge.search', 'knowledge.open'):
            from knowledge import open as knowledge_open
            from knowledge import search as knowledge_search
            if action == 'knowledge.search':
                limit = params.get('limit', 12)
                if type(limit) is not int or not 1 <= limit <= 50:
                    raise ValueError('检索条数需为 1 到 50 的整数')
                return knowledge_search(self.root, params.get('query', ''), limit=limit,
                                        scope=params.get('scope') or None)
            return knowledge_open(self.root, str(params.get('reference') or ''),
                                  level=params.get('level', 'original'),
                                  page=params.get('page', 1), count=params.get('count', 5))
        if action == 'memory.save':
            store = MemoryStore(self.root, 'qq')
            memory_id = params.get('id')
            if memory_id:
                store.get(memory_id, scope='owner')
            result = store.save(params['text'], category=params.get('category', '约定'), scope='owner', source='manual', source_text='主人在 DSH Fish 记忆页保存', memory_id=memory_id)
            return store.get(result['id'], scope='owner')
        if action.startswith('schedules.'):
            control = BotControl(self.root)
            if action == 'schedules.list':
                return control.schedules()
            if action == 'schedules.create':
                return control.execute('schedule_create', {'target': 'owner', **params})
            if action == 'schedules.delete':
                return control.execute('schedule_delete', {'id': params['id']})
            if action == 'schedules.update':
                return control.execute('schedule_update', params)
            if action == 'schedules.run':
                # Manual run of one plan, by the owner, from the task page. It uses the
                # same execution path as the scheduled run; pause only stops the timer.
                return control.run_now(params['id'])
        if action == 'workspace.write':
            # One writer only: this delegates to the agent's own file tool, so the
            # workspace never gets a second, differently-guarded write path.
            base = (project_path(self.root, 'workspace')).resolve()
            base.mkdir(parents=True, exist_ok=True)
            wanted = str(params.get('path', '') or '').strip()
            if not wanted:
                raise ValueError('需要文件路径')
            target = (base / wanted).resolve()
            if base not in target.parents:
                raise PermissionError('只能写 Fish 自己的 workspace 目录')
            from agent_tools import AgentTools
            tools = AgentTools(self.root)
            identity = {'scope': 'desktop', 'session': 'workspace-write', 'allowed_paths': [str(base)],
                        'owner': 1, 'desktop': 1}
            return tools.execute('fish_file_write',
                                  {'path': str(target), 'text': str(params.get('text', '')),
                                   **({'expected_mtime_ns': params['expected_mtime_ns']} if 'expected_mtime_ns' in params else {})},
                                  identity, 'workspace-write-' + uuid.uuid4().hex[:8])
        if action.startswith('models.'):
            import model_connections
            if action == 'models.connections':
                return model_connections.describe(self.root)
            if action == 'models.list':
                return model_connections.describe(self.root)
            if action == 'models.current':
                described = model_connections.describe(self.root)
                from model_gateway import ModelGateway
                try:
                    gateway = ModelGateway(self.root, 'qq')
                    current = {'base': gateway.base, 'model': gateway.model, 'has_key': bool(gateway.key)}
                except Exception as exc:  # noqa: BLE001 - the page shows why it is not configured
                    current = {'error': str(exc)[:200]}
                return {**described, 'gateway': current}
            if action == 'models.save':
                return model_connections.save(self.root, role=str(params.get('role') or 'qq'),
                                              base=str(params.get('base') or ''), model=str(params.get('model') or ''),
                                              key=str(params.get('key') or ''), name=str(params.get('name') or ''))
            if action == 'models.test':
                return model_connections.test(self.root, role=str(params.get('role') or 'qq'),
                                              base=str(params.get('base') or ''), model=str(params.get('model') or ''),
                                              key=str(params.get('key') or ''))
            if action == 'models.discover':
                return model_connections.discover(self.root, role=str(params.get('role') or 'qq'),
                                                  base=str(params.get('base') or ''),
                                                  key=str(params.get('key') or ''))
            if action == 'models.remove':
                return model_connections.remove(self.root, role=str(params.get('role') or 'qq'))
            raise ValueError('未知模型操作')
        if action in ('tools.settings.get','tools.settings.save'):
            from storage import read_json
            from fish_agent.settings.tools import ToolSettings
            from tool_host import builtins, skill_tool
            settings=ToolSettings(self.root)
            value=settings.save(params.get('patch',{})) if action.endswith('.save') else settings.get()
            groups=read_json(project_path(self.root,'channels/fish_qq/config.json'),{}).get('allowlist',{}).get('group_ids',[])
            return {'instance_id':self.instance_id,'settings':value,
                    'tools':[dict(name=item['name'],description=item.get('description',''),
                                  **settings.effective_capability(item['name'])) for item in builtins('qq')+[skill_tool(self.root)]],
                    'groups':[str(g) for g in groups]}
        if action.startswith('plugins.'):
            from plugin_registry import PluginRegistry
            from locations import host_root
            from fish_agent.settings.tools import ToolSettings
            registry = PluginRegistry(host_root(self.root))
            preferences=ToolSettings(self.root)
            if action == 'plugins.list':
                snapshot=registry.snapshot()
                rows=[]
                for item in snapshot['plugins']:
                    aliases={t['name']:'fishp__'+item['id']+'__'+t['name'] for t in item.get('tools',[])}
                    overrides=preferences.get()['tools']
                    selected=[name for name,alias in aliases.items() if overrides.get(alias,name in item.get('allowed_tools',[]) if item.get('allowed_tools') is not None else True)]
                    rows.append({**item,'enabled':preferences.plugin_enabled(item),'shared_enabled':item.get('enabled',False),
                                 'allowed_tools':None if len(selected)==len(aliases) else selected})
                return {**snapshot,'plugins':rows}
            if action == 'plugins.save':
                manifest = params.get('manifest')
                if not isinstance(manifest, dict):
                    raise ValueError('缺少插件配置对象')
                manifest=dict(manifest)
                requested=manifest.get('enabled',False)
                selected=manifest.pop('allowed_tools',None)
                old=next((p for p in registry.entries() if p['id']==manifest.get('id')),None)
                manifest['allowed_tools']=old.get('allowed_tools') if old else None
                # Installation metadata is shared; window enablement is one saved preference.
                manifest['enabled']=bool(old and old.get('enabled'))
                if old and any(manifest.get(k,old.get(k))!=old.get(k) for k in ('kind','command','args','url','tools')):
                    manifest['enabled']=False
                result=registry.save(manifest)
                registered=next(p for p in registry.entries() if p['id']==manifest['id'])
                aliases={'fishp__'+manifest['id']+'__'+tool['name']:selected is None or tool['name'] in selected
                         for tool in registered.get('tools',[])}
                preferences.save({'plugins':{str(manifest['id']):bool(requested)},'tools':aliases})
                return result
            if action == 'plugins.remove':
                return registry.remove(str(params.get('id') or ''))
            if action == 'plugins.enable':
                pid=str(params.get('id') or '');enabled=params.get('enabled',True)
                if type(enabled) is not bool:raise ValueError('扩展开关必须为布尔值')
                item=next((row for row in registry.entries() if row['id']==pid),None)
                if not item:raise ValueError('扩展不存在')
                if enabled and item['kind']!='dsh' and (not item.get('last_test') or item.get('test_ok') is False):
                    raise ValueError('请先测试并发现工具，再启用')
                preferences.save({'plugins':{pid:enabled}})
                return {'ok':True,'instance_id':self.instance_id,'enabled':preferences.plugin_enabled(item)}
            if action == 'plugins.test':
                return registry.discover(str(params.get('id') or ''), str(params.get('role') or 'research'))
            raise ValueError('未知插件操作')
        if action.startswith('skills.'):
            import skills_store
            if action == 'skills.list':
                return {'skills': skills_store.list_skills(self.root),
                        'note': '技能是做法资料；停用只影响是否列出与加载，不改变人格或权限'}
            if action == 'skills.save':
                return skills_store.save(self.root, name=params.get('name', ''),
                                         description=params.get('description', ''),
                                         body=params.get('body', ''),
                                         enabled=bool(params.get('enabled', True)))
            if action == 'skills.import':
                return skills_store.import_path(self.root, params.get('path', ''),
                                                enabled=bool(params.get('enabled', True)))
            if action == 'skills.enable':
                return skills_store.set_enabled(self.root, params.get('name', ''),
                                                bool(params.get('enabled', True)))
            if action == 'skills.read':
                return skills_store.read(self.root, params.get('name', ''))
            if action == 'skills.delete':
                return skills_store.delete(self.root, params.get('name', ''))
            raise ValueError('未知技能操作')
        if action == 'system.storage':
            # Read-only map of where the disk goes, so cleanup is decided on numbers
            # instead of worry. Nothing here deletes anything.
            import os as _os
            def _dir(path):
                total = 0
                for base, _dirs, files in _os.walk(path):
                    for name in files:
                        try:
                            total += _os.path.getsize(_os.path.join(base, name))
                        except OSError:
                            pass
                return round(total / 1024 / 1024, 1)
            root = self.root
            layers = {
                'raw_messages_and_media': {'bytes_mb': _dir(project_path(root, 'data/qq')), 'what': '原始消息、附件、媒体' },
                'memory': {'bytes_mb': _dir(project_path(root, 'data/qq/memory.db')) if False else round((project_path(root, 'data/qq/memory.db')).stat().st_size / 1024 / 1024, 1) if (project_path(root, 'data/qq/memory.db')).exists() else 0.0, 'what': '长期记忆数据库' },
                'social_models_and_logs': {'bytes_mb': _dir(project_path(root, 'data/social')), 'what': '用量、任务、摘要、目录' },
                'embeddings': {'bytes_mb': _dir(project_path(root, 'data/shared')), 'what': '中文向量模型（不可删）' },
                'research_cache': {'bytes_mb': _dir(project_path(root, 'data/research')), 'what': '调研缓存（可重建）' },
                'backups': {'bytes_mb': _dir(root / 'backups'), 'what': '自动备份（保留 14 份）' },
                'archived_for_owner': {'bytes_mb': _dir(root / '需要清理'), 'what': '已归档、等主人处理' },
            }
            return {'layers': layers, 'note': '这里只统计，不删除；清理由 data_retention 按保留期处理可重建的部分'}
        if action in ('workspace.list', 'workspace.read'):
            # Read-only view of Fish's own workspace. Writes stay with the tool path,
            # which already has its own permission story; a second writer would be a
            # second truth about who may touch the disk.
            base = (project_path(self.root, 'workspace')).resolve()
            base.mkdir(parents=True, exist_ok=True)
            wanted = str(params.get('path', '') or '').strip()
            target = (base / wanted).resolve() if wanted else base
            if target != base and base not in target.parents:
                raise PermissionError('只能查看 Fish 自己的 workspace 目录')
            if action == 'workspace.list':
                if target.is_file():
                    raise ValueError('这是一个文件，请用 workspace.read')
                entries = []
                for item in sorted(target.iterdir(), key=lambda p: p.name.casefold())[:400]:
                    entries.append({'name': item.name, 'directory': item.is_dir(),
                                    'bytes': item.stat().st_size if item.is_file() else None})
                return {'path': str(target.relative_to(base)), 'entries': entries}
            if not target.is_file():
                raise ValueError('文件不存在或不是文件')
            limit = min(400, max(20, int(params.get('lines', 200) or 200)))
            text = []
            with target.open(encoding='utf-8', errors='replace') as source:
                for number, line in enumerate(source, 1):
                    if number > limit:
                        break
                    text.append(line.rstrip('\n'))
            return {'path': str(target.relative_to(base)), 'lines': len(text), 'text': text}
        if action == 'chat.new':
            from owner_mode import OwnerMode
            contexts = SocialContext(self.root)
            revision = contexts.active_persona('qq')['id']
            mode = 'chat'
            workspace = ''
            return {'id': self.chat().new(params.get('title', '新对话'), revision=revision, mode=mode, workspace=workspace)}
        if action == 'media.preview':
            from image_views import ImageViews
            return ImageViews(self.root).preview(params['resource_id'], scope=None, mode=params.get('mode', 'original'), box=params.get('box'), frame=int(params.get('frame', 0)))
        if action == 'media.open':
            from doc_store import doc_detail
            record = doc_detail(self.root, params['resource_id'], scope=None)
            path = Path(record['original_path'])
            if not path.is_file():
                raise ValueError('图片原件已不存在')
            os.startfile(str(path))
            return {'ok': True, 'path': str(path)}
        if action.startswith('attachments.'):
            from attachment_store import AttachmentStore
            store = AttachmentStore(self.root, 'qq')
            if action == 'attachments.config':
                return store.limits()
            cid = str(params['id'])
            if not cid.strip():
                raise ValueError('缺少附件所属记录编号')
            if action == 'attachments.begin':
                return store.begin(cid, params['name'], params['size'])
            if action == 'attachments.chunk':
                return store.append(cid, params['upload_id'], params['offset'], params['data'])
            if action == 'attachments.finish':
                return store.finish(cid, params['upload_id'])
            if action == 'attachments.status':
                return store.upload_status(cid, params['upload_id'])
            if action == 'attachments.import':
                raise ValueError('整份base64导入入口已退役，请使用当前客户端分块导入')
            if action == 'attachments.batches':
                source = str(params.get('source') or 'desktop')
                place = source + ':' + cid
                batches = self.runtime().experience.batches.state(place)
                # One batch row keeps the registration/window/read facts of this layer;
                # each file's download, parse and index state is the attachment record's
                # own truth, attached here by resource so it is not copied or guessed.
                for batch in batches:
                    for item in batch['items']:
                        resource = str(item.get('resource') or '').split('#')[0]
                        if not resource:
                            continue
                        try:
                            record = store.get(resource)
                        except Exception:  # noqa: BLE001 - a missing record leaves the ref visible
                            record = None
                        if record:
                            item['attachment'] = {
                                'id': record['id'], 'size': record['size'], 'type': record.get('type'),
                                'processing': record.get('processing'),
                                'indexed': bool((record.get('processing') or {}).get('indexed')),
                                'preview': bool((record.get('processing') or {}).get('preview')),
                            }
                return {'place': place, 'batches': batches, 'limits': store.limits()}
            if action == 'attachments.list':
                return store.list(cid)
            if action == 'attachments.search':
                return store.search(params.get('query', ''), conversation_id=cid)
            if action in ('attachments.detail', 'attachments.open'):
                record = store.detail(params['attachment_id'])
                if action == 'attachments.detail':
                    return record
                path = Path(record['path'])
                if not path.is_file():
                    raise ValueError('附件原件已经不在记录的位置')
                os.startfile(str(path))
                return {'opened': True, 'path': str(path)}
            raise ValueError('未知附件操作')
        if action in ('chat.list', 'chat.snapshot'):
            result = self._chat_view(params.get('id', ''))
            self._selected_conversation = result['conversation_id']
            for row in result['messages']:
                row['id'] = str(row['id'])
            return result
        if action == 'chat.thoughts':
            # Scrollable list of stored thinking records, newest first.
            import sqlite3 as _sqlite3
            from contextlib import closing as _closing
            limit = min(200, max(5, int(params.get('limit', 40) or 40)))
            path = project_path(self.root, 'data/social/experience.db')
            if not path.is_file():
                return {'thoughts': []}
            with _closing(_sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                rows = list(db.execute('SELECT reference, updated, phase, reasoning, error FROM turn_traces '
                                       'WHERE reasoning!=\'\' ORDER BY updated DESC LIMIT ?', (limit,)))
            out = []
            for reference, updated, phase, reasoning, error in rows:
                text = ' '.join(str(reasoning or '').split())
                out.append({'reference': str(reference or ''), 'updated': float(updated or 0),
                            'phase': str(phase or ''), 'chars': len(str(reasoning or '')),
                            'preview': text[:120], 'error': str(error or '')[:200]})
            return {'thoughts': out}
        if action == 'chat.reasoning':
            # Stored display copy of the provider's own reasoning for one turn.
            # Nothing is regenerated here: a turn without a trace answers empty.
            import sqlite3 as _sqlite3
            from contextlib import closing as _closing
            reference = str(params.get('reference', '') or '')
            path = project_path(self.root, 'data/social/experience.db')
            if not reference or not path.is_file():
                return {'reference': reference, 'reasoning': '', 'phase': '', 'error': ''}
            with _closing(_sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute('SELECT phase, reasoning, error FROM turn_traces WHERE reference=? ORDER BY updated DESC LIMIT 1',
                                 (reference,)).fetchone()
            if not row:
                return {'reference': reference, 'reasoning': '', 'phase': '', 'error': ''}
            return {'reference': reference, 'phase': str(row[0] or ''),
                    'reasoning': str(row[1] or '')[:20000], 'error': str(row[2] or '')[:500]}
        if action == 'chat.delete':
            return self.delete_conversations(params)
        if action == 'chat.mode':
            return self.chat().set_mode(params['id'], params['mode'], params.get('workspace', ''))
        if action == 'chat.send':
            text = params.get('text', '')
            attachments = params.get('attachments', [])
            if not isinstance(text, str) or not text.strip() and not attachments:
                raise ValueError('请输入消息或添加附件')
            cid = str(params['id'])
            self.chat().snapshot(cid)
            # A caller may reuse its submission id after a transport retry.
            # Identical wording from two distinct submissions remains two requests.
            job = str(params.get('request_id') or uuid.uuid4().hex)
            if not __import__('re').fullmatch(r'[a-f0-9]{32}', job):
                raise ValueError('请求编号无效')
            now = time.time()
            with database(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT conversation,status FROM jobs WHERE id=?', (job,)).fetchone()
                if old:
                    if old['conversation'] != cid:
                        raise ValueError('请求编号已用于另一会话')
                    return {'job_id': job, 'status': old['status']}
                db.execute('INSERT INTO jobs(id,conversation,status,created,updated) VALUES(?,?,?,?,?)', (job, cid, 'queued', now, now))
            try:
                message_id = self.chat().receive(cid, text, attachments)
                with database(self.path) as db:
                    db.execute('UPDATE jobs SET input_message=? WHERE id=?', (message_id, job))
                # One main execution owns the turn: the same submission reference goes
                # to its ledger, and the client polls that real status, not a copy.
                submission = self.runtime().execution.submit(
                    source='desktop', conversation=cid, message=message_id, text=text,
                    reference='desktop:' + str(message_id), request=job, place='desktop')
                with database(self.path) as db:
                    db.execute('UPDATE jobs SET execution=? WHERE id=?', (submission.get('execution', ''), job))
            except Exception as exc:
                with database(self.path) as db:
                    db.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE id=?", (str(exc)[:500], time.time(), job))
                raise
            return {'job_id': job, 'status': submission.get('status', 'queued')}
        if action == 'chat.job':
            with database(self.path) as db:
                row = db.execute('SELECT * FROM jobs WHERE id=?', (params['job_id'],)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            result = dict(row)
            execution = str(result.get('execution') or '')
            if execution:
                live = self.runtime().execution.job_status(execution)
                result['status'] = live['status']
                result['error'] = live.get('error') or ''
                result['execution'] = live.get('execution') or execution
                result['elapsed_ms'] = live.get('elapsed_ms', 0)
            return result
        if action == 'turn.view':
            view = self.runtime().execution.turn_view(str(params.get('execution') or ''))
            if view is None:
                raise ValueError('执行记录不存在')
            return view
        if action == 'chat.cancel':
            cid = str(params['id'])
            info = self.runtime().execution.cancel(cid)
            return {'ok': True, **info}
        if action.startswith('library.'):
            from doc_store import delete_doc, doc_detail, ingest, list_docs
            from doc_store import search as search_documents
            if action == 'library.status':
                from doc_store import retrieval_status
                return retrieval_status(self.root, scope=None)
            if action == 'library.open':
                row = doc_detail(self.root, params['doc_id'], scope=None)
                path = Path(row['original_path'])
                if not row['original_path'] or not path.is_file():
                    raise ValueError('这份旧资料没有可打开的原件，已存切片仍可查看')
                os.startfile(str(path))
                return {'ok': True, 'path': str(path)}
            if action == 'library.organize':
                from doc_store import organize
                return organize(self.root, params['action'], name=params.get('name', ''), doc_id=params.get('doc_id', ''))
            if action == 'library.upload':
                return ingest(self.root, params.get('name', ''), params.get('text', ''), kind=params.get('kind', 'txt'))
            if action == 'library.list':
                return list_docs(self.root)
            if action == 'library.detail':
                return doc_detail(self.root, params.get('doc_id') or params.get('id'), scope=None)
            if action == 'library.delete':
                return delete_doc(self.root, params.get('doc_id') or params.get('id'))
            if action == 'library.search':
                limit = params.get('limit', 5)
                if type(limit) is not int or not 1 <= limit <= 50:
                    raise ValueError('检索条数需为 1 到 50 的整数')
                return search_documents(self.root, params.get('query', ''), scope=None, limit=limit, with_status=True)
            raise ValueError('未知资料库操作')
        if action.startswith('life.'):
            if action == 'life.status':
                return self.life_status()
            if action == 'life.group.set':
                chat = self._life_chat(params)
                changes = {key: params[key] for key in ('listen', 'reply', 'learn') if key in params}
                if not changes:
                    raise ValueError('没有要保存的群开关')
                if any(type(value) is not bool for value in changes.values()):
                    raise ValueError('群开关必须是开关值')
                from group_policy import GroupPolicy
                GroupPolicy(self.root).configure(chat, changes)
                return self.life_status()
            if action == 'life.dive':
                from group_policy import DIVE_MINUTES_RANGE, GroupPolicy
                minutes = params.get('minutes')
                if type(minutes) is not int or not DIVE_MINUTES_RANGE[0] <= minutes <= DIVE_MINUTES_RANGE[1]:
                    raise ValueError('潜水时长需为 %d 到 %d 分钟的整数' % DIVE_MINUTES_RANGE)
                chat = self._life_chat(params)
                GroupPolicy(self.root).configure(chat, {'dive_until': time.time() + minutes * 60})
                return self.life_status()
            if action == 'life.wake':
                from group_policy import GroupPolicy
                chat = self._life_chat(params)
                GroupPolicy(self.root).configure(chat, {'dive_until': 0})
                return self.life_status()
            if action == 'life.params':
                from group_policy import GroupPolicy
                chat = self._life_chat(params)
                changes = {}
                for key, low, high, name in (('active_cooldown_s', 0, 86400, '冷却秒数'),
                                             ('followup_s', 0, 60, '回访秒数'),
                                             ('max_active_hour', 0, 120, '每小时上限')):
                    if key not in params:
                        continue
                    value = params[key]
                    if type(value) is not int or not low <= value <= high:
                        raise ValueError('%s需为 %d 到 %d 的整数' % (name, low, high))
                    changes[key] = value
                if 'wake_probability' in params:
                    value = params['wake_probability']
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
                        raise ValueError('唤醒概率需为 0 到 1 之间的小数')
                    changes['wake_probability'] = float(value)
                if not changes:
                    raise ValueError('没有要保存的习惯设置')
                GroupPolicy(self.root).configure(chat, changes)
                return self.life_status()
            if action == 'life.events':
                return self.life_events(params)
            if action == 'life.event.done':
                return self._life_event_done(params)
            if action == 'life.rehearse':
                return '彩排请用命令行 python tools/rehearse_group.py --limit 6'
        raise ValueError('未知操作：' + str(action))

    def fault_status(self):
        """Read observed failures and current bindings without creating defaults or tables."""
        from governance import Governance
        try:
            value = Governance.read_faults(self.root)
        except json.JSONDecodeError as exc:
            raise ValueError('Fish 控制或 QQ 通道配置不是有效 JSON，请检查配置文件；状态尚未读取成功') from exc
        except sqlite3.Error as exc:
            raise ValueError('故障记录库无法读取，请检查本地数据库；状态尚未读取成功') from exc
        configuration = []
        path = project_path(self.root, 'channels/fish_qq/config.json')
        try:
            config = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError('QQ 通道配置无法读取，请检查配置文件；状态尚未读取成功') from exc
        owner = str(config.get('allowlist', {}).get('owner_id', ''))
        if not owner:
            configuration.append({'source': 'owner_private', 'code': 'owner_missing',
                                  'reason': '尚未绑定主人 QQ 账号，主人私聊不能发送'})
        context = project_path(self.root, 'data/social/context.db')
        if not context.is_file():
            configuration.append({'source': 'owner_private', 'code': 'persona_missing',
                                  'reason': 'QQ 人格库尚未建立，请先选择人格；状态读取不会创建默认人格'})
        else:
            conn = _query_only(context)
            try:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                selected = conn.execute("SELECT revision FROM agent_profiles WHERE role='qq'").fetchone() if 'agent_profiles' in tables else None
                if selected is None:
                    # Before first activation, report the existing migration source.
                    selected = conn.execute("SELECT p.revision FROM owner_routes r JOIN spaces p ON p.id=r.space WHERE r.owner=?", (owner,)).fetchone()
                if selected is None:
                    selected = conn.execute("SELECT revision FROM default_personas WHERE role='qq'").fetchone()
                revision = selected['revision'] if selected else ''
                if not revision or not conn.execute("SELECT 1 FROM personas WHERE id=? AND role='qq'", (revision,)).fetchone():
                    configuration.append({'source': 'owner_private', 'code': 'persona_missing',
                                          'reason': '当前 Fish 活动人格不存在，请重新选择现存预设；没有切换默认人格'})
            except sqlite3.Error:
                configuration.append({'source': 'owner_private', 'code': 'context_read_error',
                                      'reason': 'QQ 人格库无法读取或结构不完整，请检查本地数据；没有创建或替换人格'})
            finally:
                conn.close()
        return {**value, 'configuration': configuration, 'now': time.time()}

    # ---------------------------------------------------------------- life page
    def _life_allowlist(self):
        from storage import read_json
        config = read_json(project_path(self.root, 'channels/fish_qq/config.json'), {})
        return [str(value) for value in config.get('allowlist', {}).get('group_ids', [])]

    def _life_chat(self, params):
        allowed = self._life_allowlist()
        raw = str(params.get('chat') or '').strip()
        if not raw:
            if len(allowed) == 1:
                return 'group:' + allowed[0]
            raise ValueError('请选择要设置的群')
        number = raw.split(':', 1)[1] if raw.startswith('group:') else raw
        if number not in allowed:
            raise ValueError('只能操作白名单里的群')
        return 'group:' + number

    def _life_group(self, chat):
        from group_policy import DEFAULTS
        stored = {}
        path = project_path(self.root, 'data/social/group-policy.db')
        if path.is_file() and chat:
            conn = _query_only(path)
            try:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='groups'").fetchone():
                    row = conn.execute('SELECT config FROM groups WHERE chat=?', (chat,)).fetchone()
                    if row:
                        stored = json.loads(row['config'] or '{}')
            finally:
                conn.close()
        config = {**DEFAULTS, **stored}
        allowed = self._life_allowlist()
        return {'chat': chat, 'engine': str(config['engine']), 'listen': bool(config['listen']),
                'reply': bool(config['reply']), 'learn': bool(config['learn']), 'read': bool(config['read']),
                'dive_until': float(config['dive_until'] or 0),
                'wake_probability': float(config['wake_probability']),
                'active_cooldown_s': int(config['active_cooldown_s']),
                'followup_s': int(config['followup_s']), 'reply_limits': bool(config['reply_limits']),
                'max_active_hour': int(config['max_active_hour']),
                'groups': ['group:' + value for value in allowed]}

    def life_status(self):
        allowed = self._life_allowlist()
        chat = 'group:' + allowed[0] if allowed else ''
        return {'now': time.time(), 'group': self._life_group(chat),
                'reminder': _life_reminder(self.root), 'memory': _life_memory(self.root), 'estimate': {}}

    def life_events(self, params):
        from memory_store import MemoryStore
        limit = params.get('limit', 20)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('事件条数需为 1 到 100 的整数')
        path = project_path(self.root, 'data/qq/memory.db')
        if not path.is_file():
            return []
        store = MemoryStore(self.root, 'qq')
        conn = _query_only(path)
        try:
            scopes, _owner = store._visible_scopes(conn, 'owner')
            holes = ','.join('?' * len(scopes))
            rows = conn.execute(
                "SELECT id,text,action,due,created FROM memories WHERE scope IN (%s) "
                "AND kind='event' AND status='active' "
                "ORDER BY CASE WHEN action=? THEN 0 ELSE 1 END, "
                "CASE WHEN due=0 THEN 1 ELSE 0 END, due ASC, created DESC LIMIT ?" % holes,
                (*scopes, 'open', limit)).fetchall()
        finally:
            conn.close()
        return [{'id': str(row['id']), 'text': ' '.join(str(row['text'] or '').split())[:80],
                 'action': str(row['action'] or 'open'), 'state': LIFE_EVENT_STATE.get(str(row['action'] or 'open'), ''),
                 'due': float(row['due'] or 0), 'created': float(row['created'] or 0)} for row in rows]

    def _life_event_done(self, params):
        from memory_store import MemoryStore
        memory_id = str(params.get('id') or '').strip()
        if not memory_id:
            raise ValueError('请指定要处理的事件')
        cancel = params.get('cancel', False)
        if type(cancel) is not bool:
            raise ValueError('取消标记需为开关值')
        store = MemoryStore(self.root, 'qq')
        result = store.cancel(memory_id, scope='owner') if cancel else store.complete(memory_id, scope='owner')
        if not result.get('ok'):
            raise ValueError(str(result.get('reason') or '事件没有更新成功'))
        return self.life_events({'limit': params.get('limit', 20)})

    def close(self):
        if self._delegation_instance:
            self._delegation_instance.close()
        if self._chat_instance:
            self._chat_instance.cancel()
