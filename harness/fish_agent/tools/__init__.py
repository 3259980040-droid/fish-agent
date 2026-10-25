"""Tool execution: one catalogue, one executor, and the reply queue it feeds.

Every tool the model may call is discovered from the tool host's own registry and
executed through the same loopback identity/permission gate. There is no second
list of names on any entry: a surface that publishes no tools simply has none, and
one that publishes them gets the whole catalogue. The reply queue (``chat_says``)
belongs here too because it is the result channel of the sending tools.
"""
from __future__ import annotations

from pathlib import Path

# A call may not outlive its own work; the turn's remaining budget still wins.
TOOL_TIMEOUTS = {'fish_capabilities': 20, 'fish_group_list': 20, 'fish_group_history': 30,
                 'fish_browser': 90, 'fish_computer': 30, 'fish_qq_action': 55,
                 'fish_web_search': 40, 'fish_web_fetch': 40, 'fish_emoji_save': 45,
                 # 一次看图是一次下载加一次视觉模型调用：最多两张，按两张的预算给。
                 'fish_image_look': 90}


def catalog(root, role):
    """The tool host's current catalogue, including enabled MCP entries."""
    from tool_host import request
    return request(Path(root), '/catalog', role=role, admin=False)['tools']


def definitions(root, role):
    """The catalogue in the chat-completions function shape the model receives."""
    return [{'type': 'function', 'function': {key: item[key] for key in ('name', 'description', 'parameters')}}
            for item in catalog(root, role)]


def execute(root, role, session, name, arguments, call_id, timeout):
    """Run one published tool through the same loopback identity and permission gate."""
    from tool_host import request
    return request(Path(root), '/execute',
                   {'session': session, 'name': name, 'arguments': arguments, 'call_id': call_id},
                   role=role, admin=False, timeout=timeout)


def surface(root, session, role='qq'):
    """Which real sending surface this session has, or '' when it has no sender."""
    from tool_host import sender_surface
    return sender_surface(Path(root), session, role)


def say_queue(root, session):
    """What this turn has queued to say, oldest first; reading keeps it."""
    from tool_host import peek_says
    return peek_says(Path(root), session)


def take_says(root, session):
    """Hand the turn's queue to its one sender, once."""
    from tool_host import take_says as take
    return take(Path(root), session)


def clear_says(root, session):
    from tool_host import clear_says as clear
    clear(Path(root), session)


def note_preview(root, session, asset):
    from tool_host import note_preview as note
    note(Path(root), session, asset)


class ToolsModule:
    """The module as the assembly holds it: the same catalogue, bound to one root."""

    def __init__(self, runtime):
        self.root = runtime.root

    def catalog(self, role='qq'):
        return catalog(self.root, role)

    def execute(self, role, session, name, arguments, call_id, timeout):
        return execute(self.root, role, session, name, arguments, call_id, timeout)
