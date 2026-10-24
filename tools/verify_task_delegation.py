"""Real SDK/Workbench file handoff and concurrent chat, without QQ sends."""
import argparse
import base64
import json
import time
import uuid
from pathlib import Path
from storage import database
from tool_host import request


def verify(root, output):
    output.mkdir(parents=True, exist_ok=True)
    def call(action, **params):
        return request(root, '/workbench', {'action': action, 'params': params}, timeout=20)
    def wait(task):
        for _ in range(180):
            row = call('delegations.get', id=task)
            if row['status'] not in ('queued', 'starting', 'running', 'cancel_requested'):
                return row
            time.sleep(.5)
        raise TimeoutError('委托验收超时；未自动重新提交')
    cid = call('chat.new', title='B批验收·任务与聊天并行')['id']
    submission, challenge = uuid.uuid4().hex, uuid.uuid4().hex[:12]
    prompt = ('先通过工具在当前工作目录创建 started.txt，然后用工具等待15秒，再写一篇200字左右的中文短文，'
              '主题是雨后的书店，保存为 article.md。文章末尾包含验收编号 ' + challenge + '。请按交付约定提交成果清单。')
    started = time.monotonic()
    row = call('delegations.submit', request_id=submission, title='B批真实文章任务', prompt=prompt, conversation_id=cid)
    task = row['id']
    again = call('delegations.submit', request_id=submission, title='B批真实文章任务', prompt=prompt, conversation_id=cid)
    assert again['id'] == task
    for _ in range(120):
        active = call('delegations.get', id=task)
        workspace = Path(active['workspace'])
        if (workspace / 'started.txt').is_file():
            break
        if active['status'] not in ('queued', 'starting', 'running'):
            raise RuntimeError(active)
        time.sleep(.25)
    else:
        raise TimeoutError('没有观察到 DSH 工具创建的启动文件')
    chat_started = time.monotonic()
    chat = call('chat.send', id=cid, text='只用一句话回答：雨后散步适合带什么？', request_id=uuid.uuid4().hex)
    for _ in range(120):
        job = call('chat.job', job_id=chat['job_id'])
        if job['status'] not in ('queued', 'running'):
            break
        time.sleep(.25)
    assert job['status'] == 'complete', job
    during = call('delegations.get', id=task)
    assert during['status'] == 'running', during
    chat_snapshot = call('chat.snapshot', id=cid)
    chat_evidence = {'seconds': round(time.monotonic()-chat_started, 2), 'status': job['status'],
                     'reply': chat_snapshot['messages'][-1]['text'], 'delegation_at_chat_finish': during['status']}
    row = wait(task)
    assert row['status'] == 'completed' and row['artifact_status'] == 'ready', row
    artifact = next(a for a in row['artifacts'] if a['name'] == 'article.md')
    transfer = call('delegations.download', id=task, artifact_id=artifact['id'])
    data = base64.b64decode(transfer['data'], validate=True)
    assert data == Path(artifact['source_path']).read_bytes()
    assert challenge in data.decode('utf-8-sig')
    # Save bytes returned by the actual HTTP download action, not a copied source path.
    received = output / transfer['name']
    received.write_bytes(data)
    cancelled_request = uuid.uuid4().hex
    cancel_row = call('delegations.submit', request_id=cancelled_request, title='B批取消验证',
        prompt='先在当前目录通过工具创建 cancel-started.txt，然后用工具等待60秒，最后写 finished.txt 并提交成果清单。', conversation_id=cid)
    cancelled_id = cancel_row['id']
    for _ in range(160):
        current = call('delegations.get', id=cancelled_id)
        if (Path(current['workspace']) / 'cancel-started.txt').is_file():
            break
        if current['status'] not in ('queued','starting','running'):
            raise RuntimeError(current)
        time.sleep(.25)
    else:
        raise TimeoutError('取消测试未启动')
    cancel_started = time.monotonic()
    requested = call('delegations.cancel', id=cancelled_id)
    cancelled = wait(cancelled_id)
    assert requested['status'] in ('cancel_requested','cancelled')
    assert cancelled['status'] == 'cancelled', cancelled
    time.sleep(.5)
    assert call('delegations.get', id=cancelled_id)['status'] == 'cancelled'
    with database(root / 'data/qq/desktop-chat.db') as db:
        session = db.execute('SELECT session_id FROM conversations WHERE id=?', (cid,)).fetchone()[0]
        assert not session
    evidence = {'task': call('delegations.get', id=task), 'conversation_id': cid, 'ordinary_chat_dsh_session': session,
        'chat_parallel': chat_evidence, 'download': {'name': transfer['name'], 'bytes': len(data), 'received_path': str(received), 'matches_source': True},
        'same_request_same_task': True, 'elapsed_seconds': round(time.monotonic()-started,2),
        'cancellation': {'task': cancelled_id, 'requested': requested['status'], 'final': cancelled['status'],
                         'error': cancelled['error'], 'seconds': round(time.monotonic()-cancel_started,2),
                         'no_finished_file': not (Path(cancelled['workspace']) / 'finished.txt').exists()}}
    (output / 'B批实际入口验收.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    verify(Path(__file__).resolve().parents[1], args.output)
