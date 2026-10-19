"""Render the actual 0.3.3 failure and busy feedback from embedded UI code."""
import os,sys,json,threading,time
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='offscreen'
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.round4_completion_experience import OUT,FOLDER
experience=json.loads((OUT/'owner-photo-experience.json').read_text(encoding='utf-8'))
root=Path(experience['fixture_root']);os.environ['FISH_HARNESS_HOME']=str(root)
from PySide6.QtWidgets import QApplication,QLabel
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont,QFontDatabase
from fish_desktop.services import FishServices
from fish_desktop.main_window import MainWindow
app=QApplication([])
for name in ('segoeui.ttf','msyh.ttc','simhei.ttf'):QFontDatabase.addApplicationFont(str(Path(os.environ['WINDIR'])/'Fonts'/name))
app.setFont(QFont('Microsoft YaHei UI',9))
services=FishServices(root);window=MainWindow(services,manage_shutdown=False);window.show();window.navigate('聊天')
chat=window.pages[1][1];chat.cid=experience['chat_conversation']
snapshot=services.chat('qq').snapshot(chat.cid);chat._render(snapshot);chat._toast(experience['quota_error'])
def pump(seconds=.3):
    end=time.monotonic()+seconds
    while time.monotonic()<end:app.processEvents();time.sleep(.01)
checks=[];gate=threading.Event()
try:
    for width,height in ((1440,900),(1100,720)):
        window.resize(width,height);pump();chat.scroll.verticalScrollBar().setValue(chat.scroll.verticalScrollBar().maximum());pump()
        labels=[w for w in chat.messages.findChildren(QLabel) if '今日模型请求' in w.text()]
        assert labels and all(w.wordWrap() and w.textFormat()==Qt.PlainText for w in labels)
        assert chat.isVisible() and chat.toast.isVisible() and chat.toast.wordWrap()
        window.grab().save(str(OUT/f'quota-error-{width}.png'))
        checks.append({'width':width,'error_plain_text_and_wrap':True,'real_quota_error':chat.toast.text()})
    window.navigate('设置');system=window.pages[7][1];system.tabs.setCurrentIndex(3);page=system.tabs.widget(3).widget()
    page._submit(lambda:gate.wait(15),lambda _:None);page._progress_updates.append((42135,73932));pump()
    for width,height in ((1440,900),(1100,720)):
        window.resize(width,height);pump()
        assert page.isVisible() and page.toast.isVisible() and '42,135 / 73,932' in page.toast.text()
        assert not page.progress.isTextVisible() and not page.upgrade_btn.isEnabled()
        assert page.toast.fontMetrics().horizontalAdvance(page.toast.text())<=page.toast.width()
        window.grab().save(str(OUT/f'progress-{width}.png'))
    (OUT/'ui-layout.json').write_text(json.dumps({'ok':True,'checks':checks,'progress_sizes':[1440,1100],
        'method':'Embedded 0.3.3 Qt UI; real recorded quota failure, layout fixture for installation counts'},indent=2),encoding='utf-8')
finally:
    gate.set();pump();window.close()
print('0.3.3 error and progress UI rendered at both widths')
