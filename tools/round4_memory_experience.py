"""Final EXE embedded chat code + bundled runtime: memory correction after restart."""
import hashlib,importlib.abc,importlib.util,json,os,sqlite3,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];out=ROOT/'artifacts/round4';root=out/'migrated-data'
folder=ROOT/'dist-round4-delivery/Fish'
sys.path.insert(0,str(ROOT/'vendor/python'))
from PyInstaller.archive.readers import CArchiveReader
pyz=CArchiveReader(str(folder/'Fish.exe')).open_embedded_archive('PYZ.pyz')
owned={p.stem for p in (ROOT/'harness').glob('*.py')}
class FrozenModules(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    def find_spec(self,fullname,path=None,target=None):
        if fullname in pyz.toc and (fullname.split('.')[0] in owned or fullname.startswith(('fish_desktop','harness','channels.fish_qq'))):
            return importlib.util.spec_from_loader(fullname,self,is_package=pyz.toc[fullname][0]==1)
    def create_module(self,spec):return None
    def exec_module(self,module):
        module.__file__=str(folder/'_internal'/Path(*module.__name__.split('.')).with_suffix('.py'))
        exec(pyz.extract(module.__name__),module.__dict__)
sys.meta_path.insert(0,FrozenModules());sys._MEIPASS=str(folder/'_internal');sys.frozen=True;sys.executable=str(folder/'Fish.exe')
os.environ['FISH_HARNESS_HOME']=str(root)
from memory_store import MemoryStore
from desktop_chat import DesktopChat
from core import HarnessConfig,RuntimeManager
from model_gateway import ModelLedger
from storage import read_json,write_json
cfg=read_json(root/'harness/control.json');cfg['max_model_requests_daily']=14;write_json(root/'harness/control.json',cfg)
runtime=RuntimeManager(HarnessConfig(root));chat=DesktopChat(root,'qq',runtime)
store=MemoryStore(root,'qq');store.configure(False)
memory=store.save('R4校验暗号的当前值是青柠-5821。',source='round4:synthetic-memory',source_text='独立验收数据，不是用户偏好')
cid=chat.new('第四轮记忆纠正、重启与删除验收');checks={};answers={}
def ask(stage):
    actual=ModelLedger(ROOT,'qq');reservations=[actual.reserve('round4-memory-'+stage) for _ in range(3)]
    path=root/'data/qq/model-usage.db'
    with sqlite3.connect(path) as db:before={r[0] for r in db.execute('SELECT id FROM requests')}
    try:
        result=chat.send(cid,'本轮注入的当前有效长期记忆里，R4校验暗号的当前值是什么？没有有效记录就只回答“未保存”。禁止复用前文旧修订。只输出当前值，不调用工具。',[])
        answers[stage]=result['messages'][-1]['text']
    finally:
        with sqlite3.connect(path) as db:rows=[r for r in db.execute('SELECT id,status,input_tokens,output_tokens FROM requests') if r[0] not in before]
        for index,rid in enumerate(reservations):
            row=rows[index] if index<len(rows) else None
            actual.finish(rid,{'inputTokens':row[2],'outputTokens':row[3]} if row else {},row[1] if row else 'unused_test_budget_reservation')
    assert 1<=len(rows)<=3,'Expected no more than three model requests per memory question'
try:
    ask('initial');assert '青柠-5821' in answers['initial']
    store.save('R4校验暗号的当前值是椰青-7934。',memory_id=memory['id'])
    runtime.stop('qq');time.sleep(.5)
    ask('corrected_after_runtime_restart')
    assert '椰青-7934' in answers['corrected_after_runtime_restart'] and '青柠-5821' not in answers['corrected_after_runtime_restart']
    store.delete(memory['id']);ask('deleted')
    assert '未保存' in answers['deleted'] and not any(s in answers['deleted'] for s in ('5821','7934'))
    assert not any(s in MemoryStore(root,'research').context('R4校验暗号') for s in ('5821','7934'))
    checks.update(initial_memory_used=True,correction_after_runtime_restart=True,deletion_effective_same_conversation=True,research_private_isolation=True)
finally:
    result={'ok':len(checks)==4,'checks':checks,'answers':answers,'embedded_executable_sha256':hashlib.sha256((folder/'Fish.exe').read_bytes()).hexdigest(),'real_qq_sends':0,'method':'Embedded final EXE chat modules under test Python; actual bundled Node/DSH runtime'}
    write_json(out/'memory-experience-check.json',result)
    print(json.dumps(result,ensure_ascii=True))
