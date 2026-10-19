"""Verified-owner QQ persona tests; every queued message pins its test space."""
import asyncio
import uuid
from social_context import SocialContext
from memory_store import MemoryStore
from multimodal import MediaService
from runtime_adapter import DSHRuntimeAdapter
from tool_host import bind_session


async def command(loop, payload, sender):
    text = ''.join(s.get('text', '') for s in payload.get('segments', []) if s.get('type') == 'text').strip()
    verb, _, arg = text.partition(' ')
    verbs = ('/人格版本', '/测试窗口', '/新建测试', '/切换测试', '/结束测试', '/认可人格')
    if verb not in verbs or payload.get('chat_type') != 'private' or not loop.governance.is_owner(sender) or payload.get('chat_id') != 'private:' + str(sender):
        return False
    store = SocialContext(loop.root)
    try:
        capability='persona.approve' if verb=='/认可人格' else 'persona.test'
        if not loop.governance.permitted(sender,capability):
            raise PermissionError('主人账号尚未启用此人格测试权限，请在系统设置调整')
        if verb == '/人格版本':
            rows = store.personas('qq') or [store.ensure_default('qq')]
            response = '\n'.join(f"{r['id']} · {r['name']} · {'已认可' if r['approved'] else '待主人测试认可'}" for r in rows[:30])
        elif verb == '/测试窗口':
            current = store.owner_space(sender)
            response = '当前：' + (current['name'] + ' ' + current['id'] if current else '日常私聊') + '\n'
            response += '\n'.join(f"{r['id']} · {r['name']} · 版本 {r['revision'][:8]}" for r in store.spaces(origin='owner-qq')[:30])
        elif verb == '/新建测试':
            revision = store.resolve(arg.strip()) if arg.strip() else store.ensure_default('qq')['id']
            p = store.persona(revision)
            space = store.create_space('qq', p['name'] + ' · 主人测试', revision=revision, origin='owner-qq')
            store.select_owner_space(space['id'], sender)
            response = f"已进入独立测试窗口 {space['id']}\n人格：{p['name']} · {revision}\n接下来的消息进入此窗口。结束用 /结束测试；这不会开启群回复。"
        elif verb == '/切换测试':
            space = store.select_owner_space(store.resolve(arg.strip(), kind='spaces'), sender)
            response = f"当前独立测试：{space['name']} · {space['id']}\n人格版本 {space['revision']}"
        elif verb == '/结束测试':
            store.leave_owner_space(sender)
            response = '已返回日常私聊。测试记录保留在原窗口。'
        else:
            p = store.approve(store.resolve(arg.strip()), sender)
            response = f"已记录主人对人格 {p['name']} · {p['id']} 的认可。群回复仍需单独发布并开启。"
    except (ValueError, PermissionError) as exc:
        response = str(exc)
    await loop.channel.action('send_message', {'chat_id': payload['chat_id'], 'segments': [{'type': 'text', 'text': response}],
        'idempotency_key': 'owner-test-command:' + str(payload.get('message_id', uuid.uuid4().hex))})
    return True


async def process(loop, item):
    contexts = SocialContext(loop.root)
    space = contexts.space(item['space_id'])
    mid, conv = item['message_id'], item['conversation']
    turn = 'owner-test-' + uuid.uuid4().hex
    loop.cancel_event = asyncio.Event()
    loop.active_turn = turn
    session = space['session']
    try:
        contexts.require_owner(loop.owner)
        if not loop.governance.permitted(loop.owner,'persona.test'):raise PermissionError('人格测试权限已关闭')
        client = await asyncio.to_thread(DSHRuntimeAdapter(loop.root, 'qq', loop.runtime).connect,
                                         loop.cancel_event, require_isolation=True)
        await client.auth()
        if not session:
            session = await client.create_session(cwd=str(contexts.directory(space['id'])))
            contexts.bind_runtime(space['id'], session)
            loop.store.set_session(conv['id'], session)
        loop.active_test_session = session
        loop.store.create_turn(turn, conv['id'], session, turn)
        await asyncio.to_thread(bind_session, loop.root, 'qq', session, space['scope'], loop.owner)
        baseline = await client.baseline(session)
        media = MediaService(loop.root, 'qq')
        media.directory = contexts.directory(space['id']) / 'media'
        prompt = await asyncio.to_thread(media.prepare_content, item['payload'].get('segments', []), item['text'], cancelled=loop.cancel_event)
        prompt[0]['text'] = contexts.prompt(space['id']) + loop.governance.identity_prompt(loop.owner) + MemoryStore(loop.root, 'qq').context(item['text'], space['scope']) + prompt[0]['text']
        if loop.cancel_event.is_set():
            raise InterruptedError('已取消')
        loop.governance.consume('calls')
        await client.prompt(session, prompt)
        result = await client.wait_response(session, baseline, timeout=180, cancel_event=loop.cancel_event)
        if result.get('cancelled') or loop.cancel_event.is_set():
            raise InterruptedError('已取消')
        if not result.get('ok'):
            raise RuntimeError(result.get('error', '未取得完整回复'))
        contexts.require_owner(loop.owner)
        if not loop.governance.permitted(loop.owner,'persona.test'):raise PermissionError('人格测试权限已关闭，未发送')
        if loop.governance.config()['paused']:
            raise PermissionError('主人已暂停，测试回复未发送')
        response = str(result['text']).strip()
        loop.governance.check_output(response)
        segments = [{'type': 'text', 'text': response}]
        outbound = loop.store.record_message(conv['id'], item['payload']['chat_id'], 'bot', 'outbound', '', None, segments, response, 'pending', runtime_session_id=session, runtime_turn_id=turn)
        delivered = await loop._deliver_reply(item['payload']['chat_id'], segments, turn, cancelled=loop.cancel_event)
        if not delivered.get('ok'):
            loop.store.update_message(outbound, status='failed', error='测试回复发送失败，未自动重试')
            raise RuntimeError('测试回复发送失败，未自动重试')
        remote = str(delivered.get('payload', {}).get('message_id', ''))
        loop.store.update_message(outbound, status='sent', channel_message_id=remote)
        loop.store.update_message(mid, status='processed', runtime_session_id=session, runtime_turn_id=turn)
        loop.store.finish_turn(turn, 'done', assistant_message_id=remote)
        contexts.record_owner_reply(space['id'], loop.owner)
        MemoryStore(loop.root, 'qq').enqueue('qq-test:' + str(mid), item['text'], space['scope'],subject='qq:'+loop.owner)
    except asyncio.CancelledError:
        loop.cancel_event.set()
        if session: await asyncio.to_thread(loop.runtime.cancel,'qq',session)
        loop.store.update_message(mid,status='cancelled',cancelled=True)
        loop.store.finish_turn(turn,'cancelled',error='主人取消或客户端退出')
        raise
    except Exception as exc:
        from dsh_web import safe_runtime_error
        status = 'cancelled' if isinstance(exc, InterruptedError) else 'blocked' if isinstance(exc, PermissionError) else 'failed'
        error = safe_runtime_error(str(exc))
        loop.store.update_message(mid, status=status, error=error)
        loop.store.finish_turn(turn, status, error=error)
    finally:
        loop.active_turn = None
        loop.active_test_session = None
