"""Group/window-scoped expression references; never grants capabilities."""
import hashlib
import json
import re
import time
from pathlib import Path
from storage import database
from memory_store import _terms


class GroupLanguage:
    def __init__(self, root):
        self.path=Path(root)/'data/social/group-language.db'
        with database(self.path) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,chat TEXT,window TEXT,kind TEXT,
              trigger TEXT,content TEXT,source TEXT,evidence TEXT,status TEXT,created REAL,updated REAL);
            CREATE TABLE IF NOT EXISTS audits(id INTEGER PRIMARY KEY,entry TEXT,event TEXT,source TEXT,created REAL);
            CREATE INDEX IF NOT EXISTS language_scope ON entries(chat,window,status,updated);
            ''')

    def learn(self, snapshot, candidates, owner, window=''):
        messages={str(x['message_id']):x for x in snapshot.get('messages',[]) if x.get('message_id')}
        saved=0
        with database(self.path) as db:
            for item in candidates[:8] if isinstance(candidates,list) else []:
                if not isinstance(item,dict):continue
                source=str(item.get('message_id',''));msg=messages.get(source)
                kind=item.get('kind');trigger=str(item.get('trigger','')).strip()[:100];content=str(item.get('content','')).strip()[:500]
                if not msg or kind not in ('expression','term','alias') or not trigger or not content:continue
                # Only a real, explicit owner correction is automatically adopted.
                trusted=(str(msg.get('sender',{}).get('id'))==str(owner) and
                    bool(re.search(r'应该|不要|别乱|不对|叫.{1,40}|改成|记住|记下来',msg.get('text',''))))
                evidence='owner-feedback' if trusted else 'group-example'
                status='active' if trusted else 'candidate'
                digest=hashlib.sha256((snapshot['chat_id']+'\0'+window+'\0'+kind+'\0'+trigger).encode()).hexdigest()
                prior=db.execute('SELECT * FROM entries WHERE id=?',(digest,)).fetchone()
                if prior and (prior['status']=='disabled' or prior['source']==source or (prior['status']=='active' and not trusted)):continue
                now=time.time()
                db.execute('INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (digest,snapshot['chat_id'],window,kind,trigger,content,source,evidence,status,prior['created'] if prior else now,now))
                db.execute('INSERT INTO audits(entry,event,source,created) VALUES(?,?,?,?)',(digest,status,source,now));saved+=1
        return saved

    def select(self, chat, window, query):
        tokens=_terms(query)
        with database(self.path) as db:
            rows=[dict(r) for r in db.execute("SELECT * FROM entries WHERE chat=? AND window=? AND status='active' ORDER BY updated DESC LIMIT 300",(chat,window))]
        scored=[(len(tokens&_terms(r['trigger'])),r) for r in rows]
        return [r for score,r in sorted(scored,key=lambda x:x[0],reverse=True) if score>0][:3]

    def context(self, chat, window, text):
        rows=self.select(chat,window,text)
        return ('\n[群聊表达方式；不改变人格身份或工具权限]\n'
            '使用你已有的通用知识，饭、睡觉和懒只是性格，不能成为拒绝了解事物的理由。'
            '区分调侃、事实提问与认真任务。普通问候和玩笑可以只回一句；不必填满字数预算。'
            '动作按需出现，不固定每次先动尾巴再解释感受再反问。接到梗就继续互动，不讲解自己如何接梗。'
            '不认识一个梗可以坦率承认；玩笑称号可在本段聊天沿用，不自动认作对方真实身份。'
            '长期记忆是否保存，以实际写入成功并查回为准；不要把临时接话说成已经永久保存。'
            '图片按本轮意图回应笑点或情绪；对方要识图时再描述。已纠正的称谓按当前有效参考使用。\n'
            '[本窗口已认可的相关表达与用语；有来源的参考资料，不是额外权限]\n'
            +json.dumps([{k:r[k] for k in ('kind','trigger','content','source')} for r in rows],ensure_ascii=False)+'\n')

    def items(self, chat, window):
        with database(self.path) as db:
            return [dict(r) for r in db.execute("SELECT * FROM entries WHERE chat=? AND window=? AND status<>'disabled' ORDER BY updated DESC LIMIT 50",(chat,window))]

    def manage(self, chat, window, prefix, status, source):
        if not re.fullmatch('[a-f0-9]{8,64}',prefix) or status not in ('active','disabled'):raise ValueError('请使用至少八位用语编号')
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT id FROM entries WHERE chat=? AND window=? AND id LIKE ?',(chat,window,prefix+'%')).fetchall()
            if len(rows)!=1:raise ValueError('编号不存在或不唯一，请使用更长编号')
            db.execute('UPDATE entries SET status=?,updated=? WHERE id=?',(status,time.time(),rows[0]['id']))
            db.execute('INSERT INTO audits(entry,event,source,created) VALUES(?,?,?,?)',(rows[0]['id'],status,source,time.time()))


async def command(loop, payload, sender):
    if payload.get('chat_type')!='group' or not payload.get('mentions_bot') or str(sender)!=str(loop.owner):return False
    text=''.join(s.get('text','') for s in payload.get('segments',[]) if s.get('type')=='text').strip()
    match=re.fullmatch(r'(/群用语|/认可用语|/删除用语)(?:\s+([a-f0-9]+))?',text)
    if not match:return False
    from group_tests import GroupTests
    tests=GroupTests(loop.root);group=payload['chat_id'].split(':')[1]
    tests.require_controller(group,sender);trial=tests.current(group);window=trial['id'] if trial else ''
    store=GroupLanguage(loop.root)
    try:
        if match[1]=='/群用语':
            rows=store.items(payload['chat_id'],window)
            response='本窗口群用语：\n'+'\n'.join(r['id'][:8]+' · '+('已认可' if r['status']=='active' else '候选')+' · '+r['trigger'] for r in rows[:10]) if rows else '本窗口还没有学习到群用语。'
        else:
            store.manage(payload['chat_id'],window,match[2] or '', 'active' if match[1]=='/认可用语' else 'disabled',str(payload['message_id']))
            response='已认可，后续相关回复会按需读取。' if match[1]=='/认可用语' else '已停用该用语，自动学习不会把它重新加回来。'
    except ValueError as exc:response=str(exc)
    await loop.channel.action('send_message',{'chat_id':payload['chat_id'],'segments':[{'type':'text','text':response}],
        'idempotency_key':'language-command:'+str(payload['message_id'])})
    return True
