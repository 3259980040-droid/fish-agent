"""One Fish chat request policy, shared by desktop and verified QQ private chat."""
import json
import re
import time
from dataclasses import replace
from pathlib import Path
import requests
from chat_request import assemble_chat_request
from current_capabilities import capabilities_brief
from group_reply_style import expression_policy
from request_inspector import RequestInspector
from storage import serial_thinking

EMOJI_TOOLS = ('fish_emoji_search', 'fish_emoji_read')
# 发送工具只公布给真的有发送通道的面：主人私聊和群聊各有一个既有发送器（
# _deliver_reply 那条链）。独立窗口、群测试、工作台和预览没有通道，公布了也没有
# 人接，所以对它们连目录都不公布——那些窗口照旧把正文当回复显示。
SEND_TOOLS = ('fish_say', 'fish_send_sticker')
SEND_SURFACES = ('group', 'owner-private', 'owner-desktop')
TOOL_TIMEOUTS = {'fish_capabilities': 20, 'fish_group_list': 20, 'fish_group_history': 30,
                 'fish_browser':90,'fish_computer':30,'fish_qq_action':55,
                 'fish_web_search': 40, 'fish_web_fetch': 40, 'fish_emoji_save': 45,
                 # 一次看图是一次下载加一次视觉模型调用：最多两张，按两张的预算给。
                 'fish_image_look': 90}
# A group answer may only claim what the returned batch supports. These word forms
# are matched literally, so a correct reply about a handful of rows never starts a
# second round; the qualifier is added only where the claim actually needs it.

def tool_definitions(role, tool_context, *, root=None, emoji_state=None):
    """The model sees the executor's current catalogue, including enabled MCPs."""
    if not tool_context:
        return []
    from tool_host import request
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    catalog = request(root, '/catalog', role=role, admin=False)
    return [{'type': 'function', 'function': {key: item[key] for key in ('name', 'description', 'parameters')}}
            for item in catalog['tools']]


def call_tool(root, role, session, name, arguments, call_id, timeout):
    """Route non-emoji tools through the same loopback identity/permission gate."""
    from tool_host import request
    return request(root, '/execute', {'session': session, 'name': name, 'arguments': arguments,
                                      'call_id': call_id}, role=role, admin=False, timeout=timeout)


def _state_scope(tool_context, scope):
    """The surface the brief knows; a window that publishes no tools has none."""
    if tool_context in ('owner-private', 'owner-desktop'):
        return tool_context
    return str(scope) if tool_context == 'group' else ''


# The owner-private twin of group_decide.SOCIAL_NOTES: a tool that is only named is
# never called, so the surface that publishes the social tools also states when one
# is worth writing. It rides the system-side block references already use.
OWNER_SOCIAL_NOTES = ('主人说自己的事用 fish_social_note 记 impression（subject=qq:主人号或留空）；'
                      '将来的事用 fish_memory_save kind=event 带 due；寒暄/表情/重复不记，一轮最多一条。')


def _social_notes(references, tool_context, offered):
    """That block, closing with the note contract on a surface that can write notes."""
    if tool_context != 'owner-private' or 'fish_social_note' not in offered:
        return references
    return (references + '\n' + OWNER_SOCIAL_NOTES) if references else OWNER_SOCIAL_NOTES


# The owner's own shelf, quoted by name and chunk. Three chunks fit the budget a
# citation block may take; a shelf that returns nothing adds no block at all.
LIBRARY_LIMIT = 3
LIBRARY_HEAD = '[资料库检索结果；引用时必须写出处：文档名 + 第几块]'


def current_text(current):
    """The owner's own words of this turn, out of the assembled content parts."""
    parts = []
    if isinstance(current, str):
        parts.append(current)
    elif isinstance(current, (list, tuple)):
        for part in current:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and str(part.get('type') or '') == 'text':
                parts.append(str(part.get('text') or ''))
    return ' '.join(' '.join(parts).split())


def _library_block(root, question):
    """The citation block of the chunks that answer this turn's question, or ''."""
    from doc_store import search as search_documents
    result = search_documents(root, question, limit=LIBRARY_LIMIT, with_status=True)
    hits = result['results']
    if not hits:
        return ''
    lines = [LIBRARY_HEAD, '[实际检索] ' + json.dumps(result['retrieval'], ensure_ascii=False)]
    for hit in hits:
        # These are actual indexed child/parent references. The one token budget
        # owns final trimming, rather than another character ceiling here.
        lines.append(json.dumps({key: hit.get(key) for key in
            ('doc_id','doc_name','ord','heading','text','context','url','original_path','source','fetched_at')}, ensure_ascii=False))
    return '\n'.join(lines)


