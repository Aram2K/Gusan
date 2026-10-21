"""The plain transcript is on disk BEFORE the speaker pass starts.

The founder's 45-minute Live Assistant meeting: the speaker pass froze the app,
Windows closed it, and the recovered meeting had no transcript.txt at all (it
was only written after that pass). The pass is the longest step of the whole
run, so the transcript now goes to disk first and the final write replaces it.

Runs the Qt-free MeetingEngine (works under test_core's suite-wide stubs)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import meeting_session as msess
import meeting_store as ms


class _Rec:
    def __init__(self, result):
        self.result = result
        self._full_audio_truncated = False
        self._full_audio_max = 16000 * 60 * 120
        self._loopback_peak = 1.0
        self.on_chunk_complete = self.on_levels = self.on_partial = self.on_lang_detected = None
        self.on_source_levels = None
        self.fast_live_chunks = False
        self.partial_text = ""

    def start_recording(self, **kw):
        pass

    def stop_recording(self):
        pass

    def transcribe(self):
        return self.result

    def get_full_audio(self):
        return None

    def set_paused(self, on):
        pass

    def chunk_source_energy(self, idx):
        return None


class _App:
    def __init__(self, recorder):
        self.cfg = {"meeting_consent_ack": True, "save_history": False, "meeting_audio_mode": "default_mic",
                    "whisper_model": "base", "action_model": "managed"}
        self.recorder = recorder
        self.live_assist = None

    def save_config(self):
        pass

    def track(self, event, props=None):
        pass

    def is_pro(self):
        return True

    def release_models_after_session(self):
        pass

    def _resolve_action_engine(self, action_model=None):
        return (action_model or "managed"), {**self.cfg, "_managed_token": "tok"}


class TestPlainTranscriptFirst(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "meetings"
        self.root.mkdir()
        for p in (patch.object(ms.storage, "path_for", return_value=self.root),
                  patch.object(msess.storage, "path_for", return_value=self.root),
                  patch.object(msess, "has_loopback", return_value=True),
                  patch.object(msess.telemetry, "track"),
                  patch.object(msess.actions, "process", return_value="## Summary\n- ok")):
            p.start()
            self.addCleanup(p.stop)

    def _session(self, text):
        s = msess.MeetingEngine(_App(_Rec((text, "en"))))
        s._spawn = lambda target, *args: target(*args)
        return s

    def test_the_speaker_pass_finds_the_plain_transcript_already_saved(self):
        s = self._session("We ship on Friday. Anna owns the launch.")
        seen = {}

        def speaker_pass():
            path = s.meeting_dir / "transcript.txt"
            seen["on_disk"] = path.read_text(encoding="utf-8") if path.is_file() else None
            seen["flag"] = s._transcript_saved.is_set()
            return "Speaker 1: We ship on Friday.\nSpeaker 2: Anna owns the launch."

        with patch.object(s, "_build_attributed_transcript", speaker_pass):
            self.assertTrue(s.start(title="Sync")[0])
            self.assertTrue(s.stop())
        self.assertEqual(seen["on_disk"], "We ship on Friday. Anna owns the launch.")
        self.assertTrue(seen["flag"], "a Quit during the speaker pass must keep this complete copy")
        # ...and the final write replaced it with the speaker-labelled one
        final = (s.meeting_dir / "transcript.txt").read_text(encoding="utf-8")
        self.assertTrue(final.startswith("Speaker 1: We ship on Friday."))

    def test_a_kill_during_the_speaker_pass_leaves_the_transcript(self):
        s = self._session("We ship on Friday. Anna owns the launch.")

        class Killed(BaseException):
            pass

        def speaker_pass():
            raise Killed()                      # the process dies here: nothing after this runs

        with patch.object(s, "_build_attributed_transcript", speaker_pass):
            self.assertTrue(s.start(title="Sync")[0])
            with self.assertRaises(Killed):
                s.stop()
        folder = s.meeting_dir
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"),
                         "We ship on Friday. Anna owns the launch.")
        self.assertEqual(ms.load_transcript(folder), "We ship on Friday. Anna owns the launch.")
        self.assertEqual([p.name for p in folder.iterdir() if p.name.startswith(".transcript")], [])   # no temp left
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["title"], "Sync")

    def test_a_continued_meeting_keeps_its_earlier_part_in_front(self):
        first = self._session("Part one.")
        first.start(title="Weekly")
        with patch.object(first, "_build_attributed_transcript", lambda: ""):
            first.stop()
        folder = first.meeting_dir
        second = self._session("Part two.")
        seen = {}

        def speaker_pass():
            seen["on_disk"] = (folder / "transcript.txt").read_text(encoding="utf-8")
            return ""

        with patch.object(second, "_build_attributed_transcript", speaker_pass):
            self.assertTrue(second.continue_meeting(str(folder))[0])
            self.assertTrue(second.stop())
        self.assertTrue(seen["on_disk"].startswith("Part one."))
        self.assertIn("Resumed", seen["on_disk"])
        self.assertTrue(seen["on_disk"].rstrip().endswith("Part two."))

    def test_nothing_is_written_for_an_empty_transcript(self):
        s = self._session("")
        s.start(title="Silent")
        folder = s.meeting_dir
        s._final_transcript = ""
        s._save_plain_transcript_early("")
        self.assertFalse((folder / "transcript.txt").exists())
        self.assertFalse(s._transcript_saved.is_set())


if __name__ == "__main__":
    unittest.main()
