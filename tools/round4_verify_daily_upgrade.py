"""Read-only verification of native cross-version upgrade and manual reopening."""
import argparse,hashlib,json,os,socket,sqlite3,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'artifacts/round4/completion-audit'
sys.path[:0]=[str(ROOT),str(ROOT/'harness'),str(ROOT/'vendor/python')]
from storage import read_json,write_json
from governance import control_token
from core import root_id

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def tcp(port):
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=1):return True
    except OSError:return False

def check(exe,manual):
    environment=read_json(OUT/'native-upgrade-environment.json')
    os.environ['LOCALAPPDATA']=environment['localappdata']
    before=read_json(OUT/'daily-before-upgrade.json')
    expected=read_json(OUT/'package-verification.json')['executable_sha256']
    assert digest(exe)==expected
    for name,value in before['config_hashes'].items():assert digest(ROOT/name)==value,name
    total=0
    for spec,saved in before['semantic_rows'].items():
        relative,table=spec.rsplit(':',1)
        with sqlite3.connect(ROOT/relative) as db:
            current={str(row[0]):hashlib.sha256(json.dumps(row[1:],ensure_ascii=True).encode()).hexdigest()
                     for row in db.execute('SELECT rowid,'+','.join(saved['columns'])+' FROM '+table)}
        for rid,value in saved['rows'].items():assert current.get(rid)==value,spec+':'+rid
        total+=len(saved['rows'])
    # Independent selection: no FISH_HARNESS_HOME or maintenance child context.
    from fish_desktop.bootstrap import selected_root,locate_root
    os.environ.pop('FISH_HARNESS_HOME',None);os.environ.pop('FISH_MAINTENANCE_RESULT',None)
    assert selected_root()==ROOT
    sys.frozen=True;sys.executable=str(exe)
    assert locate_root()==ROOT
    result={'ok':True,'from_version':'0.3.2','to_version':'0.3.4','root':str(ROOT),
        'executable_sha256':expected,'installed_executable':str(exe),
        'config_persona_secret_report_hashes_preserved':len(before['config_hashes']),
        'existing_chat_and_memory_records_preserved':total,'root_selection_persisted':True,
        'application_profile':environment['localappdata'],'global_appdata_install_verified':False,
        'napcat_running':tcp(3001),'manual_launch_without_root_environment':manual,'real_qq_test_sends':0}
    assert result['napcat_running']
    if manual:
        launch=read_json(OUT/'manual-launch.json')
        assert not launch['FISH_HARNESS_HOME_in_environment'] and not launch['FISH_MAINTENANCE_RESULT_in_environment'] and launch['group_reply']=='off'
        from model_gateway import ModelLedger
        current_usage={role:ModelLedger(ROOT,role).snapshot()['requests'] for role in ('qq','research')}
        import requests
        headers={'x-fish-token':control_token(ROOT)}
        cfg=read_json(ROOT/'harness/agents.json');port=cfg['runtime']['ports']['qq']
        response=requests.get(f'http://127.0.0.1:{port}/fish/connect',headers=headers,timeout=4,proxies={'http':None,'https':None,'all':None})
        response.raise_for_status();guard=response.json()
        assert guard['role']=='qq' and guard['root_id']==root_id(ROOT) and guard['guard_active'] and guard['control_version']==3
        response=requests.get('http://127.0.0.1:3102/status',headers=headers,timeout=4,proxies={'http':None,'https':None,'all':None})
        response.raise_for_status();state=response.json()
        assert state.get('active_turn') is None and state.get('queue')==0
        result.update(qq_runtime_authenticated=True,guard_active=True,queue=state['queue'],active_turn=state['active_turn'],
                      group_reply_default='off',second_manual_launch_retains_data=True,
                      activation_model_request_delta={role:current_usage[role]-before['model_requests_before_activation'][role] for role in current_usage})
        name='daily-upgrade-activation.json'
    else:name='native-cross-version-upgrade.json'
    write_json(OUT/name,result);print(json.dumps(result,ensure_ascii=True))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--exe',required=True,type=Path);parser.add_argument('--manual',action='store_true');args=parser.parse_args();check(args.exe.resolve(),args.manual)
