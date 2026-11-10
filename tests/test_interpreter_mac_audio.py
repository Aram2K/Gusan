"""The interpreter on macOS: the process-tap helper, MacAudio, and calm failures."""
import io
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest

from interpreter import audio_io, controller, mac_audio
from interpreter.session import InterpreterError
from tests.test_interpreter_controller import FakeAudio, make as make_interpreter


class FakeProc:
    def __init__(self, out=b"", code=None, err=b"gusan-tap: started: 48000.0 Hz, 2 ch\n"):
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)
        self.stdin = mock.Mock()
        self.returncode = code
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, _t=None):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def tap_with(proc):
    got = []
    tap = mac_audio.TapCapture(got.append, "/x/gusan-tap", exclude_pids=(42,), popen=lambda args, **kw: proc)
    return tap, got


def test_tap_hands_out_20ms_chunks_and_leaves_gusan_out():
    seen = {}
    proc = FakeProc(out=bytes(640 * 3))

    def popen(args, **kw):
        seen["args"] = args
        return proc
    got = []
    tap = mac_audio.TapCapture(got.append, "/x/gusan-tap", exclude_pids=(42,), popen=popen)
    tap.start()
    tap._thread.join(1)
    assert seen["args"] == ["/x/gusan-tap", "--exclude-pid", "42"]
    assert [len(c) for c in got] == [640, 640, 640]
    assert tap.lost                      # the helper ended by itself
    tap.stop()
    assert proc.terminated


class IdleProc(FakeProc):
    """Started, but nothing plays: macOS runs no tap callback, so no bytes come."""

    def __init__(self):
        super().__init__()
        self._gate = threading.Event()
        self.stdout = mock.Mock()
        self.stdout.read = lambda _n: self._gate.wait(5) and b""

    def terminate(self):
        self.terminated = True
        self._gate.set()


def test_silence_keeps_the_timeline_going_while_nothing_plays():
    proc = IdleProc()
    tap, got = tap_with(proc)
    tap.start()
    time.sleep(0.5)
    tap.stop()
    total = sum(len(c) for c in got)
    assert not any(any(c) for c in got)
    assert 0.3 * 32000 <= total <= 0.6 * 32000          # ~0.5 s of 16 kHz int16
    assert all(len(c) % 640 == 0 for c in got)


@pytest.mark.parametrize("code,err", [(3, "no_call_sound"), (2, "mac_too_old"), (9, "no_call_sound")])
def test_a_refused_tap_raises_a_calm_code(code, err):
    tap, _got = tap_with(FakeProc(code=code, err=b"gusan-tap: the tap was refused\n"))
    with pytest.raises(InterpreterError) as e:
        tap.start()
    assert e.value.code == err
    assert str(e.value) == mac_audio.MESSAGES[err]


class MuteProc(FakeProc):
    """A helper that runs but says nothing at all (a stuck device)."""

    def __init__(self):
        super().__init__()
        self._gate = threading.Event()
        self.stderr = mock.Mock()
        self.stderr.readline = lambda: self._gate.wait(5) and b""

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True
        self._gate.set()


def test_a_mute_helper_never_freezes_the_start():
    proc = MuteProc()
    tap, _got = tap_with(proc)
    tap.start_s = 0.2
    t0 = time.monotonic()
    with pytest.raises(InterpreterError) as e:
        tap.start()
    assert time.monotonic() - t0 < 2 and proc.terminated
    assert e.value.code == "no_call_sound"


def test_the_first_start_waits_for_the_permission_prompt():
    asked = mac_audio.TapCapture(None, "/x/t", asking=True)
    assert asked.start_s == mac_audio.TapCapture.PROMPT_S >= 30   # a person reads and clicks Allow
    assert mac_audio.TapCapture(None, "/x/t").start_s == mac_audio.TapCapture.START_S


