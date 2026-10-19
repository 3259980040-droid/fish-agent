"""Hash the complete new build, then reuse unchanged compressed ZIP entries."""
import concurrent.futures,hashlib,json,shutil,subprocess,time,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
folder=ROOT/'dist-round4-complete/Fish'
old=ROOT/'dist-round4-audit/Fish-0.3.3-round4.zip'
archive=folder.parent/'Fish-0.3.4-round4.zip'
out=ROOT/'artifacts/round4/completion-audit'
stage=out/'package-update-complete';stage.mkdir(parents=True,exist_ok=True)
if archive.exists():raise RuntimeError('Archive exists; preserve it before rebuilding')

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while block:=f.read(1024*1024):h.update(block)
    return h.hexdigest()

with zipfile.ZipFile(old) as z:before=json.loads(z.read('fish-release.json'))
manifest={'product':'Fish','format':1,'version':'0.3.4','created':time.time(),'files':{}}
paths=subprocess.run(['rg','--files','--hidden','--no-ignore',str(folder)],capture_output=True,text=True,encoding='utf-8',check=True).stdout.splitlines()
def entry(name):
    file=Path(name)
    if file.is_symlink() or file.name=='fish-release.json':return None
    return file.relative_to(folder).as_posix(),digest(file)
with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
    for index,result in enumerate(pool.map(entry,paths),1):
        if result:manifest['files'][result[0]]=result[1]
        if index%10000==0:print('Hashed',index,'/',len(paths),flush=True)
removed=set(before['files'])-set(manifest['files'])
if removed:raise RuntimeError('Incremental archive cannot retain removed entries: '+str(sorted(removed)))
changed=[name for name,value in manifest['files'].items() if before['files'].get(name)!=value]
for name in changed:
    target=stage/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(folder/name,target)
(stage/'fish-release.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
if archive.exists():raise RuntimeError('Archive exists; preserve it before rebuilding')
shutil.copy2(old,archive)
subprocess.run([r'F:\7-Zip\7z.exe','u',str(archive),*changed,'fish-release.json','-tzip','-mx=3','-bb0','-bsp0','-y'],cwd=stage,check=True)
result={'ok':True,'changed_files':changed,'all_build_files_hashed':len(manifest['files']),'archive':str(archive),'old_archive_preserved':str(old)}
(out/'incremental-release-complete.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result))
