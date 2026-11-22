"""Meetings follow the computer's sound devices (audio_follow.py, Gusan 2.1.11): fake PortAudio
devices and streams and a fake clock - no sound card. Covers the switch of the output mid-meeting
(the call's loopback moves, the microphone is untouched), a device PortAudio doesn't know (a new
PortAudio, the gap filled with silence), never a new PortAudio while another stream is open, a
microphone that is unplugged (the meeting goes on, the mic comes back), other rates / channels, the
watcher's debounce, and the recorder side (main.AudioRecorder: the re-init guard, devices_changed)."""
import threading
import types
import unittest
from types import SimpleNamespace
from unittest import mock


def _np():
    try:
        import numpy as np
        if np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,):
            return np
    except Exception:
        pass
    return None


np = _np()
needs_numpy = unittest.skipUnless(np is not None, "real numpy not importable (stubbed)")
if np is not None:
    import audio_follow as af


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = 0.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += max(0.0, float(dt))
        self.slept += max(0.0, float(dt))


class FakeStream:
    """A device stream: ``level`` in every sample; a loopback offers ``per_call`` frames a poll."""

    def __init__(self, dev, rate, channels, level=0.1, per_call=None):
        self.dev, self.rate, self.channels = dev, rate, channels
        self.level, self.per_call = level, per_call
        self.closed = False
        self.fail = False                 # reads raise (unplugged)
        self.avail_error = None           # get_read_available returns this (PortAudio's error code)
        self.reads = 0

    def get_read_available(self):
        if self.closed:
            raise OSError("closed")
        if self.avail_error is not None:
            return self.avail_error
        return self.per_call if self.per_call is not None else 0

    def read(self, n, exception_on_overflow=False):
        if self.closed or self.fail:
            raise OSError("Stream closed")
        self.reads += 1
        return np.full(int(n) * self.channels, self.level, np.float32).tobytes()

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


def dev(name, ins=1, rate=16000, loop=False, host=0):
    return {"name": name, "maxInputChannels": ins, "maxOutputChannels": 0, "defaultSampleRate": rate,
            "hostApi": 2 if loop else host, "isLoopbackDevice": loop}


class FakePA:
    """PyAudio-like: a device list, defaults, open() -> FakeStream. ``no_16k``: devices that refuse
    16 kHz mono (WASAPI-like, opened in their own format)."""

    def __init__(self, devices, default_in=None, default_loop=None, no_16k=(), levels=None):
        self.devices = [dict(d, index=i) for i, d in enumerate(devices)]
        self.default_in, self.default_loop = default_in, default_loop
        self.no_16k = set(no_16k)
        self.levels = levels or {}
        self.opened = []
        self.gone = set()                 # unplugged: still in this (old) list, but won't open
        self.terminated = False

    def _by_name(self, name):
        for d in self.devices:
            if d["name"] == name:
                return d
        raise OSError("no such device %r" % name)

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, i):
        return dict(self.devices[int(i)])

    def get_default_host_api_info(self):
        return {"index": 0}

    def get_default_input_device_info(self):
        return dict(self._by_name(self.default_in))

    def get_default_wasapi_loopback(self):
        return dict(self._by_name(self.default_loop)) if self.default_loop else None

    def open(self, **kw):
        idx = kw.get("input_device_index")
        d = self.devices[idx] if idx is not None else self._by_name(self.default_in)
        if d["name"] in self.gone:
            raise OSError("Unanticipated host error")
        if kw.get("rate") == 16000 and kw.get("channels") == 1 and d["name"] in self.no_16k:
            raise OSError("Invalid sample rate")
        s = FakeStream(d["name"], kw["rate"], kw["channels"], level=self.levels.get(d["name"], 0.1),
                       per_call=(kw["frames_per_buffer"] if d.get("isLoopbackDevice") else None))
        self.opened.append((d["name"], kw))
        return s

    def terminate(self):
        self.terminated = True


class FakeWatch:
    def __init__(self, state):
        self.generation, self.state, self.what = 1, state, set()
        self.stopped = False

    def change(self, **new):
        old = self.state
        self.state = dict(old, **new)
        self.what = af.changed_keys(old, self.state)
        self.generation += 1

    def stop(self):
        self.stopped = True


SPEAKERS = "Speakers (Realtek(R) Audio)"
PHONES = "Headphones (WH-1000XM4 Stereo)"
HANDSFREE = "Headset (WH-1000XM4 Hands-Free AG Audio)"
LAPTOP_MIC = "Microphone Array (Intel(R) Smart Sound Technology)"
USB_MIC = "Microphone (USB Audio Device)"


