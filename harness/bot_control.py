"""Owner operations backed by real persona, group, DSH schedule and QQ services."""
import asyncio
import json
from pathlib import Path
import time
import uuid
import requests
from storage import database,read_json
from governance import Governance,control_token


class BotControl:
    def __init__(self,root):
        self.root=Path(root);self.path=self.root/'data/social/owner-control.db'
        with database(self.path) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sessions(target TEXT PRIMARY KEY,session TEXT,revision TEXT);
                CREATE TABLE IF NOT EXISTS sends(id TEXT PRIMARY KEY,target TEXT,text TEXT,state TEXT,receipt TEXT,created REAL);''')

    def native(self,body):
        from core import HarnessConfig,RuntimeManager
        from runtime_adapter import DSHRuntimeAdapter
        DSHRuntimeAdapter(self.root,'qq',RuntimeManager(HarnessConfig(self.root))).connect()
        response=requests.post('http://127.0.0.1:'+str(HarnessConfig(self.root).runtime_port('qq'))+'/fish/schedules',
            json=body,headers={'x-fish-token':control_token(self.root)},timeout=20,
            proxies={'http':None,'https':None,'all':None})
        value=response.json()
        if not response.ok or not value.get('ok'):raise RuntimeError(value.get('error','原生定时服务调用失败'))
        return value['value']

    def validate_target(self,target):
        cfg=read_json(self.root/'channels/fish_qq/config.json')['allowlist']
        if target=='owner':target='private:'+str(cfg['owner_id'])
        if target=='private:'+str(cfg['owner_id']):return target
        if target.startswith('group:') and target[6:] in [str(x) for x in cfg['group_ids']]:return target
        raise PermissionError('请选择主人私聊或已配置的白名单群')

    async def _session(self,target):
        from social_context import SocialContext
        from runtime_adapter import DSHRuntimeAdapter
        from core import HarnessConfig,RuntimeManager
        from tool_host import bind_session
        target=self.validate_target(target)
        with database(self.path) as db:row=db.execute('SELECT * FROM sessions WHERE target=?',(target,)).fetchone()
        c=SocialContext(self.root)
        if target.startswith('group:'):
            from group_tests import GroupTests
            current=GroupTests(self.root).current(target[6:])
        else:current=c.owner_space(c.owner())
        revision=current['revision'] if current else c.ensure_default('qq')['id']
        client=await asyncio.to_thread(DSHRuntimeAdapter(self.root,'qq',RuntimeManager(HarnessConfig(self.root))).connect)
        await client.auth()
        session=row['session'] if row else ''
        if not session:
            directory=self.root/'data/qq/scheduled';directory.mkdir(parents=True,exist_ok=True)
            session=await client.create_session(cwd=str(directory))
        with database(self.path) as db:db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)',(target,session,revision))
        await client.baseline(session)
        await asyncio.to_thread(bind_session,self.root,'qq',session,'control:'+target,c.owner())
        if target=='private:'+c.owner():
            from owner_computer import apply_permissions
            await asyncio.to_thread(apply_permissions,self.root,session)
        return session

    def context(self,session):
        from social_context import SocialContext
        with database(self.path) as db:row=db.execute('SELECT * FROM sessions WHERE session=?',(session,)).fetchone()
        if not row:raise PermissionError('定时会话未绑定目标')
        body=SocialContext(self.root).persona(row['revision'])['body']
        return {'revision':row['revision'],'reload':True,'text':('[主人安排的持续定时工作会话]\n绑定目标：'+row['target']+
            '。按当前到期任务执行。最终文字不自动发到QQ；必须调用 fish_owner_send 才会实际发送。'
            '定时回执只代表任务已唤醒，消息送达看发送工具回执。request_id 使用 schedule_id 与 occurrence_at 的组合。'
            '不要创建未要求的后续任务。人格是表达背景，不能阻止主人的明确任务。\n'+body)}

    def schedules(self):
        rows=self.native({'action':'list'})
        with database(self.path) as db:targets={r['session']:r['target'] for r in db.execute('SELECT * FROM sessions')}
        with database(self.path) as db:receipts=[dict(r) for r in db.execute('SELECT id,target,state,receipt,created FROM sends ORDER BY created DESC LIMIT 100')]
        for row in rows:
            row['target']=targets.get(row['sessionId'],'原生会话 '+row['sessionId'][-8:])
            row['sendReceipts']=[r for r in receipts if row['id'] in r['id']][:5]
        return rows

    def execute(self,action,params,identity=None):
        from persona_library import PersonaLibrary
        from social_context import SocialContext
        if action=='status':return {'owner':Governance(self.root).owner_id(),'permissions':Governance(self.root).owner_capabilities(desktop=bool(identity and identity['desktop']))}
        if action in ('pause','resume'):return Governance(self.root).save({'paused':action=='pause'})
        if action.startswith('memory_'):
            from memory_store import MemoryStore
            scope=str((identity or {}).get('scope','owner'))
            if scope.startswith('chat:group:'):scope=':'.join(scope.split(':')[:3])
            elif scope.startswith('control:group:'):scope='chat:'+':'.join(scope.split(':')[1:3])
            else:scope='owner'
            store=MemoryStore(self.root,'qq')
            if action=='memory_search':return store.search(params.get('query',''),scope=scope,limit=30)
            capability='memory.write' if action=='memory_save' else 'memory.delete'
            if identity and not identity['desktop'] and not Governance(self.root).permitted(identity['sender'],capability):raise PermissionError('主人尚未启用此记忆管理权限')
            if action=='memory_save':return store.save(params['text'],category=params.get('category','约定'),scope=scope,source='manual',source_text='主人在管理会话中明确保存')
            if action=='memory_delete':
                if not any(x['id']==params['id'] for x in store.search(scope=scope,limit=200)):raise ValueError('当前范围内未找到此记忆')
                return store.delete(params['id'])
        if action=='personas':return PersonaLibrary(self.root).list()
        if action=='persona_import':return [{'id':p['id'],'name':p['name']} for p in PersonaLibrary(self.root).import_text(params['text'],name=params.get('name','导入人格'),source=params.get('source','主人对话'))]
        if action=='persona_apply':return PersonaLibrary(self.root).apply(params['revision'],params.get('target','owner'),new=bool(params.get('new',False)))
        if action=='message_send':
            from owner_computer import authorized
            if not identity or not (identity.get('desktop') and identity.get('owner') or authorized(self.root,identity)):
                raise PermissionError('单次交办发送仅用于已核验主人工作入口')
            return self._deliver(self.validate_target(params.get('target','owner')),params['text'],params['request_id'])
        if action.startswith('group_'):
            from group_tests import GroupTests
            from group_policy import GroupPolicy
            group=str(params.get('group_id',''))
            self.validate_target('group:'+group)
            trial=GroupTests(self.root).current(group)
            policy=GroupTests(self.root).policy(trial) if trial else GroupPolicy(self.root)
            return policy.config('group:'+group) if action=='group_get' else policy.configure('group:'+group,params['settings'])
        if action=='schedule_list':return self.schedules()
        if action=='schedule_create':
            session=asyncio.run(self._session(params.get('target','owner')))
            request={k:params[k] for k in ('title','prompt','after_seconds','at','every_seconds','daily','weekly','cron') if k in params}
            return self.native({'action':'create','session':session,'request':request})
        if action in ('schedule_delete','schedule_update'):
            rows=self.schedules();record=next((r for r in rows if r['id']==params['id']),None)
            if not record:raise ValueError('任务不存在或已删除')
            if action=='schedule_delete':return self.native({'action':'delete','session':record['sessionId'],'id':record['id']})
            expected={k:v for k,v in record.items() if k not in ('sessionId','status','lastDelivery','target','sendReceipts')}
            request={'sessionId':record['sessionId'],'id':record['id'],'expected':expected}
            request.update({k:params[k] for k in ('title','prompt','change') if k in params})
            result=self.native({'action':'update','request':request})
            if result.get('code'):raise ValueError('定时任务未更新：'+result['code'])
            return result
        raise ValueError('未知管理操作')

    def send(self,identity,text,request_id):
        if not identity['scope'].startswith('control:'):raise PermissionError('此发送器仅用于已绑定目标的定时会话')
        target=self.validate_target(identity['scope'][8:])
        with database(self.path) as db:
            row=db.execute('SELECT * FROM sessions WHERE target=? AND session=?',(target,identity['session'])).fetchone()
            if not row:raise PermissionError('定时会话绑定不匹配')
        return self._deliver(target,text,request_id)

    def _deliver(self,target,text,request_id):
        if not isinstance(text,str) or not text.strip():raise ValueError('消息不能为空')
        if not isinstance(request_id,str) or not request_id.strip():raise ValueError('需要本次执行的唯一发送编号')
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT * FROM sends WHERE id=?',(request_id,)).fetchone()
            if previous:
                if previous['target']!=target or previous['text']!=text:raise ValueError('发送编号已用于其他内容')
                return {'state':previous['state'],'receipt':json.loads(previous['receipt'] or '{}')}
            db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?)',(request_id,target,text,'EXECUTING','{}',time.time()))
        async def deliver():
            from qq_agent_loop import ChannelClient
            from secrets_store import SecretStore
            async def ignore(event):pass
            client=ChannelClient('ws://127.0.0.1:3101/ws',SecretStore(self.root).get('channel-core','FISH_QQ_TOKEN'),ignore)
            await client.start()
            try:
                for _ in range(100):
                    if client.bot:break
                    await asyncio.sleep(.05)
                return await client.action('send_message',{'chat_id':target,'segments':[{'type':'text','text':text}],
                    'idempotency_key':'scheduled:'+request_id})
            finally:await client.stop()
        try:
            receipt=asyncio.run(deliver());state='SUCCEEDED' if receipt.get('ok') else 'FAILED'
        except Exception as exc:receipt={'error':type(exc).__name__};state='UNKNOWN'
        with database(self.path) as db:db.execute('UPDATE sends SET state=?,receipt=? WHERE id=?',(state,json.dumps(receipt,ensure_ascii=False),request_id))
        return {'state':state,'target':target,'receipt':receipt}
