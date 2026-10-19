"""Validate moved editors, draft isolation and complete output-rule previews."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import dependencies
from PySide6.QtWidgets import QApplication
from fish_desktop.bootstrap import initialize
from fish_desktop.services import FishServices
from fish_desktop.pages import BasePage
from governance import Governance
from storage import read_json, write_json


def immediate(page, function, result, on_error=None, on_done=None):
    value = function()
    result(value)
    if on_done: on_done()


class CleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = initialize(Path(self.temp.name) / 'fish')
        channel = read_json(self.root / 'channels/fish_qq/config.json')
        channel['allowlist']['owner_id'] = '123456789'; write_json(self.root / 'channels/fish_qq/config.json', channel)
        self.services = FishServices(self.root)
        for name, text in [('qq','QQ_SOURCE'),('research','RESEARCH_SOURCE'),('global','GLOBAL_SOURCE')]:
            path = self.services._persona_source_path(name); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text, encoding='utf-8')
        self.submit = patch.object(BasePage, '_submit', immediate); self.submit.start(); self.addCleanup(self.submit.stop)
        self.widgets = []; self.addCleanup(self.close_widgets)

    def close_widgets(self):
        for widget in self.widgets: widget.close()
        self.app.processEvents()

    def keep(self, widget):
        self.widgets.append(widget); return widget

    def test_model_editor_does_not_overwrite_persona_or_shared_rules(self):
        from fish_desktop.configuration import AgentsPage
        page = self.keep(AgentsPage(self.services)); page.name.setText('new model label')
        self.services._persona_source_path('qq').write_text('EXTERNAL_EDIT', encoding='utf-8')
        page._save()
        self.assertEqual(self.services.persona_source('qq')['text'], 'EXTERNAL_EDIT')
        self.assertEqual(self.services.persona_source('global')['text'], 'GLOBAL_SOURCE')
        self.assertFalse(hasattr(page, 'persona')); self.assertFalse(hasattr(page, 'global_rules'))

    def test_persona_drafts_are_separate_and_shared_rules_have_one_source(self):
        from fish_desktop.persona_sources import PersonaSourcePage
        daily = self.keep(PersonaSourcePage(self.services)); daily.refresh(); daily.editor.setPlainText('QQ_DRAFT')
        daily.roles.setCurrentIndex(1); self.assertEqual(daily.editor.toPlainText(), 'RESEARCH_SOURCE')
        daily.editor.setPlainText('RESEARCH_DRAFT'); daily.roles.setCurrentIndex(0)
        self.assertEqual(daily.editor.toPlainText(), 'QQ_DRAFT'); daily._save()
        shared = self.keep(PersonaSourcePage(self.services, shared=True)); shared.refresh(); shared.editor.setPlainText('GLOBAL_NEW'); shared._save()
        self.assertEqual(self.services.persona_source('qq')['text'], 'QQ_DRAFT')
        self.assertEqual(self.services.persona_source('research')['text'], 'RESEARCH_SOURCE')
        self.assertEqual(self.services.persona_source('global')['text'], 'GLOBAL_NEW')

    def test_stale_persona_draft_cannot_overwrite_new_external_content(self):
        old = self.services.persona_source('qq')
        self.services._persona_source_path('qq').write_text('NEW_EXTERNAL', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '其他窗口'):
            self.services.save_persona_source('qq', 'STALE_EDIT', old['digest'])
        self.assertEqual(self.services.persona_source('qq')['text'], 'NEW_EXTERNAL')

    def test_saving_owner_settings_keeps_rules_changed_elsewhere(self):
        from fish_desktop.system_settings import OwnerPage
        policy = Governance(self.root); policy.save({'daily_usage_limits': {'qq': False}, 'blocked_text': ['old']})
        page = self.keep(OwnerPage(self.services)); page._apply(self.services.governance_snapshot())
        policy.save({'blocked_text': ['NEW_RULE']}); page._save()
        self.assertEqual(policy.config()['blocked_text'], ['NEW_RULE'])
        self.assertFalse(policy.config()['daily_usage_limits']['qq'])
        self.assertFalse(hasattr(page, 'terms'))

    def test_preview_uses_custom_and_advanced_rules_without_saving(self):
        from fish_desktop.guard_settings import GuardSettingsPage
        page = self.keep(GuardSettingsPage(self.services))
        raw = json.dumps({'categories': [{'id':'sample', 'action':'block', 'patterns':['ADVANCED_BLOCK']} ]})
        paths = [self.root/'harness/control.json', self.root/'harness/safety/rules.json']
        before = {p: p.read_bytes() if p.exists() else None for p in paths}
        for sample, custom in [('CUSTOM_BLOCK',['CUSTOM_BLOCK']),('ADVANCED_BLOCK',[]),('PATTERN_42',[])]:
            result = page._draft(raw, '', sample, blocked_text=custom, block_patterns=['PATTERN_[0-9]+'])
            self.assertFalse(result['allowed'])
        self.assertTrue(page._draft(raw, '', 'hello', blocked_text=[], block_patterns=[])['allowed'])
        self.assertEqual(before, {p: p.read_bytes() if p.exists() else None for p in paths})

    def test_invalid_rules_change_nothing_and_valid_save_preserves_unlimited(self):
        from fish_desktop.guard_settings import GuardSettingsPage
        policy = Governance(self.root); policy.save({'daily_usage_limits': {'qq': False}})
        page = self.keep(GuardSettingsPage(self.services)); before = policy.path.read_bytes()
        with self.assertRaises(Exception): page._draft('{"categories":[]}', '', blocked_text=[], block_patterns=['['])
        self.assertEqual(policy.path.read_bytes(), before)
        page._draft('{"categories":[]}', '', blocked_text=['NEW_BLOCK'], block_patterns=[])
        self.assertEqual(policy.config()['blocked_text'], ['NEW_BLOCK'])
        self.assertFalse(policy.config()['daily_usage_limits']['qq'])
        with self.assertRaises(PermissionError): policy.check_output('NEW_BLOCK')

    def test_navigation_resolves_models_and_old_page_classes_are_gone(self):
        from fish_desktop.main_window import MainWindow
        import fish_desktop.pages as old
        window = self.keep(MainWindow(self.services, manage_shutdown=False)); window._timer.stop()
        self.assertEqual(window.nav.count(), 8)
        window.navigate('Agent 配置'); settings = window.page('设置')
        self.assertIs(window.stack.currentWidget(), settings)
        self.assertIs(settings.tabs.currentWidget(), settings.models)
        self.assertFalse(hasattr(window.page('大鱼'), 'start_btn'))
        for name in ('OverviewPage', 'ResearchPage', 'AgentsPage', 'SettingsPage'): self.assertFalse(hasattr(old, name))
        social = window.page('人格与群聊'); social.tabs.setCurrentWidget(social.daily_persona)
        self.assertEqual(social.daily_persona.editor.toPlainText(), 'QQ_SOURCE')


if __name__ == '__main__': unittest.main()
