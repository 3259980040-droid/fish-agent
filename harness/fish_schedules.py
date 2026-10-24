"""Fish-owned persistent plans and occurrences; neither CRUD nor execution needs DSH."""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from storage import database

UTC = timezone.utc
TIMING = ('after_seconds', 'at', 'every_seconds', 'daily', 'weekly', 'cron')


def moment(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(UTC)


def next_time(timing, after, created):
    """Calendar recurrence from one authoritative selector, using aware timestamps."""
    key = next(iter(timing))
    value = timing[key]
    if key in ('after_seconds', 'every_seconds'):
        if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
            raise ValueError('间隔必须是大于零的整数秒')
        seconds = int(value)
        if key == 'every_seconds' and seconds < 60:
            raise ValueError('重复间隔至少为 60 秒')
        first = created + timedelta(seconds=seconds)
        if key == 'after_seconds':
            return first if first > after else None
        steps = max(0, int((after - first).total_seconds() // seconds) + 1)
        return first + timedelta(seconds=steps * seconds)
    if not isinstance(value, dict):
        raise ValueError('日期、每日、每周和 Cron 时间必须按字段填写')
    zone = ZoneInfo(str(value.get('time_zone') or 'Asia/Shanghai'))
    local = after.astimezone(zone)
    if key == 'at':
        target = datetime.fromisoformat(str(value['date']) + 'T' + str(value['time'])).replace(tzinfo=zone)
        return target.astimezone(UTC) if target > local else None
    if key == 'cron':
        from croniter import croniter
        expression = str(value.get('expression') or '')
        if len(expression.split()) != 5:
            raise ValueError('Cron 需要标准的五段表达式：分 时 日 月 星期')
        return croniter(expression, local).get_next(datetime).astimezone(UTC)
    clock = datetime.strptime(str(value['time']), '%H:%M:%S' if len(str(value['time'])) == 8 else '%H:%M').time()
    weekdays = value.get('weekdays') if key == 'weekly' else list(range(1, 8))
    if not weekdays or any(type(d) is not int or not 1 <= d <= 7 for d in weekdays):
        raise ValueError('每周至少选一天，星期一到星期日使用 1 到 7')
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        target = datetime.combine(day, clock, zone)
        if target > local and target.isoweekday() in weekdays:
            return target.astimezone(UTC)
    raise ValueError('没有找到下一次执行时间')


class FishSchedules:
    def __init__(self, control):
        self.control, self.root, self.path = control, control.root, control.path
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = {r['name'] for r in db.execute('PRAGMA table_info(scheduled)')}
            for name, definition in (('purpose', "TEXT NOT NULL DEFAULT 'reminder'"),
                                     ('engine', "TEXT NOT NULL DEFAULT 'dsh'"),
                                     ('next_at', 'TEXT'), ('last_scan', 'REAL'),
                                     ('configuration_key', 'TEXT')):
                if name not in existing:
                    db.execute('ALTER TABLE scheduled ADD COLUMN ' + name + ' ' + definition)

    def rows(self):
        with database(self.path) as db:
            return [dict(r) for r in db.execute('SELECT * FROM scheduled ORDER BY seq DESC')]

    def get(self, schedule_id):
        with database(self.path) as db:
            row = db.execute('SELECT * FROM scheduled WHERE id=?', (schedule_id,)).fetchone()
        if not row or row['state'] == 'deleted':
            raise ValueError('任务不存在或已删除')
        return dict(row)

    def view(self, record):
        import send_policy
        from bot_control import describe
        row = dict(record)
        row.update(sessionId=row['session'], timing=json.loads(row['timing']),
                   scheduledAt=row.get('next_at') or '', nextRunAt=row.get('next_at') or '',
                   status=row['state'], targetLabel='主人私聊' if row['target'].startswith('private:') else '群聊',
                   purposeLabel='主人委托提醒' if row['purpose'] == 'reminder' else '自主唤醒机会',
                   engineLabel='Fish 独立 Agent' if row['engine'] == 'fish' else '旧 DSH 任务待迁移')
        row['when'] = describe(row['timing'], row['nextRunAt'])
        row['nextRunDisplay'] = send_policy.display(row['nextRunAt'])
        with database(self.path) as db:
            occurrences = [dict(r) for r in db.execute('SELECT * FROM occurrences WHERE schedule_id=? ORDER BY created DESC', (row['id'],))]
            receipts = [dict(r) for r in db.execute('SELECT * FROM sends WHERE id LIKE ? ORDER BY created DESC', ('sched:' + row['id'] + ':%',))]
        for receipt in receipts:
            raw = json.loads(receipt['receipt'] or '{}')
            payload = raw.get('payload') or {}
            receipt['messageId'] = str(payload.get('message_id') or next(iter(payload.get('message_ids') or []), ''))
            receipt['receipt'] = ('OK msg=' + receipt['messageId'] if receipt['state'] == 'SUCCEEDED'
                                  else json.dumps(raw.get('error') or receipt['state'], ensure_ascii=False))
        row['occurrences'] = [{**r, 'display': send_policy.display(r['slot'])} for r in occurrences]
        row['sendReceipts'] = receipts
        if occurrences:
            row['occurrenceState'] = occurrences[0]['state']
            row['result'] = occurrences[0]['note'] or ''
            row['receipt_id'] = occurrences[0]['receipt_id'] or ''
        return row

    def create(self, params, *, session=None):
        target = self.control.validate_target(params.get('target') or 'owner')
        prompt = str(params.get('prompt') or '').strip()
        if not prompt:
            raise ValueError('请填写到点要做什么')
        timing = {k: params[k] for k in TIMING if params.get(k) not in (None, '', {}, [])}
        if len(timing) != 1:
            raise ValueError('请只选一种时间：延时、指定时间、每日、每周、固定间隔或 Cron')
        purpose = str(params.get('purpose') or 'reminder')
        if purpose not in ('reminder', 'autonomy'):
            raise ValueError('任务用途应为 reminder（已委托提醒）或 autonomy（自主机会）')
        now = datetime.now(UTC)
        due = next_time(timing, now, now)
        if due is None:
            raise ValueError('执行时间已经过去，请指定未来时间')
        sid = uuid.uuid4().hex
        title = str(params.get('title') or prompt[:24]).strip()
        # The session is an execution correlation key, not a new persona/window.
        session = 'fish-schedule:' + sid
        with database(self.path) as db:
            db.execute('INSERT INTO scheduled(id,target,session,title,prompt,original,timing,timing_key,request,state,created,updated,purpose,engine,next_at,configuration_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (sid, target, session, title, prompt, str(params.get('original') or prompt), json.dumps(timing, ensure_ascii=False),
                 next(iter(timing)), json.dumps(params, ensure_ascii=False), 'active', now.timestamp(), now.timestamp(),
                 purpose, 'fish', due.isoformat(), params.get('configuration_key')))
        return self.view(self.get(sid))

    def update(self, params):
        row = self.get(params['id'])
        if row['engine'] != 'fish':
            raise ValueError('此旧任务尚未迁移；先迁移执行归属，不能同时修改两个调度器')
        change = params.get('change') or {}
        timing = {k: change[k] for k in TIMING if k in change}
        timing.update({k: params[k] for k in TIMING if k in params})
        if len(timing) > 1:
            raise ValueError('请只选择一种时间方式')
        next_at, created = row['next_at'], row['created']
        if timing:
            now = datetime.now(UTC)
            due = next_time(timing, now, now)
            if due is None:
                raise ValueError('新的执行时间已经过去')
            next_at, created = due.isoformat(), now.timestamp()
        else:
            timing = json.loads(row['timing'])
        state = str(params.get('state') or change.get('state') or row['state'])
        if state not in ('active', 'paused', 'completed'):
            raise ValueError('任务状态应为 active、paused 或 completed')
        purpose = str(params.get('purpose') or row['purpose'])
        if purpose not in ('reminder', 'autonomy'):
            raise ValueError('计划用途应为 reminder 或 autonomy')
        target = self.control.validate_target(params.get('target') or row['target'])
        prompt = str(params.get('prompt', row['prompt'])).strip()
        if not prompt:
            raise ValueError('任务内容不能为空')
        with database(self.path) as db:
            db.execute('UPDATE scheduled SET title=?,prompt=?,original=?,timing=?,timing_key=?,next_at=?,state=?,created=?,updated=?,purpose=?,target=? WHERE id=?',
                (str(params.get('title', row['title'])), prompt, prompt, json.dumps(timing, ensure_ascii=False), next(iter(timing)),
                 next_at, state, created, time.time(), purpose, target, row['id']))
        return self.view(self.get(row['id']))

    def delete(self, sid):
        row = self.get(sid)
        if row['engine'] != 'fish':
            raise ValueError('旧 DSH 计划尚未迁移，不能仅删除本地镜像假装已停止')
        with database(self.path) as db:
            db.execute("UPDATE scheduled SET state='deleted',next_at=NULL,updated=? WHERE id=?", (time.time(), sid))
            db.execute("UPDATE occurrences SET state='cancelled',note='主人删除了计划',updated=? WHERE schedule_id=? AND state IN ('due','catchup','queued')", (time.time(), sid))
        return {'id': sid, 'deleted': True}

    def detect(self, now=None):
        now = moment(now) if now else datetime.now(UTC)
        changes = []
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT * FROM scheduled WHERE engine='fish' AND state='active' AND next_at IS NOT NULL").fetchall():
                due = moment(row['next_at'])
                if due > now:
                    continue
                timing = json.loads(row['timing'])
                created = datetime.fromtimestamp(row['created'], UTC)
                # Resume one missed occurrence rather than releasing an outage burst.
                latest = due
                if row['timing_key'] == 'every_seconds':
                    latest += timedelta(seconds=int((now - due).total_seconds() // int(timing['every_seconds'])) * int(timing['every_seconds']))
                elif row['timing_key'] == 'cron':
                    from croniter import croniter
                    value = timing['cron']
                    local = (now + timedelta(microseconds=1)).astimezone(ZoneInfo(str(value.get('time_zone') or 'Asia/Shanghai')))
                    latest = max(due, croniter(str(value['expression']), local).get_prev(datetime).astimezone(UTC))
                elif row['timing_key'] in ('daily', 'weekly'):
                    previous = next_time(timing, now - timedelta(days=8 if row['timing_key'] == 'weekly' else 1), created)
                    while previous is not None and previous <= now:
                        if previous > latest:
                            latest = previous
                        previous = next_time(timing, previous, created)
                if latest != due:
                    db.execute('INSERT OR IGNORE INTO occurrences(schedule_id,slot,state,note,created,updated) VALUES(?,?,?,?,?,?)',
                        (row['id'], due.isoformat(), 'skipped', '停机期间旧周期合并，只补最近一次', time.time(), time.time()))
                slot = latest.isoformat()
                state = 'catchup' if (now - latest).total_seconds() > 60 else 'due'
                inserted = db.execute('INSERT OR IGNORE INTO occurrences(schedule_id,slot,state,created,updated) VALUES(?,?,?,?,?)',
                                      (row['id'], slot, state, time.time(), time.time())).rowcount
                upcoming = next_time(timing, now, created)
                db.execute('UPDATE scheduled SET next_at=?,state=?,last_scan=?,updated=? WHERE id=?',
                    (upcoming.isoformat() if upcoming else None, 'active' if upcoming else 'completed', now.timestamp(), time.time(), row['id']))
                if inserted:
                    changes.append({'id': row['id'], 'slot': slot, 'state': state})
        return changes

    def run_now(self, sid):
        row = self.get(sid)
        if row['engine'] != 'fish':
            raise ValueError('此计划尚未从 DSH 迁移')
        slot = datetime.now(UTC).isoformat()
        with database(self.path) as db:
            db.execute("INSERT INTO occurrences(schedule_id,slot,state,created,updated) VALUES(?,?,'due',?,?)", (sid, slot, time.time(), time.time()))
        return {'id': sid, 'slot': slot, 'state': 'due'}

    def pending(self):
        with database(self.path) as db:
            return [dict(r) for r in db.execute("SELECT o.schedule_id id,o.slot,o.state FROM occurrences o JOIN scheduled s ON s.id=o.schedule_id WHERE s.engine='fish' AND s.state NOT IN ('deleted','paused') AND o.state IN ('due','catchup','queued') ORDER BY o.created")]

    def claim(self, sid, slot):
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT s.state,s.engine FROM scheduled s WHERE s.id=?', (sid,)).fetchone()
            if not row or row['engine'] != 'fish' or row['state'] in ('deleted', 'paused'):
                return False
            return bool(db.execute("UPDATE occurrences SET state='running',attempts=attempts+1,updated=? WHERE schedule_id=? AND slot=? AND state IN ('due','catchup','queued')", (time.time(), sid, slot)).rowcount)

    def finish(self, sid, slot, state, note='', receipt_id=''):
        with database(self.path) as db:
            db.execute('UPDATE occurrences SET state=?,note=?,receipt_id=?,updated=? WHERE schedule_id=? AND slot=?', (state, note, receipt_id, time.time(), sid, slot))
        return self.occurrence(sid, slot)

    def occurrence(self, sid, slot):
        with database(self.path) as db:
            row = db.execute('SELECT * FROM occurrences WHERE schedule_id=? AND slot=?', (sid, slot)).fetchone()
        if not row:
            raise ValueError('本次执行记录不存在')
        return dict(row)

    def run(self, sid, slot, *, run_turn=None,cancelled=None):
        if not self.claim(sid, slot):
            return self.occurrence(sid, slot)
        row = self.get(sid)
        prefix = 'sched:' + sid + ':' + slot
        error = ''
        skipped = ''
        try:
            if run_turn:
                run_turn(row['session'], self.prompt(row, slot))
            else:
                skipped = self._thought(row, slot, cancelled=cancelled) or ''
        except Exception as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        with database(self.path) as db:
            receipts = [dict(r) for r in db.execute('SELECT * FROM sends WHERE id LIKE ? ORDER BY created', (prefix + '%',))]
        uncertain = next((r for r in receipts if r['state'] in ('UNKNOWN', 'EXECUTING')), None)
        failed = next((r for r in receipts if r['state'] in ('FAILED', 'BLOCKED')), None)
        sent = [r for r in receipts if r['state'] == 'SUCCEEDED']
        if uncertain:
            state, note, rid = 'uncertain', '发送未确认，没有自动重发', uncertain['id']
        elif error or failed:
            state, note, rid = 'failed', error or str(json.loads(failed['receipt'] or '{}').get('error') or '发送失败'), failed['id'] if failed else ''
        elif sent:
            state, note, rid = 'sent', '已取得实际消息编号；' + str(len(sent)) + ' 次发送', sent[-1]['id']
        elif skipped:
            state, note, rid = 'skipped', skipped, ''
        elif row['purpose'] == 'autonomy':
            state, note, rid = 'no_action', '自主回合完成，没有向外发送消息；工具过程见本轮执行记录', ''
        else:
            state, note, rid = 'failed', '已委托提醒执行结束，但没有取得发送回执', ''
        result = self.finish(sid, slot, state, note, rid)
        from governance import Governance
        policy = Governance(self.root, 'qq')
        if state in ('failed', 'uncertain'):
            policy.fault('schedule:' + sid, note, stage='delivery' if receipts else 'model', uncertain=state == 'uncertain')
        elif state == 'sent':
            policy.fault_recovered('schedule:' + sid)
        policy.audit('fish_schedule_' + state, row['title'] + '：' + note, prefix)
        return result

    @staticmethod
    def prompt(row, slot):
        head = ['[Fish 到期事件] ' + row['title'], '实际到期时间：' + slot,
                '主人记录的原始委托：' + row['original'] if row['purpose'] == 'reminder' else '你之前安排的自主机会：' + row['prompt']]
        if row['purpose'] == 'reminder':
            head.append('这是已接受的定时委托，请现在履行。需要工具就实际使用；结果将由原发送通道交给 ' + row['target'] + '。本次不是自主聊天，不以“想不想说”取消提醒，不声称已经送达。')
        else:
            head.append('这是一次新的自主唤醒机会，历史消息不是新的来信；先看哪些事之前已考虑、哪些已回复，不重复回应同一条消息。结合共同经历决定做事、查资料、联系主人、到群里接话，或不行动。想说话可调用 fish_message_send 选目标，或用 fish_say 提交给当前场所。发言身份始终是 Fish 自己。')
        return '\n'.join(head)

    def _thought(self, row, slot, *, cancelled=None):
        from experience import ExperienceTurn, delivery
        from fish_chat import respond
        from chat_output import TurnSilent, decode_reply
        from memory_store import MemoryStore
        from qq_store import QQStore
        from social_context import SocialContext
        from tool_host import bind_session
        social = SocialContext(self.root)
        persona = social.active_persona('qq')
        store = QQStore(self.root)
        if str(row.get('configuration_key') or '').startswith('autonomy-') and not self.recent_activity(row):
            return '最近两小时没有相关场所活动，未调用模型，也未打扰任何群'
        conversation = store.get_or_create_conversation(row['target'], 'qq')
        session = row['session']
        # An autonomous occurrence is her own Agent execution, not a forged owner message.
        authority = json.loads(row.get('request') or '{}').get('_authority') or {'owner':True}
        scope = 'owner' if authority.get('owner') else authority['scope']
        bind_session(self.root,'qq',session,scope,social.owner() if authority.get('owner') else authority['sender'])
        reference = 'wake:' + row['id'] + ':' + slot
        event = {'reference': reference, 'ts': datetime.now(UTC).isoformat(), 'text': row['prompt'], 'recent_window': 7200}
        channel = 'qq' if row['target'].startswith('private:') else row['target']
        experience = ExperienceTurn(self.root, store.db_path, conversation['id'], channel=channel, event=event)
        prompt = self.prompt(row, slot)
        self._bind_occurrence(session, row, slot)
        try:
            raw = respond(self.root, 'qq', persona=persona, history=[], history_source={},
                memory=MemoryStore(self.root, 'qq').context(row['prompt'], scope), scope=scope, references='',
                current=[{'type': 'text', 'text': prompt}], session=session, cancelled=cancelled,
                origin='fish-schedule:' + row['id'], output='bubbles',
                tool_context='owner-private' if authority.get('owner') else 'group', experience=experience, delivery_required=row['purpose'] == 'reminder')
        except TurnSilent:
            return
        finally:
            self._unbind_occurrence(session)
        with database(self.path) as db:
            # A direct tool send is already an outcome, including unknown delivery.
            sent = db.execute('SELECT 1 FROM sends WHERE id LIKE ?', ('sched:' + row['id'] + ':' + slot + '%',)).fetchone()
        if sent:
            return
        bubbles = decode_reply(self.root, raw)
        if cancelled is not None and cancelled.is_set(): raise InterruptedError('后台正在关闭，未继续发送本次结果')
        delivery(self.root, reference, sending=True)
        result = self.control.deliver_bubbles(row['target'], bubbles, 'sched:' + row['id'] + ':' + slot,
            source='fish-autonomous' if row['purpose'] == 'autonomy' else 'owner-reminder',
            scheduled=row['purpose'] == 'reminder', context='owner:schedule' if row['purpose'] == 'reminder' else 'fish:autonomous')
        delivery(self.root, reference, outcome=result['state'].lower())

    def recent_activity(self, row):
        """Quiet groups are not wake candidates; do not re-read already-considered inputs."""
        import sqlite3
        from governance import Governance
        from group_policy import GroupPolicy
        owner = Governance(self.root, 'qq').owner_id()
        cutoff = time.time() - 7200
        group_policy = GroupPolicy(self.root)
        patrol = row.get('configuration_key') == 'autonomy-patrol'
        for source, filename in (('qq', 'qq.db'), ('desktop', 'desktop-chat.db')):
            path = self.root / 'data/qq' / filename
            if not path.is_file():
                continue
            with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                for event in db.execute("SELECT * FROM messages WHERE direction='inbound' ORDER BY id DESC"):
                    event = dict(event)
                    stamp = event.get('ts') or event.get('created_at')
                    if not stamp or moment(stamp).timestamp() < cutoff:
                        continue
                    chat = str(event.get('chat_id') or 'private:' + owner)
                    if chat.startswith('group:'):
                        cfg = group_policy.config(chat)
                        if not cfg['listen'] or not cfg['reply']:
                            continue
                    elif patrol:
                        continue
                    return True
        return False

    def _bind_occurrence(self, session, row, slot):
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS active_occurrences(session TEXT PRIMARY KEY,schedule_id TEXT,slot TEXT,target TEXT,purpose TEXT)')
            db.execute('INSERT OR REPLACE INTO active_occurrences VALUES(?,?,?,?,?)', (session, row['id'], slot, row['target'], row['purpose']))

    def _unbind_occurrence(self, session):
        with database(self.path) as db:
            db.execute('DELETE FROM active_occurrences WHERE session=?', (session,))

    def active(self, session):
        with database(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='active_occurrences'").fetchone():
                return None
            row = db.execute('SELECT * FROM active_occurrences WHERE session=?', (session,)).fetchone()
        return dict(row) if row else None

