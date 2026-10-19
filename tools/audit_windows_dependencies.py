"""Record static PE dependencies without relying on the development PATH.

This is a packaging audit, not proof of loader behavior on another Windows OS.
Requires pefile only on the audit machine; Fish does not import this script.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pefile

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def read_pe(path):
    try:
        with path.open('rb') as stream:
            magic = stream.read(4)
        if magic[:2] != b'MZ':
            return {'foreign_binary_magic': magic.hex()}
        # Qt and Node legitimately export more than pefile's default 8192 names.
        with pefile.PE(str(path), fast_load=True, max_symbol_exports=262144) as pe:
            pe.parse_data_directories(directories=[
                pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'],
                pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT'],
                pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT'],
            ])
            imports = []
            for attribute, delayed in [('DIRECTORY_ENTRY_IMPORT', False),
                                       ('DIRECTORY_ENTRY_DELAY_IMPORT', True)]:
                for entry in getattr(pe, attribute, []):
                    imports.append({
                        'dll': entry.dll.decode('ascii', errors='replace').lower(),
                        'delay_load': delayed,
                        'symbols': [item.name.decode('ascii', errors='replace')
                                    if item.name else f'#{item.ordinal}'
                                    for item in entry.imports],
                    })
            exports = set()
            for symbol in getattr(getattr(pe, 'DIRECTORY_ENTRY_EXPORT', None), 'symbols', []):
                exports.add(f'#{symbol.ordinal}')
                if symbol.name:
                    exports.add(symbol.name.decode('ascii', errors='replace'))
            declared = getattr(getattr(pe, 'DIRECTORY_ENTRY_EXPORT', None), 'struct', None)
            if any('Assuming corrupt' in item for item in pe.get_warnings()):
                raise ValueError('Incomplete PE parsing: ' + '; '.join(pe.get_warnings()))
            return {'machine': hex(pe.FILE_HEADER.Machine), 'imports': imports,
                    'exports': exports, 'declared_named_exports': getattr(declared, 'NumberOfNames', 0)}
    except Exception as exc:
        return {'error': f'{type(exc).__name__}: {exc}'}


def main(folder, output):
    folder = folder.resolve()
    listed = subprocess.run(['rg', '--files', '--hidden', '--no-ignore', str(folder)],
                            check=True, capture_output=True, text=True, encoding='utf-8').stdout.splitlines()
    native = sorted(Path(name) for name in listed
                    if Path(name).suffix.lower() in {'.exe', '.dll', '.pyd', '.node'})
    with ThreadPoolExecutor(max_workers=8) as executor:
        metadata = dict(zip(native, executor.map(read_pe, native)))
    by_name = defaultdict(list)
    for path in native:
        by_name[path.name.lower()].append(path)
    system32 = Path(os.environ['SystemRoot']) / 'System32'
    system_cache = {}
    with pefile.PE(str(folder / 'Fish.exe'), fast_load=True) as executable:
        executable.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
        manifests = []
        for kind in executable.DIRECTORY_ENTRY_RESOURCE.entries:
            if kind.id == 24:
                for name in kind.directory.entries:
                    for lang in name.directory.entries:
                        data = lang.data.struct
                        manifests.append(executable.get_data(data.OffsetToData, data.Size).decode('utf-8', errors='replace'))
    controls_v6_manifest = any('Microsoft.Windows.Common-Controls' in manifest and 'version="6.0.0.0"' in manifest
                               for manifest in manifests)
    controls_v6_candidates = []
    if controls_v6_manifest:
        for path in (Path(os.environ['SystemRoot']) / 'WinSxS').glob('amd64_microsoft.windows.common-controls_*/comctl32.dll'):
            controls_v6_candidates.append((path, read_pe(path)))
    counts = Counter()
    unresolved = []
    mismatches = []
    binaries = []
    system_dependencies = defaultdict(set)
    for path, info in metadata.items():
        relative = path.relative_to(folder).as_posix()
        if 'error' in info:
            binaries.append({'path': relative, **info})
            counts['parse_errors'] += 1
            continue
        if 'foreign_binary_magic' in info:
            binaries.append({'path': relative, **info})
            counts['foreign_prebuilt_assets'] += 1
            continue
        records = []
        for entry in info['imports']:
            name = entry['dll']
            result = {**entry}
            if name.startswith(('api-ms-', 'ext-ms-')):
                result['resolution'] = 'windows_api_set_contract'
            elif by_name.get(name):
                candidates = by_name[name]
                compatible = [candidate for candidate in candidates
                              if metadata[candidate].get('machine') == info['machine']
                              and set(entry['symbols']) <= metadata[candidate].get('exports', set())]
                result['resolution'] = 'bundled_candidate' if compatible else 'bundled_symbol_or_architecture_mismatch'
                result['candidates'] = [item.relative_to(folder).as_posix() for item in candidates]
                # This proves file/symbol presence only, not DLL search order.
                result['compatible_candidates'] = [item.relative_to(folder).as_posix() for item in compatible]
                if not compatible:
                    mismatches.append({'binary': relative, **result})
            else:
                system_path = system32 / name
                if system_path.is_file():
                    if name not in system_cache:
                        system_cache[name] = read_pe(system_path)
                    system = system_cache[name]
                    missing = sorted(set(entry['symbols']) - system.get('exports', set()))
                    result['resolution'] = 'current_host_system32' if not missing else 'current_host_symbol_mismatch'
                    result['system_file'] = str(system_path)
                    if missing:
                        sxs = [str(candidate) for candidate, data in controls_v6_candidates
                               if name == 'comctl32.dll' and path == folder / 'Fish.exe'
                               and set(entry['symbols']) <= data.get('exports', set())]
                        if sxs:
                            result['resolution'] = 'current_host_sxs_manifest_candidate'
                            result['sxs_candidates'] = sxs
                        else:
                            result['missing_symbols'] = missing
                            mismatches.append({'binary': relative, **result})
                    system_dependencies[name].add(relative)
                else:
                    result['resolution'] = 'not_bundled_or_in_current_host_system32'
                    unresolved.append({'binary': relative, **result})
            counts[result['resolution']] += 1
            records.append(result)
        binaries.append({'path': relative, 'machine': info['machine'],
                         'declared_named_exports': info['declared_named_exports'], 'imports': records})
    result = {
        'created_at': datetime.now().astimezone().isoformat(),
        'folder': str(folder), 'executable_sha256': sha256(folder / 'Fish.exe'),
        'host_windows': list(sys.getwindowsversion()),
        'native_files_inspected': len(native),
        'machine_counts': dict(Counter(info.get('machine', 'foreign_prebuilt' if 'foreign_binary_magic' in info else 'parse_error')
                                       for info in metadata.values())),
        'dependency_edge_counts': dict(counts),
        'unresolved': unresolved, 'symbol_or_architecture_mismatches': mismatches,
        'x64_unresolved_edges': [item for item in unresolved
                                 if metadata[folder / item['binary']].get('machine') == '0x8664'],
        'x64_mismatch_edges': [item for item in mismatches
                               if metadata[folder / item['binary']].get('machine') == '0x8664'],
        'current_host_system_dependencies': {key: sorted(values) for key, values in sorted(system_dependencies.items())},
        'independent_windows_verified': False,
        'limitations': [
            'Static imports and delay imports only; dynamic LoadLibrary paths are not enumerated.',
            'Bundled candidates prove matching architecture/exports, not actual Windows loader search order.',
            'System32 existence and exports were inspected on this host only; target Windows must still be tested.',
            'API-set contracts are classified by name, not independently resolved on another OS.',
            'Export forwarders are not recursively resolved; native entry points and optional plugins may need additional runtime checks.',
        ],
        'binaries': binaries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key not in {'binaries', 'current_host_system_dependencies', 'symbol_or_architecture_mismatches'}},
                     ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--folder', type=Path, default=ROOT / 'dist-round4-complete/Fish')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/round4/completion-audit/windows-native-dependencies.json')
    args = parser.parse_args()
    main(args.folder, args.output)
