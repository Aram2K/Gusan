"""The speaker pass after the notes - when and in which order it runs (Gusan 2.0.1).

A meeting recorded from the microphone and the call's sound gets its notes at once
("You" and "Them"); telling the people on the call apart ("Speaker 1..N") takes
minutes on a slow computer, so it happens afterwards, quietly, in the background.
This class only decides WHEN: the work itself is ``run(folder, should_cancel,
status)`` (meeting_speakers.refine_meeting).

The rules, all of them about not getting in the way:

  * it starts only while nothing else needs the computer - no recording, no
    dictation, no Audio to text job (``is_busy()``) - and polls for that gently
    (POLL_S) rather than by itself at app launch: a pass exists only because a
    meeting just finished and queued itself (``enqueue``), or because the person asked for
    it (``enqueue(first=True)``: it goes to the front). After the computer was
    busy it also lets it stay quiet for SETTLE_POLLS looks first, so a person who
    dictates every few seconds does not get a pass started and stopped again and
    again (each start loads the speaker model for nothing);
  * if a recording, a dictation or a file job starts while it works, it stops at
    once (``should_cancel`` is polled by the child process' wait loop several times a
    second) and tries again when the computer is free - at most MAX_RETRIES times per
    queued job (a meeting stopped again, once continued, starts the count anew), after
    which it leaves the meeting as it is (the page keeps offering "Identify speakers").
    The work already done is not lost: the pass keeps what it made (``status.resume``,
    a dict that lives as long as the job does) and goes on from there;
  * "Cancel" and the app quitting end it for good; nothing is retried;
  * one pass at a time, the oldest meeting first; the worker thread exists only while
    there is something to do.

What the page can see (all of it read by polling, nothing here is a signal storm):

  * ``state(folder)``: "" / "waiting" / "running";
  * ``snapshot(folder)``: everything the banner says - the state, what it is waiting for
    (``wait_reason``: "recording", "dictation", "file", "notes" when ``is_busy()`` names
    it, "queue" when another meeting is ahead, "settling" while it lets the computer stay
    quiet or has only just been queued), the stage and real progress the pass reported
    through ``status.progress(stage, done, total)``, an estimate of the time left from this
    try's own pace, whether it is stopping after Cancel, and the tries so far;
  * ``take_result(folder)``: how the job ended, once - (the pass's result, "done" |
    "cancelled" | "gave_up") - for the one line the page says about it.

A stale banner is the worst outcome, so the state is guarded: a worker thread that died
leaves nothing "waiting" for ever (``state`` notices and clears it), a worker that could
not be started takes its meeting out of the queue, and everything kept for a job (retries,
progress, the settling mark, the voice prints) goes when the job ends.

Qt-free; the callbacks are called from the worker thread.
"""
import logging
import os
import threading
import time

logger = logging.getLogger("transcribe")

POLL_S = 2.0            # how often a waiting pass looks whether the computer is free
MAX_RETRIES = 5         # interruptions one meeting's pass survives; the next one ends it
SETTLE_POLLS = 3        # quiet looks (about 6 s) in a row wanted after the computer was busy
ETA_MIN_DONE = 3        # pieces made in this try before a time left is worked out ...
ETA_MIN_SECONDS = 4.0   # ... and seconds of this try's own pace

WAITING = "waiting"     # queued: behind other work, or waiting for the computer to be free
RUNNING = "running"     # the speaker tool works on it now

HOW_DONE = "done"
HOW_CANCELLED = "cancelled"
HOW_GAVE_UP = "gave_up"

R_QUEUE = "queue"           # another meeting's pass is ahead
R_SETTLING = "settling"     # the computer is free: letting it stay quiet for a moment (or just queued)


def _key(folder):
    return os.path.normcase(os.path.abspath(str(folder)))


def _thread(target):
    threading.Thread(target=target, daemon=True, name="gusan-speaker-pass").start()


class _Crashed:
    """The result of a pass that raised: it ended, and not well."""
    state = "failed"
    reason = "error"
    notice = ""
    speakers = 0


class PassStatus:
    """What the pass gets as ``status``: still callable with a text (the status line, as always),
    and now also ``progress(stage, done, total)`` for the real percentage, and ``resume`` - a dict
    that lives as long as the job does, to keep what a stopped try made."""

    def __init__(self, owner, folder, resume):
        self._owner = owner
        self._folder = folder
        self.resume = resume

    def __call__(self, text):
        self._owner._say(text)

    def progress(self, stage, done, total):
        self._owner._progress(self._folder, stage, done, total)


