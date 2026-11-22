"""A meeting's recording on disk while it is recorded (Gusan 2.1.11).

The recorder keeps a meeting's sound in memory until Stop (recorder_tracks.DualTrackBuffer): an app that
was closed by force, crashed or restarted mid-call lost all of it (an 18-minute interview on 9 Nov). Now
every part of a meeting is also written to its folder while it is recorded, a few seconds behind:

    audio_part2.live.wav          the mix (what the live transcription reads; 16-bit, 16 kHz, mono)
    audio_part2_mic.live.wav      a mic + system sound meeting: the microphone ...
    audio_part2_system.live.wav   ... and the call's sound, sample-aligned with the mix
    audio_part2.live.json         what the files are (rate, mode, the earlier parts' duration)
    audio_part2.pieces.jsonl      every live piece: where it is in the part, its words, You / Them energy

Append-only: the record thread hands over the samples it keeps (``add``) and every transcribed piece
(``add_piece``); a writer thread appends them every FLUSH_S, then rewrites the two size fields of each
WAV header and syncs the files - a file is a valid WAV after every flush, and a header a crash left
behind is made right from the file's length (``finalize_wav``). The names never match a recording part
(meeting_store's ``audio_partN.wav``), so nothing lists them as one.

At Stop the session saves the part from memory as always and then ``discard()``s the spool (or, when
that save failed, ``finalize()``s it: the spool becomes the part). A spool still there at the next start
is a meeting that was never finished: ``recover_folder`` turns it into audio_partN.wav (and its two
tracks), keeps the pieces, and the session finishes it (meeting_session.recover_interrupted).
"""
import json
import logging
import os
import struct
import threading
import time
import wave
from pathlib import Path

logger = logging.getLogger("transcribe")

RATE = 16000
FLUSH_S = 3.0
HEADER = 44
MIN_FRAMES = RATE // 10           # less than 0.1 s is no recording
LIVE_WAV = ".live.wav"
LIVE_INFO = ".live.json"
PIECES = ".pieces.jsonl"

# Folders whose part is being written right now (this process): never recovered under the recorder.
_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()


def _key(folder):
    return os.path.normcase(os.path.abspath(str(folder)))


def is_active(folder):
    with _ACTIVE_LOCK:
        return _key(folder) in _ACTIVE


def spool_paths(part_path):
    """{"mix", "mic", "system", "info", "pieces"} of the part ``audio_partN.wav`` (same folder)."""
    p = Path(part_path)
    stem, d = p.stem, p.parent
    return {"mix": d / (stem + LIVE_WAV), "mic": d / (stem + "_mic" + LIVE_WAV),
            "system": d / (stem + "_system" + LIVE_WAV), "info": d / (stem + LIVE_INFO),
            "pieces": d / (stem + PIECES)}


def wav_header(frames, rate=RATE):
    """The 44-byte header of a 16-bit mono PCM WAV with ``frames`` samples."""
    data = int(frames) * 2
    return (b"RIFF" + struct.pack("<I", 36 + data) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate,
                                                                              rate * 2, 2, 16)
            + b"data" + struct.pack("<I", data))


class WavAppender:
    """A 16-bit mono WAV that grows: samples appended, the header's sizes made right on ``settle``."""

    def __init__(self, path, rate=RATE):
        self.path, self.rate = Path(path), int(rate)
        self.frames = 0
        self._f = open(self.path, "w+b")
        self._f.write(wav_header(0, self.rate))
        self._f.flush()

    def append(self, pcm):
        if pcm:
            self._f.seek(0, os.SEEK_END)
            self._f.write(pcm)
            self.frames += len(pcm) // 2

    def settle(self, sync=True):
        f = self._f
        f.seek(4)
        f.write(struct.pack("<I", 36 + self.frames * 2))
        f.seek(40)
        f.write(struct.pack("<I", self.frames * 2))
        f.seek(0, os.SEEK_END)
        f.flush()
        if sync:
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

    def close(self):
        try:
            self.settle(sync=True)
        finally:
            self._f.close()


