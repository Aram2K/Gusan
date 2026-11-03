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

    class _Stream:
        def __init__(self):
            self.x = None

        def accept_waveform(self, rate, samples):
            self.x = samples

        def input_finished(self):
            pass

    class SpeakerEmbeddingExtractor:
        """A voice print that tells what it was given: [mean, standard deviation, samples, 1]."""
        dim = 4

        def __init__(self, config):
            pass

        def create_stream(self):
            return _Stream()

        def compute(self, stream):
            x = stream.x
            n = len(x)
            return [float(x.mean()) if n else 0.0, float(x.std()) if n else 0.0, float(n), 1.0]
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


# ── voice prints: one vector per stretch of speech (run_voices, the child's "voices" job) ───────────────

STUB_VOICES_WORKER = textwrap.dedent('''
    import json, os, struct, sys, time
    workdir = sys.argv[1]
    mode = os.environ.get("STUB_MODE", "ok")
    after = int(os.environ.get("STUB_AFTER", "2"))
    row_s = float(os.environ.get("STUB_ROW_S", "0.0"))
    assert sys.argv[5] == "voices"
    with open(os.path.join(workdir, "echo.json"), "w") as f:
        json.dump({"argv": sys.argv[2:], "threads": os.environ.get("GUSAN_DIARIZE_THREADS"),
                   "inproc": os.environ.get("GUSAN_DIARIZE_IN_PROCESS")}, f)
    with open(os.path.join(workdir, "units.json")) as f:
        lengths = json.load(f)
    audio_bytes = os.path.getsize(os.path.join(workdir, "audio.f32"))
    if mode == "fail":
        with open(os.path.join(workdir, "result.json"), "w") as f:
            json.dump({"ok": False, "error": "models missing"}, f)
        sys.exit(0)
    with open(os.path.join(workdir, "dim.json"), "w") as f:
        json.dump({"dim": 3}, f)

    def progress(done):
        tmp = os.path.join(workdir, "progress.json.tmp")
        try:
            with open(tmp, "w") as f:
                json.dump({"done": done, "total": len(lengths)}, f)
            os.replace(tmp, os.path.join(workdir, "progress.json"))
        except OSError:
            pass                      # (Windows: the parent has the file open this very moment; the next one will do)
    progress(0)
    with open(os.path.join(workdir, "voices.f32"), "wb") as rows:
        for i, n in enumerate(lengths):
            if mode in ("crash_after", "hang_after", "half_row") and i == after:
                if mode == "half_row":
                    rows.write(struct.pack("2f", 7.0, 7.0))          # a row the child died in the middle of
                    rows.flush()
                if mode == "crash_after":
                    sys.stderr.write("native crash simulated")
                    sys.exit(3)
                time.sleep(60)
            if mode == "short" and i == 1:
                break                                                  # ends well, with fewer rows than units
            time.sleep(row_s)
            row = (float("nan"), 1.0, 1.0) if (mode == "nan" and i == 1) else (float(i + 1), float(n), 1.0)
            rows.write(struct.pack("3f", *row))
            rows.flush()
            progress(i + 1)
    with open(os.path.join(workdir, "result.json"), "w") as f:
        json.dump({"ok": True, "dim": 3, "done": len(lengths), "bytes": audio_bytes}, f)
''')


@pytest.fixture
def stub_voices(tmp_path, monkeypatch):
    script = tmp_path / "stub_voices_worker.py"
    script.write_text(STUB_VOICES_WORKER, encoding="utf-8")
    monkeypatch.setattr(diarization_proc, "worker_command", lambda: [sys.executable, str(script)])
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _joined(*seconds):
    import numpy as np
    return np.zeros(int(16000 * sum(seconds)), dtype=np.float32), [int(16000 * s) for s in seconds]


def test_the_voice_child_leaves_room_and_waits_a_sensible_time():
    assert [diarization_proc.voice_threads(c) for c in (1, 2, 4, 8, 12, 16, 64)] == [2, 2, 2, 2, 3, 4, 4]
    assert diarization_proc.voice_timeout(60) == diarization_proc.VOICE_MIN_TIMEOUT_S == 600
    assert diarization_proc.voice_timeout(1000) == 1500.0
    assert diarization_proc.VOICE_STALL_S == 120.0
    assert diarization_proc.cancelled_for_quit() is False


