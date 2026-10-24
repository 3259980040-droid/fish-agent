"""Send-state matrix: for one group message, who may speak - and may they retry?

Every row of the matrix is data, and every failure names its cell, so a corner of
the send path is found by reading a table instead of by re-reading the code.

What is asked, and of whom (every one of these is an existing public predicate):

* GroupPolicy.observe / reserve / live   - the new Fish sender's gates
* GroupDialogue._engine_blocks / enabled - the old DSH sender's gates
* GroupDialogue.valid(job)               - the old path's per-segment re-check
* QQAgentLoop._fish_live                 - the single sender's last question

Nothing here builds the real machinery: each cell gets a scratch root of its own,
no channel, no model, no websocket and no QQ message. GroupDialogue's two
predicates are called on a stand-in that carries the cell's real config (only
__init__ is faked, because the deployment-approval layer it also reads answers a
different question - see test_the_approval_layer_is_a_separate_permission).

The human-readable twin of this file is tools/fixtures/send_matrix.md.
"""
import contextlib
import json
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from group_dialogue import DSH_ENGINES, GroupDialogue
from group_policy import GroupPolicy
from group_tasks import GroupTaskStore
from qq_agent_loop import QQAgentLoop
from storage import database

CHAT = 'group:987654321'
ENGINES = ('dsh', 'shadow', 'fish')
RESERVATIONS = ('mine', 'other', 'disabled', 'sent', 'uncertain')


def payload(mid, mentions=True):
    """One inbound group message; mentions_bot makes it a 'direct' cursor."""
    return {'chat_id': CHAT, 'message_id': mid,
            'sender': {'id': '333333333', 'display_name': '群友甲'},
            'mentions_bot': mentions, 'reply_to_bot': False,
            'segments': [{'type': 'text', 'text': '这句怎么回'}]}


class Dialogue:
    """GroupDialogue's predicates on a stand-in, calling the real methods.

    GroupDialogue.__init__ builds a history, a task store and a send lock, none of
    which a permission question needs; what it reads is root, chat, the policy of
    its window, the approval layer and (for valid()) its own job store. Those are
    real, so _engine_blocks(), enabled() and valid() below are the shipped code.
    """

    def __init__(self, root, policy, *, allowed=True):
        self.root = Path(root)
        self.chat = CHAT
        self.revision = ''
        self.loop = types.SimpleNamespace(governance=None)
        self.workflow = types.SimpleNamespace(policy=policy, allowed=lambda *a, **k: allowed)
        self.jobs = GroupTaskStore(self.root)
        self._engine_blocks = types.MethodType(GroupDialogue._engine_blocks, self)

    def blocks(self):
        return self._engine_blocks()

    def enabled(self):
        return GroupDialogue.enabled(self)

    def valid(self, job, token=''):
        return GroupDialogue.valid(self, job, token)


class Cell:
    """One matrix cell: its own scratch root, its one group, and its stated config."""

    def __init__(self, base, *, engine='fish', listen=True, reply=True, read=True, allowed=True):
        self.root = Path(base)
        self.policy = GroupPolicy(self.root)
        # Created through the API first so the row exists; store() then writes the
        # exact config the cell states, including states configure() refuses to build.
        self.policy.configure(CHAT, {'engine': 'dsh', 'listen': True, 'reply': True, 'debounce_s': 0})
        self.state = self.store(engine=engine, listen=listen, reply=reply, read=read)
        self.dialogue = Dialogue(self.root, self.policy, allowed=allowed)
        self.tag = uuid.uuid4().hex[:8]
        self.seen = 0

    def store(self, **cfg):
        """Write the config as given, raw.

        group_policy.configure() forces reply=False whenever listen goes off, so
        the (listen=False, reply=True) cells are reachable only by writing the row.
        They are in the matrix on purpose: no predicate may trust reply alone.
        """
        state = {'engine': 'fish', 'listen': True, 'reply': True, 'read': True,
                 'debounce_s': 0, **cfg}
        with database(self.policy.path) as db:
            db.execute('UPDATE groups SET config=? WHERE chat=?', (json.dumps(state), CHAT))
        return state

    def inbound(self, mentions=True):
        self.seen += 1
        return self.policy.observe(payload('matrix:' + self.tag + ':' + str(self.seen), mentions=mentions))

    def reserve(self, cursor, kind='direct'):
        return self.policy.reserve(CHAT, cursor, kind)

    def live(self, token):
        return bool(token) and self.policy.live(token)

    def reservation_states(self):
        with database(self.policy.path) as db:
            return [row['state'] for row in db.execute('SELECT state FROM reservations WHERE chat=?', (CHAT,))]


