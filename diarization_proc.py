"""Speaker diarization in a child process, so the window never freezes.

Why: sherpa-onnx's ``OfflineSpeakerDiarization.process()`` keeps Python's
interpreter lock for the WHOLE run. In the app that froze every thread -
the window included - for as long as it took: measured 2 min for 3 minutes of
audio (a 45 minute meeting: well over 15 minutes), which is exactly Windows'
"Gusan is not responding" after a long Live Assistant session. Threads cannot
fix it (the lock is held inside the native call); a callback only lets the
lock go for a moment between steps. A separate process has its own lock, so the
app just waits for a file to appear.

How: ``run(audio, ...)`` writes the audio to a private temp folder, starts this
program again as ``--diarize-worker <folder> ...`` (main.py hands that argument
to ``worker_main`` before importing anything heavy; a source checkout runs this
file), and waits - polling the child's progress file, honouring cancel, a
timeout and the app quitting. The child runs at a lower priority with fewer
threads than the machine has, so the computer stays usable meanwhile, and on
Windows it is bound to a job object that ends it when the app does (a crash
included). Nothing but numbers travels back (speaker turns).

Any failure - the child can't start, crashes, times out - returns ``[]`` with
the reason: callers already treat "no turns" as "keep the plain transcript".
Never raises. GUSAN_DIARIZE_IN_PROCESS=1 runs it in the app's own process
again (tests, debugging).

The same child also makes voice prints (``run_voices``; the 5th argument "voices"): one
vector per stretch of speech, for the voice-print engine of the speaker pass after a
meeting's notes (voice_prints.py, meeting_speakers.name_the_call). It differs from the
sliding-window run in what the pass needs to be quick and honest:

  * it runs at NORMAL priority with 2 threads (``voice_threads``): below-normal priority was
    starved 8 times over on a busy computer, and the pass only starts while nothing is
    recording or being dictated (speaker_followup) and is ended at once when that changes;
  * the child writes each finished voice print to ``voices.f32`` the moment it has it and a
    progress file after every one, so the parent reports real progress, keeps what was done
    when the pass is stopped (a later try goes on from there) and notices a child that made
    no progress for ``stall_s`` seconds (hung, or starved): it is ended and the pass says
    "stalled" instead of waiting for ever. The stall limit counts from the child's last sign of
    progress (until its first one - the speaker model loads - ``VOICE_START_S`` is the limit), and a
    computer that sleeps mid-pass does not end a healthy child: the clocks keep counting through
    sleep, so a gap of more than SLEEP_JUMP_S between two looks is taken off every limit.

Qt-free; numpy is imported only where audio is written or read.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

logger = logging.getLogger("transcribe")

WORKER_FLAG = "--diarize-worker"
ENV_IN_PROCESS = "GUSAN_DIARIZE_IN_PROCESS"      # "1": run in this process (no child)
ENV_THREADS = "GUSAN_DIARIZE_THREADS"            # read by diarization._build_diarizer
PREFIX = "gusan-diar-"
AUDIO_FILE = "audio.f32"
PROGRESS_FILE = "progress.json"
RESULT_FILE = "result.json"
ERR_FILE = "worker.log"
UNITS_FILE = "units.json"         # voices: how many samples each stretch took (0 = nothing to embed)
VOICES_FILE = "voices.f32"        # voices: one row of float32 per finished stretch, written as they come
DIM_FILE = "dim.json"             # voices: the length of a row
VOICES_MODE = "voices"            # the 5th argument of the child's command line
POLL_S = 0.25
MIN_TIMEOUT_S = 15 * 60
TIMEOUT_PER_AUDIO_S = 4.0         # the child may take this many seconds per second of audio
STALE_S = 6 * 3600                # leftover folders (a killed app) older than this are removed
VOICE_MIN_TIMEOUT_S = 10 * 60     # voices: at least this long ...
VOICE_TIMEOUT_PER_AUDIO_S = 1.5   # ... or this many seconds per second of audio it is given
VOICE_STALL_S = 120.0             # voices: no progress for this long means it is hung or starved
VOICE_START_S = 120.0             # voices: the child's first sign of life (the speaker model loads) may take this long
SLEEP_JUMP_S = 10.0               # two looks this far apart: the computer slept (a look is every POLL_S)

_lock = threading.Lock()
_children = set()                 # live subprocess.Popen objects
_cancel_all = threading.Event()   # the app is quitting


def isolation_enabled(environ=None):
    """True: diarize() hands the work to a child process."""
    environ = os.environ if environ is None else environ
    return str(environ.get(ENV_IN_PROCESS, "")).strip().lower() not in ("1", "true", "yes", "on")


def worker_threads(cpu=None):
    """Threads for the child: half the logical processors (2..6), so the
    window, the recorder and the rest of the computer keep room."""
    cpu = (os.cpu_count() or 4) if cpu is None else cpu
    return max(2, min(6, cpu // 2))


def voice_threads(cpu=None):
    """Threads for the voice-print child: a quarter of the logical processors (2..4). Measured: four
    threads are only 1.3 times quicker than two for 1.6 times the processor time."""
    cpu = (os.cpu_count() or 4) if cpu is None else cpu
    return max(2, min(4, cpu // 4))


def voice_timeout(audio_sec):
    return max(VOICE_MIN_TIMEOUT_S, VOICE_TIMEOUT_PER_AUDIO_S * float(audio_sec or 0))


def cancelled_for_quit():
    """The app is quitting: nothing new starts and what runs ends."""
    return _cancel_all.is_set()


def worker_command():
    """The program line that starts a worker (without its arguments)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_FLAG]
    return [sys.executable, os.path.abspath(__file__), WORKER_FLAG]


