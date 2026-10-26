"""Bind model-written memory to actual user messages, never to tool authority."""
from locations import project_path
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from storage import database

EVIDENCE_LABELS = {'manual':'主人在记忆页确认', 'self_report':'发言者自述', 'owner_quote':'主人原话',
    'observed':'对话事件观察', 'image_observation':'图片观察', 'image_text':'图片文字',
    'web_source':'网络资料（待核实）', 'model_inference':'模型推测', 'inferred':'模型推测', 'roleplay':'角色扮演', 'test_fixture':'测试资料'}


def due_epoch(value):
    """Shanghai wall-clock due time from the model; 0 means no due time at all."""
    text = str(value or '').strip()
    if not text:
        return 0.
    try:
        parsed = datetime.fromisoformat(text.replace('/', '-').replace(' ', 'T'))
    except ValueError:
        raise ValueError('due 需为 ISO 时间或 YYYY-MM-DD HH:MM')
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.timestamp()


def _desktop_quote(root, role, identity, message_id, quote):
    """The owner's own desktop message, verified in that role's desktop chat store.

    Only an inbound row of this very conversation counts. A missing id, another
    conversation's row and a QQ message id are all refused: the two stores are
    never mixed.
    """
    if not message_id:
        raise ValueError('桌面原话需提供本条对话的真实桌面消息编号source_message_id')
    path = project_path(Path(root), 'data', role, 'desktop-chat.db')
    if not path.is_file():
        raise ValueError('桌面消息库不存在，无法核实这条原话')
    resolved = str(identity.get('scope') or '')
    with database(path) as db:
        row = db.execute("SELECT m.id,m.text,c.space_id FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                         "WHERE m.direction='inbound' AND c.session_id=? AND CAST(m.id AS TEXT)=?",
                         (str(identity.get('session', '')), message_id)).fetchone()
        # A window-scoped conversation must also be the space this session is in.
        if row and resolved.startswith('space:') and str(row['space_id'] or '') != resolved[6:]:
            row = None
    if not row or quote not in str(row['text'] or ''):
        raise ValueError('原话与该桌面消息不匹配：桌面身份只认当前对话里的真实入站消息编号，QQ消息号不能当桌面消息号')
    return 'desktop:' + str(row['id']), str(row['text']), quote


def save_from_tool(root, identity, params, *, scope, role='qq', call_id=''):
    from memory_store import MemoryStore
    evidence = str(params.get('evidence', 'model_inference'))
    if evidence not in ('owner_quote', 'image_observation', 'web_source', 'model_inference'):
        raise ValueError('来源需为owner_quote、image_observation、web_source或model_inference')
    kind = str(params.get('kind') or 'fact')
    if kind not in ('fact', 'event'):
        raise ValueError('记忆类型需为 fact 或 event')
    due = due_epoch(params.get('due'))
    due_text = str(params.get('due_text') or '').strip()[:500]
    quote = str(params.get('source_quote', '')).strip()
    source = 'model-tool:' + str(identity.get('session', '')) + ':' + str(call_id)
    source_text = str(params.get('source_text', ''))[:12000]
    text = str(params['text'])
    status = 'candidate'
    if evidence == 'owner_quote':
        if not quote:
            raise ValueError('保存主人原话需提供source_quote及原消息编号source_message_id')
        message_id = str(params.get('source_message_id', ''))
        if identity.get('desktop'):
            # A desktop quote is verified in the desktop chat store only. The QQ
            # database is never consulted here and a QQ message id is never a
            # stand-in for a desktop one.
            source, source_text, text = _desktop_quote(root, role, identity, message_id, quote)
        else:
            path = project_path(Path(root), 'data', role, 'qq.db')
            with database(path) as db:
                row = db.execute("SELECT * FROM messages WHERE direction='inbound' AND sender_id=? AND (CAST(id AS TEXT)=? OR channel_message_id=?) ORDER BY id DESC LIMIT 1",
                    (str(identity.get('sender', '')), message_id, message_id)).fetchone()
            if not row or quote not in row['text']:
                raise ValueError('原话与该主人消息不匹配；模型推测不能标成主人确认')
            group = re.match(r'chat:group:([0-9]+)', scope)
            if group and row['chat_id'] != 'group:' + group[1]:
                raise ValueError('原消息不属于当前群')
            if not group and not row['chat_id'].startswith('private:'):
                raise ValueError('主人私聊记忆需来自主人私聊原话')
            source, source_text, text = 'qq:' + str(row['id']), row['text'], quote
            if params.get('image_message_id'):
                import json
                with database(path) as db:
                    image_row=db.execute('SELECT segments FROM messages WHERE CAST(id AS TEXT)=? OR channel_message_id=? ORDER BY id DESC LIMIT 1',
                        (str(params['image_message_id']),str(params['image_message_id']))).fetchone()
                    source_row=db.execute('SELECT id FROM messages WHERE CAST(id AS TEXT)=? OR channel_message_id=? ORDER BY id DESC LIMIT 1',
                        (str(params['image_message_id']),str(params['image_message_id']))).fetchone()
                images=[s for s in json.loads(image_row['segments'] or '[]') if s.get('type')=='image'] if image_row else []
                local=Path(images[0].get('local_path','')) if images else Path('')
                if not local.is_file() or not local.resolve().is_relative_to((project_path(Path(root), 'data')).resolve()):
                    raise ValueError('该消息的原图尚无本地记录，请先让当前会话读取原图')
                from image_knowledge import ImageKnowledge
                ImageKnowledge(root).confirm(local,source_row['id'],row['id'],quote)
        status = 'active'
    elif evidence == 'web_source':
        from urllib.parse import urlparse
        url = str(params.get('source_url', ''))
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or not quote:
            raise ValueError('网络资料需提供source_url和source_quote，未核实前保存为候选')
        source_text = url + '\n' + quote
    if due and due_text:
        source_text = (source_text.rstrip() + '\n约定原话：' + due_text).lstrip('\n')[:12000]
    store = MemoryStore(root, role)
    if kind == 'event' and due:
        # One event keeps one row. An explicit event_id wins; without one only an
        # exactly identical text counts as the same event, so a rephrased plan is
        # always a new event instead of silently replacing the old deadline.
        event_id = str(params.get('event_id') or '').strip()
        if event_id:
            moved = store.reschedule(event_id, scope=scope, due=due, note=due_text)
            if not moved['ok']:
                raise ValueError('不能改期：' + moved['reason'] + '；请先用fish_memory_search确认ID')
            return moved
        existing = store.find_open_event(text, scope=scope)
        if existing:
            return store.reschedule(existing['id'], scope=scope, due=due, note=due_text)
    return store.save(text, category=str(params.get('category', '事实')), scope=scope,
        source=source, source_text=source_text, evidence=evidence, status=status, kind=kind, due=due,
        confidence=1. if status == 'active' else .6)
