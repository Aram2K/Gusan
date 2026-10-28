"""Gusan 2.0 Wave 4: where every 1.x entry point lands now (show_* -> pages),
the surfaces the controller builds (tray menu, onboarding, sign-in sheet,
calendar reminder, Pro window) and the copy that changed with the rename.

Pure logic: controllers are SimpleNamespace / MagicMock doubles with the real
methods bound, under test_core's stand-ins (no Qt, no network). The real
widgets are covered in tests_qt/test_wave4_controller.py."""
import os
import re
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bind(app, *names):
    for name in names:
        setattr(app, name, getattr(main.AppController, name).__get__(app))
    return app


class Sig:
    """A Qt signal stand-in: connect / emit."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


def fake_module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def opener():
    """An app double whose show_main records (route, opts)."""
    app = SimpleNamespace(opened=[], tracked=[])
    app.show_main = lambda page=None, **o: app.opened.append((page, o)) or "win"
    app.track = lambda event, props=None: app.tracked.append(event)
    return app


# ── the 1.x entry points open their pages ─────────────────────────────────────

class TestRoutes(unittest.TestCase):
    def test_show_settings_opens_the_settings_page(self):
        app = bind(opener(), "show_settings")
        app.show_settings()
        app.show_settings("speech")
        app.show_settings(section="privacy", focus="keys")
        self.assertEqual(app.opened, [("settings", {}), ("settings", {"section": "speech"}),
                                      ("settings", {"section": "privacy", "focus": "keys"})])

    def test_files_history_and_meetings(self):
        app = bind(opener(), "show_file_transcribe", "show_history", "show_meeting")
        app.is_pro = lambda: False
        app._pro_upsell = mock.MagicMock()
        app.show_file_transcribe()
        app.show_file_transcribe(paths=["C:/rec/call.m4a"])
        app.show_history()
        app.show_meeting()                       # Free too: Record is the Pro gate, not opening
        self.assertEqual(app.opened, [("audio_to_text", {}),
                                      ("audio_to_text", {"paths": ["C:/rec/call.m4a"]}),
                                      ("home", {}), ("meetings", {})])
        self.assertEqual(app.tracked, ["history_opened"])
        app._pro_upsell.assert_not_called()

    def test_gpu_notifications_open_the_gpu_card(self):
        app = bind(opener(), "show_gpu_settings")
        app.show_gpu_settings()
        self.assertEqual(app.opened, [("settings", {"section": "speech", "focus": "gpu"})])

    def test_classic_settings_only_as_the_last_resort(self):
        # The 1.x Settings window retired: a Gusan window that can't be built
        # says so in a notification (never a silent click), and nothing else opens.
        hints, fronted = [], []
        app = SimpleNamespace(fronted=fronted, tracked=[])
        app._ensure_main_window = mock.MagicMock(side_effect=RuntimeError("no screen"))
        app._bring_to_front = fronted.append
        app.track = lambda event, props=None: app.tracked.append(event)
        app.show_tray_hint = lambda t, m, on_click=None: hints.append(t)
        self.assertIsNone(main.AppController.show_main(app, "settings"))
        self.assertEqual(hints, ["Gusan couldn't open its window"])
        self.assertEqual((fronted, app.tracked), ([], []))
        self.assertNotIn("_show_classic_settings", vars(main.AppController))
        win = SimpleNamespace(opened=[], current_route=lambda: None)
        win.open = lambda route, **o: win.opened.append((route, o))
        app._ensure_main_window = lambda: win
        self.assertIs(main.AppController.show_main(app, "settings", section="speech"), win)
        self.assertEqual((win.opened, fronted, app.tracked),
                         ([("settings", {"section": "speech"})], [win], ["settings_opened"]))

    def test_ipc_names_are_unchanged(self):
        self.assertEqual(main.IPC_ACTIONS, ("show_settings", "show_onboarding", "show_meeting", "live_prompter"))
        self.assertEqual(main.SINGLE_INSTANCE_PORT, 47823)


# ── the 1.x windows are retired ───────────────────────────────────────────────

_RETIRED = ("settings", "history", "meeting_detail", "meetings", "onboarding", "shiny_tabs",
            "mode_combo", "file_transcribe")
_RETIRED_RE = re.compile(
    r"^\s*(?:from\s+ui\.(?:%(m)s)\b|import\s+ui\.(?:%(m)s)\b|from\s+ui\s+import\s+[^\n#]*\b(?:%(m)s)\b)"
    r"|[\"']ui\.(?:%(m)s)[.\"']" % {"m": "|".join(_RETIRED)}, re.M)


def _source_files():
    skip_dirs = {"venv", ".claude", ".git", "build", "dist", "__pycache__", "node_modules"}
    me = os.path.abspath(__file__)
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        rel = os.path.relpath(root, ROOT).replace("\\", "/")
        for fn in files:
            path = os.path.join(root, fn)
            if not fn.endswith(".py") or os.path.abspath(path) == me:
                continue
            if rel == "ui" and fn[:-3] in _RETIRED:
                continue                       # the retired files themselves
            yield path


class TestRetiredWindows(unittest.TestCase):
    def test_nothing_imports_the_1x_windows(self):
        # Settings, History, the meeting detail, MeetingsWindow, the old
        # onboarding and their widgets (shiny tabs, mode combo, file tab):
        # no app module, page or test imports them, so they can be deleted.
        users = []
        for path in _source_files():
            src = open(path, encoding="utf-8", errors="ignore").read()
            users += ["%s: %s" % (os.path.relpath(path, ROOT), m.group(0).strip())
                      for m in _RETIRED_RE.finditer(src)]
        self.assertEqual(users, [])

    def test_the_controller_keeps_no_classic_window(self):
        src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        for name in ("settings_win", "history_win", "meetings_win", "_show_classic_settings"):
            self.assertNotIn(name, src)
        init = src[src.index("    def __init__(self, qapp):"):src.index("    def _setup_tray(self):")]
        self.assertIn("Overlay(main_app=self)", init)            # the dictation bar stays eager
        self.assertNotIn("MainWindow(", init)                     # the Gusan window: on first open
        self.assertNotIn("OnboardingWizard(", init)

    def test_the_shell_has_no_classic_links(self):
        from_shell = open(os.path.join(ROOT, "ui", "shell.py"), encoding="utf-8").read()
        for gone in ("PlaceholderPage", "classic", "CLASSIC"):
            self.assertNotIn(gone, from_shell)


# ── onboarding and sign-in ────────────────────────────────────────────────────

class FakeWizard:
    made = []

    def __init__(self, app):
        self.app = app
        self.accepted = Sig()
        FakeWizard.made.append(self)

    def isVisible(self):
        return False


class TestOnboardingAndSignIn(unittest.TestCase):
    def setUp(self):
        FakeWizard.made = []

    def _app(self, done):
        app = bind(opener(), "show_onboarding")
        app.cfg = {"onboarding_done": done}
        app.onboarding_win = None
        app.downloads = SimpleNamespace(probe_hardware=mock.MagicMock())
        app.fronted = []
        app._bring_to_front = app.fronted.append
        return app

    def test_an_onboarded_user_never_sees_the_wizard(self):
        app = self._app(done=True)
        with mock.patch.dict(sys.modules, {"ui.onboarding_v2": fake_module("ui.onboarding_v2",
                                                                           OnboardingWizard=FakeWizard)}):
            app.show_onboarding()
        self.assertEqual(FakeWizard.made, [])
        self.assertIsNone(app.onboarding_win)
        self.assertEqual(app.opened, [(None, {})])            # the app instead
        app.downloads.probe_hardware.assert_not_called()

    def test_first_run_builds_the_new_wizard_once_and_lands_on_home(self):
        app = self._app(done=False)
        with mock.patch.dict(sys.modules, {"ui.onboarding_v2": fake_module("ui.onboarding_v2",
                                                                           OnboardingWizard=FakeWizard)}):
            wiz = app.show_onboarding()
            again = app.show_onboarding()                     # brought forward, not rebuilt
        self.assertIs(wiz, again)
        self.assertEqual(len(FakeWizard.made), 1)
        self.assertIs(app.onboarding_win, wiz)
        self.assertEqual(app.fronted, [wiz, wiz])
        app.downloads.probe_hardware.assert_called_once_with()   # step 2's chips
        wiz.accepted.emit()
        self.assertEqual(app.opened, [("home", {})])

    def test_no_automatic_account_gate_and_no_eager_old_onboarding(self):
        src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        init = src[src.index("    def __init__(self, qapp):"):src.index("    def _setup_tray(self):")]
        self.assertNotIn("show_account_gate", init)
        self.assertNotIn("from ui.onboarding import", src)    # the 1.x wizard is not used
        self.assertIn("QTimer.singleShot(500, self.show_onboarding)", init)

    def test_auth_gate_uses_the_sign_in_sheet(self):
        app = bind(opener(), "show_auth_gate")
        app.auth = SimpleNamespace(is_authenticated=False)
        app._show_auth_sheet = mock.MagicMock(return_value="sheet")
        self.assertEqual(app.show_auth_gate(), "sheet")
        app._show_auth_sheet.assert_called_once_with("signin")
        app.auth.is_authenticated = True
        app.show_auth_gate()
        self.assertEqual(app.opened, [("account", {})])        # signed in: Account & plan

    def test_a_super_admin_previewing_guest_gets_the_sign_in_sheet(self):
        # Signed in underneath, a guest on screen: "Sign in / Sign up" must open the sheet a guest
        # would get, not bounce to Account & plan (whose own button would bounce back).
        app = bind(opener(), "show_auth_gate")
        app.auth = SimpleNamespace(is_authenticated=True, is_pro=False, user_name="Aram", user_email="a@b.c")
        app.is_pro = lambda: False
        app.current_tier = lambda: "guest"
        app._show_auth_sheet = mock.MagicMock(return_value="sheet")
        self.assertEqual(app.show_auth_gate(), "sheet")
        app._show_auth_sheet.assert_called_once_with("signin")
        self.assertEqual(app.opened, [])
        app.current_tier = lambda: "free"                      # previewing Free (or really Free): Account
        app.show_auth_gate()
        self.assertEqual(app.opened, [("account", {})])

    def test_the_sheet_is_wired_once_and_opens_the_app_when_signed_in(self):
        sheet = SimpleNamespace(signed_in=Sig(), finished=Sig())
        calls = []

        def show_auth_sheet(app, mode, parent=None):
            calls.append((mode, parent))
            return sheet
        app = bind(opener(), "_show_auth_sheet")
        app._dialog_parent = lambda: "window"
        with mock.patch.dict(sys.modules, {"ui.auth_sheet": fake_module("ui.auth_sheet",
                                                                        show_auth_sheet=show_auth_sheet)}):
            app._show_auth_sheet("signin")
            app._show_auth_sheet("signin")
        self.assertEqual(calls, [("signin", "window"), ("signin", "window")])
        self.assertEqual(len(sheet.signed_in.slots), 1)        # one slot, however often it opens
        sheet.signed_in.emit()
        self.assertEqual(app.opened, [(None, {})])

    def test_account_gate_marks_itself_seen(self):
        sheet = SimpleNamespace(signed_in=Sig(), finished=Sig())
        app = bind(SimpleNamespace(), "show_account_gate")
        app.cfg = {"account_gate_seen": False}
        app.auth = SimpleNamespace(is_authenticated=False)
        app.set_setting = mock.MagicMock(return_value=True)
        app._show_auth_sheet = mock.MagicMock(return_value=sheet)
        self.assertIs(app.show_account_gate(), sheet)
        app._show_auth_sheet.assert_called_once_with("signup")
        sheet.finished.emit(0)
        app.set_setting.assert_called_once_with("account_gate_seen", True)
        app.cfg["account_gate_seen"] = True
        self.assertIsNone(app.show_account_gate())             # once


# ── the Pro window ────────────────────────────────────────────────────────────

class TestProWindow(unittest.TestCase):
    def _run(self, feature, main_win=None):
        made = []

        class FakeProDialog:
            def __init__(self, main_app=None, feature=None, parent=None):
                made.append((feature, parent))

            def exec(self):
                return 0
        app = bind(SimpleNamespace(cfg={}, main_win=main_win), "_pro_upsell", "_dialog_parent")
        with mock.patch.dict(sys.modules, {"ui.pro_dialog": fake_module("ui.pro_dialog", ProDialog=FakeProDialog)}), \
                mock.patch.object(main.telemetry, "track") as track:
            app._pro_upsell(feature)
        return made, track.call_args[0][1]

    def test_live_assistant_shows_the_new_name_and_tracks_the_old_one(self):
        for asked in ("Live Assistance", "Live Assistant"):
            made, props = self._run(asked)
            self.assertEqual(made, [("Live Assistant", None)])
            self.assertEqual(props, {"feature": "Live Assistance"})
        for name in ("Meeting recording", "Smart Actions", "Fast cloud transcription"):
            made, props = self._run(name)
            self.assertEqual((made[0][0], props), (name, {"feature": name}))
        made, props = self._run(None)                          # the plain Upgrade
        self.assertEqual((made[0][0], props), (None, {"feature": ""}))

    def _fallback(self, guest):
        """The plain prompt shown when the Pro window cannot be built: what it offers."""
        class BrokenProDialog:
            def __init__(self, *a, **k):
                raise RuntimeError("no window")
        prompts = []
        prompt = fake_module("ui.components.mini_prompt",
                             choose=lambda parent, title, text, buttons, **kw: prompts.append(
                                 (text, [b[0] for b in buttons], [b[1] for b in buttons])) or None)
        components = fake_module("ui.components", mini_prompt=prompt)
        app = bind(SimpleNamespace(cfg={}, main_win=None, auth=SimpleNamespace(is_authenticated=not guest),
                                   is_pro=lambda: False, current_tier=lambda: "guest" if guest else "free"),
                   "_pro_upsell", "_dialog_parent")
        mods = {"ui.pro_dialog": fake_module("ui.pro_dialog", ProDialog=BrokenProDialog),
                "ui.components": components, "ui.components.mini_prompt": prompt}
        with mock.patch.dict(sys.modules, mods), mock.patch.object(main.telemetry, "track"):
            app._pro_upsell("Meeting recording")
        return prompts[0]

    def test_the_plain_prompt_leads_a_guest_through_sign_in_first(self):
        text, keys, labels = self._fallback(guest=True)
        self.assertEqual((keys, labels), (["signin", "cancel"], ["Sign in / Sign up", "Maybe later"]))
        self.assertIn("Sign in first", text)
        self.assertNotIn("Upgrade", text)                      # no price buttons, no upgrade wording
        text, keys, labels = self._fallback(guest=False)
        self.assertEqual(keys, ["monthly", "annual", "cancel"])
        self.assertIn("Upgrade to unlock", text)

    def test_it_opens_over_the_window_while_the_window_is_up(self):
        win = SimpleNamespace(isVisible=lambda: True, isMinimized=lambda: False)
        made, _ = self._run("Smart Actions", main_win=win)
        self.assertIs(made[0][1], win)
        for hidden in (SimpleNamespace(isVisible=lambda: False, isMinimized=lambda: False),
                       SimpleNamespace(isVisible=lambda: True, isMinimized=lambda: True)):
            made, _ = self._run("Smart Actions", main_win=hidden)
            self.assertIsNone(made[0][1])


# ── the tray menu ─────────────────────────────────────────────────────────────

class FakeTray:
    def __init__(self, app, menu):
        self.app, self.menu = app, menu
        self.refreshed = 0
        self.calendars = []

    def refresh(self):
        self.refreshed += 1

    def tooltip(self):
        return "Gusan"

    def attach_calendar(self, cal):
        self.calendars.append(cal)

    def on_setting_changed(self, key):
        pass


class TestTray(unittest.TestCase):
    def test_built_once_then_refreshed(self):
        built = []

        def build_tray_menu(app, menu=None, custom=None):
            built.append(FakeTray(app, menu))
            return built[-1]
        icon = mock.MagicMock()
        cal = object()
        app = bind(SimpleNamespace(tray_icon=icon, calendar=cal, sig_setting_changed=Sig()), "_build_tray_menu")
        with mock.patch.dict(sys.modules, {"ui.tray_menu": fake_module("ui.tray_menu",
                                                                       build_tray_menu=build_tray_menu)}):
            app._build_tray_menu()
            app._build_tray_menu()                              # sign-in, plan or privacy change
        self.assertEqual(len(built), 1)
        tray = app.tray_menu
        self.assertIs(tray, built[0])
        icon.setContextMenu.assert_called_once_with(tray.menu)
        self.assertEqual(app.sig_setting_changed.slots, [tray.on_setting_changed])
        self.assertEqual(tray.calendars, [cal])
        self.assertEqual(tray.refreshed, 1)
        self.assertEqual(icon.setToolTip.call_count, 2)

    def test_a_broken_menu_still_leaves_open_and_quit(self):
        def broken(app, menu=None, custom=None):
            raise RuntimeError("no style")
        app = bind(SimpleNamespace(tray_icon=mock.MagicMock(), sig_setting_changed=Sig(),
                                   quit_app=mock.MagicMock()),
                   "_build_tray_menu", "_plain_tray_menu")
        with mock.patch.dict(sys.modules, {"ui.tray_menu": fake_module("ui.tray_menu", build_tray_menu=broken)}):
            app._build_tray_menu()
        self.assertIsNotNone(app._tray_menu)
        app.tray_icon.setContextMenu.assert_called_once()


# ── calendar reminders ────────────────────────────────────────────────────────

class FakeToast:
    made = []

    def __init__(self, main_app=None):
        self.record, self.dismissed, self.join, self.closed = Sig(), Sig(), Sig(), Sig()
        self.shown, self.hidden = [], 0
        FakeToast.made.append(self)

    def show_meeting(self, meeting, anchor=None):
        self.shown.append((meeting, anchor))
        return True

    def hide_reminder(self):
        self.hidden += 1

    def refresh_account(self):
        pass

    def on_setting_changed(self, key):
        pass

    def is_showing(self):
        return bool(self.shown)


MEETING = {"id": "ev1", "title": "Weekly product sync", "attendees": ["Aram", "Anna", "David"],
           "others": ["Anna", "David"], "link": "https://meet.google.com/abc", "call_type": "Google Meet"}


def calendar_app(pro=True):
    app = bind(opener(), "_on_calendar_remind", "_ensure_reminder", "_on_reminder_dismissed",
               "_on_reminder_record", "_on_reminder_join", "_on_reminder_closed",
               "_on_session_state_for_reminder", "record_from_calendar", "calendar_start_context",
               "note_calendar_start")
    app._reminder = None
    app.live_assist = None
    app.hints = []
    app.show_tray_hint = lambda t, m, on_click=None: app.hints.append((t, m))
    app.tray_icon = SimpleNamespace(geometry=lambda: "tray-rect")
    app.sig_auth_changed, app.sig_setting_changed = Sig(), Sig()
    app.calendar = SimpleNamespace(mark_prompted=mock.MagicMock())
    app.started = []

    def start(**kw):
        app.started.append(kw)
        return app.start_result
    app.start_result = (True, "", "")
    app.meeting_session = SimpleNamespace(is_busy=False, state_changed=Sig(), start=start)
    app.plan = {"pro": pro}
    app.is_pro = lambda: app.plan["pro"]
    app.upsells = []
    app._pro_upsell = lambda feature=None: app.upsells.append(feature)
    app._dialog_parent = lambda: None
    app.assist = 0
    app.show_live_assist = lambda: setattr(app, "assist", app.assist + 1)
    app._ensure_live_assist = mock.MagicMock()
    app.recorder = SimpleNamespace(fast_live_chunks=False)
    return app


class TestCalendarReminder(unittest.TestCase):
    def setUp(self):
        FakeToast.made = []
        self.mods = mock.patch.dict(sys.modules, {
            "ui.floating.meeting_reminder": fake_module("ui.floating.meeting_reminder", MeetingReminder=FakeToast)})
        self.mods.start()
        self.consent = mock.patch("ui.recording_prompts.ensure_recording_consent", return_value=True)
        self.consent_mock = self.consent.start()

    def tearDown(self):
        self.consent.stop()
        self.mods.stop()

    def test_one_toast_is_built_and_reused(self):
        app = calendar_app()
        app._on_calendar_remind(MEETING)
        app._on_calendar_remind(dict(MEETING, id="ev2"))
        self.assertEqual(len(FakeToast.made), 1)
        toast = app._reminder
        self.assertEqual([m["id"] for m, _ in toast.shown], ["ev1", "ev2"])
        self.assertEqual(toast.shown[0][1], "tray-rect")      # the screen with the tray icon
        self.assertEqual(app.hints, [])
        self.assertEqual(len(app.sig_auth_changed.slots), 1)
        self.assertEqual(len(app.sig_setting_changed.slots), 1)
        # A recording started elsewhere closes it.
        app.meeting_session.state_changed.emit("recording")
        self.assertEqual(toast.hidden, 1)

    def test_a_toast_that_cannot_show_falls_back_to_a_notification(self):
        app = calendar_app()
        app._ensure_reminder = mock.MagicMock(side_effect=RuntimeError("no screen"))
        app._on_calendar_remind(MEETING)
        self.assertEqual(app.hints, [("Record this meeting?", "Weekly product sync is starting.")])

    def test_not_this_one_keeps_it_asked(self):
        app = calendar_app()
        app._on_calendar_remind(MEETING)
        app._reminder.dismissed.emit(MEETING)
        app.calendar.mark_prompted.assert_called_once_with("ev1")

    def test_record_starts_the_session_with_the_meeting(self):
        app = calendar_app()
        app._on_calendar_remind(MEETING)
        app._reminder.record.emit(MEETING, False)
        self.assertEqual(app.started, [{"title": "Weekly product sync", "attendees": "Aram, Anna, David",
                                        "source": "calendar", "calendar_event_id": "ev1"}])
        self.assertEqual(app.opened, [("meetings", {"view": "live"})])
        self.assertEqual((app.assist, app.upsells), (0, []))
        self.consent_mock.assert_called_once_with(None, app)

    def test_record_with_live_assistant_opens_the_card(self):
        app = calendar_app()
        self.assertTrue(app.record_from_calendar(MEETING, with_assist=True))
        self.assertEqual(app.assist, 1)
        app._ensure_live_assist.assert_called_once_with()      # built before the start: it hears it
        self.assertIs(app.recorder.fast_live_chunks, True)      # live answers: short pieces
        # A refused start puts the pieces back to normal.
        app = calendar_app()
        app.start_result = (False, "busy", "A meeting is already being recorded or saved.")
        self.assertFalse(app.record_from_calendar(MEETING, with_assist=True))
        self.assertIs(app.recorder.fast_live_chunks, False)
        self.assertEqual(app.assist, 0)
        # Plain Record leaves them alone.
        app = calendar_app()
        app.record_from_calendar(MEETING)
        self.assertIs(app.recorder.fast_live_chunks, False)
        app._ensure_live_assist.assert_not_called()

    def test_free_users_get_the_pro_window_and_nothing_records(self):
        app = calendar_app(pro=False)
        self.assertFalse(app.record_from_calendar(MEETING))
        self.assertEqual(app.upsells, ["Meeting recording"])
        self.assertEqual(app.started, [])
        # Upgraded right there: it records.
        app._pro_upsell = lambda feature=None: app.plan.update(pro=True)
        self.assertTrue(app.record_from_calendar(MEETING))
        self.assertEqual(len(app.started), 1)

    def test_no_consent_or_busy_or_refused(self):
        app = calendar_app()
        self.consent_mock.return_value = False
        self.assertFalse(app.record_from_calendar(MEETING))
        self.assertEqual(app.started, [])
        self.consent_mock.return_value = True
        app.meeting_session.is_busy = True                    # one meeting at a time
        self.assertFalse(app.record_from_calendar(MEETING))
        self.assertEqual(app.opened, [("meetings", {})])
        app.meeting_session.is_busy = False
        app.opened.clear()
        app.start_result = (False, "device", "Can't open the microphone.")
        self.assertFalse(app.record_from_calendar(MEETING))
        self.assertEqual(app.opened, [("meetings", {"title": "Weekly product sync", "attendees": ["Anna", "David"],
                                                    "calendar_event_id": "ev1", "agenda": ""})])
        self.assertEqual(app.hints, [("Couldn't start the recording", "Can't open the microphone.")])


    def test_the_agenda_goes_only_with_live_assistant(self):
        meeting = dict(MEETING, agenda="Budget\nHiring", private=False)
        app = calendar_app()
        app._on_calendar_remind(meeting)
        app._reminder.record.emit(meeting, False)               # a plain Record: title and names only
        self.assertNotIn("agenda", app.started[0])
        self.assertEqual(app.tracked[-1], "calendar_meeting_started")
        app = calendar_app()
        app._on_calendar_remind(meeting)
        app._reminder.record.emit(meeting, True)                # Record + Live Assistant: the user sees it
        self.assertEqual(app.started[0]["agenda"], "Budget\nHiring")
        self.assertEqual(app.started[0]["attendees"], "Aram, Anna, David")
        app.calendar.mark_prompted.assert_called_with("ev1")    # asked: never again
        # A private event never has one, whichever button.
        app = calendar_app()
        app.record_from_calendar(dict(meeting, private=True), with_assist=True)
        self.assertNotIn("agenda", app.started[0])

    def test_a_refused_start_fills_the_start_card_with_the_agenda_the_user_can_read(self):
        meeting = dict(MEETING, agenda="Budget\nHiring")
        app = calendar_app()
        app.start_result = (False, "busy", "A meeting is already being recorded or saved.")
        self.assertFalse(app.record_from_calendar(meeting))     # plain: nothing was sent...
        self.assertNotIn("agenda", app.started[0])
        self.assertEqual(app.opened[0][1]["agenda"], "Budget\nHiring")   # ...but the card shows it
        app.calendar.mark_prompted.assert_not_called()

    def test_the_toast_signals_are_counted_without_any_text(self):
        app = calendar_app()
        app._on_calendar_remind(MEETING)
        toast = app._reminder
        names = []
        app.track = lambda event, props=None: names.append((event, props))
        toast.join.emit(MEETING)
        toast.dismissed.emit(MEETING)
        toast.closed.emit(MEETING, "expired")
        toast.closed.emit(MEETING, "hidden")
        self.assertEqual(names, [("calendar_reminder_action", {"action": "join"}),
                                 ("calendar_reminder_action", {"action": "dismiss"}),
                                 ("calendar_reminder_action", {"action": "expired"})])


# ── copy ──────────────────────────────────────────────────────────────────────

def _string_literals(path):
    """Every string literal in a Python file (comments and docstrings aside
    as far as a regex can tell: lines starting with # are skipped)."""
    out = []
    for line in open(path, encoding="utf-8"):
        if line.lstrip().startswith("#"):
            continue
        out.extend(m.group(2) for m in re.finditer(r'(["\'])(.*?)\1', line))
    return out


class TestCopy(unittest.TestCase):
    def test_no_old_names_in_main_strings(self):
        strings = _string_literals(os.path.join(ROOT, "main.py"))
        for old in ("Transcribe Pro", "Transcribe Files", "Live Assistance overlay", "History Log",
                    "Smart Action (free trial)", "free Smart Action(s) left", "Settings > Models"):
            self.assertFalse([s for s in strings if old in s], old)

    def test_no_per_use_smart_action_notice(self):
        src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        self.assertNotIn("free Smart Action(s) left", src)
        self.assertIn("entitlements.add_smart_action_use(self.auth)", src)   # still counted

    def test_hotkey_capture_is_exported(self):
        src = open(os.path.join(ROOT, "ui", "components", "__init__.py"), encoding="utf-8").read()
        self.assertIn("from ui.components.hotkey_capture import HotkeyCaptureButton", src)
        self.assertIn('"HotkeyCaptureButton"', src)


if __name__ == "__main__":
    unittest.main()
