"""Click the real Qt UI in an isolated root; no model calls or real sends."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import Qt, QTimer, QCoreApplication, QEvent
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea, QMessageBox
from fish_desktop.bootstrap import initialize
from fish_desktop.main_window import MainWindow
from fish_desktop.services import FishServices
from fish_desktop.workers import Runner
from fish_desktop.widgets import MessageBubble
from storage import read_json,write_json
from plugin_registry import PluginRegistry


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    out=ROOT/'artifacts/round3/ui';out.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([])
    for font in ['segoeui.ttf','msyh.ttc','simhei.ttf']:
        assert QFontDatabase.addApplicationFont(str(Path(os.environ['WINDIR'])/'Fonts'/font)) >= 0
    app.setFont(QFont('Microsoft YaHei UI',9))
    def pump(seconds=.15):
        end=time.monotonic()+seconds
        while time.monotonic()<end: app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);time.sleep(.01)
    def wait(test,timeout=15):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            pump(.05)
            if test():return
        raise AssertionError('UI condition timed out')
    checks={}
    with tempfile.TemporaryDirectory(prefix='ui-round3-',dir=out.parent) as temporary:
        root=initialize(Path(temporary));cfg=read_json(root/'harness/agents.json')
        cfg['runtime']['ports']={'qq':3294,'research':3295};cfg['runtime']['tool_host_port']=3296;write_json(root/'harness/agents.json',cfg)
        services=FishServices(root);window=MainWindow(services,manage_shutdown=False);window.show()
        try:
            chat=services.chat('qq');cid=chat.new('旧消息定位测试')
            first=chat._record(cid,'inbound','旧照片的讨论 SEARCH-3819',[],'done')
            for n in range(130): chat._record(cid,'outbound','后续讨论第 '+str(n)+' 条',[],'done')
            window.navigate('聊天');page=window.pages[1][1];page.cid=cid;page.refresh();pump()
            page.editor.setPlainText('这段草稿应当保留')
            page.search.setPlainText('SEARCH-3819');pump()
            item=page.conversations.item(0);assert item and 'SEARCH-3819' in item.text()
            QTest.mouseClick(page.conversations.viewport(),Qt.LeftButton,pos=page.conversations.visualItemRect(item).center());pump()
            assert page.focus_id==first
            focused=[b for b in page.messages.findChildren(MessageBubble) if b.property('searchMatch')]
            assert len(focused)==1
            bubbles=[b for b in page.messages.findChildren(MessageBubble) if b.isVisible()]
            ordered=sorted(bubbles,key=lambda b:b.y())
            assert all(a.geometry().bottom() < b.y() for a,b in zip(ordered,ordered[1:]))
            label=focused[0].findChild(__import__('PySide6.QtWidgets',fromlist=['QLabel']).QLabel,'BubbleText')
            assert label.height() >= label.fontMetrics().height()
            page.refresh();assert page.editor.toPlainText()=='这段草稿应当保留'
            window.grab().save(str(out/'search-old-message.png'))
            page.search.clear();pump();page.scroll.verticalScrollBar().setValue(0);page.refresh();pump()
            assert page.scroll.verticalScrollBar().value()==0
            page.roles.setCurrentIndex(1);page.editor.setPlainText('研究草稿');page.roles.setCurrentIndex(0);pump()
            assert page.editor.toPlainText()=='这段草稿应当保留'
            checks['old_message_search_focus_draft_and_scroll']=True

            script=root/'read_tool.py'
            script.write_text('import sys,site\np='+repr(str(ROOT/'vendor/python'))+'\nsys.path.insert(0,p);site.addsitedir(p)\n'+"from mcp.server.fastmcp import FastMCP\nm=FastMCP('Round3 UI fixture')\nm._mcp_server.version='0.4.2'\n@m.tool()\ndef read_echo(text:str)->str:\n    return 'ACTUAL-PLUGIN:'+text\nm.run()\n",encoding='utf-8')
            broken=root/'broken_tool.py';broken.write_text('raise SystemExit(7)',encoding='utf-8')
            window.navigate('资源管理');resources=window.pages[5][1];resources.tabs.setCurrentIndex(0)
            plugin=resources.tabs.widget(0);wait(lambda:plugin.loaded and not plugin.loading)
            plugin._new();plugin.pid.setText('ui_fixture');plugin.name.setText('通过界面添加的 MCP');plugin.program.setText(sys.executable);plugin.args.setPlainText(str(script));plugin.research.setChecked(True)
            QTest.mouseClick(plugin.save_btn,Qt.LeftButton);wait(lambda:not plugin.loading and bool(plugin.entries))
            QTest.mouseClick(plugin.test_btn,Qt.LeftButton);wait(lambda:not plugin.loading and bool(plugin.tool_list.count()))
            assert PluginRegistry(root).entries()[0]['server_info']['version']=='0.4.2'
            plugin.enabled.setChecked(True);QTest.mouseClick(plugin.save_btn,Qt.LeftButton);wait(lambda:not plugin.loading and PluginRegistry(root).entries()[0]['enabled'])
            registry=PluginRegistry(root);value=registry.execute('fishp__ui_fixture__read_echo',{'text':'3819'},'research',owner=True)
            assert 'ACTUAL-PLUGIN:3819' in json.dumps(value)
            try: registry.execute('fishp__ui_fixture__read_echo',{'text':'3819'},'qq',owner=True)
            except PermissionError: pass
            else: raise AssertionError('other role called plugin')
            QTest.mouseClick(plugin.disable_btn,Qt.LeftButton);wait(lambda:not plugin.loading and not registry.entries()[0]['enabled'])
            plugin.args.setPlainText(str(broken));QTest.mouseClick(plugin.save_btn,Qt.LeftButton);wait(lambda:not plugin.loading)
            QTest.mouseClick(plugin.test_btn,Qt.LeftButton);wait(lambda:not plugin.loading and registry.entries()[0].get('test_ok') is False)
            assert 'ExceptionGroup' not in plugin.toast.text() and registry.entries()[0].get('last_error')
            plugin.args.setPlainText(str(script));QTest.mouseClick(plugin.save_btn,Qt.LeftButton);wait(lambda:not plugin.loading)
            QTest.mouseClick(plugin.test_btn,Qt.LeftButton);wait(lambda:not plugin.loading and registry.entries()[0].get('test_ok') is True)
            checks['plugin_ui_register_discover_version_role_disable_failure_recover']=True

            window.navigate('设置');settings=window.pages[7][1];settings.tabs.setCurrentIndex(1)
            connection=settings.tabs.widget(1).widget();wait(lambda:connection._loaded and not connection.loading)
            connection.network_mode.setCurrentIndex(2);connection.proxy.setText('invalid-proxy');QTest.mouseClick(connection.network_save,Qt.LeftButton);wait(lambda:not connection.loading)
            assert connection.proxy.text()=='invalid-proxy' and '代理' in connection.network_note.text()
            assert not (root/'harness/network.json').exists()
            connection.network_mode.setCurrentIndex(1);QTest.mouseClick(connection.network_save,Qt.LeftButton);wait(lambda:not connection.loading)
            assert read_json(root/'harness/network.json')['mode']=='direct'
            checks['network_ui_validation_draft_and_save']=True
            settings.tabs.setCurrentIndex(3);data=settings.tabs.widget(3).widget()
            target=root.parent/(root.name+'-new')
            with patch('fish_desktop.data_settings.QFileDialog.getExistingDirectory',return_value=str(target)),patch('fish_desktop.data_settings.QMessageBox.question',return_value=QMessageBox.Cancel),patch('updater.launch_maintenance') as maintenance:
                QTest.mouseClick(data.location_btn,Qt.LeftButton);pump();maintenance.assert_not_called()
            checks['data_location_ui_cancel_preserves_current_root']=True

            for width,height in [(1440,900),(1100,720)]:
                window.resize(width,height)
                for index,(_,p,_) in enumerate(window.pages):
                    window.switch_page(index);pump(.35)
                    window.grab().save(str(out/f'{width}-{p.__class__.__name__}.png'))
                window.navigate('设置')
                for index in range(settings.tabs.count()):
                    settings.tabs.setCurrentIndex(index);pump(.25);window.grab().save(str(out/f'{width}-Settings-{index}.png'))
                window.navigate('资源管理')
                for index in range(resources.tabs.count()):
                    resources.tabs.setCurrentIndex(index);pump(.25);window.grab().save(str(out/f'{width}-Resources-{index}.png'))
            result={'ok':all(checks.values()),'checks':checks,'sizes':[[1440,900],[1100,720]],'model_calls':0,'real_qq_sends':0}
            (out.parent/'ui-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
        finally:
            window._timer.stop()
            for timer in window.findChildren(QTimer): timer.stop()
            for runner in window.findChildren(Runner):runner.shutdown();runner.executor.shutdown(wait=True,cancel_futures=True)
            window.close();app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


if __name__=='__main__':main()