@pytest.mark.parametrize("said,asking", [("unknown\n", True), ("granted\n", False), ("denied\n", False)])
def test_mac_capture_asks_only_when_macos_has_not_asked_yet(said, asking):
    with mock.patch.object(mac_audio, "permission_state", return_value=said.strip()), \
            mock.patch.object(mac_audio, "helper_path", return_value="/x/gusan-tap"):
        tap = mac_audio.MacAudio().capture(lambda b: None, loopback=True)
    assert isinstance(tap, mac_audio.TapCapture) and (tap.start_s == tap.PROMPT_S) is asking


def test_permission_state_unknown_when_the_helper_cannot_say():
    def boom(*a, **k):
        raise OSError("gone")
    assert mac_audio.permission_state("/x/t", run=boom) == "unknown"


def test_capture_status():
    ok = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="granted\n")
    denied = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="denied\n")
    assert mac_audio.capture_status("/x/t", release="13.6", run=ok) == "mac_too_old"
    assert mac_audio.capture_status("/x/t", release="14.2", run=ok) is None
    assert mac_audio.capture_status("/x/t", release="26.6.2", run=denied) == "no_call_sound"
    with mock.patch.object(mac_audio, "helper_path", return_value=None):
        assert mac_audio.capture_status(release="15.0", run=ok) == "no_call_sound"


def test_macos_version():
    assert mac_audio.macos_version("14.2.1") == (14, 2)
    assert mac_audio.macos_version("15") == (15, 0)
    assert mac_audio.macos_version("") == (0, 0)


def test_blackhole_is_the_virtual_mic_and_never_a_real_device():
    assert audio_io.is_virtual_mic("BlackHole 2ch", output=True)
    assert audio_io.is_virtual_mic("BlackHole 2ch", output=False)
    assert not audio_io.is_virtual_mic("MacBook Pro Speakers", output=True)


def test_default_audio_per_platform():
    assert isinstance(controller.default_audio("darwin"), mac_audio.MacAudio)
    assert isinstance(controller.default_audio("win32"), controller.WindowsAudio)


def test_the_mac_ducker_does_nothing():
    d = mac_audio.MacAudio().ducker(0.25)
    d.duck(), d.restore(), d.close()
    assert d.available is False


class MacFake(FakeAudio):
    excludes_own_voice = True

    def __init__(self, problem=None, **kw):
        super().__init__(**kw)
        self.problem = problem

    def call_sound_problem(self):
        return self.problem


def test_no_call_sound_stops_the_start_before_anything_opens():
    audio = MacFake(problem="no_call_sound")
    it, _events = make_interpreter(audio=audio)
    with pytest.raises(InterpreterError) as e:
        it.start()
    assert e.value.code == "no_call_sound"
    assert not it.running and not audio.captures and not audio.sinks


def test_hear_off_never_asks_for_the_call_sound():
    audio = MacFake(problem="no_call_sound")
    it, _events = make_interpreter(audio=audio, settings=controller.Settings(my_lang="en", their_lang="ja",
                                                                             hear=False, speak=True))
    it.start()
    try:
        assert it.running and sorted(it._dirs) == ["speak"]
    finally:
        it.stop()


def test_no_blackhole_turns_speak_off_and_hear_runs():
    it, events = make_interpreter(audio=MacFake(virtual_mic=None))
    it.start()
    try:
        assert sorted(it._dirs) == ["hear"]
        assert any(e.get("code") == "no_virtual_mic" for e in events)
    finally:
        it.stop()


def test_a_tap_without_gusan_never_gates_the_other_side():
    it, _events = make_interpreter(audio=MacFake())
    it.start()
    try:
        hear = it._dirs["hear"]
        assert hear.gate.enabled is False
    finally:
        it.stop()


def test_the_mac_messages_are_one_short_line():
    from ui import interpreter_host as host
    for code in ("no_call_sound", "mac_too_old"):
        assert host.message(code) and "\n" not in host.message(code)
