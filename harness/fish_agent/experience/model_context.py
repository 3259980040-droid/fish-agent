"""The instance's current provider conversation, in its existing agent_state.

Original messages, trace display and archives keep their own jobs. This operational
projection preserves the exact model/tool protocol between wakes, including across
process restarts; only new facts append. Compaction replaces an older balanced range.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

from locations import project_path
from ..execution.state import AgentState


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _version(message):
    """A record becoming considered, or moving to recall, is not a new original."""
    content = message.get('content', '')
    if isinstance(content, str) and content.startswith(('【历史消息】', '【本轮回查原文】')):
        _, head, body = content.partition('】')
        header, newline, text = body.partition('\n')
        data = json.loads(header)
        data.pop('already_considered', None)
        content = _json(data) + newline + text
    return hashlib.sha256(_json(content).encode('utf-8')).hexdigest()


class ModelContext:
    def __init__(self, root, role='qq'):
        self.root, self.role = Path(root), role
        self.state = AgentState(root)
        self.key = 'model_context:' + role
        self.expected = self.state.get_state(self.key)
        self.value = json.loads(self.expected) if self.expected else {}
        self.metadata, self.updates = {}, {}
        self.valid = True
        self.execution = ''
        self.versions = dict(self.value.get('versions', {}))
        self.materials = dict(self.value.get('materials', {}))
        self.originals = dict(self.value.get('originals', {}))

    def retained(self):
        messages = self._images(deepcopy(self.value.get('messages', [])), restore=True)
        for message in messages:
            self._remember(message)
        return messages

    def _images(self, value, *, restore=False):
        if isinstance(value, list):
            return [self._images(item, restore=restore) for item in value]
        if not isinstance(value, dict):
            return value
        directory = project_path(self.root, 'data/qq/media/model-context')
        if restore and '_fish_model_image' in value:
            name = str(value['_fish_model_image'])
            if Path(name).name != name:
                raise ValueError('模型上下文的图片引用无效')
            raw = (directory / name).read_bytes()
            return value['prefix'] + ',' + base64.b64encode(raw).decode('ascii')
        result = {}
        for key, item in value.items():
            if not restore and key == 'url' and isinstance(item, str) and item.startswith('data:image/'):
                prefix, _, encoded = item.partition(',')
                raw = base64.b64decode(encoded, validate=True)
                name = hashlib.sha256(raw).hexdigest() + '.bin'
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / name
                if not path.exists():
                    path.write_bytes(raw)
                result[key] = {'_fish_model_image': name, 'prefix': prefix}
            else:
                result[key] = self._images(item, restore=restore)
        return result

    def _remember(self, message):
        self.metadata[id(message)] = (int(message.get('_fish_layer', 0)),
            list(message.get('_fish_refs', [])), message.get('_fish_bundle') or self.execution)

    def prepare(self, request, *, route, execution, original=None):
        self.execution = execution
        fresh = deepcopy(request['messages'])
        if self.value.get('route') != route:
            self.value = {'epoch': uuid.uuid4().hex, 'route': route, 'messages': [],
                          'versions': {}, 'materials': {}, 'originals': {}}
        self.value.setdefault('epoch', uuid.uuid4().hex)
        self.versions = dict(self.value.get('versions', {}))
        self.materials = dict(self.value.get('materials', {}))
        self.originals = dict(self.value.get('originals', {}))
        source = request.get('sources', {}).get('history') or {}
        if isinstance(source, dict):
            for node in source.get('surface', []):
                prefix = {'desktop-chat.db': 'desktop', 'qq.db': 'qq'}[node['source']]
                self.originals[prefix + ':' + str(node['id'])] = {
                    k: node[k] for k in ('source', 'conversation', 'id', 'place', 'time') if k in node}
        if original and original.get('reference'):
            self.originals[original['reference']] = {
                k: original[k] for k in ('source', 'conversation', 'id', 'place', 'time') if k in original}
        messages = self._images(deepcopy(self.value.get('messages', [])), restore=True)
        # An actual persona/style edit changes the one system projection. Normal
        # wakes reuse the old object content rather than rewriting its neighbours.
        if fresh and fresh[0].get('role') == 'system':
            if messages and messages[0].get('role') == 'system':
                messages[0] = fresh[0]
            else:
                messages.insert(0, fresh[0])
            fresh = fresh[1:]
        slots = {}
        for index, message in enumerate(fresh):
            refs = message.get('_fish_refs', [])
            current = index == len(fresh) - 1
            digest = _version(message)
            update = {'versions': {}}
            if current:
                update['versions'] = {ref: self.versions.get(ref) for ref in refs}
            elif refs:
                unseen = any(ref not in self.versions for ref in refs)
                changed = any(self.versions.get(ref) not in (None, digest) for ref in refs)
                # Keyword matches from a freshly rebuilt archive view are not new
                # originals. Reinjecting an unchanged compressed record on every
                # match would immediately undo compression and defeat the cache.
                # Explicit history tools still append their actual result below.
                if not (unseen or changed):
                    for ref in refs:
                        if self.versions.get(ref) is None:
                            self.versions[ref] = digest
                    continue
                update['versions'] = {ref: digest for ref in refs}
            else:
                shape = (message.get('role'), int(message.get('_fish_layer', 1)))
                slot = slots.get(shape, 0)
                slots[shape] = slot + 1
                key = '%s:%s:%s' % (*shape, slot)
                if self.materials.get(key) == digest:
                    continue
                update['material'] = (key, digest)
            messages.append(message)
            self.updates[id(message)] = update
        for message in messages:
            self._remember(message)
        return messages

    def save(self, messages, budget, *, connection=None, checkpoint_id=None):
        if not self.valid:
            return False
        alive = {id(message) for message in messages}
        self.metadata = {key: value for key, value in self.metadata.items() if key in alive}
        self.updates = {key: value for key, value in self.updates.items() if key in alive}
        nodes = []
        for message in messages:
            layer, refs, bundle = getattr(budget, 'materials', {}).get(id(message),
                self.metadata.get(id(message), (0, message.get('_fish_refs', []),
                                                message.get('_fish_bundle') or self.execution)))
            node = deepcopy(message)
            # Accepted messages are now the stable protocol. Compression, rather
            # than the next wake's optional-material filter, owns their retirement.
            node.update(_fish_layer=0, _fish_refs=list(refs), _fish_bundle=bundle)
            nodes.append(node)
            update = self.updates.get(id(message), {})
            self.versions.update(update.get('versions', {}))
            if update.get('material'):
                key, digest = update['material']
                self.materials[key] = digest
        value = {**self.value, 'messages': self._images(nodes), 'versions': self.versions,
                 'materials': self.materials, 'originals': self.originals,
                 'execution': self.execution}
        if checkpoint_id is not None:
            value['checkpoint_id'] = checkpoint_id
        encoded = _json(value)
        self.valid = self.state.compare_set_state(self.key, encoded, self.expected, connection=connection)
        if self.valid:
            self.expected, self.value = encoded, value
        return self.valid

    def compact(self, messages, budget, tools, options, gateway, *, cancelled=None, force=False, trace=None):
        if not self.valid:
            return None
        from .compaction import fold_model_context
        return fold_model_context(self, messages, budget, tools, options, gateway,
                                  cancelled=cancelled, force=force, trace=trace)

    @classmethod
    def forget(cls, root, references, role='qq'):
        context = cls(root, role)
        if references:
            # An in-flight first preparation may have read deleted originals that
            # have not reached versions yet. Invalidate its captured epoch too.
            context.state.set_state(context.key, _json({'epoch': uuid.uuid4().hex}))

