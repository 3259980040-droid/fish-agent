"""The QQ side of the same Agent: its own records plus the channel transport.

The QQ receive/send process stays independent. This adapter is how the one main
execution reads a stored private message, records its state, and asks that process
to perform a real send. The transport itself (websocket, pacing, receipts) stays in
the QQ process; nothing here holds or creates a model execution.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import requests

# The QQ process owns one local control server; the host calls it for a real send.
DEFAULT_CONTROL_PORT = 3102


def control_port():
    try:
        return int(os.environ.get('FISH_QQ_CONTROL_PORT', DEFAULT_CONTROL_PORT))
    except (TypeError, ValueError):
        return DEFAULT_CONTROL_PORT


class QQAdapter:
    def __init__(self, root):
        self.root = Path(root)
        self._store = None

    def store(self):
        if self._store is None:
            from qq_store import QQStore
            self._store = QQStore(self.root)
        return self._store

    @property
    def path(self):
        return self.store().db_path

    def inbound(self, message):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM messages WHERE id=?', (int(message),)).fetchone()
        return dict(row) if row else None

    def status(self, message, status, error='', *, cancelled=False, turn=''):
        updates = {'status': status, 'error': str(error)[:500], 'cancelled': bool(cancelled)}
        if turn:
            updates['runtime_turn_id'] = str(turn)
        self.store().update_message(message, **updates)

    def update_segments(self, message, segments):
        self.store().update_message(message, segments=segments)

    def begin_turn(self, turn, conversation):
        """One row per real turn, id = the main execution's reference."""
        try:
            self.store().create_turn(str(turn), str(conversation), '', str(turn))
        except sqlite3.IntegrityError:
            pass

    def finish_turn(self, turn, status, error=''):
        self.store().finish_turn(str(turn), status, error=str(error)[:1000])

    def deliver(self, *, target, bubbles, key, turn, message_id, timeout=180):
        """Ask the QQ process to send already-decided bubbles; return its real receipt.

        The request itself being accepted is not a delivery: ``state`` says what the
        channel actually confirmed, and ``sent`` is that same fact in one field.
        """
        body = json.dumps({'target': target, 'bubbles': bubbles, 'key': key, 'turn': turn,
                           'message_id': message_id}, ensure_ascii=False).encode()
        from governance import control_token
        response = requests.post(
            f'http://127.0.0.1:{control_port()}/deliver', data=body,
            headers={'x-fish-token': control_token(self.root), 'Content-Type': 'application/json'},
            timeout=timeout, proxies={'http': None, 'https': None, 'all': None})
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError('QQ 通道未返回可读的投递回执（HTTP ' + str(response.status_code) + '）')
        if not isinstance(data, dict):
            raise RuntimeError('QQ 通道投递回执格式不正确')
        if not data.get('ok'):
            raise RuntimeError(str(data.get('error') or 'QQ 通道拒绝了这次投递'))
        return data
