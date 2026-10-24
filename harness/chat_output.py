"""Semantic chat queues, following Tulpa's whole-bubble send protocol.

Reference: Tulpa cf15cf5987466c97a493bf1f1771dae99b1fcac8,
doc/MCP_CHAT_TURNS.md and MCP_STICKERS.md (MIT). This adapts the protocol to
Fish's existing verified owner route and transport; no punctuation splitter.
"""
import json
import re
from governance import Governance
from emoji_store import EmojiStore

MIN_BUBBLES = 1
MAX_BUBBLES = 3
# A turn is one delivery by one sender: her words are capped exactly as before, and
# one sticker may ride beside them without becoming a second sentence.
MAX_STICKERS = 1
DEGRADED_REPLY = '刚那句没组织好，你再说一遍嘛'


class TurnSilent(Exception):
    """This turn called no sending tool: nothing is sent, and that is her decision.

    Her plain output is her thinking now, so a turn that sends nothing is neither a
    failure nor an empty reply. The caller that owns the channel catches this type
    and ends the turn quietly instead of reporting a broken generation.
    """


def reply_contract(*, stickers=False):
    """How a turn gets spoken; length and speaking style belong to expression_policy."""
    return {'source': 'harness/chat_output.py:reply_contract',
        'prompt': '\n[怎么把话说出去]\n本轮你写的正文不会发给对方，那只是你自己的思考。'
        f'要让对方收到话，必须调用发送工具：fish_say 一次提交{MIN_BUBBLES}至{MAX_BUBBLES}项对白，'
        '每项是一条语义完整的气泡，只放真正要说出口的那句话。' +
        ('fish_send_sticker 单独发一张本地表情库里的图，可以和 fish_say 同轮配合。'
         '用fish_emoji_search查找；不熟悉或无备注的表情必须先fish_emoji_read看实际预览。不能虚构ID或内容。'
         f'一轮最多{MAX_BUBBLES}项文字加{MAX_STICKERS}张表情。' if stickers else '本次只支持文字，不编造表情已发出。') +
        '不调用任何发送工具就什么都不发，对方一个字都收不到——要不要开口由你决定，'
        '不想说就什么都不做，这很正常，也不会被当成出错。'
        '说过的不重复提交，不按标点切开一句话，不为凑条数重复同一句意思；'
        '工具回执只说明进了本轮待发队列，送达与否由通道回执确定，回执没确认就不要说自己已经说了。\n'}


def bubble_text(bubble):
    """The words a bubble still carries, even when its structure is unusable."""
    if not isinstance(bubble, dict):
        return ''
    for key in ('text', 'content', 'say'):
        value = bubble.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def well_formed(bubble):
    """The exact wire shape reply_contract() asks for; only this passes unchanged."""
    if not isinstance(bubble, dict):
        return False
    if bubble.get('kind') == 'text':
        return (set(bubble) == {'kind', 'text'} and isinstance(bubble.get('text'), str)
                and bool(bubble['text'].strip()))
    return (bubble.get('kind') == 'sticker' and set(bubble) == {'kind', 'emoji_id'}
            and isinstance(bubble.get('emoji_id'), str))


def repaired_bubbles(root, bubbles):
    """Drop what cannot be sent and keep her words as one plain text bubble."""
    broken = [bubble for bubble in bubbles if not well_formed(bubble)]
    if not broken:
        return bubbles
    text = '\n'.join(part for part in (bubble_text(bubble) for bubble in bubbles) if part)
    text = text or DEGRADED_REPLY
    Governance(root).audit('chat_reply_bubbles_repaired',
        f'{len(broken)}/{len(bubbles)} 气泡不合规，已降级为纯文字：{str(broken[0])[:120]}')
    # Repaired words are held to the same output check as any other bubble.
    Governance(root).check_output(text)
    return [{'kind': 'text', 'text': text}]


