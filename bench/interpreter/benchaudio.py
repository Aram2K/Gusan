"""Audio helpers for the bench: WAV in/out, silence trim, loudness, café noise, the
speakers-mode room leak, and a fake voice for offline runs.

numpy + the standard library only (no scipy / soundfile). Signals are float32 arrays in
[-1, 1], 16 kHz mono, unless a function says otherwise.
"""
import io
import os
import wave
from pathlib import Path

import numpy as np

SR = 16000


# -- WAV / PCM ------------------------------------------------------------------------------

def read_wav(src):
    """A path, bytes or a file object -> (float32 mono samples, sample rate)."""
    if isinstance(src, (bytes, bytearray, memoryview)):
        src = io.BytesIO(bytes(src))
    elif not hasattr(src, "read"):
        src = str(src)
    with wave.open(src, "rb") as w:
        ch, width, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        x = v.astype(np.float32) / float(1 << 23)
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        raise ValueError(f"unsupported WAV sample width: {width} bytes")
    if ch > 1:
        x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
    return x.astype(np.float32), int(sr)


def wav_frames(wav_bytes):
    """The raw sample bytes of a WAV (no header): used to recognise the same audio again."""
    with wave.open(io.BytesIO(bytes(wav_bytes)), "rb") as w:
        return w.readframes(w.getnframes())


def to_pcm16(x):
    """float samples -> little-endian 16-bit PCM bytes (beyond full scale is clipped)."""
    y = np.clip(np.round(np.asarray(x, dtype=np.float64) * 32768.0), -32768, 32767).astype("<i2")
    return y.tobytes()


def from_pcm16(pcm):
    """16-bit little-endian PCM bytes -> float32 samples."""
    return np.frombuffer(bytes(pcm), dtype="<i2").astype(np.float32) / 32768.0


def wav_bytes(x, sr=SR):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(to_pcm16(x))
    return buf.getvalue()


def write_wav(path, x, sr=SR):
    """Write a 16-bit mono WAV (atomically: a half-written file never replaces a good one)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(wav_bytes(x, sr))
    os.replace(tmp, path)


def duration(x, sr=SR):
    return len(x) / float(sr)


def resample(x, sr_from, sr_to):
    """Change the sample rate (low-pass first when going down, then linear interpolation)."""
    x = np.asarray(x, dtype=np.float64)
    if sr_from == sr_to or len(x) == 0:
        return x.astype(np.float32)
    if sr_to < sr_from:
        cutoff = 0.45 * sr_to / sr_from            # of the old sample rate
        taps = 101
        n = np.arange(taps) - (taps - 1) / 2.0
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hanning(taps)
        x = np.convolve(x, h / h.sum(), mode="same")
    n_out = int(round(len(x) * sr_to / float(sr_from)))
    t = np.arange(n_out) * (sr_from / float(sr_to))
    return np.interp(t, np.arange(len(x)), x).astype(np.float32)


# -- levels ---------------------------------------------------------------------------------

def db_to_amp(db):
    return 10.0 ** (db / 20.0)


def amp_to_db(a):
    return 20.0 * np.log10(max(float(a), 1e-10))


def peak(x):
    return float(np.max(np.abs(x))) if len(x) else 0.0


def rms(x):
    if len(x) == 0:
        return 0.0
    x = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(x * x)))


def frame_db(x, sr=SR, frame_ms=10):
    """RMS level (dBFS) of consecutive frames."""
    n = max(1, int(sr * frame_ms / 1000))
    m = len(x) // n
    if m == 0:
        return np.zeros(0)
    f = np.asarray(x[: m * n], dtype=np.float64).reshape(m, n)
    return 20.0 * np.log10(np.maximum(np.sqrt(np.mean(f * f, axis=1)), 1e-10))


def trim_silence(x, sr=SR, rel_db=-40.0, floor_db=-55.0, pad_ms=40, frame_ms=10):
    """Cut the quiet lead-in and tail. Frames quieter than max(loudest frame + rel_db, floor_db)
    count as silence; pad_ms is kept on both sides so onsets and tails are not clipped.
    All-silent input comes back empty."""
    db = frame_db(x, sr, frame_ms)
    if len(db) == 0:
        return x
    thr = max(float(db.max()) + rel_db, floor_db)
    loud = np.nonzero(db > thr)[0]
    if len(loud) == 0:
        return x[:0]
    n = int(sr * frame_ms / 1000)
    pad = int(sr * pad_ms / 1000)
    a = max(0, int(loud[0]) * n - pad)
    b = min(len(x), (int(loud[-1]) + 1) * n + pad)
    return x[a:b]


def active_rms(x, sr=SR, rel_db=-30.0, floor_db=-60.0, frame_ms=20):
    """RMS over the speech frames only (within rel_db of the loudest frame), so pauses do not
    pull a speaker's level down."""
    db = frame_db(x, sr, frame_ms)
    if len(db) == 0:
        return 0.0
    thr = max(float(db.max()) + rel_db, floor_db)
    n = int(sr * frame_ms / 1000)
    f = np.asarray(x[: len(db) * n], dtype=np.float64).reshape(len(db), n)
    sel = f[db > thr]
    return float(np.sqrt(np.mean(sel * sel))) if sel.size else 0.0


