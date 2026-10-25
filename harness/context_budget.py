"""One token budget for actual Fish requests; usage is distinct from capacity.

The same resolution also feeds compression: the input ceiling, the 80% trigger and the
16% verbatim tail are three numbers of one policy scaled by this model's real window,
not three separately read settings and not fixed token limits.
"""
from __future__ import annotations
from locations import project_path

import json
import math
import time
import uuid
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import requests
from storage import database, read_json, write_json

# Initial values from the 2026-10-24 decision; all three are resolved from the real
# window below, so they stay a policy rather than promoted constants.
DEFAULT_INPUT_RATIO = .08
DEFAULT_THRESHOLD_RATIO = .8
DEFAULT_RETAIN_RATIO = .16
DEFAULT_HEADROOM_TOKENS = 0


@lru_cache(maxsize=8)
def _tokenizer(path):
    from tokenizers import Tokenizer
    return Tokenizer.from_file(path)


def model_metadata(root, gateway):
    """Resolve real provider metadata, cached by provider + model, never by alias alone."""
    root = Path(root)
    path = project_path(root, 'data/social/model-metadata.json')
    records = read_json(path, {})
    provider = str(gateway.base).rstrip('/')
    key = provider + '|' + str(gateway.model)
    cached = records.get(key)
    if isinstance(cached, dict) and time.time() - float(cached.get('fetched_at', 0)) < 3600:
        return cached
    from network_config import session
    with session(root) as connection:
        response = connection.get(provider + '/models', headers={'Authorization': 'Bearer ' + gateway.key}, timeout=20)
    response.raise_for_status()
    selected = next((m for m in response.json().get('data', []) if m.get('id') == gateway.model), None)
    if not selected or not isinstance(selected.get('context_window'), int) or selected['context_window'] <= 0:
        raise ValueError('当前模型没有返回有效上下文窗口，未使用旧字符上限或其他模型容量代替')
    result = {k: selected[k] for k in ('id', 'context_window', 'max_output_tokens', 'input_modalities') if k in selected}
    result.update(provider=provider, fetched_at=time.time(), source=provider + '/models')
    records[key] = result
    write_json(path, records)
    return result


def current_metadata(root):
    """The configured model's cached real metadata, or None.

    Only an exact provider+model record counts: another model's cached window is not a
    capacity estimate for this one. This is a read-only lookup; it never fetches.
    """
    root = Path(root)
    records = read_json(project_path(root, 'data/social/model-metadata.json'), {}) or {}
    try:
        from core import HarnessConfig
        agent = HarnessConfig(root).agent('qq')
    except Exception:
        agent = None
    if agent is None:
        return None
    model = str(getattr(agent, 'model', '') or '')
    base = str(getattr(agent, 'base_url', '') or 'https://api.deepseek.com').rstrip('/')
    record = records.get(base + '|' + model)
    if isinstance(record, dict) and isinstance(record.get('context_window'), int) and record['context_window'] > 0:
        return record
    return None


def settings(root):
    """One read for runtime numbers: control.json, then the owner-facing override.

    Governance rewrites control.json; numbers the owner typed in context-settings.json
    must survive that, so the override is applied on top instead of being read twice.
    """
    root = Path(root)
    values = dict(read_json(project_path(root, 'harness/control.json')))
    values.update(read_json(project_path(root, 'harness/context-settings.json'), {}) or {})
    return values


