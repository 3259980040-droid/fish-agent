"""Backups and local upgrades with reviewable manifests and preserved data."""
from pathlib import Path
import json
from collections import deque
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QCheckBox, QFileDialog, QHBoxLayout, QMessageBox, QPlainTextEdit, QProgressBar, QVBoxLayout
from .configuration import _input
from .pages import BasePage, _button, _label
from .theme import SPACE
from .widgets import Card


class DataPage(BasePage):
    def __init__(self, services):
        super().__init__(services)
        root = QVBoxLayout(self); root.setSpacing(SPACE['section']); root.addWidget(self.toast)
        backup = Card('备份与恢复', '备份包含两侧对话、记忆、表情、研究和配置。恢复在程序退出后执行，并先自动保留当前数据的备份。')
        self.keys = QCheckBox('包含可跨 Windows 账号恢复的 API 密钥和账号凭证'); backup.body.addWidget(self.keys)
        self.password = _input('包含可迁移凭证时，请设置至少 10 字符的备份密码', True); backup.body.addWidget(self.password)
        row = QHBoxLayout(); self.backup_btn = _button('创建备份', primary=True); self.restore_btn = _button('选择备份并恢复'); row.addWidget(self.backup_btn); row.addWidget(self.restore_btn); row.addStretch(); backup.body.addLayout(row)
        note = _label('不包含可迁移凭证时，Windows 加密密钥仅能由原 Windows 用户解密。备份本身含聊天和资料，请自行妥善保存。', 'Caption'); note.setWordWrap(True); backup.body.addWidget(note); root.addWidget(backup)
        update = Card('安装本地升级包', '先核对版本和文件清单，校验通过后安装到新目录。重新打开时继续使用当前数据目录。')
        row = QHBoxLayout(); self.upgrade_btn = _button('选择 Fish 升级包'); self.wizard_btn = _button('重新运行配置向导'); self.diagnostic_btn = _button('导出脱敏诊断')
        for w in (self.upgrade_btn, self.wizard_btn, self.diagnostic_btn): row.addWidget(w)
        row.addStretch(); update.body.addLayout(row); root.addWidget(update)
        # §2.1/2.8: keep the destructive/restart action secondary to daily backup.
        location = Card('数据存放位置', '选择空文件夹后，Fish 退出并复制两侧数据，再从新目录打开。原目录保留。')
        self.location_note = _label(str(services.root), 'Caption'); self.location_note.setWordWrap(True); location.body.addWidget(self.location_note)
        self.location_btn = _button('选择新目录并迁移'); location.body.addWidget(self.location_btn); root.addWidget(location)
        self.location_btn.clicked.connect(self._migrate)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.setPlainText('数据目录：' + str(services.root)); root.addWidget(self.details, 1)
        self.progress = QProgressBar(); self.progress.setAccessibleName('数据维护进度'); self.progress.setTextVisible(False); self.progress.hide(); root.insertWidget(1, self.progress)
        self._progress_updates = deque(maxlen=1)
        self._progress_timer = QTimer(self); self._progress_timer.setInterval(100); self._progress_timer.timeout.connect(self._show_progress)
        self._progress_timer.start()
        self.backup_btn.clicked.connect(self._backup); self.restore_btn.clicked.connect(self._restore); self.upgrade_btn.clicked.connect(self._upgrade); self.wizard_btn.clicked.connect(self._wizard); self.diagnostic_btn.clicked.connect(self._diagnostics)

    def _submit(self, fn, on_result, on_error=None, on_done=None):
        self._progress_updates.clear()
        self.progress.setRange(0, 0); self.progress.show()
        for button in (self.backup_btn, self.restore_btn, self.upgrade_btn, self.wizard_btn, self.diagnostic_btn, self.location_btn): button.setEnabled(False)
        super()._submit(fn, on_result, on_error, on_done)

    def _done(self, on_done=None):
        super()._done(on_done)
        if not self.loading:
            self.progress.hide()
            for button in (self.backup_btn, self.restore_btn, self.upgrade_btn, self.wizard_btn, self.diagnostic_btn, self.location_btn): button.setEnabled(True)

    def _show_progress(self):
        if self._progress_updates:
            completed, total = self._progress_updates.popleft()
            self.progress.setRange(0, total); self.progress.setValue(completed)
            self._toast(f'已校验并安装 {completed:,} / {total:,} 个文件（{completed * 100 // max(1, total)}%），完成后自动重新打开。')

    def _migrate(self):
        if self.loading: return
        target = QFileDialog.getExistingDirectory(self, '选择空的数据目录')
        if not target: return
        from updater import migration_target, launch_maintenance
        try: target = migration_target(self.services.root, target)
        except (ValueError, OSError) as exc: self._toast(str(exc)); return
        answer = QMessageBox.question(self, '迁移数据目录', 'Fish 将退出，复制并校验数据，再从这里打开：\n' + str(target) + '\n\n原目录保留；迁移失败时继续使用原目录。', QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes: return
        launch_maintenance(self.services.root, 'migrate', target=target)
        QApplication.instance().quit()

    def _backup(self):
        if self.loading: return
        from datetime import datetime
        destination, _ = QFileDialog.getSaveFileName(self, '保存 Fish 备份', str(self.services.root / 'backups' / ('Fish-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.zip')), 'ZIP (*.zip)')
        if not destination: return
        keys, password = self.keys.isChecked(), self.password.text()
        def work():
            from backup import BackupManager
            return BackupManager(self.services.root).create(destination, portable_keys=keys, password=password)
        self._toast('正在创建并校验备份…')
        def created(result):
            self.details.setPlainText('已创建备份：' + result['path'] + '\n文件数：' + str(result['files']) + '\n' + result['note'])
            self._toast('备份已完成。')
        self._submit(work, created)

    def _restore(self):
        if self.loading: return
        path, _ = QFileDialog.getOpenFileName(self, '选择 Fish 备份', str(self.services.root / 'backups'), 'ZIP (*.zip)')
        if not path: return
        password = self.password.text()
        def preview():
            from backup import BackupManager, _crypt
            import zipfile
            manifest = BackupManager(self.services.root).inspect(path)
            if manifest.get('portable_keys'):
                with zipfile.ZipFile(path) as z: _crypt(z.read('secrets.enc'), password, decrypt=True)
            return manifest
        def reviewed(manifest):
            answer = QMessageBox.question(self, '恢复这份备份', f"已校验 {len(manifest['files'])} 个文件。\n当前数据会先自动备份。Fish 随后退出，完成恢复后重新打开。\n\n{manifest['credentials_note']}", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Yes:
                self._toast('已取消恢复，当前数据未更改。'); return
            from updater import launch_maintenance
            launch_maintenance(self.services.root, 'restore', archive=path, password=password)
            QApplication.instance().quit()
        self._toast('正在校验备份，完成后请确认是否恢复…')
        self._submit(preview, reviewed)

    def _upgrade(self):
        if self.loading: return
        path, _ = QFileDialog.getOpenFileName(self, '选择 Fish 升级包', '', 'ZIP (*.zip)')
        if not path: return
        from updater import inspect_release, prepare_release, launch_maintenance, VERSION
        def reviewed(manifest):
            answer = QMessageBox.question(self, '准备升级', f"当前版本：{VERSION}\n升级包版本：{manifest['version']}\n文件：{len(manifest['files'])} 个\n\n将校验并安装到新目录，保留当前数据。是否继续？", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Yes:
                self._toast('已取消升级，当前程序未更改。'); return
            destination = Path(__import__('os').environ.get('LOCALAPPDATA', self.services.root)) / 'FishApp/releases'
            def prepared(result):
                launch_maintenance(self.services.root, 'upgrade', exe=result['exe'])
                QApplication.instance().quit()
            self._toast('正在校验并安装升级包，完成后 Fish 会自动退出并重新打开。')
            self._submit(lambda: prepare_release(path, destination, progress=lambda current, total: self._progress_updates.append((current, total))), prepared)
        self._toast('正在读取升级包清单…')
        self._submit(lambda: inspect_release(path), reviewed)

    def _wizard(self):
        from .setup_wizard import SetupWizard
        SetupWizard(self.services, self).exec()

    def _diagnostics(self):
        path, _ = QFileDialog.getSaveFileName(self, '导出脱敏诊断', 'Fish-diagnostics.json', 'JSON (*.json)')
        if not path: return
        def work():
            from core import HarnessConfig
            from model_gateway import ModelLedger
            from updater import VERSION
            from plugin_registry import PluginRegistry
            config = HarnessConfig(self.services.root)
            data = {'version': VERSION, 'runtime_version': config.runtime.version,
                'roles': {role: {'key_set': self.services.runtime.api_key_status(role)['set'], 'model': config.agent(role).model,
                    'usage': ModelLedger(self.services.root, role).snapshot()} for role in ('qq', 'research')},
                'plugins': [{'id': item['id'], 'kind': item['kind'], 'enabled': item['enabled'], 'roles': item['roles']} for item in PluginRegistry(self.services.root).entries()]}
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            return '诊断已导出；不含 API 密钥、账号号、聊天正文、Cookie 或带凭证的服务地址。'
        self._toast('正在导出脱敏诊断…')
        self._submit(work, self._toast)
