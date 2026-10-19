"""Real DeepSeek review with facts separated across three source chunks."""
from pathlib import Path
import json
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
from fish_desktop.bootstrap import initialize
from fish_desktop.services import load_user_env
from model_gateway import ModelGateway
from research_autonomy import ResearchControl,ResearchReviewer


def main():
    sys.stdout.reconfigure(encoding='utf-8');load_user_env()
    out=ROOT/'artifacts/round3';out.mkdir(exist_ok=True)
    filler='补充背景：本段说明样例使用合成数据，只有单机环境，没有真实生产测量；讨论应区分恢复目标、运行成本和已验证的事实。\n'
    text='[00:00:05] 甲组把每月备份改为每日备份，最大数据丢失窗口从30天缩短到1天，这个改动叫银杏计划。\n'+filler*260
    text+='[00:06:00] 乙组增加每周恢复演练。只有备份成功日志不能证明能恢复，必须实际恢复并核对数据内容。\n'+filler*260
    text+='[00:12:20] 丙组发现每日全量备份增加带宽和存储开销，建议工作日增量、周末全量；这个样例每月增加37元，不代表其他团队。\n'
    # Force three sections at the same boundaries used by the production reader.
    assert len(text)>28000
    (out/'long-research-source.txt').write_text(text,encoding='utf-8')
    gateway=ModelGateway(ROOT,'research');before=gateway.ledger.snapshot()
    with tempfile.TemporaryDirectory(prefix='long-review-',dir=out) as temporary:
        root=initialize(Path(temporary));ResearchControl(root).configure({'directions':['个人 AI 项目的备份可靠性和成本']})
        video={'bvid':'BV0000000003','title':'【离线验收资料】备份可靠性与成本的三组取舍','url':'https://www.bilibili.com/video/BV0000000003','author':'验收夹具，非真实视频'}
        report=ResearchReviewer(root,gateway).review(video,text,'subtitle_read')
        body=report['summary']+'\n'+'\n'.join(map(str,report['key_points']))+'\n'+report['opinion']
        checks={'daily_backup':any(k in body for k in ('每日','每天','1天','一天')),'restore_drill':any(k in body for k in ('恢复演练','恢复验证','实际恢复')),'incremental_tradeoff':'增量' in body,'scope_honest':report['observation_level']=='subtitle_read','quotes_exact':bool(report['evidence']) and all(e['quote'] in text for e in report['evidence'])}
        result={'ok':all(checks.values()),'checks':checks,'source_length':len(text),'chunks':(len(text)+13999)//14000,'report':report,'usage_before':before,'usage_after':gateway.ledger.snapshot(),'real_qq_sends':0}
        (out/'long-research-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k not in ('report','usage_before','usage_after')},ensure_ascii=False))


if __name__=='__main__':main()
