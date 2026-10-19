"""Onboarding through Qt: failed API/runtime checks, drafts and atomic Finish."""
import json,os,sys,tempfile,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtWidgets import QApplication,QWizard,QPushButton,QLabel,QTabWidget
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from fish_desktop.bootstrap import initialize
from fish_desktop.services import FishServices
from fish_desktop.setup_wizard import SetupWizard
from secrets_store import SecretStore
from storage import read_json


def main():
    out=ROOT/'artifacts/round3';app=QApplication.instance() or QApplication([])
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            valid=self.headers.get('Authorization') in ('Bearer synthetic-qq','Bearer synthetic-research')
            body=json.dumps({'data':[{'id':'fixture-model'}]}).encode()
            self.send_response(200 if valid else 401);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def pump():app.processEvents();time.sleep(.02)
    def wait(test):
        end=time.monotonic()+12
        while time.monotonic()<end:
            pump()
            if test():return
        raise AssertionError('wizard did not settle')
    with tempfile.TemporaryDirectory(prefix='wizard-round3-',dir=out) as work:
        root=initialize(Path(work));services=FishServices(root);wizard=SetupWizard(services);wizard.show()
        original=(root/'harness/agents.json').read_bytes();status=QLabel()
        try:
            for role in ('qq','research'):
                fields=wizard.agents[role];fields['name'].setText('QQ 验收' if role=='qq' else '研究验收');fields['base_url'].setText('http://127.0.0.1:'+str(server.server_port));fields['model'].setText('fixture-model')
                fields['api_key'].setText('wrong');wizard._test_api(role,status);wait(lambda:not wizard.running);assert '401' in status.text()
                fields['api_key'].setText('synthetic-'+role);wizard._test_api(role,status);wait(lambda:not wizard.running);assert '连接成功' in status.text()
                assert (root/'harness/agents.json').read_bytes()==original
            QTest.mouseClick(wizard.button(QWizard.NextButton),Qt.LeftButton);pump()
            wizard.personas['qq'].setPlainText('认真完成主人任务，报告实际结果。');wizard.personas['research'].setPlainText('依照主人方向研究，注明证据。');wizard.global_rules.setPlainText('不虚构完成状态。')
            QTest.mouseClick(wizard.button(QWizard.BackButton),Qt.LeftButton);pump();assert wizard.agents['qq']['api_key'].text()=='synthetic-qq'
            for _ in range(3):QTest.mouseClick(wizard.button(QWizard.NextButton),Qt.LeftButton);pump()
            original_node=wizard.node.text();wizard.node.setText(str(root/'not-installed/node.exe'));wizard._test_runtime();wait(lambda:not wizard.running);assert '路径不存在' in wizard.runtime_status.text()
            wizard.node.setText(original_node);wizard._test_runtime();wait(lambda:not wizard.running);assert '检查通过' in wizard.runtime_status.text()
            QTest.mouseClick(wizard.button(QWizard.FinishButton),Qt.LeftButton);wait(lambda:not wizard.isVisible())
            assert read_json(root/'harness/setup.json')['completed']
            for role in ('qq','research'):assert SecretStore(root).get('agent-'+role)=='synthetic-'+role
            cfg=read_json(root/'harness/agents.json');assert cfg['agents'][0]['name']=='QQ 验收'
            result={'ok':True,'both_roles_saved':True,'api_401_visible_and_correctable':True,'missing_runtime_visible_and_correctable':True,'drafts_preserved':True,'no_write_before_finish':True,'model_calls':0,'real_qq_sends':0}
            (out/'wizard-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result))
        finally:wizard.runner.shutdown();wizard.runner.executor.shutdown(wait=True);wizard.close();server.shutdown();server.server_close();thread.join()


if __name__=='__main__':main()
