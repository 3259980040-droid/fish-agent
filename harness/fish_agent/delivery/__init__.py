"""Delivery: hand what she decided to say to its real target and keep the receipt.

A queue entry is not a delivery and a submitted message is not a confirmation. This
module writes the client's outbound records, asks the QQ channel adapter to send,
and stamps the common experience with the real send time and outcome. It never
claims a success the transport did not report, and it never resends an unknown
result.
"""
from __future__ import annotations
from locations import project_path

from pathlib import Path


class DeliveryModule:
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = Path(runtime.root)

    def _bubbles(self, answer, previewed=()):
        from chat_output import decode_reply
        if not answer:
            raise ValueError('模型没有返回聊天内容')
        return decode_reply(self.root, answer, previewed=previewed)

    def submit_desktop(self, run, answer, *, message_id, cancelled=None):
        """Write this turn's answer into its client conversation, at one commit point."""
        from experience import delivery as stamp
        root = self.root
        client = self.runtime.client
        conversation = str(run['conversation'])
        message = str(message_id)
        bubbles = self._bubbles(answer)
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('本地已取消等待；晚到结果未保存为成功回复')
        from ..settings.tools import ToolSettings
        ToolSettings(root).require_delivery('desktop',self.runtime.tools.delivery_sources(run['id']))
        # Sending and commit are one boundary: once this is written the turn is delivered.
        stamp(root, 'desktop:' + message, sending=True)
        client.commit_reply(conversation, message, bubbles)
        stamp(root, 'desktop:' + message, outcome='sent')
        return {'bubbles': len(bubbles), 'state': 'sent', 'sent': True}

    def submit_qq(self, run, answer, *, message_id, previewed=(), cancelled=None):
        """Hand the decided bubbles to the QQ channel adapter and keep its real receipt.

        The QQ process owns the websocket, pacing and receipt; this module owns what
        the turn decided to say. A queue entry in that process is not a delivery, and
        an unknown receipt is recorded as unknown - it is never resent here.
        """
        import requests
        from governance import Governance
        root = self.root
        message = str(message_id)
        owner = str(Governance(root, 'qq').owner_id())
        if not owner:
            raise ValueError('尚未配置主人账号，无法投递私聊')
        bubbles = self._bubbles(answer, previewed)
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('本地已取消等待；晚到结果未发送')
        client = self.runtime.client_qq
        try:
            receipt = client.deliver(
                target='private:' + owner, bubbles=bubbles, key='qq-agent:' + str(run['id']),
                turn=str(run['id']), message_id=message,
                reference=('self:' + str(run['id'])) if not message or message == '0' else '',
                source_tools=self.runtime.tools.delivery_sources(run['id']))
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

    def submit_group(self, run, answer, *, chat, reply_to='', message_id='0', previewed=(),
                     direct=False, cancelled=None):
        """Hand the decided bubbles to the group's channel adapter and keep its receipt.

        The group's own settings (listen/reply/dive/quiet/limits) and the real send
        stay in the QQ process; the main execution only decided what to say.
        """
        import requests
        root = self.root
        bubbles = self._bubbles(answer, previewed)
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('本地已取消等待；晚到结果未发送')
        client = self.runtime.client_qq
        reference = ('qq:' + str(message_id)) if message_id and str(message_id) != '0' else ('self:' + str(run['id']))
        try:
            receipt = client.deliver(
                target=str(chat), bubbles=bubbles, key='group-fish:' + str(run['id']) + ':' + str(reply_to),
                turn=str(run['id']), message_id=str(message_id), reply_to=str(reply_to or ''),
                reference=reference, direct=bool(direct),source_tools=self.runtime.tools.delivery_sources(run['id']))
        except (requests.Timeout, requests.ConnectionError) as exc:
            return {'bubbles': len(bubbles), 'state': 'uncertain', 'sent': False,
                    'error': str(exc)[:300], 'unknown': True}
        except Exception as exc:
            return {'bubbles': len(bubbles), 'state': 'failed', 'sent': False,
                    'error': str(exc)[:300], 'unknown': False}
        return {'bubbles': len(bubbles), 'state': str(receipt.get('state') or ''),
                'sent': bool(receipt.get('sent')), 'receipt': receipt.get('receipt') or {},
                'error': str(receipt.get('error') or ''), 'unknown': str(receipt.get('state') or '') == 'uncertain'}

    def submit_plan(self, run, answer, *, schedule_id, slot, target, purpose, cancelled=None):
        """Deliver a due occurrence's output on the plan's own target and keep the receipt."""
        from bot_control import BotControl
        from experience import delivery as stamp
        bubbles = self._bubbles(answer)
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('后台正在关闭，未继续发送本次结果')
        reference = 'wake:%s:%s' % (schedule_id, slot)
        stamp(self.root, reference, sending=True)
        from ..settings.tools import ToolSettings
        ToolSettings(self.root).require_delivery(target,self.runtime.tools.delivery_sources(run['id']))
        result = BotControl(self.root).deliver_bubbles(
            target, bubbles, 'sched:%s:%s' % (schedule_id, slot),
            source='fish-autonomous' if str(purpose) == 'autonomy' else 'owner-reminder',
            scheduled=str(purpose) == 'reminder',source_tools=self.runtime.tools.delivery_sources(run['id']),
            context='owner:schedule' if str(purpose) == 'reminder' else 'fish:autonomous')
        receipt = result.get('receipt') or {}
        error = receipt.get('error') or ''
        if isinstance(error, dict):
            error = str(error.get('message') or error.get('code') or '')
        stamp(self.root, reference, outcome=str(result.get('state') or '').lower(), error=str(error))
        return {'state': result.get('state'), 'target': target, 'sent': result.get('state') == 'SUCCEEDED',
                'unknown': result.get('state') == 'UNKNOWN', 'error': str(error),
                'receipt': {'id': result.get('request_id', ''), 'message_id': str((receipt.get('payload') or {}).get('message_id') or '')}}

    def submit_self(self, run, answer, *, previewed=(), cancelled=None):
        """Deliver a clock or follow-up turn's own decision to the place it thought about."""
        from experience import delivery as stamp
        source = str(run['source'])
        place = str(run.get('place') or '')
        bubbles = self._bubbles(answer, previewed)
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError('本地已取消等待；晚到结果未发送')
        if source == 'qq-group':
            return self.submit_group(run, answer, chat=place, reply_to='', message_id='0',
                                     previewed=previewed, cancelled=cancelled)
        if source == 'qq-private':
            return self.submit_qq(run, answer, message_id='0', previewed=previewed, cancelled=cancelled)
        if source == 'desktop':
            client = self.runtime.client
            reference = 'self:' + str(run['id'])
            from ..settings.tools import ToolSettings
            ToolSettings(self.root).require_delivery('desktop',self.runtime.tools.delivery_sources(run['id']))
            stamp(self.root, reference, sending=True)
            client.commit_reply(str(run['conversation']), None, bubbles)
            stamp(self.root, reference, outcome='sent')
            return {'bubbles': len(bubbles), 'state': 'sent', 'sent': True}
        raise ValueError('自主回合没有可投递的场所：' + (source or '(空)'))

    def receipt(self, reference):
        """The real send/receipt times of one turn, or empty fields when absent."""
        import sqlite3
        path = project_path(self.root, 'data/social/experience.db')
        if not path.is_file():
            return {'reference': reference, 'sending': None, 'receipt': None, 'outcome': ''}
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT sending,receipt,outcome FROM experience_turns WHERE reference=? '
                             'ORDER BY rowid DESC LIMIT 1', (str(reference),)).fetchone()
        if row is None:
            return {'reference': reference, 'sending': None, 'receipt': None, 'outcome': ''}
        return {'reference': reference, 'sending': row[0], 'receipt': row[1], 'outcome': str(row[2] or '')}
