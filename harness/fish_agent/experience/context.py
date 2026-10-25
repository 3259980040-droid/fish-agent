"""Material assembly: what she actually gets to see this turn, and where it came from.

The entry adapter hands in the reference of the record it just saved; this module
turns that into the one set of model materials - persona body, common experience,
memory, library references and the current message with its attachments - while the
main execution keeps the loop and the budget. Nothing here is a second history
store: every block keeps its source reference.
"""
from __future__ import annotations

import json
from pathlib import Path

# The owner-private twin of group_decide.SOCIAL_NOTES: a tool that is only named is
# never called, so the surface that publishes the social tools also states when one
# is worth writing. It rides the system-side block references already use.
OWNER_SOCIAL_NOTES = ('主人说自己的事用 fish_social_note 记 impression（subject=qq:主人号或留空）；'
                      '将来的事用 fish_memory_save kind=event 带 due；寒暄/表情/重复不记，一轮最多一条。')

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


def library_block(root, question):
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
            ('doc_id', 'doc_name', 'ord', 'heading', 'text', 'context', 'url', 'original_path', 'source', 'fetched_at')}, ensure_ascii=False))
    return '\n'.join(lines)


def library_references(root, references, tool_context, current):
    """Her own shelf, searched before the system block is assembled.

    Only the owner's private chat and client read it: a group has no shelf, so every
    other surface gets the references back untouched. A failed search costs nothing -
    it is audited and the answer is written from the same material as before.
    """
    if tool_context not in ('owner-private', 'owner-desktop'):
        return references
    question = current_text(current)
    if not question:
        return references
    try:
        block = library_block(root, question)
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


def _state_scope(tool_context, scope):
    """The surface the brief knows; a window that publishes no tools has none."""
    if tool_context in ('owner-private', 'owner-desktop'):
        return tool_context
    return str(scope) if tool_context == 'group' else ''


def state_first(root, role, tool_context, scope, body):
    """The live state block, right after the persona and before any history."""
    from current_capabilities import capabilities_brief
    brief = capabilities_brief(root, scope=_state_scope(tool_context, scope), role=role)
    return (body + '\n\n【你的实际状态】' + brief) if brief else body


def social_notes(references, tool_context, offered):
    """That block, closing with the note contract on a surface that can write notes."""
    if tool_context != 'owner-private' or 'fish_social_note' not in offered:
        return references
    return (references + '\n' + OWNER_SOCIAL_NOTES) if references else OWNER_SOCIAL_NOTES


def desktop_materials(runtime, run, inputs, cancelled=None):
    """One client turn's materials from records that already exist.

    Called after the thinking slot is held. The inbound record was saved by the
    client adapter before submission, so a failure here leaves the owner's words in
    place and the run reports the real error instead of a blank turn.
    """
    from chat_output import TurnSilent
    from desktop_context import DesktopContext
    from experience import ExperienceTurn, was_considered
    from governance import Governance
    from memory_store import MemoryStore
    from multimodal import MediaService
    from conversation_style import private_context
    from social_context import SocialContext
    from tool_host import bind_session

    root, role = runtime.root, 'qq'
    client = runtime.client
    conversation = str(run['conversation'])
    if not inputs:
        raise ValueError('这次提交没有取到已接收的输入，未生成任何内容')
    message = str(inputs[-1]['message'])
    row = client.inbound(conversation, message)
    if not row:
        raise ValueError('已接收的原始消息不存在，未重复建立消息')
    text = str(row['text'] or '')
    imported = json.loads(row['segments'] or '[]')
    space = client.conversation_context(conversation)
    scope = space['scope'] if space else 'owner'
    persona = runtime.settings.snapshot(role)['persona']
    session = 'desktop:' + conversation
    tool_context = 'owner-desktop'
    # A message this place has already actually considered is not generated twice.
    if was_considered(root, 'desktop:' + message, session=session):
        raise TurnSilent('本消息已在上一轮实际考虑，未重复生成')
    client.status(message, 'processing')
    media = MediaService(root, role)
    if space:
        media.directory = SocialContext(root).directory(space['id']) / 'media'
    content = media.prepare_content(imported, text or '请查看附件。', local=True, cancelled=cancelled,
                                    include_sources=True, source_reference='desktop:' + message)
    from owner_private import note_media_gaps
    note_media_gaps(imported, content, Governance(root, role))
    context = DesktopContext(root, client.path, channel='desktop')
    context.remember_input(message, content)
    client.update_segments(message, imported)
    memory = MemoryStore(root, role)
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError('已取消')
    Governance(root, role).consume('calls')
    memory_text = memory.context(content[0]['text'], scope)
    references = private_context(root, content[0]['text'], conversation=conversation, desktop=True)
    references = library_references(root, references, tool_context, content)
    bind_session(root, role, session, 'owner', desktop=True)
    experience_turn = ExperienceTurn(root, client.path, conversation, message)
    experience_turn.session = session
    experience_turn.persona_revision = persona['id']
    try:
        history, source = experience_turn.prepare(cancelled)
    except Exception as exc:
        experience_turn.finish('failed', str(exc))
        raise
    event = dict(source['current_event'])
    quote = event.pop('quote', None)
    current = [{'type': 'text', 'text': '[本轮来信] ' + json.dumps(event, ensure_ascii=False) +
                ('\n引用：' + json.dumps(quote, ensure_ascii=False) if quote else '')},
               *media.chat_completions_content(content)]
    return {'persona': persona, 'history': history, 'history_source': source, 'memory': memory_text,
            'scope': scope, 'references': references, 'current': current, 'session': session,
            'emoji_state': {'previewed': set(), 'tools': []}, 'tool_context': tool_context,
            'delivery_required': False, 'experience': experience_turn,
            'trace_reference': source['current_reference'],
            'conversation': conversation, 'message': message}


