"""Owner QQ private input and delivery for the same independent Fish Agent.

The QQ process no longer generates: it stores the inbound event, routes explicit
commands, and hands ordinary messages to the background host's one main execution.
Delivery of what that turn decided is a second, separate step: the channel adapter
sends the bubbles and records the real receipt.
"""
import asyncio
import threading
import uuid

from governance import Governance
from memory_store import MemoryStore
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


def submit(loop, item):
    """Hand one stored private event to the host's one main execution.

    Only the reference crosses the process boundary: the host reads the saved record
    itself and verifies the real sender, so no payload claim can become identity.
    """
    from tool_host import request
    body = {'source': 'qq-private', 'conversation': item['conversation']['id'],
            'message': item['message_id'], 'text': item['text'],
            'reference': 'qq:' + str(item['message_id']),
            'place': 'private:' + str(loop.owner),
            'payload': {'chat_id': item['payload'].get('chat_id', ''),
                        'reply_to': item['payload'].get('reply_to') or ''}}
    try:
        submission = request(loop.root, '/submit', body, role='qq', admin=True, timeout=30)
    except Exception as exc:
        loop.store.update_message(item['message_id'], status='failed',
                                  error='未能提交给 Fish 主执行：' + str(exc)[:300])
        raise
    loop.store.update_message(item['message_id'], status='submitted',
                              runtime_turn_id=submission.get('execution', ''))
    return submission


async def handle(loop, item):
    """One owner private input: verify its stored source, route commands, submit the rest."""
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
        return None
    from owner_intent import route
    intent = route(item['text'])
    if intent['kind'] == 'FISH_TASK':
        action = intent['action']
        value = await asyncio.to_thread(task_call, loop.root, action,
                                        {'message': item['message_id'], 'task': intent['task'],
                                         'retry': action == 'deliver'})
        await reply(loop, item, '系统任务状态：\n' + task_text(value), 'owner-task-control:' + str(item['message_id']))
        return None
    if intent['kind'] == 'CLARIFY':
        await reply(loop, item, intent['text'], 'owner-task-clarify:' + str(item['message_id']))
        return None
    submission = await asyncio.to_thread(submit, loop, item)
    # submit performs blocking transport work in a thread. The existing watcher
    # must be scheduled here, on the QQ event loop that owns its lifecycle.
    loop.watch_private(submission.get('execution', ''), item)
    return submission


async def reply(loop, item, text, key, *, turn='', inbound_status='processed'):
    if loop.private_cancel.is_set():
        raise InterruptedError('本轮回复已取消')
    cid, target = item['conversation']['id'], item['payload']['chat_id']
    from chat_output import delivery_plan
    bubbles = [{'kind': 'text', 'text': text}] if isinstance(text, str) else text
    plan = delivery_plan(bubbles)
    outbound = [loop.store.record_message(cid, target, 'bot', 'outbound', '', None,
        entry['segments'], bubble.get('text') or '[表情:'+bubble['emoji_id']+']', 'pending', runtime_turn_id=turn) for bubble, entry in zip(bubbles, plan)]
    try:
        validate = lambda: window_current(loop, item)
        from experience import delivery
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
    from experience import delivery
    delivery(loop.root,'qq:'+str(item['message_id']),outcome=state,error=error)
    loop.store.update_message(item['message_id'], status=inbound_status if result.get('ok') else state, error=error,
                              cancelled=state == 'cancelled')
    if turn:
        loop.store.finish_turn(turn, 'done' if result.get('ok') else 'cancelled' if state == 'cancelled' else 'send_failed',
                               error=error, assistant_message_id=receipt)
    return {'ok': bool(result.get('ok')), 'state': state, 'error': error,
            'receipt': {'message_id': receipt,
                        'message_ids': [item['message_ids'] for item in receipts if item.get('message_ids')]}}


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
