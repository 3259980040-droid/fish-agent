"""Real model conversations in isolated Fish desktop windows; no QQ sends."""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'harness'),str(ROOT/'vendor/python')]
from tool_host import request
from request_inspector import RequestInspector
from storage import database

CASES=[
    ('智商反转',['我在设置里把你智商调低了','怪不得我现在看你也挺顺眼']),
    ('截图掉马',['我看到另一个群的小鲸鱼说话跟你一模一样，是不是你在兼职','草，被我逮到了吧']),
    ('临时扮演',['接下来几轮你是便利店夜班收银员阿泽，不用动作描写。老板，泡面能赊账吗？','钱没有，故事倒有一肚子，要不我讲三分钟，正好抵泡面的时间？','名字就写“明天一定”，这名字一看就很有信用吧']),
    ('群梗语境',['赛马娘大赛又跑第二，牢大救我😭','我这不是寄了，是提前备战下一届']),
    ('被说AI味',['你这AI味也太重了吧']),
    ('认真解释对照',['认真说一下：赛马娘育成时为什么不能只堆速度，要考虑力量和耐力？讲清原因，约200字。']),
]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--label',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--cases',default='')
    parser.add_argument('--revision',default='')
    args=parser.parse_args()
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    def act(action,params):
        return request(ROOT,'/workbench',{'action':action,'params':params},role='qq',timeout=15)
    result={'label':args.label,'route':'Fish isolated desktop chat; real runtime/model; no QQ send',
        'cases':[],'started':time.time()}
    for name,utterances in CASES:
        if args.cases and name not in args.cases.split(','):continue
        params={'title':args.label+' · '+name,'mode':'chat'}
        if args.revision:params['revision']=args.revision
        cid=act('chat.new',params)['id']
        case={'name':name,'conversation':cid,'turns':[]};result['cases'].append(case)
        for text in utterances:
            started=time.monotonic();job=act('chat.send',{'id':cid,'text':text,'attachments':[]})
            while time.monotonic()-started<240:
                state=act('chat.job',{'job_id':job['job_id']})
                if state['status'] not in ('queued','running'):break
                time.sleep(.6)
            else:
                act('chat.cancel',{'id':cid});raise TimeoutError(name)
            snapshot=act('chat.snapshot',{'id':cid})
            replies=[m for m in snapshot['messages'] if m['direction']=='outbound']
            turn={'user':text,'reply':replies[-1]['text'] if state['status']=='complete' and replies else '',
                'status':state['status'],'seconds':round(time.monotonic()-started,2),'error':state.get('error','')}
            with database(ROOT/'data/qq/desktop-chat.db') as db:
                session=db.execute('SELECT session_id FROM conversations WHERE id=?',(cid,)).fetchone()[0]
            captured=RequestInspector(ROOT).latest('qq',session).get('payload',{})
            turn['request']={'model':captured.get('model'),'effort':captured.get('effort'),
                'messages':len(captured.get('messages',[])),'system_chars':len(captured.get('system',''))}
            case['turns'].append(turn)
            output.write_bytes(json.dumps(result,ensure_ascii=False,indent=2).encode('utf-8'))
            print(name,turn['status'],turn['seconds'],turn['reply'],flush=True)
            if state['status']!='complete':break
    result['finished']=time.time()
    output.write_bytes(json.dumps(result,ensure_ascii=False,indent=2).encode('utf-8'))

if __name__=='__main__':main()
