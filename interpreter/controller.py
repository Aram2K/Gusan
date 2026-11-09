"""The interpreter as one object the UI switches on and off: both directions, their sound, the
server session and the monthly time. No Qt: events go to on_event(dict) on worker threads (the
UI marshals them to its own thread).

    hear:  call sound (loopback) -> minus Gusan's own voice -> gate -> pipeline -> headphones/speakers
    speak: mic -> gate (speakers mode) -> pipeline (your own voice) -> the virtual microphone

Either direction can be switched on or off while running (the switches on the live card). While
Speak is off, the mic goes into the virtual microphone as it is, so a call that listens to it
still hears you. The two directions start side by side, each capturing at once: what is said
while the speech service connects is kept and sent on. A direction that can't start is reported
(an error event) and the other keeps going. Devices follow Windows: a headset that connects or
goes away moves the directions to the device Windows uses now. Time the computer spent asleep is
never billed, and a long sleep stops it.
"""
import logging
import threading
import time

import vocabulary
from interpreter import audio_io, eleven, guard
from interpreter.pipeline import DirectionPipeline
from interpreter.pitch import GenderGuess, PitchTracker
from interpreter.voices import MATCH as VOICES_MATCH, VoiceChoice
from interpreter.session import InterpreterError, InterpreterSession, VoiceStore
from interpreter.translate import translate_phrase, warm_up as warm_up_translator

logger = logging.getLogger(__name__)

HEARTBEAT_S = 30.0
LOW_TIME_S = 5 * 60
SLEEP_GAP_S = 10.0           # the heartbeat thread missed this long: the computer slept (not billed)
LONG_SLEEP_S = 120.0         # asleep this long: the call is over, the interpreter stops
MAX_FAILED_BEATS = 3         # this many beats in a row unanswered (signed out, offline): it stops
EARLY_S = 10.0               # sound kept while the speech service connects
HOLD_S = 0.15                # a gate stays open this long after someone talks over Gusan
BARGE_IN_RMS = 2500.0        # speakers mode: the mic counts as talking over the speakers when it is
ECHO_ROOM = 0.5              # this loud, and at least half as loud as what the speakers play
WATCH_S = 0.25               # how often the devices are checked for a stalled stream...
DEVICE_LOOK_S = 2.0          # ...and for Windows using another device
RETRY_BROKEN_S = 30.0        # a direction whose device broke again this soon stops instead


class Settings:
    def __init__(self, my_lang="en", their_lang="hy", hear=True, speak=True, output="auto",
                 duck=True, duck_level=0.25, vad_silence=0.4, glossary=(), captions_only=False,
                 voice_choice=None, own_voice=True, mic=None):
        self.my_lang = my_lang
        self.their_lang = their_lang
        self.hear = hear
        self.speak = speak
        self.output = output                  # "auto" | "headphones" | "speakers"
        self.duck = duck
        self.duck_level = duck_level
        self.vad_silence = vad_silence
        self.glossary = list(glossary)
        self.captions_only = captions_only
        self.voice_choice = voice_choice or VoiceChoice()   # the voices the others are heard in
        self.own_voice = own_voice                          # speak in my cloned voice (when I have one)
        self.mic = mic                                      # the mic picked in Gusan; None = Windows'

    @classmethod
    def from_config(cls, cfg):
        mic = cfg.get("input_device_index")
        return cls(my_lang=cfg.get("interpreter_my_language") or "en",
                   their_lang=cfg.get("interpreter_their_language") or "hy",
                   hear=bool(cfg.get("interpreter_hear", True)),
                   speak=bool(cfg.get("interpreter_speak", True)),
                   output=cfg.get("interpreter_output") or "auto",
                   duck=bool(cfg.get("interpreter_duck", True)),
                   duck_level=0.0 if not cfg.get("interpreter_keep_underneath", True) else 0.25,
                   # the Dictionary words leave the PC only when that is allowed (privacy)
                   glossary=vocabulary.cloud_terms(cfg),
                   voice_choice=VoiceChoice.from_config(cfg),
                   own_voice=bool(cfg.get("interpreter_voice_on", True)),
                   mic=mic if isinstance(mic, int) and not isinstance(mic, bool) else None)