@needs_numpy
def test_run_voices_gives_a_vector_per_stretch_none_for_the_empty_ones_and_reports_progress(stub_voices):
    audio, lengths = _joined(1.0, 2.0)
    lengths.insert(1, 0)                                           # a stretch with nothing to embed
    seen = []
    vectors, err = diarization_proc.run_voices(audio, lengths, on_progress=lambda d, t: seen.append((d, t)))
    assert err == ""
    assert vectors == [[1.0, 16000.0, 1.0], None, [3.0, 32000.0, 1.0]]
    assert seen and seen[-1] == (3, 3) and all(t == 3 for _d, t in seen)
    assert [d for d, _t in seen] == sorted(d for d, _t in seen)
    assert _work_folders(stub_voices) == [] and not diarization_proc._children        # nothing left behind


@needs_numpy
def test_the_voice_child_gets_its_job_its_threads_and_no_child_of_its_own(stub_voices, monkeypatch):
    echoes = []
    real = diarization_proc._read_voices

    def spy(workdir, lengths):
        echoes.append(diarization_proc._read_json(os.path.join(workdir, "echo.json")))
        return real(workdir, lengths)
    monkeypatch.setattr(diarization_proc, "_read_voices", spy)
    audio, lengths = _joined(1.0)
    diarization_proc.run_voices(audio, lengths, threads=3)
    assert echoes[0] == {"argv": ["0", "0", "3", "voices"], "threads": "3", "inproc": "1"}
    diarization_proc.run_voices(audio, lengths)
    assert echoes[1]["argv"][2] == str(diarization_proc.voice_threads())                 # the default: 2 on 8 logical processors


@pytest.mark.skipif(os.name != "nt", reason="Windows process priority")
@needs_numpy
def test_the_voice_child_runs_at_normal_priority_and_the_window_sliding_one_below_normal(stub_voices, monkeypatch):
    flags = []
    real = subprocess.Popen

    def popen(*a, **k):
        flags.append(k.get("creationflags", 0))
        return real(*a, **k)
    monkeypatch.setattr(subprocess, "Popen", popen)
    audio, lengths = _joined(1.0)
    diarization_proc.run_voices(audio, lengths)
    assert flags[0] & 0x08000000 and not flags[0] & 0x00004000       # no console; NOT below normal: it would starve
    monkeypatch.setenv("STUB_MODE", "ok")
    script = stub_voices / "stub_worker.py"
    script.write_text(STUB_WORKER, encoding="utf-8")
    monkeypatch.setattr(diarization_proc, "worker_command", lambda: [sys.executable, str(script)])
    diarization_proc.run(_audio())
    assert flags[1] & 0x08000000 and flags[1] & 0x00004000           # the sliding-window run is as it was


@needs_numpy
def test_a_stopped_pass_hands_back_the_voice_prints_it_had(stub_voices, monkeypatch):
    monkeypatch.setenv("STUB_ROW_S", "0.3")
    audio, lengths = _joined(*([1.0] * 20))
    marks = []
    vectors, err = diarization_proc.run_voices(
        audio, lengths, on_progress=lambda d, t: marks.append(d),
        should_cancel=lambda: bool(marks) and marks[-1] >= 3)
    assert err == "cancelled"
    assert 3 <= len(vectors) < 20                                   # what was done is kept: a later try goes on from it
    assert vectors[0] == [1.0, 16000.0, 1.0] and vectors[2][0] == 3.0
    assert _work_folders(stub_voices) == [] and not diarization_proc._children


@needs_numpy
def test_a_child_that_stops_making_progress_is_ended_as_stalled(stub_voices, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "hang_after")
    monkeypatch.setenv("STUB_AFTER", "2")
    audio, lengths = _joined(*([1.0] * 5))
    t0 = time.monotonic()
    # (the limit counts from the child's last sign of progress, not from its start - a loaded computer takes
    # seconds to start a child - and has a margin over the stub's own pace so a busy machine does not fail it)
    vectors, err = diarization_proc.run_voices(audio, lengths, stall_s=3.0)
    assert err == "stalled"
    assert len(vectors) == 2                                        # the two it finished are still handed back
    assert time.monotonic() - t0 < 60
    assert _work_folders(stub_voices) == [] and not diarization_proc._children


