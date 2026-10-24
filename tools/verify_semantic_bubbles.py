"""One live Fish API example plus controlled QQ receipts; never sends to QQ."""
import asyncio
import json
import time
import uuid
from pathlib import Path

from chat_output import decode_reply, delivery_plan
from fish_chat import respond
from governance import Governance
from qq_agent_loop import QQAgentLoop
from social_context import SocialContext
from storage import write_json


def main():
    root = Path(__file__).resolve().parents[1]
    output = Path('F:/AIWork/DSH/项目/DSH-Fish/实施/体验恢复/01-语义气泡/2026-10-21/验收记录')
    contexts = SocialContext(root)
    selected = contexts.owner_space(contexts.owner())
    persona = selected['persona'] if selected else contexts.ensure_default('qq')
    session = 'semantic-example:' + uuid.uuid4().hex
    prompt = '我在设置里给你智商调低了。请自然接这个玩笑：先回一句，再单独补一句反转，总共两条语义完整的气泡，别加动作。'
    kwargs = dict(persona=persona, history_source={'source': '本轮受控功能验收，不读取或改写QQ窗口'},
                  memory='', scope='owner', references='', session=session, cancelled=None, output='bubbles')
    first = decode_reply(root, respond(root, 'qq', history=[], current=[{'type': 'text', 'text': prompt}],
        origin='semantic-live-example', **kwargs))
    if len(first) != 2:
        raise ValueError('实际样本没有按要求生成两条气泡，未将其计作两条验收')
    captured = []
    class ReceiptChannel:
        async def action(self, action, payload, **options):
            captured.append({'elapsed': round(time.monotonic() - started, 3), 'action': action, 'payload': payload})
            return {'ok': True, 'payload': {'message_id': 'controlled-' + str(len(captured))}}
    loop = QQAgentLoop.__new__(QQAgentLoop)
    loop.owner = Governance(root).owner_id()
    loop.governance = Governance(root)
    loop.channel = ReceiptChannel()
    started = time.monotonic()
    result = asyncio.run(loop._deliver_reply('private:' + loop.owner, [], 'semantic-controlled',
                                             bubble_plan=delivery_plan(first)))
    assert result['ok'] and len(captured) == 2
    for item in captured:
        item['payload']['chat_id'] = 'private:已配置主人（未实际发送）'
    evidence = {'kind': '真实API生成 + 受控QQ发送回执；未向真实QQ发送',
        'session': session, 'persona': persona['id'], 'input': prompt, 'bubbles': first,
        'controlled_transmissions': captured, 'result': result, 'api_calls': 1, 'real_qq_sends': 0}
    write_json(output / '实际API与受控气泡验收.json', evidence)
    from qq_store import QQStore
    from desktop_context import DesktopContext
    from model_gateway import ModelGateway
    from multimodal import MediaService
    store = QQStore(root, db_path=output / '受控历史/qq.db')
    conv = store.get_or_create_conversation(session, 'qq')['id']
    mid = store.record_message(conv, session, 'test-owner', 'inbound', '', None, [], prompt, 'processed')
    context = DesktopContext(root, store.db_path, channel='qq')
    context.remember_input(mid, [{'type': 'text', 'text': prompt}])
    for index, bubble in enumerate(first):
        store.record_message(conv, session, 'bot', 'outbound', '', None, [], bubble['text'], 'sent',runtime_turn_id=session)
    question = '刚才那两句连在一起是什么意思？用一个气泡简单说，不要复述原话。'
    mid = store.record_message(conv, session, 'test-owner', 'inbound', '', None, [], question, 'queued')
    import threading
    history, source = context.history(conv, mid, MediaService(root, 'qq'), ModelGateway(root, 'qq'), threading.Event())
    assert [m['role'] for m in history] == ['user', 'assistant']
    kwargs['history_source'] = source
    try:
        raw = respond(root, 'qq', history=history, current=[{'type': 'text', 'text': question}],
            origin='semantic-live-history', **kwargs)
        evidence['followup_raw'] = raw
        followup = decode_reply(root, raw)
    except Exception as exc:
        evidence.update(followup_error=str(exc), api_calls=2)
        write_json(output / '实际API与受控气泡验收.json', evidence)
        raise
    evidence.update(followup=followup, api_calls=2, history_roles=[m['role'] for m in history])
    write_json(output / '实际API与受控气泡验收.json', evidence)
    print(json.dumps({'bubbles': first, 'interval_seconds': captured[1]['elapsed'] - captured[0]['elapsed'],
                      'followup': followup, 'real_qq_sends': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
