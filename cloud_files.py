"""Audio to text on a cloud model (Gusan 2.0): one file, in pieces.

The decoded file (16 kHz mono float32, any length) is cut into pieces at
pauses, the pieces go to Gusan Cloud or the user's own key a few at a time
(cloud_speech.send, one request each), and the text comes back in file order
with estimated times. Qt-free; numpy at the top, faster-whisper's VAD
imported the first time a job needs it.

* result = transcribe_file(audio, spec, emit=emit, cancelled=should_cancel,
                           start_pct=_P_READ_END, end_pct=78 or 96)
  on the job thread, right after decode. ``spec`` is cloud_speech.Spec
  (spec_from_cfg with the live cfg: Privacy mode and the own key are checked
  again before every piece is sent). ``emit(pct, stage, eta)`` is only ever
  called from that thread. Returns {"segments": [{start, end, text}],
  "language", "model_id", "stats"}. Speaker labels are NOT run here:
  file_jobs runs them after all pieces, as for a local job.
  Raises:
    Cancelled          -> JobCancelled
    NoSpeech           -> no speech found (nothing was sent), or no piece
                          came back with words: MSG_NO_SPEECH
    CloudUnavailable   -> Gusan Cloud can't be used at the first piece (daily
                          cap, Pro ended, no token). Nothing came back yet, so
                          the job goes on on this computer (Automatic). It is
                          a CloudFileError: catch it first.
    CloudFileError     -> ``message`` is one plain sentence for the card.
* How a file is cut (PROFILES): speech_probs runs the VAD a 5-minute block at
  a time, RegionTracker turns that into speech regions, and ONE streaming
  ChunkPlanner turns those into pieces as they arrive, so the first upload
  starts after the first block. Cuts are only in pauses, near the profile's
  target, never over hard_max; long silences are never sent.
* Times are estimates: each piece's sentences (split_sentences) are spread
  over its speech by length and snapped to pauses (spread_segments).
"""

import collections
import dataclasses
import io
import logging
import queue
import random
import re
import threading
import time
import wave
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass

import numpy as np

import cloud_speech
import vocabulary

logger = logging.getLogger("transcribe.cloud_files")

SAMPLE_RATE = 16000
WINDOW = 512                         # VAD window, 32 ms
WIN_S = WINDOW / SAMPLE_RATE
BLOCK_S = 300                        # the VAD runs on 5-minute blocks
SPEECH_ON = 0.4                      # VAD probability where speech starts...
SPEECH_OFF = 0.25                    # ...and below which silence counts
LONG_PAUSE = 2.5                     # seconds; closes a piece, never sent
SNAP_PAUSE = 0.25                    # sentence boundaries move into a pause this long...
SNAP_RANGE = 1.5                     # ...when it is this close
MIN_SENTENCE = 12                    # characters; shorter parts join a neighbour
MAX_SENTENCE = 250                   # characters; longer ones are halved

MAX_ATTEMPTS = 4                     # tries per piece
BACKOFF = (2, 6, 20)                 # seconds before the 2nd, 3rd and 4th try
JITTER = 0.3                         # +-30%
RETRY_AFTER_MAX = 60                 # a longer Retry-After is capped
RATE_LIMIT = "rate_limit"            # cloud_speech's kind for a busy own key
_POLL = 0.25                         # seconds; how often the coordinator looks at Cancel

_KEY_FIELDS = {"google": "google_api_key", "mistral": "mistral_api_key"}


# ── profiles ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Profile:
    """How one engine's pieces are cut and sent (seconds)."""
    target: float          # aim for pieces about this long
    cut_lo: float          # once a piece reaches cut_lo it ends at the longest
    cut_hi: float          #   pause that keeps it at or under cut_hi
    min_s: float           # a long pause only closes a piece this long
    hard_max: float        # never longer
    workers: int           # requests in flight


PROFILES = {
    # Gusan Cloud: 60 s WAV pieces (about 2.6 MB through the proxy), 4 at a time.
    "gusan": Profile(60, 45, 75, 30, 90, 4),
    # Own keys are limited by requests per minute and per day: fewer, longer pieces.
    "google": Profile(120, 90, 150, 60, 240, 2),
    "mistral": Profile(120, 90, 150, 60, 240, 2),
}


