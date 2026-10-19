"""Checksummed 0.4.0 local release, preserving previous releases."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import zipfile
ROOT=Path(__file__).resolve().parents[1]
FOLDER=ROOT/'dist-social-delivery-040/Fish'
OUT=ROOT/'artifacts/social-base'

def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as f:
        while block:=f.read(1024*1024):value.update(block)
    return value.hexdigest()

def main():
    archive=FOLDER.parent/'Fish-0.4.0-social-base.zip'
    if archive.exists():raise FileExistsError('Preserve the existing delivery before creating another')
    source=json.loads((ROOT/'packaging/build-source.json').read_text(encoding='utf-8'))
    assert source==json.loads((FOLDER/'_internal/build-source.json').read_text(encoding='utf-8'))
    for name,expected in source['source_files'].items():assert digest(ROOT/name)==expected,name
    old=ROOT/'dist-round4-complete/Fish-0.3.4-round4.zip'
    with zipfile.ZipFile(old) as z:before=json.loads(z.read('fish-release.json'))
    manifest={'product':'Fish','format':1,'version':'0.4.0','created':time.time(),'files':{}}
    paths=subprocess.run(['rg','--files','--hidden','--no-ignore',str(FOLDER)],capture_output=True,text=True,encoding='utf-8',check=True).stdout.splitlines()
    def entry(value):
        p=Path(value)
        if p.is_symlink() or p.name=='fish-release.json':return None
        return p.relative_to(FOLDER).as_posix(),digest(p)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for item in pool.map(entry,paths):
            if item:manifest['files'][item[0]]=item[1]
    changed=[name for name,value in manifest['files'].items() if before['files'].get(name)!=value]
    removed=set(before['files'])-set(manifest['files'])
    if removed:raise RuntimeError('Incremental archive has unexpected removed entries: '+str(sorted(removed)))
    stage=OUT/'package-stage';stage.mkdir(exist_ok=True)
    for name in changed:
        target=stage/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(FOLDER/name,target)
    (stage/'fish-release.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    shutil.copy2(old,archive)
    subprocess.run([r'F:\7-Zip\7z.exe','u',str(archive),*changed,'fish-release.json','-tzip','-mx=3','-bb0','-bsp0','-y'],cwd=stage,check=True,stdout=subprocess.DEVNULL)
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        packed=json.loads(z.read('fish-release.json'));assert packed==manifest
        names=set(z.namelist())-{'fish-release.json'}
        assert names==set(manifest['files'])
        for name,expected in manifest['files'].items():
            assert hashlib.sha256(z.read(name)).hexdigest()==expected,name
    result={'ok':True,'version':'0.4.0','executable':str(FOLDER/'Fish.exe'),'executable_sha256':digest(FOLDER/'Fish.exe'),
        'archive':str(archive),'archive_sha256':digest(archive),'archive_bytes':archive.stat().st_size,
        'source_files_verified':len(source['source_files']),'archive_files_verified':len(manifest['files']),'changed_files':changed,
        'old_release_preserved':str(old),'current_live_gui_replaced':False,'real_owner_acceptance':False,'real_group_enabled':False}
    (OUT/'delivery-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
