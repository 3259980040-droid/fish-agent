"""Application network choices. Loopback connections always remain local."""
import ipaddress
import os
from urllib.parse import urlparse
import requests
from storage import locked_settings, read_json, write_json


def validate(value):
    mode = value.get('mode', 'system')
    if mode not in ('system', 'direct', 'proxy'):
        raise ValueError('请选择系统环境、直连或 HTTP 代理')
    proxy = str(value.get('proxy', '')).strip()
    if mode == 'proxy':
        parsed = urlparse(proxy)
        try: port = parsed.port
        except ValueError: raise ValueError('代理端口不合法')
        if parsed.scheme != 'http' or not parsed.hostname or not port or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('代理请输入不含账号密码的 HTTP 地址，例如 http://127.0.0.1:7890')
    return {'mode': mode, 'proxy': proxy if mode == 'proxy' else ''}


def settings(root):
    return validate(read_json(root / 'harness/network.json', {'mode': 'system'}))


class NetworkConfig:
    def __init__(self, root): self.root = root

    @locked_settings
    def save(self, value):
        result = validate(value)
        write_json(self.root / 'harness/network.json', result)
        return result


def local_url(url):
    host = urlparse(url).hostname or ''
    if host.lower() == 'localhost': return True
    try: return ipaddress.ip_address(host).is_loopback
    except ValueError: return False


def request_options(root, url, value=None):
    cfg = validate(value) if value is not None else settings(root)
    if local_url(url) or cfg['mode'] == 'direct':
        return {'proxies': {'http': None, 'https': None, 'all': None}}
    if cfg['mode'] == 'proxy':
        return {'proxies': {'http': cfg['proxy'], 'https': cfg['proxy'], 'all': cfg['proxy']}}
    return {}


def httpx_options(root, url):
    cfg = settings(root)
    if local_url(url) or cfg['mode'] == 'direct': return {'trust_env': False}
    if cfg['mode'] == 'proxy': return {'trust_env': False, 'proxy': cfg['proxy']}
    return {'trust_env': True}


def session(root):
    result = requests.Session()
    cfg = settings(root)
    if cfg['mode'] != 'system':
        result.trust_env = False
        if cfg['mode'] == 'proxy': result.proxies.update(http=cfg['proxy'], https=cfg['proxy'])
    return result


def environment(root, env):
    env = env.copy(); cfg = settings(root)
    if cfg['mode'] != 'system':
        for key in list(env):
            if key.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'node_use_env_proxy'):
                env.pop(key)
        if cfg['mode'] == 'proxy':
            env.update(HTTP_PROXY=cfg['proxy'], HTTPS_PROXY=cfg['proxy'], NODE_USE_ENV_PROXY='1')
    # Node 24's built-in HTTP/fetch proxy support is explicitly enabled.
    elif any(env.get(k) for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')):
        env['NODE_USE_ENV_PROXY'] = '1'
    bypass = env.get('NO_PROXY', env.get('no_proxy', ''))
    env['NO_PROXY'] = ','.join(filter(None, [bypass, 'localhost', '127.0.0.1', '[::1]', '::1']))
    env['no_proxy'] = env['NO_PROXY']
    return env


def test_connection(root, value, url='https://api.deepseek.com'):
    # No key or model request: HTTP 401/403 still proves the route is reachable.
    try:
        with requests.get(url, timeout=(5, 10), allow_redirects=False, stream=True,
                          **request_options(root, url, value)) as response:
            return f'网络已连通（HTTP {response.status_code}）。此检查不调用模型、不验证 API 密钥。'
    except requests.RequestException as exc:
        raise ConnectionError('连接失败：' + type(exc).__name__ + '。请检查代理是否运行以及地址和端口。') from None
