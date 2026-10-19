"""Copy redistributable runtime files; never copy user homes or credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--node', required=True, type=Path)
    parser.add_argument('--pnpm', required=True, type=Path)
    args = parser.parse_args()
    target = ROOT / 'vendor/runtime'
    node_dir = target / 'node'; node_dir.mkdir(parents=True, exist_ok=True)
    if not args.node.is_file() or not (args.pnpm / 'bin/pnpm.cjs').is_file(): raise ValueError('runtime source missing')
    shutil.copy2(args.node, node_dir / 'node.exe')
    for parent in args.node.parents:
        if (parent / 'LICENSE').is_file():
            shutil.copy2(parent / 'LICENSE', node_dir / 'LICENSE'); break
    shutil.copytree(args.pnpm, target / 'pnpm', dirs_exist_ok=True)
    manifest = {'version': 1, 'dsh': json.loads((target / 'dsh/node_modules/@deepseek-ai/dsh/package.json').read_text())['version'],
        'pnpm': json.loads((args.pnpm / 'package.json').read_text())['version'],
        'node_sha256': hashlib.sha256((node_dir / 'node.exe').read_bytes()).hexdigest()}
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest))


if __name__ == '__main__': main()
