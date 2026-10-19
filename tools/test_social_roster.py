import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))

from social_roster import RosterError, SocialRoster

GROUP = "10001"
OTHER = "20002"
ALICE = "111111"
BOB = "222222"
CAROL = "333333"


def m(user_id, nickname="n", card=""):
    return {"user_id": user_id, "nickname": nickname, "card": card}


class RosterTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.roster = SocialRoster(self.root)
        self.roster.configure(GROUP)
        self.roster.sync_members(GROUP, [m(ALICE, "Alice", "Al"), m(BOB, "Bob"), m(CAROL, "Carol")])

    def tearDown(self):
        self._tmp.cleanup()


class SettingsTests(RosterTestBase):
    def test_defaults_and_configure(self):
        fresh = SocialRoster(self.root / "fresh")
        self.assertEqual(fresh.settings(), {"group_id": "", "auto_affinity": True})
        with self.assertRaises(RosterError):
            fresh.members(GROUP)

        fresh.configure(GROUP, auto_affinity=False)
        self.assertEqual(fresh.settings(), {"group_id": GROUP, "auto_affinity": False})

    def test_group_id_validation(self):
        for bad in ("", "1234", "1234567890123456", "12345a", 12345, None):
            with self.assertRaises(RosterError):
                self.roster.configure(bad)
        with self.assertRaises(RosterError):
            self.roster.configure(GROUP, auto_affinity="yes")


class SyncTests(RosterTestBase):
    def test_initial_values_and_counts(self):
        result = self.roster.sync_members(GROUP, [m(ALICE, "A"), m("444444", "D")])
        self.assertEqual(result, {"total": 2, "added": 1, "left": 2})
        carol = self.roster.member(GROUP, CAROL)
        self.assertFalse(carol["present"])
        self.assertEqual(carol["affinity"], 30)
        member = self.roster.member(GROUP, "444444")
        self.assertEqual(member["affinity"], 30)
        self.assertFalse(member["locked"])
        self.assertEqual(member["preferences"], "")
        self.assertEqual(member["aliases"], [])
        self.assertTrue(self.roster.member(GROUP, ALICE)["present"])

    def test_invalid_sync_is_atomic(self):
        before = self.roster.members(GROUP)
        bad_batches = [
            [],
            [m(ALICE, "changed"), m("bad")],
            [{"user_id": ALICE, "nickname": 1, "card": ""}],
            [m(ALICE), m(ALICE)],
            [m("9")],
            [m("9" * 16)],
        ]
        for batch in bad_batches:
            with self.assertRaises(RosterError):
                self.roster.sync_members(GROUP, batch)
        with self.assertRaises(RosterError):
            self.roster.sync_members(GROUP, "not a list")
        self.assertEqual(self.roster.members(GROUP), before)

    def test_resync_preserves_manual_values(self):
        self.roster.save_member(
            GROUP, ALICE, affinity=88, locked=True,
            preferences="likes tea", aliases=["Ali"],
        )
        self.roster.sync_members(GROUP, [m(ALICE, "Alice2", "A2"), m(BOB), m(CAROL)])
        alice = self.roster.member(GROUP, ALICE)
        self.assertEqual(alice["affinity"], 88)
        self.assertTrue(alice["locked"])
        self.assertEqual(alice["preferences"], "likes tea")
        self.assertEqual(alice["aliases"], ["Ali"])
        self.assertEqual(alice["nickname"], "Alice2")

    def test_save_member_validation_and_audit(self):
        for kwargs in (
            dict(affinity=True, locked=False, preferences="", aliases=[]),
            dict(affinity=101, locked=False, preferences="", aliases=[]),
            dict(affinity=50, locked=1, preferences="", aliases=[]),
            dict(affinity=50, locked=False, preferences="x" * 2001, aliases=[]),
            dict(affinity=50, locked=False, preferences="", aliases=["x" * 81]),
            dict(affinity=50, locked=False, preferences="", aliases=["ok"] * 21),
        ):
            with self.assertRaises(RosterError):
                self.roster.save_member(GROUP, ALICE, **kwargs)
        self.roster.save_member(GROUP, ALICE, affinity=45, locked=False, preferences="p", aliases=[])
        latest = self.roster.events(GROUP, ALICE)[0]
        self.assertEqual((latest["kind"], latest["delta"]), ("manual", 15))
        with self.assertRaises(RosterError):
            self.roster.save_member(GROUP, "999999", affinity=1, locked=False,
                                    preferences="", aliases=[])


