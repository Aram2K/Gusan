"""A meeting is saved while it is recorded, and a meeting the app never finished is recovered
(meeting_spool.py, meeting_session.recover_interrupted - Gusan 2.1.11). The incremental WAVs are valid
after every flush; a crash (the files left open, a header from an earlier flush, a half sample) still
gives a usable recording; Stop removes the copy, a failed save keeps it; at the next start the meeting
gets its recording, its live pieces and its notes."""
import json
import os
import tempfile
import threading
import types
import unittest
import wave
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

import meeting_session as msess
import meeting_spool as msp
import meeting_store as ms


def _np():
    try:
        import numpy as np
        if np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,):
            return np
    except Exception:
        pass
    return None


np = _np()
needs_numpy = unittest.skipUnless(np is not None, "real numpy not importable (stubbed)")


def _crash(spool):
    """The process died: the files are let go of without the final flush or header."""
    for f in spool._files.values():
        try:
            f._f.close()
        except Exception:
            pass
    spool._done.set()
    msp._ACTIVE.discard(msp._key(spool.folder))


def _frames(path):
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        return w.getnframes(), np.frombuffer(w.readframes(w.getnframes()), np.int16)


class _Dir(unittest.TestCase):
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

    def folder(self, name="20261121_101500", **meta):
        d = self.root / name
        d.mkdir()
        m = {"title": "Interview", "started": "2026-11-21 10:15:00", "duration_sec": 0, "source": "page"}
        m.update(meta)
        (d / "meta.json").write_text(json.dumps(m), encoding="utf-8")
        return d

    def spool(self, folder, part="audio_part1.wav", two=True, seconds=2.0, pieces=(), prior=0):
        """A spool that wrote ``seconds`` of a mic + call meeting, then the app died."""
        s = msp.MeetingSpool(folder, folder / part, prior_sec=prior, thread=False)
        self.assertTrue(s.begin(two_tracks=two, mode="smart_meeting" if two else "default_mic"))
        n = int(seconds * 16000) // 1024
        for i in range(n):
            mic = np.full(1024, 0.2, np.float32)
            s.add(np.full(1024, 0.3, np.float32), mic if two else None, np.full(600, 0.25, np.float32), 600)
            if i % 20 == 19:
                s.flush()
        for idx, (a, b, text) in enumerate(pieces):
            s.add_piece(idx, a, b, text, "en", 2.0, 0.1)
        s.flush()
        _crash(s)
        return s