def finalize_wav(path, frames=None, rate=RATE):
    """Make a spooled WAV's header right from its length (a crash may have left the sizes of an earlier
    flush), cut to whole samples - or to ``frames`` when given. Returns its samples (0 when unreadable)."""
    path = Path(path)
    try:
        size = path.stat().st_size
        have = max(0, (size - HEADER) // 2)
        n = have if frames is None else max(0, min(int(frames), have))
        with open(path, "r+b") as f:
            f.seek(0)
            f.write(wav_header(n, rate))
            f.truncate(HEADER + n * 2)
        return n
    except OSError:
        logger.warning("A saved recording could not be read: %s", path.name, exc_info=True)
        return 0


def wav_frames(path):
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes()
    except Exception:
        return 0


class MeetingSpool:
    """One meeting part written to disk while it is recorded. ``begin`` (record thread, once the
    streams are open) makes the files; ``add`` / ``add_piece`` never block on the disk (a writer
    thread flushes every ``flush_s``); ``close`` writes what is left; ``discard`` removes it all;
    ``finalize`` turns it into the part itself. A disk that fails stops the spool, never the meeting."""

    def __init__(self, folder, part_path, prior_sec=0, rate=RATE, flush_s=FLUSH_S, thread=True):
        self.folder, self.part_path = Path(folder), Path(part_path)
        self.paths = spool_paths(part_path)
        self.rate, self.flush_s, self.prior_sec = int(rate), float(flush_s), int(prior_sec or 0)
        self._thread_wanted = bool(thread)
        self._lock = threading.Lock()
        self._io = threading.Lock()                  # one flush at a time (writer thread / close)
        self._pending = {"mix": [], "mic": [], "system": []}
        self._pieces = []
        self._files = {}
        self._wake = threading.Event()
        self._done = threading.Event()
        self._thread = None
        self.failed = False
        self.begun = False
        self.closed = False
        self.frames = 0
        with _ACTIVE_LOCK:
            _ACTIVE.add(_key(self.folder))

    def begin(self, two_tracks=False, mode=""):
        """Make the files (the mix, and the two tracks for a mic + system sound meeting). True when
        the part is being written."""
        if self.begun or self.closed:
            return self.begun
        try:
            kinds = ("mix", "mic", "system") if two_tracks else ("mix",)
            for kind in kinds:
                self._files[kind] = WavAppender(self.paths[kind], self.rate)
            info = {"version": 1, "part": self.part_path.name, "rate": self.rate, "tracks": bool(two_tracks),
                    "mode": str(mode or ""), "prior_sec": self.prior_sec,
                    "started": time.strftime("%Y-%m-%d %H:%M:%S")}
            tmp = self.paths["info"].with_name(self.paths["info"].name + ".tmp")
            tmp.write_text(json.dumps(info), encoding="utf-8")
            os.replace(tmp, self.paths["info"])
        except OSError:
            logger.warning("The meeting can't be saved while it is recorded (only at Stop)", exc_info=True)
            self._fail()
            return False
        self.begun = True
        if self._thread_wanted:
            self._thread = threading.Thread(target=self._run, name="gusan-spool", daemon=True)
            self._thread.start()
        logger.info("Meeting: saving %s while it is recorded%s", self.part_path.name,
                    " (mic and call tracks)" if two_tracks else "")
        return True

    @property
    def two_tracks(self):
        return "mic" in self._files

    def add(self, mix, mic=None, system=None, n=0):
        """One read the recorder kept: the mix (float32) and, with two tracks, the microphone block and
        the first ``n`` samples of the call's sound that were mixed into it (the rest of the call's
        track is silence) - exactly what recorder_tracks.DualTrackBuffer keeps."""
        if not self.begun or self.closed or self.failed:
            return
        import numpy as np
        import recorder_tracks
        m = recorder_tracks.to_int16(mix)
        parts = {"mix": m.tobytes()}
        if self.two_tracks:
            length = len(m)
            mic_pcm = recorder_tracks.to_int16(mic if mic is not None else np.zeros(length, np.float32))[:length]
            if len(mic_pcm) < length:
                mic_pcm = np.concatenate([mic_pcm, np.zeros(length - len(mic_pcm), np.int16)])
            sys_pcm = np.zeros(length, np.int16)
            n = max(0, min(int(n or 0), length))
            if n and system is not None:
                got = recorder_tracks.to_int16(system[:n])
                sys_pcm[:len(got)] = got
            parts["mic"], parts["system"] = mic_pcm.tobytes(), sys_pcm.tobytes()
        with self._lock:
            for kind, pcm in parts.items():
                self._pending[kind].append(pcm)

    def add_piece(self, idx, start_s=None, end_s=None, text="", lang="", mic_e=None, call_e=None):
        """A transcribed live piece: where it sits in this part (seconds), its words and the energy of the
        microphone and of the call in it (the live You / Them hint)."""
        if not self.begun or self.closed or self.failed:
            return
        row = {"i": int(idx), "t": str(text or "")}
        if start_s is not None and end_s is not None:
            row["s"], row["e"] = round(float(start_s), 2), round(float(end_s), 2)
        if lang:
            row["lang"] = str(lang)
        if mic_e is not None and call_e is not None:
            row["mic"], row["call"] = round(float(mic_e), 6), round(float(call_e), 6)
        with self._lock:
            self._pieces.append(json.dumps(row, ensure_ascii=False))

    def _run(self):
        while not self._done.is_set():
            self._wake.wait(self.flush_s)
            self._wake.clear()
            self.flush()

    def flush(self, sync=True):
        """Write what is waiting, then make every header right. Writer thread (or close)."""
        with self._io:
            if self.failed or not self.begun:
                return
            with self._lock:
                pending = {k: v for k, v in self._pending.items()}
                self._pending = {"mix": [], "mic": [], "system": []}
                pieces, self._pieces = self._pieces, []
            try:
                for kind, f in self._files.items():
                    if pending.get(kind):
                        f.append(b"".join(pending[kind]))
                    f.settle(sync=sync)
                if pieces:
                    with open(self.paths["pieces"], "a", encoding="utf-8") as pf:
                        pf.write("\n".join(pieces) + "\n")
                        pf.flush()
                self.frames = self._files["mix"].frames if "mix" in self._files else 0
            except OSError:
                logger.warning("Saving the meeting while it is recorded stopped", exc_info=True)
                self._fail()

    def _fail(self):
        self.failed = True
        for f in self._files.values():
            try:
                f._f.close()
            except Exception:
                pass

    def close(self):
        """The recording ended: write what is left and let the files go (they stay on disk)."""
        if self.closed:
            return
        self.closed = True
        self._done.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=10.0)
        self.flush()
        with self._io:
            for f in self._files.values():
                try:
                    f.close()
                except Exception:
                    pass
            self._files = {}

    def _release(self):
        with _ACTIVE_LOCK:
            _ACTIVE.discard(_key(self.folder))

    def discard(self):
        """The part was saved another way (Stop), or the meeting was thrown away: remove the spool."""
        self.close()
        for p in self.paths.values():
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.debug("could not remove %s", p, exc_info=True)
        self._release()

    def finalize(self):
        """Turn the spool into the part (audio_partN.wav and, with two tracks, its _mic / _system
        tracks) - for when saving it from memory failed. Returns the part's path or None."""
        self.close()
        try:
            return recover_part(self.part_path, keep_existing=False)
        finally:
            self._release()