class EventTests(RosterTestBase):
    def test_source_event_dedup_per_group(self):
        r1 = self.roster.apply_event(GROUP, ALICE, event_id="e1", kind="helpful", message_id="m", now=1000)
        r2 = self.roster.apply_event(GROUP, ALICE, event_id="e1", kind="hostile", message_id="m", now=2000)
        self.assertEqual((r1["applied"], r1["delta"], r1["affinity"]), (True, 2, 32))
        self.assertEqual((r2["applied"], r2["delta"], r2["affinity"]), (False, 0, 32))
        self.assertEqual(len(self.roster.events(GROUP, ALICE)), 1)
        # Same source id is allowed again after selecting another group.
        self.roster.configure(OTHER)
        self.roster.sync_members(OTHER, [m(ALICE)])
        self.assertTrue(self.roster.apply_event(OTHER, ALICE, event_id="e1", kind="warm",
                                                message_id="m", now=5000)["applied"])

    def test_event_validation(self):
        with self.assertRaises(RosterError):
            self.roster.apply_event(GROUP, ALICE, event_id="", kind="warm", message_id="")
        with self.assertRaises(RosterError):
            self.roster.apply_event(GROUP, ALICE, event_id="x" * 201, kind="warm", message_id="")
        with self.assertRaises(RosterError):
            self.roster.apply_event(GROUP, ALICE, event_id="x", kind="great", message_id="")
        with self.assertRaises(RosterError):
            self.roster.apply_event(GROUP, ALICE, event_id="x", kind="warm", message_id="x" * 201)
        self.assertEqual(self.roster.events(GROUP, ALICE), [])

    def test_cooldown_and_daily_cap(self):
        r = self.roster.apply_event(GROUP, ALICE, event_id="a", kind="helpful", message_id="", now=0)
        self.assertEqual(r["delta"], 2)
        r = self.roster.apply_event(GROUP, ALICE, event_id="b", kind="helpful", message_id="", now=599)
        self.assertEqual((r["applied"], r["delta"]), (False, 0))
        now = 600
        for i in range(5):
            r = self.roster.apply_event(GROUP, ALICE, event_id=f"c{i}", kind="helpful",
                                        message_id="", now=now)
            now += 600
        # 2 + 2 + 2 (cap) then 0 + 0
        self.assertEqual(self.roster.member(GROUP, ALICE)["affinity"], 36)
        # Next day resets the window.
        r = self.roster.apply_event(GROUP, ALICE, event_id="next", kind="warm",
                                    message_id="", now=now + 86400)
        self.assertEqual(r["delta"], 1)

    def test_clamp_at_bounds(self):
        self.roster.save_member(GROUP, ALICE, affinity=99, locked=False, preferences='', aliases=[])
        self.assertEqual(self.roster.apply_event(GROUP, ALICE, event_id='upper',kind='helpful',message_id='',now=0)['delta'],1)
        self.assertEqual(self.roster.member(GROUP, ALICE)['affinity'],100)
        self.roster.save_member(GROUP, ALICE, affinity=0, locked=False, preferences='', aliases=[])
        self.assertEqual(self.roster.apply_event(GROUP, ALICE,event_id='lower',kind='hostile',message_id='',now=86400)['delta'],0)
        self.assertEqual(self.roster.member(GROUP, ALICE)['affinity'],0)

    def test_locked_mode(self):
        self.roster.save_member(GROUP, ALICE, affinity=50, locked=True, preferences="", aliases=[])
        r = self.roster.apply_event(GROUP, ALICE, event_id="l", kind="helpful", message_id="")
        self.assertEqual((r["applied"], r["delta"], r["affinity"]), (False, 0, 50))
        self.assertEqual(len(self.roster.events(GROUP, ALICE)), 3)  # lock + manual score + help
        lock = next(e for e in self.roster.events(GROUP, ALICE) if e['kind']=='manual_lock')
        self.assertEqual(lock['delta'],0)
        with self.assertRaises(RosterError):self.roster.undo_event(GROUP,lock['id'])

    def test_disabled_mode_and_absent_member(self):
        self.roster.configure(GROUP, auto_affinity=False)
        r = self.roster.apply_event(GROUP, ALICE, event_id="d", kind="hostile", message_id="")
        self.assertEqual((r["applied"], r["delta"], r["affinity"]), (False, 0, 30))
        r = self.roster.apply_event(GROUP, "777777", event_id="d", kind="helpful", message_id="")
        self.assertEqual((r["applied"], r["delta"]), (False, 0))
        with self.assertRaises(RosterError):
            self.roster.member(GROUP, "777777")

    def test_neutral_dedups_without_change(self):
        r = self.roster.apply_event(GROUP, ALICE, event_id="n", kind="neutral", message_id="")
        self.assertEqual((r["applied"], r["delta"], r["affinity"]), (False, 0, 30))
        self.assertEqual(len(self.roster.events(GROUP, ALICE)), 1)

    def test_events_limit_bounded(self):
        for i in range(5):
            self.roster.apply_event(GROUP, ALICE, event_id=f"e{i}", kind="warm", message_id="")
        self.assertEqual(len(self.roster.events(GROUP, ALICE, limit=0)), 1)
        self.assertEqual(len(self.roster.events(GROUP, ALICE, limit=500)), 5)


