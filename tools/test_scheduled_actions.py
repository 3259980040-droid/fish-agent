"""Focused local check: scheduled persistence -> catch-up -> execution -> send receipt, with a fake transport.

No network, no QQ, no model. Uses a temporary root with only the allowlist
(owner id / whitelisted group) and stubs the native DSH schedule service.
Exit code 0 means every assertion passed.
"""
import asyncio
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / 'harness'
sys.path.insert(0, str(ROOT))
import bot_control  # noqa: E402
import send_policy  # noqa: E402
from governance import Governance  # noqa: E402

OWNER = '3259980040'
GROUP = '1043230169'
FUTURE = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()


class Native:
    """Stand-in for the DSH native schedule service (list/create/delete/update)."""
    def __init__(self):
        self.rows = {}

    def __call__(self, body):
        action = body.get('action', 'list')
        if action == 'create':
            rid = 'sched-' + str(len(self.rows) + 1)
            req = body['request']
            kind = {'after_seconds': 'after', 'every_seconds': 'every'}.get(next((k for k in bot_control.TIMING if k in req), 'at'), next((k for k in bot_control.TIMING if k in req), 'at'))
            timeline = {'after_seconds': 'after', 'at': 'at', 'every_seconds': 'every', 'daily': 'daily', 'weekly': 'weekly', 'cron': 'cron'}
            kind = timeline.get(next(k for k in bot_control.TIMING if k in req))
            row = {'id': rid, 'sessionId': body['session'], 'title': req['title'], 'prompt': req['prompt'], 'kind': kind,
                   'status': 'active', 'scheduledAt': FUTURE, 'timeZone': 'Asia/Shanghai', 'lastDelivery': None,
                   **({'everySeconds': req['every_seconds']} if 'every_seconds' in req else {}),
                   **({'time': req['daily']['time']} if 'daily' in req else {})}
            self.rows[rid] = row
            return dict(row)
        if action == 'delete':
            self.rows.pop(body['id'], None)
            return True
        if action == 'update':
            self.rows[body['request']['id']].update({k: v for k, v in body['request'].items() if k in ('title', 'prompt')})
            return {'id': body['request']['id']}
        return [dict(r) for r in self.rows.values()]


