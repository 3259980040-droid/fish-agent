"""Owner QQ intake and result delivery for the same independent Fish Agent."""
import asyncio
import threading
import uuid
from desktop_context import DesktopContext
from governance import Governance
from memory_store import MemoryStore
from multimodal import MediaService
from social_context import SocialContext
from storage import database

ACTIVE = ('queued', 'starting', 'running', 'cancel_requested')
WINDOW_CHANGED = '主人私聊窗口或人格已切换，本轮未继续发送'
MEDIA_LABELS = {'image': '一张图片', 'audio': '一段语音', 'video': '一段视频'}


def note_media_gaps(segments, content, policy):
    """Unread media becomes one explicit note in the prompt, never a failed turn.

    owner_private and desktop_chat share this wording so both channels tell the
    owner the same thing: the picture did not arrive, keep talking normally.
    """
    for segment in segments:
        kind = str(segment.get('type') or '')
        label = MEDIA_LABELS.get(kind)
        if not label:
            continue
        error = str(segment.get('error') or '').strip()
        if not error and not segment.get('local_path'):
            error = '没有取得可读内容'
        if not error:
            continue
        detail = error.split(' for url')[0].strip()[:120]
        policy.audit('owner_media_unavailable', kind + '：' + detail)
        note = f'[{label}没有成功读取：{detail}。请照常回话，如实说明这部分没有看到，不要猜测或编造内容。]'
        # 媒体层自己写的那句英文提示换成这句话；content[0] 是主人原话，不动。
        for part in content[1:]:
            if part.get('type') == 'text' and error in str(part.get('text') or ''):
                part['text'] = note
                break
        else:
            content.append({'type': 'text', 'text': note})


def window_current(loop, item):
    """Only the real owner's identity affects this route, not record/persona edits."""
    return loop.governance.is_owner(loop.owner)


def task_call(root, action, params):
    from tool_host import request
    return request(root, '/owner-private/tasks', {'action': action, 'params': params}, role='qq', timeout=90)


def task_text(value):
    labels = {'queued': '等待执行', 'starting': '正在启动', 'running': '执行中',
              'cancel_requested': '正在请求停止', 'cancelled': '子任务已停止，已有文件修改保留',
              'completed': '执行结束', 'failed': '执行失败', 'unknown': '执行结果未确认，未自动重跑'}
    text = '任务 ' + value['id'] + '：' + labels[value['status']]
    if value.get('error'):
        text += '\n' + value['error']
    if value['status'] == 'completed':
        text += '\n成果：' + ('已归档' if value['artifact_status'] in ('ready', 'served') else '核查失败')
        latest = {r['artifact']: r for r in value.get('deliveries', [])}
        for artifact in value['artifacts']:
            state = latest.get(artifact['id'], {}).get('state', 'pending')
            text += '\n' + artifact['name'] + '：' + {'sent': 'QQ上传已确认', 'failed': '上传失败，可用 /重发成果 重发原文件',
                'unknown': '上传结果未确认，请先核对QQ，未自动重发', 'sending': '正在上传', 'pending': '等待上传'}[state]
    return text


