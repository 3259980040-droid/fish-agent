"""Create a verified local patch release using an existing full ZIP as its base."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    folder, base, output, evidence = (p.resolve() for p in
                                      (args.folder, args.base, args.output, args.evidence))
    if output.exists():
        raise FileExistsError('Existing delivery is preserved: ' + str(output))
    source = json.loads((ROOT / 'packaging/build-source.json').read_text(encoding='utf-8'))
    assert source == json.loads((folder / '_internal/build-source.json').read_text(encoding='utf-8'))
    for name, expected in source['source_files'].items():
        assert digest(ROOT / name) == expected, name
    with zipfile.ZipFile(base) as archive:
        before = json.loads(archive.read('fish-release.json'))
    assert before['product'] == 'Fish' and before['format'] == 1
    paths = subprocess.run(['rg', '--files', '--hidden', '--no-ignore', str(folder)],
                           capture_output=True, text=True, encoding='utf-8', check=True).stdout.splitlines()

    def entry(value):
        path = Path(value)
        if path.is_symlink():
            raise ValueError('Release cannot contain symbolic links: ' + str(path))
        if path.name == 'fish-release.json':
            return None
        return path.relative_to(folder).as_posix(), digest(path)

    manifest = {'product': 'Fish', 'format': 1, 'version': source['version'],
                'created': time.time(), 'files': {}}
    with ThreadPoolExecutor(max_workers=12) as pool:
        for item in pool.map(entry, paths):
            if item:
                manifest['files'][item[0]] = item[1]
    removed = set(before['files']) - set(manifest['files'])
    if removed:
        raise RuntimeError('Patch base contains removed files; use a full rebuild: ' + str(sorted(removed)))
    changed = [name for name, value in manifest['files'].items() if before['files'].get(name) != value]
    evidence.mkdir(parents=True, exist_ok=True)
    stage = evidence / 'package-stage'
    stage.mkdir(exist_ok=False)
    for name in changed:
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(folder / name, target)
    (stage / 'fish-release.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base, output)
    seven_zip = shutil.which('7z') or r'F:\7-Zip\7z.exe'
    subprocess.run([seven_zip, 'u', str(output), *changed, 'fish-release.json',
                    '-tzip', '-mx=3', '-bb0', '-bsp0', '-y'], cwd=stage, check=True,
                   stdout=subprocess.DEVNULL)
    print('Archive built; verifying every file and ZIP CRC', flush=True)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), 'Duplicate ZIP entries'
        assert set(names) == set(manifest['files']) | {'fish-release.json'}
        assert json.loads(archive.read('fish-release.json')) == manifest
        for name, expected in manifest['files'].items():
            with archive.open(name) as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected, name
    result = {'ok': True, 'version': source['version'], 'archive': str(output),
              'archive_bytes': output.stat().st_size, 'archive_sha256': digest(output),
              'executable': str(folder / 'Fish.exe'), 'executable_sha256': digest(folder / 'Fish.exe'),
              'source_files_verified': len(source['source_files']),
              'archive_files_verified': len(manifest['files']), 'zip_crc_verified': True,
              'changed_files': changed, 'base_release_preserved': str(base),
              'real_owner_acceptance': False, 'real_group_enabled': False}
    (evidence / 'delivery-manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
