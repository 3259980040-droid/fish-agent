"""Stream original attachments; doc_store owns the sole resource/text/vector index."""
from __future__ import annotations
from locations import project_path
import base64
import codecs
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree

CHUNK_BYTES = 1024 * 1024
EXCERPT_BYTES = 1024 * 1024
READ_BYTES = 128 * 1024
TEXT_EXTENSIONS = {'txt','md','markdown','csv','tsv','log','json','yaml','yml','xml','ini','py','js','ts','html','htm','css','sh','ps1','sql'}
IMAGE_EXTENSIONS = {'jpg','jpeg','png','gif','webp','bmp'}
AUDIO_EXTENSIONS = {'mp3','wav','ogg','silk','amr','m4a','flac','aac'}
VIDEO_EXTENSIONS = {'mp4','mov','mkv','webm','avi','flv'}
DOCUMENT_EXTENSIONS = {'docx','pdf'} | TEXT_EXTENSIONS


def safe_filename(name, default='attachment'):
    base = Path(str(name or '').replace('\\','/')).name.strip().strip('.')
    return re.sub(r'[^0-9A-Za-z\u4e00-\u9fff._\- ()\[\]]+','_',base)[:120].strip(' .') or default


def safe_conversation(chat_id):
    return 'qq-' + re.sub(r'[^0-9A-Za-z_.-]+','_',str(chat_id or ''))[:80]


def kind_for(extension):
    ext = extension.lower().lstrip('.')
    return 'image' if ext in IMAGE_EXTENSIONS else 'audio' if ext in AUDIO_EXTENSIONS else 'video' if ext in VIDEO_EXTENSIONS else 'file'


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _encoding(data):
    if data.startswith(b'\xef\xbb\xbf'): return 'utf-8-sig'
    if data.startswith((b'\xff\xfe',b'\xfe\xff')): return 'utf-16'
    try:
        codecs.getincrementaldecoder('utf-8')().decode(data,final=False)
        return 'utf-8'
    except UnicodeDecodeError:
        return 'gb18030'


def parse_path(path, name):
    ext, size = Path(name).suffix.lower().lstrip('.'), path.stat().st_size
    result = dict(status='unsupported',text='',format=ext,error='',notes=[],original_bytes=size,indexed_chars=0,complete=False)
    try:
        if ext in TEXT_EXTENSIONS:
            with path.open('rb') as file: raw = file.read(EXCERPT_BYTES)
            encoding = _encoding(raw)
            text = codecs.getincrementaldecoder(encoding)(errors='replace').decode(raw,final=len(raw)==size)
            result.update(text=text,encoding=encoding,parsed_bytes=len(raw),parsed_start=0,parsed_end=len(raw),complete=len(raw)==size,range_kind='original-bytes')
        elif ext == 'pdf':
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            texts, chars, count = [], 0, 0
            for page in reader.pages:
                text = page.extract_text() or ''
                texts.append(text[:max(0,EXCERPT_BYTES-chars)])
                chars += len(text); count += 1
                if chars >= EXCERPT_BYTES or count >= 200: break
            result.update(text='\n\f\n'.join(texts),parsed_pages=count,total_pages=len(reader.pages),complete=count==len(reader.pages) and chars<=EXCERPT_BYTES,range_kind='pdf-pages')
        elif ext == 'docx':
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo('word/document.xml')
                parser = ElementTree.XMLPullParser(events=('end',))
                texts, consumed, chars = [], 0, 0
                with archive.open(info) as stream:
                    while consumed < 8*EXCERPT_BYTES and chars < EXCERPT_BYTES:
                        block = stream.read(min(65536,8*EXCERPT_BYTES-consumed))
                        if not block: break
                        consumed += len(block); parser.feed(block)
                        for _,node in parser.read_events():
                            if node.tag.endswith('}p'):
                                paragraph = ''.join(n.text or '' for n in node.iter() if n.tag.endswith('}t'))
                                texts.append(paragraph[:max(0,EXCERPT_BYTES-chars)]); chars += len(paragraph)+1; node.clear()
                result.update(text='\n'.join(texts),parsed_xml_bytes=consumed,total_xml_bytes=info.file_size,complete=consumed==info.file_size and chars<=EXCERPT_BYTES,range_kind='docx-xml-bytes')
        else:
            result['error'] = '此格式仅保存原件，尚未解析；图片使用视觉视图，语音和视频保留原件接口'
            return result
        result['text'] = result['text'].replace('\x00','')
        result['indexed_chars'] = len(result['text'])
        result['status'] = ('read' if result['complete'] else 'partial') if result['text'].strip() else 'empty'
        if not result['complete']: result['notes'].append('自动索引为部分内容；未索引部分仍在原件，可按范围取用，不代表模型已读全文')
    except Exception as exc:
        result.update(status='failed',error='原件已保存，解析失败：'+type(exc).__name__+'：'+str(exc)[:200])
    return result


