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
    meeting just finished and queued itself (``enqueue``). After the computer was
    busy it also lets it stay quiet for SETTLE_POLLS looks first, so a person who
    dictates every few seconds does not get a pass started and stopped again and
    again (each start loads the speaker model for nothing);
  * if a recording, a dictation or a file job starts while it works, it stops at
    once (``should_cancel`` is polled by the child process' wait loop several times a
    second) and tries again when the computer is free - at most MAX_RETRIES times per
    queued job (a meeting stopped again, once continued, starts the count anew), after
    which it leaves the meeting as it is (the page keeps offering "Identify speakers");
  * "Cancel" and the app quitting end it for good; nothing is retried;
  * one pass at a time, the oldest meeting first; the worker thread exists only while
    there is something to do.

Qt-free; the callbacks are called from the worker thread.
"""
import logging
import os
import threading
import time

logger = logging.getLogger("transcribe")

POLL_S = 5.0            # how often a waiting pass looks whether the computer is free
MAX_RETRIES = 5         # interruptions one meeting's pass survives; the next one ends it
SETTLE_POLLS = 3        # quiet looks (about 15 s) in a row wanted after the computer was busy

WAITING = "waiting"     # queued: behind other work, or waiting for the computer to be free
RUNNING = "running"     # the diarizer works on it now


def _key(folder):
    return os.path.normcase(os.path.abspath(str(folder)))


def _thread(target):
    threading.Thread(target=target, daemon=True, name="gusan-speaker-pass").start()


class SpeakerFollowUp:
    def __init__(self, is_busy, run, on_change=None, spawn=None, poll_s=POLL_S,
                 max_retries=MAX_RETRIES, wait=None, settle_polls=SETTLE_POLLS):
        """``is_busy()``: True while a recording, dictation or file job needs the
        computer. ``run(folder, should_cancel, status)``: one pass; returns an object
        whose ``state`` is "cancelled" when it was stopped (anything else ends the
        meeting's turn). ``on_change(folder)``: the state of a meeting's pass changed
        (any thread). ``spawn(fn)`` starts the worker (a daemon thread by default).
        ``wait(seconds)`` sleeps between two looks (woken early by cancel / shutdown).
        ``settle_polls``: quiet looks in a row wanted before it starts again once the
        computer was busy (a pass that finds it free at once starts at once)."""
        self._is_busy = is_busy
        self._run = run
        self._on_change = on_change
        self._spawn = spawn or _thread
        self.poll_s = poll_s
        self.max_retries = max_retries
        self.settle_polls = max(0, int(settle_polls))
        self._cooling = set()               # folder keys that were interrupted: let the computer settle first
        self._lock = threading.RLock()
        self._queue = []                    # folders waiting, oldest first
        self._current = ""                  # the folder being worked on (waiting for the computer, or running)
        self._running = False
        self._alive = False                 # a worker thread exists
        self._worker_ident = None           # its thread id (wait_idle must not wait on itself)
        self._retries = {}                  # folder key -> interruptions so far
        self._interrupted = False           # the running pass was stopped because the computer got busy
        self._cancel = threading.Event()    # "Cancel" for the current meeting
        self._quit = threading.Event()      # the app is quitting
        self._wake = threading.Event()
        self._wait = wait or self._sleep
        self.status_text = ""

    # ── the public side ──
    def enqueue(self, folder):
        """Queue ``folder`` (a meeting whose notes are done). False when it is
        already queued or being worked on, or the app is quitting."""
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
            self._queue.append(folder)
            start = not self._alive
            if start:
                self._alive = True
        self._changed(folder)
        if start:
            try:
                self._spawn(self._loop)
            except Exception:
                with self._lock:
                    self._alive = False
                logger.warning("The speaker pass could not be started", exc_info=True)
        return True

    def state(self, folder=None):
        """"" (nothing queued or running), "waiting" or "running" - for ``folder``,
        or, with none, for any meeting."""
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

    def folders(self):
        """The meetings queued or being worked on, the current one first."""
        with self._lock:
            return ([self._current] if self._current else []) + list(self._queue)

    def retries(self, folder):
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

    def _safe_busy(self):
        try:
            return bool(self._is_busy())
        except Exception:
            logger.debug("speaker pass busy check failed", exc_info=True)
            return True                      # when in doubt, leave the computer alone

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
                    self._cancel.clear()
                    self._interrupted = False
                try:
                    self._work(folder)
                except Exception:
                    logger.warning("The speaker pass of a meeting failed", exc_info=True)
                finally:
                    with self._lock:
                        self._current = ""
                        self._running = False
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
                return
            if self._safe_busy():
                was_busy, quiet = True, 0
            elif not was_busy or quiet >= self.settle_polls:
                break
            else:
                quiet += 1
            self._wait(self.poll_s)
        with self._lock:
            self._running = True
        self._changed(folder)
        # 2. the pass; it stops by itself when the computer gets busy
        result = self._run(folder, self._should_cancel, self._say)
        if getattr(result, "state", "") != "cancelled":
            return                                        # it ended: done, nothing to do, or it said why not
        if self._quit.is_set() or self._cancel.is_set() or not self._interrupted:
            return                                        # on request: no retry
        with self._lock:
            self._retries[key] = tries = self._retries.get(key, 0) + 1
        if tries > self.max_retries:
            logger.info("The speaker pass of a meeting was interrupted %d times; left for "
                        "Identify speakers", tries)
            return
        logger.info("The speaker pass was interrupted (%d of %d); it tries again when the computer is free",
                    tries, self.max_retries)
        with self._lock:
            self._cooling.add(key)
            self._queue.append(folder)                    # later, when idle
