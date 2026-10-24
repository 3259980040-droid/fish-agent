"""Compose one Fish request; optional layers remain visible to its one token budget."""
from copy import deepcopy


def now_line() -> str:
    """The one fact she cannot infer: what time it is where she lives."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    at = datetime.now(ZoneInfo('Asia/Shanghai'))
    return '现在是 ' + at.strftime('%Y-%m-%d %H:%M:%S') + ' ' + '星期' + '一二三四五六日'[at.weekday()] + '（Asia/Shanghai）'


def assemble_chat_request(*, persona, history, memory, expression, references, current, sources):
    fixed = '\n'.join(v for v in (persona, now_line(), expression['prompt']) if v)
    messages = [{'role': 'system', 'content': fixed}] if fixed else []
    for value, layer in ((memory, 4), (references, 4)):
        if value:
            messages.append({'role': 'system', 'content': value, '_fish_layer': layer})
    for entry in deepcopy(history):
        entry.setdefault('_fish_layer', 1)
        messages.append(entry)
    current_ref = sources.get('history', {}).get('current_reference') if isinstance(sources.get('history'), dict) else None
    messages.append({'role': 'user', 'content': deepcopy(current), '_fish_layer': 0,
                     '_fish_refs': [current_ref] if current_ref else []})
    return {'messages': messages, 'sources': deepcopy(sources)}