def _library_references(root, references, tool_context, current):
    """Her own shelf, searched before the system block is assembled.

    Only the owner's private chat reads it: a group has no shelf, so every other
    surface gets the references back untouched. A failed search costs nothing -
    it is audited and the answer is written from the same material as before.
    """
    if tool_context not in ('owner-private', 'owner-desktop'):
        return references
    question = current_text(current)
    if not question:
        return references
    try:
        block = _library_block(root, question)
    except Exception as error:
        try:
            from governance import Governance
            Governance(root).audit('library.search.failed', '资料库检索失败，本轮不注入', str(error)[:200])
        except Exception:
            pass
        return references
    if not block:
        return references
    return (references + '\n\n' + block) if references else block


def _state_first(root, role, tool_context, scope, body):
    """The live state block, right after the persona and before any history."""
    brief = capabilities_brief(root, scope=_state_scope(tool_context, scope), role=role)
    return (body + '\n\n【你的实际状态】' + brief) if brief else body


def turn_says(root, role, session, thinking):
    """This turn's queue as the reply its sender decodes, or TurnSilent for a quiet turn.

    A turn that called no sending tool said nothing, whatever its prose contains: her
    plain output is thinking, and it is audited (with its opening words, so what she was
    considering stays readable) instead of being sent. A turn that did call a tool hands
    back exactly what it queued - the same bubble wire value the sender already decodes -
    so nothing downstream needs a second channel or a second opinion.
    """
    from chat_output import TurnSilent
    from governance import Governance
    from tool_host import take_says
    bubbles = take_says(root, session)
    words = ''.join(str(bubble.get('text') or '') for bubble in bubbles if bubble.get('kind') == 'text')
    stickers = [str(bubble.get('emoji_id') or '') for bubble in bubbles if bubble.get('kind') == 'sticker']
    if not bubbles:
        thought = ' '.join(str(thinking or '').split())
        Governance(root, role).audit(
            'turn_no_send',
            '这一轮没有提交默认聊天气泡；实际工具发送见同轮回执。未发正文前60字：' + (thought[:60] or '（没有写正文）'),
            thought[:2000])
        raise TurnSilent('本轮没有提交默认聊天气泡；实际工具投递按真实回执记录')
    Governance(root, role).audit(
        'turn_said', f'{len(bubbles)}条，文字{len(words)}字、表情{len(stickers)}张：' + (words[:20] or '只发了表情'),
        words[:2000])
    if stickers:
        Governance(root, role).audit(
            'turn_sticker', '表情 ' + '、'.join(stickers) + '：' + (words[:20] or '没有配文字'))
    return json.dumps({'bubbles': bubbles}, ensure_ascii=False)


@serial_thinking
def respond(root, role, *, persona, history, history_source, memory, scope,
            references, current, session, cancelled, origin, output='text', emoji_state=None,
            tool_context='', experience=None, delivery_required=False, execution_key=None):
    # Entry adapters supply facts and the current attachment payload. Preparation
    # happens only after this Fish has acquired its one thinking slot.
    from chat_output import TurnSilent
    if experience is not None:
        from social_context import SocialContext
        persona = SocialContext(root).active_persona(role)
        experience.persona_revision = persona['id']
        experience.session = session
        history, history_source = experience.prepare(cancelled)
        from experience import was_considered
        if was_considered(root, history_source['current_reference'], session=session):
            experience.finish('silent')
            raise TurnSilent('本消息已在上一轮实际考虑，未重复生成')
        event = history_source['current_event']
        quote = event.pop('quote', None)
        current = [{'type': 'text', 'text': '[本轮来信] ' + json.dumps(event, ensure_ascii=False) +
                    ('\n引用：' + json.dumps(quote, ensure_ascii=False) if quote else '')}, *current]
    from turn_trace import TurnTrace
    trace = TurnTrace(root,role,session,origin,getattr(experience,'reference',''),child=bool(execution_key))
    try:
        result = _respond(root, role, persona=persona, history=history, history_source=history_source,
            memory=memory, scope=scope, references=references, current=current, session=session,
            cancelled=cancelled, origin=origin, output=output, emoji_state=emoji_state,
            tool_context=tool_context, experience=experience, delivery_required=delivery_required,trace=trace)
    except TurnSilent:
        trace.finish('silent')
        if experience is not None:
            experience.finish('silent')
        raise
    except Exception as exc:
        trace.finish('uncertain' if isinstance(exc,TimeoutError) else 'failed',str(exc))
        if experience is not None:
            experience.finish('failed', str(exc))
        raise
    if experience is not None:
        experience.finish('considered')
    trace.finish('complete')
    return result


