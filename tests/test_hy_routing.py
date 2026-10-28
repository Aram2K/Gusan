"""Where the Armenian model is used (Gusan 2.0.1) - and where Whisper still is.

The recorder sends a local pick of the Armenian model ("hy-fastconformer") to speech_hy ONLY when the
language is Armenian; every other language, Automatic and a real mix go to a Whisper model, exactly as
before. Dictation, Smart Actions, the live pieces of a meeting or a call, the final re-read and Audio to
text all pass through the same few places (engine_model, _run_local_once, transcribe_segments,
load_model), so this file tests them with a fake speech_hy and fake Whisper models - nothing is loaded.
"""
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Never the real settings, keyring or app data (main loads config at import).
os.environ.setdefault("TRANSCRIBE_APP_DATA_DIR", tempfile.mkdtemp(prefix="gusan-hyroute-data-"))
os.environ.setdefault("TRANSCRIBE_DISABLE_KEYRING", "1")
os.environ.setdefault("TRANSCRIBE_SKIP_MIGRATION", "1")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine_choice  # noqa: E402
import file_jobs  # noqa: E402
import main  # noqa: E402
import model_manager  # noqa: E402
import settings_apply  # noqa: E402
import speech_hy  # noqa: E402
from tests.test_speech_hy import _REAL_DATA, _REAL_DIR, _real_available  # noqa: E402

HY = speech_hy.MODEL_ID


def recorder(**attrs):
    """An AudioRecorder without PyAudio (the way test_idle_unload builds it), at rest."""
    rec = main.AudioRecorder.__new__(main.AudioRecorder)
    rec._model_lock = threading.Lock()
    rec._users_lock = threading.Lock()
    rec._infer_lock = threading.Lock()
    rec._model = None
    rec._model_name = None
    rec._model_users = 0
    rec._last_model_use = 0.0
    rec.recording = False
    rec._chunk_threads = []
    rec.language_override = None
    rec.backend_override = None
    rec.model_override = None
    rec._building_model = False
    rec._fetching_model = False
    rec.model_fetcher = None
    for k, v in attrs.items():
        setattr(rec, k, v)
    return rec


class _Cfg:
    """Patches main.cfg for a test (the recorder reads that one dict)."""

    def __init__(self, **values):
        base = {"language": "auto", "whisper_model": "small", "backend": "local", "sample_rate": 16000,
                "mix_languages": [], "privacy_mode": False}
        base.update(values)
        self.patch = mock.patch.dict(main.cfg, base)

    def __enter__(self):
        self.patch.start()
        return main.cfg

    def __exit__(self, *exc):
        self.patch.stop()


def listed(value=True):
    return mock.patch.object(speech_hy, "listed", lambda *a, **k: value)


def on_disk(*names):
    return mock.patch.object(main, "model_downloaded", lambda name: name in names)


class TestEngineModel(unittest.TestCase):
    """AudioRecorder.engine_model(name, language): which model really transcribes."""

    def model(self, name, language=None, rec=None, cfg=None):
        with _Cfg(**(cfg or {})):
            return main.AudioRecorder.engine_model(rec or recorder(), name, language)

    def test_a_whisper_pick_is_never_touched(self):
        with listed():
            for lang in ("hy", "auto", "en", "multi"):
                for name in ("tiny", "small", "large-v3", "a-custom-model"):
                    self.assertEqual(self.model(name, language=lang), name)

    def test_the_armenian_pick_reads_armenian(self):
        with listed():
            self.assertEqual(self.model(HY, language="hy"), HY)
            self.assertEqual(self.model(HY, language="hye"), HY)
            self.assertEqual(self.model(HY, language="hy-AM"), HY)
            self.assertEqual(self.model(HY, cfg={"language": "hy"}), HY)

    def test_every_other_language_goes_to_whisper(self):
        with listed(), on_disk("base", "small"):
            for lang in ("auto", "en", "ru", "fr", "de", "multi"):
                self.assertEqual(self.model(HY, language=lang), "small", lang)
        with listed(), on_disk("tiny"):
            self.assertEqual(self.model(HY, language="en"), "tiny")
        with listed(), on_disk():
            self.assertEqual(self.model(HY, language="en"), "base")             # the app's default, which downloads

    def test_the_takes_own_language_decides_when_none_is_asked(self):
        rec = recorder()
        with listed(), on_disk("small"):
            self.assertEqual(self.model(HY, rec=rec, language="hy"), HY)        # cfg language is "auto"
            self.assertEqual(self.model(HY, rec=rec), "small")                  # auto: not known to be Armenian
            self.assertEqual(self.model(HY, rec=rec, cfg={"language": "hy"}), HY)
            rec.language_override = "hy"                                        # a meeting started in Armenian
            self.assertEqual(self.model(HY, rec=rec), HY)
            rec.language_override = "en"
            self.assertEqual(self.model(HY, rec=rec, cfg={"language": "hy"}), "small")

    def test_a_mix_is_armenian_only_when_everything_in_it_is(self):
        with listed(), on_disk("small"):
            with mock.patch.object(main.speech_langs, "mix_languages", lambda c: ["hy"]):    # (the page never ticks one alone)
                self.assertEqual(self.model(HY, language="multi"), HY)
            self.assertEqual(self.model(HY, language="multi", cfg={"mix_languages": ["hy", "ru"]}), "small")
            self.assertEqual(self.model(HY, language="multi", cfg={"mix_languages": ["hy"]}), "small")   # one ticked = no mix
            self.assertEqual(self.model(HY, language="multi", cfg={"mix_languages": []}), "small")

    def test_a_model_that_is_hidden_is_not_used(self):
        with listed(False), on_disk("small"):
            self.assertEqual(self.model(HY, language="hy"), "small")

    def test_no_name_means_the_takes_own_pick(self):
        rec = recorder(model_override=HY)
        with listed(), on_disk("small"):
            self.assertEqual(self.model(None, language="hy", rec=rec), HY)
            self.assertEqual(self.model(None, language="en", rec=rec), "small")
            self.assertEqual(self.model(None, language="en", rec=recorder()), "small")        # Dictation's own

    def test_the_fallback_prefers_the_gpu_models_on_a_gpu(self):
        rec = recorder(_cuda_usable=True)
        with listed(), on_disk("small", "large-v3-turbo"):
            self.assertEqual(self.model(HY, language="en", rec=rec), "large-v3-turbo")
        with listed(), on_disk("small", "large-v3-turbo"):
            self.assertEqual(self.model(HY, language="en", rec=recorder()), "small")

    def test_the_report_names_the_model_that_really_runs(self):
        with listed(), on_disk("small"), _Cfg(whisper_model=HY, language="hy"):
            self.assertEqual(main.AudioRecorder.speech_report(recorder())["whisper_model"], HY)
        with listed(), on_disk("small"), _Cfg(whisper_model=HY, language="en"):
            self.assertEqual(main.AudioRecorder.speech_report(recorder())["whisper_model"], "small")
        with _Cfg(whisper_model="medium", language="hy"):
            self.assertEqual(main.AudioRecorder.speech_report(recorder())["whisper_model"], "medium")


