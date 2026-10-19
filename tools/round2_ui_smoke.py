"""Render real Qt pages at supported widths and click the memory Save action."""
import json
import os
from pathlib import Path
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase, QFont
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from fish_desktop.main_window import MainWindow
from fish_desktop.services import FishServices
from fish_desktop.theme import QSS, COLORS
from memory_store import MemoryStore
from storage import write_json


def contrast(a, b):
    def luminance(value):
        parts = [int(value[i:i+2], 16)/255 for i in (1, 3, 5)]
        parts = [p/12.92 if p <= .04045 else ((p+.055)/1.055)**2.4 for p in parts]
        return sum(p*w for p, w in zip(parts, (.2126, .7152, .0722)))
    x, y = sorted((luminance(a), luminance(b)))
    return (y+.05)/(x+.05)


def main():
    out = ROOT / 'artifacts/round2/ui'; out.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont(r'C:\Windows\Fonts\msyh.ttc'); app.setFont(QFont('Microsoft YaHei UI', 10)); app.setStyleSheet(QSS)
    services = FishServices(ROOT); window = MainWindow(services, manage_shutdown=False); window.show()
    for width, height in [(1440, 900), (1100, 720)]:
        window.resize(width, height)
        for index, (name, page, _) in enumerate(window.pages):
            window.switch_page(index); QTest.qWait(600)
            window.grab().save(str(out / f'{width}-{page.__class__.__name__}.png'))
        resources = window.pages[5][1]; window.switch_page(5)
        for index in (1, 2):
            resources.tabs.setCurrentIndex(index); QTest.qWait(350)
            window.grab().save(str(out / f'{width}-Resources-{index}.png'))
        research = window.pages[2][1]; window.switch_page(2); research.tabs.setCurrentIndex(1); QTest.qWait(350)
        window.grab().save(str(out / f'{width}-Interests.png'))
        settings = window.pages[7][1]; window.switch_page(7)
        for index in range(settings.tabs.count()):
            settings.tabs.setCurrentIndex(index); QTest.qWait(400)
            window.grab().save(str(out / f'{width}-Settings-{index}.png'))
    # Render the already verified photo conversation with the new Markdown body.
    prior = json.loads((ROOT / 'artifacts/round1/default-photo-check.json').read_text(encoding='utf-8'))
    page = window.pages[1][1]; window.switch_page(1); page.roles.setCurrentIndex(1); page.cid = prior['conversation_id']; page._fingerprint = None; page.refresh(); QTest.qWait(700)
    window.resize(1440, 900); QTest.qWait(250); window.grab().save(str(out / 'markdown-photo-chat.png'))
    # A real click in a private fixture, then inspect the saved database.
    temp = tempfile.TemporaryDirectory(prefix='ui-memory-', dir=ROOT / 'artifacts/round2')
    fixture = Path(temp.name)
    write_json(fixture / 'harness/agents.json', {'runtime': {}, 'agents': [{'id': 'qq'}, {'id': 'research'}]})
    test = MainWindow(FishServices(fixture), manage_shutdown=False); test.show(); test.navigate('资源管理')
    memory = test.pages[5][1].tabs.widget(1); test.pages[5][1].tabs.setCurrentIndex(1)
    for _ in range(150):
        QTest.qWait(50)
        if memory.loaded and not memory.loading: break
    memory.text.setPlainText('界面点击验收：主人喜欢蓝色'); memory.category.setText('验收')
    QTest.mouseClick(memory.save_btn, Qt.LeftButton)
    for _ in range(50):
        QTest.qWait(50)
        if not memory.loading: break
    saved = MemoryStore(fixture, 'qq').search('界面点击验收')
    result = {'memory_saved_by_click': bool(saved), 'memory_toast': memory.toast.text(), 'contrast': {key: round(contrast(COLORS[key], COLORS['surface']), 3) for key in ('text', 'secondary', 'muted')}, 'supported_sizes': [[1440, 900], [1100, 720]], 'real_qq_sends': 0}
    test.close(); window.close(); app.processEvents()
    for parent in (test, window):
        for runner in parent.findChildren(__import__('fish_desktop.workers', fromlist=['Runner']).Runner): runner.executor.shutdown(wait=True)
    temp.cleanup()
    (out.parent / 'ui-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['memory_saved_by_click'] and min(result['contrast'].values()) >= 4.5 else 1


if __name__ == '__main__': raise SystemExit(main())
