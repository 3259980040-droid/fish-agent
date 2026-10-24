"""Shadow engine guard rails: engine choice, read authorisation, daily budget, one run per chat, bounded wait."""
import asyncio
import json
import sqlite3
import threading
import time
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from test_social_base import Fixture
from governance import Governance
from group_policy import GroupPolicy
from group_tests import GroupTests
from social_context import SocialContext
from storage import database
import group_decide
import qq_agent_loop
from qq_agent_loop import QQAgentLoop


class EngineTests(Fixture):
    def setUp(self):
        super().setUp()
        persona = SocialContext(self.root).save_persona('qq', '影子人格', '群里的同一个人格')
        self.revision = persona['id']
        GroupTests(self.root).create(self.group, self.revision, self.owner)
        # The owner has authorised reading this group, which is the state every
        # engine guard rail below is meant to be tested in.
        GroupPolicy(self.root).configure(self.chat, {'read': True})
        self.addCleanup(group_decide._release, self.chat)

    def usage(self):
        """The day's shadow allowance counters, read the way the ledger stores them."""
        path = self.root / 'data/social/group-shadow.db'
        if not path.is_file():
            return []
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            try:
                return [int(row[0]) for row in db.execute('SELECT calls FROM shadow_usage')]
            except sqlite3.Error:
                return []

    def audit_reasons(self, kind):
        path = self.root / 'data/qq/governance.db'
        if not path.is_file():
            return []
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            try:
                return [str(row[0]) for row in db.execute('SELECT reason FROM audit WHERE kind=?', (kind,))]
            except sqlite3.Error:
                return []

    def payload(self, text='现在这句', mid='engine-1'):
        return {'chat_type': 'group', 'chat_id': self.chat, 'message_id': mid,
                'sender': {'id': '333333333', 'display_name': '群友甲'},
                'reply_to': '', 'mentions_bot': False,
                'segments': [{'type': 'text', 'text': text}]}

    # 1
    def test_configure_accepts_fish_and_validates_the_daily_budget(self):
        policy = GroupPolicy(self.root)
        self.assertEqual(policy.config(self.chat)['engine'], 'dsh')
        for engine in ('dsh', 'shadow', 'fish'):
            self.assertEqual(policy.configure(self.chat, {'engine': engine})['engine'], engine)
            self.assertEqual(policy.config(self.chat)['engine'], engine)
        with self.assertRaises(ValueError):
            policy.configure(self.chat, {'engine': 'model'})
        self.assertEqual(policy.config(self.chat)['engine'], 'fish')
        self.assertEqual(policy.config(self.chat)['shadow_daily_limit'], 60)
        for limit in (0, 1, 500):
            self.assertEqual(policy.configure(self.chat, {'shadow_daily_limit': limit})['shadow_daily_limit'], limit)
        for limit in (-1, 501, 2.5, True, '60'):
            with self.assertRaises(ValueError):
                policy.configure(self.chat, {'shadow_daily_limit': limit})
        self.assertEqual(policy.config(self.chat)['shadow_daily_limit'], 500)

    # 2
    def test_a_stored_fish_config_reads_as_the_fish_engine(self):
        policy = GroupPolicy(self.root)
        policy.configure(self.chat, {'engine': 'shadow'})
        with database(policy.path) as db:
            db.execute('UPDATE groups SET config=? WHERE chat=?', (json.dumps({'engine': 'fish'}), self.chat))
        self.assertEqual(policy.config(self.chat)['engine'], 'fish')
        plan = group_decide.shadow_ready(self.root, self.chat)
        self.assertEqual(plan['engine'], 'fish')
        self.assertEqual(group_decide.group_engine(self.root, self.chat), 'fish')
        self.assertEqual(group_decide.shadow_ready(self.root, self.chat)['chat'], self.chat)
        # An unknown stored engine still reads as the old dsh path.
        with database(policy.path) as db:
            db.execute('UPDATE groups SET config=? WHERE chat=?', (json.dumps({'engine': 'model'}), self.chat))
        self.assertEqual(group_decide.group_engine(self.root, self.chat), 'dsh')

    # 3
    def test_a_fish_group_can_be_reconfigured_without_losing_its_engine(self):
        policy = GroupPolicy(self.root)
        policy.configure(self.chat, {'engine': 'shadow', 'listen': True})
        with database(policy.path) as db:
            db.execute('UPDATE groups SET config=? WHERE chat=?',
                       (json.dumps({'engine': 'fish', 'listen': True}), self.chat))
        self.assertEqual(policy.config(self.chat)['engine'], 'fish')
        # Turning reply off on a group that stores fish must not be blocked by it,
        # and must not silently rewrite the chosen engine either.
        healed = policy.configure(self.chat, {'listen': False})
        self.assertEqual(healed['engine'], 'fish')
        self.assertIs(False, healed['listen'])
        self.assertEqual(policy.configure(self.chat, {'engine': 'fish'})['engine'], 'fish')
        self.assertEqual(policy.config(self.chat)['engine'], 'fish')

    # 4
    def test_daily_limit_skips_without_a_model_call(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow', 'shadow_daily_limit': 1})
        respond = Mock(return_value='第一份候选')
        with patch('fish_chat.respond', respond):
            first = group_decide.respond_as_group(self.root, self.chat, payload=self.payload())
            second = group_decide.respond_as_group(self.root, self.chat, payload=self.payload('第二条', 'engine-2'))
        self.assertEqual(respond.call_count, 1)
        self.assertEqual(first['text'], '第一份候选')
        self.assertNotIn('skipped', first)
        self.assertEqual(second['skipped'], 'daily-limit')
        self.assertEqual((second['text'], second['tools']), ('', []))
        group_decide.record_shadow(self.root, self.chat, payload=self.payload(), candidate=second)
        self.assertEqual(group_decide.shadows(self.root, self.chat), [])

    # 4
    def test_zero_budget_turns_the_shadow_off(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow', 'shadow_daily_limit': 0})
        respond = Mock(return_value='不该被调用')
        with patch('fish_chat.respond', respond):
            result = group_decide.respond_as_group(self.root, self.chat, payload=self.payload())
        self.assertEqual(respond.call_count, 0)
        self.assertEqual(result['skipped'], 'daily-limit')

    # 5
    def test_second_shadow_for_the_same_chat_is_busy(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow'})
        entered, release = threading.Event(), threading.Event()
        outcomes = []

        def slow(*_args, **_kwargs):
            entered.set()
            release.wait(10)
            return '慢影子'

        def first_run():
            try:
                outcomes.append(group_decide.respond_as_group(self.root, self.chat, payload=self.payload()))
            finally:
                release.set()

        with patch('fish_chat.respond', Mock(side_effect=slow)):
            worker = threading.Thread(target=first_run, daemon=True)
            worker.start()
            self.assertTrue(entered.wait(10))
            second = group_decide.respond_as_group(self.root, self.chat, payload=self.payload('第二条', 'engine-2'))
            release.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(second['skipped'], 'busy')
        self.assertEqual(second['text'], '')
        self.assertEqual(outcomes[0]['text'], '慢影子')

    # 6
    def test_paused_governance_skips_the_shadow(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow'})
        Governance(self.root, 'qq').save({'paused': True})
        respond = Mock(return_value='不该被调用')
        with patch('fish_chat.respond', respond):
            result = group_decide.respond_as_group(self.root, self.chat, payload=self.payload())
        self.assertEqual(respond.call_count, 0)
        self.assertEqual(result['skipped'], 'paused')

    # 7
    def test_a_group_without_read_authorisation_runs_no_shadow_and_pays_nothing(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow', 'read': False})
        respond = Mock(return_value='不该被调用')
        with patch('fish_chat.respond', respond):
            refused = group_decide.respond_as_group(self.root, self.chat, payload=self.payload())
        self.assertEqual(respond.call_count, 0)
        self.assertEqual(refused['skipped'], 'no-read')
        self.assertEqual((refused['text'], refused['tools']), ('', []))
        self.assertEqual([], self.usage())
        self.assertTrue(any('no-read' in reason for reason in
                            self.audit_reasons('group_shadow_skipped')))
        # The other half of the same authorisation: a group the owner has never
        # allowlisted is refused even while its stored read switch says true.
        config = json.loads((self.root / 'channels/fish_qq/config.json').read_text(encoding='utf-8'))
        config['allowlist']['group_ids'] = []
        (self.root / 'channels/fish_qq/config.json').write_text(
            json.dumps(config, ensure_ascii=False), encoding='utf-8')
        GroupPolicy(self.root).configure(self.chat, {'read': True})
        with patch('fish_chat.respond', respond):
            unlisted = group_decide.respond_as_group(self.root, self.chat, payload=self.payload())
        self.assertEqual(respond.call_count, 0)
        self.assertEqual(unlisted['skipped'], 'no-read')
        self.assertEqual([], self.usage())
        # Restoring the authorisation restores the shadow, and now it pays once.
        config['allowlist']['group_ids'] = [self.group]
        (self.root / 'channels/fish_qq/config.json').write_text(
            json.dumps(config, ensure_ascii=False), encoding='utf-8')
        with patch('fish_chat.respond', Mock(return_value='恢复后的候选')):
            healed = group_decide.respond_as_group(self.root, self.chat, payload=self.payload())
        self.assertNotIn('skipped', healed)
        self.assertEqual(healed['text'], '恢复后的候选')
        self.assertEqual([1], self.usage())

    # 8
    def test_group_handler_bounds_its_wait_for_the_shadow(self):
        GroupPolicy(self.root).configure(self.chat, {'engine': 'shadow'})
        loop = QQAgentLoop(self.root)
        loop.channel = Mock(bot={'id': '20001'},
                            action=AsyncMock(return_value={'ok': True, 'payload': {'message_id': 'out'}}))
        observed = AsyncMock()

        def slow(*_args, **_kwargs):
            time.sleep(0.3)
            return {'engine': 'shadow', 'text': '太慢的候选', 'tools': []}

        async def run():
            with patch('group_dialogue.GroupDialogue.observe', observed), \
                    patch.object(group_decide, 'respond_as_group', slow), \
                    patch.object(qq_agent_loop, 'SHADOW_TIMEOUT_S', 0.05), \
                    patch.object(loop.governance, 'audit') as audit:
                await loop._on_group(self.payload())
                return audit
        audit = asyncio.run(run())
        self.assertEqual(observed.await_count, 1)
        kinds = [call.args[0] for call in audit.call_args_list]
        self.assertIn('group_shadow_timeout', kinds)
        self.assertEqual(loop.channel.action.await_count, 0)
        self.assertEqual(group_decide.shadows(self.root, self.chat), [])

    # -- P0-A: switching the engine is a rollback of the permission, not a setting the
    #    next send happens to read. These hold a reservation the way a generating reply
    #    does and ask live() whether that reply may still speak.
    def hold(self, policy, token, cursor=1, kind='active'):
        """One active reservation, as qq_agent_loop holds it while it generates."""
        with database(policy.path) as db:
            db.execute('INSERT INTO reservations(token,chat,cursor,kind,state,created) '
                       'VALUES(?,?,?,?,?,?)', (token, self.chat, cursor, kind, 'active', time.time()))
        return token

    def stored_state(self, policy, token):
        with database(policy.path) as db:
            row = db.execute('SELECT state FROM reservations WHERE token=?', (token,)).fetchone()
        return row['state'] if row else None

    # 9
    def test_switching_the_engine_retires_the_reply_that_is_already_generating(self):
        policy = GroupPolicy(self.root)
        policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True})
        token = self.hold(policy, 'held-fish')
        self.assertTrue(policy.live(token))          # the state the reproduction found reachable
        policy.configure(self.chat, {'engine': 'dsh'})
        self.assertFalse(policy.live(token))         # one click back to dsh, immediately silent
        self.assertEqual(self.stored_state(policy, token), 'disabled')
        # Coming back to fish cannot revive the withdrawn reservation: it is spent.
        policy.configure(self.chat, {'engine': 'fish'})
        self.assertFalse(policy.live(token))
        self.assertEqual(self.stored_state(policy, token), 'disabled')

    # 10
    def test_live_also_requires_listen_and_reply_and_any_engine_but_fish_is_false(self):
        policy = GroupPolicy(self.root)
        policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True})
        first = self.hold(policy, 'held-reply')
        self.assertTrue(policy.live(first))
        policy.configure(self.chat, {'reply': False})
        self.assertFalse(policy.live(first))
        second = self.hold(policy, 'held-listen', cursor=2)
        policy.configure(self.chat, {'listen': False})
        self.assertFalse(policy.live(second))
        self.assertEqual([self.stored_state(policy, t) for t in (first, second)], ['disabled', 'disabled'])
        # A chat the owner never configured reads DEFAULTS (engine dsh, listen and
        # reply off), so a reservation there has no authorisation either.
        untouched = policy.config('group:987654322')
        self.assertEqual((untouched['engine'], untouched['listen'], untouched['reply']),
                         ('dsh', False, False))

    # 11
    def test_an_unrelated_reconfigure_leaves_a_generating_reply_alone(self):
        policy = GroupPolicy(self.root)
        policy.configure(self.chat, {'engine': 'fish', 'listen': True, 'reply': True})
        token = self.hold(policy, 'held-keep')
        policy.configure(self.chat, {'debounce_s': 2})
        self.assertTrue(policy.live(token))          # nothing was withdrawn
        policy.configure(self.chat, {'engine': 'fish', 'reply': True, 'listen': True})
        self.assertTrue(policy.live(token))          # repeating the same engine is not a switch
        self.assertEqual(self.stored_state(policy, token), 'active')

    # 12
    def test_the_fish_judge_budget_is_validated_beside_the_shadow_one(self):
        policy = GroupPolicy(self.root)
        self.assertEqual(policy.config(self.chat)['fish_judge_daily_limit'], 80)
        for limit in (0, 1, 500):
            self.assertEqual(policy.configure(self.chat, {'fish_judge_daily_limit': limit})
                             ['fish_judge_daily_limit'], limit)
        for limit in (-1, 501, 2.5, True, '80', None):
            with self.assertRaises(ValueError) as caught:
                policy.configure(self.chat, {'fish_judge_daily_limit': limit})
            self.assertRegex(str(caught.exception), r'[\u4e00-\u9fff]')  # 文案是中文
        self.assertEqual(policy.config(self.chat)['fish_judge_daily_limit'], 500)
        # A key the defaults do not know is still refused, budget or not.
        with self.assertRaises(ValueError):
            policy.configure(self.chat, {'fish_judge_daily_limit': 10, 'worth_speaking': True})
        self.assertEqual(policy.config(self.chat)['fish_judge_daily_limit'], 500)


if __name__ == '__main__':
    unittest.main()
