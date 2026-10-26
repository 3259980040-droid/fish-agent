"""W04: one offline workbench history, originals and usage process."""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from context_budget import ContextBudget
from locations import project_path
from model_gateway import ChatResult
from storage import database
import test_execution_repair as repair
from test_unified_execution import FakeGateway, tool_call


class WorkbenchTurns(unittest.TestCase):
    setUp = repair.ExecutionRepairTests.setUp
    close_host = repair.ExecutionRepairTests.close_host
    wait_view = repair.ExecutionRepairTests.wait_view
    evidence = repair.ExecutionRepairTests.evidence

    def test_full_history_originals_and_owned_statistics(self):
        empty = self.wb.dispatch('turns.list', {'limit': 2})
        self.assertEqual(empty['runs'], [])
        self.assertEqual(empty['stats']['state'], 'unknown')
        thought = '实际替代思考。' * 3000 + '思考尾标'

        class Gateway(FakeGateway):
            model = 'deepseek-v4-test'

            def chat(self, messages, **options):
                reply = super().chat(messages, **options)
                options['on_event']('reasoning', thought)
                return reply

        fake = Gateway([
            ChatResult('', {'prompt_tokens': 100, 'completion_tokens': 10, 'prompt_cache_hit_tokens': 40},
                       'tool_calls', (tool_call('fish_workspace', {}, 'w04-workspace'),
                                      tool_call('fish_say', {'texts': ['客户端实际保存的回复全文。']}, 'w04-say'))),
            ChatResult('已查看，正常不发送。', {'prompt_tokens': 120, 'completion_tokens': 12,
                       'prompt_cache_hit_tokens': 60}, 'stop'),
            ChatResult('私聊也正常不发送。', {}, 'stop'),
            RuntimeError('offline API failure'),
        ])
        replacement = patch('model_gateway.ModelGateway', return_value=fake)
        replacement.start()
        self.addCleanup(replacement.stop)
        cid = self.wb.dispatch('chat.new', {'title': 'W04完整原文'})['id']
        original = '客户端原话' * 4000 + '原文尾标'
        job = self.wb.dispatch('chat.send', {'id': cid, 'text': original})
        execution = self.wb.dispatch('chat.job', {'job_id': job['job_id']})['execution']
        first = self.wait_view(execution)
        self.assertEqual(first['raw_status'], 'complete')
        self.assertEqual(first['materials'][0]['text'], original)
        self.assertEqual(first['trace']['reasoning'], thought * 2)
        self.assertEqual(first['usage']['closed_steps'], 2)
        self.assertEqual(first['usage']['input_tokens'], 220)
        self.assertEqual(first['usage']['output_tokens'], 22)
        self.assertAlmostEqual(first['usage']['cache_hit_ratio'], 100 / 220)
        self.assertIsNone(first['output_speed']['tokens_per_second'])
        self.assertIsNotNone(first['delivery_times']['sending'])
        self.assertTrue(any(output['text'] == '客户端实际保存的回复全文。' and output['state'] == 'sent'
                            for output in first['outputs']))
        legacy_text = self.wb.dispatch('chat.reasoning', {'reference': first['reference']})
        self.assertEqual(legacy_text['reasoning'], thought * 2)
        self.assertEqual(first['actions'][0]['call_id'], 'w04-workspace')
        with patch.object(self.runtime.execution, 'turn_view', side_effect=AssertionError('display must not run during delivery')):
            self.assertEqual(self.runtime.tools.delivery_sources(execution), ['fish_say'])

        store = self.runtime.client_qq.store()
        turns = [first]
        for kind, chat, sender, text in (
                ('qq-private', 'private:3259980040', '3259980040', '主人私聊原件'),
                ('qq-group', 'group:123456', '2100000001', '群原件' * 8000 + '群尾标')):
            conversation = store.get_or_create_conversation(chat, 'qq')['id']
            mid = store.record_message(conversation, chat, sender, 'inbound', 'external-' + kind,
                                       'quoted-message', [{'type': 'text', 'text': text}], text, 'received')
            submitted = self.runtime.execution.submit(
                source=kind, conversation=conversation, message=mid, text=text, reference='qq:' + str(mid),
                place=chat, payload={'chat_id': chat, 'message_id': 'external-' + kind,
                                    'sender': {'id': sender}, 'segments': [{'type': 'text', 'text': text}],
                                    'mentions_bot': True})
            turns.append(self.wait_view(submitted['execution']))
        self.assertEqual(turns[1]['raw_status'], 'silent')
        self.assertIsNone(turns[1]['delivery_times']['sending'])
        self.assertEqual(turns[2]['raw_status'], 'failed')
        self.assertEqual(turns[2]['materials'][0]['original']['sender_id'], '2100000001')
        self.assertEqual(turns[2]['materials'][0]['original']['reply_to'], 'quoted-message')
        self.assertTrue(turns[2]['materials'][0]['text'].endswith('群尾标'))
        self.assertEqual(turns[2]['trace']['reasoning'], '')

        # An old unlinked request is a historical fixture, never a guessed turn.
        old = ContextBudget(self.root, fake)
        old_id = old.begin('desktop:' + cid, 'desktop-chat:' + cid, {})
        old.finish(old_id, {'prompt_tokens': 9999, 'completion_tokens': 9999})
        # An unrelated legacy trace is not an accounting input. Its original
        # damage must not prevent this instance's real turns from being read.
        from turn_trace import TurnTrace
        unrelated = TurnTrace(self.root, 'qq', 'legacy', 'legacy', reference='legacy:damaged')
        with database(project_path(self.root, 'data/social/experience.db')) as db:
            db.execute('UPDATE turn_traces SET events=? WHERE id=?', ('[] trailing legacy content', unrelated.id))
        listed = self.wb.dispatch('turns.list', {'limit': 2})
        next_page = self.wb.dispatch('turns.list', {'limit': 2, 'cursor': listed['next_cursor']})
        records = listed['runs'] + next_page['runs']
        self.assertEqual(len(records), 3)
        self.assertEqual(len({row['execution'] for row in records}), 3)
        self.assertTrue(all('reasoning' not in row['trace'] and 'events' not in row['trace'] for row in records))
        self.assertTrue(any(row['materials'][0]['text'] == original for row in records))
        self.assertEqual(listed['stats']['closed_steps'], 4)
        self.assertEqual(listed['stats']['turns'], 3)
        self.assertEqual(listed['stats']['input_tokens'], 220)
        self.assertEqual(listed['stats']['coverage']['unlinked_requests'], 1)
        self.assertEqual(listed['stats']['state'], 'partial')
        self.assertEqual(self.wb.dispatch('turn.view', {'execution': execution})['trace']['reasoning'], thought * 2)

        other = self.wb.dispatch('instances.create', {'name': 'W04另一实例', 'running': False,
                                   'persona': {'name': '另一份', 'body': '独立人格'}})
        foreign = self.wb.dispatch('turns.list', {'instance_id': other['id']})
        self.assertEqual(foreign['runs'], [])
        self.assertEqual(foreign['stats']['request_count'], 0)

        self.wb.dispatch('chat.delete', {'conversation_id': cid})
        deleted = self.wb.dispatch('turn.view', {'execution': execution})
        self.assertEqual(deleted['materials'][0]['original_state'], 'missing')
        self.assertEqual(deleted['materials'][0]['text'], '')
        self.assertEqual(deleted['trace']['reasoning'], thought * 2)
        with database(project_path(self.root, 'data/social/experience.db')) as db:
            # A preserved old trace predates the execution/usage association.
            old_events = [event for event in first['trace']['events'] if event['kind'] != 'execution']
            db.execute('UPDATE turn_traces SET events=? WHERE reference=?',
                       (json.dumps(old_events, ensure_ascii=False), first['reference']))
        old_view = self.wb.dispatch('turn.view', {'execution': execution})
        self.assertEqual(old_view['usage']['state'], 'unknown')
        self.assertIsNone(old_view['usage']['input_tokens'])
        self.evidence('workbench_full_turn_history', runs=[{key: row[key] for key in
                      ('execution', 'raw_status', 'materials_count', 'actions', 'delivery_times')} for row in records],
                      stats=listed['stats'], selected=execution, reasoning_chars=len(first['trace']['reasoning']),
                      original_chars=len(original), source_tails=[turn['materials'][0]['text'][-10:] for turn in turns],
                      speed=first['output_speed'], outputs=first['outputs'], legacy_usage=old_view['usage'],
                      foreign=foreign, deleted_original=deleted['materials'][0]['original_state'])

        # Read corruption is not an empty provider thought.
        with database(project_path(self.root, 'data/social/experience.db')) as db:
            db.execute('UPDATE turn_traces SET events=? WHERE reference=?', ('broken-json', first['reference']))
        with self.assertRaises(ValueError):
            self.wb.dispatch('turn.view', {'execution': execution})


if __name__ == '__main__':
    unittest.main()
