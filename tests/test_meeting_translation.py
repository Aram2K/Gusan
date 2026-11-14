"""The conversation in the caption language (meeting_translation.py; founder 2026-11-14: "have the choice between the
original or the translated conversation" - during the call and on the saved meeting): when it applies, the live
lines' translator, the saved transcript's translation file, and the meeting session's part in both."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import meeting_session as msess
import meeting_translation as mt

ON = {"captions_on": True, "captions_language": "fr"}
TRANSCRIPT = "You: Hello there. How are you?\nThem: Fine, thanks.\nYou: Good."


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


class TestWhenTheConversationIsAlsoKeptInAnotherLanguage(unittest.TestCase):
    def test_only_with_captions_on_in_a_language_pro_and_no_privacy_mode(self):
        self.assertEqual(mt.target(ON, True), "fr")
        self.assertEqual(mt.target(dict(ON, captions_on=False), True), "")
        self.assertEqual(mt.target(dict(ON, captions_language=""), True), "")       # captions as spoken
        self.assertEqual(mt.target(dict(ON, captions_language="xx"), True), "")     # not a caption language
        self.assertEqual(mt.target(ON, False), "")                                  # Pro only
        self.assertEqual(mt.target(dict(ON, privacy_mode=True), True), "")          # the words never leave
        self.assertEqual(mt.target(None, True), "")
        self.assertEqual((mt.language_name("fr"), mt.language_name("zz")), ("French", "zz"))


class TestTheSavedTranscript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = self._tmp.name
        with open(os.path.join(self.folder, "transcript.txt"), "w", encoding="utf-8") as f:
            f.write(TRANSCRIPT)

    def test_its_paragraphs_are_what_is_translated_each_once(self):
        self.assertEqual(mt.paragraphs(TRANSCRIPT), ["Hello there. How are you?", "Fine, thanks.", "Good."])
        self.assertEqual(mt.paragraphs(TRANSCRIPT + "\nThem: Good."), mt.paragraphs(TRANSCRIPT))
        self.assertEqual([r[0] for r in mt.rows(TRANSCRIPT)], ["You", "Them", "You"])
        self.assertEqual(mt.paragraphs(""), [])

    def test_the_file_round_trips_and_a_bad_one_is_no_translation(self):
        self.assertIsNone(mt.load(self.folder))
        self.assertTrue(mt.save(self.folder, "fr", {"Good.": "Bien.", "Empty": ""}))
        self.assertEqual(mt.load(self.folder), {"language": "fr", "lines": {"Good.": "Bien."}})
        with open(os.path.join(self.folder, mt.FILE), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(mt.load(self.folder))
        with open(os.path.join(self.folder, mt.FILE), "w", encoding="utf-8") as f:
            json.dump({"lines": []}, f)
        self.assertIsNone(mt.load(self.folder))

    def test_every_paragraph_is_translated_with_the_ones_before_as_context(self):
        asked = []

        def translate(text, source, target, context):
            asked.append((text, target, list(context)))
            return text.upper()
        self.assertEqual(mt.translate_saved(self.folder, "fr", translate, workers=1), 3)
        got = mt.load(self.folder)
        self.assertEqual(got["language"], "fr")
        self.assertEqual(got["lines"], {"Hello there. How are you?": "HELLO THERE. HOW ARE YOU?",
                                        "Fine, thanks.": "FINE, THANKS.", "Good.": "GOOD."})
        self.assertEqual(asked[0], ("Hello there. How are you?", "fr", []))
        self.assertEqual(asked[2][2], ["Hello there. How are you?", "Fine, thanks."])

    def test_what_is_translated_is_kept_and_only_the_rest_is_asked_for(self):
        mt.save(self.folder, "fr", {"Good.": "Bien.", "Gone from the transcript.": "Parti."})
        asked = []
        n = mt.translate_saved(self.folder, "fr", lambda text, *_a: asked.append(text) or text.upper())
        self.assertEqual(n, 3)
        self.assertEqual(sorted(asked), ["Fine, thanks.", "Hello there. How are you?"])
        lines = mt.load(self.folder)["lines"]
        self.assertEqual(lines["Good."], "Bien.")
        self.assertNotIn("Gone from the transcript.", lines)         # (a paragraph the transcript no longer has)

    def test_another_language_starts_over_and_a_failed_paragraph_stays_as_spoken(self):
        mt.save(self.folder, "fr", {"Good.": "Bien."})

        def translate(text, *_a):
            if text.startswith("Fine"):
                raise RuntimeError("down")
            return "[de] " + text
        self.assertEqual(mt.translate_saved(self.folder, "de", translate), 2)
        got = mt.load(self.folder)
        self.assertEqual(got["language"], "de")
        self.assertEqual(got["lines"], {"Hello there. How are you?": "[de] Hello there. How are you?",
                                        "Good.": "[de] Good."})

    def test_no_transcript_no_translation(self):
        os.remove(os.path.join(self.folder, "transcript.txt"))
        self.assertEqual(mt.translate_saved(self.folder, "fr", lambda *a: "x"), 0)
        self.assertIsNone(mt.load(self.folder))


class TestTheLiveLines(unittest.TestCase):
    def test_each_line_comes_back_with_its_key_and_the_lines_before_as_context(self):
        asked, done = [], []
        tr = mt.Translator(lambda text, source, target, context: asked.append((text, target, list(context)))
                           or text.upper(), "fr", lambda key, text, got: done.append((key, text, got)), start=False)
        for i, line in enumerate(("one.", "two.", "three.", "  ")):
            tr.submit(i, line)
        self.assertEqual(tr.pending(), 3)                              # (an empty line is not sent)
        tr.drain()
        self.assertEqual(done, [(0, "one.", "ONE."), (1, "two.", "TWO."), (2, "three.", "THREE.")])
        self.assertEqual(asked[2], ("three.", "fr", ["one.", "two."]))

    def test_a_failed_line_says_so_with_nothing_and_closing_stops_it(self):
        def boom(*_a):
            raise RuntimeError("down")
        done = []
        tr = mt.Translator(boom, "fr", lambda key, text, got: done.append((key, got)), start=False)
        tr.submit(0, "one.")
        tr.drain()
        self.assertEqual(done, [(0, "")])
        tr.close()
        tr.submit(1, "two.")
        self.assertEqual(tr.pending(), 0)

    def test_several_at_once_on_their_own_threads(self):
        done = []
        tr = mt.Translator(lambda text, *_a: text.upper(), "fr", lambda key, text, got: done.append(key))
        try:
            self.assertEqual(len(tr._threads), mt.WORKERS)
            for i in range(6):
                tr.submit(i, "line %d" % i)
            self.assertTrue(_wait(lambda: len(done) == 6))
            self.assertEqual(sorted(done), list(range(6)))
        finally:
            tr.close()
        for t in tr._threads:
            t.join(2)
            self.assertFalse(t.is_alive())


class _Rec:
    """The least of a recorder the session needs."""

    def __init__(self):
        self.started = None
        self.recording = False
        self.paused = False

    def start_recording(self, **kw):
        self.started = kw
        self.recording = True

    def stop_recording(self):
        self.recording = False

    def transcribe(self):
        return ""

    def get_auth_token(self):
        return "tok"


class _App:
    def __init__(self, pro=True, **cfg):
        self.cfg = {"meeting_consent_ack": True, "save_history": False, "meeting_audio_mode": "default_mic",
                    "whisper_model": "base", "action_model": "managed", **cfg}
        self.recorder = _Rec()
        self.live_assist = None
        self.pro = pro

    def save_config(self):
        pass

    def track(self, event, props=None):
        pass

    def is_pro(self):
        return self.pro

    def release_models_after_session(self):
        pass

    def _resolve_action_engine(self, action_model=None):
        return action_model or self.cfg.get("action_model"), dict(self.cfg, _managed_token="tok")


class TestTheMeetingSession(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(msess.meeting_store, "meetings_dir", lambda: self._tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.asked = []

        def interpret(text, target, token, source="auto", context=(), timeout=None):
            self.asked.append((text, target, token))
            return "[%s] %s" % (target, text)
        import action_api
        patcher = mock.patch.object(action_api, "interpret_phrase", side_effect=interpret)
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, **cfg):
        app = _App(**cfg)
        s = msess.MeetingEngine(app)
        self.addCleanup(s._drop_translator)
        return app, s

    def _recording(self, s):
        s.state = msess.STATE_RECORDING            # (the live lines only come while it records)

    def test_live_lines_are_translated_while_captions_are_on_in_a_language(self):
        app, s = self.session(**ON)
        self._recording(s)
        seen = []
        s.line_translated.connect(lambda i, text: seen.append((i, text)))
        self.assertEqual(s.translation_language(), "fr")
        s._on_piece("Hello there.", "You")
        s._on_piece("How are you?", "Them")
        self.assertTrue(_wait(lambda: len(seen) == 2))
        self.assertEqual(sorted(seen), [(0, "[fr] Hello there."), (1, "[fr] How are you?")])
        self.assertEqual(s.live_translation(), ("fr", {0: "[fr] Hello there.", 1: "[fr] How are you?"}))
        self.assertEqual(s.live_lines(), [("You", "Hello there."), ("Them", "How are you?")])   # as spoken, as ever
        self.assertEqual({a[2] for a in self.asked}, {"tok"})

    def test_nothing_is_sent_without_a_caption_language_or_in_privacy_mode_or_without_pro(self):
        for cfg, pro in ((dict(ON, captions_language=""), True), (dict(ON, captions_on=False), True),
                         (dict(ON, privacy_mode=True), True), (ON, False)):
            app, s = self.session(pro=pro, **cfg)
            self._recording(s)
            s._on_piece("Hello there.", "You")
            self.assertIsNone(s._translator, (cfg, pro))
            self.assertEqual(s.live_translation(), ("", {}))
        time.sleep(0.05)
        self.assertEqual(self.asked, [])

    def test_a_language_picked_in_the_middle_of_the_meeting_takes_the_lines_said_before(self):
        app, s = self.session(captions_on=True, captions_language="")
        self._recording(s)
        s._on_piece("One.", "You")
        s._on_piece("Two.", "You")
        self.assertEqual(s.live_translation(), ("", {}))
        app.cfg["captions_language"] = "de"
        s._on_piece("Three.", "You")
        self.assertTrue(_wait(lambda: len(s.live_translation()[1]) == 3))
        self.assertEqual(s.live_translation(), ("de", {0: "[de] One.", 1: "[de] Two.", 2: "[de] Three."}))
        app.cfg["captions_language"] = "fr"                   # ... and another one starts over, in that language
        s._on_piece("Four.", "You")
        self.assertTrue(_wait(lambda: len(s.live_translation()[1]) == 4))
        self.assertEqual(s.live_translation()[0], "fr")
        self.assertEqual(s.live_translation()[1][3], "[fr] Four.")
        app.cfg["captions_on"] = False                        # captions off: no translations any more
        s._on_piece("Five.", "You")
        self.assertEqual(s.live_translation(), ("", {}))

    def test_a_saved_meeting_is_translated_in_the_background_and_says_when_it_is_there(self):
        app, s = self.session(**ON)
        folder = os.path.join(self._tmp.name, "20261114_101010")
        os.makedirs(folder)
        with open(os.path.join(folder, "transcript.txt"), "w", encoding="utf-8") as f:
            f.write(TRANSCRIPT)
        saved = []
        s.translation_saved.connect(lambda f: saved.append(f))
        self.assertTrue(s.translate_meeting(folder))
        self.assertTrue(_wait(lambda: saved == [folder]))
        got = mt.load(folder)
        self.assertEqual(got["language"], "fr")
        self.assertEqual(got["lines"]["Good."], "[fr] Good.")
        self.assertEqual(len(got["lines"]), 3)
        n = len(self.asked)
        self.assertTrue(s.translate_meeting(folder))            # again: nothing is asked for twice
        self.assertTrue(_wait(lambda: len(saved) == 2))
        self.assertEqual(len(self.asked), n)
        self.assertFalse(s.translate_meeting(""))

    def test_no_caption_language_and_no_file_is_nothing_to_translate(self):
        app, s = self.session(captions_on=False)
        folder = os.path.join(self._tmp.name, "20261114_111111")
        os.makedirs(folder)
        with open(os.path.join(folder, "transcript.txt"), "w", encoding="utf-8") as f:
            f.write(TRANSCRIPT)
        self.assertFalse(s.translate_meeting(folder))
        self.assertIsNone(mt.load(folder))
        self.assertEqual(self.asked, [])

    def test_the_final_transcript_of_a_meeting_with_captions_in_a_language_is_translated(self):
        app, s = self.session(**ON)
        folder = os.path.join(self._tmp.name, "20261114_121212")
        os.makedirs(folder)
        with open(os.path.join(folder, "transcript.txt"), "w", encoding="utf-8") as f:
            f.write(TRANSCRIPT)
        s._meeting_dir = folder
        saved = []
        s.translation_saved.connect(lambda f: saved.append(f))
        s._transcript_written()                                   # what Stop's worker does once it is on disk
        self.assertTrue(_wait(lambda: saved == [folder]))
        self.assertEqual(mt.load(folder)["lines"]["Fine, thanks."], "[fr] Fine, thanks.")
        app2, s2 = self.session(captions_on=False)                # captions off: the saved one stays as spoken
        other = os.path.join(self._tmp.name, "20261114_131313")
        os.makedirs(other)
        with open(os.path.join(other, "transcript.txt"), "w", encoding="utf-8") as f:
            f.write(TRANSCRIPT)
        s2._meeting_dir = other
        s2._transcript_written()
        time.sleep(0.05)
        self.assertIsNone(mt.load(other))


if __name__ == "__main__":
    unittest.main()
