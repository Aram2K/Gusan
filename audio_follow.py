"""Meetings follow the computer's sound devices (Gusan 2.1.11).

A meeting records two things: the microphone and - on Windows - the call (what the computer plays,
captured through a WASAPI loopback of the output device). Before 2.1.11 both were opened once, at
Start, on the devices PortAudio listed when Gusan started: a headset plugged in mid-call left the
call's track silent (the loopback stayed on the old speakers), and an unplugged microphone ended the
whole recording. Now:

* ``DeviceWatch`` reads what the computer uses right now, about once a second, on its own thread:
  on Windows WinMM's live lists and its default devices (``winmm_state``: a few milliseconds, also the
  "communications" output call apps may play on), on a Mac Core Audio's default devices
  (``mac_state``). A change counts once it is seen twice in a row (a headset that connects sends a
  burst of changes).
* ``MeetingDevices`` hands the recorder two stream look-alikes - ``MicInput`` and ``CallInput`` - with
  the surface the record loop already reads (get_read_available / read / stop_stream / close). They
  are only ever called on the record thread, so they can close and open streams themselves (closing
  a stream from another thread is a native crash). Both deliver 16 kHz mono float32 whatever the
  device gives (44.1 / 48 kHz, a Bluetooth hands-free 8 / 16 kHz, stereo, 5.1 / 7.1).
* A change of the default output moves the call to the new device's loopback - and to the
  communications output as well when that is another device (both are captured: a call plays on
  one of them). A change of the default microphone moves the mic there. A device that is not in
  PortAudio's list (plugged in after it was made) needs a new PortAudio: the streams are closed
  first and ``reinit`` is asked - it refuses while anything else has a stream open (the mic check in
  Settings), and then the device is looked up by name in the list we have (Windows: the Sound Mapper
  always opens the current default microphone).
* The microphone is the loop's clock. While it is gone (unplugged, its driver stalled) MicInput keeps
  time with silence, the call goes on being recorded, and the default microphone is opened again as
  soon as one is there. The gap of a switch is filled with silence, so the tracks never drift.
* ``take_events()`` tells the meeting what happened ("Switched to ...", a microphone that went away);
  ``call_quiet_sec()`` how long the call has been silent while the microphone heard someone.

Pure Python + numpy; nothing here imports PortAudio (the recorder passes its PyAudio and format).
"""
import collections
import logging
import re
import sys
import threading
import time

import numpy as np

logger = logging.getLogger("transcribe")

RATE = 16000
POLL_S = 1.0              # how often the watcher reads the system's devices
TICK_S = 0.05             # the record thread looks at the watcher at most this often
STALL_S = 1.5             # a microphone that delivers nothing this long is gone
RETRY_S = 3.0             # a device that is gone is looked for again this often
MAX_GAP_S = 5.0           # the most silence a switch adds (a sleep of the computer is not made up)
MAX_BEHIND_S = 2.0        # silence made while the mic is gone never runs further behind the clock
HEARD = 0.003             # a block louder than this (peak) carries sound
MIC_RECENT_S = 60.0       # the call's silence counts only while the microphone heard someone this recently
SOUND_MAPPER = "microsoft sound mapper - input"


# ── sound in the app's format ────────────────────────────────────────────────
class Resampler:
    """Linear resampling that carries on across blocks: no click at a block's edge and no samples
    lost to rounding (the old per-block interpolation drifted ~0.1 %)."""

    def __init__(self, rate_in, rate_out=RATE):
        self.rate_in, self.rate_out = int(rate_in), int(rate_out)
        self._step = self.rate_in / float(self.rate_out)
        self._pos = 0.0                                  # next output sample, in input samples from _prev[0]
        self._prev = np.zeros(0, np.float32)

    def process(self, x):
        x = np.asarray(x, dtype=np.float32)
        if self.rate_in == self.rate_out or not len(x):
            return x
        buf = np.concatenate([self._prev, x]) if len(self._prev) else x
        last = len(buf) - 1
        n = 0 if self._pos > last else int(np.floor((last - self._pos) / self._step)) + 1
        pos = self._pos + self._step * np.arange(n)
        out = np.interp(pos, np.arange(len(buf)), buf).astype(np.float32)
        self._pos = self._pos + self._step * n - last
        self._prev = buf[-1:].copy()
        return out


