"""One offline integration flow before the single live owner conversation.

Uses disposable data and a fake child-model boundary, real files/processes/stores;
does not connect QQ, run paid models, touch production configuration or UI input.
"""
import base64
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
import dependencies
from fish_desktop.bootstrap import initialize
from storage import read_json,write_json,database
from social_context import SocialContext
from tool_host import ToolHost
from workbench import Workbench
from attachment_store import AttachmentStore,CHUNK_BYTES
from turn_trace import TurnTrace,latest


def run():
    observations=[]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        root=initialize(Path(temp)/'fish')
        cfg=read_json(root/'channels/fish_qq/config.json');cfg['allowlist'].update(owner_id='123456789',group_ids=['987654321']);write_json(root/'channels/fish_qq/config.json',cfg)
        contexts=SocialContext(root)
        card=contexts.save_persona('qq','集成流程人格','自然说话，信任主人，按实际工具结果回答。')
        contexts.apply_active_persona('qq',card['id'])
        host=ToolHost(root);workbench=Workbench(root);host._workbench=workbench
        try:
            chat=workbench.chat();cid=chat.new('离线连续流程')
            host.bind('qq','desktop:'+cid,'owner',desktop=True)
            host.bind('qq','group-owner','chat:group:987654321','123456789')
            folder=root/'workspace/projects/existing';folder.mkdir(parents=True);file=folder/'article.md';file.write_text('原文',encoding='utf-8')
            result=host.execute('qq','group-owner','fish_file_edit',{'path':str(file),'old':'原文','new':'实际更新'},'edit')
            assert result['replacements']==1 and file.read_text(encoding='utf-8')=='实际更新'
            output=host.execute('qq','desktop:'+cid,'fish_shell',{'command':"[Console]::Write('offline-process-ok'); exit 0",'workdir':str(folder),'wait_seconds':10},'shell')
            assert output['exit_code']==0 and 'offline-process-ok' in output['output']
            observations.append({'step':'主人身份、现有文件与真实进程','file_updated':True,'exit_code':output['exit_code']})
            store=AttachmentStore(root,'qq');size=21*1024*1024+32
            upload=store.begin(cid,'large-original.txt',size);offset=0
            marker=b'FISH-END-UNINDEXED-OK'
            while offset<size:
                count=min(CHUNK_BYTES,size-offset)
                block=b'a'*count
                if offset+count==size:block=block[:-len(marker)]+marker
                store.append(cid,upload['upload_id'],offset,base64.b64encode(block).decode());offset+=count
            saved=store.finish(cid,upload['upload_id'])
            record=saved.get('attachment',saved)
            assert Path(record['path']).stat().st_size==size
            tail=store.read_range(record['id'],offset=-len(marker),scope='owner',session='offline')
            assert marker.decode() in tail['text']
            observations.append({'step':'大于20MiB原件分块与未索引尾部取用','bytes':size,'coverage':record['processing'],'tail_read':True})
            def bind(root,role,session,scope,sender='',desktop=False,**kwargs): return host.bind(role,session,scope,sender,desktop)
            def child(root,role,**kwargs):
                session=kwargs['session'];artifact=folder/'result.txt';artifact.write_text('child-result-ok',encoding='utf-8')
                host.execute('qq',session,'fish_present_files',{'paths':[str(artifact)],'title':'真实成果'},'present')
                return json.dumps({'bubbles':[{'kind':'text','text':'子任务的真实结果已生成。'}]},ensure_ascii=False)
            with patch('tool_host.bind_session',side_effect=bind),patch('fish_chat.respond',side_effect=child):
                task=workbench.delegation().submit('a'*32,'离线子任务','写入指定工程并回传',cid,kind='task',parent_session='desktop:'+cid,source_place='desktop')
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    task=workbench.delegation().get(task['id'])
                    if task['status'] not in ('queued','starting','running','cancel_requested'):break
                    time.sleep(.05)
            assert task['status']=='completed',task
            assert task['artifacts'] and task['result']=='子任务的真实结果已生成。'
            snapshot=workbench._chat_view(cid)
            while not any(m['text']==task['result'] for m in snapshot['messages']) and time.monotonic()<deadline:
                time.sleep(.05)
                snapshot=workbench._chat_view(cid)
            assert any(m['text']==task['result'] for m in snapshot['messages'])
            wire=workbench.delegation().download(task['id'],task['artifacts'][0]['id'])
            assert base64.b64decode(wire['data'])==b'child-result-ok'
            trace=TurnTrace(root,'qq','desktop:'+cid,'offline','desktop:1');trace.event('reasoning','provider-returned-only');trace.event('tool_result',{'name':'file','ok':True});trace.finish('silent')
            seen=latest(root);assert seen['reasoning']=='provider-returned-only' and seen['phase']=='silent'
            observations.append({'step':'可选Fish子任务、原客户端结果和文件、真实trace通道','status':task['status'],'result_callback':True,'artifact_bytes':True})
            owner_session='desktop:'+cid
            reminder=host.execute('qq',owner_session,'fish_schedule',{'action':'create','params':{'target':'owner','prompt':'明确委托提醒','after_seconds':600}},'reminder')
            host.bind('qq','group-friend','chat:group:987654321','222222222')
            assert host.execute('qq','group-friend','fish_schedule',{'action':'list'},'list')==[]
            try:host.execute('qq','group-friend','fish_schedule',{'action':'cancel','params':{'id':reminder['id']}},'cancel')
            except PermissionError:pass
            else:raise AssertionError('群友不应能取消主人的提醒')
            from qq_action_record import begin,finish
            from bot_control import BotControl
            control=BotControl(root)
            def sent_via_qq_tool(root,role,**kwargs):
                rec=begin(root,'send_private_msg',{'user_id':123456789,'message':'fixture-only'},kwargs['session'])
                finish(rec,{'status':'ok','data':{'message_id':'fixture-receipt'}})
                return json.dumps({'bubbles':[{'kind':'text','text':'不能自动再发的备用正文'}]},ensure_ascii=False)
            with patch('tool_host.bind_session',side_effect=bind),patch('fish_chat.respond',side_effect=sent_via_qq_tool),patch.object(control,'deliver_bubbles',side_effect=AssertionError('已发后不能重复投递')):
                control.plans._thought(control.plans.get(reminder['id']),'fixture-slot')
            with database(control.path) as db:
                receipts=db.execute("SELECT state FROM sends WHERE id LIKE ?",('sched:'+reminder['id']+':fixture-slot:%',)).fetchall()
            assert len(receipts)==1 and receipts[0]['state']=='SUCCEEDED'
            observations.append({'step':'修复审阅发现的计划归属与重复投递连接','owner_reminder_hidden_from_friend':True,'single_recorded_delivery':True,'transport':'fixture-only'})
        finally:
            workbench.close();host.computer.interaction.close()
    return {'ok':True,'paid_models':0,'qq_sends':0,'production_changes':0,'observations':observations,
            'not_proven':['真实DeepSeek模型行为','真实QQ送达','视觉识别质量','生产后台已生效','浏览器与桌面实际交互']}


if __name__=='__main__':print(json.dumps(run(),ensure_ascii=False,indent=2))