def profile_for(spec):
    return PROFILES.get(getattr(spec, "engine", None), PROFILES["gusan"])


# ── errors ───────────────────────────────────────────────────────────────────
class Cancelled(Exception):
    """The user cancelled the job."""


class NoSpeech(Exception):
    """No speech in the file, or no piece came back with words."""


class CloudFileError(Exception):
    """The job failed. ``message``: one plain sentence for the card (never
    provider text); ``kind``: cloud_speech's error kind, or "error"."""

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.message = message
        self.kind = kind


class CloudUnavailable(CloudFileError):
    """Gusan Cloud can't be used at the first piece (daily cap, Pro ended, no
    token). Nothing came back yet, so the job can run on this computer
    instead and models are never mixed."""


class _Stopped(Exception):
    """A worker saw the job stop (cancel, or another piece failed)."""


# ── pieces of audio ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Chunk:
    """One piece: a contiguous slice ``start``..``end`` of the file (seconds)
    and its speech regions ((start, end), ...)."""
    index: int
    start: float
    end: float
    speech: tuple = ()


def encode_wav(audio, start, end, rate=SAMPLE_RATE):
    """audio[start s:end s] as 16-bit mono WAV bytes, clipped to [-1, 1]. The
    one place to switch the upload format later."""
    a = max(0, int(round(start * rate)))
    b = min(len(audio), int(round(end * rate)))
    part = np.clip(np.asarray(audio[a:max(a, b)], dtype=np.float32), -1.0, 1.0)
    pcm = np.rint(part * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ── finding speech ───────────────────────────────────────────────────────────
_VAD = {"model": None, "tried": False}
_VAD_LOCK = threading.Lock()


def _vad_model():
    """faster-whisper's Silero VAD (loaded once), or None when it can't load."""
    with _VAD_LOCK:
        if not _VAD["tried"]:
            _VAD["tried"] = True
            try:
                from faster_whisper.vad import get_vad_model
                _VAD["model"] = get_vad_model()
            except Exception as e:
                logger.warning("VAD unavailable (%s); finding speech by loudness", e)
        return _VAD["model"]


def energy_probs(block):
    """The VAD's stand-in, one value per 32 ms window: main's loudness rule
    (speech above max(4 x the 10th-percentile level, 0.006)), scaled so that
    level is exactly SPEECH_ON."""
    n = len(block) // WINDOW
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = np.asarray(block[:n * WINDOW], dtype=np.float32).reshape(n, WINDOW)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    p10 = float(np.partition(rms, n // 10)[n // 10])        # main: sorted(levels)[len // 10]
    thr = max(4.0 * p10, 0.006)
    return np.minimum(1.0, SPEECH_ON * rms / thr).astype(np.float32)


def speech_probs(audio, block_s=BLOCK_S, stop=None):
    """Speech probability per 32 ms window, yielded a 5-minute block at a time
    (the last one padded to a whole window). Never the whole file at once:
    get_speech_timestamps on a 5 h array costs about 2.6 GB more memory.
    Falls back to energy_probs when the VAD can't load or run."""
    model = _vad_model()
    step = max(WINDOW, int(block_s * SAMPLE_RATE) // WINDOW * WINDOW)
    for a in range(0, len(audio), step):
        if stop is not None and stop.is_set():
            return
        part = audio[a:a + step]
        # A fresh copy: the VAD writes into the array it is given.
        block = np.zeros(-(-len(part) // WINDOW) * WINDOW, dtype=np.float32)
        block[:len(part)] = part
        probs = None
        if model is not None:
            try:
                probs = np.asarray(model(block), dtype=np.float32).reshape(-1)
                if len(probs) != len(block) // WINDOW:
                    raise ValueError(f"{len(probs)} values for {len(block) // WINDOW} windows")
            except Exception as e:
                logger.warning("VAD failed (%s); finding speech by loudness", e)
                model, probs = None, None
        yield probs if probs is not None else energy_probs(block)


class RegionTracker:
    """Speech regions from per-window probabilities, block after block (a
    region open at a block edge carries on).

    Speech starts at ``thr`` and ends after ``min_silence`` seconds below
    ``off`` (Silero's rule). Regions get ``pad`` seconds each side, half the
    gap when two are closer. With ``max_len`` no region is longer: one that
    would be ends at a short pause in progress, else at its quietest 32 ms
    window in [0.6, 1] x max_len (the two halves touch).

    feed(probs) -> [(start, end), ...] in seconds, the regions now finished
    finish(total) -> the rest, clamped to the file
    """

    def __init__(self, thr=SPEECH_ON, off=SPEECH_OFF, min_silence=0.3, pad=0.2, max_len=None):
        self.thr, self.off, self.pad = thr, off, pad
        self._min_sil = max(1, int(round(min_silence / WIN_S)))
        self._max_win = max(2, int((max_len - 2 * pad) / WIN_S)) if max_len else None
        self._i = 0                  # the next window's index in the file
        self._start = None           # the open region's first window
        self._split = False          # ...right after a cut (no pad before it)
        self._quiet = None           # first window of the silence in progress
        self._probs = []             # the open region's values (for a cut)
        self._pending = None         # [start, end, after a cut, cut after] waiting for its end pad

    def feed(self, probs):
        out = []
        values = probs.tolist() if hasattr(probs, "tolist") else probs
        for p in values:
            i = self._i
            self._i += 1
            if self._start is None:
                if p >= self.thr:
                    self._start, self._quiet, self._probs = i, None, [p]
                continue
            if self._max_win:
                self._probs.append(p)
            if p >= self.thr:
                self._quiet = None
            elif p < self.off and self._quiet is None:
                self._quiet = i
            if self._quiet is not None and i + 1 - self._quiet >= self._min_sil:
                self._close(self._quiet, False, out)
            elif self._max_win and i + 1 - self._start >= self._max_win:
                if self._quiet is not None:
                    self._close(self._quiet, False, out)
                else:
                    lo = int(self._max_win * 0.6)
                    vals = self._probs[lo:self._max_win]
                    rel = lo + min(range(len(vals)), key=vals.__getitem__)
                    cut, rest = self._start + rel, self._probs[rel:]
                    self._close(cut, True, out)
                    self._start, self._split, self._probs = cut, True, rest
        return out

    def finish(self, total):
        out = []
        if self._start is not None:
            self._close(self._quiet if self._quiet is not None else self._i, False, out)
        prev, self._pending = self._pending, None
        if prev is not None:
            end = prev[1] if prev[3] else prev[1] + self.pad
            start, end = min(prev[0], total), min(end, total)
            if end > start:
                out.append((start, end))
        return out

    def _close(self, end, cut, out):
        region = [self._start * WIN_S, end * WIN_S, self._split, cut]
        self._start, self._quiet, self._probs, self._split = None, None, [], False
        prev = self._pending
        if prev is None:
            if not region[2]:
                region[0] = max(0.0, region[0] - self.pad)
        else:
            if not region[2]:
                each = min(self.pad, max(0.0, region[0] - prev[1]) / 2)
                prev[1] += each
                region[0] -= each
            out.append((prev[0], prev[1]))
        self._pending = region


class ChunkPlanner:
    """Speech regions in, pieces out, as the regions arrive.

    * Cut only in pauses. Once a piece passes cut_hi it ends at the longest
      pause between cut_lo and cut_hi (nearest the target on a tie); with
      none there, at the longest one between min_s and hard_max as soon as a
      pause after cut_hi is known. Never over hard_max (RegionTracker's
      regions never are).
    * A pause of LONG_PAUSE or more closes the piece once the piece and the
      pause together reach min_s, and that pause is never sent. A shorter
      piece keeps it: a bit of silence costs less than one more request, and
      a file never needs more than about duration / min_s pieces.
    * The last piece, when shorter than min_s, joins the one before if that
      stays within hard_max. So every piece is held back until the next one
      is known.

    add(start, end) -> [Chunk, ...] ready to send; finish(total) -> the rest.
    ``skipped``: seconds of the file no piece covers so far (for progress).
    """

    def __init__(self, profile, long_pause=LONG_PAUSE):
        self.profile = profile
        self.long_pause = long_pause
        self.skipped = 0.0
        self._regions = []           # the open piece's speech regions
        self._held = None            # the last closed piece
        self._end = 0.0              # where it ends
        self._index = 0

    def add(self, start, end):
        out = []
        regs = self._regions
        if regs:
            gap = start - regs[-1][1]
            if gap >= self.long_pause and regs[-1][1] - regs[0][0] + gap >= self.profile.min_s:
                self._close(len(regs), out)
        if not self._regions:
            self.skipped += max(0.0, start - self._end)
        self._regions.append((start, end))
        self._cut(out)
        return out

    def finish(self, total):
        out = []
        held, self._held = self._held, None
        end = self._end
        if self._regions:
            regs, self._regions = self._regions, []
            tail = Chunk(-1, regs[0][0], regs[-1][1], tuple(regs))
            end = tail.end
            if (held is not None and tail.end - tail.start < self.profile.min_s
                    and tail.end - held.start <= self.profile.hard_max):
                self.skipped -= max(0.0, tail.start - held.end)
                held, tail = None, Chunk(-1, held.start, tail.end, held.speech + tail.speech)
            if held is not None:
                out.append(self._numbered(held))
            out.append(self._numbered(tail))
        elif held is not None:
            out.append(self._numbered(held))
        self.skipped += max(0.0, total - end)
        return out

    def _cut(self, out):
        p = self.profile
        while len(self._regions) > 1:
            regs = self._regions
            ps = regs[0][0]
            if regs[-1][1] - ps <= p.cut_hi:
                return
            # (pause length, piece length if cut there, k): pause k follows region k
            pauses = [(regs[k + 1][0] - regs[k][1], regs[k][1] - ps, k) for k in range(len(regs) - 1)]
            pick = [x for x in pauses if p.cut_lo <= x[1] <= p.cut_hi]
            if not pick:
                if regs[-1][1] - ps <= p.hard_max and not any(x[1] > p.cut_hi for x in pauses):
                    return                              # wait for a pause
                fits = [x for x in pauses if x[1] <= p.hard_max]
                pick = [x for x in fits if x[1] >= p.min_s] or fits or pauses[:1]
            k = max(pick, key=lambda x: (x[0], -abs(x[1] - p.target)))[2]
            self._close(k + 1, out)

    def _close(self, k, out):
        piece, self._regions = self._regions[:k], self._regions[k:]
        self._end = piece[-1][1]
        if self._regions:
            self.skipped += max(0.0, self._regions[0][0] - self._end)
        if self._held is not None:
            out.append(self._numbered(self._held))
        self._held = Chunk(-1, piece[0][0], self._end, tuple(piece))

    def _numbered(self, chunk):
        chunk = dataclasses.replace(chunk, index=self._index)
        self._index += 1
        return chunk


# ── sentences and their times ────────────────────────────────────────────────
_STOP = re.compile(r"[.!?…։؟]+[\"'”’»)\]]*(?=\s)|[。！？]+[\"'”’»」』）)\]]*")
_SOFT = re.compile(r"[,;،、，；]")
_SPACE = re.compile(r"\s")


def split_sentences(text):
    """A piece's text as sentences: split after . ! ? … ։ ؟ 。！？ and at
    line breaks. A part under MIN_SENTENCE characters joins its neighbour on
    the same line ("Dr. Smith…", "Yes. OK."); a line break always splits (it
    is often a new speaker). A part over MAX_SENTENCE is halved at the comma
    or semicolon nearest its middle (a space when there is none)."""
    out = []
    for line in re.split(r"\s*\n\s*", str(text or "").strip()):
        spans, at = [], 0
        for m in _STOP.finditer(line):
            spans.append([at, m.end()])
            at = m.end()
        spans.append([at, len(line)])
        joined = []
        for span in spans:
            if not line[span[0]:span[1]].strip():
                continue
            if joined and (len(line[joined[-1][0]:joined[-1][1]].strip()) < MIN_SENTENCE
                           or len(line[span[0]:span[1]].strip()) < MIN_SENTENCE):
                joined[-1][1] = span[1]
            else:
                joined.append(span)
        for a, b in joined:
            out.extend(_halve(line[a:b].strip()))
    return out


def _halve(sentence):
    if len(sentence) <= MAX_SENTENCE:
        return [sentence]
    lo, hi, mid = len(sentence) // 4, len(sentence) * 3 // 4, len(sentence) / 2
    for pattern in (_SOFT, _SPACE):
        cuts = [m.end() for m in pattern.finditer(sentence) if lo <= m.end() <= hi]
        if cuts:
            c = min(cuts, key=lambda c: abs(c - mid))
            return _halve(sentence[:c].strip()) + _halve(sentence[c:].strip())
    return [sentence]


def _weight(sentence):
    return sum(ch.isalnum() for ch in sentence) + 4


def _speech_at(speech, pos):
    """The time ``pos`` seconds into the speech regions."""
    for s, e in speech:
        if pos <= e - s:
            return s + pos
        pos -= e - s
    return speech[-1][1]


def _distance(t, pause):
    if pause[0] <= t <= pause[1]:
        return 0.0
    return min(abs(t - pause[0]), abs(t - pause[1]))


def spread_segments(chunk, text):
    """Segments [{start, end, text}] for one piece: its sentences spread over
    the piece's speech by length (letters and digits + 4 each), each boundary
    moved into a pause of SNAP_PAUSE or more within SNAP_RANGE. The times
    only estimate when a sentence was said; they rise monotonically and stay
    inside the piece."""
    if isinstance(text, str):
        sentences = split_sentences(text)
    else:
        sentences = [str(s).strip() for s in (text or ()) if str(s).strip()]
    if not sentences:
        return []
    speech = [(max(s, chunk.start), min(e, chunk.end)) for s, e in (chunk.speech or ())]
    speech = [r for r in speech if r[1] > r[0]] or [(chunk.start, max(chunk.start, chunk.end))]
    total = sum(e - s for s, e in speech)
    pauses = [(e1, s2) for (_, e1), (s2, _) in zip(speech, speech[1:]) if s2 - e1 >= SNAP_PAUSE]
    weights = [_weight(s) for s in sentences]
    whole = float(sum(weights))
    raw, acc = [], 0
    for w in weights[:-1]:
        acc += w
        raw.append(_speech_at(speech, total * acc / whole))
    last = speech[-1][1]
    out, start = [], speech[0][0]
    for k, sentence in enumerate(sentences):
        if k == len(sentences) - 1:
            out.append({"start": start, "end": max(last, start), "text": sentence})
            break
        b = max(raw[k], start)
        limit = raw[k + 1] if k + 1 < len(raw) else last
        end = nxt = b
        near = [p for p in pauses
                if p[0] > start and p[1] < limit and _distance(b, p) <= SNAP_RANGE]
        if near:
            end, nxt = min(near, key=lambda p: _distance(b, p))
        out.append({"start": start, "end": end, "text": sentence})
        start = nxt
    return out


# ── sending ──────────────────────────────────────────────────────────────────
def backoff(tries, retry_after=None):
    """Seconds to wait after ``tries`` failed tries: 2, 6, 20 (+-30%), or the
    provider's Retry-After (at most RETRY_AFTER_MAX)."""
    try:
        asked = float(retry_after) if retry_after is not None else 0.0
    except (TypeError, ValueError):
        asked = 0.0
    if asked > 0:
        return min(asked, float(RETRY_AFTER_MAX))
    base = BACKOFF[min(max(int(tries), 1), len(BACKOFF)) - 1]
    return base * random.uniform(1 - JITTER, 1 + JITTER)


def _retryable(err):
    flag = getattr(err, "retryable", None)
    if flag is None:
        flag = getattr(err, "kind", None) in cloud_speech.RETRY_KINDS
    return bool(flag)


def _answer(answer):
    """(text, language) from what send returned: (text, language), a dict or text."""
    if isinstance(answer, dict):
        text, lang = answer.get("text"), answer.get("language")
    elif isinstance(answer, (tuple, list)):
        text, lang = (list(answer) + [None, None])[:2]
    else:
        text, lang = answer, None
    lang = lang.strip() if isinstance(lang, str) and lang.strip() else None
    return str(text or "").strip(), lang


def _name(spec):
    name = getattr(spec, "name", None)
    if isinstance(name, str) and name:
        return name
    return cloud_speech.NAMES.get(getattr(spec, "engine", None), "Gusan Cloud")


class _Pool:
    """A few worker threads with real Futures (concurrent.futures.wait works
    on them). Not ThreadPoolExecutor: Python joins its threads at exit, so one
    upload still in flight would hold up quitting the app for up to its
    timeout. These are daemon threads; after a stop, the last answers are
    simply dropped."""

    def __init__(self, workers):
        self._workers = max(1, int(workers))
        self._jobs = queue.SimpleQueue()
        self._threads = []

    def submit(self, fn, *args):
        f = Future()
        self._jobs.put((f, fn, args))
        if len(self._threads) < self._workers:
            t = threading.Thread(target=self._loop, daemon=True, name="cloud-piece")
            self._threads.append(t)
            t.start()
        return f

    def _loop(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            f, fn, args = job
            if not f.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args)
            except BaseException as e:
                f.set_exception(e)
            else:
                f.set_result(result)

    def shutdown(self, cancel=False):
        if cancel:
            while True:
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    break
                if job is not None:
                    job[0].cancel()
        for _ in self._threads:
            self._jobs.put(None)


class _Sender:
    """What the workers share: one piece's tries, and the rate-limit state."""

    def __init__(self, audio, spec, profile, send, stop, sleep, clock, check, fix):
        self.audio, self.spec, self.send, self.stop = audio, spec, send, stop
        self.sleep, self.clock, self.check, self.fix = sleep, clock, check, fix
        self.limit = profile.workers      # drops to 1 after a rate limit
        self.not_before = 0.0             # nobody sends before this (clock time)
        self.retries = 0
        self.requests = 0
        self._lock = threading.Lock()

    def piece(self, chunk):
        """(text, language) for one piece. Runs on a worker thread."""
        wav, tries, until = None, 0, 0.0
        while True:
            delay = max(until, self.not_before) - self.clock()
            if delay > 0 and self.sleep(delay):
                raise _Stopped()
            if self.stop.is_set():
                raise _Stopped()
            if self.check is not None:
                self.check()
            if wav is None:
                wav = encode_wav(self.audio, chunk.start, chunk.end)
            tries += 1
            with self._lock:
                self.requests += 1
            try:
                answer = self.send(self.spec, wav)
            except Exception as e:
                if self.stop.is_set():
                    raise _Stopped() from None
                if not _retryable(e) or tries >= MAX_ATTEMPTS:
                    raise
                pause = backoff(tries, getattr(e, "retry_after", None))
                with self._lock:
                    self.retries += 1
                    if getattr(e, "kind", None) == RATE_LIMIT:
                        # Everyone waits, and one piece at a time from now on.
                        self.limit = 1
                        self.not_before = max(self.not_before, self.clock() + pause)
                    else:
                        until = self.clock() + pause
                logger.info("Piece %d, try %d failed (%s); again in %.0f s",
                            chunk.index, tries, getattr(e, "kind", type(e).__name__), pause)
                continue
            text, language = _answer(answer)
            if text and self.fix is not None:
                try:
                    text = self.fix(text)
                except Exception:
                    logger.warning("Spelling fix failed", exc_info=True)
            return text, language


def _failure(err, spec, answered):
    """The job's exception for a piece that failed for good."""
    if isinstance(err, _Stopped):
        return Cancelled()
    if isinstance(err, CloudFileError):
        return err
    kind = getattr(err, "kind", None)
    message = getattr(err, "message", "") if kind else ""
    if not kind:
        logger.error("A cloud piece failed", exc_info=(type(err), err, err.__traceback__))
        kind = "error"
    message = message or cloud_speech.error_message(kind, _name(spec))
    if (kind in cloud_speech.LOCAL_FALLBACK_KINDS and not answered
            and getattr(spec, "engine", None) == "gusan"):
        return CloudUnavailable(message, kind)
    return CloudFileError(message, kind)


def transcribe_chunks(audio, chunks, spec, profile, *, send, stop=None, cancelled=None,
                      on_progress=None, check=None, fix_text=None, sleep=None,
                      clock=time.monotonic):
    """Send the pieces, ``profile.workers`` at a time, and put the answers
    back in file order. Runs on the job thread, which is the only caller of
    ``on_progress(frac, eta_seconds)``.

    ``chunks``: a list of Chunks, or the queue the planner thread fills
    (("chunk", Chunk, skipped), ..., ("end", None, skipped); ("error", exc, 0)).
    ``send(spec, wav)`` -> (text, language) or text; raises
    cloud_speech.CloudSpeechError. Retryable errors get MAX_ATTEMPTS tries in
    all; a rate limit makes everyone wait and leaves 1 worker for the rest of
    the job. ``check()`` runs before every send and raises to stop the job.
    ``fix_text(text)`` runs on every answer. ``sleep(seconds)`` -> True when
    the job stopped (default: wait on ``stop``, so Cancel ends a back-off at
    once). A fatal error or Cancel sets ``stop`` and returns at once; answers
    still on their way are dropped.

    Returns {"pieces": [(chunk, text, language), ...], "stats": {...}}.
    Raises Cancelled, NoSpeech, CloudUnavailable or CloudFileError."""
    stop = stop if stop is not None else threading.Event()
    cancelled = cancelled or (lambda: False)
    if isinstance(chunks, queue.Queue):
        source = chunks
    else:
        source = queue.Queue()
        for c in chunks:
            source.put(("chunk", c, 0.0))
        source.put(("end", None, 0.0))
    run = _Sender(audio, spec, profile, send, stop, sleep or stop.wait, clock, check, fix_text)
    pool = _Pool(profile.workers)
    duration = len(audio) / float(SAMPLE_RATE)
    waiting, inflight, done = collections.deque(), {}, {}
    ended = False
    skipped = sent = 0.0
    frac, shown = 0.0, None
    t0 = clock()
    try:
        while True:
            if cancelled():
                raise Cancelled()
            idle = not (waiting or inflight or ended)
            while not ended:
                try:
                    what, value, skip = source.get(timeout=_POLL) if idle else source.get_nowait()
                except queue.Empty:
                    break
                idle = False
                skipped = max(skipped, skip)
                if what == "chunk":
                    waiting.append(value)
                elif what == "end":
                    ended = True
                else:
                    raise value                     # the planner thread failed
            while waiting and len(inflight) < run.limit:
                c = waiting.popleft()
                inflight[pool.submit(run.piece, c)] = c
            if inflight:
                finished, _ = wait(list(inflight), timeout=_POLL, return_when=FIRST_COMPLETED)
                for f in sorted(finished, key=lambda f: inflight[f].index):
                    c = inflight.pop(f)
                    err = f.exception()
                    if err is not None:
                        raise _failure(err, spec, len(done))
                    done[c.index] = (c,) + tuple(f.result())
                    sent += c.end - c.start
            over = ended and not (waiting or inflight)
            if over:
                now = 1.0
            else:
                now = (sent + skipped) / duration if duration > 0 else 0.0
            frac = max(frac, min(1.0, now))
            elapsed = clock() - t0
            eta = (elapsed * (1 - frac) / frac
                   if frac > 0 and len(done) >= profile.workers and elapsed > 5 else None)
            if on_progress is not None and done and (frac, eta) != shown:
                shown = (frac, eta)
                try:
                    on_progress(frac, eta)
                except Exception:
                    logger.debug("progress callback failed", exc_info=True)
            if over:
                break
    except BaseException:
        stop.set()
        pool.shutdown(cancel=True)
        raise
    pool.shutdown()
    pieces = [done[i] for i in sorted(done)]
    empty = sum(1 for _, text, _ in pieces if not text)
    stats = {"pieces": len(pieces), "empty": empty, "retries": run.retries,
             "requests": run.requests, "seconds_sent": round(sent, 1)}
    logger.info("%s: %d pieces, %d with no words, %d retries", _name(spec),
                len(pieces), empty, run.retries)
    if not pieces or empty == len(pieces):
        raise NoSpeech()
    return {"pieces": pieces, "stats": stats}


# ── one file ─────────────────────────────────────────────────────────────────
def _plan(audio, profile, out, stop, probs):
    """The planner thread: speech a block at a time, each piece into ``out``
    as soon as it is known."""
    planner = ChunkPlanner(profile)
    tracker = RegionTracker(max_len=profile.hard_max)

    def put(chunks):
        for c in chunks:
            out.put(("chunk", c, planner.skipped))
    try:
        for block in probs(audio, stop=stop):
            if stop.is_set():
                return
            for s, e in tracker.feed(block):
                put(planner.add(s, e))
        if stop.is_set():
            return
        total = len(audio) / float(SAMPLE_RATE)
        for s, e in tracker.finish(total):
            put(planner.add(s, e))
        put(planner.finish(total))
        out.put(("end", None, planner.skipped))
    except Exception as e:
        logger.exception("Finding speech failed")
        out.put(("error", e, 0.0))


def _live_cfg(spec, cfg_fn):
    if cfg_fn is not None:
        try:
            return cfg_fn()
        except Exception:
            return None
    return getattr(spec, "cfg", None)


def _live_check(spec, cfg_fn):
    """Before every send: Privacy mode turned on, or the own key removed,
    stops the job (with cloud_speech's sentences)."""
    field = _KEY_FIELDS.get(getattr(spec, "engine", None))

    def check():
        cfg = _live_cfg(spec, cfg_fn)
        if not cfg:
            return
        kind = None
        if cfg.get("privacy_mode"):
            kind = "privacy"
        elif field and not str(cfg.get(field) or "").strip():
            kind = "no_key"
        if kind:
            raise cloud_speech.CloudSpeechError(kind, cloud_speech.error_message(kind, _name(spec)))
    return check


def _speller(spec, cfg_fn):
    """The user's spellings fixed on this computer (the proxy never uses
    the vocabulary): spec.spellings, read at job start, else the cfg's."""
    terms = getattr(spec, "spellings", None)
    if terms is None:
        cfg = _live_cfg(spec, cfg_fn)
        terms = vocabulary.load_terms(cfg) if cfg else ()
    terms = list(terms or ())
    if not terms:
        return None
    return lambda text: vocabulary.correct_spellings(text, terms)


def _default_send():
    """cloud_speech.send with one requests.Session per worker thread, so a
    connection is reused from piece to piece."""
    local = threading.local()

    def send(spec, wav):
        post = getattr(local, "post", None)
        if post is None:
            import requests
            local.session = requests.Session()
            post = local.post = local.session.post
        return cloud_speech.send(spec, wav, post=post)
    return send


def transcribe_file(audio, spec, *, emit=None, cancelled=None, send=None, cfg_fn=None,
                    start_pct=6, end_pct=96, stage="Transcribing", probs=None,
                    sleep=None, clock=time.monotonic):
    """Transcribe a decoded file (16 kHz mono float32) on the cloud model in
    ``spec`` (cloud_speech.Spec). Runs on the job thread; see the module
    notes for what it raises.

    ``emit(pct, stage, eta_seconds)``: progress from ``start_pct`` to
    ``end_pct`` (finished pieces plus the silence passed over, of the whole
    file); eta stays None until ``workers`` pieces are back and 5 s passed.
    ``cancelled()`` is polled. ``cfg_fn()`` returns the live cfg (default
    spec.cfg): Privacy mode and the own key are checked before every send.
    ``send(spec, wav)`` replaces cloud_speech.send, ``probs(audio, stop=)``
    speech_probs, ``sleep`` / ``clock`` the back-off's (tests).

    Returns {"segments": [{"start", "end", "text"}], "language": the set
    language, else the most common one detected, else None, "model_id":
    spec.model_id, "stats": {pieces, empty, retries, requests, seconds_sent}}."""
    profile = profile_for(spec)
    stop = threading.Event()
    source = queue.Queue()
    threading.Thread(target=_plan, args=(audio, profile, source, stop, probs or speech_probs),
                     daemon=True, name="cloud-plan").start()

    def on_progress(frac, eta):
        if emit is not None:
            emit(start_pct + (end_pct - start_pct) * frac, stage, eta)

    on_progress(0.0, None)
    try:
        run = transcribe_chunks(audio, source, spec, profile, send=send or _default_send(),
                                stop=stop, cancelled=cancelled, on_progress=on_progress,
                                check=_live_check(spec, cfg_fn), fix_text=_speller(spec, cfg_fn),
                                sleep=sleep, clock=clock)
    finally:
        stop.set()
    segments, detected = [], collections.Counter()
    for chunk, text, language in run["pieces"]:
        if language:
            detected[language] += 1
        segments.extend(spread_segments(chunk, text))
    setting = str(getattr(spec, "language", None) or "auto")
    if setting not in ("auto", "multi"):
        language = setting
    else:
        language = detected.most_common(1)[0][0] if detected else None
    return {"segments": segments, "language": language,
            "model_id": getattr(spec, "model_id", None) or None, "stats": run["stats"]}
