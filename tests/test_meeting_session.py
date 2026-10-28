"""Gusan 2.0 meetings: the session without widgets (pause, two meters, live
lines, My notes, start refusals, outcome and metadata, Retry / Rewrite /
Continue), the meeting library additions, and the recorder's pause and
per-source levels on scripted audio.

Runs the Qt-free MeetingEngine - the same code as the app's MeetingSession -
so it works under tests/test_core.py's suite-wide PySide6 / numpy stubs."""
import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock, patch

import meeting_session as msess
import meeting_store as ms


# ── fakes ────────────────────────────────────────────────────────────────────

class _Rec:
    """The AudioRecorder surface the session touches, recording every call."""

    def __init__(self, result=("Hello team. We ship Friday.", "en"), fail_start=None):
        self.result = result
        self.fail_start = fail_start
        self._full_audio_truncated = False
        self._full_audio_max = 16000 * 60 * 120
        self._loopback_peak = 1.0
        self.on_chunk_complete = self.on_levels = None
        self.on_partial = self.on_lang_detected = None
        self.on_source_levels = None
        self.fast_live_chunks = False
        self.partial_text = ""
        self.started = None
        self.stops = 0
        self.paused_calls = []
        self.sources = {}

    def start_recording(self, **kw):
        if self.fail_start:
            raise self.fail_start
        self.started = kw

    def stop_recording(self):
        self.stops += 1

    def transcribe(self):
        return self.result

    def get_full_audio(self):
        return None

    def set_paused(self, on):
        self.paused_calls.append(on)

    def chunk_source_energy(self, idx):
        return self.sources.get(idx)


class _App:
    def __init__(self, recorder=None, pro=True, **cfg):
        self.cfg = {"meeting_consent_ack": True, "save_history": False,
                    "meeting_audio_mode": "default_mic", "whisper_model": "base",
                    "action_model": "managed", **cfg}
        self.recorder = recorder or _Rec()
        self.live_assist = None
        self.saves = 0
        self.tracked = []
        self.released = 0
        self.pro = pro
        self.engine_calls = []

    def save_config(self):
        self.saves += 1

    def track(self, event, props=None):
        self.tracked.append((event, props))

    def is_pro(self):
        return self.pro

    def release_models_after_session(self):
        self.released += 1

    def _resolve_action_engine(self, action_model=None):
        self.engine_calls.append(action_model)
        model = action_model or self.cfg.get("action_model")
        return model, {**self.cfg, "_managed_token": "tok"} if self.pro else self.cfg


class _Recorder:
    """Collects a pure signal's emissions."""

    def __init__(self, signal):
        self.calls = []
        signal.connect(lambda *a: self.calls.append(a))


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "meetings"
        self.root.mkdir()
        for p in (patch.object(ms.storage, "path_for", return_value=self.root),
                  patch.object(msess.storage, "path_for", return_value=self.root),
                  patch.object(msess, "has_loopback", return_value=True),
                  patch.object(msess.telemetry, "track")):
            p.start()
            self.addCleanup(p.stop)

    def session(self, app=None, inline=True):
        s = msess.MeetingEngine(app or _App())
        s.spawned = []
        if inline:
            s._spawn = lambda target, *args: target(*args)
        else:
            s._spawn = lambda target, *args: s.spawned.append((target, args))
        self.addCleanup(self._let_the_speaker_pass_end, s)      # (runs before the folder is removed)
        return s

    @staticmethod
    def _let_the_speaker_pass_end(s, timeout=5.0):
        """A background speaker pass tells the session it ended from its own worker thread (which
        reads meta.json) a moment AFTER the session says nothing is queued: wait for that thread
        to be gone, or on Windows the folder's removal meets a file it still has open. A queued
        pass whose worker never started (a test that holds the spawns back) is not waited for."""
        follow = getattr(s, "_followup", None)
        end = time.monotonic() + timeout
        while follow is not None and getattr(follow, "_alive", False) and not follow.folders() \
                and time.monotonic() < end:
            time.sleep(0.01)


# ── pure helpers ─────────────────────────────────────────────────────────────

class TestHelpers(unittest.TestCase):
    def test_meter_level_is_a_db_scale(self):
        self.assertEqual(msess.meter_level(0), 0.0)
        self.assertEqual(msess.meter_level(-1), 0.0)
        self.assertEqual(msess.meter_level(None), 0.0)
        self.assertEqual(msess.meter_level(0.001), 0.0)            # -60 dB
        self.assertAlmostEqual(msess.meter_level(0.01), 0.4)        # -40 dB
        self.assertEqual(msess.meter_level(1.0), 1.0)
        self.assertLess(msess.meter_level(0.02), msess.meter_level(0.05))

    def test_speaker_hint_needs_one_side_to_dominate(self):
        self.assertEqual(msess.speaker_hint((9.0, 1.0)), "You")
        self.assertEqual(msess.speaker_hint((1.0, 9.0)), "Them")
        self.assertEqual(msess.speaker_hint((6.0, 4.0)), "")         # both talked
        self.assertEqual(msess.speaker_hint((0.0, 0.0)), "")         # silence
        self.assertEqual(msess.speaker_hint(None), "")
        self.assertEqual(msess.speaker_hint(("x", 1)), "")

    def test_audio_mode_resolution(self):
        self.assertEqual(msess.resolve_audio_mode("system_only", None, True),
                         ("system_only", "system_only"))
        self.assertEqual(msess.resolve_audio_mode("default_mic", "smart_meeting", True),
                         ("smart_meeting", "default_mic"))
        # No loopback (Mac): everything heals to the microphone.
        self.assertEqual(msess.resolve_audio_mode("smart_meeting", "smart_meeting", False),
                         ("default_mic", "default_mic"))
        self.assertEqual(msess.resolve_audio_mode(None, None, True),
                         ("smart_meeting", "smart_meeting"))
        self.assertEqual(msess.available_audio_modes(False), ("default_mic",))

    def test_labels_titles_and_sentences(self):
        self.assertEqual(msess.engine_label("managed"), "Gusan Cloud · Mistral Small")
        self.assertEqual(msess.engine_label("qwen_tiny"), "This computer · Qwen 1.5B")
        self.assertEqual(msess.engine_label("rule_based"), "Built-in formatter")
        self.assertTrue(msess.engine_label("api_cerebras").endswith("· your key"))
        self.assertTrue(msess.default_title(0).startswith("Meeting · "))
        self.assertEqual(msess.split_sentences("One, two. Three? Four"),
                         ["One, two.", "Three?", "Four"])

    def test_has_loopback_reads_the_loaded_main_without_importing_it(self):
        fake = types.ModuleType("main")
        fake.HAS_LOOPBACK = False
        with patch.dict("sys.modules", {"main": fake}):
            self.assertFalse(msess.has_loopback())
        fake.HAS_LOOPBACK = True
        with patch.dict("sys.modules", {"main": fake}):
            self.assertTrue(msess.has_loopback())

    def test_default_engine_prefers_pro_then_a_downloaded_local_model(self):
        self.assertEqual(msess.default_engine_id(_App(pro=True)), "managed")
        with patch("local_llm.model_downloaded", side_effect=lambda mid: mid == "qwen_3b"):
            self.assertEqual(msess.default_engine_id(_App(pro=False)), "qwen_3b")
        with patch("local_llm.model_downloaded", return_value=False):
            self.assertEqual(msess.default_engine_id(_App(pro=False)), "managed")


# ── start ────────────────────────────────────────────────────────────────────

