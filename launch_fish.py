"""Stable GUI/backend entry for both the source checkout and Fish.exe."""

import sys
import os
from pathlib import Path

if not getattr(sys, 'frozen', False):
    source_root = Path(__file__).resolve().parent
    os.environ.setdefault('FISH_HARNESS_HOME', str(source_root))
    sys.path.insert(0, str(source_root / 'harness'))
    import dependencies
    if sys.stdout is None or sys.stderr is None:
        stream = (source_root / 'harness/source-launch.log').open('a', encoding='utf-8', buffering=1)
        sys.stdout = stream
        sys.stderr = stream

if '--legacy-ui' in sys.argv:
    print('旧 Qt 界面已停用；请使用 DSH 原生 Fish 工作区。', file=sys.stderr)
    raise SystemExit(2)
if '--backend' not in sys.argv and '--screenshot' not in sys.argv:
    from launch_dsh_fish import main
else:
    from fish_desktop.app import main

if __name__ == "__main__":
    raise SystemExit(main())
