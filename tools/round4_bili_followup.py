"""Ask the research role about a saved real video using its normal local chat path."""
import hashlib,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness'),str(ROOT/'vendor/python')]
os.environ['FISH_HARNESS_HOME']=str(ROOT)
from fish_desktop.services import load_user_env
from core import HarnessConfig,RuntimeManager
from desktop_chat import DesktopChat
from research_autonomy import ResearchControl
from memory_store import MemoryStore
from storage import write_json
load_user_env();folder=ROOT/'dist-round4-delivery/Fish';sys._MEIPASS=str(folder/'_internal');sys.frozen=True;sys.executable=str(folder/'Fish.exe')
runtime=RuntimeManager(HarnessConfig(ROOT));chat=DesktopChat(ROOT,'research',runtime)
cid=chat.new('第四轮实际 B 站研究追问验收')
control=ResearchControl(ROOT);detail=control.detail('BV1C9Jq68Eru')
try:
    result=chat.send(cid,'请用已有研究检索或详情工具查一下刚才保存的截图提问插件 BV1C9Jq68Eru。它的操作顺序和价值是什么？请给来源链接，明确你实际读的是字幕还是看过完整画面，不确定之处不要补编。',[])
    answer=result['messages'][-1]['text']
    assert all(word in answer for word in ('字幕','BV1C9Jq68Eru','截图'))
    assert any(word in answer for word in ('隐藏','框选','输入框'))
    assert not any(row['source']=='https://www.bilibili.com/video/BV1C9Jq68Eru' for row in MemoryStore(ROOT,'qq').search('BV1C9Jq68Eru'))
    evidence={'ok':True,'conversation_id':cid,'answer':answer,'source_url':detail['report']['source_url'],'source_observation_level':detail['report']['observation_level'],'real_qq_sends':0,'research_only_memory':True,'executable_sha256':hashlib.sha256((folder/'Fish.exe').read_bytes()).hexdigest(),'method':'Normal DesktopChat using final bundled Node/DSH; actual saved research tools'}
    write_json(ROOT/'artifacts/round4/bilibili-followup-check.json',evidence)
    print(json.dumps(evidence,ensure_ascii=True))
finally:
    runtime.stop('research')