class SpeakerFollowUp:
    def __init__(self, is_busy, run, on_change=None, spawn=None, poll_s=POLL_S,
                 max_retries=MAX_RETRIES, wait=None, settle_polls=SETTLE_POLLS, clock=None):
        """``is_busy()``: truthy while a recording, dictation or file job needs the
        computer - the reason it returns ("recording", "dictation", "file", "notes") is what the
        page says it waits for; ``True`` is "busy" without a name. ``run(folder, should_cancel,
        status)``: one pass; returns an object whose ``state`` is "cancelled" when it was stopped
        (anything else ends the meeting's turn); ``status`` is a PassStatus. ``on_change(folder)``: the
        state of a meeting's pass changed (any thread). ``spawn(fn)`` starts the worker (a daemon
        thread by default). ``wait(seconds)`` sleeps between two looks (woken early by cancel /
        shutdown). ``settle_polls``: quiet looks in a row wanted before it starts again once the
        computer was busy (a pass that finds it free at once starts at once). ``clock()``: seconds
        (monotonic) - for the time left."""
        self._is_busy = is_busy
        self._run = run
        self._on_change = on_change
        self._spawn = spawn or _thread
        self.poll_s = poll_s
        self.max_retries = max_retries
        self.settle_polls = max(0, int(settle_polls))
        self._clock = clock or time.monotonic
        self._cooling = set()               # folder keys that were interrupted: let the computer settle first
        self._lock = threading.RLock()
        self._queue = []                    # folders waiting, oldest first
        self._current = ""                  # the folder being worked on (waiting for the computer, or running)
        self._running = False
        self._alive = False                 # a worker thread exists
        self._worker_ident = None           # its thread id (wait_idle must not wait on itself)
        self._retries = {}                  # folder key -> interruptions so far (while the job lasts)
        self._resume = {}                   # folder key -> what a stopped try made (while the job lasts)
        self._prog = {}                     # folder key -> the current try's progress
        self._results = {}                  # folder key -> (result, how): kept until take_result
        self._reason_now = ""               # what the current job waits for (see wait_reason)
        self._interrupted = False           # the running pass was stopped because the computer got busy
        self._cancel = threading.Event()    # "Cancel" for the current meeting
        self._quit = threading.Event()      # the app is quitting
        self._wake = threading.Event()
        self._wait = wait or self._sleep
        self.status_text = ""

    # ── the public side ──
    def enqueue(self, folder, first=False):
        """Queue ``folder`` (a meeting whose notes are done). ``first``: the person asked for it, it goes
        in front of the others. False when it is already queued or being worked on, the app is
        quitting, or the worker could not be started."""
        folder = str(folder or "")
        if not folder or self._quit.is_set():
            return False
        key = _key(folder)
        with self._lock:
            working = bool(self._current) and _key(self._current) == key
            if working or any(_key(f) == key for f in self._queue):
                return False
            # A new job (a continued meeting's new part, say) starts with a fresh count: a
            # meeting that once used up its retries must not get none the next time.
            self._retries.pop(key, None)
            self._resume.pop(key, None)
            self._results.pop(key, None)
            self._prog.pop(key, None)
            if first:
                self._queue.insert(0, folder)
            else:
                self._queue.append(folder)
            start = not self._alive
            if start:
                self._alive = True
        self._changed(folder)
        if start:
            try:
                self._spawn(self._loop)
            except Exception:
                # Nobody will work on it: it must not sit "waiting" for ever.
                with self._lock:
                    self._alive = False
                    self._queue[:] = [f for f in self._queue if _key(f) != key]
                logger.warning("The speaker pass could not be started", exc_info=True)
                self._changed(folder)
                return False
        return True

    def state(self, folder=None):
        """"" (nothing queued or running), "waiting" or "running" - for ``folder``,
        or, with none, for any meeting."""
        self._check_worker()
        with self._lock:
            if folder is None:
                if self._current:
                    return RUNNING if self._running else WAITING
                return WAITING if self._queue else ""
            key = _key(folder)
            if self._current and _key(self._current) == key:
                return RUNNING if self._running else WAITING
            if any(_key(f) == key for f in self._queue):
                return WAITING
            return ""

    def wait_reason(self, folder):
        """What a queued pass is waiting for: "recording", "dictation", "file" or "notes" (what
        ``is_busy`` named), "queue" (another meeting's pass is ahead), "settling" (the computer is
        free: it stays quiet for a moment first, or the pass was only just queued) - and "" when
        ``folder`` is not waiting."""
        with self._lock:
            key = _key(folder)
            if self._current and _key(self._current) == key:
                return "" if self._running else (self._reason_now or R_SETTLING)
            for i, f in enumerate(self._queue):
                if _key(f) == key:
                    ahead = i + (1 if self._current else 0)
                    return R_QUEUE if ahead else R_SETTLING
            return ""

    def snapshot(self, folder):
        """Everything the banner needs about ``folder``'s pass, as one dict: ``state`` ("" /
        "waiting" / "running"), ``stopping`` (Cancel was pressed, the worker has not ended yet),
        ``waiting_for`` (wait_reason), ``stage`` ("" | "reading" | "voices"), ``done`` and ``total``
        (the pass's own count, 0 until it has one), ``fraction`` (0..1, None while unknown),
        ``eta_s`` (seconds left from this try's pace, None until it is worth saying) and ``tries``
        (interruptions so far)."""
        state = self.state(folder)
        out = {"state": state, "stopping": False, "waiting_for": "", "stage": "", "done": 0, "total": 0,
               "fraction": None, "eta_s": None, "tries": 0}
        if not state:
            return out
        key = _key(folder)
        with self._lock:
            out["tries"] = self._retries.get(key, 0)
            current = bool(self._current) and _key(self._current) == key
            out["stopping"] = bool(current and self._cancel.is_set())
            if state == WAITING:
                out["waiting_for"] = self.wait_reason(folder)
                return out
            p = self._prog.get(key) or {}
            out["stage"] = p.get("stage", "")
            if out["stage"] == "voices" and p.get("total", 0) > 0:
                done, total = int(p.get("done", 0)), int(p["total"])
                out["done"], out["total"] = done, total
                out["fraction"] = max(0.0, min(1.0, done / float(total)))
                made = done - int(p.get("first_done", done))
                span = float(p.get("last_t", 0.0)) - float(p.get("first_t", 0.0))
                if made >= ETA_MIN_DONE and span >= ETA_MIN_SECONDS:
                    out["eta_s"] = max(0.0, (total - done) * span / made)
        return out

    def take_result(self, folder):
        """How ``folder``'s last job ended, once: ``(result, how)`` - the pass's result (None when it
        never ran) and how: "done", "cancelled" (the person stopped it) or "gave_up" (other work
        interrupted it too often). ``(None, "")`` when there is nothing to take."""
        with self._lock:
            return self._results.pop(_key(folder), (None, ""))

    def folders(self):
        """The meetings queued or being worked on, the current one first."""
        with self._lock:
            return ([self._current] if self._current else []) + list(self._queue)

    def retries(self, folder):
        """Interruptions so far of a job that is still queued or running (0 once it ended)."""
        return self._retries.get(_key(folder), 0)

    def wait_idle(self, folder, timeout=20.0):
        """Wait until ``folder`` is neither queued nor being worked on - after a
        ``cancel`` that is: its running pass has really stopped and let go of the
        meeting's files. True when it has, False after ``timeout`` seconds. Never call
        it from the pass's own thread (the pass would wait for itself): there it
        answers False at once."""
        end = time.monotonic() + max(0.0, float(timeout))
        while self.state(folder):
            if threading.get_ident() == self._worker_ident or time.monotonic() >= end:
                return False
            time.sleep(0.02)
        return True

    def cancel(self, folder=None):
        """Stop ``folder``'s pass (or every pass, with none) for good: a queued one
        is dropped, the running one ends at once. True when there was something."""
        hit = []
        with self._lock:
            key = _key(folder) if folder else None
            keep = []
            for f in self._queue:
                (hit if key is None or _key(f) == key else keep).append(f)
            self._queue[:] = keep
            for f in hit:
                self._results[_key(f)] = (None, HOW_CANCELLED)
                self._forget(_key(f))
            if self._current and (key is None or _key(self._current) == key):
                hit.append(self._current)
                self._cancel.set()
                self._wake.set()
        for f in hit:
            self._changed(f)
        return bool(hit)

    def shutdown(self):
        """The app is quitting: end the running pass, forget the rest."""
        self._quit.set()
        self._wake.set()
        with self._lock:
            dropped = list(self._queue)
            self._queue.clear()
        for f in dropped:
            self._changed(f)

    # ── the worker ──
    def _sleep(self, seconds):
        self._wake.wait(seconds)
        self._wake.clear()

    def _changed(self, folder):
        if self._on_change is None:
            return
        try:
            self._on_change(str(folder))
        except Exception:
            logger.debug("speaker pass change callback failed", exc_info=True)

    def _say(self, text):
        self.status_text = str(text or "")

    def _progress(self, folder, stage, done, total):
        now = self._clock()
        key = _key(folder)
        with self._lock:
            p = self._prog.setdefault(key, {})
            p["stage"] = str(stage or "")
            if stage == "voices":
                if p.get("first_t") is None:
                    p["first_t"], p["first_done"] = now, int(done)
                p["done"], p["total"], p["last_t"] = int(done), int(total), now
        if stage == "voices" and total:
            self.status_text = "Telling the voices apart… %d%%" % int(100.0 * done / total)
        elif stage == "reading":
            self.status_text = "Reading the call's sound…"

    def _forget(self, key):
        """Everything kept for a job that is over (or dropped)."""
        self._resume.pop(key, None)
        self._retries.pop(key, None)
        self._cooling.discard(key)
        self._prog.pop(key, None)

    def _finish(self, folder, result, how):
        """The job is over for good: keep how it ended for the page, drop everything else of it."""
        key = _key(folder)
        with self._lock:
            self._results[key] = (result, how)
            self._forget(key)

    def _check_worker(self):
        """A worker thread that is gone (it died without cleaning up) must not leave a meeting
        "waiting" or "running" for ever: say so, clear it, and give what is still queued a new worker."""
        ident = self._worker_ident
        if ident is None:
            return
        try:
            if any(t.ident == ident for t in threading.enumerate()):
                return
        except Exception:
            return
        restart = False
        dead = ""
        with self._lock:
            if self._worker_ident != ident:
                return
            dead = self._current
            logger.warning("The speaker pass thread ended without finishing; its state was cleared")
            self._current, self._running = "", False
            self._worker_ident = None
            self._alive = False
            if dead:
                self._results[_key(dead)] = (_Crashed(), HOW_DONE)
                self._forget(_key(dead))
            if self._queue and not self._quit.is_set():
                self._alive = restart = True
        if dead:
            self._changed(dead)
        if restart:
            try:
                self._spawn(self._loop)
            except Exception:
                with self._lock:
                    self._alive = False
                    dropped, self._queue[:] = list(self._queue), []
                logger.warning("The speaker pass could not be restarted", exc_info=True)
                for f in dropped:
                    self._changed(f)

    def _busy_reason(self):
        """"" when the computer is free, else what needs it ("busy" when it is not named)."""
        try:
            busy = self._is_busy()
        except Exception:
            logger.debug("speaker pass busy check failed", exc_info=True)
            return "busy"                    # when in doubt, leave the computer alone
        if not busy:
            return ""
        return busy if isinstance(busy, str) and busy else "busy"

    def _safe_busy(self):
        return bool(self._busy_reason())

    def _should_cancel(self):
        """Polled by the running pass (several times a second)."""
        if self._quit.is_set() or self._cancel.is_set():
            return True
        if self._safe_busy():
            self._interrupted = True
            return True
        return False

    def _loop(self):
        self._worker_ident = threading.get_ident()
        try:
            while True:
                with self._lock:
                    if self._quit.is_set() or not self._queue:
                        self._alive = False
                        self._worker_ident = None
                        return
                    folder = self._queue.pop(0)
                    self._current = folder
                    self._running = False
                    self._reason_now = ""
                    self._cancel.clear()
                    self._interrupted = False
                try:
                    self._work(folder)
                except Exception:
                    logger.warning("The speaker pass of a meeting failed", exc_info=True)
                    self._finish(folder, _Crashed(), HOW_DONE)
                finally:
                    with self._lock:
                        self._current = ""
                        self._running = False
                        self._reason_now = ""
                    self._changed(folder)
        except BaseException:
            with self._lock:
                self._alive = False
                self._worker_ident = None
            raise

    def _work(self, folder):
        self._changed(folder)                             # waiting
        # 1. wait until nothing else needs the computer - and, once it was busy, until it stays
        #    quiet for a few looks (a pass interrupted before has been busy for sure)
        key = _key(folder)
        was_busy = key in self._cooling
        self._cooling.discard(key)
        quiet = 0
        while True:
            if self._quit.is_set() or self._cancel.is_set():
                self._finish(folder, None, HOW_CANCELLED)
                return
            reason = self._busy_reason()
            if reason:
                was_busy, quiet = True, 0
                self._reason_now = reason
            elif not was_busy or quiet >= self.settle_polls:
                break
            else:
                quiet += 1
                self._reason_now = R_SETTLING
            self._wait(self.poll_s)
        with self._lock:
            self._running = True
            self._reason_now = ""
            self._prog[key] = {"stage": ""}               # a new try: its own pace
            resume = self._resume.setdefault(key, {})
        self._changed(folder)
        # 2. the pass; it stops by itself when the computer gets busy
        result = self._run(folder, self._should_cancel, PassStatus(self, folder, resume))
        if getattr(result, "state", "") != "cancelled":
            self._finish(folder, result, HOW_DONE)        # it ended: done, nothing to do, or it said why not
            return
        if self._quit.is_set() or self._cancel.is_set() or not self._interrupted:
            self._finish(folder, result, HOW_CANCELLED)   # on request: no retry
            return
        with self._lock:
            self._retries[key] = tries = self._retries.get(key, 0) + 1
        if tries > self.max_retries:
            logger.info("The speaker pass of a meeting was interrupted %d times; left for "
                        "Identify speakers", tries)
            self._finish(folder, result, HOW_GAVE_UP)
            return
        logger.info("The speaker pass was interrupted (%d of %d); it tries again when the computer is free",
                    tries, self.max_retries)
        with self._lock:
            self._cooling.add(key)
            self._prog.pop(key, None)
            self._queue.append(folder)                    # later, when idle
