"""Critical boundaries: participant indexing, internal-state privacy and learning."""
import json
import math
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'harness')]
import dependencies
from fish_desktop.bootstrap import initialize
from storage import read_json,write_json
from social_roster import SocialRoster,RosterError
from social_members import reply_context,active_admitted
from memory_store import MemoryStore
from group_policy import GroupPolicy

GROUP='987654321';A='111111111';B='222222222';C='333333333'


class MemberIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=initialize(Path(self.tmp.name)/'root')
        cfg=read_json(self.root/'channels/fish_qq/config.json');cfg['allowlist'].update(owner_id=A,group_ids=[GROUP]);write_json(self.root/'channels/fish_qq/config.json',cfg)
        self.roster=SocialRoster(self.root);self.roster.configure(GROUP)
        self.roster.sync_members(GROUP,[{'user_id':uid,'nickname':uid,'card':''} for uid in (A,B,C)])
    def tearDown(self):self.tmp.cleanup()

    def test_selected_participants_and_internal_projection(self):
        for uid,score,prefs in ((A,89,'A_PUBLIC'),(B,20,'B_PUBLIC'),(C,90,'C_PRIVATE_UNRELATED')):
            self.roster.save_member(GROUP,uid,affinity=score,locked=False,preferences=prefs,aliases=[])
        m=MemoryStore(self.root,'qq');scope='chat:group:'+GROUP
        m.save('B_FACT',scope=scope,subject='qq:'+B)
        m.save('C_UNRELATED_FACT',scope=scope,subject='qq:'+C)
        payload={'chat_id':'group:'+GROUP,'sender':{'id':A},'reply_to':'quoted','segments':[{'type':'at','user_id':'999999999'}]}
        context,behavior=reply_context(self.root,payload,{'messages':[{'message_id':'quoted','sender':{'id':B}}]},'999999999')
        text=json.dumps(context)
        self.assertIn('A_PUBLIC',text);self.assertIn('B_FACT',text)
        self.assertNotIn('C_UNRELATED',text);self.assertNotIn('C_PRIVATE',text)
        for key in ('affinity','delta','locked','active_probability','cooldown_multiplier','events'):
            self.assertNotIn(key,context);self.assertNotIn('"'+key+'"',text)
        self.assertEqual(context['max_chars'],480);self.assertEqual(behavior['active_probability'],.8)

    def test_behavior_changes_do_not_bypass_group_limits(self):
        lows=self.roster.behavior(GROUP,A)
        self.roster.save_member(GROUP,A,affinity=90,locked=False,preferences='',aliases=[])
        highs=self.roster.behavior(GROUP,A)
        self.assertGreater(highs['max_chars'],lows['max_chars'])
        admitted_low=sum(active_admitted('group:'+GROUP,i,lows,0,60,1000) for i in range(1000))
        admitted_high=sum(active_admitted('group:'+GROUP,i,highs,0,60,1000) for i in range(1000))
        self.assertGreater(admitted_high,admitted_low)
        self.assertFalse(active_admitted('group:'+GROUP,1,highs,990,60,1000))
        self.assertFalse(GroupPolicy(self.root).config('group:'+GROUP)['reply'])

    def test_automatic_event_piggybacks_candidate_learning(self):
        policy=GroupPolicy(self.root);policy.configure('group:'+GROUP,{'listen':True,'learn':True})
        m=MemoryStore(self.root,'qq');m.enqueue('qq-group:group:'+GROUP+':message1','test message','chat:group:'+GROUP,subject='qq:'+A)
        gateway=Mock();gateway.json.return_value={'memories':[{'text':'fact','confidence':.9,'evidence':'self_report','kind':'preference'}],'relationship_event':'warm'}
        self.assertTrue(m.learn_one(gateway));self.assertEqual(self.roster.member(GROUP,A)['affinity'],31)
        self.assertEqual(len(m.candidates('chat:group:'+GROUP)),1)
        supplied=str(gateway.json.call_args)
        self.assertNotIn('"affinity"',supplied);self.assertNotIn('31',supplied)

    def test_learning_switch_off_in_flight_prevents_both_writes(self):
        policy=GroupPolicy(self.root);policy.configure('group:'+GROUP,{'listen':True,'learn':True})
        m=MemoryStore(self.root,'qq');m.enqueue('qq-group:group:'+GROUP+':message2','test','chat:group:'+GROUP,subject='qq:'+A)
        def change(*args):
            policy.configure('group:'+GROUP,{'learn':False})
            return {'memories':[],'relationship_event':'helpful'}
        m.learn_one(Mock(json=change));self.assertEqual(self.roster.member(GROUP,A)['affinity'],30)

    def test_absent_member_and_nonfinite_time_cannot_update(self):
        self.roster.sync_members(GROUP,[{'user_id':B}])
        self.assertFalse(self.roster.apply_event(GROUP,A,event_id='left',kind='helpful',message_id='m')['applied'])
        for value in (math.inf,-math.inf,math.nan,-1):
            with self.assertRaises(RosterError):self.roster.apply_event(GROUP,B,event_id='invalid',kind='helpful',message_id='m',now=value)

    def test_group_context_route_is_bound_and_admin_writes_only(self):
        from tool_host import ToolHost
        host=ToolHost(self.root);host.bind('qq','bound','chat:group:'+GROUP,A)
        host.dispatch('/social/context/set',{'session':'bound','text':'PUBLIC'},'qq',True)
        self.assertEqual(host.dispatch('/social/context',{'session':'bound'},'qq',False),{'text':'PUBLIC'})
        with self.assertRaises(PermissionError):host.dispatch('/social/context/set',{'session':'bound','text':'injected'},'qq',False)
        with self.assertRaises(PermissionError):host.dispatch('/social/context',{'session':'bound'},'research',False)
        with self.assertRaises(ValueError):host.dispatch('/social/roster',{},'qq',False)

    def test_concurrent_event_updates_are_atomic(self):
        from concurrent.futures import ThreadPoolExecutor
        def apply(i):return self.roster.apply_event(GROUP,A,event_id='parallel-'+str(i),kind='helpful',message_id=str(i),now=10000)
        with ThreadPoolExecutor(max_workers=5) as pool:results=list(pool.map(apply,range(10)))
        self.assertEqual(sum(r['delta'] for r in results),2)
        self.assertEqual(self.roster.member(GROUP,A)['affinity'],32)


if __name__=='__main__':unittest.main()
