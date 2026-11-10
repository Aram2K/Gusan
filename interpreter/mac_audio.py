"""macOS sound for the interpreter: the same methods as controller.WindowsAudio.

* Hear: the call's sound comes from a Core Audio process tap (macOS 14.2+), run by the small Swift
  helper ``gusan-tap`` (interpreter/mac/gusan_tap.swift) that writes 16 kHz mono 16-bit PCM to its
  stdout. The tap leaves Gusan's own process out, so its translated voice never comes back: no own
  voice to cancel (``excludes_own_voice``).
* Speak: BlackHole 2ch is the virtual microphone (audio_io's name lists); the call app picks
  "BlackHole 2ch" as its microphone. BlackHole is GPL: never bundled, the person installs it.
* The mic and the playback are plain PortAudio (Core Audio), as for meetings.
* Turning the call down: macOS has no per-app volume, so the ducker does nothing.
"""
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time

from interpreter import audio_io
from interpreter.session import InterpreterError

logger = logging.getLogger(__name__)

HELPER = "gusan-tap"
SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mac", "gusan_tap.swift")
MIN_MACOS = (14, 2)
CHUNK = 640                       # bytes: 20 ms of 16 kHz mono int16
# the helper's exit codes (gusan_tap.swift)
EXIT_CODES = {2: "mac_too_old", 3: "no_call_sound", 4: "device_lost", 5: "no_call_sound"}
MESSAGES = {
    "no_call_sound": "Gusan can't hear the call. Allow System Audio Recording in Settings.",
    "mac_too_old": "Hearing the call needs macOS 14.2 or later.",
    "device_lost": "A sound device stopped working. Check your headphones, speakers or microphone.",
}


def error(code):
    return InterpreterError(code, MESSAGES.get(code, MESSAGES["no_call_sound"]))


def macos_version(release=None):
    try:
        parts = (release if release is not None else platform.mac_ver()[0]).split(".")
        return tuple(int(p) for p in parts[:2]) + (0,) * (2 - len(parts[:2]))
    except (ValueError, AttributeError):
        return (0, 0)


def _built_dir():
    import storage
    return str(storage.path_for("bin"))


