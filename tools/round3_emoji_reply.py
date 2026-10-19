"""Real model chooses a library emoji; only a local mocked OneBot receives it."""
import asyncio,json,sys,tempfile
from pathlib import Path
from unittest.mock import AsyncMock
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
from PIL import Image
from fish_desktop.bootstrap import initialize
from fish_desktop.services import load_user_env
from emoji_store import EmojiStore
from tool_host import ToolHost
from model_gateway import ModelGateway
from governance import Governance
from storage import read_json,write_json
from channels.fish_qq.config import FishQQConfig
from channels.fish_qq.server import FishQQApp


def main():
    sys.stdout.reconfigure(encoding='utf-8');load_user_env();out=ROOT/'artifacts/round3'
    with tempfile.TemporaryDirectory(prefix='emoji-reply-',dir=out) as work:
        root=initialize(Path(work));cfg=read_json(root/'channels/fish_qq/config.json');cfg['allowlist']['owner_id']='12345678';write_json(root/'channels/fish_qq/config.json',cfg)
        store=EmojiStore(root);store.configure({'auto_reply':True})
        for color,description,tags in [('blue','庆祝成功，开心的表情','开心,庆祝'),('red','无语、吐槽的表情','无语,吐槽')]:
            path=root/(color+'.png');Image.new('RGB',(48,48),color).save(path);store.add(path,description,tags)
        library=store.candidates('开心 庆祝')
        gateway=ModelGateway(ROOT,'qq');before=gateway.ledger.snapshot()
        chosen=gateway.json('你在做离线表情选择验收。根据语境从给定表情库选一个合适的 id，输出 JSON {"id":"库中的id","reason":"关联理由"}。不要生成新的 id。',json.dumps({'context':'考试终于通过了，我现在很开心！','library':library},ensure_ascii=False),origin='round3-local-emoji-selection')
        row=next(r for r in store.list() if r['id']==chosen['id']);assert '开心' in row['tags']
        host=ToolHost(root);host.bind('qq','emoji-fixture','owner','12345678')
        host.execute('qq','emoji-fixture','fish_emoji_choose',{'id':chosen['id']},'choice-1')
        asset=host.dispatch('/emoji/take',{'session':'emoji-fixture'},'qq',True)['asset'];assert asset==chosen['id']
        app=FishQQApp(FishQQConfig(root=root,owner_id='12345678',state_dir=root/'channel-state'));app.state='ready';app.onebot=type('Bot',(),{})();app.onebot.send_action=AsyncMock(return_value={'data':{'message_id':'local-only'}})
        async def run():
            body={'chat_id':'private:12345678','segments':[{'type':'text','text':'真不错，恭喜你！'},{'type':'image','asset_id':asset}]}
            first=await app._send_message({**body,'idempotency_key':'emoji-1'});assert first['ok'],first
            first_args=app.onebot.send_action.call_args
            assert 'base64://' in str(first_args)
            second=await app._send_message({**body,'idempotency_key':'emoji-2'});assert second['ok'],second
            assert 'base64://' not in str(app.onebot.send_action.call_args)
            count=app.onebot.send_action.await_count
            Governance(root).save({'blocked_text':['BLOCK-EMOJI-TEST']})
            blocked=await app._send_message({'chat_id':'private:12345678','segments':[{'type':'text','text':'BLOCK-EMOJI-TEST'},{'type':'image','asset_id':asset}],'idempotency_key':'emoji-blocked'})
            assert not blocked['ok'];assert app.onebot.send_action.await_count==count
        asyncio.run(run());store.delete(asset)
        try:host.execute('qq','emoji-fixture','fish_emoji_choose',{'id':asset},'choice-deleted')
        except ValueError:pass
        else:raise AssertionError('deleted emoji selected')
        result={'ok':True,'real_model_context_selection':True,'selection_reason':chosen['reason'],'local_transport_image_delivery':True,'cooldown_preserves_text_without_image':True,'blocked_text_does_not_reach_transport':True,'deleted_emoji_cannot_be_selected':True,'usage_before':before,'usage_after':gateway.ledger.snapshot(),'real_qq_sends':0}
        (out/'emoji-reply-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if not k.startswith('usage_')},ensure_ascii=False))


if __name__=='__main__':main()