def generate(loop, item):
    from model_gateway import ModelGateway
    from conversation_style import private_context
    from fish_chat import respond
    contexts = SocialContext(loop.root)
    persona = contexts.active_persona('qq')
    item['_reply_revision'] = persona['id']
    media = MediaService(loop.root, 'qq')
    segments = item['payload'].get('segments', [])
    current = media.prepare_content(segments, item['text'], cancelled=loop.private_cancel, include_sources=True, source_reference='qq:' + str(item['message_id']))
    note_media_gaps(segments, current, loop.governance)
    loop.store.update_message(item['message_id'], segments=segments)
    context = DesktopContext(loop.root, loop.store.db_path, channel='qq')
    context.remember_input(item['message_id'], current)
    cid = item['conversation']['id']
    from experience import ExperienceTurn
    experience = ExperienceTurn(loop.root, loop.store.db_path, cid, item['message_id'], channel='qq')
    references = private_context(loop.root, item['text'], conversation=cid)
    # 网页检索交给模型用 fish_web_search / fish_web_fetch 自主决定，避免程序与模型双搜双计费。
    memory = MemoryStore(loop.root, 'qq')
    loop.governance.consume('calls')
    emoji_state = {'previewed':set(),'tools':[]}
    # The verified source is this loop's owner and the stored inbound message,
    # never the model or the payload.
    from tool_host import bind_session
    session = 'qq-private:' + cid
    try:
        bind_session(loop.root, 'qq', session, 'owner', loop.owner, False, chat_mode='owner')
    except Exception as exc:
        raise RuntimeError('本次主人私聊没有取得工具服务的会话身份，未生成回复：' + str(exc)) from exc
    from chat_output import TurnSilent, decode_reply
    try:
        raw = respond(loop.root, 'qq', persona=persona, history=[], history_source={},
            memory=memory.context(item['text'], 'owner'), scope='owner', references=references,
            current=media.chat_completions_content(current), session=session,
            cancelled=loop.private_cancel, origin='qq-private-chat:' + cid, output='bubbles',
            emoji_state=emoji_state, tool_context='owner-private', experience=experience)
    except TurnSilent:
        # 她这一轮想过了但没说：思考已经落进 turn_no_send 审计，这里什么都不发。
        return []
    return decode_reply(loop.root, raw, previewed=emoji_state['previewed'])


async def reply(loop, item, text, key, *, turn='', inbound_status='processed', _system_fault=False):
    if loop.private_cancel.is_set():
        raise InterruptedError('本轮回复已取消')
    cid, target = item['conversation']['id'], item['payload']['chat_id']
    from chat_output import delivery_plan
    bubbles = [{'kind': 'text', 'text': text}] if isinstance(text, str) else text
    plan = delivery_plan(bubbles)
    outbound = [loop.store.record_message(cid, target, 'bot', 'outbound', '', None,
        entry['segments'], bubble.get('text') or '[表情:'+bubble['emoji_id']+']', 'pending', runtime_turn_id=turn) for bubble, entry in zip(bubbles, plan)]
    try:
        # A system fault receipt has no persona window or fabricated inbound message.
        # It still uses this sender, its owner-only target and the ordinary send gate.
        validate = (lambda: target == 'private:' + loop.owner and loop.governance.is_owner(loop.owner)
                    and not loop.governance.config()['paused']) if _system_fault else lambda: window_current(loop, item)
        from experience import delivery
        if not _system_fault:
            delivery(loop.root, 'qq:' + str(item['message_id']), sending=True)
        result = await loop._deliver_reply(target, [], key, loop.private_cancel,
            validate=validate, validation_error=WINDOW_CHANGED, bubble_plan=plan)
    except Exception as exc:
        for mid in outbound:
            loop.store.update_message(mid, status='blocked' if isinstance(exc, PermissionError) else 'failed', error=str(exc))
        raise
    error = (result.get('error') or {}).get('code', '')
    receipt = str((result.get('payload') or {}).get('message_id', ''))
    detail = result.get('error') or {}
    interrupted = bool(detail.get('validation_failed') or detail.get('cancelled'))
    state = ('sent' if result.get('ok') else 'uncertain' if detail.get('may_have_sent')
             else 'cancelled' if interrupted else 'blocked' if detail.get('blocked') else 'failed')
    receipts = (result.get('payload') or {}).get('bubbles', [])
    for index, mid in enumerate(outbound):
        detail_bubble = receipts[index] if index < len(receipts) else {}
        bubble_state = detail_bubble.get('state', state)
        bubble_error = error
        if bubble_state == 'not_sent':
            bubble_state = 'cancelled' if interrupted else 'blocked' if detail.get('blocked') else 'failed'
            bubble_error = ('本轮中止，本条未尝试发送：' if interrupted else '前序发送未完成，本条未尝试发送：') + error
        elif bubble_state == 'partial':
            bubble_state = 'uncertain'
        ids = detail_bubble.get('message_ids', [])
        loop.store.update_message(mid, status=bubble_state, error=bubble_error if bubble_state != 'sent' else '',
            channel_message_id=ids[-1] if ids else '', cancelled=bubble_state == 'cancelled',
            segments=[{**part, 'delivery': detail_bubble} for part in plan[index]['segments']])
    if not _system_fault:
        from experience import delivery
        delivery(loop.root,'qq:'+str(item['message_id']),outcome=state,error=error)
    if _system_fault:
        item['_fault_receipt'] = {'sent': bool(result.get('ok')), 'uncertain': state == 'uncertain'}
    else:
        loop.store.update_message(item['message_id'], status=inbound_status if result.get('ok') else state, error=error,
                                  cancelled=state == 'cancelled')
    if turn:
        loop.store.finish_turn(turn, 'done' if result.get('ok') else 'cancelled' if state == 'cancelled' else 'send_failed',
                               error=error, assistant_message_id=receipt)
    return result.get('ok', False)


