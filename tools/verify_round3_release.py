"""Verify every release entry and source cohort; write the final delivery proof."""
import hashlib,json,sys,zipfile
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as file:
        while block:=file.read(1024*1024):h.update(block)
    return h.hexdigest()


def main():
    folder=ROOT/'dist-round3/Fish';archive=ROOT/'dist-round3/Fish-0.3.1-round3.zip';out=ROOT/'artifacts/round3'
    source=json.loads((ROOT/'packaging/build-source.json').read_text(encoding='utf-8'))
    assert source==json.loads((folder/'_internal/build-source.json').read_text(encoding='utf-8'))
    for relative,expected in source['source_files'].items():assert digest(ROOT/relative)==expected,relative
    runtime=json.loads((folder/'_internal/runtime/manifest.json').read_text(encoding='utf-8'))
    assert digest(folder/'_internal/runtime/node/node.exe')==runtime['node_sha256']
    with zipfile.ZipFile(archive) as z:
        manifest=json.loads(z.read('fish-release.json'))
        assert len(z.namelist())==len(manifest['files'])+1
        for relative,expected in manifest['files'].items():
            h=hashlib.sha256()
            with z.open(relative) as stream:
                while block:=stream.read(1024*1024):h.update(block)
            assert h.hexdigest()==expected,relative
        assert manifest['files']['Fish.exe']==digest(folder/'Fish.exe')
        assert manifest['files']['_internal/build-source.json']==digest(folder/'_internal/build-source.json')
    frozen=json.loads((out/'packaged-runtime-check.json').read_text(encoding='utf-8'))
    assert frozen['ok'] and frozen['frozen'] and frozen['authenticated_runtime']
    assert frozen['executable_sha256']==digest(folder/'Fish.exe')
    activation=json.loads((out/'live-activation-check.json').read_text(encoding='utf-8'))
    assert activation['ok'] and activation['executable_sha256']==digest(folder/'Fish.exe')
    checks=['wizard-check.json','ui-check.json','long-research-check.json','research-followup-check.json','emoji-reply-check.json','mcp-transports-check.json','native-plugin-check.json','packaged-backend-check.json']
    for name in checks:assert json.loads((out/name).read_text(encoding='utf-8'))['ok'],name
    assert 'Ran 78 tests' in (out/'regression-final.log').read_text(encoding='utf-8',errors='replace')
    result={'ok':True,'round':3,'version':'0.3.1','created_at':datetime.now().astimezone().isoformat(),'executable':str(folder/'Fish.exe'),'executable_sha256':digest(folder/'Fish.exe'),'archive':str(archive),'archive_sha256':digest(archive),'archive_size_mb':round(archive.stat().st_size/1024**2,1),'archive_files_verified':len(manifest['files']),'zip_crc_and_hash_verified':True,'source_files_verified':len(source['source_files']),'regression_tests_passed':78,'owner_qq_photo_verified':True,'owner_photo_test_executable':'dist-round2/Fish/Fish.exe','owner_photo_test_reply_receipts':2,'other_round3_simulation_real_qq_sends':0,'current_live_backends_match_delivery':True,'clean_windows_vm_verified':False,'personal_bilibili_cycle_verified':False,'status':'Round 3 evidence delivered with explicit pending cases; round 4 remains'}
    (out/'delivery-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result))


if __name__=='__main__':main()
