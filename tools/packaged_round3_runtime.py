"""Verify frozen SDK imports and bundled DSH in an empty, separate data root."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
ROOT = Path(__file__).resolve().parents[1]


def main():
    out = ROOT / 'artifacts/round3'; exe = ROOT / 'dist-round3-final/Fish/Fish.exe'
    with tempfile.TemporaryDirectory(prefix='frozen-new-user-', dir=out) as temporary:
        root = Path(temporary)
        env = os.environ.copy()
        for key in list(env):
            if any(word in key.upper() for word in ('API_KEY', 'TOKEN', 'SECRET', 'PYTHONPATH', 'PYTHONHOME')): env.pop(key)
        env['FISH_HARNESS_HOME'] = str(root)
        # Runtime provider configuration requires a key even when no model is
        # called. These values are inert fixture strings, not user credentials.
        env['FISH_QQ_DEEPSEEK_API_KEY'] = 'synthetic-offline-runtime-check'
        env['FISH_RESEARCH_DEEPSEEK_API_KEY'] = 'synthetic-offline-runtime-check'
        subprocess.run([str(exe), '--backend', 'diagnose'], cwd=root, env=env, timeout=35, check=True)
        config_path = root / 'harness/agents.json'; config = json.loads(config_path.read_text(encoding='utf-8'))
        assert config['runtime']['node'].startswith('@bundle/') and config['runtime']['cli'].startswith('@bundle/')
        config['runtime']['ports'] = {'qq':3194,'research':3195}; config['runtime']['tool_host_port']=3196
        config['runtime']['cli'] = str(root / 'old-computer/dsh.CMD')
        config['runtime']['node'] = str(root / 'old-computer/node.exe')
        config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
        completed = subprocess.run([str(exe), '--backend', 'diagnose', '--check-runtime'],cwd=root,env=env,timeout=280)
        if completed.returncode:
            for name in ('harness/backend-bootstrap.log', 'data/diagnostics/runtime-check.json'):
                file=root/name
                if file.exists(): (out/('packaged-failure-'+file.name)).write_bytes(file.read_bytes())
            raise AssertionError('Packaged runtime failed; see preserved diagnostic files')
        result = json.loads((root / 'data/diagnostics/runtime-check.json').read_text(encoding='utf-8'))
        assert result['frozen'] and result['authenticated_runtime'] and result['ok']
        assert result.get('runtime_startup_warning_codes') == [], result
        recovered=json.loads(config_path.read_text(encoding='utf-8'))['runtime']
        assert recovered['cli'].startswith('@bundle/') and recovered['node'].startswith('@bundle/')
        ui = out / 'packaged-ui-diagnostics.txt'
        subprocess.run([str(exe),'--ui-diagnostics',str(ui)],cwd=root,env=env,timeout=30,check=True)
        assert 'frozen=True' in ui.read_text(encoding='utf-8')
        subprocess.run([str(exe),'--screenshot',str(out/'packaged-ui')],cwd=root,env=env,timeout=180,check=True)
        import hashlib
        result['executable_sha256']=hashlib.sha256(exe.read_bytes()).hexdigest()
        result.update(empty_data_root=True, migrated_missing_paths_recovered=True, developer_python_not_used_by_runtime=True, packaged_ui_started=True, clean_windows_vm_tested=False)
        (out/'packaged-runtime-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
        time.sleep(.5)


if __name__=='__main__':main()
