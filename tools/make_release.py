"""Create a hashed, offline Fish update from a finished onedir build."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import zipfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))
from updater import VERSION, inspect_release


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--directory', type=Path, default=ROOT / 'dist-round2/Fish'); parser.add_argument('--output', type=Path, default=ROOT / 'dist-round2/Fish-0.3.0-round2.zip')
    args = parser.parse_args(); folder = args.directory.resolve()
    manifest = {'product': 'Fish', 'format': 1, 'version': VERSION, 'created': time.time(), 'files': {}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    stage = args.output.with_suffix('.tmp')
    with zipfile.ZipFile(stage, 'w', zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        for file in sorted(folder.rglob('*')):
            if not file.is_file() or file.is_symlink(): continue
            relative = file.relative_to(folder).as_posix()
            if relative == 'fish-release.json': continue
            digest = hashlib.sha256()
            with file.open('rb') as source, archive.open(relative, 'w', force_zip64=True) as target:
                while block := source.read(1024*1024): digest.update(block); target.write(block)
            manifest['files'][relative] = digest.hexdigest()
        archive.writestr('fish-release.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    stage.replace(args.output); inspect_release(args.output)
    print(json.dumps({'ok': True, 'path': str(args.output), 'files': len(manifest['files']), 'size_mb': round(args.output.stat().st_size / 1024**2, 1)}))


if __name__ == '__main__': main()
