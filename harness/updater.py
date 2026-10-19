"""Prepare a checksummed local release without overwriting a running program."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import time
import uuid
import zipfile

VERSION = '0.4.3'


def inspect_release(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 80000 or sum(i.file_size for i in infos) > 5 * 1024**3:
            raise ValueError('升级包超过允许上限')
        manifest = json.loads(archive.read('fish-release.json'))
        if manifest.get('product') != 'Fish' or manifest.get('format') != 1 or not re.fullmatch('[a-zA-Z0-9.-]{1,60}', manifest.get('version', '')):
            raise ValueError('不是支持的 Fish 升级包')
        if set(i.filename for i in infos) != set(manifest['files']) | {'fish-release.json'} or len(set(i.filename for i in infos)) != len(infos):
            raise ValueError('升级包文件与清单不一致')
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or '..' in path.parts or ':' in info.filename or '\\' in info.filename or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('升级包包含不安全路径')
            if info.file_size > 500 * 1024**2: raise ValueError('升级包含过大文件')
        if 'Fish.exe' not in manifest['files']: raise ValueError('升级包缺少 Fish.exe')
    return manifest


def prepare_release(path, parent, *, progress=None):
    path, parent = Path(path).resolve(), Path(parent).resolve()
    manifest = inspect_release(path)
    target = parent / ('Fish-' + manifest['version'] + '-' + uuid.uuid4().hex[:8])
    if not target.resolve().is_relative_to(parent): raise ValueError('升级目标路径异常')
    target.mkdir(parents=True)
    try:
        with zipfile.ZipFile(path) as archive:
            total = len(manifest['files'])
            reported = time.monotonic()
            if progress: progress(0, total)
            for completed, (relative, expected) in enumerate(manifest['files'].items(), 1):
                destination = (target / relative).resolve()
                if not destination.is_relative_to(target): raise ValueError('升级路径异常')
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with archive.open(relative) as source, destination.open('wb') as output:
                    while block := source.read(1024 * 1024): output.write(block); digest.update(block)
                if digest.hexdigest() != expected: raise ValueError('升级文件校验失败：' + relative)
                if progress and (completed == total or time.monotonic() - reported >= .2):
                    progress(completed, total)
                    reported = time.monotonic()
        return {'ok': True, 'version': manifest['version'], 'exe': str(target / 'Fish.exe')}
    except Exception:
        # Keep the incomplete directory reviewable; it is never activated.
        (target / 'INSTALL-FAILED.txt').write_text('校验未通过；此目录未启动。', encoding='utf-8')
        raise


def launch_maintenance(root, operation, *, archive='', password='', exe='', target=''):
    """The helper waits until this UI exits before touching data or launching."""
    from secrets_store import SecretStore
    task_id = uuid.uuid4().hex
    directory = Path(root) / 'harness/maintenance'; directory.mkdir(parents=True, exist_ok=True)
    request = {'operation': operation, 'archive': str(archive), 'target': str(target), 'exe': str(exe or sys.executable), 'parent': os.getpid(), 'source_mode': not getattr(sys, 'frozen', False)}
    if password: request['password'] = __import__('base64').b64encode(SecretStore._crypt(password.encode())).decode()
    path = directory / (task_id + '.json'); path.write_text(json.dumps(request), encoding='utf-8')
    env = os.environ.copy(); env['FISH_HARNESS_HOME'] = str(root)
    command = [sys.executable]
    if not getattr(sys, 'frozen', False): command += [str(Path(__file__).resolve().parents[1] / 'launch_fish.py')]
    command += ['--backend', 'maintenance', '--request', str(path)]
    subprocess.Popen(command, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return {'ok': True, 'request': str(path)}


def run_maintenance(root, request_path):
    request_path = Path(request_path).resolve(); directory = (Path(root) / 'harness/maintenance').resolve()
    if not request_path.is_relative_to(directory): raise ValueError('维护请求位置不正确')
    request = json.loads(request_path.read_text(encoding='utf-8'))
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]; kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]; kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, int(request['parent']))
        if handle:
            try:
                if kernel.WaitForSingleObject(handle, 60000) == 258: raise TimeoutError('当前 Fish 未能退出，维护没有执行')
            finally: kernel.CloseHandle(handle)
    result = {'ok': True}; active_root = Path(root)
    try:
        if request['operation'] == 'restore':
            from backup import BackupManager
            from secrets_store import SecretStore
            password = SecretStore._crypt(__import__('base64').b64decode(request['password']), True).decode() if request.get('password') else ''
            result = BackupManager(root).restore(request['archive'], password=password)
        elif request['operation'] == 'migrate':
            result = migrate_data(root, request['target'])
            active_root = Path(result['root'])
        elif request['operation'] != 'upgrade': raise ValueError('未知维护操作')
    except Exception as exc:
        result = {'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)[:500]}
    result_path = directory / ('result-' + request_path.name); result_path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    request_path.unlink(missing_ok=True)
    env = os.environ.copy(); env.update(FISH_HARNESS_HOME=str(active_root), FISH_MAINTENANCE_RESULT=str(result_path))
    command = [request['exe']]
    if request.get('source_mode') and request['operation'] in ('restore', 'migrate'): command += [str(Path(__file__).resolve().parents[1] / 'launch_fish.py')]
    subprocess.Popen(command, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return 0 if result['ok'] else 1


def migration_target(root, target):
    root, target = Path(root).resolve(), Path(target).resolve()
    if root == target or target.is_relative_to(root) or root.is_relative_to(target):
        raise ValueError('新目录不能是当前目录、它的上级或子目录')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError('请选择一个空文件夹；已有内容不会被覆盖')
    return target


def migrate_data(root, target):
    """Copy with verified SQLite snapshots, then switch only after restoration."""
    from backup import BackupManager
    from fish_desktop.bootstrap import initialize, select_root
    import tempfile
    root = Path(root).resolve(); target = migration_target(root, target)
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fish-migration-') as work:
        archive = Path(work) / 'data.zip'
        BackupManager(root).create(archive)
        result = BackupManager(target).restore(archive)
        initialize(target)
    select_root(target)
    return {**result, 'root': str(target), 'old_root': str(root), 'old_data_retained': True}