@needs_numpy
class TestSpoolFiles(_Dir):
    def test_every_flush_leaves_valid_wavs_that_grow(self):
        d = self.folder()
        s = msp.MeetingSpool(d, d / "audio_part1.wav", thread=False)
        s.begin(two_tracks=True)
        sizes = []
        for _ in range(3):
            for _ in range(10):
                s.add(np.full(1024, 0.3, np.float32), np.full(1024, 0.2, np.float32),
                      np.full(1024, 0.25, np.float32), 512)
            s.flush()
            n_mix, _a = _frames(s.paths["mix"])
            n_mic, mic = _frames(s.paths["mic"])
            n_sys, sysd = _frames(s.paths["system"])
            self.assertEqual(n_mix, n_mic)
            self.assertEqual(n_mix, n_sys)
            sizes.append(n_mix)
        self.assertEqual(sizes, [10240, 20480, 30720])
        # the call's track is silence beyond the samples that were mixed in (as recorder_tracks keeps it)
        self.assertTrue((sysd[:512] == round(0.25 * 32767)).all() and (sysd[512:1024] == 0).all())
        self.assertTrue((mic == round(0.2 * 32767)).all())
        s.discard()
        self.assertEqual([p for p in d.iterdir() if ".live." in p.name or p.name.endswith(".jsonl")], [])
        self.assertFalse(msp.is_active(d))

    def test_the_writer_thread_flushes_on_its_own_and_close_writes_the_rest(self):
        d = self.folder()
        s = msp.MeetingSpool(d, d / "audio_part1.wav", flush_s=0.05)
        s.begin(two_tracks=False)
        self.assertTrue(msp.is_active(d))
        s.add(np.full(1600, 0.1, np.float32))
        deadline = __import__("time").time() + 5
        while s.frames < 1600 and __import__("time").time() < deadline:
            __import__("time").sleep(0.01)
        self.assertEqual(_frames(s.paths["mix"])[0], 1600)       # on disk while still recording
        s.add(np.full(800, 0.1, np.float32))
        s.close()
        self.assertEqual(_frames(s.paths["mix"])[0], 2400)
        s.add(np.full(800, 0.1, np.float32))                     # after the end: nothing
        self.assertEqual(_frames(s.paths["mix"])[0], 2400)
        s.discard()

    def test_a_crash_mid_flush_still_gives_a_usable_recording(self):
        d = self.folder()
        s = msp.MeetingSpool(d, d / "audio_part1.wav", thread=False)
        s.begin(two_tracks=True)
        for _ in range(20):
            s.add(np.full(1024, 0.3, np.float32), np.full(1024, 0.2, np.float32), None, 0)
        s.flush()
        # the next flush got half way: samples written, the header not made right, half a sample
        s._files["mix"]._f.write(b"\x10\x00" * 300 + b"\x07")
        s._files["mic"]._f.write(b"\x10\x00" * 100)
        _crash(s)
        part = msp.recover_part(d / "audio_part1.wav")
        self.assertEqual(part, d / "audio_part1.wav")
        n, mix = _frames(part)
        n_mic, _m = _frames(ms.track_path(part, "mic"))
        n_sys, _s = _frames(ms.track_path(part, "system"))
        self.assertEqual((n, n_mic, n_sys), (20480, 20480, 20480))     # the same length, whole samples
        self.assertTrue((mix == round(0.3 * 32767)).all())
        self.assertEqual(msp.leftover_parts(d), [])
        import audio_export                                            # the app's own readers take it
        audio, rate = audio_export.read_wav_float32(part)
        self.assertEqual((len(audio), rate), (20480, 16000))

    def test_a_part_saved_at_stop_is_kept_and_a_shorter_one_replaced(self):
        d = self.folder()
        self.spool(d, seconds=2.0)
        import audio_export
        audio_export.write_wav(d / "audio_part1.wav", np.zeros(16000 * 3, np.float32))   # Stop saved more
        self.assertEqual(msp.recover_part(d / "audio_part1.wav"), d / "audio_part1.wav")
        self.assertEqual(_frames(d / "audio_part1.wav")[0], 48000)
        self.assertEqual(msp.leftover_parts(d), [])
        e = self.folder("20261121_111500")
        self.spool(e, seconds=2.0)
        audio_export.write_wav(e / "audio_part1.wav", np.zeros(1600, np.float32))       # a save cut short
        msp.recover_part(e / "audio_part1.wav")
        self.assertGreater(_frames(e / "audio_part1.wav")[0], 30000)

    def test_pieces_keep_their_place_and_a_line_cut_short_is_skipped(self):
        d = self.folder()
        self.spool(d, pieces=[(0.0, 1.5, "Hello there."), (1.5, 3.0, "How are you?")])
        with open(msp.spool_paths(d / "audio_part1.wav")["pieces"], "a", encoding="utf-8") as f:
            f.write('{"i": 2, "t": "half a li')
        rows = msp.load_pieces(d / "audio_part1.wav")
        self.assertEqual([r["t"] for r in rows], ["Hello there.", "How are you?"])
        self.assertEqual(rows[0]["mic"], 2.0)
        self.assertEqual(msp.piece_segments(d / "audio_part1.wav"),
                         [{"start": 0.0, "end": 1.5, "text": "Hello there."},
                          {"start": 1.5, "end": 3.0, "text": "How are you?"}])

    def test_a_disk_that_fails_stops_the_spool_never_the_meeting(self):
        d = self.folder()
        s = msp.MeetingSpool(d, d / "audio_part1.wav", thread=False)
        s.begin(two_tracks=False)
        s.add(np.full(1024, 0.1, np.float32))
        with patch.object(msp.WavAppender, "append", side_effect=OSError("disk full")):
            s.flush()
        self.assertTrue(s.failed)
        s.add(np.full(1024, 0.1, np.float32))                    # ignored, no error
        s.close()
        s.discard()


