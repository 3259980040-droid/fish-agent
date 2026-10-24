"""Real Fish API selection and optional owner-only QQ delivery; inputs are synthetic."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness'),str(ROOT/'tools'),str(ROOT/'vendor/python')]
from test_owner_private import OwnerPrivateFixture
from emoji_store import EmojiStore
from model_gateway import ModelGateway
from request_inspector import RequestInspector
from storage import database


def assets(directory):
    directory.mkdir(parents=True,exist_ok=True)
    result=[]
    for label,color,note,tag in [('static','navy','蓝底黄色圆脸，P1静图验收','P1_STATIC'),
                                 ('gif','teal','蓝绿两帧切换的测试动图，P1动图验收','P1_GIF'),
                                 ('unknown','orange','','')]:
        path=directory/(label+('.gif' if label=='gif' else '.png'))
        frames=[]
        for index in range(2 if label=='gif' else 1):
            image=Image.new('RGB',(240,180),color if index==0 else 'green');draw=ImageDraw.Draw(image)
            draw.ellipse((65+index*25,25,165+index*25,125),fill='yellow')
            draw.text((25,150),'Fish P1 '+label+' '+str(index+1),fill='white');frames.append(image)
        if label=='gif':frames[0].save(path,save_all=True,append_images=frames[1:],duration=400,loop=0)
        else:frames[0].save(path)
        result.append({'case':label,'path':str(path),'description':note,'tags':tag})
    return result


def generate(output, selected):
    output.mkdir(parents=True,exist_ok=True);gateway=ModelGateway(ROOT,'qq')
    records=[]
    for entry in assets(output/'测试素材'):
        if selected and entry['case'] not in selected:continue
        fixture=OwnerPrivateFixture();fixture.setUp()
        try:
            library=EmojiStore(fixture.root);asset=library.add(entry['path'],entry['description'],entry['tags'],source='p1-acceptance-fixture')['id']
            prompt=('只发标签'+entry['tags']+'的本地表情，不要文字代替。' if entry['tags'] else
                    '请先预览这个无备注表情，再只发这张图片，不猜描述。ID='+asset)
            fixture.gateway.base=gateway.base;fixture.gateway.model=gateway.model
            fixture.gateway.chat.side_effect=lambda messages,**kwargs:gateway.chat(messages,**kwargs)
            item=fixture.item(prompt);started=time.monotonic()
            with patch('chat_output.bubble_delay',return_value=0):asyncio.run(fixture.loop._process(item))
            with database(fixture.loop.store.db_path) as db:
                inbound=dict(db.execute('SELECT status,error FROM messages WHERE id=?',(item['message_id'],)).fetchone())
                rows=[dict(r) for r in db.execute("SELECT text,status,segments FROM messages WHERE direction='outbound' ORDER BY id")]
            bubbles=[]
            for row in rows:
                for segment in json.loads(row['segments']):
                    bubbles.append({'kind':'sticker','emoji_id':segment['asset_id']} if segment.get('asset_id') else {'kind':'text','text':segment.get('text','')})
            capture=RequestInspector(fixture.root).latest('qq','qq-private:'+item['conversation']['id'])
            result={**entry,'asset_id':asset,'inbound_kind':'synthetic isolated owner message','qq_delivery':False,
                'pass':inbound['status']=='processed' and any(b.get('emoji_id')==asset for b in bubbles),
                'seconds':round(time.monotonic()-started,2),'inbound_result':inbound,'bubbles':bubbles,'actual_request_capture':capture}
            if entry['case']=='unknown':
                messages=capture.get('payload',{}).get('messages',[])
                result['preview_on_model_request']=any(isinstance(m.get('content'),list) and any(b.get('type')=='image_url' for b in m['content']) for m in messages)
                result['pass']=result['pass'] and result['preview_on_model_request']
            records.append(result)
            (output/'真实API表情选择.json').write_text(json.dumps({'route':'Fish independent API; no DSH session; simulated transport','cases':records},ensure_ascii=False,indent=2),encoding='utf-8')
            print(entry['case'],'PASS' if result['pass'] else 'FAIL',inbound,bubbles,flush=True)
        finally:fixture.doCleanups();fixture.tearDown()


async def deliver(output, selected):
    from governance import Governance
    from secrets_store import SecretStore
    from qq_agent_loop import ChannelClient
    from owner_private import reply
    from social_context import SocialContext
    from chat_output import decode_reply
    library=EmojiStore(ROOT);owner=Governance(ROOT).owner_id()
    async def ignore(frame):pass
    client=ChannelClient('ws://127.0.0.1:3101/ws',SecretStore(ROOT).get('channel-core','FISH_QQ_TOKEN'),ignore)
    await client.start()
    try:
        for _ in range(100):
            if client.bot:break
            await asyncio.sleep(.1)
        health=await client.action('get_status',{},timeout=5)
        if (health.get('payload') or {}).get('emoji_delivery_version')!=2:raise RuntimeError('QQ通道尚未载入回执补正第2版，未发送')
        cases=json.loads((output/'真实API表情选择.json').read_text(encoding='utf-8'))['cases']
        receipt_path=output/'QQ实际发送.json'
        result=json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.is_file() else {'inbound_kind':'synthetic; owner-only real QQ outbound','cases':[]}
        for entry in cases:
            if selected and entry['case'] not in selected:continue
            if not entry['pass']:raise RuntimeError('未通过实际API选择，未发送')
            asset=library.add(entry['path'],entry['description'],entry['tags'],source='p1-acceptance-fixture')['id']
            from qq_agent_loop import QQAgentLoop
            from types import SimpleNamespace
            loop=QQAgentLoop(ROOT);receipts=[]
            async def action(name,payload,**kwargs):
                if payload.get('chat_id')!='private:'+owner:raise PermissionError('本验收仅发送主人私聊')
                value=await client.action(name,payload,**kwargs);receipts.append(value);return value
            loop.channel=SimpleNamespace(action=action)
            stamp=str(time.time_ns());contexts=SocialContext(ROOT);space=contexts.owner_space(owner)
            conv=loop.store.get_or_create_conversation('p1-acceptance:'+stamp+':'+entry['case'],'qq')
            text='P1功能验收的合成输入：'+entry['case']+'。不要保存长期记忆。'
            mid=loop.store.record_message(conv['id'],'private:'+owner,owner,'inbound','synthetic:p1:'+stamp,None,
                [{'type':'text','text':text,'origin':'test_fixture'}],text,'queued')
            item={'message_id':mid,'conversation':conv,'space_id':space['id'] if space else '',
                  'payload':{'chat_id':'private:'+owner},'text':text}
            turn='p1-acceptance-'+stamp;loop.store.create_turn(turn,conv['id'],'',turn)
            loop.store.update_message(mid,runtime_turn_id=turn)
            bubbles=decode_reply(ROOT,json.dumps({'bubbles':entry['bubbles']}),previewed={asset})
            key='owner-send:p1-acceptance:'+entry['case']+':'+stamp
            ok=await reply(loop,item,bubbles,key,turn=turn)
            with database(library.path) as db:
                sends=[dict(r) for r in db.execute('SELECT request_id,state,receipt,error FROM sends WHERE asset=? AND chat=? ORDER BY id DESC LIMIT 1',(asset,'private:'+owner))]
            with database(loop.store.db_path) as db:
                history=[dict(r) for r in db.execute('SELECT direction,status,channel_message_id,segments FROM messages WHERE conversation_id=? ORDER BY id',(conv['id'],))]
            result['cases'].append({'case':entry['case'],'asset_id':asset,'ok':ok,'request_id':key,'target':'owner_private',
                'conversation_id':conv['id'],'input_kind':'synthetic isolated acceptance conversation; not a real inbound QQ message',
                'model_selection':'reuses earlier captured real Fish API selection; not a new model run',
                'receipts':receipts,'emoji_usage':sends,'stored_history':history,
                'receiver_ui_animation_verified':False if entry['case']=='gif' else None})
            receipt_path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print('QQ',entry['case'],ok,json.dumps(receipts,ensure_ascii=False),flush=True)
    finally:await client.stop()


async def readback(output):
    """Read only exact IDs from this verifier's successful owner deliveries."""
    import requests
    from io import BytesIO
    from websockets.asyncio.client import connect
    from secrets_store import SecretStore
    from storage import read_json
    cfg=read_json(ROOT/'channels/fish_qq/config.json').get('napcat',{})
    token=SecretStore(ROOT).get('channel-napcat',cfg.get('token_env','NAPCAT_TOKEN'))
    deliveries=json.loads((output/'QQ实际发送.json').read_text(encoding='utf-8'))['cases']
    result={'source':'NapCat get_msg for exact confirmed owner-private message IDs','cases':[]}
    async with connect(cfg.get('url','ws://127.0.0.1:3001'),additional_headers={'Authorization':'Bearer '+token},proxy=None) as ws:
        for entry in deliveries:
            if not entry['ok']:continue
            for receipt in entry['receipts']:
                mid=(receipt.get('payload') or {}).get('message_id')
                if not mid:continue
                echo='p1-read-'+str(time.time_ns())
                await ws.send(json.dumps({'action':'get_msg','params':{'message_id':int(mid)},'echo':echo}))
                while True:
                    value=json.loads(await asyncio.wait_for(ws.recv(),15))
                    if value.get('echo')==echo:break
                data=value.get('data') or {};row={'case':entry['case'],'message_id':str(mid),
                    'exact_message_id_match':value.get('status')=='ok' and str(data.get('message_id'))==str(mid),
                    'image_segments':[],'receiver_ui_animation_verified':entry.get('receiver_ui_animation_verified',False) if entry['case']=='gif' else None}
                if entry.get('receiver_ui_evidence'):
                    row['receiver_ui_evidence']=entry['receiver_ui_evidence']
                for segment in data.get('message') or []:
                    if segment.get('type')!='image':continue
                    image=segment.get('data') or {};item={'type':'image','file_name':image.get('file'),'remote_url_omitted':True}
                    try:
                        from network_config import request_options
                        url=image.get('url')
                        if not url:raise ValueError('回读没有可下载的图片URL')
                        response=await asyncio.to_thread(requests.get,url,timeout=15,**request_options(ROOT,url));response.raise_for_status()
                        with Image.open(BytesIO(response.content)) as im:
                            item.update(format=im.format,frames=getattr(im,'n_frames',1))
                            suffix='.'+im.format.lower().replace('jpeg','jpg')
                        directory=output/'QQ回读图片';directory.mkdir(exist_ok=True)
                        path=directory/(entry['case']+'-'+str(mid)+suffix);path.write_bytes(response.content)
                        item['saved_path']=str(path)
                    except Exception as exc:item['download_error']=type(exc).__name__+'：图片下载或解码失败；未记录带签名URL'
                    row['image_segments'].append(item)
                result['cases'].append(row)
    (output/'QQ精确消息回读.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--case',action='append',default=[])
    parser.add_argument('--deliver-generated',action='store_true');parser.add_argument('--readback',action='store_true');args=parser.parse_args()
    if args.readback:asyncio.run(readback(Path(args.output)))
    elif args.deliver_generated:asyncio.run(deliver(Path(args.output),args.case))
    else:generate(Path(args.output),args.case)