def default_timeout(audio_sec):
    return max(MIN_TIMEOUT_S, TIMEOUT_PER_AUDIO_S * float(audio_sec or 0))


def cancel_all():
    """The app is quitting: end every running child now, and refuse new ones."""
    _cancel_all.set()
    with _lock:
        kids = list(_children)
    for p in kids:
        _kill(p)


def _kill(proc):
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def _remove_folder(path):
    """Delete a work folder (the audio in it is the user's). Windows may hold
    a file for a moment after its process ended: a few tries."""
    for attempt in range(6):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.1 * (attempt + 1))
    logger.warning("Could not remove the speaker pass folder %s", os.path.basename(path))


def sweep_stale(max_age_s=STALE_S, root=None, now=None):
    """Remove work folders an app that was killed left behind. Returns how many."""
    root = root or tempfile.gettempdir()
    now = time.time() if now is None else now
    removed = 0
    try:
        names = [n for n in os.listdir(root) if n.startswith(PREFIX)]
    except OSError:
        return 0
    for n in names:
        path = os.path.join(root, n)
        try:
            if now - os.path.getmtime(path) >= max_age_s:
                _remove_folder(path)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Removed %d speaker pass folder(s) a closed program left behind", removed)
    return removed


# ── the app's side ───────────────────────────────────────────────────────────

def _in_child(audio, args, threads, wait, finish=None, low_priority=True, extra_json=None):
    """The part every job shares: ``audio`` goes to a private temp folder, this program starts again
    as ``--diarize-worker <folder> <args...>`` (bound to a job object on Windows), ``wait(proc,
    workdir)`` waits for it and returns ``(payload, error)``, and whatever happens the child is
    gone and the folder removed at the end. ``finish(workdir, payload, error)`` runs after the
    child is gone and before its folder is, and may change the answer (the voice prints it already
    wrote are read there, even when it was stopped). Never raises."""
    if _cancel_all.is_set():
        return [], "app quitting"
    workdir = None
    proc = None
    job = None
    payload, error = [], "failed"
    try:
        import numpy as np
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        try:
            sweep_stale()
        except Exception:
            pass
        workdir = tempfile.mkdtemp(prefix=PREFIX)
        audio.tofile(os.path.join(workdir, AUDIO_FILE))
        for name, data in (extra_json or {}).items():
            with open(os.path.join(workdir, name), "w", encoding="utf-8") as f:
                json.dump(data, f)
        cmd = worker_command() + [workdir] + [str(a) for a in args]
        env = dict(os.environ)
        env[ENV_IN_PROCESS] = "1"
        env[ENV_THREADS] = str(threads)
        kw = {}
        if os.name == "nt":
            # no console window; below-normal priority unless the job must not be starved
            kw["creationflags"] = 0x08000000 | (0x00004000 if low_priority else 0)
        err = open(os.path.join(workdir, ERR_FILE), "wb")
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=err, env=env, close_fds=True, **kw)
        finally:
            err.close()
        with _lock:
            _children.add(proc)
        job = _bind_to_job(proc)
        payload, error = wait(proc, workdir)
    except Exception as e:
        logger.warning("Speaker pass could not run in its own process: %s", e)
        payload, error = [], "could not start: %s" % (str(e)[:120] or type(e).__name__)
    finally:
        if proc is not None:
            _kill(proc)
            try:
                proc.wait(timeout=10)       # it must be gone before its folder can be
            except Exception:
                pass
            with _lock:
                _children.discard(proc)
        _close_job(job)
        if finish is not None and workdir:
            try:
                payload, error = finish(workdir, payload, error)
            except Exception:
                logger.debug("Speaker pass: reading the child's work failed", exc_info=True)
        if workdir:
            _remove_folder(workdir)
    return payload, error


