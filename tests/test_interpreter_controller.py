"""Interpreter controller (both directions, session, monthly time) and the sound helpers, with
fake devices, a fake server session and fake ElevenLabs clients."""
import pytest

import numpy as np
from interpreter import audio_io, controller, pitch, voices
from interpreter.pipeline import wait_until
from interpreter.session import InterpreterError


def _real_numpy():
    try:
        return float(np.asarray([1.0, 2.0, 3.0]).mean()) == 2.0
    except Exception:  # noqa: BLE001
        return False


# test_core stubs numpy for the whole run (the suite's convention: such tests skip there and run
# on their own / in a fresh process).
@pytest.fixture(autouse=True)
def _needs_real_numpy():
    """Checked when each test RUNS: other test files swap numpy for a stub during the run."""
    import sys
    for mod in (sys.modules.get("numpy"), np, audio_io.np, pitch.np):
        try:
            ok = float(mod.asarray([1.0, 2.0, 3.0]).mean()) == 2.0
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            pytest.skip("real numpy needed (suite-wide stub in test_core)")


# ── sound helpers ────────────────────────────────────────────────────────────────────────────
def tone(seconds, freq=440.0, rate=16000, amp=8000):
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16)


def glide(seconds, f0a=150.0, f0b=260.0, rate=16000, amp=8000):
    """Speech-like: harmonics on a gliding pitch, in syllables. (A pure tone repeats every period,
    so a delay off by whole periods matches it just as well.)"""
    t = np.arange(int(seconds * rate)) / rate
    f0 = f0a + (f0b - f0a) * t / max(seconds, 1e-9)
    ph = 2 * np.pi * np.cumsum(f0) / rate
    x = sum(np.sin(k * ph) / k for k in range(1, 6)) * (0.6 + 0.4 * np.sin(2 * np.pi * 3.5 * t) ** 2)
    return (x / np.abs(x).max() * amp).astype(np.int16)


class TestConversion:
    def test_stereo_48k_float_to_mono_16k(self):
        x = np.sin(2 * np.pi * 300 * np.arange(4800) / 48000).astype(np.float32) * 0.5
        stereo = np.repeat(x, 2)
        out = audio_io.to_mono16k(stereo, 2, 48000)
        assert out.dtype == np.int16 and len(out) == 1600
        assert 0.4 * 32767 < np.abs(out).max() < 0.55 * 32767

    def test_round_trip_to_a_device(self):
        pcm = tone(0.1).tobytes()
        dev = audio_io.from_mono16k(pcm, 2, 48000)
        assert len(dev) == 4800 * 2 * 2

    def test_headphones_guess(self):
        assert audio_io.guess_output_kind("Headphones (WH-1000XM5)") == "headphones"
        assert audio_io.guess_output_kind("Headset Earphone (Jabra)") == "headphones"
        assert audio_io.guess_output_kind("Speakers (Realtek(R) Audio)") == "speakers"


class TestOwnVoiceCanceller:
    def test_removes_our_delayed_voice_and_keeps_theirs(self):
        c = audio_io.OwnVoiceCanceller()
        ours = glide(1.0)
        theirs = tone(1.0, 197.0, amp=3000)
        delay = 800                                          # 50 ms device buffer
        c.reference(ours.tobytes())
        # what the loopback hears: their voice + our voice delayed, at 0.8 gain
        cap = theirs[-1600:].astype(np.float32)
        cap += ours[len(ours) - delay - 1600:len(ours) - delay].astype(np.float32) * 0.8
        cleaned = np.frombuffer(c.process(cap.astype(np.int16).tobytes()), np.int16).astype(np.float32)
        assert c.delay == pytest.approx(delay, abs=2)
        assert c.gain == pytest.approx(0.8, abs=0.1)
        residual = cleaned - theirs[-1600:]
        assert np.sqrt(np.mean(residual ** 2)) < 0.15 * np.sqrt(np.mean((ours * 0.8).astype(np.float32) ** 2))

    def test_silent_reference_passes_through(self):
        c = audio_io.OwnVoiceCanceller()
        pcm = tone(0.1).tobytes()
        assert c.process(pcm) == pcm


class TestListenGate:
    def test_mutes_while_playing_unless_barge_in(self):
        playing = {"on": True}
        g = audio_io.ListenGate(lambda: playing["on"], barge_in_rms=2500)
        quiet = tone(0.05, amp=500).tobytes()
        loud = tone(0.05, amp=12000).tobytes()
        assert g.process(quiet) == bytes(len(quiet))
        assert g.process(loud) == loud
        playing["on"] = False
        assert g.process(quiet) == quiet

    def test_disabled_gate_passes(self):
        g = audio_io.ListenGate(lambda: True, enabled=False)
        pcm = tone(0.05).tobytes()
        assert g.process(pcm) == pcm


# ── controller with fakes ───────────────────────────────────────────────────────────────────
class FakeCapture:
    def __init__(self, on_pcm, loopback, device=None):
        self.on_pcm = on_pcm
        self.loopback = loopback
        self.device = device
        self.started = self.stopped = False
        self.fail = None

    def start(self):
        if self.fail:
            raise self.fail
        self.started = True

    def stop(self):
        self.stopped = True


class FakeSink:
    def __init__(self, device, on_played):
        self.device = device
        self.on_played = on_played
        self.played = []
        self.started = self.stopped = False
        self.playing = False
        self.stall = False

    def stalled(self):
        return self.stall

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def play(self, pcm):
        self.played.append(pcm)

    def queued_seconds(self):
        return 0.0

    def is_playing(self, tail_s=0.3):
        return self.playing

    def clear(self):
        pass


class FakeDucker:
    def __init__(self):
        self.ducks = self.restores = 0
        self.closed = False

    def duck(self):
        self.ducks += 1

    def restore(self):
        self.restores += 1

    def close(self):
        self.closed = True
        self.restores += 1


