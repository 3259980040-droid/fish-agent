"""Explicit host resources and instance-owned storage for the existing stores.

This module resolves locations only. It never substitutes pathlib behaviour, changes
process globals, copies source code, or creates a second database implementation.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import unquote, urlsplit

INITIAL_INSTANCE_ID = 'main'
_LOCK = threading.RLock()
_SHARED_DATA = {'host', 'instances', 'system', 'research', 'shared', 'integrations'}
_SHARED_CONFIG = {'agents.json', 'plugins', 'secrets', 'agents', 'global', 'runtime', 'harness.db', 'integration.json', 'network.json', 'multimodal.json',
                  'research-interests.json', 'research-autonomy.json', 'setup.json'}


def _json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


@dataclass(frozen=True)
class InstanceContext:
    host_root: Path
    instance_id: str
    directory: Path

    @property
    def root(self):
        """The instance storage context used by existing module constructors."""
        return self.directory

    @property
    def workspace(self):
        return self.directory / 'workspace'

    def path(self, *parts):
        return project_path(self, *parts)


def host_root(root):
    if isinstance(root, InstanceContext):
        return root.host_root
    path = Path(root).resolve()
    marker = path / 'instance.json'
    if marker.is_file():
        value = _json(marker, {})
        return Path(value['host_root']).resolve()
    return path


def _instance_id(root):
    if isinstance(root, InstanceContext):
        return root.instance_id
    marker = Path(root).resolve() / 'instance.json'
    return str(_json(marker, {}).get('id', INITIAL_INSTANCE_ID))


@contextlib.contextmanager
def _catalog_lock(host):
    """One maintenance writer, including simultaneous service/client startup."""
    path = host / 'data/host/instances.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _replace_path(value, moves):
    if isinstance(value, str):
        if value.startswith('file:'):
            decoded = unquote(urlsplit(value).path)
            if os.name == 'nt' and re.match(r'^/[A-Za-z]:', decoded):
                decoded = decoded[1:]
            mapped = _replace_path(decoded, moves)
            return Path(mapped).as_uri() if mapped != decoded else value
        normalized = value.replace('\\', '/')
        for old, new in moves:
            previous = old.replace('\\', '/').rstrip('/')
            if normalized.casefold() == previous.casefold():
                return new
            if normalized.casefold().startswith(previous.casefold() + '/'):
                return str(Path(new) / Path(normalized[len(previous) + 1:]))
        return value
    if isinstance(value, list):
        return [_replace_path(item, moves) for item in value]
    if isinstance(value, dict):
        return {key: _replace_path(item, moves) for key, item in value.items()}
    return value



def _replace_reference(value, moves, field=''):
    """Structured database records contain prose too; only path fields relocate."""
    fields = {'path', '_fish_path', '_fish_owner_local_path', 'file', 'filename',
              'directory', 'workspace', 'allowed_paths', 'local_path', 'source_path',
              'archive_path', 'output_path', 'db_path', 'original_path', 'stored_path',
              'home', 'note_path', 'preview_path', 'file_path', 'resource_path', 'uri', 'url'}
    if isinstance(value, dict):
        return {key: _replace_reference(item, moves, key) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_reference(item, moves, field) for item in value]
    return _replace_path(value, moves) if field in fields else value


def _rebase_references(directory, moves):
    """Only structured file references change; raw words/IDs/deletions stay facts."""
    path_columns = {'path', 'file', 'filename', 'directory', 'workspace', 'allowed_paths',
                    'local_path', 'source_path', 'archive_path', 'output_path', 'db_path',
                    'original_path', 'stored_path', 'home', 'note_path', 'preview_path',
                    'file_path', 'resource_path', 'config', 'segments', 'metadata',
                    'report', 'files', 'artifacts', 'body', 'result', 'payload'}
    changed = 0
    for file in directory.rglob('*'):
        if file.name == 'instance.json' or not file.is_file():
            continue
        relative = file.relative_to(directory)
        # Media, attachments, workspaces and resources are originals rather than
        # migration metadata. Never rewrite their bytes.
        if file.suffix == '.json' and (relative.parts[0] == 'config' or relative.parts[:1] == ('social',) and len(relative.parts) == 2):
            try:
                old = _json(file, {})
            except (ValueError, UnicodeError):
                continue
            value = _replace_path(old, moves)
            if value != old:
                _write(file, value); changed += 1
        elif file.suffix == '.db' and (len(relative.parts) <= 2 or 'spaces' in relative.parts and file.name in ('memory.db', 'qq.db', 'desktop-chat.db', 'experience.db')):
            with contextlib.closing(sqlite3.connect(file)) as db:
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                for table in tables:
                    quoted = '"' + table.replace('"', '""') + '"'
                    columns = [row[1] for row in db.execute('PRAGMA table_info(' + quoted + ')')
                               if row[1] in path_columns or table == 'chat_inputs' and row[1] == 'content']
                    for column in columns:
                        col = '"' + column.replace('"', '""') + '"'
                        try:
                            rows = db.execute('SELECT rowid,' + col + ' FROM ' + quoted + ' WHERE typeof(' + col + ")='text'").fetchall()
                        except sqlite3.OperationalError:  # WITHOUT ROWID / FTS virtual view
                            continue
                        for rowid, old in rows:
                            structured = column in {'config', 'segments', 'metadata', 'report', 'files', 'artifacts', 'body', 'result', 'payload', 'content'}
                            value = old if structured else _replace_path(old, moves)
                            if value == old:
                                try:
                                    parsed = json.loads(old)
                                except (ValueError, TypeError):
                                    continue
                                replaced = _replace_reference(parsed, moves)
                                value = json.dumps(replaced, ensure_ascii=False) if replaced != parsed else old
                            if value != old:
                                db.execute('UPDATE ' + quoted + ' SET ' + col + '=? WHERE rowid=?', (value, rowid)); changed += 1
                db.commit()
    return changed


def _migrate_initial(host, directory):
    journal = host / 'data/host/initial-migration.json'
    state = _json(journal, {'status': 'pending', 'moves': []})
    if state['status'] == 'complete':
        return
    pairs = []
    legacy_data = host / 'data'
    if legacy_data.exists():
        pairs += [(item, directory / item.name) for item in legacy_data.iterdir() if item.name not in _SHARED_DATA]
    config = host / 'harness'
    if config.exists():
        pairs += [(item, directory / 'config' / item.name) for item in config.iterdir()
                  if item.is_file() and item.suffix in ('.json', '.db') and item.name not in _SHARED_CONFIG]
        runtime = config / 'runtime'
        if runtime.exists():
            pairs += [(item, directory / 'config/runtime' / item.name) for item in runtime.glob('*.identities.json')]
    for name in ('workspace', 'outputs'):
        if (host / name).exists():
            pairs.append((host / name, directory / ('workspace' if name == 'workspace' else 'resources/outputs')))
    legacy_work = host / 'qq-work'
    if legacy_work.is_dir():
        # AGENTS.md is a tracked source template, not user work to relocate.
        pairs += [(item, directory / 'workspace/legacy-qq' / item.name)
                  for item in legacy_work.iterdir() if item.name != 'AGENTS.md']
    integration = _json(host / 'harness/integration.json', {})
    if 'qzone_enabled' in integration and not (directory / 'config/integration.json').exists():
        _write(directory / 'config/integration.json', {'qzone_enabled': integration['qzone_enabled']})
    # Persist the complete move intention before the first rename; interrupted
    # maintenance resumes the same mapping rather than forgetting a moved file.
    for source, target in pairs:
        record = {'from': str(source.resolve()), 'to': str(target.resolve())}
        if record not in state['moves']:
            state['moves'].append(record)
    state['status'] = 'moving'
    _write(journal, state)
    for record in state['moves']:
        source, target = Path(record['from']), Path(record['to'])
        if source.exists() and target.exists():
            raise RuntimeError('初始实例迁接源与目标同时存在，请检查迁接记录：' + str(target))
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
        elif not target.exists():
            raise RuntimeError('初始实例迁接文件不存在：' + str(source))
    moves = [(row['from'], row['to']) for row in state['moves']]
    state['references_updated'] = _rebase_references(directory, moves)
    state['status'] = 'complete'
    state['finished'] = time.time()
    _write(journal, state)


class InstanceCatalog:
    def __init__(self, root):
        self.host_root = host_root(root)
        self.path = self.host_root / 'data/host/instances.json'

    def initial(self):
        host = self.host_root
        with _catalog_lock(host):
            raw = _json(self.path, {})
            if raw.get('initial_instance_id'):
                return self.resolve(raw['initial_instance_id'])
            directory = host / 'data/instances' / INITIAL_INSTANCE_ID
            directory.mkdir(parents=True, exist_ok=True)
            _write(directory / 'instance.json', {'id': INITIAL_INSTANCE_ID, 'host_root': str(host)})
            _migrate_initial(host, directory)
            for name in ('config', 'social', 'qq', 'resources', 'workspace'):
                (directory / name).mkdir(exist_ok=True)
            raw = {'version': 1, 'initial_instance_id': INITIAL_INSTANCE_ID,
                   'instances': [{'id': INITIAL_INSTANCE_ID, 'name': 'Fish', 'initial': True,
                                  'created': time.time()}]}
            _write(self.path, raw)
            return InstanceContext(host, INITIAL_INSTANCE_ID, directory)

    def resolve(self, instance_id=None):
        if instance_id is None:
            return self.initial()
        value = str(instance_id)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
            raise ValueError('无效的Fish窗口编号')
        raw = _json(self.path, {})
        if not raw:
            self.initial(); raw = _json(self.path, {})
        if not any(row['id'] == value for row in raw.get('instances', [])):
            raise ValueError('Fish窗口不存在：' + value)
        return InstanceContext(self.host_root, value, self.host_root / 'data/instances' / value)

    def records(self):
        self.initial()
        return _json(self.path,{})['instances']

    def item(self,instance_id):
        context=self.resolve(instance_id)
        record=next(row for row in self.records() if row['id']==context.instance_id)
        from social_context import SocialContext, PersonaNotConfigured
        from governance import Governance
        from fish_agent.bootstrap import get_runtime
        reason=''
        try:
            persona=SocialContext(context.root).active_persona('qq')
        except PersonaNotConfigured:
            persona=None;reason='尚未配置人格，请填写或选择人格后启动'
        enabled=not Governance(context.root,'qq').config()['paused']
        runtime=get_runtime(context)
        if not reason and not enabled:reason='本窗口已暂停'
        if not reason and not runtime.started:reason='本窗口等待后台启动'
        return {**{k:v for k,v in record.items() if k!='running'},'enabled':enabled,
                'running':runtime.started,'persona_configured':persona is not None,
                'persona_name':persona['name'] if persona else '',
                'persona_id':persona['id'] if persona else '', 'status_reason':reason}

    def list(self):
        return {'initial_instance_id':self.initial().instance_id,
                'instances':[self.item(row['id']) for row in self.records()]}

    def presets(self):
        from social_context import SocialContext
        return {'presets':[{'instance_id':row['id'],'revision':p['id'],'name':p['name'],'body':p['body']}
                for row in self.records() for p in SocialContext(self.resolve(row['id']).root).personas('qq')]}

    def create(self,name,persona=None,running=None):
        import uuid
        title=' '.join(str(name or '').split())
        if not title or len(title)>80:raise ValueError('请填写窗口名称，最多80字')
        if running is not None and type(running) is not bool:raise ValueError('窗口运行状态必须为布尔值')
        from social_context import SocialContext
        if persona is not None:
            if not isinstance(persona,dict):raise ValueError('人格应为手写正文或明确预设')
            if 'revision' in persona:
                if not persona.get('instance_id'):raise ValueError('请选择明确的人格预设来源窗口')
                source=self.resolve(persona['instance_id'])
                original=SocialContext(source.root).persona(str(persona['revision']))
                if original['role']!='qq':raise ValueError('请选择Fish的人格预设')
                persona={'name':original['name'],'body':original['body']}
            if not str(persona.get('name') or '').strip() or not str(persona.get('body') or '').strip():
                raise ValueError('请填写人格名称和正文')
            if len(str(persona['name']).strip())>80 or len(str(persona['body']).strip())>40000:
                raise ValueError('人格名称最多80字，正文最多40000字')
        if running and persona is None:raise ValueError('尚未配置人格，不能启动这个窗口')
        initial=self.initial()
        instance_id='fish-'+uuid.uuid4().hex
        directory=self.host_root/'data/instances'/instance_id
        with _catalog_lock(self.host_root):
            directory.mkdir()
            _write(directory/'instance.json',{'id':instance_id,'host_root':str(self.host_root)})
            for part in ('config','social','qq','resources','workspace'):(directory/part).mkdir()
            raw=_json(self.path,{})
            raw['instances'].append({'id':instance_id,'name':title,'initial':False,'created':time.time()})
            _write(self.path,raw)
        from governance import Governance
        from storage import write_json
        from fish_agent.settings.tools import ToolSettings
        # Owner authorization is a host identity. A new persona does not revoke it.
        template=Governance(initial.root,'qq').config()
        template.pop('send_policy',None);template['paused_roles']={'qq':True,'research':True}
        template['paused']=False
        write_json(directory/'config/control.json',template)
        source_tools=ToolSettings(initial.root)
        from plugin_registry import PluginRegistry
        ToolSettings(directory).save({'plugins':{p['id']:source_tools.plugin_enabled(p) for p in PluginRegistry(self.host_root).entries()}})
        if persona is not None:
            from persona_library import PersonaLibrary
            saved=PersonaLibrary(directory).create(persona['name'],persona['body'])
            SocialContext(directory).apply_active_persona('qq',saved['id'])
        # Initialize real stores/index schemas through the existing modules.
        from fish_agent.bootstrap import get_runtime
        get_runtime(directory)
        from memory_store import MemoryStore
        MemoryStore(directory,'qq')
        if running is True or running is None and persona is not None:self.set_running(instance_id,True)
        return self.item(instance_id)

    def rename(self,instance_id,name):
        self.resolve(instance_id)
        title=' '.join(str(name or '').split())
        if not title or len(title)>80:raise ValueError('请填写窗口名称，最多80字')
        with _catalog_lock(self.host_root):
            raw=_json(self.path,{})
            next(row for row in raw['instances'] if row['id']==instance_id)['name']=title
            _write(self.path,raw)
        return self.item(instance_id)

    def set_running(self,instance_id,running):
        if type(running) is not bool:raise ValueError('窗口运行状态必须为布尔值')
        context=self.resolve(instance_id)
        if running:
            from social_context import SocialContext
            SocialContext(context.root).active_persona('qq')
        from governance import Governance
        Governance(context.root,'qq').save({'paused':not running})
        from fish_agent.bootstrap import get_runtime
        runtime=get_runtime(context)
        runtime.start() if running else runtime.stop()
        return self.item(instance_id)


def instance_context(root, instance_id=None):
    if isinstance(root, InstanceContext) and (instance_id is None or instance_id == root.instance_id):
        return root
    return InstanceCatalog(root).resolve(instance_id or _instance_id(root))


def project_path(root, *parts):
    """Resolve a known project-relative storage/resource path explicitly."""
    relative = Path(*parts)
    if relative.is_absolute():
        return relative
    host = host_root(root)
    names = relative.parts
    if not names:
        return host
    head = names[0]
    if head == 'channels' or head in ('vendor', 'source', 'bin'):
        return host / relative
    if head == 'harness':
        tail = names[1:]
        # Python/runtime packages, model connections and credentials belong to host.
        shared = (not tail or tail[0] in _SHARED_CONFIG or
                  (tail and Path(tail[0]).suffix not in ('.json', '.db', '.lock')))
        if tail and tail[0] == 'runtime' and any(str(t).endswith('.identities.json') for t in tail):
            shared = False
        if shared:
            return host / relative
        return instance_context(root).directory / 'config' / Path(*tail)
    if head == 'data':
        if len(names) > 1 and names[1] in _SHARED_DATA:
            return host / relative
        return instance_context(root).directory / Path(*names[1:])
    if head == 'outputs':
        return instance_context(root).directory / 'resources' / relative
    if head in ('workspace', 'qq-work'):
        context = instance_context(root)
        return context.directory / ('workspace' if head == 'workspace' else 'workspace/legacy-qq') / Path(*names[1:])
    return host / relative


def integration_settings(root):
    """Startup configuration is shared; publication selection belongs to instance."""
    shared = _json(host_root(root) / 'harness/integration.json', {})
    own = _json(instance_context(root).directory / 'config/integration.json', {})
    return {**shared, **own}


def save_integration_option(root, option, value):
    if option == 'qzone_enabled':
        path = instance_context(root).directory / 'config/integration.json'
    else:
        path = host_root(root) / 'harness/integration.json'
    _write(path, {**_json(path, {}), option: value})