# ── the watch over the child, on a clock of its own (no child, no waiting, no load to fail on) ──────────

class _Clock:
    """Time that only moves when the loop sleeps (or a test says so)."""

    def __init__(self):
        self.now = 1000.0
        self.jumps = {}                  # number of the sleep -> extra seconds that pass in it
        self.sleeps = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds + self.jumps.get(self.sleeps, 0.0)


class _Proc:
    """A child that runs until ``clock.now`` reaches ``ends_at`` (never, when None) and then exits with 0."""

    def __init__(self, clock, ends_at=None):
        self.clock, self.ends_at, self.killed = clock, ends_at, False

    def poll(self):
        if self.killed:
            return -9
        return 0 if self.ends_at is not None and self.clock.now >= self.ends_at else None

    def kill(self):
        self.killed = True


def _watch(tmp_path, clock, proc, progress=None, **kw):
    if progress is not None:
        diarization_proc._write_json(str(tmp_path / diarization_proc.PROGRESS_FILE),
                                     {"done": progress[0], "total": progress[1]})
    args = dict(audio_sec=60.0, on_progress=None, should_cancel=None, timeout_s=10 ** 6, stall_s=None)
    args.update(kw)
    return diarization_proc._poll(proc, str(tmp_path), args.pop("audio_sec"), args.pop("on_progress"),
                                  args.pop("should_cancel"), args.pop("timeout_s"), clock=clock, sleep=clock.sleep,
                                  **args)


def test_a_child_that_makes_no_progress_for_the_stall_limit_is_ended(tmp_path):
    clock = _Clock()
    proc = _Proc(clock)
    reason, _rc = _watch(tmp_path, clock, proc, progress=(1, 5), stall_s=10.0)
    assert reason == "stalled" and proc.killed
    assert 10.0 < clock.now - 1000.0 <= 10.0 + 2 * diarization_proc.POLL_S            # about ten seconds, not before


def test_progress_keeps_resetting_the_stall_limit(tmp_path):
    clock = _Clock()
    proc = _Proc(clock, ends_at=1100.0)
    done = [0]

    def on_progress(d, t):
        pass
    orig_sleep = clock.sleep

    def sleep(seconds):
        orig_sleep(seconds)
        if clock.sleeps % 20 == 0:                       # a new voice print every five seconds
            done[0] += 1
            diarization_proc._write_json(str(tmp_path / diarization_proc.PROGRESS_FILE), {"done": done[0], "total": 99})
    clock.sleep = sleep
    reason, rc = diarization_proc._poll(proc, str(tmp_path), 60.0, on_progress, None, 10 ** 6, stall_s=10.0,
                                        clock=clock, sleep=clock.sleep)
    assert (reason, rc) == ("", 0)                       # it ended by itself after 100 s, never "stalled"


def test_the_first_sign_of_life_has_its_own_longer_limit_the_model_loads_first(tmp_path):
    clock = _Clock()
    seen = []
    reason, _rc = _watch(tmp_path, clock, _Proc(clock), stall_s=10.0, start_s=100.0,
                         should_cancel=lambda: seen.append(clock.now) or clock.now - 1000.0 > 60.0)
    assert reason == "cancelled"                         # not "stalled" at ten seconds: nothing was due yet
    clock = _Clock()
    reason, _rc = _watch(tmp_path, clock, _Proc(clock), stall_s=10.0, start_s=100.0)
    assert reason == "stalled" and 100.0 < clock.now - 1000.0 <= 100.0 + 2 * diarization_proc.POLL_S
    assert clock.now - 1000.0 > 100.0
    clock = _Clock()                                     # (no start limit: the stall limit counts from the start)
    reason, _rc = _watch(tmp_path, clock, _Proc(clock), stall_s=10.0)
    assert reason == "stalled" and clock.now - 1000.0 < 20.0


def test_the_overall_limit_ends_a_child_that_keeps_going(tmp_path):
    clock = _Clock()
    proc = _Proc(clock)
    reason, _rc = _watch(tmp_path, clock, proc, timeout_s=30.0)
    assert reason == "timed out" and proc.killed and 30.0 < clock.now - 1000.0 <= 31.0


