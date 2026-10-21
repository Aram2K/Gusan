"""A field watchdog for a frozen window ("Gusan is not responding").

The GUI thread beats (``beat()``, from a 1 s QTimer in main.py); a daemon thread
checks the beat once a second. When the GUI has not beaten for THRESHOLD_S
(5 s, Windows' own "not responding" mark) it writes ONE warning to the log with
where the GUI thread is stuck and what every other thread is doing, writes
another at most every RELOG_S (30 s) while the freeze lasts, and says how long
it was when the window answers again. Nothing else: no files, no text of what
the user said, no names of their files - only thread names and source
locations (file, line, function) of this program and its libraries.

Qt-free on purpose (it only needs a ``beat()`` caller) and testable with fake
clocks: ``check(now)`` is the whole decision, the thread only calls it.

Second layer, for the worst case: a native call that keeps the interpreter lock
for minutes freezes this thread too, so nothing can be written until it ends.
``faulthandler.dump_traceback_later`` runs on a C thread that needs no
interpreter lock: this thread re-arms it while it is alive, and when it is not
(the whole process stalled for FAULT_DUMP_S) the C thread dumps every Python
thread's stack - including the one that holds the lock - to the same log file.

A computer that sleeps (a closed lid) or a debugger that stops the program also
holds this thread up - but the program used no CPU meanwhile, where a native call
that keeps the interpreter lock burns it. So a look that comes much too late with
almost no CPU used in between is "the computer slept": nothing is logged as a
frozen window and the watch starts over.

Off under pytest unless TRANSCRIBE_STALL_WATCHDOG=1; TRANSCRIBE_STALL_WATCHDOG=0
turns it off everywhere. Cost: one thread waking once a second, one timer slot
once a second.
"""
import faulthandler
import logging
import os
import sys
import threading
import time
import traceback

THRESHOLD_S = 5.0        # no beat for this long = the window is "not responding"
RELOG_S = 30.0           # while it lasts: at most one more entry per this
POLL_S = 1.0             # how often the watchdog thread looks
BEAT_MS = 1000           # how often the GUI beats (main.py's QTimer interval)
FAULT_DUMP_S = 45.0      # whole process stalled this long: C-level dump of every thread
FAULT_REARM_S = 10.0     # how often the live watchdog pushes that deadline back
SLEEP_CPU_SHARE = 0.05   # a late look that used less CPU than this share of the gap = the computer slept
MAX_FRAMES = 14          # frames kept of the GUI thread's stack (innermost last)
MAX_THREADS = 16         # other threads listed
MAX_ENTRIES = 12         # entries written for ONE freeze (30 s apart: 6 minutes), then silent
ENV = "TRANSCRIBE_STALL_WATCHDOG"

_log = logging.getLogger("transcribe")


def enabled_by_environment(environ=None, modules=None):
    """False under pytest (unless asked: TRANSCRIBE_STALL_WATCHDOG=1) and when
    TRANSCRIBE_STALL_WATCHDOG=0."""
    environ = os.environ if environ is None else environ
    modules = sys.modules if modules is None else modules
    flag = str(environ.get(ENV, "")).strip().lower()
    if flag in ("0", "off", "false", "no"):
        return False
    if flag in ("1", "on", "true", "yes"):
        return True
    return "pytest" not in modules and "PYTEST_CURRENT_TEST" not in environ


def _where(frame):
    """'pages/meeting_views.py:1812 in render' - the last two path parts only."""
    code = frame.f_code
    path = (code.co_filename or "?").replace("\\", "/")
    short = "/".join(path.split("/")[-2:])
    return "%s:%d in %s" % (short, frame.f_lineno, code.co_name)


def format_stack(frame, limit=MAX_FRAMES):
    """The frames of one thread, outermost first, as text lines. Never includes
    variables or source lines (those could hold what the user typed)."""
    stack = []
    while frame is not None and len(stack) < 200:
        stack.append(frame)
        frame = frame.f_back
    stack.reverse()
    if len(stack) > limit:
        skipped = len(stack) - limit
        lines = ["    ... %d outer frames ..." % skipped]
        stack = stack[-limit:]
    else:
        lines = []
    lines.extend("    " + _where(f) for f in stack)
    return lines


