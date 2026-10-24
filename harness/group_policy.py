"""Durable group listening and reply admission; no model calls or transport.

Refined from the isolated DeepSeek worker's GroupPolicy proposal. Store only a
bounded context, reserve before model work, and recheck before every send.

Of the three engines only fish really answers a group, and it answers through the
reservation this store owns: one live reservation per chat and one per cursor, so
a second inbound, a redelivered message or a competing worker cannot generate or
send beside the holder.
"""
from pathlib import Path
import json
import random
import re
import sqlite3
import time
import uuid
from storage import database

def _wake_draw(chat, cursor):
    """The wake roll for one message, the same answer every time it is asked.

    A plain random draw here was asked twice per message - once by candidate() and
    again inside reserve() - so a configured 0.5 behaved like 0.25 and a message
    could be refused by a second throw after it had already been chosen. Deriving
    the number from the chat and its cursor keeps the probability honest and makes
    a rehearsal reproduce the live choice exactly.
    """
    return random.Random('%s:%s' % (chat, cursor)).random()


DEFAULTS = dict(listen=False, learn=False, reply=False, read=False, reply_limits=True, mode='familiar', debounce_s=4,
    active_cooldown_s=60, max_active_hour=6, max_total_min=6,
    max_unanswered_active=2, quiet_start='', quiet_end='', engine='fish', shadow_daily_limit=60,
    fish_judge_daily_limit=80, dive_until=0, wake_probability=1.0, followup_s=0,
    proactive_enabled=False, proactive_idle_s=1800, proactive_check_min_s=2700,
    proactive_check_max_s=5400, proactive_probability=0.2,
    prescreen_enabled=True, prescreen_threshold=0.6, prescreen_timeout_s=20,
    energy_enabled=True, energy_cost=0.1, energy_recover_per_min=0.004, target_replies_per_hour=8,
    round_interval_s=180, round_budget_chars=1200, round_hourly_calls=40, round_daily_calls=400,
    dive_max_s=14400, dive_daily_max_s=28800)
