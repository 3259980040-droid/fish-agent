"""Bounded provider thinking excerpts for explicitly authorized trial replies."""
import re
from request_inspector import redact

THINKING_EXCERPT_CHARS = 160


def format_owner_reply(final_text, reasoning, mode='final'):
    if mode == 'final':
        return final_text
    if mode != 'thinking_excerpt':
        raise ValueError('未知主人测试输出方式')
    text = re.sub(r'\s+', ' ', redact(reasoning or '')).strip()
    if not text:
        excerpt = '本轮未返回可展示的思考内容。'
    else:
        excerpt = text[:THINKING_EXCERPT_CHARS]
        if len(text) > THINKING_EXCERPT_CHARS:
            excerpt += '…（已截断）'
    return '思考片段（API 返回）：' + excerpt + '\n回复：' + final_text