@contextlib.contextmanager
def cell(**cfg):
    """A scratch cell that lives exactly as long as one row of the matrix."""
    with tempfile.TemporaryDirectory() as base:
        yield Cell(base, **cfg)


class AuthorityMatrix(unittest.TestCase):
    """Who may speak, over engine x listen x reply x read, with no reservation yet."""

    ROWS = [dict(engine=engine, listen=listen, reply=reply, read=read)
            for engine in ENGINES for listen in (True, False)
            for reply in (True, False) for read in (True, False)]

    @staticmethod
    def coordinate(row):
        return 'engine=%s,listen=%s,reply=%s,read=%s' % (
            row['engine'], row['listen'], row['reply'], row['read'])

    @staticmethod
    def expected(row):
        """The matrix, as one place a reader can check: gate by gate, no shortcuts."""
        listen, reply, engine = row['listen'], row['reply'], row['engine']
        return {'accepted': listen,                       # group_policy.observe: listen only
                'token': listen and reply,                # group_policy._candidate: listen and reply
                'fish_sends': engine == 'fish' and listen and reply,   # group_policy.live
                'dsh_sends': engine in DSH_ENGINES and listen and reply}  # group_dialogue.enabled

    def test_authority_matrix(self):
        for row in self.ROWS:
            tag = self.coordinate(row)
            want = self.expected(row)
            with self.subTest(tag), cell(**row) as c:
                seen = c.inbound()
                self.assertEqual(bool(seen.get('accepted')), want['accepted'], tag + ' · observe 收下了吗')
                cursor = seen.get('cursor') if seen.get('accepted') else 1
                token = c.reserve(cursor, 'direct')
                self.assertEqual(bool(token), want['token'], tag + ' · reserve 拿到 token 了吗')
                self.assertEqual(c.live(token), want['fish_sends'], tag + ' · live 允许新 Fish 发吗')
                self.assertEqual(c.dialogue.enabled(), want['dsh_sends'], tag + ' · enabled 允许旧 DSH 发吗')
                self.assertFalse(want['fish_sends'] and want['dsh_sends'], tag + ' · 一格两个发送主体')

    def test_a_reservation_is_not_a_permission_for_dsh_or_shadow(self):
        # reserve() never looks at the engine, so both old engines do hold a token
        # for a listening, replying group - and still may not send, because live()
        # is where the engine is read. This is the cell most easily misread.
        for engine in ('dsh', 'shadow'):
            tag = 'engine=' + engine
            with self.subTest(tag), cell(engine=engine) as c:
                token = c.reserve(c.inbound()['cursor'], 'direct')
                self.assertTrue(token, tag + ' · reserve 仍然给出 token')
                self.assertFalse(c.live(token), tag + ' · 但 live 不给发送权')

    def test_shadow_keeps_the_old_sender_and_never_the_new_one(self):
        for listen in (True, False):
            for reply in (True, False):
                tag = 'engine=shadow,listen=%s,reply=%s' % (listen, reply)
                with self.subTest(tag), cell(engine='shadow', listen=listen, reply=reply) as c:
                    seen = c.inbound()
                    token = c.reserve(seen.get('cursor', 1), 'direct')
                    self.assertFalse(c.live(token), tag + ' · shadow 不许新 Fish 真发')
                    self.assertEqual(c.dialogue.enabled(), listen and reply, tag + ' · shadow 仍归旧 DSH')

    def test_one_message_has_exactly_one_sender(self):
        for row in self.ROWS:
            tag = self.coordinate(row)
            want = 1 if (row['listen'] and row['reply']) else 0
            with self.subTest(tag), cell(**row) as c:
                token = c.reserve(c.inbound().get('cursor') or 1, 'direct')
                senders = int(c.live(token)) + int(c.dialogue.enabled())
                self.assertEqual(senders, want, tag + ' · 有效发送主体数量')

    def test_read_switch_is_not_a_send_gate(self):
        # read only grants reading history (group_read.read_group); every send
        # predicate in this file must report the same thing with it on and off.
        for engine in ENGINES:
            for listen in (True, False):
                for reply in (True, False):
                    seen = {}
                    for read in (True, False):
                        with cell(engine=engine, listen=listen, reply=reply, read=read) as c:
                            observed = c.inbound()
                            token = c.reserve(observed.get('cursor', 1), 'direct')
                            seen[read] = (bool(observed.get('accepted')), bool(token),
                                          c.live(token), c.dialogue.enabled())
                    tag = 'engine=%s,listen=%s,reply=%s' % (engine, listen, reply)
                    self.assertEqual(seen[True], seen[False], tag + ' · read 改变了发送权限')


