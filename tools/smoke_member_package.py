"""Exercise the frozen program with an isolated local root and synthetic key."""
import argparse
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
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,default=ROOT/'dist-social-members-041/Fish/Fish.exe')
    parser.add_argument('--out',type=Path,default=ROOT/'artifacts/social-members')
    parser.add_argument('--version',default='0.4.1')
    args=parser.parse_args()
    exe=args.exe.resolve();out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    root=initialize(out/'packaged-fixture');cfg=read_json(root/'harness/agents.json')
    cfg['runtime']['ports']={'qq':port(),'research':port()};cfg['runtime']['tool_host_port']=port();write_json(root/'harness/agents.json',cfg)
    qq=read_json(root/'channels/fish_qq/config.json');qq['allowlist'].update(owner_id='123456789',group_ids=['987654321']);write_json(root/'channels/fish_qq/config.json',qq)
    write_json(root/'harness/setup.json',{'completed':True})
    env=os.environ.copy();env.update(FISH_HARNESS_HOME=str(root),LOCALAPPDATA=str(out/'packaged-profile'),
        FISH_QQ_DEEPSEEK_API_KEY='synthetic-startup-only-key',FISH_RESEARCH_DEEPSEEK_API_KEY='synthetic-startup-only-key',QT_QPA_PLATFORM='offscreen')
    roles={}
    checks=[(['--backend','diagnose','--check-runtime','--id',role],240) for role in ('qq','research')]
    checks += [(['--ui-diagnostics',str(out/'packaged-ui-diagnostics.txt')],45),(['--screenshot',str(out/'packaged-ui')],150)]
    for command,timeout in checks:
        result=subprocess.run([str(exe),*command],cwd=root,env=env,timeout=timeout,capture_output=True,text=True,encoding='utf-8')
        if result.returncode:
            (out/'packaged-smoke-error.txt').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8');raise RuntimeError('Packaged smoke failed: '+str(command))
        if '--id' in command:
            checked=read_json(root/'data/diagnostics/runtime-check.json')
            assert checked['ok'] and checked['frozen'] and checked['version']==args.version and checked['authenticated_runtime'],checked
            assert checked['runtime_startup_warning_codes']==[],checked
            roles[command[-1]]=checked
    result=read_json(root/'data/diagnostics/runtime-check.json')
    assert result['ok'] and result['frozen'] and result['version']==args.version and result['authenticated_runtime']
    assert result['runtime_startup_warning_codes']==[],result
    assert (out/'packaged-ui/social.png').is_file()
    diagnostic=(out/'packaged-ui-diagnostics.txt').read_text(encoding='utf-8');assert '人格与群聊' in diagnostic
    result.update(packaged_ui_rendered=True,paid_model_requests=0,real_qq_messages=0,root=str(root),roles=roles)
    write_json(out/'packaged-smoke.json',result);print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
