"""One token budget for actual Fish requests; usage is distinct from capacity."""
from __future__ import annotations

import json
import math
import time
import uuid
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import requests
from storage import database, read_json, write_json


@lru_cache(maxsize=8)
def _tokenizer(path):
    from tokenizers import Tokenizer
    return Tokenizer.from_file(path)


def model_metadata(root, gateway):
    """Resolve real provider metadata, cached by provider + model, never by alias alone."""
    root = Path(root)
    path = root / 'data/social/model-metadata.json'
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


class ContextBudget:
    """Keep optional materials within one cap, with complete per-request accounting."""
    def __init__(self, root, gateway, *, metadata=None):
        self.root = Path(root)
        self.gateway = gateway
        self.metadata = metadata if metadata is not None else model_metadata(root, gateway)
        self.window = int(self.metadata['context_window'])
        ratio = float(read_json(self.root / 'harness/control.json').get('context_window_ratio', .30))
        if not .25 <= ratio <= .35:
            raise ValueError('上下文预算比例应在25%到35%之间')
        self.ratio = ratio
        model = str(self.metadata.get('id') or gateway.model)
        if not model.startswith(('deepseek-flash', 'deepseek-v4')):
            raise ValueError('当前模型尚未配备匹配的分词器，未将字符数当作token')
        path = self.root / 'vendor/tokenizers/deepseek-v4/tokenizer.json'
        if not path.is_file():
            raise ValueError('DeepSeek V4官方分词器尚未就绪，请运行项目的依赖准备入口')
        self.tokenizer = _tokenizer(str(path))
        self.materials = {}
        self.turn_id = uuid.uuid4().hex
        self.path = self.root / 'data/social/context-usage.db'
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS context_requests('
                       'id TEXT PRIMARY KEY,session TEXT,origin TEXT,created REAL,budget TEXT,usage TEXT,error TEXT,turn_id TEXT)')
            if 'turn_id' not in {row[1] for row in db.execute('PRAGMA table_info(context_requests)')}:
                db.execute("ALTER TABLE context_requests ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''")

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
        budget = min(math.floor(self.ratio * self.window), self.window - int(output_reserve))
        if budget <= 0:
            raise ValueError('模型输出预留已占满上下文窗口')
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
                  'estimated_input_tokens': total, 'fixed_tokens': fixed, 'tool_definition_tokens': tool_cost,
                  'count_source': 'official-deepseek-v4-tokenizer; message wrappers and image tokens estimated',
                  'is_estimate': True, 'layers_before': before, 'layers_after': after,
                  'trimmed': removed, 'covered': covered, 'ceiling_is_target': False}
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


def snapshot(root, session='', limit=20):
    path = Path(root) / 'data/social/context-usage.db'
    if not path.exists():
        return {'requests': [], 'latest': None}
    import sqlite3
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        query = 'SELECT * FROM context_requests' + (' WHERE session=?' if session else '') + ' ORDER BY created DESC LIMIT ?'
        rows = db.execute(query, (session, limit) if session else (limit,)).fetchall()
        if not rows:
            return {'requests': [], 'latest': None}
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
    return {'requests':result, 'latest':summary}