class ReservationMatrix(unittest.TestCase):
    """Given that a token exists, or that one cannot: what may this message still do?"""

    @staticmethod
    def coordinate(engine, state):
        return 'engine=%s,reservation=%s' % (engine, state)

    def reserving_cell(self, engine, state):
        """A cell whose message has been through the named reservation history.

        Returns the cell, the message under test (its cursor and its own token, if
        it holds one) and the token the chat's live reservation was granted to.
        """
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        c = Cell(scratch.name, engine=engine)
        first = c.inbound()['cursor']
        granted = c.reserve(first, 'direct')
        self.assertTrue(granted, self.coordinate(engine, state) + ' · 场景前提：拿到了 token')
        subject = {'cursor': first, 'token': granted}
        if state == 'other':
            second = c.inbound()['cursor']
            self.assertEqual(c.reserve(second, 'direct'), '',
                             self.coordinate(engine, state) + ' · 别的消息占着 chat 的唯一 live 预约')
            subject = {'cursor': second, 'token': ''}
        elif state == 'disabled':
            c.policy.configure(CHAT, {'reply': False})   # the switch withdraws it
        elif state == 'sent':
            c.policy.finish(granted, True)
        elif state == 'uncertain':
            c.policy.finish(granted, False, uncertain=True)
        return c, subject, granted

    def test_reservation_matrix(self):
        for engine in ENGINES:
            for state in RESERVATIONS:
                tag = self.coordinate(engine, state)
                with self.subTest(tag):
                    c, subject, granted = self.reserving_cell(engine, state)
                    want = engine == 'fish' and state == 'mine'
                    self.assertEqual(c.live(subject['token']), want, tag + ' · 这条消息能发吗')
                    self.assertEqual(c.reserve(subject['cursor'], 'direct'), '',
                                     tag + ' · 同一游标被再次授予了吗')
                    if state == 'other':
                        self.assertEqual(c.live(granted), engine == 'fish',
                                         tag + ' · 新消息不能取消已经拿到 token 的那条')
                    self.assertEqual(c.dialogue.enabled(), engine in DSH_ENGINES and state != 'disabled',
                                     tag + ' · 旧 DSH 自己的权限')

    def test_uncertain_is_never_reopened(self):
        # An unknown receipt must never be retried: finish(uncertain) closes the
        # cursor, and the reservation row keeps it closed even if the handled
        # pointer were lost. Both are asserted, so either one alone is not enough.
        for engine in ENGINES:
            tag = 'engine=' + engine
            with self.subTest(tag), cell(engine=engine) as c:
                cursor = c.inbound()['cursor']
                token = c.reserve(cursor, 'direct')
                self.assertTrue(token, tag + ' · 场景前提：拿到了 token')
                c.policy.finish(token, False, uncertain=True)
                self.assertFalse(c.live(token), tag + ' · uncertain 不再可发')
                self.assertEqual(c.reserve(cursor, 'direct'), '', tag + ' · uncertain 后 cursor 不再授予')
                self.assertEqual(c.reservation_states(), ['uncertain'], tag + ' · 记录为 uncertain 而不是 cleared')
                with database(c.policy.path) as db:
                    db.execute('UPDATE groups SET handled=0 WHERE chat=?', (CHAT,))
                self.assertEqual(c.policy.candidate(CHAT)['action'], 'candidate',
                                 tag + ' · 场景前提：其余闸门确实都开着')
                self.assertEqual(c.reserve(cursor, 'direct'), '',
                                 tag + ' · 唯一的预约行本身挡住了同一游标的重发')

    def test_sent_is_never_reopened(self):
        for engine in ENGINES:
            tag = 'engine=' + engine
            with self.subTest(tag), cell(engine=engine) as c:
                cursor = c.inbound()['cursor']
                token = c.reserve(cursor, 'direct')
                c.policy.finish(token, True)
                self.assertFalse(c.live(token), tag + ' · 已发出的 token 不再可用')
                self.assertEqual(c.reserve(cursor, 'direct'), '', tag + ' · 已发出的 cursor 不再授予')
                with database(c.policy.path) as db:
                    db.execute('UPDATE groups SET handled=0 WHERE chat=?', (CHAT,))
                self.assertEqual(c.reserve(cursor, 'direct'), '',
                                 tag + ' · 唯一的预约行本身挡住了同一游标的二次发送')


