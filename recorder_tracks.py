"""What the recorder keeps of a mic + system sound meeting, and how loud it was.

DualTrackBuffer
    A meeting that records the microphone AND the system sound used to keep one
    mixed float32 track (~0.23 GB per hour). The mix hides who said what, so the
    recorder now keeps the two sources as separate 16-bit tracks instead: the
    same memory (2 x 2 bytes per sample = 4 bytes, like the float32 mix), the
    system track stored sparsely (nothing is held while the computer is silent).
    The live transcription still reads the mixed signal the record loop builds;
    get_full_audio() rebuilds that exact mix from the two tracks, so every
    existing caller keeps working.

SourceActivity
    The loudness of each source per minute of recording, so a session whose
    sound died halfway (a device switched, a headset unplugged) says so instead
    of looking like a transcript that "stopped". Only numbers - never audio or
    text.

Pure helpers (numpy only inside the audio conversions); thread-safe.
"""
import array
import math
import threading

SAMPLE_RATE = 16000
SCALE = 32767.0
# The loop's mix of the two sources (main.py _record_loop): (mic + call) * 0.75.
MIX_GAIN = 0.75


def to_int16(samples):
    """float samples in [-1, 1] -> int16 PCM (clipped, rounded)."""
    import numpy as np
    a = np.asarray(samples, dtype=np.float32)
    if not np.isfinite(a).all():                    # a driver glitch must not poison the track
        a = np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.rint(np.clip(a, -1.0, 1.0) * SCALE).astype(np.int16)


def to_float32(pcm):
    """int16 PCM -> float32 in [-1, 1]."""
    import numpy as np
    return np.asarray(pcm, dtype=np.int16).astype(np.float32) / np.float32(SCALE)


class DualTrackBuffer:
    """The mic and the system track of one recording, sample-aligned.

    ``add(mic, system, n)``: one read of the record loop - ``mic`` is the
    microphone block, ``system`` the first ``n`` samples of the call's sound that
    were mixed into it (``n`` = 0 / None while the computer was silent), exactly
    what the loop mixed. The system track is therefore zero outside those n
    samples. ``max_samples`` caps the length like the old mixed track: once
    passed, nothing more is kept and ``truncated`` is True.
    """

    def __init__(self, max_samples):
        self.max_samples = int(max_samples)
        self._lock = threading.Lock()
        self._mic = []                       # bytes of int16, one per read
        self._sys = []                       # bytes of int16 (n samples) or None
        self._lens = array.array("I")        # samples of each read
        self._ns = array.array("I")          # samples of each read that carry the call
        self.sample_count = 0
        self.nbytes = 0
        self.truncated = False

    # ── writing (record thread) ──
    def add(self, mic, system, n):
        """Keep one read. False once the cap was passed (this read is dropped)."""
        mic_pcm = to_int16(mic)
        length = int(mic_pcm.shape[0])
        n = int(n or 0)
        n = max(0, min(n, length))
        sys_bytes = None
        if n and system is not None:
            sys_pcm = to_int16(system[:n])
            n = int(sys_pcm.shape[0])             # a block shorter than ``n`` carries what it has
            sys_bytes = sys_pcm.tobytes() if n else None
        with self._lock:
            if self.truncated:
                return False
            mb = mic_pcm.tobytes()
            self._mic.append(mb)
            self._sys.append(sys_bytes)
            self._lens.append(length)
            self._ns.append(n if sys_bytes is not None else 0)
            self.sample_count += length
            self.nbytes += len(mb) + (len(sys_bytes) if sys_bytes else 0)
            if self.sample_count > self.max_samples:
                self.truncated = True
        return True

    # ── reading (any thread, normally after the recording ended) ──
    def snapshot(self):
        """A consistent copy of what is kept so far (the audio bytes are shared,
        never copied), so building a mix needs no lock."""
        snap = DualTrackBuffer(self.max_samples)
        with self._lock:
            snap._mic = list(self._mic)
            snap._sys = list(self._sys)
            snap._lens = array.array("I", self._lens)
            snap._ns = array.array("I", self._ns)
            snap.sample_count = self.sample_count
            snap.nbytes = self.nbytes
            snap.truncated = self.truncated
        return snap

    def mic(self):
        """The microphone track: int16, 16 kHz mono."""
        import numpy as np
        with self._lock:
            parts = list(self._mic)
        if not parts:
            return np.zeros(0, dtype=np.int16)
        return np.frombuffer(b"".join(parts), dtype=np.int16)

    def system(self):
        """The system sound track, the same length as the mic track."""
        import numpy as np
        with self._lock:
            sys_parts = list(self._sys)
            lens = array.array("I", self._lens)
            ns = array.array("I", self._ns)
            total = self.sample_count
        out = np.zeros(total, dtype=np.int16)
        pos = 0
        for length, n, sb in zip(lens, ns, sys_parts):
            if sb is not None and n:
                out[pos:pos + n] = np.frombuffer(sb, dtype=np.int16)[:n]
            pos += length
        return out

    def tracks(self):
        """(mic, system) as int16 arrays."""
        snap = self.snapshot()
        return snap.mic(), snap.system()

    def mix(self):
        """The mix the record loop built, as float32 in [-1, 1]: the microphone,
        and where the call's sound was present ((mic + call) * 0.75, clipped)."""
        import numpy as np
        snap = self.snapshot()
        out = to_float32(snap.mic())                 # writable copy
        sys_parts, lens, ns = snap._sys, snap._lens, snap._ns
        pos = 0
        inv = np.float32(1.0 / SCALE)
        gain = np.float32(MIX_GAIN)
        for length, n, sb in zip(lens, ns, sys_parts):
            if sb is not None and n:
                call = np.frombuffer(sb, dtype=np.int16)[:n].astype(np.float32) * inv
                seg = out[pos:pos + n]
                np.clip((seg + call) * gain, -1.0, 1.0, out=seg)
            pos += length
        return out

    def seconds(self, rate=SAMPLE_RATE):
        return self.sample_count / float(rate)


