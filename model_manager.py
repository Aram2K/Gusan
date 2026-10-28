"""One shared download manager for speech and local AI models (Gusan 2.0).

Onboarding, Settings › Speech & AI, Audio to text and dictation all see the same
state, so a model is never fetched twice and every card shows the truth.

Integration notes for Wave 4
----------------------------
Status (Wave 4, services): DONE in main.py - app.downloads built in __init__,
scan at 1.5 s, probe_hardware() in show_onboarding (first run) and
probe_hardware(force=True) after _on_gpu_done; Audio to text uses
downloader=app.downloads.fetch; dictation / meetings fetch a missing Whisper
model through AudioRecorder.model_fetcher = AppController._fetch_speech_model
(the pill shows "Getting ready · N%" once the dictation stopped);
AppController.remove_gpu_download() deletes the GPU download on a worker and
_on_gpu_removed re-probes with probe_hardware(force=True).

Controller (main.py AppController.__init__, on the GUI thread, after self.recorder):

    from model_manager import ModelDownloads
    self.downloads = ModelDownloads(self)      # uses self.track and self.recorder.unload_model
    QTimer.singleShot(1500, self.downloads.scan)   # off the GUI thread; after startup work

  - First run (onboarding_done False): also call ``self.downloads.probe_hardware()``
    when the wizard is built, so step 2 has the chips and "Best for your computer"
    by the time the user gets there (it takes 1-2 s on Windows: PowerShell CIM).
  - After the GPU download finishes (_on_gpu_done) or is removed: call
    ``self.downloads.probe_hardware(force=True)`` so gpu_state / the recommendation
    are fresh.
  - Quit: nothing to do. Downloads are daemon threads; Hugging Face (*.incomplete)
    and local_llm (*.part) both resume next time.

Signals (all on the GUI thread)
    progress(name: str, pct: int 0-100, done: int bytes, total: int bytes)
        throttled (~10/s). total 0 = size not known yet (draw it indeterminate).
    finished(name: str, state: "ready"|"failed", msg: str)   a download ended;
        msg is a short user-facing sentence when it failed ("" when ready).
    removed(name: str, ok: bool, msg: str)                    a removal ended.
    state_changed(name: str, state: str)                      any transition (also
        after scans) - model cards can listen to this one alone.
    scanned()                                                 the first / a full scan ended.
    hardware_ready(snapshot: hardware.HardwareSnapshot)        probe_hardware result.

Methods (GUI thread unless noted)
    state(name) -> str; states() -> {name: state}; is_scanned(); progress_of(name)
        -> (pct, done, total) | None; error(name) -> str; is_removing(name);
        downloading() -> [names]; ready(kind=None) -> [names] ("whisper"/"llm").
        Before the first scan finishes state() says "missing" - draw the cards and
        let state_changed correct them (the scan takes milliseconds).
    start(name) -> bool    True when a download runs after the call (started now, or
        already running: dedupe). False: unknown id, already "ready", or being
        removed. A model found on disk by the worker finishes "ready" at once
        (no network, no telemetry).
    remove(name) -> bool   unload + delete on a worker (Whisper: recorder.unload_model
        first; local AI: local_llm.remove_model refuses while a generation runs).
        False while it downloads or is already being removed.
    wait_ready(name, timeout=None) -> bool   WORKER THREADS ONLY (on the GUI thread
        it never blocks and never touches the disk: it answers state == "ready").
        Waits for an in-flight download; re-checks the disk when the map says
        missing/failed. Never starts one.
    fetch(name, on_progress=None, timeout=None) -> id   WORKER THREADS ONLY
        (RuntimeError on the GUI thread). wait_ready that also starts the
        download when needed: joins the running one (dedupe) or starts it, blocks
        until it ends, forwards progress as on_progress(pct, done, total).
        Raises model_manager.DownloadError(plain sentence) on failure, removal in
        progress, unknown id or timeout. Same signals / telemetry as start().
    scan(names=None)       rescan on a worker (e.g. after a dictation's silent
        Hugging Face download).
    probe_hardware(cfg=None, force=False) -> snapshot | None   starts the probe on a
        worker; returns the cached snapshot (and re-emits hardware_ready) when it
        has one and force is False. force while a probe runs = one more probe
        after it. A failed probe keeps the previous snapshot. Properties:
        .hardware, .recommendation.

What pages / other owners call
  - Onboarding step 2 (Model): chips from ``hardware_ready`` (snapshot.cpu_chip /
    ram_chip / gpu_chip); preselect ``snapshot.recommended.model``; labels from
    hardware.MODEL_INFO (plain / real / size). On Next: ``app.set_setting(
    "whisper_model", name)`` then ``app.downloads.start(name)`` unless
    ``state(name) == "ready"``. If ``recommended.needs_gpu_download`` and the user
    keeps Most accurate: set gpu_accel.CFG_INTRO and call
    ``app.start_gpu_download("onboarding")`` after the wizard (as 1.9.x did).
    ``recommended.suggest_cloud`` -> show Gusan Cloud first. "A little slower
    here" = ``hardware.slower_here(name, snapshot.on_gpu)``; meters =
    MODEL_INFO[name].speed / .accuracy (1-5); cloud name = hardware.CLOUD_SPEECH_MODEL.
  - Any "Runs on" line: ``hardware.model_label(cfg["whisper_model"])`` ("Balanced ·
    Whisper Small"); local AI: ``hardware.model_label(action_model)``; speed words:
    ``hardware.speed_phrase(MODEL_INFO[m].speed_rank, hardware.runs_on_gpu(
    snapshot.gpu_state, app.recorder._cuda_usable))``.
  - Onboarding steps 3-4: the small download chip listens to progress / finished.
    Step 4 (Try it): if ``state(model) == "downloading"`` when Alt+R is pressed,
    the dictation must wait on the manager (below), not on Hugging Face silently.
  - Settings › Speech & AI cards: state_changed / progress / finished / removed;
    "Download" -> start(name); "Use" -> ``app.set_settings({"backend": "local",
    "whisper_model": name})`` (local AI: ``app.set_setting("action_model", name)``);
    "Remove" -> confirm, then remove(name). If the removed model is the active
    whisper_model / action_model, the page must switch to another ready model
    (``ready("whisper")``) - the manager never edits the config.
  - Audio to text (owner C): build the controller with
    ``FileJobController(app, downloader=app.downloads.fetch)`` - file_jobs calls
    it on its worker as ``fetch(model, on_progress=cb)`` and turns any exception
    into its own "couldn't be downloaded" row, so a file job and the Speech & AI
    card share one download. Prefer ``ready("whisper")`` in pick_auto_model
    instead of calling model_downloaded.
  - Dictation / meetings (main._start, AudioRecorder.load_model path, owner D in
    Wave 2, the controller in Wave 4): before loading a local model on a worker,
    ``app.downloads.fetch(name, timeout=...)`` when ``state(name) != "ready"``
    (it waits for a running download or starts a missing one) and show the
    pill's "Getting your speech model ready" state meanwhile; on DownloadError
    show str(e). That replaces _build_model_locked downloading it silently with
    local_files_only=False (no progress, no dedupe).
  - The old ui/settings.py (with its own per-window downloads) is retired: every
    download in the app goes through this manager.

How it works
------------
    DownloadManager   the logic, no Qt: one state map, dedupe, workers, telemetry,
                      wait_ready. Thread-safe. Tested in tests/test_model_manager.py.
    ModelDownloads    the QObject the controller owns as ``app.downloads``. Built on
                      first use (PySide6 is imported lazily, so this module imports
                      without Qt). Every signal is emitted on the GUI thread.
    ModelOps          the real disk / network operations: main.model_downloaded /
                      download_whisper_model / remove_whisper_model (they stay in
                      main.py) and local_llm.model_downloaded / download_model /
                      remove_model.

States: "missing" / "downloading" / "ready" / "failed" (a failed download keeps
"failed" and its message until it is retried or found on disk). A removal is not a
state: while it runs the model stays "ready" and is_removing(name) is True.

Names: speech ids ("tiny" ... "large-v3") and local AI ids ("qwen_tiny", "qwen_3b",
"qwen_7b", "gemma_2b"; 1.9.x ids such as "aibuben_tiny" are normalised) share one
namespace - they never collide. Telemetry "kind" stays "whisper" / "llm" as in 1.9.x.

The Armenian speech model (Gusan 2.0.1, speech_hy.py) is a third kind, "speech_hy", with the one
id "hy-fastconformer". It is NOT a Whisper id: ready("whisper") and the Whisper cards never see
it. Its calls are the same (start / fetch / remove / state / progress / finished), plus
    cancel(name) -> bool   stop a running Armenian download (it keeps what it has and resumes
                           next time; the state goes back to "missing" with no message)
    refresh_catalog(force=False) -> bool   WORKER THREADS ONLY (ModelDownloads.refresh_catalog()
                           starts it on a worker): look for the model's manifest entry (at most
                           once a day, never in Privacy mode, never at startup) and emit
                           catalog_changed when speech_hy.listed() changed. Call it when a place
                           that would list the model opens (Speech & AI, a speech picker,
                           onboarding's model step), then read speech_hy.listed() again.
Privacy mode does not stop the download itself (it sends nothing of the person's, like a Whisper
download: speech_hy.DOWNLOAD_BLOCKED_IN_PRIVACY_MODE is the one switch) but it does stop the manifest
lookup; the model is hidden everywhere while speech_hy.listed() is False (nothing to download from
yet, or no runtime).
"""
from __future__ import annotations

