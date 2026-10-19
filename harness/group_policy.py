"""Durable group listening and reply admission; no model calls or transport.

Refined from the isolated DeepSeek worker's GroupPolicy proposal. Store only a
bounded context, reserve before model work, and recheck before every send.
"""
from pathlib import Path
import json
import re
import time
import uuid
from storage import database

DEFAULTS = dict(listen=False, learn=False, reply=False, reply_limits=True, mode='familiar', debounce_s=4,
    active_cooldown_s=60, max_active_hour=6, max_total_min=6,
    max_unanswered_active=2, quiet_start='', quiet_end='')
RESERVATION_TTL_S = 180


class GroupPolicy:
    def __init__(self, root, *, path=None):
        self.path = Path(path) if path else Path(root) / 'data/social/group-policy.db'
        with database(self.path) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS groups(chat TEXT PRIMARY KEY,config TEXT,cursor INTEGER DEFAULT 0,handled INTEGER DEFAULT 0,unanswered INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS observations(chat TEXT,seq INTEGER,id TEXT,text TEXT,data TEXT,created REAL,PRIMARY KEY(chat,seq));
            CREATE TABLE IF NOT EXISTS dedup(chat TEXT,id TEXT,created REAL,PRIMARY KEY(chat,id));
            CREATE TABLE IF NOT EXISTS reservations(token TEXT PRIMARY KEY,chat TEXT,cursor INTEGER,kind TEXT,state TEXT,created REAL,finished REAL DEFAULT 0,UNIQUE(chat,cursor));
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_group ON reservations(chat) WHERE state='active';
            CREATE TABLE IF NOT EXISTS summaries(chat TEXT PRIMARY KEY,cursor INTEGER,text TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS clears(chat TEXT PRIMARY KEY,created REAL);
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
            return {**DEFAULTS, **json.loads(self._state(db, chat)['config'])}

    def configure(self, chat, updates):
        if not isinstance(updates, dict) or set(updates) - set(DEFAULTS):
            raise ValueError('未知的群设置项')
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            cfg = {**DEFAULTS, **json.loads(self._state(db, chat)['config']), **updates}
            for k in ('listen', 'learn', 'reply', 'reply_limits'):
                if type(cfg[k]) is not bool: raise ValueError(k + ' 需要开关值')
            for k, lo, hi in (('debounce_s',0,60), ('active_cooldown_s',0,86400), ('max_active_hour',0,120), ('max_total_min',1,60), ('max_unanswered_active',0,10)):
                if type(cfg[k]) is not int or not lo <= cfg[k] <= hi: raise ValueError(k + ' 超出范围')
            if cfg['mode'] not in ('familiar', 'mention'): raise ValueError('未知参与方式')
            for k in ('quiet_start', 'quiet_end'):
                if not isinstance(cfg[k], str) or (cfg[k] and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', cfg[k])): raise ValueError('静默时间需为 HH:MM')
            if bool(cfg['quiet_start']) != bool(cfg['quiet_end']): raise ValueError('静默开始和结束需同时填写或留空')
            if not cfg['listen']: cfg['reply'] = False
            db.execute('UPDATE groups SET config=? WHERE chat=?', (json.dumps(cfg), chat))
            if not cfg['listen'] or not cfg['reply']:
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
            cursor = state['cursor'] + 1
            data = {'message_id':mid, 'sender':{'id':str(sender.get('id',''))[:40],'name':str(sender.get('name') or sender.get('display_name') or '')[:80]},
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

    def _candidate(self, db, chat, now, token=''):
        state = self._state(db, chat); cfg = {**DEFAULTS,**json.loads(state['config'])}; cursor = state['cursor']
        def result(action,reason,kind=None): return {'action':action,'reason':reason,'kind':kind,'cursor':cursor,'snapshot':self._snapshot(db,chat) if action=='candidate' else None}
        if not cfg['listen'] or not cfg['reply']: return result('silent','旁听或发言未开启')
        self._retire(db,chat,'expired',before=now-RESERVATION_TTL_S)
        if cursor <= self._state(db,chat)['handled']: return result('silent','没有新消息')
        if db.execute("SELECT 1 FROM reservations WHERE chat=? AND state='active' AND token<>?",(chat,token)).fetchone(): return result('silent','正在处理候选')
        obs = db.execute('SELECT data,created FROM observations WHERE chat=? AND seq>? ORDER BY seq',(chat,state['handled'])).fetchall()
        if not obs: return result('silent','没有上下文')
        if now-obs[-1]['created'] > 180 or now < obs[-1]['created']: return result('silent','上下文已过时或时钟变化')
        kind = 'direct' if any(json.loads(row['data'])['direct'] for row in obs) else 'active'
        counts = db.execute("SELECT kind,finished FROM reservations WHERE chat=? AND state IN ('sent','uncertain') AND finished>?",(chat,now-3600)).fetchall()
        if cfg['reply_limits'] and sum(r['finished']>now-60 for r in counts) >= cfg['max_total_min']: return result('wait','全部回复达到每分钟上限',kind)
        if kind=='active':
            if cfg['mode']=='mention': return result('silent','当前仅回应提及',kind)
            if now-obs[-1]['created'] < cfg['debounce_s']: return result('wait','正在合并连续消息',kind)
            if self._quiet(cfg,now): return result('wait','主动静默时段',kind)
            if cfg['reply_limits'] and state['unanswered'] >= cfg['max_unanswered_active']: return result('silent','无人接续，继续旁听',kind)
            active = [r for r in counts if r['kind']=='active']
            if cfg['reply_limits'] and len(active) >= cfg['max_active_hour']: return result('wait','主动回复达到每小时上限',kind)
            if active and now-max(r['finished'] for r in active) < cfg['active_cooldown_s']: return result('wait','主动回复冷却中',kind)
        return result('candidate','提及回应' if kind=='direct' else '可以判断是否值得插话',kind)

    def candidate(self, chat, now=None):
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE'); return self._candidate(db,chat,time.time() if now is None else float(now))

    def reserve(self, chat, cursor, kind, now=None):
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE'); now = time.time() if now is None else float(now)
            candidate = self._candidate(db,chat,now)
            if candidate['action']!='candidate' or candidate['cursor']!=cursor or candidate['kind']!=kind: return ''
            if db.execute('SELECT 1 FROM reservations WHERE chat=? AND cursor=?',(chat,cursor)).fetchone(): return ''
            token = uuid.uuid4().hex
            db.execute('INSERT INTO reservations(token,chat,cursor,kind,state,created) VALUES(?,?,?,?,?,?)',(token,chat,cursor,kind,'active',now)); return token

    def valid(self, token, now=None):
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE'); now = time.time() if now is None else float(now)
            row = db.execute("SELECT * FROM reservations WHERE token=? AND state='active'",(token,)).fetchone()
            if not row or now-row['created']>=180 or now<row['created']: return False
            c = self._candidate(db,row['chat'],now,token)
            return c['action']=='candidate' and c['cursor']==row['cursor'] and c['kind']==row['kind']

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