def win_state(out=SPEAKERS, comms=None, mic=LAPTOP_MIC, outs=(SPEAKERS,), ins=(LAPTOP_MIC,)):
    """WinMM's view: 31-character names, as Windows gives them."""
    cut = lambda n: n[:31]                                # noqa: E731
    return {"in": cut(mic), "out": cut(out), "comms_out": cut(comms or out),
            "ins": tuple(sorted(cut(n) for n in ins)), "outs": tuple(sorted(cut(n) for n in outs))}


def _meeting(pa_box, watch, clock, *, can_reinit=True, reinit_to=None, mic_name=LAPTOP_MIC,
             loop_name=SPEAKERS + " [Loopback]", settings_mic=""):
    """A mic + call meeting on ``pa_box[0]``: (devices, mic, call, reinit calls)."""
    calls = []

    def reinit():
        calls.append(clock())
        clock.sleep(0.3)                                   # PortAudio takes its time
        if reinit_to is not None:
            pa_box[0] = reinit_to
        return True
    devs = af.MeetingDevices(lambda: pa_box[0], "f32", chunk=1024, watch=watch, clock=clock,
                             sleep=clock.sleep, reinit=reinit,
                             can_reinit=can_reinit if callable(can_reinit) else (lambda: can_reinit),
                             settings_mic=settings_mic)
    pa = pa_box[0]
    li = af.find_loopback(pa, loop_name)
    loop = pa.open(format="f32", channels=2, rate=48000, input=True, input_device_index=li,
                   frames_per_buffer=3072)
    call = devs.call_input(loop, 48000, 2, loop_name)
    mi = af.find_input(pa, mic_name)
    mic = devs.mic_input(pa.open(format="f32", channels=1, rate=16000, input=True, frames_per_buffer=1024,
                                 input_device_index=mi), 16000, 1, mic_name)
    return devs, mic, call, calls


def _pass(mic, call, clock, n=1024):
    """One pass of the record loop: the mic (the clock), then the call."""
    a = mic.get_read_available()
    data = np.frombuffer(mic.read(max(n, min(a, 32000))), np.float32)
    clock.sleep(len(data) / 16000.0)
    got = call.get_read_available()
    loop = np.frombuffer(call.read(got), np.float32) if got > 0 else np.zeros(0, np.float32)
    return data, loop


@needs_numpy
class TestSoundFormat(unittest.TestCase):
    def test_the_resampler_carries_on_across_blocks_without_losing_samples(self):
        for rate in (44100, 48000, 8000, 22050):
            r = af.Resampler(rate)
            total_in, total_out = 0, 0
            for n in (1365, 1024, 7, 4096, 333, 1, 2000):
                total_in += n
                total_out += len(r.process(np.zeros(n, np.float32)))
            self.assertLessEqual(abs(total_out - total_in * 16000.0 / rate), 1.0, rate)

    def test_a_tone_keeps_its_pitch_through_the_resampler(self):
        sr, hz = 48000, 440.0
        t = np.arange(sr * 2) / float(sr)
        sig = np.sin(2 * np.pi * hz * t).astype(np.float32)
        r = af.Resampler(sr)
        out = np.concatenate([r.process(sig[i:i + 1000]) for i in range(0, len(sig), 1000)])
        crossings = np.count_nonzero(np.diff(np.signbit(out)))
        self.assertAlmostEqual(crossings / 2.0 / (len(out) / 16000.0), hz, delta=3.0)

    def test_surround_loopbacks_mix_the_front_never_the_silent_rest(self):
        frames = np.zeros((100, 8), np.float32)
        frames[:, 0] = frames[:, 1] = 0.4                      # a call's stereo on FL / FR of 7.1
        mono = af.downmix(frames.reshape(-1), 8, front=True)
        self.assertTrue(np.allclose(mono, 0.4))
        self.assertTrue(np.allclose(af.downmix(frames.reshape(-1), 8), 0.1))   # (an average: 12 dB down)

    def test_converter_makes_16k_mono_of_48k_stereo_and_8k_hands_free(self):
        c = af.Converter(48000, 2)
        out = c.process(np.full(4800 * 2, 0.25, np.float32).tobytes())
        self.assertEqual(len(out), 1600)
        self.assertTrue(np.allclose(out, 0.25))
        hf = af.Converter(8000, 1, front=True)
        self.assertEqual(len(hf.process(np.full(800, 0.1, np.float32).tobytes())), 1599)  # (+1 next block)


