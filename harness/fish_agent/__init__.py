"""One Fish agent: one assembly and one main execution for every entry.

The background host builds this package once (``bootstrap.get_runtime``) and holds
its lifecycle. Entries hand in standard inputs through ``ExecutionModule.submit``;
they never run a model turn of their own. The seven public modules are reached
through this package, not through page-specific code.
"""
from .bootstrap import FishRuntime, get_runtime, reset_runtime

__all__ = ['FishRuntime', 'get_runtime', 'reset_runtime']
