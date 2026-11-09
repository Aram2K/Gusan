"""Simulated sound devices for the interpreter bench, and offline stand-ins for its providers.

SimAudio has the methods interpreter/controller.py calls on WindowsAudio and plays a generated
conversation into the real engine:

  capture(on_pcm, loopback=True)   the call: system.wav (or a variant) in real time, 20 ms at a time,
                                   plus everything the HEAR sink played, 10 ms later and digitally
                                   (WASAPI loopback hears the app's own output too)
  capture(on_pcm, loopback=False)  the mic: mic.wav (or mic_speakers.wav); with speakers_echo it also
                                   hears the HEAR sink 40 ms later at -10 dB (the room), so feedback
                                   loops can happen
  playback(device, on_played)      a sink that plays in real time at 16 kHz: queued_seconds() and
                                   is_playing() tell the truth, on_played() sees everything the
                                   device takes as it plays (the silence between phrases too, like
                                   audio_io.PlaybackSink), and every chunk's place on the timeline is kept
  virtual_mic_output()             1 (Speak on) or None
  default_output_name()            "Speakers (...)" / "Headphones (...)" for output=auto
  ducker(level)                    records duck / restore; the call is lowered in the loopback meanwhile
                                   (close() puts it back for good)

Time: clock() runs ``speed`` times faster than the wall clock (1.0 for real runs; dry runs may go
faster). Timeline position 0, the conversation's first sample, is at clock() == origin, fixed by
begin() once the engine is up; before that the captures hand out silence. The engine gets the same
clock, so every time it records is on the timeline's scale.

Ground truth for the analysis: a capture marks the chunk it is handing out (current_chunk(), a
thread-local: the engine processes a chunk on the capture's own thread, so the STT client sees
exactly which timeline samples it was sent), and the runner's voice wrapper marks the phrase being
said (speaking(utt)), so the sink files every phrase's audio under it.

Dry-run stand-ins (no network): OracleScribe and FakeScribeSocket "hear" what reaches them using
the ground truth of every source mixed into it (the script's turns, the room leak, Gusan's own
voice), with an energy-free VAD; OracleVoice and FakeDialogueSocket speak with the preset voice's
bundled sample (assets/voices/<id>.wav); OracleTranslator answers from the reference translations.
Faults make the fake sockets hang up at given times (the engine's reconnects, offline).

Cost: a capture's 20 ms chunk takes well under a millisecond here, but the engine's own work
(canceller, pitch) and a dry-run listener per direction share one Python process: a clock 4x
faster can fall behind on a busy machine (the runner reports late chunks); the room echo path
(--echo-path room) is the heaviest, keep its dry runs at --speed 2 or less.
"""
import base64
import bisect
import collections
import contextlib
import difflib
import itertools
import json
import logging
import os
import queue
import re
import threading
import time
import unicodedata
import urllib.parse
import zlib

import numpy as np

import benchaudio

logger = logging.getLogger("bench.interpreter.sim")

RATE = 16000
CHUNK = 320                       # 20 ms: what a capture hands out at a time (the real one polls every 20 ms)
LATE_S = 0.1                      # a chunk handed out this much later than its time counts as late
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
VOICE_DIR = os.path.join(REPO, "assets", "voices")


# ── audio helpers ────────────────────────────────────────────────────────────────────────────
def load_pcm(path):
    """Any PCM WAV -> 16 kHz mono int16 array."""
    x, sr = benchaudio.read_wav(path)
    if sr != RATE:
        x = benchaudio.resample(x, sr, RATE)
    return np.clip(np.round(np.asarray(x, np.float64) * 32768.0), -32768, 32767).astype(np.int16)


def save_pcm(path, pcm, rate=RATE):
    benchaudio.write_wav(path, np.asarray(pcm, np.float32) / 32768.0, rate)