class TestRunLocal(unittest.TestCase):
    """_run_local_once: a piece of dictation, a Smart Action, a meeting or a live call."""

    def setUp(self):
        self.calls = []
        self.used = []
        p = [mock.patch.object(speech_hy, "transcribe", lambda audio, lang=None: self.calls.append(("hy", lang)) or "բարև"),
             mock.patch.object(speech_hy, "loaded", lambda: True),
             listed(), on_disk("small", "base")]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def whisper(self, rec):
        class _Use:
            def __init__(inner, name=None):
                self.used.append(name)

            def __enter__(inner):
                return SimpleNamespace(device="cpu")

            def __exit__(inner, *a):
                return False
        rec.use_model = _Use
        rec._run_local_with = lambda model, audio: ("whisper text", "en")
        return rec

    def test_armenian_goes_to_the_armenian_model_and_never_loads_whisper(self):
        rec = self.whisper(recorder())
        with _Cfg(whisper_model=HY, language="hy"):
            out = main.AudioRecorder._run_local_once(rec, [0.0] * 16000)
        self.assertEqual(out, ("բարև", "hy"))
        self.assertEqual(self.calls, [("hy", "hy")])
        self.assertEqual(self.used, [])

    def test_any_other_language_goes_to_whisper(self):
        for lang in ("auto", "en", "ru"):
            self.calls.clear()
            self.used.clear()
            rec = self.whisper(recorder())
            with _Cfg(whisper_model=HY, language=lang):
                out = main.AudioRecorder._run_local_once(rec, [0.0] * 16000)
            self.assertEqual(out, ("whisper text", "en"), lang)
            self.assertEqual(self.calls, [], lang)
            self.assertEqual(self.used, ["small"], lang)                       # the best on this computer

    def test_a_whisper_pick_is_whisper_in_armenian_too(self):
        rec = self.whisper(recorder())
        with _Cfg(whisper_model="small", language="hy"):
            out = main.AudioRecorder._run_local_once(rec, [0.0] * 16000)
        self.assertEqual(out, ("whisper text", "en"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.used, ["small"])

    def test_a_smart_actions_own_pick_and_a_live_calls_pick(self):
        rec = self.whisper(recorder(model_override=HY, backend_override="local", language_override="hy"))
        with _Cfg(whisper_model="small", language="auto"):
            out = main.AudioRecorder._run_local_once(rec, [0.0] * 16000)
        self.assertEqual(out, ("բարև", "hy"))
        rec = self.whisper(recorder(model_override=HY, backend_override="local"))
        with _Cfg(whisper_model="small", language="auto"):
            out = main.AudioRecorder._run_local_once(rec, [0.0] * 16000)
        self.assertEqual(out, ("whisper text", "en"))

    def test_a_cloud_backend_that_fell_back_still_asks_the_model_the_take_picked(self):
        rec = self.whisper(recorder())
        with _Cfg(whisper_model=HY, language="hy", backend="managed"):
            out = main.AudioRecorder._run_local(rec, [0.0] * 16000)
        self.assertEqual(out, ("բարև", "hy"))

    def test_a_model_that_cannot_be_loaded_says_why_in_one_plain_sentence(self):
        rec = self.whisper(recorder())
        with mock.patch.object(speech_hy, "loaded", lambda: False), mock.patch.object(speech_hy, "ready", lambda *a: True), \
                mock.patch.object(speech_hy, "load", side_effect=speech_hy.NoRuntime()), _Cfg(whisper_model=HY, language="hy"):
            with self.assertRaises(speech_hy.NoRuntime) as cm:
                main.AudioRecorder._run_local_once(rec, [0.0] * 16000)
        self.assertEqual(main._friendly_transcription_error(cm.exception), speech_hy.MSG_NO_RUNTIME)

    def test_the_users_own_words_are_snapped_to_their_dictionary(self):
        rec = self.whisper(recorder())
        with mock.patch.object(main.vocabulary, "load_terms", lambda c: ["Մարիամ"]), \
                mock.patch.object(main.vocabulary, "correct_spellings", lambda text, terms: text + " (checked)"), \
                _Cfg(whisper_model=HY, language="hy"):
            out = main.AudioRecorder._run_local_once(rec, [0.0] * 16000)
        self.assertEqual(out, ("բարև (checked)", "hy"))


class TestCloudPieceReadHere(unittest.TestCase):
    """A cloud piece in Armenian that has to be read on this computer: the Armenian model when it is
    already here - never Whisper, and no gap."""

    def setUp(self):
        self.calls = []
        p = [mock.patch.object(speech_hy, "transcribe", lambda audio, lang=None: self.calls.append(lang) or "բարև"),
             mock.patch.object(speech_hy, "loaded", lambda: True), listed(), on_disk("small")]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def rec(self, **kw):
        rec = recorder(backend_override="managed", **kw)
        rec.use_model = lambda name=None: self.fail("Whisper was asked")
        return rec

    def test_a_ready_model_reads_it_and_the_language_is_no_longer_a_gap(self):
        with mock.patch.object(speech_hy, "ready", lambda *a: True), mock.patch.object(speech_hy, "runtime_ok", lambda: True),                 _Cfg(whisper_model="small", language="hy", backend="managed"):
            rec = self.rec()
            self.assertEqual(main.AudioRecorder._poor_language(rec), "")
            self.assertEqual(main.AudioRecorder._run_local_once(rec, [0.0] * 16000), ("բարև", "hy"))
        self.assertEqual(self.calls, ["hy"])

    def test_without_the_model_the_language_stays_a_gap_and_whisper_stays_out_of_it(self):
        with mock.patch.object(speech_hy, "ready", lambda *a: False), _Cfg(whisper_model="small", language="hy", backend="managed"):
            self.assertEqual(main.AudioRecorder._poor_language(self.rec()), "Armenian")
        with mock.patch.object(speech_hy, "ready", lambda *a: True), mock.patch.object(speech_hy, "runtime_ok", lambda: False),                 _Cfg(whisper_model="small", language="hy", backend="managed"):
            self.assertEqual(main.AudioRecorder._poor_language(self.rec()), "Armenian")

    def test_other_languages_and_takes_that_run_here_anyway_are_unchanged(self):
        with mock.patch.object(speech_hy, "ready", lambda *a: True), mock.patch.object(speech_hy, "runtime_ok", lambda: True):
            with _Cfg(whisper_model="small", language="en", backend="managed"):
                self.assertFalse(main.AudioRecorder._hy_stands_in(self.rec()))
                self.assertEqual(main.AudioRecorder._poor_language(self.rec()), "")
            with _Cfg(whisper_model="small", language="auto", backend="managed"):
                self.assertFalse(main.AudioRecorder._hy_stands_in(self.rec()))
            with _Cfg(whisper_model="small", language="hy", backend="local"):
                local = recorder(backend_override=None)
                self.assertFalse(main.AudioRecorder._hy_stands_in(local))             # its own pick (Whisper) is used
                self.assertEqual(main.AudioRecorder._poor_language(local), "Armenian")
            with _Cfg(whisper_model="small", language="hy", backend="managed", mix_languages=["hy", "en"]):
                self.assertFalse(main.AudioRecorder._hy_stands_in(self.rec(language_override="multi")))


class TestLoad(unittest.TestCase):
    def test_load_model_loads_what_will_really_run(self):
        loaded, locked = [], []
        rec = recorder()
        rec._load_locked = lambda name: locked.append(name)
        with listed(), on_disk("small"), mock.patch.object(main.AudioRecorder, "_load_hy", lambda self_: loaded.append("hy")):
            with _Cfg(whisper_model=HY, language="hy"):
                main.AudioRecorder.load_model(rec)
                main.AudioRecorder.load_model(rec, HY)
            with _Cfg(whisper_model=HY, language="auto"):
                main.AudioRecorder.load_model(rec)
                main.AudioRecorder.load_model(rec, HY)
                main.AudioRecorder.load_model(rec, HY, language="hy")           # Audio to text's own language
            with _Cfg(whisper_model="small", language="hy"):
                main.AudioRecorder.load_model(rec)
                main.AudioRecorder.load_model(rec, "medium")
        self.assertEqual(loaded, ["hy", "hy", "hy"])
        self.assertEqual(locked, ["small", "small", "small", "medium"])

    def test_use_model_never_hands_the_armenian_id_to_whisper(self):
        rec = recorder()
        seen = []
        rec._load_locked = lambda name: (seen.append(name), setattr(rec, "_model", SimpleNamespace()))
        with listed(), on_disk("base"), _Cfg():
            with rec.use_model(HY) as model:
                self.assertIsNotNone(model)
        self.assertEqual(seen, ["base"])

    def test_the_armenian_model_comes_through_the_download_manager(self):
        order = []
        rec = recorder(model_fetcher=lambda name: order.append(("fetch", name)))
        with mock.patch.object(speech_hy, "loaded", lambda: False), \
                mock.patch.object(speech_hy, "ready", lambda *a: False), \
                mock.patch.object(speech_hy, "load", lambda: order.append(("load",))):
            main.AudioRecorder._load_hy(rec)
        self.assertEqual(order, [("fetch", HY), ("load",)])
        self.assertFalse(rec._building_model)
        self.assertFalse(rec._fetching_model)

    def test_without_a_download_manager_it_downloads_itself_and_privacy_is_passed_on(self):
        calls = []
        rec = recorder()
        with mock.patch.object(speech_hy, "loaded", lambda: False), mock.patch.object(speech_hy, "ready", lambda *a: False), \
                mock.patch.object(speech_hy, "download", lambda **kw: calls.append(kw)), \
                mock.patch.object(speech_hy, "load", lambda: None), _Cfg(privacy_mode=True):
            main.AudioRecorder._load_hy(rec)
        self.assertEqual(calls, [{"privacy": True}])

    def test_already_there_and_already_loaded_are_free(self):
        rec = recorder(model_fetcher=lambda name: self.fail("fetched"))
        with mock.patch.object(speech_hy, "loaded", lambda: True), \
                mock.patch.object(speech_hy, "load", side_effect=AssertionError("loaded again")):
            main.AudioRecorder._load_hy(rec)
        with mock.patch.object(speech_hy, "loaded", lambda: False), mock.patch.object(speech_hy, "ready", lambda *a: True), \
                mock.patch.object(speech_hy, "load", lambda: None):
            main.AudioRecorder._load_hy(rec)
        self.assertFalse(rec._building_model)

    def test_a_failed_load_does_not_leave_the_recorder_waiting(self):
        rec = recorder()
        with mock.patch.object(speech_hy, "loaded", lambda: False), mock.patch.object(speech_hy, "ready", lambda *a: True), \
                mock.patch.object(speech_hy, "load", side_effect=speech_hy.NotReady()):
            with self.assertRaises(speech_hy.NotReady):
                main.AudioRecorder._load_hy(rec)
        self.assertFalse(rec._building_model)


class TestSegments(unittest.TestCase):
    """transcribe_segments: the final re-read of a meeting or a call, and Audio to text."""

    def setUp(self):
        np_stub = SimpleNamespace(asarray=lambda a, dtype=None: a, float32=None)
        p = [mock.patch.object(main, "np", np_stub), listed(), on_disk("small", "base"),
             mock.patch.object(speech_hy, "loaded", lambda: True)]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.seen = {}

    def hy_segments(self, segments=None, error=None):
        def fake(audio, on_progress=None, should_cancel=None):
            self.seen.update(audio=len(audio), progress=on_progress, cancel=should_cancel)
            if error:
                raise error
            return segments
        return mock.patch.object(speech_hy, "transcribe_segments", fake)

    def whisper(self, rec):
        used = []

        class _Use:
            def __init__(inner, name=None):
                used.append(name)

            def __enter__(inner):
                return SimpleNamespace(device="cpu")

            def __exit__(inner, *a):
                return False
        rec.use_model = _Use
        rec._segments_with = lambda model, audio, sr, language, on_progress, should_cancel, gpu_errors=False: [
            {"start": 0.0, "end": 1.0, "text": "whisper"}]
        return rec, used

    def test_an_armenian_recording_is_read_by_the_armenian_model(self):
        segs = [{"start": 0.5, "end": 3.0, "text": "բարև"}]
        rec, used = self.whisper(recorder())
        progress = lambda done, total: None  # noqa: E731
        with self.hy_segments(segs), _Cfg(whisper_model="small"):
            out = main.AudioRecorder.transcribe_segments(rec, [0.0] * 32000, language="hy", model_name=HY,
                                                         on_progress=progress, should_cancel=lambda: False)
        self.assertEqual(out, segs)
        self.assertEqual(used, [])                                       # Whisper was never asked
        self.assertIs(self.seen["progress"], progress)
        self.assertEqual(rec._last_segments_language, "hy")
        self.assertIsNotNone(rec._last_segments_seconds)

    def test_the_pick_can_be_dictations_with_the_takes_language(self):
        segs = [{"start": 0.0, "end": 1.0, "text": "ա"}]
        rec, used = self.whisper(recorder(language_override="hy"))
        with self.hy_segments(segs), _Cfg(whisper_model=HY, language="auto"):
            out = main.AudioRecorder.transcribe_segments(rec, [0.0] * 32000)
        self.assertEqual(out, segs)
        self.assertEqual(used, [])

    def test_every_other_language_is_read_by_whisper_as_before(self):
        for lang in ("en", "auto", "ru", None):
            rec, used = self.whisper(recorder())
            with self.hy_segments([{"start": 0, "end": 1, "text": "hy"}]), _Cfg(whisper_model=HY, language="auto"):
                out = main.AudioRecorder.transcribe_segments(rec, [0.0] * 32000, language=lang, model_name=HY)
            self.assertEqual(out[0]["text"], "whisper", lang)
            self.assertEqual(used, ["small"], lang)
        rec, used = self.whisper(recorder())
        with self.hy_segments([{"start": 0, "end": 1, "text": "hy"}]), _Cfg(whisper_model="medium"):
            out = main.AudioRecorder.transcribe_segments(rec, [0.0] * 32000, language="hy")
        self.assertEqual((out[0]["text"], used), ("whisper", ["medium"]))     # a Whisper pick stays Whisper

    def test_cancel_is_none_and_a_failure_is_an_empty_list_not_an_exception(self):
        rec, _used = self.whisper(recorder())
        with self.hy_segments(None), _Cfg():
            self.assertIsNone(main.AudioRecorder.transcribe_segments(rec, [0.0] * 32000, language="hy", model_name=HY))
        with self.hy_segments(error=speech_hy.NotReady()), _Cfg():
            self.assertEqual(main.AudioRecorder.transcribe_segments(rec, [0.0] * 32000, language="hy", model_name=HY), [])

    def test_a_clip_shorter_than_half_a_second_is_nothing(self):
        rec, _used = self.whisper(recorder())
        with self.hy_segments([{"start": 0, "end": 1, "text": "x"}]), _Cfg():
            self.assertEqual(main.AudioRecorder.transcribe_segments(rec, [0.0] * 100, language="hy", model_name=HY), [])
        self.assertEqual(self.seen, {})

    def test_the_users_dictionary_is_applied_to_the_segments(self):
        rec, _used = self.whisper(recorder())
        with self.hy_segments([{"start": 0, "end": 1, "text": "ա"}]), _Cfg(), \
                mock.patch.object(main.vocabulary, "load_terms", lambda c: ["x"]), \
                mock.patch.object(main.vocabulary, "correct_spellings", lambda t, terms: t.upper()):
            out = main.AudioRecorder.transcribe_segments(rec, [0.0] * 32000, language="hy", model_name=HY)
        self.assertEqual(out[0]["text"], "Ա")


class TestMemory(unittest.TestCase):
    def test_the_idle_sweep_frees_the_armenian_model_with_whisper(self):
        rec = recorder()
        with mock.patch.object(speech_hy, "loaded", lambda: True), \
                mock.patch.object(speech_hy, "maybe_unload_idle", lambda s, now=None: True):
            self.assertTrue(main.AudioRecorder.maybe_unload_idle(rec, 600))        # only the Armenian one was loaded
            rec._model, rec._model_name = SimpleNamespace(), "small"
            self.assertTrue(main.AudioRecorder.maybe_unload_idle(rec, 600, now=1e9))   # both
            self.assertIsNone(rec._model)

    def test_nothing_is_freed_during_a_recording_or_while_pieces_are_running(self):
        rec = recorder(recording=True)
        with mock.patch.object(speech_hy, "loaded", lambda: True), \
                mock.patch.object(speech_hy, "maybe_unload_idle", side_effect=AssertionError("freed")):
            self.assertFalse(main.AudioRecorder.maybe_unload_idle(rec, 600))
            rec.recording = False
            alive = SimpleNamespace(is_alive=lambda: True)
            rec._chunk_threads = [alive]
            self.assertFalse(main.AudioRecorder.maybe_unload_idle(rec, 600))
            self.assertFalse(main.AudioRecorder.maybe_unload_idle(rec, 0))

    def test_the_sweep_reports_false_when_nothing_was_loaded(self):
        rec = recorder()
        with mock.patch.object(speech_hy, "loaded", lambda: False):
            self.assertFalse(main.AudioRecorder.maybe_unload_idle(rec, 600))

    def test_unloading_the_armenian_model_by_name(self):
        rec = recorder()
        rec._model, rec._model_name = SimpleNamespace(), "small"
        with mock.patch.object(speech_hy, "unload", return_value=True) as unload:
            main.AudioRecorder.unload_model(rec, HY)
            unload.assert_called_once()
            self.assertIsNotNone(rec._model)                                   # Whisper stays
        main.AudioRecorder.unload_model(rec, "small")
        self.assertIsNone(rec._model)


class TestModelFiles(unittest.TestCase):
    """main.model_downloaded / download_whisper_model / remove_whisper_model know the Armenian id."""

    def test_checks_downloads_and_removes_through_speech_hy(self):
        with mock.patch.object(speech_hy, "ready", lambda *a: True):
            self.assertTrue(main.model_downloaded(HY))
        with mock.patch.object(speech_hy, "ready", lambda *a: False):
            self.assertFalse(main.model_downloaded(HY))
        seen = []
        with mock.patch.object(speech_hy, "download", lambda **kw: seen.append(kw) or "dir"), _Cfg(privacy_mode=True):
            self.assertEqual(main.download_whisper_model(HY, on_progress=print), "dir")
        self.assertEqual(seen[0]["privacy"], True)
        self.assertEqual(seen[0]["on_progress"], print)
        with mock.patch.object(speech_hy, "remove", lambda *a: True):
            self.assertTrue(main.remove_whisper_model(HY))

    def test_whisper_ids_do_not_reach_speech_hy(self):
        with mock.patch.object(speech_hy, "ready", side_effect=AssertionError("asked")), \
                mock.patch.object(main, "_cached_model_bin", lambda repo: True):
            self.assertTrue(main.model_downloaded("small"))


class TestAppController(unittest.TestCase):
    def test_the_armenian_download_is_joined_like_a_whisper_one(self):
        calls = []

        class Downloads:
            def kind(self, name):
                return {"small": "whisper", HY: "speech_hy"}.get(name)

            def fetch(self, name, on_progress=None, timeout=None):
                calls.append(name)
                return name
        app = SimpleNamespace(downloads=Downloads(), _model_wait=None, _busy=False, SPEECH_MODEL_WAIT_S=1,
                              overlay=SimpleNamespace(call_soon=lambda fn, *a: None))
        for name in ("_fetch_speech_model", "_run_on_gui", "_on_model_wait_progress", "_show_model_wait", "_end_model_wait"):
            setattr(app, name, types.MethodType(getattr(main.AppController, name), app))
        results = []

        def run():
            try:
                with mock.patch.object(main, "model_downloaded", return_value=False):
                    app._fetch_speech_model(HY)
                    app._fetch_speech_model("small")
                    app._fetch_speech_model("my-finetune")
            except BaseException as e:      # noqa: B902
                results.append(e)
        t = threading.Thread(target=run)
        t.start()
        t.join(5)
        self.assertEqual(results, [])
        self.assertEqual(calls, [HY, "small"])

    def test_the_manager_knows_it_as_its_own_kind(self):
        m = model_manager.DownloadManager(ops=SimpleNamespace(normalize=lambda n: n), spawn=lambda *a: None)
        self.assertEqual(m.kind(HY), "speech_hy")
        self.assertEqual(m.kind("small"), "whisper")
        self.assertIn(HY, model_manager.ALL_IDS)
        self.assertNotIn(HY, model_manager.SPEECH_IDS)

    def test_what_loads_early_is_the_model_that_will_run(self):
        loaded = []
        rec = SimpleNamespace(engine_model=lambda name: "small" if name == HY else name,
                              load_model=lambda name: loaded.append(name))
        app = SimpleNamespace(recorder=rec, _effective_cfg=lambda: {"backend": "local", "whisper_model": HY})

        class _Now:
            def __init__(self, target=None, daemon=None):
                self.target = target

            def start(self):
                self.target()
        with mock.patch.object(main.threading, "Thread", _Now), mock.patch.object(main, "model_downloaded", lambda n: n == "small"):
            main.AppController._preload_speech_model(app)
            main.AppController.prewarm_speech_model(app, speech=("local", HY, None))
        self.assertEqual(loaded, ["small", "small"])                           # never the 105 MB model for a language it can't read
        loaded.clear()
        with mock.patch.object(main.threading, "Thread", _Now), mock.patch.object(main, "model_downloaded", lambda n: False):
            main.AppController._preload_speech_model(app)
        self.assertEqual(loaded, [])                                           # never a download

    def test_the_gpu_check_uses_a_whisper_model(self):
        app = SimpleNamespace(cfg={"whisper_model": HY})
        with mock.patch.object(main, "model_downloaded", lambda n: n == "small"):
            self.assertEqual(main.AppController._gpu_check_model(app), "small")
            app.cfg["whisper_model"] = "small"
            self.assertEqual(main.AppController._gpu_check_model(app), "small")
            app.cfg["whisper_model"] = "medium"
            self.assertIsNone(main.AppController._gpu_check_model(app))
        with mock.patch.object(main, "model_downloaded", lambda n: False):
            app.cfg["whisper_model"] = HY
            self.assertIsNone(main.AppController._gpu_check_model(app))

    def test_a_wait_on_the_armenian_model_never_offers_a_gpu_download(self):
        sent = []
        app = SimpleNamespace(cfg={"backend": "local", "whisper_model": HY},
                              sig_gpu_wait=SimpleNamespace(emit=lambda *a: sent.append(a)))
        main.AppController._note_local_wait(app, "file", 90.0, HY)
        main.AppController._note_local_wait(app, "dictation", 9.0)
        self.assertEqual(sent, [])
        main.AppController._note_local_wait(app, "file", 90.0, "large-v3")
        self.assertEqual(len(sent), 1)


class TestFileJobs(unittest.TestCase):
    SEGS = [{"start": 0.0, "end": 4.0, "text": "բարև"}]

    class Rec:
        def __init__(self, resolver=None):
            self.loaded, self.calls = [], []
            self._last_segments_language = "stale"
            self._last_segments_seconds = None
            if resolver:
                self.engine_model = resolver

        def load_model(self, name, language=None):
            self.loaded.append((name, language))

        def transcribe_segments(self, audio, language=None, model_name=None, on_progress=None, should_cancel=None):
            self.calls.append((language, model_name))
            self._last_segments_language = "hy"
            return [dict(s) for s in TestFileJobs.SEGS]

    def run_job(self, opts, rec, downloaded=lambda m: True, auto="small", **kw):
        progress = []
        downloads = []
        audio = [0.0] * (16000 * 4)

        class Audio(list):
            pass
        with mock.patch.object(speech_hy, "ready", lambda *a: kw.get("hy_ready", False)), \
                mock.patch.object(speech_hy, "listed", lambda *a, **k: kw.get("hy_listed", True)):
            result = file_jobs.run_file_job(
                "talk.wav", opts, rec, on_progress=lambda *a: progress.append(a),
                decode=lambda *a, **k: audio, probe=lambda p: 4.0, model_downloaded=downloaded,
                download_model=lambda m, on_progress=None: downloads.append(m), auto_model=lambda r: auto,
                diarizer=SimpleNamespace(sherpa_installed=lambda: False, is_available=lambda: False))
        return result, progress, downloads

    def engine(self, covers=True, fallback="small"):
        return lambda name, language=None: name if covers and language in ("hy",) else (fallback if name == HY else name)

    def test_the_armenian_model_is_a_choice_of_the_audio_to_text_page(self):
        self.assertIn(HY, file_jobs.LOCAL_MODELS)
        self.assertEqual(file_jobs.options_from_cfg({"file_model": HY})["model"], HY)
        self.assertEqual(file_jobs.options_from_cfg({"file_model": "small"})["model"], "small")
        self.assertIsNone(file_jobs.options_from_cfg({"file_model": "hy"})["model"])           # a value nothing knows
        self.assertEqual(file_jobs.whisper_name(HY), "NVIDIA FastConformer Armenian")
        self.assertEqual(file_jobs.display_name(HY), "NVIDIA FastConformer Armenian")

    def test_an_armenian_file_with_the_armenian_model(self):
        rec = self.Rec(self.engine())
        result, progress, downloads = self.run_job({"model": HY, "language": "hy", "speakers": False}, rec)
        self.assertEqual(result["model"], HY)
        self.assertEqual(rec.loaded, [(HY, "hy")])                              # told the language it will read
        self.assertEqual(rec.calls, [("hy", HY)])
        self.assertEqual(result["language"], "hy")
        self.assertEqual(downloads, [])
        self.assertIn("Loading NVIDIA FastConformer Armenian", [p[1] for p in progress])

    def test_another_language_goes_to_whisper_and_the_result_says_so(self):
        rec = self.Rec(self.engine(covers=True, fallback="base"))
        result, _progress, _downloads = self.run_job({"model": HY, "language": "en", "speakers": False}, rec)
        self.assertEqual(result["model"], "base")
        self.assertEqual(rec.loaded, [("base", None)])                          # a plain Whisper load, as before
        self.assertEqual(rec.calls, [("en", "base")])
        rec = self.Rec(self.engine(covers=True, fallback="base"))
        result, _p, _d = self.run_job({"model": HY, "language": "auto", "speakers": False}, rec)
        self.assertEqual(result["model"], "base")

    def test_a_missing_model_is_downloaded_with_a_plain_label(self):
        rec = self.Rec(self.engine())
        result, progress, downloads = self.run_job({"model": HY, "language": "hy", "speakers": False}, rec,
                                                   downloaded=lambda m: m != HY)
        self.assertEqual(downloads, [HY])
        self.assertIn("Downloading NVIDIA FastConformer Armenian", [p[1] for p in progress])

    def test_automatic_takes_the_armenian_model_for_an_armenian_file_when_it_is_already_there(self):
        rec = self.Rec(self.engine())
        result, _p, downloads = self.run_job({"language": "hy", "speakers": False}, rec, hy_ready=True)
        self.assertEqual(result["model"], HY)
        rec = self.Rec(self.engine())
        result, _p, downloads = self.run_job({"language": "hy", "speakers": False}, rec, hy_ready=False)     # not downloaded: never fetched for Automatic
        self.assertEqual(result["model"], "small")
        self.assertEqual(downloads, [])
        rec = self.Rec(self.engine())
        result, _p, _d = self.run_job({"language": "en", "speakers": False}, rec, hy_ready=True)
        self.assertEqual(result["model"], "small")
        result, _p, _d = self.run_job({"language": "hy", "speakers": False}, self.Rec(), hy_ready=True)       # a stand-in recorder: unchanged
        self.assertEqual(result["model"], "small")

    def test_a_job_that_names_a_whisper_model_is_never_changed(self):
        rec = self.Rec(self.engine())
        result, _p, _d = self.run_job({"model": "medium", "language": "hy", "speakers": False}, rec, hy_ready=True)
        self.assertEqual(result["model"], "medium")
        self.assertEqual(rec.loaded, [("medium", None)])
        self.assertEqual(rec.calls, [("hy", "medium")])

    def test_a_stand_in_recorder_without_engine_model_still_falls_back(self):
        rec = self.Rec()
        with mock.patch.object(speech_hy, "covers", lambda *a, **k: False):
            result, _p, _d = self.run_job({"model": HY, "language": "en", "speakers": False}, rec)
        self.assertNotEqual(result["model"], HY)

    def test_the_queue_counts_it_as_a_model_on_this_computer(self):
        notes = []
        app = SimpleNamespace(cfg={"whisper_model": "small"}, _note_local_wait=lambda *a: notes.append(a))
        q = file_jobs.FileJobQueue(app, persist=False, store=SimpleNamespace(), spawn=lambda fn: None)
        item = {"state": "running", "id": 1, "_cancel": threading.Event(), "started_at": 0, "pct": 0,
                "stage": "", "eta": "", "model": HY, "opts": {}}
        q._current = item
        q._on_info(item, {"model": HY})
        self.assertEqual(item["engine"], "local")
        self.assertEqual(item["model_name"], "NVIDIA FastConformer Armenian")


class TestWhatAPageMayWrite(unittest.TestCase):
    def test_settings_accept_the_armenian_id_in_every_speech_setting(self):
        self.assertTrue(settings_apply._smart_speech_ok(HY, False))
        self.assertTrue(settings_apply._smart_speech_ok(HY, True))                  # on this computer: Privacy mode is fine
        self.assertTrue(settings_apply._file_model_ok(HY, True))
        self.assertTrue(settings_apply._live_speech_ok(HY, True, {}))
        for bad in ("hy", "hy-fastconformer-int8", "Hy-Fastconformer", "cloud2", "model"):
            self.assertFalse(settings_apply._smart_speech_ok(bad, False), bad)
            self.assertFalse(settings_apply._file_model_ok(bad, False), bad)
            self.assertFalse(settings_apply._live_speech_ok(bad, False, {}), bad)

    def test_the_sessions_hand_the_recorder_a_local_pick(self):
        self.assertEqual(engine_choice.smart_speech_session({"smart_actions_speech_model": HY}, True), ("local", HY))
        self.assertEqual(engine_choice.live_speech_session({"live_assist_speech_model": HY}, True), ("local", HY, None))
        self.assertEqual(engine_choice.file_session({"file_model": HY}, False), ("local", HY))
        self.assertEqual(engine_choice.file_model_id({"file_model": HY}), HY)
        self.assertEqual(engine_choice.parse_live_speech(HY, {}), (engine_choice.LIVE_LOCAL, HY))
        self.assertEqual(engine_choice.smart_speech_session({"smart_actions_speech_model": "hy"}, True), (None, None))

    def test_the_pickers_can_save_it(self):
        res = engine_choice.set_smart_speech({}, HY, pro=False)
        self.assertEqual((res["ok"], res["changes"]), (True, {"smart_actions_speech_model": HY}))
        res = engine_choice.set_smart_speech({}, HY, pro=False, downloaded=lambda m: False)
        self.assertEqual(res["problem"], "needs_download")
        res = engine_choice.set_live_speech({}, HY, pro=False)
        self.assertEqual((res["ok"], res["changes"]), (True, {"live_assist_speech_model": HY}))
        res = engine_choice.set_file_model({}, HY, pro=False)
        self.assertEqual((res["ok"], res["changes"]), (True, {"file_model": HY}))
        res = engine_choice.set_speech_source({}, "local", model=HY, pro=False)
        self.assertEqual((res["ok"], res["changes"]), (True, {"backend": "local", "whisper_model": HY}))
        self.assertEqual(engine_choice.set_speech_source({}, "local", model="hy", pro=False)["problem"], "unknown")

    def test_it_is_named_for_what_it_is(self):
        self.assertEqual(engine_choice.speech_model_name(HY), "NVIDIA FastConformer Armenian")
        self.assertEqual(engine_choice.speech_plain(HY), "Armenian")
        self.assertEqual(engine_choice.model_tagged(HY), "NVIDIA FastConformer Armenian · Local")
        res = engine_choice.speech_engine({"whisper_model": HY, "backend": "local"}, False)
        self.assertEqual((res["kind"], res["model_name"], res["model_id"]), ("local", "NVIDIA FastConformer Armenian", HY))
        self.assertIn("Local", res["tagged"])
        import pill_copy
        self.assertEqual(pill_copy.speech_info("local", HY)["speech"], "NVIDIA FastConformer Armenian")
        self.assertEqual(pill_copy._whisper_name(HY), "NVIDIA FastConformer Armenian")

    def test_it_is_not_in_the_whisper_pickers(self):
        self.assertNotIn(HY, engine_choice.SPEECH_MODELS)
        self.assertNotIn(HY, [row["value"] for row in engine_choice._whisper_rows()])

    def test_its_row_exists_only_while_it_is_listed(self):
        with listed(False):
            self.assertIsNone(engine_choice.hy_row())
        with listed(True):
            row = engine_choice.hy_row()
            self.assertEqual(row["value"], HY)
            self.assertEqual(row["name"], "NVIDIA FastConformer Armenian")
            self.assertEqual(row["tags"], ("local",))
            # one muted line that says what it is for (and why another language is read by Whisper)
            self.assertEqual(row["note"], "Armenian only · about 105 MB · free, works offline")
            self.assertIn("NVIDIA", row["name"])                       # credited in the name itself
            self.assertIn("CC BY 4.0", row["tip"])                     # ...and the licence in its tooltip
            self.assertEqual(row["group"], engine_choice.GROUP_THIS_COMPUTER)
            self.assertIn("Local", row["line"])
            self.assertEqual(engine_choice.hy_row("Somewhere")["group"], "Somewhere")


class TestDownloadManager(unittest.TestCase):
    """model_manager with the Armenian model: its own kind, a download that can be stopped, plain sentences."""

    class Ops:
        def __init__(self, error=None):
            self.disk = set()
            self.error = error
            self.calls = []
            self.catalog_changes = []

        def normalize(self, name):
            return name

        def downloaded(self, kind, name):
            return name in self.disk

        def download(self, kind, name, on_progress, should_cancel=None):
            self.calls.append((kind, name, should_cancel))
            on_progress(50, 50, 100)
            if self.error:
                raise self.error
            self.disk.add(name)

        def remove(self, kind, name):
            self.disk.discard(name)

        def refresh_catalog(self, force=False):
            self.catalog_changes.append(force)
            return True

    def manager(self, ops):
        events = []
        m = model_manager.DownloadManager(ops=ops, notify=lambda *e: events.append(e), spawn=lambda fn, *a: fn(*a))
        return m, events

    def test_a_download_for_a_model_that_can_be_stopped(self):
        ops = self.Ops()
        m, events = self.manager(ops)
        self.assertTrue(m.start(HY))
        self.assertEqual(m.state(HY), model_manager.READY)
        kind, name, should_cancel = ops.calls[0]
        self.assertEqual((kind, name), ("speech_hy", HY))
        self.assertTrue(callable(should_cancel))
        self.assertFalse(should_cancel())

    def test_cancel_goes_back_to_missing_without_an_error(self):
        ops = self.Ops(error=speech_hy.Cancelled())
        m, events = self.manager(ops)
        m.start(HY)
        self.assertEqual(m.state(HY), model_manager.MISSING)
        self.assertEqual(m.error(HY), "")
        self.assertFalse(m.cancel("small"))                                      # only the Armenian download can be stopped
        self.assertFalse(m.cancel(HY))                                           # nothing is running

    def test_cancel_while_it_runs_is_seen_by_the_download(self):
        ops = self.Ops()
        seen = []

        def slow(kind, name, on_progress, should_cancel=None):
            m.cancel(name)
            seen.append(should_cancel())
            raise speech_hy.Cancelled()
        ops.download = slow
        m, events = self.manager(ops)
        m.start(HY)
        self.assertEqual(seen, [True])
        self.assertEqual(m.state(HY), model_manager.MISSING)

    def test_a_failure_is_one_plain_sentence_with_a_short_reason(self):
        for exc, reason in ((speech_hy.PrivacyBlocked(), "privacy"), (speech_hy.NotAvailable(), "unavailable"),
                            (speech_hy.DownloadFailed(speech_hy.MSG_UNSAFE, "bad_hash"), "bad_hash")):
            tracked = []
            ops = self.Ops(error=exc)
            m = model_manager.DownloadManager(ops=ops, track=lambda e, p: tracked.append((e, p)),
                                              notify=lambda *e: None, spawn=lambda fn, *a: fn(*a))
            m.start(HY)
            self.assertEqual(m.state(HY), model_manager.FAILED)
            self.assertEqual(m.error(HY), str(exc))
            self.assertEqual(tracked[-1], ("model_download_failed", {"kind": "speech_hy", "model": HY, "reason": reason}))

    def test_the_catalog_lookup_tells_the_pages(self):
        ops = self.Ops()
        m, events = self.manager(ops)
        self.assertTrue(m.refresh_catalog(force=True))
        self.assertEqual(ops.catalog_changes, [True])
        self.assertIn(("catalog",), events)

    def test_the_real_ops_pass_privacy_and_cancel_to_the_download(self):
        got = {}
        ops = model_manager.ModelOps(privacy=lambda: True)
        with mock.patch.object(speech_hy, "download", lambda **kw: got.update(kw) or "dir"):
            ops.download("speech_hy", HY, lambda *a: None, should_cancel=lambda: False)
        self.assertTrue(got["privacy"])
        self.assertFalse(got["should_cancel"]())
        with mock.patch.object(speech_hy, "ready", lambda *a: True):
            self.assertTrue(ops.downloaded("speech_hy", HY))
        with mock.patch.object(speech_hy, "remove", lambda *a: True):
            self.assertTrue(ops.remove("speech_hy", HY))
        with mock.patch.object(speech_hy, "refresh_catalog", lambda force=False, privacy=False: privacy and force):
            self.assertTrue(ops.refresh_catalog(force=True))


@unittest.skipUnless(_real_available(), "the converted Armenian model / its test clips are not on this computer")
class TestRealModelThroughTheRecorder(unittest.TestCase):
    """The shipped files, reached the way the app reaches them: a dictation / live piece and a final
    re-read through AudioRecorder, with the recorder's own language rule."""

    def setUp(self):
        import numpy as np
        self.np = np
        self.helper = _RealClips()
        p = [mock.patch.object(speech_hy, "model_dir", lambda pins=None: _REAL_DIR),
             mock.patch.object(speech_hy, "_ENGINE", None), mock.patch.object(speech_hy, "_runtime_cache", ("ok", "")),
             on_disk()]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.addCleanup(speech_hy.unload)

    def test_a_piece_and_a_re_read_in_armenian(self):
        clips = self.helper.clips(2)
        rec = recorder()
        rec.use_model = lambda name=None: self.fail("Whisper was asked")
        with _Cfg(whisper_model=HY, language="hy"):
            text, lang = main.AudioRecorder._run_local_once(rec, clips[0])
            self.assertEqual(lang, "hy")
            self.assertTrue(any("Ա" <= ch <= "֏" for ch in text), text)
            self.assertFalse(any(ch.isascii() and ch.isalpha() for ch in text), text)        # never Latin letters
            audio = self.np.concatenate([clips[0], self.np.zeros(2 * 16000, dtype=self.np.float32), clips[1]])
            progress = []
            segs = main.AudioRecorder.transcribe_segments(rec, audio, language="hy",
                                                          on_progress=lambda d, t: progress.append((d, t)))
        self.assertGreaterEqual(len(segs), 2)
        self.assertEqual(rec._last_segments_language, "hy")
        self.assertGreater(rec._last_segments_seconds, 0)
        self.assertTrue(progress and progress[-1][0] <= progress[-1][1] + 0.01)
        for a, b in zip(segs, segs[1:]):
            self.assertLessEqual(a["end"], b["start"] + 1e-6)

    def test_the_same_pick_in_another_language_asks_whisper_not_the_armenian_model(self):
        rec = recorder()
        rec.use_model = lambda name=None: (_ for _ in ()).throw(RuntimeError("whisper:%s" % name))
        with on_disk("small"), _Cfg(whisper_model=HY, language="en"), mock.patch.object(
                speech_hy, "transcribe", side_effect=AssertionError("the Armenian model was asked")):
            with self.assertRaises(RuntimeError) as cm:
                main.AudioRecorder._run_local_once(rec, self.helper.clips(1)[0])
        self.assertEqual(str(cm.exception), "whisper:small")
        self.assertFalse(speech_hy.loaded())


class _RealClips:
    def clips(self, n):
        import csv
        import struct
        np = __import__("numpy")
        rows = list(csv.reader(open(os.path.join(_REAL_DATA, "refs.tsv"), encoding="utf-8", newline=""),
                               delimiter="	", quoting=csv.QUOTE_NONE, escapechar="\\"))[1:]
        out = []
        for r in rows[:n]:
            b = open(os.path.join(_REAL_DATA, "clips", r[0]), "rb").read()
            pos, fmt, data = 12, None, None
            while pos + 8 <= len(b):
                cid, size = b[pos:pos + 4], struct.unpack("<I", b[pos + 4:pos + 8])[0]
                body = b[pos + 8:pos + 8 + size]
                if cid == b"fmt ":
                    fmt = struct.unpack("<HHIIHH", body[:16])
                elif cid == b"data":
                    data = body
                pos += 8 + size + (size & 1)
            tag, _ch, _rate, _br, _ba, bits = fmt
            out.append(np.frombuffer(data, dtype="<f4").astype(np.float32) if tag == 3 and bits == 32
                       else np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0)
        return out


if __name__ == "__main__":
    unittest.main()
