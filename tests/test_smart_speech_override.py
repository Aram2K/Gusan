"""Gusan 2.0 D8: a Smart Action can transcribe with its own speech model
(cfg smart_actions_speech_model: "" same as Dictation, "cloud" Gusan Cloud,
or a Whisper id). The recorder takes it per take (start_recording speech=),
every other start resets it, the pieces and the tail route by it, and
Dictation's model is loaded again afterwards.

Pure logic under test_core's stand-ins (no Qt, no audio, nothing loaded)."""
import os
import sys
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import engine_choice
import main

SMART = main.actions.ACTION_SMART_AUTO


class _Inline:
    """threading.Thread that runs its target at start()."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


# ── the recorder ──────────────────────────────────────────────────────────────

class TestRecorderOverride(unittest.TestCase):
    @mock.patch("threading.Thread")
    def test_a_start_sets_it_and_every_other_start_resets_it(self, _thread):
        rec = main.AudioRecorder()
        rec.audio = mock.MagicMock()
        self.assertEqual((rec.backend_override, rec.model_override), (None, None))
        try:
            with mock.patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": 1024,
                                            "input_device_index": None}):
                rec.start_recording(speech=("local", "small"))
                self.assertEqual((rec.backend_override, rec.model_override), ("local", "small"))
                rec.start_recording()                                   # Alt+R
                self.assertEqual((rec.backend_override, rec.model_override), (None, None))
                rec.start_recording(speech=("managed", None))
                self.assertEqual((rec.backend_override, rec.model_override), ("managed", None))
                rec.start_recording(language="de")                     # a meeting / Live Assistant
                self.assertEqual((rec.backend_override, rec.model_override), (None, None))
        finally:
            rec.recording = False

    def test_helpers_fall_back_to_the_global_settings(self):
        me = SimpleNamespace()                                          # built without __init__
        with mock.patch.dict(main.cfg, {"backend": "mistral", "whisper_model": "medium"}):
            self.assertEqual(main.AudioRecorder._backend(me), "mistral")
            self.assertEqual(main.AudioRecorder._speech_model(me), "medium")
            me.backend_override, me.model_override = "local", "small"
            self.assertEqual(main.AudioRecorder._backend(me), "local")
            self.assertEqual(main.AudioRecorder._speech_model(me), "small")
            me.backend_override = mock.MagicMock()                      # never a stray object
            self.assertEqual(main.AudioRecorder._backend(me), "mistral")


def _chunk_me(**kw):
    calls = []
    me = SimpleNamespace(
        _chunk_lock=threading.Lock(), _chunk_results={}, _chunk_idx=1, _chunk_errors=[],
        on_partial=None, on_lang_detected=None, on_chunk_complete=None,
        _run_managed=lambda a: calls.append("managed") or ("cloud", "en"),
        _run_google=lambda a: calls.append("google") or ("g", "en"),
        _run_mistral=lambda a: calls.append("mistral") or ("m", "en"),
        _run_local=lambda a: calls.append("local") or ("here", "en"))
    for k, v in kw.items():
        setattr(me, k, v)
    return me, calls


class TestRouting(unittest.TestCase):
    def test_pieces_go_where_the_take_says(self):
        for cfg_backend, override, want in (("local", "managed", "managed"),
                                            ("managed", "local", "local"),
                                            ("mistral", "local", "local"),
                                            ("managed", None, "managed"),
                                            ("local", None, "local")):
            me, calls = _chunk_me(backend_override=override)
            with mock.patch.dict(main.cfg, {"backend": cfg_backend, "google_api_key": "",
                                            "mistral_api_key": "k"}):
                main.AudioRecorder._transcribe_chunk(me, [0.0], 0)
            self.assertEqual(calls, [want], (cfg_backend, override))
            self.assertEqual(me._chunk_errors, [])

    def test_runs_locally_follows_the_take(self):
        with mock.patch.dict(main.cfg, {"backend": "managed"}):
            self.assertTrue(main.AudioRecorder._runs_locally(SimpleNamespace(backend_override="local")))
            self.assertFalse(main.AudioRecorder._runs_locally(SimpleNamespace(backend_override=None)))
        with mock.patch.dict(main.cfg, {"backend": "local"}):
            self.assertFalse(main.AudioRecorder._runs_locally(SimpleNamespace(backend_override="managed")))

    def test_a_cloud_take_that_ran_locally_is_remembered(self):
        audio = [0.0] * 8000
        with mock.patch.dict(main.cfg, {"backend": "local", "sample_rate": 16000}):
            me = SimpleNamespace(backend_override="managed", _run_local_once=lambda a: ("x", "en"))
            main.AudioRecorder._run_local(me, audio)
            self.assertTrue(hasattr(me, "_local_fallback_at"))
        with mock.patch.dict(main.cfg, {"backend": "managed", "sample_rate": 16000}):
            me = SimpleNamespace(backend_override="local", _run_local_once=lambda a: ("x", "en"))
            main.AudioRecorder._run_local(me, audio)
            self.assertFalse(hasattr(me, "_local_fallback_at"))        # a local pick, not a fallback

    def _lease_me(self, fail_on_cuda=False, **kw):
        asked = []
        state = {"device": "cuda" if fail_on_cuda else "cpu"}

        @contextmanager
        def use_model(name=None):
            asked.append(name)
            yield SimpleNamespace(name=name, device=state["device"])

        def run_with(model, audio):
            if model.device == "cuda":
                raise RuntimeError("CUDA failed with error out of memory")
            return model.name, "en"

        def failed_mid_use(failed, name, err):
            state["device"] = "cpu"                                  # the retry builds a CPU one
        me = SimpleNamespace(use_model=use_model, _run_local_with=run_with,
                             _gpu_failed_mid_use=failed_mid_use, **kw)
        return me, asked

    def test_the_local_model_is_the_takes_own_also_on_a_gpu_retry(self):
        with mock.patch.dict(main.cfg, {"whisper_model": "base"}):
            me, asked = self._lease_me(model_override="small")
            self.assertEqual(main.AudioRecorder._run_local_once(me, None), ("small", "en"))
            self.assertEqual(asked, ["small"])
            me, asked = self._lease_me()                             # no override: Dictation's
            self.assertEqual(main.AudioRecorder._run_local_once(me, None), ("base", "en"))
            # The GPU ran out of memory: the SAME model again on the processor
            # (it used to fall back to Dictation's model silently).
            with mock.patch.object(main.logger, "warning", lambda *a, **k: None):
                me, asked = self._lease_me(fail_on_cuda=True, model_override="large-v3")
                self.assertEqual(main.AudioRecorder._run_local_once(me, None), ("large-v3", "en"))
            self.assertEqual(asked, ["large-v3", "large-v3"])


# ── the controller ────────────────────────────────────────────────────────────

class TestSmartTakeStart(unittest.TestCase):
    def _start(self, smart_next=True, pro=True, **cfg):
        conf = {"backend": "local", "whisper_model": "base", "action_model": "rule_based",
                "privacy_mode": False}
        conf.update(cfg)
        app = SimpleNamespace(is_rec=False, _file_job_running=False, _smart_next=smart_next,
                              _oneshot_action=None, cfg=conf, overlay=mock.MagicMock(),
                              starts=[], loads=[], pro_asked=[])
        app.recorder = SimpleNamespace(start_recording=lambda **kw: app.starts.append(kw))

        def is_pro():
            app.pro_asked.append(1)
            return pro
        app.is_pro = is_pro
        app._cloud_preflight_warn = lambda: None
        app._register_transient_keys = lambda: None
        app._unregister_transient_keys = lambda: None
        app._load_for_dictation = lambda name=None: app.loads.append(name)
        with mock.patch.object(main.threading, "Thread", _Inline), \
                mock.patch.object(main, "model_downloaded", lambda name: True):
            main.AppController._start(app)
        self.assertTrue(app.is_rec)
        return app

    def test_its_own_whisper_model(self):
        app = self._start(smart_actions_speech_model="small")
        self.assertEqual(app.starts, [{"speech": ("local", "small")}])
        self.assertEqual(app.loads, ["small"])                   # loaded while the user speaks
        self.assertIs(app._smart_speech_restore, True)           # Dictation's comes back after
        app = self._start(smart_actions_speech_model="base")     # the same as Dictation's
        self.assertEqual(app.loads, ["base"])
        self.assertIs(app._smart_speech_restore, False)

    def test_gusan_cloud_for_pro(self):
        app = self._start(smart_actions_speech_model="cloud")
        self.assertEqual(app.starts, [{"speech": ("managed", None)}])
        self.assertEqual(app.loads, [])                          # nothing local to load
        self.assertIs(app._smart_speech_restore, True)

    def test_cloud_needs_pro_and_no_privacy_mode(self):
        for kw in ({"pro": False}, {"privacy_mode": True}):
            app = self._start(smart_actions_speech_model="cloud", **kw)
            self.assertEqual(app.starts, [{}], kw)               # like Dictation
            self.assertEqual(app.loads, [None], kw)
            self.assertIs(app._smart_speech_restore, False, kw)

    def test_same_as_dictation_and_alt_r_start_plainly(self):
        app = self._start(smart_actions_speech_model="")
        self.assertEqual((app.starts, app.loads, app.pro_asked), ([{}], [None], []))
        app = self._start(smart_next=False, smart_actions_speech_model="small")   # Alt+R
        self.assertEqual((app.starts, app.loads, app.pro_asked), ([{}], [None], []))
        self.assertIsNone(app._oneshot_action)
        app = self._start(smart_actions_speech_model="whisper-9000")              # unknown
        self.assertEqual(app.starts, [{}])

    def test_the_session_api_matches(self):
        cfg = {"smart_actions_speech_model": "small"}
        self.assertEqual(engine_choice.smart_speech_session(cfg, False), ("local", "small"))
        cfg = {"smart_actions_speech_model": "cloud"}
        self.assertEqual(engine_choice.smart_speech_session(cfg, True), ("managed", None))
        self.assertEqual(engine_choice.smart_speech_session(dict(cfg, privacy_mode=True), True),
                         (None, None))


class TestDictationModelComesBack(unittest.TestCase):
    def _app(self, restore=True):
        app = SimpleNamespace(_smart_speech_restore=restore, preloads=[], _job_seq=0,
                              _busy=False, is_rec=False)
        app._preload_speech_model = lambda: app.preloads.append(1)
        app._unregister_transient_keys = lambda: None
        return app

    def test_after_the_stop_once(self):
        app = self._app()
        app._stop_impl = lambda job: None
        main.AppController._stop(app)
        self.assertEqual(app.preloads, [1])
        self.assertIs(app._smart_speech_restore, False)
        main.AppController._stop(app)                            # the next take: nothing more
        self.assertEqual(app.preloads, [1])

    def test_not_after_a_plain_take(self):
        app = self._app(restore=False)
        app._stop_impl = lambda job: None
        main.AppController._stop(app)
        self.assertEqual(app.preloads, [])

    def test_a_superseded_stop_leaves_it_to_esc(self):
        app = self._app()

        def superseded(job):
            app._job_seq += 1                                    # Esc / a new take meanwhile
        app._stop_impl = superseded
        main.AppController._stop(app)
        self.assertEqual(app.preloads, [])
        self.assertIs(app._smart_speech_restore, True)

    def test_after_esc(self):
        app = self._app()
        app.overlay = mock.MagicMock()
        app.recorder = mock.MagicMock()
        main.AppController._abort_everything(app)
        self.assertEqual(app.preloads, [1])
        self.assertFalse(app._busy)

    def test_when_a_meeting_takes_the_recorder(self):
        app = self._app()
        app.is_rec = True
        app.overlay = mock.MagicMock()
        app.recorder = mock.MagicMock()
        self.assertTrue(main.AppController.force_stop_dictation(app))
        self.assertEqual(app.preloads, [1])


class TestProLapse(unittest.TestCase):
    def test_cloud_goes_back_to_same_as_dictation(self):
        app = mock.MagicMock()
        app.cfg = {"backend": "local", "action_model": "rule_based", "meeting_notes_model": "",
                   "smart_actions_speech_model": "cloud"}
        app.auth = SimpleNamespace(is_pro=False, entitlement_known=True, user_email="", user_id=None)
        app.calendar = None
        app.main_win = None
        main.AppController._on_auth_changed(app)
        self.assertEqual(app.cfg["smart_actions_speech_model"], "")
        app.save_config.assert_called()

    def test_a_local_pick_and_an_unknown_answer_are_kept(self):
        for value, known in (("small", True), ("cloud", False)):
            app = mock.MagicMock()
            app.cfg = {"backend": "local", "smart_actions_speech_model": value}
            app.auth = SimpleNamespace(is_pro=False, entitlement_known=known, user_email="", user_id=None)
            app.calendar = None
            app.main_win = None
            main.AppController._on_auth_changed(app)
            self.assertEqual(app.cfg["smart_actions_speech_model"], value, (value, known))


if __name__ == "__main__":
    unittest.main()
