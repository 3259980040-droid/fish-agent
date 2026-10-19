"""Exercise the frozen program with an isolated local root and synthetic key."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
from fish_desktop.bootstrap import initialize
from storage import read_json,write_json

def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def main():
    exe=ROOT/'dist-social-delivery-040/Fish/Fish.exe';out=ROOT/'artifacts/social-base'
    root=initialize(out/'packaged-fixture');cfg=read_json(root/'harness/agents.json')
    cfg['runtime']['ports']={'qq':port(),'research':port()};cfg['runtime']['tool_host_port']=port();write_json(root/'harness/agents.json',cfg)
    qq=read_json(root/'channels/fish_qq/config.json');qq['allowlist'].update(owner_id='123456789',group_ids=['987654321']);write_json(root/'channels/fish_qq/config.json',qq)
    write_json(root/'harness/setup.json',{'completed':True})
    env=os.environ.copy();env.update(FISH_HARNESS_HOME=str(root),LOCALAPPDATA=str(out/'packaged-profile'),
        FISH_QQ_DEEPSEEK_API_KEY='synthetic-startup-only-key',FISH_RESEARCH_DEEPSEEK_API_KEY='synthetic-startup-only-key',QT_QPA_PLATFORM='offscreen')
    for args,timeout in ((['--backend','diagnose','--check-runtime'],240),(['--ui-diagnostics',str(out/'packaged-ui-diagnostics.txt')],45),(['--screenshot',str(out/'packaged-ui')],150)):
        result=subprocess.run([str(exe),*args],cwd=root,env=env,timeout=timeout,capture_output=True,text=True,encoding='utf-8')
        if result.returncode:
            (out/'packaged-smoke-error.txt').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8');raise RuntimeError('Packaged smoke failed: '+str(args))
    result=read_json(root/'data/diagnostics/runtime-check.json')
    assert result['ok'] and result['frozen'] and result['version']=='0.4.0' and result['authenticated_runtime']
    assert result['runtime_startup_warning_codes']==[],result
    assert (out/'packaged-ui/social.png').is_file()
    diagnostic=(out/'packaged-ui-diagnostics.txt').read_text(encoding='utf-8');assert '人格与群聊' in diagnostic
    result.update(packaged_ui_rendered=True,paid_model_requests=0,real_qq_messages=0,root=str(root))
    write_json(out/'packaged-smoke.json',result);print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
