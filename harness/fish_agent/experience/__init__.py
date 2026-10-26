"""Common experience: record a standard input, prepare a turn, record its result.

The Interface is intentionally small. Callers hand in facts (which original record,
where it came from, what the turn actually used) and get back references and model
materials. They never read another module's tables to assemble a prompt.
"""
from __future__ import annotations
from locations import project_path

import json
from pathlib import Path

from . import context
from .batches import BatchLog


class ExperienceModule:
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = Path(runtime.root)
        self.batches = BatchLog(self.root)

    def record(self, *, reference, source, conversation='', message='', place='', text='', payload=None,
               kind='message'):
        """Register one received event in the unified order; a repeat is not counted twice."""
        event = self.runtime.state.record_input(
            reference=reference, kind=kind, source=source, conversation=conversation,
            message=message, place=place, text=text, payload=payload)
        self.register_files(event)
        return event

    def register_files(self, event):
        """Register each received file in its place's fixed 3-second window.

        The registration uses the input's own reception time, so a slow download can
        never move a file into a later batch. A text-only input registers nothing.
        """
        from .batches import file_segments, resource_of
        segments = self._segments(event)
        files = file_segments(segments)
        if not files:
            return []
        batch_place = '%s:%s' % (event.get('source') or '', event.get('conversation') or '')
        ids = []
        for index, segment in enumerate(files):
            ids.append(self.batches.register(place=batch_place, reference=event['reference'],
                                             resource=resource_of(segment) + '#' + str(index),
                                             received=float(event['received'])))
        return ids

    def _segments(self, event):
        from .batches import load_segments
        try:
            payload = json.loads(event.get('payload') or '{}')
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get('segments'), list):
            return [item for item in payload['segments'] if isinstance(item, dict)]
        return load_segments(self.root, event.get('reference') or '')

    def prepare(self, run, inputs, cancelled=None):
        """Assemble the materials of one claimed turn, with their real source references."""
        if str(run.get('kind') or '') in ('timer','followup'):
            prepared=context.self_wake_materials(self.runtime,run,inputs,cancelled)
        elif str(run.get('kind') or '')=='plan':
            prepared=context.plan_materials(self.runtime,run,inputs,cancelled)
        elif run['source']=='desktop':
            prepared=context.desktop_materials(self.runtime,run,inputs,cancelled)
        elif run['source']=='qq-private':
            prepared=context.qq_private_materials(self.runtime,run,inputs,cancelled)
        elif run['source']=='qq-group':
            prepared=context.group_materials(self.runtime,run,inputs,cancelled)
        else:
            raise ValueError('未知输入来源，未生成内容：'+str(run['source']))
        result=self.runtime.tools.result_material(run)
        if result:prepared['current']=[*prepared['current'],{'type':'text','text':result}]
        return prepared

    def record_result(self, run, status, error=''):
        """Record what the turn actually covered and what happened to each input."""
        rows = self.runtime.state.inputs_for(run['id'])
        considered = status in ('complete', 'silent')
        self.runtime.state.finish_inputs([row['reference'] for row in rows],
                                         considered=considered, cancelled=status == 'cancelled')
        if run['source'] in ('qq-private', 'qq-group'):
            client = self.runtime.client_qq
            for row in rows:
                message = str(row['message'])
                if status == 'silent':
                    client.status(message, 'considered', turn=run['id'])
                    if run['source'] == 'qq-private':
                        client.finish_turn(run['id'], 'silent')
                elif status == 'failed':
                    client.status(message, 'failed', str(error), turn=run['id'])
                    if run['source'] == 'qq-private':
                        client.finish_turn(run['id'], 'failed', str(error))
                elif status == 'cancelled':
                    client.status(message, 'cancelled', cancelled=True, turn=run['id'])
                    if run['source'] == 'qq-private':
                        client.finish_turn(run['id'], 'cancelled')
            return
        if run['source'] != 'desktop':
            return
        client = self.runtime.client
        for row in rows:
            message = str(row['message'])
            if status == 'silent':
                client.status(message, 'considered', '')
            elif status == 'failed':
                client.status(message, 'failed', str(error))
            elif status == 'cancelled':
                client.status(message, 'cancelled', '')

    def read(self, *, conversation=None, limit=50):
        """The inputs this Fish has actually recorded, newest first, with their state."""
        limit = max(1, min(200, int(limit)))
        from storage import database
        with database(project_path(self.root, 'data/social/experience.db')) as db:
            if conversation is None:
                rows = db.execute('SELECT * FROM agent_inputs ORDER BY seq DESC LIMIT ?', (limit,)).fetchall()
            else:
                rows = db.execute('SELECT * FROM agent_inputs WHERE conversation=? ORDER BY seq DESC LIMIT ?',
                                  (str(conversation), limit)).fetchall()
        return [{'reference': row['reference'], 'source': row['source'], 'conversation': row['conversation'],
                 'message': row['message'], 'place': row['place'], 'received': row['received'],
                 'considered': bool(row['considered']), 'cancelled': bool(row['cancelled']),
                 'run': row['run'], 'payload': json.loads(row['payload'] or '{}')} for row in rows]