import errno
import logging
import sys
import threading
import time

import hardware
import speech_hy

logger = logging.getLogger("transcribe")

MISSING, DOWNLOADING, READY, FAILED = "missing", "downloading", "ready", "failed"
STATES = (MISSING, DOWNLOADING, READY, FAILED)
WHISPER, LLM = "whisper", "llm"          # telemetry "kind" (1.9.x values)
HY = speech_hy.KIND                      # "speech_hy": the Armenian model (Gusan 2.0.1)

SPEECH_IDS = hardware.SPEECH_MODEL_IDS
HY_IDS = (hardware.HY_MODEL_ID,)
LLM_IDS = hardware.LOCAL_AI_MODEL_IDS
ALL_IDS = SPEECH_IDS + HY_IDS + LLM_IDS

PROGRESS_MIN_S = 0.1     # at most ~10 progress updates a second per model
PROGRESS_IDLE_S = 0.5    # ...but at least every half second while bytes move

# User-facing failure sentences (short and plain; the app is "Gusan").
MSG_DISK_FULL = "Not enough free disk space for this model."
MSG_PERMISSION = "Gusan couldn't save the model on this computer."
MSG_NETWORK = "Couldn't reach the download server. Check your connection and try again."
MSG_UNAVAILABLE = "This model can't be downloaded right now. Try again later."
MSG_SERVER = "The download server had a problem. Try again in a few minutes."
MSG_INCOMPLETE = "The download didn't finish. Try again."
MSG_OTHER = "The download stopped. Try again."
MSG_IN_USE = "The model is in use right now. Try again when it finishes."
MSG_REMOVE_FAILED = "Couldn't remove the model files."
MSG_REMOVING = "This model is being removed. Try again in a moment."
MSG_STILL_DOWNLOADING = "The model is still downloading. Try again when it finishes."


