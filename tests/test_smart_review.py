"""Gusan 2.0 Smart Actions review fixes: a Smart take's cloud speech never
outlives Privacy mode (also in what the take reports), a Smart Actions press
during a meeting names Smart Actions and never opens the Pro window or the
sign-up sheet over it, and a fresh or 1.9.4 config ends up with Alt+S armed.

Pure logic under test_core's stand-ins (no Qt, no audio)."""
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


class TestPrivacyWinsOverASmartCloudTake(unittest.TestCase):
    def test_privacy_mode_turned_on_mid_take_keeps_the_rest_local(self):
        me = SimpleNamespace(backend_override="managed", model_override=None)
        with mock.patch.dict(main.cfg, {"backend": "local", "privacy_mode": False}):
            self.assertEqual(main.AudioRecorder._backend(me), "managed")
            main.cfg["privacy_mode"] = True                 # the tray switch, mid-take
            self.assertEqual(main.AudioRecorder._backend(me), "local")
            self.assertTrue(main.AudioRecorder._runs_locally(me))

    def test_a_local_pick_is_kept_in_privacy_mode(self):
        me = SimpleNamespace(backend_override="local", model_override="small")
        with mock.patch.dict(main.cfg, {"backend": "local", "privacy_mode": True}):
            self.assertEqual(main.AudioRecorder._backend(me), "local")
            self.assertEqual(main.AudioRecorder._speech_model(me), "small")


class TestMeetingHint(unittest.TestCase):
    def _press(self, smart):
        hints, starts = [], []
        app = SimpleNamespace(
            is_rec=False, _hotkeys_suspended=False, _busy=False, _smart_next=smart,
            cfg={"hotkey": "alt+r"}, meeting_session=SimpleNamespace(holds_recorder=True))
        app.show_tray_hint = lambda title, text, on_click=None: hints.append((title, text))
        app._start = lambda: starts.append(1)
        main.AppController._on_hotkey(app)
        self.assertEqual(starts, [])                        # never on top of a meeting
        return hints

    def test_the_smart_shortcut_names_smart_actions(self):
        (title, text), = self._press(smart=True)
        self.assertEqual(title, "Meeting Recording Active")
        self.assertEqual(text, "Smart Actions are paused while a meeting is recording. "
                               "Stop the meeting to use them again.")

    def test_alt_r_names_the_dictation_keys(self):
        (_title, text), = self._press(smart=False)
        self.assertIn("dictation is paused", text)
        self.assertNotIn("Smart Actions", text)


class TestSmartPressDuringAMeeting(unittest.TestCase):
    """The gate (D3) never opens the Pro window or the sign-up sheet over a
    meeting: no take can start, so the press only gets the quiet hint."""

    def _press(self, tier, used=0, meeting=True):
        hints, upsells, gates, starts = [], [], [], []
        auth = SimpleNamespace(is_pro=tier == "pro", is_authenticated=tier != "guest",
                               user_id=None if tier == "guest" else "u1")
        app = SimpleNamespace(
            is_rec=False, _hotkeys_suspended=False, _busy=False, _smart_next=False,
            cfg={"hotkey": "alt+r"}, auth=auth,
            meeting_session=SimpleNamespace(holds_recorder=meeting))
        app.is_pro = lambda: tier == "pro"
        app._pro_upsell = lambda feature=None: upsells.append(feature)
        app.show_auth_gate = lambda: gates.append(1)
        app.show_tray_hint = lambda title, text, on_click=None: hints.append(text)
        app._start = lambda: starts.append(app._smart_next)
        app._on_hotkey = lambda: main.AppController._on_hotkey(app)
        with mock.patch.object(main.entitlements, "smart_actions_used", lambda auth=None: used):
            main.AppController._on_smart_hotkey(app)
        self.assertFalse(app._smart_next)
        return hints, upsells, gates, starts

    def test_no_upsell_or_sign_up_over_a_meeting(self):
        tries = main.entitlements.FREE_SMART_ACTION_TRIES
        for tier, used in (("free", tries), ("guest", 0), ("free", 0), ("pro", 0)):
            hints, upsells, gates, starts = self._press(tier, used)
            self.assertEqual((upsells, gates, starts), ([], [], []), tier)
            self.assertEqual(len(hints), 1, tier)
            self.assertTrue(hints[0].startswith("Smart Actions are paused"), tier)

    def test_without_a_meeting_the_gate_is_unchanged(self):
        tries = main.entitlements.FREE_SMART_ACTION_TRIES
        self.assertEqual(self._press("free", tries, meeting=False)[1], ["Smart Actions"])
        self.assertEqual(self._press("guest", meeting=False)[2], [1])
        self.assertEqual(self._press("free", 1, meeting=False)[3], [True])