def limit_peak(x, limit_db=-1.0):
    """Scale the whole signal down when its peak is above limit_db (no clipping distortion).
    Returns (signal, gain)."""
    p = peak(x)
    lim = db_to_amp(limit_db)
    if p > lim:
        g = lim / p
        return (np.asarray(x) * g).astype(np.float32), g
    return np.asarray(x, dtype=np.float32), 1.0


def speaker_gain(clips, target_db=-24.0, sr=SR, limit_db=-1.0):
    """One gain for all of a speaker's clips: their joint speech level lands on target_db
    (dBFS RMS of the speech frames), never pushing a peak above limit_db."""
    clips = [c for c in clips if len(c)]
    if not clips:
        return 1.0
    joined = np.concatenate(clips)
    level = active_rms(joined, sr)
    if level <= 0:
        return 1.0
    g = db_to_amp(target_db) / level
    p = peak(joined)
    if p > 0:
        g = min(g, db_to_amp(limit_db) / p)
    return float(g)


# -- noise ----------------------------------------------------------------------------------

def _fft_len(n):
    """A fast FFT size >= n (a power of two: odd lengths can be 10x slower)."""
    return 1 << max(0, int(n) - 1).bit_length()


def _shaped_noise(n, rng, exponent, sr=SR, highpass_hz=20.0):
    """Noise whose power falls as 1/f^exponent (1 = pink, 2 = brown), RMS 1."""
    if n <= 0:
        return np.zeros(0, np.float32)
    nfft = _fft_len(n)
    spec = np.fft.rfft(rng.standard_normal(nfft))
    f = np.fft.rfftfreq(nfft, 1.0 / sr)
    shape = np.zeros_like(f)
    ok = f >= highpass_hz
    shape[ok] = f[ok] ** (-exponent / 2.0)
    y = np.fft.irfft(spec * shape, nfft)[:n]
    r = rms(y)
    return (y / r).astype(np.float32) if r > 0 else y.astype(np.float32)


def pink_noise(n, rng, sr=SR):
    return _shaped_noise(n, rng, 1.0, sr)


def brown_noise(n, rng, sr=SR):
    return _shaped_noise(n, rng, 2.0, sr)


def lowpass(x, cutoff_hz, sr=SR):
    """A gentle FFT low-pass (4th-power roll-off)."""
    if len(x) == 0:
        return np.asarray(x, dtype=np.float32)
    nfft = _fft_len(len(x) + 256)
    spec = np.fft.rfft(np.asarray(x, dtype=np.float64), nfft)
    f = np.fft.rfftfreq(nfft, 1.0 / sr)
    spec *= 1.0 / (1.0 + (f / float(cutoff_hz)) ** 4)
    return np.fft.irfft(spec, nfft)[: len(x)].astype(np.float32)


def convolve(x, h):
    """Linear convolution by FFT, cut to len(x)."""
    x = np.asarray(x, dtype=np.float64)
    h = np.asarray(h, dtype=np.float64)
    if len(x) == 0 or len(h) == 0:
        return np.zeros(len(x), np.float32)
    n = len(x) + len(h) - 1
    nfft = 1 << (n - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(h, nfft), nfft)[: len(x)]
    return y.astype(np.float32)


