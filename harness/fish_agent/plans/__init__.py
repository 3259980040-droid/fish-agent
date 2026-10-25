"""Plans and their occurrences: the same records the task page manages.

The time wheel only detects a due occurrence and submits it; the one main execution
runs the turn and this module records the real outcome back on that same occurrence.
A paused plan still runs one explicit manual occurrence, and never changes its own
period to do it.
"""
from __future__ import annotations

import json
from pathlib import Path


class PlansModule:
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = Path(runtime.root)

    def store(self):
        from bot_control import BotControl
        return BotControl(self.root).plans

    def detect(self):
        """The time wheel's detect step; it never runs a turn itself."""
        return self.store().detect()

    def dispatch(self):
        """Submit every pending occurrence to the one main execution.

        A submission is deduplicated by its stable occurrence reference, so a
        repeated scan cannot run the same due slot twice.
        """
        submitted = []
        plans = self.store()
        for item in plans.pending():
            schedule_id, slot = str(item.get('id') or ''), str(item.get('slot') or '')
            if not schedule_id or not slot:
                continue
            try:
                row = plans.get(schedule_id)
            except ValueError:
                continue
            target = str(row.get('target') or '')
            if not target:
                continue
            from qq_store import QQStore
            conversation = QQStore(self.root).get_or_create_conversation(target, 'qq')['id']
            result = self.runtime.execution.submit(
                source='plan', kind='plan', conversation=conversation,
                reference='plan:%s:%s' % (schedule_id, slot),
                reason=str(row.get('title') or '计划到期'), place=target,
                payload={'schedule_id': schedule_id, 'slot': slot, 'target': target,
                         'purpose': str(row.get('purpose') or 'reminder')})
            submitted.append({'schedule_id': schedule_id, 'slot': slot,
                              'execution': result.get('execution', ''), 'status': result.get('status', '')})
        return submitted

    def record_result(self, run, status, *, error='', delivered=None):
        """Close the occurrence with what the turn actually produced."""
        payload = self._payload(run)
        schedule_id, slot = payload.get('schedule_id', ''), payload.get('slot', '')
        if not schedule_id or not slot:
            raise ValueError('计划回合缺少实例引用')
        plans = self.store()
        try:
            row = plans.get(schedule_id)
        except ValueError:
            return None
        # A silent turn is a completed execution: close() then says what it means for
        # this purpose (autonomy may act not at all; a reminder without a receipt is a
        # failure), instead of being reported as "skipped before running".
        result = plans.close(schedule_id, slot, row, error=str(error or ''), skipped='')
        try:
            plans._unbind_occurrence(str(row.get('session') or ''))
        except Exception:  # noqa: BLE001 - the binding is advisory; its loss cannot undo the result
            pass
        return result

    def _payload(self, run):
        rows = self.runtime.state.inputs_for(run['id'])
        if rows:
            try:
                return json.loads(rows[-1].get('payload') or '{}')
            except (TypeError, ValueError):
                return {}
        return {}

    def list(self):
        return self.store().rows()