def salvage_text(raw, limit=3):
    """The words inside a JSON reply that failed to parse, or [] when there are none.

    A truncated or half-escaped answer still contains the sentence she meant to say.
    Only plain string values are taken, and anything that looks like the envelope
    itself is rejected, so a broken blob can never be sent as prose.
    """
    found, seen = [], set()
    for match in re.finditer(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
        value = match.group(1)
        try:
            value = json.loads('"' + value + '"')
        except ValueError:
            continue
        value = str(value).strip()
        if len(value) < 2 or value in seen:
            continue
        if any(token in value for token in ('"bubbles"', '\\u', '{"', '":')):
            continue
        seen.add(value)
        found.append(value)
        if len(found) >= limit:
            break
    return found


def decode_reply(root, text, *, previewed=()):
    """Accept ordinary prose without another API call; never leak broken JSON."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('模型没有返回聊天内容')
    raw = text.strip()
    lines = raw.splitlines()
    fenced = len(lines) >= 3 and lines[0].strip().lower() in ('```json', '```') and lines[-1].strip() == '```'
    if fenced:
        raw = '\n'.join(lines[1:-1]).strip()
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        if not fenced and raw and not raw.startswith(('{', '[', '```')) and '"bubbles"' not in raw:
            Governance(root).audit('chat_reply_format_fallback', 'plain-text; one bubble; no API retry')
            return validate_bubbles(root, [{'kind': 'text', 'text': raw}], check_content=False)
        # Broken JSON still carries what she wanted to say: a truncated answer used
        # to become "本轮生成没能完成", i.e. the owner saw nothing at all. Take the
        # words out of it rather than the whole turn.
        salvaged = salvage_text(raw)
        if salvaged:
            Governance(root).audit('chat_reply_salvaged', 'JSON 不完整，已取其中的文字发送')
            return validate_bubbles(root, [{'kind': 'text', 'text': item} for item in salvaged],
                                    check_content=False)
        raise ValueError('模型未返回有效的聊天气泡JSON，原结果未作为正文发送') from None
    if not isinstance(value, dict) or set(value) != {'bubbles'}:
        raise ValueError('模型回复缺少唯一的bubbles字段')
    bubbles = value['bubbles']
    if not isinstance(bubbles, list):
        raise ValueError('聊天气泡应为text+text字段或sticker+emoji_id字段')
    result = validate_bubbles(root, repaired_bubbles(root, bubbles), check_content=False)
    unknown = {r['id'] for r in EmojiStore(root).list() if not (r['description'].strip() or r['tags'].strip())} if any(b['kind']=='sticker' for b in result) else set()
    if any(b['kind']=='sticker' and b['emoji_id'] in unknown and b['emoji_id'] not in previewed for b in result):
        raise ValueError('无备注表情尚未预览，未猜测内容或发送')
    return result


def protocol_history(history):
    """Stored dialogue stays readable: a past turn is the words she sent, not a wire format.

    It used to be rewritten as a bubble-JSON envelope because that envelope was how a
    turn was sent. A turn is now sent by calling a tool, so an assistant example that
    still looked like an envelope would teach her the format that no longer sends
    anything; her own words are the example.
    """
    return [{k: v for k, v in message.items() if k != 'fish_bubbles'} for message in history]


def validate_bubbles(root, bubbles, *, check_content=True):
    if not isinstance(bubbles, list):
        raise ValueError('聊天气泡应为列表')
    texts = sum(1 for bubble in bubbles if isinstance(bubble, dict) and bubble.get('kind') == 'text')
    stickers = sum(1 for bubble in bubbles if isinstance(bubble, dict) and bubble.get('kind') == 'sticker')
    if (not MIN_BUBBLES <= len(bubbles) <= MAX_BUBBLES + MAX_STICKERS
            or texts > MAX_BUBBLES or stickers > MAX_STICKERS):
        raise ValueError(f'请一次提交{MIN_BUBBLES}至{MAX_BUBBLES}个完整聊天气泡，表情最多{MAX_STICKERS}张')
    result = []
    library = None
    for bubble in bubbles:
        if not isinstance(bubble, dict):
            raise ValueError('无效的聊天气泡')
        if bubble.get('kind') == 'text':
            text = str(bubble.get('text', '')).strip()
            if not text:
                raise ValueError('文字气泡不能为空')
            if check_content:
                Governance(root).check_output(text)
            result.append({'kind': 'text', 'text': text})
        elif bubble.get('kind') == 'sticker':
            library = library if library is not None else {r['id']: r for r in EmojiStore(root).list()}
            asset = str(bubble.get('emoji_id', ''))
            row = library.get(asset)
            if not row or EmojiStore.inspect(row['path'])[0] != asset:
                raise ValueError('只能发送本地库内、原件有效的表情')
            if check_content:
                Governance(root).check_output(row['description'] + '\n' + row['tags'])
            result.append({'kind': 'sticker', 'emoji_id': asset})
        else:
            raise ValueError('聊天气泡类型仅支持text或sticker')
    return result


def segments(bubble):
    return ([{'type': 'text', 'text': bubble['text']}] if bubble['kind'] == 'text'
            else [{'type': 'image', 'asset_id': bubble['emoji_id']}])


def local_segments(root,bubble):
    parts = segments(bubble)
    if bubble['kind']=='sticker':
        from image_views import ImageViews
        row = next((item for item in EmojiStore(root).list() if item['id']==bubble['emoji_id']),None)
        if row is None: raise ValueError('本地表情原件不存在')
        resource = ImageViews(root).register(row['path'],source='fish_utterance')
        parts[0].update(resource_id=resource['doc_id'],path=row['path'],name=__import__('pathlib').Path(row['path']).name)
    return parts


def bubble_delay(previous):
    return min(4., 1.2 + .035 * len(previous.get('text', '')))


def delivery_plan(bubbles):
    """Resolved semantic boundaries and pacing; the QQ channel owns actual sending."""
    return [{'segments': segments(bubble), 'delay': bubble_delay(bubbles[index-1]) if index else 0.}
            for index, bubble in enumerate(bubbles)]


def transport_parts(bubbles=None, *, plan=None, group=False):
    """The existing QQ chunk boundaries, shared by chat and scheduled delivery."""
    plan = plan if plan is not None else delivery_plan(bubbles)
    parts = []
    for index, item in enumerate(plan):
        wire = [({'type': 'image', 'asset_id': str(s.get('emoji_id') or s.get('asset_id') or '')}
                 if s.get('type') == 'sticker' else s) for s in item['segments']]
        text = ''.join(str(s.get('text') or '') for s in wire if s.get('type') == 'text')
        chunks = []
        while len(text) > 1800:
            cut = max(text.rfind('\n', 900, 1800), text.rfind('。', 900, 1800))
            cut = cut + 1 if cut >= 900 else 1800
            chunks.append(text[:cut])
            text = text[cut:]
        if text:
            chunks.append(text)
        if not chunks:
            chunks = ['']
        media = [s for s in wire if s.get('type') != 'text']
        for chunk_index, chunk in enumerate(chunks):
            suffix = ':bubble:' + str(index + 1) if len(plan) > 1 else ''
            if len(chunks) > 1:
                suffix += ':part:' + str(chunk_index + 1)
            part = {'segments': ([{'type': 'text', 'text': chunk}] if chunk else []) +
                    (media if chunk_index == len(chunks) - 1 else []), 'bubble': index,
                    'delay': item['delay'] if chunk_index == 0 else 0.,
                    'last': chunk_index == len(chunks) - 1, 'key_suffix': suffix}
            body = [s for s in part['segments'] if s.get('type') == 'text']
            images = [s for s in part['segments'] if s.get('type') != 'text']
            if group and body and images:
                parts.append({**part, 'segments': body, 'last': False, 'key_suffix': suffix + ':text'})
                parts.append({**part, 'segments': images, 'delay': 0., 'emoji': True, 'key_suffix': suffix + ':emoji'})
            else:
                parts.append({**part, 'emoji': bool(images) and not body})
    return parts
