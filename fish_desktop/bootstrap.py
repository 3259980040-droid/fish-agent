"""First-run data roots and sanitized defaults; application files stay separate."""
from locations import project_path
import json
import os
from pathlib import Path
import shutil
import sys


def resources():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))


def initialize(root):
    root = Path(root).resolve()
    if (project_path(root, 'harness/agents.json')).is_file():
        # Backups contain files, so a never-started role has no home entries.
        # Recreate required empty homes after restore/migration before preflight.
        for role in ('qq', 'research'):
            (root / 'homes' / role).mkdir(parents=True, exist_ok=True)
        sync_control(root)
        repair_runtime_paths(root)
        return root
    root.mkdir(parents=True, exist_ok=True)
    defaults = resources() / 'packaging/defaults'
    for path in defaults.rglob('*'):
        if path.is_file():
            relative = path.relative_to(defaults)
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists(): shutil.copy2(path, target)
    control = resources() / 'harness/runtime/fish-control'
    if control.is_dir():
        shutil.copytree(control, project_path(root, 'harness/runtime/fish-control'), dirs_exist_ok=True)
    safety = resources() / 'harness/safety'
    if safety.is_dir(): shutil.copytree(safety, project_path(root, 'harness/safety'), dirs_exist_ok=True)
    for role in ('qq', 'research'):
        (root / 'homes' / role).mkdir(parents=True, exist_ok=True)
    return root


def sync_control(root):
    """Internal enforcement code follows the installed application on upgrade."""
    control = resources() / 'harness/runtime/fish-control'
    target = project_path(Path(root), 'harness/runtime/fish-control')
    if not control.is_dir() or control.resolve() == target.resolve(): return
    for relative in ('package.json', 'cordis.patch.yml', 'lib/index.js'):
        source, destination = control / relative, target / relative
        if not source.is_file(): continue
        value = source.read_bytes()
        if destination.is_file() and destination.read_bytes() == value: continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = destination.with_name(destination.name + '.update')
        stage.write_bytes(value); stage.replace(destination)


def repair_runtime_paths(root):
    """Recover old-machine runtime paths after migration, keeping valid choices."""
    from storage import settings_lock, read_json, write_json
    from core import resolve
    path = project_path(Path(root), 'harness/agents.json')
    bundle = resources() / ('runtime' if getattr(sys, 'frozen', False) else 'vendor/runtime')
    if not (bundle / 'manifest.json').is_file(): return
    with settings_lock(root):
        data = read_json(path); runtime = data.get('runtime', {})
        if runtime.get('version') != read_json(bundle / 'manifest.json').get('dsh'): return
        changed = False
        for key, fallback in [('cli', '@bundle/runtime/fish-dsh.mjs'), ('node', '@bundle/runtime/node/node.exe')]:
            configured = runtime.get(key, '')
            if (not configured or not resolve(root, configured).is_file()) and resolve(root, fallback).is_file():
                runtime[key] = fallback; changed = True
        if changed: write_json(path, data)


def locate_root():
    activate_install_profile()
    configured = os.environ.get('FISH_HARNESS_HOME')
    if configured: return initialize(Path(configured))
    selected = selected_root()
    if selected:
        if not (selected / 'harness/agents.json').is_file():
            raise FileNotFoundError('所选数据目录不可用，请连接对应磁盘：' + str(selected))
        return initialize(selected)
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable).resolve()
        for parent in (executable.parent, *executable.parents):
            if (parent / 'harness/agents.json').is_file(): return initialize(parent)
        if (executable.parent / 'portable.flag').is_file():
            return initialize(executable.parent / 'FishData')
        return initialize(Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'Fish')
    return Path(__file__).resolve().parents[1]


def installed_profile():
    """Recover an installed app's profile independently of its launching shell."""
    if not getattr(sys, 'frozen', False): return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.name.lower() == 'releases' and parent.parent.name.lower() == 'fishapp':
            if (parent.parent / 'location.json').is_file():
                return parent.parent.parent
    return None


def activate_install_profile():
    profile = installed_profile()
    if profile is not None:
        os.environ['LOCALAPPDATA'] = str(profile)
    return profile


def refresh_desktop_entry(root):
    """An installed GUI repairs its stable desktop shortcut after an upgrade."""
    profile = installed_profile()
    if os.name != 'nt' or profile is None: return
    import base64
    import subprocess
    from storage import write_json
    exe = Path(sys.executable).resolve()
    quoted = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = '\n'.join([
        "$ErrorActionPreference = 'Stop'",
        '$entryShell = New-Object -ComObject WScript.Shell',
        "$entryDesktop = [Environment]::GetFolderPath('Desktop')",
        "$entryLink = $entryShell.CreateShortcut((Join-Path $entryDesktop '大肥鱼 Fish.lnk'))",
        '$entryLink.TargetPath = ' + quoted(exe),
        '$entryLink.WorkingDirectory = ' + quoted(root),
        '$entryLink.IconLocation = ' + quoted(str(exe) + ',0'),
        "$entryLink.Description = '大肥鱼 Fish'",
        '$entryLink.Save()',
    ])
    result = {'ok': False, 'exe': str(exe), 'root': str(root), 'profile': str(profile)}
    try:
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                        '-EncodedCommand', base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
                       check=True, capture_output=True, timeout=12,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        result['ok'] = True
    except (OSError, subprocess.SubprocessError) as exc:
        result['error'] = type(exc).__name__
    write_json(project_path(Path(root), 'data/diagnostics/desktop-entry.json'), result)


def location_file():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'FishApp/location.json'


def selected_root():
    path = location_file()
    if not path.is_file(): return None
    return Path(json.loads(path.read_text(encoding='utf-8'))['root']).resolve()


def select_root(root):
    from storage import write_json
    root = Path(root).resolve()
    if not (project_path(root, 'harness/agents.json')).is_file(): raise ValueError('数据目录初始化尚未完成')
    write_json(location_file(), {'root': str(root)})