def rms(a):
    a = np.asarray(a, np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def _slice(arr, pos, n):
    """arr[pos:pos+n] as float32 with zeros outside it (pos may be negative)."""
    out = np.zeros(int(n), np.float32)
    lo, hi = max(0, int(pos)), min(len(arr), int(pos) + int(n))
    if hi > lo:
        out[lo - int(pos):hi - int(pos)] = arr[lo:hi]
    return out


# ── what the calling thread is doing (set by captures and by the runner's voice wrapper) ──────
_tls = threading.local()


class Chunk:
    """The 20 ms a capture is handing to the engine right now: which capture, where on the
    timeline, and how far the call was turned down (ducked) when it was mixed."""
    __slots__ = ("sim", "kind", "pos", "n", "duck")

    def __init__(self, sim, kind, pos, n, duck=1.0):
        self.sim, self.kind, self.pos, self.n, self.duck = sim, kind, int(pos), int(n), duck


def current_chunk():
    """The Chunk the calling thread is processing, or None (not inside a capture's callback, or
    before the conversation starts)."""
    return getattr(_tls, "chunk", None)


@contextlib.contextmanager
def speaking(utt):
    """Mark ``utt`` as the phrase the calling thread's voice is saying: SimSink.play() files the
    audio under it and the dry-run voices plan its length."""
    prev = getattr(_tls, "utt", None)
    _tls.utt = utt
    try:
        yield utt
    finally:
        _tls.utt = prev


def current_utterance():
    return getattr(_tls, "utt", None)


class Utterance:
    """One phrase handed to a voice (made by the runner's voice wrapper): its text and voice, its
    times (clock) and where its audio landed on the timeline (samples)."""
    _ids = itertools.count(1)

    def __init__(self, direction, text, voice_id=None):
        self.id = next(Utterance._ids)
        self.direction = direction
        self.text = text
        self.voice_id = voice_id
        self.t_call = self.t_first = self.t_done = None
        self.first_pos = self.end_pos = None
        self.planned = None          # samples the dry-run voice is about to produce (for the oracle)
        self.samples = 0             # samples that reached a sink
        self.chunks = 0
        self.skipped = None          # "sample" | "budget" | "captions" | None
        self.error = None
        self.seconds = None

    def scheduled(self, start, n):
        if self.first_pos is None:
            self.first_pos = start
        self.end_pos = start + n
        self.samples += n
        self.chunks += 1

    def span(self):
        """(first, end) sample positions of the phrase's audio (with the planned length while it
        is still streaming in), or None when nothing played."""
        if self.first_pos is None:
            return None
        end = self.end_pos
        if self.planned:
            end = max(end, self.first_pos + self.planned)
        return self.first_pos, end


# ── a timeline ───────────────────────────────────────────────────────────────────────────────
class Timeline:
    """A growable int16 track addressed by timeline sample position (negative = the lead-in)."""

    def __init__(self, base=-60 * RATE, size=240 * RATE):
        self.base = int(base)
        self.a = np.zeros(int(size), np.int16)
        self.end = None                      # one past the last sample written

    def _grow(self, idx_end):
        if idx_end > len(self.a):
            new = np.zeros(max(idx_end, 2 * len(self.a)), np.int16)
            new[:len(self.a)] = self.a
            self.a = new

    def write(self, pos, arr):
        arr = np.asarray(arr, np.int16)
        i = int(pos) - self.base
        if i < 0:
            arr = arr[-i:]
            i = 0
        if not arr.size:
            return
        self._grow(i + arr.size)
        self.a[i:i + arr.size] = arr
        e = self.base + i + arr.size
        self.end = e if self.end is None else max(self.end, e)

    def read(self, pos, n):
        out = np.zeros(int(n), np.float32)
        i = int(pos) - self.base
        lo, hi = max(0, i), min(len(self.a), i + int(n))
        if hi > lo:
            out[lo - i:hi - i] = self.a[lo:hi]
        return out

    def zero(self, a, b):
        i, j = max(0, int(a) - self.base), min(len(self.a), int(b) - self.base)
        if j > i:
            self.a[i:j] = 0

    def export(self, start=0, end=None):
        """int16 samples of [start, end) (end defaults to the last sample written)."""
        end = self.end if end is None else end
        if end is None or end <= start:
            return np.zeros(0, np.int16)
        return self.read(start, end - start).astype(np.int16)


# ── the devices ──────────────────────────────────────────────────────────────────────────────
class SimAudio:
    """WindowsAudio for the bench (see the module docstring). mic / system: int16 arrays of the
    variant being played; mic_clean / system_clean: the clean tracks (ground truth for the oracles
    and the diagnostics; default: the same arrays)."""

    def __init__(self, mic, system, *, mic_clean=None, system_clean=None, speed=1.0,
                 speakers_echo=False, echo_delay_ms=40.0, echo_gain_db=-10.0, echo_ir=None,
                 loopback_delay_ms=10.0, out_latency_ms=20.0, output_name="Headphones (Gusan bench)",
                 virtual_mic=1, leak_is_speech=False):
        self.speed = float(speed) if speed and speed > 0 else 1.0
        self._wall0 = time.monotonic()
        self.origin = None
        self.mic = np.asarray(mic, np.int16)
        self.system = np.asarray(system, np.int16)
        self.mic_clean = self.mic if mic_clean is None else np.asarray(mic_clean, np.int16)
        self.system_clean = self.system if system_clean is None else np.asarray(system_clean, np.int16)
        self.length = max(len(self.mic), len(self.system))
        self.speakers_echo = bool(speakers_echo)
        self.echo_delay = int(round(echo_delay_ms * RATE / 1000.0))
        self.echo_gain = 10.0 ** (echo_gain_db / 20.0)
        self.echo_ir = None if echo_ir is None else np.asarray(echo_ir, np.float32)
        self._ir_spec = {}
        self.loop_delay = int(round(loopback_delay_ms * RATE / 1000.0))
        self.out_latency = int(round(out_latency_ms * RATE / 1000.0))
        self.output_name = output_name
        self._vmic = virtual_mic
        self.leak_is_speech = bool(leak_is_speech)
        self.captures, self.sinks, self.duckers = [], [], []
        self.duck_gain = 1.0
        self.duck_log = []                   # (clock, gain)
        self._lock = threading.RLock()
        self.closed = False

    @property
    def duration(self):
        return self.length / RATE

    # -- time -----------------------------------------------------------------------------------
    def clock(self):
        return (time.monotonic() - self._wall0) * self.speed

    def sleep(self, seconds):
        """Sleep ``seconds`` of clock time."""
        if seconds > 0:
            time.sleep(seconds / self.speed)

    def begin(self, lead_in=0.5):
        """Start the conversation: its first sample plays ``lead_in`` (clock) seconds from now."""
        with self._lock:
            if self.origin is None:
                self.origin = self.clock() + max(0.0, float(lead_in))
            return self.origin

    def pos_now(self):
        o = self.origin
        return None if o is None else int((self.clock() - o) * RATE)

    def time_of(self, pos):
        """Clock time of a timeline position (samples)."""
        return None if pos is None or self.origin is None else self.origin + pos / RATE

    # -- the WindowsAudio interface -------------------------------------------------------------
    def capture(self, on_pcm, loopback=False, **_kw):
        cap = SimCapture(self, on_pcm, "loopback" if loopback else "mic")
        with self._lock:
            self.captures.append(cap)
        return cap

    def playback(self, device_index=None, on_played=None, **_kw):
        # the controller plays SPEAK into the virtual mic (the index virtual_mic_output() gave) and
        # HEAR on the output the person listens to (the default one: no index, or any other)
        speak = device_index is not None and self._vmic is not None and device_index == self._vmic
        sink = SimSink(self, "speak" if speak else "hear", device_index, on_played)
        with self._lock:
            self.sinks.append(sink)
        return sink

    def virtual_mic_output(self):
        return self._vmic

    def default_output_name(self):
        return self.output_name

    def ducker(self, level):
        d = SimDucker(self, level)
        with self._lock:
            self.duckers.append(d)
        return d

    def close(self):
        for c in list(self.captures):
            c.stop()
        self.closed = True

    # -- mixing ---------------------------------------------------------------------------------
    def sinks_named(self, name):
        return [s for s in self.sinks if s.name == name]

    def _set_duck(self, gain):
        with self._lock:
            self.duck_gain = float(gain)
            self.duck_log.append((self.clock(), float(gain)))

    def duck_gain_at(self, t):
        """The call's gain in the loopback at clock time t (from the duck log)."""
        g = 1.0
        for when, gain in self.duck_log:
            if when > t:
                break
            g = gain
        return g

    def gusan_shift(self, kind):
        """How much later Gusan's HEAR voice shows up in a capture of ``kind`` (samples)."""
        return self.loop_delay if kind == "loopback" else self.echo_delay

    def _gusan(self, kind, pos, n):
        """Gusan's own (HEAR) voice in a capture of ``kind`` at [pos, pos+n): float32, or None
        where it cannot get in."""
        hear = self.sinks_named("hear")
        if not hear or (kind == "mic" and not self.speakers_echo):
            return None
        out = np.zeros(int(n), np.float32)
        if kind == "loopback":
            for s in hear:
                out += s.read(pos - self.loop_delay, n)
            return out
        if self.echo_ir is not None:          # the room's whole path (delay, level, reverb)
            # The capture thread asks twice per chunk (its mix, then the dry-run listener): keep the
            # last few on the thread (what already played never changes).
            cache = getattr(_tls, "room", None)
            if cache is None or cache[0] is not self:
                cache = _tls.room = (self, collections.OrderedDict())
            key = (int(pos), int(n))
            hit = cache[1].get(key)
            if hit is not None:
                return hit.copy()
            m = len(self.echo_ir)
            for s in hear:
                if s.has_audio(pos - m + 1, pos + n):
                    out += self._room(s.read(pos - m + 1, n + m - 1), n)
            if pos + n <= (self.pos_now() or 0):
                cache[1][key] = out.copy()
                while len(cache[1]) > 16:
                    cache[1].popitem(last=False)
            return out
        for s in hear:
            out += s.read(pos - self.echo_delay, n)
        return out * self.echo_gain

    def _room(self, w, n):
        """The last ``n`` samples of ``w`` (n + len(ir) - 1 long) through the room: an FFT
        convolution with the impulse response's spectrum cached per size (np.convolve of a 0.3 s
        response for every 20 ms is too slow for a fast clock)."""
        m = len(self.echo_ir)
        nfft = 1 << (len(w) - 1).bit_length()
        spec = self._ir_spec.get(nfft)
        if spec is None:
            spec = self._ir_spec[nfft] = np.fft.rfft(self.echo_ir.astype(np.float64), nfft)
        y = np.fft.irfft(np.fft.rfft(np.asarray(w, np.float64), nfft) * spec, nfft)
        return y[m - 1:m - 1 + n].astype(np.float32)

    def gusan_track(self, kind, n):
        """Gusan's own (HEAR) voice as a capture of ``kind`` heard it over timeline [0, n), for the
        whole-run analysis (the room path by FFT); None where it cannot get in."""
        hear = self.sinks_named("hear")
        if not hear or (kind == "mic" and not self.speakers_echo):
            return None
        if kind == "loopback" or self.echo_ir is None:
            return self._gusan(kind, 0, n)
        m = len(self.echo_ir)
        out = np.zeros(int(n), np.float32)
        for s in hear:
            w = s.read(-m + 1, n + m - 1)
            out += benchaudio.convolve(w, self.echo_ir)[m - 1:m - 1 + n]
        return out

    def duck_gains(self, times):
        """The call's gain in the loopback at each clock time of ``times`` (array)."""
        times = np.asarray(times, dtype=np.float64)
        log = list(self.duck_log)
        if not log:
            return np.ones(len(times))
        lt = np.asarray([t for t, _g in log], dtype=np.float64)
        lg = np.asarray([g for _t, g in log], dtype=np.float64)
        k = np.searchsorted(lt, times, side="right") - 1
        return np.where(k >= 0, lg[np.clip(k, 0, None)], 1.0)

    def mix(self, kind, pos, n, duck=1.0):
        """The int16 samples a capture of ``kind`` hands out for [pos, pos+n)."""
        if kind == "loopback":
            for s in self.sinks_named("hear"):
                s.render_until(pos + n)      # the device took them: on_played (the canceller's reference)
            x = _slice(self.system, pos, n) * duck
        else:
            x = _slice(self.mic, pos, n)
        g = self._gusan(kind, pos, n)
        if g is not None:
            x = x + g
        return np.clip(np.round(x), -32768, 32767).astype(np.int16)

    def components(self, kind, pos, n, duck=1.0):
        """Ground truth of [pos, pos+n) in a capture of ``kind``: ({"own": the clean own track,
        "other": what the variant adds to it (the call heard through the room when
        leak_is_speech, else noise), "gusan": Gusan's own voice in it or None}, [the Utterances
        in "gusan"]). float32 arrays."""
        if kind == "loopback":
            own = _slice(self.system_clean, pos, n) * duck
            other = _slice(self.system, pos, n) * duck - own
        else:
            own = _slice(self.mic_clean, pos, n)
            other = _slice(self.mic, pos, n) - own
        gusan = self._gusan(kind, pos, n)
        utts = []
        if gusan is not None:
            shift = self.gusan_shift(kind)
            for s in self.sinks_named("hear"):
                for u in s.utterances_in(pos - shift, n):
                    if u not in utts:
                        utts.append(u)
        return {"own": own, "other": other, "gusan": gusan}, utts


class SimCapture:
    """A capture thread: hands out 20 ms chunks in real time (clock), aligned to the timeline;
    silence before the conversation begins. Late chunks (the machine could not keep up) are
    caught up at once, like WASAPI after a hiccup, and counted."""

    def __init__(self, sim, on_pcm, kind):
        self.sim = sim
        self.on_pcm = on_pcm
        self.kind = kind
        self._stop = threading.Event()
        self._thread = None
        self.started = self.stopped = False
        self.chunks = 0
        self.max_lag = 0.0                   # clock seconds a chunk was handed out late, at most
        self.late = 0                        # chunks more than LATE_S late
        self.errors = 0                      # exceptions from the engine's on_pcm
        self.first_error = None
        self.pos = None                      # the next timeline position to hand out

    def done_through(self, pos):
        """True once everything before ``pos`` has been handed out (or the capture has stopped)."""
        return self.stopped or (self.pos is not None and self.pos >= pos)

    def start(self):
        if self.started:
            return
        self.started = True
        self._thread = threading.Thread(target=self._run, name=f"sim-capture-{self.kind}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.stopped = True
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2.0)

    def _emit(self, pcm, pos):
        _tls.chunk = None if pos is None else Chunk(self.sim, self.kind, pos, CHUNK, self.sim.duck_gain)
        try:
            self.on_pcm(pcm)
        except Exception as e:  # noqa: BLE001 - the real capture swallows these too; count them
            self.errors += 1
            if self.first_error is None:
                self.first_error = f"{type(e).__name__}: {e}"
                logger.warning("engine on_pcm (%s) raised: %s", self.kind, self.first_error, exc_info=True)
        finally:
            _tls.chunk = None
        self.chunks += 1

    def _run(self):
        sim = self.sim
        silence = bytes(CHUNK * 2)
        pre_t0, pre_sent = sim.clock(), 0
        nxt = None
        step = CHUNK / RATE / sim.speed
        while not self._stop.is_set():
            if sim.origin is None:
                due = int((sim.clock() - pre_t0) * RATE) // CHUNK
                while pre_sent < due and not self._stop.is_set():
                    self._emit(silence, None)
                    pre_sent += 1
                time.sleep(min(0.005, step / 2))
                continue
            now = sim.pos_now()
            if nxt is None:
                nxt = (now // CHUNK) * CHUNK
            while nxt + CHUNK <= now and not self._stop.is_set():
                lag = (now - (nxt + CHUNK)) / RATE
                if lag > self.max_lag:
                    self.max_lag = lag
                if lag > LATE_S:
                    self.late += 1
                pcm = sim.mix(self.kind, nxt, CHUNK, sim.duck_gain)
                self._emit(pcm.tobytes(), nxt)
                nxt += CHUNK
                self.pos = nxt
            time.sleep(min(0.005, step / 2))


class SimSink:
    """A playback device on the timeline. play() appends after what is queued (or 20 ms from now
    when idle), so where every sample lands is known at once; render_until() hands everything the
    device took to on_played in order, the silence between phrases too (like PlaybackSink's
    callback: the own-voice canceller counts samples, so its reference moves with the device)."""

    def __init__(self, sim, name, device_index, on_played):
        self.sim = sim
        self.name = name
        self.device_index = device_index
        self.on_played = on_played
        self.out = Timeline()
        self.sched_end = None
        self._rendered = None                # the timeline position on_played has reached (None: not yet)
        self._render_lock = threading.Lock() # one render at a time, so on_played sees the device's order
        self._pieces = []                    # (start, end, Utterance or None), in order, never overlapping
        self._starts = []
        self.log = []                        # (clock, start, n) per play()
        self.clears = []                     # (clock, at, samples dropped)
        self._lock = threading.Lock()
        self.started = self.stopped = False
        self.played_samples = 0              # samples handed to on_played (silence included)

    def start(self):
        self.started = True
        with self._lock:
            if self._rendered is None:
                self._rendered = self._now()     # the device takes sound from now on, silence too

    def stop(self):
        self.clear()                         # closing the stream: what is still queued never plays
        self.stopped = True

    def _now(self):
        # Asked from a capture's thread (the engine's gate deciding about the chunk in hand): answer
        # for that chunk's time, as a machine that keeps up would, even when this one fell behind.
        ch = current_chunk()
        if ch is not None and ch.sim is self.sim:
            return ch.pos + ch.n
        p = self.sim.pos_now()
        return 0 if p is None else p

    def play(self, pcm):
        if self.stopped:
            return
        a = np.frombuffer(bytes(pcm), dtype=np.int16)
        if not a.size:
            return
        utt = current_utterance()
        with self._lock:
            now = self._now()
            start = now + self.sim.out_latency
            if self.sched_end is not None and self.sched_end > start:
                start = self.sched_end
            end = start + int(a.size)
            self.out.write(start, a)
            self._pieces.append((start, end, utt))
            self._starts.append(start)
            self.sched_end = end
            self.log.append((self.sim.clock(), start, int(a.size)))
        if utt is not None:
            utt.scheduled(start, int(a.size))

    def render_until(self, pos):
        """Everything before ``pos`` has now been taken by the device: on_played gets all of it, in
        order and without gaps - the phrases and the silence around them (a sink that was never
        started begins at its first sound)."""
        if self.on_played is None or self.stopped:
            return
        with self._render_lock:
            with self._lock:
                start = self._rendered
                if start is None:
                    start = min(pos, self._starts[0]) if self._starts else pos
                if pos <= start:
                    self._rendered = start
                    return
                a = self.out.read(start, pos - start).astype(np.int16)
                self._rendered = pos
            self.played_samples += int(a.size)
            try:
                self.on_played(a.tobytes())
            except Exception:  # noqa: BLE001 - like PlaybackSink
                logger.debug("on_played failed", exc_info=True)

    def read(self, pos, n):
        with self._lock:
            return self.out.read(pos, n)

    def has_audio(self, a, b):
        """True when something was scheduled to play somewhere in [a, b) (pieces are in order and
        never overlap, so the last one starting before b decides)."""
        with self._lock:
            j = bisect.bisect_left(self._starts, b) - 1
            return j >= 0 and self._pieces[j][1] > a

    def queued_seconds(self):
        with self._lock:
            now = self._now()
            if self.sched_end is None or self.sched_end <= now:
                return 0.0
            return (self.sched_end - now) / RATE

    def is_playing(self, tail_s=0.3):
        with self._lock:
            if self.sched_end is None:
                return False
            return self._now() < self.sched_end + int(tail_s * RATE)

    def clear(self):
        with self._lock:
            now = self._now()
            if self.sched_end is None or self.sched_end <= now:
                return
            self.out.zero(now, self.sched_end)
            while self._pieces and self._pieces[-1][0] >= now:
                self._pieces.pop()
                self._starts.pop()
            if self._pieces and self._pieces[-1][1] > now:
                s, _e, u = self._pieces[-1]
                self._pieces[-1] = (s, now, u)
            self.clears.append((self.sim.clock(), now, self.sched_end - now))
            self.sched_end = now

    def utterances_in(self, pos, n):
        """The Utterances whose audio is in [pos, pos+n) of this sink."""
        with self._lock:
            j = bisect.bisect_left(self._starts, pos + n) - 1
            found = []
            while j >= 0:
                st, en, u = self._pieces[j]
                if en <= pos:
                    break
                if u is not None and u not in found:
                    found.append(u)
                j -= 1
        return found[::-1]

    def spans(self):
        """[(start, end, Utterance)] of what played, one entry per phrase (pieces joined)."""
        with self._lock:
            pieces = list(self._pieces)
        out = []
        for st, en, u in pieces:
            if out and out[-1][2] is u and st <= out[-1][1] + RATE // 50:
                out[-1] = (out[-1][0], en, u)
            else:
                out.append((st, en, u))
        return out


class SimDucker:
    """Ducker for the bench: counts duck / restore; the call is turned down in the loopback.
    close() puts it back for good, like audio_io.Ducker (a duck() after it does nothing)."""

    def __init__(self, sim, level):
        self.sim = sim
        self.level = float(level)
        self.ducks = self.restores = 0
        self.ducked = False
        self.closed = False

    def duck(self):
        if not self.ducked and not self.closed:
            self.ducked = True
            self.ducks += 1
            self.sim._set_duck(self.level)

    def restore(self):
        if self.ducked:
            self.ducked = False
            self.restores += 1
            self.sim._set_duck(1.0)

    def close(self):
        self.closed = True
        self.restore()


# ── text units (shared with the runner) ──────────────────────────────────────────────────────
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
_TAG = re.compile(r"\[[^\]\n]{1,40}\]")          # ElevenLabs v3 audio tags such as [laughs]: never spoken
_DROP = {"՛", "՜", "՞", "'", "’", "ʼ", "`", "­"}   # marks inside a word


def strip_tags(text):
    return " ".join(_TAG.sub(" ", text or "").split())


def norm_text(text):
    """For comparing what was said: NFKC, case folded, audio tags and punctuation out (marks
    inside a word, like Armenian ՞ ՛ ՜ and apostrophes, are deleted so the word stays whole)."""
    s = unicodedata.normalize("NFKC", strip_tags(text)).casefold()
    out = []
    for ch in s:
        if ch in _DROP:
            continue
        cat = unicodedata.category(ch)
        out.append(" " if cat[0] in "PZC" or cat in ("Sm", "Sk", "So") else ch)
    return " ".join("".join(out).split())


def norm_units(text):
    """Comparison units: words, and single characters inside Chinese / Japanese text."""
    out = []
    for w in norm_text(text).split():
        if not _CJK.search(w):
            out.append(w)
            continue
        buf = ""
        for ch in w:
            if _CJK.match(ch):
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(ch)
            else:
                buf += ch
        if buf:
            out.append(buf)
    return out


def spoken_units(text):
    """The units a listener hears one after another in ``text`` (words; characters for Chinese /
    Japanese without spaces), as written (punctuation kept), with each unit's end as a fraction
    of the time the text takes to say. -> (units, joiner, ends)."""
    text = strip_tags(text)
    if not text:
        return [], " ", []
    dense = " " not in text and bool(_CJK.search(text))
    units = [c for c in text if not c.isspace()] if dense else text.split()
    weights = [sum(0.16 if _CJK.match(c) else 0.065 for c in u) + (0.0 if dense else 0.05) for u in units]
    total = sum(weights) or 1.0
    acc, ends = 0.0, []
    for w in weights:
        acc += w
        ends.append(acc / total)
    return units, ("" if dense else " "), ends


# ── dry-run stand-ins ────────────────────────────────────────────────────────────────────────
RX_FLOOR = 60.0        # rms of 20 ms the engine sent below which nothing is heard (muted / silent)
SRC_FLOOR = 150.0      # rms of a ground-truth component below which it is not speech
HEARD_GAIN = 0.35      # a component is heard when the sent audio still holds this much of it


class OracleWorld:
    """What the dry-run stand-ins know about one run: the script's turns on each track (and where
    each of their words sits on the timeline) and the simulated devices."""

    def __init__(self, sim, turns, *, error_rate=0.03, seed=0, leak_delay_s=0.035):
        self.sim = sim
        self.faults = None                    # Faults of a sockets dry run (fake_connect sets it)
        self.error_rate = float(error_rate)
        self.seed = seed
        self.leak_delay = int(round(leak_delay_s * RATE))
        self.tracks = {"mic": [], "system": []}
        for i, t in enumerate(turns):
            st, en = int(round(float(t["start"]) * RATE)), int(round(float(t["end"]) * RATE))
            src = self._source(("turn", i), t.get("text") or "", st, en)
            self.tracks["mic" if t.get("track") == "mic" else "system"].append(src)
        for v in self.tracks.values():
            v.sort(key=lambda s: s["start"])

    @staticmethod
    def _source(key, text, st, en):
        units, joiner, ends = spoken_units(text)
        prev = [0.0] + ends[:-1]
        spans = [(st + a * (en - st), st + b * (en - st)) for a, b in zip(prev, ends)]
        return {"key": key, "start": st, "end": en, "units": units, "joiner": joiner, "spans": spans}

    def _sources_at(self, track, pos, n, shift=0):
        return [s for s in self.tracks[track] if s["start"] + shift < pos + n and s["end"] + shift > pos]

    def _utt_source(self, utt):
        span = utt.span()
        if span is None:
            return None
        return self._source(("utt", utt.id), utt.text, span[0], span[1])

    def _garble(self, key, k, unit):
        if self.error_rate <= 0:
            return unit
        h = zlib.crc32(f"{self.seed}|{key}|{k}".encode("utf-8"))
        if h / 2 ** 32 >= self.error_rate:
            return unit
        core = unit.rstrip(".,!?;:։՝")
        tail = unit[len(core):]
        if len(core) <= 2:
            return core + core[-1:] + tail
        i = 1 + h % (len(core) - 1)
        return core[:i - 1] + core[i:] + tail          # one letter lost

    def hear(self, chunk, pcm):
        """What an ideal listener makes of ``pcm`` (the 20 ms the engine sent for ``chunk``):
        (speech?, [(source, p0, p1, heard?)]) - for every source present in this chunk (its
        ground-truth component has energy), whether it really got through to the listener: the
        sent audio is projected on the component, so what the gate muted or the canceller removed
        is not heard. p0..p1 is the chunk in the source's own time (samples)."""
        x = np.frombuffer(bytes(pcm), dtype=np.int16).astype(np.float32)
        loud = x.size > 0 and rms(x) >= RX_FLOOR
        comps, utts = self.sim.components(chunk.kind, chunk.pos, chunk.n, chunk.duck)
        own_track = "system" if chunk.kind == "loopback" else "mic"
        speech, marks = False, []
        for name in ("own", "other", "gusan"):
            c = comps.get(name)
            if c is None or rms(c) < SRC_FLOOR:
                continue
            if name == "other" and not (self.sim.leak_is_speech and chunk.kind == "mic"):
                continue                                        # noise is nobody's words
            den = float(np.dot(c, c))
            heard = loud and den > 0 and float(np.dot(x, c)) / den >= HEARD_GAIN
            speech = speech or heard
            if name == "own":
                srcs, shift = self._sources_at(own_track, chunk.pos, chunk.n), 0
            elif name == "other":
                srcs, shift = self._sources_at("system", chunk.pos, chunk.n, self.leak_delay), self.leak_delay
            else:
                srcs = [s for s in (self._utt_source(u) for u in utts) if s]
                shift = self.sim.gusan_shift(chunk.kind)
            for s in srcs:
                marks.append((s, chunk.pos - shift, chunk.pos + chunk.n - shift, heard))
        return speech, marks


class _Marks:
    """Evidence about one source: the stretches (in the source's own time) where it was present in
    what reached the listener, and whether it got through each time."""

    def __init__(self):
        self.p0, self.items = [], []

    def add(self, p0, p1, heard):
        self.p0.append(p0)
        self.items.append((p0, p1, heard))

    def heard(self, a, b, look=int(0.6 * RATE)):
        """Did the word spoken in [a, b) get through? By the evidence inside it (majority, by
        length); for a word that falls in a pause of the audio (the words are only spread over
        the turn by length), by the nearest evidence on both sides."""
        i = bisect.bisect_left(self.p0, b)
        got = lost = 0.0
        j = i - 1
        while j >= 0 and self.items[j][1] > a:
            p0, p1, h = self.items[j]
            ov = min(b, p1) - max(a, p0)
            if ov > 0:
                if h:
                    got += ov
                else:
                    lost += ov
            j -= 1
        if got or lost:
            return got > 0 and got >= lost
        near = []
        if j >= 0 and self.items[j][1] > a - look:
            near.append(self.items[j][2])
        if i < len(self.items) and self.items[i][0] < b + look:
            near.append(self.items[i][2])
        return bool(near) and all(near)


class OracleEar:
    """The listening half of the dry-run speech-to-text: a 20 ms chunk is speech when any source
    in it got through (no energy threshold, so noise and muting behave); a word is heard when its
    stretch of time got through; partial text every ``partial_every`` s while speech goes on (the
    words fully received so far), a commit after ``vad_silence`` s without speech (like Scribe's
    commit_strategy=vad), or after ``max_segment`` s at the latest."""

    def __init__(self, world, vad_silence, emit, partial_every=0.25, max_segment=25.0):
        self.world = world
        self.vad_silence = float(vad_silence)
        self.emit = emit
        self.partial_every = partial_every
        self.max_segment = max_segment
        self._lock = threading.Lock()
        self._marks = {}                 # source key -> _Marks
        self._sources = {}               # source key -> latest source dict (+ "shift")
        self._done = {}                  # source key -> unit indices already committed
        self._last_pos = None
        self._reset()

    def _reset(self):
        self._order = []
        self._in_speech = False
        self._silence = 0.0
        self._seg_start = self._last_partial = None

    def _text(self, final):
        parts = []
        for key in self._order:
            src = self._sources[key]
            marks = self._marks[key]
            done = self._done.setdefault(key, set())
            limit = self._last_pos - src["shift"]          # the newest sample received, in the source's time
            got = []
            for k, (a, b) in enumerate(src["spans"]):
                if k in done:
                    continue
                if (b > limit and not final) or a >= limit:
                    break                                   # not (fully) said yet
                if marks.heard(a, min(b, limit)):
                    got.append(self.world._garble(key, k, src["units"][k]))
                if final:
                    done.add(k)
            if got:
                parts.append(src["joiner"].join(got))
        return " ".join(p for p in parts if p)

    def feed(self, pcm, chunk):
        speech, marks = self.world.hear(chunk, pcm)
        now = self.world.sim.clock()
        out = []
        with self._lock:
            self._last_pos = chunk.pos + chunk.n
            for src, p0, p1, heard in marks:
                key = src["key"]
                self._sources[key] = dict(src, shift=chunk.pos - p0)
                self._marks.setdefault(key, _Marks()).add(p0, p1, heard)
                if heard and key not in self._order:
                    self._order.append(key)
            if speech:
                if not self._in_speech:
                    self._in_speech, self._seg_start, self._last_partial = True, now, now
                self._silence = 0.0
                if now - self._last_partial >= self.partial_every:
                    text = self._text(final=False)
                    if text:
                        self._last_partial = now
                        out.append(("partial", text))
                if now - self._seg_start >= self.max_segment:
                    out.append(("committed", self._text(final=True)))
                    self._reset()
            elif self._in_speech:
                self._silence += chunk.n / RATE
                if self._silence >= self.vad_silence:
                    out.append(("committed", self._text(final=True)))
                    self._reset()
        for kind, text in out:
            if text:
                self.emit(kind, text)


class _Pump:
    """Delivers items after a delay (clock), in order, on its own thread (like a socket reader)."""

    def __init__(self, sim, deliver, name):
        self.sim = sim
        self.deliver = deliver
        self._q = queue.Queue()
        self._last_due = 0.0
        self._lock = threading.Lock()
        self.closed = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self):
        self._thread.start()

    def put(self, delay, *item):
        with self._lock:
            due = max(self._last_due, self.sim.clock() + delay)
            self._last_due = due
        self._q.put((due, item))

    def close(self):
        self.closed = True
        self._q.put(None)

    def _run(self):
        while True:
            it = self._q.get()
            if it is None or self.closed:
                return
            due, item = it
            wait = due - self.sim.clock()
            if wait > 0:
                self.sim.sleep(wait)
            if self.closed:
                return
            try:
                self.deliver(*item)
            except Exception:  # noqa: BLE001
                logger.exception("dry-run event delivery failed")


class OracleScribe:
    """Dry run, class level: stands in for eleven.ScribeRealtime (same constructor and methods)."""
    CONNECT_S = 0.25
    PARTIAL_DELAY = 0.08
    COMMIT_DELAY = 0.15

    def __init__(self, token, language, on_event, *, token_provider=None, keyterms=(), vad_silence=0.4,
                 world=None, **_kw):
        if world is None:
            raise ValueError("OracleScribe needs world=")
        self.token = token
        self.language = language
        self._on_event = on_event
        self._token_provider = token_provider
        self.keyterms = list(keyterms or ())
        self.vad_silence = float(vad_silence)
        self.world = world
        self.session_config = None
        self._closing = False
        self._pump = _Pump(world.sim, self._deliver, "oracle-scribe")
        self._ear = OracleEar(world, self.vad_silence, self._emit)

    def start(self):
        self.world.sim.sleep(self.CONNECT_S)
        self._pump.start()

    def send_audio(self, pcm):
        ch = current_chunk()
        if self._closing or ch is None:
            return
        self._ear.feed(pcm, ch)

    def close(self):
        self._closing = True
        self._pump.close()

    def _emit(self, kind, text):
        self._pump.put(self.COMMIT_DELAY if kind == "committed" else self.PARTIAL_DELAY, kind, text)

    def _deliver(self, kind, text):
        if not self._closing:
            self._on_event(kind, text)


# -- the bundled voices -----------------------------------------------------------------------
_SAMPLES = {}
_SAMPLES_LOCK = threading.Lock()


def _voice_gender(voice_id):
    try:
        from interpreter import voices          # the app's presets (repo root on sys.path)
        p = voices.preset(voice_id)
        return p[2] if p else "neutral"
    except Exception:  # noqa: BLE001
        return "neutral"


def voice_sample(voice_id):
    """The preset's bundled sample (assets/voices/<id>.wav, trimmed) as int16, or a buzz at a man's /
    woman's pitch for a voice without one."""
    with _SAMPLES_LOCK:
        a = _SAMPLES.get(voice_id)
        if a is not None:
            return a
        path = os.path.join(VOICE_DIR, f"{voice_id}.wav")
        if voice_id and os.path.isfile(path):
            x, sr = benchaudio.read_wav(path)
            if sr != RATE:
                x = benchaudio.resample(x, sr, RATE)
            x = benchaudio.trim_silence(x)
        else:
            f0 = benchaudio.GENDER_F0.get(_voice_gender(voice_id), 165.0)
            x = benchaudio.synth_voice("la la la la la la la", f0=f0, seed=zlib.crc32(str(voice_id).encode()))
            x = benchaudio.trim_silence(x)
        a = np.clip(np.round(np.asarray(x, np.float64) * 32768.0), -32768, 32767).astype(np.int16)
        _SAMPLES[voice_id] = a
        return a


def synth_speech(voice_id, text):
    """What the dry-run voices say: the voice's sample looped to the time ``text`` takes to say
    (~14 characters a second, CJK ~6), faded in and out, then 120 ms of silence like a real
    voice's tail."""
    base = voice_sample(voice_id)
    t = strip_tags(text)
    secs = sum(0.16 if _CJK.match(c) else 1 / 14.0 for c in t if not c.isspace()) + 0.04 * t.count(" ")
    n = max(int(0.4 * RATE), int(secs * RATE))
    a = np.tile(base, n // max(1, len(base)) + 1)[:n].astype(np.float32)
    ramp = min(160, n // 4)
    if ramp:
        a[:ramp] *= np.linspace(0.0, 1.0, ramp, dtype=np.float32)
        a[-ramp:] *= np.linspace(1.0, 0.0, ramp, dtype=np.float32)
    return np.concatenate([np.round(a).astype(np.int16), np.zeros(int(0.12 * RATE), np.int16)])


class OracleVoice:
    """Dry run, class level: stands in for eleven.DialogueVoice. First audio 0.34 s after a phrase
    on a fresh socket, 0.09 s later on; audio streams 3x faster than real time in 100 ms pieces."""
    CONNECT_S, FIRST_S, NEXT_S, STREAM_X = 0.25, 0.34, 0.09, 3.0

    def __init__(self, token_provider, voice_id, language, *, model=None, world=None, **_kw):
        if world is None:
            raise ValueError("OracleVoice needs world=")
        ids = [voice_id] if isinstance(voice_id, str) or voice_id is None else list(voice_id)
        self.voice_ids = [v for v in ids if v]
        self.voice_id = self.voice_ids[0] if self.voice_ids else None
        self.language = language
        self.model = model or "oracle"
        self.world = world
        self._token_provider = token_provider
        self.warm_text = "Okay."
        self.tokens = []
        self._fresh = True
        self._open = False

    def open(self, warm=True):
        self.tokens.append(self._token_provider())
        self.world.sim.sleep(self.CONNECT_S)
        self._open, self._fresh = True, True
        if warm and self.warm_text:
            self.speak(self.warm_text, lambda _pcm: None)

    def reopen(self):
        self.close()
        self.open(warm=False)

    def set_voices(self, voice_ids):
        ids = [v for v in voice_ids if v]
        if ids:
            self.voice_ids, self.voice_id = ids, ids[0]

    def close(self):
        self._open = False

    def speak(self, text, on_audio, should_stop=lambda: False, voice_id=None):
        text = (text or "").strip()
        if not text:
            return 0.0
        if not self._open:
            raise RuntimeError("voice socket is closed")
        pcm = synth_speech(voice_id or self.voice_id, text)
        utt = current_utterance()
        if utt is not None:
            utt.planned = len(pcm)
        sim = self.world.sim
        sim.sleep(self.FIRST_S if self._fresh else self.NEXT_S)
        self._fresh = False
        step = RATE // 10
        for i in range(0, len(pcm), step):
            if not should_stop():
                on_audio(pcm[i:i + step].tobytes())
            sim.sleep(step / RATE / self.STREAM_X)
        return len(pcm) / RATE


# -- socket level: the real eleven.py clients talk to these ------------------------------------
FAULT_KINDS = ("stt_drop", "voice_drop")


class Faults:
    """Sockets that drop on purpose (sockets dry runs: does the engine reconnect, what is lost):
    {"stt_drop": [s, ...], "voice_drop": [s, ...]} in seconds into the conversation. Each time
    fires once per direction (sockets tell their direction by their language)."""

    def __init__(self, spec=None):
        self.spec = {k: sorted(float(x) for x in v) for k, v in (spec or {}).items()}
        self.fired = []                       # (kind, at, language, clock)
        self._done = set()
        self._lock = threading.Lock()

    def due(self, kind, pos_s, language, clock=None):
        """True once for each (kind, time, language) when the conversation has reached that time."""
        if pos_s is None:
            return False
        with self._lock:
            for at in self.spec.get(kind, ()):
                key = (kind, at, language)
                if pos_s >= at and key not in self._done:
                    self._done.add(key)
                    self.fired.append((kind, at, language, clock))
                    return True
        return False


class FakeScribeSocket:
    """Dry run, socket level: what wss://api.elevenlabs.io/v1/speech-to-text/realtime does for
    eleven.ScribeRealtime (input_audio_chunk in; session_started, partial_transcript,
    committed_transcript out)."""

    def __init__(self, url, world, faults=None):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.params = {k: v if len(v) > 1 else v[0] for k, v in q.items()}
        self.token = (q.get("token") or [""])[0]
        self.language = (q.get("language_code") or [None])[0]
        vad = float((q.get("vad_silence_threshold_secs") or ["0.4"])[0])
        self.world = world
        self.faults = faults
        self.sent_chunks = 0
        self._out = queue.Queue()
        self._closed = False
        self._ear = OracleEar(world, vad, self._emit)
        self._pump = _Pump(world.sim, self._put, "fake-scribe-socket")
        self._pump.start()
        self._out.put(json.dumps({"message_type": "session_started",
                                  "config": {"model_id": self.params.get("model_id"), "fake": True}}))

    def _emit(self, kind, text):
        self._pump.put(OracleScribe.COMMIT_DELAY if kind == "committed" else OracleScribe.PARTIAL_DELAY,
                       kind, text)

    def _put(self, kind, text):
        mt = "committed_transcript" if kind == "committed" else "partial_transcript"
        self._out.put(json.dumps({"message_type": mt, "text": text}))

    def send(self, msg):
        if self._closed:
            raise ConnectionError("socket closed")
        d = json.loads(msg)
        if d.get("message_type") != "input_audio_chunk":
            return
        self.sent_chunks += 1
        pcm = base64.b64decode(d.get("audio_base_64") or "")
        ch = current_chunk()
        if ch is None:
            return                              # the lead-in before the conversation
        # the client sends 100 ms at a time, ending with the chunk being handed out now: cut it
        # back into the capture's 20 ms chunks
        n = len(pcm) // 2
        end = ch.pos + ch.n
        for off in range(0, n, CHUNK):
            k = min(CHUNK, n - off)
            self._ear.feed(pcm[off * 2:(off + k) * 2], Chunk(ch.sim, ch.kind, end - n + off, k, ch.duck))
        if self.faults is not None and self.faults.due("stt_drop", end / RATE, self.language, self.world.sim.clock()):
            self.drop()

    def drop(self):
        """The service hangs up (what it said so far stays said; what was in flight is lost)."""
        if not self._closed:
            self._closed = True
            self._pump.close()
            self._out.put(None)

    def __iter__(self):
        while True:
            item = self._out.get()
            if item is None:
                return
            yield item

    def close(self):
        if not self._closed:
            self._closed = True
            self._pump.close()
            self._out.put(None)


class FakeDialogueSocket:
    """Dry run, socket level: wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input for
    eleven.DialogueVoice ({"voices"}, {"inputs"} + {"flush"}, keep_alive, close_socket in;
    {"audio"} pieces and {"is_final_audio_for_turn"} out)."""

    def __init__(self, url, world, faults=None):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.model = (q.get("model_id") or [""])[0]
        self.token = (q.get("single_use_token") or [""])[0]
        self.language = (q.get("language_code") or [None])[0]
        self.world = world
        self.faults = faults
        self.voices = []
        self.texts = []
        self._inputs = []
        self._utt = None
        self._out = queue.Queue()
        self._closed = False
        self._fresh = True

    def send(self, msg):
        if self._closed:
            raise ConnectionError("socket closed")
        d = json.loads(msg)
        if "voices" in d:
            self.voices = list(d.get("voices") or [])
        elif d.get("inputs"):
            self._inputs.extend(d["inputs"])
            self._utt = current_utterance()
        elif d.get("flush"):
            items, self._inputs = self._inputs, []
            if not items:
                return
            pos = self.world.sim.pos_now()
            if self.faults is not None and self.faults.due("voice_drop", None if pos is None else pos / RATE,
                                                           self.language, self.world.sim.clock()):
                self.close()                    # hangs up with the phrase in flight
                return
            vid = items[-1].get("voice_id")
            if vid not in self.voices:
                self._out.put(json.dumps({"error": "voice_not_registered", "message": f"{vid} not in voices"}))
                return
            text = " ".join((i.get("text") or "").strip() for i in items).strip()
            self.texts.append(text)
            pcm = synth_speech(vid, text)
            if self._utt is not None:
                self._utt.planned = len(pcm)
            threading.Thread(target=self._stream, args=(pcm,), name="fake-ttd", daemon=True).start()
        elif d.get("close_socket"):
            self.close()

    def _stream(self, pcm):
        sim = self.world.sim
        sim.sleep(OracleVoice.FIRST_S if self._fresh else OracleVoice.NEXT_S)
        self._fresh = False
        step = RATE // 10
        for i in range(0, len(pcm), step):
            if self._closed:
                return
            self._out.put(json.dumps({"audio": base64.b64encode(pcm[i:i + step].tobytes()).decode("ascii")}))
            sim.sleep(step / RATE / OracleVoice.STREAM_X)
        self._out.put(json.dumps({"is_final_audio_for_turn": True}))

    def recv(self, timeout=None):
        try:
            item = self._out.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no message")
        if item is None:
            self._out.put(None)
            raise ConnectionError("socket closed")
        return item

    def close(self):
        if not self._closed:
            self._closed = True
            self._out.put(None)


def fake_connect(world, sockets=None, faults=None):
    """A websocket ``connect(url)`` for eleven.py's clients that never leaves the process.
    faults: a Faults or its spec dict ({"stt_drop": [s], "voice_drop": [s]})."""
    if faults is not None and not isinstance(faults, Faults):
        faults = Faults(faults)
    world.faults = faults

    def connect(url):
        if "speech-to-text/realtime" in url:
            ws = FakeScribeSocket(url, world, faults)
        elif "text-to-dialogue" in url:
            ws = FakeDialogueSocket(url, world, faults)
        else:
            raise ValueError(f"dry run: no fake socket for {url.split('?')[0]}")
        if sockets is not None:
            sockets.append(ws)
        return ws
    return connect


# -- translation --------------------------------------------------------------------------------
class OracleTranslator:
    """Dry-run translation from the conversation's own references: finds the turn (in any of its
    languages) a phrase comes from and answers with the same share of that turn in the target
    language. Text it cannot place (an echo of Gusan's voice, say) comes back as "[xx] text".
    Call it as translations(text, src, dst) (FakeBench's ``translations`` hook)."""

    _SENT = re.compile(r"(?<=[.!?։;。！？])\s*")

    def __init__(self, turns):
        self.turns = list(turns)
        self.versions = []                   # (turn index, language, text, comparison units)
        for i, t in enumerate(self.turns):
            vs = dict(t.get("ref") or {})
            vs[t.get("lang") or "?"] = t.get("text") or ""
            for lang, txt in vs.items():
                u = norm_units(txt)
                if u:
                    self.versions.append((i, lang, txt, u))

    def _target(self, turn, dst):
        if dst == turn.get("lang"):
            return turn.get("text") or ""
        ref = turn.get("ref") or {}
        return ref.get(dst) or ref.get("en") or turn.get("text") or ""

    @classmethod
    def _sentences(cls, text):
        return [s for s in cls._SENT.split(strip_tags(text)) if norm_units(s)]

    @staticmethod
    def _share(text, lo_frac, hi_frac):
        tu, joiner, _ends = spoken_units(text)
        if not tu:
            return ""
        m = len(tu)
        lo = min(m - 1, int(round(lo_frac * m)))
        hi = max(lo + 1, int(round(hi_frac * m)))
        return joiner.join(tu[lo:hi])

    def __call__(self, text, src, dst):
        units = norm_units(text)
        if not units:
            return ""
        best = None
        for i, lang, vtext, vu in self.versions:
            sm = difflib.SequenceMatcher(None, units, vu, autojunk=False)
            blocks = [b for b in sm.get_matching_blocks() if b.size]
            score = sum(b.size for b in blocks) / len(units) + (0.05 if lang == src else 0.0)
            if blocks and (best is None or score > best[0]):
                best = (score, i, vtext, blocks, len(vu))
        if best is None or best[0] < 0.4:
            return f"[{dst}] {strip_tags(text)}"
        _score, i, vtext, blocks, n = best
        a, b = blocks[0].b, blocks[-1].b + blocks[-1].size
        target = self._target(self.turns[i], dst)
        ss, ts = self._sentences(vtext), self._sentences(target)
        if len(ss) > 1 and len(ss) == len(ts):
            # sentence by sentence: the share of each source sentence the phrase covers
            out, s0 = [], 0
            for s, t in zip(ss, ts):
                s1 = s0 + len(norm_units(s))
                lo, hi = max(a, s0), min(b, s1)
                if hi > lo:
                    out.append(self._share(t, (lo - s0) / (s1 - s0), (hi - s0) / (s1 - s0)))
                s0 = s1
            return " ".join(x for x in out if x)
        return self._share(target, a / n, b / n)