class WithdrawalMatrix(unittest.TestCase):
    """Authorisation withdrawn while a reply is already holding its token."""

    # The three withdrawals the send path must survive. Each is applied two ways:
    # through configure(), the owner's own path, which also retires the token, and
    # by writing the config without retiring, which leaves live() as the only gate.
    CHANGES = [('engine→dsh', {'engine': 'dsh'}),
               ('reply→False', {'reply': False}),
               ('listen→False', {'listen': False})]

    def test_withdrawal_takes_the_token_with_it(self):
        for label, change in self.CHANGES:
            for mode in ('configure', 'store'):
                tag = 'engine=fish 已持有 token 后 %s,via=%s' % (label, mode)
                with self.subTest(tag), cell(engine='fish') as c:
                    cursor = c.inbound()['cursor']
                    token = c.reserve(cursor, 'direct')
                    self.assertTrue(token, tag + ' · 场景前提：拿到了 token')
                    self.assertTrue(c.live(token), tag + ' · 场景前提：当时可以发')
                    self.assertTrue(c.policy.valid(token), tag + ' · 场景前提：游标仍归这条消息')
                    if mode == 'configure':
                        c.policy.configure(CHAT, change)
                    else:
                        c.store(**change)
                    self.assertFalse(c.live(token), tag + ' · live 立刻收回发送权')
                    # An engine rollback does not withdraw the old path's rights, it
                    # hands them back: enabled() follows the engine, live() follows fish.
                    left = c.policy.config(CHAT)
                    self.assertEqual(c.dialogue.enabled(),
                                     left['engine'] in DSH_ENGINES and left['listen'] and left['reply'],
                                     tag + ' · 旧 DSH 的权限随引擎转移')
                    self.assertEqual(c.reserve(cursor, 'direct'), '', tag + ' · 收回后不再授予')
                    if mode == 'store':
                        self.assertEqual(c.reservation_states(), ['active'],
                                         tag + ' · 原始写入没有退休，是 live 自己拒绝了它')


