"""calendar_service without Qt: the saved state (calendar_state.json) and
CalendarCore's decisions, with a fake calendar API, a fake clock and a temp
state file. The QObject (timers, threads, signals) is tested in tests_qt."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calendar_service  # noqa: E402
import calendar_sync  # noqa: E402
from calendar_service import CalendarCore, CalendarState  # noqa: E402

T = datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc)


def meeting(mid, start_min=0, length_min=60, link="https://meet.google.com/abc-defg-hij",
            title="Sync", others=("Anna", "David", "Mariam", "Sam")):
    st = T + timedelta(minutes=start_min)
    return {"id": mid, "title": title, "start": st, "end": st + timedelta(minutes=length_min),
            "attendees": ["Aram"] + list(others), "others": list(others), "link": link,
            "call_type": "Google Meet" if link else "", "recurring_id": "",
            "agenda": "", "has_others": bool(others), "private": False}


class FakeApi:
    def __init__(self, configured=True, connected=True):
        self._configured = configured
        self._connected = connected
        self.forgot = 0

    def configured(self, cfg=None):
        return self._configured

    def is_connected(self):
        return self._connected

    def forget_local(self):
        self.forgot += 1
        token, self._connected = ("rt" if self._connected else ""), False
        return token


class Clock:
    def __init__(self, now=T):
        self.now = now

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gusan-cal-test-")
        self.path = Path(self.dir) / "calendar_state.json"
        self.addCleanup(shutil.rmtree, self.dir, True)

    def core(self, cfg=None, api=None, busy=False, **kw):
        self.clock = kw.pop("clock", Clock())
        self.busy = busy
        return CalendarCore(cfg if cfg is not None else {}, api=api or FakeApi(),
                            state_path=self.path, clock=self.clock,
                            is_busy=lambda: self.busy, **kw)

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))


class TestImport(unittest.TestCase):
    def test_importing_the_module_never_loads_qt(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("import sys; sys.modules['PySide6'] = None; import calendar_service, calendar_sync; "
                "print(hasattr(calendar_service, 'CalendarCore'))")
        env = dict(os.environ, TRANSCRIBE_DISABLE_KEYRING="1", TRANSCRIBE_SKIP_MIGRATION="1",
                   TRANSCRIBE_APP_DATA_DIR=tempfile.gettempdir())
        out = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "True")

    def test_defaults_and_constants(self):
        self.assertEqual(calendar_service.DEFAULTS,
                         {"calendar_ask_to_record": True, "calendar_only_with_link": True})
        self.assertEqual(calendar_service.STATE_FILE, "calendar_state.json")
        self.assertEqual(calendar_service.FETCH_EVERY_MS, 300_000)
        self.assertLessEqual(calendar_service.REMIND_EVERY_MS, 20_000)

    def test_fetch_cadence_constants(self):
        cs = calendar_service
        self.assertEqual((cs.FETCH_FAST_S, cs.FETCH_IDLE_S, cs.FETCH_PRE_MEETING_S,
                          cs.FETCH_BACKOFF_CAP_S, cs.FETCH_STALE_S), (300, 900, 150, 1800, 1200))
        self.assertEqual(cs.FETCH_EVERY_MS, cs.FETCH_FAST_S * 1000)
        self.assertEqual((cs.STATUS_PAUSED_TEXT, cs.STATUS_RETRY_TEXT),
                         ("Paused in Privacy mode.", "Couldn't read Google Calendar. Trying again."))
        self.assertEqual(cs.LINK_ONLY_KEY, "calendar_only_with_link")      # the id keeps its old name


class TestState(Base):
    def test_round_trip_and_default_location(self):
        s = CalendarState(self.path)
        s.email, s.owner = "aram@aibuben.xyz", "user-1"
        s.prompted["a"] = (T + timedelta(hours=1)).isoformat()
        s.muted["b"] = (T + timedelta(hours=2)).isoformat()
        self.assertTrue(s.save())
        again = CalendarState(self.path)
        self.assertEqual((again.email, again.owner), ("aram@aibuben.xyz", "user-1"))
        self.assertEqual(set(again.prompted), {"a"})
        self.assertEqual(set(again.muted), {"b"})
        self.assertEqual(self.saved()["version"], calendar_service.STATE_VERSION)

    def test_missing_or_broken_file_reads_empty(self):
        self.assertEqual(CalendarState(self.path).to_dict()["prompted"], {})
        self.path.write_text("{not json", encoding="utf-8")
        s = CalendarState(self.path)
        self.assertEqual((s.email, s.prompted, s.muted), ("", {}, {}))
        self.path.write_text(json.dumps({"email": 5, "prompted": ["x"], "muted": {"a": 1, "": "x"}}),
                             encoding="utf-8")
        s = CalendarState(self.path)
        self.assertEqual((s.email, s.prompted, s.muted), ("5", {}, {}))

    def test_prune_drops_ended_and_unreadable(self):
        s = CalendarState(self.path)
        s.prompted = {"old": (T - timedelta(minutes=11)).isoformat(),
                      "grace": (T - timedelta(minutes=9)).isoformat(),
                      "naive": "2026-10-07T15:00:00", "bad": "soon"}
        s.muted = {"later": (T + timedelta(hours=3)).isoformat()}
        self.assertTrue(s.prune(T))
        self.assertEqual(set(s.prompted), {"grace"})
        self.assertEqual(set(s.muted), {"later"})
        self.assertFalse(s.prune(T))

    def test_clear(self):
        s = CalendarState(self.path)
        s.email, s.owner, s.prompted, s.muted = "e", "o", {"a": "x"}, {"b": "y"}
        s.clear(keep_prefs=True)
        self.assertEqual((s.email, s.owner, s.prompted, s.muted), ("", "", {"a": "x"}, {"b": "y"}))
        s.clear()
        self.assertEqual((s.prompted, s.muted), ({}, {}))


class TestCoreStatus(Base):
    def test_not_configured_means_nothing_shows(self):
        c = self.core(api=FakeApi(configured=False, connected=True))
        self.assertFalse(c.available)
        self.assertFalse(c.connected)
        c.apply_fetch([meeting("a")])
        self.assertEqual(c.meetings(), [])
        self.assertIsNone(c.take_reminder(T))

    def test_a_saved_email_without_a_token_is_dropped(self):
        s = CalendarState(self.path)
        s.email, s.muted = "aram@aibuben.xyz", {"b": (T + timedelta(hours=1)).isoformat()}
        s.save()
        c = self.core(api=FakeApi(connected=False))
        self.assertFalse(c.connected)
        self.assertEqual(c.state.email, "")
        self.assertEqual(self.saved()["email"], "")
        self.assertIn("b", self.saved()["muted"])          # bells kept for a reconnect

    def test_no_keyring_read_without_a_client(self):
        api = FakeApi(configured=False, connected=True)
        calls = []
        real = api.is_connected
        api.is_connected = lambda: calls.append(1) or real()
        c = self.core(api=api)
        self.assertFalse(c.refresh_connected())
        self.assertEqual(calls, [])
        api._configured = True
        self.assertTrue(c.refresh_connected())                         # a client id arrived
        self.assertTrue(c.connected)
        self.assertEqual(calls, [1])

    def test_cfg_client_id_counts(self):
        cfg = {}
        api = FakeApi()
        api.configured = calendar_sync.configured                     # the real gate
        c = CalendarCore(cfg, api=api, state_path=self.path, clock=Clock())
        self.assertFalse(c.available)
        cfg["google_oauth_client_id"] = "cid"
        self.assertTrue(c.available)


class TestCoreList(Base):
    def test_meetings_are_visible_rows_with_muted_and_people(self):
        c = self.core()
        c.apply_fetch([meeting("later", 90), meeting("ended", -90, 30), meeting("now", -5),
                       {"id": "junk"}])
        c.set_muted("later", True)
        rows = c.meetings(T)
        self.assertEqual([m["id"] for m in rows], ["now", "later"])
        self.assertEqual([m["muted"] for m in rows], [False, True])
        self.assertEqual(rows[0]["people"], "Anna, David +2")
        self.assertNotIn("muted", c.cache[0])                # copies; the cache stays clean

    def test_other_people_filter_applies_to_the_list(self):
        cfg = {}
        c = self.core(cfg)
        # A phone call with people (no link) counts; a solo Focus block with a Meet link does not.
        c.apply_fetch([meeting("call", 10, link="", others=("Anna",)),
                       meeting("lunch", 20, others=())])
        self.assertEqual([m["id"] for m in c.meetings(T)], ["call"])     # default: only with other people
        cfg[calendar_service.LINK_ONLY_KEY] = False
        self.assertEqual([m["id"] for m in c.meetings(T)], ["call", "lunch"])

    def test_next_and_current_meeting(self):
        c = self.core({calendar_service.LINK_ONLY_KEY: True})
        c.apply_fetch([meeting("running", -10, 30), meeting("next", 3, others=()), meeting("later", 60)])
        self.assertEqual(c.next_meeting(T)["id"], "later")      # "next" is alone: filtered
        self.assertEqual(c.current_meeting(T)["id"], "next")     # starts in 3 min, closest start; filter or not
        self.assertEqual(c.current_meeting(T - timedelta(minutes=6))["id"], "running")
        c.apply_fetch([])
        self.assertIsNone(c.next_meeting(T))
        self.assertIsNone(c.current_meeting(T))

    def test_the_live_card_never_picks_a_solo_block(self):
        # current_meeting() feeds the Live card, which sends the title to the AI.
        c = self.core({calendar_service.LINK_ONLY_KEY: False})          # the setting doesn't matter here
        c.apply_fetch([meeting("doctor", -20, 60, others=())])
        self.assertEqual(c.current_meeting(T)["id"], "doctor")          # the start card: any event (spec A9)
        self.assertIsNone(c.current_meeting(T, only_meetings=True))     # the Live card: nothing on
        # A solo block that started closer to now doesn't hide a real meeting about to start.
        c.apply_fetch([meeting("write-report", -2, 90, others=()), meeting("customer", 4)])
        self.assertEqual(c.current_meeting(T)["id"], "write-report")
        self.assertEqual(c.current_meeting(T, only_meetings=True)["id"], "customer")
        self.assertEqual(c.current_meeting(T, only_meetings=True)["people"], "Anna, David +2")

    def test_now_or_next(self):
        c = self.core({calendar_service.LINK_ONLY_KEY: True})
        self.assertEqual(c.now_or_next(T), ("", None))                      # nothing cached
        c.apply_fetch([meeting("running", -10, 30), meeting("later", 60), meeting("solo", 20, others=())])
        kind, m = c.now_or_next(T)
        self.assertEqual((kind, m["id"]), ("now", "running"))               # a running meeting wins
        self.assertEqual(m["people"], "Anna, David +2")                    # decorated like the list rows
        kind, m = c.now_or_next(T + timedelta(minutes=25))                  # it ended; the solo one is filtered
        self.assertEqual((kind, m["id"]), ("next", "later"))
        kind, m = c.now_or_next(T + timedelta(minutes=61))                  # "later" is running now
        self.assertEqual((kind, m["id"]), ("now", "later"))
        self.assertEqual(c.now_or_next(T + timedelta(hours=3)), ("", None))
        # Overlapping meetings: the one that started closest to now.
        c.apply_fetch([meeting("long", -50, 120), meeting("recent", -5, 60)])
        self.assertEqual(c.now_or_next(T)[1]["id"], "recent")
        # The start of a meeting is "now", the minute before it is still "next".
        c.apply_fetch([meeting("m", 5)])
        self.assertEqual(c.now_or_next(T + timedelta(minutes=4, seconds=59))[0], "next")
        self.assertEqual(c.now_or_next(T + timedelta(minutes=5))[0], "now")
        c.cache = [meeting("m", 5)]
        c.connected = False
        self.assertEqual(c.now_or_next(T), ("", None))                      # not connected

    def test_now_or_next_follows_the_filter_setting(self):
        cfg = {}
        c = self.core(cfg)
        c.apply_fetch([meeting("solo", -5, 30, others=())])
        self.assertEqual(c.now_or_next(T), ("", None))
        cfg[calendar_service.LINK_ONLY_KEY] = False
        self.assertEqual(c.now_or_next(T)[0], "now")


class TestCoreReminders(Base):
    def test_take_reminder_marks_once_and_persists(self):
        c = self.core()
        c.apply_fetch([meeting("a")])
        self.assertIsNone(c.take_reminder(T - timedelta(seconds=61)))
        m = c.take_reminder(T - timedelta(seconds=60))
        self.assertEqual(m["id"], "a")
        self.assertEqual(m["people"], "Anna, David +2")
        self.assertIsNone(c.take_reminder(T))
        self.assertEqual(self.saved()["prompted"], {"a": (T + timedelta(hours=1)).isoformat()})
        restarted = self.core()
        restarted.apply_fetch([meeting("a")])
        self.assertIsNone(restarted.take_reminder(T))

    def test_busy_skips_without_marking(self):
        c = self.core(busy=True)
        c.apply_fetch([meeting("a")])
        self.assertIsNone(c.take_reminder(T))
        self.assertEqual(c.state.prompted, {})
        self.busy = False
        self.assertEqual(c.take_reminder(T + timedelta(seconds=30))["id"], "a")

    def test_a_failing_busy_check_counts_as_busy(self):
        c = self.core()
        c.is_busy = lambda: 1 / 0
        c.apply_fetch([meeting("a")])
        self.assertIsNone(c.take_reminder(T))

    def test_muted_ask_off_and_other_people_filter(self):
        cfg = {}
        c = self.core(cfg)
        c.apply_fetch([meeting("a"), meeting("lunch", others=())])
        self.assertTrue(c.set_muted("a", True))
        self.assertFalse(c.set_muted("a", True))
        self.assertIsNone(c.take_reminder(T))                    # a muted, lunch is alone
        cfg[calendar_service.LINK_ONLY_KEY] = False
        cfg[calendar_service.ASK_KEY] = False
        self.assertIsNone(c.take_reminder(T))                    # all reminders off
        cfg[calendar_service.ASK_KEY] = True
        self.assertEqual(c.take_reminder(T)["id"], "lunch")
        self.assertTrue(c.set_muted("a", False))
        self.assertEqual(c.take_reminder(T)["id"], "a")

    def test_muting_an_unknown_id_expires_later(self):
        c = self.core()
        c.set_muted("elsewhere", True, now=T)
        self.assertEqual(self.saved()["muted"]["elsewhere"],
                         (T + calendar_service.UNKNOWN_END_KEEP).isoformat())
        self.assertFalse(c.set_muted("", True))

    def test_fetch_prunes_old_ids(self):
        c = self.core()
        c.apply_fetch([meeting("a", -120, 30)])
        c.mark_prompted("a", T - timedelta(minutes=120))
        c.apply_fetch([], now=T)
        self.assertEqual(self.saved()["prompted"], {})

    def test_not_connected_never_reminds(self):
        c = self.core(api=FakeApi(connected=False))
        c.cache = [meeting("a")]
        self.assertIsNone(c.take_reminder(T))


class TestCoreAccount(Base):
    def test_connect_then_forget_by_the_user(self):
        api = FakeApi(connected=False)
        c = self.core(api=api)
        c.reconcile_owner("user-1")
        api._connected = True
        c.on_connected("aram@aibuben.xyz")
        self.assertTrue(c.connected)
        self.assertEqual((self.saved()["email"], self.saved()["owner"]), ("aram@aibuben.xyz", "user-1"))
        c.apply_fetch([meeting("a")])
        c.set_muted("a", True)
        self.assertEqual(c.forget(calendar_service.REASON_USER), "rt")
        self.assertFalse(c.connected)
        self.assertEqual((c.cache, c.meetings(T)), ([], []))
        self.assertEqual(self.saved(), {"version": 1, "email": "", "owner": "", "prompted": {}, "muted": {}})

    def test_an_expiry_keeps_the_bells(self):
        c = self.core()
        c.apply_fetch([meeting("a")])
        c.set_muted("a", True)
        c.forget(calendar_service.REASON_EXPIRED)
        self.assertIn("a", self.saved()["muted"])

    def _connected_with_ids(self):
        api = FakeApi(connected=False)
        c = self.core(api=api)
        c.reconcile_owner("uid-123")
        api._connected = True
        c.on_connected("founder@example.com")
        c.apply_fetch([meeting("evt-muted"), meeting("evt-asked", 90)])
        c.set_muted("evt-muted", True)
        c.mark_prompted("evt-asked")
        c.state.save()                                   # once more, so a .bak holds the ids
        self.bak = Path(f"{self.path}.bak")
        self.assertTrue(self.bak.exists())
        self.assertIn("founder@example.com", self.bak.read_text(encoding="utf-8"))
        return c

    def test_forgetting_the_calendar_leaves_no_email_or_ids_in_the_backup(self):
        for reason in (calendar_service.REASON_USER, calendar_service.REASON_ACCOUNT):
            c = self._connected_with_ids()
            c.forget(reason)
            self.assertFalse(self.bak.exists(), reason)
            for leftover in (self.path, self.bak):
                text = leftover.read_text(encoding="utf-8") if leftover.exists() else ""
                for secret in ("founder@example.com", "uid-123", "evt-muted", "evt-asked"):
                    self.assertNotIn(secret, text, (reason, secret))
            fresh = CalendarState(self.path)             # nothing to fall back to
            self.assertEqual((fresh.email, fresh.owner, fresh.prompted, fresh.muted), ("", "", {}, {}))

    def test_an_expiry_keeps_the_asked_and_muted_ids(self):
        c = self._connected_with_ids()
        c.forget(calendar_service.REASON_EXPIRED)
        saved = self.saved()
        self.assertEqual((saved["email"], saved["owner"]), ("", ""))
        self.assertIn("evt-muted", saved["muted"])
        self.assertIn("evt-asked", saved["prompted"])
        self.assertTrue(CalendarState(self.path).muted)

    def test_drop_backup_without_a_backup_is_fine(self):
        CalendarState(self.path).drop_backup()

    def test_owner_rules(self):
        c = self.core()
        self.assertEqual(c.reconcile_owner(None), "none")
        self.assertEqual(c.reconcile_owner("user-1"), "adopted")      # connected as a guest
        self.assertEqual(self.saved()["owner"], "user-1")
        self.assertEqual(c.reconcile_owner("user-1"), "same")
        self.assertEqual(c.reconcile_owner(""), "none")                # signed out: keep
        self.assertEqual(c.reconcile_owner("user-2"), "mismatch")
        c.connected = False
        self.assertEqual(c.reconcile_owner("user-3"), "none")
        self.assertEqual(c.owner_hint, "user-3")                       # stamped on the next connect

    def test_a_guest_connect_after_sign_out_belongs_to_nobody(self):
        api = FakeApi(connected=False)
        c = self.core(api=api)
        c.reconcile_owner("user-1")
        c.reconcile_owner(None)                                        # user-1 signed out
        api._connected = True
        c.on_connected("guest@example.com")
        self.assertEqual(self.saved()["owner"], "")
        self.assertEqual(c.reconcile_owner("user-2"), "adopted")       # the first to sign in

    def test_token_still_kept(self):
        api = FakeApi()
        c = self.core(api=api)
        self.assertTrue(c.token_still_kept())
        api._connected = False
        self.assertFalse(c.token_still_kept())
        api.is_connected = lambda: 1 / 0
        self.assertFalse(c.token_still_kept())


if __name__ == "__main__":
    unittest.main()