class TestStart(_Base):
    def test_start_records_and_writes_the_meeting_details_at_once(self):
        app = _App()
        s = self.session(app)
        phases = _Recorder(s.state_changed)
        ok, code, msg = s.start("Q4 roadmap", "Anna, David, anna", language="en",
                                source="calendar", calendar_event_id="evt1")
        self.assertEqual((ok, code, msg), (True, "", ""))
        self.assertEqual(s.phase, "recording")
        self.assertEqual(phases.calls, [("recording",)])
        self.assertTrue(s.is_busy)
        self.assertEqual(s.active_dir(), s.meeting_dir)
        self.assertEqual(app.recorder.started, {"capture_mode": "default_mic", "language": "en"})
        # The recorder now reports to the session.
        self.assertEqual(app.recorder.on_chunk_complete, s._on_chunk_transcribed)
        self.assertEqual(app.recorder.on_source_levels, s._on_source_levels)
        meta = ms.load_meta(s.meeting_dir)
        self.assertEqual(meta["title"], "Q4 roadmap")
        self.assertEqual(meta["attendees_list"], ["Anna", "David"])
        self.assertEqual((meta["source"], meta["calendar_event_id"], meta["language"]),
                         ("calendar", "evt1", "en"))
        self.assertRegex(meta["started"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(app.saves, 0)                          # nothing saved to settings

    def test_meeting_settings_come_from_the_config(self):
        app = _App(meeting_language="fr", meeting_notes_model="qwen_3b",
                   meeting_audio_mode="system_only")
        s = self.session(app)
        s.start()
        self.assertEqual(app.recorder.started, {"capture_mode": "system_only", "language": "fr"})
        self.assertEqual(s._notes_engine, "qwen_3b")
        self.assertEqual(s.title, "Untitled Meeting")
        self.assertEqual(s._source, "page")

    def test_live_assistant_start_is_tagged(self):
        app = _App()
        app.recorder.fast_live_chunks = True          # Live Assistant set fast pieces
        s = self.session(app)
        s.start("Live session 10:00")
        self.assertEqual(ms.load_meta(s.meeting_dir)["source"], "live_assist")

    def test_a_capture_mode_this_system_lacks_is_healed_and_saved(self):
        app = _App(meeting_audio_mode="smart_meeting")
        s = self.session(app)
        with patch.object(msess, "has_loopback", return_value=False):
            s.start()
        self.assertEqual(app.recorder.started["capture_mode"], "default_mic")
        self.assertEqual((app.cfg["meeting_audio_mode"], app.saves), ("default_mic", 1))

    def test_heal_goes_through_set_setting_when_the_app_has_it(self):
        app = _App(meeting_audio_mode="smart_meeting")
        calls = []
        app.set_setting = lambda k, v: calls.append((k, v)) or True
        s = self.session(app)
        with patch.object(msess, "has_loopback", return_value=False):
            s.start()
        self.assertEqual(calls, [("meeting_audio_mode", "default_mic")])

    def test_refusals(self):
        errors = []

        def run(app, **kw):
            s = self.session(app)
            s.error.connect(lambda *a: errors.append(a))
            out = s.start(**kw)
            self.assertEqual(s.phase, "idle")
            return out

        self.assertEqual(run(_App(meeting_consent_ack=False))[:2], (False, "consent_needed"))
        app = _App()
        app._file_job_running = True
        self.assertEqual(run(app)[:2], (False, "file_job"))
        app = _App()
        app.recorder = None
        self.assertEqual(run(app)[:2], (False, "no_recorder"))
        self.assertEqual([e[0] for e in errors], ["consent_needed", "file_job", "no_recorder"])

    def test_busy_refuses_a_second_start(self):
        s = self.session()
        s.start("one")
        ok, code, _ = s.start("two")
        self.assertEqual((ok, code), (False, "busy"))
        self.assertEqual(s.title, "one")

    def test_a_device_that_will_not_open_is_not_a_recording(self):
        rec = _Rec(fail_start=OSError("device busy"))
        saved_cb = rec.on_chunk_complete = lambda *a: None
        app = _App(rec)
        s = self.session(app)
        ok, code, msg = s.start("x")
        self.assertEqual((ok, code), (False, "device"))
        self.assertIn("device busy", msg)
        self.assertEqual(s.state, "idle")
        self.assertIs(rec.on_chunk_complete, saved_cb)          # dictation's callback back
        self.assertIn(("meeting_recording_failed", {"reason": "device"}), app.tracked)

    def test_a_folder_that_cannot_be_made(self):
        blocker = Path(self._tmp.name) / "file"
        blocker.write_text("x")
        s = self.session()
        with patch.object(msess.storage, "path_for", return_value=blocker):
            ok, code, _ = s.start("x")
        self.assertEqual((ok, code), (False, "folder"))

    def test_dictation_is_stopped_and_live_assistant_told(self):
        app = _App()
        app.is_rec = True
        app.force_stop_dictation = MagicMock(return_value=True)
        app.show_tray_hint = MagicMock()
        app.live_assist = MagicMock()
        s = self.session(app)
        s.start("Sync", "Anna")
        app.force_stop_dictation.assert_called_once()
        app.show_tray_hint.assert_called_once()
        app.live_assist.set_meeting_active.assert_called_once_with(True, "Sync", "Anna")


# ── pause, levels, live text ─────────────────────────────────────────────────

class TestPauseAndLive(_Base):
    def test_pause_excludes_time_and_keeps_the_dictation_lock(self):
        app = _App()
        s = self.session(app)
        phases = _Recorder(s.state_changed)
        levels = _Recorder(s.levels)
        with patch.object(msess.time, "time", return_value=1000.0):
            s.start("x")
        with patch.object(msess.time, "time", return_value=1060.0):
            self.assertTrue(s.pause())
            self.assertFalse(s.pause())                       # already paused
        self.assertEqual((s.state, s.phase), ("recording", "paused"))   # still owns the recorder
        self.assertTrue(s.is_busy)
        self.assertEqual(levels.calls, [(0.0, 0.0)])          # meters drop to zero
        self.assertEqual(s.elapsed_sec(now=1500.0), 60.0)      # the timer stands still
        with patch.object(msess.time, "time", return_value=1120.0):
            self.assertTrue(s.resume())
        self.assertEqual(s.elapsed_sec(now=1180.0), 120.0)
        self.assertEqual(app.recorder.paused_calls, [True, False])
        self.assertEqual(phases.calls, [("recording",), ("paused",), ("recording",)])
        # Stop while paused: the open pause is not recorded time either.
        with patch.object(msess.time, "time", return_value=1200.0):
            s.pause()
        with patch.object(msess.time, "time", return_value=1500.0):
            s._process_meeting_notes = lambda: None
            s.stop("")
        self.assertEqual(s.elapsed_sec(), 140.0)
        self.assertEqual(s._duration_sec(), 140)
        self.assertEqual(s._meeting_minutes, 2.3)

    def test_levels_only_while_recording(self):
        s = self.session()
        levels = _Recorder(s.levels)
        s._on_source_levels(0.1, 0.0)                         # idle: nothing
        s.start("x")
        s._on_source_levels(0.1, 0.01)
        s.pause()
        s._on_source_levels(0.1, 0.01)
        self.assertEqual(len(levels.calls), 2)                # start's one + the pause zero
        self.assertAlmostEqual(levels.calls[0][0], msess.meter_level(0.1))
        self.assertAlmostEqual(levels.calls[0][1], 0.4)
        self.assertEqual(levels.calls[1], (0.0, 0.0))

    def test_chunks_feed_the_live_text_lines_and_the_folder(self):
        app = _App(meeting_audio_mode="smart_meeting")
        app.recorder.sources = {0: (9.0, 1.0), 1: (0.5, 9.5), 2: (5.0, 5.0)}
        s = self.session(app)
        raw, pieces, lines = _Recorder(s.sig_chunk), _Recorder(s.chunk_text), _Recorder(s.live_line)
        s.start("x")
        s._on_chunk_transcribed(0, "Where are we on pricing?", "en")
        s._on_chunk_transcribed(1, "Almost done. Friday", "en")
        s.pause()                                  # the chunk cut at Pause still lands
        s._on_chunk_transcribed(2, "it is.", "en")
        s._on_chunk_transcribed(3, "", "en")
        self.assertEqual(len(raw.calls), 3)
        self.assertEqual(pieces.calls[-1], ("it is.",))
        self.assertEqual(lines.calls, [("Where are we on pricing?", "You"),
                                       ("Almost done. Friday", "Them"), ("it is.", "")])
        self.assertEqual(s.live_sentences(),
                         ["Where are we on pricing?", "Almost done.", "Friday it is."])
        rows = (s.meeting_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 4)
        # Microphone-only meetings get no You/Them guess.
        s2 = self.session(_App())
        s2.start("y")
        s2.app.recorder.sources = {0: (9.0, 0.0)}
        lines2 = _Recorder(s2.live_line)
        s2._on_chunk_transcribed(0, "Hi.", "en")
        self.assertEqual(lines2.calls, [("Hi.", "")])

    def test_recap_waits_for_new_text_and_the_interval(self):
        app = _App()
        s = self.session(app, inline=False)
        recaps = _Recorder(s.recap)
        with patch.object(msess.time, "time", return_value=1000.0):
            s.start("x")
        s._live_text = "word " * 50
        self.assertFalse(s._maybe_recap(now=1010.0))          # too soon
        self.assertTrue(s._maybe_recap(now=1021.0))
        self.assertFalse(s._maybe_recap(now=1050.0))          # still running / no new text
        target, args = s.spawned[0]
        with patch.object(msess.actions, "process", return_value="- So far") as proc:
            target(*args)
        proc.assert_called_once()
        self.assertEqual(recaps.calls, [("- So far",)])
        self.assertEqual(s._live_summary_text, "- So far")
        # Not during a Live Assistance session, nor while paused.
        s._live_text += "more " * 60
        app.recorder.fast_live_chunks = True
        self.assertFalse(s._maybe_recap(now=1100.0))
        app.recorder.fast_live_chunks = False
        s.pause()
        self.assertFalse(s._maybe_recap(now=1100.0))
        s.resume()
        self.assertTrue(s._maybe_recap(now=1100.0))

    def test_my_notes_are_saved_while_typing_and_at_stop(self):
        s = self.session()
        s.start("x")
        s.update_my_notes("- ask about budget")
        self.assertEqual(ms.load_my_notes(s.meeting_dir), "- ask about budget")
        s._process_meeting_notes = lambda: None
        s.stop("- ask about budget\n- Anna owns pricing")
        self.assertEqual(s._user_notes, "- ask about budget\n- Anna owns pricing")
        self.assertEqual(ms.load_my_notes(s.meeting_dir), "- ask about budget\n- Anna owns pricing")

    def test_stop_without_text_keeps_the_typed_notes(self):
        s = self.session()
        s.start("x")
        s.update_my_notes("typed on the page")
        s._process_meeting_notes = lambda: None
        s.stop()
        self.assertEqual(s._user_notes, "typed on the page")


# ── stop → outcome ───────────────────────────────────────────────────────────

class TestOutcome(_Base):
    def _record(self, app=None, notes="## Summary\n- Ship Friday", **start):
        app = app or _App()
        s = self.session(app)
        s._build_attributed_transcript = lambda: ""
        s.start(start.pop("title", "Q4 roadmap"), "Anna, David", **start)
        done, errors = _Recorder(s.finished), _Recorder(s.error)
        s._done, s._errors = done, errors
        with patch.object(msess.actions, "process", return_value=notes) as proc:
            s.stop("my bullet")
        s._proc = proc
        return s

    def test_notes_land_with_their_metadata(self):
        s = self._record()
        folder = s.meeting_dir
        self.assertEqual(s.phase, "done")
        self.assertEqual(s._done.calls, [(str(folder), "## Summary\n- Ship Friday", "")])
        self.assertEqual(s.app.recorder.stops, 1)
        self.assertEqual(s.app.released, 1)
        context = s._proc.call_args[0][0]
        self.assertIn("my bullet", context)                   # My notes reach the AI
        self.assertEqual((folder / "notes.md").read_text(encoding="utf-8"),
                         "## Summary\n- Ship Friday")
        meta = ms.load_meta(folder)
        self.assertEqual(meta["notes_engine"], "managed")
        self.assertEqual(meta["notes_engine_label"], "Gusan Cloud · Mistral Small")
        self.assertIs(meta["seen"], False)
        self.assertEqual(meta["language"], "en")
        self.assertEqual(meta["notices"], [])
        self.assertNotIn("notes_error", meta)
        self.assertEqual(ms.load_my_notes(folder), "my bullet")
        # The recorder is dictation's again.
        self.assertNotEqual(s.app.recorder.on_chunk_complete, s._on_chunk_transcribed)
        self.assertIsNone(s.app.recorder.on_source_levels)
        listed = ms.list_meetings()[0]
        self.assertEqual(ms.meeting_status(listed), "notes_ready")
        self.assertFalse(listed["seen"])

    def test_an_engine_that_is_not_set_up_degrades_with_a_notice_outside_the_notes(self):
        app = _App(pro=False, action_model="api_anthropic")       # cloud, no key
        s = self._record(app, notes="## Summary\n- formatter notes")
        self.assertEqual(s._proc.call_args[1]["model"], "rule_based")
        self.assertTrue(s._summary_downgraded)
        notes = (s.meeting_dir / "notes.md").read_text(encoding="utf-8")
        self.assertEqual(notes, "## Summary\n- formatter notes")    # exports stay clean
        self.assertIn("isn't set up", s.notices()[-1])
        meta = ms.load_meta(s.meeting_dir)
        self.assertEqual(meta["notes_engine"], "rule_based")
        self.assertEqual(meta["notices"], s.notices())
        self.assertEqual(meta["notes_notice"], s.notices()[-1])

    def test_rule_based_saved_engine_uses_the_default_engine(self):
        app = _App(action_model="rule_based")
        s = self._record(app)
        self.assertEqual(app.engine_calls[-1], "managed")      # Pro default, not the formatter

    def test_failed_notes_keep_the_transcript_and_can_be_retried(self):
        app = _App()
        s = self.session(app)
        s._build_attributed_transcript = lambda: ""
        s.start("Q4", "")
        done, errors = _Recorder(s.finished), _Recorder(s.error)
        with patch.object(msess.actions, "process", side_effect=RuntimeError("quota exceeded")):
            s.stop("")
        self.assertEqual((s.state, s.phase), ("done", "failed"))
        self.assertEqual(done.calls, [(str(s.meeting_dir), "", "quota exceeded")])
        self.assertEqual(errors.calls, [("notes", "quota exceeded")])
        self.assertEqual(ms.load_meta(s.meeting_dir)["notes_error"], "quota exceeded")
        self.assertEqual(ms.meeting_status(ms.list_meetings()[0]), "notes_failed")
        self.assertIn(("meeting_notes_failed", {"minutes": s._meeting_minutes,
                                                "transcript_kept": True}), app.tracked)
        # Retry with another engine, without touching the saved settings.
        with patch.object(msess.actions, "process", return_value="## Notes") as proc:
            self.assertTrue(s.retry_notes("qwen_3b"))
        self.assertEqual(app.engine_calls[-1], "qwen_3b")
        self.assertEqual(app.cfg["action_model"], "managed")
        self.assertEqual(s.phase, "done")
        self.assertEqual(done.calls[-1], (str(s.meeting_dir), "## Notes", ""))
        self.assertNotIn("notes_error", ms.load_meta(s.meeting_dir))
        proc.assert_called_once()

    def test_classic_retry_saves_the_engine(self):
        app = _App()
        s = self._record(app)
        with patch.object(msess.actions, "process", return_value="x"):
            s.retry_notes("qwen_3b", persist=True)
        self.assertEqual(app.cfg["action_model"], "qwen_3b")

    def test_nothing_captured(self):
        app = _App(_Rec(("", "en")))
        s = self.session(app)
        s.start("x")
        done, errors = _Recorder(s.finished), _Recorder(s.error)
        s.stop("")
        self.assertEqual((s.state, s.phase), ("idle", "failed"))
        self.assertEqual(done.calls[0][2], "No transcription recorded. The meeting is empty.")
        self.assertEqual(errors.calls[0][0], "empty")
        self.assertTrue(s.reset())
        self.assertEqual(s.phase, "idle")

    def test_rewrite_notes_of_a_saved_meeting(self):
        folder = self.root / "20260929_143000"
        folder.mkdir()
        (folder / "transcript.txt").write_text("Speaker 1: we ship Friday.", encoding="utf-8")
        (folder / "notes.md").write_text("old notes", encoding="utf-8")
        ms.save_my_notes(folder, "remember pricing")
        ms.update_meta(folder, title="Q4", duration_sec=2520, seen=True,
                       notices=["The saved recording ends at about 2 h.", "old engine notice"],
                       notes_notice="old engine notice")
        app = _App(save_history=True)
        s = self.session(app)
        done = _Recorder(s.finished)
        with patch.object(msess.actions, "process", return_value="new notes") as proc, \
                patch("history.save_entry", create=True) as hist:
            self.assertTrue(s.retry_notes("qwen_3b", folder=str(folder)))
        self.assertIn("remember pricing", proc.call_args[0][0])
        self.assertEqual((folder / "notes.md").read_text(encoding="utf-8"), "new notes")
        meta = ms.load_meta(folder)
        self.assertEqual(meta["duration_sec"], 2520)            # not reset by a rewrite
        self.assertIs(meta["seen"], True)                       # the user is looking at it
        self.assertEqual(meta["notices"], ["The saved recording ends at about 2 h."])
        self.assertEqual(meta["notes_engine"], "qwen_3b")
        self.assertEqual(done.calls, [(str(folder), "new notes", "")])
        hist.assert_not_called()                                # already in History

    def test_rewriting_another_meeting_uses_its_own_details(self):
        app = _App()
        s = self._record(app, notes_engine="qwen_7b")          # this meeting's own pick
        self.assertEqual(app.engine_calls[-1], "qwen_7b")
        folder = self.root / "20260101_090000"
        folder.mkdir()
        (folder / "transcript.txt").write_text("we ship friday", encoding="utf-8")
        ms.update_meta(folder, title="Old", source="calendar", calendar_event_id="evt9")
        with patch.object(msess.actions, "process", return_value="notes"):
            self.assertTrue(s.retry_notes(folder=str(folder)))
        self.assertIsNone(app.engine_calls[-1])                 # the saved engine, not qwen_7b
        snap = s.snapshot()
        self.assertEqual((snap["title"], snap["source"], snap["calendar_event_id"]),
                         ("Old", "calendar", "evt9"))

    def test_abort_of_a_continued_meeting_keeps_its_my_notes(self):
        folder = self.root / "20260101_090000"
        folder.mkdir()
        (folder / "transcript.txt").write_text("ORIGINAL", encoding="utf-8")
        ms.save_my_notes(folder, "earlier bullet")
        s = self.session()
        self.assertTrue(s.continue_meeting(str(folder))[0])
        s.update_my_notes("")                                   # cleared while typing (autosaved)
        self.assertEqual(ms.load_my_notes(folder), "")
        self.assertTrue(s.abort())
        self.assertEqual(ms.load_my_notes(folder), "earlier bullet")
        self.assertEqual(ms.load_transcript(folder), "ORIGINAL")
        self.assertFalse((folder / ms.DISCARDED_CHUNKS).exists())

    def test_no_rewrite_while_recording(self):
        s = self.session()
        s.start("x")
        self.assertFalse(s.retry_notes("qwen_3b", folder=str(self.root)))
        self.assertFalse(s.reset())
        self.assertEqual(s.continue_meeting(str(self.root))[:2], (False, "busy"))

    def test_continue_prefills_my_notes_and_keeps_the_start(self):
        folder = self.root / "20260101_090000"
        folder.mkdir()
        (folder / "transcript.txt").write_text("ORIGINAL", encoding="utf-8")
        ms.save_my_notes(folder, "earlier bullet")
        ms.update_meta(folder, title="Board", attendees="CEO", duration_sec=60)
        s = self.session()
        ok, _code, _msg = s.continue_meeting(str(folder))
        self.assertTrue(ok)
        self.assertEqual((s.meeting_dir, s.title, s.attendees, s.my_notes),
                         (folder, "Board", "CEO", "earlier bullet"))
        meta = ms.load_meta(folder)
        self.assertEqual(meta["started"], "2026-01-01 09:00:00")   # the folder's start
        self.assertEqual(meta["duration_sec"], 60)
        self.assertNotIn("source", meta)

    def test_abort_while_paused_discards_a_new_meeting(self):
        app = _App()
        app.live_assist = MagicMock()
        s = self.session(app)
        s.start("x")
        folder = s.meeting_dir
        s._on_chunk_transcribed(0, "never mind", "en")
        s.pause()
        app.recorder.request_abort = MagicMock()
        self.assertTrue(s.abort())
        self.assertEqual(s.phase, "idle")
        self.assertEqual(app.recorder.stops, 1)
        app.recorder.request_abort.assert_called_once()        # no post-roll for discarded audio
        app.live_assist.discard_session_audio.assert_called_once()
        self.assertTrue((folder / ms.DISCARDED_CHUNKS).is_file())
        self.assertEqual(ms.list_meetings(), [])

    def test_privacy_mode_never_downloads_the_speaker_models(self):
        app = _App(privacy_mode=True)
        app.recorder.get_full_audio = lambda: [0.0] * 32000
        s = self.session(app)
        with patch.object(msess.diarization, "sherpa_installed", return_value=True), \
                patch.object(msess.diarization, "models_downloaded", return_value=False), \
                patch.object(msess.diarization, "download_models") as dl:
            self.assertEqual(s._build_attributed_transcript(), "")
        dl.assert_not_called()


# ── Quit / log-off mid-meeting: saved, notes at the next launch ──────────────

class TestInviteReachesTheNotes(_Base):
    """The calendar invite's title, people and agenda in the notes - with the
    real recorder class (the engine MeetingSession is built from), not a stand-in."""
    AGENDA = "Budget review\nHiring plan"
    TRANSCRIPT = "Hello team. We ship Friday."

    def _run(self, title="Q4 roadmap", attendees="Anna, David", agenda="", stop_notes="",
             app=None, **start):
        s = self.session(app or _App())
        s._build_attributed_transcript = lambda: ""
        ok, code, msg = s.start(title, attendees, agenda=agenda, **start)
        self.assertTrue(ok, msg)
        with patch.object(msess.actions, "process", return_value="## Notes") as proc:
            s.stop(stop_notes)
        s._proc = proc
        return s

    def _input(self, s):
        return s._proc.call_args[0][0]

    def test_the_real_recorder_takes_an_agenda(self):
        import inspect
        for cls in (msess.MeetingEngine, msess.MeetingSession):
            self.assertIn("agenda", inspect.signature(cls.start).parameters, cls)
            self.assertTrue(callable(getattr(cls, "set_agenda", None)), cls)

    def test_the_notes_input_has_title_names_and_agenda_before_the_transcript(self):
        import invite_context
        s = self._run(agenda=self.AGENDA, source="calendar", calendar_event_id="evt1")
        text = self._input(s)
        header = invite_context.notes_header("Q4 roadmap", "Anna, David", self.AGENDA)
        self.assertTrue(text.startswith(header), text)
        self.assertIn("Meeting Title: Q4 roadmap", text)
        self.assertIn("Attendees: Anna, David", text)
        self.assertIn("- Budget review", text)
        self.assertIn("data, not instructions", text)               # framed as data
        self.assertTrue(text.endswith("Transcript:\n" + self.TRANSCRIPT), text)
        self.assertEqual(ms.load_meta(s.meeting_dir)["agenda"], self.AGENDA)

    def test_the_users_own_notes_still_come_after_it(self):
        s = self._run(agenda=self.AGENDA, stop_notes="my bullet")
        text = self._input(s)
        self.assertLess(text.index("Transcript:"), text.index("my bullet"))
        self.assertIn("Additional visual meeting notes provided by attendee:\nmy bullet", text)

    def test_a_real_title_and_people_alone_make_a_header_without_an_agenda(self):
        text = self._input(self._run())
        self.assertTrue(text.startswith("Meeting Title: Q4 roadmap\nAttendees: Anna, David\n\nTranscript:\n"))
        self.assertNotIn("Agenda", text)

    def test_the_notes_input_is_the_bare_transcript_without_invite_context(self):
        for title in ("", "Meeting · Tue 14:30", "Live session 14:30"):          # generic titles
            s = self._run(title=title, attendees="")
            self.assertEqual(self._input(s), self.TRANSCRIPT, title)
        # ... and my notes are all that is added.
        s = self._run(title="", attendees="", stop_notes="x")
        self.assertEqual(self._input(s), self.TRANSCRIPT + "\n\nAdditional visual meeting notes "
                                                             "provided by attendee:\nx")

    def test_a_generic_title_with_an_agenda_still_gets_the_agenda(self):
        text = self._input(self._run(title="", attendees="", agenda=self.AGENDA))
        self.assertNotIn("Meeting Title", text)
        self.assertIn("- Hiring plan", text)

    def test_the_built_in_formatter_reads_only_the_transcript(self):
        # It treats every line as speech: the header would end up in its notes.
        app = _App(action_model="rule_based")
        app._resolve_action_engine = lambda action_model=None: ("rule_based", dict(app.cfg))
        s = self._run(agenda=self.AGENDA, app=app)
        self.assertEqual(self._input(s), self.TRANSCRIPT)

    def test_a_new_meeting_never_inherits_the_last_ones_agenda(self):
        s = self._run(agenda=self.AGENDA)
        self.assertEqual(s._meeting_agenda, self.AGENDA)
        s.reset()
        with patch.object(msess.actions, "process", return_value="## Notes") as proc:
            ok, _code, _msg = s.start("Other call", "Anna")
            s.stop("")
        self.assertTrue(ok)
        self.assertEqual(s._meeting_agenda, "")
        self.assertNotIn("Budget review", proc.call_args[0][0])
        self.assertNotIn("agenda", ms.load_meta(s.meeting_dir))

    def test_the_agenda_is_clipped_like_the_start_card_does(self):
        s = self.session()
        s.start("Sync", "Anna", agenda="  one \n\n two\x00  " + "x" * 2000)
        self.assertTrue(s._meeting_agenda.startswith("one\ntwo"))
        self.assertLessEqual(len(s._meeting_agenda), 500)

    def test_set_agenda_persists_to_meta_and_an_empty_text_clears_it(self):
        s = self.session()
        s.start("Sync", "Anna", agenda=self.AGENDA)
        s.set_agenda("New agenda\nSecond row")
        self.assertEqual(s._meeting_agenda, "New agenda\nSecond row")
        self.assertEqual(ms.load_meta(s.meeting_dir)["agenda"], "New agenda\nSecond row")
        s.set_agenda("")
        self.assertEqual(s._meeting_agenda, "")
        self.assertFalse(ms.load_meta(s.meeting_dir).get("agenda"))
        s.set_agenda("again")
        self.assertEqual(ms.load_meta(s.meeting_dir)["agenda"], "again")

    def test_set_agenda_before_a_meeting_is_harmless(self):
        s = self.session()
        s.set_agenda("x")                                   # no folder: nothing is written, nothing raises
        self.assertEqual(s._meeting_agenda, "x")

    def test_what_the_card_edited_reaches_the_notes(self):
        s = self.session()
        s._build_attributed_transcript = lambda: ""
        s.start("Q4 roadmap", "Anna, David", agenda=self.AGENDA)
        s.set_agenda("Only this row")
        with patch.object(msess.actions, "process", return_value="## Notes") as proc:
            s.stop("")
        text = proc.call_args[0][0]
        self.assertIn("- Only this row", text)
        self.assertNotIn("Budget review", text)

    def test_rewrite_and_retry_rebuild_the_same_header_from_the_saved_meeting(self):
        s = self._run(agenda=self.AGENDA)
        first = self._input(s)
        folder = s.meeting_dir
        fresh = self.session()                              # another run of the app
        with patch.object(msess.actions, "process", return_value="new notes") as proc:
            self.assertTrue(fresh.retry_notes("qwen_3b", folder=str(folder)))
        self.assertEqual(fresh._meeting_agenda, self.AGENDA)
        again = proc.call_args[0][0]
        self.assertEqual(again.split("\n\nTranscript:\n")[0], first.split("\n\nTranscript:\n")[0])
        self.assertIn("- Budget review", again)
        # A meeting saved without an agenda stays without one (and without a stale one).
        other = self.root / "20260101_090000"
        other.mkdir()
        (other / "transcript.txt").write_text("we ship friday", encoding="utf-8")
        ms.update_meta(other, title="Old call", attendees="Mariam")
        with patch.object(msess.actions, "process", return_value="x") as proc:
            self.assertTrue(fresh.retry_notes(folder=str(other)))
        self.assertEqual(fresh._meeting_agenda, "")
        self.assertNotIn("Budget review", proc.call_args[0][0])
        self.assertTrue(proc.call_args[0][0].startswith("Meeting Title: Old call\nAttendees: Mariam"))

    def test_a_continued_meeting_keeps_its_own_agenda(self):
        s = self._run(agenda=self.AGENDA)
        folder = s.meeting_dir
        ok, _code, _msg = s.continue_meeting(str(folder))
        self.assertTrue(ok)
        self.assertEqual(s._meeting_agenda, self.AGENDA)

    def test_live_assistant_hears_the_agenda_only_when_there_is_one(self):
        app = _App()
        app.live_assist = MagicMock()
        s = self.session(app)
        s.start("Sync", "Anna", agenda=self.AGENDA)
        app.live_assist.set_meeting_active.assert_called_once_with(
            True, "Sync", "Anna", agenda=self.AGENDA)
        app2 = _App()
        app2.live_assist = MagicMock()
        self.session(app2).start("Sync", "Anna")
        app2.live_assist.set_meeting_active.assert_called_once_with(True, "Sync", "Anna")

    def test_the_session_context_helper_hands_the_agenda_to_the_real_start(self):
        import live_context
        s = self.session()
        ok, _code, _msg = live_context.start_session(s, agenda=self.AGENDA, title="Sync",
                                                     attendees="Anna", source="calendar",
                                                     calendar_event_id="evt1")
        self.assertTrue(ok)
        self.assertEqual(s._meeting_agenda, self.AGENDA)
        self.assertEqual(ms.load_meta(s.meeting_dir)["agenda"], self.AGENDA)


class TestQuitKeepsTheMeeting(_Base):
    def _recording(self, app=None):
        app = app or _App()
        app.live_assist = MagicMock()
        app.recorder.request_abort = MagicMock()
        app.recorder.get_full_audio = lambda: [0.0] * 3200      # 0.2 s retained
        s = self.session(app)
        s.start("Late call", "Aram, Anna", language="en")
        s._on_chunk_transcribed(1, "Pricing next week.", "en")
        s._on_chunk_transcribed(0, "Thanks for staying late.", "en")
        s.update_my_notes("Follow up on pricing")
        return s

    def test_quit_while_recording_saves_the_recording_and_transcript(self):
        s = self._recording()
        folder = s.meeting_dir
        phases = _Recorder(s.state_changed)
        with patch("audio_export.write_wav") as wav, \
                patch.object(msess.actions, "process") as proc:
            self.assertEqual(s.save_for_quit(), str(folder))
        proc.assert_not_called()                                 # no notes now: next launch
        s.app.recorder.request_abort.assert_called_once()        # no post-roll at Quit
        self.assertEqual(s.app.recorder.stops, 1)                # its own thread closes the streams
        wav.assert_called_once()
        self.assertEqual(Path(wav.call_args[0][0]).name, "audio_part1.wav")
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"),
                         "Thanks for staying late. Pricing next week.")       # spoken order
        self.assertEqual(ms.load_my_notes(folder), "Follow up on pricing")
        meta = ms.load_meta(folder)
        self.assertIs(meta[msess.META_NOTES_PENDING], True)
        self.assertIs(meta["seen"], False)
        self.assertEqual((meta["title"], meta["language"]), ("Late call", "en"))
        self.assertIn("duration_sec", meta)
        self.assertIn(msess.QUIT_NOTICE, meta["notices"])
        self.assertNotIn("notes_error", meta)
        # The session is free and the recorder is dictation's again.
        self.assertEqual((s.state, s.is_busy, s.holds_recorder), ("idle", False, False))
        self.assertEqual(phases.calls, [("idle",)])
        self.assertNotEqual(s.app.recorder.on_chunk_complete, s._on_chunk_transcribed)
        s.app.live_assist.set_meeting_active.assert_called_with(False)
        listed = ms.list_meetings()[0]
        self.assertEqual((listed["dir"], ms.meeting_status(listed)), (str(folder), "notes_failed"))
        self.assertEqual(msess.pending_notes_folders(), [str(folder)])
        self.assertEqual(s.save_for_quit(), "")                  # a second call: nothing left

    def test_the_next_launch_writes_them_as_the_meetings_first_notes(self):
        s = self._recording()
        with patch("audio_export.write_wav"):
            folder = s.save_for_quit()
        app = _App(save_history=True)
        s2 = self.session(app, inline=False)                      # a fresh run
        done = _Recorder(s2.finished)
        self.assertTrue(s2.write_pending_notes(folder))
        self.assertEqual(s2.state, "processing")
        self.assertTrue(s2.is_busy)
        self.assertFalse(s2.holds_recorder)                      # dictation may run meanwhile
        target, args = s2.spawned[0]
        with patch.object(msess.actions, "process", return_value="## Notes\n- pricing") as proc, \
                patch("history.save_entry", create=True) as hist:
            target(*args)
        self.assertIn("Follow up on pricing", proc.call_args[0][0])      # My notes reach the AI
        self.assertIn("Thanks for staying late.", proc.call_args[0][0])
        self.assertEqual(done.calls, [(folder, "## Notes\n- pricing", "")])
        hist.assert_called_once()                                # first notes: in History
        meta = ms.load_meta(folder)
        self.assertNotIn(msess.META_NOTES_PENDING, meta)
        self.assertIs(meta["seen"], False)
        self.assertEqual(s2.title, "Late call")
        self.assertEqual(msess.pending_notes_folders(), [])

    def test_a_failed_attempt_drops_the_mark_and_keeps_the_transcript(self):
        s = self._recording()
        with patch("audio_export.write_wav"):
            folder = s.save_for_quit()
        s2 = self.session(_App())
        errors = _Recorder(s2.error)
        with patch.object(msess.actions, "process", side_effect=RuntimeError("offline")):
            self.assertTrue(s2.write_pending_notes(folder))
        self.assertEqual(errors.calls, [("notes", "offline")])
        meta = ms.load_meta(Path(folder))
        self.assertNotIn(msess.META_NOTES_PENDING, meta)
        self.assertEqual(meta["notes_error"], "offline")
        self.assertIn("Thanks for staying late.", ms.load_transcript(folder))

    def test_nothing_to_write_from_drops_the_mark(self):
        folder = self.root / "20261008_090000"
        folder.mkdir()
        ms.update_meta(folder, title="Empty", **{msess.META_NOTES_PENDING: True})
        s = self.session()
        self.assertFalse(s.write_pending_notes(str(folder)))
        self.assertNotIn(msess.META_NOTES_PENDING, ms.load_meta(folder))
        self.assertEqual(s.state, "idle")

    def test_quit_while_the_notes_are_written(self):
        s = self._recording()
        s._spawn = lambda target, *args: None                    # the worker hasn't got far
        s.stop()
        self.assertEqual(s.state, "processing")
        self.assertTrue(s.holds_recorder)                        # it transcribes the recording next
        folder = s.meeting_dir
        self.assertEqual(s.save_for_quit(wait=0), str(folder))
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"),
                         "Thanks for staying late. Pricing next week.")
        self.assertIs(ms.load_meta(folder)[msess.META_NOTES_PENDING], True)
        # Notes that made it before Quit are left alone.
        ms.update_meta(folder, **{msess.META_NOTES_PENDING: None})
        s._notes_saved.set()
        s.save_for_quit(wait=0)
        self.assertNotIn(msess.META_NOTES_PENDING, ms.load_meta(folder))

    def test_a_continued_meeting_quit_while_processing_keeps_its_new_part(self):
        folder = self.root / "20260929_143000"
        folder.mkdir()
        (folder / "transcript.txt").write_text("We met before.", encoding="utf-8")
        (folder / "notes.md").write_text("old notes", encoding="utf-8")
        ms.update_meta(folder, title="Weekly", duration_sec=60)
        app = _App()
        app.live_assist = MagicMock()
        app.recorder.request_abort = MagicMock()
        s = self.session(app)
        ok, _code, _msg = s.continue_meeting(str(folder))
        self.assertTrue(ok)
        s._on_chunk_transcribed(0, "And again today.", "en")
        s._spawn = lambda target, *args: None                    # the worker hasn't got far
        s.stop()
        self.assertEqual(s.save_for_quit(wait=0), str(folder))
        text = (folder / "transcript.txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("We met before."))       # the earlier part...
        self.assertIn("And again today.", text)                  # ...and what this part said
        self.assertIs(ms.load_meta(folder)[msess.META_NOTES_PENDING], True)
        # Once the worker saved this run's transcript, Quit leaves it alone.
        (folder / "transcript.txt").write_text("the worker's full transcript", encoding="utf-8")
        s._transcript_saved.set()
        s.save_for_quit(wait=0)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"),
                         "the worker's full transcript")

    def test_a_rewrite_of_notes_it_has_is_not_marked(self):
        folder = self.root / "20260929_143000"
        folder.mkdir()
        (folder / "transcript.txt").write_text("we ship friday", encoding="utf-8")
        (folder / "notes.md").write_text("old notes", encoding="utf-8")
        s = self.session(inline=False)
        self.assertTrue(s.retry_notes(folder=str(folder)))
        self.assertFalse(s.holds_recorder)
        s.save_for_quit(wait=0)
        self.assertNotIn(msess.META_NOTES_PENDING, ms.load_meta(folder))
        self.assertEqual((folder / "notes.md").read_text(encoding="utf-8"), "old notes")

    def test_idle_has_nothing_to_save(self):
        s = self.session()
        self.assertEqual(s.save_for_quit(), "")
        self.assertFalse(s.holds_recorder)