# The pre-screen is the decision *before* the paid generation: a cheap judge model
# scores the message 0..1 and only a message over the threshold may be generated at
# all. Enabled by default because it is what keeps the expensive call off the
# messages she was never going to answer; the timeout is the judge's own, not the
# generation's, so a stalled judgement costs one silent message and nothing else.
# Energy is the slow valve AstrBot's Heartflow uses: every active reply she really
# sends costs energy_cost, energy refills with wall-clock time (per minute, so
# 0.004 is the 0.02-per-5-minutes of the reference) and below one reply's worth she
# stops opening her mouth - a mention still gets answered. target_replies_per_hour
# is MaiBot's sliding window: at most this many of her own outbound lines in the
# last hour; 0 means unlimited.
PRESCREEN_THRESHOLD_RANGE = (0, 1)
PRESCREEN_TIMEOUT_RANGE = (5, 60)
TARGET_REPLIES_RANGE = (0, 200)
# The after-reply window: how long the fish loop may keep watching a chat after it
# delivered one bubble, so a line that arrived while it was generating (and was
# refused by the single-sender rule) still gets answered. 0 is off and is the
# default: the turn ends the moment the send does, exactly as before.
# Diving is one epoch second she picks herself (or the owner sets for her): while
# now < dive_until she does not start speaking in the group on her own, and the
# reason says so. A mention is not her own initiative, so @ still gets answered:
# the dive mutes what she would have said unprompted, never someone calling her.
# Observing and learning never stop - a dive is not a disappearance, only a
# decision not to speak. It ends by itself, so nobody has to wake her up again.
# Wake probability is the other half of the same decision: a plain message that
# passes every other gate is only judged when this roll says so. 1.0 keeps the
# old behaviour (every message reaches the judge); a direct mention never rolls.
# Proactive opening is the third way she can speak, and the only one that does not
# start from an inbound message: while it is off (the default) nothing schedules,
# nothing is called and no group behaves any differently. When it is on, a quiet
# group gets an occasional chance to let her start a topic herself - the check is
# spaced by its own random interval, so this is not a per-message rule.
# 自主回合的四个预算，和它们自己的解释：
# round_interval_s 是"每多久最多开一个思考回合"的节拍；@、有人接她的话时不等这个节拍。
# round_budget_chars 是"看"那一步能读进去多少字：最近十五条群聊加她自己的印象，
# 超出的部分按最旧先丢，宁可少读一条，也不把整个聊天记录塞进一次便宜调用。
# round_hourly_calls / round_daily_calls 是便宜判断调用的闸门：用完了这一小时/这一天
# 就不再主动想，只应答（@ 与接话照旧走回复通道）。0 表示一次也不主动想。
# dive_max_s / dive_daily_max_s 是潜水的两道上限：她自己选 15..30 分钟，单次封顶 4 小时，
# 一天累计封顶 8 小时。累计到上限那天她就没有潜水这个选项了，只能沉默或说话。
ROUND_INTERVAL_RANGE = (60, 3600)
ROUND_BUDGET_RANGE = (200, 4000)
ROUND_HOURLY_RANGE = (0, 500)
ROUND_DAILY_RANGE = (0, 5000)
DIVE_SPAN_RANGE = (0, 86400)
DIVE_MINUTES_RANGE = (5, 240)
# A group is quiet enough to open in only after this long without an inbound line;
# 30 minutes is the floor, because a group that just went two minutes silent is
# mid-conversation, not empty.
PROACTIVE_IDLE_RANGE = (300, 7200)
# The proactive check interval (random inside min..max). The floor is 30 minutes
# for the same reason: an opener that arrives every few minutes is a machine, not
# a person who happened to think of something.
PROACTIVE_CHECK_RANGE = (1800, 86400)
# dsh keeps the current GroupDialogue path; shadow also computes a fish_chat candidate
# that is recorded and never sent; fish is the real send engine - one generation per
# cursor, delivered through the group channel by the only holder of the reservation
# reserve() hands out (qq_agent_loop._fish_reply). No engine sends by itself: the
# reservation, not the model call, decides who may speak.
ENGINES = ('fish',)
# Paid shadow model calls one group may make per day; 0 turns the shadow off.
SHADOW_DAILY_LIMIT_RANGE = (0, 500)
# Paid 'is this worth saying' judgements one fish group may make per day; 0 asks
# nothing and answers only direct mentions. Read by group_decide.worth_speaking.
FISH_JUDGE_DAILY_LIMIT_RANGE = (0, 500)
RESERVATION_TTL_S = 180
# 接力点名：她刚说完话、同一个人接着喊她时，那条短话可以越过主动回复冷却一次。
# 冷却要挡的是"每隔几秒就自己起一次话头"，它从来不打算把正在喊她的人闷住——她答完
# 一句，那个人立刻又喊，这是同一段召唤的继续，不是新的主动开口。所以让路的条件必须
# 窄：这条不是 @、不是她自己、句子短（RELAY_TEXT_LIMIT 个字以内，去空白后数），并且
# 从她最近一条真发出去的话数到这条之间，喊她的都得是同一个人（他连发几条都算）。
# 同一段接力最多连续放行 RELAY_BYPASS_LIMIT 次，第三次仍然按冷却处理：有人在喊不是
# 刷屏的通行证，放行记在 relay_bypass(chat,ts) 里，读得到才作数，读不到一律不绕。
RELAY_SPAN_S = 120
RELAY_TEXT_LIMIT = 10
RELAY_BYPASS_LIMIT = 2


