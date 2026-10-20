"""Gusan 2.0: Notion's automatic send. With "Send every meeting's notes to
Notion automatically" on (notion_auto_send), the FIRST notes of a recorded
meeting go to Notion when the meeting finishes - off the GUI thread, once per
meeting, never for a rewrite, a Live Assistant session or a meeting already in
Notion, nothing at all in Privacy mode, and a failure is one calm tray hint.

Two halves: MeetingSession.last_notes_new (did the session just write the
meeting's FIRST notes?) and main.AppController's hook (_maybe_auto_send_notion
-> _auto_send_notion on a worker -> _notion_auto_done on the GUI thread).
notion_sync is a fake here: the real send is tests/test_notion_sync.py's.

Pure logic under test_core's stand-ins: no Qt, no network, no real Notion."""
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
    from tests import test_meeting_session as tms
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401
    import test_meeting_session as tms

import main
import meeting_session as msess
import meeting_store as ms
import notion_export

PAGE = "0123456789abcdef0123456789abcdef"


# ── the session: were these notes the meeting's FIRST? ───────────────────────

class TestLastNotesNew(tms._Base):
    def _session(self, app=None):
        s = self.session(app or tms._App())
        s._build_attributed_transcript = lambda: ""
        s.seen = []                      # last_notes_new as `finished` fires: what main reads
        s.finished.connect(lambda folder, notes, err: s.seen.append(s.last_notes_new))
        return s

    def _record(self, s, notes="## Summary\n- Ship Friday"):
        s.start("Q4 roadmap", "Anna")
        with patch.object(msess.actions, "process", return_value=notes):
            s.stop("")

    def _saved_meeting(self, name="20260929_143000", notes="old notes", **meta):
        folder = self.root / name
        folder.mkdir()
        (folder / "transcript.txt").write_text("We ship Friday.", encoding="utf-8")
        if notes:
            (folder / "notes.md").write_text(notes, encoding="utf-8")
        ms.update_meta(folder, title="Weekly", duration_sec=60, **meta)
        return folder

    def test_nothing_is_new_before_any_notes(self):
        self.assertIs(self._session().last_notes_new, False)

    def test_the_first_notes_of_a_recording_are_new(self):
        s = self._session()
        self._record(s)
        self.assertEqual(s.seen, [True])

    def test_a_rewrite_on_the_session_that_just_finished_is_not(self):
        s = self._session()
        self._record(s)
        with patch.object(msess.actions, "process", return_value="better notes"):
            self.assertTrue(s.retry_notes("qwen_3b"))
        self.assertEqual(s.seen, [True, False])

    def test_notes_that_failed_and_are_retried_are_new(self):
        s = self._session()
        s.start("Q4", "")
        with patch.object(msess.actions, "process", side_effect=RuntimeError("quota exceeded")):
            s.stop("")
        self.assertEqual(s.seen, [False])               # an error: nothing to send
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s.retry_notes("qwen_3b"))
        self.assertEqual(s.seen, [False, True])

    def test_a_rewrite_that_failed_and_is_retried_is_still_a_rewrite(self):
        s = self._session()
        self._record(s)
        with patch.object(msess.actions, "process", side_effect=RuntimeError("offline")):
            s.retry_notes("qwen_3b")
        with patch.object(msess.actions, "process", return_value="third try"):
            s.retry_notes("qwen_3b")
        self.assertEqual(s.seen, [True, False, False])

    def test_a_rewrite_of_a_saved_meeting_is_not(self):
        folder = self._saved_meeting()
        s = self._session()
        with patch.object(msess.actions, "process", return_value="new notes"):
            self.assertTrue(s.retry_notes("qwen_3b", folder=str(folder)))
        self.assertEqual(s.seen, [False])

    def test_a_rewrite_that_failed_on_a_saved_meeting_stays_a_rewrite(self):
        folder = self._saved_meeting()
        s = self._session()
        with patch.object(msess.actions, "process", side_effect=RuntimeError("offline")):
            s.retry_notes("qwen_3b", folder=str(folder))
        with patch.object(msess.actions, "process", return_value="new notes"):
            s.retry_notes("qwen_3b")
        self.assertEqual(s.seen, [False, False])

    def test_a_saved_meeting_that_never_got_notes_gets_its_first(self):
        folder = self._saved_meeting(notes="", notes_error="quota exceeded")
        s = self._session()
        with patch.object(msess.actions, "process", return_value="first notes"):
            self.assertTrue(s.retry_notes("qwen_3b", folder=str(folder)))
        self.assertEqual(s.seen, [True])

    def test_notes_a_quit_left_for_later_are_new(self):
        s = tms.TestQuitKeepsTheMeeting._recording(self)
        with patch("audio_export.write_wav"):
            folder = s.save_for_quit()
        s2 = self._session()
        with patch.object(msess.actions, "process", return_value="## Notes"), \
                patch("history.save_entry", create=True):
            self.assertTrue(s2.write_pending_notes(folder))
        self.assertEqual(s2.seen, [True])

    def test_notes_a_quit_left_for_a_continued_meeting_are_new_too(self):
        # The earlier part's notes.md is still there: the pending mark decides.
        folder = self._saved_meeting(**{msess.META_NOTES_PENDING: True})
        s = self._session()
        with patch.object(msess.actions, "process", return_value="old and new"), \
                patch("history.save_entry", create=True):
            self.assertTrue(s.write_pending_notes(str(folder)))
        self.assertEqual(s.seen, [True])

    def test_a_continued_meetings_new_notes_are_new(self):
        folder = self._saved_meeting()
        s = self._session()
        self.assertTrue(s.continue_meeting(str(folder))[0])
        s._on_chunk_transcribed(0, "And again today.", "en")
        with patch.object(msess.actions, "process", return_value="old and new"):
            s.stop("")
        self.assertEqual(s.seen, [True])

    def test_a_new_recording_after_a_rewrite_starts_over(self):
        s = self._session()
        self._record(s)
        with patch.object(msess.actions, "process", return_value="again"):
            s.retry_notes("qwen_3b")
        s.reset()
        self._record(s)
        self.assertEqual(s.seen, [True, False, True])


