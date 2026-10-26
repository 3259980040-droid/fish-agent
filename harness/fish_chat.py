"""The Fish chat turn as seen by the entries that have not migrated yet.

The model/tool loop itself lives in ``fish_agent.execution.runner`` - one loop for
every entry. This module keeps the QQ entries working while 01-02 moves them onto
the same main execution: it accepts the materials those entries prepared, runs the
one turn, and answers exactly as before. It is a transition adapter, not a second
loop.
"""
import json
from pathlib import Path

from fish_agent.execution.runner import (EMOJI_TOOLS, SEND_SURFACES, SEND_TOOLS,
                                         execute, run_model_loop, turn_says)
from fish_agent.experience.context import (LIBRARY_HEAD, LIBRARY_LIMIT, OWNER_SOCIAL_NOTES,
                                           current_text, library_block, library_references)
from fish_agent.experience.context import social_notes as _social_notes
from fish_agent.tools import TOOL_TIMEOUTS

__all__ = ['respond', 'tool_definitions', 'call_tool', 'TOOL_TIMEOUTS', 'EMOJI_TOOLS',
           'SEND_TOOLS', 'SEND_SURFACES', 'OWNER_SOCIAL_NOTES']


def tool_definitions(role, tool_context, *, root=None, emoji_state=None):
    """The model sees the executor's current catalogue, including enabled MCPs."""
    if not tool_context:
        return []
    from fish_agent import tools
    return tools.definitions(Path(root) if root is not None else Path(__file__).resolve().parent.parent, role)


def call_tool(root, role, session, name, arguments, call_id, timeout):
    """Route non-emoji tools through the same loopback identity/permission gate."""
    from fish_agent import tools
    return tools.execute(root, role, session, name, arguments, call_id, timeout)


def respond(root, role, *, persona, history, history_source, memory, scope,
            references, current, session, cancelled, origin, output='text', emoji_state=None,
            tool_context='', experience=None, delivery_required=False, execution_key=None):
    """Run one turn for an entry that still prepared its own materials.

    ``experience`` installs the common-experience turn: the persona and history of
    this place are read here, the current event is injected once, and an input that
    was already actually considered is not generated again.
    """
    def prepare():
        nonlocal persona, history, history_source, current
        if experience is not None:
            from social_context import SocialContext
            persona = SocialContext(root).active_persona(role)
            experience.persona_revision = persona['id']
            experience.session = session
            history, history_source = experience.prepare(cancelled)
            from experience import was_considered
            from chat_output import TurnSilent
            if was_considered(root, history_source['current_reference'], session=session):
                raise TurnSilent('本消息已在上一轮实际考虑，未重复生成')
            event = dict(history_source['current_event'])
            quote = event.pop('quote', None)
            current = [{'type': 'text', 'text': '[本轮来信] ' + json.dumps(event, ensure_ascii=False) +
                        ('\n引用：' + json.dumps(quote, ensure_ascii=False) if quote else '')}, *current]
        return {'persona': persona, 'history': history, 'history_source': history_source,
                'memory': memory, 'scope': scope, 'references': references, 'current': current,
                'session': session, 'emoji_state': emoji_state, 'tool_context': tool_context,
                'delivery_required': delivery_required, 'experience': experience,
                'trace_reference': getattr(experience, 'reference', '') if experience is not None else ''}

    return execute(root, role, prepare=prepare, origin=origin, cancelled=cancelled, output=output,
                   experience=experience, child=bool(execution_key), execution_key=execution_key)
