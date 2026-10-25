"""User-approved MCP, CLI and HTTP bridges. Runtime checks remain authoritative."""
from __future__ import annotations
from locations import project_path
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse
import requests
from storage import database, locked_settings, read_json, role_dir, write_json

# Keep optional SDK dependencies local to this application, not global Python.
_vendor = Path(__file__).resolve().parent.parent / 'vendor/python'
if _vendor.is_dir() and str(_vendor) not in sys.path:
    sys.path.insert(0, str(_vendor))
    import site
    site.addsitedir(str(_vendor))


def safe_environment(extra=None):
    allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
               'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env['PYTHONIOENCODING'] = 'utf-8'
    env.update(extra or {})
    return env


def outbound_tool(name):
    return bool(re.search(r'(^|_)(send|post|publish|comment|reply|upload|notify|onebot)(_|$)|(^|_)bili_(like|follow)(_|$)', name, re.I))


def tool_allowed(plugin, tool):
    # Enabled/discovered plugins expose their real tools. An explicitly selected
    # list remains a user configuration, rather than a name-based write ban.
    selected = plugin.get('allowed_tools')
    return selected is None or tool['name'] in selected


def failure_reason(exc):
    """Flatten SDK task groups without leaking URLs, headers or stderr."""
    children = getattr(exc, 'exceptions', None)
    if children: return '；'.join(dict.fromkeys(failure_reason(e) for e in children))[:400]
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if status: return 'HTTP ' + str(status) + ('：凭证无效或没有权限' if status in (401, 403) else '：请检查服务地址及插件端日志')
    name = type(exc).__name__
    if 'Timeout' in name: return '插件连接或执行超时，请检查服务是否就绪'
    if isinstance(exc, FileNotFoundError): return '启动程序或入口文件不存在，请重新选择本地程序'
    if any(k in name for k in ('Connect', 'Closed', 'Broken', 'EndOfStream')): return '服务未连接或提前退出，请检查端口、启动参数及插件端日志'
    if isinstance(exc, json.JSONDecodeError): return '插件返回的内容不是有效 JSON'
    if isinstance(exc, InterruptedError): return '插件调用已停止'
    # These messages originate in Fish, not an external response body.
    if isinstance(exc, (ValueError, RuntimeError)) and str(exc).startswith(('工具名称', '测试期间', '工具列表', 'CLI/HTTP', '插件退出码', 'MCP 工具报告')):
        return str(exc)[:200]
    return name + '：请检查插件端日志和本地配置'


def health_details(item):
    if item['kind'] == 'dsh':
        dependency = '本地包目录' + ('存在' if Path(item.get('command', '')).is_dir() else '缺失')
    elif item['kind'] in ('mcp_stdio', 'cli'):
        dependency = '启动程序' + ('存在' if Path(item.get('command', '')).is_file() else '缺失')
        scripts = [Path(a) for a in item.get('args', []) if Path(a).is_absolute() and Path(a).suffix in ('.py', '.js', '.mjs', '.cjs')]
        if scripts: dependency += '；入口文件' + ('存在' if all(p.is_file() for p in scripts) else '缺失')
    else: dependency = '远程服务，依赖在服务端管理'
    version = item.get('server_info', {}).get('version') or item.get('package_version') or '未声明版本'
    state = '最近测试失败' if item.get('test_ok') is False else '最近测试通过' if item.get('last_test') else '尚未测试'
    return f"版本：{version}\n依赖：{dependency}\n状态：{state}" + ('\n原因：' + item['last_error'] if item.get('last_error') else '')