class FakeAudio:
    """Devices: outputs are numbered from 1, the virtual mic's output is 7, the default mic 3."""

    def __init__(self, virtual_mic=7, out_name="Speakers (Realtek)", out_index=None, mic=3):
        self.vmic = virtual_mic
        self.out_name = out_name
        self.out_index = out_index
        self.mic = mic
        self.captures = []
        self.sinks = []
        self.duckers = []
        self.closed = False
        self.fail_mic = None              # an exception the next mic capture's start() raises

    def output_info(self):
        return {"index": self.out_index, "name": self.out_name,
                "kind": audio_io.guess_output_kind(self.out_name)}

    def mic_index(self, wanted=None):
        return wanted if wanted is not None else self.mic

    def capture(self, on_pcm, loopback, device=None):
        c = FakeCapture(on_pcm, loopback, device)
        if not loopback and self.fail_mic is not None:
            c.fail, self.fail_mic = self.fail_mic, None
        self.captures.append(c)
        return c

    def playback(self, device_index=None, on_played=None):
        s = FakeSink(device_index, on_played)
        self.sinks.append(s)
        return s

    def virtual_mic_output(self):
        return self.vmic

    def ducker(self, level):
        d = FakeDucker()
        self.duckers.append(d)
        return d

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, error=None, left=18000, own_voice=True):
        self.own_voice = own_voice
        self.error = error
        self.seconds_left = left
        self.started = None
        self.tokens = []
        self.stopped_with = None
        self.beats = []

    def start(self, hear=None, speak=None):
        if self.error:
            raise InterpreterError(self.error)
        self.started = {"hear": hear, "speak": speak}
        dirs = [d for d, v in (("hear", hear), ("speak", speak)) if v]
        return {"session_id": "s1", "seconds_left": self.seconds_left,
                "scribe_tokens": {d: "scribe-" + d for d in dirs},
                "tts_tokens": {d: "tts-" + d for d in dirs},
                "voices": {"hear": "preset", "speak": "my-voice"}, "own_voice": self.own_voice}

    def token(self, kind, direction=None):
        self.tokens.append((kind, direction))
        return f"fresh-{kind}-{direction}"

    def heartbeat(self, seconds):
        self.beats.append(seconds)
        return self.seconds_left

    def stop(self, seconds):
        self.stopped_with = seconds
        return self.seconds_left


class FakeSTT:
    made = []

    def __init__(self, token, language, on_event, token_provider=None, keyterms=(), vad_silence=0.4):
        self.token, self.language, self.on_event = token, language, on_event
        self.keyterms = keyterms
        self.audio = []
        self.closed = False
        FakeSTT.made.append(self)

    def start(self):
        pass

    def send_audio(self, pcm):
        self.audio.append(pcm)

    def close(self):
        self.closed = True


class FakeVoice:
    made = []

    def __init__(self, token_provider, voice_id, language):
        self.token_provider, self.voice_id, self.language = token_provider, voice_id, language
        self.tokens = []
        self.spoken = []
        self.used = []
        self.set_to = None
        self.closed = False
        FakeVoice.made.append(self)

    def open(self):
        self.tokens.append(self.token_provider())

    def speak(self, text, on_audio, should_stop, voice_id=None):
        self.spoken.append(text)
        self.used.append(voice_id)
        on_audio(b"\x00\x00" * 160)
        return 0.01

    def set_voices(self, ids):
        self.set_to = list(ids)

    def close(self):
        self.closed = True


def make(settings=None, audio=None, sess=None, clock=None, **kw):
    FakeSTT.made.clear()
    FakeVoice.made.clear()
    events = []

    def tr(text, a, b, ctx, on_delta, token=None, glossary=()):
        return f"[{b}] {text}"
    kw.setdefault("stt_cls", FakeSTT)
    kw.setdefault("voice_cls", FakeVoice)
    it = controller.Interpreter(lambda: "jwt", events.append,
                                settings or controller.Settings(my_lang="en", their_lang="ja", glossary=["Gusan"]),
                                audio=audio or FakeAudio(), session=sess or FakeSession(), translate=tr,
                                **({"clock": clock} if clock else {}), **kw)
    return it, events


class TestController:
    def test_starts_both_directions_with_the_right_languages_and_devices(self):
        audio, sess = FakeAudio(), FakeSession()
        it, ev = make(audio=audio, sess=sess)
        it.start()
        try:
            assert sess.started == {"hear": ("ja", "en"), "speak": ("en", "ja")}
            stts = {s.language: s for s in FakeSTT.made}
            assert stts["ja"].token == "scribe-hear" and stts["en"].token == "scribe-speak"
            assert stts["ja"].keyterms == ["Gusan"]
            hear_voice = [v for v in FakeVoice.made if v.language == "en"][0]
            speak_voice = [v for v in FakeVoice.made if v.language == "ja"][0]
            # the others are heard in the chosen woman's + man's voices, I speak in my own clone
            assert hear_voice.voice_id == [voices.DEFAULT_FEMALE, voices.DEFAULT_MALE]
            assert speak_voice.voice_id == "my-voice"
            # (each voice connects beside its speech-to-text, in the background)
            assert wait_until(lambda: hear_voice.tokens == ["tts-hear"] and speak_voice.tokens == ["tts-speak"])
            voices_ = {"preset": hear_voice}
            loop = [c for c in audio.captures if c.loopback][0]
            mic = [c for c in audio.captures if not c.loopback][0]
            assert loop.started and mic.started
            hear_sink, speak_sink = audio.sinks
            assert hear_sink.device is None and speak_sink.device == 7        # speak -> virtual mic
            assert it.speakers_mode is True                                  # "Speakers (Realtek)"
            # the call's sound reaches the hear pipeline, the mic the speak pipeline
            loop.on_pcm(b"\x01\x00" * 800)
            mic.on_pcm(b"\x02\x00" * 800)
            assert stts["ja"].audio and stts["en"].audio
            # a phrase from them is translated and spoken into the headphones
            stts["ja"].on_event("committed", "こんにちは")
            assert wait_until(lambda: voices_["preset"].spoken == ["[en] こんにちは"])
            assert wait_until(lambda: hear_sink.played)
            assert [e for e in ev if e.get("type") == "session"][0]["directions"] == ["hear", "speak"]
        finally:
            it.stop()
        assert sess.stopped_with is not None
        assert all(c.stopped for c in audio.captures) and all(s.stopped for s in audio.sinks)
        assert [e for e in ev if e.get("type") == "session"][-1]["state"] == "stopped"

    def test_no_virtual_mic_runs_hear_only_and_says_why(self):
        audio, sess = FakeAudio(virtual_mic=None), FakeSession()
        it, ev = make(audio=audio, sess=sess)
        it.start()
        try:
            assert sess.started == {"hear": ("ja", "en"), "speak": None}
            assert any(e.get("code") == "no_virtual_mic" for e in ev)
        finally:
            it.stop()

    def test_server_refusal_opens_nothing(self):
        audio = FakeAudio()
        it, _ev = make(audio=audio, sess=FakeSession(error="pro_required"))
        with pytest.raises(InterpreterError) as e:
            it.start()
        assert e.value.code == "pro_required"
        assert audio.captures == [] and not it.running

    def test_turning_speak_off_and_on_while_running(self):
        audio, sess = FakeAudio(), FakeSession()
        it, _ev = make(audio=audio, sess=sess)
        it.start()
        try:
            it.set_direction("speak", False)
            mic = [c for c in audio.captures if not c.loopback][0]
            assert mic.stopped
            it.set_direction("speak", True)
            # a new speak direction asks the server for fresh single-use tokens
            assert ("scribe", "speak") in sess.tokens
            assert wait_until(lambda: ("tts", "speak") in sess.tokens)
        finally:
            it.stop()

    def test_headphones_mode_does_not_gate_the_mic(self):
        audio = FakeAudio(out_name="Headphones (WH-1000XM5)")
        it, _ev = make(audio=audio)
        it.start()
        try:
            assert it.speakers_mode is False
            assert it._dirs["speak"].gate.enabled is False
        finally:
            it.stop()

    def test_allowance_used_up_stops_it(self, monkeypatch):
        monkeypatch.setattr(controller, "HEARTBEAT_S", 0.05)
        sess = FakeSession(left=0)
        it, ev = make(sess=sess)
        it.start()
        def stopped():
            return [e for e in ev if e.get("type") == "session" and e.get("state") == "stopped"]
        assert wait_until(lambda: bool(stopped()), timeout=3)
        assert not it.running
        assert any(e.get("code") == "allowance_used" for e in ev)
        assert stopped()[0]["reason"] == "allowance_used"

    def test_settings_from_config(self):
        s = controller.Settings.from_config({"interpreter_my_language": "hy", "interpreter_their_language": "fr",
                                             "interpreter_speak": False, "vocabulary": ["Aibuben"],
                                             "interpreter_keep_underneath": False})
        assert (s.my_lang, s.their_lang, s.speak, s.hear) == ("hy", "fr", False, True)
        assert s.glossary == ["Aibuben"] and s.duck_level == 0.0