def downmix(arr, channels, front=False):
    """Interleaved float32 -> mono. ``front``: a surround output's loopback (5.1 / 7.1), where a
    call's stereo fills only the front pair - mixed from front left and right (and the centre),
    never averaged with the silent rest (that put the call ~12 dB down)."""
    arr = np.asarray(arr, dtype=np.float32)
    if channels <= 1 or not arr.size:
        return arr
    usable = arr.size - (arr.size % channels)
    frames = arr[:usable].reshape(-1, channels)
    if front and channels > 2:
        out = (frames[:, 0] + frames[:, 1]) * 0.5
        if channels in (6, 8):
            out = out + 0.7071 * frames[:, 2]
        return np.clip(out, -1.0, 1.0).astype(np.float32)
    return frames.mean(axis=1).astype(np.float32)


class Converter:
    """A device's raw float32 bytes (its rate and channels) -> 16 kHz mono float32."""

    def __init__(self, rate=RATE, channels=1, front=False):
        self.rate, self.channels, self.front = int(rate or RATE), max(1, int(channels or 1)), bool(front)
        self._rs = Resampler(self.rate, RATE)

    def process(self, raw):
        arr = np.frombuffer(raw, dtype=np.float32) if isinstance(raw, (bytes, bytearray)) else raw
        arr = np.nan_to_num(np.asarray(arr, dtype=np.float32), nan=0.0, posinf=1.0, neginf=-1.0)
        return self._rs.process(downmix(arr, self.channels, self.front))

    def frames_for(self, out_frames):
        """Device frames that make about ``out_frames`` 16 kHz samples (at least one)."""
        return max(1, int(np.ceil(out_frames * self.rate / float(RATE))))

    def out_for(self, device_frames):
        return int(device_frames * RATE // self.rate) if device_frames > 0 else 0


# ── device names ─────────────────────────────────────────────────────────────
def device_name(info):
    """A PortAudio device's name as text ("" when there is none)."""
    name = (info or {}).get("name", "") if isinstance(info, dict) else ""
    return name.decode("utf-8", errors="ignore") if isinstance(name, bytes) else str(name or "")


def name_key(name):
    """A name to compare by: WinMM gives 31 characters and PortAudio may spell a ® or an accent
    differently - only plain letters, digits and brackets are compared, case-blind."""
    name = re.sub(r"\[loopback\]", "", str(name or ""), flags=re.I)
    return re.sub(r"[^a-z0-9()]+", " ", name.lower()).strip()


def same_device(a, b):
    """True when two names (a full one and a 31-character WinMM one) are the same device."""
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, full = (ka, kb) if len(ka) < len(kb) else (kb, ka)
    return len(short) >= 12 and full.startswith(short)


def nice_name(name):
    """What a notice calls a device: 'Headphones (WH-1000XM4 Stereo)' (no [Loopback], no (R))."""
    name = re.sub(r"\s*\[loopback\]\s*", "", str(name or ""), flags=re.I)
    for junk in ("(R)", "(r)", "(TM)", "(tm)", "®", "™"):
        name = name.replace(junk, "")
    name = " ".join(name.split()).replace("( ", "(").replace(" )", ")")
    if name.count("(") > name.count(")"):
        name += "…"                                       # WinMM cuts names at 31 characters
    return name.strip()


# ── what the computer uses now ───────────────────────────────────────────────
_WINMM = []


def winmm_state():
    """Windows, read live through WinMM in a few milliseconds: {"in", "out", "comms_out": the default
    microphone, output and communications output; "ins", "outs": every microphone and output} by the
    names PortAudio's MME list gives them (31 characters). PortAudio keeps the list it found when it
    started. None when it can't be read."""
    try:
        import ctypes
        from ctypes import wintypes
        if not _WINMM:
            class _InCaps(ctypes.Structure):
                _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                            ("szPname", wintypes.WCHAR * 32), ("dwFormats", wintypes.DWORD),
                            ("wChannels", wintypes.WORD), ("wReserved1", wintypes.WORD)]

            class _OutCaps(ctypes.Structure):
                _fields_ = _InCaps._fields_ + [("dwSupport", wintypes.DWORD)]

            winmm = ctypes.WinDLL("winmm")
            sides = []
            for count, caps, message, cls in (
                    (winmm.waveInGetNumDevs, winmm.waveInGetDevCapsW, winmm.waveInMessage, _InCaps),
                    (winmm.waveOutGetNumDevs, winmm.waveOutGetDevCapsW, winmm.waveOutMessage, _OutCaps)):
                caps.argtypes = [ctypes.c_size_t, ctypes.c_void_p, wintypes.UINT]
                message.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
                sides.append((count, caps, message, cls))
            _WINMM.append(sides)
        found = []
        for count, caps, message, cls in _WINMM[0]:
            names = []
            for i in range(int(count())):
                c = cls()
                if caps(i, ctypes.byref(c), ctypes.sizeof(c)) == 0:
                    names.append(c.szPname)
            picked = []
            # DRVM_MAPPER_PREFERRED_GET / DRVM_MAPPER_CONSOLEVOICECOM_GET on WAVE_MAPPER: the default
            # device and the default communications device
            for msg in (0x2015, 0x2017):
                dev, flags = wintypes.DWORD(0xFFFFFFFF), wintypes.DWORD(0)
                name = ""
                if message(ctypes.c_void_p(0xFFFFFFFF), msg, ctypes.byref(dev), ctypes.byref(flags)) == 0:
                    c = cls()
                    if dev.value != 0xFFFFFFFF and caps(dev.value, ctypes.byref(c), ctypes.sizeof(c)) == 0:
                        name = c.szPname
                picked.append(name)
            found.append((names, picked))
        (ins, (pin, _cin)), (outs, (pout, cout)) = found
        return {"in": pin, "out": pout, "comms_out": cout or pout,
                "ins": tuple(sorted(ins)), "outs": tuple(sorted(outs))}
    except Exception:
        logger.debug("Audio devices: Windows' list could not be read", exc_info=True)
        return None


