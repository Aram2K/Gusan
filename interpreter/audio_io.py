"""Windows sound for the interpreter: capture (mic, the call's sound), playback, the virtual
microphone, turning the call down, and keeping Gusan's own voice out of what it listens to.

Lessons kept from the meeting recorder (main.AudioRecorder):
* WASAPI loopback delivers NO frames while the computer is silent, and a blocking read can hang
  (closing the stream under it crashed the app). So capture polls ``get_read_available`` on a
  pump thread and never blocks; for the call's sound, silence is filled in by the clock so the
  speech-to-text service still sees the pause that ends a phrase.
* Everything is converted to 16 kHz mono 16-bit, the format the speech service and the voice use.
* Devices are opened on WASAPI in their own format (channels and rate): shared mode takes nothing
  else (5.1 / 7.1 outputs, 4-mic arrays). The device is the one Windows uses now, read live
  (``windows_default``): PortAudio's own idea of the default is from when it started.

Gusan's own voice:
* Hear direction: the loopback also captures the translation Gusan plays. We know exactly what we
  played, so ``OwnVoiceCanceller`` subtracts it. Capture and reference are matched sample by
  sample (both move with the same sound card), so the two threads' timing never matters; a short
  filter models what the sound card did to the voice. What is left is our voice ~30 dB down, so
  ``ListenGate`` lets the other person through while Gusan talks (they talk over the translation)
  and mutes only the leftovers - and everything until our voice has been found in the capture.
* Speak direction with speakers: the mic hears the room. ``ListenGate`` mutes the mic for the speak
  pipeline while the speakers play (the call, or the hear voice), unless the person is clearly
  talking over it (barge-in). True acoustic echo cancelling (WebRTC AEC3) plugs in at the same
  place later.
"""
import logging
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

RATE = 16000
CALL_APPS = {
    "zoom.exe", "ms-teams.exe", "teams.exe", "slack.exe", "webex.exe", "ciscocollabhost.exe",
    "discord.exe", "skype.exe", "whatsapp.exe", "whatsapp.root.exe", "telegram.exe",
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "arc.exe",
}
# BlackHole 2ch (macOS) is one device for both: Speak writes into it, the call app records it.
VIRTUAL_MIC_OUTPUTS = ("gusan speaker", "cable input", "blackhole 2ch")       # what we write into
VIRTUAL_MIC_INPUTS = ("gusan microphone", "cable output", "blackhole 2ch")    # what the call app picks
# How Windows names headphones, in its display languages (the form factor below comes first).
_HEADPHONE_WORDS = (
    "headphone", "headset", "earphone", "earbud", "airpods", "buds", "hands-free",
    "casque", "écouteurs", "kopfhörer", "hoofdtelefoon", "auricular", "cuffie", "fones de ouvido",
    "auscultadores", "słuchawki", "sluchátka", "slúchadlá", "hörlurar", "hovedtelefon",
    "hodetelefon", "kuulokkeet", "fejhallgató", "căști", "kulaklık", "наушники", "навушники",
    "гарнитура", "ακουστικά", "אוזניות", "سماعات الرأس", "หูฟัง", "tai nghe", "ヘッドホン",
    "ヘッドフォン", "ヘッドセット", "イヤホン", "耳机", "耳機", "헤드폰", "헤드셋", "이어폰", "ականջակալ",
)
FORM_FACTOR = "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 0"     # PKEY_AudioEndpoint_FormFactor
_HEADPHONE_FORMS = (3, 5, 6)                                  # headphones, headset, handset
_SPEAKER_FORMS = (1, 9)                                       # speakers, a screen's speakers (HDMI)