# ── voices: match the speaker ────────────────────────────────────────────────────────────────
def voiced(f0, seconds=1.0, amp=6000):
    t = np.arange(int(seconds * 16000)) / 16000
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 6))
    return (x / np.abs(x).max() * amp).astype(np.int16)


class TestPitch:
    @pytest.mark.parametrize("f0", [110.0, 125.0, 200.0, 230.0])
    def test_frame_pitch(self, f0):
        got = pitch.frame_pitch(voiced(f0, 0.04).astype(np.float32))
        assert got == pytest.approx(f0, rel=0.04)

    def test_quiet_frame_has_no_pitch(self):
        assert pitch.frame_pitch(np.zeros(640, np.float32)) is None

    def test_tracker_and_guess(self):
        tr = pitch.PitchTracker()
        tr.feed(voiced(118.0, 1.0).tobytes(), 1.0)
        tr.feed(voiced(215.0, 1.0).tobytes(), 2.0)
        assert tr.typical(0.0, 1.0)[0] == pytest.approx(118.0, rel=0.05)
        assert tr.typical(1.05, 2.0)[0] == pytest.approx(215.0, rel=0.05)
        g = pitch.GenderGuess()
        assert g.judge(*tr.typical(0.0, 1.0)) == "male"
        assert g.judge(*tr.typical(1.05, 2.0)) == "male"            # decided once: a woman later changes nothing
        tr.clear()
        assert tr.typical(0.0, 2.0) == (None, 0)

    def test_the_voice_is_decided_once_for_the_call(self):
        """The founder's rule (2026-11-09): one decision per call, then never again - no flip, no
        more pitch work. Until it is clear (enough voice, not in between) nothing is decided."""
        g = pitch.GenderGuess()
        assert g.judge(None, 0) is None and not g.decided
        assert g.judge(120, 5) is None                    # too little voice to decide for good
        assert g.judge(148, 30) is None                   # in between: wait for clearer voice
        assert g.judge(205, 30) == "female" and g.decided
        for hz, n in ((120, 30), (100, 60), (None, 0), (148, 30)):
            assert g.judge(hz, n) == "female"            # final


class TestVoiceChoice:
    def test_match_and_fixed(self):
        c = voices.VoiceChoice()
        assert c.for_gender("male") == voices.DEFAULT_MALE and c.for_gender("female") == voices.DEFAULT_FEMALE
        assert c.for_gender(None) == voices.DEFAULT_FEMALE
        f = voices.VoiceChoice(mode=voices.FIXED, fixed="nPczCjzI2devNBz1zQrb")
        assert f.for_gender("female") == "nPczCjzI2devNBz1zQrb" and f.all_ids() == ["nPczCjzI2devNBz1zQrb"]

    def test_unknown_ids_fall_back(self):
        c = voices.VoiceChoice.from_config({"interpreter_voice_female": "nope", "interpreter_voice_mode": "x"})
        assert c.mode == voices.MATCH and c.female == voices.DEFAULT_FEMALE

    def test_every_preset_has_a_sample(self):
        import os as _os
        for vid, *_rest in voices.PRESETS:
            assert _os.path.isfile(voices.sample_path(vid)), vid
        assert voices.label(voices.DEFAULT_FEMALE) == "Sarah \u00b7 calm"


class TestMatchTheSpeaker:
    def test_a_man_is_heard_in_the_mans_voice(self):
        audio = FakeAudio()
        clk = {"t": 1000.0}                  # sound arrives in real time: the clock moves with it
        it, _ev = make(audio=audio, clock=lambda: clk["t"])
        it.start()
        try:
            loop = [c for c in audio.captures if c.loopback][0]
            hear_stt = [s for s in FakeSTT.made if s.language == "ja"][0]
            hear_voice = [v for v in FakeVoice.made if v.language == "en"][0]
            clk["t"] += 1.5
            loop.on_pcm(voiced(115.0, 1.5).tobytes())
            hear_stt.on_event("committed", "こんにちは")
            assert wait_until(lambda: hear_voice.used == [voices.DEFAULT_MALE])
            # Decided once for the call: a higher voice later keeps the man's voice, and no pitch
            # is measured any more (no frame is even looked at).
            calls = []
            real = pitch.frame_pitch
            pitch.frame_pitch = lambda x: calls.append(1) or real(x)
            try:
                clk["t"] += 1.5
                loop.on_pcm(voiced(220.0, 1.5).tobytes())
                hear_stt.on_event("committed", "ありがとう")
                assert wait_until(lambda: hear_voice.used == [voices.DEFAULT_MALE, voices.DEFAULT_MALE])
            finally:
                pitch.frame_pitch = real
            assert calls == []
            it.set_voice_choice(voices.VoiceChoice(mode=voices.FIXED, fixed="nPczCjzI2devNBz1zQrb"))
            assert hear_voice.set_to == ["nPczCjzI2devNBz1zQrb"]
            hear_stt.on_event("committed", "またね")
            assert wait_until(lambda: hear_voice.used[-1:] == ["nPczCjzI2devNBz1zQrb"])
        finally:
            it.stop()

    def test_without_a_clone_my_voice_is_matched_too(self):
        audio = FakeAudio()
        it, _ev = make(audio=audio, sess=FakeSession(own_voice=False))
        it.start()
        try:
            speak_voice = [v for v in FakeVoice.made if v.language == "ja"][0]
            assert speak_voice.voice_id == [voices.DEFAULT_FEMALE, voices.DEFAULT_MALE]
            mic = [c for c in audio.captures if not c.loopback][0]
            speak_stt = [s for s in FakeSTT.made if s.language == "en"][0]
            mic.on_pcm(voiced(120.0, 1.5).tobytes())
            speak_stt.on_event("committed", "hello")
            assert wait_until(lambda: speak_voice.used == [voices.DEFAULT_MALE])
        finally:
            it.stop()

    def test_no_pitch_work_when_one_voice_is_used_or_nothing_is_voiced(self):
        """One voice for everyone, or captions only: the pitch of the call is never measured."""
        calls = []
        real = pitch.frame_pitch
        pitch.frame_pitch = lambda x: calls.append(1) or real(x)
        try:
            for kw in ({"voice_choice": voices.VoiceChoice(mode=voices.FIXED, fixed="nPczCjzI2devNBz1zQrb")},
                       {"captions_only": True}):
                audio = FakeAudio()
                it, _ev = make(controller.Settings(my_lang="en", their_lang="ja", **kw), audio=audio,
                               sess=FakeSession(own_voice=True))
                it.start()
                try:
                    loop = [c for c in audio.captures if c.loopback][0]
                    hear_stt = [s for s in FakeSTT.made if s.language == "ja"][0]
                    loop.on_pcm(voiced(115.0, 1.5).tobytes())
                    hear_stt.on_event("committed", "こんにちは")
                    assert wait_until(lambda: any(e.get("type") == "phrase" for e in _ev))
                finally:
                    it.stop()
        finally:
            pitch.frame_pitch = real
        assert calls == []


