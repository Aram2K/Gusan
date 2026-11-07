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
