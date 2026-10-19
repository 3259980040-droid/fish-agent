"""Phase 2 最小闭环验收：3 个视频 + 第二次同步 + 新增 + 失败重试。"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from bilibili_source import FixtureBilibiliAdapter
from research_pipeline import sync_favorites
from research_store import ResearchStore

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        FAILS.append(name)


class FakeAnalyzer:
    def __init__(self, fail_bvids: set[str] | None = None):
        self.fail_bvids = fail_bvids or set()
        self.calls: list[str] = []

    def analyze(self, url: str, bvid: str) -> dict:
        self.calls.append(bvid)
        if bvid in self.fail_bvids:
            return {"ok": False, "error": "fake analyzer failure"}
        return {"ok": True, "text": f"{bvid} 的本地视频转写：这是无字幕路径的测试文本。", "warnings": []}


def main() -> int:
    base = Path(__file__).resolve().parent / ".phase2-tmp"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    root = base / "run"
    root.mkdir(parents=True, exist_ok=True)

    fixture = json.loads((Path(__file__).resolve().parent / "fixtures" / "research_favorites.json").read_text(encoding="utf-8"))
    adapter = FixtureBilibiliAdapter(fixture)
    store = ResearchStore(root)
    store.select_favorite(7001, "Research 测试收藏夹")

    analyzer = FakeAnalyzer()
    first = sync_favorites(store, adapter, analyzer)
    check("第一次同步：3 条处理", first["processed"] == 3 and first["failed"] == 0, str(first))
    check("第一次同步：observation/memory 各 3 条", store.counts() == {"sources": 3, "observations": 3, "memories": 3}, str(store.counts()))

    rows = {row["bvid"]: row for row in store.list_sources()}
    check("A 有字幕 → subtitle_read", rows["BV1TEST00001"]["observation_level"] == "subtitle_read")
    check("C 有字幕 → subtitle_read", rows["BV1TEST00003"]["observation_level"] == "subtitle_read")
    check("B 无字幕 → video_analyzed", rows["BV1TEST00002"]["observation_level"] == "video_analyzed")
    check("B 的 processed 含 transcript", "transcript" in json.loads(rows["BV1TEST00002"]["processed_modalities"]))
    check("字幕优先：A/C 没调用 analyzer", "BV1TEST00001" not in analyzer.calls and "BV1TEST00003" not in analyzer.calls)
    check("B 调用了 analyzer", "BV1TEST00002" in analyzer.calls)

    second = sync_favorites(store, adapter, analyzer)
    check("第二次同步：不重复", second["processed"] == 0 and second["skipped"] == 3, str(second))
    check("第二次同步：计数不变", store.counts() == {"sources": 3, "observations": 3, "memories": 3}, str(store.counts()))

    adapter.items["7001"].append({
        "bvid": "BV1TEST00004",
        "title": "测试视频 D：新增",
        "author": "测试UP-D",
        "duration": 64,
        "cover": "",
        "cid": 1004,
        "url": "https://www.bilibili.com/video/BV1TEST00004",
        "subtitle": "视频 D 的字幕：只处理新增项。",
    })
    third = sync_favorites(store, adapter, analyzer)
    check("新增一个视频：只处理 1 条", third["processed"] == 1 and third["skipped"] == 3, str(third))

    adapter.items["7001"].append({
        "bvid": "BV1TEST00005",
        "title": "测试视频 E：分析失败",
        "author": "测试UP-E",
        "duration": 65,
        "cover": "",
        "cid": 1005,
        "url": "https://www.bilibili.com/video/BV1TEST00005",
        "subtitle": None,
    })
    analyzer.fail_bvids.add("BV1TEST00005")
    before_memories = store.counts()["memories"]
    failed_run = sync_favorites(store, adapter, analyzer)
    check("单条失败不中断整批", failed_run["failed"] == 1 and failed_run["skipped"] == 4, str(failed_run))
    rows = {row["bvid"]: row for row in store.list_sources()}
    row_e = rows["BV1TEST00005"]
    check("失败记录 metadata_only", row_e["status"] == "failed" and row_e["observation_level"] == "metadata_only")
    check("失败 warnings 含 analyzer_failed", "video_analyzer_failed" in row_e["warnings"])
    check("metadata 不冒充已看：失败不写 memory", store.counts()["memories"] == before_memories, str(store.counts()))

    analyzer.fail_bvids.discard("BV1TEST00005")
    retry = sync_favorites(store, adapter, analyzer)
    rows = {row["bvid"]: row for row in store.list_sources()}
    check("失败可重试：E 成功", retry["processed"] == 1 and rows["BV1TEST00005"]["status"] == "done", str(retry))
    check("重试后 level=video_analyzed", rows["BV1TEST00005"]["observation_level"] == "video_analyzed")
    check("重试后写入 memory", store.counts()["memories"] == before_memories + 1, str(store.counts()))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