# ── devices (a fake PyAudio: no sound card is touched) ─────────────────────────────────────────
def _dev(name, host, outs=0, ins=0, rate=48000, loopback=False):
    return {"name": name, "hostApi": host, "maxOutputChannels": outs, "maxInputChannels": ins,
            "defaultSampleRate": rate, "isLoopbackDevice": loopback}


# host 0 = MME, host 1 = WASAPI. Windows' default mic is the virtual cable (some people set it so).
DEVICES = [
    _dev("Speakers (Realtek(R) Audio)", 0, outs=2, rate=44100),          # 0: MME
    _dev("Microphone Array (Realtek(R) Audio)", 1, ins=4),                 # 1: a 4-mic array
    _dev("CABLE Output (VB-Audio Virtual Cable)", 1, ins=2),               # 2: the virtual mic
    _dev("Speakers (Realtek(R) Audio)", 1, outs=8),                        # 3: 7.1 output
    _dev("Headphones (AirPods Pro)", 1, outs=2),                           # 4
    _dev("CABLE Input (VB-Audio Virtual Cable)", 1, outs=2),               # 5
    _dev("Speakers (Realtek(R) Audio) [Loopback]", 1, ins=8, loopback=True),   # 6
    _dev("Headphones (AirPods Pro) [Loopback]", 1, ins=2, loopback=True),      # 7
    _dev("USB Mic (Blue Yeti)", 1, ins=2),                                 # 8
]


class FakeStream:
    def __init__(self, fail_reads=False):
        self.fail_reads = fail_reads
        self.closed = False

    def get_read_available(self):
        if self.fail_reads:
            raise OSError(-9999, "Unanticipated host error")
        return 0

    def read(self, n, exception_on_overflow=False):
        return b""

    def start_stream(self):
        pass

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class FakePA:
    """PyAudio over DEVICES. WASAPI shared mode takes only a device's own channel count."""

    def __init__(self, devices=DEVICES, wasapi_out=3, wasapi_in=2, mme_out=0, fail_reads=False):
        self.devices, self.wasapi_out, self.wasapi_in, self.mme_out = devices, wasapi_out, wasapi_in, mme_out
        self.fail_reads = fail_reads
        self.opened = []
        self.terminated = False

    def get_host_api_info_by_type(self, _t):
        return {"index": 1, "defaultOutputDevice": self.wasapi_out, "defaultInputDevice": self.wasapi_in}

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, i):
        if not 0 <= i < len(self.devices):
            raise OSError(-9996, "Invalid device")
        return dict(self.devices[i], index=i)

    def get_default_wasapi_device(self, d_out=False, d_in=False):
        return self.get_device_info_by_index(self.wasapi_out if d_out else self.wasapi_in)

    def get_default_output_device_info(self):
        return self.get_device_info_by_index(self.mme_out)

    def get_wasapi_loopback_analogue_by_index(self, i):
        name = self.devices[i]["name"]
        for j, d in enumerate(self.devices):
            if d["isLoopbackDevice"] and name in d["name"]:
                return self.get_device_info_by_index(j)
        raise LookupError(name)

    def open(self, **kw):
        self.opened.append(kw)
        idx = kw.get("input_device_index", kw.get("output_device_index"))
        info = self.devices[idx]
        own = info["maxInputChannels"] if kw.get("input") else info["maxOutputChannels"]
        if info["hostApi"] == 1 and (kw["channels"] != own or kw["rate"] != info["defaultSampleRate"]):
            raise OSError(-9998, "Invalid number of channels")
        return FakeStream(self.fail_reads)

    def terminate(self):
        self.terminated = True


class FakePyAudioModule:
    paWASAPI, paFloat32, paInt16, paContinue = 13, 1, 8, 0

    def __init__(self, pa):
        self._pa = pa

    def PyAudio(self):
        return self._pa


@pytest.fixture
def fake_pa(monkeypatch):
    pa = FakePA()
    monkeypatch.setattr(audio_io, "_pyaudio", lambda: FakePyAudioModule(pa))
    return pa


