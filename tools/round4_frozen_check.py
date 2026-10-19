"""Exercise the final EXE in a fresh root with inert credentials and no model calls."""
import argparse,hashlib,json,os,subprocess,tempfile,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
exe=ROOT/'dist-round4-delivery/Fish/Fish.exe';out=ROOT/'artifacts/round4'
parser=argparse.ArgumentParser();parser.add_argument('--folder',type=Path);parser.add_argument('--output',type=Path);args=parser.parse_args()
if args.folder:exe=args.folder.resolve()/'Fish.exe'
if args.output:out=args.output.resolve()
out.mkdir(parents=True,exist_ok=True)
env=os.environ.copy()
for key in list(env):
    if any(word in key.upper() for word in ('API_KEY','TOKEN','SECRET','PYTHONPATH','PYTHONHOME')):env.pop(key)
with tempfile.TemporaryDirectory(prefix='frozen-round4-',dir=out) as temporary:
    root=Path(temporary);env.update(FISH_HARNESS_HOME=str(root),FISH_QQ_DEEPSEEK_API_KEY='synthetic-no-model',FISH_RESEARCH_DEEPSEEK_API_KEY='synthetic-no-model')
    subprocess.run([str(exe),'--backend','diagnose'],env=env,cwd=root,check=True,timeout=40)
    sys.path[:0]=[str(ROOT/'harness'),str(ROOT/'vendor/python')]
    from backup import BackupManager
    archive=Path(temporary)/'acceptance-backup.zip';restored=Path(temporary)/'restored'
    BackupManager(root).create(archive);BackupManager(restored).restore(archive)
    assert not (restored/'homes/qq').exists()
    root=restored;env['FISH_HARNESS_HOME']=str(root)
    path=root/'harness/agents.json';cfg=json.loads(path.read_text(encoding='utf-8'))
    cfg['runtime']['ports']={'qq':3394,'research':3395};cfg['runtime']['tool_host_port']=3396
    path.write_text(json.dumps(cfg),encoding='utf-8')
    subprocess.run([str(exe),'--backend','diagnose','--check-runtime'],env=env,cwd=root,check=True,timeout=280)
    result=json.loads((root/'data/diagnostics/runtime-check.json').read_text(encoding='utf-8'))
    assert result['ok'] and result['frozen'] and result['authenticated_runtime']
    assert not result['runtime_startup_warning_codes']
    subprocess.run([str(exe),'--screenshot',str(out/'packaged-ui')],env=env,cwd=root,check=True,timeout=150)
    assert (root/'homes/qq').is_dir() and (root/'harness/runtime/fish-control/cordis.patch.yml').is_file()
    result.update(executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),restored_empty_homes_and_control_bundle_repaired=True,real_model_calls=0,real_qq_sends=0,clean_windows_vm_tested=False)
    (out/'packaged-runtime-check.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
