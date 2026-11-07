"""Real-time dictation in the recorder (main.AudioRecorder): when a take streams to Gladia live, and when its live text
is the take's text at Stop (only a clean session: nothing dropped) - else the pieces, as before."""
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

AR = main.AudioRecorder


class FakeSession:
    def __init__(self, text="Hello everyone.", failed="", dropped=False, language="en"):
        self._text, self.failed, self.dropped_audio, self.language = text, failed, dropped, language
        self.stopped = self.cancelled = False

    def stop(self, timeout=None):
        self.stopped = True
        return self._text

    def cancel(self):
        self.cancelled = True


def _rec(**kw):
    r = SimpleNamespace(_chunk_pipeline=None, _abort=False, _live_session=None, fast_live_chunks=False,
                        get_auth_token=lambda: "tok", _speech_ran=None)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


class TestWhenItRuns(unittest.TestCase):
    def _wanted(self, capture_mode=None, backend="managed", token=True, **cfg):
        r = _rec(get_auth_token=(lambda: "tok") if token else None)
        with mock.patch.dict(main.cfg, dict({"privacy_mode": False, "live_stt": True}, **cfg)), \
                mock.patch.object(AR, "_backend", lambda self: backend):
            return AR._live_wanted(r, capture_mode)

    def test_a_dictation_on_gusan_cloud_streams(self):
        self.assertTrue(self._wanted())

    def test_never_a_meeting_another_engine_privacy_mode_or_signed_out(self):
        self.assertFalse(self._wanted(capture_mode="smart_meeting"))
        self.assertFalse(self._wanted(backend="local"))
        self.assertFalse(self._wanted(privacy_mode=True))
        self.assertFalse(self._wanted(token=False))
        self.assertFalse(self._wanted(live_stt=False))


class TestLanguages(unittest.TestCase):
    """Real-time on Auto-detect: Gladia detects each sentence among the person's languages (never forced into one,
    which made Gladia write French and Armenian speech in English)."""

    def _live(self, setting, cfg=None, computer=("en", "fr", "hy")):
        import speech_langs
        with mock.patch.object(speech_langs, "computer_languages", lambda: computer):
            return speech_langs.live_languages(setting, cfg or {})

    def test_auto_detect_switches_among_the_mixed_list_and_the_computers_languages(self):
        self.assertEqual(self._live("auto", {"mix_languages": ["hy", "en"]}), ("multi", ("en", "hy", "fr")))
        self.assertEqual(self._live("auto"), ("multi", ("en", "fr", "hy")))
        self.assertEqual(self._live("Auto-detect", computer=("en", "xx", "en", "de")), ("multi", ("en", "de")))

    def test_one_or_no_known_language_lets_gladia_detect_freely(self):
        self.assertEqual(self._live("auto", computer=("en",)), ("auto", ()))
        self.assertEqual(self._live("auto", computer=()), ("auto", ()))

    def test_a_set_language_is_the_language(self):
        self.assertEqual(self._live("fr"), ("fr", ()))
        self.assertEqual(self._live("en", {"mix_languages": ["hy", "en"]}), ("en", ()))

    def test_mixed_languages_is_its_ticked_list(self):
        self.assertEqual(self._live("multi", {"mix_languages": ["hy", "fr", "en"]}), ("multi", ("en", "hy", "fr")))
        self.assertEqual(self._live("multi", {"mix_languages": ["hy"]}), ("multi", ("hy", "en", "fr")))

    def test_at_most_a_few_languages(self):
        many = ("en", "fr", "de", "es", "it", "nl", "pt", "hy")
        self.assertEqual(len(self._live("auto", computer=many)[1]), 5)

    def test_this_computers_languages_never_raise(self):
        import speech_langs
        speech_langs.computer_languages.cache_clear()
        got = speech_langs.computer_languages()
        self.assertIsInstance(got, tuple)
        self.assertTrue(all(isinstance(c, str) for c in got))


class TestAtStop(unittest.TestCase):
    def test_a_clean_session_gives_the_text_at_once(self):
        s = FakeSession("Hello everyone.")
        r = _rec(_live_session=s)
        self.assertEqual(AR.transcribe(r), ("Hello everyone.", "en"))
        self.assertTrue(s.stopped)
        self.assertIsNone(r._live_session)

    def test_any_trouble_means_the_pieces_text(self):
        for s in (FakeSession(failed="dropped"), FakeSession(dropped=True), FakeSession(text="")):
            r = _rec(_live_session=s)
            self.assertIsNone(AR._finish_live(r))
            self.assertTrue(s.stopped)

    def test_esc_drops_the_session_at_once(self):
        s = FakeSession()
        r = _rec(_live_session=s, stop_requested=False)
        AR.request_abort(r)
        self.assertTrue(s.cancelled)
        self.assertIsNone(r._live_session)

    def test_the_stop_waits_at_most_a_moment(self):
        self.assertLessEqual(main.LIVE_STOP_S, 3)


if __name__ == "__main__":
    unittest.main()