class TestDevices:
    def test_the_virtual_microphone_is_never_recorded_or_played_into(self, fake_pa):
        pick = audio_io.pick_device
        assert pick(fake_pa, output=False) == 1                  # the default is the cable: a real mic
        assert pick(fake_pa, output=False, wanted=2) == 1        # picked in Gusan, but it's the cable
        assert pick(fake_pa, output=False, wanted=8) == 8        # the mic picked in Gusan
        assert pick(fake_pa, output=False, name="USB Mic (Blue Yeti)") == 8   # Windows' default, read live
        assert pick(fake_pa, output=True, name="Headphones (AirPods Pro)") == 4
        assert pick(fake_pa, output=True, name="CABLE Input (VB-Audio Virtual Cable)") == 3
        assert audio_io.loopback_of(fake_pa, 4) == 7

    def test_devices_open_in_their_own_channel_count(self, fake_pa):
        cap = audio_io.CaptureSource(lambda pcm: None, device_index=1, pa=fake_pa)     # 4-mic array
        cap.start()
        cap.stop()
        assert cap.channels == 4 and fake_pa.opened[-1]["channels"] == 4
        loop = audio_io.CaptureSource(lambda pcm: None, loopback=True, device_index=6, pa=fake_pa)   # 7.1
        loop.start()
        loop.stop()
        assert loop.channels == 8
        sink = audio_io.PlaybackSink(device_index=3, pa=fake_pa)
        sink.start()
        sink.stop()
        assert sink.channels == 8 and fake_pa.opened[-1]["channels"] == 8
        assert len(audio_io.from_mono16k(b"\x01\x00" * 320, 8, 48000)) == 960 * 8 * 2

    def test_the_hear_voice_plays_on_wasapi_not_mme(self, fake_pa):
        sink = audio_io.PlaybackSink(pa=fake_pa)
        sink.start()
        sink.stop()
        assert sink.device_index == 3 and fake_pa.opened[-1]["output_device_index"] == 3

    def test_a_sink_whose_device_went_away_stops_playing(self, fake_pa, monkeypatch):
        import time as _t
        monkeypatch.setattr(audio_io.PlaybackSink, "STALL_S", 0.05)
        sink = audio_io.PlaybackSink(device_index=4, pa=fake_pa)
        sink.start()                          # the fake device never asks for sound
        sink.play(b"\x01\x00" * 16000)
        assert sink.is_playing()
        _t.sleep(0.12)
        assert sink.stalled() and not sink.is_playing() and sink.queued_seconds() == 0
        sink.stop()

    def test_a_capture_whose_device_went_away_says_so(self, monkeypatch):
        pa = FakePA(fail_reads=True)
        monkeypatch.setattr(audio_io, "_pyaudio", lambda: FakePyAudioModule(pa))
        monkeypatch.setattr(audio_io.CaptureSource, "LOST_S", 0.05)
        cap = audio_io.CaptureSource(lambda pcm: None, device_index=8, pa=pa, poll_s=0.005)
        cap.start()
        try:
            assert wait_until(lambda: cap.lost, timeout=2)
        finally:
            cap.stop()

    def test_a_long_gap_is_skipped_and_a_bad_chunk_does_not_stop_the_capture(self, fake_pa, monkeypatch):
        import time as _t

        class Clock:                            # time moves only as the pump sleeps (or jumps)
            t = 1000.0

            @staticmethod
            def monotonic():
                return Clock.t

            @staticmethod
            def sleep(s):
                _t.sleep(0.001)
                Clock.t += s
        monkeypatch.setattr(audio_io, "time", Clock)
        sizes = []

        def on_pcm(pcm):
            sizes.append(len(pcm))
            if len(sizes) == 1:
                raise ValueError("a bad chunk")
        cap = audio_io.CaptureSource(on_pcm, loopback=True, device_index=6, pa=fake_pa, poll_s=0.02)
        cap.start()
        try:
            assert wait_until(lambda: len(sizes) >= 2, timeout=2)      # still pumping after the error
            Clock.t += 8 * 3600                                         # the laptop slept all night
            n = len(sizes)
            assert wait_until(lambda: len(sizes) >= n + 2, timeout=2)
            assert max(sizes) <= int(0.5 * 16000) * 2                   # at most half a second at once
        finally:
            cap.stop()


class TestWindowsAudio:
    def test_it_follows_the_device_windows_uses_now(self, fake_pa, monkeypatch):
        # PortAudio still thinks Speakers (3) is the default; Windows moved to the AirPods, and
        # its default mic is the virtual cable
        monkeypatch.setattr(audio_io, "windows_default", lambda output=True: (
            ("Headphones (AirPods Pro)", 3) if output else ("CABLE Output (VB-Audio Virtual Cable)", 4)))
        w = controller.WindowsAudio()
        assert w.output_info() == {"index": 4, "name": "Headphones (AirPods Pro)", "kind": "headphones"}
        assert w.mic_index() == 1 and w.mic_index(8) == 8
        loop = w.capture(lambda pcm: None, loopback=True, device=4)
        assert loop.device_index == 7                                  # the headset's own loopback
        assert w.playback().device_index == 4
        w.close()                                                      # the sound system is let go
        assert fake_pa.terminated
        with pytest.raises(RuntimeError):
            w.virtual_mic_output()

    def test_headphones_by_form_factor_and_in_any_language(self):
        kind = audio_io.guess_output_kind
        assert kind("Speakers (Jabra Evolve2 65)", 5) == "headphones"     # a USB headset
        assert kind("Headphones (Realtek(R) Audio)", 1) == "speakers"
        assert kind("Kopfhörer (Realtek(R) Audio)") == "headphones"
        assert kind("Наушники (AirPods Pro)") == "headphones"
        assert kind("Auriculares (WH-1000XM5)") == "headphones"
        assert kind("ヘッドホン (Realtek(R) Audio)") == "headphones"
        assert kind("Lautsprecher (Realtek(R) Audio)") == "speakers"


class TestConversionInStep:
    def test_chunks_are_converted_without_stretching(self):
        ramp = (np.arange(320) * 50).astype(np.int16)
        dev = np.frombuffer(audio_io.from_mono16k(ramp.tobytes(), 1, 48000), np.int16)
        assert np.array_equal(dev[::3], ramp)            # every third device sample is a source sample
        back = audio_io.to_mono16k(dev.astype(np.float32) / 32768.0, 1, 48000)
        assert np.abs(back[1:-1].astype(int) - ramp[1:-1]).max() <= 2

    def test_rms_of_loud_int16_does_not_overflow(self):
        assert audio_io.rms(np.full(160, 20000, np.int16)) == pytest.approx(20000, rel=1e-3)


class TestOwnVoiceInStep:
    def test_after_a_phrase_the_silence_stays_silent(self):
        c = audio_io.OwnVoiceCanceller()
        sink = audio_io.PlaybackSink(on_played=c.reference)
        sink.rate, sink.channels = 16000, 1
        sink.play(glide(0.8).tobytes())
        delay, played, outs = 480, [], []
        for i in range(100):                              # 2 s of callbacks: the phrase, then silence
            out, _flag = sink._callback(None, 320, None, 0)
            played.append(np.frombuffer(out, np.int16).astype(np.float32))
            loop = np.concatenate([np.zeros(delay, np.float32)] + played)    # what played, later
            chunk = loop[i * 320:(i + 1) * 320].astype(np.int16).tobytes()
            outs.append(np.frombuffer(c.process(chunk), np.int16))
        after = np.concatenate(outs)[int(1.2 * 16000):]   # well after the phrase and its echo
        assert np.abs(after).max() == 0

    def test_their_voice_is_told_from_our_leftovers(self):
        c = audio_io.OwnVoiceCanceller()
        ours = glide(1.5, amp=8000)
        theirs = glide(1.5, 110.0, 135.0, amp=8000)
        delay, verdicts = 640, []
        for i in range(0, len(ours) - 320, 320):
            c.reference(ours[i:i + 320].tobytes())
            j = i - delay
            cap = ours[j:j + 320].astype(np.float32) if j >= 0 else np.zeros(320, np.float32)
            if i >= 16000:                                 # from 1 s on, they talk (turned down)
                cap = cap + theirs[i:i + 320] * 0.25
            c.process(cap.astype(np.int16).tobytes())
            verdicts.append((i, c.their_voice()))
        assert not any(v for i, v in verdicts if 3200 <= i < 16000)      # only our voice: never theirs
        assert np.mean([v for i, v in verdicts if i >= 16000 + delay]) > 0.6