class GroupPolicy:
    def __init__(self, root, *, path=None, audit=None):
        self.root = Path(root)
        self.path = Path(path) if path else self.root / 'data/social/group-policy.db'
        # 放行要留痕，而审计账本属于调用方（群循环的 governance）：这里只存一个可选的
        # 记账口，没有它照常判断，绝不因为记账而拒绝或改变任何一次放行。
        self.audit = audit
        with database(self.path) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS groups(chat TEXT PRIMARY KEY,config TEXT,cursor INTEGER DEFAULT 0,handled INTEGER DEFAULT 0,unanswered INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS observations(chat TEXT,seq INTEGER,id TEXT,text TEXT,data TEXT,created REAL,PRIMARY KEY(chat,seq));
            CREATE TABLE IF NOT EXISTS dedup(chat TEXT,id TEXT,created REAL,PRIMARY KEY(chat,id));
            CREATE TABLE IF NOT EXISTS reservations(token TEXT PRIMARY KEY,chat TEXT,cursor INTEGER,kind TEXT,state TEXT,created REAL,finished REAL DEFAULT 0,UNIQUE(chat,cursor));
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_group ON reservations(chat) WHERE state='active';
            CREATE TABLE IF NOT EXISTS summaries(chat TEXT PRIMARY KEY,cursor INTEGER,text TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS clears(chat TEXT PRIMARY KEY,created REAL);
            CREATE TABLE IF NOT EXISTS group_energy(chat TEXT PRIMARY KEY,energy REAL,updated REAL);
            CREATE TABLE IF NOT EXISTS relay_bypass(chat TEXT,ts REAL);
            ''')

    @staticmethod
    def _chat(chat):
        if not re.fullmatch(r'group:[0-9]{5,15}', str(chat)):
            raise ValueError('群范围格式应为 group:群号')
        return str(chat)

    def _state(self, db, chat):
        self._chat(chat)
        db.execute('INSERT OR IGNORE INTO groups(chat,config) VALUES(?,?)', (chat, json.dumps(DEFAULTS)))
        return dict(db.execute('SELECT * FROM groups WHERE chat=?', (chat,)).fetchone())

    def config(self, chat):
        with database(self.path) as db:
            return {**DEFAULTS, **json.loads(self._state(db, chat)['config']), 'engine': 'fish'}

    def configure(self, chat, updates):
        if not isinstance(updates, dict) or set(updates) - set(DEFAULTS):
            raise ValueError('未知的群设置项')
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            stored = {**DEFAULTS, **json.loads(self._state(db, chat)['config']), 'engine': 'fish'}
            cfg = {**stored, **updates}
            for k in ('listen', 'learn', 'reply', 'read', 'reply_limits', 'proactive_enabled',
                      'prescreen_enabled', 'energy_enabled'):
                if type(cfg[k]) is not bool: raise ValueError(k + ' 需要开关值')
            for k, lo, hi in (('debounce_s',0,60), ('active_cooldown_s',0,86400), ('max_active_hour',0,120), ('max_total_min',1,60), ('max_unanswered_active',0,10),
                               ('followup_s',0,60), ('shadow_daily_limit', *SHADOW_DAILY_LIMIT_RANGE),
                               ('prescreen_timeout_s', *PRESCREEN_TIMEOUT_RANGE),
                               ('target_replies_per_hour', *TARGET_REPLIES_RANGE),
                               ('round_interval_s', *ROUND_INTERVAL_RANGE),
                               ('round_budget_chars', *ROUND_BUDGET_RANGE),
                               ('round_hourly_calls', *ROUND_HOURLY_RANGE),
                               ('round_daily_calls', *ROUND_DAILY_RANGE),
                               ('dive_max_s', *DIVE_SPAN_RANGE),
                               ('dive_daily_max_s', *DIVE_SPAN_RANGE)):
                if type(cfg[k]) is not int or not lo <= cfg[k] <= hi: raise ValueError(k + ' 超出范围')
            # The pre-screen threshold, one reply's energy cost and the per-minute
            # recovery are all fractions: bool is excluded for the same reason as
            # dive_until, because True as a cost would mean "one whole unit".
            for k in ('prescreen_threshold', 'energy_cost', 'energy_recover_per_min'):
                if isinstance(cfg[k], bool) or not isinstance(cfg[k], (int, float)):
                    raise ValueError(k + ' 需要数值')
                if not 0 <= float(cfg[k]) <= 1:
                    raise ValueError(k + ' 需在 0 到 1 之间')
            # bool is an int in Python, so the type check has to exclude it: True as a
            # dive deadline would mean "dived in 1970". Both keys are numbers only.
            for k in ('dive_until', 'wake_probability'):
                if isinstance(cfg[k], bool) or not isinstance(cfg[k], (int, float)):
                    raise ValueError(k + ' 需要数值')
            if not 0 <= float(cfg['wake_probability']) <= 1:
                raise ValueError('唤醒概率需在 0 到 1 之间：1 表示每条消息都进入判断')
            if float(cfg['dive_until']) < 0: raise ValueError('潜水截止时间需为 epoch 秒')
            # Proactive opening: its own idle threshold, its own check interval and
            # its own probability, so turning it on never changes how she answers a
            # message that is already there.
            for k, (lo, hi) in (('proactive_idle_s', PROACTIVE_IDLE_RANGE),
                                ('proactive_check_min_s', PROACTIVE_CHECK_RANGE),
                                ('proactive_check_max_s', PROACTIVE_CHECK_RANGE)):
                if type(cfg[k]) is not int or not lo <= cfg[k] <= hi:
                    raise ValueError(k + ' 超出范围')
            if cfg['proactive_check_max_s'] < cfg['proactive_check_min_s']:
                raise ValueError('主动开口的最长检查间隔不能小于最短间隔')
            if isinstance(cfg['proactive_probability'], bool) or not isinstance(cfg['proactive_probability'], (int, float)):
                raise ValueError('主动开口概率需要数值')
            if not 0 <= float(cfg['proactive_probability']) <= 1:
                raise ValueError('主动开口概率需在 0 到 1 之间：0 表示一次也不主动开口')
            judge, (lo, hi) = cfg['fish_judge_daily_limit'], FISH_JUDGE_DAILY_LIMIT_RANGE
            if type(judge) is not int or not lo <= judge <= hi:
                raise ValueError('值不值得开口的每日判断上限需为 0 到 500 的整数（0 表示不做价值判断，只回应提及）')
            if cfg['mode'] not in ('familiar', 'mention'): raise ValueError('未知参与方式')
            if cfg['engine'] not in ENGINES:
                raise ValueError('未知群决策引擎：dsh=旧群通道，shadow=只算不发，'
                                 'fish=真生成并经群发送通道发出（同一游标只有一个发送者）')
            for k in ('quiet_start', 'quiet_end'):
                if not isinstance(cfg[k], str) or (cfg[k] and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', cfg[k])): raise ValueError('静默时间需为 HH:MM')
            if bool(cfg['quiet_start']) != bool(cfg['quiet_end']): raise ValueError('静默开始和结束需同时填写或留空')
            if not cfg['listen']: cfg['reply'] = False
            db.execute('UPDATE groups SET config=? WHERE chat=?', (json.dumps(cfg), chat))
            # Changing the engine is a rollback of whatever that group's sender was
            # allowed to do, so a reply already generating under the old engine loses
            # its permission here rather than at its next send. _retire's own state
            # and handled bookkeeping is reused; no new table, no new column.
            if cfg['engine'] != stored['engine'] or not cfg['listen'] or not cfg['reply']:
                self._retire(db, chat, 'disabled')
        return cfg

    def observe(self, payload, now=None):
        chat = self._chat(payload.get('chat_id', '')); now = time.time() if now is None else float(now)
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE'); state = self._state(db, chat)
            if not json.loads(state['config'])['listen']: return {'accepted': False}
            mid = str(payload.get('message_id', ''))
            if not mid or len(mid) > 200: raise ValueError('消息缺少有效编号')
            if db.execute('SELECT 1 FROM dedup WHERE chat=? AND id=?', (chat, mid)).fetchone(): return {'accepted': False, 'duplicate': True}
            sender = payload.get('sender') or {}
            if not isinstance(sender, dict): raise ValueError('无效发送者')
            parts = []; text = ''
            for segment in payload.get('segments', [])[:40]:
                if segment.get('type') == 'text':
                    value = str(segment.get('text', ''))[:4000-len(text)]; text += value; parts.append({'type':'text','text':value})
                elif segment.get('type') in ('image','audio','video'):
                    parts.append({'type': segment['type'], 'ref': str(segment.get('asset_id') or mid)[:160]})
                elif segment.get('type') == 'at':
                    parts.append({'type': 'at', 'user_id': str(segment.get('user_id') or '')[:40]})
            cursor = state['cursor'] + 1
            person = {key: str(sender[key])[:80] for key in ('nickname', 'card') if key in sender}
            person.update(id=str(sender.get('id',''))[:40], name=str(sender.get('display_name') or sender.get('name') or '')[:80])
            data = {'message_id':mid, 'sender':person,
                    'text':text,'segments':parts,'reply_to':str(payload.get('reply_to') or '')[:200],
                    'direct':bool(payload.get('mentions_bot') or payload.get('reply_to_bot')), 'cursor':cursor,'ts':now}
            db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)', (chat,cursor,mid,text,json.dumps(data,ensure_ascii=False),now))
            db.execute('INSERT INTO dedup VALUES(?,?,?)', (chat,mid,now))
            db.execute('UPDATE groups SET cursor=?,unanswered=CASE WHEN ? THEN 0 ELSE unanswered END WHERE chat=?', (cursor,data['direct'],chat))
            db.execute('DELETE FROM observations WHERE chat=? AND seq<=?', (chat,cursor-60))
            db.execute('DELETE FROM dedup WHERE created<?', (now-30*86400,))
            return {'accepted':True,'cursor':cursor}

    def _snapshot(self, db, chat, limit=40):
        state = self._state(db, chat)
        rows = db.execute('SELECT data FROM observations WHERE chat=? ORDER BY seq DESC LIMIT ?', (chat,max(1,min(60,int(limit))))).fetchall()
        summary = db.execute('SELECT * FROM summaries WHERE chat=?', (chat,)).fetchone()
        return {'chat_id':chat, 'cursor':state['cursor'], 'messages':[json.loads(r['data']) for r in reversed(rows)],
                'summary':summary['text'] if summary else '', 'summary_cursor':summary['cursor'] if summary else 0,
                'config':{**DEFAULTS,**json.loads(state['config'])}}

    def snapshot(self, chat, limit=40):
        with database(self.path) as db: return self._snapshot(db, chat, limit)

    def save_summary(self, chat, through_cursor, text):
        if type(through_cursor) is not int or not isinstance(text,str): raise ValueError('摘要参数无效')
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE'); state = self._state(db, chat)
            if not 0 <= through_cursor <= state['cursor']: raise ValueError('摘要游标无效')
            if through_cursor and not db.execute('SELECT 1 FROM observations WHERE chat=? AND seq=?',(chat,through_cursor)).fetchone():
                return False  # cleared/trimmed while summarization was in flight
            old = db.execute('SELECT cursor FROM summaries WHERE chat=?',(chat,)).fetchone()
            if not old or old['cursor'] <= through_cursor:
                db.execute('INSERT OR REPLACE INTO summaries VALUES(?,?,?,?)', (chat,through_cursor,text[:6000],time.time()))

    def _retire(self, db, chat, state, *, before=None):
        rows = db.execute("SELECT token,cursor FROM reservations WHERE chat=? AND state='active'" + (' AND created<=?' if before is not None else ''),
                          (chat,before) if before is not None else (chat,)).fetchall()
        for row in rows:
            db.execute('UPDATE reservations SET state=? WHERE token=?', (state,row['token']))
            db.execute('UPDATE groups SET handled=MAX(handled,?) WHERE chat=?', (row['cursor'],chat))

    @staticmethod
    def _quiet(cfg, now):
        if not cfg['quiet_start']: return False
        current = time.strftime('%H:%M',time.localtime(now)); a,b = cfg['quiet_start'],cfg['quiet_end']
        return (a <= current < b) if a < b else (current >= a or current < b)

    def _recent_lines(self, chat, now, span=RELAY_SPAN_S):
        """这个群最近 span 秒里说过的话，从旧到新；读不到就是一个空列表。

        qq.db 是群自己记下的账（她真发出去的和别人真说出来的都在里面），所以这里是
        只读地看那一份账，不往里面写、也不用我们自己再记一份"谁说了什么"。时间戳是
        ts 文本（%Y-%m-%d %H:%M:%S），解析不了的行直接跳过：一行读不懂只损失一行。
        """
        store = self.root / 'data/qq/qq.db'
        if not store.is_file():
            return []
        try:
            conn = sqlite3.connect(store.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        except sqlite3.Error:
            return []
        try:
            conn.row_factory = sqlite3.Row
            since = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - span))
            rows = conn.execute(
                "SELECT ts,direction,user_id,text,status FROM messages WHERE chat_id=? AND ts>=? "
                "AND direction IN ('inbound','outbound') ORDER BY ts,id", (chat, since)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        lines = []
        for row in rows:
            try:
                stamp = time.mktime(time.strptime(str(row['ts'])[:19], '%Y-%m-%d %H:%M:%S'))
            except (TypeError, ValueError):
                continue
            if stamp > now:  # 时钟倒退或未来行：它还没说过，不能拿来当"接力"的证据
                continue
            lines.append({'ts': stamp, 'direction': str(row['direction'] or ''),
                          'user': str(row['user_id'] or ''), 'text': str(row['text'] or ''),
                          'status': str(row['status'] or '')})
        return lines

    def _relay_summons(self, chat, sender, text, now):
        """这条短话是不是"同一个人在她答完之后还在喊她"？

        从这条往前数，她自己最近一条真的发出去的话之后，喊她的入站消息必须全部来自
        同一个人（他连发几条都算），这段窗口不超过 RELAY_SPAN_S。本条本身不参与这个
        集合：接力是否成立看的是"她答完之后有哪些人在喊"，本条是谁喊的由入站记录自己
        决定（另一个人接着喊，仍然是同一段接力）。

        一律只看得到的事实：读不到 qq.db、格式不认识、找不到这一条自己的记录，都答
        False。绕开冷却是一个洞，洞只能由能复现的证据打开；读失败最多是不绕。
        """
        try:
            words = re.sub(r'\s+', '', str(text or ''))
            if not words or len(words) > RELAY_TEXT_LIMIT:
                return False
            me = str((sender or {}).get('id') or '') if isinstance(sender, dict) else str(sender or '')
            if not me or me == 'bot':  # 她自己发的行不在"有人喊她"之列
                return False
            lines = self._recent_lines(chat, now)
            if not lines:
                return False
            here = None
            for index in range(len(lines) - 1, -1, -1):
                line = lines[index]
                if line['direction'] != 'inbound' or line['user'] != me:
                    continue
                if re.sub(r'\s+', '', line['text']) == words:
                    here = index
                    break
            if here is None:
                return False
            callers = set()
            anchor = None
            for line in reversed(lines[:here]):
                if line['direction'] == 'outbound':
                    if line['status'] and line['status'] != 'sent':
                        continue  # 回执不明的那条不算"她说过话"，接着往前找
                    anchor = line
                    break
                # 他连发几条都算同一段接力，所以同一发送者重复出现不拆开这次接力。
                callers.add(line['user'])
            if anchor is None:
                return False  # 这段窗口里她没说过话：冷却根本不是挡在这里的那道门
            return len(callers) <= 1
        except Exception:  # noqa: BLE001 - 读不到或异常一律不绕冷却
            return False

    def _relay_used(self, db, chat, now):
        """这段接力已经放行过几次：只数真放行（reserve 记的账），不数判定被问过几次。"""
        row = db.execute('SELECT COUNT(*) FROM relay_bypass WHERE chat=? AND ts>?',
                         (chat, now - RELAY_SPAN_S)).fetchone()
        return int(row[0] or 0) if row else 0

    def relay_granted(self, chat, cursor):
        """这个游标的放行是接力点名给的吗？（预筛用同一个事实，不另开额度）

        reserve() 在同一个时刻写 reservations 和 relay_bypass 两行，所以拿 created
        和 ts 对上就是"这一票是它给的"，而不是"这个群最近放行过"——后者会把额度
        借给别的消息。没有这一行、读不到、抛异常，一律 False：调用方（预筛）拿到
        False 就照常花那一次预筛，异常绝不等于放行。
        """
        try:
            with database(self.path) as db:
                return bool(db.execute(
                    'SELECT 1 FROM reservations r JOIN relay_bypass b ON b.chat=r.chat AND b.ts=r.created '
                    'WHERE r.chat=? AND r.cursor=?', (chat, int(cursor))).fetchone())
        except Exception:  # noqa: BLE001 - 读不到就当没放行过
            return False

    def _relay_audit(self, words, chat):
        if not self.audit:
            return
        try:
            self.audit('group_relay_bypass', ('接力点名越过冷却 ' + words)[:250] + ' ' + chat)
        except Exception:  # noqa: BLE001 - 记账失败不该让已经给的这一次放行作废
            pass

    def _candidate(self, db, chat, now, token='', *, continuation=False):
        state = self._state(db, chat)
        cfg = {**DEFAULTS, **json.loads(state['config'])}
        cursor = state['cursor']
        def result(action, reason, kind=None):
            return {'action': action, 'reason': reason, 'kind': kind, 'cursor': cursor,
                    'snapshot': self._snapshot(db, chat) if action == 'candidate' else None}
        if not cfg['listen'] or not cfg['reply']:
            return result('silent', '旁听或发言未开启')
        if cursor <= state['handled']:
            return result('silent', '没有新消息')
        if db.execute("SELECT 1 FROM reservations WHERE chat=? AND state='active' AND token<>?", (chat, token)).fetchone():
            return result('wait', '正在处理本群上一轮；新消息继续保存')
        observations = db.execute('SELECT data FROM observations WHERE chat=? AND seq>? ORDER BY seq', (chat, state['handled'])).fetchall()
        if not observations:
            return result('silent', '没有上下文')
        kind = 'direct' if any(json.loads(row['data'])['direct'] for row in observations) else 'active'
        if kind == 'active' and cfg['mode'] == 'mention':
            return result('silent', '主人设置为只回应提及', kind)
        if kind == 'active' and now < float(cfg.get('dive_until') or 0):
            return result('silent', 'Fish 当前选择潜水到 ' + time.strftime('%H:%M', time.localtime(cfg['dive_until'])), kind)
        return result('candidate', '交给同一 Fish 回合决定是否行动', kind)

    def recover_abandoned(self):
        # Called only after the QQ loop has acquired its singleton listening port.
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE reservations SET state='uncertain',finished=? WHERE state='active'", (time.time(),))

    def candidate(self, chat, now=None, *, continuation=False):
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            return self._candidate(db,chat,time.time() if now is None else float(now),continuation=continuation)

    def reserve(self, chat, cursor, kind, now=None, *, continuation=False):
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE'); now = time.time() if now is None else float(now)
            candidate = self._candidate(db,chat,now,continuation=continuation)
            if candidate['action']!='candidate' or candidate['cursor']!=cursor or candidate['kind']!=kind: return ''
            if db.execute('SELECT 1 FROM reservations WHERE chat=? AND cursor=?',(chat,cursor)).fetchone(): return ''
            token = uuid.uuid4().hex
            db.execute('INSERT INTO reservations(token,chat,cursor,kind,state,created) VALUES(?,?,?,?,?,?)',(token,chat,cursor,kind,'active',now))
            # 放行只在这里记账：预留是这一次开口的唯一一张票，同一个游标只有一张，所以
            # relay_bypass 数的是"真放行了第几次"，不是"判定被问了几次"——candidate()、
            # reserve()、valid() 都会问判定，只有拿到票的这一次才是放行。
            relay = str(candidate.get('relay') or '')
            if relay:
                db.execute('INSERT INTO relay_bypass(chat,ts) VALUES(?,?)',(chat,now))
                self._relay_audit(relay, chat)
            return token

    def valid(self, token, now=None, *, continuation=False):
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE'); now = time.time() if now is None else float(now)
            row = db.execute("SELECT * FROM reservations WHERE token=? AND state='active'",(token,)).fetchone()
            if not row or now-row['created']>=180 or now<row['created']: return False
            c = self._candidate(db,row['chat'],now,token,continuation=continuation)
            return c['action']=='candidate' and c['cursor']==row['cursor'] and c['kind']==row['kind']

    def live(self, token, now=None):
        """May this reservation still send: mine, active, in TTL, and still authorised?

        Unlike valid() this does not re-ask the candidate: a newer inbound message
        must not silently cancel a reply that is already generating, and it cannot
        start a second one either, because this reservation stays the chat's only
        live one until finish() ends it.

        It does re-read the chat's current authorisation, because a reservation
        outlives the settings that granted it: only engine=fish with listen and
        reply on may put a message in a group, so a rollback to dsh/shadow or a
        withdrawn switch stops a reply that is already half-way to the channel.
        Engine and switches come from the stored config merged over DEFAULTS, the
        same view config() reports, so DEFAULTS-only rows read the same way here.
        """
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time() if now is None else float(now)
            row = db.execute("SELECT * FROM reservations WHERE token=? AND state='active'", (token,)).fetchone()
            if not row: return False
            cfg = {**DEFAULTS, **json.loads(self._state(db, row['chat'])['config'])}
            return cfg['listen'] is True and cfg['reply'] is True

    def finish(self, token, sent, now=None, *, uncertain=False):
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM reservations WHERE token=? AND state='active'",(token,)).fetchone()
            if not row: return
            state = 'sent' if sent else 'uncertain' if uncertain else 'skipped'
            db.execute('UPDATE reservations SET state=?,finished=? WHERE token=?',(state,time.time() if now is None else float(now),token))
            db.execute('UPDATE groups SET handled=MAX(handled,?),unanswered=unanswered+? WHERE chat=?',(row['cursor'],int((sent or uncertain) and row['kind']=='active'),row['chat']))

    def clear(self, chat):
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE'); self._state(db,chat); self._retire(db,chat,'cleared')
            db.execute('DELETE FROM observations WHERE chat=?',(chat,)); db.execute('DELETE FROM summaries WHERE chat=?',(chat,))
            db.execute('UPDATE groups SET handled=cursor WHERE chat=?',(chat,))
            db.execute('INSERT OR REPLACE INTO clears VALUES(?,?)',(chat,time.time()))
        return {'ok':True}

    def cleared_after(self, chat, created):
        cleared = self.cleared_at(chat)
        return bool(cleared and cleared>=created)

    def cleared_at(self, chat):
        with database(self.path) as db:
            row=db.execute('SELECT created FROM clears WHERE chat=?',(chat,)).fetchone()
        return float(row['created']) if row else 0

    def _energy(self, db, chat, now, cfg):
        """Energy at this instant: the stored value refilled by the time since it moved.

        The refill is linear (energy_recover_per_min per minute) and capped at 1.0,
        which is what makes it a valve rather than a budget: a chat she has just
        spoken in is at 0.9 and recovers in a couple of minutes, while a burst that
        spends everything has to wait. The refilled value is written back with this
        read, so the next caller sees the same number this one decided on.
        """
        row = db.execute('SELECT energy,updated FROM group_energy WHERE chat=?', (chat,)).fetchone()
        if row is None:
            db.execute('INSERT OR REPLACE INTO group_energy VALUES(?,?,?)', (chat, 1.0, now))
            return 1.0
        stored = min(1.0, max(0.0, float(row['energy'] if row['energy'] is not None else 1.0)))
        span = max(0.0, now - float(row['updated'] or now))
        value = min(1.0, stored + span / 60.0 * float(cfg['energy_recover_per_min']))
        db.execute('UPDATE group_energy SET energy=?,updated=? WHERE chat=?', (value, now, chat))
        return value

    def energy(self, chat, now=None):
        now = time.time() if now is None else float(now)
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE'); state = self._state(db, chat)
            return self._energy(db, chat, now, {**DEFAULTS, **json.loads(state['config'])})

    def spend_energy(self, chat, amount, now=None):
        """Charge one reply. Never below zero, and only ever charged for a real send."""
        now = time.time() if now is None else float(now)
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE'); state = self._state(db, chat)
            cfg = {**DEFAULTS, **json.loads(state['config'])}
            left = max(0.0, self._energy(db, chat, now, cfg) - max(0.0, float(amount)))
            db.execute('UPDATE group_energy SET energy=?,updated=? WHERE chat=?', (left, now, chat))
            return left

    def replies_last_hour(self, chat, now=None):
        """How many active lines she put in this chat in the last hour (MaiBot's window).

        Read from the group's own outbound ledger (qq.db) instead of a counter of our
        own: only a line the channel really accepted is stored as 'sent', so a failed
        or unknown receipt never counts against her target. A missing or unreadable
        store answers 0, because a window that cannot be read must not invent silence.
        """
        now = time.time() if now is None else float(now)
        store = self.root / 'data/qq/qq.db'
        if not store.is_file(): return 0
        since = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 3600))
        try:
            conn = sqlite3.connect(store.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        except sqlite3.Error:
            return 0
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND direction='outbound' "
                               "AND status='sent' AND ts>=?", (chat, since)).fetchone()
            return int(row[0] or 0) if row else 0
        except sqlite3.Error:
            return 0
        finally:
            conn.close()