_CA = []


def _fourcc(s):
    return int.from_bytes(s.encode("ascii"), "big")


def mac_state():
    """macOS, read live through Core Audio: {"in", "out": the default input's and output's names,
    "count": how many devices there are}. None when it can't be read."""
    try:
        import ctypes
        if not _CA:
            ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

            class Addr(ctypes.Structure):
                _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("elem", ctypes.c_uint32)]
            ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32,
                                                      ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                                      ctypes.c_void_p]
            ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
            ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32,
                                                          ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
            cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
            cf.CFStringGetCString.restype = ctypes.c_bool
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            cf.CFRelease.restype = None
            _CA.append((ca, cf, Addr))
        ca, cf, Addr = _CA[0]
        glob = _fourcc("glob")

        def default(sel):
            dev, size = ctypes.c_uint32(0), ctypes.c_uint32(4)
            if ca.AudioObjectGetPropertyData(1, ctypes.byref(Addr(_fourcc(sel), glob, 0)), 0, None,
                                             ctypes.byref(size), ctypes.byref(dev)) != 0:
                return 0
            return int(dev.value)

        def name(dev):
            if not dev:
                return ""
            ref, size = ctypes.c_void_p(), ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
            if ca.AudioObjectGetPropertyData(dev, ctypes.byref(Addr(_fourcc("lnam"), glob, 0)), 0, None,
                                             ctypes.byref(size), ctypes.byref(ref)) != 0 or not ref.value:
                return ""
            buf = ctypes.create_string_buffer(512)
            ok = cf.CFStringGetCString(ref, buf, 512, 0x08000100)       # kCFStringEncodingUTF8
            cf.CFRelease(ref)
            return buf.value.decode("utf-8", errors="ignore") if ok else ""

        size = ctypes.c_uint32(0)
        ca.AudioObjectGetPropertyDataSize(1, ctypes.byref(Addr(_fourcc("dev#"), glob, 0)), 0, None,
                                          ctypes.byref(size))
        return {"in": name(default("dIn ")), "out": name(default("dOut")), "count": int(size.value) // 4}
    except Exception:
        logger.debug("Audio devices: Core Audio could not be read", exc_info=True)
        return None


def system_state():
    """What this computer uses right now (winmm_state / mac_state), or None."""
    if sys.platform == "win32":
        return winmm_state()
    if sys.platform == "darwin":
        return mac_state()
    return None


def changed_keys(old, new):
    old, new = old or {}, new or {}
    return {k for k in set(old) | set(new) if old.get(k) != new.get(k)}


# ── watching ─────────────────────────────────────────────────────────────────
class DeviceWatch:
    """Reads ``read_state()`` every ``poll_s`` on its own thread. ``generation`` grows with every
    settled change (and once for the first reading); ``state`` is the settled state, ``what`` the keys
    the last change touched. A new state counts once it is read twice in a row (a headset connecting
    is a burst of changes). ``step(state)`` is one reading, for tests."""

    def __init__(self, read_state=None, poll_s=POLL_S, start=True):
        self._read = read_state or system_state
        self.poll_s = float(poll_s)
        self.generation = 0
        self.state = None
        self.what = set()
        self._candidate = None
        self._stop = threading.Event()
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="gusan-devices", daemon=True)
            self._thread.start()

    def step(self, now):
        """One reading. True when it published a change."""
        if now is None:
            return False
        if self.state is None:
            self.state, self.what, self._candidate = now, set(), None
            self.generation += 1
            return True
        if now == self.state:
            self._candidate = None
            return False
        if now == self._candidate:
            self.what = changed_keys(self.state, now)
            self.state, self._candidate = now, None
            self.generation += 1
            logger.info("Audio devices changed: %s", ", ".join(sorted(self.what)) or "?")
            return True
        self._candidate = now
        return False

    def _run(self):
        while not self._stop.is_set():
            try:
                self.step(self._read())
            except Exception:
                logger.debug("Audio devices: reading them failed", exc_info=True)
            self._stop.wait(self.poll_s)

    def stop(self):
        self._stop.set()


