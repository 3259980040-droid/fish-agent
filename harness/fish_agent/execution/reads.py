"""Read the original trace and request facts; no display state is persisted here."""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

from locations import project_path


def read_trace(root, reference, *, main_only=True):
    empty = {'id': '', 'reference': reference, 'session': '', 'phase': '',
             'state': 'missing', 'has_reasoning': False, 'reasoning': '', 'events': [], 'error': ''}
    path = project_path(Path(root), 'data/social/experience.db')
    if not reference or not path.is_file():
        return empty
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='turn_traces'").fetchone():
            return empty
        row = db.execute('SELECT * FROM turn_traces WHERE reference=? ' + ('AND child=0 ' if main_only else '') +
                         'ORDER BY updated DESC,id DESC LIMIT 1', (reference,)).fetchone()
    if row is None:
        return empty
    events = json.loads(row['events'] or '[]')
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise ValueError('回合工具记录格式错误，未按空记录显示')
    reasoning = str(row['reasoning'] or '')
    return {'id': row['id'], 'reference': reference, 'session': row['session'],
            'phase': row['phase'], 'state': 'recorded', 'has_reasoning': bool(reasoning),
            'reasoning': reasoning, 'events': events, 'error': str(row['error'] or '')}


def actions(events):
    """A brief view over actual same-call events; missing start times stay missing."""
    result, pending = [], {}
    for event in events:
        kind, value = event.get('kind'), event.get('value')
        if kind not in ('tool_start', 'tool_result') or not isinstance(value, dict):
            continue
        call = str(value.get('call_id') or '')
        if kind == 'tool_start':
            item = {'call_id': call, 'name': str(value.get('name') or ''),
                    'state': 'started', 'started': event.get('at'), 'finished': None}
            result.append(item)
            if call:
                pending[call] = item
        else:
            item = pending.pop(call, None) if call else None
            if item is None:
                item = {'call_id': call, 'name': str(value.get('name') or ''), 'started': None}
                result.append(item)
            output = value.get('result')
            item.update(state='failed' if isinstance(output, dict) and 'error' in output else 'succeeded',
                        finished=event.get('at'))
    return result


def outputs(events, delivery, target):
    """Generated prose and actual message attempts retain separate meanings."""
    result, calls = [], {}
    def state(receipt, default='queued'):
        if not isinstance(receipt, dict):
            return default
        value = str(receipt.get('state') or '').lower()
        if receipt.get('sent') or value in ('sent', 'succeeded'):
            return 'sent'
        if value in ('uncertain', 'unknown'):
            return 'uncertain'
        if value in ('failed', 'blocked') or receipt.get('error'):
            return 'failed'
        return default
    for event in events:
        kind, value = event.get('kind'), event.get('value')
        if kind == 'final_content' and isinstance(value, str) and value:
            result.append({'text': value, 'kind': 'generated', 'state': 'generated',
                           'target': '', 'call_id': ''})
        elif kind == 'tool_start' and isinstance(value, dict) and value.get('call_id'):
            calls[value['call_id']] = value
        elif kind == 'tool_result' and isinstance(value, dict):
            call = calls.pop(value.get('call_id'), None)
            if not call or call.get('name') not in ('fish_say', 'fish_message_send'):
                continue
            arguments, receipt = call.get('arguments') or {}, value.get('result') or {}
            if not isinstance(arguments, dict) or not isinstance(receipt, dict):
                continue
            queued = call['name'] == 'fish_say' and receipt.get('status') == 'staged'
            if call['name'] == 'fish_say' and not queued:
                continue
            actual = receipt.get('delivery') if isinstance(receipt.get('delivery'), dict) else receipt
            outcome = state(delivery) if queued else state(actual, 'generated')
            for text in arguments.get('texts') or []:
                if isinstance(text, str):
                    result.append({'text': text, 'kind': 'message', 'state': outcome,
                                   'target': target if queued else str(arguments.get('target') or ''),
                                   'call_id': str(value.get('call_id') or '')})
    return result


