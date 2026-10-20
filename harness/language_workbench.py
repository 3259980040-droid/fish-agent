"""Read/edit the two real language libraries; never grants send or persona changes.

Sources (existing databases only, no parallel tables):

- ``group_language``: ``data/social/group-language.db`` (``entries``/``audits``).
  Read/write. The group runtime only selects ``active``/``candidate`` rows and
  never auto-reactivates a ``disabled`` row, so a stopped entry stays stopped.
  ``candidate`` is what that prompt shows the model; this page reports it as
  "待采用（候选）" rather than inventing an enable it cannot perform.
- ``tulpa``: ``data/integrations/tulpa/app/data/mcp-language.sqlite3``, owned by
  the original Tulpa MCP store. Read only, opened with ``mode=ro``. Its
  ``enabled``/``manual`` semantics belong to that process, so edits stay there.
- ``files``: owner memories under a group chat scope (``data/qq/memory.db``)
  and Fish's own QQ message log (``data/qq/qq.db``). Read only.

Usage counts are data, not inference: extraction is the ``candidate``/``active``
audit rows (how often the summarizer wrote the entry), retrieval is how many of
those same IDs later reached a reply prompt, and adoption is a reply that sent
one of those IDs to the group. The group runtime does not record which IDs it
actually used, so adoption is ``None`` (unknown) instead of zero. Tulpa
retrieval/adoption happens inside MCP results the host does not log, so it is
always unknown. None of these numbers proves a style was "learned".
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from storage import database

KINDS = {'expression': '表达方式', 'term': '群内术语', 'alias': '别称/称谓'}
SOURCE_TYPES = {
    'group_language': 'Fish 群语言库',
    'tulpa': '原版 Tulpa 语言学习库（只读）',
    'files': '主人记忆 / 群聊记录（只读）',
}
STATUSES = {'candidate': '待采用（候选）', 'active': '已启用', 'disabled': '已停用'}
USAGE_FIELDS = {
    'extracted': '提取：资料库写入次数（候选 + 主人纠正）',
    'retrieved': '检索：提取后进入 Fish 群回复提示的次数（精确）',
    'adopted': '实际采用：真实回复中实际使用了该条资料；运行时未记录，显示未知',
    'priority': '纠正优先：主人纠正的内容在提示中排在候选前面',
}
UNKNOWN = '未知'
UNKNOWN_NOTE = ('Fish 群语言库没有记录哪条资料被实际用于哪次回复。任务窗口内的真实群回复中，'
                '出现条目内容（或来源原话）且出现其主词的最近 10 条只算“可能采用”；'
                'Tulpa 的选择发生在 MCP 结果内，宿主没有记录，一律未知。数字不是“已学会”的证据。')
LIMITS = {'query': 200, 'trigger': 100, 'content': 500, 'note': 1000}
ID_PATTERN = re.compile(r'[a-f0-9]{8,64}')
OWNER_CORRECTION = re.compile(r'应该|不要|别乱|不对|叫.{1,40}|改成|记住|记下来|其实是|指的是|意思是')
_ENTRY = '''SELECT e.id,e.chat,e.window,e.kind,e.trigger,e.content,e.status,e.source,e.evidence,
            (SELECT a.created FROM audits a WHERE a.entry=e.id AND a.event!='owner-correction'
             ORDER BY a.id LIMIT 1) AS extracted_at,
            (SELECT a.created FROM audits a WHERE a.entry=e.id AND a.event!='owner-correction'
             ORDER BY a.id DESC LIMIT 1) AS changed_at
            FROM entries e'''


def _clean(value, limit, label, *, required=True):
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ValueError(f'{label}必须是文本')
    value = value.strip()
    if required and not value:
        raise ValueError(f'请填写{label}')
    if len(value) > limit:
        raise ValueError(f'{label}不能超过 {limit} 字')
    return value


def _clock(value):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(value))


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _trim(value, size=280):
    value = str(value or '')
    return value if len(value) <= size else value[:size] + '…'


class LanguageWorkbench:
    def __init__(self, root):
        self.root = Path(root)
        self.group_path = self.root / 'data/social/group-language.db'
        self.tulpa_path = self.root / 'data/integrations/tulpa/app/data/mcp-language.sqlite3'
        self.memory_path = self.root / 'data/qq/memory.db'
        self.qq_path = self.root / 'data/qq/qq.db'
        self.workbench_path = self.root / 'data/social/workbench.db'

    # ---------- read-only access ----------

    def _readonly(self, path):
        if not path.is_file():
            return None
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _messages(self, conn, channel_ids):
        """Original QQ messages by channel ID, newest row first (resends duplicate rows)."""
        found = {key: [] for key in channel_ids}
        ids = [int(key) for key in channel_ids if str(key).isdigit()]
        if conn is None or not ids:
            return found
        rows = conn.execute(
            'SELECT id,ts,chat_id,user_id,text,sender_id,direction,channel_message_id,status,error '
            f'FROM messages WHERE channel_message_id IN ({",".join("?" * len(ids))}) ORDER BY ts DESC,id DESC',
            ids).fetchall()
        for row in rows:
            if row['channel_message_id'] in found:
                found[row['channel_message_id']].append(dict(row))
        return found

    def _windows(self, conn):
        return {r['window']: r['chat'] for r in conn.execute(
            "SELECT window,MAX(chat) chat FROM entries WHERE window<>'' GROUP BY window")}

    def _settings(self, conn):
        if conn is None:
            return {}
        try:
            return {r['scope']: dict(enabled=bool(r['enabled']), dropped=r['dropped'] or 0, failure=r['failure'] or '')
                    for r in conn.execute('SELECT scope,enabled,failure,dropped FROM settings')}
        except sqlite3.Error:
            return {}

    def _group_rows(self, conn, qq):
        entries = conn.execute(_ENTRY).fetchall()
        originals = self._messages(qq, {r['source'] for r in entries})
        result = []
        for row in entries:
            item = dict(row)
            found = originals.get(item['source']) or []
            source = found[0] if found else None
            notes = []
            if source and source['direction'] != 'inbound':
                notes.append('来源记录不是收到的群友消息，不能当作群友原话')
                source = None
            elif source and source['error']:
                notes.append('来源消息当时发送失败：' + _trim(source['error'], 80))
            if not source:
                if not notes:
                    notes.append('原 QQ 消息库中找不到这条来源（可能已按保留期清理），只保留来源编号')
            elif len(found) > 1:
                notes.append(f'同一来源编号有 {len(found)} 条记录，显示最新一条')
            owner_fixed = item['evidence'] == 'owner-feedback'
            notes.append('主人纠正过的内容，提示中优先于候选' if owner_fixed
                         else '群聊摘要自动提取的含义猜测，可能理解有误')
            result.append({
                'id': item['id'], 'source_type': 'group_language', 'source_type_label': SOURCE_TYPES['group_language'],
                'kind': item['kind'], 'kind_label': KINDS.get(item['kind'], item['kind']),
                'trigger': item['trigger'], 'content': item['content'],
                'status': item['status'], 'status_label': STATUSES.get(item['status'], item['status']),
                'usage_state': '已启用：主动回复会读取' if item['status'] == 'active'
                else '已停用：原文保留；自动学习不会重新启用它' if item['status'] == 'disabled'
                else '待采用：作为候选参考，不是已确认用法',
                'evidence': item['evidence'],
                'evidence_label': '主人纠正' if owner_fixed else '群聊示例（自动提取）',
                'editable': True, 'removable': True,
                'source': {'kind': 'qq-group' if source else 'unresolved', 'message_id': item['source'],
                           'chat': item['chat'], 'sender': source['sender_id'] if source else '',
                           'at': source['ts'] if source else '', 'text': source['text'] if source else '',
                           'note': '；'.join(notes)},
                'extracted_at': item['extracted_at'], 'changed_at': item['changed_at'],
                'window': item['window'],
            })
        return result

    def _tulpa_rows(self, conn, local_chats, qq):
        records = [dict(r) for r in conn.execute(
            '''SELECT e.id,e.scope,e.situation,e.style,e.count,e.enabled,e.independence,g.form_type,
                      g.surface_form,g.batch_id,g.quote_sha256,e.independence independence
               FROM expressions e LEFT JOIN expression_grounding g ON g.scope=e.scope AND g.item_id=e.id
               UNION ALL
               SELECT j.id,j.scope,'',j.term,j.count,j.enabled,j.independence,'','',NULL,NULL,j.independence
               FROM jargon j''')]
        batches = {r['id']: json.loads(r['evidence'] or '[]') for r in
                   conn.execute('SELECT id,evidence FROM batches')}
        hit_counts = {}
        for table in ('expression_hits', 'jargon_hits'):
            for r in conn.execute(f'SELECT scope,item_id,COUNT(*) n FROM {table} GROUP BY scope,item_id'):
                hit_counts[(r['scope'], r['item_id'])] = r['n']
        result = []
        for r in records:
            scope = r['scope']
            group = f'group:{scope[3:]}' if scope.startswith('qq:') else scope
            messages = batches.get(r['batch_id']) if r['batch_id'] else None
            quote = self._quote(messages, r['batch_id'], scope, qq)
            hits = hit_counts.get((scope, r['id']), r['count'])
            is_expression = bool(r['batch_id'])
            result.append({
                'id': r['id'], 'source_type': 'tulpa', 'source_type_label': SOURCE_TYPES['tulpa'],
                'kind': 'expression' if is_expression else 'term',
                'kind_label': KINDS['expression'] if is_expression else KINDS['term'],
                'trigger': r['situation'] or (r['form_type'] or ''),
                'content': r['style'] if is_expression else r['style'],
                'status': 'active' if r['enabled'] else 'disabled',
                'status_label': STATUSES['active'] if r['enabled'] else STATUSES['disabled'],
                'usage_state': 'Tulpa 中启用' if r['enabled'] else 'Tulpa 中已停用',
                'evidence': 'tulpa-learning',
                'evidence_label': 'Tulpa 自动学习（' + (r['independence'] or 'degraded') + '）',
                'editable': False, 'removable': False,
                'source': quote,
                'scope': scope, 'group': group, 'in_group': group in local_chats,
                'usage': {'extracted': hits, 'extracted_label': f'提取 {hits} 次（原文命中）',
                          'retrieved': None, 'retrieved_label': UNKNOWN,
                          'adopted': None, 'adopted_label': UNKNOWN,
                          'possible_adoption': None, 'priority': '否：自动提取，主人纠正走 manual 释义'},
            })
        return result

    def _quote(self, messages, batch_id, scope, qq):
        chat = scope[3:] if scope.startswith('qq:') else scope
        chat = chat if ':' in chat else 'group:' + chat
        base = {'kind': 'tulpa-batch', 'message_id': '', 'chat': chat,
                'sender': '', 'at': '', 'text': ''}
        if messages is None:
            return dict(base, kind='purged', note='Tulpa 学习成功后原批次已被清理（或已到期），原始样例和发言人都不可读')
        hits = []
        for message in messages:
            for m in message.get('messages', []):
                text = (m.get('text') or '').strip()
                if not text:
                    continue
                row = None
                if qq is not None:
                    row = qq.execute("SELECT ts,chat_id,user_id,text FROM messages WHERE TRIM(text)=? "
                                     "ORDER BY ts DESC,id DESC LIMIT 1", (text,)).fetchone()
                if row:
                    hits.append(dict(row, source='QQ 原话'))
                else:
                    hits.append(dict(ts='', chat_id=message.get('chat') or '', user_id=m.get('sender') or '',
                                     text=text, source='Tulpa 批次（未匹配到 QQ 原话）'))
        if not hits:
            return dict(base, note='批次仍在，但没有可读的原话')
        chosen = hits[0]
        return {'kind': 'qq-group', 'message_id': '', 'chat': chosen['chat_id'], 'sender': chosen['user_id'],
                'at': chosen['ts'], 'text': _trim(chosen['text']),
                'note': f"{chosen['source']}；Tulpa 批次 {batch_id[:8]}"}

    def _memory_rows(self, conn):
        if conn is None:
            return []
        try:
            rows = conn.execute("SELECT id,text,category,subject,source,status,created,updated,scope "
                                "FROM memories WHERE scope LIKE 'chat:%' LIMIT 500").fetchall()
        except sqlite3.Error:
            return []
        return [{
            'id': r['id'], 'source_type': 'files', 'source_type_label': SOURCE_TYPES['files'],
            'kind': 'owner-note', 'kind_label': '主人记忆',
            'trigger': r['subject'] or r['category'], 'content': r['text'],
            'status': 'active' if r['status'] == 'active' else 'candidate',
            'status_label': STATUSES['active'] if r['status'] == 'active' else STATUSES['candidate'],
            'usage_state': '主人记忆（' + (r['scope'] or '') + '），只读',
            'evidence': 'owner-memory', 'evidence_label': '主人记忆',
            'editable': False, 'removable': False,
            'source': {'kind': 'owner-memory', 'message_id': r['id'], 'chat': (r['scope'] or '')[5:],
                       'sender': '', 'at': r['created'], 'text': _trim(r['source']),
                       'note': '在记忆页有来源记录时以其为准'},
            'extracted_at': r['created'], 'changed_at': r['updated'],
            'usage': {'extracted': None, 'retrieved': None, 'adopted': None,
                      'extracted_label': '见主人记忆库', 'retrieved_label': UNKNOWN,
                      'adopted_label': UNKNOWN, 'priority': UNKNOWN},
        } for r in rows]

    def _load(self):
        self.group_path.parent.mkdir(parents=True, exist_ok=True)
        qq, tulpa, mem = self._readonly(self.qq_path), None, self._readonly(self.memory_path)
        try:
            with database(self.group_path) as conn:
                windows = self._windows(conn)
                group = self._group_rows(conn, qq)
                audits = [dict(r) for r in conn.execute(
                    "SELECT entry,event,created FROM audits WHERE event IN "
                    "('candidate','active') ORDER BY id")]
                corrections = {r['entry'] for r in conn.execute(
                    "SELECT DISTINCT entry FROM audits WHERE event='owner-correction'")}
            if self.tulpa_path.is_file():
                tulpa = self._readonly(self.tulpa_path)
            settings = self._settings(tulpa)
            tulpa_rows = self._tulpa_rows(tulpa, set(windows.values()), qq) if tulpa else []
            files = self._memory_rows(mem)
        finally:
            for conn in (qq, tulpa, mem):
                if conn is not None:
                    conn.close()
        return dict(group=group, tulpa=tulpa_rows, files=files, windows=windows,
                    audits=audits, corrections=corrections, settings=settings)

    # ---------- usage ----------

    def _usage(self, data):
        """Exact prompt reads per entry; adoption stays unknown when unrecorded."""
        group = data['group']
        writers = {}
        for audit in data['audits']:
            writers.setdefault(audit['entry'], []).append(audit['created'])
        replies = self._reply_texts({r['source']['chat'] for r in group})
        for r in group:
            seen = sorted(set(writers.get(r['id'], [])))
            runs = [(t, t + 86400) for t in seen]
            if runs and runs[-1][1] > time.time():
                runs[-1] = (runs[-1][0], None)  # still visible: next batch reads it
            possible = 0
            quote = r['content'].strip('“”"「」 \n')
            for start, end in runs:
                start, end = _clock(start), _now() if end is None else _clock(end)
                recent = [text for at, text in replies.get(r['source']['chat'], []) if start <= at < end][-10:]
                possible += sum(1 for text in recent if quote and quote in text
                                and (r['trigger'] in text or len(r['trigger']) < 2))
            total_ext, total_ret = len(runs), len(runs)
            fixed = 1 if r['id'] in data['corrections'] else 0
            r['usage'] = {
                'extracted': total_ext + fixed,
                'extracted_label': f'提取 {total_ext} 次' + (f' · 主人纠正 {fixed} 次' if fixed else ''),
                'retrieved': total_ret,
                'retrieved_label': f'检索 {total_ret} 次（写入后的群回复从这里读到）',
                'adopted': None, 'adopted_label': UNKNOWN,
                'possible_adoption': possible,
                'priority': '是：主人纠正优先' if r['evidence'] == 'owner-feedback' else '否：候选排在纠正之后',
            }
        return {'adopted': None, 'adopted_label': UNKNOWN, 'recorded': False, 'retrieved_exact': True,
                'unknown_reason': UNKNOWN_NOTE}

    def _reply_texts(self, chats):
        """Real group replies per chat from Fish's QQ log, ordered by time."""
        if not chats:
            return {}
        qq = self._readonly(self.qq_path)
        if qq is None:
            return {}
        try:
            result = {}
            for r in qq.execute(
                    f'''SELECT chat_id,ts,text FROM messages WHERE direction='outbound' AND chat_id IN
                    ({",".join("?" * len(chats))}) AND COALESCE(cancelled,0)=0 AND COALESCE(error,'')=''
                    ORDER BY ts,id''', list(chats)):
                result.setdefault(r['chat_id'], []).append((str(r['ts'] or '')[:19], r['text'] or ''))
            return result
        except sqlite3.Error:
            return {}
        finally:
            qq.close()

    # ---------- public API ----------

    def status(self):
        """Source paths, availability, record counts and what is explicitly unknown."""
        data = self._load()
        counts = {key: {'candidate': 0, 'active': 0, 'disabled': 0} for key in SOURCE_TYPES}
        for rows in (data['group'], data['tulpa'], data['files']):
            for r in rows:
                counts[r['source_type']][r['status']] += 1
        usage = self._usage(data)
        return {
            'sources': [
                {'type': 'group_language', 'label': SOURCE_TYPES['group_language'], 'path': str(self.group_path),
                 'exists': self.group_path.is_file(), 'writable': True, 'counts': counts['group_language'],
                 'note': 'Fish 群聊实际读取的资料库；可编辑、启用或停用，停用后自动学习不会重新启用。'},
                {'type': 'tulpa', 'label': SOURCE_TYPES['tulpa'], 'path': str(self.tulpa_path),
                 'exists': self.tulpa_path.is_file(), 'writable': False, 'counts': counts['tulpa'],
                 'note': '原版 Tulpa MCP 自己维护的库；这里只读。要改请在 Tulpa 内操作，避免两方互相覆盖。'},
                {'type': 'files', 'label': SOURCE_TYPES['files'],
                 'path': str(self.qq_path) + ' ; ' + str(self.memory_path),
                 'exists': self.qq_path.is_file() or self.memory_path.is_file(), 'writable': False,
                 'counts': counts['files'],
                 'note': '原始来源：群友原话与主人记忆，用于核对来源；不在此修改。'},
            ],
            'kinds': KINDS, 'statuses': STATUSES, 'usage_fields': USAGE_FIELDS, 'usage': usage,
            'unknown_label': UNKNOWN,
            'closed': {'group_chat': '本页不发送 QQ 消息，不改变群聊开关',
                       'persona': '不修改主人人格与人物卡'},
            'tulpa_libraries': len({r['scope'] for r in data['tulpa']}),
        }

    def list(self, query='', kind='', status=''):
        """All sources' records with source, status and usage; filtered by substring."""
        query = _clean(query, LIMITS['query'], '搜索词', required=False)
        if kind not in ('', *KINDS, 'owner-note'):
            raise ValueError('未知的类别：' + str(kind))
        if status not in ('', *STATUSES):
            raise ValueError('未知的状态：' + str(status))
        data = self._load()
        usage = self._usage(data)
        needle = query.casefold()
        items = []
        for r in data['group'] + data['tulpa'] + data['files']:
            if kind and r['kind'] != kind or status and r['status'] != status:
                continue
            if needle:
                hay = ' '.join(str(r.get(k, '')) for k in ('trigger', 'content', 'id', 'kind_label', 'evidence_label'))
                hay += ' ' + ' '.join(str(r['source'].get(k, '')) for k in ('text', 'message_id', 'chat', 'sender'))
                if needle not in hay.casefold():
                    continue
            items.append(r)
        order = {'group_language': 0, 'tulpa': 1, 'files': 2}
        items.sort(key=lambda r: (order[r['source_type']], -(r.get('changed_at') or r.get('extracted_at') or 0)))
        return {'items': items, 'total': len(items), 'query': query, 'kind': kind, 'status': status,
                'usage': usage, 'unknown_label': UNKNOWN}

    def save(self, id, **changes):
        """Owner correction of a Fish group entry. ``id`` may be an 8+ hex prefix.

        Fields: ``trigger``, ``content``, ``kind``, ``status`` plus optional
        ``note``/``source``/``owner`` for the audit trail. Source message and
        history are kept. Editing text marks the row ``owner-feedback`` (as the
        group ``/认可用语`` command does) so the prompt ranks it first; a
        disabled row must be re-enabled explicitly in the same save.
        """
        allowed = {'trigger', 'content', 'kind', 'status', 'source', 'message_id', 'owner', 'note', 'reason'}
        if set(changes) - allowed:
            raise ValueError('不支持修改：' + '、'.join(sorted(set(changes) - allowed)))
        prefix = _clean(id, 64, '用语编号')
        if not ID_PATTERN.fullmatch(prefix):
            raise ValueError('请使用至少八位的用语编号')
        values = {}
        if 'trigger' in changes:
            values['trigger'] = _clean(changes['trigger'], LIMITS['trigger'], '适用词或情境')
        if 'content' in changes:
            values['content'] = _clean(changes['content'], LIMITS['content'], '含义或表达')
        if 'kind' in changes:
            if changes['kind'] not in KINDS:
                raise ValueError('类别只能是 expression、term 或 alias')
            values['kind'] = changes['kind']
        if 'status' in changes:
            if changes['status'] not in STATUSES:
                raise ValueError('状态只能是 candidate、active 或 disabled')
            values['status'] = changes['status']
        if not values:
            raise ValueError('没有要保存的修改')
        origin = _clean(changes.get('source') or changes.get('message_id') or 'DSH Fish 语言资料库', 200, '来源')
        note = _clean(changes.get('note') or changes.get('reason') or '', LIMITS['note'], '纠正说明', required=False)
        owner = _clean(changes.get('owner') or '', 80, '纠正人', required=False)
        with database(self.group_path) as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM entries WHERE id LIKE ?', (prefix + '%',)).fetchall()
            if len(rows) != 1:
                raise ValueError('编号不存在或不唯一，请使用更长编号')
            row = dict(rows[0])
            edited = {k: v for k, v in values.items() if k in ('trigger', 'content', 'kind') and row[k] != v}
            if edited and row['status'] == 'disabled':
                raise ValueError('该用语已停用，自动学习不会重新启用；请同时把状态改为启用或待采用再保存')
            changed = {**edited, **{k: v for k, v in values.items() if k == 'status' and row[k] != v}}
            if not changed:
                return dict(row, applied=False, message='内容没有变化')
            now = time.time()
            db.execute('UPDATE entries SET ' + ','.join(f'{k}=?' for k in changed) + ',updated=? WHERE id=?',
                       (*changed.values(), now, row['id']))
            if edited:
                # The same marker /learn sets for owner corrections: the
                # prompt labels and ranks this row as corrected.
                db.execute("UPDATE entries SET evidence='owner-feedback' WHERE id=?", (row['id'],))
            event = 'owner-correction' if edited or row['evidence'] != 'owner-feedback' else changed['status']
            detail = json.dumps({'fields': sorted(changed), 'note': note, 'owner': owner,
                                 'before': {k: row[k] for k in changed},
                                 'evidence': 'owner-feedback' if edited else row['evidence']}, ensure_ascii=False)
            db.execute('INSERT INTO audits(entry,event,source,created) VALUES(?,?,?,?)',
                       (row['id'], event, f'{origin}｜{detail}'[:2000], now))
            current = dict(db.execute('SELECT * FROM entries WHERE id=?', (row['id'],)).fetchone())
        return dict(current, applied=True,
                    message='已保存纠正，并标记为主人纠正，后续相关回复优先使用' if edited else '已保存状态')

    def manage(self, id, status):
        """``active`` enables, ``disabled`` stops (auto-learning keeps it stopped), ``candidate`` reopens as candidate."""
        if status not in STATUSES:
            raise ValueError('状态只能是 candidate、active 或 disabled')
        saved = self.save(id, status=status, source='DSH Fish 语言资料库 · 启停')
        return dict(saved, status=status, status_label=STATUSES[status])

    def delete(self, id, *, owner='', note=''):
        """Remove an entry but keep its deleted tombstone in ``audits`` (source never deleted)."""
        prefix = _clean(id, 64, '用语编号')
        if not ID_PATTERN.fullmatch(prefix):
            raise ValueError('请使用至少八位的用语编号')
        note = _clean(note, LIMITS['note'], '说明', required=False)
        with database(self.group_path) as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM entries WHERE id LIKE ?', (prefix + '%',)).fetchall()
            if len(rows) != 1:
                raise ValueError('编号不存在或不唯一，请使用更长编号')
            row = dict(rows[0])
            db.execute('DELETE FROM entries WHERE id=?', (row['id'],))
            detail = json.dumps({'deleted': {k: row[k] for k in ('kind', 'trigger', 'content', 'status', 'source')},
                                 'note': note, 'owner': _clean(owner, 80, '操作人', required=False)}, ensure_ascii=False)
            db.execute('INSERT INTO audits(entry,event,source,created) VALUES(?,?,?,?)',
                       (row['id'], 'deleted', ('DSH Fish 语言资料库删除｜' + detail)[:2000], time.time()))
        return {'id': row['id'], 'deleted': True,
                'message': '已删除，来源消息与删除记录保留。之后群聊摘要仍可能重新提取相同用语作为候选。'}