class DownloadError(Exception):
    """fetch() couldn't make the model ready; str(e) is one plain sentence for
    the user (the same text the model card shows)."""

_DISK_FULL_WINERRORS = (39, 112)       # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
_NETWORK_WORDS = ("connection", "timed out", "timeout", "getaddrinfo", "name resolution",
                  "temporary failure", "network is unreachable", "max retries", "ssl",
                  "proxy", "offline", "remote end closed", "connectionreset")
# Exception class names (requests, urllib3, and httpx - which huggingface_hub 1.x
# uses: ConnectError / ReadTimeout / NetworkError / TransportError).
_NETWORK_CLASSES = ("connection", "connecterror", "networkerror", "transporterror", "timeout",
                    "sslerror", "proxyerror", "offlinemode", "remoteprotocolerror")


def _http_status(exc):
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None) if resp is not None else None
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def failure_message(exc):
    """(reason, message) for a failed download: reason is a short code safe for
    analytics, message one plain sentence for the card."""
    if isinstance(exc, speech_hy.SpeechHyError):          # the Armenian model's own plain sentence
        return exc.reason, exc.user_message
    text = str(exc or "").lower()
    if isinstance(exc, OSError) and (
            getattr(exc, "errno", None) == errno.ENOSPC
            or getattr(exc, "winerror", None) in _DISK_FULL_WINERRORS):
        return "disk_full", MSG_DISK_FULL
    if "no space left" in text or "not enough space" in text or "disk is full" in text:
        return "disk_full", MSG_DISK_FULL
    status = _http_status(exc)
    if status in (401, 403, 404, 410):
        return "unavailable", MSG_UNAVAILABLE
    if status is not None and (status == 429 or status >= 500):
        return "server", MSG_SERVER
    if isinstance(exc, PermissionError):
        return "permission", MSG_PERMISSION
    names = " ".join(c.__name__.lower() for c in type(exc).__mro__)
    if (isinstance(exc, (ConnectionError, TimeoutError))
            or any(k in names for k in _NETWORK_CLASSES)
            or any(k in text for k in _NETWORK_WORDS)):
        return "network", MSG_NETWORK
    return "other", MSG_OTHER


def remove_failure_message(exc):
    """Message for a failed removal: a file still open (Windows) or a local AI
    model busy with a generation reads as 'in use'."""
    if isinstance(exc, PermissionError) or "busy" in str(exc).lower() or "in use" in str(exc).lower():
        return MSG_IN_USE
    return MSG_REMOVE_FAILED