async def fault_notice(loop, fault, token, *, recovered=False):
    """Explicit system notice, never a model utterance or a second send channel."""
    if not loop.owner or not loop.governance.is_owner(loop.owner):
        return {'sent': False, 'uncertain': False}
    cfg = loop.governance.config()
    if cfg['paused'] or not cfg['send_policy']['owner_private']['owner_reply']:
        return {'sent': False, 'uncertain': False}
    conversation = loop.conversation or loop.store.get_or_create_conversation('private:' + loop.owner, loop.agent_id)
    item = {'conversation': conversation, 'payload': {'chat_id': 'private:' + loop.owner}}
    label = '本阶段已恢复' if recovered else '故障'
    recovery = '此阶段后续调用已经实际成功，连续失败已清零。'
    if fault.get('uncertain'):
        recovery += '原条消息的送达结果仍未确认，没有自动重发。'
    text = ('系统故障通知：' + label + '\n来源：' + fault['source'] + '\n阶段：' + fault['stage']
            + '\n' + (recovery if recovered else fault['reason'])
            + '\n累计失败：' + str(fault['total']) + ' 次；连续失败：' + str(fault['consecutive'])
            + ' 次。详情可在 Fish 设置的故障状态查看。')
    await reply(loop, item, text, 'system-fault:' + token, _system_fault=True)
    return item.get('_fault_receipt', {'sent': False, 'uncertain': False})


async def process(loop, item):
    # Re-check stored source, not a model-provided identity or a persona field.
    with database(loop.store.db_path) as db:
        source = db.execute('SELECT * FROM messages WHERE id=?', (item['message_id'],)).fetchone()
    if (not source or source['direction'] != 'inbound' or source['chat_id'] != 'private:' + loop.owner
            or source['conversation_id'] != item['conversation']['id'] or not loop.governance.is_owner(source['sender_id'])
            or item['payload'].get('chat_id') != source['chat_id']):
        raise PermissionError('主人私聊来源不匹配')
    loop.private_cancel = threading.Event()
    loop.cancel_event = loop.private_cancel
    if not window_current(loop, item):
        loop.store.update_message(item['message_id'], status='cancelled', cancelled=True, error=WINDOW_CHANGED)
        return
    from owner_intent import route
    intent = route(item['text'])
    if intent['kind']=='FISH_TASK':
        action = intent['action']
        value = await asyncio.to_thread(task_call,loop.root,action,{'message':item['message_id'],'task':intent['task'],'retry':action=='deliver'})
        await reply(loop,item,'系统任务状态：\n'+task_text(value),'owner-task-control:'+str(item['message_id']))
        return
    if intent['kind'] == 'CLARIFY':
        await reply(loop, item, intent['text'], 'owner-task-clarify:' + str(item['message_id']))
        return
    turn = 'fish-private-' + uuid.uuid4().hex
    loop.active_turn = turn
    loop.store.create_turn(turn, item['conversation']['id'], '', turn)
    loop.store.update_message(item['message_id'], runtime_turn_id=turn)
    try:
        text = await asyncio.to_thread(generate, loop, item)
        if not text:
            # 沉默是她做过的一个决定，不是一次失败的生成：收下这条消息，什么都不发。
            loop.store.update_message(item['message_id'], status='processed')
            loop.store.finish_turn(turn, 'silent')
            return
        if await reply(loop, item, text, 'qq-agent:' + turn, turn=turn):
            if item.get('space_id'):
                SocialContext(loop.root).record_owner_reply(item['space_id'], loop.owner)
            try:
                MemoryStore(loop.root, 'qq').enqueue('qq:' + str(item['message_id']), item['text'], 'owner', subject='qq:' + loop.owner)
            except Exception as exc:
                loop.governance.audit('owner_memory_enqueue_failed', type(exc).__name__)
    except Exception as exc:
        if not window_current(loop, item):
            exc = InterruptedError(WINDOW_CHANGED)
        loop._finish_failed(item['message_id'], turn, '', exc)
        await loop._failure_notice(item, exc, turn, validate=lambda: window_current(loop, item))
    finally:
        loop.active_turn = None


