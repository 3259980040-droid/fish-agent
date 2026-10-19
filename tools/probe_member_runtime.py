"""Actual Harness + synthetic local API: replace per-turn profiles, no paid calls."""
import asyncio
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import sys
import threading
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
import dependencies
from tools.probe_social_runtime import free_port
from fish_desktop.bootstrap import initialize
from storage import read_json,write_json
from core import HarnessConfig,RuntimeManager
from secrets_store import SecretStore
from social_context import SocialContext
from runtime_adapter import DSHRuntimeAdapter
from tool_host import bind_session,set_group_context


def main():
    out=ROOT/'artifacts/social-members'/('runtime-'+str(int(time.time())));root=initialize(out/'root');received=[]
    class Provider(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))));received.append(data)
            message={'id':'msg_'+str(len(received)),'type':'message','role':'assistant','model':'deepseek-flash','content':[], 'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':30,'output_tokens':6}}
            if data.get('stream'):
                events=[('message_start',{'type':'message_start','message':message}),('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'Synthetic reply.'}}),('content_block_stop',{'type':'content_block_stop','index':0}),('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':6}}),('message_stop',{'type':'message_stop'})]
                body=''.join('event: '+kind+'\ndata: '+json.dumps(value)+'\n\n' for kind,value in events).encode();ctype='text/event-stream'
            else:
                body=json.dumps({**message,'content':[{'type':'text','text':'Title'}],'stop_reason':'end_turn'}).encode();ctype='application/json'
            self.send_response(200);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Provider);threading.Thread(target=server.serve_forever,daemon=True).start()
    raw=read_json(root/'harness/agents.json');raw['runtime']['tool_host_port']=free_port();raw['runtime']['ports']['qq']=free_port()
    for a in raw['agents']:
        if a['id']=='qq':a.update(base_url='http://127.0.0.1:'+str(server.server_port),model='deepseek-flash',provider='deepseek-official')
    write_json(root/'harness/agents.json',raw)
    cfg=read_json(root/'channels/fish_qq/config.json');cfg['allowlist'].update(owner_id='123456789',group_ids=['987654321']);write_json(root/'channels/fish_qq/config.json',cfg)
    SecretStore(root).put('agent-qq','local-synthetic-key');manager=RuntimeManager(HarnessConfig(root))
    result={'ok':False,'paid_requests':0,'qq_sends':0,'root':str(root)}
    async def probe():
        contexts=SocialContext(root);p=contexts.ensure_default('qq');s=contexts.create_space('qq',revision=p['id'],origin='owner-qq')
        contexts.record_owner_reply(s['id'],'123456789');contexts.approve(p['id'],'123456789');contexts.deploy('987654321',p['id'],'123456789',enabled=True)
        client=await asyncio.to_thread(DSHRuntimeAdapter(root,'qq',manager).connect,require_group_context=True)
        await client.auth();directory=root/'data/qq/groups/probe';directory.mkdir(parents=True,exist_ok=True)
        session=await client.create_session(cwd=str(directory));bind_session(root,'qq',session,'chat:group:987654321','123456789')
        for mark in ('A','B'):
            set_group_context(root,session,'EPHEMERAL_PROFILE_'+mark+'_7029')
            baseline=await client.baseline(session);await client.prompt(session,[{'type':'text','text':'TURN_'+mark+'_6038'}])
            reply=await client.wait_response(session,baseline,timeout=90)
            assert reply.get('ok'),reply
        formal=[d for d in received if d.get('tools') and 'TURN_B_6038' in json.dumps(d)]
        assert formal,'second formal request missing'
        # Native provider metadata includes logged delivery records; it is outside
        # model input. Count only the messages/system/tools the model receives.
        last=json.dumps({k:formal[-1].get(k) for k in ('messages','system','tools')})
        assert 'EPHEMERAL_PROFILE_B_7029' in last,'current profile absent'
        assert 'EPHEMERAL_PROFILE_A_7029' not in last,'old profile persisted in request'
        assert last.count('EPHEMERAL_PROFILE_B_7029')==1,'profile repeated'
        assert 'TURN_A_6038' in last,'ordinary conversation history lost'
        from request_inspector import RequestInspector
        captured=RequestInspector(root).latest('qq',session)['payload']['messages']
        assert captured[0]['role']=='system','context displaced the native system prefix'
        result.update(ok=True,provider_requests=len(received),current_profile_once=True,previous_profile_removed=True,ordinary_history_retained=True)
    try:asyncio.run(probe())
    finally:
        manager.stop('qq');server.shutdown();server.server_close()
        from tool_host import _hosts
        host=_hosts.get(str(root.resolve()))
        if host:host.stop.set()
        write_json(out/'provider-requests.json',received)
        write_json(out/'result.json',result);print(json.dumps(result),flush=True)
if __name__=='__main__':main()
