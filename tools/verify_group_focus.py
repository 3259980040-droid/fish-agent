"""Native-DSH focused group replays; isolated input/history, no QQ group sending."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'harness'), str(ROOT/'tools'), str(ROOT/'vendor/python')]
from test_group_concurrency import ConcurrentTests
from core import HarnessConfig, RuntimeManager
from runtime_adapter import DSHRuntimeAdapter
from group_tasks import GroupTaskStore, child_scope
from social_context import SocialContext
from send_policy import outbound_source
from storage import database, read_json
from tool_host import bind_session, set_group_context
from request_inspector import RequestInspector


def shared_history_checks(a, b, view, capture):
    """Require original attributed history blocks, not IDs repeated in metadata."""
    selected = {r['message_id'] for r in view.get('related_history', [])}
    originals = {}
    for message in capture.get('payload', {}).get('messages', []):
        if message.get('source') != 'fish-dialogue-history': continue
        for block in message.get('content', []):
            text = block.get('text', '')
            if not text.startswith('【群消息 '): continue
            try:
                head, body = text.split('】\n', 1)
                header = json.loads(head.removeprefix('【群消息 '))
                originals[str(header['message_id'])] = (header, body)
            except (ValueError, KeyError): continue
    def original_visible(payload):
        header, body = originals.get(payload['message_id'], ({}, ''))
        expected = ''.join(s.get('text', '') for s in payload['segments'] if s.get('type') == 'text')
        return (header.get('sender_id') == str(payload['sender']['id']) and expected in body
                and header.get('reply_to', '') == payload.get('reply_to', ''))
    return {'a_b_selected': {a['message_id'], b['message_id']} <= selected,
            'a_original_visible_with_sender': original_visible(a),
            'b_original_visible_with_sender_and_reply': original_visible(b)}


async def run(output, selected=()):
    client = DSHRuntimeAdapter(ROOT, 'qq', RuntimeManager(HarnessConfig(ROOT))).connect(
        require_isolation=True, require_group_context=True, require_group_history=True)
    personas = SocialContext(ROOT)
    persona = personas.ensure_default('qq')
    native_jobs = GroupTaskStore(ROOT)
    result = {'route': 'actual GroupDialogue.run_one + native DSH; synthetic inbound; simulated QQ transport',
              'qq_group_send': False, 'cases': []}
    if selected and output.is_file(): result=json.loads(output.read_text(encoding='utf-8'))
    labels = ('unrelated_questions', 'shared_reply_chain', 'nonempty_at_text', 'owner_delegated_source',
              'owner_delegated_unquoted', 'mixed_origins_unquoted', 'generic_phrase_unrelated')
    for index, label in enumerate(labels):
        if selected and label not in selected: continue
        fixture = ConcurrentTests(methodName='test_mention_without_words_is_a_valid_prompt')
        fixture.setUp()
        try:
            fixture.setup_dialogue()
            fixture.d.merge_s = 0
            fixture.d.session = 'legacy-surface-not-reused'
            mapping = {}
            for item in fixture.patches[:3]: item.stop()
            def native_bind(root, role, session, scope, sender):
                local = fixture.d.jobs.get(scope.rsplit(':', 1)[1])
                native = native_jobs.add(local['payload'][0]['payload'], 0, '', persona['id'])
                native_jobs.claim(native['id'], local['snapshot'])
                native_jobs.update(native['id'], session=session)
                mapping.update(job=native['id'], session=session)
                return bind_session(ROOT, role, session, child_scope(local['chat'], '', native['id']), sender)
            def native_context(root, session, text):
                fixture.contexts[session] = text
                return set_group_context(ROOT, session, text)
            patches = [patch('runtime_adapter.DSHRuntimeAdapter.connect', return_value=client),
                       patch('tool_host.bind_session', side_effect=native_bind),
                       patch('tool_host.set_group_context', side_effect=native_context)]
            for p in patches: p.start()
            prefix = 'p0-' + str(time.time_ns()) + '-'
            a = fixture.payload('这是有点记恨上我了？', prefix+'a')
            b = fixture.payload('给我出一道leetcode题', prefix+'b', sender='444444444')
            previous = []
            if index == 0:
                previous, current = [a], b
            elif index == 1:
                a = fixture.payload('这道双指针题为什么能在线性时间完成？', prefix+'a')
                b = fixture.payload('两个边界都只向右移动', prefix+'b', sender='444444444');b['reply_to']=a['message_id']
                current = fixture.payload('沿着他们这道题解释，为什么每个指针只走一遍？', prefix+'c', sender='555555555')
                unrelated = fixture.payload('给猫买了猫粮 UNRELATED_CAT_CANARY', prefix+'unrelated',sender='666666666')
                previous = [unrelated,a,b]  # C joins A/B without replaying the unrelated body.
            elif index == 2:
                current = fixture.payload('max_active_hour == 24', prefix+'c')
                current['segments'] = [{'type':'at','user_id':'20001'}, {'type':'text','text':' '}, {'type':'text','text':'max_active_hour == 24'}]
            elif index in (3, 4, 5):
                # Read a real old receipt; duplicate only its public projection in
                # this fixture's existing owner-control store, never private input.
                source = outbound_source(ROOT, 'group:1043230169', '680545992')
                if not source: raise RuntimeError('历史受托发送回执不可用')
                with database(fixture.root/'data/social/owner-control.db') as db:
                    db.execute('CREATE TABLE IF NOT EXISTS sends(id TEXT PRIMARY KEY,target TEXT,text TEXT,state TEXT,receipt TEXT,created REAL)')
                    receipt={'ok':True,'payload':{'chat_id':fixture.chat,'message_id':'public-fixture','sent_at':time.time()-1}}
                    public_text = '大家晚安' if index == 5 else source['text']
                    db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?)',('owner-send:PRIVATE_REQUEST_CANARY:call',fixture.chat,public_text,
                        'SUCCEEDED',json.dumps(receipt),time.time()))
                current=fixture.payload('你刚才那句是自己想说的吗，还是主人让你代发的？',prefix+'d')
                if index == 3: current['reply_to']='public-fixture'
                if index == 5:
                    from channels.fish_qq.spool import Outbox
                    state=Path(read_json(fixture.root/'channels/fish_qq/config.json').get('spool_dir','channel-state'))
                    box=Outbox(state if state.is_absolute() else fixture.root/state)
                    row=box.add('send_message',{'chat_id':fixture.chat,'segments':[{'type':'text','text':'算法那道题我想明白了'}]},origin_kind='group_agent')
                    box.set_status(row['id'],'confirmed',receipt={'chat_id':fixture.chat,'message_id':'agent-y'})
                    current=fixture.payload('你刚才说的“大家晚安”是谁决定发的？另外“算法那道题我想明白了”也是同一个来源吗？分别说清楚。',prefix+'e')
            else:
                previous=[fixture.payload('你觉得今晚打游戏怎么样',prefix+'a')]
                current=fixture.payload('你觉得今晚看电影怎么样',prefix+'b',sender='444444444')
            for payload in [*previous,current]: fixture.d.history.observe(payload)
            job=fixture.d.jobs.add(current,0,fixture.d.window,fixture.d.revision)
            started=time.monotonic()
            try:
                await asyncio.wait_for(fixture.d.run_one(job['id']),150)
            finally:
                for p in reversed(patches):p.stop()
            completed=fixture.d.jobs.get(job['id'])
            reply=completed['response']
            checks = [bool(reply) and '记恨' not in reply,
                      bool(reply) and any(w in reply for w in ('指针','边界','线性','遍')),
                      bool(reply) and '24' in reply and not any(w in reply for w in ('正文为空','没有附加文字','内容没跟上','这条消息到我这儿是空')),
                      bool(reply) and any(w in reply for w in ('主人','代发','受托')),
                      bool(reply) and any(w in reply for w in ('主人','代发','受托')),
                      bool(reply) and all(w in reply for w in ('晚安','算法','主人')) and any(w in reply for w in ('群','聊天任务')),
                      bool(reply) and '电影' in reply and '打游戏' not in reply]
            capture=RequestInspector(ROOT).latest('qq',mapping.get('session',''))
            captured_text=json.dumps(capture.get('payload',{}),ensure_ascii=False)
            input_checks={'captured':capture.get('captured') is True, 'private_request_excluded':'PRIVATE_REQUEST_CANARY' not in captured_text}
            if index == 1:
                input_checks.update(shared_history_checks(a,b,completed['snapshot']['request_view'],capture))
                input_checks['unrelated_body_excluded']='UNRELATED_CAT_CANARY' not in captured_text
            if index in (4,5): input_checks['unquoted_candidates_visible']='recent_outbound_candidates' in captured_text and 'owner_delegated' in captured_text
            if index == 5:
                candidates=completed['snapshot']['request_view']['current_request'].get('recent_outbound_candidates',[])
                input_checks['exact_two_origins_projected']={c['message_id']:c['origin_kind'] for c in candidates}=={'public-fixture':'owner_delegated','agent-y':'group_agent'}
                input_checks['two_receipt_ids_visible']='agent-y' in captured_text and 'public-fixture' in captured_text
            if index == 6: input_checks['unrelated_body_excluded']='打游戏' not in captured_text
            entry={'case':label,'status':completed['state'],'pass':checks[index] and completed['state']=='sent',
                'seconds':round(time.monotonic()-started,2),'reply':reply,'error':completed['error'],
                'inbound_kind':'synthetic','inbound_message_id':current['message_id'],
                'native_job_id':mapping.get('job'),'native_session_id':mapping.get('session'),
                'selected_context':completed['snapshot'].get('request_view'),
                'actual_request_capture':capture,'input_checks':input_checks,'qq_outbound_message_id':None,
                'transport_note':'in-memory transport only; no QQ delivery claimed'}
            entry['pass'] = entry['pass'] and all(input_checks.values())
            result['cases']=[c for c in result['cases'] if c['case']!=label]
            result['cases'].append(entry)
            output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(label,'PASS' if entry['pass'] else 'FAIL',reply,completed['error'],flush=True)
            if mapping.get('job'):
                native_jobs.update(mapping['job'],state='evaluated_not_sent',response=reply,
                    snapshot=completed['snapshot'],error='P0 synthetic replay; no QQ send')
        finally:
            fixture.doCleanups();fixture.tearDown()


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--case',action='append',default=[])
    args=parser.parse_args();output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    asyncio.run(run(output,args.case))
