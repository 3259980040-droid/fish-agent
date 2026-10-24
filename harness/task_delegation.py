"""Optional Fish child tasks over the existing Fish model/tool loop and task ledger."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import threading
import time
import uuid
from pathlib import Path
from storage import database
from governance import Governance
from request_inspector import redact
import delegation_artifacts

ACTIVE = ('queued','starting','running','cancel_requested')


class TaskDelegation:
    def __init__(self,root,on_result=None):
        self.root = Path(root)
        self.path = self.root/'data/delegation/tasks.db'
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='fish-child')
        self._cancels,self._futures = {},{}
        self._closing,self.on_result = False,on_result
        with database(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,title TEXT NOT NULL,prompt TEXT NOT NULL,
                source_conversation TEXT NOT NULL,source_message TEXT NOT NULL,session TEXT NOT NULL,status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '',config TEXT NOT NULL,artifacts TEXT NOT NULL DEFAULT '[]',artifact_status TEXT NOT NULL DEFAULT 'pending',created REAL NOT NULL,updated REAL NOT NULL)''')
            db.execute("UPDATE tasks SET status='unknown',error='后台在任务执行期间退出，结果未确认，没有自动重做',updated=? WHERE status IN ('queued','starting','running','cancel_requested')",(time.time(),))

    def _permission(self):
        cfg = Governance(self.root,'qq').config()
        if cfg['paused'] or 'computer.work' not in cfg['owner_permissions']:
            raise PermissionError('主人当前的本机工具授权未启用，或 Fish 已暂停')

    @staticmethod
    def _identifier(task):
        if not re.fullmatch(r'[a-f0-9]{32}',str(task)):
            raise ValueError('任务编号无效')

    def get(self,task):
        self._identifier(task)
        with database(self.path) as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
        if row is None: raise ValueError('任务不存在')
        value = dict(row)
        value.update(config=json.loads(value['config']),artifacts=json.loads(value['artifacts']))
        value['workspace'] = value['config'].get('workspace') or str(self.root/'data/delegation/tasks'/task/'workspace')
        value['kind'] = value['config'].get('kind','legacy')
        value['engine'] = value['config'].get('engine','dsh')
        return value

    def directory(self,task):
        value = self.get(task)
        return Path(value['workspace'])

    def list(self):
        with database(self.path) as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM tasks ORDER BY created DESC LIMIT 100')]
        return [self.get(task) for task in ids]

    def for_session(self,session):
        with database(self.path) as db:
            row = db.execute('SELECT id FROM tasks WHERE session=?',(session,)).fetchone()
        return self.get(row['id']) if row else None

    def submit(self,request_id,title,prompt,source_conversation='',source_message='',*,kind='task',parent_session='',source_scope='owner',source_place='desktop',notify='source',parent_task=''):
        self._permission()
        if not re.fullmatch(r'[a-f0-9]{32}',str(request_id)):
            raise ValueError('请提供稳定的32位提交编号')
        if not isinstance(prompt,str) or not prompt.strip(): raise ValueError('请说明子任务要完成什么')
        if kind not in ('task','research','summary'): raise ValueError('任务类型应为task、research或summary')
        if notify not in ('source','owner','none'): raise ValueError('结果通知目标应为source、owner或none')
        task,now = uuid.uuid4().hex,time.time()
        owner = Governance(self.root,'qq').owner_id()
        target = 'private:'+owner if notify=='owner' else source_place if notify=='source' else ''
        config = {'engine':'fish','role':'qq','kind':kind,'owner_id':owner,'workspace':str(self.root/'workspace/tasks'/task),
                  'parent_session':parent_session,'parent_task':parent_task,'source_scope':source_scope,'source_place':source_place,
                  'notify_target':target,'model_source':'harness/agents.json; same Fish ModelGateway; no DSH system prompt'}
        with self._lock:
            if self._closing: raise RuntimeError('Fish 子任务服务正在关闭')
            with database(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT id,prompt FROM tasks WHERE request_id=?',(request_id,)).fetchone()
                if old:
                    if old['prompt']!=prompt: raise ValueError('此提交编号已经用于另一项任务')
                    return self.get(old['id'])
                db.execute('INSERT INTO tasks(id,request_id,title,prompt,source_conversation,source_message,session,status,config,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (task,request_id,str(title).strip() or prompt[:40],prompt,str(source_conversation),str(source_message),'fish-task-'+task,'queued',json.dumps(config,ensure_ascii=False),now,now))
            self._cancels[task] = threading.Event()
            future = self._executor.submit(self._run,task)
            self._futures[task] = future
        return self.get(task)

    def _run(self,task):
        with database(self.path) as db:
            changed = db.execute("UPDATE tasks SET status='starting',updated=? WHERE id=? AND status='queued'",(time.time(),task)).rowcount
        if not changed:
            with self._lock:
                self._cancels.pop(task,None); self._futures.pop(task,None)
            return
        row = self.get(task)
        cancellation = self._cancels[task]
        workspace = Path(row['workspace'])
        for name in ('files','outputs','artifacts','records'): (workspace/name).mkdir(parents=True,exist_ok=True)
        tools = {'previewed':set(),'tools':[]}
        text,error,status = '','','failed'
        try:
            self._permission()
            from social_context import SocialContext
            from memory_store import MemoryStore
            from tool_host import bind_session
            from fish_chat import respond
            context = SocialContext(self.root)
            persona = context.active_persona('qq')
            bind_session(self.root,'qq',row['session'],'owner',context.owner())
            prompt = ('[Fish 自己安排的可选子任务，结果回到原主线]\n'+row['prompt']+
                '\n本任务工作区域：'+str(workspace)+'；files放工作文件，outputs放运行输出，artifacts放成果。'
                '主人指定的现有工程仍按其真实绝对路径处理，不限制为只创建新文件。'
                '\n按需要实际使用你已有的文件、终端、MCP、联网工具。文件做好后用fish_present_files提交实际绝对路径，'
                '没有文件的研究/摘要直接在最终结果里给出内容、来源及未完成事项。仅返回排队或建议不算完成。'
                '这是独立工具上下文，不冒充主人本人发言；通常不额外打扰真人，由已记录的结果通知目标交付。')
            if row['kind']=='research': prompt += '\n研究时核对来源和时间，区分资料事实、来源观点与推测，不能把搜索摘要说成读过原文。'
            if row['kind']=='summary': prompt += '\n按主任务目的归纳实际取得的材料，保留来源、不确定和未覆盖范围，不补写未读内容。'
            with database(self.path) as db:
                cfg = {**row['config'],'persona_revision':persona['id']}
                db.execute("UPDATE tasks SET status='running',config=?,updated=? WHERE id=?",(json.dumps(cfg,ensure_ascii=False),time.time(),task))
            raw = respond(self.root,'qq',persona=persona,history=[],history_source={'kind':'optional-child','parent_session':row['config']['parent_session']},
                memory=MemoryStore(self.root,'qq').context(row['prompt'],'owner'),scope='owner',references='',
                current=[{'type':'text','text':prompt}],session=row['session'],cancelled=cancellation,
                origin='fish-task:'+task,output='bubbles',emoji_state=tools,tool_context='owner-private',execution_key='child-'+task,delivery_required=True)
            from chat_output import decode_reply
            bubbles = decode_reply(self.root,raw)
            text = '\n\n'.join(b.get('text','') for b in bubbles if b.get('kind')=='text')
            with database(self.path) as db:
                current = json.loads(db.execute('SELECT config FROM tasks WHERE id=?',(task,)).fetchone()['config'])
                direct = [call['delivery'] for call in tools['tools'] if call.get('delivery') and call['delivery'].get('target') in ('owner',current.get('notify_target'))]
                db.execute('UPDATE tasks SET config=? WHERE id=?',(json.dumps({**current,'result_bubbles':bubbles,'direct_deliveries':direct},ensure_ascii=False),task))
            if not bubbles and not self.get(task)['artifacts']:
                raise ValueError('子任务没有返回结果，也没有提交实际成果文件')
            status = 'completed'
        except InterruptedError as exc:
            status,error = 'cancelled',str(exc)
        except Exception as exc:
            status = 'unknown' if isinstance(exc,TimeoutError) else 'failed'
            error = type(exc).__name__+'：'+redact(str(exc))
        finally:
            if cancellation.is_set():
                status = 'cancelled'
                previous = self.get(task)['error']
                error = '; '.join(dict.fromkeys(filter(None,[error,previous]))) or '已停止本次模型/工具接续；已有文件修改保留，服务商计算状态不作虚假确认'
            (workspace/'records/tools.json').write_text(redact(json.dumps(tools['tools'],ensure_ascii=False,indent=2)),encoding='utf-8')
            with database(self.path) as db:
                db.execute('UPDATE tasks SET status=?,error=?,result=?,artifact_status=CASE WHEN artifact_status=\'pending\' THEN \'ready\' ELSE artifact_status END,updated=? WHERE id=?',
                    (status,error,str(text or ''),time.time(),task))
            with self._lock:
                self._cancels.pop(task,None); self._futures.pop(task,None)
            Governance(self.root,'qq').audit('task.'+status,row['title']+'：'+(error or '结果已写入原任务记录'),task)
            if callable(self.on_result):
                try: self.on_result(self.get(task))
                except Exception as exc:
                    Governance(self.root,'qq').fault('task-result:'+task,exc,stage='delivery')

    def present(self,session,paths,*,title='本轮成果',parent_session='',source_conversation='',source_message='',source_scope='owner',source_place='desktop'):
        self._permission()
        if not isinstance(paths,list) or not paths: raise ValueError('请提交至少一个实际成果文件路径')
        from attachment_store import AttachmentStore
        store = AttachmentStore(self.root,'qq')
        files = []
        for name in dict.fromkeys(paths):
            path = Path(name)
            if not path.is_absolute() or not path.is_file(): raise ValueError('成果必须是已存在文件的真实绝对路径：'+str(path))
            record = store.import_path('task-output',path,source='fish-task-artifact',source_ref=source_message or session)
            files.append({'id':record['id'],'resource_id':record['resource_id'],'name':record['name'],'size':record['size'],'source_path':str(path.resolve())})
        row = self.for_session(session)
        if row:
            previous = {f.get('source_path') or f['id']:f for f in row['artifacts']}
            previous.update({f['source_path']:f for f in files})
            with database(self.path) as db:
                db.execute("UPDATE tasks SET artifacts=?,artifact_status='ready',updated=? WHERE id=?",(json.dumps(list(previous.values()),ensure_ascii=False),time.time(),row['id']))
            task = row['id']
        else:
            task,now = uuid.uuid4().hex,time.time()
            config = dict(engine='fish',kind='inline',owner_id=Governance(self.root).owner_id(),workspace=str(Path(files[0]['source_path']).parent),
                          parent_session=parent_session or session,source_scope=source_scope,source_place=source_place,notify_target='')
            with database(self.path) as db:
                db.execute('INSERT INTO tasks(id,request_id,title,prompt,source_conversation,source_message,session,status,config,artifacts,artifact_status,created,updated,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (task,task,title,'主回合实际执行并提交成果',source_conversation,source_message,session+':files:'+task,'completed',json.dumps(config,ensure_ascii=False),json.dumps(files,ensure_ascii=False),'ready',now,now,'成果已真实归档，投递状态由发送工具回执另记'))
        return {'task_id':task,'files':files,'artifact_status':'ready','note':'实际文件已归档并出现在任务页；这不是QQ文件已送达的证明'}

    def cancel(self,task,*,timeout=False):
        row = self.get(task)
        if row['status'] not in ACTIVE: return row
        with self._lock:
            event = self._cancels.get(task)
            if event is None:
                with database(self.path) as db:
                    db.execute("UPDATE tasks SET status='unknown',error='当前后台未持有该任务执行句柄，未伪装已停止或重新执行',updated=? WHERE id=?",(time.time(),task))
                return self.get(task)
            event.set()
            queued = row['status']=='queued'
            with database(self.path) as db:
                db.execute('UPDATE tasks SET status=?,error=?,updated=? WHERE id=?',('cancelled' if queued else 'cancel_requested',
                    '尚未启动，已取消排队' if queued else '停止后续模型/工具调用；已有文件修改不会撤销，正在确认本任务进程',time.time(),task))
        from tool_host import request
        try:
            stopped = request(self.root,'/tasks/stop-processes',{'session':row['session']},role='qq',timeout=15)
            if any(r['status']=='unknown' for r in stopped.get('jobs',[])):
                with database(self.path) as db: db.execute("UPDATE tasks SET error='模型任务已请求取消，部分已启动进程无法确认停止，详见进程记录' WHERE id=?",(task,))
        except Exception as exc:
            with database(self.path) as db: db.execute('UPDATE tasks SET error=? WHERE id=?',('已请求取消，进程停止确认失败：'+str(exc),task))
        return self.get(task)

    def download(self,task,artifact_id,*,offset=0):
        row = self.get(task)
        if row['artifact_status'] not in ('ready','served'): raise ValueError('任务尚未提交可核查的文件')
        artifact = next((a for a in row['artifacts'] if a['id']==artifact_id),None)
        if not artifact: raise ValueError('此附件不属于本任务')
        result = delegation_artifacts.download(self.root,artifact,offset=offset)
        if result['next_offset'] is None:
            with database(self.path) as db: db.execute("UPDATE tasks SET artifact_status='served',updated=? WHERE id=?",(time.time(),task))
        return result

    def close(self):
        with self._lock:
            self._closing = True
            for cancellation in self._cancels.values(): cancellation.set()
            with database(self.path) as db:
                db.execute("UPDATE tasks SET status='unknown',error='后台关闭，原任务结果未确认，没有自动重做',updated=? WHERE status IN ('queued','starting','running','cancel_requested')",(time.time(),))
        self._executor.shutdown(wait=True,cancel_futures=True)