# ── conversion ───────────────────────────────────────────────────────────────────────────────
# Sample positions follow the exact rate ratio, so a chunk is never stretched: the sound in and
# out stays in step with the reference the own-voice canceller subtracts.
def to_mono16k(samples, channels, rate):
    """float32 or int16 interleaved samples -> 16 kHz mono int16 numpy array."""
    arr = np.asarray(samples)
    if arr.dtype == np.int16:
        arr = arr.astype(np.float32) / 32768.0
    else:
        arr = arr.astype(np.float32, copy=False)
    if channels > 1 and arr.size:
        usable = arr.size - (arr.size % channels)
        arr = arr[:usable].reshape(-1, channels).mean(axis=1)
    if rate != RATE and arr.size:
        n = int(round(arr.size * RATE / rate))
        if n <= 0:
            return np.zeros(0, np.int16)
        if rate > RATE:                       # crude anti-alias before thinning
            k = max(1, int(rate // RATE))
            if k > 1:
                arr = np.convolve(arr, np.ones(k, np.float32) / k, mode="same")
        pos = np.minimum(np.arange(n) * (rate / RATE), arr.size - 1)
        arr = np.interp(pos, np.arange(arr.size), arr)
    return (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)


def from_mono16k(pcm, channels, rate):
    """16 kHz mono int16 bytes -> interleaved int16 bytes for a device."""
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if rate != RATE and arr.size:
        n = int(round(arr.size * rate / RATE))
        pos = np.minimum(np.arange(n) * (RATE / rate), arr.size - 1)
        arr = np.interp(pos, np.arange(arr.size), arr)
    out = arr.astype(np.int16)
    if channels > 1:
        out = np.repeat(out, channels)
    return out.tobytes()


def rms(pcm):
    a = np.frombuffer(pcm, dtype=np.int16) if isinstance(pcm, (bytes, bytearray)) else np.asarray(pcm)
    a = a.astype(np.float64)                  # int16 squared overflows
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0


# ── devices ──────────────────────────────────────────────────────────────────────────────────
def _pyaudio():
    try:
        import pyaudiowpatch as pa_mod
    except ImportError:  # macOS / Linux dev boxes
        import pyaudio as pa_mod
    return pa_mod


def _name(info):
    n = info.get("name", "")
    return n.decode("utf-8", "ignore") if isinstance(n, bytes) else str(n)


def _wasapi_index(pa):
    try:
        return pa.get_host_api_info_by_type(_pyaudio().paWASAPI)["index"]
    except Exception:  # noqa: BLE001
        return None


def list_devices(pa, output=True):
    """[(index, name, info)] of WASAPI devices (outputs or inputs), loopback twins left out."""
    host = _wasapi_index(pa)
    out = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if host is not None and info.get("hostApi") != host:
            continue
        if info.get("isLoopbackDevice"):
            continue
        chans = info.get("maxOutputChannels" if output else "maxInputChannels", 0)
        if chans > 0:
            out.append((i, _name(info), info))
    return out


def is_virtual_mic(name, output):
    """The virtual microphone's output (what Speak writes into) or its input (what the call app
    picks as its microphone)."""
    low = (name or "").lower()
    return any(w in low for w in (VIRTUAL_MIC_OUTPUTS if output else VIRTUAL_MIC_INPUTS))


def find_virtual_mic_output(pa):
    """The output we write the translated voice into (the Gusan Speaker, or VB-CABLE's
    "CABLE Input"); None when no virtual microphone is installed."""
    for i, name, _info in list_devices(pa, output=True):
        if is_virtual_mic(name, output=True):
            return i
    return None


def pick_device(pa, output, wanted=None, name=""):
    """The device to open (an index), never the virtual microphone - Speak would hear its own
    voice, Hear would play into the call: ``wanted`` (a device picked in Gusan) when it is there,
    else the WASAPI device called ``name`` (the one Windows uses now), else PortAudio's WASAPI
    default, else the first real one. None when there is none."""
    key = "maxOutputChannels" if output else "maxInputChannels"

    def usable(i):
        try:
            info = pa.get_device_info_by_index(i)
        except Exception:  # noqa: BLE001
            return False
        return (int(info.get(key) or 0) > 0 and not info.get("isLoopbackDevice")
                and not is_virtual_mic(_name(info), output))

    if isinstance(wanted, int) and not isinstance(wanted, bool) and usable(wanted):
        return wanted
    try:
        real = [(i, n) for i, n, _info in list_devices(pa, output=output) if not is_virtual_mic(n, output)]
    except Exception:  # noqa: BLE001
        real = []
    for i, n in real:
        if name and n == name:
            return i
    try:
        i = int(pa.get_default_wasapi_device(d_out=output)["index"])
        if usable(i):
            return i
    except Exception:  # noqa: BLE001 - no WASAPI (a dev box)
        pass
    return real[0][0] if real else None


def loopback_of(pa, out_index):
    """The loopback twin of an output (records what it plays); None when there is none."""
    if out_index is None:
        return None
    try:
        return int(pa.get_wasapi_loopback_analogue_by_index(out_index)["index"])
    except Exception:  # noqa: BLE001
        return None


def default_output_index(pa):
    """Windows' default output on WASAPI (low latency, no extra resampling), else PortAudio's."""
    try:
        return int(pa.get_default_wasapi_device(d_out=True)["index"])
    except Exception:  # noqa: BLE001
        return int(pa.get_default_output_device_info()["index"])


def _com_ready():
    """COM on this thread: pycaw needs it, and a worker thread has none of its own."""
    try:
        import comtypes
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except Exception:  # noqa: BLE001 - already set up on this thread (either way), or no comtypes
        pass


def windows_default(output=True):
    """(name, form factor) of the output (or microphone) Windows uses right now, read live: the
    list PortAudio holds is from when it started. ("", None) when it can't be read."""
    try:
        _com_ready()
        from pycaw.pycaw import AudioUtilities
        dev = AudioUtilities.GetSpeakers() if output else AudioUtilities.GetMicrophone()
        if not hasattr(dev, "FriendlyName"):
            dev = AudioUtilities.CreateDevice(dev)
        form = (getattr(dev, "properties", None) or {}).get(FORM_FACTOR)
        return str(dev.FriendlyName or ""), (int(form) if form is not None else None)
    except Exception:  # noqa: BLE001
        return "", None


def guess_output_kind(name, form_factor=None):
    """"headphones" or "speakers": Windows' own form factor when known (any language, any
    product name), else the device name."""
    if form_factor in _HEADPHONE_FORMS:
        return "headphones"
    if form_factor in _SPEAKER_FORMS:
        return "speakers"
    low = (name or "").casefold()
    return "headphones" if any(w in low for w in _HEADPHONE_WORDS) else "speakers"


# ── capture ──────────────────────────────────────────────────────────────────────────────────
class CaptureSource:
    """Polls a mic or a loopback stream and hands 16 kHz mono int16 bytes to on_pcm.

    loopback=True captures what an output plays (the call). fill_silence keeps the timeline going
    while the device delivers nothing. ``lost`` turns True when reads keep failing (the device is
    gone: unplugged, out of range)."""

    LOST_S = 1.0                  # reads failing this long: the device is gone
    MAX_FILL_S = 0.5              # one silence fill at most (after sleep or a stall the gap is skipped)

    def __init__(self, on_pcm, *, loopback=False, device_index=None, pa=None, fill_silence=None,
                 poll_s=0.02):
        self.on_pcm = on_pcm
        self.loopback = loopback
        self.device_index = device_index
        self._pa = pa
        self._own_pa = pa is None
        self.fill_silence = loopback if fill_silence is None else fill_silence
        self.poll_s = poll_s
        self._stream = None
        self._thread = None
        self._stop = threading.Event()
        self.channels = 1
        self.rate = RATE
        self.device_name = ""
        self.lost = False

    def start(self):
        mod = _pyaudio()
        if self._pa is None:
            self._pa = mod.PyAudio()
        idx = self.device_index
        if idx is None:
            if self.loopback:
                idx = int(self._pa.get_default_wasapi_loopback()["index"])
            else:
                idx = int(self._pa.get_default_wasapi_device(d_out=False)["index"]) if hasattr(
                    self._pa, "get_default_wasapi_device") else self._pa.get_default_input_device_info()["index"]
        info = self._pa.get_device_info_by_index(idx)
        self.device_index = idx
        self.device_name = _name(info)
        self.rate = int(info.get("defaultSampleRate") or 48000)
        own = max(1, int(info.get("maxInputChannels") or 1))
        last = None
        for channels in dict.fromkeys((own, min(2, own))):   # its own format first (WASAPI takes no other)
            try:
                self._stream = self._pa.open(format=mod.paFloat32, channels=channels, rate=self.rate,
                                             input=True, input_device_index=idx,
                                             frames_per_buffer=int(self.rate * 0.02))
                self.channels = channels
                break
            except Exception as e:  # noqa: BLE001
                last = e
        else:
            raise last
        self._stop.clear()
        self.lost = False
        self._thread = threading.Thread(target=self._pump, name="interp-capture", daemon=True)
        self._thread.start()
        logger.info("interpreter capture: %s (%s, %d Hz, %d ch)", self.device_name,
                    "loopback" if self.loopback else "mic", self.rate, self.channels)

    def _pump(self):
        produced = 0                          # 16 kHz samples handed out
        t0 = time.monotonic()
        failing_since = None
        max_fill = int(self.MAX_FILL_S * RATE)
        while not self._stop.is_set():
            pcm = None
            try:
                avail = self._stream.get_read_available()
                if avail > 0:
                    raw = self._stream.read(avail, exception_on_overflow=False)
                    pcm = to_mono16k(np.frombuffer(raw, dtype=np.float32), self.channels, self.rate)
                failing_since = None
            except Exception:  # noqa: BLE001
                logger.debug("interpreter capture read failed", exc_info=True)
                now = time.monotonic()
                failing_since = failing_since or now
                if not self.lost and now - failing_since >= self.LOST_S:
                    self.lost = True
                    logger.warning("interpreter capture lost: %s", self.device_name)
            try:
                if pcm is not None and pcm.size:
                    produced += pcm.size
                    self.on_pcm(pcm.tobytes())
                elif self.fill_silence:
                    behind = int((time.monotonic() - t0) * RATE) - produced
                    if behind > max_fill:              # asleep or stalled: skip the gap, keep a pause
                        produced += behind - max_fill
                        behind = max_fill
                    if behind > int(0.1 * RATE):       # the device went quiet: keep time moving
                        produced += behind
                        self.on_pcm(bytes(behind * 2))
            except Exception:  # noqa: BLE001 - a bad chunk must not stop the capture
                logger.debug("interpreter capture handler failed", exc_info=True)
            time.sleep(self.poll_s)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(1.0)              # no read in flight when the stream closes
        s, self._stream = self._stream, None
        if s is not None:
            for f in (s.stop_stream, s.close):
                try:
                    f()
                except Exception:  # noqa: BLE001
                    pass
        if self._own_pa and self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._pa = None


# ── playback ─────────────────────────────────────────────────────────────────────────────────
class PlaybackSink:
    """Plays 16 kHz mono int16 on a device (headphones/speakers, or the virtual mic's output).
    queued_seconds() tells the pipeline how far behind the voice is; clear() stops at once.
    on_played(pcm16k) (optional) sees everything handed to the device, silence included - the
    reference the echo removal needs, in step with the device. stalled() is True when the device
    stopped asking for sound (unplugged, out of range): nothing plays any more."""

    STALL_S = 1.0                 # no callback this long while open: the device is gone

    def __init__(self, device_index=None, pa=None, gain=1.0, on_played=None):
        self.device_index = device_index
        self._pa = pa
        self._own_pa = pa is None
        self.gain = gain
        self.on_played = on_played
        self._buf = bytearray()                 # 16 kHz mono int16 waiting to play
        self._lock = threading.Lock()
        self._stream = None
        self.channels = 2
        self.rate = 48000
        self.device_name = ""
        self.last_played = 0.0
        self.last_callback = 0.0
        self._opened_at = 0.0

    def start(self):
        mod = _pyaudio()
        if self._pa is None:
            self._pa = mod.PyAudio()
        idx = self.device_index
        if idx is None:
            idx = default_output_index(self._pa)
        info = self._pa.get_device_info_by_index(idx)
        self.device_index = idx
        self.device_name = _name(info)
        self.rate = int(info.get("defaultSampleRate") or 48000)
        own = max(1, int(info.get("maxOutputChannels") or 2))
        last = None
        for channels in dict.fromkeys((own, min(2, own))):   # its own format first (WASAPI takes no other)
            try:
                self.channels = channels
                self._stream = self._pa.open(format=mod.paInt16, channels=channels, rate=self.rate,
                                             output=True, output_device_index=idx,
                                             frames_per_buffer=int(self.rate * 0.02),
                                             stream_callback=self._callback)
                break
            except Exception as e:  # noqa: BLE001
                last = e
        else:
            raise last
        self._opened_at = time.monotonic()
        self._stream.start_stream()

    def _callback(self, _in, frames, _time, _status):
        need = int(round(frames * RATE / self.rate)) * 2      # bytes of 16 kHz mono
        with self._lock:
            chunk = bytes(self._buf[:need])
            del self._buf[:need]
        now = time.monotonic()
        self.last_callback = now
        if chunk:
            self.last_played = now
        if len(chunk) < need:
            chunk += bytes(need - len(chunk))
        if self.on_played:
            try:
                self.on_played(chunk)
            except Exception:  # noqa: BLE001
                pass
        out = from_mono16k(chunk, self.channels, self.rate)
        want = frames * self.channels * 2
        out = (out + bytes(want))[:want] if len(out) < want else out[:want]
        mod = _pyaudio()
        return out, mod.paContinue

    def play(self, pcm):
        if self.gain != 1.0:
            a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * self.gain
            pcm = np.clip(a, -32768, 32767).astype(np.int16).tobytes()
        with self._lock:
            self._buf.extend(pcm)

    def queued_seconds(self):
        with self._lock:
            return len(self._buf) / 2 / RATE

    def stalled(self):
        if self._stream is None or not self._opened_at:
            return False
        return time.monotonic() - max(self.last_callback, self._opened_at) > self.STALL_S

    def is_playing(self, tail_s=0.3):
        if self.stalled():
            self.clear()                        # nothing reaches anyone: no ducking, no muting
            return False
        return self.queued_seconds() > 0 or (time.monotonic() - self.last_played) < tail_s

    def clear(self):
        with self._lock:
            self._buf.clear()

    def stop(self):
        s, self._stream = self._stream, None
        if s is not None:
            for f in (s.stop_stream, s.close):
                try:
                    f()
                except Exception:  # noqa: BLE001
                    pass
        if self._own_pa and self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._pa = None


# ── keeping Gusan's own voice out ────────────────────────────────────────────────────────────
class OwnVoiceCanceller:
    """Removes what Gusan itself played from the loopback capture (hear direction).

    The loopback signal is (call sound, turned down) + (our voice, as the sound card played it).
    reference(pcm) gets everything handed to the device, silence included (PlaybackSink.on_played),
    and process(pcm) the capture in order. Both count their samples and both move with the same
    sound card, so a capture sample is matched with the reference sample it came from - however
    the callback and the capture thread happen to be timed. The offset between the two counts,
    and a short filter for what the sound card did to the voice, are found by cross-correlation
    while we play (on the last TUNE_S of capture), checked again every RETUNE_S, and at once when
    the subtraction suddenly stops working (the device dropped some sound).

    After process(): echo_rms is how loud our voice was in that chunk, left_rms what was left;
    their_voice() says whether more than our leftovers is in there."""

    TAPS = 5                      # the sound card's filter, around the matched sample
    TUNE_S = 0.5
    RETUNE_S = 0.5
    UNLOCKED_TRY_S = 0.1
    THEIRS_RATIO = 0.2            # our leftovers are ~30 dB down; their voice is far above that
    THEIRS_MIN_RMS = 120.0

    def __init__(self, max_delay_s=0.5, history_s=3.0):
        self.max_delay = int(max_delay_s * RATE)
        self._hist = int(history_s * RATE)
        self._ref = np.zeros(2 * self._hist, np.float32)    # the newest reference, oldest first
        self._ref_len = 0
        self._written = 0            # reference samples ever written (position after the newest)
        self._cap = np.zeros(int(self.TUNE_S * RATE), np.float32)   # the newest capture
        self._cap_held = 0
        self._cap_pos = 0            # capture samples ever processed
        self._since_tune = 0
        self._odd = 0                # capture samples in a row where the subtraction failed
        self.offset = None           # reference position = capture position - offset
        self.taps = None
        self.delay = None            # how far behind the newest reference the capture was, last tune
        self.gain = 1.0
        self.echo_rms = 0.0
        self.left_rms = 0.0
        self._lock = threading.Lock()

    @property
    def locked(self):
        return self.offset is not None

    def reference(self, pcm):
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        with self._lock:
            self._written += len(a)
            a = a[-self._hist:]
            if self._ref_len + len(a) > len(self._ref):          # make room, keep the newest
                keep = self._hist - len(a)
                self._ref[:keep] = self._ref[self._ref_len - keep:self._ref_len]
                self._ref_len = keep
            self._ref[self._ref_len:self._ref_len + len(a)] = a
            self._ref_len += len(a)

    def _window(self, lo, hi):
        """The reference at positions lo..hi (zeros where none is held)."""
        out = np.zeros(max(0, hi - lo), np.float32)
        first = self._written - self._ref_len
        a, b = max(lo, first), min(hi, self._written)
        if a < b:
            out[a - lo:b - lo] = self._ref[a - first:b - first]
        return out

    def _filtered(self, seg, n):
        """Our voice as the capture holds it: the taps over a reference stretch padded by TAPS-1."""
        out = np.zeros(n, np.float32)
        for k in range(self.TAPS):
            out += self.taps[k] * seg[self.TAPS - 1 - k:self.TAPS - 1 - k + n]
        return out

    def process(self, pcm):
        cap = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        n = len(cap)
        h = self.TAPS // 2
        with self._lock:
            c0 = self._cap_pos
            self._cap_pos += n
            keep = cap[-len(self._cap):]
            if len(keep):
                self._cap = np.roll(self._cap, -len(keep))
                self._cap[-len(keep):] = keep
            self._cap_held = min(len(self._cap), self._cap_held + n)
            if self.offset is not None:
                lo, hi = c0 - self.offset - h, c0 + n - self.offset + h
            else:
                lo, hi = self._written - self.max_delay - len(self._cap) - n, self._written
            seg = self._window(lo, hi)
            if not n or not seg.size or float(np.abs(seg).max()) < 1.0:   # we played nothing here
                self.echo_rms, self.left_rms = 0.0, rms(cap)
                return pcm
            self._since_tune += n
            every = self.RETUNE_S if self.offset is not None else self.UNLOCKED_TRY_S
            if self._since_tune >= int(every * RATE):
                self._since_tune = 0
                self._tune(self.TUNE_S)
            if self.offset is None:
                self.echo_rms = self.left_rms = rms(cap)
                return pcm
            echo = self._filtered(self._window(c0 - self.offset - h, c0 + n - self.offset + h), n)
            out = cap - echo
            self.echo_rms, self.left_rms = rms(echo), rms(out)
            # the subtraction stopped working (the device dropped or repeated some sound): look
            # again on just that stretch; their voice alone never moves the match
            if self.echo_rms > 300.0 and self.left_rms > 0.5 * self.echo_rms:
                self._odd += n
                if self._odd >= int(0.12 * RATE):
                    self._odd = 0
                    self._tune(0.12)
            else:
                self._odd = 0
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()

    def their_voice(self):
        """After process(): is there more in that chunk than what is left of our own voice (the
        other person talking over the translation)? Never before our voice has been found."""
        return self.offset is not None and self.left_rms >= max(self.THEIRS_MIN_RMS,
                                                                self.THEIRS_RATIO * self.echo_rms)

    def _tune(self, window_s):
        """Find our voice in the last capture: the offset to the reference and the taps. Nothing
        changes when our voice isn't clearly in there (e.g. they talk louder)."""
        m = min(self._cap_held, int(window_s * RATE))
        if m < 320:
            return False
        x = self._cap[len(self._cap) - m:]
        ex = float(np.dot(x, x))
        if ex < m * 100.0:                               # too quiet to tell
            return False
        lo = self._written - self.max_delay - m
        y = self._window(lo, self._written)
        nfft = 1 << int(np.ceil(np.log2(len(y) + m)))
        corr = np.fft.irfft(np.fft.rfft(y, nfft) * np.conj(np.fft.rfft(x, nfft)), nfft)[:self.max_delay + 1]
        e2 = np.concatenate(([0.0], np.cumsum(y.astype(np.float64) ** 2)))
        ey = e2[m:m + self.max_delay + 1] - e2[:self.max_delay + 1]
        score = np.where(ey > m, corr * corr / np.maximum(ey, 1.0), 0.0)
        j = int(np.argmax(score))
        if score[j] < 0.5 * ex:                          # the reference doesn't explain it
            return False
        offset = (self._cap_pos - m) - (lo + j)
        h = self.TAPS // 2
        c0 = self._cap_pos - m
        seg = self._window(c0 - offset - h, c0 + m - offset + h).astype(np.float64)
        a = np.stack([seg[self.TAPS - 1 - k:self.TAPS - 1 - k + m] for k in range(self.TAPS)], axis=1)
        ata = a.T @ a
        tr = float(np.trace(ata))
        if tr <= 1e-6:
            return False
        # least squares, pulled towards the plain delay + gain: what the window can't tell apart
        # (sounds it doesn't contain) stays that simple
        w0 = np.zeros(self.TAPS)
        w0[h] = corr[j] / ey[j]
        lam = 3e-4 * tr / self.TAPS
        w = np.linalg.solve(ata + np.eye(self.TAPS) * lam, a.T @ x.astype(np.float64) + lam * w0)
        self.offset, self.taps = offset, np.clip(w, -2.0, 2.0).astype(np.float32)
        self.delay = self.max_delay - j
        self.gain = float(self.taps[h])
        return True


class ListenGate:
    """Mutes a listening pipeline while Gusan's voice plays (is_playing()), unless the person is
    clearly talking over it: barge_in(pcm) when given, else louder than barge_in_rms. hold_s keeps
    it open a moment after that, so words aren't chopped into pieces. ``enabled`` may change while
    running (speakers mode on or off)."""

    def __init__(self, is_playing, barge_in_rms=2500.0, enabled=True, barge_in=None, hold_s=0.0):
        self.is_playing = is_playing
        self.barge_in_rms = barge_in_rms
        self.barge_in = barge_in
        self.enabled = enabled
        self.hold_s = hold_s
        self._open_until = 0.0

    def process(self, pcm):
        if not self.enabled or not self.is_playing():
            return pcm
        now = time.monotonic()
        if self.barge_in is not None:
            talking = bool(self.barge_in(pcm))
        else:
            talking = bool(self.barge_in_rms) and rms(pcm) >= self.barge_in_rms
        if talking:
            self._open_until = now + self.hold_s
            return pcm
        if now < self._open_until:
            return pcm
        return bytes(len(pcm))


class SoundLevel:
    """How loud a source was lately - the speakers, read from their loopback. In speakers mode the
    mic hears whatever they play (the call too), so the speak gate mutes while they are loud."""

    def __init__(self, floor=400.0, tail_s=0.3):
        self.floor = floor
        self.tail_s = tail_s
        self._loud_at = 0.0
        self._level = 0.0
        self._level_at = 0.0

    def feed(self, pcm):
        r = rms(pcm)
        now = time.monotonic()
        if r >= self.floor:
            self._loud_at = now
        if r >= self._level or now - self._level_at > self.tail_s:
            self._level, self._level_at = r, now

    def active(self):
        return time.monotonic() - self._loud_at < self.tail_s

    def level(self):
        """The loudest chunk within the tail (0 when it's been quiet)."""
        return self._level if time.monotonic() - self._level_at <= self.tail_s else 0.0


# ── turning the call down ────────────────────────────────────────────────────────────────────
class Ducker:
    """Lowers the call apps' own volume (Windows per-app volume) while a translation plays and
    puts it back afterwards. Needs pycaw; without it, it quietly does nothing. close() puts it back
    for good: a duck() that comes after it does nothing."""

    def __init__(self, level=0.25, apps=CALL_APPS):
        self.level = level
        self.apps = {a.lower() for a in apps}
        self._saved = {}
        self._lock = threading.Lock()
        self._closed = False
        self.available = True

    def _sessions(self):
        try:
            _com_ready()
            from pycaw.pycaw import AudioUtilities
        except Exception:  # noqa: BLE001
            self.available = False
            return []
        out = []
        for s in AudioUtilities.GetAllSessions():
            proc = getattr(s, "Process", None)
            name = (proc.name() if proc else "").lower()
            if name in self.apps:
                out.append((s.ProcessId, s.SimpleAudioVolume))
        return out

    def duck(self):
        with self._lock:
            if self._saved or self._closed:
                return
            try:
                sessions = self._sessions()
            except Exception:  # noqa: BLE001 - never stop the interpreter over the volume
                logger.debug("duck failed", exc_info=True)
                return
            for pid, vol in sessions:
                try:
                    self._saved[pid] = (vol, vol.GetMasterVolume())
                    vol.SetMasterVolume(self._saved[pid][1] * self.level, None)
                except Exception:  # noqa: BLE001
                    logger.debug("duck failed", exc_info=True)

    def restore(self):
        with self._lock:
            for _pid, (vol, level) in self._saved.items():
                try:
                    vol.SetMasterVolume(level, None)
                except Exception:  # noqa: BLE001
                    pass
            self._saved = {}

    def close(self):
        with self._lock:
            self._closed = True
        self.restore()
