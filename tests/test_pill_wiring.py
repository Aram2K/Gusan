"""How main._stop_impl tells the dictation pill what is happening: the speech
model before it transcribes, what a Smart Action writes (and with what)
BEFORE the writer runs, and the kind in the "done" line.

Pure logic under test_core's stand-ins: the controller is a SimpleNamespace
double with the real _stop_impl called through the class (no Qt, no audio);
the overlay double logs its calls in order."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main
import pill_copy
import engine_choice  # noqa: F401  (in the app the overlay warms it when a take starts)

SMART = main.actions.ACTION_SMART_AUTO


class _Overlay:
    """Logs each pill call - as main makes them, through call_soon."""

    def __init__(self, log):
        self.log = log

    def call_soon(self, fn, *a, **k):
        fn(*a, **k)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: self.log.append((name, a, k))


class _Recorder:
    def __init__(self, text, backend=None, model=None):
        self.text = text
        self.backend_override = backend
        self.model_override = model
        self._fetching_model = False
        self._last_load_wait = 0.0
        self._cloud_capped = False

    def stop_recording(self):
        pass

    def transcribe(self):
        return self.text, "en"


class TestPillWiring(unittest.TestCase):
    def _run(self, text, oneshot=SMART, tier="pro", used=0, backend=None, model=None,
             pasted=True, **cfg):
        conf = {"backend": "local", "whisper_model": "base", "save_history": False,
                "privacy_mode": False, "translate_target": "en", "analytics_enabled": False}
        conf.update(cfg)
        log = []
        auth = SimpleNamespace(is_pro=tier == "pro", is_authenticated=tier != "guest",
                               user_id=None if tier == "guest" else "u1")
        app = SimpleNamespace(cfg=conf, auth=auth, _oneshot_action=oneshot, _job_seq=1, is_rec=True,
                              recorder=_Recorder(text, backend, model), overlay=_Overlay(log),
                              hints=[])
        app._show_model_wait = lambda: None
        app._note_local_wait = lambda *a, **k: None
        app.show_tray_hint = lambda title, text, on_click=None: app.hints.append(title)
        app._postprocess_transcript = lambda t: t
        app.is_pro = lambda: tier == "pro"
        app.sig_auth_changed = SimpleNamespace(emit=lambda: None)
        app._resolve_action_engine = lambda: ("managed", conf)
        app._wait_keys_released = lambda s=0: None
        app._paste_via_clipboard = lambda text, **kw: pasted
        app._nudge_update_after_dictation = lambda: None
        app.notify_history_changed = lambda: None

        def process(text, mode, **kw):
            log.append(("process", (mode,), {}))
            return "done text"
        with mock.patch.object(main.actions, "process", process), \
                mock.patch.object(main.entitlements, "smart_actions_used", lambda auth=None: used), \
                mock.patch.object(main.entitlements, "add_smart_action_use", lambda auth=None: None):
            main.AppController._stop_impl(app, 1)
        return log

    @staticmethod
    def _names(log):
        return [name for name, _a, _k in log]

    def _call(self, log, name):
        calls = [(a, k) for n, a, k in log if n == name]
        self.assertEqual(len(calls), 1, (name, log))
        return calls[0]

    def test_a_smart_take_says_what_it_writes_before_the_writer_runs(self):
        log = self._run("make a todo list call David and book the room")
        names = self._names(log)
        self.assertLess(names.index("set_task"), names.index("process"))
        self.assertLess(names.index("set_task"), names.index("set_state", names.index("set_state") + 1))
        args, _k = self._call(log, "set_task")
        self.assertEqual(args, ("list", "", {"writer": "Gusan Cloud", "writer_local": False}))
        self.assertEqual(self._call(log, "show_done"), ((True,), {"kind": "list"}))

    def test_a_translation_names_its_language(self):
        log = self._run("translate to Spanish see you at five")
        self.assertEqual(self._call(log, "set_task")[0][:2], ("translate", "Spanish"))
        self.assertEqual(self._call(log, "show_done"), ((True,), {"kind": "translate"}))

    def test_no_clear_ask_is_thinking_and_done(self):
        log = self._run("the meeting is at three pm in room b", pasted=False)
        self.assertEqual(self._call(log, "set_task")[0][:2], (None, ""))
        self.assertEqual(self._call(log, "show_done"), ((False,), {"kind": None}))

    def test_a_local_writer_is_named(self):
        with mock.patch.object(main.local_llm, "model_downloaded", lambda m: True):
            log = self._run("write an email to John the report is ready", tier="free",
                            action_model="qwen_tiny")
        self.assertEqual(self._call(log, "set_task")[0],
                         ("email", "", {"writer": "Qwen 1.5B", "writer_local": True}))

    def test_the_speech_model_comes_before_transcribing(self):
        from ui import overlay as ov_mod
        log = self._run("hello there", oneshot=None, whisper_model="small")
        names = self._names(log)
        self.assertLess(names.index("set_speech"), names.index("set_state"))
        self.assertEqual(log[names.index("set_state")][1], (ov_mod.TRANSCRIBING,))
        self.assertEqual(self._call(log, "set_speech")[0],
                         ({"speech": "Whisper Small", "speech_local": True},))
        # A Smart take on Gusan Cloud says so.
        log = self._run("hello there", backend="managed")
        self.assertEqual(self._call(log, "set_speech")[0], ({"speech": "Gusan Cloud", "speech_local": False},))

    def test_dictation_has_no_task_and_a_plain_done(self):
        log = self._run("make a todo list call David", oneshot=None)
        self.assertNotIn("set_task", self._names(log))
        self.assertNotIn("process", self._names(log))
        self.assertEqual(self._call(log, "show_done"), ((True,), {}))

    def test_tries_that_ran_out_paste_the_exact_words_plainly(self):
        tries = main.entitlements.FREE_SMART_ACTION_TRIES
        log = self._run("write an email to John", tier="free", used=tries)
        self.assertNotIn("set_task", self._names(log))
        self.assertEqual(self._call(log, "show_done"), ((True,), {}))

    def test_the_pill_words_never_break_a_take(self):
        with mock.patch("engine_choice.runs_on", side_effect=RuntimeError("boom")):
            log = self._run("make a todo list call David")
        self.assertEqual(self._call(log, "set_task")[0], ("list", "", {}))
        self.assertIn("process", self._names(log))
        self.assertEqual(self._call(log, "show_done"), ((True,), {"kind": "list"}))

    def test_a_take_never_waits_for_an_import(self):
        # engine_choice not loaded yet (pill_copy.warm hasn't landed): the
        # pill gets the words without a writer name, nothing is imported.
        with mock.patch.dict(sys.modules, {"engine_choice": None}):
            log = self._run("write an email to John the report is ready", backend="local", model="small")
            self.assertIsNone(sys.modules["engine_choice"])
        self.assertEqual(self._call(log, "set_task")[0], ("email", "", {}))
        self.assertEqual(self._call(log, "set_speech")[0], ({"speech": "Whisper Small", "speech_local": True},))
        self.assertIn("process", self._names(log))

    def test_task_is_cheap(self):
        import time
        app = SimpleNamespace(cfg={"action_model": "rule_based"}, is_pro=lambda: True,
                              auth=SimpleNamespace(is_authenticated=True))
        pill_copy.task("warm up", app)
        start = time.perf_counter()
        for _ in range(50):
            pill_copy.task("write an email to John that the Q3 report is ready", app)
        self.assertLess((time.perf_counter() - start) / 50, 0.005)        # well under 5 ms


if __name__ == "__main__":
    unittest.main()