class WindowsAudio:
    """The real devices, looked up when they are opened: the ones Windows uses now, never the
    virtual microphone itself. Tests pass a fake with the same methods."""

    def __init__(self):
        self._pa = None
        self._lock = threading.RLock()     # every use of the sound system, and close(), one at a time
        self._closed = False

    def _ensure(self):
        """(with the lock held)"""
        if self._closed:
            raise RuntimeError("the interpreter's sound is closed")
        if self._pa is None:
            self._pa = audio_io._pyaudio().PyAudio()
        return self._pa

    def output_info(self):
        """The output Hear plays on and records the call from: the one Windows uses now.
        {"index", "name", "kind"}; kind is "headphones" or "speakers"."""
        with self._lock:
            pa = self._ensure()
            live, form = audio_io.windows_default(output=True)
            idx = audio_io.pick_device(pa, output=True, name=live)
            name = ""
            if idx is not None:
                try:
                    name = audio_io._name(pa.get_device_info_by_index(idx))
                except Exception:  # noqa: BLE001
                    name = ""
        if live and name != live:
            # Windows plays on a device this sound system can't see (connected after it started),
            # or on the virtual microphone: its form factor says nothing about this one
            logger.info("interpreter: Windows plays on %r, using %r", live, name)
            form = None
        return {"index": idx, "name": name or live, "kind": audio_io.guess_output_kind(name or live, form)}

    def default_output_name(self):
        try:
            return self.output_info()["name"]
        except Exception:  # noqa: BLE001
            return ""

    def mic_index(self, wanted=None):
        """The microphone Speak records: the one picked in Gusan, else the one Windows uses now."""
        with self._lock:
            pa = self._ensure()
            live, _form = audio_io.windows_default(output=False)
            return audio_io.pick_device(pa, output=False, wanted=wanted, name=live)

    def capture(self, on_pcm, loopback, device=None):
        """loopback: what the output ``device`` plays (the call); else the microphone ``device``."""
        with self._lock:
            pa = self._ensure()
            if loopback:
                out = device if device is not None else self.output_info()["index"]
                idx = audio_io.loopback_of(pa, out)
            else:
                idx = device if device is not None else self.mic_index()
                if idx is None:
                    raise InterpreterError("no_mic_sound", "No microphone.")
            return audio_io.CaptureSource(on_pcm, loopback=loopback, device_index=idx, pa=pa)

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
        return audio_io.Ducker(level=level)

    def close(self):
        """Release the sound system (a new start opens it again and sees today's devices)."""
        with self._lock:
            self._closed = True
            pa, self._pa = self._pa, None
            if pa is None:
                return
            try:
                pa.terminate()
            except Exception:  # noqa: BLE001
                pass


class _Early:
    """Sound captured before the speech service is connected: kept (the last EARLY_S) and sent on
    once it is, so words said while the interpreter starts are not lost."""

    def __init__(self, feed):
        self._feed = feed
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._ready = False
        self._limit = int(EARLY_S * audio_io.RATE) * 2

    def __call__(self, pcm):
        with self._lock:
            if not self._ready:
                self._buf.extend(pcm)
                del self._buf[:max(0, len(self._buf) - self._limit)]
                return
        self._feed(pcm)

    def ready(self):
        with self._lock:                        # in order: the next chunk waits for this one
            buf, self._buf = bytes(self._buf), bytearray()
            if buf:
                self._feed(buf)
            self._ready = True


