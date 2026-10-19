"""Real bundled Harness + local synthetic provider; no paid API or QQ sends."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
import dependencies
from fish_desktop.bootstrap import initialize
from storage import read_json,write_json
from core import HarnessConfig,RuntimeManager
from secrets_store import SecretStore
from social_context import SocialContext
from desktop_chat import DesktopChat
from request_inspector import RequestInspector


def free_port():
    with closing(socket.socket()) as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def main():
    fixture=ROOT/'artifacts/social-base/runtime-probe'/str(int(time.time()))
    root=initialize(fixture); received=[]
    class Provider(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))));received.append(data)
            message={'id':'msg_local_'+str(len(received)),'type':'message','role':'assistant','model':'deepseek-flash','content':[{'type':'text','text':'本地模拟接收完成。'}], 'stop_reason':'end_turn','stop_sequence':None,'usage':{'input_tokens':30,'output_tokens':6}}
            if data.get('stream'):
                events=[('message_start',{'type':'message_start','message':{**message,'content':[],'stop_reason':None}}),
                    ('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),
                    ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'本地模拟接收完成。'}}),
                    ('content_block_stop',{'type':'content_block_stop','index':0}),
                    ('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':6}}),
                    ('message_stop',{'type':'message_stop'})]
                body=''.join('event: '+kind+'\ndata: '+json.dumps(value,ensure_ascii=False)+'\n\n' for kind,value in events).encode()
                self.send_response(200);self.send_header('Content-Type','text/event-stream')
            else:
                body=json.dumps(message,ensure_ascii=False).encode();self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Provider);threading.Thread(target=server.serve_forever,daemon=True).start()
    raw=read_json(root/'harness/agents.json');raw['runtime']['tool_host_port']=free_port()
    raw['runtime']['ports']['qq']=free_port()
    for a in raw['agents']:
        if a['id']=='qq': a.update(base_url='http://127.0.0.1:'+str(server.server_port),model='deepseek-flash',provider='deepseek-official')
    write_json(root/'harness/agents.json',raw)
    (root/'harness/agents/qq.persona.md').write_text('UNWANTED_SHARED_PERSONA_MARKER',encoding='utf-8')
    (root/'harness/global/AGENTS.md').write_text('UNWANTED_SHARED_GLOBAL_MARKER',encoding='utf-8')
    SecretStore(root).put('agent-qq','synthetic-local-provider-key')
    manager=RuntimeManager(HarnessConfig(root));result={'root':str(root),'paid_requests':0,'qq_messages_sent':0}
    try:
        contexts=SocialContext(root)
        p=contexts.save_persona('qq','A','ISOLATED_PERSONA_A',global_rules='ISOLATED_GLOBAL_A',workspace_rules='ISOLATED_WORKSPACE_A')
        q=contexts.save_persona('qq','B','ISOLATED_PERSONA_B',global_rules='ISOLATED_GLOBAL_B',workspace_rules='ISOLATED_WORKSPACE_B')
        chat=DesktopChat(root,'qq',manager);a=chat.new('A',revision=p['id']);b=chat.new('B',revision=q['id'])
        for cid,prompt in ((a,'CANARY_WINDOW_A_902'),(b,'CANARY_WINDOW_B_903')):
            chat.send(cid,prompt,[])
        relevant=[data for data in received if data.get('tools') and any('CANARY_WINDOW_' in json.dumps(m,ensure_ascii=False) for m in data.get('messages',[]))]
        assert len(relevant)>=2,'local provider did not receive both prompts'
        a_req=next(d for d in relevant if 'CANARY_WINDOW_A_902' in json.dumps(d))
        b_req=next(d for d in relevant if 'CANARY_WINDOW_B_903' in json.dumps(d))
        for data in (a_req,b_req):
            assert 'UNWANTED_SHARED_' not in json.dumps(data),'shared mutable instruction file leaked into isolated request'
        assert 'CANARY_WINDOW_A_902' not in json.dumps(b_req),'window A history leaked into B'
        assert 'ISOLATED_PERSONA_A' not in json.dumps(b_req),'window A persona leaked into B'
        for cid in (a,b):
            context=chat.conversation_context(cid);capture=RequestInspector(root).latest('qq',context['session'])
            assert capture['captured'],'actual model request snapshot missing'
        result.update(ok=True,provider_requests=len(received),conversation_requests=len(relevant),separate_sessions=chat.conversation_context(a)['session']!=chat.conversation_context(b)['session'])
    except Exception as exc:
        result.update(ok=False,error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        manager.stop('qq');server.shutdown();server.server_close()
        from tool_host import _hosts
        host=_hosts.get(str(root.resolve()))
        if host:host.stop.set()
        write_json(fixture/'probe-result.json',result);print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