def group_materials(runtime, run, inputs, cancelled=None):
    """One group turn's materials, built from the recorded message and the group's own reads.

    The group's address (sender names, @ targets, quotes) and its own memory are the
    group domain's job: this function only assembles them into one turn's materials.
    The stored row proves the message exists; the channel payload is not identity.
    """
    from group_decide import (_attribution, _current, _experience, _group_memory, _names,
                              _owner_revision, _persona, _quotes, _speaking_notes)
    from tool_host import bind_session

    root, role = runtime.root, 'qq'
    client = runtime.client_qq
    conversation = str(run['conversation'])
    if not inputs:
        raise ValueError('这次提交没有取到已接收的输入，未生成任何内容')
    event = inputs[-1]
    try:
        payload = json.loads(event.get('payload') or '{}')
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict) or not payload.get('chat_id'):
        raise ValueError('这次群输入缺少来源，未生成内容')
    chat = str(payload.get('chat_id') or '')
    if not chat.startswith('group:'):
        raise ValueError('群输入来源不正确')
    sender = str((payload.get('sender') or {}).get('id') or '')
    if not sender:
        raise ValueError('群消息缺少发送者，未生成内容')
    row = client.inbound(event['message'])
    if (not row or str(row.get('chat_id') or '') != chat
            or str(row.get('direction') or '') != 'inbound'):
        raise PermissionError('群消息来源不匹配')
    persona = _persona(root, _owner_revision(root))
    names = _names(root, chat)
    references = _quotes(root, chat, payload, names)
    current_text = _current(root, chat, payload, names, references)
    experience_turn = _experience(root, chat, payload)
    session = 'group-fish:' + chat
    experience_turn.session = session
    experience_turn.persona_revision = persona['id']
    try:
        history, source = experience_turn.prepare(cancelled)
    except Exception as exc:
        experience_turn.finish('failed', str(exc))
        raise
    bind_session(root, role, session, 'chat:' + chat, sender)
    memory_text = _group_memory(root, chat, current_text)
    references = _speaking_notes(references, True, _attribution(root, chat, payload, names, []))
    event_body = dict(source['current_event'])
    quote = event_body.pop('quote', None)
    current = [{'type': 'text', 'text': '[本轮来信] ' + json.dumps(event_body, ensure_ascii=False) +
                ('\n引用：' + json.dumps(quote, ensure_ascii=False) if quote else '')},
               {'type': 'text', 'text': current_text}]
    return {'persona': persona, 'history': history, 'history_source': source, 'memory': memory_text,
            'scope': 'chat:' + chat, 'references': references, 'current': current, 'session': session,
            'emoji_state': {'previewed': set(), 'tools': []}, 'tool_context': 'group',
            'delivery_required': False, 'experience': experience_turn,
            'trace_reference': source['current_reference'],
            'conversation': conversation, 'message': str(event['message']), 'chat': chat}


