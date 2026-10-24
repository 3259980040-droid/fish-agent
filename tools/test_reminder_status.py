"""The tool description and capability brief describe the consumer's live flag."""
from pathlib import Path
import unittest
from unittest.mock import patch

import current_capabilities
import memory_reminder
from tool_host import builtins


class ReminderStatusTests(unittest.TestCase):
    def descriptions(self):
        description = next(tool['description'] for tool in builtins('qq') if tool['name'] == 'fish_memory_save')
        with patch.object(current_capabilities, '_tools', return_value=['fish_memory_save']):
            brief = current_capabilities.capabilities_brief(Path('unused'), scope='owner-private')
        return description, brief

    def test_process_enabled_is_enabled_in_both_actual_strings(self):
        with patch.dict('os.environ', {'FISH_MEMORY_REMINDER': '1'}):
            self.assertTrue(memory_reminder.enabled())
            values = self.descriptions()
        for text in values:
            self.assertIn('当前进程到期提醒已开启', text)
            self.assertNotIn('已经在运行', text)

    def test_empty_process_flag_never_inherits_a_pending_registry_change(self):
        with patch.dict('os.environ', {'FISH_MEMORY_REMINDER': ''}), \
                patch.object(current_capabilities, '_user_value', return_value='1') as registry:
            self.assertFalse(memory_reminder.enabled())
            values = self.descriptions()
            registry.assert_not_called()
        for text in values:
            self.assertIn('当前进程到期提醒未开启', text)
            self.assertIn('重启后台后才会生效', text)
            self.assertNotIn('已经在运行', text)

    def test_unreadable_live_flag_is_unknown_not_a_guessed_registry_value(self):
        with patch('memory_reminder.enabled', side_effect=OSError('read unavailable')), \
                patch.object(current_capabilities, '_user_value', side_effect=PermissionError('registry unavailable')):
            self.assertIsNone(current_capabilities._switch_on())
            values = self.descriptions()
        for text in values:
            self.assertIn('当前进程到期提醒状态未知', text)
            self.assertNotIn('已经在运行', text)


if __name__ == '__main__': unittest.main()
