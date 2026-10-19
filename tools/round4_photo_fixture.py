"""Enable one real local photo request, charged against the actual role quota."""
import json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
from fish_desktop.services import load_user_env
from secrets_store import SecretStore
from model_gateway import ModelLedger
from storage import read_json,write_json
load_user_env();out=ROOT/'artifacts/round4';root=out/'migrated-data'
config=read_json(root/'harness/control.json');config['max_model_requests_daily']=1;write_json(root/'harness/control.json',config)
key=SecretStore(ROOT).get('agent-qq','FISH_QQ_DEEPSEEK_API_KEY')
if not key:raise RuntimeError('Protected QQ model key missing')
SecretStore(root).put('agent-qq',key)
previous=read_json(out/'photo-fixture.json')
if previous.get('root_ledger_reservation'):
    ModelLedger(ROOT,'qq').finish(previous['root_ledger_reservation'],status='preflight_failed_no_model_call')
rid=ModelLedger(ROOT,'qq').reserve('round4-native-local-photo')
env=os.environ.copy()
for name in list(env):
    if any(word in name.upper() for word in ('API_KEY','TOKEN','SECRET','PYTHONPATH','PYTHONHOME')):env.pop(name)
env.update(FISH_HARNESS_HOME=str(root),LOCALAPPDATA=str(out/'localappdata'),FISH_QQ_GROUP_REPLY='off')
exe=ROOT/'dist-round4-delivery/Fish/Fish.exe'
process=subprocess.Popen([str(exe)],env=env,cwd=root,creationflags=subprocess.CREATE_NO_WINDOW)
write_json(out/'photo-fixture.json',{'root':str(root),'pid':process.pid,'exe':str(exe),'root_ledger_reservation':rid,'key_value_logged':False,'real_qq_sends':0})
print('Final EXE launched with one model request cap; credentials remain DPAPI protected')