@needs_numpy
class TestNamesAndWatch(unittest.TestCase):
    def test_a_31_character_winmm_name_is_the_same_device_as_portaudios_full_one(self):
        self.assertTrue(af.same_device("Microphone Array (Intel® Smart ",
                                       "Microphone Array (Intel� Smart Sound Technology (Intel� SST))"))
        self.assertTrue(af.same_device(SPEAKERS, SPEAKERS + " [Loopback]"))
        self.assertFalse(af.same_device(SPEAKERS, "Speakers (Steam Streaming Speakers)"))
        self.assertFalse(af.same_device("", SPEAKERS))
        self.assertEqual(af.nice_name("Speakers (Realtek(R) Audio) [Loopback]"), "Speakers (Realtek Audio)")

    def test_a_change_counts_once_it_is_read_twice_in_a_row(self):
        w = af.DeviceWatch(read_state=lambda: None, start=False)
        a, b, c = {"out": "A"}, {"out": "B"}, {"out": "C"}
        self.assertTrue(w.step(a))                           # the first reading: generation 1
        self.assertEqual((w.generation, w.what), (1, set()))
        self.assertFalse(w.step(b))                          # a headset connecting: a burst...
        self.assertFalse(w.step(c))
        self.assertTrue(w.step(c))                           # ...settles on C, published once
        self.assertEqual((w.generation, w.state, w.what), (2, c, {"out"}))
        self.assertFalse(w.step(None))                       # an unreadable moment changes nothing
        self.assertFalse(w.step(c))

    def test_the_watcher_thread_stops(self):
        seen = threading.Event()

        def read():
            seen.set()
            return {"out": "A"}
        w = af.DeviceWatch(read_state=read, poll_s=0.01)
        self.assertTrue(seen.wait(5))
        w.stop()
        w._thread.join(5)
        self.assertFalse(w._thread.is_alive())
        self.assertEqual(w.generation, 1)


