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
POLL_S = 0.25
MIN_TIMEOUT_S = 15 * 60
TIMEOUT_PER_AUDIO_S = 4.0         # the child may take this many seconds per second of audio
STALE_S = 6 * 3600                # leftover folders (a killed app) older than this are removed

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

def run(audio, num_speakers=None, threshold=0.7, on_progress=None, should_cancel=None,
        timeout_s=None, threads=None, sample_rate=16000):
    """Diarize 16 kHz mono float32 ``audio`` in a child process.
    Returns ``(turns, error)``: ``turns`` is ``[(start_s, end_s, speaker_id)]``
    (``[]`` on failure), ``error`` is "" on success else a short reason."""
    if _cancel_all.is_set():
        return [], "app quitting"
    workdir = None
    proc = None
    job = None
    try:
        import numpy as np
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        audio_sec = len(audio) / float(sample_rate)
        try:
            sweep_stale()
        except Exception:
            pass
        workdir = tempfile.mkdtemp(prefix=PREFIX)
        audio.tofile(os.path.join(workdir, AUDIO_FILE))
        threads = int(threads or worker_threads())
        cmd = worker_command() + [workdir, str(int(num_speakers or 0)), repr(float(threshold)), str(threads)]
        env = dict(os.environ)
        env[ENV_IN_PROCESS] = "1"
        env[ENV_THREADS] = str(threads)
        kw = {}
        if os.name == "nt":
            # no console window; below-normal priority (the window stays smooth)
            kw["creationflags"] = 0x08000000 | 0x00004000
        err = open(os.path.join(workdir, ERR_FILE), "wb")
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=err, env=env, close_fds=True, **kw)
        finally:
            err.close()
        with _lock:
            _children.add(proc)
        job = _bind_to_job(proc)
        return _wait(proc, workdir, audio_sec, on_progress, should_cancel,
                     default_timeout(audio_sec) if timeout_s is None else float(timeout_s))
    except Exception as e:
        logger.warning("Speaker pass could not run in its own process: %s", e)
        return [], "could not start: %s" % (str(e)[:120] or type(e).__name__)
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
        if workdir:
            _remove_folder(workdir)


def _wait(proc, workdir, audio_sec, on_progress, should_cancel, timeout_s):
    started = time.monotonic()
    last_seen = None
    while True:
        rc = proc.poll()
        if on_progress is not None:
            last_seen = _report_progress(workdir, last_seen, on_progress)
        if _cancel_all.is_set():            # (cancel_all kills the child itself: rc is set then too)
            _kill(proc)
            return [], "app quitting"
        if rc is not None:
            break
        if should_cancel is not None:
            try:
                if should_cancel():
                    _kill(proc)
                    return [], "cancelled"
            except Exception:
                pass
        if time.monotonic() - started > timeout_s:
            _kill(proc)
            logger.warning("Speaker pass gave up after %.0f s (%.0f s of audio)", timeout_s, audio_sec)
            return [], "timed out"
        time.sleep(POLL_S)
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


def _report_progress(workdir, last_seen, on_progress):
    data = _read_json(os.path.join(workdir, PROGRESS_FILE))
    if not isinstance(data, dict):
        return last_seen
    mark = (data.get("done"), data.get("total"))
    if mark != last_seen and None not in mark:
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

def worker_main(argv=None):
    """``--diarize-worker <folder> <num_speakers> <threshold> <threads>``:
    reads <folder>/audio.f32, writes <folder>/progress.json while it works and
    <folder>/result.json at the end. Returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == WORKER_FLAG:
        argv = argv[1:]
    try:
        workdir, num_speakers, threshold, threads = argv[0], int(argv[1]), float(argv[2]), int(argv[3])
    except (IndexError, ValueError):
        return 2
    os.environ[ENV_IN_PROCESS] = "1"
    os.environ[ENV_THREADS] = str(threads)
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
