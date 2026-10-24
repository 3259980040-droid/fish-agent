"""B1 closeout: the owner's save decision, run offline end to end, once per shape.

Everything here is disposable: a temporary root, the real ToolHost over loopback,
a scripted model, and a fetch that never opens a connection. The question these
tests answer is whether "the model decided to keep this page" really leaves the
right file and the right facts, and whether the other branches leave nothing.
"""
import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fish_chat import respond
from fish_library import library_dir, list_documents
from governance import Governance
from model_gateway import ChatOptions, ChatResult
from tool_host import ToolHost, bind_session
from web_knowledge import WebKnowledge

OWNER = '3259980040'
GROUP = '1043230169'
CHAT = 'group:' + GROUP
PAGE = 'http://93.184.216.34/notes.md'
BODY = '滑窗笔记：左右指针都只往前走。\n第二段：右指针扩张，左指针收缩。'
DDL = ('CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT, '
       'user_id TEXT, text TEXT, replied INTEGER DEFAULT 0, conversation_id TEXT, sender_id TEXT, '
       'direction TEXT, channel_message_id TEXT, reply_to TEXT, segments TEXT, status TEXT)')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def call(name, arguments, call_id='call-1'):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


def wanting(*calls):
    return ChatResult('', {}, 'tool_calls', tuple(calls))


def final(text):
    return ChatResult(text, {}, 'stop')


def page(text=BODY, *, truncated=False, title='滑窗笔记', url=PAGE):
    return {'ok': True, 'url': url, 'title': title, 'text': text,
            'truncated': truncated, 'source': 'deepseek-fetch'}


class FakeGateway:
    """Scripted model: it never opens a connection, and it records what it was sent."""

    base = 'https://fake.invalid'
    model = 'fake-model'

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests, self.kwargs = [], []

    def chat(self, messages, **kwargs):
        self.requests.append(messages)
        self.kwargs.append({key: value for key, value in kwargs.items() if key != 'cancelled'})
        if not self.replies:
            raise AssertionError('假模型被调用的次数超过本用例提供的回复')
        return self.replies.pop(0)

    def tool_payloads(self, turn=1):
        return [json.loads(message['content']) for message in self.requests[turn]
                if message.get('role') == 'tool']


