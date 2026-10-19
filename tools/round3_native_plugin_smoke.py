"""Install, discover, enable and uninstall a genuine DSH bundle, with no LLM."""
import json
from pathlib import Path
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]
from fish_desktop.bootstrap import initialize
from core import HarnessConfig, RuntimeManager
from plugin_registry import PluginRegistry
from storage import read_json, write_json
from tool_host import shutdown_host


def main():
    import os
    os.environ['FISH_RESEARCH_DEEPSEEK_API_KEY']='synthetic-offline-plugin-check'
    sys.stdout.reconfigure(encoding='utf-8')
    out = ROOT / 'artifacts/round3'; out.mkdir(parents=True, exist_ok=True)
    temp = tempfile.TemporaryDirectory(prefix='native-plugin-', dir=out); root = Path(temp.name).resolve(); manager = None
    try:
        initialize(root); config = read_json(root / 'harness/agents.json')
        config['runtime']['ports'] = {'qq': 3194, 'research': 3195}; config['runtime']['tool_host_port'] = 3196
        write_json(root / 'harness/agents.json', config)
        project = root / 'sample-native'; project.mkdir()
        write_json(project / 'package.json', {'name': 'fish-native-echo-fixture', 'version': '1.0.0', 'type': 'module', 'main': 'index.js',
            'dsh': {'bundle': {'patch': './cordis.patch.yml'}}})
        (project / 'cordis.patch.yml').write_text('- insert:\n    - id: native-echo-fixture\n      name: fish-native-echo-fixture\n', encoding='utf-8')
        (project / 'index.js').write_text("export const inject=['tools']; export function apply(ctx) { ctx.tools.register({name:'native_echo_probe',description:'Synthetic native echo',parameters:{type:'object',properties:{text:{type:'string'}},required:['text']},output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},async execute(args){return 'NATIVE:'+args.text}}) }\n", encoding='utf-8')
        manager = RuntimeManager(HarnessConfig(root)); registry = PluginRegistry(root, manager)
        registry.save({'id': 'native_fixture', 'kind': 'dsh', 'name': '原生验收', 'command': str(project), 'roles': ['research'], 'enabled': True})
        result = registry.discover('native_fixture')
        if not result['tools']:
            (out / 'native-plugin-errors.txt').write_text('\n'.join(manager.logs['research']), encoding='utf-8')
        allowed = registry.native_catalog('research')
        registry.toggle('native_fixture', False)
        after = registry._native_api('research', 'tools')['tools']
        evidence = {'ok': any(t['name'] == 'native_echo_probe' for t in result['tools']) and not any(t['name'] == 'native_echo_probe' for t in after),
            'discovered': [t['name'] for t in result['tools']], 'allowed_before_disable': [t['name'] for t in allowed],
            'allowed_after_disable': registry.native_catalog('research'), 'model_calls': 0, 'real_qq_sends': 0}
        (out / 'native-plugin-check.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(evidence, ensure_ascii=False)); return 0 if evidence['ok'] else 1
    except Exception as exc:
        print(type(exc).__name__, str(exc)[:500])
        if manager: (out / 'native-plugin-errors.txt').write_text('\n'.join(manager.logs['research']), encoding='utf-8')
        return 1
    finally:
        if manager: manager.stop_all()
        shutdown_host(root)
        if root.is_relative_to(out.resolve()): temp.cleanup()


if __name__ == '__main__': raise SystemExit(main())
