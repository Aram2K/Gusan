"""The speaker pass in its own process (diarization_proc.py).

Why it exists: sherpa-onnx's process() keeps the interpreter lock for its whole
run, so the app's window froze for the length of the speaker pass (minutes for
a long meeting). These tests prove the child-process path returns the same
turns, never leaves a folder or a process behind, honours cancel / timeout /
quit - and that a native call that holds the lock for seconds no longer stalls
the app's other threads (while the in-process control still does).

The worker is replaced by a tiny script or by a fake ``sherpa_onnx`` package, so
no model and no real sherpa is needed. Needs real numpy for the audio file
(skipped under test_core's suite-wide numpy stub; run this file on its own).
"""
import ctypes
import json
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import diarization
import diarization_proc


def _real_numpy():
    try:
        import numpy as np
        return float(np.asarray([1.0, 2.0, 3.0]).mean()) == 2.0
    except Exception:
        return False


needs_numpy = pytest.mark.skipif(not _real_numpy(), reason="real numpy needed (suite-wide stub in test_core)")

STUB_WORKER = textwrap.dedent('''
    import json, os, sys, time
    workdir = sys.argv[1]
    mode = os.environ.get("STUB_MODE", "ok")
    size = os.path.getsize(os.path.join(workdir, "audio.f32"))
    if mode == "crash":
        sys.stderr.write("native crash simulated")
        sys.exit(3)
    if mode == "hang":
        time.sleep(60)
    with open(os.path.join(workdir, "progress.json"), "w") as f:
        json.dump({"done": 1, "total": 2}, f)
    time.sleep(float(os.environ.get("STUB_WORK_S", "0.6")))
    if mode == "fail":
        res = {"ok": False, "error": "models missing"}
    else:
        res = {"ok": True, "turns": [[0.0, size / 4 / 16000.0, 0], [1.0, 2.0, 1]],
               "args": sys.argv[2:], "threads": os.environ.get("GUSAN_DIARIZE_THREADS"),
               "inproc": os.environ.get("GUSAN_DIARIZE_IN_PROCESS")}
    with open(os.path.join(workdir, "result.json"), "w") as f:
        json.dump(res, f)
''')


@pytest.fixture(autouse=True)
def _fresh_flags():
    diarization_proc._cancel_all.clear()
    yield
    diarization_proc._cancel_all.clear()


@pytest.fixture
def stub_worker(tmp_path, monkeypatch):
    script = tmp_path / "stub_worker.py"
    script.write_text(STUB_WORKER, encoding="utf-8")
    monkeypatch.setattr(diarization_proc, "worker_command", lambda: [sys.executable, str(script)])
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _work_folders(root):
    return [p for p in os.listdir(str(root)) if p.startswith(diarization_proc.PREFIX)]


def _audio(seconds=3.0):
    import numpy as np
    return np.zeros(int(16000 * seconds), dtype=np.float32)


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_the_child_process_is_the_default_and_one_variable_turns_it_off():
    assert diarization_proc.isolation_enabled({}) is True
    assert diarization_proc.isolation_enabled({"GUSAN_DIARIZE_IN_PROCESS": "0"}) is True
    for on in ("1", "true", "YES", "on"):
        assert diarization_proc.isolation_enabled({"GUSAN_DIARIZE_IN_PROCESS": on}) is False


def test_the_child_leaves_room_for_the_rest_of_the_computer():
    assert diarization_proc.worker_threads(2) == 2
    assert diarization_proc.worker_threads(4) == 2
    assert diarization_proc.worker_threads(8) == 4
    assert diarization_proc.worker_threads(16) == 6
    assert diarization_proc.worker_threads(64) == 6
    assert diarization_proc.worker_threads(1) == 2


def test_the_timeout_grows_with_the_audio():
    assert diarization_proc.default_timeout(60) == diarization_proc.MIN_TIMEOUT_S
    assert diarization_proc.default_timeout(3600) == 4.0 * 3600


def test_the_worker_command_for_source_and_frozen_builds(monkeypatch):
    cmd = diarization_proc.worker_command()
    assert cmd[0] == sys.executable and cmd[-1] == "--diarize-worker"
    assert os.path.basename(cmd[1]) == "diarization_proc.py"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert diarization_proc.worker_command() == [sys.executable, "--diarize-worker"]


