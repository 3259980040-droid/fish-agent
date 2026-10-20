"""Owner operations backed by real persona, group, DSH schedule and QQ services.

Scheduled actions keep the native DSH schedule (persistence, timer, wake-up) and
add Fish's own execution record around it:

* every Fish schedule row stores the owner's original prompt, model time
  expression (after/at/daily/every/weekly/cron + time zone), the bound runtime
  session and the target ('owner' or 'group:<id>');
* each occurrence gets at most one catch-up: when Fish was down at the due
  time, the first run after restart executes the single most recent occurrence,
  and every later lapse is marked ``skipped`` for the owner to see;
* execution runs in the bound session with a bound identity (owner private
  route keeps ``computer.work`` permission even if the current chat mode is
  chat; groups stay bound to their group);
* a real send produces a real receipt in ``sends`` and the adaptive ledger
  (send_policy record_send); the owner-private route always sends at least a
  state receipt (success receipt / failure notice) so a failure is visible.

``transport`` is injectable for the focused local check; production uses
``ChannelClient`` -> fish-qq -> NapCat, text segments only.
"""
import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from storage import database, read_json
from governance import Governance, control_token
import send_policy

ZONE = 'Asia/Shanghai'
TIMING = ('after_seconds', 'at', 'every_seconds', 'daily', 'weekly', 'cron')
PENDING_STATES = ('due', 'catchup', 'queued', 'running', 'sent')
KIND_TO_TIMING = {'after': 'after_seconds', 'at': 'at', 'every': 'every_seconds', 'daily': 'daily', 'weekly': 'weekly', 'cron': 'cron'}


def zone_info(name):
    try:
        return ZoneInfo(str(name or ZONE))
    except Exception:
        raise ValueError('未知时区：' + str(name))


def _as_items(value):
    return value if isinstance(value, list) else [value]


def timing_from_row(timing, row):
    """Model time arguments, falling back to fields the native schedule reports."""
    timing = dict(timing) if isinstance(timing, dict) else {}
    present = [k for k in TIMING if timing.get(k) not in (None, '', {}, [])]
    if present:
        return {present[0]: timing[present[0]]}, present[0]
    kind = row.get('kind', 'at')
    timing = KIND_TO_TIMING.get(kind, kind)
    if kind == 'after':
        return {'after_seconds': int(row.get('afterSeconds') or 60)}, timing
    if kind == 'every':
        return {'every_seconds': int(row.get('everySeconds') or 60)}, timing
    if kind in ('daily', 'weekly'):
        value = {'time': str(row.get('time', ''))[:8], 'time_zone': row.get('timeZone') or ZONE}
        if kind == 'weekly':
            value['weekdays'] = list(row.get('weekdays') or [])
        return {timing: value}, timing
    if kind == 'cron':
        return {'cron': {'expression': row.get('expression', ''), 'time_zone': row.get('timeZone') or ZONE}}, timing
    return {'at': {'date': str(row.get('scheduledAt', ''))[:10], 'time': str(row.get('scheduledAt', ''))[11:19],
                   'time_zone': row.get('timeZone') or ZONE}}, 'at'


def describe(timing, when_iso=''):
    """Owner-facing Asia/Shanghai description of a schedule."""
    key = next((k for k in TIMING if k in timing), '')
    value = timing.get(key)
    when = send_policy.display(when_iso) if when_iso else ''
    if key == 'after_seconds':
        n = int(value)
        text = (f'{n // 86400} 天后' if n >= 86400 else f'{n // 3600} 小时后' if n >= 3600 else f'{n // 60} 分钟后' if n >= 60 else f'{n} 秒后')
        return f'一次：{text}' + (f'（约 {when}）' if when else '')
    if key == 'at':
        value = value if isinstance(value, dict) else {}
        return '一次：' + (f"{value.get('date', '')} {value.get('time', '')}".strip() or when)
    if key == 'every_seconds':
        return f'每 {int(value)} 秒重复'
    if key in ('daily', 'weekly'):
        value = value if isinstance(value, dict) else {'time': str(value)}
        zone = value.get('time_zone') or ZONE
        if key == 'daily':
            return f"每天 {value.get('time', '')}（{zone}）"
        names = '一二三四五六日'
        days = ','.join('周' + names[int(d) - 1] if 1 <= int(d) <= 7 else str(d) for d in value.get('weekdays') or [])
        return f"每{days or '周?'} {value.get('time', '')}（{zone}）"
    if key == 'cron':
        value = value if isinstance(value, dict) else {'expression': str(value)}
        return f"Cron {value.get('expression', '')}（{value.get('time_zone') or ZONE}）"
    return str(timing)