@needs_numpy
class TestFollowingTheDevices(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    def _pa(self, *extra, default_in=LAPTOP_MIC, default_loop=SPEAKERS + " [Loopback]", **kw):
        devices = [dev(LAPTOP_MIC), dev(SPEAKERS + " [Loopback]", ins=2, rate=48000, loop=True)]
        devices += list(extra)
        return FakePA(devices, default_in=default_in, default_loop=default_loop, **kw)

    def test_switching_the_output_mid_meeting_moves_the_call_and_leaves_the_mic_alone(self):
        pa = self._pa(dev(PHONES + " [Loopback]", ins=2, rate=44100, loop=True),
                      levels={PHONES + " [Loopback]": 0.3})
        box = [pa]
        watch = FakeWatch(win_state(outs=(SPEAKERS, PHONES)))
        devs, mic, call, reinits = _meeting(box, watch, self.clock)
        for _ in range(3):
            _pass(mic, call, self.clock)
        old_loop, mic_stream, switches = call.legs[0].stream, mic._stream, call.switches
        watch.change(out=PHONES[:31], comms_out=PHONES[:31])
        self.clock.sleep(0.1)
        data, loop = _pass(mic, call, self.clock)
        self.assertTrue(old_loop.closed)                            # the speakers' loopback let go
        self.assertEqual(call.names(), [PHONES + " [Loopback]"])     # the headphones' one is read now
        self.assertEqual(call.switches, switches + 1)               # (the loop drops the old call sound)
        self.assertIs(mic._stream, mic_stream)                      # the microphone was never touched
        self.assertEqual(reinits, [])                               # a device PortAudio knows: no new PortAudio
        self.assertEqual(devs.take_events(), [("call", "Headphones (WH-1000XM4 Stereo)")])
        _d, loop = _pass(mic, call, self.clock)
        self.assertTrue(len(loop) and np.allclose(loop, 0.3, atol=1e-3))   # 44.1 kHz stereo -> 16 kHz mono
        self.assertEqual(len(data), 1024)

    def test_a_device_portaudio_does_not_know_gets_a_new_portaudio_and_the_gap_is_silence(self):
        fresh = self._pa(dev(PHONES + " [Loopback]", ins=2, rate=48000, loop=True), dev(HANDSFREE),
                         default_in=HANDSFREE, default_loop=PHONES + " [Loopback]")
        box = [self._pa()]
        watch = FakeWatch(win_state())
        devs, mic, call, reinits = _meeting(box, watch, self.clock, reinit_to=fresh)
        for _ in range(2):
            _pass(mic, call, self.clock)
        watch.change(**{"in": HANDSFREE[:31], "out": PHONES[:31], "comms_out": PHONES[:31],
                        "ins": (HANDSFREE[:31],), "outs": (PHONES[:31],)})
        got = []
        for _ in range(10):
            data, _loop = _pass(mic, call, self.clock)
            got.append(data)
        self.assertEqual(len(reinits), 1)                           # once, not on every pass
        self.assertEqual(call.names(), [PHONES + " [Loopback]"])
        self.assertEqual(mic.name, HANDSFREE)                       # the headset's microphone too
        self.assertFalse(mic.lost)
        events = devs.take_events()
        self.assertIn(("call", "Headphones (WH-1000XM4 Stereo)"), events)
        self.assertIn(("mic", "Headset (WH-1000XM4 Hands-Free AG Audio)"), events)
        # the 0.3 s PortAudio took is silence on the mic track (the timeline never drifts), then the headset
        heard = np.concatenate(got) != 0
        start = int(np.argmin(heard))                               # where the silence begins...
        zeros = int(np.argmax(heard[start:]))                       # ...and how long it lasts
        self.assertLessEqual(abs(zeros - 0.3 * 16000), 64)
        self.assertTrue(np.allclose(data, 0.1))

    def test_never_a_new_portaudio_while_another_stream_is_open(self):
        fresh = self._pa(dev(PHONES + " [Loopback]", ins=2, rate=48000, loop=True))
        box = [self._pa()]
        watch = FakeWatch(win_state())
        monitor_open = [True]                                       # the mic check in Settings has a stream
        devs, mic, call, reinits = _meeting(box, watch, self.clock, can_reinit=lambda: not monitor_open[0],
                                            reinit_to=fresh)
        old_loop, old_mic = call.legs[0].stream, mic._stream
        watch.change(out=PHONES[:31], comms_out=PHONES[:31], outs=(SPEAKERS[:31], PHONES[:31]))
        for _ in range(60):                                         # ~4 s
            _pass(mic, call, self.clock)
        self.assertEqual(reinits, [])
        self.assertIs(call.legs[0].stream, old_loop)                # the speakers stay meanwhile
        self.assertFalse(old_loop.closed)
        self.assertIs(mic._stream, old_mic)
        monitor_open[0] = False                                     # the mic check closed: now it can
        for _ in range(60):
            _pass(mic, call, self.clock)
        self.assertEqual(len(reinits), 1)
        self.assertEqual(call.names(), [PHONES + " [Loopback]"])
        self.assertTrue(old_loop.closed and old_mic.closed)
        self.assertFalse(mic.lost)
        for _ in range(60):
            _pass(mic, call, self.clock)
        self.assertEqual(len(reinits), 1)                           # and only once

    def test_both_outputs_are_captured_when_calls_play_on_the_communications_one(self):
        pa = self._pa(dev(HANDSFREE + " [Loopback]", ins=1, rate=16000, loop=True),
                      levels={HANDSFREE + " [Loopback]": 0.2, SPEAKERS + " [Loopback]": 0.1})
        box = [pa]
        watch = FakeWatch(win_state(comms=HANDSFREE, outs=(SPEAKERS, HANDSFREE)))
        devs, mic, call, _r = _meeting(box, watch, self.clock)
        _pass(mic, call, self.clock)
        self.assertEqual(sorted(call.names()), sorted([SPEAKERS + " [Loopback]", HANDSFREE + " [Loopback]"]))
        _d, loop = _pass(mic, call, self.clock)
        self.assertTrue(np.allclose(loop[:1024], 0.3, atol=1e-3))   # what plays on either is the call

    def test_an_unplugged_microphone_never_ends_the_meeting_and_the_next_one_is_used(self):
        pa = self._pa(dev(USB_MIC), default_in=USB_MIC)
        box = [pa]
        watch = FakeWatch(win_state(mic=USB_MIC, ins=(LAPTOP_MIC, USB_MIC)))
        devs, mic, call, _r = _meeting(box, watch, self.clock, mic_name=USB_MIC)
        start, delivered = self.clock(), 0
        data, _loop = _pass(mic, call, self.clock)
        delivered += len(data)
        mic._stream.fail = True                                     # unplugged
        pa.gone.add(USB_MIC)
        for _ in range(40):
            data, loop = _pass(mic, call, self.clock)
            delivered += len(data)
            self.assertTrue(len(loop) > 0)                          # the call goes on being recorded
        self.assertTrue(mic.lost)
        self.assertEqual(float(np.abs(data).max()), 0.0)            # the mic keeps time with silence
        self.assertEqual(devs.take_events(), [("mic_lost", "Microphone (USB Audio Device)")])
        # Windows falls back to the laptop's microphone: the meeting's mic follows it
        watch.change(**{"in": LAPTOP_MIC[:31], "ins": (LAPTOP_MIC[:31],)})
        pa.default_in = LAPTOP_MIC
        for _ in range(3):
            data, _loop = _pass(mic, call, self.clock)
            delivered += len(data)
        self.assertFalse(mic.lost)
        self.assertEqual(mic.name, LAPTOP_MIC)
        self.assertTrue(np.allclose(data, 0.1))
        self.assertEqual(devs.take_events(), [("mic", "Microphone Array (Intel Smart Sound Technology)")])
        elapsed = self.clock() - start
        self.assertLessEqual(abs(delivered - elapsed * 16000), 2100)            # no drift across the gap

    def test_a_lost_microphone_is_looked_for_again_without_a_watcher(self):
        pa = self._pa()
        box = [pa]
        devs, mic, call, reinits = _meeting(box, None, self.clock)
        mic._stream.fail = True
        _pass(mic, call, self.clock)
        self.assertTrue(mic.lost)
        for _ in range(60):                                         # ~4 s: the retry opens the default again
            _pass(mic, call, self.clock)
        self.assertFalse(mic.lost)
        self.assertEqual(reinits, [])                               # a retry never makes PortAudio anew


    def test_a_stream_that_stops_delivering_is_lost_after_a_moment_not_waited_on_for_good(self):
        pa = self._pa()
        devs, mic, call, _r = _meeting([pa], None, self.clock)
        s = mic._stream
        s.get_read_available = lambda: 2048 if s.reads < 3 else 0      # it said how much it held... then nothing
        for _ in range(3):
            _pass(mic, call, self.clock)
        t0 = self.clock()
        data, _loop = _pass(mic, call, self.clock)
        self.assertTrue(mic.lost)                                   # after STALL_S, not forever
        self.assertLess(self.clock() - t0, af.STALL_S + 0.5)
        self.assertEqual(len(data), 1024)

    def test_portaudios_error_code_marks_a_loopback_gone(self):
        pa = self._pa()
        devs, mic, call, _r = _meeting([pa], None, self.clock)
        call.legs[0].stream.avail_error = -9999                     # paUnanticipatedHostError, returned
        _pass(mic, call, self.clock)
        self.assertTrue(call.lost)
        self.assertEqual(devs.take_events(), [("call_lost", "Speakers (Realtek Audio)")])

    def test_one_of_two_outputs_going_away_is_no_notice_the_other_still_plays(self):
        pa = self._pa(dev(HANDSFREE + " [Loopback]", ins=1, rate=16000, loop=True))
        watch = FakeWatch(win_state(comms=HANDSFREE, outs=(SPEAKERS, HANDSFREE)))
        devs, mic, call, _r = _meeting([pa], watch, self.clock)
        _pass(mic, call, self.clock)
        self.assertEqual(len(call.names()), 2)
        hf = [leg for leg in call.legs if "Hands-Free" in leg.name][0]
        hf.stream.avail_error = -9999
        pa.gone.add(HANDSFREE + " [Loopback]")
        _pass(mic, call, self.clock)
        self.assertFalse(call.lost)
        self.assertEqual(devs.take_events(), [])
        self.assertTrue(devs._retry_at)                             # (looked for again soon)

    def test_the_call_counts_as_quiet_only_while_the_microphone_hears_someone(self):
        pa = self._pa(levels={SPEAKERS + " [Loopback]": 0.0})
        devs, mic, call, _r = _meeting([pa], None, self.clock)
        for _ in range(int(200 / 0.064)):
            _pass(mic, call, self.clock)
        self.assertGreater(devs.call_quiet_sec(), 180)              # you talk, the call says nothing
        pa_mute = mic._stream
        pa_mute.level = 0.0
        self.clock.sleep(af.MIC_RECENT_S + 1)
        self.assertEqual(devs.call_quiet_sec(), 0.0)                # nobody talks: nothing to tell


# ── the recorder's side (main.py) ────────────────────────────────────────────
def _real_main(case):
    try:
        import main
    except Exception as e:                          # no PySide6 / audio libs here
        case.skipTest("main can't be imported here: %s" % e)
    if type(main.np) is not types.ModuleType or np is None:
        case.skipTest("numpy stubbed in main by another test module")
    return main


class _Monitor:
    def __init__(self, running=False):
        self.running = running
        self._stream = object() if running else None
        self._pa = None
        self._pa_lock = threading.RLock()

    def is_running(self):
        return self.running


@needs_numpy
class TestRecorderSide(unittest.TestCase):
    def setUp(self):
        self.main = _real_main(self)
        self.rec = self.main.AudioRecorder.__new__(self.main.AudioRecorder)
        self.rec._pa_life = threading.RLock()
        self.rec.recording = False
        self.rec._record_thread = None

    def test_portaudio_is_never_made_anew_while_the_mic_check_has_a_stream_open(self):
        main, rec = self.main, self.rec
        rec.audio = mock.create_autospec(main.pyaudio.PyAudio, instance=True)
        rec.mic_monitor = _Monitor(running=True)
        made = mock.Mock(side_effect=AssertionError("no new PortAudio"))
        with mock.patch.object(main.pyaudio, "PyAudio", made):
            self.assertFalse(main.AudioRecorder._can_reinit_audio(rec))
            self.assertFalse(main.AudioRecorder._reinit_audio(rec))
        rec.audio.terminate.assert_not_called()

    def test_a_new_portaudio_takes_the_mic_check_with_it_and_moves_the_settings_mic(self):
        main, rec = self.main, self.rec
        old = mock.create_autospec(main.pyaudio.PyAudio, instance=True)
        new = mock.create_autospec(main.pyaudio.PyAudio, instance=True)
        rec.audio = old
        mon = _Monitor(running=False)
        mon._pa = old
        rec.mic_monitor = mon
        moved = []
        rec.on_devices_moved = moved.append
        lists = {id(old): [(1, "Microphone (USB)"), (2, "Headset")], id(new): [(1, "Headset"), (4, "Microphone (USB)")]}
        with mock.patch.object(main.pyaudio, "PyAudio", return_value=new), \
                mock.patch.object(main.AudioRecorder, "_microphones", side_effect=lambda pa: lists[id(pa)]), \
                mock.patch.object(main, "isinstance", create=True, side_effect=isinstance):
            with mock.patch.object(main.AudioRecorder, "_can_reinit_audio", return_value=True):
                self.assertTrue(main.AudioRecorder._reinit_audio(rec))
        old.terminate.assert_called_once()
        self.assertIs(rec.audio, new)
        self.assertIs(mon._pa, new)                                 # the mic check shares the new one
        self.assertEqual(moved, [{1: 4, 2: 1}])

    def test_rescan_before_a_meeting_waits_for_a_take_and_the_mic_check(self):
        main, rec = self.main, self.rec
        rec.audio = mock.Mock()
        rec.mic_monitor = _Monitor(running=True)
        with mock.patch.object(main.pyaudio, "PyAudio", side_effect=AssertionError("no new PortAudio")):
            self.assertIsNone(main.AudioRecorder.rescan_devices(rec))
            rec.mic_monitor = _Monitor(running=False)
            rec.recording = True
            self.assertIsNone(main.AudioRecorder.rescan_devices(rec))
        rec.audio.terminate.assert_not_called()

    def test_devices_changed_sees_a_headset_that_came_after_portaudio_started(self):
        main, rec = self.main, self.rec
        # PortAudio's MME list: WinMM's 31-character names
        pa = FakePA([dev(LAPTOP_MIC[:31], host=0), dict(dev(SPEAKERS, ins=0, host=0), maxOutputChannels=2)],
                    default_in=LAPTOP_MIC[:31])
        pa.get_host_api_info_by_type = lambda t: {"index": 0}
        pa.get_default_output_device_info = lambda: dict(pa.devices[1])
        rec.audio = pa
        same = win_state()
        with mock.patch.object(main.sys, "platform", "win32"), \
                mock.patch("audio_follow.system_state", return_value=same):
            self.assertFalse(main.AudioRecorder.devices_changed(rec))
        plugged = win_state(out=PHONES, outs=(SPEAKERS, PHONES))
        with mock.patch.object(main.sys, "platform", "win32"), \
                mock.patch("audio_follow.system_state", return_value=plugged):
            self.assertTrue(main.AudioRecorder.devices_changed(rec))
        with mock.patch("audio_follow.system_state", return_value=None):
            self.assertFalse(main.AudioRecorder.devices_changed(rec))
