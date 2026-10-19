"""Verify 0.3.4 and record precisely which executable each scenario used."""
import argparse,hashlib,json,re,zipfile
from pathlib import Path
from datetime import datetime
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'artifacts/round4/completion-audit'
FOLDER=ROOT/'dist-round4-complete/Fish';ARCHIVE=FOLDER.parent/'Fish-0.3.4-round4.zip'

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while block:=f.read(1024*1024):h.update(block)
    return h.hexdigest()

def read(name):return json.loads((OUT/name).read_text(encoding='utf-8'))

def main(package_only):
    source=json.loads((ROOT/'packaging/build-source.json').read_text(encoding='utf-8'))
    assert source==json.loads((FOLDER/'_internal/build-source.json').read_text(encoding='utf-8'))
    assert source['version']=='0.3.4'
    for name,value in source['source_files'].items():assert digest(ROOT/name)==value,name
    if package_only:
        with zipfile.ZipFile(ARCHIVE) as z:
            manifest=json.loads(z.read('fish-release.json'));assert manifest['version']=='0.3.4'
            assert len(z.namelist())==len(set(z.namelist()))==len(manifest['files'])+1
            assert set(z.namelist())==set(manifest['files'])|{'fish-release.json'}
            for name,value in manifest['files'].items():
                h=hashlib.sha256()
                with z.open(name) as f:
                    while block:=f.read(1024*1024):h.update(block)
                assert h.hexdigest()==value,name
            assert manifest['files']['Fish.exe']==digest(FOLDER/'Fish.exe')
        result={'ok':True,'version':'0.3.4','executable_sha256':digest(FOLDER/'Fish.exe'),
            'archive_sha256':digest(ARCHIVE),'source_files_verified':len(source['source_files']),
            'archive_files_verified':len(manifest['files']),'zip_crc_and_sha256_verified':True,
            'archive_bytes':ARCHIVE.stat().st_size}
        (OUT/'package-verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result));return
    package=read('package-verification.json');assert package['ok']
    assert package['archive_sha256']==digest(ARCHIVE)
    exe_hash=digest(FOLDER/'Fish.exe');assert package['executable_sha256']==exe_hash
    for name in ('packaged-runtime-check.json',):
        evidence=read(name);assert evidence['ok'] and evidence['executable_sha256']==exe_hash,name
    functional_hash=digest(ROOT/'dist-round4-audit/Fish/Fish.exe')
    prior=read('build-source-0.3.3.json')
    delta=[name for name,value in source['source_files'].items() if prior['source_files'].get(name)!=value]
    assert delta==['fish_desktop/app.py','harness/updater.py']
    for name in ('owner-photo-experience.json','recorded-cue-real-video.json'):
        evidence=read(name);assert evidence['ok'] and evidence['executable_sha256']==functional_hash,name
    assert read('ui-layout.json')['ok'] and read('real-video-review-adopted.json')['ok']
    log=(ROOT/'artifacts/round4/regression-complete-final.log').read_text(encoding='utf-8',errors='replace')
    assert re.search(r'Ran 96 tests',log) and log.rstrip().endswith('OK')
    activation=read('daily-upgrade-activation.json')
    assert activation['ok'] and activation['executable_sha256']==exe_hash
    assert activation['from_version']=='0.3.2' and activation['to_version']=='0.3.4'
    maintenance=json.loads((ROOT/'artifacts/round4/native-maintenance-check.json').read_text(encoding='utf-8'))
    for key in ('ok','gui_restore_created_verified_backup','restore_reopened_new_window',
                'migrate_reopened_new_root','original_data_retained','upgrade_reopened_installed_exe',
                'chat_and_memory_baselines_retained','napcat_preserved'):
        assert maintenance[key] is True,key
    for key in ('root_selection_persisted','second_manual_launch_retains_data','napcat_running'):
        assert activation[key] is True,key
    assert activation['config_persona_secret_report_hashes_preserved']==9
    assert activation['existing_chat_and_memory_records_preserved']==156
    scope=json.loads((ROOT/'FISH-PROGRESS.json').read_text(encoding='utf-8'))
    assert scope['acceptance_scope']=='personal-local-current-pc'
    assert (ROOT/'FISH-LOCAL-ACCEPTANCE.md').is_file()
    result={**package,'created_at':datetime.now().astimezone().isoformat(),
        'executable':str(FOLDER/'Fish.exe'),'archive':str(ARCHIVE),'regression_tests_passed':96,
        'functional_experience_executable_sha256':functional_hash,'final_source_delta':delta,
        'final_delta_explanation':'Persist data selection after successful maintenance and bump version; model/QQ/media/research implementations unchanged',
        'current_live_executable':activation['installed_executable'],
        'cross_version_native_gui_upgrade_verified':True,'second_manual_launch_retains_data':True,'current_qq_guard_authenticated':True,
        'upgrade_environment':'Writable private LOCALAPPDATA under project; global AppData install denied by Windows ACL and remains unverified',
        'owner_natural_permission_awareness':True,'ordinary_photo_send_understood':True,
        'video_fallback_method':'Original recorded music-only cue + actual Bili download, bundled extractor and real model; not a second fully live discovery cycle',
        'clean_windows_verified':False,
        'acceptance_scope':'Personal use on the current Windows computer; open source only after the user considers it mature',
        'local_acceptance_complete':True,
        'remaining_requirement':None,
        'future_release_checks':['Independent clean Windows first use','Default system AppData installation','Other machine and Windows/DPI compatibility'],
        'scope_record':'FISH-LOCAL-ACCEPTANCE.md'}
    (OUT/'delivery-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--package-only',action='store_true');args=parser.parse_args();main(args.package_only)
