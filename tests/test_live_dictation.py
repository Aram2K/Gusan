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
    def __init__(self, text="Hello everyone.", failed="", dropped=False, language="en", languages=("en",)):
        self._text, self.failed, self.dropped_audio, self.language = text, failed, dropped, language
        self.languages = list(languages)
        self.stopped = self.cancelled = False

    def stop(self, timeout=None):
        self.stopped = True
        return self._text

    def cancel(self):
        self.cancelled = True


def _rec(**kw):
    r = SimpleNamespace(_chunk_pipeline=None, _abort=False, _live_session=None, fast_live_chunks=False,
                        language_override=None,
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

    def _engine(self, **cfg):
        r = _rec()
        with mock.patch.dict(main.cfg, dict({"privacy_mode": False, "live_stt": True}, **cfg)), \
                mock.patch.object(AR, "_backend", lambda self: "managed"):
            return AR._live_engine(r, None)

    def test_armenian_streams_to_openai_on_fast_and_not_at_all_on_best_accuracy(self):
        """The founder's pick (2026-11-07): Armenian Fast = OpenAI gpt-live-transcribe (Gladia writes it poorly),
        Armenian Best accuracy = ElevenLabs Scribe v2 (the pieces); every other language = Gladia."""
        self.assertEqual(self._engine(language="hy", managed_speech_profile="fast"), "openai")
        self.assertIsNone(self._engine(language="hy", managed_speech_profile="best"))
        self.assertEqual(self._engine(language="fr", managed_speech_profile="best"), "gladia")
        self.assertEqual(self._engine(language="auto", managed_speech_profile="fast"), "gladia")
        self.assertIsNone(self._engine(language="hy", managed_speech_profile="fast", privacy_mode=True))

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


class TestAnotherLanguageThanTheOneSet(unittest.TestCase):
    """Armenian set, English spoken: the backup (detecting by itself) wrote Latin letters - its text is used, as
    spoken, not the real-time text forced into Armenian. Only pieces already back count: Stop never waits."""

    def _rec(self, pieces, detects=True, language="hy"):
        import threading
        r = _rec(_backup_detects=detects, _chunk_results=dict(enumerate(pieces)), _chunk_lock=threading.Lock())
        self._cfg = mock.patch.dict(main.cfg, {"language": language})
        self._cfg.start()
        self.addCleanup(self._cfg.stop)
        return r

    def test_english_pieces_with_armenian_set_mean_another_language(self):
        r = self._rec(["Okay so tomorrow I have to go to basketball practice."])
        self.assertTrue(AR._spoke_other_language(r))
        s = FakeSession("Լավ, ուրեմն վաղը պետք է գնամ բասկետբոլի։", language="hy", languages=("hy",))
        s.engine = "openai"
        r._live_session = s
        self.assertIsNone(AR._finish_live(r))                      # the pieces' text, as spoken

    def test_armenian_pieces_or_a_few_english_words_are_fine(self):
        self.assertFalse(AR._spoke_other_language(self._rec(["Ուրեմն վաղը պետք է գնամ բասկետբոլի"])))
        self.assertFalse(AR._spoke_other_language(self._rec(["Ուրեմն վաղը Gusan-ով պետք է գնամ"])))

    def test_nothing_back_yet_or_too_short_never_judges(self):
        self.assertFalse(AR._spoke_other_language(self._rec([])))
        self.assertFalse(AR._spoke_other_language(self._rec(["Okay"])))

    def test_only_when_the_backup_detected_by_itself(self):
        self.assertFalse(AR._spoke_other_language(self._rec(["Okay so tomorrow I have to go."], detects=False)))
        self.assertFalse(AR._spoke_other_language(self._rec(["Okay so tomorrow I have to go."], language="fr")))


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

    def test_openais_armenian_text_is_used(self):
        s = FakeSession("Ուրեմն վաղը պետք է գնամ։", language="hy", languages=("hy",))
        s.engine = "openai"
        r = _rec(_live_session=s, _speech_ran={})
        self.assertEqual(AR._finish_live(r), ("Ուրեմն վաղը պետք է գնամ։", "hy"))
        self.assertEqual(r._speech_ran["ran_model"], "gpt-live-transcribe")

    def test_a_take_with_armenian_uses_the_pieces_text(self):
        """Real-time Gladia writes Armenian poorly: Gusan Cloud's own engines (ElevenLabs Scribe first) do it."""
        s = FakeSession("I'm testing. Je voudrais dormir. Armenian.", language="en", languages=("en", "fr", "hy"))
        r = _rec(_live_session=s)
        self.assertIsNone(AR._finish_live(r))
        self.assertTrue(s.stopped)
        s = FakeSession("I'm testing. Je voudrais dormir.", languages=("en", "fr"))
        self.assertEqual(AR._finish_live(_rec(_live_session=s)), ("I'm testing. Je voudrais dormir.", "en"))

    def test_the_stop_waits_at_most_a_moment(self):
        self.assertLessEqual(main.LIVE_STOP_S, 3)


if __name__ == "__main__":
    unittest.main()
