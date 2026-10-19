"""Upgrade idle, authenticated owner-only backends; preserve the open GUI."""
import json,os,sys,subprocess,time,hashlib
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
from fish_desktop.services import load_user_env,tcp_open
from governance import control_token
from secrets_store import SecretStore
from core import HarnessConfig,RuntimeManager,root_id
import requests


def main():
    load_user_env();out=ROOT/'artifacts/round3';headers={'x-fish-token':control_token(ROOT)};direct={'http':None,'https':None,'all':None}
    status=requests.get('http://127.0.0.1:3102/status',headers=headers,timeout=3,proxies=direct);status.raise_for_status();state=status.json()
    if state['active_turn'] or state['queue']:raise RuntimeError('仍有正在处理的消息，暂不切换')
    from channels.fish_qq.spool import Outbox
    if any(r['status'] in ('queued','inflight') for r in Outbox(ROOT/'channel-state').all()):raise RuntimeError('仍有通道待发送项，暂不切换')
    promote='--promote-final' in sys.argv
    old_exe=ROOT/('dist-round3/Fish/Fish.exe' if promote else 'dist-round2/Fish/Fish.exe')
    if promote:
        staged=ROOT/'dist-round3-final/Fish';zip_path=ROOT/'dist-round3-final/Fish-0.3.1-round3.zip'
        frozen=json.loads((out/'packaged-runtime-check.json').read_text(encoding='utf-8'))
        if not zip_path.is_file() or not frozen['ok'] or frozen.get('executable_sha256')!=hashlib.sha256((staged/'Fish.exe').read_bytes()).hexdigest():raise RuntimeError('最终成品或冻结验证尚未就绪')
    command="Get-CimInstance Win32_Process -Filter \"Name='Fish.exe'\" | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    entries=json.loads(subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],capture_output=True,text=True,encoding='utf-8',errors='replace',check=True).stdout)
    entries=[entries] if isinstance(entries,dict) else entries
    old=[e for e in entries if e['ExecutablePath'].casefold()==str(old_exe).casefold() and '--backend' in e['CommandLine']]
    if len(old)!=2:raise RuntimeError('旧后台状态和预期不符，未停止任何进程')
    requests.post('http://127.0.0.1:3102/shutdown',headers=headers,timeout=3,proxies=direct).raise_for_status()
    deadline=time.monotonic()+12
    while tcp_open('127.0.0.1',3102) and time.monotonic()<deadline:time.sleep(.1)
    # Every stop target was identified by its executable and backend argument.
    for entry in old:
        pid=int(entry['ProcessId'])
        script=f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; if ($p -and $p.ExecutablePath -eq '{old_exe}' -and $p.CommandLine -like '*--backend*') {{ Stop-Process -Id {pid} -ErrorAction Stop }}"
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    RuntimeManager(HarnessConfig(ROOT)).stop('qq')
    deadline=time.monotonic()+12
    while any(tcp_open('127.0.0.1',p) for p in (3090,3101,3102,3103)) and time.monotonic()<deadline:time.sleep(.1)
    if any(tcp_open('127.0.0.1',p) for p in (3090,3101,3102,3103)):raise RuntimeError('旧服务端口未释放；未启动重复服务')
    previous=None
    if promote:
        # Only task-created release folders move; the separate user data root
        # and the open first-round GUI are never moved or removed.
        previous=ROOT/'dist-round3/intermediate-Fish-before-final'
        destination=ROOT/'dist-round3/Fish'
        for path in (staged,previous,destination,zip_path):
            if not path.resolve().is_relative_to(ROOT.resolve()) or path.resolve()==ROOT.resolve():raise ValueError('交付路径超出项目')
        if previous.exists():raise FileExistsError('中间目录已存在，未覆盖')
        destination.rename(previous)
        try:staged.rename(destination)
        except BaseException:
            previous.rename(destination);raise
        zip_path.replace(ROOT/'dist-round3/Fish-0.3.1-round3.zip')
    env=os.environ.copy();env.update(FISH_HARNESS_HOME=str(ROOT),FISH_QQ_GROUP_REPLY='off')
    for key,ref in [('FISH_QQ_TOKEN','channel-core'),('NAPCAT_TOKEN','channel-napcat')]:
        value=SecretStore(ROOT).get(ref,key)
        if value:env[key]=value
    exe=ROOT/'dist-round3/Fish/Fish.exe';processes=[]
    for kind in ('fish-qq','qq-agent-loop'):
        args=[str(exe),'--backend',kind]
        if kind=='fish-qq':args+=['--config',str(ROOT/'channels/fish_qq/config.json')]
        with (out/(kind+'-0.3.1-launch.log')).open('a',encoding='utf-8') as log:
            processes.append(subprocess.Popen(args,cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP))
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        if any(p.poll() is not None for p in processes):raise RuntimeError('新版后台提前退出，请查看后台日志')
        try:
            r=requests.get('http://127.0.0.1:3090/fish/connect',headers=headers,timeout=2,proxies=direct);d=r.json()
            if r.ok and d.get('root_id')==root_id(ROOT) and d.get('control_version')==3 and tcp_open('127.0.0.1',3102):
                result={'ok':True,'version':'0.3.1','executable':str(exe),'executable_sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),'backend_pids':[p.pid for p in processes],'owner_only':True,'group_reply':'off','napcat_still_connected':tcp_open('127.0.0.1',3001),'ui_unchanged':True,'model_calls_started_by_activation':0,'messages_sent_by_activation':0}
                (out/'live-activation-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result));return
        except (requests.RequestException,ValueError):pass
        time.sleep(.3)
    raise TimeoutError('新后台未在等待时间内就绪')


if __name__=='__main__':main()
