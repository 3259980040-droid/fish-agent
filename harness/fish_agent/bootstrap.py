"""The one assembly: build the agent's modules once and keep their lifecycle.

The background host calls ``get_runtime`` at startup and ``start``/``stop`` around
its own lifecycle; every entry then reaches the same modules. Nothing here is a new
process, database or work root - it is the existing Fish source getting one owner
for state that used to be re-created per entry.
"""
from __future__ import annotations

import threading
from pathlib import Path

_RUNTIMES = {}
_LOCK = threading.Lock()


class FishRuntime:
    def __init__(self, root):
        self.root = Path(root).resolve()
        from .adapters.desktop import DesktopAdapter
        from .adapters.qq import QQAdapter
        from .delivery import DeliveryModule
        from .execution import AgentState, ExecutionModule
        from .experience import ExperienceModule
        from .plans import PlansModule
        from .settings import SettingsModule
        from .tools import ToolsModule
        self.state = AgentState(self.root)
        self.client = DesktopAdapter(self.root)
        self.client_qq = QQAdapter(self.root)
        self.experience = ExperienceModule(self)
        self.settings = SettingsModule(self)
        self.tools = ToolsModule(self)
        self.plans = PlansModule(self)
        self.delivery = DeliveryModule(self)
        self.execution = ExecutionModule(self)
        self._started = False

    def start(self):
        """Start the one consumer. Called by the background host, not by entries."""
        if self._started:
            return False
        self.state.fail_stale()
        self.execution.start()
        self._started = True
        return True

    def stop(self):
        if not self._started:
            return False
        self.execution.stop()
        self._started = False
        return True

    @property
    def started(self):
        return self._started


def get_runtime(root):
    """The one assembly for this Fish source root."""
    key = str(Path(root).resolve())
    with _LOCK:
        runtime = _RUNTIMES.get(key)
        if runtime is None:
            runtime = _RUNTIMES[key] = FishRuntime(root)
        return runtime


def reset_runtime(root=None):
    """Drop the assembly (tests and shutdown only); stops the consumer if running."""
    with _LOCK:
        if root is None:
            runtimes = list(_RUNTIMES.values())
            _RUNTIMES.clear()
        else:
            runtime = _RUNTIMES.pop(str(Path(root).resolve()), None)
            runtimes = [runtime] if runtime is not None else []
    for runtime in runtimes:
        try:
            runtime.stop()
        except Exception:
            pass
    return len(runtimes)
