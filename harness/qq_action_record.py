"""Record real QQ tool side effects in the existing conversation stream."""
from qq_store import QQStore

SEND_ACTIONS = {'send_msg','send_private_msg','send_group_msg','send_forward_msg',
                'send_private_forward_msg','send_group_forward_msg','upload_private_file','upload_group_file'}


def begin(root,action,params,session=''):
    if action not in SEND_ACTIONS:
        return None
    kind = 'group' if params.get('group_id') else 'private' if params.get('user_id') else ''
    peer = params.get('group_id' if kind=='group' else 'user_id') if kind else None
    if not peer:
        return None
    store = QQStore(root)
    target = kind+':'+str(peer)
    conversation = store.get_or_create_conversation(target,'qq')
    message = params.get('message',params.get('messages',[]))
    parts = message if isinstance(message,list) else [{'type':'text','data':{'text':str(message)}}] if message else []
    parts = [{**p,'origin':'fish-qq-tool'} for p in parts if isinstance(p,dict)]
    if action.startswith('upload_'):
        parts.append({'type':'file','path':params.get('file'),'name':params.get('name'),'origin':'fish-qq-tool'})
    text = ''.join(str(p.get('text') or (p.get('data') or {}).get('text') or '') for p in parts)
    mid = store.record_message(conversation['id'],target,'Fish','outbound','',None,parts,text,'sending',session)
    effect = external_begin(root,session,'qq-message:'+str(mid),target,text) if session else None
    return store,mid,effect


def finish(record,receipt=None,error=None):
    if record is None:
        return None
    store,mid,effect = record
    receipt = receipt or {}
    data = receipt.get('data') or {}
    readback = receipt.get('readback') or {}
    message_id = str((data.get('message_id') if isinstance(data,dict) else '') or readback.get('message_id') or '')
    state = 'failed' if receipt.get('status')=='failed' or receipt.get('retcode',0)!=0 else 'sent' if message_id else 'uncertain'
    store.update_message(mid,status=state,channel_message_id=message_id,error='' if state=='sent' else str(error or 'QQ 尚未提供消息送达凭据'))
    external_finish(effect,state,receipt,message_id,error)
    return {'state':state,'message_id':message_id,'record':mid,'replayed':False}


def external_begin(root,session,key,target,text):
    from bot_control import BotControl
    from storage import database
    import json,time
    control = BotControl(root)
    from experience import reference_for_session,delivery
    reference = reference_for_session(root,session)
    if reference:delivery(root,reference,sending=True)
    occurrence = control.plans.active(session)
    if not occurrence:return (None,'',root,reference)
    rid = 'sched:'+occurrence['schedule_id']+':'+occurrence['slot']+':external:'+key
    with database(control.path) as db:
        db.execute('INSERT OR IGNORE INTO sends VALUES(?,?,?,?,?,?)',(rid,target,text,'EXECUTING',json.dumps({'note':'实际工具调用中，尚未取得送达凭据'}),time.time()))
    return control.path,rid,root,reference


def external_finish(effect,state,receipt,message_id='',error=None):
    if effect is None: return
    import json
    from storage import database
    path,rid,root,reference = effect
    from experience import delivery
    if reference:delivery(root,reference,outcome=state,error='' if state=='sent' else str(error or '工具发送未确认'))
    value = {'SUCCEEDED':'sent','UNKNOWN':'uncertain','FAILED':'failed'}
    code = next((code for code,name in value.items() if name==state),'UNKNOWN')
    if path is not None:
        with database(path) as db:
            db.execute('UPDATE sends SET state=?,receipt=? WHERE id=?',(code,json.dumps({'payload':{'message_id':message_id},'raw':receipt,'error':str(error or '')},ensure_ascii=False,default=str),rid))


def plugin_finish(effect,result=None,error=None):
    """A sending MCP call closes its occurrence even if its receipt is incomplete."""
    if effect is None:return
    import json
    ids=[];failed=False
    def walk(value):
        nonlocal failed
        if isinstance(value,dict):
            if value.get('isError') or value.get('ok') is False or value.get('status') in ('failed','rejected'):failed=True
            for key,item in value.items():
                if key in ('message_id','message_ids') and item:ids.extend(item if isinstance(item,list) else [item])
                elif key=='text' and isinstance(item,str):
                    try:walk(json.loads(item))
                    except (ValueError,TypeError):pass
                else:walk(item)
        elif isinstance(value,list):
            for item in value:walk(item)
    walk(result)
    external_finish(effect,'failed' if failed or error else 'sent' if ids else 'uncertain',result or {},str(ids[-1]) if ids else '',error)
