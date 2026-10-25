"""Verified local memory snapshots and narrowly scoped derived-data pruning."""
from locations import project_path
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from storage import database,write_json


class DataRetention:
    def __init__(self, root):self.root=Path(root)

    @staticmethod
    def verify(target):
        target=Path(target);manifest=json.loads((target/'manifest.json').read_text(encoding='utf-8'))
        for item in manifest['files'].values():
            filename=item['file']
            if Path(filename).name!=filename:raise ValueError('无效备份文件名')
            path=target/filename
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('备份校验失败')
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('备份数据库不完整')
        return manifest

    def run(self, now=None):
        now=time.time() if now is None else now
        state=project_path(self.root, 'data/qq/retention-state.json')
        if state.exists():
            previous=json.loads(state.read_text(encoding='utf-8'))
            prior_manifest=Path(previous.get('backup',''))/'manifest.json'
            # Completed backups are verified when created, not every five minutes.
            prior_files=json.loads(prior_manifest.read_text(encoding='utf-8')).get('files',{}) if prior_manifest.is_file() else {}
            required=[r for r in ('data/qq/memory.db','data/social/group-language.db','data/social/people.db') if (project_path(self.root, r)).exists()]
            if now-previous.get('last_success',0)<86400 and all(r in prior_files for r in required):return {'skipped':True}
        target=self.root/'backups/automatic-qq'/time.strftime('%Y%m%d-%H%M%S',time.localtime(now))
        target.mkdir(parents=True,exist_ok=True);files={}
        for rel in ('data/qq/memory.db','data/social/group-language.db','data/social/people.db'):
            source=project_path(self.root, rel)
            if not source.exists():continue
            dest=target/(source.parent.name+'-'+source.name)
            temp=dest.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(temp)) as db:
                src.backup(db)
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('自动备份完整性校验失败')
            with temp.open('r+b') as stream:os.fsync(stream.fileno())
            temp.replace(dest);files[rel]={'file':dest.name,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()}
        write_json(target/'manifest.json',{'created':now,'files':files})
        self.verify(target)
        # Keep 14 recent copies handy; older snapshots remain recoverable in
        # the user-managed cleanup tree. Never delete original data.
        import shutil
        copies=sorted(p for p in target.parent.iterdir() if p.is_dir() and (p/'manifest.json').is_file())
        archive=self.root/'需要清理/过期自动备份'
        for old in copies[:-14]:
            archive.mkdir(parents=True,exist_ok=True)
            dest=archive/old.name
            if not dest.exists(): shutil.move(str(old),str(dest))
        # Only disposable derivatives; never touch chat history, memories,
        # learning jobs, attachments, persona windows, or native session logs.
        cache=project_path(self.root, 'data/qq/web-cache.db');pruned=0
        if cache.exists():
            with database(cache) as db:pruned+=db.execute('DELETE FROM searches WHERE expires<?',(now,)).rowcount
        # Context usage is observation, not content: old rows keep their token
        # numbers and lose the 31KB budget snapshot that made the file 40MB.
        usage=project_path(self.root, 'data/social/context-usage.db')
        if usage.exists():
            cutoff=now-30*86400
            with database(usage) as db:
                pruned+=db.execute("UPDATE context_requests SET budget='{}' "
                                   "WHERE created<? AND LENGTH(budget)>2",(cutoff,)).rowcount
        jobs=project_path(self.root, 'data/social/group-replies.db')
        if jobs.exists():
            with database(jobs) as db:
                pruned+=db.execute("UPDATE jobs SET snapshot='{}' WHERE updated<? AND state IN ('sent','failed','cancelled','interrupted') AND snapshot<>'{}'",(now-7*86400,)).rowcount
        result={'last_success':now,'backup':str(target),'files':len(files),'derived_rows_pruned':pruned,'long_term_deleted':0}
        write_json(state,result);return result
