"""Repair staged workspace links without copying files or following links."""
from pathlib import Path
import os
import time
import _winapi
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from finish_native_migration import ROOTS,remap

kept = Path('E:/Program Files/DSH/需要清理/损坏依赖链接')/time.strftime('%Y%m%d-%H%M%S')
fixed = 0
errors = []
for src_root,dst_root in ROOTS.items():
    for current,dirs,_ in os.walk(src_root,followlinks=False):
        base = Path(current)
        if base == src_root:
            dirs[:]=[d for d in dirs if d not in ('artifacts','backups','data','homes','.tmp','napcat') and not d.startswith(('dist-','build-'))]
        for name in list(dirs):
            src=base/name
            if not (src.is_junction() or src.is_symlink()):continue
            dirs.remove(name)
            dst=dst_root/src.relative_to(src_root)
            raw=os.readlink(src)
            target=remap(raw) if os.path.isabs(raw) else remap(os.path.normpath(str(src.parent/raw)))
            target=os.path.normpath(target)
            if '\x00' in target:errors.append(str(src));continue
            if dst.is_junction() or dst.is_symlink():
                existing=os.readlink(dst)
                if '\x00' not in existing and os.path.normcase(os.path.normpath(remap(existing)))==os.path.normcase(target) and dst.exists():continue
                saved=kept/dst.relative_to(Path('E:/Program Files/DSH'))
                saved.parent.mkdir(parents=True,exist_ok=True);dst.rename(saved)
            elif dst.exists():continue
            try:
                dst.parent.mkdir(parents=True,exist_ok=True)
                # Normalize '..' before CreateJunction. CPython's Windows helper
                # can otherwise leave NUL padding in the reparse target.
                _winapi.CreateJunction(target,str(dst));fixed+=1
            except OSError as exc:errors.append((str(dst),str(exc)))
print({'fixed':fixed,'errors':errors[:8],'error_count':len(errors),'old_links_kept':str(kept)})
