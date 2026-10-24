"""Verified-owner QQ persona selection; chat uses the shared Fish API path."""
import uuid
from social_context import SocialContext


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
    """Compatibility import for tests/tools; QQ dispatch uses the same Fish path."""
    from owner_private import process as private_process
    return await private_process(loop, item)