class TestGates:
    def test_hold_keeps_the_gate_open_between_words(self):
        talking = {"now": True}
        g = audio_io.ListenGate(lambda: True, barge_in=lambda pcm: talking["now"], hold_s=10.0)
        pcm = tone(0.02, amp=500).tobytes()
        assert g.process(pcm) == pcm
        talking["now"] = False
        assert g.process(pcm) == pcm                       # within the hold
        g.hold_s, g._open_until = 0.0, 0.0
        assert g.process(pcm) == bytes(len(pcm))

    def test_sound_level_follows_the_speakers(self):
        import time as _t
        lv = audio_io.SoundLevel(tail_s=0.05)
        lv.feed(tone(0.02, amp=6000).tobytes())
        assert lv.active() and lv.level() > 4000
        _t.sleep(0.08)
        assert not lv.active() and lv.level() == 0.0


class TestDucker:
    class Vol:
        def __init__(self):
            self.v = 1.0

        def GetMasterVolume(self):
            return self.v

        def SetMasterVolume(self, v, _ctx):
            self.v = v

    def test_close_puts_it_back_for_good(self):
        vol = self.Vol()
        d = audio_io.Ducker(level=0.25)
        d._sessions = lambda: [(1234, vol)]
        d.duck()
        assert vol.v == 0.25
        d.close()                                 # Stop / Quit
        d.duck()                                  # the duck loop's last step, a moment later
        assert vol.v == 1.0

    def test_duck_never_raises_and_sets_up_com_on_its_thread(self, monkeypatch):
        import sys
        import threading
        import types
        inits = []
        comtypes = types.SimpleNamespace(COINIT_MULTITHREADED=0,
                                         CoInitializeEx=lambda flags: inits.append(threading.get_ident()))
        vol = self.Vol()
        session = types.SimpleNamespace(Process=types.SimpleNamespace(name=lambda: "Zoom.exe"), ProcessId=7,
                                        SimpleAudioVolume=vol)
        pycaw = types.SimpleNamespace(AudioUtilities=types.SimpleNamespace(GetAllSessions=lambda: [session]))
        monkeypatch.setitem(sys.modules, "comtypes", comtypes)
        monkeypatch.setitem(sys.modules, "pycaw", types.SimpleNamespace(pycaw=pycaw))
        monkeypatch.setitem(sys.modules, "pycaw.pycaw", pycaw)
        d = audio_io.Ducker(level=0.25)
        t = threading.Thread(target=d.duck)
        t.start()
        t.join()
        assert inits == [t.ident] and vol.v == 0.25
        d.restore()

        def broken():
            raise OSError(-2147221008, "CoInitialize has not been called")
        pycaw.AudioUtilities.GetAllSessions = broken
        audio_io.Ducker().duck()                  # no exception: the interpreter goes on


# ── the controller: findings of the 2026-11-08 review ──────────────────────────────────────────
class ServerLikeSession(FakeSession):
    """Answers like interpreter-session: a speak voice only when Speak was asked for."""

    def start(self, hear=None, speak=None):
        info = super().start(hear, speak)
        info["voices"] = {"hear": "preset"} if hear else {}
        if speak:
            info["voices"]["speak"] = "my-voice" if self.own_voice else "preset"
        info["own_voice"] = bool(speak) and self.own_voice
        return info


class FakeVoiceStore:
    def __init__(self, voice):
        self.voice = voice
        self.calls = 0

    def get(self):
        self.calls += 1
        return self.voice


def silent(chunks):
    return not b"".join(chunks).strip(b"\x00")


