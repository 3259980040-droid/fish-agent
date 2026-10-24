"""Focused attribution checks with account collisions, renames and old quotes."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from group_history import GroupHistory, user_message, summary_sources, attributed_summary
from group_tasks import GroupTaskStore
from group_policy import GroupPolicy
from social_members import reply_context
from social_roster import SocialRoster
from social_context import SocialContext
from storage import database
from channels.fish_qq.normalize import normalize_message


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'channels/fish_qq').mkdir(parents=True)
        (self.root / 'channels/fish_qq/config.json').write_text(
            json.dumps({'allowlist': {'owner_id': '111111', 'group_ids': ['123456']}}), encoding='utf-8')
        self.roster = SocialRoster(self.root)
        self.roster.configure('123456')
        self.jobs = GroupTaskStore(self.root)

    def payload(self, mid, uid, name, text, reply=''):
        return {'chat_id': 'group:123456', 'message_id': mid, 'sender': {'id': uid, 'display_name': name, 'nickname': name, 'card': ''},
                'segments': [{'type': 'text', 'text': text}], 'reply_to': reply}

    def make_history(self):
        # This isolated window has no legacy messages to import.
        with database(self.jobs.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS dialogue_imports(scope TEXT PRIMARY KEY)')
            db.execute('INSERT INTO dialogue_imports VALUES(?)', (json.dumps(['group:123456','a'*32,'history-v2'],separators=(',',':')),))
        self.history = GroupHistory(self.root, self.jobs, Mock(), 'group:123456', 'a'*32, 'r1')
        return self.history

    def test_same_name_accounts_and_rename_keep_separate_profiles(self):
        a = self.payload('a', '111111', '小明', '我喜欢乌龙茶')
        b = self.payload('b', '222222', '小明', '我喜欢可乐')
        for p in (a, b): self.roster.observe_sender('123456', p['sender'])
        people, _ = reply_context(self.root, b, {'messages': [a, b]}, '999999')
        self.assertEqual(people['ambiguous_names']['小明'], ['111111', '222222'])
        renamed = self.payload('c', '111111', '阿茶', '我改名了')
        self.roster.observe_sender('123456', renamed['sender'])
        self.assertEqual(self.roster.public_profile('123456','111111')['nickname'], '阿茶')
        self.assertEqual(self.roster.public_profile('123456','222222')['nickname'], '小明')

    def test_old_bot_quote_retains_original_recipient_and_current_questioner(self):
        history = self.make_history()
        original = self.payload('q-a', '111111', '小明', '记住我的茶')
        history.observe(original)
        job = self.jobs.add(original, 0, 'a'*32, 'r1')
        self.jobs.update(job['id'], state='sent', remote='bot-a', response='记住了你的乌龙茶')
        history.delivered({**job,'remote':'bot-a'}, '记住了你的乌龙茶')
        b = self.payload('q-b', '222222', '小明', '你刚才是在回谁？', 'bot-a')
        ref = history.reference('bot-a','999999')
        people, _ = reply_context(self.root, b, {'messages': []}, '999999', ref)
        self.assertEqual(people['reply_target'], '222222')
        self.assertEqual(people['quoted_message']['reply_target_id'], '111111')
        self.assertEqual(people['quoted_message']['author']['id'], '999999')
        self.assertEqual(history.reference('q-a')['author']['id'], '111111')
        with database(history.path) as db:
            row=json.loads(db.execute("SELECT message FROM dialogue_messages WHERE source=?",('out:'+job['id'],)).fetchone()[0])
        self.assertEqual(row['fish_reply']['message_id'], 'bot-a')

    def test_transport_and_snapshot_preserve_names_and_at_target(self):
        raw={'post_type':'message','message_type':'group','group_id':123456,'user_id':111111,'message_id':123,
             'sender':{'user_id':111111,'nickname':'昵称','card':'群名片'},
             'message':[{'type':'at','data':{'qq':'222222'}},{'type':'text','data':{'text':'他说的'}}]}
        p=normalize_message(raw,'999999')
        self.assertEqual(p['sender']['nickname'],'昵称')
        self.assertEqual(p['sender']['card'],'群名片')
        policy=GroupPolicy(self.root);policy.configure('group:123456',{'listen':True});policy.observe(p)
        snap=policy.snapshot('group:123456')
        self.assertEqual(snap['messages'][0]['segments'][0],{'type':'at','user_id':'222222'})
        self.assertEqual(snap['messages'][0]['sender']['card'],'群名片')

    def test_legacy_summary_replays_retained_sources_instead_of_old_name_merge(self):
        history=self.make_history()
        history.observe(self.payload('old','111111','小明','我喜欢乌龙茶'))
        current=self.payload('new','222222','小明','刚才谁喜欢乌龙茶？')
        history.observe(current)
        with database(history.path) as db:
            db.execute('INSERT INTO dialogue_summaries VALUES(?,?,?,?)',(history.key,999,'两个小明是同一个人，喜欢可乐',0))
        messages,_=history.pending_history({'payload':[{'payload':current}],'sender':'222222'})
        text=json.dumps(messages,ensure_ascii=False)
        self.assertIn('我喜欢乌龙茶',text)
        self.assertNotIn('两个小明是同一个人',text)

    def test_focus_excludes_unrelated_question_but_keeps_original(self):
        history=self.make_history()
        a=self.payload('a','111111','甲','这是有点记恨上我了？')
        b=self.payload('b','222222','乙','给我出一道leetcode题')
        for p in (a,b):history.observe(p)
        job=self.jobs.add(b,0,'a'*32,'r1')
        messages,_=history.pending_history(job)
        self.assertNotIn('记恨',json.dumps(messages,ensure_ascii=False))
        view=job['snapshot']['request_view']
        self.assertEqual(view['current_request']['focus_sender_id'],'222222')
        self.assertEqual(view['excluded'][0]['message_id'],'a')
        self.assertEqual(history.reference('a')['text'],'这是有点记恨上我了？')

    def test_reply_chain_connects_three_different_people(self):
        history=self.make_history()
        a=self.payload('a','111111','甲','这道题怎么做双指针？')
        b=self.payload('b','222222','乙','窗口左边界单调移动','a')
        c=self.payload('c','333333','丙','沿着他们这道题解释，为什么每个指针只走一遍？')
        for p in (a,b,c):history.observe(p)
        job=self.jobs.add(c,0,'a'*32,'r1')
        messages,_=history.pending_history(job)
        text=json.dumps(messages,ensure_ascii=False)
        self.assertIn('双指针',text);self.assertIn('左边界',text)
        self.assertEqual({r['message_id'] for r in job['snapshot']['request_view']['related_history']},{'a','b'})

    def test_shared_followup_phrases_require_recent_uninterrupted_reply_chain(self):
        from group_history import focus_projection
        a=self.payload('a','111111','甲','这道双指针题为什么能在线性时间完成？')
        b=self.payload('b','222222','乙','两个边界都只向右移动','a')
        unrelated=self.payload('other','444444','丁','我家猫今天又爬窗帘了')
        def project(text, *, interrupted=False, stale=False, linked=True):
            old=[a,{**b,'reply_to':'a' if linked else ''}]
            if interrupted:old.append(unrelated)
            rows=[{'source':'in:'+p['message_id'],'created':i+(0 if stale else 1000),
                   'message':json.dumps(user_message(p),ensure_ascii=False)} for i,p in enumerate(old)]
            c=self.payload('c','333333','丙',text)
            return focus_projection({'payload':[{'payload':c}],'sender':'333333','created':1003},rows,'group:123456')[1]
        for text in ('沿着他们这道题解释，为什么每个指针只走一遍？',
                     '刚才你们说的，再解释一下？','接着上面的题讲讲','那为什么只走一遍？'):
            with self.subTest(text=text):
                self.assertEqual({r['message_id'] for r in project(text)['related_history']},{'a','b'})
        for boundary in ({'interrupted':True},{'stale':True},{'linked':False}):
            with self.subTest(boundary=boundary):
                self.assertEqual(project('沿着他们这道题再解释一下？',**boundary)['related_history'],[])

    def test_shared_temporary_role_survives_a_change_of_speaker(self):
        history=self.make_history()
        a=self.payload('role','111111','甲','你现在是侦探，来扮演破案现场。')
        b=self.payload('q','222222','乙','先介绍一下你自己')
        for p in (a,b):history.observe(p)
        job=self.jobs.add(b,0,'a'*32,'r1')
        messages,_=history.pending_history(job)
        self.assertIn('侦探',json.dumps(messages,ensure_ascii=False))
        self.assertEqual(job['snapshot']['request_view']['related_history'][0]['reason'],'window_role_context')

    def test_generic_phrases_do_not_link_different_topics(self):
        from group_history import focus_projection, user_message
        pairs = [('明天去吃火锅怎么样？','你觉得明天这个方法怎么样？'),
                 ('你觉得今晚打游戏怎么样','你觉得今晚看电影怎么样'),
                 ('一个人去旅游怎么样','你觉得一个人打工怎么样')]
        for old, current in pairs:
            with self.subTest(old=old):
                a=self.payload('a','111111','甲',old);b=self.payload('b','222222','乙',current)
                rows=[{'source':'in:a','message':json.dumps(user_message(a),ensure_ascii=False)}]
                chosen, view=focus_projection({'payload':[{'payload':b}],'sender':'222222'},rows,'group:123456')
                self.assertEqual(chosen,[])
                self.assertEqual(view['excluded'][0]['message_id'],'a')

    def test_stable_topic_connects_people_without_quote(self):
        history=self.make_history()
        a=self.payload('tea','111111','甲','乌龙茶最近涨价了')
        b=self.payload('q','222222','乙','乌龙茶的产地在哪里？')
        for p in (a,b):history.observe(p)
        job=self.jobs.add(b,0,'a'*32,'r1');messages,_=history.pending_history(job)
        self.assertIn('涨价',json.dumps(messages,ensure_ascii=False))

    def seed_owner_send(self, mid='sent-public', chat='group:123456', text='大家晚安', when=None):
        receipt={'ok':True,'payload':{'chat_id':chat,'message_id':mid,
                                    'sent_at':time.time()-1 if when is None else when}}
        with database(self.root/'data/social/owner-control.db') as db:
            db.execute('CREATE TABLE IF NOT EXISTS sends(id TEXT,target TEXT,text TEXT,state TEXT,receipt TEXT,created REAL)')
            db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?)',('owner-send:private-secret-session:call-secret',
                chat,text,'SUCCEEDED',json.dumps(receipt),time.time()-2))

    def test_unquoted_origin_uses_only_current_group_confirmed_public_receipts(self):
        history=self.make_history();self.seed_owner_send()
        self.seed_owner_send('other','group:654321','OTHER_GROUP_PRIVATE_CANARY')
        self.seed_owner_send('future',text='FUTURE_CANARY',when=time.time()+100)
        p=self.payload('q','222222','乙','你刚才那句是自己想说的吗？');history.observe(p)
        job=self.jobs.add(p,0,'a'*32,'r1');messages,_=history.pending_history(job)
        candidates=job['snapshot']['request_view']['current_request']['recent_outbound_candidates']
        self.assertEqual([(r['message_id'],r['origin_kind']) for r in candidates],[('sent-public','owner_delegated')])
        text=json.dumps(messages,ensure_ascii=False)
        self.assertIn('大家晚安',text);self.assertIn('owner_delegated',text)
        for secret in ('private-secret','call-secret','OTHER_GROUP_PRIVATE_CANARY','FUTURE_CANARY'):
            self.assertNotIn(secret,text)

    def test_unquoted_mixed_origins_remain_separate_and_exact(self):
        from channels.fish_qq.spool import Outbox
        history=self.make_history();self.seed_owner_send()
        # Same receipt in two existing stores must stay one candidate.
        box=Outbox(self.root/'channel-state')
        for mid,kind,text,chat in [('sent-public','owner_delegated','大家晚安','group:123456'),
                ('agent-y','group_agent','算法那道题我想明白了','group:123456'),
                ('sent-public','scheduled','OTHER_GROUP_SAME_ID','group:654321')]:
            row=box.add('send_message',{'chat_id':chat,'segments':[{'type':'text','text':text}]},origin_kind=kind)
            box.set_status(row['id'],'confirmed',receipt={'chat_id':chat,'message_id':mid,'sent_at':time.time()-1})
        box.add('send_message',{'chat_id':'group:123456','segments':[{'type':'text','text':'QUEUED_CANARY'}]},origin_kind='owner_delegated')
        p=self.payload('q','222222','乙','刚才你那条是谁决定发的？');history.observe(p)
        job=self.jobs.add(p,0,'a'*32,'r1');messages,_=history.pending_history(job)
        view=job['snapshot']['request_view']['current_request']
        self.assertEqual({r['message_id']:r['origin_kind'] for r in view['recent_outbound_candidates']},
                         {'sent-public':'owner_delegated','agent-y':'group_agent'})
        self.assertIn('不能',view['outbound_candidate_note'])
        for secret in ('OTHER_GROUP_SAME_ID','QUEUED_CANARY','private-secret'):
            self.assertNotIn(secret,json.dumps(messages,ensure_ascii=False))

    def test_forward_screenshot_does_not_match_local_unquoted_receipts(self):
        history=self.make_history();self.seed_owner_send()
        p=self.payload('q','222222','乙','这张转发截图里你那条是谁决定发的？');history.observe(p)
        job=self.jobs.add(p,0,'a'*32,'r1');messages,_=history.pending_history(job)
        view=job['snapshot']['request_view']['current_request']
        self.assertEqual(view['recent_outbound_candidates'],[])
        self.assertIn('截图',view['outbound_candidate_note'])
        self.assertNotIn('大家晚安',json.dumps(messages,ensure_ascii=False))

    def test_regular_question_does_not_load_recent_outbound_candidates(self):
        history=self.make_history();self.seed_owner_send()
        p=self.payload('q','222222','乙','给我出一道leetcode题');history.observe(p)
        job=self.jobs.add(p,0,'a'*32,'r1');messages,_=history.pending_history(job)
        self.assertNotIn('recent_outbound_candidates',job['snapshot']['request_view']['current_request'])
        self.assertNotIn('大家晚安',json.dumps(messages,ensure_ascii=False))

    def test_later_assistant_cannot_import_a_future_question(self):
        history=self.make_history()
        current=self.payload('q','222222','乙','乌龙茶是什么茶？');history.observe(current)
        job=self.jobs.add(current,0,'a'*32,'r1')
        future=self.payload('future','333333','丙','乌龙茶的新配方');history.observe(future)
        later=self.jobs.add(future,0,'a'*32,'r1')
        history.delivered({**later,'remote':'future-bot'},'乌龙茶 FUTURE_ANSWER_CANARY')
        messages,_=history.pending_history(job)
        self.assertNotIn('FUTURE_ANSWER_CANARY',json.dumps(messages,ensure_ascii=False))
        self.assertNotIn('新配方',json.dumps(messages,ensure_ascii=False))

    def test_earlier_answer_delivered_while_waiting_keeps_causal_label(self):
        history=self.make_history()
        first=self.payload('old','111111','甲','乌龙茶是哪种茶？');history.observe(first)
        earlier=self.jobs.add(first,0,'a'*32,'r1')
        current=self.payload('q','222222','乙','乌龙茶呢？');history.observe(current)
        job=self.jobs.add(current,0,'a'*32,'r1')
        history.delivered({**earlier,'remote':'late-bot'},'乌龙茶是半发酵茶')
        messages,_=history.pending_history(job)
        answer=next(m for m in messages if m['role']=='assistant')
        self.assertIn('半发酵茶',json.dumps(answer,ensure_ascii=False))
        self.assertEqual(answer['fish_timing']['relation'],'earlier_request_answer_delivered_while_waiting')

    def test_owner_tool_quote_projects_only_public_destination_text(self):
        history=self.make_history()
        path=self.root/'data/social/owner-control.db'
        with database(path) as db:
            db.execute('CREATE TABLE sends(id TEXT,target TEXT,text TEXT,state TEXT,receipt TEXT,created REAL)')
            receipt={'ok':True,'payload':{'chat_id':'group:123456','message_id':'sent-public'}}
            db.execute('INSERT INTO sends VALUES(?,?,?,?,?,?)',('owner-send:private-secret-session:call-secret',
                'group:123456','大家晚安','SUCCEEDED',json.dumps(receipt),1))
        p=self.payload('q','222222','乙','这是谁主动发的？','sent-public');history.observe(p)
        job=self.jobs.add(p,0,'a'*32,'r1')
        messages,_=history.pending_history(job,bot_id='999999')
        ref=history.reference('sent-public','999999')
        self.assertEqual(ref['origin_kind'],'owner_delegated')
        text=json.dumps(messages,ensure_ascii=False)
        self.assertIn('大家晚安',text);self.assertIn('owner_delegated',text)
        self.assertNotIn('private-secret',text);self.assertNotIn('call-secret',text)
        self.assertIsNone(history.reference('not-sent','999999'))

    def test_confirmed_outbox_receipt_is_exact_and_durable(self):
        from channels.fish_qq.spool import Outbox
        from send_policy import outbound_source
        box=Outbox(self.root/'channel-state')
        row=box.add('send_message',{'chat_id':'group:123456','segments':[{'type':'text','text':'定时问候'}]},origin_kind='scheduled')
        box.set_status(row['id'],'confirmed',receipt={'chat_id':'group:123456','message_id':'remote-1'})
        self.assertEqual(outbound_source(self.root,'group:123456','remote-1')['origin_kind'],'scheduled')
        self.assertIsNone(outbound_source(self.root,'group:654321','remote-1'))
        self.assertIsNone(outbound_source(self.root,'group:123456','remote-2'))

    def test_default_change_retains_original_revision_and_space_history(self):
        c=SocialContext(self.root)
        old=c.save_persona('qq','旧版','旧的口癖',global_rules='',workspace_rules='')
        space=c.create_space('qq',revision=old['id'],origin='owner-qq')
        c.bind_runtime(space['id'],'continuous-session')
        new=c.save_persona('qq','精简版','自然回应当前对话',parent=old['id'])
        c.set_default('qq',new['id']);c.change_persona(space['id'],new['id'])
        self.assertEqual(c.ensure_default('qq')['id'],new['id'])
        self.assertEqual(c.space(space['id'])['session'],'continuous-session')
        self.assertEqual(c.persona(old['id'])['body'],'旧的口癖')

    def test_summary_names_come_from_transport_and_notes_require_source_message(self):
        sources=summary_sources([user_message(self.payload('a','111111','小明','我喜欢乌龙茶')),
                                 user_message(self.payload('b','222222','小明','我喜欢可乐'))])
        text=attributed_summary(sources,[{'message_id':'a','summary':'说喜欢乌龙茶'},
                                        {'message_id':'not-real','summary':'两人是同一个账号'}])
        self.assertIn('QQ 111111',text);self.assertIn('QQ 222222',text)
        self.assertIn('消息 a 的发言摘记',text)
        self.assertNotIn('两人是同一个账号',text)


if __name__ == '__main__':
    unittest.main()