# ── meeting_store additions ──────────────────────────────────────────────────

class TestStoreAdditions(_Base):
    def _folder(self, name="20260929_143000", **files):
        d = self.root / name
        d.mkdir()
        for fname, text in files.items():
            (d / fname.replace("_dot_", ".")).write_text(text, encoding="utf-8")
        return d

    def test_update_meta_merges_and_removes(self):
        d = self._folder()
        (d / "meta.json").write_text(json.dumps({"title": "Old", "future_key": 1}), encoding="utf-8")
        merged = ms.update_meta(d, title="New", seen=False)
        self.assertEqual(merged, {"title": "New", "future_key": 1, "seen": False})
        self.assertEqual(ms.load_meta(d), merged)
        ms.update_meta(d, seen=None)
        self.assertNotIn("seen", ms.load_meta(d))
        self.assertEqual([p.name for p in d.iterdir()], ["meta.json"])   # no temp files left
        self.assertIsNone(ms.update_meta(self.root / "missing", title="x"))

    def test_update_meta_from_many_threads_keeps_every_key(self):
        d = self._folder()
        threads = [threading.Thread(target=ms.update_meta, args=(d,), kwargs={f"k{i}": i})
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(ms.load_meta(d), {f"k{i}": i for i in range(20)})

    def test_my_notes_round_trip(self):
        d = self._folder()
        self.assertEqual(ms.load_my_notes(d), "")
        self.assertTrue(ms.save_my_notes(d, "- one"))
        self.assertEqual(ms.load_my_notes(d), "- one")
        self.assertTrue(ms.save_my_notes(d, "  "))
        self.assertFalse((d / ms.MY_NOTES_FILE).exists())
        self.assertFalse(ms.save_my_notes(self.root / "missing", "x"))

    def test_list_fields_status_search_title_seen(self):
        a = self._folder("20260929_143000", transcript_dot_txt="we ship friday",
                         notes_dot_md="## Summary\n- Pricing page is late")
        ms.update_meta(a, title="Q4 roadmap", attendees="Anna, David", language="en",
                       notes_engine_label="Gusan Cloud · Mistral Small", seen=False,
                       source="page")
        b = self._folder("20260928_100000", transcript_dot_txt="budget talk")
        ms.update_meta(b, title="Weekly sync")                       # notes failed
        c = self._folder("20260927_100000")
        (c / "audio_part1.wav").write_bytes(b"RIFF")
        old = self._folder("20260101_090000", notes_dot_md="x")
        ms.update_meta(old, title="Live session 10:15")              # a 1.9 Live session
        items = ms.list_meetings()
        by = {m["id"]: m for m in items}
        qa = by["20260929_143000"]
        self.assertEqual(qa["attendees_list"], ["Anna", "David"])
        self.assertEqual(qa["started"], "2026-09-29 14:30:00")
        self.assertFalse(qa["seen"])
        self.assertTrue(by["20260101_090000"]["seen"])               # pre-2.0: seen
        self.assertEqual(qa["notes_engine_label"], "Gusan Cloud · Mistral Small")
        self.assertEqual([ms.meeting_status(by[k]) for k in
                          ("20260929_143000", "20260928_100000", "20260927_100000")],
                         ["notes_ready", "notes_failed", "audio_only"])
        self.assertEqual(ms.meeting_status(by["20260927_100000"], active_dir=c,
                                           active_state="paused"), "recording")
        self.assertEqual(ms.meeting_status(qa, active_dir=c, active_state="processing"),
                         "notes_ready")
        self.assertEqual([m["id"] for m in ms.search("anna q4", items)], ["20260929_143000"])
        self.assertEqual([m["id"] for m in ms.search("pricing", items)], ["20260929_143000"])
        self.assertEqual(ms.search("budget", items), [])
        self.assertEqual([m["id"] for m in ms.search("budget", items, transcripts=True)],
                         ["20260928_100000"])
        self.assertEqual(len(ms.search("  ", items)), 4)
        self.assertTrue(ms.set_title(b, "  Budget   review "))
        self.assertEqual(ms.load_meta(b)["title"], "Budget review")
        self.assertTrue(ms.set_title(b, ""))
        self.assertEqual(ms.load_meta(b)["title"], "Untitled Meeting")
        self.assertTrue(ms.mark_seen(a))
        self.assertIs(ms.load_meta(a)["seen"], True)
        self.assertFalse(ms.mark_seen(self.root / "missing"))
        self.assertEqual([m["id"] for m in ms.list_live_sessions(items)], ["20260101_090000"])
        ms.update_meta(a, source="live_assist")
        self.assertEqual(len(ms.list_live_sessions()), 2)
        self.assertEqual(ms.folder_timestamp("20260929_143000_2"), "2026-09-29 14:30:00")
        self.assertEqual(ms.folder_timestamp("odd"), "odd")


# ── the recorder: pause and per-source levels on scripted audio ──────────────

def _real_main_numpy(case):
    import main
    if type(main.np) is not types.ModuleType:        # the stub answers any attribute
        case.skipTest("numpy stubbed in main by another test module")
    return main


FRAME = 1024                                         # cfg chunk_size at 16 kHz: 64 ms


class TestRecorderPause(unittest.TestCase):
    def setUp(self):
        self.main = _real_main_numpy(self)
        import numpy as np
        self.np = np

    def _frame(self, level):
        np = self.np
        t = np.arange(FRAME, dtype=np.float32) / 16000.0
        return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    def _rec(self, script, capture_mode="default_mic", **extra):
        """Run the real _record_loop. ``script(n, rec)`` returns the frame level
        of read n and may flip rec.paused / rec.stop_requested."""
        main = self.main
        reads, chunks, levels = [], [], []

        def read(stream, sr):
            n = len(reads)
            reads.append(n)
            return self._frame(script(n, rec)).tobytes()

        rec = SimpleNamespace(
            recording=True, stop_requested=False, _abort=False, _capture_mode=capture_mode,
            fast_live_chunks=False, on_levels=None, _chunk_lock=threading.Lock(),
            _chunk_frames=[], _full_audio_truncated=False, _full_audio_frames=[],
            _full_audio_len=0, _full_audio_max=10 ** 9, _chunk_idx=0,
            _chunk_silence_before={}, _chunk_threads=[], _level_peak=0.05,
            _record_error="", _read_input_draining=read, paused=False,
            _chunk_sources={}, on_source_levels=lambda m, c: levels.append((m, c)),
            _transcribe_chunk=lambda audio, idx: chunks.append((idx, len(audio))), **extra)
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": FRAME}):
            main.AudioRecorder._record_loop(rec, extra.pop("_is_loopback", False),
                                            object(), extra.pop("_mic", None))
        return rec, reads, chunks, levels

    def test_pause_cuts_the_pending_speech_and_keeps_nothing_until_resume(self):
        def script(n, rec):
            if n == 20:
                rec.paused = True
            if n == 40:
                rec.paused = False
            if n == 50:
                rec.stop_requested = True
            return 0.3 if 5 <= n < 30 else 0.0005

        rec, reads, chunks, levels = self._rec(script)
        # Speech before Pause became chunk 0 on the first paused read.
        self.assertEqual(chunks[0][0], 0)
        self.assertEqual(chunks[0][1], 20 * FRAME)
        # 20 paused reads (20..39) were read but not kept.
        self.assertEqual(rec._full_audio_len, (len(reads) - 20) * FRAME)
        self.assertEqual(len(levels), len(reads) - 20)
        self.assertGreater(levels[10][0], 0.1)                 # the mic meter moved
        self.assertEqual(levels[10][1], 0.0)                   # no call in mic-only mode

    def test_stop_while_paused_has_no_post_roll(self):
        def script(n, rec):
            if n == 10:
                rec.paused = True
            if n == 14:
                rec.stop_requested = True
            return 0.3
        rec, reads, chunks, _levels = self._rec(script)
        # Stopped at once: read 14 (the one that asked to stop) is the last -
        # no post-roll reads of after-Pause audio.
        self.assertEqual(len(reads), 15)
        self.assertEqual(rec._full_audio_len, 10 * FRAME)

    def test_dictation_reports_no_source_levels(self):
        def script(n, rec):
            if n == 5:
                rec.stop_requested = True
            return 0.0005
        _rec, _reads, _chunks, levels = self._rec(script, capture_mode=None)
        self.assertEqual(levels, [])

    def test_mic_and_call_are_measured_before_the_mix(self):
        np = self.np
        main = self.main
        reads, chunks, levels = [], [], []
        # A meeting cuts a chunk every 10 s of audio (157 reads of 64 ms):
        # chunk 0 is you talking, chunk 1 the call.
        per_chunk = 157

        def read_mic(stream, sr):
            n = len(reads)
            reads.append(n)
            if n == 2 * per_chunk + 5:
                rec.stop_requested = True
            return self._frame(0.3 if n < per_chunk else 0.0005).tobytes()

        def read_loop(stream):
            n = len(reads) - 1                   # the mic read of this same pass
            if n == 0:
                return np.zeros(0, np.float32)   # the call delivers nothing yet
            return self._frame(0.2 if per_chunk <= n < 2 * per_chunk else 0.0)

        rec = SimpleNamespace(
            recording=True, stop_requested=False, _abort=False, _capture_mode="smart_meeting",
            fast_live_chunks=False, on_levels=None, _chunk_lock=threading.Lock(),
            _chunk_frames=[], _full_audio_truncated=False, _full_audio_frames=[],
            _full_audio_len=0, _full_audio_max=10 ** 9, _chunk_idx=0,
            _chunk_silence_before={}, _chunk_threads=[], _level_peak=0.05,
            _record_error="", _read_input_draining=read_mic, _read_available_loopback=read_loop,
            paused=False, _chunk_sources={},
            on_source_levels=lambda m, c: levels.append((m, c)),
            _transcribe_chunk=lambda audio, idx: chunks.append(idx))
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": FRAME}):
            main.AudioRecorder._record_loop(rec, True, object(), object())
        self.assertGreater(levels[5][0], 0.1)                  # you talking
        self.assertEqual(levels[5][1], 0.0)
        self.assertLess(levels[200][0], 0.01)                  # them talking
        self.assertGreater(levels[200][1], 0.1)
        # Each chunk remembers whose energy it carried.
        self.assertEqual(sorted(rec._chunk_sources), [0, 1])
        self.assertEqual(msess.speaker_hint(rec._chunk_sources[0]), "You")
        self.assertEqual(msess.speaker_hint(rec._chunk_sources[1]), "Them")
        for t in rec._chunk_threads:
            t.join(5)
        self.assertEqual(sorted(chunks), [0, 1])

    def test_system_sound_only_meters_fall_to_zero_when_the_call_goes_quiet(self):
        np = self.np
        main = self.main
        reads, levels = [], []

        def read_loop(stream):
            # WASAPI loopback: frames while something plays, nothing at all
            # while the computer is silent.
            n = len(reads)
            reads.append(n)
            if n == 12:
                rec.stop_requested = True
            return self._frame(0.3) if n < 3 else np.zeros(0, np.float32)

        rec = SimpleNamespace(
            recording=True, stop_requested=False, _abort=False, _capture_mode="system_only",
            fast_live_chunks=False, on_levels=None, _chunk_lock=threading.Lock(),
            _chunk_frames=[], _full_audio_truncated=False, _full_audio_frames=[],
            _full_audio_len=0, _full_audio_max=10 ** 9, _chunk_idx=0,
            _chunk_silence_before={}, _chunk_threads=[], _level_peak=0.05,
            _record_error="", _read_available_loopback=read_loop,
            paused=False, _chunk_sources={},
            on_source_levels=lambda m, c: levels.append((m, c)),
            _transcribe_chunk=lambda audio, idx: None)
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": FRAME,
                                        "min_speech_sec": 5.0}), \
                mock.patch.object(main.time, "sleep"):
            main.AudioRecorder._record_loop(rec, True, object(), None)
        self.assertGreater(len(reads), 12)
        self.assertEqual(len(levels), 4)
        self.assertTrue(all(m == 0.0 and c > 0.1 for m, c in levels[:3]))
        self.assertEqual(levels[3], (0.0, 0.0))            # once, not on every idle read

    def test_set_paused_and_chunk_source_energy_on_the_real_recorder(self):
        main = self.main
        rec = main.AudioRecorder.__new__(main.AudioRecorder)
        rec._chunk_lock = threading.Lock()
        rec._chunk_sources = {3: (1.0, 2.0)}
        main.AudioRecorder.set_paused(rec, 1)
        self.assertIs(rec.paused, True)
        self.assertEqual(main.AudioRecorder.chunk_source_energy(rec, 3), (1.0, 2.0))
        self.assertIsNone(main.AudioRecorder.chunk_source_energy(rec, 4))
        self.assertEqual(main.AudioRecorder._samples_rms(None), 0.0)