class AttachmentStore:
    def __init__(self, root, role='qq'):
        if role not in ('qq','research'): raise ValueError('未知的 Agent')
        self.root,self.role = Path(root),role
        self.base = project_path(self.root, 'data', role, 'attachments')
        self.db_path = project_path(self.root, 'data', role, 'attachments.db')
        self.base.mkdir(parents=True,exist_ok=True)
        with self._connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,role TEXT NOT NULL,name TEXT,stored_name TEXT,path TEXT,type TEXT,format TEXT,size INTEGER,source TEXT,source_ref TEXT,status TEXT,error TEXT,text TEXT,dedup_of TEXT,created_at TEXT);
            CREATE INDEX IF NOT EXISTS attachments_conv ON attachments(conversation_id);
            CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY AUTOINCREMENT,attachment_id TEXT NOT NULL,source TEXT,source_ref TEXT,filename TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS links(attachment_id TEXT NOT NULL,conversation_id TEXT NOT NULL,created_at TEXT,UNIQUE(attachment_id,conversation_id));
            CREATE TABLE IF NOT EXISTS attachment_uploads(id TEXT PRIMARY KEY,conversation TEXT,name TEXT,path TEXT,size INTEGER,received INTEGER,state TEXT,attachment_id TEXT,error TEXT,created REAL,updated REAL);
            CREATE TABLE IF NOT EXISTS attachment_reads(id TEXT PRIMARY KEY,attachment_id TEXT,session TEXT,mode TEXT,start INTEGER,end INTEGER,query TEXT,created REAL);''')
            db.execute('CREATE TABLE IF NOT EXISTS attachment_scopes(attachment_id TEXT,scope TEXT,resource_id TEXT,PRIMARY KEY(attachment_id,scope))')
            db.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(attachments)')}
            for name,definition in (('resource_id',"TEXT NOT NULL DEFAULT ''"),('processing',"TEXT NOT NULL DEFAULT '{}'"),('scope',"TEXT NOT NULL DEFAULT 'owner'")):
                if name not in columns: db.execute('ALTER TABLE attachments ADD COLUMN '+name+' '+definition)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path,timeout=30); db.row_factory = sqlite3.Row
        try:
            yield db; db.commit()
        except BaseException:
            db.rollback(); raise
        finally: db.close()

    def limits(self):
        from tool_host import control_ceiling
        from image_views import NATIVE_IMAGE_BYTES
        from multimodal import load_config
        return dict(chunk_bytes=CHUNK_BYTES,original_max_bytes=None,automatic_text_preview_bytes=EXCERPT_BYTES,range_read_max_bytes=READ_BYTES,
            max_per_message=int(load_config(self.root)['max_attachments']), native_image_bytes=NATIVE_IMAGE_BYTES,
            workbench_control_bytes=control_ceiling('/workbench'),tool_control_bytes=control_ceiling('/catalog'),
            source='AttachmentStore / ToolHost HTTP控制 / 模型视觉协议',
            note='原件分块保存，不设20MiB原件上限；可保存不代表已解析或模型可一次理解；磁盘与QQ外部通道以实际结果为准')

    @staticmethod
    def public(row): return {k:v for k,v in row.items() if k!='text'}

    @staticmethod
    def _row(row):
        result = dict(row)
        result['scope'] = result.pop('visible_scope', None) or result['scope']
        result['resource_id'] = result.pop('visible_resource', None) or result['resource_id']
        result['processing'] = json.loads(result.get('processing') or '{}')
        return result

    def get(self, attachment_id, *, scope=None):
        with self._connect() as db:
            sql = 'SELECT a.*,s.scope visible_scope,s.resource_id visible_resource FROM attachments a LEFT JOIN attachment_scopes s ON s.attachment_id=a.id WHERE (a.id=? OR a.resource_id=? OR s.resource_id=?)'
            args = [attachment_id,attachment_id,attachment_id]
            if scope is not None:
                sql += " AND (s.scope IN (?,'public') OR (s.scope IS NULL AND a.scope IN (?,'public')))"
                args.extend((scope,scope))
            sql += ' ORDER BY CASE WHEN s.resource_id=? THEN 0 WHEN s.scope=a.scope THEN 1 ELSE 2 END LIMIT 1'
            args.append(attachment_id)
            row = db.execute(sql,args).fetchone()
        return self._row(row) if row else None

    def _conversation_dir(self,cid): return self.base/safe_filename(str(cid),'conversation').replace(':','_')

    def import_bytes(self,cid,filename,data,*,source='upload',source_ref='',type_hint=''):
        if not isinstance(data,(bytes,bytearray,memoryview)): raise TypeError('附件内容必须是字节')
        directory = self.base/'.incoming'/uuid.uuid4().hex; directory.mkdir(parents=True)
        path = directory/safe_filename(filename)
        with path.open('wb') as file: file.write(data)
        return self.import_path(cid,path,name=filename,source=source,source_ref=source_ref,type_hint=type_hint,move=True)

    def import_path(self,cid,path,*,source='local',source_ref='',type_hint='',name='',move=False,scope=''):
        original = Path(path).expanduser().resolve(); cid = str(cid or '').strip()
        if not original.is_file(): raise FileNotFoundError('附件文件不存在：'+str(original))
        if not cid: raise ValueError('缺少会话编号')
        if move and not original.is_relative_to(self.base.resolve()): raise ValueError('只移动本模块上传暂存；用户原文件始终复制保留')
        hasher = hashlib.sha256()
        with original.open('rb') as file:
            for block in iter(lambda:file.read(CHUNK_BYTES),b''): hasher.update(block)
        content_id = hasher.hexdigest(); existing = self.get(content_id); filename = safe_filename(name or original.name)
        group = re.fullmatch(r'(?:group:|qq-group_)([0-9]+)',cid)
        scope = scope or ('chat:group:'+group[1] if group else 'owner')
        if existing and Path(existing['path']).is_file():
            with self._connect() as db:
                db.execute('INSERT OR IGNORE INTO links VALUES(?,?,?)',(content_id,cid,_now()))
                db.execute('INSERT INTO sources(attachment_id,source,source_ref,filename,created_at) VALUES(?,?,?,?,?)',(content_id,source,source_ref,filename,_now()))
            if not existing['resource_id']: existing = self._index(existing)
            resource_id = self._register(existing,scope,source_ref or str(original))
            with self._connect() as db:
                db.execute('INSERT OR REPLACE INTO attachment_scopes VALUES(?,?,?)',(content_id,scope,resource_id))
            existing.update(scope=scope,resource_id=resource_id)
            if move and original!=Path(existing['path']).resolve(): self._retire_staging(original,content_id)
            return {**existing,'duplicate':True}
        directory = self._conversation_dir(cid)/content_id; directory.mkdir(parents=True,exist_ok=True); target = directory/filename
        if original!=target.resolve():
            if move: os.replace(original,target)
            else:
                staged = directory/('.'+uuid.uuid4().hex+'.part')
                with original.open('rb') as incoming,staged.open('xb') as output:
                    shutil.copyfileobj(incoming,output,CHUNK_BYTES); output.flush(); os.fsync(output.fileno())
                os.replace(staged,target)
        ext = target.suffix.lower().lstrip('.'); kind = kind_for(ext)
        if kind=='file' and type_hint in ('image','audio','video'): kind = type_hint
        row = dict(id=content_id,conversation_id=cid,role=self.role,name=filename,stored_name=filename,path=str(target),type=kind,
            format=ext,size=target.stat().st_size,source=source,source_ref=source_ref or str(original),status='archived',error='',text='',
            dedup_of=content_id,created_at=_now(),resource_id='',processing='{}',scope=scope)
        with self._connect() as db:
            db.execute('INSERT OR REPLACE INTO attachments('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
            db.execute('INSERT OR IGNORE INTO links VALUES(?,?,?)',(content_id,cid,_now()))
            db.execute('INSERT INTO sources(attachment_id,source,source_ref,filename,created_at) VALUES(?,?,?,?,?)',(content_id,source,source_ref,filename,_now()))
        return {**self._index(self.get(content_id)),'duplicate':False}

    def _index(self,row):
        parsed = (parse_path(Path(row['path']),row['name']) if row['type']=='file' else
                  dict(status='archived',text='',format=row['format'],error='',notes=['原媒体已保存，模型通过对应视图取用'],original_bytes=row['size'],indexed_chars=0,complete=False,range_kind='media-original'))
        processing = {k:v for k,v in parsed.items() if k!='text'}
        try:
            resource_id = self._register({**row,'text':parsed['text'],'processing':processing},row['scope'],row['source_ref'])
        except Exception as exc:
            resource_id = ''; parsed.update(status='failed',error='原件已保存，统一资源索引失败：'+type(exc).__name__+'：'+str(exc)[:200])
            processing.update(status=parsed['status'],error=parsed['error'])
        with self._connect() as db:
            db.execute('UPDATE attachments SET resource_id=?,processing=?,status=?,error=?,text=?,format=? WHERE id=?',
                (resource_id,json.dumps(processing,ensure_ascii=False),parsed['status'],parsed['error'],parsed['text'],parsed['format'],row['id']))
            if resource_id:
                db.execute('INSERT OR REPLACE INTO attachment_scopes VALUES(?,?,?)',(row['id'],row['scope'],resource_id))
        return self.get(row['id'])

    def _register(self,row,scope,reference):
        if row['type']=='image':
            from image_views import ImageViews
            return ImageViews(self.root).register(row['path'],scope=scope,source=row['source'],reference=reference)['doc_id']
        from doc_store import ingest
        kind = row['format'] if row['format'] in ('md','html','json','pdf') else 'txt' if row['format'] in TEXT_EXTENSIONS else 'file'
        return ingest(self.root,row['name'],row.get('text') or '',kind=kind,scope=scope,source=row['source'],
            resource_key='attachment:'+row['id'],original_path=row['path'],
            metadata={'attachment_id':row['id'],'source_reference':reference,'processing':row['processing']})['doc_id']

    def _retire_staging(self,path,key):
        path = path.resolve()
        if not path.is_relative_to(self.base.resolve()): raise ValueError('未移动用户原文件')
        root = (self.root/'需要清理/附件上传暂存').resolve(); target = (root/(key+'-'+uuid.uuid4().hex)/path.name).resolve()
        if not target.is_relative_to(root): raise ValueError('暂存归档位置无效')
        target.parent.mkdir(parents=True,exist_ok=True); shutil.move(str(path),str(target))

    def begin(self,cid,name,size):
        if type(size) is not int or size<0: raise ValueError('附件实际字节数无效')
        uid = uuid.uuid4().hex; directory = self.base/'.incoming'/uid; directory.mkdir(parents=True)
        path = directory/safe_filename(name); path.touch(exist_ok=False)
        with self._connect() as db:
            db.execute('INSERT INTO attachment_uploads VALUES(?,?,?,?,?,0,\'receiving\',\'\',\'\',?,?)',(uid,cid,safe_filename(name),str(path),size,time.time(),time.time()))
        return dict(upload_id=uid,received=0,size=size,chunk_bytes=CHUNK_BYTES,state='receiving')

    def upload_status(self,cid,uid):
        with self._connect() as db: row = db.execute('SELECT * FROM attachment_uploads WHERE id=? AND conversation=?',(uid,cid)).fetchone()
        if not row: raise ValueError('附件上传记录不存在或不属于当前对话')
        return dict(row)

    def append(self,cid,uid,offset,encoded):
        if type(offset) is not int or offset<0 or not isinstance(encoded,str) or len(encoded)>(CHUNK_BYTES+2)//3*4:
            raise ValueError('分块位置或大小无效，请按chunk_bytes导入')
        try: data = base64.b64decode(encoded,validate=True)
        except ValueError as exc: raise ValueError('附件分块不能解码') from exc
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE'); row = db.execute('SELECT * FROM attachment_uploads WHERE id=? AND conversation=?',(uid,cid)).fetchone()
            if not row or row['state']!='receiving': raise ValueError('此附件不在接收中，请查看上传状态')
            if offset+len(data)>row['size'] or offset>row['received']: raise ValueError('分块超出原件大小或中间缺块')
            path = Path(row['path'])
            with path.open('r+b') as file:
                if path.stat().st_size<row['received']: raise ValueError('暂存长度异常，已保留现有文件，未伪装导入成功')
                file.seek(offset)
                if offset<row['received']:
                    if offset+len(data)>row['received'] or file.read(len(data))!=data: raise ValueError('重复分块与已保存内容不同')
                    return dict(upload_id=uid,received=row['received'],size=row['size'],state='receiving')
                file.write(data); file.truncate(offset+len(data)); file.flush(); os.fsync(file.fileno())
            received = offset+len(data); db.execute('UPDATE attachment_uploads SET received=?,updated=? WHERE id=?',(received,time.time(),uid))
        return dict(upload_id=uid,received=received,size=row['size'],state='receiving')

    def finish(self,cid,uid):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            stored = db.execute('SELECT * FROM attachment_uploads WHERE id=? AND conversation=?',(uid,cid)).fetchone()
            if not stored:
                raise ValueError('附件上传记录不存在')
            row = dict(stored)
            if row['state']=='completed':
                return self.public(self.get(row['attachment_id']))
            if row['state']=='processing':
                raise ValueError('原件正在登记解析，可查询附件上传状态；没有重复导入')
            if row['state']!='receiving' or row['received']!=row['size'] or Path(row['path']).stat().st_size!=row['size']:
                raise ValueError('原件尚未接收完整，已保留暂存，未解析或交给模型')
            db.execute("UPDATE attachment_uploads SET state='processing',updated=? WHERE id=?",(time.time(),uid))
        try:
            result = self.import_path(cid,row['path'],name=row['name'],source='client-chunk-upload',source_ref='upload:'+uid,move=True)
        except Exception as exc:
            with self._connect() as db:
                db.execute("UPDATE attachment_uploads SET state='failed',error=?,updated=? WHERE id=?",(str(exc),time.time(),uid))
            raise
        with self._connect() as db:
            db.execute("UPDATE attachment_uploads SET state='completed',attachment_id=?,updated=? WHERE id=?",(result['id'],time.time(),uid))
        return self.public(result)

    def list(self,cid,limit=100):
        group = re.fullmatch(r'(?:group:|qq-group_)([0-9]+)',str(cid))
        scope = 'chat:group:'+group[1] if group else 'owner'
        with self._connect() as db:
            return [self.public(self._row(r)) for r in db.execute('SELECT a.*,s.scope visible_scope,s.resource_id visible_resource FROM attachments a JOIN links l ON l.attachment_id=a.id LEFT JOIN attachment_scopes s ON s.attachment_id=a.id AND s.scope=? WHERE l.conversation_id=? ORDER BY a.created_at DESC,a.rowid DESC LIMIT ?',(scope,cid,int(limit)))]

    def detail(self,attachment_id,*,scope=None):
        row = self.get(attachment_id,scope=scope)
        if not row: raise ValueError('附件记录不存在或当前范围不可见')
        with self._connect() as db:
            reads = [dict(r) for r in db.execute('SELECT session,mode,start,end,query,created FROM attachment_reads WHERE attachment_id=? ORDER BY created DESC LIMIT 30',(row['id'],))]
        return {**self.public(row),'reads':reads,'original_exists':Path(row['path']).is_file()}

    def search(self,query,conversation_id=None,limit=10):
        from doc_store import search
        with self._connect() as db:
            allowed = {r[0] for r in db.execute("SELECT COALESCE(s.resource_id,a.resource_id) FROM attachments a JOIN links l ON l.attachment_id=a.id LEFT JOIN attachment_scopes s ON s.attachment_id=a.id AND s.scope='owner' WHERE l.conversation_id=?",(conversation_id,))} if conversation_id else None
        return search(self.root,query,limit=limit,scope='owner',document_ids=allowed)

    def _read_row(self,attachment_id,scope):
        row = self.get(attachment_id,scope=scope)
        if not row or not Path(row['path']).is_file(): raise ValueError('附件原件不存在或当前范围不可见')
        if row['format'] not in TEXT_EXTENSIONS: raise ValueError('本工具读取文本原件；二进制格式请用资料切片、视觉视图或对应文件解析工具')
        return row

    def _codec(self,row,encoding,offset=0):
        codec = encoding or row['processing'].get('encoding') or 'utf-8'
        if codec=='utf-16' and offset>0:
            with Path(row['path']).open('rb') as file: codec = 'utf-16-le' if file.read(2)==b'\xff\xfe' else 'utf-16-be'
        return codec

    def _record_read(self,row,session,mode,start,end,query=''):
        with self._connect() as db:
            db.execute('INSERT INTO attachment_reads VALUES(?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,row['id'],session,mode,start,end,query,time.time()))

    def read_range(self,attachment_id,*,offset=0,length=65536,encoding='',scope=None,session=''):
        row = self._read_row(attachment_id,scope)
        if type(offset) is not int or type(length) is not int or not 1<=length<=READ_BYTES:
            raise ValueError('每次读取1到'+str(READ_BYTES)+'字节，负offset从末尾算')
        start = max(0,row['size']+offset) if offset<0 else min(offset,row['size'])
        with Path(row['path']).open('rb') as file: file.seek(start); raw = file.read(length)
        codec = self._codec(row,encoding,start); end = start+len(raw)
        self._record_read(row,session,'bytes',start,end)
        return dict(attachment_id=row['id'],resource_id=row['resource_id'],path=row['path'],size=row['size'],start=start,end=end,
            next_offset=end if end<row['size'] else None,encoding=codec,text=raw.decode(codec,errors='replace'),
            coverage='仅本次字节范围；多字节边界可能显示替换符，原件未改')

    def find(self,attachment_id,query,*,offset=0,limit=10,encoding='',scope=None,session=''):
        row = self._read_row(attachment_id,scope); codec = self._codec(row,encoding,1)
        if codec=='utf-8-sig': codec = 'utf-8'
        needle = str(query).encode(codec)
        if not needle or type(offset) is not int or offset<0 or not 1<=int(limit)<=50: raise ValueError('请给非空查询、非负字节位置及1到50个命中数')
        hits,carry,position,last = [],b'',min(offset,row['size']),-1
        with Path(row['path']).open('rb') as file:
            file.seek(position)
            while True:
                block = file.read(CHUNK_BYTES)
                if not block: break
                data,base,cursor = carry+block,position-len(carry),0
                lowered = data.lower(); wanted = needle.lower()
                while True:
                    found = lowered.find(wanted,cursor)
                    if found<0: break
                    at = base+found; cursor = found+max(1,len(needle))
                    if at<offset or at<=last: continue
                    last = at; hits.append(dict(offset=at,text=data[max(0,found-160):found+len(needle)+160].decode(codec,errors='replace')))
                    if len(hits)>=int(limit): break
                position += len(block)
                if len(hits)>=int(limit): break
                carry = data[-(len(needle)+320):]
        next_offset = hits[-1]['offset']+len(needle) if len(hits)>=int(limit) else None
        self._record_read(row,session,'search',offset,position,str(query))
        return dict(attachment_id=row['id'],resource_id=row['resource_id'],size=row['size'],matches=hits,searched_start=offset,searched_end=position,
                    next_offset=next_offset,complete=position>=row['size'] and next_offset is None,method='原件流式字节匹配，不是全文语义理解')

    def context_line(self,attachment_id,max_chars=8000):
        row = self.get(attachment_id)
        if not row: return '[附件不在索引中：'+str(attachment_id)+']'
        return ('附件：'+row['name']+'\nattachment_id='+row['id']+'\nresource_id='+row['resource_id']+'\n原件路径：'+row['path']+
                '\n原件字节：'+str(row['size'])+'\n解析范围：'+json.dumps(row['processing'],ensure_ascii=False)+
                '\n开头摘录（不是全文）：\n'+(row.get('text') or '')[:max_chars]+
                '\n其余内容用fish_attachment_read分范围读取、fish_attachment_search搜索原件；原件保留。')