def _main_module():
    """main.py's functions without executing main.py a second time: the app runs
    it as __main__, so a plain ``import main`` would load a second copy (with its
    own config). Prefer the running app (__main__), then an imported main."""
    for key in ("__main__", "main"):
        mod = sys.modules.get(key)
        if mod is not None and hasattr(mod, "download_whisper_model") and hasattr(mod, "model_downloaded"):
            return mod
    import main  # noqa: WPS433 - only when nothing has loaded it (tests, tools)
    return main


class ModelOps:
    """The real operations. Swap for a fake in tests. ``privacy``: () -> bool, Privacy mode on
    (the Armenian model's manifest and download are refused while it is)."""

    def __init__(self, unload_whisper=None, privacy=None):
        self._unload_whisper = unload_whisper
        self._privacy = privacy

    def privacy_on(self):
        try:
            return bool(self._privacy()) if self._privacy is not None else False
        except Exception:
            return False

    @staticmethod
    def normalize(name):
        return hardware.LEGACY_LOCAL_AI_IDS.get(name, name)

    def downloaded(self, kind, name):
        if kind == WHISPER:
            return bool(_main_module().model_downloaded(name))
        if kind == HY:
            return bool(speech_hy.ready())
        import local_llm
        return bool(local_llm.model_downloaded(name))

    def download(self, kind, name, on_progress, should_cancel=None):
        if kind == WHISPER:
            return _main_module().download_whisper_model(name, on_progress=on_progress)
        if kind == HY:
            return speech_hy.download(on_progress=on_progress, should_cancel=should_cancel,
                                      privacy=self.privacy_on())
        import local_llm
        return local_llm.download_model(name, on_progress=on_progress)

    def remove(self, kind, name):
        if kind == WHISPER:
            if self._unload_whisper is not None:
                self._unload_whisper(name)      # may wait for a load in progress: worker only
            return _main_module().remove_whisper_model(name)
        if kind == HY:
            return speech_hy.remove()           # unloads it first; raises InUse while it transcribes
        import local_llm
        return local_llm.remove_model(name)     # raises LocalLLMError while busy

    def refresh_catalog(self, force=False):
        """The Armenian model's manifest entry (speech_hy.refresh_catalog): True when it changed."""
        return bool(speech_hy.refresh_catalog(force=force, privacy=self.privacy_on()))


def _thread(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True, name="gusan-models").start()


