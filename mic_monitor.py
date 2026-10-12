"""MicMonitor: a short, level-only microphone stream for the mic check (Gusan 2.0).

    mon = MicMonitor(pa=app.recorder.audio, parent=app)    # share the app's PyAudio
    mon.devices_ready.connect(fill_picker)                  # [(index, name)], default index
    mon.refresh_devices()                                   # lists on a worker
    mon.level_changed.connect(draw)                         # RMS 0..1, ~30 a second, GUI thread
    mon.start(index)                                        # None = the system's default mic
    ...
    mon.stop()                                              # on leaving the page

Nothing is recorded or kept: each ~33 ms block (16 kHz mono, 16-bit) becomes one
RMS value and is thrown away. Creating PyAudio, listing, opening, closing and
terminating all happen on worker threads (a PortAudio open can take a few
hundred ms) under their own lock, so the GUI thread never waits on the audio
driver: start() / stop() / close() only flip state and hand the work over.
Each start() gets its own stream callback, so a late block from a stream that
is being closed never moves the level. The level comes from PortAudio's
callback thread; ``level_changed`` is a Qt signal, so a slot on a GUI object
runs on the GUI thread (queued).

The interface also matches what ui/pages/settings.py reads from an optional
``app.mic_monitor``: ``devices()`` -> [(index, name)], ``start(index)``,
``stop()``, ``level`` (0..1: the last block's peak, like the page's own
PyAudioLevel) and ``error`` ("" or a short reason). ``rms`` is the last RMS
(0..1) - what ``level_changed`` carries; ``display_level(rms)`` maps it to a
0..1 meter value (-55 dBFS .. -12 dBFS).

Devices are listed the way ui/pages/settings.py does it: the default host API
only (MME on Windows: one entry per mic, resampled to any rate), inputs only,
no loopback / "Sound Mapper" / "Primary Sound Capture" entries, no duplicate
names. "(R)" / "(TM)" are dropped from the names ("Microphone (Realtek Audio)").

Integration notes for Wave 4
----------------------------
* AppController: ``self.mic_monitor = MicMonitor(pa=self.recorder.audio,
  parent=self)`` (cheap: nothing opens until start()). Settings' Microphone
  card and the onboarding mic check then share it (both already look for
  ``app.mic_monitor``; the onboarding builds its own otherwise). DONE; at
  Quit the controller calls ``stop_now()`` (closes on the GUI thread before
  recorder.shutdown() terminates the shared PyAudio).
* Dictation keeps its own stream (AudioRecorder): the monitor is only open
  while a mic-check is on screen, and both can be open at once.
* Without a PyAudio (``pa=None``) the monitor creates its own on the first
  worker that needs it (pyaudiowpatch on Windows, else pyaudio - as main.py
  imports it) and terminates it in close().
"""

import logging
import math
import threading

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger("transcribe")

RATE = 16000
BLOCK = 533                       # 16000 / 533 = ~30 levels a second
FALLBACK_RATES = (48000, 44100)   # a device that refuses 16 kHz (rare with MME)
FLOOR_DB = -55.0                  # display_level: silence ...
CEIL_DB = -12.0                   # ... to a loud voice close to the mic

MSG_NO_MIC = "No microphone found."
MSG_CANT_OPEN = "Can't open this microphone."

_SKIP_NAMES = ("loopback", "sound mapper", "primary sound capture", "stereo mix", "what u hear")


