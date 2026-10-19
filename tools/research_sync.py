"""Research 收藏夹 CLI（Phase 2 最小闭环）。

示例：
  python tools/research_sync.py --list-favorites
  python tools/research_sync.py --select-media-id 123456
  python tools/research_sync.py --sync --limit 3
  python tools/research_sync.py --show
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))

from bilibili_source import (  # noqa: E402
    BilibiliSourceAdapter,
    FixtureBilibiliAdapter,
    SourceAuthError,
    SourceError,
)
from research_pipeline import LocalVideoAnalyzer, sync_favorites  # noqa: E402
from research_store import ResearchStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Research 收藏夹最小闭环")
    parser.add_argument("--list-favorites", action="store_true")
    parser.add_argument("--select-media-id", type=int)
    parser.add_argument("--title", default="")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--fixture", help="离线 fixture JSON；仅用于测试")
    args = parser.parse_args()

    store = ResearchStore(ROOT)
    if args.fixture:
        adapter = FixtureBilibiliAdapter(json.loads(Path(args.fixture).read_text(encoding="utf-8")))
    else:
        adapter = BilibiliSourceAdapter(ROOT)

    try:
        if args.list_favorites:
            print(json.dumps(adapter.list_favorites(), ensure_ascii=False, indent=2))
            return 0

        if args.select_media_id:
            title = args.title
            if not title:
                for item in adapter.list_favorites():
                    if int(item["media_id"]) == int(args.select_media_id):
                        title = item.get("title", "")
                        break
            store.select_favorite(args.select_media_id, title or f"media_id={args.select_media_id}")
            print(json.dumps(store.selected_favorite(), ensure_ascii=False, indent=2))
            return 0

        if args.sync:
            analyzer = LocalVideoAnalyzer(ROOT)
            result = sync_favorites(store, adapter, analyzer, limit=args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "completed" else 1

        if args.show:
            print(json.dumps({
                "favorite": store.selected_favorite(),
                "counts": store.counts(),
                "sources": store.list_sources(),
            }, ensure_ascii=False, indent=2))
            return 0
    except SourceAuthError as exc:
        print(f"B 站未登录：{exc}", file=sys.stderr)
        return 2
    except SourceError as exc:
        print(f"B 站读取失败：{exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
