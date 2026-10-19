"""Positive checks of persona reload and real owner management adapters."""
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harness'),str(ROOT/'tools')]
import dependencies
from test_social_base import Fixture
from persona_library import PersonaLibrary
from tool_host import ToolHost
from group_policy import GroupPolicy
from social_context import SocialContext


class WorkbenchTests(Fixture):
    def test_import_then_reload_keeps_native_history_session(self):
        library=PersonaLibrary(self.root)
        first=library.import_text('{"persona_id":"A","system_prompt":"原始风格"}')[0]
        second=library.import_text('[bot]\nnickname="B"\n[personality]\npersonality="新的风格"')[0]
        result=library.apply(first['id']);store=SocialContext(self.root)
        store.bind_runtime(result['space'],'native-history-session')
        changed=library.apply(second['id'])
        self.assertEqual(result['space'],changed['space'])
        self.assertEqual(store.owner_space(self.owner)['session'],'native-history-session')
        self.assertEqual(store.owner_space(self.owner)['persona']['body'],'personality：新的风格')

    def test_verified_group_owner_changes_actual_group_configuration(self):
        host=ToolHost(self.root);host.bind('qq','owner-group','chat:'+self.chat,self.owner)
        identity=host.identity('qq','owner-group');self.assertTrue(identity['owner'])
        host.execute('qq','owner-group','fish_owner_admin',{'action':'group_set','params':{'group_id':self.group,'settings':{'debounce_s':1,'reply_limits':False}}},'management')
        self.assertEqual(GroupPolicy(self.root).config(self.chat)['debounce_s'],1)
        self.assertFalse(GroupPolicy(self.root).config(self.chat)['reply_limits'])

    def test_owner_private_gets_native_computer_in_existing_history_session(self):
        from owner_computer import context
        from governance import Governance
        store=SocialContext(self.root)
        space=store.create_space('qq',origin='owner-qq');store.select_owner_space(space['id'],self.owner)
        host=ToolHost(self.root);host.bind('qq','same-private-history',space['scope'],self.owner)
        identity=host.identity('qq','same-private-history')
        work=context(self.root,identity)
        self.assertEqual(work['permission'],'danger-full-access')
        self.assertTrue(Path(work['workspace']).is_dir())
        self.assertEqual(store.owner_space(self.owner)['session'],'same-private-history')
        host.bind('qq','owner-in-group','chat:'+self.chat,self.owner)
        with self.assertRaises(PermissionError):context(self.root,host.identity('qq','owner-in-group'))
        policy=Governance(self.root)
        policy.save({'owner_permissions':[p for p in policy.config()['owner_permissions'] if p!='computer.work']})
        with self.assertRaises(PermissionError):context(self.root,identity)


if __name__=='__main__':unittest.main()
