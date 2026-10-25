"""The one Fish model/tool loop: a turn's materials in, its real answer out.

This is the loop every entry ends up in. It asks the tool host for the same
catalogue the executor holds, assembles one request against the one token budget,
and continues the same protocol when the model calls tools. Entry adapters do not
pass their own prompt, history assembly or tool switches.

The turn's input range is fixed by the caller's claim: a message that arrives while
she is thinking is registered for the next turn and is not appended here. Tool
results that belong to this turn still continue the same request.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import requests

from .. import tools as tool_api
from ..experience import context as material

EMOJI_TOOLS = ('fish_emoji_search', 'fish_emoji_read')
# 发送工具只公布给真的有发送通道的面：主人私聊、群聊和主人客户端各有既有发送器。
# 独立窗口、群测试和工作台预览没有通道，公布了也没有人接，所以对它们连目录都不公布。
SEND_TOOLS = ('fish_say', 'fish_send_sticker')
SEND_SURFACES = ('group', 'owner-private', 'owner-desktop')


def run_model_loop(root, role, *, persona, history, history_source, memory, scope, references,
                   current, session, cancelled, origin, output='text', emoji_state=None,
                   tool_context='', delivery_required=False, trace=None, experience=None):
    """Run one turn to its real result.

    Returns the answer text (``text`` output) or this turn's queued bubbles as JSON
    (``bubbles`` output on a real sending surface). Raises ``TurnSilent`` when a
    sending surface decided not to say anything, ``InterruptedError`` on cancellation
    and ``TimeoutError``/provider errors when the turn did not complete.
    """
    from model_gateway import ModelGateway, chat_options
    gateway = ModelGateway(root, role)
    if output not in ('text', 'bubbles'):
        raise ValueError('未知聊天输出格式')
    from group_reply_style import expression_policy
    expression, options = expression_policy(semantic_bubbles=output == 'bubbles'), chat_options(root)
    from context_budget import ContextBudget
    budget = ContextBudget(root, gateway)
    output_source = 'text'
    library = None
    tool_list = tool_api.definitions(root, role) if tool_context else []
    offered = tuple(definition['function']['name'] for definition in tool_list)
    # 这一轮的话由谁送出去：只有真的绑成了发送面的会话（群、主人私聊、主人客户端），
    # 正文才是思考、说话必须调用工具；其它窗口照旧把正文当回复。
    sending = output == 'bubbles' and tool_api.surface(root, session, role) in SEND_SURFACES
    if sending:
        tool_api.clear_says(root, session)  # 只发这一轮自己决定要说的话
    # 挑表情要气泡契约接住：能力是同一套，这一轮接不接得住由本轮的输出形式和已核验的
    # 范围决定。接不住时工具照常公布，调用会拿到一句中文说明，而不是被悄悄藏起来。
    stickers = output == 'bubbles' and (scope == 'owner' or tool_context == 'group')
    if stickers and any(name in EMOJI_TOOLS for name in offered):
        from emoji_store import EmojiStore
        library = EmojiStore(root)
    observed = emoji_state['tools'] if isinstance(emoji_state, dict) else []
    if output == 'bubbles':
        from chat_output import protocol_history
        history = protocol_history(history)
        if sending:
            from chat_output import reply_contract
            contract = reply_contract(stickers=library is not None)
            current = [*current, {'type': 'text', 'text': contract['prompt']}]
            if delivery_required:
                current.append({'type': 'text', 'text': '本轮是已接受的到期委托，需要交付提醒或实际结果；不能把它当作可不行动的自主聊天。可以用发送工具提交，或直接输出要送达的正文。遇到失败如实说明。'})
            output_source = contract['source']
    from chat_request import assemble_chat_request
    request = assemble_chat_request(
        persona=material.state_first(root, role, tool_context, scope, persona['body']),
        history=history, memory=memory, expression=expression,
        references=material.social_notes(references, tool_context, offered), current=current,
        sources={'execution': 'fish-api', 'persona_revision': persona['id'],
                 'persona': 'SocialContext.body', 'expression': expression['source'],
                 'memory_scope': scope, 'history': history_source,
                 'model': 'harness/agents.json',
                 'parameters': 'ModelGateway.chat_options / harness/control.json'})
    request['sources']['output'] = output_source
    messages = request['messages']
    from governance import Governance
    tool_limit = Governance(root, role).config()['max_tool_calls_per_turn']
    tools_used = 0
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('本轮聊天已取消，未继续生成或调用工具')
        budget_report = budget.fit(messages, tool_list, options.max_tokens)
        preview = []
        for message in messages:
            body = message.get('content') or ''
            if isinstance(body, list):
                body = [({'type': 'image_url', 'omitted': '附件数据'} if block['type'] == 'image_url'
                         else {'type': 'text', 'text': block['text'][:1000]}) for block in body]
            else:
                body = body[:2000]
            preview.append({**{k: v for k, v in message.items() if k in ('role', 'tool_calls', 'tool_call_id')},
                            'content': body})
        from request_inspector import RequestInspector
        RequestInspector(root).capture(role, {'session': session, 'scope': scope},
            {'provider': gateway.base, 'model': gateway.model, 'purpose': origin, 'messages': preview[-30:],
             'system': request['sources'], 'tools': tool_list or [], 'maxTokens': options.max_tokens,
             'effort': options.effort, 'context_budget': budget_report,
             'omitted': {'preview': '快照只截取文字，完整组装结果与工具结果见 messages',
                         'tools': '工具目录取自 tool_host.builtins；本轮实际公布的工具见 tools，执行结果见 messages'}})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        accounting = budget.begin(session, origin, budget_report)
        if experience is not None:
            # The assembled surface already carries its own prices; this counter is the
            # same one that priced the request, used when a surface has no price yet.
            experience.requested(budget_report, tokens=getattr(budget, 'tokens', None))
        trace.event('request', {'model': gateway.model,
                                'estimated_input_tokens': budget_report.get('estimated_input_tokens'),
                                'input_ceiling': budget_report.get('input_ceiling')})
        try:
            result = gateway.chat(messages, options=replace(options, timeout_seconds=remaining),
                                  cancelled=cancelled, origin=origin, on_event=trace.event,
                                  **({'tools': tool_list} if tool_list else {}))
            budget.finish(accounting, result.usage)
            if experience is not None:
                experience.received(getattr(result, 'first_output_at', None))
        except Exception as exc:
            budget.finish(accounting, error=str(exc))
            raise
        # A result that lands after the owner cancelled is not saved as a reply.
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('本轮已取消；晚到结果未保存为成功回复')
        if not result.tool_calls:
            trace.event('final_content', result.text or '')
            if sending:
                if delivery_required:
                    if not tool_api.say_queue(root, session):
                        from chat_output import decode_reply
                        return json.dumps({'bubbles': decode_reply(root, result.text)}, ensure_ascii=False)
                return turn_says(root, role, session, result.text)
            return result.text
        assistant = {'role': 'assistant', 'content': result.text or '', 'tool_calls': list(result.tool_calls)}
        assistant['reasoning_content'] = result.reasoning_content
        messages.append(assistant)
        previews = []
        for call in result.tool_calls:
            if tool_limit > 0 and tools_used >= tool_limit:
                raise ValueError('本轮工具调用达到当前设置的上限，结果未冒充已发送')
            tools_used += 1
            function = call['function']
            name = str(function.get('name') or '')
            params = {}
            try:
                params = json.loads(function.get('arguments') or '{}')
                trace.event('tool_start', {'name': name, 'arguments': params})
                if not isinstance(params, dict):
                    raise ValueError('工具参数必须为对象')
                if name not in offered:
                    raise ValueError('本轮未提供该工具：' + (name or '(空)') + '；只能用本轮公布的工具')
                if name == 'fish_emoji_search':
                    if not stickers:
                        raise ValueError('这一轮是纯文本回复，没有能接住表情气泡的契约，不能挑表情；直接用文字说')
                    value = {'assets': library.candidates(str(params['context']))}
                elif name == 'fish_emoji_read':
                    if not stickers:
                        raise ValueError('这一轮是纯文本回复，没有能接住表情气泡的契约，不能预览表情；直接用文字说')
                    from multimodal import MediaService
                    value = library.read(str(params['id']))
                    asset = value.pop('content')[0]
                    media = MediaService(root, 'qq')
                    if not media.config['vision']['enabled']:
                        raise ValueError('图片输入已关闭，不能向模型传入表情预览')
                    uri = media._image_uri(Path(asset['path']))
                    previews.extend([{'type': 'text', 'text': '[本地表情 ' + value['id'] + ' 的实际预览；GIF仅预览首帧，不能据此推测未见动作]'},
                                     {'type': 'image_url', 'image_url': {'url': uri}}])
                    if isinstance(emoji_state, dict):
                        emoji_state['previewed'].add(value['id'])
                    # 发送工具的执行器在另一个进程里，所以看过的那张图要落一行，
                    # "没有备注的表情必须预览过"这条闸门才在两个地方是同一句话。
                    tool_api.note_preview(root, session, value['id'])
                else:
                    value = tool_api.execute(root, role, session, name, params, call['id'],
                                             min(tool_api.TOOL_TIMEOUTS.get(name, 30),
                                                 max(1.0, deadline - time.monotonic())))
            except (ValueError, KeyError, OSError, PermissionError, InterruptedError, requests.RequestException) as exc:
                value = {'error': str(exc)}
            if isinstance(value, dict) and value.get('_fish_images'):
                previews.extend(value.pop('_fish_images'))
            trace.event('tool_result', {'name': name, 'result': value})
            observed_call = {'name': name, 'arguments': params,
                             'ok': not isinstance(value, dict) or 'error' not in value}
            actual_delivery = (value.get('delivery') if isinstance(value.get('delivery'), dict) else value) if isinstance(value, dict) else {}
            if actual_delivery.get('state') in ('SUCCEEDED', 'UNKNOWN', 'FAILED', 'sent', 'uncertain', 'failed'):
                observed_call['delivery'] = {'state': actual_delivery['state'],
                                             'target': value.get('target', params.get('target', ''))}
            observed.append(observed_call)
            messages.append({'role': 'tool', 'tool_call_id': call['id'],
                             'content': json.dumps(value, ensure_ascii=False)})
        if previews:
            messages.append({'role': 'user', 'content': previews})
    raise TimeoutError('本轮聊天及工具调用超过当前聊天超时设置；记录保留，未声称已发送')


def turn_says(root, role, session, thinking):
    """This turn's queue as the reply its sender decodes, or TurnSilent for a quiet turn.

    A turn that called no sending tool said nothing, whatever its prose contains: her
    plain output is thinking, and it is audited (with its opening words) instead of
    being sent. A turn that did call a tool hands back exactly what it queued.
    """
    from chat_output import TurnSilent
    from governance import Governance
    bubbles = tool_api.take_says(root, session)
    words = ''.join(str(bubble.get('text') or '') for bubble in bubbles if bubble.get('kind') == 'text')
    stickers = [str(bubble.get('emoji_id') or '') for bubble in bubbles if bubble.get('kind') == 'sticker']
    if not bubbles:
        thought = ' '.join(str(thinking or '').split())
        Governance(root, role).audit(
            'turn_no_send',
            '这一轮没有提交默认聊天气泡；实际工具发送见同轮回执。未发正文前60字：' + (thought[:60] or '（没有写正文）'),
            thought[:2000])
        raise TurnSilent('本轮没有提交默认聊天气泡；实际投递按真实回执记录')
    Governance(root, role).audit(
        'turn_said', f'{len(bubbles)}条，文字{len(words)}字、表情{len(stickers)}张：' + (words[:20] or '只发了表情'),
        words[:2000])
    if stickers:
        Governance(root, role).audit(
            'turn_sticker', '表情 ' + '、'.join(stickers) + '：' + (words[:20] or '没有配文字'))
    return json.dumps({'bubbles': bubbles}, ensure_ascii=False)


def execute(root, role, *, prepare, origin, cancelled=None, output='text', experience=None,
            trace_reference='', child=False, execution_key=None):
    """Hold the same Fish's thinking slot, prepare materials, run one real turn.

    Preparation happens only after this Fish has acquired its one thinking slot, so
    two entries never assemble or generate at the same time. The trace is written
    only after the slot is held, and its phase reflects the real outcome.
    """
    from storage import thinking_slot
    from turn_trace import TurnTrace
    from chat_output import TurnSilent
    with thinking_slot(root, role, cancelled, key=execution_key):
        trace = None
        try:
            materials = prepare()
            if materials.get('experience') is not None:
                experience = materials['experience']
            session = materials.get('session') or ''
            trace = TurnTrace(root, role, session, origin,
                              materials.get('trace_reference') or trace_reference, child=child)
            result = run_model_loop(
                root, role, persona=materials['persona'], history=materials.get('history') or [],
                history_source=materials.get('history_source') or {}, memory=materials.get('memory') or '',
                scope=materials.get('scope') or 'owner', references=materials.get('references') or '',
                current=materials['current'], session=session, cancelled=cancelled, origin=origin,
                output=output, emoji_state=materials.get('emoji_state') or {'previewed': set(), 'tools': []},
                tool_context=materials.get('tool_context') or '',
                delivery_required=bool(materials.get('delivery_required')), trace=trace,
                experience=experience)
        except TurnSilent:
            if trace is not None:
                trace.finish('silent')
            if experience is not None:
                experience.finish('silent')
            raise
        except Exception as exc:
            if trace is not None:
                trace.finish('uncertain' if isinstance(exc, TimeoutError) else 'failed', str(exc))
            if experience is not None:
                experience.finish('failed', str(exc))
            raise
        if experience is not None:
            experience.finish('considered')
        trace.finish('complete')
        return result
