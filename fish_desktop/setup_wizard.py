"""Onboarding: editable drafts, explicit connection tests and a final commit.

§2.1/§2.8/§3.6: one Finish action commits all pages. Credentials use password
fields; labels stay next to their inputs. Existing spacing tokens are retained.
"""
import asyncio
import json
import time
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QPlainTextEdit, QTabWidget, QVBoxLayout, QWizard, QWizardPage, QWidget)
from .configuration import _input
from .pages import _button, _label
from .theme import SPACE, QSS
from .workers import Runner


class SetupWizard(QWizard):
    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.services = services; self.runner = Runner(self); self.running = False
        self.setWindowTitle('Fish · 首次配置'); self.resize(780, 720); self.setStyleSheet(QSS)
        self.setWizardStyle(QWizard.ModernStyle)
        self.setButtonText(QWizard.NextButton, '下一步'); self.setButtonText(QWizard.BackButton, '上一步'); self.setButtonText(QWizard.FinishButton, '保存并开始'); self.setButtonText(QWizard.CancelButton, '稍后配置')
        self.agents = {}; self.personas = {}
        roles = QWizardPage(); roles.setTitle('接入 DeepSeek'); roles.setSubTitle('两个角色分别保存对话和记忆。照片随普通消息直接发送。')
        layout = QVBoxLayout(roles); tabs = QTabWidget(); layout.addWidget(tabs)
        for role, label in [('qq', 'QQ 角色'), ('research', '研究角色')]:
            current = services.agent_editor(role)
            page = QWidget(); form = QFormLayout(page); form.setVerticalSpacing(SPACE['gap'])
            fields = {key: _input(hint, key == 'api_key') for key, hint in [('name', '角色名称'), ('base_url', 'https://api.deepseek.com'), ('model', 'deepseek-flash'), ('api_key', '留空保留已有密钥；首次接入时填写')]}
            for key, title in [('name', '角色名称'), ('base_url', 'API 地址'), ('model', '模型'), ('api_key', 'API 密钥')]:
                if key in current: fields[key].setText(current[key])
                form.addRow(title, fields[key])
            state = _label('密钥已配置' if current['key_set'] else '尚未配置密钥', 'Muted'); form.addRow(state)
            test = _button('测试 API 连接'); form.addRow(test); test.clicked.connect(lambda _=False, r=role, s=state: self._test_api(r, s))
            self.agents[role] = fields; tabs.addTab(page, label)
        self.addPage(roles)
        personas = QWizardPage(); personas.setTitle('人格与研究方向'); personas.setSubTitle('人格可以有特色；对主人任务的认真执行和权限核验由程序继续约束。')
        layout = QVBoxLayout(personas); tabs = QTabWidget(); layout.addWidget(tabs, 1)
        for role, label in [('qq', 'QQ 人格'), ('research', '研究人格')]:
            text = QPlainTextEdit(); text.setPlainText(services.agent_editor(role)['persona']); self.personas[role] = text; tabs.addTab(text, label)
        self.global_rules = QPlainTextEdit(); self.global_rules.setPlainText(services.agent_editor('qq')['global']); tabs.addTab(self.global_rules, '全局注入')
        self.directions = QPlainTextEdit(); self.directions.setMaximumHeight(90); self.directions.setPlaceholderText('你决定研究兴趣方向，每行一项。留空时不会自动研究。')
        from research_autonomy import ResearchControl
        self.directions.setPlainText('\n'.join(ResearchControl(services.root).config()['directions']))
        layout.addWidget(_label('B 站研究方向', 'CardTitle')); layout.addWidget(self.directions); self.addPage(personas)
        qq = QWizardPage(); qq.setTitle('主人与 QQ 连接'); qq.setSubTitle('可以先使用本地对话，稍后再连接 NapCat。真实 QQ 号决定主人身份。')
        form = QFormLayout(qq); form.setVerticalSpacing(SPACE['gap'])
        self.enable_qq = QCheckBox('配置 QQ 接入（群聊回复仍保持关闭）'); self.owner = _input('主人 QQ 号'); self.owner.setText(services.owner_id())
        connections = services.connection_snapshot(); self.napcat_url = _input('ws://127.0.0.1:3001'); self.napcat_url.setText(connections['napcat_url']); self.napcat_token = _input('留空保留已保存的 NapCat 密钥', True)
        form.addRow(self.enable_qq); form.addRow('主人 QQ', self.owner); form.addRow('NapCat 地址', self.napcat_url); form.addRow('NapCat 密钥', self.napcat_token)
        state = _label('测试只读取登录状态，不发送消息。', 'Muted'); form.addRow(state)
        test = _button('测试 NapCat 连接'); form.addRow(test); test.clicked.connect(lambda: self._test_napcat(state)); self.addPage(qq)
        runtime = QWizardPage(); runtime.setTitle('确认运行环境'); runtime.setSubTitle('使用随 Fish 提供的 DSH 和 Node，或选择已有安装。保存后仍可在系统设置修改。')
        form = QFormLayout(runtime); form.setVerticalSpacing(SPACE['gap'])
        self.cli = _input('DSH 启动器路径'); self.cli.setText(connections['cli']); self.node = _input('Node 路径'); self.node.setText(connections['node'])
        form.addRow('DSH', self.cli); form.addRow('Node', self.node)
        if not self.node.text():
            detected = services.runtime.node_path()
            if detected: self.node.setText(str(detected))
        self.runtime_status = _label('', 'Muted'); self.runtime_status.setWordWrap(True); form.addRow(self.runtime_status)
        test = _button('检查运行环境'); form.addRow(test); test.clicked.connect(self._test_runtime)
        self.error = _label('', 'Muted'); self.error.setWordWrap(True); form.addRow(self.error)
        form.addRow(_label('保存会一次提交上述配置；API 密钥由 Windows 加密。已有对话不会因为修改配置而自动重发。', 'Caption')); self.addPage(runtime)

    def _run(self, fn, success, status):
        if self.running: return
        self.running = True; status.setText('正在检查…')
        self.runner.submit(fn, success, lambda message: status.setText(message), lambda: setattr(self, 'running', False))

    def _test_api(self, role, status):
        values = {k: w.text() for k, w in self.agents[role].items()}
        def work():
            import requests
            from setup_config import validate_api
            from secrets_store import SecretStore
            validate_api(values['base_url'], values['model'])
            env = self.services.config.agent(role).apiKeyEnv
            key = values['api_key'] or SecretStore(self.services.root).get('agent-' + role, env)
            if not key: raise ValueError('请先填写 API 密钥')
            response = requests.get(values['base_url'].rstrip('/') + '/models', headers={'Authorization': 'Bearer ' + key}, timeout=(5, 12),
                **__import__('network_config').request_options(self.services.root, values['base_url']))
            if not response.ok: raise ValueError(f'API 连接失败（HTTP {response.status_code}）')
            models = [m['id'] for m in response.json().get('data', [])]
            return '连接成功；模型已列出' if values['model'] in models else '连接成功；所填模型未在列表中出现，首次聊天时仍需核对。'
        self._run(work, status.setText, status)

    def _test_napcat(self, status):
        url, raw_key = self.napcat_url.text(), self.napcat_token.text()
        async def check():
            from websockets.asyncio.client import connect
            from secrets_store import SecretStore
            key = raw_key or SecretStore(self.services.root).get('channel-napcat', 'NAPCAT_TOKEN')
            headers = {'Authorization': 'Bearer ' + key} if key else {}
            async with connect(url, additional_headers=headers, open_timeout=5,
                               proxy=None if __import__('network_config').local_url(url) else True) as ws:
                await ws.send(json.dumps({'action': 'get_login_info', 'params': {}, 'echo': 'fish-setup-check'}))
                end = time.monotonic() + 8
                while time.monotonic() < end:
                    data = json.loads(await asyncio.wait_for(ws.recv(), end-time.monotonic()))
                    if data.get('echo') == 'fish-setup-check':
                        if data.get('retcode') != 0: raise ValueError('NapCat 尚未登录或拒绝查询')
                        return '连接成功，NapCat 已登录。'
                raise ValueError('未收到 NapCat 登录状态')
        self._run(lambda: asyncio.run(check()), status.setText, status)

    def _test_runtime(self):
        cli, node = self.cli.text(), self.node.text()
        def work():
            from core import HarnessConfig, RuntimeManager, resolve
            config = HarnessConfig(self.services.root); config.runtime.cli = cli; config.runtime.node = node
            for value in (cli, node):
                if not resolve(self.services.root, value).is_file(): raise ValueError('运行时路径不存在')
            version = RuntimeManager(config)._version()
            if version != config.runtime.version: raise ValueError('DSH 版本不匹配：' + str(version))
            return 'DSH ' + version + ' 检查通过；没有启动模型请求。'
        self._run(work, self.runtime_status.setText, self.runtime_status)

    def accept(self):
        if self.running: self.error.setText('请等待当前连接检查完成。'); return
        values = {'agents': {role: {**{k: w.text() for k, w in fields.items()}, 'persona': self.personas[role].toPlainText()} for role, fields in self.agents.items()},
            'global': self.global_rules.toPlainText(), 'directions': self.directions.toPlainText().splitlines(), 'enable_qq': self.enable_qq.isChecked(), 'owner_id': self.owner.text(),
            'napcat_url': self.napcat_url.text(), 'napcat_token': self.napcat_token.text(), 'cli': self.cli.text(), 'node': self.node.text()}
        from setup_config import save_setup
        def done(_):
            from core import HarnessConfig
            self.services.config = HarnessConfig(self.services.root); self.services.runtime.config = self.services.config; self.services.runtime.personas.config = self.services.config
            super(SetupWizard, self).accept()
        self._run(lambda: save_setup(self.services.root, values), done, self.error)

    def done(self, result):
        if self.running and result != QDialog.DialogCode.Accepted:
            self.error.setText('正在检查或保存，请等待完成后关闭。')
            return
        self.runner.shutdown()
        super().done(result)
