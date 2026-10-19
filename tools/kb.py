#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb.py — 个人知识库 CLI（零第三方依赖，Python 3.10+）

知识库根目录由环境变量 KB_ROOT 指定，默认是脚本上一级的 knowledge/。

用法:
  python kb.py add --type video --title "..." --url "..." --tags llm,agent --rating 4 --summary "..."
  python kb.py search "关键词" [--type video] [--tag llm] [--limit 10]
  python kb.py signal --id 20261017-xxx --kind like --reason "..."
  python kb.py digest [--date 2026-10-17]
  python kb.py stats
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(
    os.environ.get("KB_ROOT") or (Path(__file__).resolve().parent.parent / "knowledge")
).resolve()
NOTES = ROOT / "notes"
INDEX = ROOT / "index.json"
SIGNALS = ROOT / "profile" / "signals.jsonl"

TYPES = ("video", "repo", "topic", "note")
SIGNAL_KINDS = ("like", "skip", "save", "engaged", "note")


def eprint(*args) -> None:
    print(*args, file=sys.stderr)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str, limit: int = 32) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:limit] or "note"


def new_id(kind: str, title: str) -> str:
    base = f"{date.today():%Y%m%d}-{slugify(title)}"
    folder = NOTES / kind
    candidate = base
    n = 2
    while (folder / f"{candidate}.md").exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def parse_scalar(raw: str):
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip('"').strip("'") for x in inner.split(",")]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    return value.strip('"').strip("'")


def read_frontmatter(path: Path) -> dict:
    meta: dict = {}
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end == -1:
        return meta
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = parse_scalar(value)
    return meta


def save_index(data: dict) -> None:
    NOTES.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def rebuild_index() -> dict:
    notes: dict = {}
    for path in NOTES.rglob("*.md"):
        meta = read_frontmatter(path)
        if not meta.get("id"):
            continue
        meta["path"] = str(path.relative_to(ROOT)).replace("\\", "/")
        notes[str(meta["id"])] = meta
    data = {"version": 1, "notes": notes}
    save_index(data)
    return data


def load_index() -> dict:
    if INDEX.exists():
        try:
            data = json.loads(INDEX.read_text(encoding="utf-8"))
            if isinstance(data.get("notes"), dict):
                return data
        except (json.JSONDecodeError, OSError):
            eprint("! index.json 不可读，正在重建")
    return rebuild_index()


def write_note(meta: dict, body: str) -> Path:
    folder = NOTES / str(meta.get("type", "note"))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{meta['id']}.md"

    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            value = "[" + ", ".join(str(x) for x in value) + "]"
        lines.append(f"{key}: {value}")
    lines.extend(["---", ""])
    path.write_text("\n".join(lines) + body.strip() + "\n", encoding="utf-8")
    return path


def cmd_add(args) -> int:
    if args.type not in TYPES:
        eprint(f"type 必须是 {TYPES} 之一")
        return 2

    body = ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body:
        body = args.body
    elif not sys.stdin.isatty():
        body = sys.stdin.read()

    meta = {
        "id": args.id or new_id(args.type, args.title),
        "type": args.type,
        "title": args.title,
        "url": args.url or "",
        "author": args.author or "",
        "captured": args.date or date.today().isoformat(),
        "tags": [t.strip() for t in (args.tags or "").split(",") if t.strip()],
        "rating": args.rating if args.rating is not None else "",
        "summary": args.summary or "",
        "source": args.source or "",
    }

    path = write_note(meta, body)
    index = load_index()
    meta["path"] = str(path.relative_to(ROOT)).replace("\\", "/")
    index["notes"][meta["id"]] = meta
    save_index(index)
    print(meta["id"])
    return 0