class TestReviewFixes:
    def test_their_words_reach_hear_while_the_translation_plays(self):
        audio = FakeAudio(out_name="Headphones (WH-1000XM5)")     # headphones: Hear's gate still runs
        it, _ev = make(audio=audio)
        it.start()
        try:
            hear = it._dirs["hear"]
            hear_stt = [s for s in FakeSTT.made if s.language == "ja"][0]
            hear.sink.playing = True                                 # Gusan's translation plays
            ours = glide(2.0, amp=6000)
            theirs = glide(2.0, 105.0, 135.0, amp=6000)
            delay = 640
            ref = np.concatenate([ours, np.zeros(delay, np.int16)])
            cap = np.concatenate([np.zeros(delay), ours.astype(np.float64)])
            cap[16000:32000] += theirs[:16000] * 0.25                # from 1 s on they talk over it
            for i in range(0, len(ref) - 320, 320):
                hear.sink.on_played(ref[i:i + 320].tobytes())       # what the device was handed
                loop_chunk = np.clip(cap[i:i + 320], -32768, 32767).astype(np.int16).tobytes()
                hear.capture.on_pcm(loop_chunk)                      # what the loopback heard
            out = np.frombuffer(b"".join(hear_stt.audio), np.int16).astype(np.float64)
            assert audio_io.rms(out[4000:15500]) < 0.05 * audio_io.rms(ours)            # not our voice
            assert audio_io.rms(out[16000 + delay:32000]) > 0.5 * audio_io.rms(theirs * 0.25)   # theirs
        finally:
            it.stop()

    def test_speakers_mode_mic_gate_follows_hear_switched_on_later(self):
        audio = FakeAudio()                                          # laptop speakers
        it, _ev = make(settings=controller.Settings(my_lang="en", their_lang="ja", hear=False), audio=audio)
        it.start()
        try:
            speak = it._dirs["speak"]
            speak_stt = [s for s in FakeSTT.made if s.language == "en"][0]
            echo = tone(0.05, amp=1500).tobytes()                   # the hear voice, heard by the mic
            for _ in range(2):                                       # on later, then off and on again
                it.set_direction("hear", True)
                it._dirs["hear"].sink.playing = True
                speak_stt.audio.clear()
                speak.capture.on_pcm(echo)
                assert speak_stt.audio and silent(speak_stt.audio)
                it.set_direction("hear", False)
        finally:
            it.stop()

    def test_speakers_mode_mutes_the_mic_while_the_call_plays(self):
        audio = FakeAudio()                                          # laptop speakers
        it, _ev = make(settings=controller.Settings(my_lang="en", their_lang="ja", hear=False), audio=audio)
        it.start()
        try:
            speak = it._dirs["speak"]
            assert speak.monitor is not None and speak.monitor.loopback and speak.monitor.started
            speak_stt = [s for s in FakeSTT.made if s.language == "en"][0]
            speak.monitor.on_pcm(tone(0.05, freq=220.0, amp=6000).tobytes())    # the call plays
            speak_stt.audio.clear()
            speak.capture.on_pcm(tone(0.05, freq=220.0, amp=2000).tobytes())    # ...and the mic hears it
            assert silent(speak_stt.audio)
            loud = tone(0.05, freq=180.0, amp=12000).tobytes()                   # I talk over them
            speak.capture.on_pcm(loud)
            assert speak_stt.audio[-1] == loud
        finally:
            it.stop()

    def test_a_speak_that_cannot_open_the_mic_leaves_hear_running_and_nothing_behind(self):
        audio = FakeAudio()
        audio.fail_mic = OSError(-9996, "Invalid device")
        it, ev = make(audio=audio)
        it.start()
        try:
            assert it.running and sorted(it._dirs) == ["hear"]
            assert any(e.get("code") == "no_mic_sound" and e.get("direction") == "speak" for e in ev)
            speak_sink = [s for s in audio.sinks if s.device == 7][0]
            assert speak_sink.started and speak_sink.stopped
            assert [v for v in FakeVoice.made if v.language == "ja"][0].tokens == []   # never connected
        finally:
            it.stop()

    def test_a_direction_that_fails_to_connect_closes_what_it_opened(self):
        class NoConnection(FakeSTT):
            def start(self):
                if self.language == "en":
                    raise OSError("connect failed")
        audio = FakeAudio()
        it, ev = make(audio=audio, stt_cls=NoConnection)
        it.start()
        try:
            assert sorted(it._dirs) == ["hear"]
            assert any(e.get("code") == "start" and e.get("direction") == "speak" for e in ev)
            speak_mic = [c for c in audio.captures if not c.loopback][0]
            assert speak_mic.started and speak_mic.stopped
            assert [s for s in FakeSTT.made if s.language == "en"][0].closed
            assert [v for v in FakeVoice.made if v.language == "ja"][0].closed
            # switched on again later: it says so again, and nothing is raised
            it.set_direction("speak", True)
            assert sum(1 for e in ev if e.get("code") == "start" and e.get("direction") == "speak") == 2
        finally:
            it.stop()

    def test_nothing_starts_raises_with_the_reason(self):
        audio = FakeAudio()
        audio.fail_mic = OSError(-9996, "Invalid device")
        it, _ev = make(settings=controller.Settings(my_lang="en", their_lang="ja", hear=False), audio=audio)
        with pytest.raises(InterpreterError) as e:
            it.start()
        assert e.value.code == "no_mic_sound" and not it.running
        assert all(s.stopped for s in audio.sinks if s.started)

    def test_dictionary_words_stay_on_the_pc_when_asked(self):
        words = ["Anna Petrosyan", "anna petrosyan", ""]
        local = {"vocabulary": words, "vocabulary_share_with_cloud": False}
        assert controller.Settings.from_config(local).glossary == []
        assert controller.Settings.from_config({"vocabulary": words, "privacy_mode": True}).glossary == []
        assert controller.Settings.from_config({"vocabulary": words}).glossary == ["Anna Petrosyan"]

    def test_speak_records_the_mic_picked_in_gusan(self):
        s = controller.Settings.from_config({"input_device_index": 8, "interpreter_my_language": "en",
                                             "interpreter_their_language": "ja"})
        assert s.mic == 8
        assert controller.Settings.from_config({"input_device_index": "smart_meeting"}).mic is None
        audio = FakeAudio()
        it, _ev = make(settings=s, audio=audio)
        it.start()
        try:
            assert it._dirs["speak"].capture.device == 8
        finally:
            it.stop()

    def test_sleep_is_never_billed_and_a_long_sleep_stops_it(self, monkeypatch):
        import time as _t
        monkeypatch.setattr(controller, "HEARTBEAT_S", 0.3)
        monkeypatch.setattr(controller, "SLEEP_GAP_S", 0.6)
        monkeypatch.setattr(controller, "LONG_SLEEP_S", 3.0)
        slept = {"s": 0.0}
        sess = FakeSession()
        it, ev = make(sess=sess, clock=lambda: _t.monotonic() + slept["s"])
        it.start()
        try:
            assert wait_until(lambda: len(sess.beats) >= 1, timeout=3)
            slept["s"] += 1.5                                  # a short sleep: not billed, it goes on
            n = len(sess.beats)
            assert wait_until(lambda: len(sess.beats) >= n + 2, timeout=3)
            assert it.running and max(sess.beats) < 1.0
            slept["s"] += 8 * 3600                             # closed for the night
            assert wait_until(lambda: any(e.get("type") == "session" and e.get("state") == "stopped"
                                          for e in ev), timeout=3)
        finally:
            it.stop()
        assert max(sess.beats) < 1.0 and sess.stopped_with < 1.0
        assert [e for e in ev if e.get("type") == "session" and e.get("state") == "stopped"][0]["reason"] == "sleep"

    def test_stop_puts_the_call_volume_back_first(self):
        audio = FakeAudio()
        it, _ev = make(audio=audio)
        it.start()
        duck = audio.duckers[0]
        seen = []
        for c in audio.captures:
            c.stop = lambda: seen.append(duck.closed)
        it.stop()
        assert duck.closed and seen and all(seen)

    def test_quit_puts_the_call_volume_back_even_while_a_switch_is_connecting(self):
        import threading
        connecting = threading.Event()
        slow = {"on": False}

        class SlowSTT(FakeSTT):
            def start(self):
                if self.language == "en" and slow["on"]:
                    connecting.wait(5)
        audio = FakeAudio()
        it, _ev = make(audio=audio, stt_cls=SlowSTT)
        it.start()
        duck = audio.duckers[0]
        it.set_direction("speak", False)
        slow["on"] = True
        switch = threading.Thread(target=it.set_direction, args=("speak", True), daemon=True)
        switch.start()                                   # holds the controller while it connects
        quit_ = threading.Thread(target=it.stop, args=("quit",), daemon=True)
        try:
            assert wait_until(lambda: len([s for s in FakeSTT.made if s.language == "en"]) == 2)
            quit_.start()
            assert wait_until(lambda: duck.closed, timeout=1)    # at once, not after the switch
        finally:
            connecting.set()
            switch.join(5)
            quit_.join(5)
        assert not it.running

    @pytest.mark.parametrize("code,beats", [("network", 3), ("auth", 3), ("pro_required", 1)])
    def test_a_session_nobody_can_bill_stops(self, monkeypatch, code, beats):
        monkeypatch.setattr(controller, "HEARTBEAT_S", 0.05)
        sess = FakeSession()
        calls = []

        def beat(seconds):
            calls.append(seconds)
            raise InterpreterError(code)
        sess.heartbeat = beat
        it, ev = make(sess=sess)
        it.start()

        def stopped():
            return [e for e in ev if e.get("type") == "session" and e.get("state") == "stopped"]
        assert wait_until(lambda: bool(stopped()), timeout=3)
        assert len(calls) == beats and not it.running
        assert stopped()[0]["reason"] == code
        assert any(e.get("type") == "error" and e.get("code") == code for e in ev)

    def test_speak_switched_on_later_speaks_in_my_clone(self):
        store = FakeVoiceStore({"voice_id": "my-voice", "source": "record"})
        it, _ev = make(settings=controller.Settings(my_lang="en", their_lang="ja", speak=False),
                       sess=ServerLikeSession(), voice_store=store)
        it.start()
        try:
            it.set_direction("speak", True)
            assert [v for v in FakeVoice.made if v.language == "ja"][-1].voice_id == "my-voice"
            assert it._dirs["speak"].uses_choice is False
            it.set_direction("speak", False)
            it.set_direction("speak", True)
            assert store.calls == 1                         # asked once
        finally:
            it.stop()

    def test_both_directions_capture_at_once_and_keep_what_is_said_meanwhile(self):
        import threading
        connect, warm = threading.Event(), threading.Event()

        class SlowSTT(FakeSTT):
            def start(self):
                if self.language == "ja":                   # Hear's speech-to-text is still connecting
                    connect.wait(5)

        class SlowVoice(FakeVoice):
            def open(self):
                super().open()
                if self.language == "en":                   # Hear's voice is still warming up
                    warm.wait(5)
        audio = FakeAudio()
        it, _ev = make(audio=audio, stt_cls=SlowSTT, voice_cls=SlowVoice)
        t = threading.Thread(target=it.start, daemon=True)
        t.start()
        try:
            assert wait_until(lambda: any(c.started for c in audio.captures if not c.loopback))   # Speak listens
            hear_loop = [c for c in audio.captures if c.loopback][0]
            assert hear_loop.started and t.is_alive()        # Hear too, before its connections are up
            said = b"\x05\x00" * 1600
            hear_loop.on_pcm(said)                           # the other side talks right away
            connect.set()
            t.join(5)
            # F28: running once the speech-to-text is up - the voice's warm-up never holds it up ...
            assert not t.is_alive() and it.running and not warm.is_set()
            more = b"\x06\x00" * 1600
            hear_loop.on_pcm(more)                           # ... so what they say now goes on at once
            hear_stt = [s for s in FakeSTT.made if s.language == "ja"][0]
            heard = b"".join(hear_stt.audio)
            assert heard.startswith(said) and heard.endswith(more)
        finally:
            connect.set()
            warm.set()
            t.join(5)
            it.stop()

    def test_hear_switched_on_later_turns_the_call_down(self):
        audio = FakeAudio()
        it, _ev = make(settings=controller.Settings(my_lang="en", their_lang="ja", hear=False), audio=audio)
        it.start()
        try:
            assert audio.duckers == []
            it.set_direction("hear", True)
            assert len(audio.duckers) == 1
            it._dirs["hear"].sink.playing = True
            assert wait_until(lambda: audio.duckers[0].ducks == 1)
        finally:
            it.stop()

    def test_speak_off_keeps_my_own_voice_in_the_call(self):
        audio = FakeAudio(out_name="Headphones (WH-1000XM5)")
        it, _ev = make(audio=audio)
        it.start()
        try:
            it.set_direction("speak", False)
            mic = [c for c in audio.captures if not c.loopback][-1]
            into_call = audio.sinks[-1]
            assert mic.started and into_call.started and into_call.device == 7
            me = tone(0.05, amp=3000).tobytes()
            mic.on_pcm(me)
            assert into_call.played == [me]                  # the call still hears me, as I am
            it.set_direction("speak", True)                  # Speak takes the mic again
            assert mic.stopped and into_call.stopped
        finally:
            it.stop()
        # Hear only, from the start: the same
        audio = FakeAudio(out_name="Headphones (WH-1000XM5)")
        it, _ev = make(settings=controller.Settings(my_lang="en", their_lang="ja", speak=False), audio=audio)
        it.start()
        try:
            assert audio.sinks[-1].device == 7 and audio.sinks[-1].started
        finally:
            it.stop()
        assert all(c.stopped for c in audio.captures) and all(s.stopped for s in audio.sinks)

    def test_hear_listeners_get_what_the_hear_output_plays(self):
        it, _ev = make()
        got = []
        it.hear_listeners.append(got.append)
        it.start()
        try:
            it._dirs["hear"].sink.on_played(b"\x01\x00" * 320)
            assert got == [b"\x01\x00" * 320]
        finally:
            it.stop()

    def test_stop_releases_the_sound_system(self):
        audio = FakeAudio()
        it, _ev = make(audio=audio)
        it.start()
        it.stop()
        assert audio.closed

    def test_hear_follows_windows_to_a_new_headset(self, monkeypatch):
        monkeypatch.setattr(controller, "WATCH_S", 0.01)
        monkeypatch.setattr(controller, "DEVICE_LOOK_S", 0.02)
        audio = FakeAudio(out_name="Speakers (Realtek)", out_index=3)
        it, ev = make(audio=audio)
        it.start()
        try:
            assert it.speakers_mode is True
            hear, speak = it._dirs["hear"], it._dirs["speak"]
            audio.out_name, audio.out_index = "Headphones (AirPods Pro)", 4     # Windows moved to them
            assert wait_until(lambda: it._dirs.get("hear") not in (None, hear)
                              and it._dirs.get("speak") not in (None, speak), timeout=3)
            assert it._dirs["hear"].sink.device == 4 and it._dirs["hear"].capture.device == 4
            assert it.speakers_mode is False and it._dirs["speak"].gate.enabled is False
            assert it._dirs["speak"].monitor is None
            assert hear.sink.stopped and hear.capture.stopped and speak.capture.stopped
            assert wait_until(lambda: any(e.get("type") == "session" and e.get("speakers_mode") is False
                                          for e in ev))
        finally:
            it.stop()

    def test_a_device_that_goes_away_is_tried_again_once_then_stopped(self, monkeypatch):
        monkeypatch.setattr(controller, "WATCH_S", 0.01)
        audio = FakeAudio()
        it, ev = make(audio=audio)
        it.start()
        try:
            hear = it._dirs["hear"]
            hear.sink.stall = True                           # a glitch: opened again
            assert wait_until(lambda: it._dirs.get("hear") not in (None, hear), timeout=3)
            assert hear.sink.stopped and hear.capture.stopped
            it._dirs["hear"].sink.stall = True               # gone again at once: it stops and says so
            assert wait_until(lambda: "hear" not in it._dirs, timeout=3)
            assert any(e.get("code") == "device_lost" and e.get("direction") == "hear" for e in ev)
            assert it.running and sorted(it._dirs) == ["speak"]
        finally:
            it.stop()

    def test_speech_service_gone_is_passed_on_with_its_direction(self):
        it, ev = make()
        it.start()
        try:
            [s for s in FakeSTT.made if s.language == "ja"][0].on_event("closed", "")
            # the host reacts to it (a fresh session, or a stop that says so)
            assert any(e.get("type") == "state" and e.get("state") == "stt_closed"
                       and e.get("direction") == "hear" for e in ev)
            assert it.running and sorted(it._dirs) == ["hear", "speak"]
        finally:
            it.stop()
