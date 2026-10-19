"""QR login flow adapted from dsh-bili-scroll/lib/login.mjs (MIT).

The original license is retained under vendor/sources/dsh-bili-scroll/LICENSE.
Fish stores the successful cookie bundle with Windows DPAPI, never in reports.
"""
import json
import threading
import time
from pathlib import Path
import requests
from bilibili_source import UA
from secrets_store import SecretStore


class BiliLogin:
    def __init__(self, root):
        self.root = Path(root); self.key = ''; self.started = 0
        self.session = __import__('network_config').session(root); self.session.headers['User-Agent'] = UA
        self.lock = threading.Lock()

    def start(self):
        import plugin_registry
        import qrcode
        with self.lock:
            response = self.session.get('https://passport.bilibili.com/x/passport-login/web/qrcode/generate', params={'source': 'main-fe-header'}, timeout=15)
            response.raise_for_status(); data = response.json().get('data', {})
            if not data.get('qrcode_key') or not data.get('url'):
                raise RuntimeError('B 站未返回可用二维码')
            self.key, self.started = data['qrcode_key'], time.time()
            path = self.root / 'data/research/login-qr.png'; path.parent.mkdir(parents=True, exist_ok=True)
            qrcode.make(data['url']).save(path)
            return {'ok': True, 'path': str(path), 'state': 'waiting'}

    def poll(self):
        with self.lock:
            if not self.key or time.time()-self.started > 180: return {'state': 'expired'}
            response = self.session.get('https://passport.bilibili.com/x/passport-login/web/qrcode/poll', params={'qrcode_key': self.key, 'source': 'main-fe-header'}, timeout=15, allow_redirects=False)
            response.raise_for_status(); data = response.json().get('data', {})
            code = data.get('code')
            if code != 0: return {'state': {86101: 'waiting', 86090: 'scanned', 86038: 'expired'}.get(code, 'unknown')}
            names = {'SESSDATA': 'sessdata', 'bili_jct': 'bili_jct', 'DedeUserID': 'dedeuserid', 'buvid3': 'buvid3'}
            credential = {names[c.name]: c.value for c in self.session.cookies if c.name in names}
            if not credential.get('sessdata'): raise RuntimeError('登录返回成功，但凭证不完整，请重新扫码')
            if not credential.get('buvid3'):
                try:
                    info = self.session.get('https://api.bilibili.com/x/frontend/finger/spi', timeout=10).json()
                    credential['buvid3'] = info.get('data', {}).get('b_3', '')
                except requests.RequestException: pass
            SecretStore(self.root).put('bili-account', json.dumps(credential))
            self.key = ''
            (self.root / 'data/research/login-qr.png').unlink(missing_ok=True)
            return {'state': 'done'}
