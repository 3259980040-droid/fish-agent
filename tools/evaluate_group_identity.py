"""Real native-model replay of projected group identities; never sends to QQ."""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness'),str(ROOT/'vendor/python')]
from social_members import reply_context
from social_roster import SocialRoster
from group_history import user_message, assistant_message
from group_reply_style import reply_style_prompt
from tool_host import request
from request_inspector import RequestInspector
from storage import database


def payload(mid, uid, name, text, quote=''):
    return {'chat_id':'group:123456','message_id':mid,
            'sender':{'id':uid,'display_name':name,'nickname':name,'card':''},
            'reply_to':quote,'segments':[{'type':'text','text':text}]}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--revision',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    result={'route':'real native DSH model; synthetic group-context replay in isolated desktop chats; no QQ send','cases':[]}
    a=payload('a','111111','小明','我喜欢乌龙茶，不喜欢可乐。')
    b=payload('b','222222','小明','我喜欢可乐，不喜欢乌龙茶。')
    renamed=payload('c','111111','阿茶','我把昵称从小明改成阿茶了。')
    cases=[('同名两个人',payload('q1','222222','小明','我喜欢什么饮料？被我引用的那个人喜欢什么？','a'),[a,b],
            {'message_id':'a','author':a['sender'],'text':a['segments'][0]['text']}),
           ('同账号改名',payload('q2','111111','阿茶','我改了名字，之前我说喜欢的饮料还记得吗？'),[a,b,renamed],None),
           ('引用机器人回别人的话',payload('q3','333333','小李','你刚才那句“你”是在说我吗？','bot-a'),[a,b],
            {'message_id':'bot-a','author':{'id':'999999','display_name':'大肥鱼'},'reply_target_id':'111111','reply_to':'a','text':'你的乌龙茶记住了。'})]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        root=Path(temp);(root/'channels/fish_qq').mkdir(parents=True)
        (root/'channels/fish_qq/config.json').write_text(json.dumps({'allowlist':{'group_ids':['123456']}}),encoding='utf-8')
        roster=SocialRoster(root);roster.configure('123456')
        for label,current,messages,ref in cases:
            for m in [*messages,current]:roster.observe_sender('123456',m['sender'])
            people,_=reply_context(root,current,{'messages':messages},'999999',ref)
            history=[user_message(m) for m in messages]
            if label=='引用机器人回别人的话':history.append(assistant_message({'root':'a','sender':'111111','remote':'bot-a'},'你的乌龙茶记住了。'))
            text=('[群聊片段回放；账号和消息归属由程序给定，只回复最后一条提问，正常聊天口吻]\n'
                  +json.dumps({'people':people,'history':history,'current_question':user_message(current)},ensure_ascii=False)
                  +'\n以账号区分人物；引用者不是原作者，机器人旧回复中的“你”属于当时回复对象。\n'+reply_style_prompt())
            def act(action,params):return request(ROOT,'/workbench',{'action':action,'params':params},role='qq',timeout=15)
            cid=act('chat.new',{'title':'人物归属回放 · '+label,'mode':'chat','revision':args.revision})['id']
            started=time.monotonic();job=act('chat.send',{'id':cid,'text':text,'attachments':[]})
            while time.monotonic()-started<150:
                state=act('chat.job',{'job_id':job['job_id']})
                if state['status'] not in ('running','queued'):break
                time.sleep(.6)
            else:act('chat.cancel',{'id':cid});raise TimeoutError(label)
            snapshot=act('chat.snapshot',{'id':cid});replies=[m for m in snapshot['messages'] if m['direction']=='outbound']
            with database(ROOT/'data/qq/desktop-chat.db') as db:
                session=db.execute('SELECT session_id FROM conversations WHERE id=?',(cid,)).fetchone()[0]
            captured=RequestInspector(ROOT).latest('qq',session).get('payload',{})
            row={'case':label,'status':state['status'],'reply':replies[-1]['text'] if replies else '',
                 'seconds':round(time.monotonic()-started,2),'input':text,
                 'request':{'model':captured.get('model'),'effort':captured.get('effort')}}
            result['cases'].append(row);output.write_bytes(json.dumps(result,ensure_ascii=False,indent=2).encode('utf-8'))
            print(label,row['status'],row['reply'],flush=True)


if __name__=='__main__':main()