class SingleSenderQuestions(unittest.TestCase):
    """The remaining gates that decide a single send, each on its own axis."""

    def test_in_flight_task_axis(self):
        # A job already generating under the old path carries no permission of its
        # own (GroupDialogue.valid asks enabled() every time), and it does not take
        # the new sender's permission away either. _on_group is what cancels it.
        for engine in ENGINES:
            for running in (True, False):
                tag = 'engine=%s,在途DSH任务=%s' % (engine, '有' if running else '无')
                with self.subTest(tag), cell(engine=engine) as c:
                    job = c.dialogue.jobs.add(payload('job:' + c.tag), 0, '', '', kind='direct')
                    c.dialogue.jobs.update(job['id'], state='generating' if running else 'sent')
                    token = c.reserve(c.inbound()['cursor'], 'direct')
                    self.assertEqual(c.live(token), engine == 'fish', tag + ' · 新 Fish 的发送权不受在途任务影响')
                    self.assertEqual(c.dialogue.valid(job), engine in DSH_ENGINES and running,
                                     tag + ' · 在途任务没有自带发送权')

    def test_the_approval_layer_is_a_separate_permission(self):
        # workflow.allowed() answers "is this group deployed, allowlisted, unpaused",
        # not "which engine". It vetoes the old path and leaves the new sender to
        # its own gates; the matrix pins it True everywhere else.
        for engine in ENGINES:
            tag = 'engine=' + engine
            with self.subTest(tag), cell(engine=engine, allowed=False) as c:
                token = c.reserve(c.inbound()['cursor'], 'direct')
                self.assertFalse(c.dialogue.enabled(), tag + ' · 未获准的群旧 DSH 不发')
                self.assertEqual(c.live(token), engine == 'fish', tag + ' · 新 Fish 由自己的复判与暂停决定')

    def test_pause_vetoes_the_single_sender(self):
        for paused in (False, True):
            tag = 'paused=' + str(paused)
            with self.subTest(tag), cell(engine='fish') as c:
                token = c.reserve(c.inbound()['cursor'], 'direct')
                self.assertTrue(c.live(token), tag + ' · 场景前提：预约本身仍有效')
                sender = types.SimpleNamespace(governance=types.SimpleNamespace(config=lambda: {'paused': paused}))
                self.assertEqual(QQAgentLoop._fish_live(sender, c.policy, token), not paused,
                                 tag + ' · 主人暂停是最后一道闸门')

    def test_an_unreadable_engine_store_blocks(self):
        # Fail-closed: when the owner's choice cannot be confirmed, silence.
        with cell(engine='fish') as c:
            stand = c.dialogue
            stand.chat = 'group:abc'          # not a group scope, so config() raises
            self.assertTrue(stand.blocks(), '读不到引擎时 _engine_blocks 必须为 True')
            self.assertFalse(stand.enabled(), '读不到引擎时 enabled 必须为 False')

    def test_policy_valid_is_not_a_send_permission(self):
        # GroupPolicy.valid() re-asks the cursor, not the engine, so it survives an
        # engine rollback. It is only ever used through GroupDialogue.valid(), which
        # ANDs enabled() - so the composite is safe, and this cell records why the
        # two must never be swapped for one another.
        with cell(engine='dsh') as c:
            cursor = c.inbound()['cursor']
            token = c.reserve(cursor, 'direct')
            job = c.dialogue.jobs.add(payload('job:' + c.tag), 0, '', '', kind='direct')
            c.dialogue.jobs.update(job['id'], state='generating')
            self.assertTrue(c.policy.valid(token), 'policy.valid() 只看游标')
            self.assertFalse(c.policy.live(token), 'dsh 下的 token 不能发')
            self.assertTrue(c.dialogue.valid(job), 'dialogue.valid() = enabled() 与 policy.valid()')
            c.store(engine='fish')
            self.assertTrue(c.policy.valid(token), 'policy.valid() 不随引擎变化')
            self.assertTrue(c.policy.live(token), 'policy.live() 跟着引擎回来')
            self.assertFalse(c.dialogue.valid(job), 'dialogue.valid() 组合上 enabled() 后随引擎变化')


if __name__ == '__main__':
    unittest.main()