class BotControl:
    def __init__(self, root, *, transport=None, on_fire=None, session_factory=None):
        self.root = Path(root); self.path = self.root / 'data/social/owner-control.db'
        # transport(target, text, request_id) -> {'ok': bool, 'payload': {...}, 'error': {...}}; default real channel.
        self.transport = transport or self._channel_transport
        # on_fire(record) runs the bound session via BotControl.execute_turn; None means detect + mark only.
        self.on_fire = on_fire
        self.session_factory = session_factory or self._session
        with database(self.path) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sessions(target TEXT PRIMARY KEY,session TEXT,revision TEXT);
                CREATE TABLE IF NOT EXISTS sends(id TEXT PRIMARY KEY,target TEXT,text TEXT,state TEXT,receipt TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS scheduled(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,target TEXT,session TEXT,
                    title TEXT,prompt TEXT,original TEXT,timing TEXT,timing_key TEXT,request TEXT,state TEXT,created REAL,updated REAL);
                CREATE TABLE IF NOT EXISTS occurrences(schedule_id TEXT,slot TEXT,state TEXT,attempts INTEGER DEFAULT 0,
                    receipt_id TEXT,note TEXT,created REAL,updated REAL,PRIMARY KEY(schedule_id,slot));''')

    # ---------------------------------------------------------------- native DSH
    def _runtime(self):
        from core import HarnessConfig, RuntimeManager
        from runtime_adapter import DSHRuntimeAdapter
        DSHRuntimeAdapter(self.root, 'qq', RuntimeManager(HarnessConfig(self.root))).connect()
        return HarnessConfig(self.root).runtime_port('qq')

    def native(self, body):
        from core import HarnessConfig
        response = requests.post('http://127.0.0.1:' + str(HarnessConfig(self.root).runtime_port('qq')) + '/fish/schedules',
            json=body, headers={'x-fish-token': control_token(self.root)}, timeout=20,
            proxies={'http': None, 'https': None, 'all': None})
        value = response.json()
        if not response.ok or not value.get('ok'):
            raise RuntimeError(value.get('error', '原生定时服务调用失败'))
        return value['value']

    def validate_target(self, target):
        cfg = read_json(self.root / 'channels/fish_qq/config.json')['allowlist']
        if target == 'owner': target = 'private:' + str(cfg['owner_id'])
        if target == 'private:' + str(cfg['owner_id']): return target
        if target.startswith('group:') and target[6:] in [str(x) for x in cfg['group_ids']]: return target
        raise PermissionError('请选择主人私聊或已配置的白名单群')

    async def _session(self, target):
        """Bound session for a target: reuse the persisted one, else the owner private chat session."""
        from social_context import SocialContext
        from runtime_adapter import DSHRuntimeAdapter
        from core import HarnessConfig, RuntimeManager
        from tool_host import bind_session
        target = self.validate_target(target)
        with database(self.path) as db:
            row = db.execute('SELECT * FROM sessions WHERE target=?', (target,)).fetchone()
        c = SocialContext(self.root)
        if target.startswith('group:'):
            from group_tests import GroupTests
            current = GroupTests(self.root).current(target[6:])
        else:
            current = c.owner_space(c.owner())
        revision = current['revision'] if current else c.ensure_default('qq')['id']
        session = row['session'] if row else ''
        if not session and target != 'private:' + c.owner():
            # Owner private reminders default to the owner's existing chat session.
            with database(self.path) as db:
                shared = db.execute('SELECT session FROM sessions WHERE target=?', ('private:' + c.owner(),)).fetchone()
            session = shared['session'] if shared else ''
        if not session:
            client = await asyncio.to_thread(DSHRuntimeAdapter(self.root, 'qq', RuntimeManager(HarnessConfig(self.root))).connect)
            await client.auth()
            directory = self.root / 'data/qq/scheduled'; directory.mkdir(parents=True, exist_ok=True)
            session = await client.create_session(cwd=str(directory))
        with database(self.path) as db:
            db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)', (target, session, revision))
        self.bind(target, session)
        return session

    def bind(self, target, session=None):
        """Bind identity for the target: owner private keeps control scope (= computer.work), groups bind their chat.

        Returns the validated target string (callers must not use truthiness on it).
        """
        from tool_host import bind_session
        from social_context import SocialContext
        target = self.validate_target(target)
        if not session:
            with database(self.path) as db:
                row = db.execute('SELECT session FROM sessions WHERE target=?', (target,)).fetchone()
            session = row['session'] if row else ''
        if not session:
            raise PermissionError('定时目标尚未绑定会话')
        owner = SocialContext(self.root).owner()
        bind_session(self.root, 'qq', session, 'control:' + target, owner)
        return target

    # ---------------------------------------------------------------- schedules CRUD
    def _timing(self, params):
        timing = {k: params[k] for k in TIMING if params.get(k) not in (None, '', {}, [])}
        if len(timing) != 1:
            raise ValueError('请只用一种时间方式：after_seconds（N 秒后一次）、at（日期时间）、daily、every_seconds、weekly 或 cron')
        key = next(iter(timing))
        value = _as_items(timing[key])
        if key in ('after_seconds', 'every_seconds') and (len(value) != 1 or isinstance(value[0], bool) or not str(value[0]).strip().isdigit()):
            raise ValueError('after_seconds/every_seconds 需为单个整数秒')
        return {key: value[0] if len(value) == 1 else value}, key

    def create(self, params, *, session=None):
        default = 'owner' if session is None else 'group:' + session
        target = self.validate_target(params.get('target') or default)
        title = str(params.get('title') or '').strip()[:120]
        prompt = str(params.get('prompt') or '').strip()
        if not prompt: raise ValueError('请填写到点要做什么')
        if not title: title = prompt[:24]
        timing, key = self._timing(params)
        session = session or asyncio.run(self.session_factory(target))
        request = {'title': title, 'prompt': prompt, **timing}
        row = self.native({'action': 'create', 'session': session, 'request': request})
        now = time.time()
        with database(self.path) as db:
            db.execute('INSERT OR REPLACE INTO scheduled(id,target,session,title,prompt,original,timing,timing_key,request,state,created,updated)'
                       ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (row['id'], target, session, title, prompt,
                       str(params.get('original') or prompt), json.dumps(timing, ensure_ascii=False), key,
                       json.dumps(request, ensure_ascii=False), 'active', now, now))
        return self._view(row)

    def _remote(self):
        rows = self.native({'action': 'list'}) or []
        return {r['id']: r for r in rows}

    def _rows(self):
        with database(self.path) as db:
            return [dict(r) for r in db.execute('SELECT * FROM scheduled ORDER BY seq DESC')]

    def _view(self, native_row):
        rid = native_row['id']
        with database(self.path) as db:
            meta = db.execute('SELECT * FROM scheduled WHERE id=?', (rid,)).fetchone()
            occ = [dict(r) for r in db.execute('SELECT * FROM occurrences WHERE schedule_id=? ORDER BY slot', (rid,))]
            receipts = [dict(r) for r in db.execute('SELECT id,target,state,receipt,created FROM sends WHERE id LIKE ? OR id LIKE ? ORDER BY created DESC LIMIT 20', ('sched:' + rid + '%', rid + '@%'))]
        meta = dict(meta) if meta else {}
        timing = json.loads(meta.get('timing') or '{}') if meta else {}
        with database(self.path) as db:
            bound = {r['target']: r['session'] for r in db.execute('SELECT target,session FROM sessions')}
        target = meta.get('target') or ''
        row = dict(native_row)
        row.update(target=target or '未绑定', targetLabel='主人私聊' if target.startswith('private:') else '白名单群',
                   session=meta.get('session') or native_row.get('sessionId', ''),
                   sessionTarget=next((t for t, s in bound.items() if s == meta.get('session')), ''),
                   original=meta.get('original') or row.get('prompt', ''), prompt=meta.get('prompt') or row.get('prompt', ''),
                   timing=timing, timingKey=meta.get('timing_key', ''),
                   when=describe(timing, row.get('scheduledAt', '')), scheduledAtDisplay=send_policy.display(row.get('scheduledAt', '')),
                   nextRunDisplay=send_policy.display(row.get('nextRunAt', '')),
                   ownerPrivate=target.startswith('private:'))
        for item in receipts:
            try:
                item['receipt'] = json.loads(item['receipt'] or '{}')
            except ValueError:
                item['receipt'] = {}
            payload = item['receipt'].get('payload') or {}
            item['messageId'] = payload.get('message_id') or payload.get('message_ids', [''])[-1]
            item['receipt'] = 'OK msg=' + str(item['messageId']) if item['state'] == 'SUCCEEDED' else json.dumps(item['receipt'].get('error') or item['state'], ensure_ascii=False)
        row['sendReceipts'] = receipts
        row['occurrences'] = [{**o, 'display': send_policy.display(o['slot'])} for o in occ]
        row['catchUp'] = [o for o in row['occurrences'] if o['state'] == 'catchup']
        row['pending'] = [o for o in row['occurrences'] if o['state'] in PENDING_STATES]
        if receipts:
            row['receipt_id'] = str(receipts[0]['messageId'] or receipts[0]['id'])
            row['occurrenceState'] = 'sent' if receipts[0]['state'] == 'SUCCEEDED' else receipts[0]['state'].lower()
        elif occ:
            row['occurrenceState'] = occ[-1]['state']
        return row

    def schedules(self):
        remote = self._remote()
        rows = [self._view(remote[r['id']]) for r in self._rows() if r['id'] in remote]
        known = {r['id'] for r in rows}
        for rid, native_row in remote.items():
            if rid not in known:
                row = dict(native_row); row['target'] = '原生会话 ' + str(native_row.get('sessionId', ''))[-8:]
                row['unmanaged'] = True; rows.append(row)
        return rows

    def delete(self, schedule_id):
        record = self._find(schedule_id)
        self.native({'action': 'delete', 'session': record['sessionId'], 'id': record['id']})
        with database(self.path) as db:
            db.execute("UPDATE scheduled SET state='deleted',updated=? WHERE id=?", (time.time(), record['id']))
        return {'id': record['id'], 'deleted': True}

    def update(self, params):
        record = self._find(params['id'])
        rows = self.native({'action': 'list'}) or []
        native = next(r for r in rows if r['id'] == record['id'])
        expected = {k: v for k, v in native.items() if k not in ('sessionId', 'status', 'lastDelivery')}
        request = {'sessionId': native['sessionId'], 'id': native['id'], 'expected': expected}
        request.update({k: params[k] for k in ('title', 'prompt', 'change') if k in params})
        result = self.native({'action': 'update', 'request': request})
        if isinstance(result, dict) and result.get('code'):
            raise ValueError('定时任务未更新：' + result['code'])
        with database(self.path) as db:
            meta = db.execute('SELECT * FROM scheduled WHERE id=?', (record['id'],)).fetchone()
            title = params.get('title', meta['title']); prompt = params.get('prompt', meta['prompt'])
            timing = json.loads(meta['timing']); key = meta['timing_key']
            change = params.get('change') or {}
            if 'kind' in change:
                key = KIND_TO_TIMING.get(change['kind'], change['kind']); timing = {}
            for k in TIMING:
                if k in change: timing = {k: change[k]}; key = k
            timing = timing or json.loads(meta['timing'])
            req = {'title': title, 'prompt': prompt, **timing}
            db.execute('UPDATE scheduled SET title=?,prompt=?,timing=?,timing_key=?,request=?,state=?,updated=? WHERE id=?',
                       (title, prompt, json.dumps(timing, ensure_ascii=False), key, json.dumps(req, ensure_ascii=False), 'active', time.time(), record['id']))
        return self._view(next(r for r in self.native({'action': 'list'}) if r['id'] == record['id']))

    def _find(self, schedule_id):
        record = next((r for r in self.schedules() if r['id'] == schedule_id), None)
        if not record: raise ValueError('任务不存在或已删除')
        if record.get('unmanaged'): raise ValueError('此任务不是通过 Fish 定时创建的，请在本机定时面板直接管理')
        return record

    def run_now(self, schedule_id):
        """Owner-triggered immediate run of one schedule (the running model session performs the work)."""
        record = self._find(schedule_id)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        slot = now.isoformat()
        if not self._occurrence(record['id'], slot, 'due'):
            return {'id': record['id'], 'state': 'already_pending'}
        if not self._fire(record):
            task = self._executor()
            if task: task.submit(lambda: asyncio.run(self._run_scheduled(record['id'], slot)))
        return {'id': record['id'], 'slot': slot, 'display': send_policy.display(slot), 'state': 'due'}

    # ---------------------------------------------------------------- occurrences
    def _occurrence(self, schedule_id, slot, state):
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute('INSERT OR IGNORE INTO occurrences(schedule_id,slot,state,created,updated) VALUES(?,?,?,?,?)',
                                (schedule_id, slot, state, time.time(), time.time()))
            if cursor.rowcount:
                return True
            current = db.execute('SELECT state FROM occurrences WHERE schedule_id=? AND slot=?', (schedule_id, slot)).fetchone()
            return bool(current and state == 'catchup' and current['state'] == 'skipped')

    def _set_occurrence(self, schedule_id, slot, **values):
        with database(self.path) as db:
            values = {**values, 'updated': time.time()}
            keys = list(values)
            if values.get('state') == 'catchup':
                db.execute('UPDATE occurrences SET attempts=attempts+1 WHERE schedule_id=? AND slot=?', (schedule_id, slot))
            db.execute('UPDATE occurrences SET ' + ','.join(k + '=?' for k in keys) + ' WHERE schedule_id=? AND slot=?',
                       (*values.values(), schedule_id, slot))

    def _last_delivery(self, row):
        delivery = row.get('lastDelivery') or {}
        return delivery.get('deliveredAt') or ''

    def scan(self, now=None):
        """Turn schedule facts into occurrence slots: due slots and at most one missed slot (slot = due time).

        The analysis part is pure (no writes); ``detect`` persists it and fires due/catch-up slots.
        """
        now = now or datetime.now(timezone.utc)
        remote = self._remote()
        due, missed = [], []
        for meta in self._rows():
            remote_row = remote.get(meta['id'])
            if not remote_row: continue
            last = self._last_delivery(remote_row)
            last_dt = datetime.fromisoformat(last) if last else None
            if last_dt and last_dt.tzinfo is None: last_dt = last_dt.replace(tzinfo=timezone.utc)
            timing = json.loads(meta['timing'])
            if meta['timing_key'] in ('after_seconds', 'at'):
                if not last_dt:
                    when = remote_row.get('scheduledAt', '')
                    try: at = datetime.fromisoformat(when)
                    except ValueError: continue
                    if at.tzinfo is None: at = at.replace(tzinfo=timezone.utc)
                    if at <= now: due.append((meta, at.isoformat()))
            else:
                next_at = remote_row.get('nextRunAt', '')
                try: nxt = datetime.fromisoformat(next_at)
                except (ValueError, TypeError): nxt = None
                if nxt and nxt.tzinfo is None: nxt = nxt.replace(tzinfo=timezone.utc)
                if nxt and nxt <= now:
                    due.append((meta, nxt.isoformat()))
                elif last_dt is not None and (nxt is None or nxt > now):
                    # Paused past one or more periods: one catch-up for the most recent one.
                    missed.append((meta, self._latest_missed(remote_row, last_dt, now).isoformat()))
        return {'due': due, 'catchup': missed, 'remote': remote, 'now': now}

    def _latest_missed(self, row, last, now):
        """Latest recurrence that should have fired after ``last`` but before ``now`` (approx by native period)."""
        key = KIND_TO_TIMING.get(row.get('kind'), row.get('kind'))
        if key == 'every_seconds':
            period = timedelta(seconds=int(row.get('everySeconds') or 60))
            slot = last + period
            while slot + period <= now: slot += period
            return slot
        if key in ('daily', 'weekly', 'cron'):
            nxt = row.get('nextRunAt')
            if nxt:
                nxt = datetime.fromisoformat(nxt)
                if nxt.tzinfo is None: nxt = nxt.replace(tzinfo=timezone.utc)
                return nxt
        return now

    def detect(self, now=None):
        """Persist due/missed slots without running them (used at startup and for inspection)."""
        plan = self.scan(now)
        changes = []
        for meta, slot in plan['due']:
            if self._occurrence(meta['id'], slot, 'due'): changes.append({'id': meta['id'], 'slot': slot, 'state': 'due'})
        for meta, slot in plan['catchup']:
            if self._occurrence(meta['id'], slot, 'catchup'): changes.append({'id': meta['id'], 'slot': slot, 'state': 'catchup'})
        for meta, _ in plan['due'] + plan['catchup']:
            with database(self.path) as db:
                db.execute("UPDATE occurrences SET state='skipped',note=?,updated=? WHERE schedule_id=? AND state='due'"
                           " AND slot NOT IN (SELECT slot FROM occurrences o2 WHERE o2.schedule_id=occurrences.schedule_id AND o2.state IN ('due','catchup','queued','running'))",
                           ('已跳过：停机期间错过，只补一次', time.time(), meta['id']))
        return changes

    def dispatch(self, changes=None):
        """Fire each persisted due/catch-up slot once: bound-session turn, or fall back to stored prompt."""
        changes = changes if changes is not None else self.detect()
        results = []
        for change in changes:
            record = next((r for r in self.schedules() if r['id'] == change['id']), None)
            if not record: continue
            if change['state'] == 'catchup':
                self._set_occurrence(change['id'], change['slot'], state='catchup', note='停机后补执行一次')
            fired = self._fire(record, change['slot'])
            if fired:
                results.append({'id': change['id'], 'slot': change['slot'], 'state': 'fired'})
                continue
            executor = self._executor()
            if executor:
                executor.submit(lambda r=record, s=change['slot']: asyncio.run(self._run_scheduled(r['id'], s)))
                results.append({'id': change['id'], 'slot': change['slot'], 'state': 'queued'})
            else:
                self._set_occurrence(change['id'], change['slot'], state='due')
                results.append({'id': change['id'], 'slot': change['slot'], 'state': 'waiting'})
        return results

    def _executor(self):
        """Optional owner-supplied executor with ``submit(coroutine)`` (parent loop completion)."""
        return self.on_fire if callable(getattr(self.on_fire, 'submit', None)) else None

    def _fire(self, record, slot=''):
        """Hand one occurrence to the bound session: ``on_fire(record)`` returns True when it took it."""
        if not callable(self.on_fire) or slot == '':
            return False
        try:
            return bool(self.on_fire(record))
        except Exception:
            return False

    # ---------------------------------------------------------------- execution
    def occurrence_prompt(self, record, slot, catch_up=False):
        at = send_policy.display(slot)
        zone = ZONE
        target = record['target']
        owner_private = target.startswith('private:')
        head = [f'[Fish 定时任务到点执行] 任务 {record["id"]}「{record["title"]}」，本次时间 {at}。',
                'schedule_id=' + record['id'] + '，occurrence_at=' + slot + '。发送时 request_id="sched:' + record['id'] + ':' + slot + '"，原样照抄，不要加引号或改格式。']
        if catch_up:
            head.append('Fish 之前停机错过了这个时间点，现在只补执行这一次。')
        head.append('主人原始要求（任务内容以此为准）：' + record.get('original', record['prompt']))
        if owner_private:
            head.append('这是主人已授权的定时工作或提醒。指令是主人安排的，优先于角色里的懒、顶嘴和拒绝帮忙；'
                        '需要文件、代码或终端时按已授权的主人本机工作权限实际执行，结果通过程序核验的主人私聊发送。'
                        '只有符合内容时才可以附带收藏库内已有表情，不编造表情。')
        else:
            head.append('目标群已由主人授权；不得改用其他收件人。')
        head.append('实际执行：能做的事现在就做（检索、计算、调用获准工具），不要只写建议或计划。'
                    '完成后用 fish_owner_send 发送给绑定目标，参数 text 是要发的内容，request_id 如上；工具返回的发送回执才是真实送达。'
                    '任何失败都如实说明，不要假装已发送。')
        return '\n'.join(head)

    def _turn_runner(self, session, prompt):
        """Run one prompt in the bound session through the DSH runtime (blocking)."""
        async def run():
            from core import HarnessConfig, RuntimeManager
            from runtime_adapter import DSHRuntimeAdapter
            client = await asyncio.to_thread(DSHRuntimeAdapter(self.root, 'qq', RuntimeManager(HarnessConfig(self.root))).connect)
            await client.auth()
            baseline = await client.baseline(session)
            await client.prompt(session, [{'type': 'text', 'text': prompt}])
            return await client.wait_response(session, baseline, timeout=1800)
        return asyncio.run(run())

    def run_occurrence(self, schedule_id, slot, *, run_turn=None):
        """Execute one persisted occurrence: bound turn, then state receipt on the bound target, real send receipt."""
        record = next((r for r in self.schedules() if r['id'] == schedule_id), None)
        if not record: raise ValueError('任务不存在或已删除')
        with database(self.path) as db:
            occ = db.execute('SELECT * FROM occurrences WHERE schedule_id=? AND slot=?', (schedule_id, slot)).fetchone()
        if not occ: raise ValueError('缺少本次执行记录')
        if occ['state'] in ('sent', 'failed', 'running', 'no_send'):
            # Already executed (or a crash left it 'running'): never run the same occurrence twice.
            return self._occurrence_view(schedule_id, slot)
        catch_up = occ['state'] == 'catchup'
        self._set_occurrence(schedule_id, slot, state='running', note='')
        # Bind identity for the whole turn: owner private keeps computer.work regardless of current chat mode.
        target = self.bind(record['target'], record['session'])
        slot = str(occ['slot'])  # canonical slot recorded in SQLite (what the model's request_id copies)
        error = ''; receipts = []
        try:
            # The bound turn sends through fish_owner_send, which records real receipts in ``sends``.
            (run_turn or self._turn_runner)(record['session'], self.occurrence_prompt(record, slot, catch_up=catch_up))
            receipts = list(self._occurrence_receipts(schedule_id, slot))
        except Exception as exc:
            error = type(exc).__name__ + ': ' + str(exc)[:300]
            receipts = list(self._occurrence_receipts(schedule_id, slot))
        after = self._send_count(receipts)
        if target.startswith('private:') and not after:
            # Owner private route: the result itself is a receipt target; a missing send is a real failure notice.
            text = ('定时任务「' + record['title'] + '」本次没有完成发送。' + (('原因：' + error) if error else '模型回合结束但没有调用发送工具。')
                    + ' 可回复"重试"或查看定时任务详情。')
            self._deliver(target, text, f'sched:{schedule_id}:{slot}:notice', source='schedule-notice')
        receipt = self._latest_receipt(receipts)
        if after:
            state, note = 'sent', f'已通过绑定目标发送 {after} 条，回执 {(receipt or {}).get("id", "")}'
        else:
            state = 'no_send'
            note = ('未调用发送工具' + (('；执行错误 ' + error) if error else '')
                    + ('；已给主人发失败通知' if target.startswith('private:') else ''))
        self._set_occurrence(schedule_id, slot, state=state, receipt_id=(receipt or {}).get('id', ''), note=note)
        return self._occurrence_view(schedule_id, slot)

    async def _run_scheduled(self, schedule_id, slot, run_turn=None):
        return await asyncio.to_thread(self.run_occurrence, schedule_id, slot, run_turn=run_turn)

    async def execute_turn(self, record, slot, run_turn=None):
        """Adapter for the parent loop: execute one occurrence without blocking the loop."""
        return await self._run_scheduled(record['id'], slot, run_turn=run_turn)

    def _occurrence_receipts(self, schedule_id, slot):
        prefix = 'sched:' + str(schedule_id) + ':' + str(slot)
        with database(self.path) as db:
            ids = [r['id'] for r in db.execute("SELECT id FROM sends WHERE id LIKE 'sched:%'")]
        return [i for i in ids if i.startswith(prefix)]

    def _send_count(self, rids):
        if not rids: return 0
        with database(self.path) as db:
            return db.execute("SELECT COUNT(*) n FROM sends WHERE id IN (%s) AND state IN ('SUCCEEDED','EXECUTING')" % ','.join('?' * len(rids)), rids).fetchone()['n']

    def _latest_receipt(self, rids):
        for rid in rids:
            with database(self.path) as db:
                row = db.execute('SELECT * FROM sends WHERE id=?', (rid,)).fetchone()
            if row and row['state'] == 'SUCCEEDED':
                return {'id': row['id'], 'state': row['state'], 'receipt': json.loads(row['receipt'] or '{}')}
        return None

    def _occurrence_view(self, schedule_id, slot):
        with database(self.path) as db:
            row = db.execute('SELECT * FROM occurrences WHERE schedule_id=? AND slot=?', (schedule_id, slot)).fetchone()
        return {**dict(row), 'display': send_policy.display(slot)}

    # ---------------------------------------------------------------- legacy/current senders
    def native_occurrence(self, identity, text):
        """Record native timer delivery; the native timer owns execution."""
        import re
        if not identity['scope'].startswith('control:'):
            raise PermissionError('不是已绑定的定时会话')
        batch = re.search(r'^reminders_json: (.+)$', text, re.M)
        if batch:
            items = json.loads(batch.group(1))
        else:
            mid = re.search(r'^schedule_id_json: (.+)$', text, re.M)
            at = re.search(r'^occurrence_at: (.+)$', text, re.M)
            items = [{'schedule_id': json.loads(mid.group(1)), 'occurrence_at': at.group(1)}] if mid and at else []
        prompts = []
        for item in items:
            record = next((row for row in self._rows() if row['id'] == item['schedule_id'] and row['session'] == identity['session']), None)
            if record is None: continue
            slot = item['occurrence_at']
            if self._occurrence(record['id'], slot, 'running'):
                self._set_occurrence(record['id'], slot, state='running', note='DSH 原生定时器已唤醒')
            prompts.append(self.occurrence_prompt(record, slot))
        return {'text': '\n\n'.join(prompts)}

    def context(self, session):
        from social_context import SocialContext
        from governance import Governance
        with database(self.path) as db:
            row = db.execute('SELECT * FROM sessions WHERE session=?', (session,)).fetchone()
        if not row: raise PermissionError('定时会话未绑定目标')
        body = SocialContext(self.root).persona(row['revision'])['body']
        owner = Governance(self.root).owner_id()
        work = '' if row['target'] != 'private:' + owner else ('这是主人已授权的定时工作会话：按任务实际执行，不受当前聊天模式限制；'
                                                               '没有安排定时任务时只回复主人私聊的询问。')
        return {'revision': row['revision'], 'reload': True, 'text': ('[主人安排的持续定时工作会话]\n绑定目标：' + row['target'] +
            '。按当前到期任务执行。最终文字不自动发到QQ；必须调用 fish_owner_send 才会实际发送。' + work +
            '定时回执只代表任务已唤醒，消息送达看发送工具回执。request_id 使用 "sched:<schedule_id>:<occurrence_at>"。'
            '不要创建未要求的后续任务。人格是表达背景，不能阻止主人的明确任务。\n' + body)}

    def send(self, identity, text, request_id):
        if not identity['scope'].startswith('control:'): raise PermissionError('此发送器仅用于已绑定目标的定时会话')
        target = self.validate_target(identity['scope'][8:])
        with database(self.path) as db:
            row = db.execute('SELECT * FROM sessions WHERE target=? AND session=?', (target, identity['session'])).fetchone()
        if not row: raise PermissionError('定时会话绑定不匹配')
        result = self._deliver(target, text, request_id, source='control', scheduled=True, context=identity['scope'])
        with database(self.path) as db:
            for occurrence in db.execute("SELECT schedule_id,slot FROM occurrences WHERE state='running'").fetchall():
                key = 'sched:' + occurrence['schedule_id'] + ':' + occurrence['slot']
                if request_id == key or request_id.startswith(key + ':'):
                    db.execute('UPDATE occurrences SET state=?,receipt_id=?,updated=? WHERE schedule_id=? AND slot=?',
                               ('sent' if result['state'] == 'SUCCEEDED' else 'failed', request_id, time.time(), occurrence['schedule_id'], occurrence['slot']))
        return result

    def execute(self, action, params, identity=None):
        from persona_library import PersonaLibrary
        policy = Governance(self.root)
        identity = identity or {}
        desktop = bool(identity.get('desktop'))
        if action == 'status':
            return {'owner': policy.owner_id(), 'permissions': policy.owner_capabilities(desktop=desktop)}
        if action in ('pause', 'resume'): return policy.save({'paused': action == 'pause'})
        if action.startswith('memory_'):
            return self._memory(action, params, identity)
        if action == 'personas': return PersonaLibrary(self.root).list()
        if action == 'persona_import':
            return [{'id': p['id'], 'name': p['name']} for p in PersonaLibrary(self.root).import_text(params['text'], name=params.get('name', '导入人格'), source=params.get('source', '主人对话'))]
        if action == 'persona_apply': return PersonaLibrary(self.root).apply(params['revision'], params.get('target', 'owner'), new=bool(params.get('new', False)))
        if action == 'message_send': return self._manual_send(params, identity)
        if action == 'file_send':
            from owner_files import send_file
            return send_file(self.root, params.get('target','owner'), params['path'], params['request_id'])
        if action in ('qzone_draft','qzone_publish'):
            from social_publish import execute
            return execute(self.root, action, params)
        if action.startswith('send_'):
            return self._send_settings(action, params, identity)
        if action.startswith('group_'):
            from group_tests import GroupTests
            from group_policy import GroupPolicy
            group = str(params.get('group_id', ''))
            self.validate_target('group:' + group)
            trial = GroupTests(self.root).current(group)
            group_policy = GroupTests(self.root).policy(trial) if trial else GroupPolicy(self.root)
            return group_policy.config('group:' + group) if action == 'group_get' else group_policy.configure('group:' + group, params['settings'])
        if action == 'schedule_list': return self.schedules()
        if action == 'schedule_create': return self.create(params, session=params.get('session'))
        if action == 'schedule_delete': return self.delete(params['id'])
        if action == 'schedule_update': return self.update(params)
        if action == 'schedule_run_now': return self.run_now(params['id'])
        if action == 'schedule_check':
            changes = self.detect()
            return self.dispatch(changes) if params.get('run') else changes
        raise ValueError('未知管理操作')

    def _memory(self, action, params, identity):
        from memory_store import MemoryStore
        scope = str(identity.get('scope', 'owner'))
        if scope.startswith('chat:group:'): scope = ':'.join(scope.split(':')[:3])
        elif scope.startswith('control:group:'): scope = 'chat:' + ':'.join(scope.split(':')[1:3])
        else: scope = 'owner'
        store = MemoryStore(self.root, 'qq')
        if action == 'memory_search': return store.search(params.get('query', ''), scope=scope, limit=30)
        capability = 'memory.write' if action == 'memory_save' else 'memory.delete'
        if identity and not identity.get('desktop') and not Governance(self.root).permitted(identity.get('sender'), capability):
            raise PermissionError('主人尚未启用此记忆管理权限')
        if action == 'memory_save':
            return store.save(params['text'], category=params.get('category', '约定'), scope=scope, source='manual', source_text='主人在管理会话中明确保存')
        if not any(x['id'] == params['id'] for x in store.search(scope=scope, limit=200)):
            raise ValueError('当前范围内未找到此记忆')
        return store.delete(params['id'])

    def _owner_request(self, identity):
        """Identity for owner-authorized manual sends; program state, no per-message prompt."""
        from owner_computer import authorized
        if identity.get('desktop') and identity.get('owner'):
            return True
        if not (identity.get('owner') and identity.get('role') == 'qq'):
            return False
        return bool(authorized(self.root, identity) if not str(identity.get('scope', '')).startswith('control:')
                    else Governance(self.root).permitted(identity.get('sender', ''), 'computer.work'))

    def _manual_send(self, params, identity):
        target = self.validate_target(params.get('target', 'owner'))
        if not identity.get('scope'):
            raise PermissionError('缺少已核验的会话入口')
        # Reaching this tool with a bound owner identity IS the owner's explicit request in the current
        # session, so the decision and the transport use that same context (never an empty one).
        context = 'owner:' + str(identity['scope'])
        ok, reason = send_policy.decide(self.root, target, action='group_send' if target.startswith('group:') else 'owner_reply', context=context)
        if not ok: raise PermissionError(reason)
        if target.startswith('group:') and not (identity.get('owner') and self._owner_request(identity)):
            raise PermissionError('发往群聊需要主人在当前会话的明确要求；公开群自动回复保持关闭')
        if not all(identity.get(k) for k in ('owner', 'scope', 'sender', 'session')) and not identity.get('desktop'):
            raise PermissionError('单次交办发送仅用于已核验主人工作入口')
        request_id = params.get('request_id') or 'owner-send:' + identity['scope'] + ':' + uuid.uuid4().hex[:12]
        result = self._deliver(target, params['text'], request_id, source='owner-request:' + str(identity.get('scope')), context=context)
        if result.get('error'): raise PermissionError(result['error'])
        return result

    def _send_settings(self, action, params, identity):
        policy = Governance(self.root)
        values = self._load_settings()
        if action == 'send_get': return values
        changes = params.get('settings') if isinstance(params.get('settings'), dict) else params
        if action == 'send_set':
            values = policy.save({'send_policy': changes})['send_policy']
            return {'message': '发送设置已保存', **self._load_settings()}
        if action == 'send_usage': return send_policy.summary(self.root)
        if action == 'send_test':
            target = self.validate_target(params.get('target', 'owner'))
            ok, reason = send_policy.decide(self.root, target, action='scheduled_result' if target.startswith('private:') else 'group_send',
                                            scheduled=True, context='control:' + target)
            if not ok: raise PermissionError(reason)
            request_id = 'owner-send:policy-test:' + str(identity.get('session', 'desktop'))[:40] + ':' + uuid.uuid4().hex[:8]
            return self._deliver(target, params.get('text') or '发送设置测试：程序可以按当前设置向这个目标发送。', request_id, source='send-test')
        raise ValueError('未知发送设置操作')

    def _load_settings(self):
        cfg = Governance(self.root).config()
        group_policy = {}
        try:
            from group_policy import GroupPolicy
            for meta in self._rows():
                if meta['target'].startswith('group:') and meta['target'] not in group_policy:
                    group_policy[meta['target']] = GroupPolicy(self.root).config(meta['target'])
        except Exception:
            pass
        return {'send_policy': cfg['send_policy'], 'editable': send_policy.SETTABLE,
                'owner_private_route': 'private:' + Governance(self.root).owner_id(),
                'group_policy': {k: {g: v for g, v in c.items() if g in ('listen', 'reply', 'mode')} for k, c in group_policy.items()},
                'public_auto_reply': '由群聊监听/回复开关控制，默认关闭', 'capabilities': send_policy.settings_view(self.root)['capabilities'],
                'actual': send_policy.summary(self.root)}

    # ---------------------------------------------------------------- delivery
    def _deliver(self, target, text, request_id, *, source='', scheduled=False, context='', transport=None):
        # No validate_target here: callers decide the target (owner route or the schedule's bound group).
        if not isinstance(text, str) or not text.strip(): raise ValueError('消息不能为空')
        if not isinstance(request_id, str) or not request_id.strip(): raise ValueError('需要本次执行的唯一发送编号')
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM sends WHERE id=?', (request_id,)).fetchone()
            if previous:
                if previous['target'] != target or previous['text'] != text: raise ValueError('发送编号已用于其他内容')
                return {'state': previous['state'], 'receipt': json.loads(previous['receipt'] or '{}')}
            db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?)', (request_id, target, text, 'EXECUTING', '{}', time.time()))
        action = 'scheduled_result' if scheduled or request_id.startswith('sched:') else 'group_send' if target.startswith('group:') else 'owner_reply'
        ok, reason = send_policy.decide(self.root, target, action=action, scheduled=scheduled, context=context)
        if ok:
            try:
                Governance(self.root).check_output(text)
            except (PermissionError, OSError, ValueError) as exc:
                ok, reason = False, str(exc)
        if not ok:
            receipt = {'ok': False, 'error': {'code': 'policy_blocked', 'message': reason, 'may_have_sent': False}}
            state = 'BLOCKED'
        else:
            try:
                receipt = (transport or self.transport)(target, text, request_id)
                state = 'SUCCEEDED' if receipt.get('ok') else 'FAILED'
            except Exception as exc:
                receipt = {'ok': False, 'error': {'code': type(exc).__name__, 'may_have_sent': True}}
                state = 'UNKNOWN'
        with database(self.path) as db:
            db.execute('UPDATE sends SET state=?,receipt=? WHERE id=?', (state, json.dumps(receipt, ensure_ascii=False), request_id))
        send_policy.record_send(self.root, request_id=request_id, chat_id=target, text=text, action=action, state=state,
                                receipt=receipt, source=source or action, target_label=target)
        return {'state': state, 'target': target, 'request_id': request_id, 'receipt': receipt, **({} if ok else {'error': reason})}

    @staticmethod
    def _channel_transport(target, text, request_id):
        """One-shot send over the fish-qq channel (same protocol/token source as the QQ loop).

        Connects once; on failure the outcome is UNKNOWN and the caller must not blind-retry.
        ``FISH_QQ_TRANSPORT`` (documented in RESULT.md) may name an importable factory
        ``(root) -> callable(target, text, key)`` for embeds that own a live channel client.
        """
        root = Path(__file__).resolve().parent.parent
        factory = os.environ.get('FISH_QQ_TRANSPORT', '').strip()
        if factory:
            module, _, name = factory.partition(':')
            transport = getattr(__import__(module), name or 'transport')(root)
            return transport(target, text, request_id)

        async def deliver():
            from websockets.asyncio.client import connect
            from secrets_store import SecretStore
            token = os.environ.get('FISH_QQ_TOKEN', '') or SecretStore(root).get('channel-core', 'FISH_QQ_TOKEN')
            if not token:
                return {'ok': False, 'error': {'code': 'no_channel_token', 'may_have_sent': False}}
            async with connect('ws://127.0.0.1:3101/ws', additional_headers={'Authorization': 'Bearer ' + token},
                               max_size=1024 * 1024, open_timeout=5) as ws:
                await ws.send(json.dumps({'v': 1, 'type': 'hello', 'id': 'scheduled-hello', 'ts': '',
                                          'payload': {'client': 'fish-scheduled-sender'}}))
                hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if hello.get('type') != 'hello.ok':
                    return {'ok': False, 'error': {'code': 'channel_hello_failed', 'may_have_sent': False}}
                action_id = 'core-' + uuid.uuid4().hex[:12]
                await ws.send(json.dumps({'v': 1, 'type': 'action', 'id': action_id, 'action': 'send_message', 'ts': '',
                    'payload': {'chat_id': target, 'segments': [{'type': 'text', 'text': text}], 'idempotency_key': 'scheduled:' + request_id}}))
                while True:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), 30))
                    if frame.get('type') == 'action.result' and frame.get('id') == action_id:
                        return frame
        return asyncio.run(deliver())


__all__ = ['BotControl', 'describe', 'timing_from_row', 'ZONE', 'TIMING']
