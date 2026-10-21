"""The live transcription queue of a meeting: bounded, supervised, never silent.

A meeting cuts a piece of audio every few seconds and each piece is turned into
text while the next one is recorded. The old design started one thread per
piece and left them all waiting on the speech model: a slow computer fell
further and further behind with no limit, a thread that died took its piece
with it unnoticed, and nothing said how far behind the live text was.

ChunkPipeline replaces that for meetings (dictation keeps its own path):

  * one FIFO queue and ``workers`` worker threads (the speech model itself is
    serialized by the recorder, so one is the norm; cloud engines get more);
  * catch-up: when pieces are waiting, the oldest ones are joined into one
    longer piece (up to ``coalesce_max_sec``) - a Whisper pass costs about the
    same for 5 s as for 30 s, so a backlog melts instead of growing;
  * a hard bound on waiting audio (``max_backlog_sec``): past it the OLDEST
    pieces are dropped - counted, logged, reported through ``on_drop`` - and the
    recording itself keeps everything for the transcript written after Stop;
  * supervision: a worker that dies (anything but an ordinary exception in one
    piece, which is counted and survived) is replaced at the next ``submit`` /
    ``ensure_workers`` call, logged with a restart counter;
  * ``health()``: queued / done / failed / merged / dropped / restarts and the
    lag in seconds - numbers only, never text - for the one-line-a-minute log.

Pure Python (numpy only for the default merge); safe to import anywhere.
"""
import collections
import logging
import threading
import time

logger = logging.getLogger("transcribe")

DEFAULT_MAX_BACKLOG_SEC = 20 * 60.0     # audio waiting for the speech model
DEFAULT_COALESCE_MIN = 3                # this many waiting (the one taken included)
DEFAULT_COALESCE_MAX_SEC = 30.0         # a joined piece never exceeds this
LOG_EVERY_SEC = 30.0                    # at most one warning of a kind per this


def _numpy_merge(parts):
    import numpy as np
    return np.concatenate(parts)


class _Item:
    __slots__ = ("idx", "audio", "dur", "enq")

    def __init__(self, idx, audio, dur, enq):
        self.idx, self.audio, self.dur, self.enq = idx, audio, dur, enq


