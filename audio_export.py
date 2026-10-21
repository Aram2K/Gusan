"""Save and export meeting recordings.

The recorder keeps the meeting as 16 kHz mono float32; each recording
segment (a fresh meeting, or a resumed one) is written as audio_partN.wav so
nothing is ever overwritten. Export concatenates the parts in order and writes
WAV (stdlib) or MP3 (PyAV's libmp3lame, already shipped with faster-whisper).
"""
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np

RATE = 16000


def float_to_int16(audio):
    a = np.asarray(audio, dtype=np.float32)
    if a.size == 0:
        return np.zeros(0, dtype=np.int16)
    return (np.clip(a, -1.0, 1.0) * 32767.0).astype(np.int16)


def write_wav(path, audio_f32, rate=RATE):
    """16-bit mono PCM WAV from float32 samples. Returns the path."""
    data = float_to_int16(audio_f32)
    path = Path(path)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return path


def write_wav_int16(path, samples_int16, rate=RATE):
    """16-bit mono PCM WAV from int16 samples - the lossless form a meeting's
    separate microphone / system sound tracks are kept in. Written to a temp
    name next to the target and renamed, so a crash never leaves half a file
    (the file is either there whole or not at all). Returns the path."""
    import os
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    data = np.asarray(samples_int16, dtype=np.int16)
    try:
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(data.tobytes())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def read_wav_float32(path):
    """(float32 samples in [-1, 1], rate) of a 16-bit WAV (stereo is
    downmixed) - what Whisper and the speaker pass take."""
    data, rate = read_wav_int16(path)
    return data.astype(np.float32) / np.float32(32767.0), rate


def read_wav_int16(path):
    """(int16 samples, rate). Stereo files are downmixed."""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"Unsupported sample width {width}")
    data = np.frombuffer(frames, dtype=np.int16)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return data, rate


def concat_wavs(paths):
    """Join recording parts in order -> (int16 samples, rate)."""
    chunks, rate = [], RATE
    for p in paths:
        data, rate = read_wav_int16(p)
        chunks.append(data)
    if not chunks:
        return np.zeros(0, dtype=np.int16), rate
    return np.concatenate(chunks), rate


def duration_seconds(paths):
    total = 0.0
    for p in paths:
        try:
            with wave.open(str(p), "rb") as w:
                total += w.getnframes() / float(w.getframerate() or RATE)
        except Exception:
            pass
    return total


# ── the in-app player (ui/components/audio_player.py) ──────────────────────
# Pure helpers, run off the GUI thread. They read WAV headers and a few short
# windows only, so a 2 h meeting costs the same as a 2 min one.

_DTYPES = {1: np.uint8, 2: np.int16, 4: np.int32}


def _wav_info(path):
    """(frames, rate, sample width, channels) of a WAV part, or None.

    The frame count is the header's, capped by what the file really holds:
    a part cut short by a crash may claim more than it has (the data starts
    44 bytes in for the files write_wav makes)."""
    try:
        with wave.open(str(path), "rb") as w:
            frames, rate = w.getnframes(), w.getframerate() or RATE
            width, ch = w.getsampwidth(), w.getnchannels() or 1
        size = Path(path).stat().st_size
    except Exception:
        return None
    block = max(1, width * ch)
    frames = max(0, min(frames, (size - 44) // block))
    return frames, rate, width, ch


def part_durations_ms(parts):
    """Each part's length in ms, in the order given (0 for a missing,
    unreadable or empty file)."""
    out = []
    for p in parts or []:
        info = _wav_info(p)
        out.append(int(info[0] * 1000 // info[1]) if info else 0)
    return out


def locate(durations_ms, position_ms):
    """Where ``position_ms`` (across all parts) falls: (part index, ms into
    that part). Parts of 0 ms are never picked; past the end -> the end of the
    last part; (-1, 0) when nothing is playable."""
    durs = [max(0, int(d or 0)) for d in durations_ms or []]
    playable = [i for i, d in enumerate(durs) if d > 0]
    if not playable:
        return -1, 0
    pos = max(0, int(position_ms or 0))
    start = 0
    for i, d in enumerate(durs):
        if d and pos < start + d:
            return i, pos - start
        start += d
    last = playable[-1]
    return last, durs[last]


def peaks(parts, bars, window_ms=100):
    """``bars`` loudness values (0..1) across all parts, as if they were one
    recording. Each bar reads one ~``window_ms`` window (wave.setpos), so the
    cost is O(bars) for any length. The loudest bar is 1.0; silence, or
    nothing readable, gives all zeros."""
    bars = max(0, int(bars or 0))
    out = [0.0] * bars
    if not bars:
        return out
    infos = []
    for p in parts or []:
        info = _wav_info(p)
        if info and info[0] > 0 and info[2] in _DTYPES:
            infos.append((str(p),) + info)
    total = sum(frames / float(rate) for _p, frames, rate, _w, _c in infos)
    if total <= 0:
        return out
    starts, acc = [], 0.0
    for _p, frames, rate, _w, _c in infos:
        starts.append(acc)
        acc += frames / float(rate)
    # Each bar's middle -> (part, frame); bars come out in part order.
    by_part = {}
    for k in range(bars):
        t = (k + 0.5) * total / bars
        idx = 0
        while idx + 1 < len(infos) and t >= starts[idx + 1]:
            idx += 1
        by_part.setdefault(idx, []).append((k, int((t - starts[idx]) * infos[idx][2])))
    for idx, wanted in by_part.items():
        path, frames, rate, width, _ch = infos[idx]
        win = max(1, min(int(rate * window_ms / 1000.0), frames))
        try:
            with wave.open(path, "rb") as w:
                for k, centre in wanted:
                    w.setpos(max(0, min(frames - win, centre - win // 2)))
                    data = np.frombuffer(w.readframes(win), dtype=_DTYPES[width])
                    if not data.size:
                        continue
                    if width == 1:
                        out[k] = float(np.abs(data.astype(np.int16) - 128).max()) / 128.0
                    else:
                        out[k] = float(np.abs(data.astype(np.int64)).max()) / float(2 ** (8 * width - 1))
        except Exception:
            pass                                       # this part's bars stay 0
    top = max(out)
    if top > 0:
        out = [min(1.0, v / top) for v in out]
    return out


def write_mp3(path, samples_int16, rate=RATE, bitrate=96_000):
    """MP3 via PyAV/libmp3lame. Returns True on success, False if the encoder
    is unavailable - the caller then offers WAV instead."""
    try:
        import av
    except Exception:
        return False
    try:
        samples = np.asarray(samples_int16, dtype=np.int16)
        with av.open(str(path), "w") as container:
            stream = container.add_stream("libmp3lame", rate=rate)
            stream.bit_rate = bitrate
            step = rate            # one-second frames; the encoder re-frames internally
            pts = 0
            for i in range(0, len(samples), step):
                chunk = samples[i:i + step]
                frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
                frame.sample_rate = rate
                frame.pts = pts
                frame.time_base = Fraction(1, rate)
                pts += len(chunk)
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
        return True
    except Exception:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
        return False


def export_recording(parts, out_path):
    """Write all ``parts`` as one file; the extension picks the format
    (.mp3 or .wav). Returns the written path, or None when nothing to export."""
    parts = [p for p in parts if Path(p).is_file()]
    if not parts:
        return None
    data, rate = concat_wavs(parts)
    out = Path(out_path)
    if out.suffix.lower() == ".mp3":
        if write_mp3(out, data, rate):
            return out
        out = out.with_suffix(".wav")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return out