def test_a_computer_that_slept_does_not_end_a_healthy_child(tmp_path):
    # The lid closes for half an hour: the clock keeps counting through sleep, the child did nothing meanwhile.
    clock = _Clock()
    clock.jumps = {8: 1800.0}
    proc = _Proc(clock, ends_at=1000.0 + 1800.0 + 5.0)
    reason, rc = _watch(tmp_path, clock, proc, progress=(3, 9), stall_s=10.0, timeout_s=600.0)
    assert (reason, rc) == ("", 0), reason               # it was not "stalled", not "timed out" - it finished
    assert not proc.killed


def test_a_real_stall_after_a_sleep_is_still_found_and_a_real_timeout_too(tmp_path):
    clock = _Clock()
    clock.jumps = {4: 3600.0}
    proc = _Proc(clock)
    reason, _rc = _watch(tmp_path, clock, proc, progress=(3, 9), stall_s=10.0)
    assert reason == "stalled"
    assert clock.now - 1000.0 > 3600.0                    # (it went on to count the time that really passed)
    clock = _Clock()
    clock.jumps = {4: 3600.0}
    reason, _rc = _watch(tmp_path, clock, _Proc(clock), timeout_s=30.0)
    assert reason == "timed out"


def test_a_short_gap_between_two_looks_is_not_a_sleep(tmp_path):
    clock = _Clock()
    clock.jumps = {2: diarization_proc.SLEEP_JUMP_S - 1.0}   # a computer that was only busy for a few seconds
    proc = _Proc(clock)
    reason, _rc = _watch(tmp_path, clock, proc, progress=(3, 9), stall_s=10.0)
    assert reason == "stalled"
    assert clock.now - 1000.0 < 10.0 + diarization_proc.SLEEP_JUMP_S                    # the gap counted in full


@needs_numpy
def test_a_stall_limit_of_none_lets_a_slow_child_finish(stub_voices, monkeypatch):
    monkeypatch.setenv("STUB_ROW_S", "0.4")
    audio, lengths = _joined(*([1.0] * 3))
    vectors, err = diarization_proc.run_voices(audio, lengths, stall_s=None)
    assert (err, len(vectors)) == ("", 3)


@needs_numpy
def test_a_voice_pass_that_takes_too_long_is_ended_with_what_it_made(stub_voices, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "hang_after")
    monkeypatch.setenv("STUB_AFTER", "1")
    audio, lengths = _joined(*([1.0] * 4))
    # (the clock starts when the child does; the margin is for a computer that takes seconds to start one)
    vectors, err = diarization_proc.run_voices(audio, lengths, timeout_s=10.0, stall_s=None)
    assert err == "timed out" and len(vectors) == 1


@needs_numpy
def test_a_child_that_crashes_leaves_its_prefix_and_a_reason(stub_voices, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "crash_after")
    monkeypatch.setenv("STUB_AFTER", "2")
    audio, lengths = _joined(*([1.0] * 4))
    vectors, err = diarization_proc.run_voices(audio, lengths)
    assert err == "worker exited with code 3"
    assert len(vectors) == 2


@needs_numpy
def test_a_child_that_fails_before_any_voice_print_says_why(stub_voices, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "fail")
    audio, lengths = _joined(1.0, 1.0)
    assert diarization_proc.run_voices(audio, lengths) == ([], "models missing")


@needs_numpy
def test_a_child_that_says_it_is_done_with_fewer_voice_prints_than_asked_is_incomplete(stub_voices, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "short")
    audio, lengths = _joined(*([1.0] * 3))
    vectors, err = diarization_proc.run_voices(audio, lengths)
    assert err == "incomplete" and len(vectors) == 1


@needs_numpy
def test_voice_prints_that_are_not_numbers_become_none_and_a_half_written_row_is_ignored(stub_voices, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "nan")
    audio, lengths = _joined(1.0, 1.0, 1.0)
    vectors, err = diarization_proc.run_voices(audio, lengths)
    assert err == "" and vectors[0] == [1.0, 16000.0, 1.0] and vectors[1] is None and vectors[2][0] == 3.0
    monkeypatch.setenv("STUB_MODE", "half_row")
    monkeypatch.setenv("STUB_AFTER", "1")
    vectors, err = diarization_proc.run_voices(audio, lengths, stall_s=3.0)
    assert len(vectors) == 1                                        # the half row at the end is not a voice print