class TestPieceSpans(unittest.TestCase):
    """Gusan 2.0.1: every live piece knows where it sits in the retained recording, so a cloud
    session's pieces can stand in for the re-read (AudioRecorder.piece_segments)."""

    def setUp(self):
        self.main = _real_main_numpy(self)
        import numpy as np
        self.np = np

    def _loop(self, script, truncated=False):
        main, np = self.main, self.np
        chunks, reads = [], []

        def read(stream, sr):
            n = len(reads)
            reads.append(n)
            t = np.arange(FRAME, dtype=np.float32) / 16000.0
            return (script(n, rec) * np.sin(2 * np.pi * 220 * t)).astype(np.float32).tobytes()

        rec = SimpleNamespace(
            recording=True, stop_requested=False, _abort=False, _capture_mode="smart_meeting",
            fast_live_chunks=True, on_levels=None, _chunk_lock=threading.Lock(),
            _chunk_frames=[], _full_audio_truncated=truncated, _full_audio_frames=[],
            _full_audio_len=0, _full_audio_max=10 ** 9, _chunk_idx=0,
            _chunk_silence_before={}, _chunk_threads=[], _level_peak=0.05,
            _record_error="", _read_input_draining=read, paused=False,
            _chunk_sources={}, _chunk_spans={}, on_source_levels=lambda m, c: None,
            _transcribe_chunk=lambda audio, idx: chunks.append((idx, len(audio))))
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": FRAME}):
            main.AudioRecorder._record_loop(rec, False, object(), None)
        for t in rec._chunk_threads:
            t.join(5)
        rec.reads = reads
        return rec, sorted(chunks)

    def test_the_spans_tile_the_recording_across_a_pause(self):
        def script(n, rec):                       # (a live piece is cut every 5 s = 78 reads)
            if n == 100:
                rec.paused = True
            if n == 120:
                rec.paused = False
            if n == 330:
                rec.stop_requested = True
            return 0.3 if 5 <= n < 320 else 0.0005
        rec, chunks = self._loop(script)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertEqual(sorted(rec._chunk_spans), [i for i, _n in chunks])
        edge = 0
        for idx, length in chunks:
            start, end = rec._chunk_spans[idx]
            self.assertEqual((start, end - start), (edge, length), idx)    # right after the last one, as long as it is
            edge = end
        self.assertLessEqual(edge, rec._full_audio_len)
        # The 20 paused reads kept nothing: the timeline has no gap where they were.
        self.assertEqual(rec._full_audio_len, (len(rec.reads) - 20) * FRAME)

    def test_a_recording_past_its_memory_cap_has_no_places(self):
        def script(n, rec):
            if n == 60:
                rec.stop_requested = True
            return 0.3 if 5 <= n < 50 else 0.0005
        rec, chunks = self._loop(script, truncated=True)
        self.assertTrue(chunks)
        self.assertEqual(rec._chunk_spans, {})
        me = SimpleNamespace(_chunk_lock=threading.Lock(), _chunk_results={0: "words"},
                             _chunk_spans=rec._chunk_spans, _full_audio_len=0)
        with mock.patch.dict(self.main.cfg, {"sample_rate": 16000}):
            self.assertIsNone(self.main.AudioRecorder.piece_segments(me))      # -> the plain transcript stays

    def test_the_tail_after_stop_gets_its_place_too(self):
        main, np = self.main, self.np
        tail = np.full(8000, 0.2, dtype=np.float32)
        me = SimpleNamespace(
            _chunk_pipeline=None, _chunk_threads=[], _building_model=False, _abort=False, _record_error="",
            partial_text="", _chunk_errors=[], _chunk_frames=[tail.tobytes()], _chunk_idx=2,
            _chunk_results={0: "first", 1: "second"}, _chunk_lock=threading.Lock(), _capture_mode="smart_meeting",
            _tail_speech_sec=1.0, on_lang_detected=None, on_finalising=None, backend_override="managed",
            _run_managed=lambda audio: ("tail words", "hy"), _full_audio_len=56000,
            _full_audio_truncated=False, _chunk_spans={0: (0, 24000), 1: (24000, 48000)})
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "backend": "local"}):
            text, lang = main.AudioRecorder.transcribe(me)
            segs = main.AudioRecorder.piece_segments(me)
        self.assertEqual((text, lang), ("first second tail words", "hy"))
        self.assertEqual(me._chunk_spans[2], (48000, 56000))                  # the last 0.5 s
        self.assertEqual(segs, [{"start": 0.0, "end": 1.5, "text": "first"},
                                {"start": 1.5, "end": 3.0, "text": "second"},
                                {"start": 3.0, "end": 3.5, "text": "tail words"}])

    def test_a_tail_that_was_too_short_to_send_is_not_a_piece(self):
        main, np = self.main, self.np
        me = SimpleNamespace(
            _chunk_pipeline=None, _chunk_threads=[], _building_model=False, _abort=False, _record_error="",
            partial_text="", _chunk_errors=[], _chunk_frames=[np.zeros(100, np.float32).tobytes()],
            _chunk_idx=1, _chunk_results={0: "only"}, _chunk_lock=threading.Lock(), _capture_mode="smart_meeting",
            _tail_speech_sec=0.0, on_lang_detected=None, on_finalising=None, backend_override="managed",
            _run_managed=lambda audio: ("", "hy"), _full_audio_len=24000,
            _full_audio_truncated=False, _chunk_spans={0: (0, 24000)})
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "backend": "local"}):
            text, _lang = main.AudioRecorder.transcribe(me)
            segs = main.AudioRecorder.piece_segments(me)
        self.assertEqual(text, "only")
        self.assertEqual(segs, [{"start": 0.0, "end": 1.5, "text": "only"}])


# ── Gusan 2.0.1: the two tracks, speaker labels, interrupted meetings, Identify speakers ──

def _real_numpy():
    try:
        import numpy as np
        return np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,)
    except Exception:
        return False


class _SpeakerRec(_Rec):
    """A recorder that retained a mic + system sound meeting."""

    def __init__(self, seconds=3, **kw):
        super().__init__(**kw)
        import numpy as np
        n = 16000 * seconds
        self.mic = (np.sin(np.arange(n) / 20.0) * 8000).astype(np.int16)
        self.system = (np.sin(np.arange(n) / 7.0) * 4000).astype(np.int16)
        self.with_tracks = True
        self.stretches = []
        self.health = {}
        self.quiet = 0.0

    def get_full_audio(self):
        import numpy as np
        return ((self.mic.astype(np.float32) + self.system.astype(np.float32)) / 32767.0 * 0.75)

    def get_full_audio_tracks(self):
        return (self.mic, self.system) if self.with_tracks else None

    def quiet_stretches(self, min_sec=None):
        return list(self.stretches)

    def chunk_health(self):
        return dict(self.health)

    def quiet_seconds(self):
        return self.quiet


def _outcome(texts=("You: hi\nSpeaker 1: hello",), reason="", notice="", cancelled=False):
    out = msess.meeting_speakers.Outcome()
    out.texts = list(texts)
    out.reason = reason or ("cancelled" if cancelled else "")
    out.notice = notice
    out.mode = "sources"
    out.speakers = 1
    return out


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestTracksOnDisk(_Base):
    def _meeting(self, rec=None, mode="smart_meeting"):
        rec = rec or _SpeakerRec()
        app = _App(rec, meeting_audio_mode=mode)
        s = self.session(app)
        s._build_attributed_transcript = lambda: ""
        s.start("Call", "", audio_mode=mode)
        return s, rec

    def test_stop_saves_the_recording_then_the_two_sources_next_to_it(self):
        import audio_export
        s, rec = self._meeting()
        order = []
        real = audio_export.write_wav_int16

        def spy(path, samples, rate=16000):
            order.append((Path(path).name, s._segment_saved.is_set()))
            return real(path, samples, rate)
        with patch("audio_export.write_wav_int16", side_effect=spy), \
                patch.object(msess.actions, "process", return_value="## N"):
            s.stop("")
        folder = s.meeting_dir
        self.assertEqual(sorted(p.name for p in folder.glob("*.wav")),
                         ["audio_part1.wav", "audio_part1_mic.wav", "audio_part1_system.wav"])
        # After the recording itself was safe (a Quit from there on keeps it).
        self.assertEqual(order, [("audio_part1_mic.wav", True), ("audio_part1_system.wav", True)])
        mic, _ = audio_export.read_wav_int16(folder / "audio_part1_mic.wav")
        system, _ = audio_export.read_wav_int16(folder / "audio_part1_system.wav")
        import numpy as np
        self.assertTrue(np.array_equal(mic, rec.mic))
        self.assertTrue(np.array_equal(system, rec.system))
        # Only the mix is a recording part - lists, the player and export never see the tracks.
        self.assertEqual([p.name for p in ms.audio_parts(folder)], ["audio_part1.wav"])
        self.assertEqual(len(ms.list_meetings()[0]["audio_parts"]), 1)
        self.assertEqual([(p.name, m.name, y.name) for p, m, y in ms.audio_tracks(folder)],
                         [("audio_part1.wav", "audio_part1_mic.wav", "audio_part1_system.wav")])

    def test_other_modes_and_old_recorders_keep_only_the_mix(self):
        rec = _SpeakerRec()
        rec.with_tracks = False
        s, _ = self._meeting(rec, mode="default_mic")
        with patch.object(msess.actions, "process", return_value="## N"):
            s.stop("")
        self.assertEqual(sorted(p.name for p in s.meeting_dir.glob("*.wav")), ["audio_part1.wav"])
        self.assertEqual([(m, y) for _p, m, y in ms.audio_tracks(s.meeting_dir)], [(None, None)])

    def test_a_failed_second_track_leaves_no_lone_file(self):
        import audio_export
        s, rec = self._meeting()
        real = audio_export.write_wav_int16
        calls = []

        def flaky(path, samples, rate=16000):
            calls.append(Path(path).name)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(path, samples, rate)
        with patch("audio_export.write_wav_int16", side_effect=flaky), \
                patch.object(msess.actions, "process", return_value="## N"):
            s.stop("")
        self.assertEqual(sorted(p.name for p in s.meeting_dir.glob("*.wav")), ["audio_part1.wav"])
        self.assertEqual(s.phase, "done")                       # the meeting itself is fine
        self.assertEqual([(m, y) for _p, m, y in ms.audio_tracks(s.meeting_dir)], [(None, None)])

    def test_a_temp_file_never_stays_behind_and_a_tracks_file_is_all_or_nothing(self):
        import audio_export
        import numpy as np
        target = Path(self._tmp.name) / "t.wav"
        with patch("os.replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                audio_export.write_wav_int16(target, np.zeros(100, np.int16))
        self.assertFalse(target.exists())
        self.assertEqual(list(Path(self._tmp.name).glob("t.wav*")), [])

    def test_continuing_a_meeting_adds_the_next_parts_tracks(self):
        s, rec = self._meeting()
        with patch.object(msess.actions, "process", return_value="## N"):
            s.stop("")
        folder = s.meeting_dir
        s2 = self.session(_App(_SpeakerRec(seconds=2), meeting_audio_mode="smart_meeting"))
        s2._build_attributed_transcript = lambda: ""
        ok, _c, _m = s2.continue_meeting(str(folder), audio_mode="smart_meeting")
        self.assertTrue(ok)
        with patch.object(msess.actions, "process", return_value="## N2"):
            s2.stop("")
        self.assertEqual(sorted(p.name for p in folder.glob("*.wav")),
                         ["audio_part1.wav", "audio_part1_mic.wav", "audio_part1_system.wav",
                          "audio_part2.wav", "audio_part2_mic.wav", "audio_part2_system.wav"])
        self.assertEqual([p.name for p in ms.audio_parts(folder)], ["audio_part1.wav", "audio_part2.wav"])
        self.assertEqual(len(ms.audio_tracks(folder)), 2)

    def test_deleting_the_meeting_removes_its_tracks_too(self):
        s, rec = self._meeting()
        with patch.object(msess.actions, "process", return_value="## N"):
            s.stop("")
        folder = s.meeting_dir
        self.assertTrue((folder / "audio_part1_mic.wav").is_file())
        self.assertTrue(ms.delete_meeting(folder))
        self.assertFalse(folder.exists())

    def test_an_old_meeting_with_only_the_mix_is_untouched(self):
        d = self.root / "20260101_090000"
        d.mkdir()
        (d / "audio_part1.wav").write_bytes(b"RIFF" + b"\0" * 100)
        (d / "transcript.txt").write_text("hello", encoding="utf-8")
        self.assertEqual([(m, y) for _p, m, y in ms.audio_tracks(d)], [(None, None)])
        self.assertEqual(len(ms.list_meetings()[0]["audio_parts"]), 1)
        # an empty / header-only track file is not a track
        (d / "audio_part1_mic.wav").write_bytes(b"RIFF")
        (d / "audio_part1_system.wav").write_bytes(b"")
        self.assertEqual([(m, y) for _p, m, y in ms.audio_tracks(d)], [(None, None)])

    def test_a_quit_keeps_the_recording_and_transcript_first_then_the_tracks(self):
        import audio_export
        rec = _SpeakerRec()
        rec.request_abort = MagicMock()
        app = _App(rec, meeting_audio_mode="smart_meeting")
        app.live_assist = MagicMock()
        s = self.session(app)
        s.start("Late call", "", language="en", audio_mode="smart_meeting")
        s._on_chunk_transcribed(0, "We are nearly done.", "en")
        seen = []
        real = audio_export.write_wav_int16

        def spy(path, samples, rate=16000):
            folder = s.meeting_dir
            seen.append((Path(path).name, (folder / "audio_part1.wav").is_file(),
                         (folder / "transcript.txt").is_file(),
                         ms.load_meta(folder).get(msess.META_NOTES_PENDING)))
            return real(path, samples, rate)
        with patch("audio_export.write_wav_int16", side_effect=spy):
            folder = s.save_for_quit()
        self.assertEqual(seen[0], ("audio_part1_mic.wav", True, True, True))
        self.assertEqual(sorted(p.name for p in Path(folder).glob("*.wav")),
                         ["audio_part1.wav", "audio_part1_mic.wav", "audio_part1_system.wav"])


class _SpeakerBase(_Base):
    def _stopped(self, rec=None, mode="smart_meeting", build=None, **cfg):
        """A meeting recorded and stopped with the speaker pass's build_transcript
        replaced by ``build``."""
        if rec is None:
            rec = _SpeakerRec()
        app = _App(rec, meeting_audio_mode=mode, **cfg)
        s = self.session(app)
        s.start("Call", "Anna", audio_mode=mode, language="en")
        s._on_chunk_transcribed(0, "live piece", "en")
        calls = []

        def fake_build(recorder, parts, **kw):
            calls.append((parts, kw))
            return build(recorder, parts, **kw) if callable(build) else (build or _outcome())
        with patch.object(msess.meeting_speakers, "build_transcript", side_effect=fake_build), \
                patch.object(msess.actions, "process", return_value="## Notes") as proc:
            s.stop("")
        s._build_calls, s._proc = calls, proc
        return s


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestSpeakerLabelsAtStop(_SpeakerBase):
    def test_the_labelled_transcript_is_the_meetings_transcript(self):
        s = self._stopped(build=_outcome(("You: hi there\nSpeaker 1: hello",)))
        self.assertEqual((s.meeting_dir / "transcript.txt").read_text(encoding="utf-8"),
                         "You: hi there\nSpeaker 1: hello")
        parts, kw = s._build_calls[0]
        self.assertEqual(len(parts), 1)
        self.assertTrue(parts[0].has_tracks)                    # the two sources, not only the mix
        self.assertEqual(kw["language"], "en")
        self.assertFalse(kw["privacy"])
        self.assertEqual(s.notices(), [])
        self.assertIn("You: hi there", s._proc.call_args[0][0])  # the notes are written from it

    def test_without_the_two_tracks_the_mix_alone_goes_in(self):
        rec = _SpeakerRec()
        rec.with_tracks = False
        s = self._stopped(rec, mode="smart_meeting")
        parts, _kw = s._build_calls[0]
        self.assertFalse(parts[0].has_tracks)
        s = self._stopped(_SpeakerRec(), mode="default_mic")    # only a mic + system meeting uses them
        self.assertFalse(s._build_calls[0][0][0].has_tracks)

    def test_when_no_labels_can_be_made_the_transcript_says_so_and_how_to_retry(self):
        reason = msess.meeting_speakers.R_PRIVACY
        out = _outcome((), reason, msess.meeting_speakers.notice_for(reason))
        s = self._stopped(build=out, privacy_mode=True)
        self.assertEqual((s.meeting_dir / "transcript.txt").read_text(encoding="utf-8"),
                         "Hello team. We ship Friday.")          # the plain transcript stays
        notice = s.notices()[0]
        self.assertIn("Speaker labels weren't added", notice)
        self.assertIn("Privacy mode", notice)
        self.assertIn("Identify speakers", notice)
        self.assertEqual(ms.load_meta(s.meeting_dir)["notices"], s.notices())
        self.assertTrue(s._build_calls[0][1]["privacy"])
        self.assertEqual(s.phase, "done")                        # the notes are written anyway

    def test_an_exception_in_the_pass_is_a_notice_not_silence(self):
        def boom(recorder, parts, **kw):
            raise RuntimeError("segfault in a model")
        s = self._stopped(build=boom)
        self.assertEqual(s.phase, "done")
        self.assertTrue(any("something went wrong" in n for n in s.notices()))

    def test_too_short_a_recording_adds_no_notice_and_no_warning(self):
        # Nothing to label is no problem: INFO in the log, nothing to worry the reader (see also
        # TestNotesFirstSpeakersAfter.test_a_recording_too_short_to_label_is_info_and_no_notice).
        rec = _SpeakerRec()
        rec.get_full_audio = lambda: None
        s = self._stopped(rec)
        self.assertEqual(s._build_calls, [])
        self.assertEqual(s.notices(), [])
        self.assertEqual(s.phase, "done")

    def test_people_on_the_call_who_cannot_be_told_apart_keep_a_notice_with_the_text(self):
        out = _outcome(("You: a\nThem: b",), msess.meeting_speakers.R_NO_VOICES,
                       msess.meeting_speakers.CALL_TOGETHER)
        s = self._stopped(build=out)
        self.assertEqual((s.meeting_dir / "transcript.txt").read_text(encoding="utf-8"), "You: a\nThem: b")
        self.assertIn(msess.meeting_speakers.CALL_TOGETHER, s.notices())

    def test_a_long_silence_in_the_recording_is_named_with_its_times(self):
        rec = _SpeakerRec()
        rec.stretches = [(1260.0, 2715.0)]
        s = self.session(_App(rec))
        s._duration_sec = lambda: 2716
        s._note_quiet_stretches()
        text = [n for n in s.notices() if n.startswith("No sound reached Gusan")]
        self.assertEqual(len(text), 1)
        self.assertIn("from 21 min 00 s to the end", text[0])
        self.assertIn("check the microphone and the speaker output", text[0])
        rec2 = _SpeakerRec()
        rec2.stretches = [(300.0, 600.0), (900.0, 1500.0)]
        s2 = self.session(_App(rec2))
        s2._duration_sec = lambda: 3000
        s2._note_quiet_stretches()
        self.assertIn("from 15 min 00 s to 25 min 00 s", s2.notices()[0])        # the longest stretch
        s3 = self.session(_App(_SpeakerRec()))                                   # nothing quiet: no notice
        s3._note_quiet_stretches()
        self.assertEqual(s3.notices(), [])

    def test_live_pieces_skipped_for_speed_are_reported_only_when_the_text_was_not_rebuilt(self):
        rec = _SpeakerRec()
        rec.health = {"dropped_sec": 300.0}
        failed = _outcome((), msess.meeting_speakers.R_NO_COMPONENT,
                          msess.meeting_speakers.notice_for(msess.meeting_speakers.R_NO_COMPONENT))
        s = self._stopped(rec, build=failed)
        self.assertTrue(any("skipped about 5 min" in n for n in s.notices()))
        rec2 = _SpeakerRec()
        rec2.health = {"dropped_sec": 300.0}
        s2 = self._stopped(rec2)                                  # rebuilt from the recording: nothing lost
        self.assertFalse(any("skipped" in n for n in s2.notices()))

    def test_a_retried_pass_replaces_the_old_failure_notice(self):
        s = self.session(_App())
        s._transcript_notices = [msess.meeting_speakers.notice_for("privacy"), "Something else."]
        s._drop_speaker_notices()
        self.assertEqual(s._transcript_notices, ["Something else."])


class TestWatchSound(_Base):
    def test_one_tray_hint_per_quiet_stretch(self):
        rec = _Rec()
        rec.quiet = 0.0
        rec.quiet_seconds = lambda: rec.quiet
        app = _App(rec)
        app.show_tray_hint = MagicMock()
        s = self.session(app)
        s.start("x")
        for q in (0.0, 120.0, 180.0, 240.0):
            rec.quiet = q
            s._watch_sound()
        self.assertEqual(app.show_tray_hint.call_count, 1)
        self.assertIn("3 minutes", app.show_tray_hint.call_args[0][1])
        rec.quiet = 0.0                                          # sound is back...
        s._watch_sound()
        rec.quiet = 300.0                                        # ...and gone again
        s._watch_sound()
        self.assertEqual(app.show_tray_hint.call_count, 2)

    def test_never_raises_and_never_during_a_recorder_without_the_feature(self):
        s = self.session(_App())
        s.start("x")
        s._watch_sound()                                         # _Rec has no quiet_seconds
        s.app.recorder.quiet_seconds = MagicMock(side_effect=RuntimeError("x"))
        s._watch_sound()


class TestJoinParts(unittest.TestCase):
    def test_parts_are_joined_with_the_resumed_markers(self):
        join = msess.MeetingCore._join_parts
        self.assertEqual(join(["only"]), "only")
        self.assertEqual(join(["A", "B"]), "A\n\n— Resumed —\n\nB")
        old = "x\n\n— Resumed 2026-10-21 11:20 —\n\ny"
        self.assertEqual(join(["A", "B"], old), "A\n\n— Resumed 2026-10-21 11:20 —\n\nB")
        self.assertEqual(join(["A", "", "C"], old), "A\n\n— Resumed —\n\nC")   # 1 marker, 2 expected
        self.assertEqual(join(["", "B"]), "B")


class _SavedMeeting(_Base):
    """A meeting folder as the app leaves it: files only."""

    def _folder(self, name="20261021_103134", transcript=None, chunks=("live one", "live two"),
                audio=True, notes=None, tracks=False, **meta):
        d = self.root / name
        d.mkdir()
        m = {"title": "Live session 10:31", "duration_sec": 100, "started": "2026-10-21 10:31:34",
             "language": "en", "source": "live_assist"}
        m.update(meta)
        (d / "meta.json").write_text(json.dumps(m), encoding="utf-8")
        if transcript is not None:
            (d / "transcript.txt").write_text(transcript, encoding="utf-8")
        if chunks:
            with open(d / "chunks.jsonl", "w", encoding="utf-8") as f:
                for i, t in enumerate(chunks):
                    f.write(json.dumps({"index": i, "text": t, "language": "en"}) + "\n")
        if audio:
            (d / "audio_part1.wav").write_bytes(b"RIFF" + b"\0" * 200)
        if tracks:
            (d / "audio_part1_mic.wav").write_bytes(b"RIFF" + b"\0" * 200)
            (d / "audio_part1_system.wav").write_bytes(b"RIFF" + b"\0" * 200)
        if notes is not None:
            (d / "notes.md").write_text(notes, encoding="utf-8")
        return d

    def _build(self, session, outcome):
        calls = []

        def fake(recorder, parts, **kw):
            calls.append((parts, kw))
            return outcome(recorder, parts, **kw) if callable(outcome) else outcome
        p = patch.object(msess.meeting_speakers, "build_transcript", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return calls


class TestFinishingAnInterruptedMeeting(_SavedMeeting):
    def test_notes_on_a_meeting_the_app_never_finished_read_the_recording_first(self):
        d = self._folder()                                    # live pieces + the recording, no transcript.txt
        app = _App()
        s = self.session(app, inline=False)
        calls = self._build(s, _outcome(("You: rebuilt a\nSpeaker 1: rebuilt b",)))
        done = _Recorder(s.finished)
        self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual(s.state, "processing")
        self.assertTrue(s.holds_recorder)                     # it uses the speech model now
        self.assertEqual(s.busy_label, "Rebuilding the transcript and notes…")
        self.assertTrue(s.is_busy)
        target, args = s.spawned[0]
        with patch.object(msess.actions, "process", return_value="## Notes") as proc:
            target(*args)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][0]), 1)                 # the one saved part
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"),
                         "You: rebuilt a\nSpeaker 1: rebuilt b")
        self.assertIn("rebuilt a", proc.call_args[0][0])      # the notes come from the rebuilt text
        self.assertNotIn("live one", proc.call_args[0][0])
        meta = ms.load_meta(d)
        self.assertIn(msess.RECOVERED_NOTICE, meta["notices"])
        self.assertEqual(done.calls, [(str(d), "## Notes", "")])
        self.assertEqual(s.phase, "done")
        self.assertFalse(s._saved_audio_run)
        self.assertEqual(s.busy_label, "Writing notes…")

    def test_when_the_recording_cannot_be_read_the_live_pieces_become_the_transcript(self):
        d = self._folder()
        s = self.session(_App())
        reason = msess.meeting_speakers.R_NO_COMPONENT
        self._build(s, _outcome((), reason, msess.meeting_speakers.notice_for(reason)))
        with patch.object(msess.actions, "process", return_value="## Notes") as proc:
            self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "live one live two")
        self.assertIn("live one live two", proc.call_args[0][0])
        notices = ms.load_meta(d)["notices"]
        self.assertIn(msess.RECOVERED_NOTICE, notices)
        self.assertTrue(any("Speaker labels weren't added" in n for n in notices))

    def test_an_error_while_reading_the_recording_is_not_the_end_of_the_notes(self):
        d = self._folder()
        s = self.session(_App())
        self._build(s, lambda recorder, parts, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual(s.phase, "done")
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "live one live two")

    def test_a_recording_with_no_live_pieces_at_all_is_still_recoverable(self):
        d = self._folder(chunks=())
        s = self.session(_App())
        self._build(s, _outcome(("You: all there is",)))
        with patch.object(msess.actions, "process", return_value="## Notes") as proc:
            self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "You: all there is")
        self.assertIn("all there is", proc.call_args[0][0])

    def test_while_a_dictation_runs_the_notes_use_the_live_pieces_as_before(self):
        d = self._folder()
        app = _App()
        app.is_rec = True
        s = self.session(app)
        calls = self._build(s, _outcome(("never used",)))
        with patch.object(msess.actions, "process", return_value="## Notes") as proc:
            self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual(calls, [])
        self.assertFalse((d / "transcript.txt").exists())
        self.assertIn("live one live two", proc.call_args[0][0])
        self.assertEqual(s.phase, "done")

    def test_a_dictation_blocks_a_recovery_that_has_nothing_else_to_go_on(self):
        d = self._folder(chunks=())
        app = _App()
        app.is_rec = True
        s = self.session(app)
        self.assertFalse(s.retry_notes(None, folder=str(d)))
        self.assertEqual(s.state, "idle")

    def test_a_meeting_with_a_saved_transcript_is_not_read_again(self):
        d = self._folder(transcript="Anna: saved text")
        s = self.session(_App())
        calls = self._build(s, _outcome(("never",)))
        with patch.object(msess.actions, "process", return_value="## Notes") as proc:
            self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual(calls, [])
        self.assertIn("saved text", proc.call_args[0][0])
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "Anna: saved text")

    def test_a_meeting_without_audio_behaves_as_before(self):
        d = self._folder(audio=False)
        s = self.session(_App())
        calls = self._build(s, _outcome(("never",)))
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual(calls, [])
        self.assertFalse((d / "transcript.txt").exists())
        d2 = self._folder("20261021_120000", audio=False, chunks=())
        self.assertFalse(self.session(_App()).retry_notes(None, folder=str(d2)))

    def test_notes_a_quit_left_for_later_are_written_from_the_audio_only_without_a_transcript(self):
        d = self._folder(**{msess.META_NOTES_PENDING: True})
        s = self.session(_App())
        calls = self._build(s, _outcome(("You: x",)))
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s.write_pending_notes(str(d)))
        self.assertEqual(len(calls), 1)
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "You: x")
        # with the transcript a quit saved there is no heavy pass at launch
        d2 = self._folder("20261021_130000", transcript="live", **{msess.META_NOTES_PENDING: True})
        s2 = self.session(_App())
        calls2 = self._build(s2, _outcome(("never",)))
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s2.write_pending_notes(str(d2)))
        self.assertEqual(calls2, [])

    def test_quitting_during_a_recovery_keeps_the_meeting_as_it_was(self):
        d = self._folder()
        s = self.session(_App(), inline=False)
        self._build(s, _outcome(("You: x",)))
        self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual(s.save_for_quit(), "")                 # nothing live to keep
        self.assertFalse((d / "transcript.txt").exists())        # the stale pieces are NOT written over it
        self.assertIsNone(ms.load_meta(d).get(msess.META_NOTES_PENDING))


