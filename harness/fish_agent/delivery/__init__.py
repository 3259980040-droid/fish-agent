"""Delivery: hand what she decided to say to its real target and keep the receipt.

A queue entry is not a delivery and a submitted message is not a confirmation. This
module writes the client's outbound records and stamps the common experience with
the real send time and outcome; it never claims a success the transport did not
report, and it never resends an unknown result.
"""
from __future__ import annotations

from pathlib import Path


class DeliveryModule:
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = Path(runtime.root)

    def submit_desktop(self, run, inputs, answer, *, cancelled=None):
        """Write this turn's answer into its client conversation, at one commit point."""
        from chat_output import decode_reply
        from experience import delivery as stamp
        root = self.root
        client = self.runtime.client
        conversation = str(run['conversation'])
        message = str(inputs[-1]['message'])
        bubbles = decode_reply(root, answer) if answer else []
        if not bubbles:
            raise ValueError('模型没有返回聊天内容')
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('本地已取消等待；晚到结果未保存为成功回复')
        # Sending and commit are one boundary: once this is written the turn is delivered.
        stamp(root, 'desktop:' + message, sending=True)
        client.commit_reply(conversation, message, bubbles)
        stamp(root, 'desktop:' + message, outcome='sent')
        return {'bubbles': len(bubbles)}

    def submit_qq(self, run, inputs, answer, *, previewed=(), cancelled=None):
        """Hand the decided bubbles to the QQ channel adapter and keep its real receipt.

        The QQ process owns the websocket, pacing and receipt; this module owns what
        the turn decided to say. A queue entry in that process is not a delivery, and
        an unknown receipt is recorded as unknown - it is never resent here.
        """
        import requests
        from chat_output import decode_reply
        from governance import Governance
        root = self.root
        message = str(inputs[-1]['message'])
        owner = str(Governance(root, 'qq').owner_id())
        if not owner:
            raise ValueError('尚未配置主人账号，无法投递私聊')
        bubbles = decode_reply(root, answer, previewed=previewed) if answer else []
        if not bubbles:
            raise ValueError('模型没有返回聊天内容')
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('本地已取消等待；晚到结果未发送')
        client = self.runtime.client_qq
        try:
            receipt = client.deliver(
                target='private:' + owner, bubbles=bubbles, key='qq-agent:' + str(run['id']),
                turn=str(run['id']), message_id=message)
        except (requests.Timeout, requests.ConnectionError) as exc:
            # The request may have reached the channel: unknown, not failed, not resent.
            state = 'uncertain'
            client.status(message, state, str(exc), turn=run['id'])
            client.finish_turn(run['id'], 'send_failed', str(exc))
            return {'bubbles': len(bubbles), 'state': state, 'sent': False,
                    'error': str(exc)[:300], 'unknown': True}
        except Exception as exc:
            client.status(message, 'failed', str(exc), turn=run['id'])
            client.finish_turn(run['id'], 'send_failed', str(exc))
            return {'bubbles': len(bubbles), 'state': 'failed', 'sent': False,
                    'error': str(exc)[:300], 'unknown': False}
        return {'bubbles': len(bubbles), 'state': str(receipt.get('state') or ''),
                'sent': bool(receipt.get('sent')), 'receipt': receipt.get('receipt') or {},
                'error': str(receipt.get('error') or ''), 'unknown': str(receipt.get('state') or '') == 'uncertain'}

    def receipt(self, reference):
        """The real send/receipt times of one turn, or empty fields when absent."""
        import sqlite3
        path = self.root / 'data/social/experience.db'
        if not path.is_file():
            return {'reference': reference, 'sending': None, 'receipt': None, 'outcome': ''}
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT sending,receipt,outcome FROM experience_turns WHERE reference=? '
                             'ORDER BY rowid DESC LIMIT 1', (str(reference),)).fetchone()
        if row is None:
            return {'reference': reference, 'sending': None, 'receipt': None, 'outcome': ''}
        return {'reference': reference, 'sending': row[0], 'receipt': row[1], 'outcome': str(row[2] or '')}