def run(audio, num_speakers=None, threshold=0.7, on_progress=None, should_cancel=None,
        timeout_s=None, threads=None, sample_rate=16000):
    """Diarize 16 kHz mono float32 ``audio`` in a child process.
    Returns ``(turns, error)``: ``turns`` is ``[(start_s, end_s, speaker_id)]``
    (``[]`` on failure), ``error`` is "" on success else a short reason."""
    threads = int(threads or worker_threads())
    audio_sec = len(audio) / float(sample_rate)
    limit = default_timeout(audio_sec) if timeout_s is None else float(timeout_s)
    return _in_child(
        audio, [int(num_speakers or 0), repr(float(threshold)), threads], threads,
        lambda proc, workdir: _wait(proc, workdir, audio_sec, on_progress, should_cancel, limit))


def run_voices(audio, lengths, on_progress=None, should_cancel=None, timeout_s=None, threads=None,
               stall_s=VOICE_STALL_S, sample_rate=16000):
    """One voice print per stretch of speech, made in a child process (see the module notes).
    ``audio``: the stretches joined, 16 kHz mono float32; ``lengths``: how many samples each took
    (0 = nothing to embed: it gets no voice print). Returns ``(vectors, error)``: ``vectors`` has an
    entry per stretch - a list of floats, or None for a length of 0 - and ``error`` is "" when all
    are done. On an error (cancelled, app quitting, timed out, stalled, a crash) ``vectors`` holds
    the stretches that were finished before it (a prefix: the child wrote each one as it had it).
    ``on_progress(done, total)`` counts stretches. ``stall_s``: seconds without any progress after
    which the child is ended as "stalled" (None or 0: never)."""
    lengths = [max(0, int(n)) for n in lengths]
    threads = int(threads or voice_threads())
    audio_sec = sum(lengths) / float(sample_rate)
    limit = voice_timeout(audio_sec) if timeout_s is None else float(timeout_s)

    def wait(proc, workdir):
        reason, rc = _poll(proc, workdir, audio_sec, on_progress, should_cancel, limit, stall_s=stall_s,
                           start_s=max(VOICE_START_S, float(stall_s or 0)))
        if reason:
            return [], reason
        return [], _voices_error(workdir, rc)

    def finish(workdir, payload, error):
        vectors = _read_voices(workdir, lengths)
        if not error and len(vectors) < len(lengths):
            error = "incomplete"
        return vectors, error
    return _in_child(audio, [0, "0", threads, VOICES_MODE], threads, wait, finish,
                     low_priority=False, extra_json={UNITS_FILE: lengths})


