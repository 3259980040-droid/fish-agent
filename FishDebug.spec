# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve()
hiddenimports = [
    "core", "qq_store", "research_store", "dsh_web", "qq_agent_loop",
    "bilibili_source", "research_pipeline",
]
hiddenimports += collect_submodules("channels.fish_qq")
a = Analysis(
    [str(ROOT / "launch_fish.py")],
    pathex=[str(ROOT), str(ROOT / "harness")],
    binaries=[],
    datas=[(str(ROOT / "fish_desktop" / "assets"), "fish_desktop/assets")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "rthook_pyside.py")],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
a.binaries = [item for item in a.binaries if Path(item[0]).name.lower() != "icuuc.dll"]
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="FishDebug",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name="FishDebug")
