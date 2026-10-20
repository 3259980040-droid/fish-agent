"""Offline checks for persona import, owner selection and history preservation."""
import tempfile
import unittest
from pathlib import Path

from memory_store import MemoryStore
from persona_library import PersonaLibrary
from social_context import SocialContext


class PersonaSelectionTests(unittest.TestCase):
    def test_apply_keeps_history_and_switches_revision(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            (root / 'harness/agents').mkdir(parents=True)
            (root / 'channels/fish_qq').mkdir(parents=True)
            (root / 'channels/fish_qq/config.json').write_text(
                '{"allowlist":{"owner_id":"200000001"}}', encoding='utf-8')
            (root / 'harness/agents.json').write_text(
                '{"agents":[{"id":"qq","name":"Test","persona":"harness/agents/qq.persona.md"}]}',
                encoding='utf-8')
            (root / 'harness/agents/qq.persona.md').write_text('base persona', encoding='utf-8')

            contexts = SocialContext(root)
            base = contexts.ensure_default('qq')
            owner_space = contexts.create_space('qq', 'owner', revision=base['id'], origin='owner-qq')
            contexts.select_owner_space(owner_space['id'], '200000001')
            memory = MemoryStore(root, 'qq')
            fact = memory.save('主人喜欢先看结论。', scope='owner', source='manual')

            library = PersonaLibrary(root)
            selected = library.add('新人格', '新的表达方式。', source='test')
            result = library.apply(selected['id'])

            self.assertEqual(result['revision'], selected['id'])
            self.assertEqual(contexts.space(owner_space['id'])['revision'], selected['id'])
            self.assertIn('新的表达方式', contexts.prompt(owner_space['id']))
            self.assertEqual(contexts.persona(base['id'])['body'], 'base persona')
            self.assertIn(fact['id'], [row['id'] for row in memory.search('结论', scope='owner')])


if __name__ == '__main__':
    unittest.main()
