"""Gusan 2.0 Smart Actions shortcut (D1-D5): Alt+R always pastes the exact
words, a Smart Action runs from its own shortcut (Alt+S by default), who may
start one, the schema-3 config migration, the recording pill's Smart mark and
the local AI model's keep rule.

Pure logic under test_core's stand-ins: controllers are SimpleNamespace
doubles with the real methods called through the class (no Qt, no audio)."""
import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main

SMART = main.actions.ACTION_SMART_AUTO
EXACT = main.actions.ACTION_TRANSCRIBE_ONLY


class _Inline:
    """threading.Thread that runs its target at start()."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


# ── who may start one (D3) ────────────────────────────────────────────────────

class TestShortcutGate(unittest.TestCase):
    def _app(self, tier="pro", used=0, rec=False):
        auth = SimpleNamespace(is_pro=tier == "pro", is_authenticated=tier != "guest",
                               user_id=None if tier == "guest" else "u1")
        app = SimpleNamespace(is_rec=rec, _hotkeys_suspended=False, _smart_next=False,
                              cfg={}, auth=auth, presses=[], upsells=[], gates=[])
        app.is_pro = lambda: tier == "pro"
        app._pro_upsell = lambda feature=None: app.upsells.append(feature)
        app.show_auth_gate = lambda: app.gates.append(1)
        app._on_hotkey = lambda: app.presses.append(app._smart_next)
        self._used = used
        return app

    def _press(self, app):
        with mock.patch.object(main.entitlements, "smart_actions_used", lambda auth=None: self._used):
            main.AppController._on_smart_hotkey(app)
        return app.presses, app.upsells, app.gates

    def test_the_gate_when_nothing_records(self):
        tries = main.entitlements.FREE_SMART_ACTION_TRIES
        for tier, used, want in (
                ("pro", 99, ([True], [], [])),                     # Pro: always
                ("free", 0, ([True], [], [])),                     # Free: unlimited
                ("free", tries - 1, ([True], [], [])),
                ("free", tries, ([True], [], [])),                 # the old 5-try cap is gone
                ("guest", 0, ([], [], [1]))):                      # guest: sign in first
            app = self._app(tier, used)
            self.assertEqual(self._press(app), want, (tier, used))
            self.assertFalse(app._smart_next)                      # only that one start

    def test_pressed_while_recording_it_just_stops(self):
        for tier in ("pro", "free", "guest"):
            app = self._app(tier, used=99, rec=True)
            self.assertEqual(self._press(app), ([False], [], []), tier)   # a stop, not a new kind

    def test_a_late_press_while_capturing_a_shortcut_does_nothing(self):
        app = self._app("guest")
        app._hotkeys_suspended = True
        self.assertEqual(self._press(app), ([], [], []))


# ── the take: Alt+R vs the Smart Actions shortcut (D1, D4) ───────────────────

class TestStart(unittest.TestCase):
    def _start(self, smart_next, **cfg):
        conf = {"backend": "local", "whisper_model": "base", "action_model": "rule_based",
                "output_action": "smart_auto"}           # a 1.9.x switch: ignored
        conf.update(cfg)
        app = SimpleNamespace(
            is_rec=False, _file_job_running=False, _smart_next=smart_next, _oneshot_action="stale",
            cfg=conf, overlay=mock.MagicMock(), loads=[], starts=[])
        app.recorder = SimpleNamespace(start_recording=lambda **kw: app.starts.append(kw))
        app.is_pro = lambda: True
        app._cloud_preflight_warn = lambda: None
        app._register_transient_keys = lambda: None
        app._unregister_transient_keys = lambda: None
        app._load_for_dictation = lambda name=None: app.loads.append(name)
        with mock.patch.object(main.threading, "Thread", _Inline):
            main.AppController._start(app)
        self.assertTrue(app.is_rec)
        return app

    def test_alt_r_is_the_exact_words_whatever_output_action_says(self):
        app = self._start(False)
        self.assertIsNone(app._oneshot_action)
        self.assertEqual(app.overlay.show_overlay.call_args, mock.call("recording", smart=False))

    def test_the_smart_shortcut_starts_a_smart_take_and_marks_the_pill(self):
        app = self._start(True)
        self.assertEqual(app._oneshot_action, SMART)
        args, kwargs = app.overlay.show_overlay.call_args
        self.assertEqual(kwargs, {"smart": True})
        self.assertEqual(app.starts, [{}])                      # Same as Dictation: a plain start
        self.assertEqual(app.loads, [None])                     # Dictation's model, while speaking

    def test_only_a_smart_take_prewarms_the_local_writer(self):
        loads = []
        with mock.patch.object(main.local_llm, "MODEL_CATALOG", {"qwen_3b": {}}), \
                mock.patch.object(main.local_llm, "model_downloaded", lambda m: True), \
                mock.patch.object(main.local_llm, "_load_model", loads.append), \
                mock.patch.object(main.actions, "normalize_action_model", lambda m: m):
            self._start(False, action_model="qwen_3b")              # output_action=smart_auto
            self.assertEqual(loads, [])
            self._start(True, action_model="qwen_3b")
            self.assertEqual(loads, ["qwen_3b"])


class _Recorder:
    def __init__(self, text="call David and book the room", backend=None):
        self.text = text
        self.backend_override = backend
        self.model_override = None
        self._fetching_model = False
        self._last_load_wait = 0.0
        self._cloud_capped = False

    def stop_recording(self):
        pass

    def transcribe(self):
        return self.text, "en"


class TestStopImpl(unittest.TestCase):
    """_stop_impl end to end with doubles: which takes run a Smart Action, the
    stop-time backstop and its hints, and how the result is pasted."""

    def _run(self, oneshot, tier="pro", used=0, backend=None, **cfg):
        conf = {"backend": "local", "whisper_model": "base", "output_action": "smart_auto",
                "save_history": True, "privacy_mode": False, "translate_target": "en",
                "analytics_enabled": False}
        conf.update(cfg)
        auth = SimpleNamespace(is_pro=tier == "pro", is_authenticated=tier != "guest",
                               user_id=None if tier == "guest" else "u1")
        app = SimpleNamespace(cfg=conf, auth=auth, _oneshot_action=oneshot, _job_seq=1, is_rec=True,
                              recorder=_Recorder(backend=backend), overlay=mock.MagicMock(),
                              hints=[], pastes=[], counted=[], saved=[], processed=[])
        app.overlay.call_soon = lambda fn, *a, **k: fn(*a, **k)
        app._show_model_wait = lambda: None
        app._note_local_wait = lambda *a, **k: None
        app.show_tray_hint = lambda title, text, on_click=None: app.hints.append((title, text))
        app._postprocess_transcript = lambda t: t
        app.is_pro = lambda: tier == "pro"
        app.sig_auth_changed = SimpleNamespace(emit=lambda: None)
        app._resolve_action_engine = lambda: ("rule_based", conf)
        app._wait_keys_released = lambda s=0: None
        app._paste_via_clipboard = lambda text, **kw: app.pastes.append((text, kw)) or True
        app._nudge_update_after_dictation = lambda: None
        app.notify_history_changed = lambda: None

        def process(text, mode, **kw):
            app.processed.append(mode)
            return "## To-do\n\n- [ ] Call David\n- [ ] Book the room"
        with mock.patch.object(main.actions, "process", process), \
                mock.patch.object(main.entitlements, "smart_actions_used", lambda auth=None: used), \
                mock.patch.object(main.entitlements, "add_smart_action_use",
                                  lambda auth=None: app.counted.append(1)), \
                mock.patch.object(main.hist, "save_entry",
                                  lambda text, lang, backend, **extra: app.saved.append((backend, extra))):
            main.AppController._stop_impl(app, 1)
        return app

    def test_alt_r_ignores_a_1_9_smart_auto_switch(self):
        app = self._run(None, output_action="smart_auto")
        self.assertEqual(app.processed, [])                         # no Smart Action ran
        self.assertEqual(app.pastes, [("call David and book the room", {})])   # exact words, plain
        self.assertEqual(app.saved, [("local", {})])

    def test_a_smart_take_runs_and_pastes_rich(self):
        app = self._run(SMART)
        self.assertEqual(app.processed, [SMART])
        text, kw = app.pastes[0]
        self.assertTrue(text.startswith("## To-do"))                # history/paste get the Markdown
        self.assertEqual(kw, {"rich": True})
        self.assertEqual(app.saved[0][1]["action"], SMART)
        self.assertEqual(app.counted, [])                           # Pro: nothing counted

    def test_free_smart_actions_are_not_counted(self):
        app = self._run(SMART, tier="free", used=1)
        self.assertEqual((app.processed, app.counted), ([SMART], []))

    def test_a_guest_gets_their_exact_words(self):
        tries = main.entitlements.FREE_SMART_ACTION_TRIES
        app = self._run(SMART, tier="free", used=tries)
        self.assertEqual(app.processed, [SMART])
        app = self._run(SMART, tier="guest")
        self.assertEqual(app.hints, [("Sign in to use Smart Actions", "Pasted your exact words.")])

    def test_history_records_the_backend_that_really_transcribed(self):
        app = self._run(SMART, backend="managed")                   # Gusan Cloud for this take
        self.assertEqual(app.saved[0][0], "managed")
        app = self._run(None)
        self.assertEqual(app.saved[0][0], "local")


# ── the config (D2) ───────────────────────────────────────────────────────────

class TestMigration(unittest.TestCase):
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

    def test_an_early_2_0_config_gets_alt_s(self):
        c = self._load({"config_schema_version": 2, "smart_actions_hotkey": ""})
        self.assertEqual(c["smart_actions_hotkey"], "alt+s")
        self.assertEqual(c["config_schema_version"], 3)

    def test_a_1_9_config_gets_alt_s(self):
        self.assertEqual(self._load({"language": "en"})["smart_actions_hotkey"], "alt+s")

    def test_alt_s_taken_by_another_shortcut_stays_unset(self):
        for disk in ({"config_schema_version": 2, "smart_actions_hotkey": "", "hotkey": "Alt+S"},
                     {"config_schema_version": 2, "smart_actions_hotkey": "",
                      "live_assist_hotkey": "alt+s"},
                     {"hotkey": "alt+s"}):                          # 1.9.x, no key at all
            self.assertEqual(self._load(disk)["smart_actions_hotkey"], "", disk)

    def test_a_chosen_shortcut_is_kept_and_schema_3_is_left_alone(self):
        c = self._load({"config_schema_version": 2, "smart_actions_hotkey": "ctrl+shift+s"})
        self.assertEqual(c["smart_actions_hotkey"], "ctrl+shift+s")
        c = self._load({"config_schema_version": 3, "smart_actions_hotkey": ""})
        self.assertEqual(c["smart_actions_hotkey"], "")             # set "" after migrating: kept

    def test_output_action_is_kept_for_1_9(self):
        c = self._load({"config_schema_version": 2, "output_action": "smart_auto"})
        self.assertEqual(c["output_action"], "smart_auto")           # never dropped


class TestTracking(unittest.TestCase):
    def test_settings_report_the_shortcut_as_set_not_its_keys(self):
        self.assertNotIn("output_action", main.AppController._TRACKED_SETTINGS)
        c = main.AppController.__new__(main.AppController)
        c.cfg = {"smart_actions_hotkey": "ctrl+shift+s", "smart_actions_speech_model": "small"}
        snap = main.AppController._settings_snapshot(c)
        self.assertIs(snap["smart_actions_hotkey"], True)
        self.assertEqual(snap["smart_actions_speech_model"], "small")
        c.cfg["smart_actions_hotkey"] = ""
        self.assertIs(main.AppController._settings_snapshot(c)["smart_actions_hotkey"], False)


# ── the local AI model after a meeting (D5) ──────────────────────────────────

class TestReleaseKeepRule(unittest.TestCase):
    def _release(self, **state):
        app = SimpleNamespace(cfg={"action_model": "qwen_3b", "output_action": "smart_auto"}, **state)
        with mock.patch.object(main.threading, "Thread", _Inline), \
                mock.patch.object(main.local_llm, "unload_idle") as llm, \
                mock.patch.dict(main.cfg, {"llm_idle_unload_min": 5}):
            main.AppController.release_models_after_session(app)
        return llm.call_args

    def test_the_writer_stays_only_for_a_smart_take_in_flight(self):
        self.assertEqual(self._release().kwargs, {"keep": None})         # the 1.9 switch: not kept
        self.assertEqual(self._release(_oneshot_action=SMART, is_rec=True).kwargs,
                         {"keep": "qwen_3b"})
        self.assertEqual(self._release(_oneshot_action=SMART, _busy=False, is_rec=False).kwargs,
                         {"keep": None})


if __name__ == "__main__":
    unittest.main()