def test_main_hands_the_worker_flag_over_before_anything_heavy_loads():
    # main.py's first lines: exactly this argv, only when run as the program.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "main.py"), encoding="utf-8") as f:
        head = f.read(2500)
    assert 'if __name__ == "__main__" and sys.argv[1:2] == ["--diarize-worker"]:' in head
    assert "diarization_proc.worker_main(sys.argv[1:])" in head
    assert head.index("--diarize-worker") < head.index("import threading")
    assert diarization_proc.WORKER_FLAG == "--diarize-worker"


def test_sweep_removes_only_old_work_folders(tmp_path):
    old = tmp_path / (diarization_proc.PREFIX + "old")
    new = tmp_path / (diarization_proc.PREFIX + "new")
    other = tmp_path / "somebody-elses"
    for d in (old, new, other):
        d.mkdir()
    past = time.time() - 7 * 3600
    os.utime(str(old), (past, past))
    os.utime(str(other), (past, past))
    assert diarization_proc.sweep_stale(root=str(tmp_path)) == 1
    assert not old.exists() and new.exists() and other.exists()


# ── the child process, with a stand-in worker ────────────────────────────────

@needs_numpy
def test_a_run_returns_the_turns_reports_progress_and_cleans_up(stub_worker):
    seen = []
    turns, err = diarization_proc.run(_audio(4.0), num_speakers=3, threshold=0.7,
                                      on_progress=lambda d, t: seen.append((d, t)), threads=2)
    assert err == ""
    assert turns == [(0.0, 4.0, 0), (1.0, 2.0, 1)]      # the worker saw the whole audio (4 s of float32)
    assert seen and seen[0] == (1, 2)
    assert _work_folders(stub_worker) == []             # no audio left behind
    assert not diarization_proc._children               # no process left registered


@needs_numpy
def test_the_child_gets_its_thread_count_and_the_in_process_flag(stub_worker, monkeypatch):
    # (the stub echoes what it was given into result.json; read it back through a spy)
    captured = {}
    real = diarization_proc._wait

    def spy(proc, workdir, *a, **k):
        out = real(proc, workdir, *a, **k)
        return out

    monkeypatch.setattr(diarization_proc, "_wait", spy)
    real_read = diarization_proc._read_json

    def reading(path):
        data = real_read(path)
        if isinstance(data, dict) and "args" in data:
            captured.update(data)
        return data

    monkeypatch.setattr(diarization_proc, "_read_json", reading)
    diarization_proc.run(_audio(), num_speakers=2, threshold=0.65, threads=3)
    assert captured["args"] == ["2", "0.65", "3"]
    assert captured["threads"] == "3"
    assert captured["inproc"] == "1"                    # the child never starts a child of its own


@needs_numpy
def test_a_crashing_child_is_an_empty_result_not_an_error(stub_worker, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "crash")
    turns, err = diarization_proc.run(_audio())
    assert turns == [] and err == "worker exited with code 3"
    assert _work_folders(stub_worker) == []


@needs_numpy
def test_a_failing_child_says_why(stub_worker, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "fail")
    assert diarization_proc.run(_audio()) == ([], "models missing")


@needs_numpy
def test_a_stuck_child_is_ended_at_the_timeout(stub_worker, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "hang")
    t0 = time.monotonic()
    turns, err = diarization_proc.run(_audio(), timeout_s=0.8)
    assert (turns, err) == ([], "timed out")
    assert time.monotonic() - t0 < 10
    assert _work_folders(stub_worker) == [] and not diarization_proc._children


@needs_numpy
def test_cancel_ends_the_child(stub_worker, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "hang")
    t0 = time.monotonic()
    answers = iter([False, False, True])
    turns, err = diarization_proc.run(_audio(), should_cancel=lambda: next(answers, True))
    assert (turns, err) == ([], "cancelled")
    assert time.monotonic() - t0 < 10 and not diarization_proc._children


@needs_numpy
def test_quitting_the_app_ends_a_running_pass_and_refuses_new_ones(stub_worker, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "hang")
    out = []
    t = threading.Thread(target=lambda: out.append(diarization_proc.run(_audio())))
    t.start()
    deadline = time.monotonic() + 15
    while not diarization_proc._children and time.monotonic() < deadline:
        time.sleep(0.05)
    assert diarization_proc._children, "the child should be running"
    diarization_proc.cancel_all()
    t.join(15)
    assert not t.is_alive()
    assert out == [([], "app quitting")]
    assert diarization_proc.run(_audio()) == ([], "app quitting")      # nothing new starts
    assert _work_folders(stub_worker) == []


