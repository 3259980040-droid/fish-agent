"""Entry/output adapters: the transport differences each entry really has."""
from .desktop import DesktopAdapter
from .qq import QQAdapter

__all__ = ['DesktopAdapter', 'QQAdapter']