class TestWhatASmartTakeReports(unittest.TestCase):
    """_stop_impl reports the backend that really ran: a Smart cloud pick
    with Privacy mode on (turned on mid-take) ran on this computer."""

    def _run(self, override, privacy):
        conf = {"backend": "local", "whisper_model": "base", "save_history": True,
                "privacy_mode": privacy, "translate_target": "en", "analytics_enabled": False}
        rec = SimpleNamespace(backend_override=override, model_override=None, _fetching_model=False,
                              _last_load_wait=0.0, _cloud_capped=False,
                              stop_recording=lambda: None, transcribe=lambda: ("hello there", "en"))
        app = SimpleNamespace(cfg=conf, auth=None, _oneshot_action=None, _job_seq=1, is_rec=True,
                              recorder=rec, overlay=mock.MagicMock(), waits=[], tracked=[], saved=[])
        app.overlay.call_soon = lambda fn, *a, **k: fn(*a, **k)
        app._show_model_wait = lambda: None
        app._note_local_wait = lambda ctx, s, model=None, backend=None: app.waits.append(backend)
        app._postprocess_transcript = lambda t: t
        app.is_pro = lambda: True
        app._wait_keys_released = lambda s=0: None
        app._paste_via_clipboard = lambda text, **kw: True
        app._nudge_update_after_dictation = lambda: None
        app.notify_history_changed = lambda: None

        def track(event, props=None, *a, **k):
            if event == "transcription_completed":
                app.tracked.append(props["backend"])
        with mock.patch.object(main.telemetry, "track", track), \
                mock.patch.object(main.hist, "save_entry",
                                  lambda text, lang, backend, **extra: app.saved.append(backend)):
            main.AppController._stop_impl(app, 1)
        return app

    def test_a_cloud_pick_in_privacy_mode_reports_this_computer(self):
        app = self._run("managed", privacy=True)
        self.assertEqual((app.waits, app.tracked, app.saved), (["local"], ["local"], []))

    def test_otherwise_the_takes_own_backend(self):
        app = self._run("managed", privacy=False)
        self.assertEqual((app.waits, app.tracked, app.saved), (["managed"], ["managed"], ["managed"]))
        app = self._run("local", privacy=True)                        # a local pick: kept
        self.assertEqual((app.waits, app.tracked), (["local"], ["local"]))


class TestAltSArmedEndToEnd(unittest.TestCase):
    """A fresh install and a 1.9.4 config.json both load with Alt+S, and that
    shortcut really registers (next to Alt+R); a clash leaves nothing armed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(main, "CONFIG_PATH", os.path.join(self._tmp.name, "config.json"))
        p.start()
        self.addCleanup(p.stop)

    def _arm(self, disk=None):
        if disk is not None:
            with open(main.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(disk, f)
        cfg = main.load_config()
        added = []
        kbd = SimpleNamespace(add_hotkey=lambda hk, fn, **kw: added.append(hk),
                              remove_hotkey=lambda hk: None)
        app = SimpleNamespace(cfg=cfg, _registered_smart_hotkey=None, _smart_listener=None)
        app._release_smart_hotkey = main.AppController._release_smart_hotkey.__get__(app)
        with mock.patch.dict(sys.modules, {"keyboard": kbd}), \
                mock.patch.object(main.sys, "platform", "win32"):
            ok = main.AppController._setup_smart_hotkey.__get__(app)(cfg.get("smart_actions_hotkey", ""))
        return cfg, ok, added

    def test_a_fresh_install(self):
        cfg, ok, added = self._arm()                                  # no config.json yet
        self.assertEqual((cfg["smart_actions_hotkey"], cfg["hotkey"]), ("alt+s", "alt+r"))
        self.assertEqual((ok, added), (True, ["alt+s"]))

    def test_a_1_9_4_config(self):
        cfg, ok, added = self._arm({"hotkey": "alt+r", "live_assist_hotkey": "ctrl+alt+a",
                                    "output_action": "smart_auto", "config_schema_version": 1,
                                    "whisper_model": "small"})
        self.assertEqual((cfg["smart_actions_hotkey"], cfg["config_schema_version"]), ("alt+s", 3))
        self.assertEqual(cfg["output_action"], "smart_auto")          # kept for 1.9.x (never read)
        self.assertEqual((ok, added), (True, ["alt+s"]))

    def test_a_1_9_4_config_whose_dictation_key_is_alt_s(self):
        cfg, ok, added = self._arm({"hotkey": "alt+s", "config_schema_version": 1})
        self.assertEqual(cfg["smart_actions_hotkey"], "")             # the page shows "Set shortcut"
        self.assertEqual((ok, added), (False, []))


if __name__ == "__main__":
    unittest.main()