@needs_numpy
def test_quitting_the_app_ends_a_voice_pass_and_keeps_what_it_made(stub_voices, monkeypatch):
    monkeypatch.setenv("STUB_ROW_S", "0.2")
    audio, lengths = _joined(*([1.0] * 20))
    out = []
    marks = []
    t = threading.Thread(target=lambda: out.append(diarization_proc.run_voices(
        audio, lengths, on_progress=lambda d, n: marks.append(d))))
    t.start()
    deadline = time.monotonic() + 20
    while (not marks or marks[-1] < 2) and time.monotonic() < deadline:
        time.sleep(0.05)
    diarization_proc.cancel_all()
    t.join(20)
    assert not t.is_alive()
    vectors, err = out[0]
    assert err == "app quitting" and 2 <= len(vectors) < 20
    assert diarization_proc.cancelled_for_quit() is True
    assert diarization_proc.run_voices(audio, lengths) == ([], "app quitting")      # nothing new starts
    assert _work_folders(stub_voices) == []


@needs_numpy
def test_a_voice_pass_that_cannot_start_is_an_empty_result(tmp_path, monkeypatch):
    monkeypatch.setattr(diarization_proc, "worker_command", lambda: [str(tmp_path / "no-such-program.exe")])
    audio, lengths = _joined(1.0)
    vectors, err = diarization_proc.run_voices(audio, lengths)
    assert vectors == [] and err.startswith("could not start")


def test_read_voices_ignores_what_is_not_there_or_half_there(tmp_path):
    import struct
    assert diarization_proc._read_voices(str(tmp_path), [16000]) == []              # no dim.json
    if not _real_numpy():
        return
    (tmp_path / diarization_proc.DIM_FILE).write_text(json.dumps({"dim": 2}), encoding="utf-8")
    assert diarization_proc._read_voices(str(tmp_path), [16000]) == []              # no rows yet
    (tmp_path / diarization_proc.VOICES_FILE).write_bytes(struct.pack("2f", 1.0, 2.0) + struct.pack("2f", 3.0, 4.0)
                                                          + struct.pack("1f", 5.0))
    assert diarization_proc._read_voices(str(tmp_path), [16000, 0, 16000]) == [[1.0, 2.0], None]
    assert diarization_proc._read_voices(str(tmp_path), [16000]) == [[1.0, 2.0]]    # never more than were asked for
    (tmp_path / diarization_proc.DIM_FILE).write_text("{broken", encoding="utf-8")
    assert diarization_proc._read_voices(str(tmp_path), [16000]) == []


# ── the child's own side, with a stand-in for the speaker model ──────────────────────────────────────

def _fake_sherpa_module():
    import types
    mod = types.ModuleType("sherpa_onnx")
    exec(compile(FAKE_SHERPA, "fake_sherpa.py", "exec"), mod.__dict__)
    return mod


@needs_numpy
def test_the_voices_worker_writes_each_voice_print_as_it_goes(tmp_path, monkeypatch):
    import numpy as np
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _fake_sherpa_module())
    monkeypatch.setenv(diarization_proc.ENV_IN_PROCESS, "1")       # (the worker sets these for itself: undone after)
    monkeypatch.setenv(diarization_proc.ENV_THREADS, "2")
    work = tmp_path / "work"
    work.mkdir()
    audio = np.concatenate([np.full(16000, 0.25, np.float32), np.full(32000, 0.5, np.float32)])
    audio.tofile(str(work / diarization_proc.AUDIO_FILE))
    (work / diarization_proc.UNITS_FILE).write_text(json.dumps([16000, 0, 32000]), encoding="utf-8")
    assert diarization_proc.worker_main([str(work), "0", "0", "2", "voices"]) == 0
    result = json.loads((work / diarization_proc.RESULT_FILE).read_text(encoding="utf-8"))
    assert result == {"ok": True, "dim": 4, "done": 3}
    assert json.loads((work / diarization_proc.DIM_FILE).read_text(encoding="utf-8")) == {"dim": 4}
    assert json.loads((work / diarization_proc.PROGRESS_FILE).read_text(encoding="utf-8")) == {"done": 3, "total": 3}
    rows = np.fromfile(str(work / diarization_proc.VOICES_FILE), dtype=np.float32).reshape(-1, 4)
    assert rows.shape == (3, 4)
    assert rows[0].tolist() == [0.25, 0.0, 16000.0, 1.0]            # [mean, std, samples, 1] of its own stretch only
    assert rows[1].tolist() == [0.0, 0.0, 0.0, 0.0]                 # nothing to embed: zeros, no work
    assert rows[2].tolist() == [0.5, 0.0, 32000.0, 1.0]


