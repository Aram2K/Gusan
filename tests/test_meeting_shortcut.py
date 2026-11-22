"""The meeting shortcut (Alt+M; Control+M on a Mac): its config key and the heal for older configs, the
registration (Windows and the pynput path macOS uses) that never shares another shortcut's keys, what a
press does (main._on_meeting_hotkey: Record as the Meetings page's button, or open the meeting that is
recording), and settings_apply's checks when it - or another shortcut - changes.

Pure logic under test_core's stand-ins: controllers are SimpleNamespace doubles with the real methods
called through the class (no Qt, no audio). The page side is tests_qt/test_meeting_shortcut_ui.py."""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main
import settings_apply

MEETING = main.hotkeys.MEETING_DEFAULT


def bind(app, *names):
    for name in names:
        setattr(app, name, getattr(main.AppController, name).__get__(app))
    return app


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(main, "CONFIG_PATH", os.path.join(self._tmp.name, "config.json"))
        p.start()
        self.addCleanup(p.stop)

    def _load(self, disk):
        with open(main.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(disk, f)
        return main.load_config()

    def test_the_default_is_alt_m_and_control_m_on_a_mac(self):
        self.assertEqual(main.DEFAULT["meeting_hotkey"], MEETING)
        self.assertEqual(MEETING, "ctrl+m" if main.hotkeys.IS_MAC else "alt+m")

    def test_an_older_config_gets_it(self):
        c = self._load({"config_schema_version": 4, "hotkey": "alt+r"})
        self.assertEqual(c["meeting_hotkey"], MEETING)

    def test_taken_by_another_shortcut_it_stays_unset(self):
        for key in ("hotkey", "live_assist_hotkey", "smart_actions_hotkey"):
            disk = {"config_schema_version": 4, key: MEETING.upper().replace("+", " + ")}
            self.assertEqual(self._load(disk)["meeting_hotkey"], "", key)

    def test_a_chosen_or_cleared_shortcut_is_kept(self):
        self.assertEqual(self._load({"meeting_hotkey": "ctrl+shift+m"})["meeting_hotkey"], "ctrl+shift+m")
        self.assertEqual(self._load({"meeting_hotkey": ""})["meeting_hotkey"], "")


class TestRegistration(unittest.TestCase):
    def _app(self, **cfg):
        base = {"hotkey": "alt+r", "live_assist_hotkey": "ctrl+alt+a", "smart_actions_hotkey": "alt+s"}
        base.update(cfg)
        app = SimpleNamespace(cfg=base, _registered_meeting_hotkey=None, _meeting_listener=None,
                              sig_meeting_hotkey=SimpleNamespace(emit=lambda: app.pressed.append(1)), pressed=[],
                              _to_pynput_hotkey=main.AppController._to_pynput_hotkey)
        return bind(app, "_setup_meeting_hotkey", "_release_meeting_hotkey")

    def test_windows_takes_only_free_key_combos(self):
        added, removed, fns = [], [], []
        kbd = SimpleNamespace(add_hotkey=lambda hk, fn, **kw: (added.append(hk), fns.append(fn)),
                              remove_hotkey=lambda hk: removed.append(hk))
        app = self._app()
        with mock.patch.dict(sys.modules, {"keyboard": kbd}), mock.patch.object(main.sys, "platform", "win32"), \
                self.assertLogs("transcribe", "INFO") as logs:
            for refused in ("", "Alt+R", "ctrl+alt+a", "alt+s", "m", "mouse:middle"):
                self.assertFalse(app._setup_meeting_hotkey(refused), refused)
            self.assertEqual(added, [])
            self.assertTrue(app._setup_meeting_hotkey("Alt + M"))
            self.assertEqual((added, app._registered_meeting_hotkey), (["alt+m"], "alt+m"))
            fns[0]()                                          # the keys pressed: the signal
            self.assertEqual(app.pressed, [1])
            app._setup_meeting_hotkey("")                     # cleared: the hook goes
        self.assertEqual(removed, ["alt+m"])
        self.assertIsNone(app._registered_meeting_hotkey)
        self.assertIn("Registered meeting hotkey: alt+m", "\n".join(logs.output))

    def test_a_mac_listens_through_pynput(self):
        made = []

        class GlobalHotKeys:
            def __init__(self, mapping):
                self.mapping, self.daemon, self.started, self.stopped = mapping, False, False, False
                made.append(self)

            def start(self):
                self.started = True

            def stop(self):
                self.stopped = True

        pynput = SimpleNamespace(keyboard=SimpleNamespace(GlobalHotKeys=GlobalHotKeys))
        app = self._app(hotkey="ctrl+r", smart_actions_hotkey="ctrl+s")
        with mock.patch.dict(sys.modules, {"pynput": pynput, "pynput.keyboard": pynput.keyboard}), \
                mock.patch.object(main.sys, "platform", "darwin"), \
                mock.patch.object(main.hotkeys, "prepare_pynput_listeners", lambda: True):
            self.assertFalse(app._setup_meeting_hotkey("ctrl+r"))        # dictation's keys
            self.assertTrue(app._setup_meeting_hotkey("ctrl+m"))
        self.assertEqual(list(made[0].mapping), ["<ctrl>+m"])
        self.assertTrue(made[0].started and made[0].daemon)
        made[0].mapping["<ctrl>+m"]()
        self.assertEqual(app.pressed, [1])
        app._release_meeting_hotkey()
        self.assertTrue(made[0].stopped)
        self.assertIsNone(app._meeting_listener)

    def test_rebinding_registers_it_again(self):
        app = SimpleNamespace(cfg={"hotkey": "alt+r", "live_assist_hotkey": "", "smart_actions_hotkey": "alt+s",
                                   "meeting_hotkey": "alt+m"}, calls=[])
        app._setup_hotkey = lambda hk: app.calls.append(("hotkey", hk))
        app._setup_assist_hotkey = lambda hk: app.calls.append(("assist", hk))
        app._setup_smart_hotkey = lambda hk: app.calls.append(("smart", hk))
        app._setup_meeting_hotkey = lambda hk: app.calls.append(("meeting", hk))
        main.AppController.apply_tray_bindings(app)
        self.assertEqual(app.calls[-1], ("meeting", "alt+m"))

    def test_capturing_a_new_shortcut_pauses_it(self):
        app = SimpleNamespace(_hotkeys_suspended=False, calls=[])
        app._unregister_kbd_hotkey = lambda: None
        app._unregister_mouse_listener = lambda: None
        app._setup_assist_hotkey = lambda hk: None
        app._release_smart_hotkey = lambda: None
        app._release_meeting_hotkey = lambda: app.calls.append("released")
        main.AppController.suspend_hotkeys(app, True)
        self.assertEqual(app.calls, ["released"])


class TestPress(unittest.TestCase):
    """What Alt+M does: Record, exactly as the Meetings page's button (its on_open(record=True) runs the
    start card's settings, the recording notice and every check), or the meeting already recording."""

    def _app(self, phase="idle", suspended=False, session=True):
        app = SimpleNamespace(_hotkeys_suspended=suspended, _quitting=False, opened=[], pages=[],
                              meeting_session=SimpleNamespace(phase=phase) if session else None)
        win = SimpleNamespace(open=lambda route, **o: app.pages.append((route, o)))
        app.show_main = lambda route=None, **o: app.opened.append((route, o)) or win
        return app

    def test_idle_it_presses_record_on_the_meetings_page(self):
        for app in (self._app("idle"), self._app("done"), self._app(session=False)):
            main.AppController._on_meeting_hotkey(app)
            self.assertEqual(app.opened, [("meetings", {})])           # on screen first (the notice needs it)
            self.assertEqual(app.pages, [("meetings", {"record": True})])

    def test_while_one_records_it_opens_that_meeting_and_never_stops_it(self):
        for phase in ("recording", "paused"):
            app = self._app(phase)
            main.AppController._on_meeting_hotkey(app)
            self.assertEqual(app.opened, [("meetings", {"view": "live"})], phase)
            self.assertEqual(app.pages, [], phase)                     # no Record press

    def test_nothing_while_a_shortcut_is_captured_or_the_app_quits(self):
        app = self._app(suspended=True)
        main.AppController._on_meeting_hotkey(app)
        app2 = self._app()
        app2._quitting = True
        main.AppController._on_meeting_hotkey(app2)
        self.assertEqual(app.opened + app.pages + app2.opened + app2.pages, [])

    def test_a_window_that_could_not_open_does_nothing_more(self):
        app = self._app()
        app.show_main = lambda route=None, **o: None
        main.AppController._on_meeting_hotkey(app)                     # no error
        self.assertEqual(app.pages, [])


class FakeApp:
    def __init__(self, meeting_ok=True, **cfg):
        self.cfg = {"hotkey": "alt+r", "live_assist_hotkey": "ctrl+alt+a", "smart_actions_hotkey": "alt+s",
                    "meeting_hotkey": "alt+m"}
        self.cfg.update(cfg)
        self.meeting_ok, self.calls, self.saves = meeting_ok, [], 0
        self.sig_setting_changed = SimpleNamespace(emitted=[])
        self.sig_setting_changed.emit = self.sig_setting_changed.emitted.append
        self.tray_icon = None

    def _setup_hotkey(self, hk):
        self.calls.append(("hotkey", hk))
        return True

    def _setup_assist_hotkey(self, hk):
        self.calls.append(("assist", hk))
        return True

    def _setup_smart_hotkey(self, hk):
        self.calls.append(("smart", hk))
        return True

    def _setup_meeting_hotkey(self, hk):
        self.calls.append(("meeting", hk))
        return self.meeting_ok if not callable(self.meeting_ok) else self.meeting_ok(hk)

    def schedule_config_save(self):
        self.saves += 1


class TestSettingsApply(unittest.TestCase):
    KEY = "meeting_hotkey"

    def test_a_new_shortcut_registers_and_is_announced(self):
        app = FakeApp()
        self.assertTrue(settings_apply.apply(app, {self.KEY: "Ctrl + Shift + M"})[self.KEY])
        self.assertEqual(app.cfg[self.KEY], "ctrl+shift+m")
        self.assertEqual(app.calls, [("meeting", "ctrl+shift+m")])
        self.assertEqual((app.sig_setting_changed.emitted, app.saves), ([self.KEY], 1))

    def test_another_shortcuts_keys_are_refused(self):
        for keys in ("Alt+R", "ctrl+alt+a", "alt+s", "m", "mouse:middle"):
            app = FakeApp()
            self.assertFalse(settings_apply.apply(app, {self.KEY: keys})[self.KEY], keys)
            self.assertEqual((app.cfg[self.KEY], app.calls), ("alt+m", []), keys)

    def test_the_other_shortcuts_cant_take_its_keys(self):
        app = FakeApp()
        for key in ("hotkey", "live_assist_hotkey", "smart_actions_hotkey"):
            self.assertFalse(settings_apply.apply(app, {key: "Alt + M"})[key], key)
        self.assertEqual((app.cfg["hotkey"], app.cfg["live_assist_hotkey"], app.cfg["smart_actions_hotkey"]),
                         ("alt+r", "ctrl+alt+a", "alt+s"))
        self.assertEqual(app.calls, [])

    def test_one_that_wont_register_rolls_back(self):
        app = FakeApp(meeting_ok=lambda hk: hk != "ctrl+shift+m")
        self.assertFalse(settings_apply.apply(app, {self.KEY: "ctrl+shift+m"})[self.KEY])
        self.assertEqual(app.cfg[self.KEY], "alt+m")                         # the old one is kept
        self.assertEqual(app.calls, [("meeting", "ctrl+shift+m"), ("meeting", "alt+m")])
        self.assertEqual(app.sig_setting_changed.emitted, [])

    def test_clearing_it_is_ok_and_frees_its_keys(self):
        app = FakeApp(meeting_ok=False)
        self.assertTrue(settings_apply.apply(app, {self.KEY: ""})[self.KEY])
        self.assertEqual(app.cfg[self.KEY], "")
        self.assertTrue(settings_apply.apply(app, {"smart_actions_hotkey": "alt+m"})["smart_actions_hotkey"])

    def test_another_shortcut_changing_registers_it_again(self):
        app = FakeApp()
        self.assertTrue(settings_apply.apply(app, {"smart_actions_hotkey": "ctrl+shift+s"})["smart_actions_hotkey"])
        self.assertEqual(app.calls, [("smart", "ctrl+shift+s"), ("meeting", "alt+m")])

    def test_keys_trading_places_let_go_first(self):
        app = FakeApp()
        res = settings_apply.apply(app, {"smart_actions_hotkey": "alt+m", self.KEY: "alt+s"})
        self.assertEqual(res, {"smart_actions_hotkey": True, self.KEY: True})
        self.assertEqual(app.calls, [("meeting", ""), ("smart", "alt+m"), ("meeting", "alt+s")])