def _ratio(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(name + '需要是数字') from None
    if not 0 < number <= 1:
        raise ValueError(name + '应在 0 到 1 之间')
    return number


def policy(root):
    """Resolve the one compression/budget policy used by every caller."""
    values = settings(root)
    input_ratio = _ratio(values.get('context_window_ratio', DEFAULT_INPUT_RATIO), '上下文预算比例')
    threshold_ratio = _ratio(values.get('compact_trigger_ratio', DEFAULT_THRESHOLD_RATIO), '压缩触发比例')
    retain_ratio = _ratio(values.get('retain_ratio', DEFAULT_RETAIN_RATIO), '最近原文保留比例')
    if retain_ratio >= threshold_ratio:
        raise ValueError('最近原文保留比例应小于压缩触发比例，否则压缩不会释放空间')
    try:
        headroom = int(values.get('compaction_headroom_tokens', DEFAULT_HEADROOM_TOKENS))
    except (TypeError, ValueError):
        raise ValueError('压缩余量需要是整数 token') from None
    if headroom < 0:
        raise ValueError('压缩余量不能为负数')
    return {'input_ratio': input_ratio, 'threshold_ratio': threshold_ratio,
            'retain_ratio': retain_ratio, 'headroom_tokens': headroom,
            'auto': str(values.get('context_compaction', 'auto')) != 'off',
            'source': 'harness/control.json + harness/context-settings.json',
            'keys': {'input_ratio': 'context_window_ratio', 'threshold_ratio': 'compact_trigger_ratio',
                     'retain_ratio': 'retain_ratio', 'auto': 'context_compaction'}}


def resolve_spec(values, window, output_reserve):
    """Scale one policy to this model's token numbers (DSH resolveCompactSpec).

    The input ceiling is the working budget; pressure is capped by the ceiling left
    after the compression headroom; the retained tail is a fraction of that same
    ceiling. All three are budgets (upper bounds), never targets to fill.
    """
    window, output_reserve = int(window), int(output_reserve)
    if window <= 0:
        raise ValueError('当前模型上下文窗口无效，未使用估计容量代替')
    if output_reserve < 0:
        raise ValueError('模型输出预留不能为负数')
    message_budget = window - output_reserve
    if message_budget <= 0:
        raise ValueError('模型输出预留已占满上下文窗口')
    ceiling = min(math.floor(values['input_ratio'] * window), message_budget)
    if ceiling <= 0:
        raise ValueError('当前模型窗口与输入比例算不出可用输入预算')
    pressure_budget = ceiling - int(values['headroom_tokens'])
    if pressure_budget <= 0:
        raise ValueError('压缩余量已占满输入预算，自动压缩不会释放空间')
    threshold = math.floor(min(ceiling * values['threshold_ratio'], pressure_budget))
    retain = math.floor(ceiling * values['retain_ratio'])
    if threshold <= 0:
        raise ValueError('压缩触发预算小于 1 token')
    if retain >= threshold:
        raise ValueError('最近原文保留预算不小于压缩触发预算，压缩不会释放空间')
    return {'window': window, 'output_reserve': output_reserve, 'input_ceiling': ceiling,
            'threshold_ratio': values['threshold_ratio'], 'threshold_tokens': threshold,
            'retain_ratio': values['retain_ratio'], 'retain_tokens': retain,
            'headroom_tokens': int(values['headroom_tokens']), 'auto': values['auto'],
            'input_ratio': values['input_ratio'], 'source': values['source']}


class ContextBudget:
    """Keep optional materials within one cap, with complete per-request accounting."""
    def __init__(self, root, gateway, *, metadata=None, tokenizer=None):
        self.root = Path(root)
        self.gateway = gateway
        self.metadata = metadata if metadata is not None else model_metadata(root, gateway)
        self.window = int(self.metadata['context_window'])
        self.policy = policy(self.root)
        self.ratio = self.policy['input_ratio']
        model = str(self.metadata.get('id') or gateway.model)
        if tokenizer is not None:
            self.tokenizer = tokenizer
        else:
            if not model.startswith(('deepseek-flash', 'deepseek-v4')):
                raise ValueError('当前模型尚未配备匹配的分词器，未将字符数当作token')
            path = project_path(self.root, 'vendor/tokenizers/deepseek-v4/tokenizer.json')
            if not path.is_file():
                raise ValueError('DeepSeek V4官方分词器尚未就绪，请运行项目的依赖准备入口')
            self.tokenizer = _tokenizer(str(path))
        self.materials = {}
        self.turn_id = uuid.uuid4().hex
        self.path = project_path(self.root, 'data/social/context-usage.db')
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS context_requests('
                       'id TEXT PRIMARY KEY,session TEXT,origin TEXT,created REAL,budget TEXT,usage TEXT,error TEXT,turn_id TEXT)')
            if 'turn_id' not in {row[1] for row in db.execute('PRAGMA table_info(context_requests)')}:
                db.execute("ALTER TABLE context_requests ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''")

    def ceiling(self, output_reserve):
        """The same working budget ``fit`` enforces, for callers pricing their own request."""
        return resolve_spec(self.policy, self.window, output_reserve)['input_ceiling']

    def tokens(self, value):
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        return len(self.tokenizer.encode(value, add_special_tokens=False).ids)

    def message_tokens(self, message):
        # Wrapper costs and image conversion remain estimates; server usage is authoritative.
        total = 8 + self.tokens(str(message.get('role') or ''))
        content = message.get('content') or ''
        if isinstance(content, list):
            for block in content:
                kind = block.get('type')
                if kind in ('image_url', 'image', 'input_image', 'file'):
                    total += 1024  # Official V4 upper bound per image, independent of base64 bytes.
                elif kind == 'text':
                    total += self.tokens(block.get('text', ''))
                else:
                    total += self.tokens(block)
        else:
            total += self.tokens(content)
        for field in ('tool_calls', 'tool_call_id', 'name', 'reasoning_content'):
            if message.get(field):
                total += self.tokens(message[field])
        return total

    def fit(self, messages, tools, output_reserve):
        spec = resolve_spec(self.policy, self.window, output_reserve)
        budget = spec['input_ceiling']
        for message in messages:
            if '_fish_layer' in message or '_fish_refs' in message:
                self.materials[id(message)] = (int(message.pop('_fish_layer', 1)), message.pop('_fish_refs', []), message.pop('_fish_bundle', str(id(message))))
            message.pop('fish_bubbles', None)
        tool_cost = self.tokens(tools) if tools else 0
        costs = {id(m): self.message_tokens(m) for m in messages}
        before = {str(layer): 0 for layer in range(6)}
        for m in messages:
            layer, _, _ = self.materials.get(id(m), (0, [], ''))
            before[str(layer)] += costs[id(m)]
        before['0'] += tool_cost
        fixed = before['0']
        # Pressure is what this request would cost if nothing were optional: the same
        # number compression compares with its trigger, so both read one measurement.
        pressure = sum(before.values())
        if fixed > budget:
            raise ValueError('当前输入、工具协议和必要定义超过模型本轮输入预算；原记录保留，请按资源片段取用')
        total = sum(before.values())
        removed = []
        # The least valuable optional layer leaves first; a raw record is never deleted.
        for layer in (5, 4, 3, 2, 1):
            for message in list(messages):
                if total <= budget:
                    break
                item_layer, refs, bundle = self.materials.get(id(message), (0, [], ''))
                if item_layer != layer:
                    continue
                group = [m for m in messages if self.materials.get(id(m), (0, [], ''))[2] == bundle]
                if not group:
                    continue
                group_ids = {id(m) for m in group}
                group_refs = [ref for m in group for ref in self.materials[id(m)][1]]
                group_cost = sum(costs[id(m)] for m in group)
                messages[:] = [m for m in messages if id(m) not in group_ids]
                total -= group_cost
                removed.append({'layer': layer, 'references': group_refs, 'tokens': group_cost})
        after = {str(layer): 0 for layer in range(6)}
        covered = []
        for message in messages:
            layer, refs, _ = self.materials.get(id(message), (0, [], ''))
            after[str(layer)] += costs[id(message)]
            covered.extend(refs)
        after['0'] += tool_cost
        report = {'model': self.metadata['id'], 'model_window': self.window,
                  'metadata_source': self.metadata.get('source'), 'metadata_fetched_at': self.metadata.get('fetched_at'),
                  'ratio': self.ratio, 'input_ceiling': budget, 'output_reserve': int(output_reserve),
                  'estimated_input_tokens': total, 'pressure_tokens': pressure,
                  'fixed_tokens': fixed, 'tool_definition_tokens': tool_cost,
                  'count_source': 'official-deepseek-v4-tokenizer; message wrappers and image tokens estimated',
                  'is_estimate': True, 'layers_before': before, 'layers_after': after,
                  'trimmed': removed, 'covered': covered, 'ceiling_is_target': False,
                  # Compression reads the same resolution, not a second settings read.
                  'threshold_ratio': spec['threshold_ratio'], 'threshold_tokens': spec['threshold_tokens'],
                  'retain_ratio': spec['retain_ratio'], 'retain_tokens': spec['retain_tokens'],
                  'compaction_auto': spec['auto'], 'compaction_headroom_tokens': spec['headroom_tokens'],
                  'budget_policy_source': spec['source']}
        return report

    def begin(self, session, origin, report):
        request_id = uuid.uuid4().hex
        with database(self.path) as db:
            db.execute('INSERT INTO context_requests(id,session,origin,created,budget,usage,error,turn_id) VALUES(?,?,?,?,?,?,?,?)',
                       (request_id, session, origin, time.time(), json.dumps(report, ensure_ascii=False), '', '', self.turn_id))
        return request_id

    def finish(self, request_id, usage=None, error=''):
        with database(self.path) as db:
            db.execute('UPDATE context_requests SET usage=?,error=? WHERE id=?',
                       (json.dumps(usage or {}, ensure_ascii=False), str(error)[:500], request_id))


def effective(root):
    """The resolved policy for this model, for the client; read-only and never guesses."""
    try:
        values = policy(root)
    except Exception as exc:
        return {'error': str(exc), 'source': 'harness/control.json + harness/context-settings.json'}
    try:
        output_reserve = int(settings(root).get('desktop_chat_max_tokens', 8192))
    except (TypeError, ValueError):
        output_reserve = 8192
    metadata = current_metadata(root)
    result = {'input_ratio': values['input_ratio'], 'threshold_ratio': values['threshold_ratio'],
              'retain_ratio': values['retain_ratio'], 'headroom_tokens': values['headroom_tokens'],
              'auto': values['auto'], 'output_reserve': output_reserve,
              'model': metadata.get('id') if metadata else None,
              'window': metadata.get('context_window') if metadata else None,
              'source': values['source'], 'keys': values['keys']}
    if metadata is None:
        result.update(unresolved='当前模型上下文窗口元数据尚未取得，未使用其他模型容量代替')
        return result
    try:
        spec = resolve_spec(values, metadata['context_window'], output_reserve)
    except ValueError as exc:
        result['error'] = str(exc)
        return result
    result.update({key: spec[key] for key in ('input_ceiling', 'threshold_tokens', 'retain_tokens')})
    result['window_source'] = metadata.get('source')
    return result


def snapshot(root, session='', limit=20):
    path = project_path(Path(root), 'data/social/context-usage.db')
    if not path.exists():
        return {'requests': [], 'latest': None, 'effective': effective(root)}
    import sqlite3
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        query = 'SELECT * FROM context_requests' + (' WHERE session=?' if session else '') + ' ORDER BY created DESC LIMIT ?'
        rows = db.execute(query, (session, limit) if session else (limit,)).fetchall()
        if not rows:
            return {'requests': [], 'latest': None, 'effective': effective(root)}
        latest_turn = rows[0]['turn_id']
        whole_turn = db.execute('SELECT usage FROM context_requests WHERE turn_id=?', (latest_turn,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['budget'] = json.loads(item['budget'])
        item['usage'] = json.loads(item['usage'] or '{}')
        result.append(item)
    usages = [json.loads(row['usage']) for row in whole_turn if row['usage'] and json.loads(row['usage'])]
    latest = result[0]
    summary = {k: latest['budget'][k] for k in ('model_window','input_ceiling','estimated_input_tokens','output_reserve')}
    summary.update(turn_requests=len(whole_turn), usage_reports=len(usages), turn_id=latest_turn,
                   input_tokens=sum(int(u.get('prompt_tokens',u.get('input_tokens',0))) for u in usages) if usages else None,
                   output_tokens=sum(int(u.get('completion_tokens',u.get('output_tokens',0))) for u in usages) if usages else None,
                   trimmed_layers=sorted({int(r['layer']) for r in latest['budget']['trimmed']}))
    summary['pressure_tokens'] = latest['budget'].get('pressure_tokens')
    summary['threshold_tokens'] = latest['budget'].get('threshold_tokens')
    summary['retain_tokens'] = latest['budget'].get('retain_tokens')
    return {'requests':result, 'latest':summary, 'effective': effective(root)}
