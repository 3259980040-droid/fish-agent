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
