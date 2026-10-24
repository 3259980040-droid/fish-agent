"""Deploy the approved stable Agent profile and legacy owner-memory scope migration.

Default is read-only planning. --apply uses the existing root lock, SQLite
backups and source-bound updates. Unknown/group scopes are never guessed.
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))
import dependencies
from storage import settings_lock, database, read_json, write_json


def readonly(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def plan(root):
    config = read_json(root / 'channels/fish_qq/config.json', {})
    owner = str(config.get('allowlist', {}).get('owner_id') or '')
    if not owner:
        raise ValueError('没有已核验的主人配置，不能迁入活动人格')
    path = root / 'data/social/context.db'
    with readonly(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        active = db.execute("SELECT revision FROM agent_profiles WHERE role='qq'").fetchone() if 'agent_profiles' in tables else None
        if not active:
            active = db.execute('SELECT s.revision FROM owner_routes r JOIN spaces s ON r.space=s.id WHERE r.owner=?', (owner,)).fetchone()
        if not active:
            active = db.execute("SELECT revision FROM default_personas WHERE role='qq'").fetchone()
        if not active:
            raise ValueError('没有现存活动人格来源，未创建替代人格或窗口')
        persona = db.execute("SELECT * FROM personas WHERE id=? AND role='qq'", (active['revision'],)).fetchone()
        if not persona:
            raise ValueError('原活动人格缺失，未换默认人格')
        origins = {r['id']: r['origin'] for r in db.execute('SELECT id,origin FROM spaces')}
    memory_path = root / 'data/qq/memory.db'
    memories, unresolved = [], []
    if memory_path.exists():
        with readonly(memory_path) as db:
            for row in db.execute("SELECT id,scope FROM memories WHERE scope LIKE 'space:%'"):
                origin = origins.get(row['scope'][6:])
                if origin in ('owner-qq', 'desktop'):
                    memories.append({'id': row['id'], 'old_scope': row['scope'], 'new_scope': 'owner'})
                elif origin is None:
                    unresolved.append({'id': row['id'], 'scope': row['scope'], 'reason': '没有原窗口来源，未推断归属'})
    registry = read_json(root / 'harness/plugins/registry.json', {'plugins': []})
    plugins = [row['id'] for row in registry['plugins'] if row.get('id') == 'tulpa_owner' and row.get('allowed_tools') is not None]
    return {'profile_revision': persona['id'], 'profile_name': persona['name'], 'owner_memory_moves': memories,
            'unresolved_memory_scopes': unresolved, 'remove_legacy_tool_selection': plugins}


def apply(root, proposed):
    from social_context import SocialContext
    backup = root / 'backups/agent-update' / time.strftime('%Y%m%d-%H%M%S')
    backup.mkdir(parents=True, exist_ok=False)
    for relative in ('data/social/context.db', 'data/qq/memory.db', 'data/qq/desktop-chat.db', 'data/tool-host.db'):
        source = root / relative
        if source.exists():
            target = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with readonly(source) as before, sqlite3.connect(target) as after:
                before.backup(after)
    registry_path = root / 'harness/plugins/registry.json'
    registry = read_json(registry_path, {'plugins': []})
    (backup / 'plugin-registry.json').write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding='utf-8')
    (backup / 'plan.json').write_text(json.dumps(proposed, ensure_ascii=False, indent=2), encoding='utf-8')
    contexts = SocialContext(root)
    contexts.apply_active_persona('qq', proposed['profile_revision'])
    moves = proposed['owner_memory_moves']
    path = root / 'data/qq/memory.db'
    if moves:
        with database(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS memory_scope_migrations(memory_id TEXT,old_scope TEXT,new_scope TEXT,created REAL,PRIMARY KEY(memory_id,old_scope))')
            db.execute('BEGIN IMMEDIATE')
            for move in moves:
                changed = db.execute('UPDATE memories SET scope=? WHERE id=? AND scope=?',
                                     (move['new_scope'], move['id'], move['old_scope'])).rowcount
                if changed:
                    db.execute('INSERT OR IGNORE INTO memory_scope_migrations VALUES(?,?,?,?)',
                               (move['id'], move['old_scope'], move['new_scope'], time.time()))
                    db.execute('UPDATE memory_embeddings SET scope=? WHERE memory_id=?', (move['new_scope'], move['id']))
            db.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")
    # Only exact obsolete protocol/length sentences are replaced. Arbitrary
    # personality traits and owner-authored preferences are left intact.
    persona = contexts.active_persona('qq')
    body = persona['body']
    body = body.replace('，通常在 20—500 字内按问题需要决定，不为凑字数补尾巴。', '，按问题需要决定，不为凑字数补尾巴。')
    body = body.replace('再把sticker气泡交给fish_chat_reply', '再用fish_send_sticker发送')
    body = body.replace('用fish_chat_reply提交完整聊天内容', '用fish_say提交实际要说的完整聊天内容')
    body = re.sub(r'(?<![A-Za-z_])web_search\b', 'fish_web_search', body)
    body = re.sub(r'(?<![A-Za-z_])web_fetch\b', 'fish_web_fetch', body)
    if body != persona['body']:
        revised = contexts.save_persona('qq', persona['name'], body, parent=persona['id'])
        contexts.apply_active_persona('qq', revised['id'])
    for plugin in registry['plugins']:
        if plugin['id'] in proposed['remove_legacy_tool_selection']:
            plugin.pop('allowed_tools', None)
    write_json(registry_path, registry)
    result = {'backup': str(backup), 'profile_revision': contexts.active_persona('qq')['id'],
              'moved_memories': len(moves), 'unresolved_memory_scopes': proposed['unresolved_memory_scopes'],
              'tool_selection_removed': proposed['remove_legacy_tool_selection']}
    (backup / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.apply:
        with settings_lock(root):
            result = apply(root, plan(root))
    else:
        result = plan(root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