# ── PortAudio lookups (any PyAudio-like object) ──────────────────────────────
def _devices(pa):
    try:
        count = int(pa.get_device_count())
    except Exception:
        return []
    out = []
    for i in range(count):
        try:
            out.append((i, pa.get_device_info_by_index(i)))
        except Exception:
            continue
    return out


def find_loopback(pa, name):
    """The index of the WASAPI loopback of the output called ``name`` (a WinMM name will do), or None."""
    if not name:
        return None
    best = None
    for i, d in _devices(pa):
        if not (d.get("isLoopbackDevice") or "[loopback]" in device_name(d).lower()):
            continue
        if name_key(device_name(d)) == name_key(name):
            return int(d.get("index", i))
        if best is None and same_device(device_name(d), name):
            best = int(d.get("index", i))
    return best


def default_loopback(pa):
    """(index, name) of the loopback of PortAudio's default output, or (None, "")."""
    try:
        info = pa.get_default_wasapi_loopback()
        if info and info.get("index") is not None:
            return int(info["index"]), device_name(info)
    except Exception:
        logger.debug("no default WASAPI loopback", exc_info=True)
    return None, ""


def find_input(pa, name):
    """The index of the microphone called ``name`` in PortAudio's default host API (MME on Windows,
    Core Audio on a Mac), or None. Loopbacks never count."""
    if not name:
        return None
    try:
        host = pa.get_default_host_api_info().get("index")
    except Exception:
        host = None
    best = None
    for i, d in _devices(pa):
        try:
            if int(d.get("maxInputChannels") or 0) <= 0 or d.get("isLoopbackDevice"):
                continue
        except (TypeError, ValueError):
            continue
        if host is not None and d.get("hostApi") not in (None, host):
            continue
        if name_key(device_name(d)) == name_key(name):
            return int(d.get("index", i))
        if best is None and same_device(device_name(d), name):
            best = int(d.get("index", i))
    return best


def default_input_name(pa):
    try:
        return device_name(pa.get_default_input_device_info())
    except Exception:
        return ""


def open_input(pa, fmt, index=None, chunk=1024):
    """A microphone stream: (stream, rate, channels). 16 kHz mono first (MME and Core Audio convert);
    a device that won't open so is opened in its own rate and channels (converted afterwards)."""
    args = dict(format=fmt, channels=1, rate=RATE, input=True, frames_per_buffer=int(chunk))
    if index is not None:
        args["input_device_index"] = int(index)
    try:
        return pa.open(**args), RATE, 1
    except Exception as first:
        try:
            info = (pa.get_device_info_by_index(int(index)) if index is not None
                    else pa.get_default_input_device_info())
            rate = int(info.get("defaultSampleRate") or 48000)
            channels = max(1, min(2, int(info.get("maxInputChannels") or 1)))
        except Exception:
            raise first
        if rate == RATE and channels == 1:
            raise first
        args.update(rate=rate, channels=channels, frames_per_buffer=int(chunk * rate / RATE))
        return pa.open(**args), rate, channels


def open_loopback(pa, fmt, index, chunk=1024):
    """A WASAPI loopback stream in the device's own format (WASAPI takes no other): (stream, rate,
    channels)."""
    info = pa.get_device_info_by_index(int(index))
    rate = int(info.get("defaultSampleRate") or 48000)
    channels = max(1, int(info.get("maxInputChannels") or 2))
    stream = pa.open(format=fmt, channels=channels, rate=rate, input=True, input_device_index=int(index),
                     frames_per_buffer=int(chunk * rate / RATE))
    return stream, rate, channels


def close_stream(stream):
    """Stop and close a stream; whatever a device that went away answers is ignored."""
    if stream is None:
        return
    try:
        stream.stop_stream()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