def _respond(root, role, *, persona, history, history_source, memory, scope,
             references, current, session, cancelled, origin, output='text', emoji_state=None,
              tool_context='', experience=None, delivery_required=False,trace=None):
    from model_gateway import ModelGateway, chat_options
    gateway = ModelGateway(root, role)
    if output not in ('text', 'bubbles'):
        raise ValueError('未知聊天输出格式')
    expression, options = expression_policy(semantic_bubbles=output == 'bubbles'), chat_options(root)
    from context_budget import ContextBudget
    budget = ContextBudget(root, gateway)
    output_source = 'text'
    library = None
    tools = tool_definitions(role, tool_context, root=root)
    offered = tuple(definition['function']['name'] for definition in tools)
    # 这一轮的话由谁送出去：只有真的绑成了发送面的会话（群、主人私聊），正文才是
    # 思考、说话必须调用工具；其它窗口（桌面、工作台、预览、群里的主动开口草稿）
    # 照旧把正文当回复。那张工具表仍然对它们全部公布，调用会在执行器里被按场合拒绝。
    from tool_host import sender_surface
    sending = output == 'bubbles' and sender_surface(root, session, role) in SEND_SURFACES
    if sending:
        from tool_host import clear_says
        clear_says(root, session)  # 只发这一轮自己决定要说的话
    # 挑表情要气泡契约接住：能力是同一套，这一轮接不接得住由本轮的输出形式和已核验的
    # 范围决定。接不住时工具照常公布，调用会拿到一句中文说明，而不是被悄悄藏起来。
    stickers = output == 'bubbles' and (scope == 'owner' or tool_context == 'group')
    if stickers and any(name in EMOJI_TOOLS for name in offered):
        from emoji_store import EmojiStore
        library = EmojiStore(root)
    observed = emoji_state['tools'] if isinstance(emoji_state, dict) else []
    # Before the reply contract joins the current turn, so a shelf search reads
    # the owner's question and not the protocol text.
    references = _library_references(root, references, tool_context, current)
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
    request = assemble_chat_request(persona=_state_first(root, role, tool_context, scope, persona['body']), history=history,
        memory=memory, expression=expression,
        references=_social_notes(references, tool_context, offered), current=current,
        sources={'execution': 'fish-api', 'persona_revision': persona['id'],
                 'persona': 'SocialContext.body', 'expression': expression['source'],
                 'memory_scope': scope, 'history': history_source,
                 'model': 'harness/agents.json', 'parameters': 'ModelGateway.chat_options / harness/control.json'})
    request['sources']['output'] = output_source
    messages = request['messages']
    from governance import Governance
    tool_limit=Governance(root,role).config()['max_tool_calls_per_turn']
    tools_used=0
    deadline=time.monotonic()+options.timeout_seconds
    while time.monotonic()<deadline:
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('本轮聊天已取消，未继续生成或调用工具')
        if experience is not None:
            messages.extend(experience.updates(cancelled))
        budget_report = budget.fit(messages, tools, options.max_tokens)
        preview = []
        for message in messages:
            body = message.get('content') or ''
            if isinstance(body,list):
                body=[({'type':'image_url','omitted':'附件数据'} if b['type']=='image_url'
                       else {'type':'text','text':b['text'][:1000]}) for b in body]
            else: body=body[:2000]
            preview.append({**{k:v for k,v in message.items() if k in ('role','tool_calls','tool_call_id')},'content':body})
        RequestInspector(root).capture(role,{'session':session,'scope':scope},
            {'provider':gateway.base,'model':gateway.model,'purpose':origin,'messages':preview[-30:],
             'system':request['sources'],'tools':tools or [],'maxTokens':options.max_tokens,'effort':options.effort,
             'context_budget':budget_report,
             'omitted':{'preview':'快照只截取文字，完整组装结果与工具结果见 messages',
                        'tools':'工具目录取自 tool_host.builtins；本轮实际公布的工具见 tools，执行结果见 messages'}})
        remaining=deadline-time.monotonic()
        if remaining<=0:break
        accounting = budget.begin(session, origin, budget_report)
        if experience is not None:
            experience.requested(budget_report)
        trace.event('request',{'model':gateway.model,'estimated_input_tokens':budget_report.get('estimated_input_tokens'),'input_ceiling':budget_report.get('input_ceiling')})
        try:
            result=gateway.chat(messages,options=replace(options,timeout_seconds=remaining),cancelled=cancelled,origin=origin,
                                on_event=trace.event,**({'tools':tools} if tools else {}))
            budget.finish(accounting, result.usage)
            if experience is not None:
                experience.received(getattr(result, 'first_output_at', None))
        except Exception as exc:
            budget.finish(accounting, error=str(exc))
            raise
        if not result.tool_calls:
            trace.event('final_content',result.text or '')
            if sending:
                if delivery_required:
                    from tool_host import peek_says
                    if not peek_says(root, session):
                        from chat_output import decode_reply
                        return json.dumps({'bubbles': decode_reply(root, result.text)}, ensure_ascii=False)
                return turn_says(root, role, session, result.text)
            return result.text
        assistant={'role':'assistant','content':result.text or '', 'tool_calls':list(result.tool_calls)}
        assistant['reasoning_content']=result.reasoning_content
        messages.append(assistant)
        previews=[]
        for call in result.tool_calls:
            if tool_limit>0 and tools_used>=tool_limit:raise ValueError('本轮工具调用达到当前设置的上限，结果未冒充已发送')
            tools_used+=1
            function=call['function'];name=str(function.get('name') or '')
            params={}
            try:
                params=json.loads(function.get('arguments') or '{}')
                trace.event('tool_start',{'name':name,'arguments':params})
                if not isinstance(params,dict):raise ValueError('工具参数必须为对象')
                if name not in offered:raise ValueError('本轮未提供该工具：'+(name or '(空)')+'；只能用本轮公布的工具')
                if name=='fish_emoji_search':
                    if not stickers:
                        raise ValueError('这一轮是纯文本回复，没有能接住表情气泡的契约，不能挑表情；直接用文字说')
                    value={'assets':library.candidates(str(params['context']))}
                elif name=='fish_emoji_read':
                    if not stickers:
                        raise ValueError('这一轮是纯文本回复，没有能接住表情气泡的契约，不能预览表情；直接用文字说')
                    from multimodal import MediaService
                    value=library.read(str(params['id']));asset=value.pop('content')[0]
                    media=MediaService(root,'qq')
                    if not media.config['vision']['enabled']:raise ValueError('图片输入已关闭，不能向模型传入表情预览')
                    uri=media._image_uri(Path(asset['path']))
                    previews.extend([{'type':'text','text':'[本地表情 '+value['id']+' 的实际预览；GIF仅预览首帧，不能据此推测未见动作]'},
                                     {'type':'image_url','image_url':{'url':uri}}])
                    if isinstance(emoji_state, dict):
                        emoji_state['previewed'].add(value['id'])
                    # 发送工具的执行器在另一个进程里，所以看过的那张图要落一行，
                    # "没有备注的表情必须预览过"这条闸门才在两个地方是同一句话。
                    from tool_host import note_preview
                    note_preview(root, session, value['id'])
                else:
                    value=call_tool(root,role,session,name,params,call['id'],
                                  min(TOOL_TIMEOUTS.get(name,30),max(1.0,deadline-time.monotonic())))
            except (ValueError,KeyError,OSError,PermissionError,InterruptedError,requests.RequestException) as exc:
                value={'error':str(exc)}
            if isinstance(value, dict) and value.get('_fish_images'):
                previews.extend(value.pop('_fish_images'))
            trace.event('tool_result',{'name':name,'result':value})
            observed_call = {'name':name,'arguments':params,'ok':not isinstance(value,dict) or 'error' not in value}
            actual_delivery = (value.get('delivery') if isinstance(value.get('delivery'),dict) else value) if isinstance(value,dict) else {}
            if actual_delivery.get('state') in ('SUCCEEDED','UNKNOWN','FAILED','sent','uncertain','failed'):
                observed_call['delivery'] = {'state':actual_delivery['state'],'target':value.get('target',params.get('target',''))}
            observed.append(observed_call)
            messages.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(value,ensure_ascii=False)})
        if previews:messages.append({'role':'user','content':previews})
    raise TimeoutError('本轮聊天及工具调用超过当前聊天超时设置；记录保留，未声称已发送')