class TestIdentifySpeakers(_SavedMeeting):
    def _start(self, d, outcome, app=None, inline=False):
        s = self.session(app or _App(), inline=inline)
        calls = self._build(s, outcome)
        return s, calls

    def test_refusals(self):
        d = self._folder(transcript="old")
        errors = []
        s = self.session(_App())
        s.error.connect(lambda *a: errors.append(a))
        no_audio = self._folder("20261021_110000", transcript="t", audio=False)
        self.assertEqual(s.identify_speakers(no_audio)[:2], (False, "no_audio"))
        self.assertEqual(s.identify_speakers(self.root / "nothing")[:2], (False, "no_audio"))
        app = _App()
        app.recorder = None
        self.assertEqual(self.session(app).identify_speakers(d)[:2], (False, "no_recorder"))
        app = _App()
        app.is_rec = True
        self.assertEqual(self.session(app).identify_speakers(d)[:2], (False, "busy"))
        app = _App()
        app._file_job_running = True
        self.assertEqual(self.session(app).identify_speakers(d)[:2], (False, "file_job"))
        busy = self.session(_App())
        busy.start("recording now")
        self.assertEqual(busy.identify_speakers(d)[:2], (False, "busy"))
        self.assertEqual([e[0] for e in errors], ["no_audio", "no_audio"])
        self.assertEqual(s.state, "idle")

    def test_labels_replace_the_transcript_and_nothing_else_changes(self):
        d = self._folder(transcript="old plain transcript", notes="## Notes\n- kept",
                         notices=["Gusan was closed during this meeting."],
                         notes_notice="", notes_engine="managed")
        s, calls = self._start(d, _outcome(("You: new a\nSpeaker 1: new b",)))
        done, errors = _Recorder(s.finished), _Recorder(s.error)
        phases = _Recorder(s.state_changed)
        ok, code, msg = s.identify_speakers(d)
        self.assertEqual((ok, code, msg), (True, "", ""))
        self.assertEqual((s.state, s.phase), ("processing", "processing"))
        self.assertTrue(s.holds_recorder)
        self.assertEqual(s.busy_label, "Identifying speakers…")
        self.assertEqual(s.active_dir(), d)
        self.assertEqual(phases.calls, [("processing",)])
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "old plain transcript")
        target, args = s.spawned[0]
        target(*args)
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"),
                         "You: new a\nSpeaker 1: new b")
        self.assertFalse((d / ms.BEFORE_SPEAKERS).exists())      # success: the old copy is gone
        self.assertEqual((d / "notes.md").read_text(encoding="utf-8"), "## Notes\n- kept")
        meta = ms.load_meta(d)
        self.assertEqual(meta["notices"], ["Gusan was closed during this meeting."])
        self.assertIn("speakers_at", meta)
        self.assertEqual(meta["notes_engine"], "managed")
        # The page learns through finished, with the notes it had; nothing is "new".
        self.assertEqual(done.calls, [(str(d), "## Notes\n- kept", "")])
        self.assertEqual(errors.calls, [])
        self.assertEqual((s.phase, s.last_run_speakers, s.last_notes_new), ("done", True, False))
        self.assertEqual(phases.calls, [("processing",), ("done",)])
        self.assertEqual(s.app.released, 1)
        self.assertFalse(s.is_busy)
        self.assertEqual(s.busy_label, "Writing notes…")
        self.assertEqual(len(calls), 1)

    def test_the_old_text_is_kept_while_it_runs(self):
        d = self._folder(transcript="old plain transcript")
        seen = {}

        def build(recorder, parts, **kw):
            seen["backup"] = (d / ms.BEFORE_SPEAKERS).read_text(encoding="utf-8")
            seen["current"] = (d / "transcript.txt").read_text(encoding="utf-8")
            return _outcome(("You: new",))
        s, _ = self._start(d, build, inline=True)
        self.assertTrue(s.identify_speakers(d)[0])
        self.assertEqual(seen, {"backup": "old plain transcript", "current": "old plain transcript"})
        self.assertFalse((d / ms.BEFORE_SPEAKERS).exists())

    def test_the_two_tracks_are_used_when_the_meeting_kept_them(self):
        d = self._folder(transcript="old", tracks=True)
        s, calls = self._start(d, _outcome(("You: x",)), inline=True)
        s.identify_speakers(d)
        parts, kw = calls[0]
        self.assertEqual(len(parts), 1)
        self.assertTrue(parts[0].has_tracks)
        self.assertEqual(kw["language"], "en")                   # the language the meeting found
        d2 = self._folder("20261021_150000", transcript="old")
        s2, calls2 = self._start(d2, _outcome(("Speaker 1: x",)), inline=True)
        s2.identify_speakers(d2)
        self.assertFalse(calls2[0][0][0].has_tracks)             # an old meeting: the mix

    def test_the_language_is_the_meetings_own_not_the_last_recordings(self):
        d = self._folder(transcript="old", language="fr")
        s, calls = self._start(d, _outcome(("You: x",)), inline=True)
        s._session_language = "multi"                    # left over from another meeting
        s.identify_speakers(d)
        self.assertEqual(calls[0][1]["language"], "fr")

    def test_an_audio_only_meeting_gets_its_first_transcript(self):
        d = self._folder(chunks=())
        s, _ = self._start(d, _outcome(("You: first words",)), inline=True)
        self.assertTrue(s.identify_speakers(d)[0])
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "You: first words")
        self.assertEqual(ms.meeting_status(ms.list_meetings()[0]), "notes_failed")   # transcript, no notes yet

    def test_a_cancelled_run_changes_nothing(self):
        d = self._folder(transcript="old")
        holder = {}

        def build(recorder, parts, **kw):
            holder["session"].cancel_identify()
            return _outcome((), cancelled=True) if kw["should_cancel"]() else _outcome(("never",))
        s, _ = self._start(d, build, inline=True)
        holder["session"] = s
        done, errors = _Recorder(s.finished), _Recorder(s.error)
        self.assertTrue(s.identify_speakers(d)[0])
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "old")
        self.assertFalse((d / ms.BEFORE_SPEAKERS).exists())
        self.assertEqual(len(done.calls), 1)
        self.assertEqual(errors.calls, [])
        self.assertEqual(s.phase, "done")
        self.assertNotIn("speakers_at", ms.load_meta(d))
        self.assertFalse(s.cancel_identify())                    # nothing running any more

    def test_a_run_that_cannot_label_keeps_the_transcript_and_says_why(self):
        d = self._folder(transcript="old")
        reason = msess.meeting_speakers.R_NO_SEGMENTS
        s, _ = self._start(d, _outcome((), reason, msess.meeting_speakers.notice_for(reason)), inline=True)
        errors = _Recorder(s.error)
        done = _Recorder(s.finished)
        self.assertTrue(s.identify_speakers(d)[0])
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "old")
        self.assertTrue(ms.load_meta(d)["notices"][0].startswith("Speaker labels weren't added"))
        self.assertEqual(errors.calls[0][0], "speakers")
        self.assertIn("couldn't be read again", errors.calls[0][1])
        self.assertEqual(len(done.calls), 1)
        self.assertEqual(s.phase, "done")

    def test_an_exception_is_reported_the_same_way(self):
        d = self._folder(transcript="old")

        def build(recorder, parts, **kw):
            raise RuntimeError("model crashed")
        s, _ = self._start(d, build, inline=True)
        errors = _Recorder(s.error)
        self.assertTrue(s.identify_speakers(d)[0])
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "old")
        self.assertEqual(errors.calls[0][0], "speakers")
        self.assertEqual(s.phase, "done")
        # the copy of the old text stays after a run that did not finish
        self.assertTrue((d / ms.BEFORE_SPEAKERS).exists())

    def test_a_second_run_replaces_the_first_runs_failure_notice(self):
        d = self._folder(transcript="old")
        reason = msess.meeting_speakers.R_PRIVACY
        s, calls = self._start(d, _outcome((), reason, msess.meeting_speakers.notice_for(reason)), inline=True)
        s.identify_speakers(d)
        self.assertEqual(len(ms.load_meta(d)["notices"]), 1)
        s2, _ = self._start(d, _outcome(("You: ok",)), inline=True)
        s2.identify_speakers(d)
        self.assertEqual(ms.load_meta(d)["notices"], [])
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "You: ok")

    def test_parts_of_a_continued_meeting_are_joined_with_the_resumed_marker(self):
        d = self._folder(transcript="a\n\n— Resumed 2026-10-21 11:20 —\n\nb")
        (d / "audio_part2.wav").write_bytes(b"RIFF" + b"\0" * 200)
        s, calls = self._start(d, _outcome(("You: part one", "Speaker 1: part two")), inline=True)
        s.identify_speakers(d)
        self.assertEqual(len(calls[0][0]), 2)
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"),
                         "You: part one\n\n— Resumed 2026-10-21 11:20 —\n\nSpeaker 1: part two")

    def test_quitting_during_the_run_leaves_everything_as_it_was(self):
        d = self._folder(transcript="old")
        s, _ = self._start(d, _outcome(("You: x",)))
        s.identify_speakers(d)
        self.assertEqual(s.save_for_quit(), "")
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "old")
        self.assertIsNone(ms.load_meta(d).get(msess.META_NOTES_PENDING))

    def test_the_session_is_busy_meanwhile_and_refuses_other_work(self):
        d = self._folder(transcript="old")
        s, _ = self._start(d, _outcome(("You: x",)))
        s.identify_speakers(d)
        self.assertTrue(s.is_busy)
        self.assertEqual(s.identify_speakers(d)[1], "busy")
        self.assertFalse(s.retry_notes(None, folder=str(d)))
        self.assertFalse(s.abort())                              # never while the work runs
        ok, code, _m = s.start("another")
        self.assertEqual((ok, code), (False, "busy"))

    def test_progress_reaches_the_status_signal(self):
        d = self._folder(transcript="old")

        def build(recorder, parts, **kw):
            kw["status"]("Re-reading the recording… 40%")
            return _outcome(("You: x",))
        s, _ = self._start(d, build, inline=True)
        status = _Recorder(s.status)
        s.identify_speakers(d)
        self.assertIn(("Re-reading the recording… 40%",), status.calls)