# ── recovery: a part that was never finished ─────────────────────────────────
def leftover_parts(folder):
    """The parts of ``folder`` that have a spool (audio_partN.wav paths), in order."""
    folder = Path(folder)
    stems = set()
    try:
        for p in folder.iterdir():
            name = p.name
            for end in (LIVE_INFO, LIVE_WAV):
                if name.endswith(end):
                    stem = name[:-len(end)]
                    if stem.endswith(("_mic", "_system")) and end == LIVE_WAV:
                        continue
                    stems.add(stem)
    except OSError:
        return []
    return [folder / (s + ".wav") for s in sorted(stems)]


def recover_part(part_path, keep_existing=True):
    """Turn a part's spool into the part: the mix -> audio_partN.wav, the tracks -> its _mic / _system
    WAVs (cut to the same length). A part already saved with at least as much sound is kept as it is
    (``keep_existing``) and the spool is dropped. Returns the part's path, or None (nothing usable)."""
    part = Path(part_path)
    paths = spool_paths(part)
    import meeting_store
    mix_n = finalize_wav(paths["mix"]) if paths["mix"].is_file() else 0
    tracks = paths["mic"].is_file() and paths["system"].is_file()
    if tracks:
        n = min(mix_n, finalize_wav(paths["mic"]), finalize_wav(paths["system"]))
        for kind in ("mix", "mic", "system"):
            finalize_wav(paths[kind], frames=n)
        mix_n = n
    result = None
    try:
        if mix_n < MIN_FRAMES:
            logger.info("A meeting part left nothing to keep: %s", part.name)
        elif keep_existing and part.is_file() and wav_frames(part) >= mix_n:
            logger.info("A meeting part was saved already: %s", part.name)
            result = part
        else:
            os.replace(paths["mix"], part)
            if tracks:
                for kind in ("mic", "system"):
                    os.replace(paths[kind], meeting_store.track_path(part, kind))
            logger.info("Recovered a meeting part from disk: %s (%.0f s)", part.name, mix_n / float(RATE))
            result = part
    except OSError:
        logger.warning("Recovering %s failed", part.name, exc_info=True)
        return None
    for kind in ("mix", "mic", "system", "info"):
        try:
            paths[kind].unlink()
        except OSError:
            pass
    return result


def load_info(part_path):
    try:
        data = json.loads(spool_paths(part_path)["info"].read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_pieces(part_path):
    """The live pieces saved for a part, in spoken order: [{"i", "t", "s", "e", "lang", "mic", "call"}]."""
    rows = []
    try:
        with open(spool_paths(part_path)["pieces"], "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue                         # a line cut short by a crash
                if isinstance(row, dict) and isinstance(row.get("i"), int) and str(row.get("t") or "").strip():
                    rows.append(row)
    except OSError:
        return []
    seen, out = set(), []
    for row in sorted(rows, key=lambda r: r["i"]):
        if row["i"] not in seen:
            seen.add(row["i"])
            out.append(row)
    return out


def piece_segments(part_path):
    """The saved pieces as timestamped segments ([{"start", "end", "text"}]) - None when one with words
    has no place (a cloud session's transcript is labelled from these, never read again)."""
    out = []
    for row in load_pieces(part_path):
        if "s" not in row or "e" not in row:
            return None
        start = float(row["s"])
        out.append({"start": start, "end": max(float(row["e"]), start + 0.05), "text": str(row["t"]).strip()})
    return out or None