def helper_path(build=True):
    """The gusan-tap executable: next to the packaged app, else built from source once (needs
    Xcode's command line tools). None when there is none."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = os.path.join(base, HELPER)
        return p if os.access(p, os.X_OK) else None
    try:
        out = os.path.join(_built_dir(), HELPER)
    except Exception:  # noqa: BLE001
        return None
    fresh = os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(SOURCE)
    if fresh or not build:
        return out if os.access(out, os.X_OK) else None
    swiftc = shutil.which("swiftc")
    if not swiftc:
        logger.warning("interpreter: no swiftc, so the call's sound can't be captured from source")
        return None
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        subprocess.run([swiftc, "-O", "-o", out, SOURCE], check=True, capture_output=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        logger.warning("interpreter: gusan-tap could not be built: %s", e)
        return None
    return out


# The call apps, by bundle-id prefix, in the order they win: when one of them has sound open, Hear
# taps only it (no music or notifications); none = everything the Mac plays except Gusan. Browsers
# come second: Meet runs in them, but so does everything else.
CALL_APPS = (
    ("us.zoom.", "com.microsoft.teams"),
    ("com.google.chrome", "com.apple.safari", "com.apple.webkit.", "company.thebrowser."),
)


def permission_state(helper, run=subprocess.run):
    """granted / denied / unknown (never asked yet: the first start shows macOS's prompt)."""
    try:
        return run([helper, "--status"], capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def capture_status(helper=None, release=None, run=subprocess.run):
    """None when the call's sound can be captured, else the code that says why not:
    mac_too_old, no_call_sound (no helper, or the permission was refused)."""
    if macos_version(release) < MIN_MACOS:
        return "mac_too_old"
    helper = helper or helper_path()
    if not helper:
        return "no_call_sound"
    try:
        state = run([helper, "--status"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        return "no_call_sound"
    return "no_call_sound" if state == "denied" else None


class TapCapture:
    """The call's sound (everything the Mac plays but Gusan) as 16 kHz mono int16 bytes to on_pcm,
    20 ms at a time, in step with the output device (silence included). ``lost`` turns True when
    the helper ends by itself (the output device went away)."""

    START_S = 2.0                 # the helper says no within this, or it runs
    PROMPT_S = 60.0               # first start: macOS's System Audio Recording prompt waits for a click
    FILL_S = 0.02

    def __init__(self, on_pcm, helper, exclude_pids=(), popen=subprocess.Popen, asking=False,
                 call_apps=()):
        self.on_pcm = on_pcm
        self.call_apps = tuple(call_apps)
        self.start_s = self.PROMPT_S if asking else self.START_S
        self.helper = helper
        self.exclude_pids = tuple(exclude_pids)
        self._popen = popen
        self._proc = None
        self._thread = None
        self._stopping = False
        self.device_name = "the Mac's sound"
        self.lost = False
        self.first_at = None
        self._last = 0.0
        self._hand_lock = threading.Lock()

    def start(self):
        args = [self.helper]
        for pid in self.exclude_pids:
            args += ["--exclude-pid", str(pid)]
        for group in self.call_apps:
            args += ["--call-apps", ",".join(group)]
        self._stopping = False
        self.lost = False
        self._proc = proc = self._popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0)
        said = self._first_line(proc)              # "gusan-tap: started ..." or why not
        if "started" not in said:
            if proc.poll() is None:                # no word within START_S: must not freeze the start
                self._end(proc)
            code = proc.poll()
            logger.warning("interpreter: the call's sound can't be captured (%s) %s", code, said.strip())
            self._proc = None
            raise error(EXIT_CODES.get(code, "no_call_sound"))
        self._last = self.first_at = time.monotonic()
        self._thread = threading.Thread(target=self._pump, args=(proc,), name="interp-tap", daemon=True)
        self._thread.start()
        self._filler = threading.Thread(target=self._fill, name="interp-tap-fill", daemon=True)
        self._filler.start()
        self.only = said.split(" only ", 1)[1].strip().split(",") if " only " in said else []
        logger.info("interpreter capture: %s", f"only {', '.join(self.only)} (process tap)" if self.only
                    else "the Mac's sound (process tap, Gusan left out)")

    def _first_line(self, proc):
        box = []
        t = threading.Thread(target=lambda: box.append(proc.stderr.readline()), daemon=True)
        t.start()
        t.join(self.start_s)
        return (box[0] or b"").decode("utf-8", "ignore") if box else ""

    @staticmethod
    def _end(proc):
        for step in (lambda: proc.stdin.close(), proc.terminate):
            try:
                step()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(1.0)
        except Exception:  # noqa: BLE001
            proc.kill()

    def _hand(self, chunk):
        with self._hand_lock:
            self._last = time.monotonic()
            try:
                self.on_pcm(chunk)
            except Exception:  # noqa: BLE001 - a bad chunk must not stop the capture
                logger.debug("interpreter capture handler failed", exc_info=True)

    def _pump(self, proc):
        while True:
            try:
                chunk = proc.stdout.read(CHUNK)
            except Exception:  # noqa: BLE001
                chunk = b""
            if not chunk:
                break
            self._hand(chunk)
        if not self._stopping:
            self.lost = True
            logger.warning("interpreter capture lost: the process tap ended (%s)", proc.poll())

    def _fill(self):
        """macOS runs a tap only while something plays: silence keeps the timeline going meanwhile."""
        while not self._stopping and not self.lost:
            time.sleep(self.FILL_S)
            gap = time.monotonic() - self._last
            if gap >= 5 * self.FILL_S:
                self._hand(bytes(int(gap / 0.02) * CHUNK))

    def stop(self):
        self._stopping = True
        p, self._proc = self._proc, None
        if p is None:
            return
        self._end(p)
        if self._thread:
            self._thread.join(1.0)


class NullDucker:
    """macOS has no per-app volume: the call stays as loud as it is."""
    available = False

    def __init__(self, level=0.25):
        self.level = level

    def duck(self):
        pass

    def restore(self):
        pass

    def close(self):
        pass


class MacAudio:
    """The real devices on a Mac, looked up when they are opened. Tests pass a fake."""

    excludes_own_voice = True

    def __init__(self, helper=None):
        self._pa = None
        self._helper = helper
        self._lock = threading.RLock()
        self._closed = False

    def _ensure(self):
        if self._closed:
            raise RuntimeError("the interpreter's sound is closed")
        if self._pa is None:
            self._pa = audio_io._pyaudio().PyAudio()
        return self._pa

    def _default_name(self, output):
        pa = self._ensure()
        try:
            info = pa.get_default_output_device_info() if output else pa.get_default_input_device_info()
            return audio_io._name(info)
        except Exception:  # noqa: BLE001
            return ""

    def call_sound_problem(self):
        """None when Hear can capture the call, else the error code (said before anything starts)."""
        return capture_status(self._helper_path())

    def _helper_path(self):
        if self._helper is None:
            self._helper = helper_path() or ""
        return self._helper or None

    def output_info(self):
        with self._lock:
            pa = self._ensure()
            live = self._default_name(output=True)
            idx = audio_io.pick_device(pa, output=True, name=live)
            name = ""
            if idx is not None:
                try:
                    name = audio_io._name(pa.get_device_info_by_index(idx))
                except Exception:  # noqa: BLE001
                    name = ""
        return {"index": idx, "name": name or live, "kind": audio_io.guess_output_kind(name or live)}

    def default_output_name(self):
        try:
            return self.output_info()["name"]
        except Exception:  # noqa: BLE001
            return ""

    def mic_index(self, wanted=None):
        with self._lock:
            pa = self._ensure()
            return audio_io.pick_device(pa, output=False, wanted=wanted, name=self._default_name(output=False))

    def capture(self, on_pcm, loopback, device=None):
        with self._lock:
            if loopback:
                helper = self._helper_path()
                if not helper:
                    raise error("no_call_sound")
                return TapCapture(on_pcm, helper, exclude_pids=(os.getpid(),),
                                  asking=permission_state(helper) == "unknown", call_apps=CALL_APPS)
            pa = self._ensure()
            idx = device if device is not None else self.mic_index()
            if idx is None:
                raise InterpreterError("no_mic_sound", "No microphone.")
            return audio_io.CaptureSource(on_pcm, loopback=False, device_index=idx, pa=pa)

    def playback(self, device_index=None, on_played=None):
        with self._lock:
            pa = self._ensure()
            if device_index is None:
                device_index = self.output_info()["index"]
            return audio_io.PlaybackSink(device_index=device_index, pa=pa, on_played=on_played)

    def virtual_mic_output(self):
        with self._lock:
            return audio_io.find_virtual_mic_output(self._ensure())

    def ducker(self, level):
        return NullDucker(level)

    def close(self):
        with self._lock:
            self._closed = True
            pa, self._pa = self._pa, None
            if pa is None:
                return
            try:
                pa.terminate()
            except Exception:  # noqa: BLE001
                pass
