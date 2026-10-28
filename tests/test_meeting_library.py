"""meeting_store + audio_export: listing, parts, WAV round-trip, MP3, concat;
and the meeting window's post-recording pipeline and setup state."""
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

import audio_export as ax
import meeting_store as ms


def _real_numpy():
    # tests/test_core.py stubs numpy for the whole discover run; the WAV/MP3
    # tests need the real thing and are skipped under the stub.
    try:
        return np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,)
    except Exception:
        return False


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestAudioExport(unittest.TestCase):
    def _tone(self, seconds=0.5, freq=440.0):
        t = np.arange(int(ax.RATE * seconds)) / ax.RATE
        return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    def test_wav_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.wav"
            ax.write_wav(p, self._tone())
            data, rate = ax.read_wav_int16(p)
            self.assertEqual(rate, ax.RATE)
            self.assertEqual(len(data), int(ax.RATE * 0.5))
            self.assertGreater(int(np.abs(data).max()), 10000)

    def test_concat_and_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2 = Path(tmp) / "1.wav", Path(tmp) / "2.wav"
            ax.write_wav(p1, self._tone(0.5))
            ax.write_wav(p2, self._tone(0.25))
            data, _ = ax.concat_wavs([p1, p2])
            self.assertEqual(len(data), int(ax.RATE * 0.75))
            self.assertAlmostEqual(ax.duration_seconds([p1, p2]), 0.75, places=2)

    def test_export_wav_and_mp3(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "1.wav"
            ax.write_wav(p1, self._tone(1.0))
            out = ax.export_recording([p1], Path(tmp) / "rec.wav")
            self.assertTrue(out.is_file() and out.stat().st_size > 20000)
            mp3 = ax.export_recording([p1], Path(tmp) / "rec.mp3")
            # PyAV ships libmp3lame in this venv; if a build lacked it the
            # export must still produce a playable WAV instead of failing.
            self.assertTrue(mp3.is_file())
            self.assertIn(mp3.suffix, (".mp3", ".wav"))
            if mp3.suffix == ".mp3":
                self.assertGreater(mp3.stat().st_size, 2000)

    def test_export_nothing(self):
        self.assertIsNone(ax.export_recording([], "x.wav"))

    def test_clipping_is_safe(self):
        loud = np.array([2.0, -2.0, 0.0], dtype=np.float32)
        ints = ax.float_to_int16(loud)
        self.assertEqual(list(ints), [32767, -32767, 0])


class TestMeetingStore(unittest.TestCase):
    def _make(self, root, name, meta=None, transcript="", notes="", parts=0):
        d = Path(root) / name
        d.mkdir(parents=True)
        if meta is not None:
            (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        if transcript:
            (d / "transcript.txt").write_text(transcript, encoding="utf-8")
        if notes:
            (d / "notes.md").write_text(notes, encoding="utf-8")
        for i in range(parts):
            (d / f"audio_part{i + 1}.wav").write_bytes(b"RIFF")
        return d

    def test_list_newest_first_and_skips_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp, "20260101_090000", {"title": "Old", "duration_sec": 60,
                                                 "timestamp": "2026-01-01 09:00:00"},
                       transcript="hi", notes="## Summary\n- first point", parts=2)
            self._make(tmp, "20260301_090000", None, transcript="later one")
            (Path(tmp) / "20260201_000000").mkdir()          # aborted, empty
            with patch.object(ms.storage, "path_for", return_value=Path(tmp)):
                items = ms.list_meetings()
        self.assertEqual([i["id"] for i in items], ["20260301_090000", "20260101_090000"])
        old = items[1]
        self.assertEqual(old["title"], "Old")
        self.assertEqual(len(old["audio_parts"]), 2)
        self.assertTrue(old["has_notes"] and old["has_transcript"])
        # Folder without meta gets a timestamp derived from its name.
        self.assertEqual(items[0]["timestamp"], "2026-03-01 09:00:00")
        self.assertEqual(items[0]["title"], "Untitled Meeting")

    def test_audio_parts_order_and_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make(tmp, "m", {}, parts=0)
            for n in (1, 2, 10):        # 10 must sort after 2 (natural order)
                (d / f"audio_part{n}.wav").write_bytes(b"RIFF")
            names = [p.name for p in ms.audio_parts(d)]
            self.assertEqual(names, ["audio_part1.wav", "audio_part2.wav", "audio_part10.wav"])
            self.assertEqual(ms.next_audio_part_path(d).name, "audio_part11.wav")
            empty = self._make(tmp, "e", {})
            self.assertEqual(ms.next_audio_part_path(empty).name, "audio_part1.wav")

    def test_summary_preview_and_duration(self):
        self.assertEqual(ms.summary_preview("## Summary\n- The team agreed on Friday."),
                         "The team agreed on Friday.")
        self.assertEqual(ms.summary_preview(""), "")
        # No Markdown symbols on the list card.
        self.assertEqual(ms.summary_preview("**Decisions:**\n- [ ] Fix the **speaker** drift"),
                         "Fix the speaker drift")
        self.assertEqual(ms.summary_preview("> a quote\n\nThe **Live Assistant** ships.\nMore"),
                         "The Live Assistant ships.")
        self.assertEqual(ms.summary_preview("1. Launch on `Friday`"), "Launch on Friday")
        self.assertTrue(ms.summary_preview("x " * 200).endswith("…"))
        self.assertEqual(ms.format_duration(3725), "1 h 02 min")
        self.assertEqual(ms.format_duration(65), "1 min 05 s")

    def test_lists_meetings_with_only_a_recording_or_chunks(self):
        # Transcription/notes failed: the recording, and live chunks of a
        # meeting that reached processing (meta.json), must still be reachable
        # from History. Chunks alone are the meeting being recorded right now,
        # or one discarded before 1.9.1 - never listed; so is meta.json alone.
        with tempfile.TemporaryDirectory() as tmp:
            rec_only = self._make(tmp, "20260927_100000")
            _write_wav(rec_only / "audio_part1.wav", seconds=2)
            chunks_only = self._make(tmp, "20260927_110000")
            _write_chunks(chunks_only, [(1, "second."), (0, "First,")])
            discarded = self._make(tmp, "20260927_120000")
            _write_chunks(discarded, [(0, "thrown away")])
            ms.mark_discarded(discarded)
            late = self._make(tmp, "20260927_130000")        # a chunk landing after Abort
            (late / ms.DISCARDED_CHUNKS).write_text("", encoding="utf-8")
            _write_chunks(late, [(0, "late chunk")])
            bad_meta = self._make(tmp, "20260927_140000", {"duration_sec": "n/a", "title": 7})
            _write_chunks(bad_meta, [(0, "processing failed")])
            self._make(tmp, "20260927_150000")               # empty: skipped
            self._make(tmp, "20260927_160000", {"title": "Nothing captured"})   # meta only
            with patch.object(ms.storage, "path_for", return_value=Path(tmp)):
                items = {i["id"]: i for i in ms.list_meetings()}
            self.assertEqual(set(items), {"20260927_100000", "20260927_140000"})
            rec = items["20260927_100000"]
            # Every field the History card reads is present and sensible.
            self.assertEqual(rec["title"], "Untitled Meeting")
            self.assertEqual(rec["timestamp"], "2026-09-27 10:00:00")
            self.assertEqual(rec["duration_sec"], 2)           # from the WAV header
            self.assertEqual(rec["attendees"], "")
            self.assertEqual(len(rec["audio_parts"]), 1)
            self.assertFalse(rec["has_transcript"] or rec["has_notes"])
            self.assertEqual(items["20260927_140000"]["duration_sec"], 0)
            self.assertEqual(items["20260927_140000"]["title"], "7")
            # No transcript.txt: the detail view / Resume get the chunk text,
            # in spoken order.
            self.assertEqual(ms.load_transcript(chunks_only), "First, second.")
            self.assertEqual(ms.load_transcript(rec_only), "")
            self.assertTrue((discarded / ms.DISCARDED_CHUNKS).is_file())

    def test_transcript_file_wins_over_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make(tmp, "m", transcript="Final transcript.")
            _write_chunks(d, [(0, "live chunk")])
            self.assertEqual(ms.load_transcript(d), "Final transcript.")
            # A resumed folder repeats chunk indices per segment: keep file order.
            r = self._make(tmp, "r")
            _write_chunks(r, [(0, "one"), (1, "two"), (0, "three")])
            with open(r / "chunks.jsonl", "a", encoding="utf-8") as f:
                f.write('{"index": 1, "text": "cut sho')        # crash mid-write
            self.assertEqual(ms.load_transcript(r), "one two three")


def _write_wav(path, seconds=1, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(rate * seconds))


def _write_chunks(folder, rows):
    with open(Path(folder) / "chunks.jsonl", "a", encoding="utf-8") as f:
        for idx, text in rows:
            f.write(json.dumps({"index": idx, "text": text, "language": "en"}) + "\n")


class TestMeetingDeletion(unittest.TestCase):
    """Clear History deletes meetings too - it must never reach outside the
    library, and never the meeting still being recorded."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "meetings"
        self.root.mkdir()
        p = patch.object(ms.storage, "path_for", return_value=self.root)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def _meeting(self, name):
        d = self.root / name
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"title": name}), encoding="utf-8")
        (d / "audio_part1.wav").write_bytes(b"RIFF")
        return d

    def test_delete_one_meeting(self):
        d = self._meeting("20260926_210641")
        self.assertTrue(ms.delete_meeting(d))
        self.assertFalse(d.exists())
        self.assertFalse(ms.delete_meeting(d))              # already gone

    def test_refuses_anything_outside_the_library(self):
        outside = Path(self._tmp.name) / "important"
        outside.mkdir()
        self.assertFalse(ms.delete_meeting(outside))
        self.assertFalse(ms.delete_meeting(self.root))      # the library itself
        self.assertFalse(ms.delete_meeting(self.root / ".." / "important"))
        self.assertTrue(outside.exists() and self.root.exists())

    def test_delete_all_keeps_the_active_meeting(self):
        self._meeting("20260101_000000")
        self._meeting("20260102_000000")
        active = self._meeting("20260103_000000")
        (self.root / "20260104_000000").mkdir()             # an aborted, empty leftover
        self.assertEqual(ms.delete_all_meetings(keep=active), 3)
        self.assertEqual([p.name for p in self.root.iterdir()], ["20260103_000000"])
        self.assertEqual(len(ms.list_meetings()), 1)


# ── Meeting session logic, without a display ─────────────────────────────────
# The recording engine moved out of the 1.x MeetingsWindow into
# meeting_session. MeetingEngine is that session without Qt (plain callback
# signals), so these characterization tests run it directly - same fakes,
# same assertions as when they borrowed the window's methods. The window
# itself retired in Gusan 2.0: what it did on top (Start from its pickers,
# Resume, Abort, the Done page's notices) is checked on the session API the
# Meetings page and Live Assistant call. Widgets are tiny fakes.

import meeting_session as msess  # noqa: E402


class _Emitter:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _Rec:
    """Just the AudioRecorder surface the meeting session touches."""

    def __init__(self, result=("", "en"), truncated=False):
        self.result = result
        self._full_audio_truncated = truncated
        self._full_audio_max = 16000 * 60 * 120
        self._loopback_peak = 1.0
        self.on_chunk_complete = self.on_levels = None
        self.on_partial = self.on_lang_detected = None
        self.on_source_levels = None
        self.fast_live_chunks = False
        self.started = None
        self.stops = 0

    def transcribe(self):
        return self.result

    def start_recording(self, **kw):
        self.started = kw

    def stop_recording(self):
        self.stops += 1


class _App:
    def __init__(self, recorder=None, **cfg):
        self.cfg = {"meeting_consent_ack": True, "save_history": False,
                    "meeting_audio_mode": "default_mic", "whisper_model": "base",
                    "action_model": "managed", **cfg}
        self.recorder = recorder or _Rec()
        self.live_assist = None
        self.saves = 0

    def save_config(self):
        self.saves += 1

    def track(self, *a, **k):
        pass

    def is_pro(self):
        return True


def _session(app, meeting_dir=None, chunks=(), prior=""):
    s = msess.MeetingEngine(app)
    s.state = s.STATE_PROCESSING
    s._chunks = list(chunks)
    s._meeting_dir = meeting_dir
    s._meeting_title, s._meeting_attendees = "Sync", ""
    s._resume_dir, s._resume_prior, s._resume_prior_duration = None, prior, 0
    s.proc_signals = types.SimpleNamespace(finished=_Emitter())
    s.statuses = []
    s._emit_status = s.statuses.append
    s._save_recording_segment = lambda: None
    s.summarized = []
    s._summarize_and_finish = lambda: s.summarized.append(s._final_transcript)
    s._build_attributed_transcript = lambda: ""
    s.spawned = []
    s._spawn = lambda target, *args: s.spawned.append((target, args))
    return s


def _chunk(idx, text):
    return {"index": idx, "text": text, "language": "en"}


class TestMeetingTranscript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _run(self, result, chunks=(), prior="", truncated=False):
        s = _session(_App(_Rec(result, truncated)), self.dir, chunks, prior)
        s._process_meeting_notes()
        return s

    def test_one_failed_chunk_keeps_every_chunk_that_transcribed(self):
        # Chunks land in completion order; chunk 1 failed on the cloud.
        w = self._run(("", "!managed:Managed cloud error (503)."),
                      chunks=[_chunk(2, "third part."), _chunk(0, "First part,")])
        self.assertEqual(w.proc_signals.finished.calls, [])     # no error page / reset
        self.assertEqual(len(w.summarized), 1)
        self.assertTrue(w._final_transcript.startswith("First part, third part."))
        self.assertIn("— Part of the audio couldn't be transcribed —", w._final_transcript)
        self.assertEqual(w._final_lang, "en")
        # Written for the Done page, Retry and History.
        self.assertEqual((self.dir / "transcript.txt").read_text(encoding="utf-8"),
                         w._final_transcript)
        self.assertIn("transcribed: Managed cloud error (503). The", w._transcript_notices[0])

    def test_failure_with_nothing_transcribed_still_reports_the_error(self):
        w = self._run(("", "!managed:Managed cloud error (503)."))
        self.assertEqual(w.proc_signals.finished.calls, [("", "Managed cloud error (503).")])
        self.assertEqual(w.summarized, [])

    def test_failure_in_a_resumed_meeting_keeps_the_earlier_transcript(self):
        w = self._run(("", "!transcribe:Timed out."), prior="Earlier talk.")
        self.assertEqual(len(w.summarized), 1)
        self.assertTrue(w._final_transcript.startswith("Earlier talk.\n\n— Resumed "))
        self.assertIn("couldn't be transcribed", w._final_transcript)

    def test_speaker_pass_covers_failed_chunks_but_not_a_dead_device(self):
        for lang, expect_notice in (("!managed:503", False), ("!audio:device lost", True)):
            w = _session(_App(_Rec(("", lang))), self.dir, [_chunk(0, "hello")])
            w._build_attributed_transcript = lambda: "Speaker 1: hello there"
            w._process_meeting_notes()
            self.assertTrue(w._final_transcript.startswith("Speaker 1: hello there"))
            self.assertEqual(bool(w._transcript_notices), expect_notice, lang)
            if expect_notice:
                self.assertIn("stopped early: device lost.", w._transcript_notices[0])

    def test_transcript_is_not_doubled(self):
        # Speaker labelling unavailable: the transcript is transcribe()'s joined
        # text (ordered, with the final tail) - exactly once.
        joined = "Hello team. We ship Friday. Any questions?"
        w = self._run((joined, "en"), chunks=[_chunk(0, "Hello team."),
                                              _chunk(1, "We ship Friday.")])
        self.assertEqual(w._final_transcript, joined)
        self.assertEqual(w.summarized, [joined])

    def test_long_meeting_keeps_the_full_transcript(self):
        # Past the recorder's full-audio cap, the speaker pass would only see
        # the first 2 h and replace the complete transcript - it must not run.
        full = " ".join(f"minute {m}." for m in range(150))
        w = _session(_App(_Rec((full, "en"), truncated=True)), self.dir)
        w._build_attributed_transcript = MagicMock(return_value="Speaker 1: minute 0.")
        w._process_meeting_notes()
        w._build_attributed_transcript.assert_not_called()
        self.assertEqual(w._final_transcript, full)
        self.assertIn("minute 149.", (self.dir / "transcript.txt").read_text(encoding="utf-8"))
        self.assertIn("2 h 00 min", w._transcript_notices[0])
        # ...and a shorter meeting still gets its speaker labels.
        w = _session(_App(_Rec((full, "en"))), self.dir)
        w._build_attributed_transcript = lambda: "Speaker 1: all of it"
        w._process_meeting_notes()
        self.assertEqual(w._final_transcript, "Speaker 1: all of it")
        self.assertEqual(w._transcript_notices, [])

    def test_a_recording_that_ends_before_the_transcript_marks_the_meeting(self):
        # ...so Identify speakers never rebuilds the transcript from it later (it would
        # replace the whole meeting with the part the recording has).
        full = " ".join(f"minute {m}." for m in range(150))

        def run(name, truncated, saved, result=(full, "en"), prior=""):
            d = self.dir / name
            d.mkdir()
            w = _session(_App(_Rec(result, truncated)), d, prior=prior)
            w._save_recording_segment = lambda: (d / "audio_part1.wav") if saved else None
            w._process_meeting_notes()
            self.assertEqual(len(w.summarized), 1, name)               # the notes are written all the same
            return ms.load_meta(d).get(ms.META_RECORDING_CUT_SHORT)
        self.assertIs(run("capped", True, True), True)                  # past the ~2-hour cap
        self.assertIs(run("unsaved", False, False), True)               # this part's sound could not be saved
        self.assertIsNone(run("whole", False, True))                    # the recorder kept all of it
        self.assertIsNone(run("nothing new", False, False, ("", "en"), prior="Earlier talk."))

    def test_done_page_shows_the_notices_above_the_notes(self):
        s = _session(_App(), self.dir)
        s._final_transcript = "t"
        s._transcript_notices = ["The saved recording ends at about 2 h 00 min."]
        done = []
        s.finished.connect(lambda *a: done.append(a))
        with patch.object(msess.telemetry, "track"):
            s._on_processing_finished("## Notes", "")
        self.assertEqual(s._final_notes, "## Notes")      # exports stay clean
        self.assertEqual(done, [(str(self.dir), "## Notes", "")])
        self.assertEqual(s.phase, "done")
        # The notes view (Meetings page) draws the notices above the notes:
        # from the session, and from the meeting's meta.json afterwards.
        notice = "The saved recording ends at about 2 h 00 min."
        self.assertEqual(s.notices(), [notice])
        self.assertEqual(s._result_meta()["notices"], [notice])
        self.assertNotIn("2 h 00 min", s.notes)


class TestMeetingStartState(unittest.TestCase):
    """Settings changes survive a Start, and a Start after a finished (or a
    failed resumed) session is a genuinely new meeting."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "meetings"
        self.root.mkdir()
        for p in (patch.object(ms.storage, "path_for", return_value=self.root),
                  patch.object(msess, "has_loopback", return_value=True),
                  patch.object(msess.telemetry, "track")):
            p.start()
            self.addCleanup(p.stop)

    def _idle(self, app, *a, **kw):
        s = _session(app, *a, **kw)
        s.state = s.STATE_IDLE
        return s

    def test_start_uses_settings_changed_after_launch_without_saving(self):
        app = _App()
        s = self._idle(app)
        # Settings saved new picks after the session was built at launch.
        app.cfg.update(whisper_model="large-v3", action_model="api_cerebras",
                       meeting_audio_mode="smart_meeting", meeting_notes_model="qwen_3b")
        ok, code, _msg = s.start(language="en")
        self.assertTrue(ok, code)
        self.assertEqual(s.state, s.STATE_RECORDING)
        self.assertEqual((app.cfg["whisper_model"], app.cfg["action_model"],
                          app.cfg["meeting_audio_mode"]),
                         ("large-v3", "api_cerebras", "smart_meeting"))
        self.assertEqual(app.saves, 0)
        self.assertEqual(s._notes_engine, "qwen_3b")              # the saved meeting pick, live
        self.assertEqual(app.recorder.started, {"capture_mode": "smart_meeting",
                                                "language": "en"})

    def test_live_assist_hears_both_sides_without_changing_the_saved_mode(self):
        app = _App(meeting_audio_mode="default_mic")
        s = self._idle(app)
        ok, code, _msg = s.start(language="en", audio_mode="smart_meeting")
        self.assertTrue(ok, code)
        self.assertEqual(app.recorder.started["capture_mode"], "smart_meeting")
        self.assertEqual(app.cfg["meeting_audio_mode"], "default_mic")    # saved pick kept
        self.assertEqual(s._session_audio_mode, "smart_meeting")

    def test_a_mode_this_system_cannot_capture_falls_back(self):
        app = _App(meeting_audio_mode="default_mic")
        s = self._idle(app)
        with patch.object(msess, "has_loopback", return_value=False):    # no loopback here
            s.start(language="en", audio_mode="smart_meeting")
        self.assertEqual(app.recorder.started["capture_mode"], "default_mic")

    def test_resync_is_read_only_and_a_pick_here_is_saved(self):
        app = _App(action_model="rule_based")        # not offered as a notes engine
        self.assertEqual(msess.default_engine_id(app), "managed")   # Pro default shown and used
        s = self._idle(app)
        s.start(language="en", notes_engine="api_cerebras")       # this meeting's pick...
        self.assertEqual(s._notes_engine, "api_cerebras")
        self.assertEqual((app.cfg["action_model"], app.saves), ("rule_based", 0))   # ...never saved
        # A pick made to keep (Rewrite notes with persist) is written once.
        done = _session(app, self.root / "20260101_080000")
        done.state = done.STATE_DONE
        done._final_transcript = "We agreed on Friday."
        self.assertTrue(done.retry_notes("qwen_3b", persist=True))
        self.assertEqual((app.cfg["action_model"], app.saves), ("qwen_3b", 1))
        self.assertEqual(done._notes_engine, "qwen_3b")

    def test_live_assist_start_after_done_is_a_new_meeting(self):
        old = self.root / "20260101_090000"
        old.mkdir()
        app = _App()
        s = _session(app, old, prior="Old meeting transcript.")
        s._resume_dir = old                     # a resumed meeting...
        s._meeting_title, s._meeting_attendees = "Board meeting", "CEO, CFO"
        s._final_transcript = "Old meeting transcript."
        s._on_processing_finished("", "quota exceeded")   # ...whose summary failed
        self.assertEqual(s.state, s.STATE_DONE)
        self.assertEqual(s.phase, "failed")
        self.assertEqual(s._resume_dir, old)                  # kept for Retry
        # What the Live Assistant card does: a new, titled session.
        ok, code, _msg = s.start(title="Live session 11:00", language="en", source="live_assist")
        self.assertTrue(ok, code)
        self.assertEqual(s.state, s.STATE_RECORDING)
        self.assertNotEqual(s._meeting_dir, old)
        self.assertEqual(s._meeting_dir.parent, self.root)
        self.assertEqual((s._resume_dir, s._resume_prior), (None, ""))
        self.assertEqual((s._meeting_title, s._meeting_attendees), ("Live session 11:00", ""))

    def test_resume_keeps_its_folder(self):
        old = self.root / "20260101_090000"
        old.mkdir()
        (old / "transcript.txt").write_text("ORIGINAL", encoding="utf-8")
        (old / "meta.json").write_text(json.dumps({"title": "Board", "duration_sec": 60}),
                                       encoding="utf-8")
        s = _session(_App())
        s.state = s.STATE_DONE
        ok, code, _msg = s.start(resume_dir=str(old))          # the 1.x window's Resume
        self.assertTrue(ok, code)
        self.assertEqual(s.state, s.STATE_RECORDING)
        self.assertEqual(s._meeting_dir, old)
        self.assertEqual((s._resume_prior, s._meeting_title), ("ORIGINAL", "Board"))
        # The same through continue_meeting (the notes view's Continue).
        s2 = _session(_App())
        s2.state = s2.STATE_DONE
        ok, _code, _msg = s2.continue_meeting(str(old))
        self.assertTrue(ok)
        self.assertEqual((s2._meeting_dir, s2._resume_prior, s2._meeting_title),
                         (old, "ORIGINAL", "Board"))
        self.assertEqual(s2._resume_prior_duration, 60)

    def test_meetings_started_in_the_same_second_get_their_own_folders(self):
        (self.root / "20260927_100000").mkdir()
        s = _session(_App())
        s.state = s.STATE_DONE
        with patch.object(msess.time, "strftime", return_value="20260927_100000"):
            s.start()
        self.assertEqual(s._meeting_dir.name, "20260927_100000_2")
        self.assertEqual(ms._folder_timestamp(s._meeting_dir.name), "2026-09-27 10:00:00")

    def test_abort_keeps_a_discarded_session_out_of_history(self):
        s = self._idle(_App())
        s.start()
        _write_chunks(s._meeting_dir, [(0, "never mind")])
        s.abort()
        self.assertEqual(s.state, s.STATE_IDLE)
        self.assertEqual(ms.list_meetings(), [])
        # A resumed meeting's folder is left alone.
        old = self.root / "20260101_090000"
        old.mkdir()
        (old / "transcript.txt").write_text("ORIGINAL", encoding="utf-8")
        s.continue_meeting(str(old))
        _write_chunks(old, [(0, "more")])
        s.abort()
        self.assertTrue((old / "chunks.jsonl").is_file())
        self.assertEqual([m["id"] for m in ms.list_meetings()], ["20260101_090000"])


# ── Gusan 2.0.1: the two sources next to a meeting's recording ───────────────

@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestTrackFiles(unittest.TestCase):
    """audio_partN_mic.wav / audio_partN_system.wav: kept with the meeting,
    never mistaken for a recording part, cleaned up with it, optional."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "meetings"
        self.root.mkdir()
        p = patch.object(ms.storage, "path_for", return_value=self.root)
        p.start()
        self.addCleanup(p.stop)

    def _meeting(self, name="20261021_103134", parts=(1,), tracks=(1,), transcript="You: hi"):
        d = self.root / name
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"title": "Call", "duration_sec": 3}), encoding="utf-8")
        if transcript:
            (d / "transcript.txt").write_text(transcript, encoding="utf-8")
        tone = (np.sin(np.arange(16000 * 3) / 30.0) * 0.3).astype(np.float32)
        for n in parts:
            ax.write_wav(d / f"audio_part{n}.wav", tone)
        for n in tracks:
            ax.write_wav_int16(ms.track_path(d / f"audio_part{n}.wav", "mic"), ax.float_to_int16(tone))
            ax.write_wav_int16(ms.track_path(d / f"audio_part{n}.wav", "system"), ax.float_to_int16(tone * 0.5))
        return d

    def test_track_paths_sit_next_to_their_part(self):
        self.assertEqual(ms.track_path(Path("x") / "audio_part2.wav", "mic").name, "audio_part2_mic.wav")
        self.assertEqual(ms.track_path("audio_part10.wav", "system").name, "audio_part10_system.wav")
        with self.assertRaises(ValueError):
            ms.track_path("audio_part1.wav", "both")

    def test_tracks_are_never_recording_parts(self):
        d = self._meeting(parts=(1, 2), tracks=(1, 2))
        self.assertEqual([p.name for p in ms.audio_parts(d)], ["audio_part1.wav", "audio_part2.wav"])
        self.assertEqual(ms.next_audio_part_path(d).name, "audio_part3.wav")
        item = ms.list_meetings()[0]
        self.assertEqual([Path(p).name for p in item["audio_parts"]], ["audio_part1.wav", "audio_part2.wav"])
        self.assertEqual(item["duration_sec"], 3)

    def test_audio_tracks_pairs_each_part_with_its_two_files(self):
        d = self._meeting(parts=(1, 2, 3), tracks=(1, 3))
        got = [(p.name, m.name if m else None, s.name if s else None) for p, m, s in ms.audio_tracks(d)]
        self.assertEqual(got, [("audio_part1.wav", "audio_part1_mic.wav", "audio_part1_system.wav"),
                               ("audio_part2.wav", None, None),
                               ("audio_part3.wav", "audio_part3_mic.wav", "audio_part3_system.wav")])
        self.assertEqual(ms.audio_tracks(self.root / "nothing"), [])

    def test_a_half_written_or_empty_track_does_not_count(self):
        d = self._meeting(tracks=())
        (d / "audio_part1_mic.wav").write_bytes(b"")
        (d / "audio_part1_system.wav.part").write_bytes(b"RIFF" + b"\0" * 500)    # an unfinished write
        self.assertEqual([(m, s) for _p, m, s in ms.audio_tracks(d)], [(None, None)])

    def test_deleting_one_or_all_meetings_removes_the_tracks(self):
        a = self._meeting("20261021_103134")
        b = self._meeting("20261021_113134")
        self.assertTrue((a / "audio_part1_system.wav").is_file())
        self.assertTrue(ms.delete_meeting(a))
        self.assertFalse(a.exists())
        self.assertEqual(ms.delete_all_meetings(), 1)
        self.assertFalse(b.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_meeting_that_has_only_its_tracks_is_not_listed_as_a_recording(self):
        d = self.root / "20261021_090000"
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"title": "x"}), encoding="utf-8")
        ax.write_wav_int16(d / "audio_part1_mic.wav", np.zeros(1000, np.int16))
        self.assertEqual(ms.list_meetings(), [])

    def test_the_old_text_kept_by_identify_speakers_is_not_a_transcript(self):
        d = self.root / "20261021_090000"
        d.mkdir()
        (d / ms.BEFORE_SPEAKERS).write_text("old", encoding="utf-8")
        self.assertEqual(ms.list_meetings(), [])
        self.assertEqual(ms.load_transcript(d), "")
        self.assertFalse(ms.has_transcript_file(d))

    def test_transcript_files_are_written_whole(self):
        d = self.root / "20261021_090000"
        d.mkdir()
        self.assertTrue(ms.write_transcript(d, "You: one\nSpeaker 1: two"))
        self.assertEqual(ms.load_transcript(d), "You: one\nSpeaker 1: two")
        self.assertTrue(ms.has_transcript_file(d))
        self.assertTrue(ms.write_transcript(d, "You: three"))
        self.assertEqual(ms.load_transcript(d), "You: three")
        self.assertEqual([p.name for p in d.iterdir()], ["transcript.txt"])      # no temp file left
        self.assertFalse(ms.write_transcript(self.root / "gone", "x"))
        (d / "transcript.txt").write_text("  \n", encoding="utf-8")
        self.assertFalse(ms.has_transcript_file(d))                  # blank is no transcript

    def test_has_transcript_file_ignores_live_chunks(self):
        d = self.root / "20261021_090000"
        d.mkdir()
        with open(d / "chunks.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"index": 0, "text": "live words"}) + "\n")
        self.assertEqual(ms.load_transcript(d), "live words")
        self.assertFalse(ms.has_transcript_file(d))

    def test_int16_round_trip_and_float_read(self):
        p = Path(self._tmp.name) / "t.wav"
        data = (np.arange(-1000, 1000) * 30).astype(np.int16)
        self.assertEqual(ax.write_wav_int16(p, data), p)
        got, rate = ax.read_wav_int16(p)
        self.assertEqual(rate, ax.RATE)
        self.assertTrue(np.array_equal(got, data))
        f, _ = ax.read_wav_float32(p)
        self.assertEqual(f.dtype, np.float32)
        self.assertAlmostEqual(float(f[-1]), data[-1] / 32767.0, places=6)
        self.assertEqual(list(p.parent.glob("t.wav.part")), [])

    def test_a_failed_write_leaves_neither_the_file_nor_a_temp(self):
        p = Path(self._tmp.name) / "u.wav"
        with patch("os.replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                ax.write_wav_int16(p, np.zeros(100, np.int16))
        self.assertEqual([x.name for x in p.parent.iterdir() if x.name.startswith("u.wav")], [])
        ax.write_wav_int16(p, np.ones(10, np.int16))
        with patch("os.replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                ax.write_wav_int16(p, np.zeros(100, np.int16))
        self.assertEqual(len(ax.read_wav_int16(p)[0]), 10)         # the earlier file is intact


def _wav_of(path, seconds):
    """A WAV whose header says it is ``seconds`` long - one frame a second, so a
    two-hour recording is a few kilobytes here."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(1)
        w.writeframes(b"\0\0" * int(seconds))
    return path


class TestRecordingCutShort(unittest.TestCase):
    """A meeting whose saved recording ends before its transcript does (past the
    recorder's ~2-hour cap): rebuilding the transcript from it would lose the rest."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_part_seconds_reads_only_the_headers(self):
        a = _wav_of(self.dir / "audio_part1.wav", 7200)
        b = self.dir / "audio_part2.wav"
        b.write_bytes(b"RIFF" + b"\0" * 100)                       # unreadable: unknown, not 0
        self.assertEqual(ms.part_seconds([a, b, self.dir / "gone.wav"]), [7200.0, None, None])
        self.assertEqual(ms.part_seconds([]), [])

    def test_meta_or_a_2_0_notice_says_so(self):
        self.assertTrue(ms.recording_cut_short({ms.META_RECORDING_CUT_SHORT: True}))
        notice = ("The saved recording ends at about 2 h 00 min - the most one session keeps. "
                  "The transcript and notes still cover the whole meeting (without speaker labels).")
        self.assertTrue(ms.recording_cut_short({"notices": ["Something else.", notice]}))
        self.assertTrue(notice.startswith(ms.CAP_NOTICE_START))
        for meta in ({}, None, "x", {"notices": "not a list"}, {ms.META_RECORDING_CUT_SHORT: False},
                     {"notices": ["Gusan was closed during this meeting."]}):
            self.assertFalse(ms.recording_cut_short(meta), meta)

    def test_an_older_meeting_is_told_by_a_part_that_reached_the_cap(self):
        cap = ms.RECORDING_CAP_SEC
        self.assertTrue(ms.recording_cut_short({"duration_sec": 3 * 3600}, [cap + 0.04]))
        self.assertTrue(ms.recording_cut_short({"duration_sec": 3 * 3600}, [cap, 600.0]))     # continued later
        self.assertTrue(ms.recording_cut_short({"duration_sec": 3 * 3600}, [cap, None]))      # one unreadable
        # stopped right at the cap: the clock and the sound differ by seconds, not by a margin
        self.assertFalse(ms.recording_cut_short({"duration_sec": cap + 20}, [cap]))
        # shorter than the meeting but nowhere near the cap: a system-sound-only meeting keeps
        # nothing while the computer is silent - that recording still covers every word
        self.assertFalse(ms.recording_cut_short({"duration_sec": 3600}, [1800.0]))
        self.assertFalse(ms.recording_cut_short({"duration_sec": 3 * 3600}, [None]))
        self.assertFalse(ms.recording_cut_short({"duration_sec": "bad"}, [cap]))


class TestMeetingListNeverBreaksOnOldFolders(unittest.TestCase):
    def test_a_meeting_from_before_the_tracks_lists_and_reads_as_ever(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "20260101_090000"
            d.mkdir()
            (d / "meta.json").write_text(json.dumps({"title": "Old", "duration_sec": 60}), encoding="utf-8")
            (d / "audio_part1.wav").write_bytes(b"RIFF")
            (d / "audio_part2.wav").write_bytes(b"RIFF")
            (d / "transcript.txt").write_text("Speaker 1: hello", encoding="utf-8")
            with patch.object(ms.storage, "path_for", return_value=root):
                items = ms.list_meetings()
                tracks = ms.audio_tracks(d)
        self.assertEqual(len(items[0]["audio_parts"]), 2)
        self.assertEqual([(m, s) for _p, m, s in tracks], [(None, None), (None, None)])


if __name__ == "__main__":
    unittest.main()