def check():
    root = Path(tempfile.mkdtemp(prefix='fish-sched-check-'))
    (root / 'harness').mkdir(parents=True, exist_ok=True)
    (root / 'channels/fish_qq').mkdir(parents=True, exist_ok=True)
    (root / 'channels/fish_qq/config.json').write_text(json.dumps({'allowlist': {'owner_id': OWNER, 'group_ids': [GROUP]}}), encoding='utf-8')
    (root / 'harness/control.json').write_text(json.dumps({'version': 3, 'paused_roles': {'qq': False, 'research': False}}), encoding='utf-8')
    sent, bindings, turns = [], [], []
    native = Native()

    def transport(target, text, request_id):
        sent.append((target, text, request_id))
        return {'ok': True, 'payload': {'message_id': 'msg-' + str(len(sent))}}

    def run_turn(session, prompt):
        turns.append((session, prompt))
        # Simulate the bound model turn calling fish_owner_send with the id given in the prompt
        # (the failure-case turn is the caller's no-op lambda, so only scheduled prompts send).
        if not prompt.startswith('[Fish 定时任务到点执行]'):
            return
        rid = prompt.split('request_id="')[1].split('"')[0]
        try:
            control.send({'scope': 'control:private:' + OWNER, 'session': session}, '今天的日程已整理。', rid)
        except Exception as exc:
            print('send failed:', exc, repr(rid))

    control = bot_control.BotControl(root, transport=transport, session_factory=lambda target: asyncio.sleep(0, result='session-owner-1'))
    control.native = native
    with bot_control.database(control.path) as db:  # the persisted target->session binding _session writes
        db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)', ('private:' + OWNER, 'session-owner-1', 'rev-1'))
    # Stub only the local ToolHost RPC (another worker has a service on the default port).
    control.bind = lambda target, session=None: (bindings.append((target, session)), control.validate_target(target))[1]

    # 1. persistence: model time arguments, original prompt, bound target/session, display.
    reminder = control.execute('schedule_create', {'target': 'owner', 'title': '喝水', 'prompt': '提醒我喝水',
                                                   'after_seconds': [90], 'original': '两分钟后提醒我喝水'})
    daily = control.create({'target': 'owner', 'title': '早报', 'prompt': '整理今天的日程', 'daily': {'time': '09:00', 'time_zone': 'Asia/Shanghai'}})
    rows = control.schedules()
    assert {r['id'] for r in rows} == {reminder['id'], daily['id']}
    r = next(x for x in rows if x['id'] == reminder['id'])
    assert r['original'] == '两分钟后提醒我喝水' and r['target'] == 'private:' + OWNER and r['session'] == 'session-owner-1'
    assert r['timingKey'] == 'after_seconds' and 'Asia/Shanghai' in r['when'] and r['scheduledAtDisplay'].endswith('Asia/Shanghai'), r['when']
    d = next(x for x in rows if x['id'] == daily['id'])
    assert '每天 09:00' in d['when'] and d['nextRunDisplay'] == '', d
    assert daily['when'] == '每天 09:00（Asia/Shanghai）', daily['when']

    # 2. downtime catch-up at most once: the daily task was due 40 minutes ago while Fish was down.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    due_at = now - timedelta(minutes=40)
    assert bot_control.timing_from_row({'every_seconds': 60}, {'kind': 'every'})[0] == {'every_seconds': 60}
    native.rows[daily['id']]['nextRunAt'] = due_at.isoformat()
    native.rows[daily['id']]['lastDelivery'] = {'deliveredAt': (due_at - timedelta(hours=20)).isoformat()}
    first = control.detect(now=now)
    second = control.detect(now=now)
    assert len(first) == 1 and first[0]['state'] == 'due' and first[0]['slot'] == due_at.isoformat(), first
    assert second == [], second  # the same due slot is never re-detected

    # 3. execution: bound session turn -> owner private send by the model -> real receipt; second run is a no-op.
    slot = first[0]['slot']
    native.rows[daily['id']]['lastDelivery'] = {'deliveredAt': slot}
    state = control.run_occurrence(daily['id'], slot, run_turn=run_turn)
    assert bindings[-1] == ('private:' + OWNER, 'session-owner-1'), bindings
    assert turns and turns[0][0] == 'session-owner-1' and '整理今天的日程' in turns[0][1] and 'occurrence_at=' in turns[0][1]
    assert state['state'] == 'sent' and state['receipt_id'], state
    assert sent[-1] == ('private:' + OWNER, '今天的日程已整理。', 'sched:' + daily['id'] + ':' + slot), sent[-1]
    again = control.run_occurrence(daily['id'], slot, run_turn=run_turn)
    assert again['state'] == 'sent' and len(turns) == 1

    # 4. owner private failure becomes a visible failure notice (still the owner route).
    msgs = len(sent)
    failed = control.create({'target': 'owner', 'title': '检查', 'prompt': '检查备份', 'after_seconds': 60})
    slot2 = failed['id'] + '-slot'  # not a real occurrence slot: exercises the execution path only
    control._occurrence(failed['id'], slot2, 'due')
    st = control.run_occurrence(failed['id'], slot2, run_turn=lambda s, p: None)
    assert st['state'] == 'no_send' and len(sent) == msgs + 1 and sent[-1][2].endswith(':notice'), (st, sent[-1:])

    # 5. one slot, one execution: a finished slot stays closed for catch-up and create returns no duplicate.
    assert not control._occurrence(failed['id'], slot2, 'due')

    # 6. send settings: defaults, group needs explicit owner, owner reply can be disabled, unknown rejected.
    cfg = Governance(root).config()
    assert cfg['send_policy']['owner_private'] == {'owner_reply': True, 'scheduled_result': True}
    assert cfg['send_policy']['group']['owner_request'] is True and cfg['send_policy']['group']['public_auto_reply'] is False
    snap = Governance(root).snapshot()
    assert snap['send_policy'] == cfg['send_policy'] and 'qzone_publish' in {c['action'] for c in snap['send_capabilities']}
    group_result = control.execute('message_send', {'target': 'group:' + GROUP, 'text': '主人要求发到群里', 'request_id': 'owner-send:test:g1'},
                                   {'role': 'qq', 'owner': True, 'desktop': False, 'sender': OWNER, 'session': 'session-owner-1', 'scope': 'owner'})
    assert group_result['state'] == 'SUCCEEDED' and sent[-1][0] == 'group:' + GROUP
    Governance(root).save({'send_policy': {'owner_private': {'owner_reply': False}}})
    blocked = control._deliver('private:' + OWNER, '主人私聊回复', 'check-owner-off')
    assert blocked['state'] == 'BLOCKED' and '主人私聊回复' in blocked['error'], blocked
    scheduled = control._deliver('private:' + OWNER, '定时结果', 'sched:' + reminder['id'] + ':x', scheduled=True)
    assert scheduled['state'] == 'SUCCEEDED', scheduled
    try:
        Governance(root).save({'send_policy': {'group': {'public_auto_reply': True}}})
        raise AssertionError('public auto reply must not be a send setting')
    except ValueError:
        pass
    ok, reason = send_policy.decide(root, 'group:999', action='group_send', context='owner')
    assert not ok and '白名单' in reason, reason
    ok, reason = send_policy.decide(root, 'group:' + GROUP, action='group_send', context='')
    assert not ok, reason  # no explicit owner request in context
    summary = send_policy.summary(root)
    assert summary['slots'].get('owner_private') and summary['slots'].get('group'), summary
    print('checks passed:', json.dumps({'schedules': len(rows), 'catch_up': first[0]['state'], 'sent': len(sent),
                                        'slots': {k: list(v) for k, v in summary['slots'].items()}}, ensure_ascii=False))
    shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    check()
