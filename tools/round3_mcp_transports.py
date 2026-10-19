"""Actual SDK servers over Streamable HTTP and SSE, using only loopback."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))
from plugin_registry import PluginRegistry, safe_environment
from process_tools import stop_process_tree


def main():
    out = ROOT / 'artifacts/round3'; outcomes = []
    with tempfile.TemporaryDirectory(prefix='mcp-http-fixture-', dir=out) as temp:
        root = Path(temp); script = root / 'server.py'
        script.write_text('import sys,site\np=' + repr(str(ROOT / 'vendor/python')) + '\nsys.path.insert(0,p);site.addsitedir(p)\n' + '''
from mcp.server.fastmcp import FastMCP
server=FastMCP('Fish transport fixture',host='127.0.0.1',port=int(sys.argv[1]),json_response=True,stateless_http=True)
@server.tool()
def read_echo(text:str)->str:
    return 'FISH-HTTP-SSE-7369:'+text
server.run(transport=sys.argv[2])
''', encoding='utf-8')
        for kind, transport, endpoint in [('mcp_http', 'streamable-http', '/mcp'), ('mcp_sse', 'sse', '/sse')]:
            with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            log = root / (kind + '.log')
            with log.open('w', encoding='utf-8') as output:
                proc = subprocess.Popen([sys.executable, str(script), str(port), transport], env=safe_environment(), stdout=output, stderr=output, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    deadline = time.monotonic()+12
                    while time.monotonic()<deadline:
                        if proc.poll() is not None: raise RuntimeError(log.read_text(encoding='utf-8')[-1200:])
                        try:
                            with socket.create_connection(('127.0.0.1',port), timeout=.2): break
                        except OSError: time.sleep(.1)
                    registry = PluginRegistry(root); registry.save({'id':kind, 'name':kind, 'kind':kind, 'url':f'http://127.0.0.1:{port}'+endpoint, 'roles':['research']})
                    discovery = registry.discover(kind); registry.toggle(kind,True)
                    value = registry.execute('fishp__'+kind+'__read_echo',{'text':'fixture'},'research',owner=True)
                    assert 'FISH-HTTP-SSE-7369:fixture' in json.dumps(value)
                    outcomes.append({'transport':transport,'discovered':len(discovery['tools']),'real_call':True})
                finally: stop_process_tree(proc)
    result={'ok':len(outcomes)==2,'transports':outcomes,'model_calls':0,'real_qq_sends':0}
    (out/'mcp-transports-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':main()