async def deliveries(loop):
    """Deliver accepted task results once; never replay uncertain files or messages."""
    with database(loop.store.db_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS task_notices(task TEXT PRIMARY KEY,state TEXT)')
        db.execute("UPDATE task_notices SET state='unknown' WHERE state='sending'")
    from bot_control import BotControl
    control = BotControl(loop.root)
    while not loop._stop.is_set():
        try:
            rows = await asyncio.to_thread(task_call,loop.root,'list',{})
            for value in rows['tasks']:
                if value['status'] in ACTIVE:
                    continue
                target = value['config'].get('notify_target')
                if not target or not str(target).startswith(('private:','group:')):
                    continue
                if value['status']=='completed' and value['artifact_status'] in ('ready','served'):
                    if any(not any(r['artifact']==a['id'] for r in value['deliveries']) for a in value['artifacts']):
                        value = await asyncio.to_thread(task_call,loop.root,'deliver',{'task':value['id']})
                with database(loop.store.db_path) as db:
                    changed = db.execute("INSERT OR IGNORE INTO task_notices VALUES(?,'sending')",(value['id'],)).rowcount
                if not changed:
                    continue
                direct = value['config'].get('direct_deliveries') or []
                if value['status']=='completed' and any(row.get('state') in ('SUCCEEDED','sent','UNKNOWN','uncertain') for row in direct):
                    # This child already contacted the same recipient. Keep its
                    # final result on the task page; do not repeat it as a notice.
                    state = 'uncertain' if any(row.get('state') in ('UNKNOWN','uncertain') for row in direct) else 'sent'
                    with database(loop.store.db_path) as db:
                        db.execute('UPDATE task_notices SET state=? WHERE task=?',(state,value['id']))
                    continue
                status_text = '系统任务结果：\n'+task_text(value)
                bubbles = [{'kind':'text','text':status_text}]
                if value['status']=='completed':
                    bubbles.extend(value['config'].get('result_bubbles') or ([{'kind':'text','text':value['result']}] if value['result'] else []))
                try:
                    result = await asyncio.to_thread(control.deliver_bubbles,target,bubbles,'owner-task-result:'+value['id'],
                        source='task-result',context='owner:task:'+value['id'])
                    state = {'SUCCEEDED':'sent','UNKNOWN':'uncertain'}.get(result['state'],'failed')
                except Exception as exc:
                    state = 'uncertain' if not isinstance(exc,(ValueError,PermissionError)) else 'failed'
                    await loop._fault('task-result:'+value['id'],exc,stage='delivery',uncertain=state=='uncertain')
                with database(loop.store.db_path) as db:
                    db.execute('UPDATE task_notices SET state=? WHERE task=?',(state,value['id']))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await loop._fault('task-result-monitor',exc,stage='delivery')
        await asyncio.sleep(2)
