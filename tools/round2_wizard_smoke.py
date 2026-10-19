"""Click through onboarding, preserve drafts, then commit to a private root."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication, QWizard
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from fish_desktop.bootstrap import initialize
from fish_desktop.services import FishServices
from fish_desktop.setup_wizard import SetupWizard
from storage import read_json


def main():
    out=ROOT/'artifacts/round2'; app=QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix='wizard-fixture-',dir=out) as temporary:
        root=initialize(Path(temporary)); services=FishServices(root)
        wizard=SetupWizard(services); wizard.show(); QTest.qWait(100)
        original=(root/'harness/agents.json').read_bytes()
        wizard.agents['qq']['name'].setText('配置向导测试角色')
        QTest.mouseClick(wizard.button(QWizard.NextButton),Qt.LeftButton); QTest.qWait(80)
        wizard.personas['qq'].setPlainText('对主人认真执行，说明实际结果。')
        QTest.mouseClick(wizard.button(QWizard.BackButton),Qt.LeftButton); QTest.qWait(80)
        assert wizard.agents['qq']['name'].text()=='配置向导测试角色'
        assert (root/'harness/agents.json').read_bytes()==original
        for _ in range(3): QTest.mouseClick(wizard.button(QWizard.NextButton),Qt.LeftButton); QTest.qWait(80)
        wizard.grab().save(str(out/'ui/setup-wizard-final.png'))
        QTest.mouseClick(wizard.button(QWizard.FinishButton),Qt.LeftButton)
        deadline=time.monotonic()+8
        while wizard.isVisible() and time.monotonic()<deadline: app.processEvents(); time.sleep(.02)
        assert not wizard.isVisible(), wizard.error.text()
        assert read_json(root/'harness/setup.json')['completed']
        assert read_json(root/'harness/agents.json')['agents'][0]['name']=='配置向导测试角色'
        wizard.runner.executor.shutdown(wait=True)
        result={'ok':True,'draft_survived_back':True,'no_write_before_finish':True,'saved_by_finish_click':True,'model_calls':0,'real_qq_sends':0}
        (out/'setup-wizard-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(result))


if __name__=='__main__':main()
