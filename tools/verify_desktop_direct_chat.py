"""Manual real-API check through the running Workbench; never sends QQ messages."""
import argparse
import json
import time
import uuid
from pathlib import Path
from PIL import Image, ImageDraw
from tool_host import request
from storage import database


def verify(root, output):
    output.mkdir(parents=True, exist_ok=True)
    def call(action, **params):
        return request(root, '/workbench', {'action': action, 'params': params}, timeout=20)
    cid = call('chat.new', title='A批验收·Fish独立聊天')['id']
    result = {'conversation': cid, 'checks': []}
    def turn(text, attachments=None):
        now = time.monotonic()
        submission = uuid.uuid4().hex
        job = call('chat.send', id=cid, text=text, attachments=attachments or [], request_id=submission)
        repeat = call('chat.send', id=cid, text=text, attachments=attachments or [], request_id=submission)
        assert repeat['job_id'] == job['job_id']
        for _ in range(200):
            state = call('chat.job', job_id=job['job_id'])
            if state['status'] not in ('queued', 'running'):
                break
            time.sleep(.5)
        else:
            raise TimeoutError('实测任务没有在约定时间内结束')
        snapshot = call('chat.snapshot', id=cid)
        row = {'input': text, 'job': job['job_id'], 'state': state['status'],
               'seconds': round(time.monotonic() - now, 2), 'error': state.get('error', '')}
        if state['status'] == 'complete':
            row['reply'] = snapshot['messages'][-1]['text']
        result['checks'].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        assert state['status'] == 'complete', row
        return row
    turn('本窗口的临时设定：我们在筹备一家叫“蓝鲸书屋”的小店，开门暗号是“月亮汽水”。只用一句话确认。')
    row = turn('刚才我们的小店叫什么，开门暗号是什么？直接回答。')
    assert '蓝鲸书屋' in row['reply'] and '月亮汽水' in row['reply']
    image = Image.new('RGB', (160, 100), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 75, 99), fill='red')
    draw.rectangle((85, 0, 159, 99), fill='blue')
    path = output / '红蓝图片输入.png'
    image.save(path)
    row = turn('这张图片左侧和右侧分别是什么颜色？只用一句话回答。', [{'path': str(path), 'type': 'image', 'name': path.name}])
    assert '红' in row['reply'] and '蓝' in row['reply']
    pending = call('chat.send', id=cid, text='请分析一万字长篇小说的章节结构，逐章展开。', request_id=uuid.uuid4().hex)
    time.sleep(1)
    cancel_note = call('chat.cancel', id=cid)
    for _ in range(40):
        state = call('chat.job', job_id=pending['job_id'])
        if state['status'] not in ('queued', 'running'):
            break
        time.sleep(.25)
    assert state['status'] == 'cancelled', state
    result['cancellation'] = {'job': pending['job_id'], 'status': state['status'], 'error': state['error'], 'note': cancel_note}
    with database(root / 'data/qq/desktop-chat.db') as db:
        session = db.execute('SELECT session_id,space_id FROM conversations WHERE id=?', (cid,)).fetchone()
        result['session_id'] = session['session_id']
        assert not session['session_id'], 'ordinary chat unexpectedly created a DSH session'
        rows = db.execute('SELECT direction,status FROM messages WHERE conversation_id=? ORDER BY id', (cid,)).fetchall()
        assert len(rows) == 7, 'duplicate submission or late cancellation output was recorded'
        result['raw_messages'] = len(rows)
    with database(root / 'data/social/request-inspector.db') as db:
        rows = db.execute('SELECT payload FROM requests WHERE session=? ORDER BY id', ('desktop:' + cid,)).fetchall()
        records = [json.loads(r['payload']) for r in rows]
        assert records and all(r['purpose'] == 'fish-desktop-chat' for r in records)
        result['request_sources'] = records[-1]['system']
    result['request_snapshots'] = len(rows)
    (output / '实际入口验收.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'verified': True, 'conversation': cid, 'cancellation': state['status']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    verify(Path(__file__).resolve().parents[1], args.output)