class ChunkPipeline:
    def __init__(self, fn, *, workers=1, sample_rate=16000, name="gusan-chunks",
                 max_backlog_sec=DEFAULT_MAX_BACKLOG_SEC, coalesce_min=DEFAULT_COALESCE_MIN,
                 coalesce_max_sec=DEFAULT_COALESCE_MAX_SEC, merge=None, on_merge=None,
                 on_drop=None, clock=time.monotonic, duration_of=None, log=None):
        """``fn(audio, idx)`` transcribes one piece (it records its own result;
        what it returns is ignored). ``merge(list of audio) -> audio`` joins
        pieces (numpy by default); ``on_merge(first_idx, [other idx...])`` runs
        before a joined piece is transcribed; ``on_drop([idx...], seconds)``
        after pieces were dropped for lack of room."""
        self._fn = fn
        self._workers = max(1, int(workers))
        self._rate = float(sample_rate)
        self._name = name
        self._max_backlog = float(max_backlog_sec)
        self._co_min = max(2, int(coalesce_min))
        self._co_max = float(coalesce_max_sec)
        self._merge = merge or _numpy_merge
        self._on_merge = on_merge
        self._on_drop = on_drop
        self._clock = clock
        self._duration_of = duration_of or (lambda audio: len(audio) / self._rate)
        self._log = log or logger
        self._cv = threading.Condition()
        self._q = collections.deque()
        self._queued_sec = 0.0
        self._closed = False
        self._aborted = False
        self._threads = []                   # every worker thread ever started
        self._live = {}                      # thread -> (idx, started, enqueued) while in a piece
        self._died = set()                   # worker threads that ended abnormally
        self._last_log = {}
        # counters (guarded by _cv)
        self.submitted = 0
        self.done = 0
        self.failed = 0
        self.merged = 0                      # pieces joined into an earlier one
        self.dropped = 0
        self.dropped_sec = 0.0
        self.restarts = 0
        self.last_error = ""

    # ── input ──
    def submit(self, idx, audio):
        """Queue one piece. False when the pipeline is closed or aborted - the
        caller then runs the piece itself (nothing is lost)."""
        dur = float(self._duration_of(audio))
        dropped = []
        with self._cv:
            if self._closed or self._aborted:
                return False
            self.submitted += 1
            self._q.append(_Item(idx, audio, dur, self._clock()))
            self._queued_sec += dur
            while self._queued_sec > self._max_backlog and len(self._q) > 1:
                old = self._q.popleft()
                self._queued_sec -= old.dur
                dropped.append(old)
            if dropped:
                self.dropped += len(dropped)
                self.dropped_sec += sum(i.dur for i in dropped)
            self._ensure_locked()
            self._cv.notify()
        if dropped:
            self._warn("drop", "Live transcription is behind: dropped %d piece(s) "
                       "(%.0f s of audio) to keep up; the recording keeps them",
                       len(dropped), sum(i.dur for i in dropped))
            if self._on_drop is not None:
                try:
                    self._on_drop([i.idx for i in dropped], sum(i.dur for i in dropped))
                except Exception:
                    self._log.debug("on_drop failed", exc_info=True)
        return True

    def close(self):
        """No more input: the workers finish what is queued, then exit."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def abort(self):
        """Cancel: queued pieces are discarded; a piece being transcribed
        finishes. Idempotent."""
        with self._cv:
            self._aborted = True
            self._closed = True
            self._q.clear()
            self._queued_sec = 0.0
            self._cv.notify_all()

    # ── supervision ──
    def ensure_workers(self):
        """Replace any worker that died (call once in a while; submit does)."""
        with self._cv:
            self._ensure_locked()

    def _ensure_locked(self):
        if self._aborted or (self._closed and not self._q):
            return
        alive = [t for t in self._threads if t.is_alive()]
        # Threads that are still starting up count as alive (is_alive is True
        # from start()), so a replacement is only made for a real death.
        for t in self._threads:
            if t not in alive and t in self._died:
                self._died.discard(t)
                self.restarts += 1
                self._warn("restart", "A live-transcription worker stopped unexpectedly; "
                           "started a new one (restart #%d)", self.restarts)
        while len(alive) < self._workers:
            t = threading.Thread(target=self._work, name="%s-%d" % (self._name, len(self._threads) + 1),
                                 daemon=True)
            self._threads.append(t)
            alive.append(t)
            t.start()

    def threads(self):
        """Every worker thread started so far (the recorder keeps them in
        _chunk_threads, so waiting for "the chunk threads" still works)."""
        with self._cv:
            return list(self._threads)

    # ── the worker ──
    def _take(self):
        with self._cv:
            while True:
                if self._aborted:
                    return None
                if self._q:
                    break
                if self._closed:
                    return None
                self._cv.wait(0.5)
            first = self._q.popleft()
            self._queued_sec -= first.dur
            parts = [first]
            if len(self._q) + 1 >= self._co_min:
                total = first.dur
                while self._q and total + self._q[0].dur <= self._co_max:
                    nxt = self._q.popleft()
                    self._queued_sec -= nxt.dur
                    total += nxt.dur
                    parts.append(nxt)
            self._queued_sec = max(0.0, self._queued_sec)
            if len(parts) > 1:
                self.merged += len(parts) - 1
            self._live[threading.current_thread()] = (first.idx, self._clock(), first.enq)
            return parts

    def _run_parts(self, parts):
        """Transcribe what _take handed over: one piece, or several joined into
        one (if joining fails, each piece goes on its own - none is lost)."""
        first = parts[0]
        audio = first.audio
        if len(parts) > 1:
            try:
                audio = self._merge([p.audio for p in parts])
            except Exception:
                self._log.warning("Joining %d waiting pieces failed; transcribing them one by one",
                                  len(parts), exc_info=True)
                with self._cv:
                    self.merged = max(0, self.merged - (len(parts) - 1))
                for p in parts:
                    self._fn(p.audio, p.idx)
                    with self._cv:
                        self.done += 1
                return
            if self._on_merge is not None:
                try:
                    self._on_merge(first.idx, [p.idx for p in parts[1:]])
                except Exception:
                    self._log.debug("on_merge failed", exc_info=True)
        self._fn(audio, first.idx)
        with self._cv:
            self.done += 1

    def _work(self):
        me = threading.current_thread()
        clean = False
        try:
            while True:
                parts = self._take()
                if parts is None:
                    clean = True
                    return
                first = parts[0]
                try:
                    self._run_parts(parts)
                except Exception as e:           # one piece's problem is not the worker's
                    with self._cv:
                        self.failed += 1
                        self.last_error = "%s: %s" % (type(e).__name__, str(e)[:200])
                    self._warn("fail", "Live transcription of piece %s failed (%s) - continuing",
                               first.idx, type(e).__name__, exc_info=True)
                except BaseException as e:       # the worker itself is going down
                    with self._cv:
                        self.failed += 1
                        self.last_error = "%s: %s" % (type(e).__name__, str(e)[:200])
                    raise
                finally:
                    with self._cv:
                        self._live.pop(me, None)
        finally:
            if not clean:
                with self._cv:
                    self._died.add(me)
                    self.last_error = self.last_error or "worker died"
                self._log.warning("Live transcription worker %s ended unexpectedly", me.name)

    # ── numbers ──
    def outstanding(self):
        """Pieces waiting or being transcribed."""
        with self._cv:
            return len(self._q) + len(self._live)

    def health(self):
        """A snapshot of the counters and the lag, for logging and tests."""
        now = self._clock()
        with self._cv:
            oldest = [self._q[0].enq] if self._q else []
            oldest += [enq for (_i, _s, enq) in self._live.values()]
            inflight = [now - started for (_i, started, _e) in self._live.values()]
            return {
                "queued": len(self._q), "inflight": len(self._live),
                "queued_sec": round(self._queued_sec, 1),
                "submitted": self.submitted, "done": self.done, "failed": self.failed,
                "merged": self.merged, "dropped": self.dropped,
                "dropped_sec": round(self.dropped_sec, 1), "restarts": self.restarts,
                "lag_sec": round(now - min(oldest), 1) if oldest else 0.0,
                "inflight_sec": round(max(inflight), 1) if inflight else 0.0,
                "workers": sum(1 for t in self._threads if t.is_alive()),
                "closed": self._closed, "aborted": self._aborted,
                "last_error": self.last_error,
            }

    def _warn(self, kind, msg, *args, **kw):
        """At most one warning of a kind every LOG_EVERY_SEC - a stuck queue
        must not flood the log."""
        now = self._clock()
        last = self._last_log.get(kind)
        if last is not None and now - last < LOG_EVERY_SEC:
            return
        self._last_log[kind] = now
        self._log.warning(msg, *args, **kw)