def qq_private_materials(runtime, run, inputs, cancelled=None):
    """One owner private turn's materials, read from the QQ record that was saved on arrival.

    The QQ process only stored the event and submitted it. Verification here uses the
    stored row (owner account, private place), never the model's or payload's claim.
    """
    from chat_output import TurnSilent
    from desktop_context import DesktopContext
    from experience import ExperienceTurn, was_considered
    from governance import Governance
    from memory_store import MemoryStore
    from multimodal import MediaService
    from conversation_style import private_context
    from tool_host import bind_session

    root, role = runtime.root, 'qq'
    client = runtime.client_qq
    conversation = str(run['conversation'])
    if not inputs:
        raise ValueError('这次提交没有取到已接收的输入，未生成任何内容')
    message = str(inputs[-1]['message'])
    row = client.inbound(message)
    if not row:
        raise ValueError('已接收的私聊原始消息不存在，未重复建立消息')
    governance = Governance(root, role)
    owner = str(governance.owner_id())
    if (str(row.get('direction') or '') != 'inbound' or str(row.get('chat_id') or '') != 'private:' + owner
            or not governance.is_owner(str(row.get('sender_id') or ''))):
        raise PermissionError('主人私聊来源不匹配')
    text = str(row['text'] or '')
    segments = json.loads(row['segments'] or '[]')
    scope, tool_context = 'owner', 'owner-private'
    session = 'qq-private:' + conversation
    persona = runtime.settings.snapshot(role)['persona']
    # A message this place has already actually considered is not generated twice.
    if was_considered(root, 'qq:' + message, session=session):
        raise TurnSilent('本消息已在上一轮实际考虑，未重复生成')
    client.status(message, 'processing', turn=run['id'])
    client.begin_turn(run['id'], conversation)
    media = MediaService(root, role)
    content = media.prepare_content(segments, text, cancelled=cancelled, include_sources=True,
                                    source_reference='qq:' + message)
    from owner_private import note_media_gaps
    note_media_gaps(segments, content, governance)
    client.update_segments(message, segments)
    context = DesktopContext(root, client.path, channel='qq')
    context.remember_input(int(message), content)
    memory = MemoryStore(root, role)
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError('已取消')
    governance.consume('calls')
    memory_text = memory.context(text, scope)
    references = private_context(root, text, conversation=conversation)
    references = library_references(root, references, tool_context, content)
    bind_session(root, role, session, 'owner', owner, False, chat_mode='owner')
    experience_turn = ExperienceTurn(root, client.path, conversation, int(message), channel='qq')
    experience_turn.session = session
    experience_turn.persona_revision = persona['id']
    try:
        history, source = experience_turn.prepare(cancelled)
    except Exception as exc:
        experience_turn.finish('failed', str(exc))
        raise
    event = dict(source['current_event'])
    quote = event.pop('quote', None)
    current = [{'type': 'text', 'text': '[本轮来信] ' + json.dumps(event, ensure_ascii=False) +
                ('\n引用：' + json.dumps(quote, ensure_ascii=False) if quote else '')},
               *media.chat_completions_content(content)]
    return {'persona': persona, 'history': history, 'history_source': source, 'memory': memory_text,
            'scope': scope, 'references': references, 'current': current, 'session': session,
            'emoji_state': {'previewed': set(), 'tools': []}, 'tool_context': tool_context,
            'delivery_required': False, 'experience': experience_turn,
            'trace_reference': source['current_reference'],
            'conversation': conversation, 'message': message}


