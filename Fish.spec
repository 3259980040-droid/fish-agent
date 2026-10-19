# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys
import site
import shutil

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve()
VENDOR = ROOT / 'vendor/python'
sys.path.insert(0, str(VENDOR)); site.addsitedir(str(VENDOR))

hiddenimports = [
    "core", "qq_store", "research_store", "dsh_web", "qq_agent_loop",
    "bilibili_source", "research_pipeline",
    "multimodal", "secrets_store",
    "governance", "harness.governance", "harness.guard", "desktop_chat", "runtime_adapter",
    'storage', 'harness.storage', 'dependencies', 'harness.dependencies', 'guard', 'emoji_store', 'harness.emoji_store',
    'memory_store', 'model_gateway', 'plugin_registry', 'plugin_media', 'tool_host', 'research_autonomy',
    'social_context', 'request_inspector', 'owner_tests', 'owner_reply', 'group_policy', 'group_workflow',
    'social_roster', 'social_members',
    'bili_login', 'backup', 'setup_config', 'updater', 'process_tools', 'network_config', 'regex',
    'mcp', 'mcp.client.stdio', 'mcp.client.sse', 'mcp.client.streamable_http',
    'bilibili_api', 'bilibili_api.clients.HTTPXClient', 'bilibili_api.clients.AioHTTPClient',
    'cryptography.hazmat.primitives.ciphers.aead', 'cryptography.hazmat.primitives.kdf.scrypt', 'qrcode',
]
hiddenimports += collect_submodules("channels.fish_qq", filter=lambda name: ".tests" not in name and not name.endswith(".real_check"))

a = Analysis(
    [str(ROOT / "launch_fish.py")],
    pathex=[str(ROOT), str(ROOT / "harness"), str(VENDOR), str(VENDOR / 'win32'), str(VENDOR / 'win32/lib')],
    binaries=[],
    datas=[(str(ROOT / "fish_desktop" / "assets"), "fish_desktop/assets"),
           (str(ROOT / "harness" / "safety"), "harness/safety"),
           (str(ROOT / 'harness/runtime/fish-control'), 'harness/runtime/fish-control'),
           (str(ROOT / 'packaging/defaults'), 'packaging/defaults'),
           (str(ROOT / 'packaging/licenses'), 'licenses'),
           (str(ROOT / 'THIRD-PARTY.md'), '.'),
           (str(ROOT / 'packaging/build-source.json'), '.'),
           (str(VENDOR / 'bilibili_api/data'), 'bilibili_api/data'),
           *copy_metadata('mcp'), *copy_metadata('bilibili-api-python')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "rthook_pyside.py")],
    excludes=["numpy", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
# Qt's bundled ICU 78 conflicts with the Windows system ICU on this host;
# let Qt6Core use the system DLL (verified with the packaged exe).
a.binaries = [item for item in a.binaries if Path(item[0]).name.lower() != "icuuc.dll"]
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Fish",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Fish",
)
# Node packages are opaque runtime resources. Copy the complete locked closure
# after Python analysis instead of attempting to classify 70k JS/data files as
# Python DLL dependencies. Relative native module paths and licenses are kept.
runtime_target = Path(DISTPATH) / 'Fish/_internal/runtime'
runtime_staged = Path(DISTPATH) / 'runtime-staged'
if runtime_staged.is_dir() and (runtime_staged / 'manifest.json').read_bytes() == (ROOT / 'vendor/runtime/manifest.json').read_bytes():
    runtime_staged.rename(runtime_target)
else:
    shutil.copytree(ROOT / 'vendor/runtime', runtime_target, dirs_exist_ok=True)