# ── how loud was each source ─────────────────────────────────────────────────

# A minute with both sources under these RMS levels had no sound: the mic at a
# muted/gated floor (-56 dBFS) and the call's sound at digital silence (-66 dBFS).
DEAD_MIC_RMS = 0.0015
DEAD_CALL_RMS = 0.0005
DEAD_MIN_SEC = 180.0         # worth telling the user about from three minutes on


def _db(rms):
    return 20.0 * math.log10(rms) if rms > 1e-9 else -99.0


class SourceActivity:
    """Loudness of the microphone and of the call, one value per ``window_sec``
    of recording. ``add`` takes each read's sum of squares and sample count (the
    loop already measures both sources before mixing)."""

    def __init__(self, window_sec=60.0, rate=SAMPLE_RATE):
        self.window_samples = max(1, int(window_sec * rate))
        self.rate = rate
        self._lock = threading.Lock()
        self._mic_e = 0.0
        self._call_e = 0.0
        self._n = 0
        self.windows = []            # [(mic_rms, call_rms)] of each finished window
        self.peak_mic = 0.0
        self.peak_call = 0.0

    def add(self, mic_sq, call_sq, n, mic_peak=0.0, call_peak=0.0):
        """``mic_sq`` / ``call_sq``: sum of squared samples of the read; ``n``: its
        length in samples (the call counts as zero where it delivered nothing)."""
        if n <= 0:
            return
        with self._lock:
            self._mic_e += float(mic_sq)
            self._call_e += float(call_sq)
            self._n += int(n)
            self.peak_mic = max(self.peak_mic, float(mic_peak))
            self.peak_call = max(self.peak_call, float(call_peak))
            if self._n >= self.window_samples:
                self._close()

    def _close(self):
        n = max(1, self._n)
        self.windows.append((math.sqrt(self._mic_e / n), math.sqrt(self._call_e / n)))
        self._mic_e = self._call_e = 0.0
        self._n = 0

    @staticmethod
    def is_dead(mic_rms, call_rms):
        return mic_rms < DEAD_MIC_RMS and call_rms < DEAD_CALL_RMS

    def quiet_run_sec(self):
        """How long (seconds, whole windows) the recording has been without any
        sound up to now - the newest windows, back to the last one with sound."""
        with self._lock:
            run = 0
            for mic, call in reversed(self.windows):
                if not self.is_dead(mic, call):
                    break
                run += 1
        return run * self.window_samples / float(self.rate)

    def dead_stretches(self, min_sec=DEAD_MIN_SEC):
        """[(start_sec, end_sec)] of runs of windows without sound lasting at
        least ``min_sec`` (the unfinished last window is not counted)."""
        with self._lock:
            windows = list(self.windows)
        win_sec = self.window_samples / float(self.rate)
        out, start = [], None
        for i, (mic, call) in enumerate(windows + [(1.0, 1.0)]):     # a sentinel with sound
            if self.is_dead(mic, call):
                if start is None:
                    start = i
            elif start is not None:
                if (i - start) * win_sec >= min_sec:
                    out.append((start * win_sec, i * win_sec))
                start = None
        return out

    def last_levels_db(self):
        """(mic dBFS, call dBFS) of the newest finished window, or None."""
        with self._lock:
            if not self.windows:
                return None
            mic, call = self.windows[-1]
        return _db(mic), _db(call)
