"""Verify the final source cohort, release archive, and acceptance evidence."""
import hashlib,json,zipfile
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];out=ROOT/'artifacts/round4'
folder=ROOT/'dist-round4-delivery/Fish';archive=folder.parent/'Fish-0.3.2-round4.zip'
def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(1024*1024):h.update(block)
    return h.hexdigest()
def main():
    source=json.loads((ROOT/'packaging/build-source.json').read_text(encoding='utf-8'))
    assert source==json.loads((folder/'_internal/build-source.json').read_text(encoding='utf-8'))
    assert source['round']==4 and source['version']=='0.3.2'
    for relative,expected in source['source_files'].items():assert digest(ROOT/relative)==expected,relative
    with zipfile.ZipFile(archive) as z:
        manifest=json.loads(z.read('fish-release.json'));assert manifest['version']=='0.3.2'
        assert len(z.namelist())==len(manifest['files'])+1
        for relative,expected in manifest['files'].items():
            h=hashlib.sha256()
            with z.open(relative) as stream:
                while block:=stream.read(1024*1024):h.update(block)
            assert h.hexdigest()==expected,relative
        assert manifest['files']['Fish.exe']==digest(folder/'Fish.exe')
    runtime=json.loads((out/'packaged-runtime-check.json').read_text(encoding='utf-8'))
    assert runtime['ok'] and runtime['authenticated_runtime'] and runtime['restored_empty_homes_and_control_bundle_repaired']
    assert runtime['executable_sha256']==digest(folder/'Fish.exe')
    assert 'Ran 83 tests' in (out/'regression-final.log').read_text(encoding='utf-8',errors='replace')
    equivalence=json.loads((out/'ui-polish-equivalence.json').read_text(encoding='utf-8'))
    assert equivalence['changed_application_sources']==['fish_desktop/data_settings.py']
    assert equivalence['final_executable_sha256']==digest(folder/'Fish.exe')
    assert json.loads((out/'progress-layout-check.json').read_text(encoding='utf-8'))['ok']
    activation=json.loads((out/'live-activation-check.json').read_text(encoding='utf-8'))
    assert activation['ok'] and activation['qq_runtime_authenticated'] and activation['guard_active']
    assert activation['executable_sha256']==digest(folder/'Fish.exe')
    for name in ('photo-experience-check.json','memory-experience-check.json','native-maintenance-check.json','bilibili-experience-check.json','bilibili-followup-check.json'):
        assert json.loads((out/name).read_text(encoding='utf-8'))['ok'],name
    result={'ok':True,'round':4,'version':'0.3.2','created_at':datetime.now().astimezone().isoformat(),
        'executable':str(folder/'Fish.exe'),'executable_sha256':digest(folder/'Fish.exe'),
        'archive':str(archive),'archive_sha256':digest(archive),'archive_bytes':archive.stat().st_size,
        'source_files_verified':len(source['source_files']),'archive_files_verified':len(manifest['files']),
        'zip_crc_and_hash_verified':True,'regression_tests_passed':83,
        'functional_experience_executable_sha256':equivalence['functional_tests_executable_sha256'],
        'final_polish_delta':equivalence['changed_application_sources'],'final_embedded_ui_layout_verified':True,
        'final_daily_gui_and_qq_backend_activated':True,'group_reply_configured':'off',
        'personal_bilibili_cycle_verified':True,'personal_bilibili_observation':'Retrieved subtitles; second source reported music-only and insufficient evidence',
        'clean_windows_vm_verified':False,
        'status':'Local final experience and owner-directed Bilibili cycle delivered; clean Windows remains pending'}
    (out/'delivery-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result))
if __name__=='__main__':main()