def test_a_program_that_cannot_start_is_an_empty_result(tmp_path, monkeypatch):
    if not _real_numpy():
        pytest.skip("real numpy needed")
    monkeypatch.setattr(diarization_proc, "worker_command", lambda: [str(tmp_path / "no-such-program.exe")])
    turns, err = diarization_proc.run(_audio())
    assert turns == [] and err.startswith("could not start")


# ── diarization.diarize goes through the child by default ────────────────────

@needs_numpy
def test_diarize_uses_the_child_and_sets_last_error(stub_worker, monkeypatch):
    monkeypatch.setattr(diarization, "is_available", lambda: True)
    assert diarization.diarize(_audio(2.0)) == [(0.0, 2.0, 0), (1.0, 2.0, 1)]
    assert getattr(diarization, "last_error", "") == ""
    monkeypatch.setenv("STUB_MODE", "fail")
    assert diarization.diarize(_audio(2.0)) == []
    assert getattr(diarization, "last_error", "") == "models missing"


@needs_numpy
def test_diarize_in_process_flag_keeps_the_old_path_and_starts_nothing(monkeypatch):
    monkeypatch.setenv("GUSAN_DIARIZE_IN_PROCESS", "1")
    monkeypatch.setattr(diarization, "is_available", lambda: True)

    class Seg:
        def __init__(self, s, e, k):
            self.start, self.end, self.speaker = s, e, k

    class Res(list):
        def sort_by_start_time(self):
            return self

    class Fake:
        def process(self, audio, callback=None):
            if callback:
                callback(1, 1)
            return Res([Seg(0.0, 1.5, 2)])

    monkeypatch.setattr(diarization, "_build_diarizer", lambda *a, **k: Fake())
    monkeypatch.setattr(diarization, "_diarizer", None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no child expected")))
    got = []
    assert diarization.diarize(_audio(2.0), on_progress=lambda d, t: got.append((d, t))) == [(0.0, 1.5, 2)]
    assert got == [(1, 1)]


# ── the point: a call that holds the interpreter lock no longer stalls the app ──

FAKE_SHERPA = textwrap.dedent('''
    """A stand-in for sherpa_onnx whose process() keeps the interpreter lock for
    HOLD_S seconds, the way the real one does (ctypes.PyDLL does not release it)."""
    import ctypes, os, sys

    HOLD_S = float(os.environ.get("FAKE_SHERPA_HOLD_S", "2.5"))

    def _hold(seconds):
        if sys.platform == "win32":
            ctypes.PyDLL("kernel32").Sleep(int(seconds * 1000))
        else:
            ctypes.PyDLL(None).usleep(int(seconds * 1000000))

    class _Cfg:
        def __init__(self, *a, **k):
            self.k = k

    OfflineSpeakerDiarizationConfig = OfflineSpeakerSegmentationModelConfig = _Cfg
    OfflineSpeakerSegmentationPyannoteModelConfig = SpeakerEmbeddingExtractorConfig = FastClusteringConfig = _Cfg

    class _Seg:
        def __init__(self, s, e, k):
            self.start, self.end, self.speaker = s, e, k

    class _Res(list):
        def sort_by_start_time(self):
            return self

    class OfflineSpeakerDiarization:
        def __init__(self, config):
            pass

        def process(self, samples, callback=None):
            _hold(HOLD_S)
            if callback:
                callback(1, 1)
            return _Res([_Seg(0.0, 1.0, 0), _Seg(1.0, 2.0, 1)])
''')


@pytest.fixture
def fake_sherpa(tmp_path, monkeypatch):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "sherpa_onnx.py").write_text(FAKE_SHERPA, encoding="utf-8")
    data = tmp_path / "appdata"
    models = data / "models" / "diarization"
    (models / "sherpa-onnx-pyannote-segmentation-3-0").mkdir(parents=True)
    (models / "embedding.onnx").write_bytes(b"x")
    (models / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx").write_bytes(b"x")
    monkeypatch.setenv("TRANSCRIBE_APP_DATA_DIR", str(data))
    monkeypatch.setenv("TRANSCRIBE_DISABLE_KEYRING", "1")
    monkeypatch.setenv("TRANSCRIBE_SKIP_MIGRATION", "1")
    monkeypatch.setenv("PYTHONPATH", str(stubs) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("FAKE_SHERPA_HOLD_S", "2.5")
    return stubs


def _longest_gap_while(fn, slice_s=0.02):
    """Run ``fn`` on a worker thread; this (the 'GUI') thread sleeps in 20 ms
    slices and returns (result, longest lateness of a wake-up, seconds taken)."""
    box = []
    t = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    t0 = time.perf_counter()
    t.start()
    last, worst = time.perf_counter(), 0.0
    while t.is_alive():
        time.sleep(slice_s)
        now = time.perf_counter()
        worst = max(worst, now - last - slice_s)
        last = now
    t.join()
    return box[0], worst, time.perf_counter() - t0


@needs_numpy
def test_a_call_that_holds_the_interpreter_lock_stalls_the_app_in_process(fake_sherpa, monkeypatch):
    """The control: the old way. This is the freeze."""
    monkeypatch.setenv("GUSAN_DIARIZE_IN_PROCESS", "1")
    monkeypatch.syspath_prepend(str(fake_sherpa))
    monkeypatch.delitem(sys.modules, "sherpa_onnx", raising=False)
    monkeypatch.setattr(diarization, "_diarizer", None)
    monkeypatch.setattr(diarization, "is_available", lambda: True)
    turns, worst, took = _longest_gap_while(lambda: diarization.diarize(_audio(3.0)))
    assert turns == [(0.0, 1.0, 0), (1.0, 2.0, 1)]
    assert worst > 1.5, "the fake should hold the lock for ~2.5 s (got a %.2f s gap)" % worst


@needs_numpy
def test_in_its_own_process_the_same_call_does_not_stall_the_app(fake_sherpa, monkeypatch):
    """The stall budget of the fixed path: the app's other threads keep running."""
    monkeypatch.delenv("GUSAN_DIARIZE_IN_PROCESS", raising=False)
    monkeypatch.setattr(diarization, "is_available", lambda: True)
    turns, worst, took = _longest_gap_while(lambda: diarization.diarize(_audio(3.0)))
    assert turns == [(0.0, 1.0, 0), (1.0, 2.0, 1)]
    assert took >= 2.4                                   # the child really did the 2.5 s of holding
    assert worst < 1.0, "the app stalled for %.2f s while the child held ITS lock" % worst


def test_main_py_is_a_working_worker_entry_before_any_heavy_import(fake_sherpa, tmp_path, monkeypatch):
    """A frozen build starts the worker as `<the app> --diarize-worker ...`, i.e.
    main.py run as the program: it must answer from its first lines (no Qt, no
    config, no audio devices) - the app's own window never opens for it."""
    import struct
    work = tmp_path / "work"
    work.mkdir()
    (work / diarization_proc.AUDIO_FILE).write_bytes(struct.pack("<f", 0.0) * (16000 * 3))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["FAKE_SHERPA_HOLD_S"] = "0"
    env["QT_QPA_PLATFORM"] = "minimal"
    proc = subprocess.run([sys.executable, os.path.join(here, "main.py"), diarization_proc.WORKER_FLAG,
                           str(work), "0", "0.7", "2"], env=env, cwd=str(tmp_path),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    assert proc.returncode == 0, proc.stdout.decode("utf-8", "replace")[-400:]
    result = json.loads((work / diarization_proc.RESULT_FILE).read_text(encoding="utf-8"))
    assert result == {"ok": True, "turns": [[0.0, 1.0, 0], [1.0, 2.0, 1]]}
    assert json.loads((work / diarization_proc.PROGRESS_FILE).read_text(encoding="utf-8")) == {"done": 1, "total": 1}


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_on_windows_the_child_ends_with_the_app(tmp_path):
    """Closing the job (what Windows does when the app dies, a crash or "Close
    the program" included) ends the child: no speaker pass keeps burning the
    processor for half an hour after Gusan is gone."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        job = diarization_proc._bind_to_job(child)
        if job is None:
            pytest.skip("this process cannot create / join a job object here")
        assert child.poll() is None
        diarization_proc._close_job(job)
        deadline = time.monotonic() + 15
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert child.poll() is not None, "the child should have been ended with its job"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(10)