# ── the meeting's microphone ─────────────────────────────────────────────────
class MicInput:
    """The meeting's microphone as the record loop reads it (16 kHz mono float32), whatever device is
    behind it - or none: while the microphone is gone it keeps time with silence (``lost``), so the
    loop and the call's track go on. Record thread only."""

    def __init__(self, devs, stream=None, rate=RATE, channels=1, name=""):
        self._devs = devs
        self._stream = None
        self._conv = Converter(rate, channels)
        self.name = ""
        self.lost = True
        self.heard_at = None
        self._buf = np.zeros(0, np.float32)
        self._pad_from = devs.clock()                   # silence is due from here while there is no stream
        self._padded = 0
        self._reports = False                           # the stream tells how much it holds
        if stream is not None:
            self.attach(stream, rate, channels, name)

    # the gap between two devices: silence, as long as the switch took
    def detach(self):
        """Close the stream (a switch is coming); time runs on as silence."""
        close_stream(self._stream)
        self._stream = None
        if self._pad_from is None:
            self._pad_from, self._padded = self._devs.clock(), 0

    def attach(self, stream, rate, channels, name):
        gap = 0
        if self._pad_from is not None:
            gap = int((self._devs.clock() - self._pad_from) * RATE) - self._padded
            gap = max(0, min(gap, int(MAX_GAP_S * RATE)))
        if gap:
            self._buf = np.concatenate([self._buf, np.zeros(gap, np.float32)])
        self._pad_from, self._padded = None, 0
        self._stream = stream
        self._conv = Converter(rate, channels)
        self.name = name or self.name
        self.lost = False
        self._reports = False

    def lose(self, why=""):
        if self.lost:
            return
        logger.warning("Meeting: the microphone stopped (%s) - recording goes on, the call too",
                       str(why)[:120] or "no sound")
        self.detach()
        self.lost = True
        self._devs.note("mic_lost", self.name)

    @property
    def live(self):
        return self._stream is not None and not self.lost

    # PortAudio's surface
    def get_read_available(self):
        self._devs.tick()
        n = len(self._buf)
        if self.live:
            try:
                a = int(self._stream.get_read_available())
            except Exception as e:
                self.lose(e)
                return n
            if a < 0:
                self.lose("error %d" % a)
                return n
            if a > 0:
                self._reports = True
            return n + self._conv.out_for(a)
        return n

    def read(self, n, exception_on_overflow=False):
        self._devs.tick()
        n = max(0, int(n))
        while len(self._buf) < n:
            if self.live:
                self._fill(n - len(self._buf))
            else:
                self._silence(n - len(self._buf))
        out, self._buf = self._buf[:n], self._buf[n:]
        if len(out) and float(np.abs(out).max()) > HEARD:
            self.heard_at = self._devs.clock()
        return out.astype(np.float32).tobytes()

    def _fill(self, need):
        stream, k = self._stream, self._conv.frames_for(need)
        if self._reports:
            # a stream that tells how much it holds is never read blindly: a device that stopped
            # delivering would block the read for good
            deadline, last = self._devs.clock() + STALL_S, -1
            while True:
                try:
                    a = int(stream.get_read_available())
                except Exception as e:
                    self.lose(e)
                    return
                if a < 0:
                    self.lose("error %d" % a)
                    return
                if a >= k:
                    break
                if a > last:
                    last = a
                    if a > 0:
                        deadline = self._devs.clock() + STALL_S
                if self._devs.clock() >= deadline:
                    if a > 0:
                        k = a                            # what there is
                        break
                    self.lose("stalled")
                    return
                self._devs.sleep(min(0.05, max(0.005, (k - a) / float(self._conv.rate))))
        try:
            raw = stream.read(k, exception_on_overflow=False)
        except Exception as e:
            self.lose(e)
            return
        arr = self._conv.process(raw)
        if len(arr):
            self._buf = np.concatenate([self._buf, arr])

    def _silence(self, need):
        now = self._devs.clock()
        if self._pad_from is None:
            self._pad_from, self._padded = now, 0
        due = int((now - self._pad_from) * RATE) - self._padded
        if due > need + MAX_BEHIND_S * RATE:            # the computer slept: that time is not made up
            self._pad_from, self._padded = now - need / float(RATE), 0
            due = need
        if due < need:
            self._devs.sleep((need - due) / float(RATE))
        self._buf = np.concatenate([self._buf, np.zeros(need, np.float32)])
        self._padded += need

    def stop_stream(self):
        pass

    def close(self):
        close_stream(self._stream)
        self._stream = None
        self._devs.closed(self)


# ── the call's sound ─────────────────────────────────────────────────────────
class _Leg:
    """One loopback stream (an output device) and its converter."""

    def __init__(self, stream, rate, channels, name):
        self.stream, self.name, self.lost = stream, name or "", False
        self.conv = Converter(rate, channels, front=True)