class B1CloseoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write_stores()
        Governance(self.root).save({'max_tool_calls_per_turn': 0})
        self.host = ToolHost(self.root)
        self.host.serve(background_workers=False, listen_port=0)
        self.addCleanup(self.stop_host)
        config = json.loads((self.root / 'harness/agents.json').read_text(encoding='utf-8'))
        config['runtime']['tool_host_port'] = int(self.host.server.server_address[1])
        write_json(self.root / 'harness/agents.json', config)

    def stop_host(self):
        self.host.stop.set()
        self.host.server.shutdown()
        self.host.server.server_close()

    def write_stores(self):
        write_json(self.root / 'harness/agents.json', {
            'agents': [{'id': 'qq', 'model': 'fake-model', 'base_url': 'https://fake.invalid',
                        'apiKeyEnv': 'FISH_TEST_QQ_KEY'}],
            'runtime': {'ports': {'qq': 0, 'research': 0}, 'tool_host_port': 0}})
        write_json(self.root / 'channels/fish_qq/config.json', {
            'allowlist': {'owner_id': OWNER, 'group_ids': [GROUP]},
            'bot': {'display_name': '大肥鱼'}})
        (self.root / 'data/qq').mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.root / 'data/qq/qq.db')) as db:
            db.execute(DDL)
            db.commit()

    def stored(self):
        """Files the library really holds, index JSON aside."""
        web = library_dir(self.root) / 'web'
        return sorted(item.name for item in web.iterdir() if item.is_file())

    def turn(self, replies, *, text='帮我存一下这个页面'):
        session = 'qq-private:' + uuid.uuid4().hex
        bind_session(self.root, 'qq', session, 'owner', OWNER, False, chat_mode='owner')
        gateway = FakeGateway(replies)
        with patch('model_gateway.ModelGateway', lambda *args, **kwargs: gateway), \
                patch('model_gateway.chat_options', lambda root: ChatOptions('high', 512, 30)):
            answer = respond(self.root, 'qq', persona={'id': 'rev-1', 'body': '你是一条鱼。'},
                             history=[], history_source='test', memory='', scope='owner',
                             references='', current=[{'type': 'text', 'text': text}],
                             session=session, cancelled=None, origin='test:b1',
                             output='text', tool_context='owner-desktop')
        gateway.answer = answer
        return gateway

    def sidecar(self, doc_id):
        return json.loads((library_dir(self.root) / 'web/index' / (doc_id + '.json'))
                          .read_text(encoding='utf-8'))

    # 1
    def test_a_save_call_lands_a_real_file_with_matching_facts(self):
        with patch.object(WebKnowledge, 'fetch_tool', return_value=page(truncated=True)) as fetch:
            gateway = self.turn([wanting(call('fish_doc_save', {'url': PAGE})),
                                 final('存好了，就放在资料库里。')])
        self.assertEqual(1, fetch.call_count)
        self.assertEqual('存好了，就放在资料库里。', gateway.answer)
        payload = gateway.tool_payloads()[0]
        self.assertTrue(payload['ok'], payload)
        path = Path(payload['path'])
        self.assertTrue(path.is_file(), payload)
        self.assertEqual(BODY, path.read_text(encoding='utf-8'))
        digest = hashlib.sha256(BODY.encode('utf-8')).hexdigest()
        self.assertEqual(digest, payload['sha256'])
        self.assertEqual(digest, self.sidecar(payload['doc_id'])['sha256'])
        self.assertTrue(path.resolve().is_relative_to(library_dir(self.root).resolve()))
        self.assertEqual(PAGE, payload['url'])
        # The reader's own truncation is carried through as upstream, not invented as local.
        self.assertTrue(payload['upstream_truncated'])
        self.assertFalse(payload['local_truncated'])
        self.assertTrue(payload['truncated'])
        # The second turn really received that result, so the turn ended on the fact.
        delivered = [message for message in gateway.requests[1] if message.get('role') == 'tool']
        self.assertEqual(1, len(delivered))
        self.assertIn(payload['doc_id'], delivered[0]['content'])
        self.assertEqual(1, len(self.stored()))

    # 2
    def test_no_tool_call_means_no_fetch_no_save_and_an_empty_library(self):
        with patch.object(WebKnowledge, 'fetch_tool') as fetch, \
                patch('fish_library.save_public_page') as save:
            gateway = self.turn([final('这页我不用存，你直接问我就行。')])
        self.assertEqual('这页我不用存，你直接问我就行。', gateway.answer)
        self.assertEqual(1, len(gateway.requests))
        fetch.assert_not_called()
        save.assert_not_called()
        self.assertEqual([], self.stored())
        self.assertEqual([], list_documents(self.root))

    # 3
    def test_a_failed_read_is_an_error_fact_and_saves_nothing(self):
        failed = {'ok': False, 'url': PAGE, 'error': '网络请求失败（Timeout）'}
        with patch.object(WebKnowledge, 'fetch_tool', return_value=failed) as fetch:
            gateway = self.turn([wanting(call('fish_doc_save', {'url': PAGE})),
                                 final('这页没读到，我没存。')])
        self.assertEqual(1, fetch.call_count)
        self.assertEqual('这页没读到，我没存。', gateway.answer)
        payload = gateway.tool_payloads()[0]
        self.assertFalse(payload['ok'])
        self.assertIn('网络请求失败', payload['error'])
        self.assertNotIn('doc_id', payload)
        delivered = [message for message in gateway.requests[1] if message.get('role') == 'tool']
        self.assertIn('网络请求失败', delivered[0]['content'])
        self.assertEqual([], self.stored())
        self.assertEqual([], list_documents(self.root))

    # 4
    def test_the_same_page_twice_leaves_one_file_and_says_so(self):
        with patch.object(WebKnowledge, 'fetch_tool', return_value=page()):
            first = self.turn([wanting(call('fish_doc_save', {'url': PAGE})), final('存好了。')])
            again = self.turn([wanting(call('fish_doc_save', {'url': PAGE})), final('这篇已经在了。')])
        one, two = first.tool_payloads()[0], again.tool_payloads()[0]
        self.assertTrue(one['ok'], one)
        self.assertFalse(one['deduplicated'])
        self.assertFalse(one['upstream_truncated'])
        self.assertTrue(two['ok'], two)
        self.assertTrue(two['deduplicated'])
        self.assertEqual(one['path'], two['path'])
        self.assertEqual(one['doc_id'], two['doc_id'])
        self.assertEqual(1, len(self.stored()))
        self.assertEqual(1, len(list_documents(self.root)))

    # 5
    def test_the_library_module_starts_no_process_and_runs_no_command(self):
        source = (Path(__file__).resolve().parents[1] / 'harness/fish_library.py').read_text(
            encoding='utf-8')
        for banned in ('subprocess', 'os.system', 'Popen', 'shutil.which', 'os.popen', 'socket'):
            self.assertNotIn(banned, source)
        self.assertNotIn('import os\n', source)


if __name__ == '__main__':
    unittest.main()
