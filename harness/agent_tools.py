"""Fish-owned computer tools; no DSH prompt, session history, or model request.

Filesystem and shell contracts follow DSH fs-local/tool-fs and tool-bash:
absolute targets, bounded views with explicit continuation, atomic writes, and
process jobs whose completion is separate from their initial acknowledgement.
This is a Python implementation at Fish's existing ToolHost seam, not a second
Agent loop. DSH interfaces/source references are recorded in docs/agents/tools.md.
"""
from locations import project_path
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from storage import database

STRING = {'type': 'string'}
INTEGER = {'type': 'integer', 'minimum': 1}


def definition(name, description, properties, required=()):
    return {'name': name, 'description': description, 'parameters': {
        'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}}


def definitions():
    return [
        definition('fish_browser','操作Fish自己的Edge浏览器：打开/读取网页、点按、填写、按键、运行页面脚本、截图、下载和标签管理。selector为真实CSS/Playwright选择器；截图像素返回当前主模型，网页文字分段给next_offset；登录保存在Fish自己的资料目录。',
            {'action':{'type':'string','enum':['tabs','open','navigate','read','click','fill','press','evaluate','screenshot','download','close']},
             'tab_id':STRING,'url':STRING,'selector':STRING,'text':STRING,'key':STRING,'script':STRING,'offset':{'type':'integer','minimum':0},'full_page':{'type':'boolean'}},['action']),
        definition('fish_computer','查看并操作当前Windows桌面：windows列窗口、screenshot取真实屏幕像素、focus聚焦句柄、click绝对屏幕坐标、type输入文字、key发送CTRL+S等组合键、scroll滚轮。动作执行回执不等于应用完成，截图核对实际结果。',
            {'action':{'type':'string','enum':['windows','screenshot','focus','click','type','key','scroll']},
             'handle':{'type':'integer'},'x':{'type':'integer'},'y':{'type':'integer'},'text':STRING,'key':STRING,
             'button':{'type':'string','enum':['left','right']},'count':INTEGER,'delta':{'type':'integer'}},['action']),
        definition('fish_file_read', '读取本机文件：文本按行分段返回下一段位置；PNG/JPEG/WebP/GIF/BMP图片直接把原图像素交回当前主模型，并返回resource_id供fish_image_view局部查看。原文件不改；绝对路径可指向主人交办的现有工程。看本机图片不用打开桌面软件。',
                   {'path': STRING, 'start_line': INTEGER, 'lines': INTEGER, 'encoding': STRING}, ['path']),
        definition('fish_directory', '列出本机目录中的文件与子目录；提供绝对路径，可查看已有工程。', {'path': STRING}, ['path']),
        definition('fish_file_write', '真实创建或更新文件；路径为绝对路径，已有内容将被替换。临时文件写完后原子替换。可用 expected_mtime_ns 核对先前读取版本。默认成果保存在 fish_workspace 返回的分类任务目录。',
                   {'path': STRING, 'text': STRING, 'encoding': STRING, 'expected_mtime_ns': INTEGER}, ['path', 'text']),
        definition('fish_file_edit', '在现有文本文件内精确替换旧文本；默认要求恰好匹配一次，all=true 时替换全部匹配。返回真实修改次数和文件路径。',
                   {'path': STRING, 'old': STRING, 'new': STRING, 'all': {'type': 'boolean'}, 'encoding': STRING}, ['path', 'old', 'new']),
        definition('fish_workspace', '取得当前 Fish 任务的分类工作目录。未指定现有工程时，文件、终端输出和成果分别保存，不混入源码或下载根目录。', {}, []),
        definition('fish_shell', '在本机启动 PowerShell 命令并返回实际输出、退出码或仍在运行的任务编号。用 workdir 指定工作目录；不传则用 Fish 当前任务目录。等待结束不代表任务已完成，长任务用 fish_process 继续查询。',
                   {'command': STRING, 'workdir': STRING, 'wait_seconds': {'type': 'number', 'minimum': 0, 'maximum': 20}}, ['command']),
        definition('fish_process', '查询、读取输出或停止 fish_shell 创建的真实进程任务。服务重启后若不能核验该进程会明确返回 unknown，不自动再次运行命令。',
                   {'id': STRING, 'action': {'type': 'string', 'enum': ['status', 'output', 'stop']}, 'offset': {'type': 'integer', 'minimum': 0}}, ['id']),
        definition('fish_qq_action', '通过当前 QQ/NapCat OneBot 连接调用实际账号操作。action 是接口动作名，params 是其真实参数；可查询账号、消息、好友、群，发送消息/表情/文件或执行接口支持的管理操作。使用 Fish 当前 QQ 账号，不能伪称主人本人发送。失败返回真实错误；发送结果不明不会自动重发。工具可调用不等于 QQ 服务支持任意动作。',
                   {'action': STRING, 'params': {'type': 'object'}}, ['action']),
    ]


class AgentTools:
    def __init__(self, root):
        self.root = Path(root)
        self.jobs = {}
        self.lock = threading.RLock()
        from interaction_tools import InteractionTools
        self.interaction = InteractionTools(root,self.workspace)
        self.path = project_path(self.root, 'data/social/computer-jobs.db')
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,session TEXT NOT NULL,pid INTEGER,command TEXT,workdir TEXT,output TEXT,created REAL,ended REAL,status TEXT,exit_code INTEGER)')

    def workspace(self, session):
        import hashlib
        task = hashlib.sha256(session.encode()).hexdigest()[:16]
        base = project_path(self.root, 'workspace/tasks', task)
        task_db = project_path(self.root, 'data/delegation/tasks.db')
        if task_db.is_file():
            import sqlite3
            with sqlite3.connect(task_db.resolve().as_uri()+'?mode=ro',uri=True) as db:
                row = db.execute('SELECT config FROM tasks WHERE session=?',(session,)).fetchone()
            if row and json.loads(row[0]).get('engine')=='fish':
                base = Path(json.loads(row[0])['workspace'])
        for name in ('files', 'outputs', 'artifacts'):
            (base / name).mkdir(parents=True, exist_ok=True)
        return {name: str((base / name).resolve()) for name in ('files', 'outputs', 'artifacts')}

    @staticmethod
    def target(value):
        path = Path(value)
        if not path.is_absolute():
            raise ValueError('文件工具需要绝对路径，请使用实际工程路径或 fish_workspace 返回的目录')
        return path.resolve()

    def atomic_write(self, path, text, encoding='utf-8', expected=None):
        if expected is not None and (not path.exists() or path.stat().st_mtime_ns != expected):
            raise ValueError('文件已在读取后改变，未覆盖其他修改，请先读取当前版本')
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with staged.open('x', encoding=encoding, newline='') as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            os.replace(staged, path)
        finally:
            if staged.exists():
                # This is only this call's unused staging file, not user content.
                staged.unlink()
        return {'ok': True, 'path': str(path), 'bytes': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns}

    def execute(self, name, args, identity, call_id):
        if not identity.get('owner'):
            raise PermissionError('本机与账号操作需要真实主人身份；群友不会因此取得主人的电脑权限')
        session = identity['session']
        if name=='fish_browser': return self.interaction.browser_call(session,args)
        if name=='fish_computer': return self.interaction.computer_call(session,args)
        if name == 'fish_workspace':
            return self.workspace(session)
        if name == 'fish_directory':
            path = self.target(args['path'])
            return {'path': str(path), 'entries': [{'name': p.name, 'directory': p.is_dir(),
                     'bytes': p.stat().st_size if p.is_file() else None} for p in sorted(path.iterdir(), key=lambda p: p.name.casefold())]}
        if name == 'fish_file_read':
            path = self.target(args['path'])
            if path.suffix.lower() in ('.png','.jpg','.jpeg','.webp','.gif','.bmp'):
                from image_views import ImageViews
                images = ImageViews(self.root)
                record = images.register(path,reference='local-file:'+str(path))
                return images.view(record['doc_id'])
            start, lines = int(args.get('start_line', 1)), int(args.get('lines', 200))
            if start < 1 or lines < 1:
                raise ValueError('读取的起始行和行数必须是正整数')
            picked, used, last, more = [], 0, start - 1, False
            with path.open(encoding=args.get('encoding', 'utf-8'), newline='') as source:
                for number, line in enumerate(source, 1):
                    if number < start:
                        continue
                    if len(picked) >= lines or used + len(line.encode('utf-8')) > 128 * 1024:
                        if not picked:
                            return {'path': str(path), 'error': '单行大于128 KiB，请使用终端或附件分块工具读取原件', 'line': number}
                        more = True
                        break
                    picked.append(line); used += len(line.encode('utf-8')); last = number
            return {'path': str(path), 'text': ''.join(picked), 'start_line': start, 'through_line': last,
                    'next_line': last + 1 if more else None, 'mtime_ns': path.stat().st_mtime_ns,
                    'bytes': path.stat().st_size, 'complete': not more}
        if name == 'fish_file_write':
            return self.atomic_write(self.target(args['path']), args['text'], args.get('encoding', 'utf-8'), args.get('expected_mtime_ns'))
        if name == 'fish_file_edit':
            path = self.target(args['path']); old = args['old']
            if not old:
                raise ValueError('精确替换需要非空旧文本')
            version = path.stat().st_mtime_ns
            encoding = args.get('encoding', 'utf-8')
            with path.open(encoding=encoding, newline='') as source:
                before = source.read()
            count = before.count(old)
            if not count or count > 1 and not args.get('all'):
                raise ValueError('旧文本匹配%d处，未执行替换；请提供唯一片段或明确 all=true' % count)
            result = self.atomic_write(path, before.replace(old, args['new']), encoding, version)
            return {**result, 'replacements': count}
        if name == 'fish_shell':
            return self.shell(session, args)
        if name == 'fish_process':
            return self.process(session, args)
        if name == 'fish_qq_action':
            return self.qq(args,identity)
        raise ValueError('本机工具不存在：' + name)

    def shell(self, session, args):
        command = args['command']
        if not command.strip():
            raise ValueError('命令不能为空')
        dirs = self.workspace(session)
        cwd = self.target(args['workdir']) if args.get('workdir') else Path(dirs['files'])
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if shell is None:
            raise FileNotFoundError('本机未找到 PowerShell')
        job = uuid.uuid4().hex
        output = Path(dirs['outputs']) / (job + '.log')
        wire_command = '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding; ' + command
        with output.open('xb') as handle:
            process = subprocess.Popen([shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', wire_command],
                cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with self.lock:
            self.jobs[job] = process
        with database(self.path) as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (job, session, process.pid, command, str(cwd), str(output), time.time(), None, 'running', None))
        try:
            process.wait(timeout=max(0, min(20, float(args.get('wait_seconds', 1)))))
        except subprocess.TimeoutExpired:
            pass
        return self.process(session, {'id': job, 'action': 'output'})

    def process(self, session, args):
        with database(self.path) as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (args['id'],)).fetchone()
            if row is None:
                raise ValueError('Fish 没有这个进程任务记录')
            row = dict(row)
        with self.lock:
            process = self.jobs.get(args['id'])
        if args.get('action') == 'stop':
            if process is None:
                raise ValueError('此服务未持有该进程，状态无法核验，未按可能复用的PID停止其他进程')
            if process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
                process.wait(timeout=10)
        code = process.poll() if process is not None else row['exit_code']
        state = ('running' if code is None else 'completed' if code == 0 else 'failed') if process is not None else (
                row['status'] if row['status'] in ('completed', 'failed') else 'unknown')
        with database(self.path) as db:
            db.execute('UPDATE jobs SET status=?,exit_code=?,ended=CASE WHEN ? IS NULL THEN ended ELSE COALESCE(ended,?) END WHERE id=?',
                       (state, code, code, time.time(), row['id']))
        offset = int(args.get('offset', 0))
        with Path(row['output']).open('rb') as source:
            source.seek(offset); chunk = source.read(64 * 1024); next_offset = source.tell()
        return {'id': row['id'], 'status': state, 'exit_code': code, 'workdir': row['workdir'],
                'output_path': row['output'], 'output': chunk.decode('utf-8', errors='replace'),
                'next_offset': next_offset, 'output_complete': next_offset >= Path(row['output']).stat().st_size}

    def cancel_session(self,session):
        with database(self.path) as db:
            jobs = [dict(r) for r in db.execute('SELECT id,status FROM jobs WHERE session=?',(session,))]
        results = []
        for job in jobs:
            if job['status'] in ('completed','failed'):
                results.append(job)
                continue
            try:
                results.append(self.process(session,{'id':job['id'],'action':'stop'}))
            except Exception as exc:
                results.append({'id':job['id'],'status':'unknown','error':str(exc)})
        return {'jobs':results}

    def qq(self, args, identity):
        import asyncio
        import sys
        channels = str(project_path(self.root, 'channels'))
        if channels not in sys.path:
            sys.path.insert(0, channels)
        from fish_qq.onebot import OneBotClient, OneBotTimeout, OneBotUnavailable, OneBotError
        from secrets_store import SecretStore
        from storage import read_json
        config = read_json(project_path(self.root, 'channels/fish_qq/config.json'))
        action = str(args['action'])
        if not action or not all(c.isalnum() or c == '_' for c in action):
            raise ValueError('QQ动作名不是合法的接口动作')
        async def run():
            client = OneBotClient(config['napcat']['url'], SecretStore(self.root).get('channel-napcat', 'NAPCAT_TOKEN'))
            await client.start()
            try:
                await asyncio.wait_for(client.ready.wait(), 15)
                from fish_agent.settings.tools import ToolSettings
                ToolSettings(self.root).require('fish_qq_action',args,identity)
                return await client.send_action(action, args.get('params') or {}, timeout=30)
            finally:
                await client.stop()
        # A timeout may follow a real side effect. This call is never replayed.
        from qq_action_record import begin,finish
        record = begin(self.root,action,args.get('params') or {},identity['session'])
        try:
            receipt = asyncio.run(run())
            return {'action': action, 'result': receipt, 'delivery':finish(record,receipt),'account': 'Fish', 'replayed': False}
        except (TimeoutError, OneBotTimeout, OneBotUnavailable, ConnectionError) as exc:
            finish(record,error=str(exc))
            return {'action': action, 'error': 'QQ接口没有返回确认，未自动重试', 'state': 'uncertain', 'may_have_sent': True}
        except OneBotError as exc:
            finish(record,{'status':'failed'},str(exc))
            return {'action': action, 'error': str(exc), 'state': 'failed', 'may_have_sent': False}