def log_stream(logger=None):
    """The open log file of the root logger (or the given logger's chain), for
    faulthandler. None when the log is not a plain file."""
    logger = logger or logging.getLogger()
    seen = logger
    while seen is not None:
        for h in getattr(seen, "handlers", ()):
            stream = getattr(h, "stream", None)
            try:
                if stream is not None and not stream.closed and stream.fileno() >= 0:
                    return stream
            except Exception:
                continue
        seen = seen.parent if getattr(seen, "propagate", True) else None
    return None


class StallWatchdog:
    def __init__(self, logger=None, *, threshold_s=THRESHOLD_S, relog_s=RELOG_S, poll_s=POLL_S,
                 clock=time.monotonic, gui_thread_id=None, frames=None, threads=None,
                 fault_stream=None, fault_dump_s=FAULT_DUMP_S, fault_rearm_s=FAULT_REARM_S,
                 cpu_clock=None):
        self.log = logger or _log
        self.threshold_s = float(threshold_s)
        self.relog_s = float(relog_s)
        self.poll_s = float(poll_s)
        self._clock = clock
        self.gui_thread_id = gui_thread_id or threading.main_thread().ident
        self._frames = frames or sys._current_frames
        self._threads = threads or threading.enumerate
        self._fault_stream = fault_stream
        self.fault_dump_s = float(fault_dump_s)
        self.fault_rearm_s = float(fault_rearm_s)
        now = self._clock()
        self._last_beat = now          # written by the GUI thread
        self._long_gaps = []           # gaps >= threshold that a beat ended (GUI thread appends)
        self._in_stall = False
        self._stall_since = now        # the last beat before the freeze
        self._last_logged = 0.0
        self._entries = 0
        self._last_check = now
        self._last_arm = None
        # The process's CPU time: how a sleeping computer is told from a stalled
        # program (only for the real clock; a test's fake clock has no CPU to read).
        if cpu_clock is None and clock is time.monotonic:
            cpu_clock = time.process_time
        self._cpu = cpu_clock
        self._last_cpu = self._cpu() if self._cpu is not None else 0.0
        self._stop = threading.Event()
        self._thread = None
        self.stalls = 0                # freezes seen (read by tests)
        self.slept = 0                 # looks that came after the computer slept (not freezes)
        self.longest_s = 0.0

    # ── the GUI thread's side ──
    def beat(self):
        """Called by the GUI thread's timer: 'I am running'."""
        now = self._clock()
        gap = now - self._last_beat
        self._last_beat = now
        if gap >= self.threshold_s:
            self._long_gaps.append(gap)

    # ── the decision (no thread, no sleeping: tests call it with a fake clock) ──
    def check(self, now=None):
        """One look. Returns "stall" (first entry of a freeze), "still" (another
        entry of the same freeze), "recovered" (the window answers again), or
        None."""
        now = self._clock() if now is None else now
        gap_since_look = now - self._last_check
        late = gap_since_look - self.poll_s                 # this thread itself was held up
        self._last_check = now
        if self._cpu is not None:
            cpu = self._cpu()
            used, self._last_cpu = cpu - self._last_cpu, cpu
            if late >= self.threshold_s and used < SLEEP_CPU_SHARE * gap_since_look:
                return self._slept(now, gap_since_look)
        done = self._long_gaps[:]                            # freezes a beat has ended since the last look
        del self._long_gaps[:len(done)]
        gap = max(done) if done else 0.0
        silent = now - self._last_beat
        if not self._in_stall:
            if silent >= self.threshold_s:
                self._in_stall = True
                self._stall_since = self._last_beat
                self._last_logged = now
                self._entries = 1
                self.stalls += 1
                self._write("stall", silent, late)
                return "stall"
            if done:
                # The whole freeze fell between two looks (or this thread was frozen too).
                self.stalls += 1
                self._recovered(gap, late, seen=False)
                return "recovered"
            return None
        if done:                                            # a beat since the freeze began
            self._in_stall = False
            self._recovered(gap, late, seen=True)
            return "recovered"
        if now - self._last_logged >= self.relog_s and self._entries < MAX_ENTRIES:
            self._last_logged = now
            self._entries += 1
            self._write("still", silent, late)
            return "still"
        return None

    def _slept(self, now, gap):
        """This thread did not run for ``gap`` s and the program used almost no CPU
        in that time: the computer slept (or a debugger stopped it). Nothing was
        frozen: forget the silence and start over."""
        self.slept += 1
        self._last_beat = now
        del self._long_gaps[:]
        self._in_stall = False
        try:
            self.log.info("Gusan was paused for %.0f s (the computer slept or a debugger stopped it) - "
                          "not a frozen window", gap)
        except Exception:
            pass
        return "slept"

    def _recovered(self, gap, late, seen):
        self.longest_s = max(self.longest_s, gap)
        extra = ""
        if late >= self.threshold_s:
            extra = " (this watchdog was held up %.0f s too: the whole app was stalled)" % late
        if not seen:
            extra += " (it ended before the watchdog could look at the stack)"
        try:
            self.log.warning("GUI thread answering again after %.1f s%s", gap, extra)
        except Exception:
            pass

    def _write(self, kind, silent, late):
        self.longest_s = max(self.longest_s, silent)
        try:
            self.log.warning("%s", self._report(kind, silent, late))
        except Exception:
            pass

    def _report(self, kind, silent, late):
        if kind == "stall":
            head = "GUI thread is not responding: no sign of life for %.1f s" % silent
        else:
            head = "GUI thread still not responding: %.0f s so far" % silent
        if late >= self.threshold_s:
            head += " (this watchdog was held up %.0f s as well: another thread held the interpreter lock, or the computer slept)" % late
        lines = [head]
        try:
            frames = self._frames()
        except Exception:
            frames = {}
        gui = frames.get(self.gui_thread_id)
        lines.append("  GUI thread is in:")
        if gui is not None:
            lines.extend(format_stack(gui))
        else:
            lines.append("    (not found)")
        others = []
        try:
            live = list(self._threads())
        except Exception:
            live = []
        for t in live:
            ident = getattr(t, "ident", None)
            if ident is None or ident == self.gui_thread_id or ident == threading.get_ident():
                continue
            frame = frames.get(ident)
            where = _where(frame) if frame is not None else "?"
            others.append("    %s%s: %s" % (getattr(t, "name", "?"),
                                              " (daemon)" if getattr(t, "daemon", False) else "", where))
        lines.append("  other threads (%d):" % len(others))
        lines.extend(others[:MAX_THREADS])
        if len(others) > MAX_THREADS:
            lines.append("    ... and %d more" % (len(others) - MAX_THREADS))
        return "\n".join(lines)

    # ── the thread ──
    def _arm_fault_dump(self, now=None):
        """Push the C-level whole-process dump deadline back (this thread is alive)."""
        stream = self._fault_stream
        if stream is None or self.fault_dump_s <= 0:
            return
        now = time.monotonic() if now is None else now
        if self._last_arm is not None and now - self._last_arm < self.fault_rearm_s:
            return
        self._last_arm = now
        try:
            faulthandler.dump_traceback_later(self.fault_dump_s, repeat=True, file=stream)
        except Exception:
            self._fault_stream = None

    def run(self):
        while not self._stop.wait(self.poll_s):
            try:
                self.check()
                self._arm_fault_dump()
            except Exception:
                try:
                    self.log.debug("stall watchdog check failed", exc_info=True)
                except Exception:
                    pass

    def start(self):
        if self._thread is not None:
            return self
        self._stop.clear()
        self._last_beat = self._last_check = self._clock()
        if self._cpu is not None:
            self._last_cpu = self._cpu()
        self._thread = threading.Thread(target=self.run, name="gusan-stall-watchdog", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._fault_stream is not None:
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:
                pass
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)


def start(logger=None, environ=None, modules=None, **kw):
    """Start the watchdog for the running app (main.py, GUI thread) and return
    it - the caller wires ``beat`` to a BEAT_MS timer - or None when it is off
    (tests, TRANSCRIBE_STALL_WATCHDOG=0). Never raises."""
    try:
        if not enabled_by_environment(environ, modules):
            return None
        kw.setdefault("fault_stream", log_stream())
        return StallWatchdog(logger, **kw).start()
    except Exception:
        try:
            (logger or _log).debug("stall watchdog not started", exc_info=True)
        except Exception:
            pass
        return None