class _Rec:
    """The recorder surface the session touches."""

    def __init__(self, audio=None, fail_start=None):
        self.audio = audio
        self.fail_start = fail_start
        self.meeting_spool = None
        self.started = None
        self._full_audio_truncated = False
        self._full_audio_max = 16000 * 7200
        self._loopback_peak = 1.0
        self.fast_live_chunks = False
        self.partial_text = ""

    def start_recording(self, **kw):
        if self.fail_start:
            raise self.fail_start
        self.started = kw
        self.spool_at_start = self.meeting_spool
        self.meeting_spool = None                         # the take keeps it (main: _spool)

    def stop_recording(self):
        pass

    def request_abort(self):
        pass

    def transcribe(self):
        return ("Hello.", "en")

    def get_full_audio(self):
        return self.audio


class _App:
    def __init__(self, rec):
        self.cfg = {"meeting_consent_ack": True, "save_history": False, "meeting_audio_mode": "default_mic",
                    "whisper_model": "base", "action_model": "managed"}
        self.recorder = rec
        self.live_assist = None

    def save_config(self):
        pass

    def track(self, *a, **k):
        pass

    def is_pro(self):
        return True


@needs_numpy
class TestTheSessionKeepsTheCopy(_Dir):
    def session(self, rec):
        s = msess.MeetingEngine(_App(rec))
        s._spawn = lambda target, *args: None
        return s

    def test_start_hands_the_part_to_the_recorder_and_abort_removes_it(self):
        rec = _Rec()
        s = self.session(rec)
        self.assertTrue(s.start("Interview")[0])
        spool = rec.spool_at_start
        self.assertIsInstance(spool, msp.MeetingSpool)
        self.assertEqual(spool.part_path, Path(s.meeting_dir) / "audio_part1.wav")
        spool.begin(two_tracks=False)                     # (what the recorder does once its streams are open)
        spool.add(np.full(4000, 0.1, np.float32))
        s.abort()
        self.assertEqual(msp.leftover_parts(s.meeting_dir or spool.folder), [])
        self.assertFalse(msp.is_active(spool.folder))

    def test_a_start_that_fails_leaves_nothing_behind(self):
        rec = _Rec(fail_start=OSError("no device"))
        s = self.session(rec)
        self.assertFalse(s.start("Interview")[0])
        self.assertIsNone(rec.meeting_spool)
        self.assertIsNone(getattr(s, "_spool", None))

    def test_stop_saves_the_part_from_memory_and_removes_the_copy(self):
        rec = _Rec(audio=np.full(16000, 0.1, np.float32))
        s = self.session(rec)
        s.start("Interview")
        spool = rec.spool_at_start
        spool.begin(two_tracks=False)
        spool.add(np.full(16000, 0.1, np.float32))
        path = s._save_recording_segment()
        s._save_recording_tracks(path)
        s._drop_spool()
        self.assertEqual(path, Path(s.meeting_dir) / "audio_part1.wav")
        self.assertEqual(msp.leftover_parts(s.meeting_dir), [])

    def test_when_saving_from_memory_fails_the_copy_on_disk_becomes_the_part(self):
        rec = _Rec(audio=np.full(32000, 0.1, np.float32))
        s = self.session(rec)
        s.start("Interview")
        spool = rec.spool_at_start
        spool.begin(two_tracks=True)
        for _ in range(30):
            spool.add(np.full(1024, 0.3, np.float32), np.full(1024, 0.2, np.float32), None, 0)
        with patch("audio_export.write_wav", side_effect=MemoryError("no memory")):
            path = s._save_recording_segment()
        self.assertEqual(path, Path(s.meeting_dir) / "audio_part1.wav")
        self.assertEqual(_frames(path)[0], 30720)
        self.assertTrue(s._save_recording_tracks(path))       # its tracks came with it
        self.assertIsNotNone(ms.track_file(path, "mic"))
        s._drop_spool()
        self.assertTrue(path.is_file())


