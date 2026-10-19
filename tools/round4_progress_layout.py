"""Render final EXE embedded maintenance widget at both supported sizes."""
import importlib.abc,importlib.util,json,os,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];folder=ROOT/'dist-round4-delivery/Fish';out=ROOT/'artifacts/round4'
os.environ.update(QT_QPA_PLATFORM='offscreen',FISH_HARNESS_HOME=str(out/'migrated-data'))
sys.path.insert(0,str(ROOT/'vendor/python'))
from PyInstaller.archive.readers import CArchiveReader
pyz=CArchiveReader(str(folder/'Fish.exe')).open_embedded_archive('PYZ.pyz');owned={p.stem for p in (ROOT/'harness').glob('*.py')}
class FrozenModules(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    def find_spec(self,name,path=None,target=None):
        if name in pyz.toc and (name.split('.')[0] in owned or name.startswith(('fish_desktop','harness','channels.fish_qq'))):return importlib.util.spec_from_loader(name,self,is_package=pyz.toc[name][0]==1)
    def create_module(self,spec):return None
    def exec_module(self,module):
        module.__file__=str(folder/'_internal'/Path(*module.__name__.split('.')).with_suffix('.py'));exec(pyz.extract(module.__name__),module.__dict__)
sys.meta_path.insert(0,FrozenModules());sys._MEIPASS=str(folder/'_internal');sys.frozen=True;sys.executable=str(folder/'Fish.exe')
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont,QFontDatabase
from fish_desktop.services import FishServices
from fish_desktop.main_window import MainWindow
app=QApplication([])
for name in ('segoeui.ttf','msyh.ttc','simhei.ttf'):QFontDatabase.addApplicationFont(str(Path(os.environ['WINDIR'])/'Fonts'/name))
app.setFont(QFont('Microsoft YaHei UI',9))
window=MainWindow(FishServices(out/'migrated-data'),manage_shutdown=False);window.show();window.navigate('设置')
system=window.pages[7][1];system.tabs.setCurrentIndex(3);page=system.tabs.widget(3).widget()
gate=threading.Event()
def pump(seconds=.3):
    end=time.monotonic()+seconds
    while time.monotonic()<end:app.processEvents();time.sleep(.01)
try:
    page._submit(lambda:gate.wait(20),lambda _:None);page._progress_updates.append((42135,73932));pump()
    checks=[]
    for width,height in ((1440,900),(1100,720)):
        window.resize(width,height);pump()
        assert page.isVisible() and page.toast.isVisible()
        window.grab().save(str(out/f'final-progress-{width}.png'))
        print(json.dumps({'width':width,'label_height':page.toast.height(),'font_height':page.toast.fontMetrics().height(),'label_hint_height':page.toast.sizeHint().height(),'text_width':page.toast.fontMetrics().horizontalAdvance(page.toast.text()),'label_width':page.toast.width()}),flush=True)
        assert '42,135 / 73,932' in page.toast.text() and '56%' in page.toast.text()
        assert page.toast.fontMetrics().horizontalAdvance(page.toast.text())<=page.toast.width()
        assert page.toast.height()>=page.toast.sizeHint().height()
        assert not page.progress.isTextVisible() and not page.upgrade_btn.isEnabled()
        window.grab().save(str(out/f'final-progress-{width}.png'));checks.append({'width':width,'height':height,'status_text_fits':True,'duplicate_actions_disabled':True})
    (out/'progress-layout-check.json').write_text(json.dumps({'ok':True,'checks':checks,'method':'Final EXE embedded Qt widget with explicit layout fixture counts; full actual installation recorded separately'},indent=2),encoding='utf-8')
finally:
    gate.set();pump();window.close()
print('Final embedded progress layout passed at 1440 and 1100')
