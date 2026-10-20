"""Offline checks for the normalized inbound message envelope."""
import unittest

from channels.fish_qq.normalize import normalize_message


class InboundEnvelopeTests(unittest.TestCase):
    def test_private_quote_file_and_sender_are_preserved(self):
        event = normalize_message({
            'post_type': 'message', 'message_type': 'private', 'user_id': 200000001,
            'message_id': 'm-1', 'sender': {'user_id': 200000001, 'nickname': 'owner'},
            'message': [
                {'type': 'reply', 'data': {'id': 'm-0'}},
                {'type': 'text', 'data': {'text': '看看这个'}},
                {'type': 'file', 'data': {'file': 'report.docx', 'file_id': 'f-1'}},
            ],
        }, bot_id='900001')
        self.assertEqual(event['chat_id'], 'private:200000001')
        self.assertEqual(event['message_id'], 'm-1')
        self.assertEqual(event['sender']['id'], '200000001')
        self.assertEqual(event['reply_to'], 'm-0')
        self.assertTrue(event['received_at'])
        self.assertEqual([segment['type'] for segment in event['segments']], ['text', 'file'])
        self.assertNotIn('m-0', [segment.get('text') for segment in event['segments']])

    def test_quote_is_not_a_new_user_segment(self):
        event = normalize_message({
            'post_type': 'message', 'message_type': 'group', 'group_id': 123456789,
            'user_id': 200000001, 'message_id': 'm-2', 'sender': {'user_id': 200000001},
            'message': [
                {'type': 'reply', 'data': {'id': 'm-1'}},
                {'type': 'text', 'data': {'text': '这是本轮新话'}},
            ],
        }, bot_id='900001')
        self.assertEqual(event['reply_to'], 'm-1')
        self.assertEqual(len(event['segments']), 1)
        self.assertEqual(event['segments'][0]['text'], '这是本轮新话')


if __name__ == '__main__':
    unittest.main()
