"""Import community personas as data, with source attribution and live selection.

Formats follow AstrBot persona exports, MaiBot personality configuration and
plain character Markdown. Source prompts never grant runtime tool authority.
"""
from locations import project_path
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import time
import tomllib
import uuid
from social_context import SocialContext
from storage import database, locked_settings

# Columns a persona backup has to carry to be restorable with one INSERT.
BACKUP_COLUMNS = ('id', 'role', 'name', 'body', 'global_rules', 'workspace_rules', 'digest', 'parent', 'created')


class PersonaLibrary:
    def __init__(self,root):
        self.root=Path(root);self.contexts=SocialContext(root)
        with database(self.contexts.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS persona_sources(revision TEXT PRIMARY KEY,source TEXT,format TEXT)')

    def add(self,name,body,source='',format='markdown'):
        p=self.contexts.save_persona('qq',name,body,global_rules='',workspace_rules='')
        with database(self.contexts.path) as db:
            db.execute('INSERT INTO persona_sources VALUES(?,?,?)',(p['id'],source,format))
        return p

    def import_text(self,text,*,name='导入人格',source='',format='auto'):
        if format=='auto':
            format='toml' if '[personality]' in text or '[persona]' in text else 'json' if text.lstrip().startswith(('{','[')) else 'markdown'
        if format=='markdown':return [self.add(name,text,source,format)]
        data=json.loads(text) if format=='json' else tomllib.loads(text)
        if format=='toml':
            personality=data.get('personality',data.get('persona',{}))
            bot=data.get('bot',{})
            body='\n\n'.join(f'{key}：{value}' for key,value in personality.items() if isinstance(value,(str,list)) and value)
            if not body:raise ValueError('文件中没有可导入的 personality 人格设定')
            return [self.add(str(bot.get('nickname') or name),body,source,'maibot-toml')]
        if isinstance(data,dict) and isinstance(data.get('data'),dict):data=data['data']
        rows=data if isinstance(data,list) else data.get('personas',data.get('roles',[data]))
        if isinstance(rows,dict):rows=[dict(v,persona_id=k) if isinstance(v,dict) else {'name':k,'system_prompt':v} for k,v in rows.items()]
        result=[]
        for item in rows:
            if not isinstance(item,dict):continue
            title=str(item.get('name') or item.get('persona_id') or item.get('id') or name)
            body=item.get('system_prompt') or item.get('systemPrompt') or item.get('prompt') or item.get('text') or item.get('description')
            if not isinstance(body,str) or not body.strip():continue
            examples=item.get('begin_dialogs') or item.get('scenario') or item.get('mes_example')
            if examples:body+='\n\n示例对话或场景：\n'+(examples if isinstance(examples,str) else json.dumps(examples,ensure_ascii=False))
            result.append(self.add(title,body,source,'astrbot-json'))
        if not result:raise ValueError('未找到人格正文；支持 Markdown、AstrBot JSON 和 MaiBot TOML')
        return result

    def create(self,name,body):
        """Store one persona the owner wrote here; it is never applied automatically."""
        title=' '.join(str(name or '').split());text=str(body or '').strip()
        if not title:raise ValueError('请填写人格名称')
        if len(title)>80:raise ValueError('人格名称最多 80 字')
        if not text:raise ValueError('请填写人格正文')
        if len(text)>40000:raise ValueError('人格正文最多 40000 字')
        return self.add(title,text,source='DSH Fish 新建人格')

    @locked_settings
    def clear(self,keep=''):
        """Back up bindings, then remove only QQ personas with no live references."""
        contexts=self.contexts;owner=contexts.owner()
        live=str(keep or contexts.active_persona('qq')['id'])
        if not live:live=contexts.ensure_default('qq')['id']
        with database(contexts.path) as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT * FROM personas WHERE role='qq' ORDER BY created DESC,id").fetchall()
            selected=next((row for row in rows if row['id']==live),None)
            if selected is None:raise ValueError('要保留的 QQ 人格版本不存在，已停止清理')
            tables={row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            spaces={row['id']:dict(row) for row in db.execute('SELECT * FROM spaces')}
            in_use={}

            def protect(revisions,reason):
                for revision in set(revisions)-{''}:
                    counts=in_use.setdefault(revision,{})
                    counts[reason]=counts.get(reason,0)+1

            for table,reason in (('default_personas','角色默认'),('deployments','群部署'),('agent_profiles','Agent活动人格')):
                if table not in tables:
                    continue
                for row in db.execute(f'SELECT revision FROM {table}'):
                    protect([row['revision']],reason)
            for table in ('owner_routes','group_test_routes'):
                if table not in tables:continue
                for row in db.execute(f'SELECT space FROM {table}'):
                    if row['space'] in spaces:
                        protect([spaces[row['space']]['revision']],'正在使用的窗口')
            # These references cross database boundaries; never create/migrate a
            # database just to inspect it, or treat a read error as zero references.
            for kind,path in (('desktop',project_path(self.root, 'data/qq/desktop-chat.db')),
                              ('tools',project_path(self.root, 'data/tool-host.db'))):
                if not path.exists():continue
                try:
                    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=20)) as refs:
                        refs.row_factory=sqlite3.Row
                        if kind=='desktop':
                            for row in refs.execute('SELECT space_id,revision FROM conversations'):
                                revisions=[row['revision']] if row['revision'] else []
                                if row['space_id'] in spaces:revisions.append(spaces[row['space_id']]['revision'])
                                protect(revisions,'桌面对话')
                        elif refs.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='identities'").fetchone():
                            from group_tests import parse_scope
                            sessions={value['session']:value['revision'] for value in spaces.values() if value['session']}
                            for row in refs.execute('SELECT session,scope FROM identities'):
                                trial=parse_scope(row['scope'])
                                sid=row['scope'][6:] if row['scope'].startswith('space:') else trial[1] if trial else ''
                                revisions=[sessions[row['session']]] if row['session'] in sessions else []
                                if sid in spaces:revisions.append(spaces[sid]['revision'])
                                protect(revisions,'工具会话')
                except sqlite3.Error as exc:
                    raise ValueError(f'引用检查失败，已停止人格清理（{path.name}）：{exc}') from exc
            backup=self._backup(db,live,tables)
            result={'kept':{'id':live,'name':selected['name']},
                    'deleted':0,'backup':str(backup),'protected':[],'windows':0}
            for row in rows:
                revision=row['id']
                if revision==live:continue
                if revision in in_use:
                    counts=in_use[revision]
                    result['protected'].append({'id':revision,'name':row['name'],'reason':'、'.join(counts),
                                                'reference_count':sum(counts.values()),'references':counts})
                    continue
                for space in [str(value['id']) for value in db.execute('SELECT id FROM spaces WHERE revision=?',(revision,))]:
                    for table in ('group_test_routes','group_test_spaces','owner_routes'):
                        if table in tables:db.execute(f'DELETE FROM {table} WHERE space=?',(space,))
                    db.execute('DELETE FROM spaces WHERE id=?',(space,))
                    result['windows']+=1
                for table in ('approvals','persona_output_modes','persona_sources'):
                    db.execute(f'DELETE FROM {table} WHERE revision=?',(revision,))
                db.execute('DELETE FROM personas WHERE id=?',(revision,))
                result['deleted']+=1
        return result

    def _backup(self,db,keep,tables):
        """Snapshot personas and bindings from the same transaction as cleanup."""
        path=self.root/'backups'/('personas-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.json')
        sources={row['revision']:{'source':row['source'],'format':row['format']}
                 for row in db.execute('SELECT * FROM persona_sources')}
        payload={'format':'fish-personas','version':2,'created':time.time(),'root':str(self.root),
                 'kept':keep,'columns':list(BACKUP_COLUMNS),'sources':sources,
                 'personas':[dict(row) for row in db.execute('SELECT * FROM personas')]}
        for table in ('spaces','owner_routes','group_test_routes','group_test_spaces',
                      'default_personas','deployments','approvals','persona_output_modes','agent_profiles'):
            payload[table]=[dict(row) for row in db.execute(f'SELECT * FROM {table}')] if table in tables else []
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+chr(10),encoding='utf-8')
        except OSError as exc:
            raise ValueError(f'人格备份失败，未执行清理：{exc}') from exc
        return path

    def list(self):
        with database(self.contexts.path) as db:
            sources={r['revision']:r['source'] for r in db.execute('SELECT * FROM persona_sources')}
        return [{'id':p['id'],'name':p['name'],'source':sources.get(p['id'],'Fish 本地人格')} for p in self.contexts.personas('qq')]

    def install_presets(self):
        rows=json.loads((project_path(self.root, 'vendor/integrations/community-personas/personas.json')).read_text(encoding='utf-8'))
        result=[]
        for row in rows:
            source=row['source']+'#'+row['key']
            with database(self.contexts.path) as db:
                old=db.execute('SELECT revision FROM persona_sources WHERE source=?',(source,)).fetchone()
            result.append(self.contexts.persona(old['revision']) if old else self.add(row['name'],row['system_prompt'],source,'community-'+row['license']))
        return result

    def apply(self,revision,target='owner',*,new=False):
        from group_tests import GroupTests
        c=self.contexts;p=c.persona(c.resolve(revision) if len(revision)<32 else revision);owner=c.owner()
        if target=='owner':
            c.apply_active_persona('qq', p['id'])
            return {'ok':True,'space':'','revision':p['id'],'name':p['name'],'history':'keep'}
        elif target.startswith('group:'):
            group=target.split(':',1)[1];tests=GroupTests(self.root);tests.require_controller(group,owner)
            current=tests.current(group)
            space=c.change_persona(current['id'],p['id']) if current and not new else tests.create(group,p['id'],owner)
        else:raise ValueError('请选择主人私聊或白名单群')
        return {'ok':True,'space':space['id'],'revision':p['id'],'name':p['name'],'history':'new' if new else 'keep'}