@needs_numpy
def test_the_voices_worker_says_why_when_it_cannot_work(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _fake_sherpa_module())
    monkeypatch.setenv(diarization_proc.ENV_IN_PROCESS, "1")
    monkeypatch.setenv(diarization_proc.ENV_THREADS, "2")
    assert diarization_proc.worker_main([str(work), "0", "0", "2", "voices"]) == 0     # no audio file: still a clean end
    result = json.loads((work / diarization_proc.RESULT_FILE).read_text(encoding="utf-8"))
    assert result["ok"] is False and result["error"]
    assert diarization_proc.worker_main([str(work), "0", "0"]) == 2                    # bad command line: as before


def test_main_py_hands_the_voices_job_to_the_worker_too(fake_sherpa, tmp_path):
    """The frozen app starts `<the app> --diarize-worker <folder> 0 0 2 voices`: main.py answers from its first lines."""
    import struct
    work = tmp_path / "work"
    work.mkdir()
    (work / diarization_proc.AUDIO_FILE).write_bytes(struct.pack("<f", 0.5) * 32000)
    (work / diarization_proc.UNITS_FILE).write_text(json.dumps([16000, 16000]), encoding="utf-8")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "minimal"
    proc = subprocess.run([sys.executable, os.path.join(here, "main.py"), diarization_proc.WORKER_FLAG,
                           str(work), "0", "0", "2", "voices"], env=env, cwd=str(tmp_path),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    assert proc.returncode == 0, proc.stdout.decode("utf-8", "replace")[-400:]
    result = json.loads((work / diarization_proc.RESULT_FILE).read_text(encoding="utf-8"))
    assert result == {"ok": True, "dim": 4, "done": 2}
    assert (work / diarization_proc.VOICES_FILE).stat().st_size == 2 * 4 * 4


@needs_numpy
def test_embed_spans_goes_through_the_child_and_slices_the_right_stretches(fake_sherpa, monkeypatch):
    import numpy as np
    monkeypatch.delenv("GUSAN_DIARIZE_IN_PROCESS", raising=False)
    monkeypatch.setattr(diarization, "is_available", lambda: True)
    audio = np.concatenate([np.full(32000, 0.1, np.float32), np.full(32000, 0.2, np.float32),
                            np.zeros(32000, np.float32), np.full(8000, 0.3, np.float32)])
    spans = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 6.5), (7.0, 9.0)]        # speech, speech, silence, too short, past the end
    marks = []
    vectors, err = diarization.embed_spans(audio, spans, on_progress=lambda d, t: marks.append((d, t)))
    assert err == "" and diarization.last_error == ""
    assert [v is None for v in vectors] == [False, False, True, True, True]
    assert round(vectors[0][0], 3) == 0.1 and vectors[0][2] == 32000.0
    assert round(vectors[1][0], 3) == 0.2 and vectors[1][2] == 32000.0
    assert marks[-1] == (5, 5)                                                   # the silent ones count as done


@needs_numpy
def test_embed_spans_reads_int16_sound_as_the_same_voice_as_float(fake_sherpa, monkeypatch):
    import numpy as np
    monkeypatch.delenv("GUSAN_DIARIZE_IN_PROCESS", raising=False)
    monkeypatch.setattr(diarization, "is_available", lambda: True)
    as_float = np.full(32000, 0.5, np.float32)
    as_int16 = np.rint(as_float * 32767).astype(np.int16)
    a, _ = diarization.embed_spans(as_float, [(0.0, 2.0)])
    b, _ = diarization.embed_spans(as_int16, [(0.0, 2.0)])
    assert abs(a[0][0] - b[0][0]) < 1e-3
