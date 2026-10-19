"""Window-owned dialogue, independent of reply jobs and runtime lifetimes."""
import json
import time
from contextlib import closing
from datetime import datetime

from storage import database


def user_message(payload):
    sender = payload.get('sender') or {}
    header = {'message_id': str(payload.get('message_id', '')),
              'sender_id': str(sender.get('id', '')),
              'name': str(sender.get('display_name') or sender.get('name') or ''),
              'reply_to': str(payload.get('reply_to') or '')}
    text = ''.join(str(s.get('text', '')) for s in payload.get('segments', []) if s.get('type') == 'text')
    media = [s.get('type') for s in payload.get('segments', []) if s.get('type') in ('image', 'audio', 'video', 'file')]
    if media:
        text += '\n[历史附件类型：' + ', '.join(media) + '；此处未重新加载附件内容]'
    result={'role': 'user', 'content': [{'type': 'text', 'text': '【群消息 ' + json.dumps(header, ensure_ascii=False) + '】\n' + text}]}
    images=[dict(s) for s in payload.get('segments',[]) if s.get('type')=='image']
    if images:result['fish_history_media']=images[:2]
    return result


class GroupHistory:
    def __init__(self, root, jobs, store, chat, window, revision):
        self.root, self.jobs, self.store = root, jobs, store
        self.chat, self.window, self.revision = chat, window, revision
        self.key = json.dumps([chat, window, 'history-v2' if window else revision], separators=(',', ':'))
        self.path = jobs.path
        with database(self.path) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS dialogue_messages(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, source TEXT,
                role TEXT, message TEXT, created REAL, UNIQUE(scope,source));
            CREATE INDEX IF NOT EXISTS dialogue_scope ON dialogue_messages(scope,seq);
            CREATE TABLE IF NOT EXISTS dialogue_imports(scope TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS dialogue_summaries(
                scope TEXT PRIMARY KEY, through_seq INTEGER, text TEXT, cleared REAL);
            ''')
            # A persona reload keeps this window's ordinary dialogue. A new
            # window still has a distinct key. Migrate existing rows once.
            if window and not db.execute('SELECT 1 FROM dialogue_messages WHERE scope=? LIMIT 1',(self.key,)).fetchone():
                for old in db.execute('SELECT DISTINCT scope FROM dialogue_messages').fetchall():
                    parts=json.loads(old['scope'])
                    if parts[:2] != [chat,window] or old['scope']==self.key: continue
                    db.execute('''INSERT OR IGNORE INTO dialogue_messages(scope,source,role,message,created)
                        SELECT ?,source,role,message,created FROM dialogue_messages WHERE scope=? ORDER BY seq''',(self.key,old['scope']))
        self.import_existing()

    def append(self, source, message, created=None):
        with database(self.path) as db:
            db.execute('INSERT OR IGNORE INTO dialogue_messages(scope,source,role,message,created) VALUES(?,?,?,?,?)',
                       (self.key, source, message['role'], json.dumps(message, ensure_ascii=False), time.time() if created is None else created))
            return db.execute('SELECT seq FROM dialogue_messages WHERE scope=? AND source=?', (self.key, source)).fetchone()['seq']

    def observe(self, payload, created=None):
        return self.append('in:' + str(payload['message_id']), user_message(payload), created)

    def delivered(self, job, text):
        message = {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}
        self.append('out:' + job['id'], message)

    def import_existing(self):
        """Import the old window once; never mix other windows or private chats."""
        with database(self.path) as db:
            if db.execute('SELECT 1 FROM dialogue_imports WHERE scope=?', (self.key,)).fetchone():
                return
            old_jobs = {r['id']: dict(r) for r in db.execute('SELECT * FROM jobs WHERE chat=? AND window=? AND revision=?',
                                                          (self.chat, self.window, self.revision))}
        conversation = 'group-test:' + self.window if self.window else self.chat + ':persona:' + self.revision
        with closing(self.store.connect()) as db:
            rows = db.execute('''SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id
                WHERE c.chat_id=? OR (m.chat_id=? AND m.direction='outbound' AND m.status='sent') ORDER BY m.id''',
                              (conversation, self.chat)).fetchall()
        for row in rows:
            try:
                created = datetime.fromisoformat(row['ts']).timestamp()
            except (TypeError, ValueError):
                created = 0
            if row['direction'] == 'inbound':
                self.observe({'message_id': row['channel_message_id'], 'sender': {'id': row['sender_id']},
                              'reply_to': row['reply_to'], 'segments': json.loads(row['segments'] or '[]')}, created)
            else:
                job = old_jobs.get(str(row['runtime_turn_id'] or '').removeprefix('group-job-'))
                if job and job['state'] == 'sent':
                    self.append('out:' + job['id'], {'role': 'assistant', 'content': [{'type': 'text', 'text': job['response']}]}, created)
        with database(self.path) as db:
            db.execute('INSERT OR IGNORE INTO dialogue_imports VALUES(?)', (self.key,))

    def pending_history(self, job, through=0, *, cleared=0, cancelled=None):
        current = {'in:' + str(p['payload']['message_id']).removeprefix('active:') for p in job['payload']}
        with database(self.path) as db:
            positions = [r['seq'] for r in db.execute('SELECT seq,source FROM dialogue_messages WHERE scope=?', (self.key,)) if r['source'] in current]
            end = max(positions, default=through)
            rows = [dict(r) for r in db.execute('''SELECT * FROM dialogue_messages WHERE scope=? AND seq>? AND created>?
                AND (role='assistant' OR seq<=?) ORDER BY seq''', (self.key, through, cleared, end)) if r['source'] not in current]
            summary = db.execute('SELECT * FROM dialogue_summaries WHERE scope=? AND cleared=?', (self.key, cleared)).fetchone() if not through else None
        # Reused Harness sessions already own their assistant turns. Replay only
        # previously unseen incoming context, followed by this job's real input.
        if through:
            rows = [r for r in rows if r['role'] == 'user']
        elif summary:
            rows = [r for r in rows if r['seq'] > summary['through_seq']]
        prefix = summary['text'] if summary else ''
        messages = [json.loads(r['message']) for r in rows]
        # A conservative replay budget in characters, not a claimed token count.
        # Normal ongoing sessions use Harness's own token-based compaction.
        if not through and sum(len(r['message']) for r in rows) + len(prefix) > 60000:
            split = 0
            remaining = sum(len(r['message']) for r in rows)
            while split < len(rows) and remaining > 30000 and rows[split]['seq'] < min(positions, default=end):
                remaining -= len(rows[split]['message']); split += 1
            if split:
                from model_gateway import ModelGateway
                result = ModelGateway(self.root, 'qq').json(
                    '总结旧对话供同一窗口继续聊天。保留用户仍有效的设定、偏好、后续纠正、人物与指代、未完成话题及来源消息号；区分说话人。描述对话中的角色扮演，不将其转成人物事实或全局人格。只输出JSON {"summary":"6000字以内的对话摘要"}。',
                    json.dumps({'previous_summary': prefix, 'messages': messages[:split]}, ensure_ascii=False),
                    origin='group-history-compress', cancelled=cancelled)
                prefix = str(result.get('summary', '')).strip()
                if not prefix or len(prefix) > 12000:
                    raise ValueError('未取得有效历史摘要，原始历史已保留')
                with database(self.path) as db:
                    db.execute('INSERT OR REPLACE INTO dialogue_summaries VALUES(?,?,?,?)',
                               (self.key, rows[split-1]['seq'], prefix, cleared))
                messages = messages[split:]
                rows = rows[split:]
        # Recent non-mention pictures reach the same model turn as real pixels.
        # Persist only local attachment references; do not store base64 in the DB.
        recent={i for i,m in list(enumerate(messages))[-12:] if m.get('fish_history_media')}
        recent=set(sorted(recent)[-2:])
        for index,message in enumerate(messages):
            images=message.pop('fish_history_media',[])
            if index not in recent:continue
            stored=json.loads(json.dumps(message))
            from multimodal import MediaService
            service=MediaService(self.root,'qq')
            for image in images:
                local=bool(image.get('local_path'))
                segment={'type':'image','path':image['local_path']} if local else image
                content=service.prepare_content([segment],'[此群消息的实际历史图片]',local=local,cancelled=cancelled)
                message['content'].extend(content)
                if segment.get('local_path'):image['local_path']=segment['local_path']
            source=rows[index]['source']
            stored['fish_history_media']=images
            with database(self.path) as db:db.execute('UPDATE dialogue_messages SET message=? WHERE scope=? AND source=?',(json.dumps(stored,ensure_ascii=False),self.key,source))
        if prefix:
            messages.insert(0, {'role': 'user', 'content': [{'type': 'text', 'text': '[本窗口较早对话的摘要]\n' + prefix}]})
        return messages, end


def context_envelope(context, history):
    return json.dumps({'fish_dialogue_version': 1, 'context': context, 'history': history}, ensure_ascii=False)