class CallInput:
    """The call's sound as the record loop reads a WASAPI loopback - without blocking, 16 kHz mono
    float32, empty while nothing plays - from one or two outputs at once (the default output and the
    communications one, when they differ: a call plays on one of them). ``switches`` grows whenever
    the outputs change: the loop then drops the call's sound still waiting to be mixed. Record
    thread only."""

    def __init__(self, devs, legs=()):
        self._devs = devs
        self.legs = [leg for leg in legs if leg is not None]
        self.switches = 0
        self.heard_at = None
        self._out = np.zeros(0, np.float32)

    @property
    def name(self):
        live = [leg for leg in self.legs if not leg.lost]
        return live[0].name if live else (self.legs[0].name if self.legs else "")

    @property
    def lost(self):
        return not any(not leg.lost for leg in self.legs)

    def names(self):
        return [leg.name for leg in self.legs if not leg.lost]

    def replace(self, legs):
        """New outputs (the old streams are closed by the caller)."""
        self.legs = [leg for leg in legs if leg is not None]
        self.switches += 1
        self._out = self._out[:0]

    def _lose(self, leg, why):
        if leg.lost:
            return
        leg.lost = True
        logger.warning("Meeting: the call's output stopped (%s): %s", nice_name(leg.name), str(why)[:120])
        close_stream(leg.stream)
        leg.stream = None
        if self.lost:                                     # (one of two outputs gone: the other still plays)
            self._devs.note("call_lost", leg.name)
        else:
            self._devs.retry_soon()

    def _pull(self):
        parts = []
        for leg in self.legs:
            if leg.lost or leg.stream is None:
                continue
            try:
                a = int(leg.stream.get_read_available())
            except Exception as e:
                self._lose(leg, e)
                continue
            if a < 0:
                self._lose(leg, "error %d" % a)
                continue
            if a == 0:
                continue
            try:
                raw = leg.stream.read(a, exception_on_overflow=False)
            except Exception as e:
                self._lose(leg, e)
                continue
            arr = leg.conv.process(raw)
            if len(arr):
                parts.append(arr)
        if not parts:
            return
        if len(parts) == 1:
            mixed = parts[0]
        else:                                             # both outputs played: their sum
            mixed = np.zeros(max(len(p) for p in parts), np.float32)
            for p in parts:
                mixed[:len(p)] += p
            np.clip(mixed, -1.0, 1.0, out=mixed)
        if float(np.abs(mixed).max()) > HEARD:
            self.heard_at = self._devs.clock()
        self._out = np.concatenate([self._out, mixed]) if len(self._out) else mixed

    # PortAudio's surface
    def get_read_available(self):
        self._devs.tick()
        self._pull()
        return len(self._out)

    def read(self, n, exception_on_overflow=False):
        n = max(0, int(n))
        out, self._out = self._out[:n], self._out[n:]
        return out.astype(np.float32).tobytes()

    def stop_stream(self):
        pass

    def close(self):
        for leg in self.legs:
            close_stream(leg.stream)
            leg.stream = None
        self._devs.closed(self)