# ── Notes first, speaker names after ─────────────────────────────────────────

class _SegRec(_Rec):
    """A recorder that kept a mic + system sound meeting (or only the system sound),
    whose re-read of the recording finds scripted segments with timestamps."""

    SEGMENTS = [{"start": 2.0, "end": 6.0, "text": "Hello team."},
                {"start": 7.0, "end": 11.0, "text": "Welcome."},
                {"start": 12.0, "end": 15.0, "text": "Thanks."},
                {"start": 16.0, "end": 36.0, "text": "Let us ship on Friday."}]

    def __init__(self, seconds=40, you=((2, 6), (12, 15)), them=((7, 11), (16, 36)),
                 system_only=False, segments=None, **kw):
        super().__init__(**kw)
        import numpy as np
        from tests.test_meeting_speakers import call_tracks
        self.mic, self.system = call_tracks(np, seconds, [] if system_only else list(you), list(them))
        self.system_only = system_only
        self.segments = [dict(s) for s in (segments or self.SEGMENTS)]
        self.reads = 0

    def get_full_audio(self):
        import numpy as np
        if self.system_only:
            return self.system.astype(np.float32) / 32767.0
        return (self.mic.astype(np.float32) + self.system.astype(np.float32)) / 32767.0 * 0.75

    def get_full_audio_tracks(self):
        return None if self.system_only else (self.mic, self.system)

    def transcribe_segments(self, audio, language=None, model_name=None, on_progress=None,
                            should_cancel=None):
        self.reads += 1
        return [dict(s) for s in self.segments]

    def quiet_stretches(self, min_sec=None):
        return []

    def chunk_health(self):
        return {}

    def quiet_seconds(self):
        return 0.0


def _interruptible(hook, **kw):
    """A diarizer (the fake of test_meeting_speakers) that runs ``hook`` first and then
    stops when asked to, like the real child process does; ``.runs`` counts its calls."""
    from tests.test_meeting_speakers import FakeDiarizer

    class Diarizer(FakeDiarizer):
        runs = 0

        def diarize(self, audio, num_speakers=None, threshold=0.7, on_progress=None, should_cancel=None):
            self.runs += 1
            hook(self)
            if should_cancel is not None and should_cancel():
                self.last_error = "cancelled"
                return []
            return FakeDiarizer.diarize(self, audio, num_speakers, threshold, on_progress, should_cancel)
    return Diarizer(**kw)


LABELLED = "You: Hello team.\nSpeaker 1: Welcome.\nYou: Thanks.\nSpeaker 1: Let us ship on Friday."
SOURCES_ONLY = "You: Hello team.\nThem: Welcome.\nYou: Thanks.\nThem: Let us ship on Friday."


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestNotesFirstSpeakersAfter(_SpeakerBase):
    def _meeting(self, diarizer, mode="smart_meeting", rec=None, inline=True, **app_kw):
        rec = rec or _SegRec(system_only=(mode == "system_only"))
        app = _App(rec, meeting_audio_mode=mode, **app_kw)
        s = self.session(app, inline=inline)
        s._followup.poll_s = 0.0
        s._followup.settle_polls = 0                                # (the waits are scripted by each test)
        s.events = []
        s.finished.connect(lambda *a: s.events.append(("finished", a)))
        s.changes = _Recorder(s.speakers_changed)
        patchers = [patch.object(msess.meeting_speakers, "diarization", diarizer)]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        return s, rec, app

    def _stop(self, s, mode="smart_meeting", notes="## Notes\n- Ship Friday"):
        def process(text, action, **kw):
            folder = s.meeting_dir
            s.stopped_folder = folder
            s.events.append(("notes", text, s.is_busy,
                             (folder / "transcript.txt").read_text(encoding="utf-8"),
                             (folder / "segments.json").is_file()))
            return notes
        with patch.object(msess.actions, "process", side_effect=process) as proc:
            self.assertTrue(s.start("Call", "Anna", audio_mode=mode, language="en")[0])
            self.assertTrue(s.stop(""))
        s._proc = proc
        return s.stopped_folder                  # (the session may hold another meeting by now)

    # ── the order ──
    def test_the_notes_and_you_them_transcript_come_first_the_names_after(self):
        from tests.test_meeting_speakers import Probe
        seen = {}

        def hook(d):
            folder = s.meeting_dir
            s.events.append(("diarize",))
            seen.update(busy=s.is_busy, phase=s.phase, state=s.speakers_state(folder),
                        transcript=(folder / "transcript.txt").read_text(encoding="utf-8"),
                        backup=(folder / ms.BEFORE_SPEAKERS).read_text(encoding="utf-8"),
                        notes=(folder / "notes.md").read_text(encoding="utf-8"))
        s, rec, app = self._meeting(Probe(hook))
        folder = self._stop(s)
        order = [e[0] for e in s.events]
        self.assertEqual(order, ["notes", "finished", "diarize"])
        # 1. the notes were written from the You / Them transcript, already on disk with its segments
        notes = s.events[0]
        self.assertTrue(notes[2])                                  # still processing: the session is busy
        self.assertIn(SOURCES_ONLY, notes[1])                      # the notes engine was given You / Them
        self.assertEqual(notes[3], SOURCES_ONLY)
        self.assertTrue(notes[4])                                  # segments.json was there
        # 2. the session is idle when the names start: another one could start right now
        self.assertEqual(s.events[1][1], (str(folder), "## Notes\n- Ship Friday", ""))
        self.assertFalse(seen["busy"])
        self.assertEqual(seen["phase"], "done")
        self.assertEqual(seen["state"], "running")
        self.assertEqual(seen["transcript"], SOURCES_ONLY)         # replaced only at the end...
        self.assertEqual(seen["backup"], SOURCES_ONLY)             # ...with the old text kept until then
        self.assertEqual(seen["notes"], "## Notes\n- Ship Friday")
        # 3. the end: names in the transcript, notes NOT written again, nothing re-read
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), LABELLED)
        self.assertEqual(s._proc.call_count, 1)
        self.assertEqual((folder / "notes.md").read_text(encoding="utf-8"), "## Notes\n- Ship Friday")
        self.assertEqual(rec.reads, 1)                             # no second Whisper pass
        self.assertFalse((folder / ms.BEFORE_SPEAKERS).exists())
        self.assertEqual(s.speakers_state(), "")
        self.assertIn("speakers_at", ms.load_meta(folder))
        self.assertEqual(ms.load_meta(folder)["notices"], [])
        self.assertEqual(s.phase, "done")
        self.assertTrue(set(c[0] for c in s.changes.calls) == {str(folder)})   # the open page is told

    def test_what_is_kept_with_the_meeting(self):
        from tests.test_meeting_speakers import FakeDiarizer
        s, rec, app = self._meeting(FakeDiarizer(fail="no speaker turns"))      # names can't be told: you/them stay
        folder = self._stop(s)
        data = ms.load_segments(folder)
        self.assertEqual(data["version"], 1)
        part = data["parts"][0]
        self.assertEqual((part["audio"], part["kind"]), ("audio_part1.wav", "sources"))
        self.assertEqual(part["text"], SOURCES_ONLY)
        self.assertEqual(part["seconds"], 40.0)
        self.assertEqual(part["segments"][0], {"s": 2.0, "e": 6.0, "t": "Hello team.", "src": "you"})
        self.assertEqual([x["src"] for x in part["segments"]], ["you", "them", "you", "them"])
        self.assertEqual(sorted(p.name for p in folder.glob("*.wav")),
                         ["audio_part1.wav", "audio_part1_mic.wav", "audio_part1_system.wav"])
        # the transcript is the You / Them one and the meeting says so
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), SOURCES_ONLY)
        self.assertIn(msess.meeting_speakers.CALL_TOGETHER, ms.load_meta(folder)["notices"])
        # compact: one small file, no wasted space
        self.assertLess((folder / "segments.json").stat().st_size, 2000)
        self.assertNotIn("\n", (folder / "segments.json").read_text(encoding="utf-8"))
        self.assertEqual([p.name for p in folder.iterdir() if p.name.endswith(".tmp")], [])

    def test_a_meeting_recorded_from_the_system_sound_only_gets_speaker_n_only_and_in_the_background(self):
        from tests.test_meeting_speakers import Probe
        seen = {}

        def hook(d):
            seen["before"] = (s.meeting_dir / "transcript.txt").read_text(encoding="utf-8")
            seen["busy"] = s.is_busy
        s, rec, app = self._meeting(Probe(hook), mode="system_only")
        folder = self._stop(s, mode="system_only")
        self.assertEqual(seen, {"before": "Them: Hello team. Welcome. Thanks. Let us ship on Friday.",
                                "busy": False})
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"),
                         "Speaker 1: Hello team. Welcome. Thanks. Let us ship on Friday.")
        self.assertEqual(ms.load_segments(folder)["parts"][0]["kind"], "call")
        self.assertEqual([p.name for p in folder.glob("*.wav")], ["audio_part1.wav"])     # the mix IS the call
        self.assertEqual(rec.reads, 1)

    # ── the rules of the background pass ──
    def test_it_waits_while_anything_needs_the_computer(self):
        from tests.test_meeting_speakers import Probe
        ran = []
        s, rec, app = self._meeting(Probe(lambda d: ran.append(1)))
        app.is_rec = True                                           # a dictation is running when the notes land
        polls = []
        s._followup._wait = lambda sec: (polls.append(sec), setattr(app, "is_rec", len(polls) < 3))
        folder = self._stop(s)
        self.assertEqual(len(polls), 3)                             # it looked again and again
        self.assertEqual(len(ran), 1)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), LABELLED)
        for flag in ("_file_job_running", "_busy"):                 # an Audio to text job, a dictation being written
            other, _r, app2 = self._meeting(Probe(lambda d: ran.append(1)))
            other.finished.connect(lambda *a, ap=app2, f=flag: setattr(ap, f, True))     # starts as the notes land
            other._followup._wait = lambda sec, ap=app2, f=flag: setattr(ap, f, False)
            self._stop(other)
            self.assertFalse(getattr(app2, flag))
        self.assertEqual(len(ran), 3)

    def test_a_recording_that_starts_mid_pass_stops_it_at_once_and_it_retries_when_free(self):
        calls = {"n": 0}

        def hook(d):
            calls["n"] += 1
            if calls["n"] == 1:
                app.is_rec = True                                   # Alt-R pressed while the names are worked out
        s, rec, app = self._meeting(_interruptible(hook))
        s._followup._wait = lambda sec: setattr(app, "is_rec", False)
        folder = self._stop(s)
        self.assertEqual(calls["n"], 2)                             # stopped once, then done
        self.assertEqual(s._followup.retries(folder), 1)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), LABELLED)
        self.assertFalse((folder / ms.BEFORE_SPEAKERS).exists())
        self.assertEqual(s._proc.call_count, 1)

    def test_a_meeting_that_starts_recording_cancels_it_too(self):
        def hook(d):
            s.state = msess.STATE_RECORDING                         # a new meeting started
        s, rec, app = self._meeting(_interruptible(hook))
        s._followup._wait = lambda sec: (setattr(s, "state", msess.STATE_DONE), s._followup.shutdown())
        folder = self._stop(s)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), SOURCES_ONLY)
        self.assertFalse((folder / ms.BEFORE_SPEAKERS).exists())

    def test_at_most_five_retries_then_the_suggestion_stays(self):
        def hook(d):
            app.is_rec = True                                       # every time
        dia = _interruptible(hook)
        s, rec, app = self._meeting(dia)
        s._followup._wait = lambda sec: setattr(app, "is_rec", False)
        folder = self._stop(s)
        self.assertEqual(dia.runs, 6)                               # the first run + five retries
        self.assertEqual(s._followup.retries(folder), 6)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), SOURCES_ONLY)
        self.assertEqual(s.speakers_state(), "")                    # nothing waits any more
        meta = ms.load_meta(folder)
        self.assertNotIn("speakers_at", meta)                       # so the page keeps offering Identify speakers
        self.assertEqual(meta["notices"], [])
        self.assertFalse((folder / ms.BEFORE_SPEAKERS).exists())
        self.assertEqual(s.phase, "done")
        self.assertEqual(s._proc.call_count, 1)

    def test_quitting_ends_the_pass_and_keeps_you_and_them(self):
        def hook(d):
            s.shutdown_speaker_followup()                           # the app is quitting
        s, rec, app = self._meeting(_interruptible(hook))
        folder = self._stop(s)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), SOURCES_ONLY)
        self.assertEqual(s._followup.retries(folder), 0)            # not retried
        self.assertEqual(s.speakers_state(), "")
        self.assertFalse((folder / ms.BEFORE_SPEAKERS).exists())
        self.assertFalse(s._followup.enqueue(str(folder)))          # nothing starts once it quits

    def test_cancel_on_the_banner_stops_it_for_good(self):
        def hook(d):
            self.assertEqual(s.speakers_state(s.meeting_dir), "running")
            self.assertTrue(s.cancel_speaker_followup(s.meeting_dir))
        s, rec, app = self._meeting(_interruptible(hook))
        folder = self._stop(s)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), SOURCES_ONLY)
        self.assertEqual(s._followup.retries(folder), 0)
        self.assertEqual(s.speakers_state(), "")
        self.assertNotIn("speakers_at", ms.load_meta(folder))

    def test_a_crash_during_the_pass_keeps_the_you_and_them_transcript_and_the_notes(self):
        class Killed(BaseException):
            pass

        def hook(d):
            raise Killed()
        from tests.test_meeting_speakers import Probe
        s, rec, app = self._meeting(Probe(hook))
        with self.assertRaises(Killed):
            self._stop(s)
        folder = s.meeting_dir
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), SOURCES_ONLY)
        self.assertEqual((folder / "notes.md").read_text(encoding="utf-8"), "## Notes\n- Ship Friday")
        self.assertEqual((folder / ms.BEFORE_SPEAKERS).read_text(encoding="utf-8"), SOURCES_ONLY)
        self.assertEqual([p.name for p in folder.iterdir() if p.name.endswith(".tmp")], [])
        self.assertEqual(ms.load_transcript(folder), SOURCES_ONLY)
        self.assertEqual(ms.meeting_status(ms.list_meetings()[0]), "notes_ready")

    def test_a_failed_write_never_leaves_a_half_written_transcript(self):
        from tests.test_meeting_speakers import FakeDiarizer
        s, rec, app = self._meeting(FakeDiarizer())
        real = ms.os.replace

        def refuse_the_names(src, dst, *a, **k):
            if str(dst).endswith("transcript.txt") and "Speaker" in open(src, encoding="utf-8").read():
                raise PermissionError("locked")
            return real(src, dst, *a, **k)
        with patch.object(ms.os, "replace", side_effect=refuse_the_names), patch.object(ms.time, "sleep"):
            folder = self._stop(s)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), SOURCES_ONLY)
        self.assertEqual([p.name for p in folder.iterdir() if p.name.endswith(".tmp")], [])
        self.assertEqual(s.phase, "done")

    def test_a_new_meeting_can_start_while_the_names_wait_and_the_pass_never_touches_it(self):
        from tests.test_meeting_speakers import Probe
        ran = []
        s, rec, app = self._meeting(Probe(lambda d: ran.append(1)))
        s.finished.connect(lambda *a: setattr(app, "is_rec", True))     # a dictation starts as the notes land
        waits = []

        def wait(sec):
            waits.append(s.state)
            if len(waits) == 1:
                app.is_rec = False
                # ...then the next meeting starts at once (the session is idle: nothing refuses it)
                self.assertTrue(s.start("Next", "", audio_mode="smart_meeting")[0])
            else:
                self.assertTrue(s.abort())                              # and is dropped again
        s._followup._wait = wait
        folder = self._stop(s)
        self.assertEqual(waits, [msess.STATE_DONE, msess.STATE_RECORDING])    # it did not run while recording
        self.assertEqual(len(ran), 1)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), LABELLED)
        self.assertEqual(s.state, msess.STATE_IDLE)

    def test_it_never_starts_by_itself_at_launch(self):
        from tests.test_meeting_speakers import Probe
        ran = []
        s, rec, app = self._meeting(Probe(lambda d: ran.append(1)))
        folder = self._stop(s)
        self.assertEqual(len(ran), 1)
        # An old meeting that still has its segments (say the first pass was cancelled) and a Quit's notes:
        d = self.root / "20261001_090000"
        d.mkdir()
        (d / "transcript.txt").write_text(SOURCES_ONLY, encoding="utf-8")
        ms.write_segments(d, {"version": 1, "parts": [{"audio": "audio_part1.wav", "kind": "sources",
                                                       "seconds": 40.0, "done": False, "text": SOURCES_ONLY,
                                                       "segments": []}]})
        ms.update_meta(d, title="Old", **{msess.META_NOTES_PENDING: True})
        s2, _r, _a = self._meeting(Probe(lambda dd: ran.append(1)))
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s2.write_pending_notes(str(d)))
        self.assertEqual(len(ran), 1)                                # nothing was queued
        self.assertEqual(s2.speakers_state(), "")
        self.assertEqual(s2._followup.folders(), [])

    def test_identify_speakers_by_hand_stands_in_for_the_queued_pass(self):
        from tests.test_meeting_speakers import FakeDiarizer
        s, rec, app = self._meeting(FakeDiarizer(), inline=False)
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s.start("Call", "", audio_mode="smart_meeting")[0])
            self.assertTrue(s.stop(""))
            target, args = s.spawned[0]
            target(*args)                                       # Stop's worker; the names are only queued
        folder = s.meeting_dir
        self.assertEqual(s.speakers_state(folder), "waiting")
        out = msess.meeting_speakers.Outcome()
        out.texts = ["You: x"]
        with patch.object(msess.meeting_speakers, "build_transcript", return_value=out):
            self.assertTrue(s.identify_speakers(folder)[0])
        self.assertEqual(s.speakers_state(folder), "")          # the queued pass stood down
        self.assertEqual(s._followup.folders(), [])

    def test_a_rewrite_after_the_names_uses_them_and_keeps_the_notice(self):
        from tests.test_meeting_speakers import FakeDiarizer
        s, rec, app = self._meeting(FakeDiarizer())
        folder = self._stop(s)
        with patch.object(msess.actions, "process", return_value="## Rewritten") as proc:
            self.assertTrue(s.retry_notes("qwen_3b"))                 # Rewrite notes on the open meeting
        self.assertIn(LABELLED, proc.call_args[0][0])                 # written from the names, not You / Them
        self.assertNotIn("Them:", proc.call_args[0][0])
        self.assertEqual((folder / "notes.md").read_text(encoding="utf-8"), "## Rewritten")

    def test_the_names_of_a_continued_meeting_follow_only_its_new_part(self):
        from tests.test_meeting_speakers import FakeDiarizer
        s, rec, app = self._meeting(FakeDiarizer())
        folder = self._stop(s)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), LABELLED)
        second = _SegRec(seconds=40, segments=[{"start": 2.0, "end": 6.0, "text": "Back again."},
                                               {"start": 7.0, "end": 11.0, "text": "Welcome back."},
                                               {"start": 16.0, "end": 36.0, "text": "Where were we."}])
        app2 = _App(second, meeting_audio_mode="smart_meeting")
        s2 = self.session(app2)
        s2._followup.poll_s = 0.0
        with patch.object(msess.actions, "process", return_value="## Notes 2"):
            self.assertTrue(s2.continue_meeting(str(folder), audio_mode="smart_meeting")[0])
            self.assertTrue(s2.stop(""))
        text = (folder / "transcript.txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(LABELLED + "\n\n— Resumed "), text)
        self.assertTrue(text.endswith("You: Back again.\nSpeaker 1: Welcome back. Where were we."), text)
        data = ms.load_segments(folder)
        self.assertEqual([p["audio"] for p in data["parts"]], ["audio_part1.wav", "audio_part2.wav"])
        self.assertEqual([p["done"] for p in data["parts"]], [True, True])

    # ── a second look: what else can happen while the names are worked out ──
    def test_notes_being_rewritten_hold_the_names_back_like_everything_else_that_needs_the_computer(self):
        s, rec, app = self._meeting(_interruptible(lambda d: None))
        self.assertFalse(s._speaker_pass_blocked())
        for state, notes_only in ((msess.STATE_RECORDING, False), (msess.STATE_PROCESSING, False),
                                  (msess.STATE_PROCESSING, True)):      # the last: Rewrite notes / Retry
            s.state, s._notes_only = state, notes_only
            self.assertTrue(s._speaker_pass_blocked(), (state, notes_only))
        s.state = msess.STATE_DONE
        self.assertFalse(s._speaker_pass_blocked())

    def test_a_rewrite_of_the_notes_stops_a_running_pass_and_it_runs_again_after(self):
        calls = {"n": 0}
        rewrites = []

        def hook(d):
            calls["n"] += 1
            if calls["n"] == 1:
                # the person presses Rewrite notes while the names are worked out: only started here,
                # its worker waits in `spawned` (the computer is busy with it from now on)
                inline, s._spawn = s._spawn, (lambda target, *a: s.spawned.append((target, a)))
                try:
                    self.assertTrue(s.retry_notes("qwen_3b"))
                finally:
                    s._spawn = inline

        def wait(seconds):                      # the pass looks again: the rewrite runs, then the computer is free
            while s.spawned:
                target, args = s.spawned.pop(0)
                with patch.object(msess.actions, "process", return_value="## Rewritten") as proc:
                    target(*args)
                rewrites.append(proc.call_args[0][0])
        s, rec, app = self._meeting(_interruptible(hook))
        s._followup._wait = wait
        folder = self._stop(s)
        self.assertEqual(calls["n"], 2)                             # told to stop at once, ran again after
        self.assertEqual(s._followup.retries(folder), 1)
        self.assertEqual(len(rewrites), 1)
        self.assertIn(SOURCES_ONLY, rewrites[0])                    # the rewrite read one transcript, whole...
        self.assertNotIn("Speaker 1", rewrites[0])                  # ...not one the names were replacing meanwhile
        self.assertEqual((folder / "notes.md").read_text(encoding="utf-8"), "## Rewritten")
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), LABELLED)
        self.assertEqual(s.transcript.strip(), LABELLED)            # and the open meeting follows the names
        self.assertFalse((folder / ms.BEFORE_SPEAKERS).exists())
        self.assertEqual(s.speakers_state(), "")

    def test_identify_by_hand_starts_only_when_the_background_pass_has_really_stopped(self):
        started = threading.Event()

        def hook(d):
            started.set()
            end = time.time() + 10
            while not s._followup._cancel.is_set() and time.time() < end:
                time.sleep(0.005)
            time.sleep(0.25)                                        # ...and it takes a moment to let go
        s, rec, app = self._meeting(_interruptible(hook))
        s._followup._spawn = lambda fn: threading.Thread(target=fn, daemon=True).start()
        folder = self._stop(s)
        self.assertTrue(started.wait(5))
        self.assertEqual(s.speakers_state(str(folder)), "running")
        seen = {}

        def identify_reads(rec_, parts, **kw):
            seen["pass"] = s._followup.state(folder)                # must be over by now
            backup = folder / ms.BEFORE_SPEAKERS
            seen["backup"] = backup.read_text(encoding="utf-8") if backup.is_file() else None
            return _outcome(("You: by hand",))
        with patch.object(msess.meeting_speakers, "build_transcript", side_effect=identify_reads):
            self.assertTrue(s.identify_speakers(str(folder))[0])    # (inline: the run is over on return)
        self.assertEqual(seen, {"pass": "", "backup": SOURCES_ONLY})     # the pass was gone; Identify's own copy there
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), "You: by hand")
        self.assertFalse((folder / ms.BEFORE_SPEAKERS).exists())
        self.assertEqual(s._followup.folders(), [])

    def test_stop_speaker_pass_ends_a_waiting_pass_and_comes_back_when_it_is_gone(self):
        s, rec, app = self._meeting(_interruptible(lambda d: None))
        s._followup.poll_s = 0.01
        s._followup._spawn = lambda fn: threading.Thread(target=fn, daemon=True).start()
        s.finished.connect(lambda *a: setattr(app, "is_rec", True))          # a dictation starts as the notes land
        folder = self._stop(s)
        self.assertEqual(s.speakers_state(str(folder)), "waiting")
        self.assertTrue(s.stop_speaker_pass(str(folder), timeout=5.0))
        self.assertEqual(s.speakers_state(str(folder)), "")
        self.assertEqual(s._followup.folders(), [])
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), SOURCES_ONLY)
        self.assertTrue(s.stop_speaker_pass(str(folder), timeout=0.1))       # nothing left: true at once

    def test_continuing_the_meeting_while_its_names_wait_gives_one_pass_for_both_parts(self):
        dia = _interruptible(lambda d: None)
        s, rec, app = self._meeting(dia)
        once = []
        s.finished.connect(lambda *a: (not once) and (once.append(1), setattr(app, "is_rec", True)))
        steps = []
        second = [{"start": 2.0, "end": 6.0, "text": "Back again."},
                  {"start": 7.0, "end": 11.0, "text": "Welcome back."},
                  {"start": 16.0, "end": 36.0, "text": "Where were we."}]

        def wait(seconds):
            steps.append(s.state)
            if len(steps) == 1:                    # the dictation is over, and the person continues THIS meeting
                app.is_rec = False
                app.recorder = _SegRec(segments=second)
                self.assertTrue(s.continue_meeting(str(s.meeting_dir), audio_mode="smart_meeting")[0])
            elif len(steps) == 2:                  # ...records, and stops: its own notes first
                self.assertTrue(s.stop(""))
        s._followup._wait = wait
        folder = self._stop(s)
        self.assertEqual(steps, [msess.STATE_DONE, msess.STATE_RECORDING])      # it only looked again: never ran while it recorded
        self.assertEqual(dia.runs, 1)                                      # ONE pass, over both parts
        text = (folder / "transcript.txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(LABELLED + "\n\n— Resumed "), text)
        self.assertTrue(text.endswith("You: Back again.\nSpeaker 1: Welcome back. Where were we."), text)
        self.assertEqual([p["done"] for p in ms.load_segments(folder)["parts"]], [True, True])
        self.assertFalse((folder / ms.BEFORE_SPEAKERS).exists())
        self.assertEqual(s.speakers_state(), "")
        self.assertEqual(s.transcript.strip(), text.strip())               # the open meeting follows

    def test_a_continued_meeting_whose_first_names_never_came_gets_both_parts_named_together(self):
        calls = {"n": 0}

        def hook(d):
            calls["n"] += 1
            if calls["n"] == 1:
                s.cancel_speaker_followup(s.meeting_dir)                    # Cancel on the banner, the first time
        dia = _interruptible(hook)
        s, rec, app = self._meeting(dia)
        folder = self._stop(s)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), SOURCES_ONLY)
        self.assertFalse(ms.load_segments(folder)["parts"][0]["done"])
        second = _SegRec(segments=[{"start": 2.0, "end": 6.0, "text": "Back again."},
                                   {"start": 7.0, "end": 11.0, "text": "Welcome back."},
                                   {"start": 16.0, "end": 36.0, "text": "Where were we."}])
        s2 = self.session(_App(second, meeting_audio_mode="smart_meeting"))
        s2._followup.poll_s, s2._followup.settle_polls = 0.0, 0
        with patch.object(msess.actions, "process", return_value="## Notes 2"):
            self.assertTrue(s2.continue_meeting(str(folder), audio_mode="smart_meeting")[0])
            self.assertTrue(s2.stop(""))
        text = (folder / "transcript.txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(LABELLED + "\n\n— Resumed "), text)       # the first part's names came too
        self.assertTrue(text.endswith("You: Back again.\nSpeaker 1: Welcome back. Where were we."), text)
        self.assertEqual([p["done"] for p in ms.load_segments(folder)["parts"]], [True, True])
        self.assertEqual(dia.runs, 2)                                              # the cancelled one + one for both

    def test_privacy_mode_switched_on_while_the_names_wait_is_obeyed_when_they_start(self):
        from tests.test_meeting_speakers import FakeDiarizer
        dia = FakeDiarizer(downloaded=False)                       # the speaker model is not on this computer yet
        s, rec, app = self._meeting(dia)
        once = []
        s.finished.connect(lambda *a: (not once) and (once.append(1), setattr(app, "is_rec", True)))

        def wait(seconds):
            app.is_rec = False
            app.cfg["privacy_mode"] = True                         # Privacy mode goes on while it waits
        s._followup._wait = wait
        folder = self._stop(s)
        self.assertEqual(dia.downloads, 0)                         # nothing was fetched from the internet
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), SOURCES_ONLY)
        notices = ms.load_meta(folder)["notices"]
        self.assertEqual(len(notices), 1)
        self.assertIn("Privacy mode keeps Gusan offline", notices[0])
        self.assertNotIn("speakers_at", ms.load_meta(folder))      # not concluded: Identify speakers stays offered

    def test_segments_that_cannot_be_saved_leave_you_and_them_and_queue_nothing(self):
        from tests.test_meeting_speakers import Probe
        ran = []
        s, rec, app = self._meeting(Probe(lambda d: ran.append(1)))
        with patch.object(msess.meeting_store, "write_segments", return_value=False):
            folder = self._stop(s)
        self.assertEqual(ran, [])                                                 # no names: nothing to read them from
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), SOURCES_ONLY)
        self.assertEqual((folder / "notes.md").read_text(encoding="utf-8"), "## Notes\n- Ship Friday")
        self.assertFalse((folder / "segments.json").exists())
        self.assertEqual(s.speakers_state(), "")
        self.assertEqual(s.phase, "done")
        meta = ms.load_meta(folder)
        self.assertNotIn("speakers_at", meta)                                     # so the page keeps offering Identify speakers

    def test_a_meeting_renamed_while_its_names_are_worked_out_keeps_its_new_name(self):
        def hook(d):
            self.assertTrue(ms.set_title(s.stopped_folder, "Renamed on the page"))
        s, rec, app = self._meeting(_interruptible(hook))
        folder = self._stop(s)
        meta = ms.load_meta(folder)
        self.assertEqual(meta["title"], "Renamed on the page")                    # the pass only writes its own keys
        self.assertIn("speakers_at", meta)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), LABELLED)

    def test_a_meeting_deleted_while_its_names_are_worked_out_is_not_finished_or_recreated(self):
        deleted = {}

        def hook(d):
            deleted["ok"] = ms.delete_meeting(s.stopped_folder)
        s, rec, app = self._meeting(_interruptible(hook))
        folder = self._stop(s)
        self.assertTrue(deleted["ok"])
        self.assertFalse(folder.exists())                           # not re-created by the pass's writes
        self.assertEqual(s._followup.retries(folder), 0)            # and not retried: it is gone
        self.assertEqual(s.speakers_state(), "")
        self.assertEqual(s.phase, "done")

    # ── what is no problem is no warning, and no notice ──
    def test_a_recording_too_short_to_label_is_info_and_no_notice(self):
        rec = _SpeakerRec()
        rec.get_full_audio = lambda: None
        with self.assertLogs("transcribe", level="INFO") as cm:
            s = self._stopped(rec)
        skipped = [r for r in cm.records if "Speaker labels skipped" in r.getMessage()]
        self.assertEqual([r.levelno for r in skipped], [20])        # INFO, not WARNING
        self.assertEqual(s.notices(), [])
        self.assertFalse([r for r in cm.records if r.levelno >= 30 and "Speaker" in r.getMessage()])
        self.assertEqual(s.phase, "done")

    def test_the_other_modes_keep_the_whole_pass_at_stop(self):
        for mode, tracks in (("default_mic", True), ("smart_meeting", False), ("system_only", False)):
            rec = _SpeakerRec()
            rec.with_tracks = tracks
            s = self._stopped(rec, mode=mode)
            kw = s._build_calls[0][1]
            parts = s._build_calls[0][0]
            if mode == "system_only":
                self.assertFalse(kw["diarize"])                     # the call alone: names after the notes
                self.assertTrue(parts[0].call_only)
            else:
                self.assertTrue(kw["diarize"], mode)                # unchanged: Speaker N before the notes
                self.assertFalse((s.meeting_dir / "segments.json").exists(), mode)
            self.assertEqual(s.speakers_state(), "")
        # a mic + system sound meeting with its tracks is the one that goes notes first
        s = self._stopped(_SpeakerRec(), mode="smart_meeting")
        self.assertFalse(s._build_calls[0][1]["diarize"])

    def test_without_the_recordings_files_the_whole_pass_stays_at_stop(self):
        rec = _SpeakerRec()
        with patch.object(msess.MeetingCore, "_save_recording_tracks", return_value=False):
            s = self._stopped(rec, mode="smart_meeting")
        self.assertTrue(s._build_calls[0][1]["diarize"])            # the names could not follow: do it now


