"""file_jobs: the Qt-free Audio to text job runner and queue.

run_file_job gets fakes for every heavy piece (decoder, model download,
recorder, diarizer), so nothing here loads main, Qt, numpy or a model. The
streaming decoder is checked against faster-whisper's own decoder when real
numpy / PyAV / faster-whisper are importable (skipped under test_core's stubs).
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import file_jobs as fj  # noqa: E402

SR = 16000


class FakeAudio:
    def __init__(self, seconds):
        self.n = int(seconds * SR)

    def __len__(self):
        return self.n


SEGS = [{"start": 0.0, "end": 4.0, "text": "Hello there."},
        {"start": 4.2, "end": 8.0, "text": "Hi, how are you?"},
        {"start": 8.5, "end": 9.5, "text": "Good."}]


class FakeRecorder:
    def __init__(self, segments=SEGS, detected=None, load_error=None, cancel_flag=None):
        self.segments = segments
        self.detected = detected
        self.load_error = load_error
        self.loaded = []
        self.calls = []
        self._last_segments_language = "stale"
        self._last_segments_seconds = None

    def load_model(self, name):
        if self.load_error:
            raise RuntimeError(self.load_error)
        self.loaded.append(name)

    def transcribe_segments(self, audio, language=None, model_name=None,
                            on_progress=None, should_cancel=None):
        self.calls.append({"language": language, "model": model_name, "samples": len(audio),
                           "lang_before": self._last_segments_language})
        if self.segments is None:
            return []
        total = len(audio) / SR
        out = []
        for s in self.segments:
            if should_cancel is not None and should_cancel():
                return None
            out.append(dict(s))
            if on_progress:
                on_progress(s["end"], total)
        self._last_segments_seconds = 2.5
        if self.detected:
            self._last_segments_language = self.detected
        return out


class FakeDiarizer:
    def __init__(self, installed=True, downloaded=True, turns=((0.0, 4.1, 0), (4.1, 10.0, 1))):
        self.installed = installed
        self.downloaded = downloaded
        self.turns = list(turns)
        self.downloads = 0
        self.diarized = 0

    def sherpa_installed(self):
        return self.installed

    def models_downloaded(self):
        return self.downloaded

    def download_models(self):
        self.downloads += 1
        self.downloaded = True
        return True

    def is_available(self):
        return self.installed and self.downloaded

    def diarize(self, audio, on_progress=None):
        self.diarized += 1
        if on_progress:
            on_progress(1, 2)
            on_progress(2, 2)
        return self.turns

    def attribute_segments(self, segments, turns):
        out = []
        for seg in segments:
            mid = (seg["start"] + seg["end"]) / 2
            spk = next((t[2] for t in turns if t[0] <= mid < t[1]), None)
            out.append(dict(seg, speaker=spk))
        return out


def run(opts=None, recorder=None, *, seconds=60.0, probe=60.0, downloaded=True,
        download_error=None, cancel=lambda: False, progress=None, info=None, diarizer=None,
        decode=None, auto="base"):
    recorder = recorder or FakeRecorder()
    events = progress if progress is not None else []
    downloads = []

    def fake_decode(path, sr, expected_seconds=None, on_progress=None, should_cancel=None):
        if on_progress:
            on_progress(0.5)
            on_progress(1.0)
        return FakeAudio(seconds)

    def fake_download(name, on_progress=None):
        downloads.append(name)
        if download_error:
            raise download_error
        on_progress(None, 0, 0)
        on_progress(50, 5, 10)
        on_progress(99, 9, 10)

    result = fj.run_file_job(
        "C:/audio/talk.m4a", opts if opts is not None else {}, recorder,
        lambda pct, stage, eta: events.append((pct, stage, eta)), cancel,
        on_info=(info.append if info is not None else None),
        decode=decode or fake_decode, probe=lambda p: probe,
        model_downloaded=lambda m: downloaded, download_model=fake_download,
        auto_model=lambda rec: auto, diarizer=diarizer or FakeDiarizer())
    return result, recorder, events, downloads


class TestRunFileJob(unittest.TestCase):
    def test_happy_path_result(self):
        info = []
        result, rec, events, downloads = run({"model": "small", "language": "en"}, info=info)
        self.assertEqual(result["path"], "C:/audio/talk.m4a")
        self.assertEqual(result["name"], "talk.m4a")
        self.assertEqual(result["duration"], 60.0)
        self.assertEqual(result["model"], "small")
        self.assertEqual((result["language"], result["language_setting"]), ("en", "en"))
        self.assertEqual(result["speaker_count"], 2)
        self.assertEqual([s["speaker"] for s in result["segments"]], [0, 1, 1])
        self.assertEqual([s["end"] for s in result["segments"]], [4.0, 8.0, 9.5])
        self.assertEqual([p["speaker"] for p in result["paragraphs"]], [0, 1])
        self.assertTrue(result["timestamps"] and result["speakers"])
        self.assertEqual(result["decode_seconds"], 2.5)
        self.assertEqual(rec.loaded, ["small"])
        self.assertEqual(rec.calls[0]["model"], "small")
        self.assertEqual(rec.calls[0]["samples"], 60 * SR)
        self.assertEqual(downloads, [])
        self.assertIn({"duration": 60.0}, info)
        self.assertIn({"model": "small"}, info)

    def test_progress_is_ordered_and_bounded(self):
        _, _, events, _ = run({"model": "small"}, downloaded=False)
        pcts = [e[0] for e in events]
        self.assertEqual(pcts, sorted(pcts))
        self.assertTrue(all(0 <= p <= 100 for p in pcts))
        stages = [e[1] for e in events]
        for needed in (fj.STAGE_READING, "Downloading Whisper Small", "Loading Whisper Small",
                       fj.STAGE_TRANSCRIBING, fj.STAGE_SPEAKERS, fj.STAGE_FINISHING):
            self.assertIn(needed, stages)
        self.assertLess(stages.index("Downloading Whisper Small"), stages.index(fj.STAGE_TRANSCRIBING))
        self.assertTrue(all(isinstance(e[2], (float, type(None))) for e in events))

    def test_language_is_passed_explicitly(self):
        # "auto" is passed as such: the recorder's stale per-meeting override is never used.
        _, rec, _, _ = run({"language": "auto"})
        self.assertEqual(rec.calls[0]["language"], "auto")
        self.assertEqual(rec.calls[0]["lang_before"], None)     # reset before the call
        _, rec, _, _ = run({"language": "multi"})
        self.assertEqual(rec.calls[0]["language"], "multi")
        _, rec, _, _ = run({})
        self.assertIsNone(rec.calls[0]["language"])             # classic: recorder default

    def test_detected_language_for_auto_and_multi(self):
        result, *_ = run({"language": "auto"}, FakeRecorder(detected="fr"))
        self.assertEqual((result["language"], result["language_setting"]), ("fr", "auto"))
        result, *_ = run({"language": "multi"}, FakeRecorder(detected="de"))
        self.assertEqual((result["language"], result["language_setting"]), ("de", "multi"))
        result, *_ = run({"language": "auto"}, FakeRecorder())   # main.py not updated yet
        self.assertIsNone(result["language"])
        result, *_ = run({"language": "es"}, FakeRecorder(detected="en"))
        self.assertEqual(result["language"], "es")

    def test_automatic_model(self):
        result, rec, _, _ = run({"model": "auto"}, auto="large-v3-turbo")
        self.assertEqual(result["model"], "large-v3-turbo")
        self.assertEqual(rec.loaded, ["large-v3-turbo"])

    def test_download_once_then_load(self):
        result, rec, _, downloads = run({"model": "medium"}, downloaded=False)
        self.assertEqual(downloads, ["medium"])
        self.assertEqual(rec.loaded, ["medium"])

    def test_download_failure(self):
        with self.assertRaises(fj.JobError) as cm:
            run({"model": "medium"}, downloaded=False, download_error=OSError("[Errno 11001] getaddrinfo"))
        self.assertEqual(cm.exception.reason, "download")
        self.assertEqual(cm.exception.message, "Whisper Medium couldn't be downloaded. "
                                               "Check your internet connection and try again.")

    def test_download_error_sentence_from_model_manager(self):
        class DownloadError(Exception):       # model_manager.DownloadError: str(e) is for people
            pass
        with self.assertRaises(fj.JobError) as cm:
            run({"model": "small"}, downloaded=False,
                download_error=DownloadError("Not enough free disk space for this model."))
        self.assertEqual(cm.exception.message, "Whisper Small couldn't be downloaded. "
                                               "Not enough free disk space for this model.")

    def test_model_load_failure_is_reported(self):
        with self.assertRaises(fj.JobError) as cm:
            run({"model": "small"}, FakeRecorder(load_error="CUDA out of memory"))
        self.assertEqual(cm.exception.reason, "model")
        self.assertIn("Whisper Small couldn't be loaded: CUDA out of memory", cm.exception.message)

    def test_no_audio_and_no_speech(self):
        with self.assertRaises(fj.JobError) as cm:
            run(seconds=0.2)
        self.assertEqual((cm.exception.reason, cm.exception.message), ("no_audio", fj.MSG_NO_AUDIO))
        with self.assertRaises(fj.JobError) as cm:
            run(recorder=FakeRecorder(segments=None))
        self.assertEqual((cm.exception.reason, cm.exception.message), ("no_speech", fj.MSG_NO_SPEECH))

    def test_unreadable_and_memory(self):
        def broken(*a, **k):
            raise ValueError("Invalid data found when processing input")

        def huge(*a, **k):
            raise MemoryError()
        with self.assertRaises(fj.JobError) as cm:
            run(decode=broken)
        self.assertEqual(cm.exception.reason, "unreadable")
        with self.assertRaises(fj.JobError) as cm:
            run(decode=huge)
        self.assertEqual(cm.exception.reason, "memory")

    def test_cancel_before_transcribing(self):
        state = {"n": 0}

        def cancel():
            state["n"] += 1
            return state["n"] > 1          # after the probe check
        rec = FakeRecorder()
        with self.assertRaises(fj.JobCancelled):
            run(recorder=rec, cancel=cancel)
        self.assertEqual(rec.calls, [])

    def test_cancel_mid_transcription(self):
        flag = {"on": False}

        class CancelOnSecond(FakeRecorder):
            def transcribe_segments(self, audio, **kw):
                flag["on"] = True
                return super().transcribe_segments(audio, **kw)
        diar = FakeDiarizer()
        with self.assertRaises(fj.JobCancelled):
            run(recorder=CancelOnSecond(), cancel=lambda: flag["on"], diarizer=diar)
        self.assertEqual(diar.diarized, 0)

    def test_cancel_during_speakers_drops_the_result(self):
        flag = {"on": False}

        class SetFlag(FakeDiarizer):
            def diarize(self, audio, on_progress=None):
                flag["on"] = True
                return super().diarize(audio, on_progress)
        with self.assertRaises(fj.JobCancelled):
            run(cancel=lambda: flag["on"], diarizer=SetFlag())

    def test_without_speaker_labels(self):
        diar = FakeDiarizer()
        result, *_ = run({"speakers": False}, diarizer=diar)
        self.assertEqual(diar.diarized, 0)
        self.assertEqual(result["speaker_count"], 0)
        self.assertTrue(all(s["speaker"] is None for s in result["segments"]))
        self.assertEqual(len(result["paragraphs"]), 1)

    def test_speaker_model_downloaded_once_or_skipped(self):
        diar = FakeDiarizer(downloaded=False)
        result, *_ = run(diarizer=diar)
        self.assertEqual((diar.downloads, diar.diarized, result["speaker_count"]), (1, 1, 2))
        diar = FakeDiarizer(installed=False)
        result, *_ = run(diarizer=diar)
        self.assertEqual((diar.downloads, diar.diarized, result["speaker_count"]), (0, 0, 0))

    def test_any_length_for_everyone(self):
        # Ten hours: no refusal, no limit anywhere in the module.
        result, *_ = run(seconds=10 * 3600, probe=10 * 3600)
        self.assertEqual(result["duration"], 10 * 3600)
        for name in ("MAX_SEC", "duration_error"):
            self.assertFalse(hasattr(fj, name), name)
        with open(fj.__file__, encoding="utf-8") as f:
            src = f.read().lower()
        for words in ("up to 5 hours", "1 hour", "too long", "over the free"):
            self.assertNotIn(words, src)


class TestHelpers(unittest.TestCase):
    def test_options(self):
        self.assertEqual(fj.normalize_options(None), {
            "model": None, "language": None, "speakers": True, "timestamps": True, "save_word": False})
        self.assertIsNone(fj.normalize_options({"model": "Auto"})["model"])
        cfg = {"file_model": "large-v3", "file_language": "multi", "file_speakers": False,
               "file_timestamps": False, "file_save_word": False}
        self.assertEqual(fj.options_from_cfg(cfg), {
            "model": "large-v3", "language": "multi", "speakers": False, "timestamps": False,
            "save_word": False})
        self.assertEqual(fj.options_from_cfg({}), {
            "model": None, "language": "auto", "speakers": True, "timestamps": True,
            "save_word": True})
        self.assertIsNone(fj.options_from_cfg({"file_model": "huge-v9"})["model"])
        self.assertEqual(set(fj.FILE_DEFAULTS), {"file_model", "file_language", "file_speakers",
                                                 "file_timestamps", "file_save_word",
                                                 "notify_when_ready"})

    def test_whisper_names(self):
        self.assertEqual(fj.whisper_name("small"), "Whisper Small")
        self.assertEqual(fj.whisper_name("large-v3-turbo"), "Whisper Large v3 Turbo")
        self.assertEqual(fj.whisper_name(None), "Whisper")
        self.assertEqual(set(fj.WHISPER_NAMES), set(fj.WHISPER_MODELS))

    def test_time_copy(self):
        self.assertEqual(fj.ready_in_text(None), "")
        self.assertEqual(fj.ready_in_text(20), "Ready in under a minute")
        self.assertEqual(fj.ready_in_text(6 * 60 + 10), "Ready in about 6 min")
        self.assertEqual(fj.ready_in_text(80 * 60), "Ready in about 1 h 20 min")
        self.assertEqual(fj.ready_in_text(120 * 60), "Ready in about 2 h")
        self.assertEqual(fj._fmt_eta(30), "under a minute left")
        self.assertEqual(fj._fmt_eta(400), "about 7 min left")
        self.assertEqual(fj._fmt_dur(59), "59 s")
        self.assertEqual(fj._fmt_dur(3725), "1 h 02 min")

    def test_progress_stage_bounds_are_ordered(self):
        self.assertLess(fj._P_READ_END, fj._P_DOWNLOAD_END)
        self.assertLess(fj._P_DOWNLOAD_END, fj._P_TRANSCRIBE_END_WITH_SPK)
        self.assertLess(fj._P_TRANSCRIBE_END_WITH_SPK, fj._P_SPEAKERS_END)
        self.assertLessEqual(fj._P_TRANSCRIBE_END_NO_SPK, fj._P_FINISHING)

    def test_audio_files(self):
        self.assertTrue(fj.is_audio_file("a/B.MP3"))
        self.assertTrue(fj.is_audio_file("clip.mov"))
        self.assertFalse(fj.is_audio_file("notes.txt"))
        self.assertEqual(fj.file_kind("lecture.mp4"), "video")

    def test_pick_auto_model(self):
        have = {"small", "large-v3"}
        self.assertEqual(fj.pick_auto_model(device="cuda", downloaded=have.__contains__), "large-v3")
        self.assertEqual(fj.pick_auto_model(device="cuda", downloaded=lambda m: False), "large-v3-turbo")
        self.assertEqual(fj.pick_auto_model(device="cpu", downloaded=have.__contains__), "small")
        self.assertEqual(fj.pick_auto_model(device="cpu", downloaded=lambda m: False), "base")
        broken_gpu = type("R", (), {"_cuda_usable": False})()
        self.assertEqual(fj.pick_auto_model(broken_gpu, device="cuda",
                                            downloaded=have.__contains__), "small")

    def test_refusal_message(self):
        class App:
            is_rec = False
            _busy = False
            _file_job_running = False
            meeting = False

            def _is_meeting_busy(self):
                return self.meeting
        app = App()
        self.assertEqual(fj.refusal_message(None), "")
        self.assertEqual(fj.refusal_message(app), "")
        app.is_rec = True
        self.assertEqual(fj.refusal_message(app), fj.MSG_RECORDING)
        app.is_rec, app._busy = False, True
        self.assertEqual(fj.refusal_message(app), fj.MSG_RECORDING)
        app._busy, app.meeting = False, True
        self.assertEqual(fj.refusal_message(app), fj.MSG_RECORDING)
        app.meeting, app._file_job_running = False, True
        self.assertEqual(fj.refusal_message(app), fj.MSG_OTHER_JOB)
        for msg in (fj.MSG_RECORDING, fj.BUSY_FOR_DICTATION, fj.BUSY_FOR_MEETING, fj.BUSY_TITLE):
            self.assertNotIn("Transcribe", msg)

    def test_imports_without_qt_numpy_or_main(self):
        code = ("import sys, file_jobs, transcript_store; "
                "bad = sorted({m.split('.')[0] for m in sys.modules} & "
                "{'PySide6', 'numpy', 'main', 'faster_whisper', 'av', 'ui'}); "
                "print(bad); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# ── the queue ────────────────────────────────────────────────────────────────

class FakeApp:
    def __init__(self, **cfg):
        self.cfg = {"whisper_model": "base"}
        self.cfg.update(cfg)
        self.recorder = object()
        self.is_rec = False
        self._busy = False
        self._file_job_running = False
        self.meeting = False
        self.tracked = []
        self.waits = []
        self.preloads = 0

    def track(self, event, props=None):
        self.tracked.append((event, props))

    def _note_local_wait(self, context, seconds, model=None):
        self.waits.append((context, seconds, model))

    def _is_meeting_busy(self):
        return self.meeting

    def _preload_speech_model(self):
        self.preloads += 1

    def events(self, name):
        return [p for e, p in self.tracked if e == name]


class FakeStore:
    def __init__(self, fail=False):
        self.fail = fail
        self.saved = []
        self.words = []
        self.meta = []

    def save_transcript(self, result):
        if self.fail:
            raise OSError("disk full")
        self.saved.append(result)
        return Path("lib") / f"2026100{len(self.saved)}_112000"

    def save_word_copy(self, doc):
        self.words.append(doc)
        return "C:/out/talk transcript.docx"

    def update_meta(self, id_or_dir, **fields):
        self.meta.append((id_or_dir, fields))
        return True


def make_result(path, model="small", duration=90.0):
    return {"path": path, "name": os.path.basename(path), "duration": duration,
            "segments": [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": None}],
            "paragraphs": [{"start": 0.0, "speaker": None, "text": "hi"}],
            "timestamps": True, "speakers": False, "speaker_count": 0, "model": model,
            "language": "en", "language_setting": "auto", "decode_seconds": 42.0}


class TestQueue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.files = []
        for name in ("a.mp3", "b.m4a", "c.wav"):
            p = os.path.join(self._tmp.name, name)
            open(p, "wb").close()
            self.files.append(p)
        self.app = FakeApp()
        self.store = FakeStore()
        self.log = []
        self.spawned = []
        self.runs = []
        self.behaviour = {}

    def tearDown(self):
        self._tmp.cleanup()

    def runner(self, path, opts, recorder, on_progress, should_cancel, **kw):
        self.runs.append({"path": path, "opts": opts, "flag": self.app._file_job_running,
                          "recorder": recorder, "kw": sorted(kw)})
        kw["on_info"]({"duration": 90.0, "model": "small"})
        on_progress(40, fj.STAGE_TRANSCRIBING, 200.0)
        how = self.behaviour.get(os.path.basename(path))
        if how == "wait_cancel":
            if not should_cancel():
                raise AssertionError("expected a cancel")
            raise fj.JobCancelled()
        if how == "error":
            raise fj.JobError("no_speech", fj.MSG_NO_SPEECH)
        if how == "crash":
            raise RuntimeError("boom")
        if how == "oom":
            raise MemoryError()
        return make_result(path)

    def queue(self, sync=True, **kw):
        return fj.FileJobQueue(
            self.app, runner=self.runner, store=self.store,
            spawn=(lambda fn: fn()) if sync else self.spawned.append,
            listener=lambda event, *args: self.log.append((event,) + args), **kw)

    def names(self):
        return [e[0] for e in self.log]

    def test_one_file_end_to_end(self):
        q = self.queue()
        items, msg = q.enqueue(self.files[0], {"model": "small", "save_word": True})
        self.assertEqual(msg, "")
        item = items[0]
        self.assertEqual(self.names(), ["queued", "started", "running_changed", "progress",
                                        "progress", "finished", "running_changed"])
        self.assertTrue(self.runs[0]["flag"])                 # the flag is up while it runs
        self.assertFalse(self.app._file_job_running)          # and down after
        self.assertIs(self.runs[0]["recorder"], self.app.recorder)
        self.assertEqual(item["state"], "done")
        self.assertEqual((item["pct"], item["stage"], item["eta"]), (100, fj.STAGE_DONE, ""))
        self.assertEqual((item["duration"], item["model"], item["model_name"]),
                         (90.0, "small", "Whisper Small"))
        self.assertEqual(item["transcript_id"], "20261001_112000")
        self.assertEqual(item["word_path"], "C:/out/talk transcript.docx")
        self.assertEqual(self.store.meta, [("20261001_112000", {"word_path": item["word_path"]})])
        progress = [e for e in self.log if e[0] == "progress"]
        self.assertEqual(progress[-1][1:], (40, fj.STAGE_TRANSCRIBING, "Ready in about 3 min"))
        self.assertEqual(self.app.events("file_transcription_started"),
                         [{"model": "small", "speakers": True, "timestamps": True}])
        done = self.app.events("file_transcription_completed")[0]
        self.assertEqual((done["model"], done["audio_minutes"], done["speakers"], done["timestamps"]),
                         ("small", 1.5, 0, True))
        self.assertEqual(self.app.events("file_transcription_saved"),
                         [{"format": "docx", "auto": True}])  # the "Save as Word" copy
        self.assertEqual(self.app.waits, [("file", 42.0, "small")])
        self.assertEqual(self.app.preloads, 1)                # small != dictation's base
        self.assertEqual(self.log[-1], ("running_changed", False))

    def test_options_come_from_cfg(self):
        self.app.cfg.update(file_model="medium", file_language="fr", file_speakers=False,
                            file_save_word=False)
        self.queue().enqueue([self.files[0]])
        self.assertEqual(self.runs[0]["opts"], {"model": "medium", "language": "fr",
                                                "speakers": False, "timestamps": True,
                                                "save_word": False})
        self.assertEqual(self.store.words, [])
        self.assertEqual(self.app.events("file_transcription_saved"), [])

    def test_partial_opts_take_the_rest_from_cfg(self):
        # A page that only overrides the model still gets the rail's language -
        # never None (= the recorder's stale per-meeting language).
        self.app.cfg.update(file_language="fr", file_timestamps=False)
        self.queue().enqueue([self.files[0]], {"model": "large-v3"})
        self.assertEqual(self.runs[0]["opts"], {"model": "large-v3", "language": "fr",
                                                "speakers": True, "timestamps": False,
                                                "save_word": True})
        self.queue().enqueue([self.files[1]], {"model": "auto", "language": "de"})
        self.assertEqual((self.runs[1]["opts"]["model"], self.runs[1]["opts"]["language"]),
                         (None, "de"))

    def test_post_after_the_controller_is_gone(self):
        def dead(fn):
            raise RuntimeError("Internal C++ object already deleted.")
        q = fj.FileJobQueue(self.app, runner=self.runner, store=self.store,
                            spawn=lambda fn: fn(), post=dead)
        q.enqueue(self.files[0])          # must not raise in the worker
        self.assertEqual(len(self.runs), 1)

    def test_fifo_one_at_a_time(self):
        q = self.queue(sync=False)
        items, _ = q.enqueue(self.files[:2])
        self.assertEqual([i["state"] for i in items], ["running", "queued"])
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual([i["id"] for i in q.pending()], [items[1]["id"]])
        more, _ = q.enqueue([self.files[2], self.files[0]])  # a.mp3 is already running
        self.assertEqual([i["name"] for i in more], ["c.wav"])
        self.spawned.pop(0)()
        self.assertEqual(items[0]["state"], "done")
        self.assertIs(q.current(), items[1])
        self.assertTrue(self.app._file_job_running)           # stays up between jobs
        self.spawned.pop(0)()
        self.spawned.pop(0)()
        self.assertEqual([r["path"] for r in self.runs], [self.files[0], self.files[1], self.files[2]])
        self.assertFalse(q.is_running())
        self.assertFalse(self.app._file_job_running)
        self.assertEqual([e for e in self.log if e[0] == "running_changed"],
                         [("running_changed", True), ("running_changed", False)])
        self.assertEqual(len(self.app.events("file_transcription_started")), 3)

    def test_refused_while_recording_or_in_a_meeting(self):
        q = self.queue()
        for attr in ("is_rec", "_busy", "meeting"):
            setattr(self.app, attr, True)
            items, msg = q.enqueue(self.files[0])
            self.assertEqual((items, msg), ([], fj.MSG_RECORDING))
            setattr(self.app, attr, False)
        self.app._file_job_running = True                     # the classic window's job
        self.assertEqual(q.enqueue(self.files[0]), ([], fj.MSG_OTHER_JOB))
        self.assertEqual(self.runs, [])
        self.assertEqual(self.app.tracked, [])

    def test_missing_files(self):
        q = self.queue(sync=False)
        missing = os.path.join(self._tmp.name, "gone.mp3")
        self.assertEqual(q.enqueue(missing), ([], "That file couldn't be found."))
        items, msg = q.enqueue([missing, self.files[0], self._tmp.name])
        self.assertEqual(len(items), 1)
        self.assertEqual(msg, "2 of the files couldn't be found.")
        self.assertEqual(q.enqueue([]), ([], ""))

    def test_cancel_queued_and_running(self):
        q = self.queue(sync=False)
        items, _ = q.enqueue(self.files[:2])
        self.assertTrue(q.cancel(items[1]["id"]))
        self.assertEqual(items[1]["state"], "cancelled")
        self.assertEqual(q.pending(), [])
        self.assertFalse(q.cancel(999))
        self.behaviour["a.mp3"] = "wait_cancel"
        self.assertTrue(q.cancel())
        self.assertEqual(items[0]["stage"], fj.STAGE_STOPPING)
        self.assertEqual(self.log[-1], ("progress", 0, fj.STAGE_STOPPING, ""))
        self.spawned.pop(0)()
        self.assertEqual(items[0]["state"], "cancelled")
        self.assertIn(("cancelled", items[0]), self.log)
        self.assertEqual(self.app.events("file_transcription_failed")[0]["reason"], "cancelled")
        self.assertEqual(self.app.events("file_transcription_completed"), [])
        self.assertEqual(self.store.saved, [])
        self.assertFalse(self.app._file_job_running)
        self.assertFalse(q.cancel())

    def test_cancel_after_the_result_drops_it(self):
        q = self.queue(sync=False)
        items, _ = q.enqueue(self.files[0])
        q.cancel()
        self.spawned.pop(0)()              # the runner returns a result anyway
        self.assertEqual(items[0]["state"], "cancelled")
        self.assertEqual(self.store.saved, [])

    def test_failures(self):
        self.behaviour.update({"a.mp3": "error", "b.m4a": "crash", "c.wav": "oom"})
        q = self.queue()
        for path in self.files:
            q.enqueue(path)
        failed = [e for e in self.log if e[0] == "failed"]
        self.assertEqual(len(failed), 3)
        self.assertEqual(failed[0][2], fj.MSG_NO_SPEECH)
        self.assertEqual((failed[0][1]["state"], failed[0][1]["reason"]), ("failed", "no_speech"))
        # An unexpected crash: one plain sentence on the card, never "boom".
        self.assertEqual((failed[1][2], failed[1][1]["reason"]), (fj.MSG_ERROR, "error"))
        self.assertEqual((failed[2][2], failed[2][1]["reason"]), (fj.MSG_MEMORY, "memory"))
        self.assertEqual([p["reason"] for p in self.app.events("file_transcription_failed")],
                         ["error", "error", "error"])
        self.assertEqual(self.app.waits, [])
        self.assertFalse(self.app._file_job_running)

    def test_store_failure_keeps_the_result(self):
        self.store.fail = True
        q = self.queue()
        items, _ = q.enqueue(self.files[0], {"save_word": False})
        self.assertEqual(items[0]["state"], "done")
        self.assertIsNone(items[0]["transcript_id"])
        self.assertTrue(items[0]["save_error"])
        self.assertIsNotNone(items[0]["result"])

    def test_listener_can_queue_the_next_file(self):
        q = self.queue()
        follow = {"done": False}

        def listener(event, *args):
            self.log.append((event,) + args)
            if event == "finished" and not follow["done"]:
                follow["done"] = True
                q.enqueue(self.files[1])
        q._listener = listener
        q.enqueue(self.files[0])
        self.assertEqual(len(self.runs), 2)
        self.assertEqual([e for e in self.log if e[0] == "running_changed"],
                         [("running_changed", True), ("running_changed", False)])
        self.assertFalse(self.app._file_job_running)

    def test_same_model_does_not_reload_dictation(self):
        self.app.cfg["whisper_model"] = "small"
        self.queue().enqueue(self.files[0])
        self.assertEqual(self.app.preloads, 0)

    def test_downloader_is_passed_through(self):
        dl = object()
        self.queue(downloader=dl).enqueue(self.files[0])
        self.assertIn("download_model", self.runs[0]["kw"])
        self.queue().enqueue(self.files[1])
        self.assertNotIn("download_model", self.runs[1]["kw"])

    def test_track_saved_and_cancel_all(self):
        q = self.queue(sync=False)
        q.track_saved("srt")
        q.track_saved("docx", auto=True)
        self.assertEqual(self.app.events("file_transcription_saved"),
                         [{"format": "srt"}, {"format": "docx", "auto": True}])
        items, _ = q.enqueue(self.files)
        q.cancel_all()
        self.assertEqual([i["state"] for i in items[1:]], ["cancelled", "cancelled"])
        self.assertTrue(items[0]["_cancel"].is_set())


def _real_decoder_stack():
    try:
        import numpy as np
        import av  # noqa: F401
        from faster_whisper.audio import decode_audio  # noqa: F401
        return np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,) \
            and callable(decode_audio) and decode_audio.__module__ == "faster_whisper.audio"
    except Exception:
        return False


@unittest.skipUnless(_real_decoder_stack(), "real numpy / PyAV / faster-whisper not importable (stubbed)")
class TestStreamingDecode(unittest.TestCase):
    """The streaming decoder returns exactly what faster-whisper's does."""

    @classmethod
    def setUpClass(cls):
        import wave

        import numpy as np
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls._tmp.name, "tone.wav")
        rate, secs = 44100, 3.3
        t = np.arange(int(rate * secs)) / rate
        left = 0.4 * np.sin(2 * np.pi * 440 * t)
        right = 0.3 * np.sin(2 * np.pi * 660 * t)
        pcm = (np.stack([left, right], axis=1) * 32767).astype("<i2")
        with wave.open(cls.path, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_identical_to_faster_whisper(self):
        import numpy as np
        from faster_whisper.audio import decode_audio
        ref = decode_audio(self.path, sampling_rate=SR)
        steps = []
        for expected in (fj.probe_duration(self.path), None, 0.1):   # sized, unknown, too small
            out = fj._decode_streaming(self.path, SR, expected, steps.append, None)
            self.assertEqual(out.dtype, np.float32)
            self.assertTrue(np.array_equal(out, ref), expected)
        self.assertAlmostEqual(fj.probe_duration(self.path), 3.3, places=2)
        self.assertEqual(steps[-1], 1.0)

    def test_lying_length_never_refuses_a_short_file(self):
        # Metadata claiming an impossible length: the first buffer can't be
        # allocated, so it starts small and grows - same samples as faster-whisper.
        import numpy as np
        from unittest import mock

        from faster_whisper.audio import decode_audio
        real = fj._initial_capacity
        huge = lambda secs, sr: 10 ** 14 if secs else real(secs, sr)  # noqa: E731  (400 TB)
        with mock.patch.object(fj, "_initial_capacity", huge):
            out = fj._decode_streaming(self.path, SR, 20 * 3600, None, None)
        self.assertTrue(np.array_equal(out, decode_audio(self.path, sampling_rate=SR)))

    def test_cancel_while_reading(self):
        with self.assertRaises(fj.JobCancelled):
            fj.decode_file(self.path, SR, 3.3, None, lambda: True)

    def test_unreadable_file(self):
        bad = os.path.join(self._tmp.name, "bad.mp3")
        with open(bad, "wb") as f:
            f.write(b"not audio at all" * 10)
        with self.assertRaises(Exception):
            fj.decode_file(bad)


if __name__ == "__main__":
    unittest.main()
