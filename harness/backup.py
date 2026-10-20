"""Checksummed backups with SQLite snapshots and optional portable credentials."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from storage import settings_lock, read_json, write_json

ROOTS = ('channel-state', 'data', 'homes', 'knowledge', 'qq-work', 'harness/agents', 'harness/global', 'harness/safety', 'harness/secrets')
FILES = ('harness/network.json', 'harness/agents.json', 'harness/control.json', 'harness/multimodal.json', 'harness/emoji.json',
         'harness/research-interests.json', 'harness/plugins/registry.json', 'harness/setup.json', 'channels/fish_qq/config.json',
         'harness/owner-computer.json', 'harness/integration.json', 'harness/memory-embeddings.json')


def _allowed(relative):
    return relative in FILES or any(relative == root or relative.startswith(root + '/') for root in ROOTS)


def _target(root, relative):
    if '\\' in relative or ':' in relative or '\x00' in relative:
        raise ValueError('备份含无效路径')
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or not _allowed(relative):
        raise ValueError('备份路径超出 Fish 数据范围')
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()) or result == root.resolve():
        raise ValueError('目标路径超出数据目录')
    return result


def _crypt(value, password, *, decrypt=False):
    import dependencies
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    if not 10 <= len(password) <= 200:
        raise ValueError('可迁移密钥的备份密码需为 10–200 字符')
    if decrypt:
        if not value.startswith(b'FISHKEY1') or len(value) < 52:
            raise ValueError('加密密钥文件损坏')
        salt, nonce, payload = value[8:24], value[24:36], value[36:]
    else:
        salt, nonce, payload = os.urandom(16), os.urandom(12), value
    key = Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password.encode('utf-8'))
    try:
        if decrypt: return AESGCM(key).decrypt(nonce, payload, b'Fish backup v1')
        return b'FISHKEY1' + salt + nonce + AESGCM(key).encrypt(nonce, payload, b'Fish backup v1')
    except Exception as exc:
        raise ValueError('备份密码不正确，或加密密钥文件已损坏') from exc


class BackupManager:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def create(self, destination, *, password='', portable_keys=False):
        destination = Path(destination).resolve()
        if destination.is_relative_to(self.root / 'data') or destination.is_relative_to(self.root / 'homes'):
            raise ValueError('请把备份保存在数据目录以外')
        from secrets_store import SecretStore
        secrets = {}
        if portable_keys:
            if not 10 <= len(password) <= 200: raise ValueError('包含可迁移密钥时，请设置至少 10 字符的备份密码')
            for file in (self.root / 'harness/secrets').glob('*.bin'):
                secrets[file.stem] = SecretStore(self.root).get(file.stem)
            for ref, env in [('agent-qq', 'FISH_QQ_DEEPSEEK_API_KEY'), ('agent-research', 'FISH_RESEARCH_DEEPSEEK_API_KEY'), ('channel-core', 'FISH_QQ_TOKEN'), ('channel-napcat', 'NAPCAT_TOKEN')]:
                value = SecretStore(self.root).get(ref, env)
                if value: secrets[ref] = value
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
        manifest = {'format': 'fish-backup', 'version': 1, 'created': time.time(), 'source_root': str(self.root), 'files': {},
            'portable_keys': portable_keys, 'credentials_note': '有密码的密钥包可跨 Windows 账号恢复' if portable_keys else 'DPAPI 密钥只适用于原 Windows 用户；跨机需重新填写或使用密码加密备份'}
        try:
            with tempfile.TemporaryDirectory(prefix='fish-backup-sql-') as workspace, settings_lock(self.root), zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
                selected = [self.root / name for name in FILES if (self.root / name).is_file()]
                for prefix in ROOTS:
                    base = self.root / prefix
                    if base.is_dir():
                        selected.extend(p for p in base.rglob('*') if p.is_file() and not p.is_symlink())
                for source in sorted(set(selected)):
                    relative = source.relative_to(self.root).as_posix()
                    if any(p in ('node_modules', '.cache', '_extractor_cache', 'plugin-work', '__pycache__') for p in source.relative_to(self.root).parts): continue
                    if source.suffix.lower() in ('.lock', '.log') or source.name.endswith(('-wal', '-shm')) or source.name == 'control.token' or source.name.startswith('.env'): continue
                    _target(self.root, relative)
                    if source.stat().st_size > 500 * 1024 * 1024: raise ValueError('单个备份文件超过 500MB：' + relative)
                    if source.suffix.lower() in ('.db', '.sqlite', '.sqlite3'):
                        snapshot = Path(workspace) / (uuid.uuid4().hex + '.db')
                        original = sqlite3.connect(source); target = sqlite3.connect(snapshot)
                        try: original.backup(target)
                        finally: target.close(); original.close()
                        value = snapshot.read_bytes()
                    else: value = source.read_bytes()
                    manifest['files'][relative] = hashlib.sha256(value).hexdigest()
                    archive.writestr(relative, value)
                if portable_keys:
                    encrypted = _crypt(json.dumps(secrets, ensure_ascii=False).encode(), password)
                    archive.writestr('secrets.enc', encrypted)
                    manifest['keys_sha256'] = hashlib.sha256(encrypted).hexdigest()
                archive.writestr('fish-backup.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            temporary.replace(destination)
            return {'ok': True, 'path': str(destination), 'files': len(manifest['files']), 'note': manifest['credentials_note']}
        finally: temporary.unlink(missing_ok=True)

    def inspect(self, archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > 50000 or sum(i.file_size for i in infos) > 4 * 1024**3:
                raise ValueError('备份内容超过恢复上限')
            if len({i.filename for i in infos}) != len(infos): raise ValueError('备份存在重名条目')
            manifest = json.loads(archive.read('fish-backup.json'))
            if manifest.get('format') != 'fish-backup' or manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
                raise ValueError('不支持的 Fish 备份格式')
            allowed = set(manifest['files']) | {'fish-backup.json'} | ({'secrets.enc'} if manifest.get('portable_keys') else set())
            if {i.filename for i in infos} != allowed: raise ValueError('备份清单与内容不一致')
            for info in infos:
                if stat.S_ISLNK(info.external_attr >> 16) or info.file_size > 500 * 1024**2: raise ValueError('备份含不安全的文件类型或过大文件')
                if info.filename in manifest['files']:
                    _target(self.root, info.filename)
                    if hashlib.sha256(archive.read(info)).hexdigest() != manifest['files'][info.filename]: raise ValueError('文件校验失败：' + info.filename)
            if manifest.get('portable_keys') and hashlib.sha256(archive.read('secrets.enc')).hexdigest() != manifest.get('keys_sha256'):
                raise ValueError('密钥包校验失败')
        return manifest

    def restore(self, archive_path, *, password=''):
        archive_path = Path(archive_path).resolve()
        manifest = self.inspect(archive_path)
        secrets = {}
        with zipfile.ZipFile(archive_path) as archive:
            if manifest.get('portable_keys'):
                secrets = json.loads(_crypt(archive.read('secrets.enc'), password, decrypt=True))
        from secrets_store import SecretStore
        encrypted_secrets = {ref: SecretStore._crypt(str(value).encode()) for ref, value in secrets.items()}
        for ref in encrypted_secrets: SecretStore(self.root)._path(ref)
        # Preparation and password validation finish before changing current files.
        before = self.root / 'backups' / ('before-restore-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6] + '.zip')
        self.create(before)
        with settings_lock(self.root), tempfile.TemporaryDirectory(prefix='.restore-', dir=self.root) as workspace:
            stage = Path(workspace).resolve(); rollback = stage / 'rollback'; extracted = stage / 'new'; extracted.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                for relative in manifest['files']:
                    target = _target(extracted, relative); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(archive.read(relative))
            for ref, value in encrypted_secrets.items():
                target = extracted / 'harness/secrets' / (ref + '.bin'); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(value)
            relocated = self._relocate(extracted, manifest.get('source_root', ''))
            moved, installed = [], []
            try:
                # Replace owned directory roots as units so post-backup rows cannot
                # survive accidentally. Every resolved move stays inside this root.
                for relative in (*ROOTS, *FILES):
                    target, fresh = _target(self.root, relative), _target(extracted, relative)
                    old = _target(rollback, relative)
                    if target.exists():
                        old.parent.mkdir(parents=True, exist_ok=True); target.rename(old); moved.append((old, target))
                    if fresh.exists():
                        target.parent.mkdir(parents=True, exist_ok=True); fresh.rename(target); installed.append(target)
            except Exception:
                for target in reversed(installed):
                    if target.is_dir(): shutil.rmtree(target)
                    else: target.unlink(missing_ok=True)
                for old, target in reversed(moved):
                    if old.exists(): old.rename(target)
                raise
        return {'ok': True, 'files': len(manifest['files']), 'before_restore': str(before), 'relocated_fields': relocated, 'restart_required': True}

    def _relocate(self, extracted, old_root):
        """Remap path values only, never rewrite conversation prose or binaries."""
        if not old_root or old_root.rstrip('/\\').casefold() == str(self.root).rstrip('/\\').casefold(): return 0
        count = 0
        prefix = old_root.replace('\\', '/').rstrip('/')
        def move(value):
            nonlocal count
            if isinstance(value, str):
                normalized = value.replace('\\', '/')
                if normalized.casefold().startswith(prefix.casefold() + '/'):
                    suffix = normalized[len(prefix)+1:]
                    # Only paths actually belonging to this backup are portable.
                    if '..' not in PurePosixPath(suffix).parts:
                        count += 1
                        return str(self.root / Path(suffix))
            elif isinstance(value, list): return [move(v) for v in value]
            elif isinstance(value, dict): return {k: move(v) for k, v in value.items()}
            return value
        for file in extracted.rglob('*.json'):
            if file.parts[-2] == 'secrets': continue
            try: value = json.loads(file.read_text(encoding='utf-8'))
            except (ValueError, OSError, UnicodeError): continue
            write_json(file, move(value))
        # Known structured attachment/report fields; user text/source excerpts
        # remain byte-for-byte unchanged even if they mention an old path.
        for file in extracted.rglob('*.db'):
            with closing(sqlite3.connect(file)) as db, db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table, column, structured in [('messages', 'segments', True), ('reviews', 'report', True),
                        ('reviews', 'path', False), ('memories', 'note_path', False)]:
                    if table not in tables or column not in {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}: continue
                    for rowid, value in db.execute(f'SELECT rowid,"{column}" FROM "{table}"').fetchall():
                        if not value: continue
                        try: result = json.dumps(move(json.loads(value)), ensure_ascii=False) if structured else move(value)
                        except (ValueError, TypeError): continue
                        if result != value: db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (result, rowid))
        return count