# ── Gusan 2.0.1: what transcribes a Live Assistant session ───────────────────────────

class TestLiveSessionStart(_Base):
    """A session started from the Live Assistant transcribes with ITS choice (cfg
    live_assist_speech_model); every other source with Dictation's model, as before."""

    def _start(self, source, pro=True, **cfg):
        app = _App(pro=pro, **cfg)
        app.prewarmed = []
        app.prewarm_speech_model = lambda **kw: app.prewarmed.append(kw)
        s = self.session(app)
        self.assertTrue(s.start("Call", language=cfg.get("live_assist_language", "hy"), source=source)[0])
        return app, s

    def test_automatic_armenian_on_a_pro_plan_goes_to_gusan_cloud(self):
        app, s = self._start("live_assist", backend="local")
        self.assertEqual(app.recorder.started, {"capture_mode": "default_mic", "language": "hy",
                                                "speech": ("managed", None, "fast")})
        self.assertEqual(app.prewarmed, [{"speech": ("managed", None, "fast")}])     # nothing local is loaded
        self.assertEqual(s._session_speech, ("managed", None, "fast"))

    def test_the_other_picks(self):
        for pick, want in (("cloud:best", ("managed", None, "best")),
                           ("small", ("local", "small", None)),
                           ("own:google:gemini-2.5-pro", ("google", None, "gemini-2.5-pro"))):
            app, _s = self._start("live_assist", live_assist_speech_model=pick, google_api_key="k",
                                  live_assist_language="en")
            self.assertEqual(app.recorder.started["speech"], want, pick)
            self.assertEqual(app.prewarmed, [{"speech": want}], pick)

    def test_free_armenian_and_other_languages_start_exactly_as_before(self):
        for pro, lang in ((False, "hy"), (True, "en"), (False, "en")):
            app, s = self._start("live_assist", pro=pro, live_assist_language=lang, backend="local")
            self.assertEqual(app.recorder.started, {"capture_mode": "default_mic", "language": lang})
            self.assertEqual(app.prewarmed, [{}])                    # the plain prewarm, no speech
            self.assertIsNone(s._session_speech)

    def test_meetings_and_the_calendar_keep_dictations_model(self):
        for source in ("page", "calendar", "classic"):
            app, s = self._start(source, live_assist_speech_model="cloud:best", backend="local")
            self.assertEqual(app.recorder.started, {"capture_mode": "default_mic", "language": "hy"}, source)
            self.assertNotIn("speech", app.recorder.started)
            self.assertEqual(app.prewarmed, [{}], source)
            self.assertIsNone(s._session_speech, source)

    def test_a_live_session_the_recorder_tagged_by_fast_pieces_counts_as_the_live_assistant(self):
        app = _App(pro=True, backend="local", live_assist_language="hy")
        app.recorder.fast_live_chunks = True
        s = self.session(app)
        s.start("Live session 10:00", language="hy")                  # source None -> live_assist
        self.assertEqual(app.recorder.started["speech"], ("managed", None, "fast"))

    def test_a_problem_in_the_choice_is_dictations_model_not_a_refused_start(self):
        app = _App(pro=True, backend="local")
        s = self.session(app)
        with patch("engine_choice.live_speech_session", side_effect=RuntimeError("boom")):
            ok, code, _msg = s.start("Call", language="hy", source="live_assist")
        self.assertEqual((ok, code), (True, ""))
        self.assertEqual(app.recorder.started, {"capture_mode": "default_mic", "language": "hy"})

    def test_an_app_whose_prewarm_takes_no_argument_still_starts_a_live_session(self):
        app = _App(pro=True, backend="local")
        calls = []
        app.prewarm_speech_model = lambda: calls.append(1)           # an older controller
        s = self.session(app)
        self.assertTrue(s.start("Call", language="hy", source="live_assist")[0])
        self.assertEqual(s.phase, "recording")                       # (the failed call is never fatal)

    def test_the_session_says_where_it_came_from(self):
        s = self.session(_App())
        s.start("Call", source="live_assist")
        self.assertEqual(s.source, "live_assist")


class _CloudRec(_SegRec):
    """A Live Assistant session whose words came from a cloud engine: the recorder reports its
    backend and hands over the live pieces with their own timestamps; reading the recording
    again (local Whisper) is counted - and would give a different, worse text."""

    PIECES = [{"start": 2.0, "end": 6.0, "text": "Բարև ձեզ"},
              {"start": 7.0, "end": 11.0, "text": "Ինչպե՞ս եք"},
              {"start": 12.0, "end": 15.0, "text": "Շնորհակալություն"},
              {"start": 16.0, "end": 36.0, "text": "Եկեք սկսենք աշխատանքը"}]
    PLAIN = "Բարև ձեզ Ինչպե՞ս եք Շնորհակալություն Եկեք սկսենք աշխատանքը"

    def __init__(self, backend="managed", pieces="default", whisper_model="base", cloud_pieces=None,
                 **kw):
        kw.setdefault("result", (self.PLAIN, "hy"))
        super().__init__(segments=[{"start": 2.0, "end": 36.0, "text": "bad local whisper text"}], **kw)
        self.backend = backend
        self.pieces = [dict(p) for p in self.PIECES] if pieces == "default" else pieces
        self.whisper_model = whisper_model
        # Pieces the cloud answered: all of them on a cloud backend, none on a local one - unless
        # Privacy mode moved the rest of the take onto this computer part-way (backend local, 2).
        if cloud_pieces is None:
            cloud_pieces = 4 if backend in ("managed", "google", "mistral") else 0
        self.cloud_pieces = cloud_pieces
        self.models = []

    def speech_report(self):
        return {"backend": self.backend, "whisper_model": self.whisper_model, "language": "hy",
                "cloud_model": "" if self.backend == "managed" else None,
                "ran_model": "gemini-2.5-flash" if self.cloud_pieces else "",
                "cloud_pieces": self.cloud_pieces, "local_pieces": 0}

    def piece_segments(self):
        return None if self.pieces is None else [dict(p) for p in self.pieces]

    def transcribe_segments(self, audio, language=None, model_name=None, on_progress=None,
                            should_cancel=None):
        self.models.append(model_name)
        return super().transcribe_segments(audio, language, model_name, on_progress, should_cancel)


