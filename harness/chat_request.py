"""Compose one Fish request; optional layers remain visible to its one token budget."""
from copy import deepcopy


def now_line() -> str:
    """The one fact she cannot infer: what time it is where she lives."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    at = datetime.now(ZoneInfo('Asia/Shanghai'))
    return '现在是 ' + at.strftime('%Y-%m-%d %H:%M:%S') + ' ' + '星期' + '一二三四五六日'[at.weekday()] + '（Asia/Shanghai）'


def assemble_chat_request(*, persona, history, memory, expression, references, current, sources,
                          runtime_state=''):
    # The clock must not sit in the prefix: a per-second timestamp there invalidates
    # the cached prefix on every single turn. It rides with the newest message instead,
    # so persona + rules + history stay byte-identical between turns.
    fixed = '\n'.join(v for v in (persona, expression['prompt']) if v)
    messages = [{'role': 'system', 'content': fixed}] if fixed else []
    # Volatile layers (recalled memory, references) change every turn; anything placed
    # after them can never be cached. They go last, after the append-only history.
    recalled = []
    for entry in deepcopy(history):
        entry.setdefault('_fish_layer', 1)
        if entry['_fish_layer'] == 4:
            recalled.append(entry)
        else:
            messages.append(entry)
    current_ref = sources.get('history', {}).get('current_reference') if isinstance(sources.get('history'), dict) else None
    for value, layer in ((memory, 4), (references, 4)):
        if value:
            messages.append({'role': 'system', 'content': value, '_fish_layer': layer})
    # Retrieved originals are a reference list, newest first. Budget pressure can
    # release its oldest tail before dropping the actual memory/reference blocks.
    messages.extend(recalled)
    if runtime_state:
        messages.append({'role': 'system', 'content': runtime_state, '_fish_layer': 0})
    stamp = now_line()
    if isinstance(current, str):
        newest = stamp + '\n' + current
    elif isinstance(current, list):
        newest = [{'type': 'text', 'text': stamp}] + deepcopy(current)
    else:
        newest = deepcopy(current)
    messages.append({'role': 'user', 'content': newest, '_fish_layer': 0,
                     '_fish_refs': [current_ref] if current_ref else []})
    return {'messages': messages, 'sources': deepcopy(sources)}
