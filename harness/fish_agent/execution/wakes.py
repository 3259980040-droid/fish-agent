"""Which inputs may start a turn, and what a busy turn keeps for the next one.

This is the one place a trigger is decided. The client's own message and the owner's
QQ private message are immediate; an ordinary group message is only counted, and the
eighth one recorded for that group starts a turn (the 45-minute clock belongs to
06-01 and is added here, not in an entry). While a turn runs, new inputs are only
registered: they stay pending for the next turn instead of interrupting or being
appended to the running one.
"""
import json

# Sources whose new message starts thinking at once: the owner's client message and
# the owner's QQ private message are explicit conversation, not background chatter.
IMMEDIATE_SOURCES = ('desktop', 'qq-private')
GROUP_SOURCE = 'qq-group'
# 普通群消息累计到这个条数才产生一次思考机会；被 @ 或回复她时不等计数。
GROUP_THRESHOLD = 8


class Wakes:
    def __init__(self, runtime):
        self.runtime = runtime

    def payload(self, event):
        try:
            value = json.loads(event.get('payload') or '{}')
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def can_wake(self, event):
        """Whether a just-recorded event asks for a turn right away."""
        if str(event.get('cancelled') or 0) not in ('0', ''):
            return False
        source = str(event.get('source') or '')
        if str(event.get('kind') or 'message') != 'message':
            return True
        if source == GROUP_SOURCE:
            payload = self.payload(event)
            if payload.get('mentions_bot') or payload.get('reply_to_bot'):
                return True
            waiting = self.runtime.state.pending_inputs(source=source, conversation=event.get('conversation'))
            return waiting >= GROUP_THRESHOLD
        return source in IMMEDIATE_SOURCES