def _poll(proc, workdir, audio_sec, on_progress, should_cancel, timeout_s, stall_s=None, start_s=None,
          clock=time.monotonic, sleep=time.sleep):
    """Wait for the child: ("", exit code) when it ended on its own, else (why it was ended,
    exit code): "app quitting", "cancelled", "timed out" or, with ``stall_s``, "stalled".

    ``stall_s`` counts from the child's last sign of progress; until its first one (the speaker model
    loads) ``start_s`` is the limit instead (default: ``stall_s``). A computer that sleeps mid-pass
    must not end a healthy child: the clock keeps counting through sleep (Windows' monotonic clock does),
    so a gap between two looks of more than SLEEP_JUMP_S is taken off every limit.
    ``clock`` and ``sleep`` are the time (tests stand fakes in)."""
    started = clock()
    changed_at = started
    last_look = started
    last_seen = None
    while True:
        rc = proc.poll()
        now = clock()
        if now - last_look > SLEEP_JUMP_S:
            gap = now - last_look - POLL_S
            started += gap
            changed_at += gap
            logger.info("Speaker pass: the computer was asleep for about %.0f s; its limits wait for it", gap)
        last_look = now
        if on_progress is not None or stall_s:
            seen = _report_progress(workdir, last_seen, on_progress)
            if seen != last_seen:
                last_seen, changed_at = seen, now
        if _cancel_all.is_set():            # (cancel_all kills the child itself: rc is set then too)
            _kill(proc)
            return "app quitting", rc
        if rc is not None:
            return "", rc
        if should_cancel is not None:
            try:
                if should_cancel():
                    _kill(proc)
                    return "cancelled", rc
            except Exception:
                pass
        if now - started > timeout_s:
            _kill(proc)
            logger.warning("Speaker pass gave up after %.0f s (%.0f s of audio)", timeout_s, audio_sec)
            return "timed out", rc
        if stall_s:
            limit = stall_s if (last_seen is not None or start_s is None) else max(start_s, stall_s)
            if now - changed_at > limit:
                _kill(proc)
                logger.warning("Speaker pass made no progress for %.0f s (%.0f s of audio): ended", limit, audio_sec)
                return "stalled", rc
        sleep(POLL_S)


def _wait(proc, workdir, audio_sec, on_progress, should_cancel, timeout_s):
    reason, rc = _poll(proc, workdir, audio_sec, on_progress, should_cancel, timeout_s)
    if reason:
        return [], reason
    result = _read_json(os.path.join(workdir, RESULT_FILE))
    if not isinstance(result, dict):
        tail = _tail(os.path.join(workdir, ERR_FILE))
        logger.warning("Speaker pass ended without a result (exit code %s)%s", rc,
                       (": " + tail) if tail else "")
        return [], "worker exited with code %s" % rc
    if not result.get("ok"):
        return [], str(result.get("error") or "failed")[:200]
    turns = []
    for t in result.get("turns") or []:
        try:
            turns.append((float(t[0]), float(t[1]), int(t[2])))
        except (TypeError, ValueError, IndexError):
            continue
    return turns, ""


def _voices_error(workdir, rc):
    """"" when the voice-print child ended with an ok result, else why not."""
    result = _read_json(os.path.join(workdir, RESULT_FILE))
    if not isinstance(result, dict):
        tail = _tail(os.path.join(workdir, ERR_FILE))
        logger.warning("Speaker pass ended without a result (exit code %s)%s", rc,
                       (": " + tail) if tail else "")
        return "worker exited with code %s" % rc
    if not result.get("ok"):
        return str(result.get("error") or "failed")[:200]
    return ""


