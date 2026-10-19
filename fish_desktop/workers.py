"""Qt 异步任务：Python 线程池 + UI 线程定时排空结果队列。

不跨线程 emit Qt Signal，避免 QRunnable/QObject 生命周期导致的
“Signal source has been deleted”；所有回调只在 UI 线程执行。
"""

from __future__ import annotations

import itertools
import queue
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QObject, QTimer


class Runner(QObject):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fish-ui")
        self.results: queue.Queue[tuple[str, int, Any]] = queue.Queue()
        self._ids = itertools.count(1)
        self.closed = False
        self._callbacks: dict[int, tuple[Callable[[Any], None], Callable[[str], None] | None,
                                       Callable[[], None] | None]] = {}
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._drain)
        self.timer.start()

    def submit(
        self,
        fn: Callable[[], Any],
        on_result: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if self.closed:
            return
        job_id = next(self._ids)
        self._callbacks[job_id] = (on_result, on_error, on_done)
        self.executor.submit(self._work, job_id, fn)

    def _work(self, job_id: int, fn: Callable[[], Any]) -> None:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001
            self.results.put(("error", job_id, f"{type(exc).__name__}: {exc}"))
        else:
            self.results.put(("result", job_id, value))
        finally:
            self.results.put(("done", job_id, None))

    def _drain(self) -> None:
        while True:
            try:
                kind, job_id, value = self.results.get_nowait()
            except queue.Empty:
                return
            callback = self._callbacks.get(job_id)
            if callback is None:
                continue
            if kind == "result":
                callback[0](value)
            elif kind == "error":
                if callback[1] is not None:
                    callback[1](str(value))
            elif kind == "done":
                self._callbacks.pop(job_id, None)
                if callback[2] is not None:
                    callback[2]()

    def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        self._callbacks.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)
