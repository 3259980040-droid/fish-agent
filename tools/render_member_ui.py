"""Synthetic member UI and owner-edit workflow, no real QQ or model calls."""
import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
os.environ['QT_QPA_PLATFORM']='offscreen'
import dependencies
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtTest import QTest
from fish_desktop.bootstrap import initialize
from fish_desktop.services import FishServices
from fish_desktop.main_window import MainWindow
from social_roster import SocialRoster
from storage import read_json,write_json


def main():
    out=ROOT/'artifacts/social-members/ui';out.mkdir(parents=True,exist_ok=True)
    root=initialize(out/'fixture');cfg=read_json(root/'channels/fish_qq/config.json');cfg['allowlist'].update(owner_id='123456789',group_ids=['987654321']);write_json(root/'channels/fish_qq/config.json',cfg)
    app=QApplication([])
    for file in ('msyh.ttc','segoeui.ttf'):
        p=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/file
        if p.exists():QFontDatabase.addApplicationFont(str(p))
    app.setFont(QFont('Microsoft YaHei UI',9))
    roster=SocialRoster(root);roster.configure('987654321')
    roster.sync_members('987654321',[{'user_id':str(100000000+i),'nickname':f'群友 {i:02d}','card':name} for i,name in enumerate(['清风','小北','海盐','前端爱好者']+['']*56)])
    window=MainWindow(FishServices(root),manage_shutdown=False);window.resize(1320,860);window.show();window.nav.setCurrentRow(8)
    social=window.pages[8][1];social.tabs.setCurrentIndex(2);QTest.qWait(300);panel=social.members
    panel.score.setValue(86);panel.preferences.setPlainText('喜欢聊前端和开源项目；可以适当接梗，认真讨论时少打岔。');panel.locked.setChecked(True);panel._save()
    uid=panel.uid;assert roster.member('987654321',uid)['affinity']==86
    panel.preferences.setPlainText('这是一份未保存草稿');panel.table.selectRow(1);panel.table.selectRow(0)
    assert panel.preferences.toPlainText()=='这是一份未保存草稿','switching member lost draft'
    panel.preferences.setPlainText('喜欢聊前端和开源项目；可以适当接梗，认真讨论时少打岔。')
    panel._save();window.grab().save(str(out/'members-wide.png'))
    window.resize(1100,740);QTest.qWait(150);window.grab().save(str(out/'members-compact.png'))
    assert panel.save_btn.isVisible();assert panel.table.rowCount()==60
    window.close();app.processEvents();print('Member UI save, per-person drafts and two sizes passed:',out)
if __name__=='__main__':main()
