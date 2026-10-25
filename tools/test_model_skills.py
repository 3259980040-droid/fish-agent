"""09-03/09-05 backend: the one model source and real skills, no model calls."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]

import model_connections
import skills_store
from core import HarnessConfig
from model_gateway import ModelGateway


def write_agents(root, *, model='deepseek-flash', base='https://api.deepseek.com', env='FISH_QQ_KEY'):
    (root / 'harness').mkdir(parents=True, exist_ok=True)
    (root / 'harness/agents.json').write_text(json.dumps({
        'agents': [{'id': 'qq', 'name': 'Fish', 'model': model, 'base_url': base,
                    'apiKeyEnv': env, 'persona': 'harness/agents/qq.persona.md'},
                   {'id': 'research', 'name': 'Fish Research', 'model': 'deepseek-flash',
                    'base_url': base, 'apiKeyEnv': 'FISH_RESEARCH_KEY'}],
        'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0},
    }, ensure_ascii=False, indent=2), encoding='utf-8')


class ModelConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_agents(self.root)

    def test_save_writes_the_one_source_the_gateway_reads(self):
        described = model_connections.describe(self.root)
        self.assertEqual(described['connections'][0]['model'], 'deepseek-flash')
        self.assertFalse(described['connections'][0]['has_key'])
        saved = model_connections.save(self.root, role='qq', model='deepseek-chat',
                                       base='https://api.example.com/', key='sk-test-123', name='主连接')
        self.assertEqual(saved['connection']['model'], 'deepseek-chat')
        self.assertEqual(saved['connection']['base'], 'https://api.example.com')
        self.assertTrue(saved['connection']['has_key'])
        self.assertIn('chat', saved['applied_uses'])
        self.assertIn('memory', saved['applied_uses'])
        agent = HarnessConfig(self.root).agent('qq')
        self.assertEqual(agent.model, 'deepseek-chat')
        self.assertEqual(agent.base_url, 'https://api.example.com')
        gateway = ModelGateway(self.root, 'qq')
        self.assertEqual(gateway.model, 'deepseek-chat')
        self.assertEqual(gateway.base, 'https://api.example.com')
        self.assertEqual(gateway.key, 'sk-test-123')
        # The page never echoes the key back in its own reads.
        self.assertNotIn('sk-test-123', json.dumps(model_connections.describe(self.root), ensure_ascii=False))

    def test_bad_address_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            model_connections.save(self.root, role='qq', model='m', base='api.example.com')
        self.assertEqual(HarnessConfig(self.root).agent('qq').model, 'deepseek-flash')

    def test_remove_leaves_the_use_unconfigured_instead_of_falling_back(self):
        model_connections.save(self.root, role='qq', model='deepseek-chat',
                               base='https://api.example.com', key='sk-test-123')
        removed = model_connections.remove(self.root, role='qq')
        self.assertTrue(removed['removed'])
        self.assertFalse(removed['ready'])
        self.assertIn('不会回退', removed['note'])
        described = model_connections.describe(self.root)
        self.assertFalse(described['connections'][0]['has_key'])
        # The model field is still the saved one; only the credential is gone, and the
        # next request fails with a real missing-key reason.
        self.assertEqual(described['connections'][0]['model'], 'deepseek-chat')

    def test_switching_clears_the_exact_window_cache_entry(self):
        cache = self.root / 'data/social/model-metadata.json'
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({'https://api.example.com|deepseek-chat': {'window': 1},
                                     'https://other.example|m': {'window': 2}}), encoding='utf-8')
        model_connections.save(self.root, role='qq', model='deepseek-chat',
                               base='https://api.example.com', key='sk-test-123')
        left = json.loads(cache.read_text(encoding='utf-8'))
        self.assertNotIn('https://api.example.com|deepseek-chat', left)
        self.assertIn('https://other.example|m', left)

    def test_uses_that_are_not_connected_say_so(self):
        uses = {item['use']: item for item in model_connections.describe(self.root)['uses']}
        self.assertFalse(uses['vision']['supported'])
        self.assertFalse(uses['vision']['ready'])
        self.assertIn('不冒充', uses['vision']['reason'])
        self.assertFalse(uses['chat']['ready'])  # no credential saved yet


class SkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_save_read_disable_and_delete(self):
        saved = skills_store.save(self.root, name='weekly-report', description='写周报时用',
                                  body='第一步：读本周任务。\n第二步：按事实写。')
        self.assertTrue(saved['enabled'])
        listed = skills_store.list_skills(self.root)
        self.assertEqual([item['name'] for item in listed], ['weekly-report'])
        self.assertEqual(skills_store.index(self.root)[0]['description'], '写周报时用')
        text = skills_store.read(self.root, 'weekly-report')
        self.assertIn('第一步', text['body'])
        self.assertIn('不改变身份', text['note'])
        skills_store.set_enabled(self.root, 'weekly-report', False)
        self.assertEqual(skills_store.index(self.root), [])
        with self.assertRaises(PermissionError):
            skills_store.read(self.root, 'weekly-report')
        skills_store.delete(self.root, 'weekly-report')
        self.assertEqual(skills_store.list_skills(self.root), [])

    def test_import_reads_a_real_skill_file(self):
        source = self.root / 'incoming' / 'SKILL.md'
        source.parent.mkdir(parents=True)
        source.write_text('---\nname: tidy-notes\ndescription: 整理会议记录\n---\n\n先归类再合并。\n',
                         encoding='utf-8')
        imported = skills_store.import_path(self.root, source)
        self.assertEqual(imported['name'], 'tidy-notes')
        self.assertEqual(skills_store.read(self.root, 'tidy-notes')['body'].strip(), '先归类再合并。')

    def test_invalid_names_and_empty_bodies_are_refused(self):
        with self.assertRaises(ValueError):
            skills_store.save(self.root, name='Bad Name', description='x', body='y')
        with self.assertRaises(ValueError):
            skills_store.save(self.root, name='ok-name', description='', body='y')
        with self.assertRaises(ValueError):
            skills_store.save(self.root, name='ok-name', description='x', body='   ')

    def test_the_agent_tool_only_lists_enabled_skills(self):
        from tool_host import skill_tool
        empty = skill_tool(self.root)
        self.assertIn('当前没有启用的技能', empty['description'])
        skills_store.save(self.root, name='weekly-report', description='写周报时用', body='步骤')
        listed = skill_tool(self.root)['description']
        self.assertIn('weekly-report：写周报时用', listed)


if __name__ == '__main__':
    unittest.main()
