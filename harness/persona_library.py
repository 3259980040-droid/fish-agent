"""Import community personas as data, with source attribution and live selection.

Formats follow AstrBot persona exports, MaiBot personality configuration and
plain character Markdown. Source prompts never grant runtime tool authority.
"""
import json
from pathlib import Path
import tomllib
from social_context import SocialContext
from storage import database


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

    def list(self):
        with database(self.contexts.path) as db:
            sources={r['revision']:r['source'] for r in db.execute('SELECT * FROM persona_sources')}
        return [{'id':p['id'],'name':p['name'],'source':sources.get(p['id'],'Fish 本地人格')} for p in self.contexts.personas('qq')]

    def install_presets(self):
        rows=json.loads((self.root/'vendor/integrations/community-personas/personas.json').read_text(encoding='utf-8'))
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
            current=c.owner_space(owner)
            from tulpa_owner import matches
            tulpa=bool(current and matches(self.root,current))
            if current and not new:
                space=c.change_persona(current['id'],p['id'])
            else:space=c.create_space('qq',p['name']+' · 主人私聊',revision=p['id'],origin='owner-qq')
            c.select_owner_space(space['id'],owner)
            if tulpa:
                try:self.reload_tulpa(space,p)
                except Exception:
                    c.change_persona(current['id'],current['revision']);c.select_owner_space(current['id'],owner)
                    raise
        elif target.startswith('group:'):
            group=target.split(':',1)[1];tests=GroupTests(self.root);tests.require_controller(group,owner)
            current=tests.current(group)
            space=c.change_persona(current['id'],p['id']) if current and not new else tests.create(group,p['id'],owner)
        else:raise ValueError('请选择主人私聊或白名单群')
        return {'ok':True,'space':space['id'],'revision':p['id'],'name':p['name'],'history':'new' if new else 'keep'}

    def reload_tulpa(self,space,persona):
        from storage import settings_lock
        with settings_lock(self.root/'data/integrations/tulpa/call-queue',timeout=120):
            return self._reload_tulpa(space,persona)

    def _reload_tulpa(self,space,persona):
        from storage import read_json,write_json
        from plugin_registry import PluginRegistry
        path=self.root/'data/integrations/tulpa/owner-route.json';cfg=read_json(path)
        directory=self.root/'data/integrations/tulpa/app/prompts';directory.mkdir(parents=True,exist_ok=True)
        preset='fish_'+persona['id'];(directory/(preset+'.md')).write_text(persona['body'],encoding='utf-8')
        reg=PluginRegistry(self.root);plugin=next(p for p in reg.entries() if p['id']=='tulpa_owner')
        old=self.contexts.persona(cfg['persona_revision']);old_preset='fish_'+old['id']
        (directory/(old_preset+'.md')).write_text(old['body'],encoding='utf-8')
        def start(selected):
            value=reg._run(plugin,'call','start_chat_session',{'conversation_id':cfg['conversation_id'],
                'persona_preset':selected,'idempotency_key':'fish-persona-'+__import__('uuid').uuid4().hex})
            packet=json.loads(next(b['text'] for b in value['content'] if b['type']=='text'))
            if not packet.get('session',{}).get('active'):raise RuntimeError('原版人格会话尚未开始，请检查 MCP 记录')
            return packet
        reg._run(plugin,'call','stop_chat_session',{'session_id':cfg['chat_session_id']})
        try:packet=start(preset)
        except Exception:
            restored=start(old_preset);cfg['chat_session_id']=restored['session']['id'];write_json(path,cfg)
            raise
        cfg.update(chat_session_id=packet['session']['id'],space_id=space['id'],persona_revision=persona['id'])
        write_json(path,cfg)
