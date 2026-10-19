"""Real semantic review of a labeled offline source; never contacts QQ."""
import json
from pathlib import Path
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
from fish_desktop.bootstrap import initialize
from research_autonomy import ResearchControl, ResearchReviewer, ResearchWorker
from memory_store import MemoryStore
from model_gateway import ModelGateway
from fish_desktop.services import load_user_env

TEXT = '''[00:00:05] 本样例比较缓存带来的性能收益和一致性代价。
[00:00:20] 单机原始查询平均耗时为 120 毫秒；加入本地缓存后，同样的重复查询平均耗时降到 18 毫秒。
[00:00:40] 这组测量只包含重复读取，没有测量不同硬件、多实例部署或高并发场景，不能把结果推广到所有系统。
[00:01:00] 如果缓存只靠十分钟过期，更新后的数据可能暂时仍显示旧值。采用写入后失效可缩短这一窗口，但仍需考虑并发更新和失败补偿。
[00:01:20] 作者认为应先明确可接受的旧数据窗口，再决定是否缓存，并同时记录命中率与过期读取比例。'''

class Source:
    def get_video(self, bvid): return {'bvid': bvid, 'title': '【离线验收样例】缓存性能与一致性', 'url': 'https://www.bilibili.com/video/' + bvid, 'author': '测试夹具，非真实视频', 'duration': 90, 'cid': 1}
    def get_subtitle(self, *args): return {'text': TEXT}


def main():
    sys.stdout.reconfigure(encoding='utf-8'); load_user_env()
    out = ROOT / 'artifacts/round2'; out.mkdir(parents=True, exist_ok=True)
    gateway = ModelGateway(ROOT, 'research'); before = gateway.ledger.snapshot()
    with tempfile.TemporaryDirectory(prefix='research-semantic-', dir=out) as temporary:
        root = initialize(Path(temporary)); control = ResearchControl(root)
        control.configure({'directions': ['理解软件性能优化中的实际代价与取舍'], 'auto': False})
        control.enqueue(Source().get_video('BV0000000000'), origin='主人指定')
        worker = ResearchWorker(root, adapter=Source(), reviewer=ResearchReviewer(root, gateway))
        outcome = worker.process(control.claim())
        assert outcome['ok'], outcome.get('error')
        report = control.detail('BV0000000000')['report']
        assert report['summary'] != TEXT[:240] and report['opinion'] and report['uncertainties']
        assert report['evidence'] and all(e['quote'] in TEXT for e in report['evidence'])
        assert MemoryStore(root, 'research').search() and not MemoryStore(root, 'qq').search()
        result = {'ok': True, 'fixture': 'labeled offline transcript, not a watched Bilibili video', 'real_deepseek_semantic_review': True,
            'job_status': control.snapshot()['jobs'][0]['status'], 'source_quotes_verified': len(report['evidence']), 'report': report,
            'usage_before': before, 'usage_after': gateway.ledger.snapshot(), 'role_memory_isolated': True, 'real_qq_sends': 0}
        (out / 'research-semantic-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in result.items() if k not in ('report', 'usage_before', 'usage_after')}, ensure_ascii=False))


if __name__ == '__main__': main()
