"""A successful group hand-off is the real recovery of the group binding fault."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]

from qq_agent_loop import QQAgentLoop


class GroupBindingRecoveryTests(unittest.TestCase):
    def test_a_successful_submit_clears_the_old_binding_fault(self):
        recovered, faults, watched = [], [], []
        chat = 'group:1043230169'

        async def recovered_call(source, *, stage='model'):
            recovered.append((source, stage))

        async def fault_call(*args, **kwargs):
            faults.append((args, kwargs))

        loop = SimpleNamespace(
            root=ROOT, store=SimpleNamespace(update_message=lambda *a, **k: None),
            _recovered=recovered_call, _fault=fault_call,
            _watch_turn=lambda execution, **kwargs: watched.append((execution, kwargs)),
            _group_conversation=lambda value: 'conversation-1',
        )
        with patch('tool_host.request', return_value={'execution': 'run-1', 'status': 'queued'}):
            asyncio.run(QQAgentLoop._submit_group(loop, {'chat_id': chat, 'message_id': 'm1'}, chat, 7, '文本'))
        self.assertEqual(recovered, [('group-fish:' + chat, 'binding')])
        self.assertEqual(faults, [])
        self.assertEqual(watched, [('run-1', {'source': 'group-fish:' + chat, 'chat': chat})])

    def test_a_failed_submit_records_the_binding_fault_and_does_not_recover(self):
        recovered, faults = [], []
        chat = 'group:1043230169'

        async def recovered_call(*args, **kwargs):
            recovered.append((args, kwargs))

        async def fault_call(source, error, *, stage='model', uncertain=False):
            faults.append((source, stage, type(error).__name__))

        loop = SimpleNamespace(
            root=ROOT, store=SimpleNamespace(update_message=lambda *a, **k: None),
            _recovered=recovered_call, _fault=fault_call,
            _watch_turn=lambda *a, **k: None,
            _group_conversation=lambda value: 'conversation-1',
        )
        with patch('tool_host.request', side_effect=PermissionError('未知输入来源')):
            asyncio.run(QQAgentLoop._submit_group(loop, {'chat_id': chat}, chat, 8, '文本'))
        self.assertEqual(recovered, [])
        self.assertEqual(faults, [('group-fish:' + chat, 'binding', 'PermissionError')])


if __name__ == '__main__':
    unittest.main()
