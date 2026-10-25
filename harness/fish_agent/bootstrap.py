"""The one assembly: build the agent's modules once and keep their lifecycle.

The background host calls ``get_runtime`` at startup and ``start``/``stop`` around
its own lifecycle; every entry reaches the modules of its explicit instance.
Code, credentials and transport stay on the host; each instance owns existing
store implementations and its one consumer, without copying the program.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

_RUNTIMES = {}
_LOCK = threading.RLock()


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
        self._created_at=time.time()
        self._recovered=False
        self.client = DesktopAdapter(self.root)
        self.client_qq = QQAdapter(self.root)
        self.experience = ExperienceModule(self)
        self.settings = SettingsModule(self)
        self.tools = ToolsModule(self)
        self.plans = PlansModule(self)
        self.delivery = DeliveryModule(self)
        self.execution = ExecutionModule(self)
        self._started = False
        self.lifecycle_stop = threading.Event()
        self._lifecycle_lock = threading.RLock()

    def start(self,*,resume=True):
        """Run this enabled instance only; no account connection is created here."""
        with self._lifecycle_lock:
            if not resume and self.lifecycle_stop.is_set():return False
            from governance import Governance
            if Governance(self.root,'qq').config()['paused']:return False
            from social_context import SocialContext
            SocialContext(self.root).active_persona('qq')
            if self.started:return False
            # A previous cancelled generation must leave before another consumer starts.
            if self.execution._thread is not None and self.execution._thread.is_alive():return False
            for name in ('memory_thread','scheduler_thread'):
                previous=getattr(self,name,None)
                if previous is not None and previous.is_alive():return False
            self.lifecycle_stop=threading.Event()
            if not self._recovered:
                self.state.fail_stale(before=self._created_at)
                self._recovered=True
            self.execution.start()
            from memory_maintenance import start_background
            self.memory_thread=start_background(self,stop=self.lifecycle_stop)
            self._started=True
            from agent_scheduler import AgentScheduler
            self.scheduler=AgentScheduler(self.root,self.lifecycle_stop)
            self.scheduler.runtime=self
            self.scheduler_thread=threading.Thread(target=self.scheduler.loop,daemon=True,name='fish-scheduler-'+self.instance_id)
            self.scheduler_thread.start()
            return True

    def stop(self):
        with self._lifecycle_lock:
            self.lifecycle_stop.set()
            was_running=self._started
            self.execution.stop()
            for name in ('memory_thread','scheduler_thread'):
                thread=getattr(self,name,None)
                if thread is not None and thread is not threading.current_thread():thread.join(timeout=1)
            self._started=False
            return was_running

    @property
    def started(self):
        thread=self.execution._thread
        return bool(self._started and thread is not None and thread.is_alive() and not self.execution._stop.is_set())


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


def loaded_runtime(root):
    from .instances import instance_context
    with _LOCK:return _RUNTIMES.get(str(instance_context(root).root))


def start_enabled(root):
    """Host startup/reconciliation uses the persisted setting of every instance."""
    from .instances import InstanceCatalog
    from social_context import PersonaNotConfigured
    catalog=InstanceCatalog(root)
    for row in catalog.records():
        runtime=get_runtime(catalog.resolve(row['id']))
        try:runtime.start()
        except PersonaNotConfigured:pass


def stop_host_runtimes(root):
    from locations import host_root
    host=host_root(root)
    with _LOCK:runtimes=[value for value in _RUNTIMES.values() if value.host_root==host]
    for runtime in runtimes:runtime.stop()
