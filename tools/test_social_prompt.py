"""The one behaviour contract that makes the two social tools worth calling.

A tool that is only named is never called: this proves the note contract reaches the
group system prompt of a speaking turn and the owner-private one, exactly once each,
and stays out of a turn that publishes no such tool.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from test_social_base import Fixture
from social_context import SocialContext
from model_gateway import ChatOptions, ChatResult
import fish_chat
import group_decide

CHAT = 'group:987654321'
PAYLOAD = {'chat_type': 'group', 'chat_id': CHAT, 'message_id': 'note-1',
           'sender': {'id': '333333333', 'display_name': '群友甲'}, 'reply_to': '',
           'mentions_bot': False,
           'segments': [{'type': 'text', 'text': '我最近腰疼，医生让我少坐'}]}


class ScriptedGateway:
    """The model seam is the only fake: every request it receives is kept."""

    base = 'fixture://scripted-model'
    model = 'fixture-model'

    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        # 同一轮可交出多个完整气泡；记笔记的说明不能变成回复条数限制。
        if any(isinstance(message, dict) and message.get('role') == 'tool' for message in messages):
            return ChatResult('', {}, 'stop')
        return ChatResult('', {}, 'tool_calls', (
            {'id': 'call-1', 'type': 'function',
             'function': {'name': 'fish_say', 'arguments': '{"texts": ["嗯。", "下个月是哪天？"]}'}},))


class SocialPromptTests(Fixture):
    def test_group_speaking_turn_carries_the_note_contract_once(self):
        revision = SocialContext(self.root).save_persona('qq', '契约人格', '群里的同一个人格')
        seen = {}

        def captured(root, role, **kwargs):
            seen.update(kwargs)
            return '好'

        with patch('fish_chat.respond', captured):
            candidate = group_decide.fish_candidate(self.root, CHAT, payload=PAYLOAD,
                                                    revision=revision['id'])
        self.assertIsNone(candidate.get('error'))
        notes = seen['references']
        self.assertEqual(notes.count(group_decide.SOCIAL_NOTES), 1)
        self.assertEqual(notes.count(group_decide.READ_SCOPE), 1)
        self.assertLessEqual(len(group_decide.SOCIAL_NOTES), 120)
        for instruction in ('fish_social_note', 'impression', 'subject=qq:', 'topic', 'thought'):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, group_decide.SOCIAL_NOTES)
        self.assertNotIn(group_decide.SOCIAL_NOTES, group_decide._speaking_notes([], False))

    def test_owner_private_turn_carries_the_note_contract_once(self):
        gateway = ScriptedGateway()
        # 发送工具走的是同一个工具服务，会话必须先按程序身份绑定。
        from tool_host import bind_session
        bind_session(self.root, 'qq', 'qq-private:probe', 'owner', self.owner, False, chat_mode='owner')
        with patch('model_gateway.ModelGateway', lambda *a, **k: gateway), \
                patch('model_gateway.chat_options', lambda root: ChatOptions('high', 512, 30)):
            result = fish_chat.respond(self.root, 'qq', persona={'body': '人格正文', 'id': 'rev-1'},
                              history=[], history_source='probe', memory='', scope='owner',
                              references='', current=[{'type': 'text', 'text': '我下个月要去看演出'}],
                              session='qq-private:probe', cancelled=None, origin='probe',
                              output='bubbles', emoji_state={'previewed': set(), 'tools': []},
                              tool_context='owner-private')
        system = gateway.calls[0][0]['content']
        self.assertEqual(system.count(fish_chat.OWNER_SOCIAL_NOTES), 1)
        self.assertLessEqual(len(fish_chat.OWNER_SOCIAL_NOTES), 120)
        for instruction in ('fish_social_note', 'impression', 'subject=qq:',
                            'fish_memory_save kind=event', 'due'):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, fish_chat.OWNER_SOCIAL_NOTES)
        self.assertEqual([{'kind': 'text', 'text': '嗯。'},
                          {'kind': 'text', 'text': '下个月是哪天？'}], json.loads(result)['bubbles'])
        # The group surface states its own contract, so these two never stack.
        self.assertEqual(fish_chat._social_notes('x', 'group', ('fish_social_note',)), 'x')
        self.assertEqual(fish_chat._social_notes('x', 'owner-private', ()), 'x')


if __name__ == '__main__':
    unittest.main()