class PluginRegistry:
    def __init__(self, root: Path, manager=None):
        self.root = root
        self.path = project_path(root, 'harness/plugins/registry.json')
        self.log = project_path(root, 'data/plugin-events.db')
        self.active = {}
        self.lock = threading.RLock()
        self.tulpa_call_lock = threading.Lock()
        self.manager = manager
        with database(self.log) as db:
            db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts REAL,plugin TEXT,role TEXT,action TEXT,ok INTEGER,detail TEXT)')

    def entries(self):
        return read_json(self.path, {'plugins': []})['plugins']

    @locked_settings
    def save(self, manifest: dict):
        item = json.loads(json.dumps(manifest))
        pid = str(item.get('id', ''))
        if not re.fullmatch('[a-z][a-z0-9_]{1,39}', pid):
            raise ValueError('插件 ID 需为 2–40 位小写字母、数字和下划线，首位为字母')
        if item.get('kind') not in ('mcp_stdio', 'mcp_http', 'mcp_sse', 'cli', 'http', 'dsh'):
            raise ValueError('未知插件类型')
        item['name'] = str(item.get('name', pid))[:100]
        item['roles'] = [r for r in item.get('roles', []) if r in ('qq', 'research')]
        item['enabled'] = bool(item.get('enabled', False))
        item['owner_only'] = bool(item.get('owner_only', True))
        item['timeout'] = max(3, min(240, int(item.get('timeout', 60))))
        item['args'] = [str(x) for x in item.get('args', [])]
        if item['kind'] == 'dsh':
            project = Path(str(item.get('command', '')))
            if not project.is_dir() or not (project / 'package.json').is_file():
                raise ValueError('DSH 原生插件请选择含 package.json 的本地项目目录')
            package = read_json(project / 'package.json')
            if not re.fullmatch(r'(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*', str(package.get('name', ''))):
                raise ValueError('插件 package.json 的 name 不合法')
            item['package_name'] = package['name']
            item['package_version'] = str(package.get('version', '未声明版本'))[:100]
        elif item['kind'] in ('mcp_stdio', 'cli'):
            command = str(item.get('command', ''))
            if not Path(command).is_file():
                raise ValueError('请选择已安装程序的绝对路径')
            if Path(command).suffix.lower() in ('.bat', '.cmd', '.ps1'):
                raise ValueError('请选择程序本体（如 node.exe 加 JS 入口），不要选择 shell 启动脚本')
        else:
            parsed = urlparse(str(item.get('url', '')))
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('插件地址需为不含账号密码的 HTTP(S) URL')
        from secrets_store import SecretStore
        api_key = str(item.pop('api_key', ''))
        if api_key.strip():
            SecretStore(self.root).put('plugin-' + pid + '-http', api_key)
            item['bearer_ref'] = 'plugin-' + pid + '-http'
        # Only references persist. Existing protected values survive blank fields.
        for key, value in item.pop('secrets', {}).items():
            if not re.fullmatch('[A-Z][A-Z0-9_]{0,79}', key):
                raise ValueError('环境变量名不合法')
            import hashlib
            ref = 'plugin-' + hashlib.sha256((pid + ':' + key).encode()).hexdigest()[:48]
            SecretStore(self.root).put(ref, str(value))
            item.setdefault('secret_env', {})[key] = ref
        entries = self.entries()
        old = next((e for e in entries if e['id'] == pid), None)
        if old:
            if old['kind'] == 'dsh' and old.get('installed_roles') and (item['kind'] != 'dsh' or item.get('command') != old.get('command')):
                raise ValueError('更换已安装的原生项目之前，请先停用并卸载当前包')
            for key in ('installed_roles', 'native_tools_by_role'):
                if key in old: item[key] = old[key]
            if old.get('bearer_ref') and not item.get('bearer_ref'): item['bearer_ref'] = old['bearer_ref']
            item['secret_env'] = {**old.get('secret_env', {}), **item.get('secret_env', {})}
            # A changed executable/config needs another discovery, no stale schema.
            if any(item.get(k) != old.get(k) for k in ('kind', 'command', 'args', 'url')):
                item['tools'] = []
                item['last_test'] = None; item['test_ok'] = False; item.pop('server_info', None)
            elif item.get('tools', old.get('tools')) == old.get('tools'):
                for key in ('last_test', 'test_ok', 'last_error', 'server_info'):
                    if key in old: item[key] = old[key]
        item.setdefault('tools', old.get('tools', []) if old else [])
        if item['enabled'] and item['kind'] != 'dsh' and (not item.get('last_test') or item.get('test_ok') is False):
            raise ValueError('请先保存并测试插件，再启用')
        write_json(self.path, {'version': 1, 'plugins': [e for e in entries if e['id'] != pid] + [item]})
        self.publish()
        self.event(pid, '', 'save', True, '设置已保存')
        return {'ok': True, 'id': pid}

    @locked_settings
    def toggle(self, pid, enabled, role=None):
        entries = self.entries()
        item = next((e for e in entries if e['id'] == pid), None)
        if not item:
            raise ValueError('插件不存在')
        if role is not None and role not in item['roles']:
            raise PermissionError('插件未授权给此角色')
        if enabled and item['kind'] != 'dsh' and (not item.get('tools') or item.get('test_ok') is False):
            raise ValueError('请先测试并发现工具，再启用')
        item['enabled'] = bool(enabled)
        write_json(self.path, {'version': 1, 'plugins': entries})
        if not enabled:
            self.cancel(pid)
        self.publish()
        if item['kind'] == 'dsh' and not enabled:
            self._uninstall_native(item)
        return {'ok': True}

    @locked_settings
    def remove(self, pid):
        item = next((e for e in self.entries() if e['id'] == pid), None)
        if item and item['kind'] == 'dsh': self._uninstall_native(item)
        self.cancel(pid)
        write_json(self.path, {'version': 1, 'plugins': [e for e in self.entries() if e['id'] != pid]})
        self.publish()
        return {'ok': True}

    def event(self, pid, role, action, ok, detail=''):
        # No arguments, raw stderr or bearer URL in audit records.
        with database(self.log) as db:
            db.execute('INSERT INTO events(ts,plugin,role,action,ok,detail) VALUES(?,?,?,?,?,?)',
                       (time.time(), pid, role, action, int(ok), detail[:400]))

    def snapshot(self):
        with database(self.log) as db:
            logs = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 80')]
        return {'plugins': self.entries(), 'events': logs, 'active': list(self.active)}

    def _env(self, item, role='research'):
        from secrets_store import SecretStore
        secrets = {key: SecretStore(self.root).get(ref) for key, ref in item.get('secret_env', {}).items()}
        directory = role_dir(self.root, role) / 'plugin-work' / item['id']
        home = directory / 'home'; home.mkdir(parents=True, exist_ok=True)
        temporary = directory / 'tmp'; temporary.mkdir(exist_ok=True)
        return {**__import__('network_config').environment(self.root, safe_environment(secrets)), 'HOME': str(home), 'USERPROFILE': str(home),
                'APPDATA': str(home / 'AppData/Roaming'), 'LOCALAPPDATA': str(home / 'AppData/Local'),
                'FISH_PLUGIN_ROLE': role, 'FISH_DATA_DIR': str(directory), 'TEMP': str(temporary), 'TMP': str(temporary)}

    def _args(self, item, role):
        directory = role_dir(self.root, role) / 'plugin-work' / item['id']
        return [value.replace('{role}', role).replace('{data_dir}', str(directory)) for value in item['args']]

    async def _mcp(self, item, method, tool='', arguments=None, role='research'):
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.client.sse import sse_client
        async with AsyncExitStack() as stack:
            if item['kind'] == 'mcp_stdio':
                cwd = role_dir(self.root, role) / 'plugin-work' / item['id']
                cwd.mkdir(parents=True, exist_ok=True)
                params = StdioServerParameters(command=item['command'], args=self._args(item, role), env=self._env(item, role), cwd=str(cwd))
                errlog = stack.enter_context(tempfile.TemporaryFile(mode='w+', encoding='utf-8'))
                read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
            else:
                headers = {}
                ref = item.get('bearer_ref')
                if ref:
                    from secrets_store import SecretStore
                    headers['Authorization'] = 'Bearer ' + SecretStore(self.root).get(ref)
                import httpx
                local = urlparse(item['url']).hostname in ('127.0.0.1', 'localhost', '::1')
                def http_client(headers=None, timeout=None, auth=None):
                    # A loopback tool must stay on this computer even when the
                    # user's network environment routes general HTTP via a proxy.
                    return httpx.AsyncClient(headers=headers, timeout=timeout or httpx.Timeout(30, read=300), auth=auth, **__import__('network_config').httpx_options(self.root, item['url']))
                connection = streamablehttp_client(item['url'], headers=headers, timeout=timedelta(seconds=item['timeout']), httpx_client_factory=http_client) if item['kind'] == 'mcp_http' else sse_client(item['url'], headers=headers, timeout=item['timeout'], httpx_client_factory=http_client)
                streams = await stack.enter_async_context(connection)
                read, write = streams[:2]
            session = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=timedelta(seconds=item['timeout'])))
            initialized = await session.initialize()
            item['server_info'] = initialized.serverInfo.model_dump(mode='json', exclude_none=True)
            if method == 'discover':
                tools, cursor = [], None
                for _ in range(20):
                    result = await session.list_tools(cursor=cursor)
                    tools.extend(t.model_dump(mode='json', exclude_none=True) for t in result.tools)
                    cursor = result.nextCursor
                    if not cursor:
                        return tools
                raise ValueError('工具列表分页过多')
            result = await session.call_tool(tool, arguments or {})
            if result.isError:
                raise RuntimeError('MCP 工具报告执行失败；可检查插件自身日志')
            value = result.model_dump(mode='json', exclude_none=True)
            if item.get('id') == 'tulpa_owner' and value.get('content'):
                # Tulpa supplies the same JSON twice (text and structured).
                # Preserve original content/image blocks, not duplicate history.
                value.pop('structuredContent', None)
            return value

    async def _run_cancellable(self, coroutine, timeout, cancel):
        task = asyncio.create_task(coroutine)
        end = time.monotonic() + timeout
        try:
            while not task.done():
                if cancel.is_set():
                    raise InterruptedError('插件调用已停止；外部服务已执行的动作无法撤销')
                if time.monotonic() > end:
                    raise TimeoutError('插件调用超时，未自动重试以避免重复动作')
                await asyncio.sleep(.05)
            return await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass

    def _run(self, item, method, tool='', arguments=None, role='research', cancel=None):
        if item.get('id')=='tulpa_owner':
            # The UI and ToolHost use separate processes and SDK connections.
            # One cross-process queue also covers persona stop/start batches.
            from storage import settings_lock
            with settings_lock(project_path(self.root, 'data/integrations/tulpa/call-queue'),timeout=120):
                return self._run_unlocked(item,method,tool,arguments,role,cancel)
        return self._run_unlocked(item,method,tool,arguments,role,cancel)

    def _run_unlocked(self, item, method, tool='', arguments=None, role='research', cancel=None):
        cancel = cancel or threading.Event()
        if item['kind'].startswith('mcp_'):
            return asyncio.run(self._run_cancellable(self._mcp(item, method, tool, arguments, role), item['timeout'], cancel))
        if item['kind'] in ('http', 'cli'):
            if method == 'discover':
                tools = item.get('tools', [])
                if not tools:
                    raise ValueError('CLI/HTTP 插件需在配置中声明 tools；调用使用 JSON 参数')
                return tools
            if item['kind'] == 'http':
                headers = {}
                if item.get('bearer_ref'):
                    from secrets_store import SecretStore
                    headers['Authorization'] = 'Bearer ' + SecretStore(self.root).get(item['bearer_ref'])
                response = requests.post(item['url'], headers=headers, json={'tool': tool, 'arguments': arguments or {}}, timeout=(5, item['timeout']), allow_redirects=False, stream=True, **__import__('network_config').request_options(self.root, item['url']))
                response.raise_for_status()
                output = bytearray()
                for chunk in response.iter_content(65536):
                    if cancel.is_set():
                        raise InterruptedError('已停止等待 HTTP 工具')
                    output.extend(chunk)
                    if len(output) > 2 * 1024 * 1024:
                        raise ValueError('插件输出超过 2MB')
                return json.loads(output)
            cwd = role_dir(self.root, role) / 'plugin-work' / item['id']
            cwd.mkdir(parents=True, exist_ok=True)
            from process_tools import OwnedJob
            job_scope = OwnedJob()
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
                proc = subprocess.Popen([item['command'], *self._args(item, role)], stdin=subprocess.PIPE, stdout=output, stderr=error,
                    cwd=cwd, env=self._env(item, role), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    job_scope.attach(proc)
                    proc.stdin.write(json.dumps({'tool': tool, 'arguments': arguments or {}}, ensure_ascii=False).encode('utf-8'))
                    proc.stdin.close()
                    end = time.monotonic() + item['timeout']
                    while proc.poll() is None:
                        if cancel.wait(.05):
                            raise InterruptedError('插件调用已停止')
                        if time.monotonic() > end:
                            raise TimeoutError('插件执行超时，未自动重试')
                        if output.tell() > 2 * 1024 * 1024 or error.tell() > 2 * 1024 * 1024:
                            raise ValueError('插件输出超过上限')
                    if proc.returncode:
                        raise RuntimeError(f'插件退出码 {proc.returncode}，请检查插件自身配置')
                    output.seek(0)
                    return json.loads(output.read(2 * 1024 * 1024))
                finally:
                    if proc.poll() is None:
                        from process_tools import stop_process_tree
                        stop_process_tree(proc)
                    job_scope.close()
        raise ValueError('DSH 原生包请使用安装操作；不作为独立工具执行')

    def discover(self, pid, role='research'):
        item = next(e for e in self.entries() if e['id'] == pid)
        if item['kind'] == 'dsh': return self.install_native(pid)
        try:
            tools = self._run(item, 'discover', role=role)
            from jsonschema import Draft202012Validator
            for tool in tools:
                if not re.fullmatch('[a-zA-Z0-9_-]{1,100}', tool.get('name', '')):
                    raise ValueError('工具名称不合法')
                Draft202012Validator.check_schema(tool.get('inputSchema', {}))
            with __import__('storage').settings_lock(self.root):
                entries = self.entries()
                current = next(e for e in entries if e['id'] == pid)
                if any(current.get(k) != item.get(k) for k in ('command', 'args', 'url', 'kind')):
                    raise RuntimeError('测试期间配置已更改，请重新测试')
                current['tools'] = tools
                current['last_test'] = time.time()
                current['test_ok'] = True; current['last_error'] = ''
                if item.get('server_info'): current['server_info'] = item['server_info']
                write_json(self.path, {'version': 1, 'plugins': entries})
                self.publish()
            self.event(pid, role, 'discover', True, f'发现 {len(tools)} 个工具')
            return {'ok': True, 'tools': tools}
        except Exception as exc:
            reason = failure_reason(exc)
            with __import__('storage').settings_lock(self.root):
                entries = self.entries(); current = next((e for e in entries if e['id'] == pid), None)
                if current and all(current.get(k) == item.get(k) for k in ('command', 'args', 'url', 'kind')):
                    current.update(test_ok=False, last_error=reason, last_test=time.time(), enabled=False)
                    write_json(self.path, {'version': 1, 'plugins': entries}); self.publish()
            self.cancel(pid)
            self.event(pid, role, 'discover', False, reason)
            raise RuntimeError(reason) from None

    def catalog(self, role):
        return [{'name': f"fishp__{p['id']}__{t['name']}", 'description': f"{p['name']}：{t.get('description', '')}"[:4000],
                 'parameters': t.get('inputSchema', {'type': 'object', 'properties': {}}), 'owner_only': p['owner_only']}
                for p in self.entries() if p['enabled'] and role in p['roles'] and p['kind'] != 'dsh' for t in p.get('tools', []) if tool_allowed(p, t)]

    def native_catalog(self, role):
        return [{'name': t['name'], 'owner_only': p['owner_only'], 'plugin': p['id']}
                for p in self.entries() if p['kind'] == 'dsh' and p['enabled'] and role in p.get('installed_roles', []) and role in p['roles'] for t in p.get('native_tools_by_role', {}).get(role, []) if tool_allowed(p, t)]

    def _runtime(self):
        if self.manager is None:
            from core import HarnessConfig, RuntimeManager
            self.manager = RuntimeManager(HarnessConfig(self.root))
        return self.manager

    def _native_api(self, role, route):
        from governance import control_token
        from core import root_id
        manager = self._runtime()
        response = requests.get(f'http://127.0.0.1:{manager.config.runtime_port(role)}/fish/' + route,
            headers={'x-fish-token': control_token(self.root)}, timeout=5, proxies={'http':None,'https':None,'all':None})
        response.raise_for_status(); data = response.json()
        if data.get('root_id') != root_id(self.root) or data.get('role') != role:
            raise PermissionError('服务不属于所选角色和数据目录')
        return data

    def _stop_native_role(self, role):
        import socket
        manager = self._runtime()
        try:
            status = self._native_api(role, 'status')
            if status.get('active'):
                raise RuntimeError('该角色正在对话，请等待完成或先停止当前任务')
        except requests.ConnectionError: return False
        manager.stop(role)
        try:
            with socket.create_connection(('127.0.0.1', manager.config.runtime_port(role)), timeout=.3):
                raise RuntimeError('角色尚未停止，未修改插件安装')
        except OSError: pass
        return True

    def _uninstall_native(self, item):
        manager = self._runtime()
        for role in list(item.get('installed_roles', [])):
            running = self._stop_native_role(role)
            result = manager.run_cli(['plugin', '--profile', manager.config.runtime.profile, 'remove', item['package_name']],
                home=manager.config.runtime_home(role), environment=safe_environment(), timeout=180)
            if result.returncode: raise RuntimeError('DSH 停用失败，请查看原生插件依赖；角色已停止以避免继续运行插件')
            with __import__('storage').settings_lock(self.root):
                entries = self.entries(); current = next(p for p in entries if p['id'] == item['id'])
                current['installed_roles'] = [r for r in current.get('installed_roles', []) if r != role]
                write_json(self.path, {'version': 1, 'plugins': entries}); self.publish()
            if running:
                from runtime_adapter import DSHRuntimeAdapter
                DSHRuntimeAdapter(self.root, role, manager).connect()
        return {'ok': True}

    def install_native(self, pid):
        item = next(p for p in self.entries() if p['id'] == pid)
        if not item['roles']: raise ValueError('先选择允许安装的角色')
        manager, catalogs = self._runtime(), {}
        from runtime_adapter import DSHRuntimeAdapter
        try:
            for role in item['roles']:
                DSHRuntimeAdapter(self.root, role, manager).connect()
                before = self._native_api(role, 'tools')['tools']
                self._stop_native_role(role)
                result = manager.run_cli(['plugin', '--profile', manager.config.runtime.profile, 'add', str(Path(item['command']).resolve())],
                    home=manager.config.runtime_home(role), timeout=180, environment=safe_environment())
                if result.returncode:
                    raise RuntimeError('DSH 原生插件安装失败（退出码 ' + str(result.returncode) + '）；检查该项目的依赖和构建说明')
                DSHRuntimeAdapter(self.root, role, manager).connect()
                after = self._native_api(role, 'tools')['tools']
                old_names = {t['name'] for t in before}
                own_names = {t['name'] for t in item.get('native_tools_by_role', {}).get(role, [])}
                catalogs[role] = [{'name': t['name'], 'description': t.get('description', ''), 'inputSchema': t.get('parameters', {})}
                    for t in after if t['name'] not in old_names or t['name'] in own_names]
                with __import__('storage').settings_lock(self.root):
                    entries = self.entries(); current = next(p for p in entries if p['id'] == pid)
                    current['installed_roles'] = sorted(set(current.get('installed_roles', []) + [role]))
                    current.setdefault('native_tools_by_role', {})[role] = catalogs[role]
                    current['tools'] = list({t['name']: t for rows in current['native_tools_by_role'].values() for t in rows}.values())
                    current['last_test'] = time.time(); write_json(self.path, {'version': 1, 'plugins': entries}); self.publish()
            updated = next(p for p in self.entries() if p['id'] == pid)
            if not updated['enabled']: self._uninstall_native(updated)
            tools = list({t['name']: t for rows in catalogs.values() for t in rows}.values())
            self.event(pid, '', 'install_native', True, f'发现 {len(tools)} 个原生工具；安装范围：' + '/'.join(item['roles']))
            return {'ok': True, 'tools': tools, 'native': True, 'enabled': updated['enabled']}
        except Exception as exc:
            self.event(pid, '', 'install_native', False, type(exc).__name__)
            raise

    def publish(self):
        for role in ('qq', 'research'):
            write_json(project_path(self.root, f'harness/runtime/{role}.tools.json'), {'tools': self.catalog(role)})

    def execute(self, alias, arguments, role, *, owner=False, cancel=None, chat_authorized=False):
        _, pid, tool = alias.split('__', 2)
        if pid == 'tulpa_owner' and not chat_authorized:
            raise PermissionError('Tulpa MCP 需要已核验的 Fish 主人授权')
        item = next((p for p in self.entries() if p['id'] == pid), None)
        if not item or not item['enabled'] or role not in item['roles']:
            raise PermissionError('插件已禁用或未授权给当前角色')
        if item['owner_only'] and not owner:
            raise PermissionError('此插件仅允许主人私聊和本地控制台调用')
        schema = next((t for t in item.get('tools', []) if t['name'] == tool), None)
        if schema is None:
            raise PermissionError('工具未注册')
        if not tool_allowed(item, schema):
            raise PermissionError('此工具未被插件当前配置选中')
        from jsonschema import validate
        validate(arguments, schema.get('inputSchema', {}))
        from governance import Governance
        policy = Governance(self.root, role)
        if policy.config()['paused']:
            raise PermissionError('此角色已暂停')
        if pid == 'tulpa_owner':
            # Protocol prompts, quoted examples and received chat are input
            # data, not outgoing QQ text. Check only text actually being sent.
            if tool == 'send_chat_reply':
                for bubble in arguments.get('bubbles', []):
                    if bubble.get('kind') == 'text':
                        policy.check_output(str(bubble.get('text', '')))
        else:
            policy.check_output(json.dumps(arguments, ensure_ascii=False))
        cancel = cancel or threading.Event()
        key = pid + ':' + __import__('uuid').uuid4().hex
        with self.lock:
            self.active[key] = cancel
        try:
            if pid == 'tulpa_owner':
                # Each SDK connection starts its JSON-RPC counter at zero.
                # Tulpa detects duplicate IDs per grant, including across
                # connections, so concurrent calls must use its single queue.
                with self.tulpa_call_lock:
                    if cancel.is_set(): raise InterruptedError('插件调用已停止')
                    result = self._run(item, 'call', tool, arguments, role, cancel)
            else:
                result = self._run(item, 'call', tool, arguments, role, cancel)
            from plugin_media import normalize_result
            result = normalize_result(self.root, role, result)
            if len(json.dumps(result, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
                raise ValueError('插件结果超过 2MB，请在插件配置中减少返回内容')
            if pid != 'tulpa_owner':
                policy.check_output(json.dumps(result, ensure_ascii=False))
            self.event(pid, role, tool, True)
            return result
        except Exception as exc:
            reason = failure_reason(exc)
            self.event(pid, role, tool, False, reason)
            if isinstance(exc, InterruptedError): raise
            raise RuntimeError(reason) from None
        finally:
            with self.lock:
                self.active.pop(key, None)

    def cancel(self, pid, notify=True):
        with self.lock:
            for key, event in self.active.items():
                if key.startswith(pid + ':'):
                    event.set()
        if notify:
            try:
                from tool_host import request
                request(self.root, '/plugins/cancel', {'id': pid}, timeout=1)
            except (requests.RequestException, ValueError, PermissionError):
                pass
        return {'ok': True}
