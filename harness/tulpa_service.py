"""Run unmodified Tulpa MCP with private runtime data and Fish's existing QQ WS.

The HTTP facade adapts transport only. Tulpa owns chat batches, reply plans,
sticker selection, send queues and receipts; Harness owns the model and timers.
"""
from locations import project_path
import argparse
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import sys
import threading


def application(root: Path, ui_port: int):
    source = project_path(root, 'vendor/integrations/tulpa/source')
    home = project_path(root, 'data/integrations/tulpa/app')
    home.mkdir(parents=True, exist_ok=True)
    for name in ('data', '.tmp', '.cache', 'imports'):
        (home / name).mkdir(exist_ok=True)
    sys.path[:0] = [str(source), str(project_path(root, 'channels')), str(project_path(root, 'harness'))]
    from secrets_store import SecretStore
    token = SecretStore(root).get('channel-napcat', 'NAPCAT_TOKEN')
    channel = json.loads((project_path(root, 'channels/fish_qq/config.json')).read_text('utf-8'))
    owner = str(channel['allowlist']['owner_id'])
    ws_url = channel['napcat']['url']
    os.environ.update(REPLY_ONEBOT_URL=f'http://127.0.0.1:{ui_port}/onebot',
                      REPLY_ONEBOT_TOKEN=token, REPLY_ONEBOT_WS_URL=ws_url,
                      REPLY_ONEBOT_WS_TOKEN=token, TEMP=str(home / '.tmp'),
                      TMP=str(home / '.tmp'), HF_HOME=str(home / '.cache'))
    # Override config before importing original stores. No upstream files edited.
    import chatlocal.config as config
    config.ROOT, config.DATA, config.DB = home, home / 'data', home / 'data/chats.sqlite3'
    import shutil
    prompts=home/'prompts';prompts.mkdir(exist_ok=True)
    shutil.copytree(source/'chatlocal/prompts/mcp_chat',prompts,dirs_exist_ok=True)
    import chatlocal.mcp_chat_prompts as chat_prompts
    chat_prompts.ROOT=prompts
    from fastapi import FastAPI, HTTPException, Request
    from chatlocal.mcp_routes import install_mcp_routes
    from fish_qq.onebot import OneBotClient
    bridge = OneBotClient(ws_url, token)

    @asynccontextmanager
    async def lifespan(app):
        await bridge.start()
        try:
            await asyncio.wait_for(bridge.ready.wait(), 15)
            yield
        finally:
            await bridge.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post('/onebot/{action}')
    async def onebot(action: str, request: Request):
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
            raise HTTPException(403)
        args = await request.json()
        # The owner explicitly granted the complete QQ tool environment. The
        # adapter authenticates the service, then returns the actual API result.
        if not bridge.ready.is_set():
            raise HTTPException(503, 'QQ 连接正在恢复。')
        try:
            from qq_action_record import begin,finish
            record = begin(root,action,args)
            try:
                receipt = await bridge.send_action(action, args, timeout=30)
            except Exception as exc:
                finish(record,error=str(exc))
                raise
            finish(record,receipt)
            return receipt
        except Exception as exc:
            # The caller records unknown send states; never retry here.
            raise HTTPException(502, type(exc).__name__) from None

    @app.get('/health')
    def health():
        return {'ok': True, 'qq_connected': bridge.ready.is_set(),
                'owner': owner, 'transport': 'existing-onebot-ws', 'source': str(source)}

    install_mcp_routes(app)
    return app


def watch_parent(pid: int):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        os._exit(0)
    kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
    os._exit(0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent', required=True, type=int)
    parser.add_argument('--port', type=int, default=3120)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    threading.Thread(target=watch_parent, args=(args.parent,), daemon=True).start()
    import uvicorn
    uvicorn.run(application(root, args.port), host='127.0.0.1', port=args.port, access_log=False)