def self_wake_materials(runtime, run, inputs, cancelled=None):
    """A clock or follow-up turn: think about its place without pretending a message came.

    The note is the only "current" content. Group material stays group-only (its own
    dialogue), while the owner's private places keep their existing common-experience
    view. Nothing here sends anything: she decides, using the same tools as always.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from desktop_context import DesktopContext
    from experience import ExperienceTurn
    from governance import Governance
    from memory_store import MemoryStore
    from conversation_style import private_context
    from tool_host import bind_session

    root, role = runtime.root, 'qq'
    source = str(run['source'])
    conversation = str(run['conversation'])
    place = str(run.get('place') or '')
    persona = runtime.settings.snapshot(role)['persona']
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')
    kind = str(run['kind'])
    reason = str(run.get('reason') or '')
    note = ('[自主思考机会] ' + ('45 分钟到了' if kind == 'timer' else '你刚才请求 3 秒后再看一眼')
            + (('（' + reason + '）') if reason and reason != '45分钟到了' else '')
            + '。现在是 ' + stamp + '（北京时间）。看看最近的共同经历，决定说点什么、做点事，'
              '或者什么都不做；没有新消息也可以继续想。想开口就调用这个场所的发送工具，不开口就不要调用。')
    event = {'reference': 'self:' + str(run['id']), 'created_at': stamp, 'text': note, 'reply_to': ''}
    if source == 'qq-group':
        from group_decide import _dialogue, _group_memory, _names
        chat = place
        if not chat.startswith('group:'):
            raise ValueError('自主回合缺少群场所，未生成内容')
        path = root / 'data/qq/qq.db'
        names = _names(root, chat)
        history = []
        for item in _dialogue(root, chat, names)[-12:]:
            try:
                message = json.loads(item.get('message') or '{}')
            except (TypeError, ValueError):
                continue
            if isinstance(message, dict) and message.get('role') in ('user', 'assistant'):
                history.append({'role': message['role'], 'content': message.get('content') or '',
                                '_fish_layer': 1})
        session = 'group-fish:' + chat
        bind_session(root, role, session, 'chat:' + chat, '')
        experience_turn = ExperienceTurn(root, path, conversation, None, channel=chat, event=event)
        experience_turn.session = session
        experience_turn.persona_revision = persona['id']
        _, source_info = experience_turn.prepare(cancelled)
        return {'persona': persona, 'history': history, 'history_source': source_info,
                'memory': _group_memory(root, chat, note), 'scope': 'chat:' + chat,
                'references': '', 'current': [{'type': 'text', 'text': note}], 'session': session,
                'emoji_state': {'previewed': set(), 'tools': []}, 'tool_context': 'group',
                'delivery_required': False, 'experience': experience_turn,
                'trace_reference': source_info['current_reference'],
                'conversation': conversation, 'message': '', 'chat': chat}
    if source == 'qq-private':
        path = root / 'data/qq/qq.db'
        owner = str(Governance(root, role).owner_id())
        if not owner:
            raise ValueError('尚未配置主人账号，未生成自主回合')
        session = 'qq-private:' + conversation
        bind_session(root, role, session, 'owner', owner, False, chat_mode='owner')
        memory = MemoryStore(root, role)
        experience_turn = ExperienceTurn(root, path, conversation, None, channel='qq', event=event)
        experience_turn.session = session
        experience_turn.persona_revision = persona['id']
        history, source_info = experience_turn.prepare(cancelled)
        references = library_references(root, private_context(root, '', conversation=conversation),
                                       'owner-private', [])
        return {'persona': persona, 'history': history, 'history_source': source_info,
                'memory': memory.context(note, 'owner'), 'scope': 'owner',
                'references': references, 'current': [{'type': 'text', 'text': note}], 'session': session,
                'emoji_state': {'previewed': set(), 'tools': []}, 'tool_context': 'owner-private',
                'delivery_required': False, 'experience': experience_turn,
                'trace_reference': source_info['current_reference'],
                'conversation': conversation, 'message': ''}
    if source == 'desktop':
        path = root / 'data/qq/desktop-chat.db'
        session = 'desktop:' + conversation
        bind_session(root, role, session, 'owner', '', True)
        memory = MemoryStore(root, role)
        experience_turn = ExperienceTurn(root, path, conversation, None, channel='desktop', event=event)
        experience_turn.session = session
        experience_turn.persona_revision = persona['id']
        history, source_info = experience_turn.prepare(cancelled)
        references = library_references(root, private_context(root, '', conversation=conversation, desktop=True),
                                       'owner-desktop', [])
        return {'persona': persona, 'history': history, 'history_source': source_info,
                'memory': memory.context(note, 'owner'), 'scope': 'owner',
                'references': references, 'current': [{'type': 'text', 'text': note}], 'session': session,
                'emoji_state': {'previewed': set(), 'tools': []}, 'tool_context': 'owner-desktop',
                'delivery_required': False, 'experience': experience_turn,
                'trace_reference': source_info['current_reference'],
                'conversation': conversation, 'message': ''}
    raise ValueError('自主回合没有可思考的场所：' + (source or '(空)'))


def plan_materials(runtime, run, inputs, cancelled=None):
    """A due occurrence: the plan record the task page shows, run by the one execution.

    The occurrence is claimed here so the task page sees it running; the reminder
    prompt is the owner's accepted commission, and a reminder may not choose to stay
    silent. Delivery happens after the turn, back on this same occurrence.
    """
    from datetime import datetime, timezone

    from bot_control import BotControl
    from experience import ExperienceTurn
    from qq_store import QQStore
    from social_context import SocialContext
    from tool_host import bind_session

    root, role = runtime.root, 'qq'
    payload = {}
    if inputs:
        try:
            payload = json.loads(inputs[-1].get('payload') or '{}')
        except (TypeError, ValueError):
            payload = {}
    schedule_id, slot = str(payload.get('schedule_id') or ''), str(payload.get('slot') or '')
    if not schedule_id or not slot:
        raise ValueError('计划回合缺少实例引用')
    plans = BotControl(root).plans
    row = plans.get(schedule_id)
    if not plans.claim(schedule_id, slot):
        raise ValueError('这次计划实例已经不可执行（已暂停、取消或已被认领）')
    prompt = plans.prompt(row, slot)
    authority = json.loads(row.get('request') or '{}').get('_authority') or {'owner': True}
    scope = 'owner' if authority.get('owner') else str(authority.get('scope') or 'owner')
    owner = SocialContext(root).owner()
    sender = owner if authority.get('owner') else str(authority.get('sender') or '')
    session = str(row.get('session') or ('fish-schedule:' + schedule_id))
    store = QQStore(root)
    conversation = store.get_or_create_conversation(str(row.get('target') or ''), 'qq')['id']
    channel = 'qq' if str(row.get('target') or '').startswith('private:') else str(row.get('target') or '')
    reference = 'wake:' + schedule_id + ':' + slot
    event = {'reference': reference, 'ts': datetime.now(timezone.utc).isoformat(),
             'text': str(row.get('prompt') or ''), 'recent_window': 7200}
    experience_turn = ExperienceTurn(root, store.db_path, conversation, channel=channel, event=event)
    experience_turn.session = session
    persona = runtime.settings.snapshot(role)['persona']
    experience_turn.persona_revision = persona['id']
    plans._bind_occurrence(session, row, slot)
    try:
        history, source = experience_turn.prepare(cancelled)
    except Exception as exc:
        experience_turn.finish('failed', str(exc))
        raise
    bind_session(root, role, session, scope, sender)
    from memory_store import MemoryStore
    event_body = dict(source['current_event'])
    event_body.pop('quote', None)
    current = [{'type': 'text', 'text': '[本轮来信] ' + json.dumps(event_body, ensure_ascii=False)},
               {'type': 'text', 'text': prompt}]
    return {'persona': persona, 'history': history, 'history_source': source,
            'memory': MemoryStore(root, role).context(str(row.get('prompt') or ''), scope),
            'scope': scope, 'references': '', 'current': current, 'session': session,
            'emoji_state': {'previewed': set(), 'tools': []},
            'tool_context': 'owner-private' if authority.get('owner') else 'group',
            'delivery_required': str(row.get('purpose')) == 'reminder',
            'experience': experience_turn, 'trace_reference': reference,
            'conversation': str(run.get('conversation') or conversation), 'message': '',
            'plan': {'schedule_id': schedule_id, 'slot': slot,
                     'target': str(row.get('target') or ''), 'purpose': str(row.get('purpose') or '')}}
