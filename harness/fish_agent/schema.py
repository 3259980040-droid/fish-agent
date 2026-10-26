"""One shape for the agent's own state, created by the one assembly that owns it.

These tables live in the existing common-experience store (``experience.db``) so a
turn's claim, its inputs and its coverage commit against the same database the raw
records already point at. They add state that did not exist before: the unified
receive order, the one wake/run ledger, and the pending wake set. They do not copy
original messages; an input row keeps only the reference into the store that owns
the words.
"""
from locations import project_path
from pathlib import Path
from storage import database

INPUTS = """CREATE TABLE IF NOT EXISTS agent_inputs(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    reference TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'message',
    source TEXT NOT NULL,
    conversation TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    place TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    received REAL NOT NULL,
    run TEXT NOT NULL DEFAULT '',
    considered INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0)"""

RUNS = """CREATE TABLE IF NOT EXISTS agent_runs(
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    conversation TEXT NOT NULL DEFAULT '',
    request TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    error TEXT NOT NULL DEFAULT '',
    error_kind TEXT NOT NULL DEFAULT '',
    merged_into TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    inputs TEXT NOT NULL DEFAULT '[]')"""

STATE = """CREATE TABLE IF NOT EXISTS agent_state(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL)"""

BATCHES = """CREATE TABLE IF NOT EXISTS agent_batches(
    id TEXT PRIMARY KEY,
    place TEXT NOT NULL,
    anchor REAL NOT NULL,
    window_index INTEGER NOT NULL,
    opens REAL NOT NULL,
    closes REAL NOT NULL,
    state TEXT NOT NULL,
    run TEXT NOT NULL DEFAULT '')"""

BATCH_ITEMS = """CREATE TABLE IF NOT EXISTS agent_batch_items(
    batch TEXT NOT NULL,
    seq INTEGER NOT NULL,
    reference TEXT NOT NULL,
    resource TEXT NOT NULL DEFAULT '',
    received REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'registered',
    run TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(batch, reference, resource))"""

BATCH_PLACES = """CREATE TABLE IF NOT EXISTS agent_batch_places(
    place TEXT PRIMARY KEY,
    anchor REAL NOT NULL,
    last_index INTEGER NOT NULL)"""

WAKES = """CREATE TABLE IF NOT EXISTS agent_wakes(
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    claimed_run TEXT NOT NULL DEFAULT '')"""

TABLES = (INPUTS, RUNS, WAKES, STATE, BATCHES, BATCH_ITEMS, BATCH_PLACES)


def path(root) -> Path:
    return project_path(Path(root), 'data/social/experience.db')


def ensure(root) -> Path:
    target = path(root)
    with database(target) as db:
        for statement in TABLES:
            db.execute(statement)
        # Older agent_runs rows predate the trigger reason/place; add them in place.
        columns = {row[1] for row in db.execute('PRAGMA table_info(agent_runs)')}
        for name, decl in (('reason', "TEXT NOT NULL DEFAULT ''"), ('place', "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                db.execute('ALTER TABLE agent_runs ADD COLUMN ' + name + ' ' + decl)
        item_columns = {row[1] for row in db.execute('PRAGMA table_info(agent_batch_items)')}
        for name, decl in (('state', "TEXT NOT NULL DEFAULT 'registered'"), ('run', "TEXT NOT NULL DEFAULT ''")):
            if name not in item_columns:
                db.execute('ALTER TABLE agent_batch_items ADD COLUMN ' + name + ' ' + decl)
    return target
