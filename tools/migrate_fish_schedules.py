"""Transfer Fish-owned schedules off DSH without two live timers; default is read-only."""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))
import dependencies
import requests
from storage import database, settings_lock, write_json
from governance import control_token
from core import HarnessConfig
from bot_control import BotControl
from fish_schedules import next_time, moment


def native(root, body, *, offline=False):
    port = HarnessConfig(root).runtime_port('qq')
    if not port:
        raise RuntimeError('旧 DSH 调度服务端口尚不明确，未同时启动第二个定时器')
    if offline:
        import socket
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=3):pass
        except ConnectionRefusedError:pass
        else:raise RuntimeError('旧运行时仍在线，不能使用离线迁移')
        path=HarnessConfig(root).runtime_home('qq')/'storages/schedule.json'
        data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('unit')!={'name':'schedule','version':1} or not isinstance(data.get('tables',{}).get('tasks'),dict):
            raise ValueError('DSH定时存储结构与实际源码不符，未猜测写入')
        tasks=data['tables']['tasks']
        if body['action']=='delete':
            row=tasks.get(body['id'])
            if row and row['sessionId']==body['session']:
                tasks.pop(body['id']);write_json(path,data)
            return {'id':body['id'],'deleted':row is not None}
        return [{'id':key,**value['record'],**{k:v for k,v in value.items() if k!='record'}} for key,value in tasks.items()]
    response = requests.post('http://127.0.0.1:' + str(port) + '/fish/schedules', json=body,
        headers={'x-fish-token': control_token(root)}, timeout=15, proxies={'http': None, 'https': None})
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get('ok') is False:
        raise RuntimeError(str(data.get('error') or '旧调度器拒绝操作'))
    return data.get('value',data.get('result',data)) if isinstance(data,dict) else data


def migrate(root, *, apply=False,offline=False):
    control = BotControl.__new__(BotControl)
    control.root, control.path = Path(root), Path(root) / 'data/social/owner-control.db'
    if not control.path.is_file():
        return {'changed': [], 'pending': [], 'mode': 'apply' if apply else 'read-only'}
    # Read-only mode must not run the schema initializer or mutate old state.
    with sqlite3.connect(control.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        columns = {r[1] for r in db.execute('PRAGMA table_info(scheduled)')}
        query = "SELECT * FROM scheduled WHERE state!='deleted'" + (" AND engine='dsh'" if 'engine' in columns else '')
        old = [dict(r) for r in db.execute(query)]
    if not old:
        return {'changed': [], 'pending': [], 'mode': 'apply' if apply else 'read-only'}
    remote = {row['id']: row for row in native(root,{'action':'list'},offline=offline)}
    preview = [{'id': row['id'], 'state': row['state'], 'native_present': row['id'] in remote,
                'timing': json.loads(row['timing']), 'target': row['target']} for row in old]
    if not apply:
        return {'mode': 'read-only', 'would_change': preview}
    with settings_lock(root):
        backup = root / 'backups/agent-update/schedules' / time.strftime('%Y%m%d-%H%M%S')
        backup.mkdir(parents=True, exist_ok=False)
        with sqlite3.connect(control.path) as original, sqlite3.connect(backup / 'owner-control.db') as copy:
            original.backup(copy)
        write_json(backup / 'native-owned.json', [remote[row['id']] for row in old if row['id'] in remote])
        write_json(backup / 'before.json', old)
        if offline:
            import shutil
            shutil.copy2(HarnessConfig(root).runtime_home('qq')/'storages/schedule.json',backup/'native-schedule.json')
        control = BotControl(root)
        plans = control.plans
        report = {'mode': 'apply', 'backup': str(backup), 'changed': [], 'pending': []}
        for row in old:
            try:
                timing = json.loads(row['timing'])
                created = datetime.fromtimestamp(row['created'], timezone.utc)
                first = next_time(timing, created - timedelta(microseconds=1), created)
                if first is None:
                    raise ValueError('原任务没有可解析的实际时间')
                previous = remote.get(row['id'])
                if previous:
                    # Removal is recorded before enabling Fish. A failure leaves one owner.
                    native(root,{'action':'delete','session':previous['sessionId'],'id':row['id']},offline=offline)
                    if any(item['id']==row['id'] for item in native(root,{'action':'list'},offline=offline)):
                        raise RuntimeError('旧任务删除后仍在旧调度器中，未开启 Fish 副本')
                with database(control.path) as db:
                    successful = db.execute("SELECT receipt FROM sends WHERE (id LIKE ? OR id LIKE ?) AND state='SUCCEEDED'", ('sched:'+row['id']+':%',row['id']+'%')).fetchall()
                    confirmed = any((json.loads(r['receipt'] or '{}').get('payload') or {}).get('message_id') for r in successful)
                    completed = row['timing_key'] in ('after_seconds', 'at') and confirmed
                    attempted=db.execute('SELECT 1 FROM occurrences WHERE schedule_id=?',(row['id'],)).fetchone()
                    unresolved=not completed and bool(attempted or previous and previous.get('status')=='inactive')
                    state='completed' if completed else 'paused' if row['state']=='paused' or unresolved else 'active'
                    db.execute('UPDATE scheduled SET engine=\'fish\',session=?,purpose=\'reminder\',next_at=?,state=?,updated=? WHERE id=?',
                        ('fish-schedule:'+row['id'],None if completed or unresolved else first.isoformat(),state,time.time(),row['id']))
                    if unresolved:db.execute("UPDATE occurrences SET state='uncertain',note='迁移前已发生执行，但没有完整QQ投递凭据；保留历史，不自动重做',updated=? WHERE schedule_id=? AND state='running'",(time.time(),row['id']))
                report['changed'].append({'id':row['id'],'native_removed':bool(previous),'fish_state':state,'previous_attempt_unconfirmed':unresolved})
            except Exception as exc:
                report['pending'].append({'id': row['id'], 'error': str(exc)})
        write_json(backup / 'result.json', report)
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--offline',action='store_true',help='仅在旧DSH运行时关闭时操作其源码定义的schedule v1存储')
    args = parser.parse_args()
    result = migrate(args.root,apply=args.apply,offline=args.offline)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get('pending'):
        raise SystemExit(1)