class UndoTests(RosterTestBase):
    def test_undo_preserves_intervening_changes(self):
        self.roster.apply_event(GROUP, ALICE, event_id="src", kind="helpful", message_id="m", now=0)
        event = self.roster.events(GROUP, ALICE)[0]
        self.assertNotEqual(event["id"], "src")
        self.roster.save_member(GROUP, ALICE, affinity=40, locked=False, preferences="", aliases=[])
        member = self.roster.undo_event(GROUP, event["id"])
        self.assertEqual(member["affinity"], 38)
        self.assertTrue(self.roster.events(GROUP, ALICE)[-1]["undone"])

    def test_no_double_undo_or_undo_of_undo(self):
        self.roster.apply_event(GROUP, ALICE, event_id="src", kind="hostile", message_id="", now=0)
        event = self.roster.events(GROUP, ALICE)[0]
        self.roster.undo_event(GROUP, event["id"])
        with self.assertRaises(RosterError):
            self.roster.undo_event(GROUP, event["id"])
        reversal = self.roster.events(GROUP, ALICE)[0]
        self.assertEqual(reversal["kind"], "undo")
        with self.assertRaises(RosterError):
            self.roster.undo_event(GROUP, reversal["id"])
        with self.assertRaises(RosterError):
            self.roster.undo_event(GROUP, "not-an-id")
        self.assertEqual(self.roster.member(GROUP, ALICE)["affinity"], 30)


class ScopeTests(RosterTestBase):
    def test_selected_group_isolation(self):
        self.roster.configure(OTHER)
        self.roster.sync_members(OTHER, [m(ALICE, "OtherAlice")])
        for call in (
            lambda: self.roster.members(GROUP),
            lambda: self.roster.member(GROUP, ALICE),
            lambda: self.roster.sync_members(GROUP, [m(BOB)]),
            lambda: self.roster.apply_event(GROUP, ALICE, event_id="x", kind="warm", message_id=""),
            lambda: self.roster.events(GROUP, ALICE),
            lambda: self.roster.public_profile(GROUP, ALICE),
            lambda: self.roster.behavior(GROUP, ALICE),
            lambda: self.roster.save_member(GROUP, ALICE, affinity=1, locked=False,
                                            preferences="", aliases=[]),
        ):
            with self.assertRaises(RosterError):
                call()
        self.assertEqual(self.roster.member(OTHER, ALICE)["nickname"], "OtherAlice")
        # Switching back keeps the old group's records.
        self.roster.configure(GROUP)
        self.assertEqual(self.roster.member(GROUP, ALICE)["nickname"], "Alice")


