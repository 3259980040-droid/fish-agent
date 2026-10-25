"""The client's own records, reached through one adapter.

The client half of an input/output is the desktop conversation store: it owns the
inbound row, its imported attachments and the outbound bubbles the page reads. This
adapter is the only place the runtime touches those tables; the page keeps using the
same records it always did.
"""
from __future__ import annotations

from pathlib import Path


class DesktopAdapter:
    def __init__(self, root):
        self.root = Path(root)
        self._chat = None

    def chat(self):
        if self._chat is None:
            # Imported here: desktop_chat reaches back into fish_agent only when its
            # legacy send() shim submits a turn, so the two modules stay importable.
            from desktop_chat import DesktopChat
            self._chat = DesktopChat(self.root, 'qq', None)
        return self._chat

    @property
    def path(self):
        return self.chat().db

    def inbound(self, conversation, message):
        return self.chat().inbound(conversation, message)

    def status(self, message, status, error=''):
        self.chat().status(message, status, error)

    def update_segments(self, message, segments):
        self.chat().update_segments(message, segments)

    def conversation_context(self, conversation):
        return self.chat().conversation_context(conversation)

    def mode(self, conversation):
        return self.chat().mode(conversation)

    def commit_reply(self, conversation, message, bubbles):
        self.chat().commit_reply(conversation, message, bubbles)