@needs_numpy
class TestRecoveryAtStart(_Dir):
    def test_a_new_meeting_gets_its_recording_and_its_notes_are_queued(self):
        d = self.folder()
        with open(d / "chunks.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"index": 0, "text": "Tell me about you.", "language": "en"}) + "\n")
        self.spool(d, seconds=3.0, pieces=[(0.0, 2.0, "Tell me about you.")])
        found = msess.recover_interrupted()
        self.assertEqual(found, [str(d)])
        part = d / "audio_part1.wav"
        self.assertEqual(_frames(part)[0], 46 * 1024)               # every sample the spool was handed
        self.assertIsNotNone(ms.track_file(part, "mic"))
        self.assertIsNotNone(ms.track_file(part, "system"))
        meta = ms.load_meta(d)
        self.assertIs(meta.get(msess.META_NOTES_PENDING), True)
        self.assertIs(meta.get("seen"), False)
        self.assertEqual(meta.get("duration_sec"), 3)
        self.assertIn(str(d), msess.pending_notes_folders())
        self.assertEqual(msp.leftover_parts(d), [])
        self.assertEqual(msess.recover_interrupted(), [])           # once
        # the notes run reads the recording again (no transcript.txt: an interrupted meeting)
        s = msess.MeetingEngine(_App(_Rec()))
        self.assertTrue(s._load_for_rewrite(d))
        self.assertTrue(s._recover_audio)

    def test_a_continued_meeting_gets_the_lost_parts_words_after_its_transcript(self):
        d = self.folder(duration_sec=600)
        (d / "transcript.txt").write_text("You: First part.", encoding="utf-8")
        import audio_export
        audio_export.write_wav(d / "audio_part1.wav", np.zeros(16000, np.float32))
        self.spool(d, part="audio_part2.wav", seconds=2.0, prior=600,
                   pieces=[(0.0, 1.0, "Second part,"), (1.0, 2.0, "said live.")])
        self.assertEqual(msess.recover_interrupted(), [str(d)])
        self.assertTrue((d / "audio_part2.wav").is_file())
        text = ms.load_transcript(d)
        self.assertTrue(text.startswith("You: First part.\n\n— Resumed "))
        self.assertTrue(text.endswith("Second part, said live."))
        meta = ms.load_meta(d)
        self.assertIn(msess.QUIT_NOTICE, meta.get("notices"))
        self.assertEqual(meta.get("duration_sec"), 602)                # the earlier 600 s + this part
        self.assertTrue(meta.get(msess.META_NOTES_PENDING))

    def test_a_discarded_or_active_meeting_is_left_alone(self):
        gone = self.folder("20261121_090000")
        (gone / ms.DISCARDED_CHUNKS).touch()
        self.spool(gone)
        busy = self.folder("20261121_093000")
        self.spool(busy)
        self.assertEqual(msess.recover_interrupted(skip=[busy]), [])
        self.assertFalse((gone / "audio_part1.wav").exists())
        self.assertEqual(msp.leftover_parts(gone), [])
        self.assertEqual(len(msp.leftover_parts(busy)), 1)          # (the meeting being recorded)
        self.assertNotIn(msess.META_NOTES_PENDING, ms.load_meta(gone))

    def test_a_cloud_session_is_labelled_from_its_saved_pieces_never_read_again(self):
        d = self.folder(transcribed_with=msess.TRANSCRIBED_CLOUD, source="live_assist")
        with open(d / "chunks.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"index": 0, "text": "Barev.", "language": "hy"}) + "\n")
        self.spool(d, seconds=2.0, pieces=[(0.0, 1.5, "Barev.")])
        msess.recover_interrupted()
        s = msess.MeetingEngine(_App(_Rec()))
        s._spawn = lambda target, *args: None
        self.assertTrue(s._load_for_rewrite(d))
        out = types.SimpleNamespace(texts=["You: Barev."], notice="", reason="", parts=[[]], canonical=[],
                                    mode="sources", speakers=0, auto_names={})
        with patch.object(msess.meeting_speakers, "build_transcript", return_value=out) as build, \
                patch.object(msess.MeetingCore, "_summarize_and_finish"):
            s._recover_then_notes()
        kw = build.call_args[1]
        self.assertIn("reader", kw)
        self.assertEqual(kw["reader"](None), [{"start": 0.0, "end": 1.5, "text": "Barev."}])
        self.assertIn(msess.RECOVERED_NOTICE, s._transcript_notices)
        self.assertNotIn(msess.RECOVERED_CLOUD_NOTICE, s._transcript_notices)
        self.assertEqual(ms.load_transcript(d), "You: Barev.")


@needs_numpy
class TestTheRecorderWritesWhileRecording(_Dir):
    """The real AudioRecorder on fake streams: every read it keeps goes to the part on disk, the same
    samples as its two tracks in memory, and every transcribed piece with its place."""

    def test_the_part_on_disk_is_what_the_recorder_kept(self):
        from tests import test_recorder_tracks as trt
        main = trt._real_main(self)
        with patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": 1024, "min_speech_sec": 1.0,
                                   "backend": "local", "language": "en"}):
            with patch.object(main.pyaudio, "PyAudio", return_value=MagicMock()):
                rec = main.AudioRecorder()

            def fake_transcribe(audio, idx):
                with rec._chunk_lock:
                    rec._chunk_results[idx] = "piece %d" % idx
                main.AudioRecorder._spool_piece(rec, rec._spool, idx, "piece %d" % idx, "en")
            rec._transcribe_chunk = fake_transcribe
            mic, call = trt._hiss(np, 12.0) + trt._tone(np, 220, 4.0, 0.3, 2.0, total=12.0), \
                trt._tone(np, 880, 3.0, 0.25, 7.0, total=12.0)

            def stop():
                rec.stop_requested = True
            loop = trt._FakeStream(call, np, per_call=1024, wasapi=True)
            rec.audio = trt._FakeAudio([loop, trt._FakeStream(mic, np, on_empty=stop)])
            rec._find_default_loopback_device = lambda: 5
            d = self.folder()
            spool = msp.MeetingSpool(d, d / "audio_part1.wav", flush_s=0.05)
            rec.meeting_spool = spool
            rec.start_recording(capture_mode="smart_meeting")
            rec._record_thread.join(30)
            rec.stop_recording()
            for t in rec._chunk_pipeline.threads():
                t.join(10)
        mic_pcm, sys_pcm = rec.get_full_audio_tracks()
        _crash(spool)                                       # (closed already: the record thread did it)
        part = msp.recover_part(d / "audio_part1.wav")
        n, mix = _frames(part)
        self.assertEqual(n, len(mic_pcm))
        self.assertTrue((_frames(ms.track_path(part, "mic"))[1] == mic_pcm).all())
        self.assertTrue((_frames(ms.track_path(part, "system"))[1] == sys_pcm).all())
        import recorder_tracks
        self.assertTrue((abs(mix.astype(int) - recorder_tracks.to_int16(rec.get_full_audio()).astype(int)) <= 1).all())
        pieces = msp.load_pieces(part)
        self.assertTrue(pieces and all("s" in p and "e" in p for p in pieces))
        self.assertEqual(pieces[0]["t"], "piece 0")
