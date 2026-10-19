"""Offscreen review of social UI with synthetic data; no running bot touched."""
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
os.environ['QT_QPA_PLATFORM']='offscreen'
import dependencies
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtTest import QTest
from fish_desktop.bootstrap import initialize
from fish_desktop.services import FishServices
from fish_desktop.main_window import MainWindow
from storage import read_json,write_json

def main():
    root=initialize(ROOT/'artifacts/social-base/ui-fixture')
    cfg=read_json(root/'channels/fish_qq/config.json');cfg['allowlist'].update(owner_id='123456789',group_ids=['987654321']);write_json(root/'channels/fish_qq/config.json',cfg)
    app=QApplication([])
    for name in ('segoeui.ttf','msyh.ttc','simhei.ttf'):
        font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name
        if font.exists():QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont('Microsoft YaHei UI',9))
    window=MainWindow(FishServices(root),manage_shutdown=False);window.show()
    out=ROOT/'artifacts/social-base/ui';out.mkdir(exist_ok=True)
    social=window.pages[8][1];window.nav.setCurrentRow(8);QTest.qWait(500)
    window.grab().save(str(out/'personas.png'));social.tabs.setCurrentIndex(1);QTest.qWait(200);window.grab().save(str(out/'groups.png'))
    window.nav.setCurrentRow(1);QTest.qWait(400);chat=window.pages[1][1];chat._new();chat.editor.setPlainText('A 窗口尚未发送的草稿')
    first=chat.cid;chat._new();assert not chat.editor.toPlainText(),'composer draft leaked into new window'
    for i in range(chat.conversations.count()):
        item=chat.conversations.item(i)
        if item.data(256)==first:chat.conversations.setCurrentItem(item);break
    assert chat.editor.toPlainText()=='A 窗口尚未发送的草稿','per-window draft was lost'
    window.grab().save(str(out/'conversation.png'));window._timer.stop();chat.live_timer.stop();QTest.qWait(500);window.close();app.processEvents()
    print('Social UI and per-window draft checks passed:',out)
if __name__=='__main__':main()
