"""Offline checks for the unified workbench state surface."""
import json
import tempfile
import unittest
from pathlib import Path

from workbench import Workbench


class WorkbenchStateTests(unittest.TestCase):
    def make_root(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(temp.name)
        (root / 'harness/agents').mkdir(parents=True)
        (root / 'channels/fish_qq').mkdir(parents=True)
        (root / 'channels/fish_qq/config.json').write_text(
            json.dumps({'allowlist': {'owner_id': '200000001', 'group_ids': ['123456789']}}), encoding='utf-8')
        (root / 'harness/agents.json').write_text(json.dumps({'agents': [
            {'id': 'qq', 'name': 'Test', 'persona': 'harness/agents/qq.persona.md'},
        ]}), encoding='utf-8')
        (root / 'harness/agents/qq.persona.md').write_text('test persona', encoding='utf-8')
        return temp, root

    def test_settings_memory_persona_and_channels_share_one_reply(self):
        temp, root = self.make_root()
        self.addCleanup(temp.cleanup)
        workbench = Workbench(root)
        paused = workbench.dispatch('settings.save', {'paused': True})
        self.assertTrue(paused['paused'])
        self.assertTrue(workbench.dispatch('settings.get', {})['paused'])

        saved = workbench.dispatch('memory.save', {'text': '主人喜欢先看结论。', 'category': '偏好'})
        found = workbench.dispatch('memory.search', {'query': '结论'})
        self.assertIn(saved['id'], [row['id'] for row in found])

        persona = workbench.dispatch('personas.save', {'name': '测试人格', 'body': '新的表达。'})
        self.assertIn(persona['id'], [row['id'] for row in workbench.dispatch('personas.list', {})])

        group = workbench.dispatch('channels.update', {
            'group_id': '123456789', 'listen': True, 'reply': False,
        })
        self.assertTrue(group['listen'])
        self.assertFalse(group['reply'])


if __name__ == '__main__':
    unittest.main()