YOU_THEM = "You: Բարև ձեզ\nThem: Ինչպե՞ս եք\nYou: Շնորհակալություն\nThem: Եկեք սկսենք աշխատանքը"
YOU_SPEAKER = "You: Բարև ձեզ\nSpeaker 1: Ինչպե՞ս եք\nYou: Շնորհակալություն\nSpeaker 1: Եկեք սկսենք աշխատանքը"


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestFinalRereadOfALiveSession(_SpeakerBase):
    """DECISION (meeting_session._reread_plan): the re-read is always local Whisper and replaces
    the plain transcript, so a cloud session is not read again - its pieces are the segments."""

    def _run(self, rec, source="live_assist", mode="smart_meeting", speech_cfg=None, **app_kw):
        from tests.test_meeting_speakers import FakeDiarizer
        conf = {"meeting_audio_mode": mode, "backend": "local", "live_assist_language": "hy"}
        conf.update(speech_cfg or {})
        conf.update(app_kw)
        app = _App(rec, **conf)
        s = self.session(app)
        s._followup.poll_s = 0.0
        s._followup.settle_polls = 0
        notes_in = []
        p = patch.object(msess.meeting_speakers, "diarization", FakeDiarizer())
        p.start()
        self.addCleanup(p.stop)
        with patch.object(msess.actions, "process",
                          side_effect=lambda text, action, **kw: notes_in.append(text) or "## Notes") as proc:
            self.assertTrue(s.start("Call", "Anna", audio_mode=mode, source=source,
                                    language=conf["live_assist_language"])[0])
            s._on_chunk_transcribed(0, "live piece", "hy")
            self.assertTrue(s.stop(""))
        folder = s.meeting_dir
        s._notes_in, s._proc = notes_in, proc
        s._text = (folder / "transcript.txt").read_text(encoding="utf-8")
        return s, folder, app

    def test_a_cloud_session_keeps_the_clouds_words_and_never_calls_local_whisper(self):
        rec = _CloudRec()
        s, folder, _app = self._run(rec)
        self.assertEqual(rec.reads, 0)                               # transcribe_segments was never called
        self.assertEqual(rec.models, [])
        self.assertNotIn("bad local whisper", s._text)
        self.assertEqual(s._text, YOU_SPEAKER)                       # You, and the call's voice by name after
        self.assertIn(YOU_THEM, s._notes_in[0])                      # the notes were written from the cloud's words
        part = ms.load_segments(folder)["parts"][0]
        self.assertEqual(part["kind"], "sources")
        self.assertEqual(part["segments"][1], {"s": 7.0, "e": 11.0, "t": "Ինչպե՞ս եք", "src": "them"})
        self.assertEqual(ms.load_meta(folder)["transcribed_with"], "cloud")      # said where the words came from
        self.assertEqual(ms.load_meta(folder)["notices"], [])

    def test_every_cloud_backend_counts(self):
        for backend in ("managed", "google", "mistral"):
            rec = _CloudRec(backend=backend)
            s, _folder, _app = self._run(rec)
            self.assertEqual(rec.reads, 0, backend)
            self.assertEqual(s._text, YOU_SPEAKER, backend)

    def test_a_local_choice_is_read_again_with_the_sessions_own_whisper_model(self):
        rec = _CloudRec(backend="local", whisper_model="small")
        s, _folder, _app = self._run(rec, speech_cfg={"live_assist_speech_model": "small"})
        self.assertEqual(rec.reads, 1)
        self.assertEqual(rec.models, ["small"])                      # not Dictation's "base"
        self.assertIn("bad local whisper text", s._text)             # the re-read's words, as always

    def test_automatic_on_a_local_dictation_is_read_again_with_dictations_model(self):
        rec = _CloudRec(backend="local")
        s, _folder, _app = self._run(rec, live_assist_language="en")      # nothing moved it to the cloud
        self.assertEqual((rec.reads, rec.models), (1, [None]))
        self.assertIn("bad local whisper text", s._text)
        self.assertNotIn("transcribed_with", ms.load_meta(s.meeting_dir))

    def test_meetings_in_a_language_whisper_is_poor_at_keep_the_words_the_cloud_made(self):
        # A Meetings or calendar session on a cloud Dictation backend, in Armenian: the local Whisper
        # re-read would REPLACE the cloud's good words with its own poor ones, so - like a Live
        # Assistant session - it is not read again: the live pieces are the segments.
        for source in ("page", "calendar", "classic"):
            rec = _CloudRec(backend="managed")
            s, folder, _app = self._run(rec, source=source)          # (the call's language is Armenian)
            self.assertEqual((rec.reads, rec.models), (0, []), source)
            self.assertNotIn("bad local whisper text", s._text, source)
            self.assertEqual(s._text, YOU_SPEAKER, source)
            self.assertEqual(ms.load_meta(folder)["transcribed_with"], "cloud", source)   # later steps must know

    def test_meetings_in_any_other_language_are_unchanged(self):
        # The same recorder report, but a language local Whisper reads well: the re-read still runs (and
        # still replaces the words) exactly as it did before. Only Armenian moved.
        for source in ("page", "calendar"):
            rec = _CloudRec(backend="managed")
            s, _folder, _app = self._run(rec, source=source, speech_cfg={"live_assist_language": "en"})
            self.assertEqual((rec.reads, rec.models), (1, [None]), source)
            self.assertIn("bad local whisper text", s._text, source)
            self.assertNotIn("transcribed_with", ms.load_meta(s.meeting_dir), source)

    def test_a_meeting_that_made_its_words_with_whisper_here_is_read_again_in_armenian_too(self):
        # (nothing good to keep: the words were Whisper's, the re-read is Whisper's again)
        rec = _CloudRec(backend="local")
        s, _folder, _app = self._run(rec, source="page")
        self.assertEqual((rec.reads, rec.models), (1, [None]))
        self.assertNotIn("transcribed_with", ms.load_meta(s.meeting_dir))

    def test_the_armenian_model_reads_the_recording_again_with_its_own_segments(self):
        # A Live Assistant session on the Armenian model on this computer: the re-read is the model's own
        # (AudioRecorder.transcribe_segments takes its id: the recorder reads Armenian with it)
        rec = _CloudRec(backend="local", whisper_model="hy-fastconformer")
        s, _folder, _app = self._run(rec, speech_cfg={"live_assist_speech_model": "hy-fastconformer"})
        self.assertEqual(rec.reads, 1)
        self.assertEqual(rec.models, ["hy-fastconformer"])
        self.assertNotIn("transcribed_with", ms.load_meta(s.meeting_dir))
        # A Meetings session whose Dictation model is the Armenian one: Dictation's model, as ever
        rec = _CloudRec(backend="local", whisper_model="hy-fastconformer")
        s, _folder, _app = self._run(rec, source="page", speech_cfg={"whisper_model": "hy-fastconformer"})
        self.assertEqual((rec.reads, rec.models), (1, [None]))

    def test_pieces_that_cannot_be_placed_keep_the_plain_transcript_never_local_whisper(self):
        rec = _CloudRec(pieces=None)
        with self.assertLogs("transcribe", level="INFO") as cm:
            s, _folder, _app = self._run(rec)
        self.assertEqual(rec.reads, 0)
        self.assertEqual(s._text, _CloudRec.PLAIN)
        self.assertTrue([r for r in cm.records if "Speaker labels skipped" in r.getMessage()])
        self.assertFalse((s.meeting_dir / "segments.json").exists())
        self.assertEqual(s.notices(), [])                            # no "try Identify speakers" (it would re-read)
        self.assertEqual(s.phase, "done")
        self.assertEqual(ms.load_meta(s.meeting_dir)["transcribed_with"], "cloud")   # (later steps must know)

    def test_privacy_mode_part_way_does_not_turn_the_clouds_words_into_a_local_reread(self):
        # The cloud answered two pieces, then Privacy mode moved the rest of the take onto this
        # computer: the recorder's backend reads "local" by Stop, but those words are the cloud's.
        rec = _CloudRec(backend="local", cloud_pieces=2)
        s, folder, _app = self._run(rec)
        self.assertEqual(rec.reads, 0)
        self.assertEqual(s._text, YOU_SPEAKER)
        self.assertEqual(ms.load_meta(folder)["transcribed_with"], "cloud")

    def test_a_piece_that_failed_stays_missing_and_the_transcript_says_so(self):
        rec = _CloudRec(result=("", "!managed:Session expired - sign in again."))
        rec.partial_text = _CloudRec.PLAIN
        s, _folder, _app = self._run(rec)
        self.assertEqual(rec.reads, 0)
        self.assertIn("Part of the audio couldn't be transcribed", s._text)
        self.assertIn("Բարև ձեզ", s._text)
        self.assertTrue(any("Part of the audio couldn't be transcribed" in n for n in s.notices()))

    def test_a_recorder_without_the_new_methods_is_read_again_as_before(self):
        rec = _SegRec()                                              # (an older recorder / a test double)
        s, _folder, _app = self._run(rec)
        self.assertEqual(rec.reads, 1)
        self.assertEqual(s._text.split("\n")[0], "You: Hello team.")


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestACloudSessionIsNeverReadAgainLater(_SpeakerBase):
    """meta.json says where a Live Assistant session's words came from (transcribed_with "cloud"):
    Identify speakers and the recovery of an interrupted one must not read the recording again
    with local Whisper - that would replace the cloud's words (for Armenian: with a far worse text)."""

    def _stopped(self, rec=None, inline=False, **cfg):
        from tests.test_meeting_speakers import FakeDiarizer
        rec = rec or _CloudRec()
        app = _App(rec, meeting_audio_mode="smart_meeting", backend="local", live_assist_language="hy", **cfg)
        s = self.session(app, inline=inline)
        s._followup.poll_s = 0.0
        s._followup.settle_polls = 0
        p = patch.object(msess.meeting_speakers, "diarization", FakeDiarizer())
        p.start()
        self.addCleanup(p.stop)
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s.start("Call", "Anna", audio_mode="smart_meeting", source="live_assist",
                                    language="hy")[0])
            self.mark_at_start = ms.load_meta(s.meeting_dir).get("transcribed_with")
            s._on_chunk_transcribed(0, "live piece", "hy")
            self.assertTrue(s.stop(""))
            if not inline:
                target, args = s.spawned.pop(0)               # Stop's worker; the names are only queued
                target(*args)
        return s, rec, s.meeting_dir

    def _identify(self, s, folder):
        errors = _Recorder(s.error)
        self.assertTrue(s.identify_speakers(folder)[0])
        for target, args in [t for t in s.spawned if t[0].__name__ == "_run_identify"]:
            target(*args)
        return errors

    def test_the_mark_is_written_when_the_session_starts_not_only_at_stop(self):
        s, rec, folder = self._stopped()
        self.assertEqual(self.mark_at_start, "cloud")                # a crash in the middle would keep it
        s2, rec2, folder2 = self._stopped(_CloudRec(backend="local"))
        self.assertIsNone(self.mark_at_start)                        # a model on this computer: no mark
        self.assertNotIn("transcribed_with", ms.load_meta(folder2))
        # A Meetings session on a cloud Dictation backend carries it too in a language local Whisper is
        # poor at (Armenian) - and none in any other.
        app = _App(_CloudRec(), meeting_audio_mode="smart_meeting")
        page = self.session(app)
        self.assertTrue(page.start("Call", audio_mode="smart_meeting", source="page", language="hy")[0])
        self.assertEqual(ms.load_meta(page.meeting_dir)["transcribed_with"], "cloud")
        app = _App(_CloudRec(), meeting_audio_mode="smart_meeting")
        page = self.session(app)
        self.assertTrue(page.start("Call", audio_mode="smart_meeting", source="page", language="en")[0])
        self.assertNotIn("transcribed_with", ms.load_meta(page.meeting_dir))

    def test_identify_speakers_names_the_call_from_the_kept_segments_and_never_reads_the_recording(self):
        s, rec, folder = self._stopped()
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), YOU_THEM)
        self.assertEqual(s.speakers_state(str(folder)), "waiting")
        errors = self._identify(s, folder)
        self.assertEqual(rec.reads, 0)                               # the recording was never read again
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), YOU_SPEAKER)
        self.assertIn("speakers_at", ms.load_meta(folder))
        self.assertEqual(errors.calls, [])
        self.assertEqual((s.phase, s.last_run_speakers), ("done", True))
        self.assertFalse((folder / ms.BEFORE_SPEAKERS).exists())
        self.assertEqual(s._final_transcript, YOU_SPEAKER)

    def test_identify_speakers_with_nothing_left_to_name_keeps_the_words_and_says_why(self):
        s, rec, folder = self._stopped(inline=True)                  # the names came at Stop already
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), YOU_SPEAKER)
        errors = self._identify(s, folder)
        self.assertEqual(rec.reads, 0)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), YOU_SPEAKER)
        self.assertEqual(errors.calls, [(msess.ERR_SPEAKERS, msess.CLOUD_WORDS_KEPT)])
        self.assertIn("transcribed in the cloud", msess.CLOUD_WORDS_KEPT)
        self.assertIn(msess.CLOUD_WORDS_KEPT, ms.load_meta(folder)["notices"])    # where the page shows it
        self.assertEqual(ms.load_meta(folder)["notices"].count(msess.CLOUD_WORDS_KEPT), 1)
        self.assertFalse((folder / ms.BEFORE_SPEAKERS).exists())
        self.assertEqual(s.phase, "done")

    def test_a_meeting_without_the_mark_is_still_read_again(self):
        # (the same flow for a meeting whose words are this computer's: nothing changed)
        s, rec, folder = self._stopped(_CloudRec(backend="local"), inline=True)
        self.assertEqual(rec.reads, 1)                               # the re-read at Stop
        self._identify(s, folder)
        self.assertEqual(rec.reads, 2)                               # Identify speakers reads it again, as always

    def test_a_session_the_armenian_model_read_is_read_again_by_it_never_by_dictations_whisper(self):
        # The Live Assistant picked the Armenian model; Dictation runs a Whisper model. Stop's re-read
        # uses the session's model - and so must Identify speakers later, or Whisper's far worse Armenian
        # would replace the model's words.
        rec = _CloudRec(backend="local", whisper_model="hy-fastconformer")
        s, rec, folder = self._stopped(rec, inline=True, live_assist_speech_model="hy-fastconformer",
                                       whisper_model="small")
        meta = ms.load_meta(folder)
        self.assertEqual(meta["speech_model"], "hy-fastconformer")          # written when the session starts
        self.assertNotIn("transcribed_with", meta)                          # (these are not the cloud's words)
        self.assertEqual(rec.models, ["hy-fastconformer"])                  # Stop's re-read
        self._identify(s, folder)
        self.assertEqual(rec.models, ["hy-fastconformer", "hy-fastconformer"])

    def test_a_session_on_a_whisper_model_writes_no_speech_model_and_is_read_as_before(self):
        rec = _CloudRec(backend="local", whisper_model="small")
        s, rec, folder = self._stopped(rec, inline=True)
        self.assertNotIn("speech_model", ms.load_meta(folder))
        self._identify(s, folder)
        self.assertEqual(rec.models, [None, None])                          # Dictation's model, as always

    def test_a_cloud_take_never_writes_it_even_when_the_pick_is_the_armenian_model(self):
        rec = _CloudRec(backend="managed", whisper_model="hy-fastconformer")
        _s, _rec, folder = self._stopped(rec)
        self.assertNotIn("speech_model", ms.load_meta(folder))
        self.assertEqual(ms.load_meta(folder)["transcribed_with"], "cloud")

    def test_a_cancelled_run_on_a_cloud_meeting_changes_nothing(self):
        s, rec, folder = self._stopped()
        s._cancel_run.set()
        s.identify_speakers(folder)                                  # (identify clears the flag: cancel from inside)
        errors = _Recorder(s.error)
        original = msess.meeting_speakers.refine_meeting

        def cancelled(folder, **kw):
            s._cancel_run.set()
            return original(folder, **kw)
        with patch.object(msess.meeting_speakers, "refine_meeting", side_effect=cancelled):
            for target, args in [t for t in s.spawned if t[0].__name__ == "_run_identify"]:
                target(*args)
        self.assertEqual((folder / "transcript.txt").read_text(encoding="utf-8"), YOU_THEM)
        self.assertEqual(rec.reads, 0)
        self.assertEqual(errors.calls, [])


class TestRecoveringACloudSession(_SavedMeeting):
    """An interrupted Live Assistant session (no transcript.txt, only its live pieces and the saved
    recording) whose words came from a cloud engine is finished from those pieces."""

    def test_the_live_pieces_are_the_transcript_and_the_recording_is_not_read_again(self):
        d = self._folder(transcribed_with="cloud", language="hy")
        s = self.session(_App())
        calls = self._build(s, _outcome(("You: bad local text",)))
        with patch.object(msess.actions, "process", return_value="## Notes") as proc:
            self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual(calls, [])                                  # no local re-read
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "live one live two")
        self.assertIn("live one live two", proc.call_args[0][0])
        notices = ms.load_meta(d)["notices"]
        self.assertIn(msess.RECOVERED_CLOUD_NOTICE, notices)
        self.assertNotIn(msess.RECOVERED_NOTICE, notices)
        self.assertEqual(s.phase, "done")

    def test_an_interrupted_armenian_model_session_is_rebuilt_with_that_model(self):
        # (the recovery must not hand an Armenian recording to Dictation's Whisper)
        d = self._folder(speech_model="hy-fastconformer", language="hy")
        s = self.session(_App())
        calls = self._build(s, _outcome(("You: rebuilt",)))
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1].get("model"), "hy-fastconformer")

    def test_a_saved_model_that_is_not_the_armenian_one_is_ignored(self):
        d = self._folder(speech_model="large-v3")                    # (a made-up or hand-edited meta.json)
        s = self.session(_App())
        calls = self._build(s, _outcome(("You: rebuilt",)))
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertNotIn("model", calls[0][1])

    def test_an_interrupted_session_on_this_computer_is_rebuilt_as_before(self):
        d = self._folder()                                           # (source live_assist, no mark)
        s = self.session(_App())
        calls = self._build(s, _outcome(("You: rebuilt",)))
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual(len(calls), 1)
        self.assertEqual((d / "transcript.txt").read_text(encoding="utf-8"), "You: rebuilt")
        self.assertIn(msess.RECOVERED_NOTICE, ms.load_meta(d)["notices"])


if __name__ == "__main__":
    unittest.main()
