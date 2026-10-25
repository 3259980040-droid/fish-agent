"""Durable reply targets and delivery state for shared-window conversations."""
from locations import project_path
import json
import re
import time
import uuid
from pathlib import Path
from storage import database

LIVE = ('queued', 'generating', 'sending')


def child_scope(chat, window, task):
    return 'chat:' + chat + (':test:' + window if window else '') + ':task:' + task


def parse_child(scope):
    match = re.fullmatch(r'chat:group:([0-9]{5,15})(?::test:([a-f0-9]{32}))?:task:([a-f0-9]{32})', str(scope))
    return match.groups() if match else None


class GroupTaskStore:
    def __init__(self, root):
        self.root = Path(root); self.path = project_path(self.root, 'data/social/group-replies.db')
        with database(self.path) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, chat TEXT, window TEXT, revision TEXT,
                sender TEXT, root TEXT, thread TEXT, kind TEXT, state TEXT,
                payload TEXT, snapshot TEXT DEFAULT '{}', session TEXT DEFAULT '',
                response TEXT DEFAULT '', remote TEXT DEFAULT '', error TEXT DEFAULT '',
                created REAL, updated REAL);
            CREATE TABLE IF NOT EXISTS inputs(message TEXT PRIMARY KEY, job TEXT, local_id INTEGER);
            CREATE INDEX IF NOT EXISTS jobs_window ON jobs(chat,window,state,updated);
            ''')

    @staticmethod
    def decode(row):
        if not row: return None
        item = dict(row)
        for key in ('payload', 'snapshot'): item[key] = json.loads(item[key])
        return item

    def get(self, task):
        with database(self.path) as db:
            return self.decode(db.execute('SELECT * FROM jobs WHERE id=?', (task,)).fetchone())

    def add(self, payload, local_id, window, revision, *, kind='direct', merge_s=.65, supplement=False, consecutive=False):
        now=time.time(); chat=payload['chat_id']; sender=str(payload['sender']['id']); message=str(payload['message_id'])
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM inputs WHERE message=?',(message,)).fetchone():return None
            prior=db.execute("SELECT * FROM jobs WHERE chat=? AND window=? AND sender=? AND state='queued' AND kind='direct' ORDER BY updated DESC LIMIT 1",(chat,window,sender)).fetchone()
            quote=str(payload.get('reply_to') or '')
            if kind=='direct' and prior and consecutive and now-prior['updated']<=merge_s and (not quote or quote==prior['root']):
                task=prior['id'];items=json.loads(prior['payload']);items.append({'payload':payload,'local_id':local_id})
                db.execute('UPDATE jobs SET payload=?,updated=? WHERE id=?',(json.dumps(items,ensure_ascii=False),now,task))
                db.execute('INSERT INTO inputs VALUES(?,?,?)',(message,task,local_id));return self.decode(db.execute('SELECT * FROM jobs WHERE id=?',(task,)).fetchone())
            if supplement:
                active=db.execute("SELECT id FROM jobs WHERE chat=? AND window=? AND sender=? AND state='generating' ORDER BY created DESC LIMIT 1",(chat,window,sender)).fetchone()
                text=''.join(s.get('text','') for s in payload.get('segments',[]) if s.get('type')=='text').strip()
                if not consecutive or not active or not re.match(r'^(补充|主要|还有|另外|我指的是|改成|更正)',text):return None
            task=uuid.uuid4().hex;thread=task;ref=None
            if quote:
                ref=db.execute('SELECT j.* FROM jobs j LEFT JOIN inputs i ON i.job=j.id WHERE j.chat=? AND j.window=? AND (j.root=? OR j.remote=? OR i.message=?) ORDER BY j.created DESC LIMIT 1',(chat,window,quote,quote,quote)).fetchone()
            if ref:thread=ref['thread']
            db.execute('INSERT INTO jobs(id,chat,window,revision,sender,root,thread,kind,state,payload,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (task,chat,window,revision,sender,message,thread,kind,'queued',json.dumps([{'payload':payload,'local_id':local_id}],ensure_ascii=False),now,now))
            db.execute('INSERT INTO inputs VALUES(?,?,?)',(message,task,local_id))
            return self.decode(db.execute('SELECT * FROM jobs WHERE id=?',(task,)).fetchone())

    def claim(self, task, snapshot):
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute("UPDATE jobs SET state='generating',snapshot=?,updated=? WHERE id=? AND state='queued'",(json.dumps(snapshot,ensure_ascii=False),time.time(),task)).rowcount
            return self.decode(db.execute('SELECT * FROM jobs WHERE id=?',(task,)).fetchone()) if changed else None

    def update(self, task, **fields):
        if set(fields)-{'state','session','response','remote','error','snapshot'}:raise ValueError('未知任务字段')
        if 'snapshot' in fields:fields['snapshot']=json.dumps(fields['snapshot'],ensure_ascii=False)
        with database(self.path) as db:
            db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+',updated=? WHERE id=?',(*fields.values(),time.time(),task))

    def recent_replies(self, chat, window):
        with database(self.path) as db:
            rows=db.execute("SELECT root,remote,sender,response FROM jobs WHERE chat=? AND window=? AND state='sent' ORDER BY updated DESC LIMIT 8",(chat,window)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recover(self, chat, window):
        with database(self.path) as db:
            db.execute("UPDATE jobs SET state='interrupted',error='后台已重启，旧任务未自动重发',updated=? WHERE chat=? AND window=? AND state IN ('queued','generating','sending')",(time.time(),chat,window))

    def cancel_target(self, chat, window, sender, quote=''):
        with database(self.path) as db:
            rows=db.execute("SELECT j.* FROM jobs j LEFT JOIN inputs i ON i.job=j.id WHERE j.chat=? AND j.window=? AND j.sender=? AND j.state IN ('queued','generating') AND (?='' OR j.root=? OR i.message=?) ORDER BY j.created DESC LIMIT 1",(chat,window,str(sender),quote,quote,quote)).fetchall()
        return rows[0]['id'] if rows else ''

    def directory(self, job):
        if job['window']:
            from social_context import SocialContext
            base=SocialContext(self.root).directory(job['window'])
        else:base=project_path(self.root, 'data/qq/groups', job['chat'].split(':')[1], job['revision'])
        return base/'children'/job['id']

    def bind(self, scope, sender, session):
        parts=parse_child(scope)
        if not parts:raise PermissionError('无效子任务范围')
        group,window,task=parts;job=self.get(task)
        if not job or job['chat']!='group:'+group or job['window']!=(window or '') or job['sender']!=str(sender) or job['state']!='generating':
            raise PermissionError('子任务身份、范围或状态不匹配')
        if job['session'] and job['session']!=session:raise PermissionError('子任务已绑定其他模型会话')
        self.update(task,session=session)
        return job
