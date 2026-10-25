"""Which inputs may start a turn, and what a busy turn keeps for the next one.

This is the one place a trigger is decided. The client's own message (and, once
01-02 lands, the owner's private message) is an immediate trigger; ordinary group
messages and the time-based triggers are added here by 01-03/06-01. While a turn
runs, new inputs are only registered: they stay pending for the next turn instead
of interrupting or being appended to the running one.
"""

# Sources whose new message starts thinking at once: the owner's client message and
# the owner's QQ private message are explicit conversation, not background chatter.
IMMEDIATE_SOURCES = ('desktop', 'qq-private')


class Wakes:
    def __init__(self, runtime):
        self.runtime = runtime

    def can_wake(self, event):
        """Whether a just-recorded event asks for a turn right away."""
        if str(event.get('cancelled') or 0) not in ('0', ''):
            return False
        if str(event.get('kind') or 'message') != 'message':
            return True
        return str(event.get('source') or '') in IMMEDIATE_SOURCES