# ── the meeting's devices ────────────────────────────────────────────────────
class MeetingDevices:
    """Owns a meeting's MicInput and CallInput and moves them when the computer's devices change.
    ``get_pa()``: the recorder's PyAudio now; ``can_reinit()`` / ``reinit()``: may PortAudio be made
    anew (nothing else has a stream open) / do it (our streams are closed first) - True when done.
    ``settings_mic``: the microphone picked in Settings ("" = the default one); a microphone the
    computer makes its default mid-meeting wins over it. Record thread only, except take_events /
    call_quiet_sec (any thread)."""

    def __init__(self, get_pa, fmt, *, chunk=1024, watch=None, can_reinit=None, reinit=None,
                 settings_mic="", clock=time.monotonic, sleep=time.sleep):
        self.get_pa, self.fmt, self.chunk = get_pa, fmt, int(chunk)
        self.watch = watch
        self._can_reinit = can_reinit or (lambda: False)
        self._reinit = reinit or (lambda: False)
        self.settings_mic = settings_mic or ""
        self.clock, self.sleep = clock, sleep
        self.mic = None
        self.call = None
        self.events = collections.deque(maxlen=20)
        self.started_at = clock()
        self.reinits = 0
        self._seen_gen = 0
        self._state = None
        self._pending = False
        self._last_tick = None
        self._retry_at = 0.0
        self._reinit_for = None
        self._unmet = False                 # the devices wanted are not all open (a new PortAudio had to wait)
        self._closed = False
        self._open = []

    # building
    def mic_input(self, stream, rate=RATE, channels=1, name=""):
        self.mic = MicInput(self, stream, rate, channels, name)
        self._open.append(self.mic)
        return self.mic

    def call_input(self, stream, rate=RATE, channels=1, name=""):
        legs = [_Leg(stream, rate, channels, name)] if stream is not None else []
        self.call = CallInput(self, legs)
        self._open.append(self.call)
        return self.call

    # what happened (read by the meeting, any thread)
    def note(self, kind, name):
        self.events.append((kind, nice_name(name)))
        if kind.endswith("_lost"):
            self.retry_soon()

    def retry_soon(self):
        """A device went away: look for it (or the next one) in half a second."""
        soon = self.clock() + 0.5
        self._retry_at = min(self._retry_at, soon) if self._retry_at else soon

    def take_events(self):
        out = []
        while self.events:
            try:
                out.append(self.events.popleft())
            except IndexError:
                break
        return out

    def call_quiet_sec(self, now=None):
        """How long the call has made no sound while the microphone heard someone (0 when it can't
        be told: no call, no mic, or the microphone silent too)."""
        mic, call = self.mic, self.call
        if mic is None or call is None or not call.legs:
            return 0.0
        now = self.clock() if now is None else now
        mic_at = mic.heard_at
        if mic_at is None or now - mic_at > MIC_RECENT_S:
            return 0.0
        last = call.heard_at if call.heard_at is not None else self.started_at
        return max(0.0, now - last) if mic_at >= last else 0.0

    def closed(self, part):
        if part in self._open:
            self._open.remove(part)
        if not self._open:
            self._closed = True
            if self.watch is not None:
                self.watch.stop()

    # following (record thread)
    def tick(self):
        if self._closed:
            return
        now = self.clock()
        if self._last_tick is not None and now - self._last_tick < TICK_S:
            return
        self._last_tick = now
        w = self.watch
        if w is not None and w.generation != self._seen_gen:
            self._seen_gen = w.generation
            self._state = w.state
            if "in" in (w.what or ()) and self.settings_mic:
                logger.info("Meeting: the computer has a new default microphone - following it")
                self.settings_mic = ""
            self._pending = True
        due = bool(self._retry_at) and now >= self._retry_at
        if not (self._pending or (due and (self._gone() or self._unmet))):
            return
        changed, self._pending = self._pending, False
        try:
            # PortAudio is made anew for a change the watcher saw, or later for one that had to wait (the
            # mic check had a stream open) - at most once for the same wanted devices, so a device that
            # stays away never closes the microphone every few seconds
            self._switch(allow_reinit=changed or self._unmet)
        except Exception:
            logger.warning("Meeting: following the sound devices failed", exc_info=True)
        self._retry_at = (self.clock() + RETRY_S) if (self._gone() or self._unmet) else 0.0

    def _gone(self):
        """A device of ours is missing: the microphone, or (some of) the call's outputs."""
        if self.mic is not None and (self.mic.lost or self.mic._stream is None):
            return True
        call = self.call
        return call is not None and (not call.legs or any(leg.lost for leg in call.legs))

    def _wanted_mic(self):
        if self.settings_mic:
            return self.settings_mic
        return (self._state or {}).get("in") or None

    def _wanted_outs(self):
        st = self._state
        if not st or "out" not in st:
            return None                                    # (a Mac, or no watcher: PortAudio's default)
        names = []
        for n in (st.get("out"), st.get("comms_out")):
            if n and not any(same_device(n, m) for m in names):
                names.append(n)
        return names

    def _needs(self):
        need_mic = need_call = False
        mic = self.mic
        if mic is not None:
            want = self._wanted_mic()
            need_mic = mic.lost or bool(want and not same_device(mic.name, want))
        call = self.call
        if call is not None and call.legs is not None:
            outs = self._wanted_outs()
            if any(leg.lost for leg in call.legs) or (not call.legs and outs is None):
                need_call = True
            elif outs:
                have = call.names()
                need_call = (any(not any(same_device(h, w) for h in have) for w in outs)
                             or any(not any(same_device(h, w) for w in outs) for h in have))
        return need_mic, need_call

    def _plan_mic(self, pa):
        """(index or None for PortAudio's default, name) of the microphone to open on ``pa``, or None
        when the wanted one is not in its list."""
        want = self._wanted_mic()
        if want:
            idx = find_input(pa, want)
            if idx is not None:
                return idx, device_name(_info(pa, idx)) or want
            if self.settings_mic and same_device(want, self.settings_mic):
                logger.info("Meeting: the microphone picked in Settings is gone - the default one instead")
                self.settings_mic = ""
                return self._plan_mic(pa)
            if self._state and "ins" in self._state:
                idx = find_input(pa, SOUND_MAPPER)         # Windows: always opens the current default
                if idx is not None:
                    return idx, want
            return None
        return None, default_input_name(pa)

    def _plan_call(self, pa):
        """([(index, name)] of the loopbacks to open on ``pa``, all found)."""
        outs = self._wanted_outs()
        if outs is None:
            idx, name = default_loopback(pa)
            return ([(idx, name)] if idx is not None else []), idx is not None
        found = []
        for name in outs:
            idx = find_loopback(pa, name)
            if idx is not None:
                found.append((idx, device_name(_info(pa, idx)) or name))
        return found, len(found) == len(outs)

    def _switch(self, allow_reinit=False):
        need_mic, need_call = self._needs()
        self._unmet = False
        if not (need_mic or need_call):
            return
        pa = self.get_pa()
        mic_plan = self._plan_mic(pa) if need_mic else None
        call_plan, call_all = self._plan_call(pa) if need_call else ([], True)
        complete = (not need_mic or mic_plan is not None) and call_all
        wanted = (self._wanted_mic(), tuple(self._wanted_outs() or ()))
        if not complete and allow_reinit and wanted != self._reinit_for and self._can_reinit():
            self._reinit_for = wanted                 # once per wanted set: names that never match stop here
            self._reopen_all()
            return
        self._unmet = not complete and wanted != self._reinit_for
        if need_mic:
            if mic_plan is not None:
                self._reopen_mic(pa, mic_plan)
            elif self.mic.lost or self.mic._stream is None:
                self._reopen_mic(pa, (None, default_input_name(pa)))   # the best there is
        if need_call and (call_plan or self.call.lost):
            self._reopen_call(pa, call_plan)

    def _reopen_all(self):
        """A device PortAudio doesn't know: close our streams, make PortAudio anew, open both again."""
        logger.info("Meeting: listing the sound devices again (a new device)")
        if self.mic is not None:
            self.mic.detach()
        if self.call is not None:
            for leg in self.call.legs:
                close_stream(leg.stream)
                leg.stream, leg.lost = None, True
        if self._reinit():
            self.reinits += 1
        pa = self.get_pa()
        if self.mic is not None:
            plan = self._plan_mic(pa) or (None, default_input_name(pa))
            self._reopen_mic(pa, plan, force=True)
        if self.call is not None:
            plan, _all = self._plan_call(pa)
            if not plan:
                idx, name = default_loopback(pa)
                plan = [(idx, name)] if idx is not None else []
            self._reopen_call(pa, plan, force=True)

    def _reopen_mic(self, pa, plan, force=False):
        """Open the planned microphone FIRST, then let the old one go: a device that won't open
        leaves the one that works."""
        idx, name = plan
        mic = self.mic
        if mic.live and not force and same_device(mic.name, name):
            return
        old, was_lost = mic.name, mic.lost or mic._stream is None
        for i in [idx] + ([None] if idx is not None else []):
            try:
                stream, rate, channels = open_input(pa, self.fmt, i, self.chunk)
            except Exception as e:
                logger.info("Meeting: microphone %s wouldn't open: %s", nice_name(name) or "(default)", e)
                continue
            if i is None and idx is not None:
                name = default_input_name(pa) or name
            mic.detach()
            mic.attach(stream, rate, channels, name)
            logger.info("Meeting: microphone %s (%d Hz, %d ch)", nice_name(name), rate, channels)
            if was_lost or not same_device(old, name):
                self.note("mic", name)
            return
        if mic._stream is None and not mic.lost:
            mic.lost = True
            self.note("mic_lost", old)

    def _reopen_call(self, pa, plan, force=False):
        call = self.call
        old_primary = call.name
        keep, opened = [], []
        for leg in call.legs:
            wanted = any(same_device(leg.name, n) for _i, n in plan)
            if not force and not leg.lost and leg.stream is not None and wanted:
                keep.append(leg)
            else:
                close_stream(leg.stream)
                leg.stream = None
        for idx, name in plan:
            if any(same_device(leg.name, name) for leg in keep):
                continue
            try:
                stream, rate, channels = open_loopback(pa, self.fmt, idx, self.chunk)
            except Exception as e:
                logger.info("Meeting: the call's output %s wouldn't open: %s", nice_name(name), e)
                continue
            logger.info("Meeting: the call's sound from %s (%d Hz, %d ch)", nice_name(name), rate, channels)
            opened.append(_Leg(stream, rate, channels, name))
        legs = keep + opened
        if [id(x) for x in legs] != [id(x) for x in call.legs] or opened:
            call.replace(legs)
        if legs and not same_device(old_primary, call.name):
            self.note("call", call.name)


def _info(pa, idx):
    try:
        return pa.get_device_info_by_index(int(idx))
    except Exception:
        return {}
