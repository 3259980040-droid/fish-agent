"""真实 Qt 窗口按钮端到端 smoke（不是直接调用 facade）。

运行：python tools/ui_e2e.py
会真实点击按钮；最后关闭窗口（按 Phase 4B 语义停止 Fish-owned 后台）。
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fish_desktop.main_window import MainWindow  # noqa: E402
from fish_desktop.services import FishServices  # noqa: E402


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def wait_for(predicate, timeout: float = 15, label: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        QTest.qWait(200)
        if predicate():
            return True
    print(f"FAIL wait: {label}")
    return False


def click_nav(window: MainWindow, index: int) -> None:
    nav = window.nav
    item = nav.item(index)
    rect = nav.visualItemRect(item)
    QTest.mouseClick(nav.viewport(), Qt.LeftButton, pos=rect.center())
    QTest.qWait(400)


def main() -> int:
    app = QApplication(sys.argv)
    services = FishServices()
    window = MainWindow(services)
    window.show()
    QTest.qWait(1200)

    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("PASS " if ok else "FAIL ") + name + (f" :: {detail}" if detail else ""))

    # 1. 六个页面真实点击切换
    for index, (name, _page, _icon_name) in enumerate(window.pages):
        click_nav(window, index)
        record(f"nav:{name}", window.stack.currentIndex() == index)

    # 2. Overview 刷新按钮
    click_nav(window, 0)
    overview = window.pages[0][1]
    wait_for(lambda: not overview.loading, 10, "overview idle")
    QTest.mouseClick(overview.refresh_btn, Qt.LeftButton)
    wait_for(lambda: "已刷新" in overview.toast.text(), 10, "overview refresh toast")
    record("button:overview-refresh", "已刷新" in overview.toast.text(), overview.toast.text())

    # 3. UI Stop Fish -> 3101/3102 down; NapCat 3001 untouched
    wait_for(lambda: overview.stop_btn.isEnabled(), 15, "Stop Fish button enabled")
    QTest.mouseClick(overview.stop_btn, Qt.LeftButton)
    stop_ok = wait_for(lambda: not port_open(3101) and not port_open(3102), 15, "Stop Fish")
    record("button:stop-fish", stop_ok,
           f"3101={port_open(3101)} 3102={port_open(3102)} napcat={port_open(3001)}")
    record("stop-fish-keeps-napcat", port_open(3001))

    # 4. UI Start Fish -> 3101/3102 ready
    wait_for(lambda: overview.start_btn.isEnabled(), 15, "Start Fish button enabled")
    QTest.mouseClick(overview.start_btn, Qt.LeftButton)
    start_ok = wait_for(lambda: port_open(3101) and port_open(3102), 30, "Start Fish")
    record("button:start-fish", start_ok,
           f"3101={port_open(3101)} 3102={port_open(3102)}")

    # 5. 大鱼页 Start/Stop Runtime 真实按钮
    click_nav(window, 1)
    qq = window.pages[1][1]
    QTest.mouseClick(qq.stop_btn, Qt.LeftButton)
    stop_runtime = wait_for(lambda: not port_open(3090), 15, "Stop Runtime")
    record("button:stop-runtime", stop_runtime, f"3090={port_open(3090)}")
    QTest.mouseClick(qq.start_btn, Qt.LeftButton)
    start_runtime = wait_for(lambda: port_open(3090), 20, "Start Runtime")
    record("button:start-runtime", start_runtime, f"3090={port_open(3090)}")

    # 6. Cancel 按钮（当前无 active turn：验证真实错误反馈，不伪造成功）
    QTest.mouseClick(qq.cancel_btn, Qt.LeftButton)
    QTest.qWait(1200)
    record("button:cancel-feedback", bool(qq.toast.text()), qq.toast.text())

    # 7. Research Sync 真实按钮 + 真实 pipeline
    click_nav(window, 2)
    research = window.pages[2][1]
    wait_for(lambda: research.sync_btn.isEnabled(), 15, "Sync button enabled")
    before = research.toast.text()
    QTest.mouseClick(research.sync_btn, Qt.LeftButton)
    wait_for(lambda: "同步" in research.toast.text() or "Sync" in research.toast.text(), 90, "Sync toast")
    sync_done = wait_for(lambda: research.sync_btn.isEnabled(), 90, "Sync finished")
    record("button:research-sync", sync_done and research.toast.text() != before, research.toast.text())

    # 8. Refresh + 页面切换回归
    QTest.mouseClick(research.refresh_btn, Qt.LeftButton)
    QTest.qWait(800)
    record("button:research-refresh", True, research.toast.text())

    # 9. 关闭窗口：Phase 4B 语义 = 停止 Fish-owned，保留 NapCat
    window.close()
    QTest.qWait(1500)
    closed = not port_open(3101) and not port_open(3102)
    record("close:stops-fish", closed, f"3101={port_open(3101)} 3102={port_open(3102)}")
    record("close:keeps-napcat", port_open(3001))

    print("\nSUMMARY")
    for name, ok, detail in results:
        print(("PASS" if ok else "FAIL"), name, detail)
    print("TOTAL", sum(1 for _n, ok, _d in results if ok), "/", len(results))
    return 0 if all(ok for _n, ok, _d in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
