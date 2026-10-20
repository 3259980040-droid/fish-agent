"""Finish the staged DSH/Fish copy and recreate project-owned Windows links.

Source files are read only. Live data is copied separately after shutdown.
Existing, mismatched destination links are kept in the manual cleanup tree.
"""
from pathlib import Path
import argparse
import json
import os
import re
import shutil
import time
import _winapi

BASE = Path('E:/Program Files/DSH')
ROOTS = {Path('F:/Documents/ChatGPT/deepseek/deepseek-harness'): BASE/'source/dsh',
         Path('F:/Downloads/dsh-personal'): BASE/'source/fish'}
REPORT = {'copied': 0, 'bytes': 0, 'links': 0, 'errors': [], 'external_links': []}
LINKS = []

def remap(value):
    raw = str(value).removeprefix('\\\\?\\')
    # Only Windows namespace prefixes may precede a drive. A URL such as
    # ws:// must never be mistaken for the drive-looking substring s:/.
    drive = re.search(r'[A-Za-z]:[\\/]', raw) if raw.startswith('\\') else None
    if drive: raw = raw[drive.start():]
    for old, new in ROOTS.items():
        for a, b in [(str(old), str(new)), (old.as_posix(), new.as_posix())]:
            if raw.casefold() == a.casefold() or raw.casefold().startswith(a.casefold() + ('/' if '/' in a else '\\')):
                return b + raw[len(a):]
    return raw

def copy_tree(src, dst, exclude=()):
    if src.is_junction() or src.is_symlink():
        LINKS.append((src, dst)); return
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for entry in src.iterdir():
            if entry.name in exclude or entry.name.startswith(('dist-', 'build-')):
                continue
            copy_tree(entry, dst/entry.name, exclude)
        return
    try:
        info = src.stat()
        if dst.is_file() and dst.stat().st_size == info.st_size and abs(dst.stat().st_mtime-info.st_mtime) < .01:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        REPORT['copied'] += 1; REPORT['bytes'] += info.st_size
    except OSError as exc:
        REPORT['errors'].append({'path': str(src), 'error': str(exc)})

def links():
    for src, dst in LINKS:
        target = remap(os.readlink(src))
        if not Path(target).is_absolute(): target = str(dst.parent/target)
        target = os.path.normpath(target)
        if str(target).casefold().startswith('f:'):
            REPORT['external_links'].append({'path': str(dst), 'target': target})
            continue
        if dst.is_junction() or dst.is_symlink():
            if str(Path(os.readlink(dst).removeprefix('\\\\?\\'))).casefold() == str(Path(target)).casefold(): continue
            old = BASE/'需要清理/迁移暂存链接'/str(time.time_ns())/dst.name
            old.parent.mkdir(parents=True, exist_ok=True)
            dst.rename(old)
        elif dst.is_dir() and not any(dst.iterdir()):
            empty = BASE/'需要清理/迁移暂存链接'/str(time.time_ns())/dst.name
            empty.parent.mkdir(parents=True, exist_ok=True)
            dst.rename(empty)
        elif dst.exists():
            # pnpm may have installed a physical equivalent. Keep it.
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir(): _winapi.CreateJunction(str(target), str(dst))
            else: os.symlink(target, dst)
            REPORT['links'] += 1
        except OSError as exc:
            REPORT['errors'].append({'path': str(dst), 'error': str(exc)})

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['code', 'data'])
    args = parser.parse_args()
    if any(src.resolve() == dst.resolve() for src, dst in ROOTS.items()):
        parser.error('迁移已经完成，旧入口现为兼容链接；不要再次从兼容入口复制到正式目录')
    for src, dst in ROOTS.items():
        if args.phase == 'code':
            excluded = {'artifacts','backups','.tmp','__pycache__','data','homes','napcat','qq-work','electron-user-data','home'}
            copy_tree(src, dst, excluded)
        elif src.name == 'dsh-personal':
            for name in ['data','homes','qq-work','napcat','channel-state']:
                if (src/name).exists(): copy_tree(src/name, dst/name)
        else:
            for name in ['home','electron-user-data']:
                rel = Path('apps/desktop/.desktop-build/development')/name
                copy_tree(src/rel, dst/rel, {'Cache','Code Cache','GPUCache','DawnGraphiteCache','DawnWebGPUCache'})
    links()
    log = BASE/'backups/migration'/f'{args.phase}-{time.strftime("%Y%m%d-%H%M%S")}.json'
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(REPORT,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({**REPORT,'errors':REPORT['errors'][:12],'log':str(log)},ensure_ascii=False))
    return bool(REPORT['errors'])

if __name__ == '__main__': raise SystemExit(main())
