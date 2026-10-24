"""Private/group expression context and asynchronous learning from human messages."""
import json
import re
import threading
import time
from pathlib import Path
from storage import database

_learning=set()
_lock=threading.Lock()
_SIGNATURE=re.compile(r'白饭|鲸鲸|尾巴|鱼|🐟|水池|池子|游(?:过来|出去|两圈|来|回)')


def expression_examples(root,text):
    if re.search(r'认真|解释|原理|介绍|分析|写|编(?:一|个)|讲个|为什么|如何',text):return ''
    from storage import read_json
    config=read_json(Path(root)/'harness/chat-expression-examples.json',{})
    rows=[{k:row[k] for k in ('situation','guidance','dialogue')} for row in config.get('examples',[])
        if re.search(row['pattern'],text)][:1]
    if not rows:return ''
    return ('\n[本轮相关的口语表达关系示例；是虚构示例，不是本会话真实历史，不改变当前角色。'
        '只参考接话方式，用当前话题自己的词，不照搬台词。]\n'+json.dumps(rows,ensure_ascii=False)+'\n')


def repetition_context(text,replies):
    recent=[str(value) for value in replies[-4:]]
    if _SIGNATURE.search(text):return ''
    if re.search(r'认真|解释|介绍|分析|写.{0,8}(文|代码)|为什么|如何|你是谁|你是.{0,5}鱼|白饭|鲸鲸|尾巴',text):return ''
    if sum(bool(_SIGNATURE.search(value)) for value in recent)<2:return ''
    return ('\n[本轮表达节奏] 近几轮已经反复用了鱼、白饭等身份意象。'
        '这一轮直接沿当前话题接话，不再额外拐回身份、食物或固定签名；保留当前角色的口吻即可。\n')


def private_context(root,text,*,conversation='',desktop=False,window=''):
    root=Path(root)
    path=root/('data/qq/desktop-chat.db' if desktop else 'data/qq/qq.db')
    replies=[];human=[]
    if path.is_file():
        with database(path) as db:
            rows=[dict(r) for r in db.execute('SELECT direction,text FROM messages WHERE conversation_id=? '
                'ORDER BY id DESC LIMIT 12',(conversation,))]
        rows.reverse()
        replies=[r['text'] for r in rows if r['direction']=='outbound']
        human=[r['text'] for r in rows if r['direction']=='inbound'][-3:]
    from governance import Governance
    from group_language import GroupLanguage
    chat='desktop:'+conversation if desktop else 'private:'+Governance(root).owner_id()
    reference=GroupLanguage(root).context(chat,window,text,'\n'.join(human))
    return reference+expression_examples(root,text)+repetition_context(text,replies)


def learn_private_async(root,conversation):
    """Batch only real owner's new messages, off the reply's critical path."""
    root=Path(root);key=(str(root.resolve()),conversation)
    with _lock:
        if key in _learning:return
        _learning.add(key)
    def run():
        try:
            from governance import Governance
            from group_language import GroupLanguage
            from model_gateway import ModelGateway
            owner=Governance(root).owner_id();chat='private:'+owner;store=GroupLanguage(root)
            with database(store.path) as db:
                state=db.execute('SELECT * FROM learning_state WHERE chat=? AND window=?',(chat,'')).fetchone()
            if state and time.time()-state['updated']<300:return
            through=state['through'] if state else 0
            with database(root/'data/qq/qq.db') as db:
                rows=[dict(r) for r in db.execute('SELECT id,text,channel_message_id FROM messages '
                    "WHERE conversation_id=? AND direction='inbound' AND sender_id=? AND id>? ORDER BY id DESC LIMIT 12",
                    (conversation,owner,through))]
            if len(rows)<8:return
            rows.reverse()
            with database(root/'data/qq/memory.db') as db:
                excluded={r[0] for r in db.execute('SELECT source FROM exclusions')}
            messages=[{'message_id':str(r['channel_message_id'] or 'local:'+str(r['id'])),
                'sender':{'id':owner},'text':r['text']} for r in rows
                if 'qq:'+str(r['id']) not in excluded and 'qq:'+str(r['channel_message_id']) not in excluded
                and not r['text'].lstrip().startswith('/') and not re.search(r'不.{0,6}(?:保存|记忆)|临时扮演|收尾测试',r['text'])]
            if len(messages)>=3:
                result=ModelGateway(root,'qq').json(
                    '只从给出的真人消息学习可复用的说话方式或他明确解释的会话用语。不要生成回复，不学习机器人发言。'
                    '表达用“适用情境”和“可以怎样表达”描述，去掉具体人名和角色名，不复制提问指令或办事任务。'
                    '每条绑定实际message_id；没有明确表达特点就空数组。最多3条。'
                    '只输出JSON {"language":[{"message_id":"原编号","kind":"expression/term",'
                    '"trigger":"适用情境或原词，最多40字","content":"说话方式或已解释含义，最多100字"}]}。',
                    json.dumps(messages,ensure_ascii=False),origin='private-expression-learning')
                store.learn({'chat_id':chat,'messages':messages},result.get('language',[]),owner)
            with database(store.path) as db:
                db.execute('INSERT OR REPLACE INTO learning_state VALUES(?,?,?,?)',(chat,'',rows[-1]['id'],time.time()))
        except Exception as exc:
            from dsh_web import safe_runtime_error
            from governance import Governance
            Governance(root).audit('private_expression_learning_failed',safe_runtime_error(str(exc)))
        finally:
            with _lock:_learning.discard(key)
    threading.Thread(target=run,name='fish-private-expression',daemon=True).start()