class DownloadManager:
    """See the module docstring. ``notify(event, *args)`` receives
    ("state", name, state), ("progress", name, pct, done, total),
    ("finished", name, state, msg), ("removed", name, ok, msg) and ("scanned",)
    from whichever thread made the change. ``track(event, props)`` is the
    controller's analytics hook. ``spawn(fn, *args)`` runs fn on a worker."""

    def __init__(self, ops=None, track=None, notify=None, spawn=None, clock=time.monotonic):
        self._ops = ops if ops is not None else ModelOps()
        self._track = track
        self._notify_cb = notify
        self._spawn = spawn or _thread
        self._clock = clock
        self._lock = threading.RLock()
        self._states = {}
        self._progress = {}
        self._errors = {}
        self._events = {}          # name -> threading.Event of the current download
        self._removing = set()
        self._last_emit = {}       # name -> (time, pct)
        self._listeners = {}       # name -> [on_progress] of fetch() callers waiting on it
        self._gen = {}             # name -> bumped on every change a scan must not undo
        self._cancels = set()      # names whose running download was asked to stop (cancel())
        self._scanned = False

    # ── helpers ───────────────────────────────────────────────────────────────
    def _notify(self, *event):
        if self._notify_cb is None:
            return
        try:
            self._notify_cb(*event)
        except Exception:
            logger.debug("Model manager listener failed for %s", event[:2], exc_info=True)

    def track(self, event, props=None):
        if self._track is None:
            return
        try:
            self._track(event, props or {})
        except Exception:
            logger.debug("Model analytics failed", exc_info=True)

    def normalize(self, name):
        try:
            return self._ops.normalize(name)
        except Exception:
            return name

    def kind(self, name):
        """'whisper' / 'speech_hy' (the Armenian model) / 'llm' / None for an unknown id."""
        name = self.normalize(name)
        if name in SPEECH_IDS:
            return WHISPER
        if name in HY_IDS:
            return HY
        if name in LLM_IDS:
            return LLM
        return None

    def _check_disk(self, kind, name):
        try:
            return bool(self._ops.downloaded(kind, name))
        except Exception:
            logger.debug("Model disk check failed for %s", name, exc_info=True)
            return False

    def _set(self, name, state):
        """Write a state under the lock (callers hold it) and bump its generation."""
        self._states[name] = state
        self._gen[name] = self._gen.get(name, 0) + 1

    # ── queries ───────────────────────────────────────────────────────────────
    def state(self, name):
        with self._lock:
            return self._states.get(self.normalize(name), MISSING)

    def states(self):
        with self._lock:
            return {n: self._states.get(n, MISSING) for n in ALL_IDS}

    def is_scanned(self):
        with self._lock:
            return self._scanned

    def progress_of(self, name):
        with self._lock:
            return self._progress.get(self.normalize(name))

    def error(self, name):
        with self._lock:
            return self._errors.get(self.normalize(name), "")

    def is_removing(self, name):
        with self._lock:
            return self.normalize(name) in self._removing

    def downloading(self):
        with self._lock:
            return [n for n in ALL_IDS if self._states.get(n) == DOWNLOADING]

    def ready(self, kind=None):
        """Ready ids in display order; kind 'whisper' / 'llm' narrows it."""
        ids = (SPEECH_IDS if kind == WHISPER else LLM_IDS if kind == LLM
               else HY_IDS if kind == HY else ALL_IDS)
        with self._lock:
            return [n for n in ids if self._states.get(n) == READY]

    # ── scan ──────────────────────────────────────────────────────────────────
    def scan(self, names=None):
        """Rescan on a worker."""
        self._spawn(self.scan_now, names)

    def scan_now(self, names=None):
        """Check the disk for each model (blocking - workers and tests). A model
        downloading or being removed is left alone; a failed one that is still
        missing keeps "failed" (and its message)."""
        full = names is None
        names = ALL_IDS if full else [self.normalize(n) for n in names]
        changed = []
        for name in names:
            kind = self.kind(name)
            if kind is None:
                continue
            with self._lock:
                gen = self._gen.get(name, 0)
            on_disk = self._check_disk(kind, name)
            with self._lock:
                old = self._states.get(name)
                if old == DOWNLOADING or name in self._removing or self._gen.get(name, 0) != gen:
                    continue                      # changed while we looked: theirs is newer
                new = READY if on_disk else (FAILED if old == FAILED else MISSING)
                if new == READY:
                    self._errors.pop(name, None)
                if new != old:
                    self._set(name, new)
                    changed.append((name, new))
        with self._lock:
            if full:
                self._scanned = True
        for name, new in changed:
            self._notify("state", name, new)
        if full:
            self._notify("scanned")
        return self.states()

    # ── download ──────────────────────────────────────────────────────────────
    def start(self, name):
        name = self.normalize(name)
        kind = self.kind(name)
        if kind is None:
            logger.warning("Unknown model %r: not downloading", name)
            return False
        with self._lock:
            old = self._states.get(name)
            if old == DOWNLOADING:
                return True                        # dedupe: one download per model
            if old == READY or name in self._removing:
                return False
            self._set(name, DOWNLOADING)
            self._errors.pop(name, None)
            self._cancels.discard(name)
            self._progress[name] = (0, 0, 0)
            self._last_emit.pop(name, None)
            done = threading.Event()
            self._events[name] = done
        self._notify("state", name, DOWNLOADING)
        try:
            self._spawn(self._download_worker, name, kind, done)
        except Exception as e:                     # no thread: fail visibly, never hang
            logger.warning("Could not start the %s download: %s", name, e)
            self._finish(name, FAILED, MSG_OTHER, done)
            return False
        return True

    def _download_worker(self, name, kind, done):
        if self._check_disk(kind, name):           # already there: nothing to fetch
            self._finish(name, READY, "", done)
            return
        props = {"kind": kind, "model": name}
        self.track("model_download_started", props)

        def progress(pct, got=0, total=0):
            self._on_progress(name, pct, got, total)
        try:
            if kind == HY:
                # The Armenian download can be stopped (cancel) and says why it can't run in a
                # plain sentence (Privacy mode, nothing to download from).
                self._ops.download(kind, name, progress, should_cancel=lambda: name in self._cancels)
            else:
                self._ops.download(kind, name, progress)
            if not self._check_disk(kind, name):
                raise _Incomplete()
        except _Incomplete:
            self.track("model_download_failed", dict(props, reason="incomplete"))
            self._finish(name, FAILED, MSG_INCOMPLETE, done)
            return
        except speech_hy.Cancelled:
            with self._lock:
                self._cancels.discard(name)
            self.track("model_download_failed", dict(props, reason="cancelled"))
            self._finish(name, MISSING, "", done)   # not an error: the card offers Download again
            return
        except BaseException as e:                  # noqa: B902 - a worker must always finish
            reason, msg = failure_message(e)
            logger.warning("Model download failed (%s, %s): %s", name, reason, e)
            self.track("model_download_failed", dict(props, reason=reason))
            self._finish(name, FAILED, msg, done)
            if not isinstance(e, Exception):
                raise
            return
        self.track("model_download_completed", props)
        self._finish(name, READY, "", done)

    def _on_progress(self, name, pct, got, total):
        # Runs inside the download (local_llm doesn't guard its callback): it
        # must never raise, or one odd value would fail the whole download.
        try:
            pct = max(0, min(100, int(pct))) if pct is not None else 0
        except (TypeError, ValueError, OverflowError):
            pct = 0
        try:
            got, total = max(0, int(got or 0)), max(0, int(total or 0))
        except (TypeError, ValueError, OverflowError):
            got, total = 0, 0
        now = self._clock()
        with self._lock:
            if self._states.get(name) != DOWNLOADING:
                return
            self._progress[name] = (pct, got, total)
            last = self._last_emit.get(name)
            if last is not None:
                last_t, last_pct = last
                quiet = now - last_t
                if pct < 100 and not ((pct != last_pct and quiet >= PROGRESS_MIN_S) or quiet >= PROGRESS_IDLE_S):
                    return
            self._last_emit[name] = (now, pct)
            listeners = list(self._listeners.get(name, ()))
        self._notify("progress", name, pct, got, total)
        for cb in listeners:
            try:
                cb(pct, got, total)
            except Exception:
                logger.debug("Model progress listener failed for %s", name, exc_info=True)

    def _finish(self, name, state, msg, done):
        with self._lock:
            self._set(name, state)
            if state == READY:
                _pct, got, total = self._progress.get(name) or (0, 0, 0)
                self._progress[name] = (100, max(got, total), max(got, total))
                self._errors.pop(name, None)
            else:
                self._errors[name] = msg
            self._last_emit.pop(name, None)
        self._notify("state", name, state)
        self._notify("finished", name, state, msg)
        done.set()                                  # last: a waiter sees the final state and events

    def wait_ready(self, name, timeout=None):
        """True once the model is on disk. Blocks only while a download runs
        (up to ``timeout`` seconds); never starts one. Worker threads only."""
        name = self.normalize(name)
        kind = self.kind(name)
        if kind is None:
            return False
        with self._lock:
            st = self._states.get(name)
            done = self._events.get(name)
        if st == READY:
            return True
        if st == DOWNLOADING and done is not None:
            done.wait(timeout)
            return self.state(name) == READY
        if self._check_disk(kind, name):           # unknown / stale "missing"
            with self._lock:
                if self._states.get(name) in (None, MISSING, FAILED) and name not in self._removing:
                    self._set(name, READY)
                    self._errors.pop(name, None)
                    changed = True
                else:
                    changed = False
            if changed:
                self._notify("state", name, READY)
            return True
        return False

    def fetch(self, name, on_progress=None, timeout=None):
        """Make sure the model is on disk and block until it is: joins the
        running download or starts one (still one download per model, with the
        usual signals and telemetry). WORKER THREADS ONLY - for a file job
        (file_jobs ``downloader=``) or a dictation whose model is missing.
        ``on_progress(pct, done, total)`` gets the same throttled updates as the
        cards. Returns the normalised id; raises DownloadError (its text is the
        card's sentence) when the download fails, the model is being removed,
        the id is unknown, or ``timeout`` seconds pass first."""
        name = self.normalize(name)
        if self.kind(name) is None:
            raise DownloadError(MSG_UNAVAILABLE)
        if self.is_removing(name):                  # "ready" now, gone in a moment
            raise DownloadError(MSG_REMOVING)
        if on_progress is not None:
            with self._lock:
                self._listeners.setdefault(name, []).append(on_progress)
        try:
            if self.wait_ready(name, 0):            # ready, or found on disk
                return name
            if not self.start(name):                # dedupe: True when one runs
                if self.state(name) == READY:
                    return name
                if self.is_removing(name):
                    raise DownloadError(MSG_REMOVING)
                raise DownloadError(self.error(name) or MSG_OTHER)
            if self.wait_ready(name, timeout):
                return name
            if self.state(name) == DOWNLOADING:
                raise DownloadError(MSG_STILL_DOWNLOADING)
            raise DownloadError(self.error(name) or MSG_OTHER)
        finally:
            if on_progress is not None:
                with self._lock:
                    waiting = self._listeners.get(name, [])
                    for i, cb in enumerate(waiting):
                        if cb is on_progress:
                            del waiting[i]
                            break
                    if not waiting:
                        self._listeners.pop(name, None)

    def cancel(self, name):
        """Stop a running download that can be stopped (the Armenian model's): it keeps what it has
        and the next start resumes. True when the request was made, False for any other model or
        when nothing is downloading."""
        name = self.normalize(name)
        if self.kind(name) != HY:
            return False
        with self._lock:
            if self._states.get(name) != DOWNLOADING:
                return False
            self._cancels.add(name)
        try:
            speech_hy.request_cancel()
        except Exception:
            pass
        return True

    def refresh_catalog(self, force=False):
        """Look for the Armenian model's manifest entry (once a day at most, never in Privacy mode):
        blocking, worker threads only. Emits ("catalog",) and returns True when the model's
        listing changed - the pages then read speech_hy.listed() again."""
        refresh = getattr(self._ops, "refresh_catalog", None)
        if refresh is None:
            return False
        try:
            changed = bool(refresh(force))
        except Exception:
            logger.debug("Model catalog refresh failed", exc_info=True)
            return False
        if changed:
            self._notify("catalog")
        return changed

    # ── remove ────────────────────────────────────────────────────────────────
    def remove(self, name):
        name = self.normalize(name)
        kind = self.kind(name)
        if kind is None:
            return False
        with self._lock:
            if self._states.get(name) == DOWNLOADING or name in self._removing:
                return False
            self._removing.add(name)
        try:
            self._spawn(self._remove_worker, name, kind)
        except Exception as e:
            logger.warning("Could not start removing %s: %s", name, e)
            with self._lock:
                self._removing.discard(name)
            return False
        return True

    def _remove_worker(self, name, kind):
        msg = ""
        result = None
        try:
            # main.remove_whisper_model / local_llm.remove_model return False
            # when there was nothing to delete (1.9.x tracked only real removals).
            result = self._ops.remove(kind, name)
        except Exception as e:
            logger.warning("Removing model %s failed: %s", name, e)
            msg = remove_failure_message(e)
        on_disk = self._check_disk(kind, name)
        if on_disk and not msg:
            msg = MSG_REMOVE_FAILED
        ok = not on_disk and not msg
        with self._lock:
            self._removing.discard(name)
            old = self._states.get(name)
            # The disk decides: half-deleted files no longer count as a model.
            new = READY if on_disk else MISSING
            if new == MISSING:
                self._progress.pop(name, None)
                self._errors.pop(name, None)
            self._set(name, new)
        if ok and result is not False:
            self.track("model_removed", {"kind": kind, "model": name})
        if new != old:
            self._notify("state", name, new)
        self._notify("removed", name, ok, msg)


