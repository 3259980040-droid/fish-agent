"""Run an authorized, isolated DeepSeek Harness development job.

The prompt and outputs stay in the job directory. API credentials are passed in
the child environment only; the worker never inherits QQ transport credentials.
"""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]


def main(job):
    from core import HarnessConfig, RuntimeManager
    from secrets_store import SecretStore
    from fish_desktop.services import load_user_env
    load_user_env()
    manager = RuntimeManager(HarnessConfig(ROOT))
    job = job.resolve()
    if not job.is_relative_to(ROOT / 'artifacts/social-base/deepseek'):
        raise ValueError('Worker directory outside the dedicated development area')
    job.mkdir(parents=True, exist_ok=True)
    home = job / 'home'; home.mkdir(exist_ok=True)
    patch = job / 'worker.patch.yml'
    patch.write_text('''- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: deepseek-official
    model: deepseek-flash
    reasoningEffort: off
- id: llm-deepseek
  config:
    apiKeyEnv: DEEPSEEK_API_KEY
    baseURL: https://api.deepseek.com/anthropic
    maxTokens: 24576
    thinking: disabled
    reasoningEffort: off
    retryPolicy:
      mode: normal
      maxRetries: 0
- id: llm-deepseek-account
  disabled: true
''', encoding='utf-8')
    key = SecretStore(ROOT).get('agent-qq', 'FISH_QQ_DEEPSEEK_API_KEY')
    if not key:
        raise ValueError('Configured DeepSeek credential unavailable')
    env = manager.runtime_env()
    for name in list(env):
        if any(word in name.upper() for word in ('TOKEN', 'API_KEY', 'SECRET', 'FISH_', 'DSH_HOME', 'DSH_AGENTS_HOME')):
            env.pop(name, None)
    env.update(DSH_HOME=str(home), DSH_AGENTS_HOME=str(home / 'shared-agents'),
               DEEPSEEK_API_KEY=key, PYTHONIOENCODING='utf-8')
    command = manager._cmd(['--profile', 'headless', '--patch', str(patch), '--json', '-'])
    if not command:
        raise ValueError('DeepSeek Harness CLI unavailable')
    started = time.time()
    with (job / 'events.jsonl').open('wb') as output, (job / 'stderr.log').open('wb') as error:
        process = subprocess.Popen(command, cwd=job, env=env, stdin=subprocess.PIPE,
            stdout=output, stderr=error, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        (job / 'process.json').write_text(json.dumps({'pid': process.pid, 'started': started,
            'model': 'deepseek-flash', 'provider': 'DeepSeek via configured Harness',
            'workspace': str(job)}, indent=2), encoding='utf-8')
        try:
            process.communicate((job / 'TASK.md').read_bytes(), timeout=1200)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            raise TimeoutError('DeepSeek job reached its bounded run time')
    result = {'exit_code': process.returncode, 'elapsed_seconds': round(time.time()-started, 1),
              'files': [str(p.relative_to(job)) for p in job.glob('*.py')]}
    (job / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
    return process.returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('job', type=Path)
    raise SystemExit(main(parser.parse_args().job))
