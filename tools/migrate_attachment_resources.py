"""Index existing original attachments in the canonical resource store; default is read-only."""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))
import dependencies


def inventory(root):
    path = root / 'data/qq/attachments.db'
    if not path.is_file():
        return []
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        columns = {r[1] for r in db.execute('PRAGMA table_info(attachments)')}
        if not columns:
            return []
        return [{**dict(row), 'original_exists':Path(row['path']).is_file()} for row in db.execute(
            'SELECT id,conversation_id,path,source,source_ref,name FROM attachments' +
            (" WHERE resource_id=''" if 'resource_id' in columns else ''))]


def migrate(root, *, apply=False):
    rows = inventory(root)
    result = {'mode':'apply' if apply else 'read-only', 'inventory':rows, 'changed':[], 'failed':[]}
    if not apply or not rows:
        return result
    from storage import settings_lock, write_json
    backup = root / 'backups/agent-update/attachments' / time.strftime('%Y%m%d-%H%M%S')
    backup.mkdir(parents=True, exist_ok=False)
    with settings_lock(root):
        for name in ('attachments.db', 'memory.db'):
            original = root / 'data/qq' / name
            if original.is_file():
                with sqlite3.connect(original) as db, sqlite3.connect(backup / name) as target:
                    db.backup(target)
        write_json(backup / 'before.json', rows)
    from attachment_store import AttachmentStore
    store = AttachmentStore(root, 'qq')
    for row in rows:
        if not row['original_exists']:
            result['failed'].append({'id':row['id'], 'error':'原件缺失；没有重建或伪装恢复原件，旧索引保留在原库'})
            continue
        try:
            current = store.import_path(row['conversation_id'], row['path'], source=row['source'],
                                        source_ref=row['source_ref'], name=row['name'])
            if not current['resource_id']:
                raise ValueError(current['error'] or '统一资源编号未生成')
            result['changed'].append({'id':row['id'], 'resource_id':current['resource_id'],
                                      'status':current['status'], 'size':current['size'], 'processing':current['processing']})
        except Exception as exc:
            result['failed'].append({'id':row['id'], 'error':str(exc)})
    result['backup'] = str(backup)
    write_json(backup / 'result.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    outcome = migrate(args.root, apply=args.apply)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    if outcome['failed']:
        raise SystemExit(1)
