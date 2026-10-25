"""The one assembly: build the agent's modules once and keep their lifecycle.

The background host calls ``get_runtime`` at startup and ``start``/``stop`` around
its own lifecycle; every entry reaches the modules of its explicit instance.
Code, credentials and transport stay on the host; each instance owns existing
store implementations and its one consumer, without copying the program.
"""
from __future__ import annotations

import threading
from pathlib import Path

_RUNTIMES = {}
_LOCK = threading.Lock()


class FishRuntime:
    def __init__(self, root):
        from .instances import instance_context
        self.context = instance_context(root)
        self.instance_id = self.context.instance_id
        self.host_root = self.context.host_root
        self.root = self.context.root
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
    """The one assembly for this explicit Fish instance."""
    from .instances import instance_context
    context = instance_context(root)
    key = str(context.root)
    with _LOCK:
        runtime = _RUNTIMES.get(key)
        if runtime is None:
            runtime = _RUNTIMES[key] = FishRuntime(context)
        return runtime


def reset_runtime(root=None):
    """Drop the assembly (tests and shutdown only); stops the consumer if running."""
    with _LOCK:
        if root is None:
            runtimes = list(_RUNTIMES.values())
            _RUNTIMES.clear()
        else:
            from .instances import instance_context
            runtime = _RUNTIMES.pop(str(instance_context(root).root), None)
            runtimes = [runtime] if runtime is not None else []
    for runtime in runtimes:
        try:
            runtime.stop()
        except Exception:
            pass
    return len(runtimes)