class _Direction:
    """One direction: its pipeline, its capture and its playback (and in speakers mode, Speak's
    ear on the speakers)."""

    def __init__(self, name, voice=None, uses_choice=False):
        self.name = name
        self.voice = voice
        self.uses_choice = uses_choice      # speaks in the chosen preset voices (not my own clone)
        self.pipeline = self.capture = self.sink = self.gate = self.canceller = None
        self.monitor = None                 # speak, speakers mode: what the speakers play
        self.early = None
        self.out = self.mic = None          # the devices it was opened on (the watch compares)
        self.step = "start"                 # what it was doing when it failed (the error code)
        self.restarted_at = None

    def stop(self):
        for part in (self.capture, self.monitor, self.pipeline, self.sink):
            if part is None:
                continue
            try:
                part.stop()
            except Exception:  # noqa: BLE001
                logger.debug("interpreter direction stop failed", exc_info=True)

    def broken(self):
        """Its device is gone: the sink stopped asking for sound, or a capture keeps failing."""
        stalled = getattr(self.sink, "stalled", None)
        return bool((callable(stalled) and stalled()) or getattr(self.capture, "lost", False)
                    or getattr(self.monitor, "lost", False))


class Interpreter:
    def __init__(self, token_getter, on_event, settings, *, audio=None, session=None,
                 translate=None, stt_cls=None, voice_cls=None, clock=time.monotonic, voice_store=None,
                 warm_up=None):
        self._token_getter = token_getter
        self._on_event = on_event or (lambda e: None)
        self.settings = settings
        self.audio = audio or WindowsAudio()
        self.session = session or InterpreterSession(token_getter)
        self._translate = translate or translate_phrase
        # Gusan Cloud's translator is warmed up as each direction starts; another translator (a
        # test, the bench) only with a warm_up(token_getter) of its own
        self._warm_up = warm_up or (None if translate else warm_up_translator)
        self._stt_cls = stt_cls or eleven.ScribeRealtime
        self._voice_cls = voice_cls or eleven.DialogueVoice
        self._voice_store = voice_store
        self._clock = clock
        self._dirs = {}
        self._lock = threading.RLock()
        self._devices = threading.Lock()   # one device opens at a time (the directions start side by side)
        self._running = False
        self._started_at = 0.0
        self._billed_at = 0.0
        self._seen_at = 0.0                 # the heartbeat thread's last look at the clock
        self._paused = 0.0                  # seconds asleep since the last beat (not billed)
        self._beat = None
        self._watch = None
        self._duck = None
        self._duck_watch = None
        self._pass = None                   # (capture, sink): the mic into the virtual mic while Speak is off
        self._out_index = None
        self._voices = {}
        self._own_voice = False
        self._asked_speak = False
        self._voice_checked = False
        self.speakers_mode = False
        self.warned_low = False
        # fn(pcm16k) for everything the hear output plays (silence too, in step with the device):
        # whoever records the same output (a meeting) can take Gusan's voice out of it
        self.hear_listeners = []

    # -- public ----------------------------------------------------------------------------------
    @property
    def running(self):
        return self._running

    def start(self):
        """Start both enabled directions. Raises InterpreterError (pro_required, allowance_used,
        ...) before anything is opened; a missing virtual microphone only turns Speak off. A
        direction that can't start is reported and the other one runs; with none, it raises."""
        s = self.settings
        with self._lock:
            if self._running:
                return
            speak_ok = s.speak and self.audio.virtual_mic_output() is not None
            if s.speak and not speak_ok:
                self._emit({"type": "error", "code": "no_virtual_mic", "direction": "speak"})
            if not s.hear and not speak_ok:
                raise InterpreterError("bad_request", "Turn on at least one direction.")
            try:
                out = self._output_info()
            except Exception:  # noqa: BLE001 - speakers: the safe guess (the mic is gated)
                logger.warning("interpreter: the output could not be read", exc_info=True)
                out = {"index": None, "name": "", "kind": "speakers"}
            info = self.session.start(hear=(s.their_lang, s.my_lang) if s.hear else None,
                                      speak=(s.my_lang, s.their_lang) if speak_ok else None)
            self._voices = dict(info.get("voices") or {})
            self._own_voice = bool(info.get("own_voice"))
            self._asked_speak = speak_ok
            self._running = True
            self._started_at = self._billed_at = self._seen_at = self._clock()
            self._paused = 0.0
            self._set_output(out)
            names = [n for n, on in (("hear", s.hear), ("speak", speak_ok)) if on]
            failed = self._open_all(names, info)
            if not self._dirs:
                self._stop_all("failed")
                raise InterpreterError(failed[0][1] if failed else "start") from (failed[0][2] if failed else None)
            for name, code, _e in failed:
                self._emit({"type": "error", "code": code, "direction": name})
            self._start_pass()
            self._ensure_ducker()
            self._beat = threading.Thread(target=self._heartbeat_loop, name="interp-heartbeat", daemon=True)
            self._beat.start()
            self._watch = threading.Thread(target=self._watch_loop, name="interp-devices", daemon=True)
            self._watch.start()
        self._emit({"type": "session", "state": "running", "seconds_left": self.session.seconds_left,
                    "speakers_mode": self.speakers_mode, "directions": sorted(self._dirs)})

    def stop(self, reason="user"):
        duck = self._duck
        if duck is not None:
            duck.close()           # the call's volume first, even while a switch holds the lock (Quit waits 1.5 s)
        with self._lock:
            if not self._running:
                return
            self._stop_all(reason)

    def set_direction(self, name, on):
        """Switch Hear or Speak on/off while running. One that can't start says so (an error
        event) and the other keeps going."""
        with self._lock:
            setattr(self.settings, name, on)
            if not self._running:
                return
            if not on and name in self._dirs:
                self._dirs.pop(name).stop()
                if name == "speak":
                    self._start_pass()
            elif on and name not in self._dirs:
                if name == "speak" and self.audio.virtual_mic_output() is None:
                    self._emit({"type": "error", "code": "no_virtual_mic", "direction": "speak"})
                    return
                if name == "speak":
                    self._stop_pass()                     # the mic goes to Speak now
                for n, code, _e in self._open_all([name], None):
                    self._emit({"type": "error", "code": code, "direction": n})
                self._start_pass()
                self._ensure_ducker()
        self._emit({"type": "session", "state": "running", "directions": sorted(self._dirs)})

    def set_voice_choice(self, choice):
        """The person picked other voices: used from the next phrase."""
        with self._lock:
            self.settings.voice_choice = choice
            for d in self._dirs.values():
                if d.uses_choice and d.voice is not None and hasattr(d.voice, "set_voices"):
                    d.voice.set_voices(choice.all_ids())

    def latency(self):
        """The delay to show: the larger of the two directions' recent medians."""
        vals = [d.pipeline.recent_latency() for d in list(self._dirs.values())]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    # -- directions ------------------------------------------------------------------------------
    def _output_info(self):
        """{"index", "name", "kind"} of the output Windows uses now (a simpler audio, like the
        bench's, may only know the default output's name)."""
        info = getattr(self.audio, "output_info", None)
        if callable(info):
            return info()
        name = self.audio.default_output_name()
        return {"index": None, "name": name, "kind": audio_io.guess_output_kind(name)}

    def _mic_index(self):
        find = getattr(self.audio, "mic_index", None)
        return find(self.settings.mic) if callable(find) else self.settings.mic

    def _set_output(self, out):
        """The output Windows uses now: Hear plays on it and records the call from it."""
        s = self.settings
        self._out_index = out.get("index")
        self.speakers_mode = s.output == "speakers" or (s.output == "auto" and out.get("kind") == "speakers")

    def _hear_playing(self, tail_s=0.3):
        """Gusan's hear voice is playing (looked up now: Hear may have been switched off and on)."""
        h = self._dirs.get("hear")
        try:
            return bool(h is not None and h.sink.is_playing(tail_s=tail_s))
        except Exception:  # noqa: BLE001
            return False

    def _own_voice_id(self):
        """My cloned voice for Speak, when I have one and want it: from the start answer, else asked
        for once (the answer has it only when Speak was on at the start)."""
        if not self.settings.own_voice:
            return None
        vid = self._voices.get("speak") if self._own_voice else None
        if vid or self._asked_speak or self._voice_checked:
            return vid
        self._voice_checked = True
        try:
            store = self._voice_store or VoiceStore(self._token_getter)
            v = store.get() or {}
            vid = v.get("voice_id") if isinstance(v, dict) else None
        except Exception as e:  # noqa: BLE001 - the chosen voices still work
            logger.info("interpreter: my voice could not be read: %s", e)
            vid = None
        if vid:
            self._voices["speak"], self._own_voice = vid, True
        return vid

    def _played_by(self, canceller):
        def played(pcm):
            canceller.reference(pcm)
            for fn in list(self.hear_listeners):
                try:
                    fn(pcm)
                except Exception:  # noqa: BLE001
                    logger.debug("interpreter hear listener failed", exc_info=True)
        return played

    def _build(self, name, info):
        """One direction, nothing opened yet."""
        s = self.settings
        src, dst = (s.their_lang, s.my_lang) if name == "hear" else (s.my_lang, s.their_lang)
        first = {"scribe": None, "tts": None}
        if info:
            first["scribe"] = (info.get("scribe_tokens") or {}).get(name)
            first["tts"] = (info.get("tts_tokens") or {}).get(name)
        own_id = self._own_voice_id() if name == "speak" else None

        def token_for(kind):
            def get():
                tok = first.get(kind)
                if tok:
                    first[kind] = None             # single-use: the next call asks the server
                    return tok
                return self.session.token(kind, name)
            return get

        scribe_token = token_for("scribe")
        # Speak goes out in my own cloned voice when I have one and want it; everything else (the
        # others, or me without a clone) in the chosen voices, a woman's or a man's to match
        # whoever is talking (pitch.py), or one voice for everyone.
        uses_choice = not own_id
        voice_ids = s.voice_choice.all_ids() if uses_choice else [own_id]
        voice = self._voice_cls(token_for("tts"), voice_ids if len(voice_ids) > 1 else voice_ids[0], dst)
        d = _Direction(name, voice, uses_choice)
        # The woman's / man's voice is decided ONCE per call (pitch.GenderGuess): pitch is tracked
        # only while that is still open, the voices match the speaker and this direction is voiced -
        # then the tracker is cleared and never fed again (no per-chunk work for the rest of the call).
        tracker, guess = PitchTracker(), GenderGuess()
        voiced_out = name == "speak" or not s.captions_only

        def wants_pitch():
            return (uses_choice and voiced_out and not guess.decided
                    and self.settings.voice_choice.mode == VOICES_MATCH)

        def phrase_info(now):
            if wants_pitch():
                hz, n = tracker.typical(float("-inf"), now)  # all this call's voice so far (kept 30 s)
                if guess.judge(hz, n) is not None:
                    tracker.clear()
            return {"gender": guess.last}

        def voice_for(ph):
            return self.settings.voice_choice.for_gender(ph.gender)
        glossary = list(s.glossary)
        token_getter = self._token_getter

        def translate(text, a, b, ctx, on_delta):
            return self._translate(text, a, b, ctx, on_delta, token=token_getter(), glossary=glossary)
        warm = None
        if self._warm_up is not None:
            def warm():
                self._warm_up(token_getter)        # the token is read on its side thread too

        def stt_factory(on_stt):
            return self._stt_cls(scribe_token(), src, on_stt, token_provider=scribe_token,
                                 keyterms=glossary, vad_silence=s.vad_silence)

        d.out = self._out_index
        if name == "hear":
            d.canceller = canceller = audio_io.OwnVoiceCanceller()
            d.sink = self.audio.playback(d.out, on_played=self._played_by(canceller))
            # Our own voice is subtracted; while Gusan talks, what is left passes when it is more
            # than our leftovers (they talk over the translation) - muted otherwise, and until our
            # voice has been found in the capture.
            d.gate = audio_io.ListenGate(d.sink.is_playing, barge_in=lambda _pcm: canceller.their_voice(),
                                         hold_s=HOLD_S)
        else:
            vmic = self.audio.virtual_mic_output()
            if vmic is None:                      # never the translated voice into my own ears
                raise InterpreterError("no_virtual_mic")
            d.sink = self.audio.playback(vmic)
            # Speakers mode: the mic hears whatever the speakers play - the hear voice and the call
            # itself - so it is muted while they play, unless I clearly talk over them.
            speakers = audio_io.SoundLevel()
            d.gate = audio_io.ListenGate(
                lambda: self._hear_playing() or speakers.active(),
                barge_in=lambda pcm: audio_io.rms(pcm) >= max(BARGE_IN_RMS, ECHO_ROOM * speakers.level()),
                enabled=self.speakers_mode, hold_s=HOLD_S)
        # Made-up text is caught before a phrase is queued (guard.py); on Hear, a phrase with almost
        # none of their voice under it while Gusan's voice played is shown, not voiced.
        d.pipeline = pipe = DirectionPipeline(name, src, dst, stt_factory=stt_factory, translate=translate,
                                              voice=voice, sink=d.sink, on_event=self._emit, clock=self._clock,
                                              speak_enabled=not s.captions_only or name == "speak",
                                              phrase_info=phrase_info if uses_choice else None,
                                              voice_for=voice_for if uses_choice else None,
                                              guard=guard.Guard(src, glossary),
                                              min_voice_s=guard.MIN_VOICE_S if name == "hear" else 0.0,
                                              warm_up=warm)
        d.early = early = _Early(pipe.feed)
        gate = d.gate
        if name == "hear":
            def on_pcm(pcm):
                clean = gate.process(canceller.process(pcm))
                if wants_pitch():                        # only until the voice is decided
                    tracker.feed(clean, self._clock())
                early(clean)
            d.capture = self.audio.capture(on_pcm, loopback=True, device=d.out)
        else:
            def on_pcm(pcm):
                clean = gate.process(pcm)
                if wants_pitch():
                    tracker.feed(clean, self._clock())
                early(clean)
            d.mic = self._mic_index()
            d.capture = self.audio.capture(on_pcm, loopback=False, device=d.mic)
            if self.speakers_mode:
                d.monitor = self.audio.capture(speakers.feed, loopback=True, device=d.out)
        return d

    def _run(self, d):
        """Start a built direction: its devices first (capturing at once), then its connections.
        Whatever already started is stopped again when a step fails."""
        started = []
        try:
            with self._devices:
                for part, step in ((d.sink, "start"), (d.capture, "no_mic_sound" if d.name == "speak" else "start"),
                                   (d.monitor, "start")):
                    if part is not None:
                        d.step = step
                        started.append(part)
                        part.start()
            d.step = "start"
            started.append(d.pipeline)
            d.pipeline.start()
            d.early.ready()
        except Exception:
            for part in reversed(started):
                try:
                    part.stop()
                except Exception:  # noqa: BLE001
                    logger.debug("interpreter cleanup failed", exc_info=True)
            raise
        self._dirs[d.name] = d

    def _open_all(self, names, info):
        """Build the directions, then start them side by side (each waits on its own connections).
        Returns [(name, error code, exception)] for those that could not start."""
        built, failed = [], []
        for n in names:
            try:
                built.append(self._build(n, info))
            except Exception as e:  # noqa: BLE001
                logger.warning("interpreter: %s could not be set up: %s", n, e)
                failed.append((n, e.code if isinstance(e, InterpreterError) else "start", e))

        def run(d):
            try:
                self._run(d)
            except Exception as e:  # noqa: BLE001
                logger.warning("interpreter: %s could not start: %s", d.name, e)
                failed.append((d.name, e.code if isinstance(e, InterpreterError) else d.step, e))
        threads = [threading.Thread(target=run, args=(d,), name="interp-open-" + d.name, daemon=True)
                   for d in built[1:]]
        for t in threads:
            t.start()
        if built:
            run(built[0])
        for t in threads:
            t.join()
        return failed

    def _start_pass(self):
        """Speak is off: the mic goes into the virtual microphone as it is, so a call that listens
        to it still hears me (untranslated). Gusan's own hear voice never goes in (speakers mode)."""
        if self._pass is not None or "speak" in self._dirs or not self._running:
            return
        vmic = self.audio.virtual_mic_output()
        if vmic is None:
            return
        try:
            sink = self.audio.playback(vmic)
            gate = audio_io.ListenGate(self._hear_playing, enabled=self.speakers_mode)

            def on_pcm(pcm):
                if sink.queued_seconds() > 0.25:      # the two devices' clocks drift: never fall behind
                    sink.clear()
                sink.play(gate.process(pcm))
            mic = self._mic_index()
            capture = self.audio.capture(on_pcm, loopback=False, device=mic)
        except Exception as e:  # noqa: BLE001
            logger.warning("interpreter: the mic can't go into the virtual microphone: %s", e)
            return
        started = []
        try:
            with self._devices:
                for part in (sink, capture):
                    started.append(part)
                    part.start()
        except Exception as e:  # noqa: BLE001
            logger.warning("interpreter: the mic can't go into the virtual microphone: %s", e)
            for part in reversed(started):
                try:
                    part.stop()
                except Exception:  # noqa: BLE001
                    pass
            return
        self._pass = (capture, sink, mic)

    def _stop_pass(self):
        p, self._pass = self._pass, None
        for part in (p or ())[:2]:
            try:
                part.stop()
            except Exception:  # noqa: BLE001
                logger.debug("interpreter pass-through stop failed", exc_info=True)

    # -- devices ---------------------------------------------------------------------------------
    def _watch_loop(self):
        """A direction whose device went away, or Windows using another device now (a headset
        connected or gone): that direction starts again on the device Windows uses, or stops."""
        next_look = time.monotonic() + DEVICE_LOOK_S
        while self._running:
            time.sleep(WATCH_S)
            if not self._running:
                return
            broken = [d for d in list(self._dirs.values()) if d.broken()]
            now = time.monotonic()
            if not broken and now < next_look:
                continue
            next_look = now + DEVICE_LOOK_S
            try:
                out = self._output_info()
                mic = self._mic_index()
            except Exception:  # noqa: BLE001
                logger.debug("interpreter device check failed", exc_info=True)
                out = mic = None
            self._follow_devices(out, mic, broken)

    def _follow_devices(self, out, mic, broken):
        with self._lock:
            if not self._running:
                return
            was = self.speakers_mode
            moved = out is not None and out.get("index") != self._out_index
            if moved:
                self._set_output(out)
            now = time.monotonic()
            redo, lost = [], []
            for d in list(self._dirs.values()):
                if d.name == "hear":
                    again = moved
                else:          # Speak's gate and its ear on the speakers follow the output too
                    again = (moved and (was or self.speakers_mode)) or (mic is not None and d.mic != mic)
                if again:
                    redo.append(d)
                elif d in broken:
                    if d.restarted_at is not None and now - d.restarted_at < RETRY_BROKEN_S:
                        lost.append(d)
                    else:
                        redo.append(d)            # the same device once more (a glitch)
            pass_moved = self._pass is not None and mic is not None and self._pass[2] != mic
            if not (redo or lost or moved or pass_moved):
                return
            for d in lost:
                self._dirs.pop(d.name).stop()
                mic_gone = d.name == "speak" and getattr(d.capture, "lost", False)
                self._emit({"type": "error", "code": "no_mic_sound" if mic_gone else "device_lost",
                            "direction": d.name})
            for d in redo:
                logger.info("interpreter: %s moves to the device Windows uses now", d.name)
                self._dirs.pop(d.name).stop()
                for n, code, _e in self._open_all([d.name], None):
                    self._emit({"type": "error", "code": code, "direction": n})
                if d.name in self._dirs:
                    self._dirs[d.name].restarted_at = now if d in broken else None
            if moved or pass_moved:
                self._stop_pass()
            self._start_pass()
            if not self._dirs:
                self._stop_all("device_lost")
                return
            self._ensure_ducker()
        self._emit({"type": "session", "state": "running", "speakers_mode": self.speakers_mode,
                    "directions": sorted(self._dirs)})

    # -- the call's volume ------------------------------------------------------------------------
    def _ensure_ducker(self):
        """Hear runs: turn the call down while it talks (also when Hear was switched on later)."""
        if not (self.settings.duck and self._running and self._duck is None and "hear" in self._dirs):
            return
        self._duck = duck = self.audio.ducker(self.settings.duck_level)
        self._duck_watch = threading.Thread(target=self._duck_loop, args=(duck,), name="interp-duck",
                                            daemon=True)
        self._duck_watch.start()

    def _duck_loop(self, duck):
        ducked = False
        quiet_since = None
        while self._running and self._duck is duck:
            if self._hear_playing(tail_s=0.0):
                quiet_since = None
                if not ducked:
                    duck.duck()
                    ducked = True
            elif ducked:
                quiet_since = quiet_since or self._clock()
                if self._clock() - quiet_since > 0.6:
                    duck.restore()
                    ducked = False
            time.sleep(0.1)

    # -- stop, billing -----------------------------------------------------------------------------
    def _stop_all(self, reason):
        used = self._billable(self._clock())     # up to now: closing everything takes a moment
        self._running = False
        duck, self._duck = self._duck, None
        if duck is not None:
            duck.close()           # the call's volume first: a slow stop (or a quit) never leaves it down
        self._stop_pass()
        for d in list(self._dirs.values()):
            d.stop()
        self._dirs = {}
        left = self.session.stop(used)
        self.audio.close()         # the next start opens the sound system again and sees today's devices
        self._emit({"type": "session", "state": "stopped", "reason": reason, "seconds_left": left})

    def _billable(self, now):
        """Seconds to bill since the last beat, without the time the computer slept (the heartbeat
        thread looks every 0.2 s: a much longer gap is sleep, not use)."""
        paused = self._paused
        if now - self._seen_at > SLEEP_GAP_S:             # asleep until just now (not looked at yet)
            paused += now - self._seen_at
        return max(0.0, now - self._billed_at - paused)

    def _heartbeat_loop(self):
        failed = 0
        while self._running:
            end = self._clock() + HEARTBEAT_S
            while self._running and self._clock() < end:
                time.sleep(0.2)
                now = self._clock()
                gap = now - self._seen_at
                if gap > SLEEP_GAP_S:                     # this thread didn't run: the computer slept
                    self._paused += gap
                    self._seen_at = now
                    if gap > LONG_SLEEP_S:                # the call is long over, its connections gone
                        self.stop("sleep")
                        return
                else:
                    self._seen_at = now
            if not self._running:
                return
            now = self._clock()
            try:
                left = self.session.heartbeat(self._billable(now))
                self._billed_at, self._paused = now, 0.0
                failed = 0
            except InterpreterError as e:
                failed += 1
                # out of time or Pro, or no answer for a while (signed out, offline): it stops, and
                # no more time is counted for an interpreter nobody can bill
                if e.code in ("allowance_used", "pro_required") or failed >= MAX_FAILED_BEATS:
                    self._emit({"type": "error", "code": e.code})
                    self.stop(e.code)
                    return
                logger.info("interpreter heartbeat failed: %s", e.code)
                continue
            finally:
                self._seen_at = max(self._seen_at, self._clock())   # a slow server call is not sleep
            self._emit({"type": "session", "state": "running", "seconds_left": left})
            if left is not None and left <= 0:
                self._emit({"type": "error", "code": "allowance_used"})
                self.stop("allowance_used")
                return
            if left is not None and left <= LOW_TIME_S and not self.warned_low:
                self.warned_low = True
                self._emit({"type": "low_time", "seconds_left": left})

    def _emit(self, event):
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001
            logger.debug("interpreter event handler failed", exc_info=True)