class QueryTests(RosterTestBase):
    def test_substring_and_injection(self):
        self.roster.sync_members(GROUP, [m(ALICE,'Alice','Al'),m(BOB,'Other'),m(CAROL,'Carol'),
                                         m('444444','percent%'),m('666666','under_'),m("555555", "Bobby Tables")])
        self.assertEqual([x["user_id"] for x in self.roster.members(GROUP, "Bob")], ["555555"])
        self.assertEqual([x["user_id"] for x in self.roster.members(GROUP, "111")], [ALICE])
        self.assertEqual([x["user_id"] for x in self.roster.members(GROUP, "Al")], [ALICE])
        for evil in ("' OR 1=1 --", "'; DROP TABLE members; --", "%' OR '%'='", "%", "_"):
            self.assertIsInstance(self.roster.members(GROUP, evil), list)
        self.assertEqual(self.roster.members(GROUP, "' OR 1=1 --"), [])
        self.assertEqual(len(self.roster.members(GROUP)), 6)


class ProjectionTests(RosterTestBase):
    def test_public_profile_allowlist(self):
        self.roster.save_member(GROUP, ALICE, affinity=90, locked=True,
                                preferences="pref", aliases=["A"])
        self.roster.apply_event(GROUP, ALICE, event_id="p", kind="warm", message_id="")
        profile = self.roster.public_profile(GROUP, ALICE)
        self.assertEqual(set(profile), {"user_id", "nickname", "card", "aliases", "preferences"})
        self.assertEqual(profile["user_id"], ALICE)
        unknown = self.roster.public_profile(GROUP, "999999")
        self.assertEqual(set(unknown), set(profile))

    def test_behavior_allowlist_and_bands(self):
        for score, expected in ((0, (120, 0.2, 2.0)), (39, (120, 0.2, 2.0)),
                                (40, (240, 0.5, 1.5)), (74, (240, 0.5, 1.5)),
                                (75, (480, 0.8, 1.0)), (100, (480, 0.8, 1.0))):
            self.roster.save_member(GROUP, ALICE, affinity=score, locked=False,
                                    preferences="", aliases=[])
            b = self.roster.behavior(GROUP, ALICE)
            self.assertEqual(set(b), {"max_chars", "active_probability", "cooldown_multiplier"})
            self.assertEqual((b["max_chars"], b["active_probability"],
                              b["cooldown_multiplier"]), expected)
        unknown = self.roster.behavior(GROUP, "999999")
        self.assertEqual(unknown, {"max_chars": 120, "active_probability": 0.2,
                                   "cooldown_multiplier": 2.0})
        with self.assertRaises(RosterError):
            self.roster.member(GROUP, "999999")  # not invented


class PersistenceTests(RosterTestBase):
    def test_restart_persistence(self):
        self.roster.save_member(GROUP, ALICE, affinity=61, locked=False,
                                preferences="likes tea", aliases=["Ali"])
        self.roster.apply_event(GROUP, ALICE, event_id="s", kind="warm", message_id="mid", now=0)
        db = self.root / "data" / "social" / "people.db"
        self.assertTrue(db.exists())
        reopened = SocialRoster(self.root)
        self.assertEqual(reopened.settings()["group_id"], GROUP)
        alice = reopened.member(GROUP, ALICE)
        self.assertEqual((alice["affinity"], alice["preferences"], alice["aliases"]),
                         (62, "likes tea", ["Ali"]))
        self.assertEqual(reopened.events(GROUP, ALICE)[0]["message_id"], "mid")


if __name__ == "__main__":
    unittest.main()
