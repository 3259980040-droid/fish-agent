"""Semantic chat queues, following Tulpa's whole-bubble send protocol.

Reference: Tulpa cf15cf5987466c97a493bf1f1771dae99b1fcac8,
doc/MCP_CHAT_TURNS.md and MCP_STICKERS.md (MIT). This adapts the protocol to
Fish's existing verified owner route and transport; no punctuation splitter.
"""
from governance import Governance
from emoji_store import EmojiStore


def validate_bubbles(root, bubbles):
    if not isinstance(bubbles, list) or not 1 <= len(bubbles) <= 3:
        raise ValueError('请一次提交1至3个完整聊天气泡')
    result = []
    library = None
    for bubble in bubbles:
        if not isinstance(bubble, dict):
            raise ValueError('无效的聊天气泡')
        if bubble.get('kind') == 'text':
            text = str(bubble.get('text', '')).strip()
            if not text or len(text) > 1800:
                raise ValueError('文字气泡需为1至1800字')
            Governance(root).check_output(text)
            result.append({'kind': 'text', 'text': text})
        elif bubble.get('kind') == 'sticker':
            library = library if library is not None else {r['id']: r for r in EmojiStore(root).list()}
            asset = str(bubble.get('emoji_id', ''))
            row = library.get(asset)
            if not row or EmojiStore.inspect(row['path'])[0] != asset:
                raise ValueError('只能发送本地库内、原件有效的表情')
            Governance(root).check_output(row['description'] + '\n' + row['tags'])
            result.append({'kind': 'sticker', 'emoji_id': asset})
        else:
            raise ValueError('聊天气泡类型仅支持text或sticker')
    return result


def segments(bubble):
    return ([{'type': 'text', 'text': bubble['text']}] if bubble['kind'] == 'text'
            else [{'type': 'image', 'asset_id': bubble['emoji_id']}])


def bubble_delay(previous):
    return min(4., 1.2 + .035 * len(previous.get('text', '')))
