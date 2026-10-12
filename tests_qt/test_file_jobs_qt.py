"""FileJobController (file_jobs) on a real event loop, running run_file_job
with the Audio to text options (offscreen)."""
import os
import threading
import time

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from tests_qt.conftest import make_stub_app


def _wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    QApplication.processEvents()
    return cond()


def _result(path, model="small"):
    return {"path": path, "name": os.path.basename(path), "duration": 75.0,
            "segments": [{"start": 0.0, "end": 2.0, "text": "Hello there.", "speaker": 0},
                         {"start": 2.5, "end": 4.0, "text": "Hi!", "speaker": 1}],
            "paragraphs": [{"start": 0.0, "speaker": 0, "text": "Hello there."},
                           {"start": 2.5, "speaker": 1, "text": "Hi!"}],
            "timestamps": True, "speakers": True, "speaker_count": 2, "model": model,
            "language": "en", "language_setting": "auto", "decode_seconds": 12.0}


@pytest.fixture
def audio_files(tmp_path):
    paths = []
    for name in ("interview_mariam.m4a", "board_call.mp3"):
        p = tmp_path / "src" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"")
        paths.append(str(p))
    return paths


def _app():
    app = make_stub_app("free")
    app.recorder = object()
    app._file_job_running = False
    app.is_rec = False
    app._busy = False
    app._is_meeting_busy = lambda: False
    app._note_local_wait = lambda *a: app.calls.append(("_note_local_wait",) + a)
    app._preload_speech_model = lambda: app.calls.append(("_preload_speech_model",))
    return app


def _record(ctrl):
    log = []
    main_thread = QApplication.instance().thread()
    for name in ("queued", "started", "progress", "finished", "failed", "cancelled",
                 "running_changed"):
        def slot(*args, _name=name):
            log.append((_name, QThread.currentThread() is main_thread) + args)
        getattr(ctrl, name).connect(slot)
    return log


def test_controller_runs_a_job_on_a_worker_and_reports_on_the_gui_thread(qapp, audio_files):
    import file_jobs
    import transcript_store
    app = _app()
    workers = []

    def runner(path, opts, recorder, on_progress, should_cancel, **kw):
        workers.append(threading.current_thread() is not threading.main_thread())
        assert app._file_job_running is True
        kw["on_info"]({"duration": 75.0, "model": "small"})
        on_progress(50, file_jobs.STAGE_TRANSCRIBING, 400.0)
        return _result(path)

    ctrl = file_jobs.FileJobController(app, runner=runner)
    log = _record(ctrl)
    items, msg = ctrl.enqueue(audio_files[0], {"model": "small", "save_word": True})
    assert msg == "" and ctrl.is_running() and ctrl.current() is items[0]
    assert _wait_for(lambda: any(e[0] == "running_changed" and e[2] is False for e in log))
    assert workers == [True]
    assert all(e[1] for e in log), "every signal is delivered on the GUI thread"
    names = [e[0] for e in log]
    assert names[:3] == ["queued", "started", "running_changed"]
    assert "finished" in names and "failed" not in names
    assert ("progress", True, 50, "Transcribing", "Ready in about 7 min") in log
    item = items[0]
    assert item["state"] == "done" and app._file_job_running is False
    # Saved to the library (the test data folder) and as Word next to the source.
    doc = transcript_store.load_transcript(item["transcript_id"])
    assert doc["source_name"] == "interview_mariam.m4a" and doc["speaker_count"] == 2
    assert doc["word_path"] == item["word_path"]
    assert item["word_path"] == os.path.join(os.path.dirname(audio_files[0]),
                                             "interview_mariam transcript.docx")
    assert os.path.isfile(item["word_path"])
    assert ("_note_local_wait", "file", 12.0, "small") in app.calls
    tracked = [c[1:] for c in app.calls if c[0] == "track"]
    assert [t[0] for t in tracked] == ["file_transcription_started", "file_transcription_completed",
                                       "file_transcription_saved"]
    assert tracked[2][1] == {"format": "docx", "auto": True}
    ctrl.deleteLater()


def test_controller_cancel_and_queue(qapp, audio_files):
    import file_jobs
    app = _app()
    gate = threading.Event()

    def runner(path, opts, recorder, on_progress, should_cancel, **kw):
        if path.endswith(".m4a"):
            gate.set()
            end = time.monotonic() + 5
            while not should_cancel() and time.monotonic() < end:
                time.sleep(0.01)
            raise file_jobs.JobCancelled()
        return _result(path)

    ctrl = file_jobs.FileJobController(app, runner=runner, persist=False)
    log = _record(ctrl)
    items, _ = ctrl.enqueue(audio_files)
    assert [i["state"] for i in items] == ["running", "queued"]
    assert gate.wait(5)
    assert ctrl.cancel() is True
    assert _wait_for(lambda: items[1]["state"] == "done")
    assert items[0]["state"] == "cancelled"
    names = [e[0] for e in log]
    assert names.count("cancelled") == 1 and names.count("finished") == 1
    assert names.index("cancelled") < names.index("finished")
    assert app._file_job_running is False
    reasons = [c[2]["reason"] for c in app.calls
               if c[0] == "track" and c[1] == "file_transcription_failed"]
    assert reasons == ["cancelled"]
    ctrl.shutdown()
    ctrl.deleteLater()


def test_controller_refuses_while_dictating(qapp, audio_files):
    import file_jobs
    app = _app()
    app.is_rec = True
    ctrl = file_jobs.FileJobController(app, runner=lambda *a, **k: pytest.fail("ran"))
    log = _record(ctrl)
    assert ctrl.enqueue(audio_files[0]) == ([], file_jobs.MSG_RECORDING)
    QApplication.processEvents()
    assert log == [] and app._file_job_running is False
    ctrl.deleteLater()


def test_classic_tab_still_works_on_run_file_job(qapp, audio_files, monkeypatch):
    # The 1.x file tab retired; Audio to text's controller runs the same
    # run_file_job with the rail's saved options (file_language, explicit -
    # never the recorder's override).
    import file_jobs
    import transcript_store
    app = _app()
    app.cfg.update(language="de", file_language="fr", file_save_word=False)
    seen = {}

    def fake_run(path, opts, recorder, on_progress, should_cancel, **kw):
        seen.update(path=path, opts=opts, recorder=recorder)
        on_progress(50, "Transcribing", 120.0)
        return _result(path, model="base")

    monkeypatch.setattr(file_jobs, "run_file_job", fake_run)
    ctrl = file_jobs.FileJobController(app)
    items, msg = ctrl.enqueue(audio_files[0])
    assert msg == "" and app._file_job_running is True
    assert _wait_for(lambda: items[0]["state"] == "done")
    assert app._file_job_running is False
    assert seen["opts"]["language"] == "fr"
    assert seen["recorder"] is app.recorder
    doc = transcript_store.load_transcript(items[0]["transcript_id"])
    assert transcript_store.plain_text(doc) == transcript_store.plain_text(_result(audio_files[0]))
    assert doc["model"] == "base"
    tracked = [c[1] for c in app.calls if c[0] == "track"]
    assert tracked == ["file_transcription_started", "file_transcription_completed"]
    ctrl.shutdown()
    ctrl.deleteLater()
