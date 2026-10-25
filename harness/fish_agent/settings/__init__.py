"""Runtime settings: which persona, expression and model a turn actually uses.

Saving and applying presets is the settings owner's job (tickets 09-01/09-03); this
module already resolves the snapshot a model request reads, so no entry rebuilds a
persona or a model parameter list on its own. A snapshot is read at the start of a
request; a later edit does not rewrite a request that was already sent.
"""
from __future__ import annotations

from pathlib import Path


class SettingsModule:
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = Path(runtime.root)

    def snapshot(self, role='qq'):
        """The values the next model request reads, with where each one came from."""
        from core import HarnessConfig
        from group_reply_style import expression_policy
        from model_gateway import chat_options
        from social_context import SocialContext
        contexts = SocialContext(self.root)
        persona = contexts.active_persona(role) if role == 'qq' else contexts.ensure_default(role)
        agent = HarnessConfig(self.root).agent(role)
        expression = expression_policy()
        return {'role': role,
                'persona': {'id': persona['id'], 'name': persona.get('name', ''), 'body': persona['body'],
                            'source': 'SocialContext.active_persona'},
                'expression': {'source': expression['source'], 'min_chars': expression['min_chars'],
                               'max_chars': expression['max_chars']},
                'model': {'name': getattr(agent, 'model', '') if agent else '',
                          'provider': getattr(agent, 'base_url', '') if agent else '',
                          'source': 'harness/agents.json'},
                'options': chat_options(self.root)}
