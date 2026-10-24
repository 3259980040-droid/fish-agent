"""Display copies of real provider reasoning and tool facts; never ask a model to explain itself."""
import json
import time
import uuid
from pathlib import Path
from storage import database
from request_inspector import redact


class TurnTrace:
    def __init__(self,root,role,session,origin,reference='',child=False):
        self.path = Path(root)/'data/social/experience.db'
        self.id,self.reasoning,self.events = uuid.uuid4().hex,'',[]
        self.phase,self.last = 'preparing',0
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS turn_traces(id TEXT PRIMARY KEY,role TEXT,session TEXT,origin TEXT,reference TEXT,child INTEGER,phase TEXT,reasoning TEXT,events TEXT,updated REAL,error TEXT)')
            db.execute('INSERT INTO turn_traces VALUES(?,?,?,?,?,?,?,?,?,?,?)',(self.id,role,session,origin,reference,int(child),self.phase,'','[]',time.time(),''))

    def event(self,kind,value):
        stamp = time.time()
        if kind=='reasoning':
            self.reasoning += str(value)
            self.phase = 'thinking'
        else:
            self.events.append({'kind':kind,'at':stamp,'value':value})
            if kind=='phase': self.phase = value
            elif kind=='tool_start': self.phase = 'tool'
        if kind!='reasoning' or stamp-self.last>=.35: self.flush()

    def flush(self,error=''):
        with database(self.path) as db:
            db.execute('UPDATE turn_traces SET phase=?,reasoning=?,events=?,updated=?,error=? WHERE id=?',
                (self.phase,redact(self.reasoning),redact(json.dumps(self.events,ensure_ascii=False,default=str)),time.time(),redact(str(error)),self.id))
        self.last = time.time()

    def finish(self,phase,error=''):
        self.phase = phase
        self.flush(error)


def latest(root,role='qq'):
    path = Path(root)/'data/social/experience.db'
    if not path.is_file(): return None
    import sqlite3
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='turn_traces'").fetchone(): return None
        row = db.execute('SELECT * FROM turn_traces WHERE role=? AND child=0 ORDER BY updated DESC LIMIT 1',(role,)).fetchone()
        if row is None: return None
        result = dict(row); result['events'] = json.loads(result['events'])
        if result['reference'] and db.execute("SELECT 1 FROM sqlite_master WHERE name='experience_turns'").fetchone():
            delivery = db.execute('SELECT arrival,started,first_output,sending,receipt,outcome,error FROM experience_turns WHERE reference=? ORDER BY rowid DESC LIMIT 1',(result['reference'],)).fetchone()
            result['timing'] = dict(delivery) if delivery else None
        return result