def cmd_search(args) -> int:
    index = load_index()
    terms = [t.casefold() for t in args.query.split() if t.strip()]
    tag_filter = args.tag.casefold() if args.tag else ""

    hits = []
    for note in index["notes"].values():
        if args.type and note.get("type") != args.type:
            continue
        tags = [str(t).casefold() for t in note.get("tags", [])]
        if tag_filter and tag_filter not in tags:
            continue

        title = str(note.get("title", "")).casefold()
        summary = str(note.get("summary", "")).casefold()
        tag_text = " ".join(tags)

        score = 0
        for term in terms:
            if term in title:
                score += 5
            if term in tag_text:
                score += 3
            if term in summary:
                score += 2
        if terms and score == 0:
            continue
        hits.append((score, note))

    hits.sort(
        key=lambda item: (item[0], str(item[1].get("captured", ""))), reverse=True
    )
    for score, note in hits[: args.limit]:
        print(
            f"[{note.get('type')}] {note.get('title')}"
            f"  ({note.get('captured')})  {note.get('url')}"
        )
        if note.get("summary"):
            print(f"    {note['summary']}")

    if not hits:
        print("(no match)")
        return 1
    return 0


def cmd_signal(args) -> int:
    SIGNALS.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": now_iso(),
        "id": args.id,
        "kind": args.kind,
        "reason": args.reason or "",
        "weight": args.weight,
    }
    with SIGNALS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("ok")
    return 0


def cmd_digest(args) -> int:
    day = args.date or date.today().isoformat()
    index = load_index()
    notes = [n for n in index["notes"].values() if str(n.get("captured")) == day]
    notes.sort(key=lambda n: (str(n.get("type")), str(n.get("title"))))

    print(f"# {day} 知识入库 {len(notes)} 条\n")
    for note in notes:
        print(f"## {note.get('title')}")
        print(f"- 类型：{note.get('type')}｜评分：{note.get('rating') or '-'}")
        print(f"- 来源：{note.get('url')}")
        if note.get("summary"):
            print(f"- 摘要：{note['summary']}")
        print()
    return 0


def cmd_stats(args) -> int:
    index = load_index()
    notes = list(index["notes"].values())

    print(f"notes: {len(notes)}")
    by_type: dict = {}
    tags: dict = {}
    for note in notes:
        kind = str(note.get("type", "?"))
        by_type[kind] = by_type.get(kind, 0) + 1
        for tag in note.get("tags", []):
            tags[str(tag)] = tags.get(str(tag), 0) + 1

    for kind, count in sorted(by_type.items(), key=lambda item: -item[1]):
        print(f"  {kind}: {count}")

    print("top tags:")
    for tag, count in sorted(tags.items(), key=lambda item: -item[1])[:10]:
        print(f"  {tag}: {count}")

    if SIGNALS.exists():
        with SIGNALS.open(encoding="utf-8") as handle:
            print(f"signals: {sum(1 for _ in handle)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kb")
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="新增笔记")
    add.add_argument("--type", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--url", default="")
    add.add_argument("--author", default="")
    add.add_argument("--tags", default="")
    add.add_argument("--rating", type=int)
    add.add_argument("--summary", default="")
    add.add_argument("--source", default="")
    add.add_argument("--date", default="")
    add.add_argument("--id", default="")
    add.add_argument("--body", default="")
    add.add_argument("--body-file", default="")
    add.set_defaults(func=cmd_add)

    search = sub.add_parser("search", help="检索笔记")
    search.add_argument("query")
    search.add_argument("--type")
    search.add_argument("--tag")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=cmd_search)

    signal = sub.add_parser("signal", help="记录偏好信号")
    signal.add_argument("--id", required=True)
    signal.add_argument("--kind", required=True, choices=SIGNAL_KINDS)
    signal.add_argument("--reason", default="")
    signal.add_argument("--weight", type=float, default=1.0)
    signal.set_defaults(func=cmd_signal)

    digest = sub.add_parser("digest", help="生成当日摘要")
    digest.add_argument("--date", default="")
    digest.set_defaults(func=cmd_digest)

    stats = sub.add_parser("stats", help="统计")
    stats.set_defaults(func=cmd_stats)

    return parser


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
