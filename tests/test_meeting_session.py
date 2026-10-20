"""Gusan 2.0 meetings: the session without widgets (pause, two meters, live
lines, My notes, start refusals, outcome and metadata, Retry / Rewrite /
Continue), the meeting library additions, and the recorder's pause and
per-source levels on scripted audio.

Runs the Qt-free MeetingEngine - the same code as the app's MeetingSession -
so it works under tests/test_core.py's suite-wide PySide6 / numpy stubs."""
import json
import tempfile
import threading
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
        return s


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


if __name__ == "__main__":
    unittest.main()
