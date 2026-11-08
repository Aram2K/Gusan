"""A microphone that sends no sound at all (its driver stuck - the founder's built-in mic after a sleep, 2026-11-08):
the take is stopped after NO_SOUND_S and the pill says the microphone sends nothing, instead of a silent wait."""
import os
import sys
import threading
import time
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


def _rec(**kw):
    r = SimpleNamespace(recording=True, stop_requested=False, _got_audio=False, on_no_sound=None, _record_error="",
                        _chunk_pipeline=None, mic_stream=None, _record_thread=None)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


class TestTheWatch(unittest.TestCase):
    def _watch(self, r, wait=0.3):
        with mock.patch.object(main, "NO_SOUND_S", 0.05):
            AR._watch_for_sound(r)
            time.sleep(wait)

    def test_no_audio_stops_the_take(self):
        called = []
        r = _rec(on_no_sound=lambda: called.append(1))
        self._watch(r)
        self.assertEqual(called, [1])

    def test_audio_stopped_or_a_newer_take_never_does(self):
        for kw in ({"_got_audio": True}, {"stop_requested": True}, {"recording": False}):
            called = []
            self._watch(_rec(on_no_sound=lambda: called.append(1), **kw))
            self.assertEqual(called, [], kw)
        called = []
        r = _rec(on_no_sound=lambda: called.append(1))
        with mock.patch.object(main, "NO_SOUND_S", 0.1):
            AR._watch_for_sound(r)
            AR._watch_for_sound(r)                         # the newer take's watch replaces the first
            time.sleep(0.4)
        self.assertEqual(called, [1])


class TestStop(unittest.TestCase):
    def _stuck_thread(self):
        gate = threading.Event()
        t = threading.Thread(target=gate.wait, daemon=True)
        t.start()
        self.addCleanup(gate.set)
        return t

    def test_a_stop_with_no_audio_at_all_says_the_microphone_sends_nothing(self):
        r = _rec(_record_thread=self._stuck_thread())
        started = time.monotonic()
        AR.stop_recording(r)
        self.assertLess(time.monotonic() - started, 2.0)             # not the full 5 s
        self.assertTrue(r._record_error.startswith("!mic:Your microphone isn't sending any sound"))

    def test_a_stop_after_real_audio_is_unchanged(self):
        r = _rec(_got_audio=True)
        AR.stop_recording(r)
        self.assertEqual(r._record_error, "")

    def test_the_message_is_plain(self):
        for msg in (main.MIC_SILENT, main.MIC_SILENT_MAC):
            text = msg.split(":", 1)[1]
            self.assertIn("microphone", text)
            self.assertLess(len(text), 140)


class TestTheController(unittest.TestCase):
    def test_no_sound_stops_like_the_shortcut(self):
        stopped = threading.Event()
        c = SimpleNamespace(is_rec=True, _busy=False, _stop=stopped.set)
        main.AppController._stop_without_sound(c)
        self.assertTrue(stopped.wait(2))
        stopped.clear()
        main.AppController._stop_without_sound(SimpleNamespace(is_rec=False, _busy=False, _stop=stopped.set))
        self.assertFalse(stopped.wait(0.2))


if __name__ == "__main__":
    unittest.main()
