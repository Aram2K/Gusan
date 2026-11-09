"""Tell a woman's voice from a man's voice by pitch, on the PC, ONCE per call.

Speaking pitch (fundamental frequency) is typically 85-155 Hz for men and 165-255 Hz for women.
Each 40 ms frame gets a pitch estimate from its normalized autocorrelation (unvoiced or quiet
frames are skipped). At the end of each phrase, everything heard so far is judged (the 30th
percentile of the voiced frames); the first time it is clear (GenderGuess.LOCK_VOICED frames, outside
the in-between band) the voice is decided for the whole call and the tracker stops - no more pitch
work, and the voice never changes mid-call (the founder's choice, 2026-11-09: fewer computations,
one stable voice). Nothing leaves the PC.

It is a guess: a deep woman's voice or a high man's voice can be read the other way, and in a group
call everyone is heard in the voice decided for the first person, which is why the person can always
pick one fixed voice instead.
"""
import collections
import threading

import numpy as np

RATE = 16000
FRAME = 640                 # 40 ms
MIN_LAG = RATE // 400       # 400 Hz
MAX_LAG = RATE // 70        # 70 Hz
FEMALE_ABOVE = 158.0
MALE_BELOW = 140.0


def frame_pitch(x):
    """Pitch in Hz of one frame (float array), or None when it is quiet or unvoiced."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < MAX_LAG * 2:
        return None
    x = x - x.mean()
    if np.sqrt(np.mean(x * x)) < 250.0:
        return None
    n = len(x)
    spec = np.fft.rfft(x, n=2 * n)
    r = np.fft.irfft(spec * np.conj(spec))[:n]
    if r[0] <= 0:
        return None
    # normalise by the overlap so long lags are not penalised
    r = r / (r[0] * (n - np.arange(n)) / n)
    seg = r[MIN_LAG:MAX_LAG + 1]
    best = int(np.argmax(seg))
    peak = seg[best]
    if peak < 0.5:
        return None
    # Octave guard: the strongest peak can sit at 2x (a very steady voice: 3x, 4x) the true period,
    # which reads a woman as a man. Move to the shortest whole fraction of it that is almost as
    # strong; never prefer shorter lags in general, that doubles low (men's) voices.
    top_lag = best + MIN_LAG
    for d in (4, 3, 2):
        cand = int(round(top_lag / d)) - MIN_LAG
        if cand < 0:
            continue
        lo, hi = max(0, cand - 2), min(len(seg), cand + 3)
        k = lo + int(np.argmax(seg[lo:hi]))
        if seg[k] >= 0.92 * peak:
            best = k
            break
    lag = best + MIN_LAG
    # parabolic refinement
    if MIN_LAG < lag < MAX_LAG:
        a, b, c = r[lag - 1], r[lag], r[lag + 1]
        den = a - 2 * b + c
        if den != 0:
            lag = lag + 0.5 * (a - c) / den
    return RATE / lag


class PitchTracker:
    """Feed it the call's 16 kHz mono int16 sound with the clock time; ask for the pitch of a window."""

    def __init__(self, keep_s=30.0):
        self._buf = np.zeros(0, np.float32)
        self._frames = collections.deque()      # (time, hz)
        self._keep = keep_s
        self._lock = threading.Lock()

    def feed(self, pcm, now):
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        with self._lock:
            self._buf = np.concatenate([self._buf, a]) if self._buf.size else a
            n_frames = len(self._buf) // FRAME
            if not n_frames:
                return
            end_t = now
            for i in range(n_frames):
                f = frame_pitch(self._buf[i * FRAME:(i + 1) * FRAME])
                if f is not None:
                    t = end_t - (n_frames - 1 - i) * FRAME / RATE
                    self._frames.append((t, f))
            self._buf = self._buf[n_frames * FRAME:]
            while self._frames and self._frames[0][0] < now - self._keep:
                self._frames.popleft()

    def voiced(self, since, until):
        with self._lock:
            return [f for (t, f) in self._frames if since <= t <= until]

    def median(self, since, until):
        v = self.voiced(since, until)
        return (float(np.median(v)), len(v)) if v else (None, 0)

    def typical(self, since, until):
        """The phrase's pitch for judging: the 30th percentile of its voiced frames. A slip to
        the octave above (common in men's voices) lands in the upper part and is ignored."""
        v = self.voiced(since, until)
        return (float(np.percentile(v, 30)), len(v)) if v else (None, 0)

    def clear(self):
        """Forget everything (the voice is decided: nothing more is fed)."""
        with self._lock:
            self._buf = np.zeros(0, np.float32)
            self._frames.clear()


class GenderGuess:
    """Decided once for the call: "female", "male", or None while not known yet (the default voice
    speaks meanwhile). The first judgement with LOCK_VOICED voiced frames (~0.5 s of voice) whose
    pitch is outside the in-between band (MALE_BELOW .. FEMALE_ABOVE) is final: later phrases never
    change it, so the voice stays the same all call and the caller can stop tracking pitch."""

    LOCK_VOICED = 12

    def __init__(self):
        self.last = None

    @property
    def decided(self):
        return self.last is not None

    def judge(self, hz, n_voiced):
        if self.last is None and hz is not None and n_voiced >= self.LOCK_VOICED:
            if hz >= FEMALE_ABOVE:
                self.last = "female"
            elif hz <= MALE_BELOW:
                self.last = "male"
        return self.last
