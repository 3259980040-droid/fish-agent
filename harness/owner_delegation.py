"""QQ identity and file delivery adapter around the single B-batch task service."""
from locations import project_path
import json
import threading
import time
import uuid
from pathlib import Path
from governance import Governance
from storage import database


class OwnerDelegation:
    def __init__(self, root, tasks):
        self.root, self.tasks = Path(root), tasks
        self.path = project_path(self.root, 'data/social/owner-control.db')
        self.lock = threading.Lock()
        with database(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS task_file_deliveries(
                task TEXT,artifact TEXT,attempt INTEGER,request_id TEXT UNIQUE,state TEXT,receipt TEXT,updated REAL,
                PRIMARY KEY(task,artifact,attempt))''')
            db.execute("UPDATE task_file_deliveries SET state='unknown' WHERE state='sending'")

    def message(self, mid):
        from qq_store import QQStore
        store = QQStore(self.root)
        with database(store.db_path) as db:
            row = db.execute('SELECT * FROM messages WHERE id=?', (int(mid),)).fetchone()
        policy = Governance(self.root)
        if (not row or row['direction'] != 'inbound' or row['chat_id'] != 'private:' + policy.owner_id()
                or not policy.is_owner(row['sender_id']) or not policy.permitted(row['sender_id'], 'computer.work')):
            raise PermissionError('仅真实主人私聊消息可使用电脑委托')
        return dict(row)

    def owned(self,task):
        value = self.tasks.get(task)
        owner = value['config'].get('owner_id')
        if owner and owner!=Governance(self.root).owner_id():
            raise PermissionError('此任务登记的主人账号与当前账号不同')
        # Historical source rows are provenance, never the continuing authority.
        return value

    def list(self):
        result = []
        for row in self.tasks.list():
            target = row['config'].get('notify_target')
            if target is None:
                target = 'private:'+Governance(self.root).owner_id() if row['source_conversation'].startswith('qq:') else ''
            if not target.startswith(('private:','group:')):
                continue
            result.append(self.owned(row['id']))
        return result

    def dispatch(self,action,params):
        if action=='list':
            return {'tasks':[self.snapshot(row) for row in self.list()]}
        if params.get('message'):
            self.message(params['message'])
        task = params.get('task') or next((r['id'] for r in self.list()),'')
        if not task: raise ValueError('还没有可管理的任务')
        value = self.owned(task)
        if action=='get': return self.snapshot(value)
        if action=='cancel': return self.snapshot(self.tasks.cancel(task))
        if action=='deliver': return self.deliver(value,retry=bool(params.get('retry')))
        raise ValueError('普通交办请直接给Fish，由她选择工具或子任务；此处只管理已有结果')

    def snapshot(self, row):
        with database(self.path) as db:
            deliveries = [dict(r) for r in db.execute('SELECT artifact,attempt,state,receipt FROM task_file_deliveries WHERE task=? ORDER BY attempt', (row['id'],))]
        return {k: row[k] for k in ('id', 'status', 'title', 'error', 'artifact_status', 'source_conversation', 'source_message', 'artifacts', 'result', 'config')} | {'deliveries': deliveries}

    def deliver(self, row, *, retry=False):
        import owner_files
        row = self.owned(row['id'])
        if row['artifact_status'] not in ('ready', 'served'):
            raise ValueError('任务还没有可交付的已归档成果')
        target = row['config'].get('notify_target') or 'private:' + Governance(self.root).owner_id()
        for artifact in row['artifacts']:
            with self.lock:
                with database(self.path) as db:
                    db.execute('BEGIN IMMEDIATE')
                    prior = db.execute('SELECT * FROM task_file_deliveries WHERE task=? AND artifact=? ORDER BY attempt DESC LIMIT 1', (row['id'], artifact['id'])).fetchone()
                    if prior and not retry:
                        continue
                    attempt = prior['attempt'] + 1 if prior else 1
                    key = 'qq-task-file:' + row['id'] + ':' + artifact['id'] + ':' + str(attempt)
                    db.execute('INSERT INTO task_file_deliveries VALUES(?,?,?,?,?,?,?)',
                        (row['id'], artifact['id'], attempt, key, 'sending', '{}', time.time()))
            try:
                from attachment_store import AttachmentStore
                record = AttachmentStore(self.root, 'qq').get(artifact['id'])
                if not record or not Path(record['path']).is_file():
                    raise ValueError('已归档成果原件不存在')
                path = Path(record['path'])
                sent = owner_files.send_file(self.root, target, str(path), key)
                state, receipt = sent['state'], sent.get('receipt', {})
            except Exception as exc:
                state, receipt = 'failed', {'error': type(exc).__name__, 'detail': str(exc)[:200]}
            with database(self.path) as db:
                db.execute('UPDATE task_file_deliveries SET state=?,receipt=?,updated=? WHERE request_id=?',
                    (state, json.dumps(receipt, ensure_ascii=False), time.time(), key))
        return self.snapshot(self.tasks.get(row['id']))