def room_ir(sr=SR, delay_ms=35.0, rt60=0.3, length_s=0.3, seed=0, reverb_db=-6.0):
    """Impulse response of a loudspeaker heard by a laptop mic: the direct sound after
    delay_ms (gain 1), then a short decaying reverb tail (60 dB down after rt60 s) whose
    energy is reverb_db relative to the direct sound."""
    rng = np.random.default_rng(seed)
    d = int(round(sr * delay_ms / 1000.0))
    length = max(2, int(sr * length_s))
    h = np.zeros(d + length, np.float64)
    h[d] = 1.0
    t = np.arange(1, length) / float(sr)
    tail = rng.standard_normal(length - 1) * np.exp(-6.9078 * t / rt60)
    tail = np.convolve(tail, [0.25, 0.5, 0.25], mode="same")         # rooms eat the highs
    energy = float(np.sum(tail * tail))
    if energy > 0:
        tail *= np.sqrt(db_to_amp(reverb_db) ** 2 / energy)
    h[d + 1:] = tail
    return h.astype(np.float32)


def apply_leak(mic, played, gain=0.3, ir=None, sr=SR):
    """What a mic hears in speakers mode: its own signal plus ``played`` (everything the
    speakers play: the call, and at run time the interpreter's own voice) through the room.
    The result has the mic's length."""
    if ir is None:
        ir = room_ir(sr)
    mic = np.asarray(mic, dtype=np.float32)
    p = np.zeros(len(mic), np.float32)
    m = min(len(mic), len(played))
    p[:m] = np.asarray(played[:m], dtype=np.float32)
    return (mic + gain * convolve(p, ir)).astype(np.float32)


def babble(sources, n, rng, talkers=6, sr=SR):
    """Café babble, RMS 1: several copies of speech, each shifted in time by a random amount,
    summed, low-passed and smeared by a room, so no single sentence stays clear. The filtering
    is circular, so the result loops seamlessly (cafe_noise tiles it)."""
    srcs = [np.asarray(s, dtype=np.float32) for s in sources if len(s)]
    if n <= 0 or not srcs:
        return np.zeros(max(n, 0), np.float32)
    order = rng.permutation(len(srcs))
    src = np.concatenate([srcs[i] for i in order])
    if len(src) < sr // 2:
        return np.zeros(n, np.float32)
    out = np.zeros(n, np.float64)
    for _ in range(talkers):
        out += rng.uniform(0.5, 1.0) * np.resize(np.roll(src, int(rng.integers(0, len(src)))), n)
    spec = np.fft.rfft(out)
    f = np.fft.rfftfreq(n, 1.0 / sr)
    spec *= 1.0 / (1.0 + (f / 3200.0) ** 4)
    ir = room_ir(sr, delay_ms=0.0, rt60=0.5, length_s=min(0.4, n / float(sr)),
                 seed=int(rng.integers(1 << 30)), reverb_db=-2.0)
    spec *= np.fft.rfft(ir[:n], n)
    out = np.fft.irfft(spec, n)
    r = rms(out)
    return (out / r).astype(np.float32) if r > 0 else out.astype(np.float32)


def clinks(n, rng, sr=SR, every_s=8.0):
    """Cups and cutlery: short bright bursts at random times (peak ~1)."""
    out = np.zeros(max(n, 0), np.float32)
    if n <= 0:
        return out
    for _ in range(int(rng.poisson(n / float(sr) / every_s))):
        length = int(sr * rng.uniform(0.02, 0.06))
        if length >= n:
            continue
        t0 = int(rng.integers(0, n - length))
        burst = rng.standard_normal(length) * np.exp(-np.arange(length) / (length / 5.0))
        burst = np.diff(burst, prepend=0.0)
        burst /= max(1e-9, float(np.max(np.abs(burst))))
        out[t0:t0 + length] += (burst * rng.uniform(0.3, 1.0)).astype(np.float32)
    return out