def output_speed():
    # The old first_output includes reasoning/tool deltas; completion tokens are
    # not a content-only counter. Neither is a truthful content decode reading.
    return {'scope': 'content', 'tokens_per_second': None,
            'reason': 'missing_content_timing_and_tokens'}


def _count(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value >= 0 else None


def _tokens(usage):
    """Normalize only reported provider buckets, keeping absent fields unknown."""
    def field(*names):
        return next((number for name in names if (number := _count(usage.get(name))) is not None), None)
    prompt = field('prompt_tokens', 'input_tokens')
    read = field('prompt_cache_hit_tokens', 'cache_read_input_tokens', 'cacheReadTokens')
    if read is None and isinstance(usage.get('prompt_tokens_details'), dict):
        read = _count(usage['prompt_tokens_details'].get('cached_tokens'))
    write = field('cache_creation_input_tokens', 'cacheWriteTokens')
    uncached = field('prompt_cache_miss_tokens', 'uncached_input_tokens', 'inputTokens')
    # DSH camelCase inputTokens is the uncached bucket; provider prompt_tokens
    # already includes cached input and must never have cache hits added twice.
    if prompt is None and uncached is not None and read is not None and write is not None:
        prompt = uncached + read + write
    return {'input_tokens': prompt, 'output_tokens': field('completion_tokens', 'output_tokens', 'outputTokens'),
            'cache_read_tokens': read, 'cache_write_tokens': write, 'uncached_input_tokens': uncached}


def usage(root, *, trace_id=None):
    """Exact main-turn joins over existing trace IDs and context request IDs.

    Old rows without a saved execution association remain unlinked. They are not
    assigned by time, session, origin, or the currently selected window.
    """
    root = Path(root)
    requests, owners = [], {}
    path = project_path(root, 'data/social/context-usage.db')
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='context_requests'").fetchone():
                requests = [dict(row) for row in db.execute('SELECT * FROM context_requests')]
    path = project_path(root, 'data/social/experience.db')
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='turn_traces'").fetchone():
                rows = db.execute('SELECT id,events FROM turn_traces WHERE child=0').fetchall()
                for identifier, raw in rows:
                    events = json.loads(raw or '[]')
                    for event in events:
                        if isinstance(event, dict) and event.get('kind') == 'execution' and isinstance(event.get('value'), str):
                            owners[identifier] = event['value']
                            break
    linked = [row for row in requests if row.get('turn_id') in owners
              and (trace_id is None or row['turn_id'] == trace_id)]
    recorded = requests if trace_id is None else [row for row in requests if trace_id and row.get('turn_id') == trace_id]
    closed = [row for row in linked if row.get('usage') != '']
    reports = []
    for row in closed:
        value = json.loads(row.get('usage') or '{}')
        if not isinstance(value, dict):
            raise ValueError('回合用量记录格式错误，未按零用量显示')
        reports.append(_tokens(value))
    fields = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'uncached_input_tokens')
    totals = {field: sum(values) if (values := [item[field] for item in reports if item[field] is not None]) else None
              for field in fields}
    field_reports = {field: sum(item[field] is not None for item in reports) for field in fields}
    reported = sum(any(value is not None for value in item.values()) for item in reports)
    complete = bool(closed) and len(closed) == len(linked) and len(recorded) == len(linked) \
        and all(field_reports[field] == len(closed) for field in ('input_tokens', 'output_tokens'))
    ratio = None
    if closed and totals['input_tokens'] and field_reports['input_tokens'] == field_reports['cache_read_tokens'] == len(closed):
        ratio = totals['cache_read_tokens'] / totals['input_tokens']
        if not 0 <= ratio <= 1:
            ratio = None
    return {'scope': 'instance_main_execution' if trace_id is None else 'turn',
            'state': 'reported' if complete else ('partial' if linked else 'unknown'),
            'request_count': len(linked), 'closed_steps': len(closed),
            'turns': len({owners[row['turn_id']] for row in closed}), 'usage_reports': reported,
            **totals, 'cache_hit_ratio': ratio, 'field_reports': field_reports,
            'coverage': {'recorded_requests': len(recorded), 'linked_requests': len(linked),
                         'unlinked_requests': len(recorded) - len(linked)}}