# ── main.py: the hook ────────────────────────────────────────────────────────

class _Inline:
    """threading.Thread that runs its target at start()."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def bind(app, *names):
    for name in names:
        setattr(app, name, getattr(main.AppController, name).__get__(app))
    return app


def _sync(result=None, raises=None):
    """A stand-in notion_sync module; .calls records every send_meeting."""
    mod = types.ModuleType("notion_sync")
    mod.calls = []

    def send_meeting(folder, cfg, *, auto=False, persist=None, **kw):
        mod.calls.append({"folder": folder, "cfg": cfg, "auto": auto, "persist": persist,
                          "thread": threading.current_thread()})
        if raises is not None:
            raise raises
        return result if result is not None else {"ok": True, "url": "https://www.notion.so/p",
                                                  "code": "", "sentence": ""}
    mod.send_meeting = send_meeting
    return mod


def titles(a):
    return [h[0] for h in a.hints]


class _HookCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = str(Path(self._tmp.name) / "20261008_101500")
        os.mkdir(self.folder)
        ms.update_meta(self.folder, title="Weekly sync", source="page")
        self.sync = _sync()
        p = patch.dict(sys.modules, {"notion_sync": self.sync})
        p.start()
        self.addCleanup(p.stop)

    def app(self, route="home", last_new=True, **cfg):
        settings = {"notion_auto_send": True, "notion_api_key": "ntn_secret",
                    "notion_parent_page": PAGE, "notion_auth_mode": "oauth",
                    "notify_when_ready": True, **cfg}
        a = SimpleNamespace(cfg=settings, hints=[], history=0, opened=[], tracked=[], applied=[],
                            ran=[], meeting_session=SimpleNamespace(_meeting_title="Weekly sync",
                                                                    last_notes_new=last_new))
        a.notify_history_changed = lambda: setattr(a, "history", a.history + 1)
        a.show_tray_hint = lambda t, m, on_click=None: a.hints.append((t, m, on_click))
        a.show_main = lambda route=None, **o: a.opened.append((route, o))
        a.main_win = SimpleNamespace(isVisible=lambda: True, isMinimized=lambda: False,
                                     current_route=lambda: route)
        a.track = lambda event, props=None: a.tracked.append((event, props))
        a.set_setting = lambda k, v: a.applied.append((k, v)) or True
        a.set_settings = lambda d: a.applied.append(dict(d)) or {k: True for k in d}
        a._run_on_gui = lambda fn, *args: (a.ran.append(fn), fn(*args))[1]
        return bind(a, "_auto_send_notion", "_notion_auto_done")

    def finish(self, a, notes="## Notes\n- Ship Friday", error="", folder=None):
        with patch.object(main.threading, "Thread", _Inline):
            main.AppController._on_meeting_finished(a, self.folder if folder is None else folder,
                                                    notes, error)

    def sent(self):
        return self.sync.calls


class TestAutoSend(_HookCase):
    def test_the_notes_go_once_when_everything_holds(self):
        a = self.app()
        self.finish(a)
        self.assertEqual(len(self.sent()), 1)
        call = self.sent()[0]
        self.assertEqual((call["folder"], call["auto"]), (self.folder, True))
        self.assertEqual(call["cfg"]["notion_api_key"], "ntn_secret")
        self.assertIsNot(call["cfg"], a.cfg)                 # a copy: the worker never reads the live one
        self.assertEqual(a.tracked, [("meeting_exported", {"target": "notion", "ok": True,
                                                           "auto": True, "mode": "oauth"})])
        # (A real send takes seconds, so "Notes ready" comes first there.)
        self.assertEqual([h[:2] for h in a.hints], [("Sent to Notion", "Weekly sync"),
                                                    ("Notes ready", "Weekly sync")])

    def test_the_send_is_the_first_thing_the_finish_handler_does(self):
        # Even where the finish handler would return early (Meetings on screen,
        # "Notify me when ready" off) the notes still go.
        a = self.app(route="meetings", notify_when_ready=False)
        self.finish(a)
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(a.hints, [])                        # ...and it says nothing
        self.assertGreaterEqual(a.history, 2)                # the list refreshed (meta changed)

    def test_off_by_default_nothing_is_sent_and_no_thread_starts(self):
        for cfg in ({"notion_auto_send": False}, {"notion_auto_send": None}):
            a = self.app(**cfg)
            with patch.object(main.threading, "Thread", side_effect=AssertionError("no thread")):
                main.AppController._on_meeting_finished(a, self.folder, "## Notes", "")
            self.assertEqual(self.sent(), [])
        a = self.app()
        del a.cfg["notion_auto_send"]
        self.finish(a)
        self.assertEqual(self.sent(), [])

    def test_only_a_finished_meeting_with_notes_is_sent(self):
        for kw in ({"error": "quota exceeded", "notes": ""}, {"notes": ""}, {"notes": "   \n"},
                   {"folder": ""}):
            self.finish(self.app(), **kw)
        self.assertEqual(self.sent(), [])

    def test_a_rewrite_is_never_sent(self):
        self.finish(self.app(last_new=False))
        self.assertEqual(self.sent(), [])
        a = self.app()
        a.meeting_session = SimpleNamespace(_meeting_title="x")        # a session that never said
        self.finish(a)
        a.meeting_session = None
        self.finish(a)
        self.assertEqual(self.sent(), [])

    def test_nothing_is_sent_while_quitting(self):
        a = self.app()
        a._quitting = True
        self.finish(a)
        self.assertEqual(self.sent(), [])

    def test_a_meeting_that_is_not_connected_is_left_alone(self):
        for cfg in ({"notion_api_key": ""}, {"notion_parent_page": ""},
                    {"notion_parent_page": "not a page"}):
            self.finish(self.app(**cfg))
        self.assertEqual(self.sent(), [])

    def test_a_live_assistant_session_is_never_sent(self):
        ms.update_meta(self.folder, source="live_assist")
        a = self.app()
        self.finish(a)
        self.assertEqual((self.sent(), a.tracked), ([], []))
        # Older ones had no source, only the card's name.
        ms.update_meta(self.folder, source=None, title="Live session 14:05")
        self.finish(self.app())
        self.assertEqual(self.sent(), [])

    def test_a_meeting_already_in_notion_is_not_sent_again(self):
        ms.update_meta(self.folder, notion_url="https://www.notion.so/earlier")
        a = self.app()
        self.finish(a)
        self.assertEqual((self.sent(), a.tracked), ([], []))
        self.assertEqual(titles(a), ["Notes ready"])

    def test_a_calendar_meeting_is_sent(self):
        ms.update_meta(self.folder, source="calendar")
        self.finish(self.app())
        self.assertEqual(len(self.sent()), 1)

    def test_a_manual_click_already_sending_it_is_left_alone(self):
        self.sync.send_meeting = lambda *a, **k: {"ok": False, "url": "", "code": "busy",
                                                  "sentence": ""}
        a = self.app()
        self.finish(a)
        self.assertEqual((a.tracked, titles(a)), ([], ["Notes ready"]))


class TestPrivacyMode(_HookCase):
    def test_nothing_is_sent_nobody_is_asked_and_meta_says_why(self):
        a = self.app(privacy_mode=True)
        self.finish(a)
        self.assertEqual(self.sent(), [])
        self.assertEqual(ms.load_meta(self.folder)["notion_error"], "privacy")
        self.assertEqual(titles(a), ["Notes ready"])         # nothing about Notion
        self.assertEqual(a.applied, [])                      # the switch keeps its value
        self.assertTrue(a.cfg["notion_auto_send"])
        self.assertEqual(a.tracked, [("meeting_exported", {"target": "notion", "ok": False,
                                                           "auto": True, "mode": "oauth",
                                                           "reason": "privacy"})])

    def test_it_is_the_value_when_the_meeting_finished_that_counts(self):
        a = self.app(privacy_mode=False)
        self.finish(a)
        self.assertEqual(len(self.sent()), 1)
        self.assertNotIn("notion_error", ms.load_meta(self.folder))

    def test_live_sessions_and_sent_meetings_are_not_marked(self):
        ms.update_meta(self.folder, source="live_assist")
        self.finish(self.app(privacy_mode=True))
        self.assertNotIn("notion_error", ms.load_meta(self.folder))
        ms.update_meta(self.folder, source="page", notion_url="https://www.notion.so/p")
        self.finish(self.app(privacy_mode=True))
        self.assertNotIn("notion_error", ms.load_meta(self.folder))


class TestResult(_HookCase):
    def _fail(self, code, **cfg):
        self.sync.send_meeting = lambda *a, **k: {"ok": False, "url": "", "code": code,
                                                  "sentence": "Couldn't send to Notion."}
        a = self.app(**cfg)
        self.finish(a)
        return a

    def test_a_failure_is_one_calm_hint_that_opens_the_meeting(self):
        a = self._fail("offline", route="meetings", notify_when_ready=False)
        self.assertEqual(len(a.hints), 1)                    # whatever the notice switch says
        title, text, on_click = a.hints[0]
        self.assertEqual((title, text), ("Couldn't send to Notion", "Check your internet · Open"))
        on_click()
        self.assertEqual(a.opened, [("meetings", {"meeting": self.folder})])
        self.assertEqual(a.tracked[0][1]["reason"], "offline")
        self.assertIs(a.tracked[0][1]["ok"], False)

    def test_the_hint_says_what_to_do_when_that_is_known(self):
        for code, cfg, line in (
                ("auth", {}, "Reconnect it in Meeting settings · Open"),
                ("auth", {"notion_auth_mode": "own"}, "Notion rejected the secret · Open"),
                ("offline", {}, "Check your internet · Open"),
                ("no_access", {}, "Pick another page in Meeting settings · Open"),
                # nothing useful to add: which meeting it was
                ("rate", {}, "Weekly sync · Open"),
                ("other", {}, "Weekly sync · Open")):
            a = self._fail(code, **cfg)
            self.assertEqual(a.hints[0][:2], ("Couldn't send to Notion", line), (code, cfg))
            a.hints[0][2]()
            self.assertEqual(a.opened, [("meetings", {"meeting": self.folder})])

    def test_reasons_are_a_short_fixed_list(self):
        for code, reason in (("auth", "auth"), ("no_access", "no_access"), ("rate", "rate"),
                             ("offline", "offline"), ("other", "other"), ("", "other"),
                             ("some new thing", "other")):
            self.assertEqual(self._fail(code).tracked[0][1]["reason"], reason, code)

    def test_telemetry_carries_no_text_ids_or_urls(self):
        a = self.app()
        self.finish(a)
        event, props = a.tracked[0]
        self.assertEqual(set(props), {"target", "ok", "auto", "mode"})
        self.assertNotIn("https", repr(props))
        a = self._fail("other")
        self.assertEqual(set(a.tracked[0][1]), {"target", "ok", "auto", "mode", "reason"})

    def test_a_refused_connection_asks_for_a_reconnect_once(self):
        a = self._fail("auth")
        self.assertEqual(a.applied, [("notion_reconnect", True)])
        self.assertEqual(a.hints[0][0], "Couldn't send to Notion")
        a = self._fail("auth", notion_auth_mode="own")               # a pasted secret: no reconnect
        self.assertEqual(a.applied, [])
        a = self._fail("rate")
        self.assertEqual(a.applied, [])

    def test_a_connection_already_marked_for_reconnect_starts_no_send_at_all(self):
        # One hint when it broke; after that nothing is sent, no refresh is tried
        # and no hint repeats after every meeting (Settings says "Reconnect").
        a = self.app(notion_reconnect=True)
        with patch.object(main.threading, "Thread", side_effect=AssertionError("no thread")):
            main.AppController._on_meeting_finished(a, self.folder, "## Notes", "")
        self.assertEqual((self.sent(), a.tracked, a.applied), ([], [], []))
        self.assertEqual(titles(a), ["Notes ready"])
        # ...and once the user reconnects (the flag is cleared) it sends again
        a = self.app(notion_reconnect=False)
        self.finish(a)
        self.assertEqual(len(self.sent()), 1)

    def test_a_real_success_carries_the_code_sent_and_still_says_so(self):
        # notion_sync answers {"ok": True, "code": "sent"}; only a skipped
        # (ok False) "sent" is silent.
        self.sync.send_meeting = lambda *a, **k: {"ok": True, "url": "https://www.notion.so/p",
                                                  "code": "sent", "sentence": ""}
        a = self.app()
        self.finish(a)
        self.assertEqual(len(a.tracked), 1)
        self.assertIs(a.tracked[0][1]["ok"], True)
        self.assertIn("Sent to Notion", titles(a))
        self.sync.send_meeting = lambda *a, **k: {"ok": False, "url": "", "code": "sent", "sentence": ""}
        a = self.app()
        self.finish(a)
        self.assertEqual((a.tracked, titles(a)), ([], ["Notes ready"]))

    def test_a_success_says_so_only_away_from_meetings_and_with_the_switch_on(self):
        a = self.app()
        self.finish(a)
        title, text, on_click = a.hints[0]
        self.assertEqual((title, text), ("Sent to Notion", "Weekly sync"))
        on_click()
        self.assertEqual(a.opened, [("meetings", {"meeting": self.folder})])
        for kw in ({"route": "meetings"}, {"notify_when_ready": False}):
            a = self.app(**kw)
            self.finish(a)
            self.assertEqual(a.hints, [], kw)
            self.assertEqual(len(a.tracked), 1)              # still counted

    def test_the_title_is_the_one_the_meeting_had_when_it_finished(self):
        a = self.app()
        gui = []
        a._run_on_gui = lambda fn, *args: gui.append((fn, args))
        self.finish(a)
        a.meeting_session._meeting_title = "A newer meeting"
        for fn, args in gui:
            fn(*args)
        self.assertEqual(a.hints[-1][:2], ("Sent to Notion", "Weekly sync"))

    def test_a_result_that_arrives_while_quitting_shows_nothing(self):
        a = self.app()
        gui = []
        a._run_on_gui = lambda fn, *args: gui.append((fn, args))
        self.finish(a)
        a._quitting = True
        for fn, args in gui:
            fn(*args)
        self.assertNotIn("Sent to Notion", titles(a))

    def test_a_send_that_blows_up_is_a_failure_hint_not_a_crash(self):
        self.sync.send_meeting = _sync(raises=RuntimeError("boom")).send_meeting
        a = self.app()
        self.finish(a)
        self.assertEqual(a.hints[0][:2], ("Couldn't send to Notion", "Weekly sync · Open"))
        self.assertEqual(a.tracked[0][1]["reason"], "other")


class TestOffTheGuiThread(_HookCase):
    def test_the_send_runs_on_its_own_thread_and_the_answer_comes_back_through_the_gui(self):
        a = self.app()
        done = threading.Event()
        a._run_on_gui = lambda fn, *args: (a.ran.append(fn.__name__), fn(*args), done.set())[1]
        main.AppController._on_meeting_finished(a, self.folder, "## Notes", "")   # real thread
        self.assertTrue(done.wait(10))
        call = self.sent()[0]
        self.assertIsNot(call["thread"], threading.main_thread())
        self.assertTrue(call["thread"].daemon)
        self.assertEqual(call["thread"].name, "gusan-notion-auto")
        self.assertEqual(a.ran, ["_notion_auto_done"])
        self.assertIn("Sent to Notion", titles(a))

    def test_saving_a_refreshed_token_hops_to_the_gui_thread(self):
        def send(folder, cfg, *, auto=False, persist=None, **kw):
            persist({"notion_api_key": "new", "notion_refresh_token": "newer"})
            return {"ok": True, "url": "", "code": "", "sentence": ""}
        self.sync.send_meeting = send
        a = self.app()
        hops = []
        a._run_on_gui = lambda fn, *args: hops.append((fn, args)) or fn(*args)
        self.finish(a)
        self.assertEqual(a.applied, [{"notion_api_key": "new", "notion_refresh_token": "newer"}])
        self.assertIn((a.set_settings, ({"notion_api_key": "new", "notion_refresh_token": "newer"},)),
                      hops)


class TestNeverBreaksTheMeeting(_HookCase):
    def test_a_hook_that_fails_leaves_the_finish_handler_working(self):
        a = self.app(route="home")
        with patch.object(notion_export, "is_connected", side_effect=RuntimeError("boom")):
            self.finish(a)
        self.assertEqual(self.sent(), [])
        self.assertEqual(a.hints[0][:2], ("Notes ready", "Weekly sync"))

    def test_notes_ready_still_shows_and_the_old_doubles_still_work(self):
        # The bare doubles of tests/test_services_wiring.py have no hook methods.
        a = SimpleNamespace(cfg={"notify_when_ready": True}, hints=[], history=0, opened=[],
                            meeting_session=SimpleNamespace(_meeting_title="Weekly sync"))
        a.notify_history_changed = lambda: setattr(a, "history", a.history + 1)
        a.show_tray_hint = lambda t, m, on_click=None: a.hints.append((t, m))
        a.main_win = SimpleNamespace(isVisible=lambda: True, isMinimized=lambda: False,
                                     current_route=lambda: "home")
        main.AppController._on_meeting_finished(a, self.folder, "## Notes", "")
        self.assertEqual(a.hints, [("Notes ready", "Weekly sync")])
        self.assertEqual(self.sent(), [])


if __name__ == "__main__":
    unittest.main()