# ── pure helpers (no Qt, no audio) ───────────────────────────────────────────
def rms_int16(data):
    """(rms, peak), both 0..1, of little-endian 16-bit mono PCM bytes."""
    if not data:
        return 0.0, 0.0
    try:
        import numpy as np
        a = np.frombuffer(data, dtype=np.int16)
        if not a.size:
            return 0.0, 0.0
        f = a.astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(f * f))), float(np.max(np.abs(f)))
    except ImportError:
        import array
        a = array.array("h")
        a.frombytes(bytes(data[: len(data) // 2 * 2]))
        if not len(a):
            return 0.0, 0.0
        sq = sum(int(v) * int(v) for v in a) / float(len(a))
        return math.sqrt(sq) / 32768.0, max(abs(int(v)) for v in a) / 32768.0


def level_db(rms):
    """RMS (0..1) in dBFS; -120 for silence."""
    try:
        rms = float(rms)
    except (TypeError, ValueError):
        return -120.0
    if rms <= 1e-6:
        return -120.0
    return max(-120.0, 20.0 * math.log10(rms))


def display_level(rms, floor_db=FLOOR_DB, ceil_db=CEIL_DB):
    """A meter value 0..1 for an RMS level: FLOOR_DB -> 0, CEIL_DB -> 1."""
    db = level_db(rms)
    v = (db - floor_db) / float(ceil_db - floor_db)
    return max(0.0, min(1.0, v))


def clean_device_name(name):
    """'Microphone (Realtek(R) Audio)' -> 'Microphone (Realtek Audio)'."""
    if isinstance(name, bytes):
        name = name.decode("utf-8", errors="ignore")
    name = str(name or "")
    for junk in ("(R)", "(r)", "(TM)", "(tm)", "®", "™"):
        name = name.replace(junk, "")
    name = " ".join(name.split())
    name = name.replace("( ", "(").replace(" )", ")")
    if name.count("(") > name.count(")"):
        name = name + "…"          # MME cuts names at 31 characters
    return name.strip()


def list_input_devices(pa):
    """[(index, name)] of the microphones in the default host API (see the
    module docstring). [] when PortAudio can't answer."""
    out, seen = [], set()
    if pa is None:
        return out
    try:
        host = pa.get_default_host_api_info().get("index")
    except Exception:
        host = None
    try:
        count = int(pa.get_device_count())
    except Exception:
        return out
    for i in range(count):
        try:
            d = pa.get_device_info_by_index(i)
        except Exception:
            continue
        try:
            if int(d.get("maxInputChannels", 0) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        if host is not None and d.get("hostApi") not in (None, host):
            continue
        raw = d.get("name") or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        low = str(raw).lower()
        if not raw or any(k in low for k in _SKIP_NAMES) or d.get("isLoopbackDevice"):
            continue
        name = clean_device_name(raw)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append((int(d.get("index", i)), name))
    return out


def default_input_index(pa):
    """The system's default input device index, or None."""
    if pa is None:
        return None
    try:
        return int(pa.get_default_input_device_info().get("index"))
    except Exception:
        return None


def _default_pa_factory():
    try:
        import pyaudiowpatch as pyaudio          # Windows builds (main.py prefers it)
    except ImportError:
        import pyaudio
    return pyaudio.PyAudio()


def _thread(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True, name="gusan-mic").start()


# ── the monitor ──────────────────────────────────────────────────────────────
class MicMonitor(QObject):
    """See the module docstring."""

    level_changed = Signal(float)          # RMS 0..1, ~30/s
    devices_ready = Signal(list, object)   # [(index, name)], default index (or None)
    opened = Signal(object)                # the device index that opened (None = default)
    failed = Signal(str)                   # MSG_CANT_OPEN / MSG_NO_MIC

    def __init__(self, pa=None, parent=None, pa_factory=None, spawn=None, block=BLOCK):
        super().__init__(parent)
        self._pa = pa
        self._own_pa = False
        self._pa_factory = pa_factory or _default_pa_factory
        self._spawn = spawn or _thread
        self._block = int(block)
        self._lock = threading.RLock()       # state only (gen, stream, levels): held briefly, GUI-safe
        self._pa_lock = threading.RLock()    # PyAudio's life (create, list, open, close, terminate): workers
        self._stream = None
        self._gen = 0
        self._running = False
        self.device_index = None
        self.level = 0.0          # last block's peak (0..1)
        self.peak = 0.0
        self.rms = 0.0            # last block's RMS (0..1)
        self.error = ""

    # ── PortAudio ──
    def audio(self):
        """The PyAudio instance (created on first use: call on a worker)."""
        with self._pa_lock:
            if self._pa is None:
                self._pa = self._pa_factory()
                self._own_pa = True
            return self._pa

    def devices(self):
        """[(index, name)] - blocking, any thread."""
        try:
            with self._pa_lock:
                return list_input_devices(self.audio())
        except Exception:
            logger.info("Listing microphones failed", exc_info=True)
            return []

    def default_index(self):
        try:
            with self._pa_lock:
                return default_input_index(self.audio())
        except Exception:
            return None

    def refresh_devices(self):
        """List the microphones on a worker -> devices_ready(list, default)."""
        def _run():
            devs = self.devices()
            default = self.default_index()
            self._emit(self.devices_ready, devs, default)
        self._spawn(_run)

    # ── the level stream ──
    def is_running(self):
        return self._running

    def start(self, index=None):
        """Open ``index`` (None = default mic) on a worker and start measuring.
        Replaces a stream that is already open. Any thread; never blocks."""
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._running = True
            self.device_index = index
            self.error = ""
        self._spawn(self._open, gen, index)

    def stop(self):
        """Close the stream on a worker. Any thread; never blocks."""
        with self._lock:
            self._gen += 1
            self._running = False
            stream, self._stream = self._stream, None
            self.level = self.peak = self.rms = 0.0
        if stream is not None:
            self._spawn(self._close_locked, stream)

    def stop_now(self, timeout=1.0):
        """App quit: stop and close the stream on THIS thread, before the
        app terminates the shared PyAudio (a worker's close racing that
        terminate is a native crash). Waits at most ``timeout`` s for a worker
        that holds PyAudio (an open in flight then closes its own stream).
        True when nothing of this monitor is still touching PortAudio."""
        with self._lock:
            self._gen += 1
            self._running = False
            stream, self._stream = self._stream, None
            self.level = self.peak = self.rms = 0.0
        if not self._pa_lock.acquire(timeout=max(0.0, float(timeout))):
            logger.info("Mic monitor still busy at quit")
            return False
        try:
            if stream is not None:
                self._close_stream(stream)
        finally:
            self._pa_lock.release()
        return True

    def close(self):
        """stop(), then let go of a PyAudio this monitor created itself - on one
        worker, after any open still in flight (never terminate under it)."""
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._running = False
            stream, self._stream = self._stream, None
            self.level = self.peak = self.rms = 0.0

        def _shut():
            with self._pa_lock:
                if stream is not None:
                    self._close_stream(stream)
                with self._lock:
                    if gen != self._gen:
                        return                   # started again meanwhile: keep PyAudio
                    pa, own = self._pa, self._own_pa
                    if own:
                        self._pa, self._own_pa = None, False
                if own and pa is not None:
                    try:
                        pa.terminate()
                    except Exception:
                        pass
        self._spawn(_shut)

    # ── workers ──
    def _current(self, gen):
        with self._lock:
            return gen == self._gen

    def _open(self, gen, index):
        with self._pa_lock:
            if not self._current(gen):
                return                           # stopped before this worker ran: open nothing
            try:
                pa = self.audio()
            except Exception as e:
                logger.info("Mic monitor: no audio system: %s", e)
                self._fail(gen, MSG_NO_MIC)
                return
            stream, err = None, None
            rates = (RATE,) + FALLBACK_RATES
            if index is not None:
                try:
                    dr = int(float(pa.get_device_info_by_index(int(index)).get("defaultSampleRate") or 0))
                    if dr and dr not in rates:
                        rates = rates + (dr,)
                except Exception:
                    pass
            for rate in rates:
                if not self._current(gen):
                    return                       # stopped / restarted meanwhile
                try:
                    kw = dict(format=pa.get_format_from_width(2), channels=1, rate=int(rate), input=True,
                              frames_per_buffer=max(64, int(self._block * rate / RATE)),
                              stream_callback=self._callback_for(gen))
                    if index is not None:
                        kw["input_device_index"] = int(index)
                    stream = pa.open(**kw)
                    break
                except Exception as e:
                    err = e
                    stream = None
            if stream is None:
                logger.info("Mic monitor: can't open device %r: %s", index, err)
                no_mic = index is None and default_input_index(pa) is None
                self._fail(gen, MSG_NO_MIC if no_mic else MSG_CANT_OPEN)
                return
            with self._lock:
                stale = gen != self._gen
                old = None
                if not stale:
                    old, self._stream = self._stream, stream
            if stale:
                self._close_stream(stream)
                return
            if old is not None:
                self._close_stream(old)
            try:
                if hasattr(stream, "start_stream") and hasattr(stream, "is_active") and not stream.is_active():
                    stream.start_stream()
            except Exception:
                pass
        self._emit(self.opened, index)

    def _fail(self, gen, msg):
        with self._lock:
            if gen != self._gen:
                return
            self.error = msg
            self.level = self.peak = self.rms = 0.0
        self._emit(self.failed, msg)

    def _callback_for(self, gen):
        """The stream callback of one start(): blocks from an older stream
        (still closing on a worker) are dropped."""
        def callback(in_data, frames, time_info, status):
            if gen != self._gen:
                return (None, 0)                 # paContinue; its worker closes it
            return self._callback(in_data, frames, time_info, status)
        return callback

    def _callback(self, in_data, _frames, _time_info, _status):
        try:
            rms, peak = rms_int16(in_data)
            if self._running:
                self.rms, self.peak, self.level = rms, peak, peak
                self._emit(self.level_changed, rms)
        except Exception:
            pass
        return (None, 0)                        # paContinue

    def _close_locked(self, stream):
        with self._pa_lock:
            self._close_stream(stream)

    @staticmethod
    def _close_stream(stream):
        try:
            stream.stop_stream()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def _emit(self, signal, *args):
        try:
            signal.emit(*args)
        except RuntimeError:
            pass                                # the monitor was deleted (window closed)
