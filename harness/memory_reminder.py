"""Existing memory reminder opt-in, now a discovery adapter to Fish's single queue."""
import os
import time
from datetime import datetime, timedelta, timezone

from storage import database, role_dir

SWITCH = 'FISH_MEMORY_REMINDER'
ON_VALUES = {'1', 'on', 'true', 'yes'}


def enabled():
    return str(os.environ.get(SWITCH, '')).strip().lower() in ON_VALUES


def discover(control):
    """Memory-derived dates are opportunities, never invented owner commissions."""
    if not enabled():
        return
    from memory_store import MemoryStore
    store = MemoryStore(control.root, 'qq')
    now = time.time()
    path = role_dir(control.root, 'qq') / 'memory.db'
    with database(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS memory_reminders(id INTEGER PRIMARY KEY AUTOINCREMENT,memory_id TEXT NOT NULL,due REAL NOT NULL,state TEXT NOT NULL DEFAULT \'pending\',receipt TEXT DEFAULT \'\',attempts INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(memory_id,due))')
    for row in store.due_events('owner', now=now, limit=50, unrecorded=True):
        key, due = str(row['id']), float(row['due'])
        with database(path) as db:
            previous = db.execute('SELECT 1 FROM memory_reminders WHERE memory_id=? AND due=?', (key, due)).fetchone()
        if previous:
            continue
        configuration_key = 'memory-event:' + key + ':' + str(due)
        with database(control.path) as db:
            delegated = db.execute('SELECT id FROM scheduled WHERE configuration_key=?', (configuration_key,)).fetchone()
        if delegated:
            sid, state = delegated['id'], 'delegated'
        elif now - due > 48 * 3600:
            sid, state = '', 'skipped'
        else:
            when = datetime.now(timezone(timedelta(hours=8))) + timedelta(seconds=1)
            plan = control.create({'target': 'owner', 'purpose': 'autonomy', 'title': '记忆到期线索',
                'configuration_key': configuration_key,
                'prompt': '记忆里这件事到时间了：' + row['text'] + '。这是待核实的记忆线索，结合共同经历决定要不要提，不代表主人刚刚委托了提醒。',
                'at': {'date': when.date().isoformat(), 'time': when.strftime('%H:%M:%S'), 'time_zone': 'Asia/Shanghai'}})
            sid, state = plan['id'], 'delegated'
        with database(path) as db:
            db.execute('INSERT OR IGNORE INTO memory_reminders(memory_id,due,state,receipt,attempts,created,updated) VALUES(?,?,?,?,0,?,?)', (key, due, state, sid, now, now))