def _read_voices(workdir, lengths):
    """The voice prints the child has written so far (a row per finished stretch; None where the
    stretch had nothing to embed or its numbers are not usable). A half-written last row is ignored."""
    meta = _read_json(os.path.join(workdir, DIM_FILE))
    try:
        dim = int(meta.get("dim")) if isinstance(meta, dict) else 0
    except (TypeError, ValueError):
        dim = 0
    if dim <= 0:
        return []
    try:
        import numpy as np
        data = np.fromfile(os.path.join(workdir, VOICES_FILE), dtype=np.float32)
    except Exception:
        return []
    out = []
    for i in range(min(len(lengths), len(data) // dim)):
        row = data[i * dim:(i + 1) * dim]
        out.append(row.tolist() if lengths[i] > 0 and bool(np.isfinite(row).all()) else None)
    return out


def _report_progress(workdir, last_seen, on_progress):
    data = _read_json(os.path.join(workdir, PROGRESS_FILE))
    if not isinstance(data, dict):
        return last_seen
    mark = (data.get("done"), data.get("total"))
    if mark != last_seen and None not in mark:
        if on_progress is not None:
            try:
                on_progress(int(mark[0]), max(1, int(mark[1])))
            except Exception:
                pass
        return mark
    return last_seen


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _tail(path, n=300):
    """The last ``n`` bytes of the worker's own log (library error text only)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace").strip().replace("\r", "")
    except OSError:
        return ""


# ── Windows: the child ends with the app (a crash included) ──────────────────

def _bind_to_job(proc):
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = Extended()
        info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return None
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):
            k32.CloseHandle(job)
            return None
        return (k32, job)
    except Exception:
        return None


def _close_job(job):
    if not job:
        return
    try:
        k32, handle = job
        k32.CloseHandle(handle)
    except Exception:
        pass


# ── the child's side ─────────────────────────────────────────────────────────

def _voices_main(workdir, threads):
    """The voice-print job (``... <threads> voices``): reads <folder>/audio.f32 (the stretches joined)
    and <folder>/units.json (the samples of each), writes <folder>/dim.json, then one row of
    voices.f32 per stretch the moment it has it, progress.json after every one (at least every half
    second) and result.json at the end. A length of 0 is a stretch with nothing to embed: a row of
    zeros, no work."""
    out = {"ok": False, "error": "worker failed"}
    try:
        import numpy as np
        audio = np.fromfile(os.path.join(workdir, AUDIO_FILE), dtype=np.float32)
        with open(os.path.join(workdir, UNITS_FILE), "r", encoding="utf-8") as f:
            lengths = [max(0, int(n)) for n in json.load(f)]
        import diarization
        extractor = diarization.build_extractor(threads)
        dim = int(extractor.dim)
        _write_json(os.path.join(workdir, DIM_FILE), {"dim": dim})
        total = len(lengths)
        _write_json(os.path.join(workdir, PROGRESS_FILE), {"done": 0, "total": total})
        last = time.monotonic()
        pos = 0
        with open(os.path.join(workdir, VOICES_FILE), "wb") as rows:
            for i, n in enumerate(lengths):
                row = np.zeros(dim, dtype=np.float32)
                if n > 0:
                    vec = np.asarray(diarization.voice_print(extractor, audio[pos:pos + n]), dtype=np.float32)
                    pos += n
                    if vec.shape == (dim,):
                        row = vec
                rows.write(row.tobytes())
                rows.flush()
                now = time.monotonic()
                if now - last >= 0.5 or i + 1 >= total:
                    last = now
                    _write_json(os.path.join(workdir, PROGRESS_FILE), {"done": i + 1, "total": total})
        out = {"ok": True, "dim": dim, "done": total}
    except BaseException as e:      # noqa: BLE001 - the parent only needs the reason
        out = {"ok": False, "error": (str(e)[:160] or type(e).__name__)}
    _write_json(os.path.join(workdir, RESULT_FILE), out)
    return 0


def worker_main(argv=None):
    """``--diarize-worker <folder> <num_speakers> <threshold> <threads> [voices]``:
    reads <folder>/audio.f32, writes <folder>/progress.json while it works and
    <folder>/result.json at the end. Returns the exit code. With the 5th argument
    "voices" it makes voice prints instead (_voices_main)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == WORKER_FLAG:
        argv = argv[1:]
    try:
        workdir, num_speakers, threshold, threads = argv[0], int(argv[1]), float(argv[2]), int(argv[3])
    except (IndexError, ValueError):
        return 2
    os.environ[ENV_IN_PROCESS] = "1"
    os.environ[ENV_THREADS] = str(threads)
    if len(argv) > 4 and argv[4] == VOICES_MODE:
        return _voices_main(workdir, threads)         # (normal priority: see the module notes)
    if os.name != "nt":
        try:
            os.nice(10)
        except OSError:
            pass
    out = {"ok": False, "error": "worker failed"}
    try:
        import numpy as np
        audio = np.fromfile(os.path.join(workdir, AUDIO_FILE), dtype=np.float32)
        import diarization
        last = [0.0]

        def progress(done, total):
            now = time.monotonic()
            if now - last[0] >= 0.5 or done >= total:
                last[0] = now
                _write_json(os.path.join(workdir, PROGRESS_FILE), {"done": int(done), "total": int(total)})

        turns = diarization.diarize(audio, num_speakers=num_speakers or None, threshold=threshold,
                                    on_progress=progress)
        error = str(getattr(diarization, "last_error", "") or "")
        if turns:
            out = {"ok": True, "turns": [[float(s), float(e), int(k)] for s, e, k in turns]}
        else:
            out = {"ok": False, "error": error or "no speaker turns"}
    except BaseException as e:      # noqa: BLE001 - the parent only needs the reason
        out = {"ok": False, "error": (str(e)[:160] or type(e).__name__)}
    _write_json(os.path.join(workdir, RESULT_FILE), out)
    return 0


def _write_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(worker_main(sys.argv[1:]))