def cafe_noise(n, rng, babble_sources, sr=SR, loop_s=32.0):
    """Café background, RMS 1: babble from other speech, pink + brown noise, the odd clink.
    The steady part is made once as a seamless loop of ~loop_s seconds and repeated (big FFTs
    are slow); the clinks never repeat."""
    if n <= 0:
        return np.zeros(0, np.float32)
    period = min(_fft_len(n), _fft_len(int(loop_s * sr)))
    steady = (0.75 * babble(babble_sources, period, rng, sr=sr) + 0.5 * pink_noise(period, rng, sr)
              + 0.35 * brown_noise(period, rng, sr))
    mix = np.resize(steady, n) + 0.15 * clinks(n, rng, sr)
    r = rms(mix)
    return (mix / r).astype(np.float32) if r > 0 else mix.astype(np.float32)


def add_noise_at_snr(speech, noise, snr_db, sr=SR, ref_level=None):
    """speech + noise, the noise scaled so the speech's active level (or ref_level when the
    track has no speech of its own) sits snr_db above the noise's RMS. -> (mixed, noise gain)."""
    speech = np.asarray(speech, dtype=np.float32)
    level = active_rms(speech, sr) or (ref_level or 0.0)
    n_rms = rms(noise[: len(speech)])
    if level <= 0 or n_rms <= 0:
        return speech.copy(), 0.0
    g = level / (n_rms * db_to_amp(snr_db))
    noise = np.resize(np.asarray(noise, dtype=np.float32), len(speech))
    return (speech + g * noise).astype(np.float32), float(g)


def snr_db(speech, noise, sr=SR):
    """Active speech level over the noise's RMS, in dB."""
    s, n = active_rms(speech, sr), rms(noise)
    return amp_to_db(s) - amp_to_db(n) if s > 0 and n > 0 else None


# -- a fake voice (offline runs) -----------------------------------------------------------

def _is_dense_char(ch):
    """Characters that take a syllable each when spoken (CJK, kana, Hangul)."""
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
            or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF)


GENDER_F0 = {"female": 210.0, "male": 115.0}


def synth_voice(text, f0=165.0, seed=0, sr=SR, sec_per_char=0.065, dense_sec_per_char=0.16,
                level=0.25):
    """A deterministic stand-in for speech: a voiced buzz at pitch f0 (with vibrato and a
    falling contour) shaped into syllables, one burst per word with short pauses between
    words, plus a near-silent lead-in and tail (so trimming has something to do). About
    0.065 s per character (0.16 s per CJK / kana / Hangul character)."""
    rng = np.random.default_rng(seed)
    words = (text or "").split() or ["."]
    pieces = [rng.standard_normal(int(0.15 * sr)) * 1e-4]
    for k, word in enumerate(words):
        dur = sum(dense_sec_per_char if _is_dense_char(c) else sec_per_char for c in word)
        n = max(int(0.12 * sr), int(dur * sr))
        t = np.arange(n) / float(sr)
        contour = np.linspace(1.04, 0.96, n) * (1.0 + 0.03 * np.sin(2 * np.pi * 4.5 * t + rng.uniform(0, 6.28)))
        phase = 2 * np.pi * np.cumsum(f0 * contour) / sr
        sig = np.zeros(n)
        for h in range(1, 9):
            boost = 1.6 if h in (3, 4) else 1.0                # a rough formant
            sig += boost / h * np.sin(h * phase + rng.uniform(0, 6.28))
        syl_rate = rng.uniform(3.5, 5.5)
        env = 0.55 + 0.45 * np.sin(np.pi * syl_rate * t + rng.uniform(0, 1.0)) ** 2
        ramp = min(n // 2, int(0.012 * sr))
        if ramp:
            env[:ramp] *= np.linspace(0, 1, ramp)
            env[-ramp:] *= np.linspace(1, 0, ramp)
        sig *= env
        sig *= level / max(1e-9, float(np.max(np.abs(sig))))
        pieces.append(sig)
        if k < len(words) - 1:
            pieces.append(rng.standard_normal(int(0.06 * sr)) * 1e-4)
    pieces.append(rng.standard_normal(int(0.25 * sr)) * 1e-4)
    return np.concatenate(pieces).astype(np.float32)
