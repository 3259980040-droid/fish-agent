"""Offline checks for the model-facing web tools and the target-address guard."""
import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
import web_knowledge
from web_knowledge import WebKnowledge

SEARCH='/v1/tools/search'
FETCH='/v1/tools/fetch'


class _StubProvider(BaseHTTPRequestHandler):
    """Local stand-in for the provider tools endpoints; counts every hit."""
    def do_POST(self):
        length=int(self.headers.get('Content-Length') or 0)
        if length:self.rfile.read(length)
        with self.server.lock:
            self.server.hits[self.path]=self.server.hits.get(self.path,0)+1
        delay=self.server.delays.get(self.path,0)
        if delay:time.sleep(delay)
        status,payload=self.server.routes.get(self.path,(404,{'error':'unknown path'}))
        data=json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self,*args):pass


def free_port():
    """A loopback port nothing listens on, for a real refused connection."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        return probe.getsockname()[1]


class WebToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),_StubProvider)
        cls.server.daemon_threads=True
        cls.server.lock=threading.Lock()
        cls.server.hits={}
        cls.server.routes={}
        cls.server.delays={}
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def setUp(self):
        self.server.hits.clear();self.server.routes.clear();self.server.delays.clear()
        self.route(SEARCH,{'results':[{'title':'标题'+str(i),'url':'https://example.invalid/'+str(i),
            'summary':'摘要'+str(i)} for i in range(5)]})
        self.route(FETCH,{'title':'Stub Page','content':'x'*200})
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def route(self,path,payload,status=200):
        self.server.routes[path]=(status,payload)

    def hits(self,path=SEARCH):
        return self.server.hits.get(path,0)

    def total_hits(self):
        return sum(self.server.hits.values())

    def gateway(self,base=None):
        return patch.object(web_knowledge,'ModelGateway',
            return_value=SimpleNamespace(key='synthetic-key',base=base or self.base))

    def allow_public(self):
        """The provider opens the page, not this process; only the guard is bypassed here."""
        return patch.object(WebKnowledge,'public_target',staticmethod(lambda url:str(url).strip()))

    # 1. success shape, the 4-result cap, and cache reuse
    def test_search_tool_returns_four_results_and_reuses_cache(self):
        with self.gateway():
            service=WebKnowledge(self.root)
            first=service.search_tool('DeepSeek 最新版本',chat='')
            self.assertTrue(first['ok'])
            self.assertEqual(first['source'],'deepseek-search')
            self.assertIs(first['cached'],False)
            self.assertEqual(first['query'],'DeepSeek 最新版本')
            self.assertEqual(len(first['results']),4)
            self.assertEqual(set(first['results'][0]),{'title','url','text'})
            self.assertEqual(first['results'][0]['text'],'摘要0')
            self.assertEqual(self.hits(SEARCH),1)
            second=service.search_tool('DeepSeek 最新版本',chat='')
            self.assertEqual(self.hits(SEARCH),1)
            self.assertIs(second['cached'],True)
            self.assertEqual(second['results'],first['results'])
            self.assertEqual(service.search('DeepSeek 最新版本'),first['results'])

    # 2. provider error is a fact, not an exception
    def test_search_tool_reports_provider_http_error(self):
        self.route(SEARCH,{'error':'boom'},status=500)
        with self.gateway():
            result=WebKnowledge(self.root).search_tool('版本')
        self.assertIs(result['ok'],False)
        self.assertIn('500',result['error'])

    # 3. timeout and refused connection
    def test_search_tool_reports_timeout_and_refused_connection(self):
        with self.gateway(),patch.object(web_knowledge.requests,'post',side_effect=requests.Timeout('timed out')):
            result=WebKnowledge(self.root).search_tool('版本')
        self.assertIs(result['ok'],False)
        self.assertIn('超时',result['error'])
        self.assertNotIn('results',result)
        with self.gateway(base='http://127.0.0.1:'+str(free_port())):
            result=WebKnowledge(self.root).search_tool('另一个版本')
        self.assertIs(result['ok'],False)
        self.assertIn('连接失败',result['error'])
        self.assertNotIn('results',result)

    # 4. query hygiene
    def test_search_tool_rejects_empty_secret_and_overlong_queries(self):
        with self.gateway():
            service=WebKnowledge(self.root)
            for query,marker in (('','空'),('   ','空'),('sk-abcdef1234567890','密钥'),
                    ('api_key=abcdef','密钥'),('C:\\Users\\me\\secret.txt','密钥'),('版'*201,'过长')):
                result=service.search_tool(query)
                self.assertIs(result['ok'],False,query[:12])
                self.assertIn(marker,result['error'],query[:12])
        self.assertEqual(self.total_hits(),0)

    # 5. page read, including the 12000 character limit
    def test_fetch_tool_returns_page_and_marks_truncation(self):
        self.route(FETCH,{'title':'长页面','content':'正'*13000})
        with self.gateway(),self.allow_public():
            service=WebKnowledge(self.root)
            result=service.fetch_tool('https://example.com/page')
            self.assertIs(result['ok'],True)
            self.assertEqual(result['url'],'https://example.com/page')
            self.assertEqual(result['title'],'长页面')
            self.assertEqual(result['source'],'deepseek-fetch')
            self.assertEqual(len(result['text']),12000)
            self.assertIs(result['truncated'],True)
            self.route(FETCH,{'title':'短文','content':'只有一小段正文'})
            short=service.fetch_tool('https://example.com/short')
            self.assertIs(short['truncated'],False)
            self.assertEqual(short['text'],'只有一小段正文')
            self.assertEqual(short['title'],'短文')

    # 6. addresses decided locally are refused before any DNS lookup
    def test_public_target_refuses_local_private_and_metadata_targets(self):
        targets=('http://127.0.0.1/x','http://localhost:8080/','http://169.254.169.254/latest/meta-data/',
            'http://10.0.0.5/','file:///etc/passwd','http://user:pass@example.com/','http://metadata.google.internal/',
            'http://[::1]/','http://0.0.0.0/','http://192.168.1.1/','http://x.localhost/','http://printer.local/',
            'http://[::ffff:127.0.0.1]/','http://100.64.0.1/','http://metadata.azure.internal/')
        with patch.object(web_knowledge.socket,'getaddrinfo') as resolving:
            for url in targets:
                with self.assertRaises(ValueError,msg=url) as caught:
                    WebKnowledge.public_target(url)
                self.assertTrue(str(caught.exception),url)
            self.assertEqual(resolving.call_count,0)
        with self.assertRaises(ValueError) as caught:
            WebKnowledge.public_target('http://127.0.0.1/x')
        self.assertIn('不允许访问',str(caught.exception))

    # 6b. domain names are verified through DNS, and any blocked address refuses the whole name
    def test_public_target_verifies_resolved_addresses(self):
        public=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
        private=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.5',80))]
        mixed=public+private
        with patch.object(web_knowledge.socket,'getaddrinfo',return_value=public):
            self.assertEqual(WebKnowledge.public_target('https://example.com/a?b=1'),'https://example.com/a?b=1')
            self.assertEqual(WebKnowledge.public_target('http://example.com:443/'),'http://example.com:443/')
        with patch.object(web_knowledge.socket,'getaddrinfo',return_value=private):
            with self.assertRaises(ValueError) as caught:
                WebKnowledge.public_target('https://looks-public.example/')
            self.assertIn('不允许访问内网地址',str(caught.exception))
        with patch.object(web_knowledge.socket,'getaddrinfo',return_value=mixed):
            with self.assertRaises(ValueError):
                WebKnowledge.public_target('https://mixed.example/')
        with patch.object(web_knowledge.socket,'getaddrinfo',side_effect=socket.gaierror(-2,'no dns')):
            with self.assertRaises(ValueError) as caught:
                WebKnowledge.public_target('https://unknown.invalid/')
            self.assertIn('无法验证',str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            WebKnowledge.public_target('https://example.com:8080/')
        self.assertIn('端口',str(caught.exception))

    # 6c. slow DNS is refused rather than waited on, and a stalled lookup never allows the target
    def test_public_target_refuses_when_resolution_stalls(self):
        def stalled(host,port):
            time.sleep(2)
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
        with patch.object(web_knowledge,'RESOLVE_TIMEOUT',0.05), \
                patch.object(web_knowledge.socket,'getaddrinfo',side_effect=stalled):
            started=time.monotonic()
            with self.assertRaises(ValueError) as caught:
                WebKnowledge.public_target('https://slow.example/')
            elapsed=time.monotonic()-started
        self.assertIn('无法验证',str(caught.exception))
        self.assertLess(elapsed,1.0)

    # 7. a refused target must not reach the provider at all
    def test_fetch_tool_refuses_target_without_any_request(self):
        with self.gateway():
            result=WebKnowledge(self.root).fetch_tool('http://127.0.0.1/x')
        self.assertIs(result['ok'],False)
        self.assertIn('不允许访问',result['error'])
        self.assertNotIn('text',result)
        self.assertEqual(self.total_hits(),0)

    # 8. cancellation wins over everything else
    def test_cancelled_work_raises_interrupted_error(self):
        cancelled=threading.Event();cancelled.set()
        with self.gateway():
            service=WebKnowledge(self.root)
            with self.assertRaises(InterruptedError):
                service.search_tool('版本',cancelled=cancelled)
            with self.assertRaises(InterruptedError):
                service.fetch_tool('https://example.com/page',cancelled=cancelled)
        self.assertEqual(self.total_hits(),0)

    # legacy text API keeps returning failure text instead of raising
    def test_legacy_fetch_returns_failure_text_and_reads_public_page(self):
        text=WebKnowledge(self.root).fetch('http://127.0.0.1/x')
        self.assertIn('本轮网页读取失败',text)
        self.assertIn('不能声称已读取',text)
        self.assertEqual(self.total_hits(),0)
        with self.gateway(),self.allow_public():
            text=WebKnowledge(self.root).fetch('https://example.com/page')
        self.assertIn('[已读取网页：Stub Page]',text)
        self.assertEqual(self.hits(FETCH),1)


if __name__=='__main__':
    unittest.main()