class _Incomplete(Exception):
    """The download returned but the model isn't complete on disk."""


# ── The Qt face: app.downloads ────────────────────────────────────────────────
_QT_CLASS = None


def _build_qt_class():
    from PySide6.QtCore import QObject, Qt, Signal, Slot

    class ModelDownloads(QObject):
        """app.downloads - see the module docstring for the signals and calls."""

        progress = Signal(str, int, object, object)   # name, pct, done, total (bytes > 2 GB: object)
        finished = Signal(str, str, str)              # name, "ready"/"failed", msg
        removed = Signal(str, bool, str)              # name, ok, msg
        state_changed = Signal(str, str)              # name, state
        scanned = Signal()
        catalog_changed = Signal()                    # speech_hy.listed() may have changed (refresh_catalog)
        hardware_ready = Signal(object)               # hardware.HardwareSnapshot
        _relay = Signal(object)                       # worker -> GUI thread

        def __init__(self, app=None, parent=None, ops=None, spawn=None, track=None):
            super().__init__(parent)
            self._app = app
            self._spawn = spawn or _thread
            self._gui_ident = threading.get_ident()    # built on the GUI thread
            self._hardware = None
            self._hw_running = False                   # GUI thread only, like _hw_again
            self._hw_again = False
            self._relay.connect(self._deliver, Qt.QueuedConnection)
            if ops is None:
                ops = ModelOps(unload_whisper=self._unload_whisper, privacy=self._privacy_on)
            if track is None and app is not None:
                track = getattr(app, "track", None)
            self.core = DownloadManager(ops=ops, track=track, notify=self._post, spawn=self._spawn)

        # Workers call this from any thread; _deliver runs on the GUI thread.
        def _post(self, *event):
            self._relay.emit(event)

        @Slot(object)
        def _deliver(self, event):
            what, args = event[0], event[1:]
            if what == "state":
                self.state_changed.emit(*args)
            elif what == "progress":
                self.progress.emit(*args)
            elif what == "finished":
                self.finished.emit(*args)
            elif what == "removed":
                self.removed.emit(*args)
            elif what == "scanned":
                self.scanned.emit()
            elif what == "catalog":
                self.catalog_changed.emit()
            elif what == "hardware":                   # a probe finished
                self._hw_running = False
                if args[0] is not None:
                    self._hardware = args[0]
                    self.hardware_ready.emit(args[0])
                if self._hw_again:                     # force asked while it ran
                    self._hw_again = False
                    try:
                        self.probe_hardware(force=True)
                    except Exception:
                        logger.warning("Hardware probe could not start again", exc_info=True)
            elif what == "hardware_cached":            # the cached one, announced again
                self.hardware_ready.emit(args[0])

        def _unload_whisper(self, name):
            rec = getattr(self._app, "recorder", None)
            if rec is not None and hasattr(rec, "unload_model"):
                rec.unload_model(name)

        def _privacy_on(self):
            cfg = getattr(self._app, "cfg", None)
            return bool(cfg.get("privacy_mode")) if isinstance(cfg, dict) else False

        def _on_gui_thread(self):
            return threading.get_ident() == self._gui_ident

        # ── the API (delegates to the core) ───────────────────────────────────
        def start(self, name):
            return self.core.start(name)

        def remove(self, name):
            return self.core.remove(name)

        def cancel(self, name):
            """Stop a running Armenian-model download (False for any other model)."""
            return self.core.cancel(name)

        def refresh_catalog(self, force=False):
            """Look for the Armenian model's manifest entry on a worker (at most once a day, never
            in Privacy mode, never at startup): call it when a page that would list the model
            opens. catalog_changed fires when speech_hy.listed() changed."""
            self._spawn(self.core.refresh_catalog, force)

        def scan(self, names=None):
            self.core.scan(names)

        def state(self, name):
            return self.core.state(name)

        def states(self):
            return self.core.states()

        def is_scanned(self):
            return self.core.is_scanned()

        def progress_of(self, name):
            return self.core.progress_of(name)

        def error(self, name):
            return self.core.error(name)

        def is_removing(self, name):
            return self.core.is_removing(name)

        def downloading(self):
            return self.core.downloading()

        def ready(self, kind=None):
            return self.core.ready(kind)

        def kind(self, name):
            return self.core.kind(name)

        def wait_ready(self, name, timeout=None):
            if self._on_gui_thread():
                # Never block the GUI thread - not even on the disk check
                # (it can import local_llm, ~2 s cold): answer from the map.
                if timeout is None or timeout > 0:
                    logger.warning("wait_ready(%s) on the GUI thread: not waiting", name)
                return self.core.state(name) == READY
            return self.core.wait_ready(name, timeout)

        def fetch(self, name, on_progress=None, timeout=None):
            """DownloadManager.fetch - blocks, so worker threads only."""
            if self._on_gui_thread():
                raise RuntimeError("model_manager.fetch() blocks: call it on a worker thread")
            return self.core.fetch(name, on_progress=on_progress, timeout=timeout)

        # ── hardware ──────────────────────────────────────────────────────────
        @property
        def hardware(self):
            return self._hardware

        @property
        def recommendation(self):
            return self._hardware.recommended if self._hardware is not None else None

        def probe_hardware(self, cfg=None, force=False):
            """hardware.snapshot(cfg) on a worker -> hardware_ready. With a cached
            snapshot and no force: returns it and re-emits hardware_ready. A
            force while a probe runs probes again once it ends (its result may
            predate the change that asked for it, e.g. the GPU download)."""
            if self._hardware is not None and not force:
                snap = self._hardware
                self._relay.emit(("hardware_cached", snap))
                return snap
            if self._hw_running:
                if force:
                    self._hw_again = True
                return None
            self._hw_running = True
            if cfg is None:
                cfg = getattr(self._app, "cfg", None)

            def _run():
                try:
                    snap = hardware.snapshot(cfg)
                except Exception:
                    logger.warning("Hardware probe failed", exc_info=True)
                    try:
                        snap = hardware.snapshot(cfg, probe_gpu=False)
                    except Exception:
                        snap = None
                try:
                    self._post("hardware", snap)
                except RuntimeError:                   # the manager was deleted (quitting)
                    pass

            try:
                self._spawn(_run)
            except Exception:
                self._hw_running = False
                raise
            return None

    ModelDownloads.__qualname__ = "ModelDownloads"
    return ModelDownloads


def __getattr__(name):
    # ``from model_manager import ModelDownloads`` builds the Qt class on first
    # use, so importing this module never imports PySide6.
    global _QT_CLASS
    if name == "ModelDownloads":
        if _QT_CLASS is None:
            _QT_CLASS = _build_qt_class()
        return _QT_CLASS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
