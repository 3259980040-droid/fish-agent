"""入口：python -m fish_desktop.app

--screenshot DIR 用 offscreen Qt 生成各页面截图，便于验收。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
_SINGLE_MUTEX = None


def _acquire_single_instance() -> bool:
    """Windows 命名 mutex；进程退出/崩溃时由 OS 自动释放。"""
    global _SINGLE_MUTEX
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, "FishDesktop-4A-SingleInstance")
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _SINGLE_MUTEX = handle
        return True
    except Exception:
        return True


def _run_backend() -> int | None:
    """同一个 Fish.exe 也可作为 fish-qq / qq-agent-loop 后端进程。"""
    argv = sys.argv[1:]
    if "--backend" not in argv:
        return None
    index = argv.index("--backend")
    if index + 1 >= len(argv):
        return 2
    kind = argv[index + 1]
    rest = argv[:index] + argv[index + 2:]
    root = Path(os.environ.get("FISH_HARNESS_HOME", str(Path(__file__).resolve().parent.parent)))
    if getattr(sys, "frozen", False) or sys.stdout is None or sys.stderr is None:
        log = root / "harness" / "backend-bootstrap.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        stream = log.open("a", encoding="utf-8", buffering=1)
        sys.stdout = stream
        sys.stderr = stream
        print("Fish backend bootstrap:", kind)
    os.chdir(root)
    if not getattr(sys, 'frozen', False):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))
    if kind == "fish-qq":
        sys.argv = [sys.argv[0], *rest]
        from channels.fish_qq.__main__ import main as backend_main
        return int(backend_main())
    if kind == "qq-agent-loop":
        sys.argv = [sys.argv[0]]
        from qq_agent_loop import main as backend_main
        return int(backend_main())
    if kind == 'maintenance':
        parser = argparse.ArgumentParser(); parser.add_argument('--request', required=True)
        request = parser.parse_args(rest)
        from updater import run_maintenance
        return int(run_maintenance(root, request.request))
    if kind == 'research-job':
        parser = argparse.ArgumentParser(); parser.add_argument('--id', required=True)
        options = parser.parse_args(rest)
        from research_autonomy import ResearchWorker
        from storage import database
        worker = ResearchWorker(root)
        with database(worker.control.path) as db:
            job = db.execute('SELECT * FROM jobs WHERE id=?', (options.id,)).fetchone()
        if not job or job['status'] != 'running': return 2
        return 0 if worker.process(dict(job)).get('ok') else 1
    if kind == 'diagnose':
        parser = argparse.ArgumentParser(); parser.add_argument('--check-runtime', action='store_true'); parser.add_argument('--id', default='research', choices=['qq', 'research'])
        options = parser.parse_args(rest)
        from .bootstrap import initialize
        initialize(root)
        import dependencies
        import mcp, bilibili_api, cryptography, qrcode, regex
        from core import HarnessConfig, RuntimeManager
        from storage import write_json
        from updater import VERSION
        manager = RuntimeManager(HarnessConfig(root))
        result = {'ok': True, 'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
            'sdk_imports': ['mcp', 'bilibili_api', 'cryptography', 'qrcode', 'regex'], 'model_calls': 0, 'real_qq_sends': 0}
        try:
            result['runtime_version'] = manager._version()
            if options.check_runtime:
                from runtime_adapter import DSHRuntimeAdapter
                client = DSHRuntimeAdapter(root, options.id, manager).connect()
                result['authenticated_runtime'] = bool(client.origin)
        except Exception as exc:
            result.update(ok=False, error=type(exc).__name__ + ': ' + str(exc)[:300])
        finally:
            import re
            runtime_lines = '\n'.join(manager.logs.get(options.id, []))
            result['runtime_startup_warning_codes'] = sorted(set(re.findall(r'\b(?:EPERM|EACCES|ENOENT|ERR_[A-Z_]+)\b', runtime_lines)))
            manager.stop_all()
            from tool_host import shutdown_host
            shutdown_host(root)
        write_json(root / 'data/diagnostics/runtime-check.json', result)
        return 0 if result['ok'] else 1
    print(f"unknown backend: {kind}", file=sys.stderr)
    return 2


def main() -> int:
    try:
        backend_result = _run_backend()
    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    if backend_result is not None:
        return backend_result
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot", default="")
    parser.add_argument("--page", default="")
    parser.add_argument("--ui-diagnostics", default="")
    args = parser.parse_args()

    if args.screenshot or args.ui_diagnostics:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication, QLabel, QWidget

    from .main_window import MainWindow
    from .services import FishServices

    app = QApplication(sys.argv)
    if not (args.ui_diagnostics or args.screenshot) and not _acquire_single_instance():
        print("大鱼 Fish 已经在运行。")
        return 0
    for font_path in (
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "simhei.ttf",
    ):
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont("Microsoft YaHei UI", 9))
    app.setApplicationName("大鱼 Fish")
    services = FishServices()
    window = MainWindow(services, manage_shutdown=not (args.screenshot or args.ui_diagnostics))
    window.show()
    if not (args.screenshot or args.ui_diagnostics):
        from .setup_wizard import SetupWizard
        from storage import read_json
        if read_json(services.root / 'harness/setup.json', {'completed': True}).get('completed') is False:
            SetupWizard(services, window).exec()
        maintenance_result = os.environ.pop('FISH_MAINTENANCE_RESULT', '')
        if maintenance_result:
            from PySide6.QtWidgets import QMessageBox
            try:
                result = read_json(Path(maintenance_result))
                if result.get('ok'):
                    # Older upgrade helpers pass the root only in the child
                    # environment. Persist it in the newly installed GUI so a
                    # later manual launch returns to these same personal data.
                    from .bootstrap import select_root
                    select_root(services.root)
                    QMessageBox.information(window, '维护完成', '操作已完成。当前数据目录：\n' + str(services.root))
                else: QMessageBox.warning(window, '维护未完成', str(result.get('error', '请检查维护记录')))
            except (OSError, ValueError):
                QMessageBox.warning(window, '维护结果', '维护记录无法读取，请查看备份与升级页面。')
        services.start_background()

    def pump(ms: int) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    if args.screenshot:
        out = Path(args.screenshot)
        out.mkdir(parents=True, exist_ok=True)
        pump(900)
        for index, (name, page, _icon_name) in enumerate(window.pages):
            window.switch_page(index)
            page.refresh()
            pump(1100)
            page.refresh()
            pump(700)
            filename = f"{page.__class__.__name__.replace('Page', '').lower()}.png"
            window.grab().save(str(out / filename))
            from PySide6.QtWidgets import QTabWidget
            tabs = getattr(page, 'tabs', None)
            if isinstance(tabs, QTabWidget):
                for tab_index in range(tabs.count()):
                    tabs.setCurrentIndex(tab_index); page.refresh(); pump(600)
                    window.grab().save(str(out / (filename[:-4] + '-tab-' + str(tab_index) + '.png')))
                    selected = tabs.currentWidget()
                    from PySide6.QtWidgets import QScrollArea
                    areas = ([selected] if isinstance(selected, QScrollArea) else []) + selected.findChildren(QScrollArea)
                    for area_index, area in enumerate(areas):
                        if area.isVisible() and area.verticalScrollBar().maximum() > 0:
                            bar = area.verticalScrollBar(); previous = bar.value(); bar.setValue(bar.maximum()); pump(150)
                            window.grab().save(str(out / (filename[:-4] + f'-tab-{tab_index}-scroll-{area_index}.png')))
                            bar.setValue(previous)
        print(str(out))
        return 0

    if args.ui_diagnostics:
        import hashlib
        import fish_desktop.pages as pages_module
        import fish_desktop.theme.qss as qss_module

        out = Path(args.ui_diagnostics)
        out.parent.mkdir(parents=True, exist_ok=True)
        overview = window.pages[0][1]
        lines = [
            f"executable={sys.executable}",
            f"frozen={getattr(sys, 'frozen', False)}",
            f"window_title={window.windowTitle()}",
            f"pages_module={pages_module.__file__}",
            f"qss_module={qss_module.__file__}",
            f"qss_len={len(qss_module.QSS)}",
            f"qss_sha256={hashlib.sha256(qss_module.QSS.encode('utf-8')).hexdigest()}",
            "nav=" + " | ".join(name for name, _page, _icon in window.pages),
            "brand_labels=" + " | ".join(
                label.text() for label in window.findChildren(QLabel)
                if label.objectName() in ("BrandName", "BrandSub")
            ),
            "overview_widgets:",
        ]
        for widget in overview.findChildren(QWidget):
            name = widget.objectName()
            if name and not name.startswith("qt_"):
                lines.append(f"  {widget.__class__.__name__} #{name}")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(str(out))
        return 0

    if args.page:
        for index, (name, _page, _icon) in enumerate(window.pages):
            if name == args.page or str(index) == args.page:
                window.switch_page(index)
                break
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
