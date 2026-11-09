"""Sound for the Interpreter page: record the microphone (My voice > Record now, Try it) and play a
voice sample (Hear yourself in, Try it). Qt-free; every function here blocks, so the page runs them
on worker threads (never the GUI thread).

    wav = record_mic(stop=event, on_level=fn, max_seconds=30)      # 16 kHz mono WAV bytes
    play_pcm(pcm, 16000, stop=event)                                # 16-bit mono PCM
    result = try_it(app, "en", "hy", on_phase=fn)                  # say one sentence, hear it back

Tests replace these functions (no sound devices in a test).
"""

import logging
import time

import numpy as np

from interpreter import audio_io, voice_builder

logger = logging.getLogger("transcribe")

TRY_SECONDS = 4.0            # Try it: nothing said by then, the take ends ...
TRY_MAX_S = 8.0              # ... and one sentence may run this long at most;
TRY_QUIET_S = 0.7            # it ends this long after the speech stops (speed first)
CHUNK_S = 0.05
POLL_S = 0.02
NO_SOUND_S = 3.0             # no sound at all from the microphone this long: say so (never wait forever)
ZEROS_S = 6.0                # only digital silence this long (a muted or blocked mic sends zeros): say so
SILENT_PEAK = 16             # a take whose loudest sample is this quiet holds no sound at all
FLOOR_S = 0.3                # Try it: the room's own level is taken from this first bit of the take
SPEECH_RMS = 500.0           # speech is this loud at least (and 3x the room); quiet is under half of it


class MicSilent(RuntimeError):
    """The microphone opened but sent no sound (blocked, in use, or switched off)."""
    code = "no_mic_sound"


def _device_index(cfg):
    idx = (cfg or {}).get("input_device_index")
    return idx if isinstance(idx, int) and not isinstance(idx, bool) else None


def _open_mic(pa, mod, cfg):
    """The microphone, opened the way the dictation recorder opens it (float32, mono, 16 kHz - what
    laptop mic arrays deliver reliably), else at the device's own rate and channels.
    Returns (stream, channels, rate)."""
    idx = _device_index(cfg)
    if idx is None:
        idx = int(pa.get_default_input_device_info()["index"])
    info = pa.get_device_info_by_index(idx)
    native = (max(1, min(2, int(info.get("maxInputChannels") or 1))), int(info.get("defaultSampleRate") or 48000))
    last = None
    for channels, rate in ((1, audio_io.RATE), native):
        try:
            stream = pa.open(format=mod.paFloat32, channels=channels, rate=rate, input=True,
                             input_device_index=idx, frames_per_buffer=int(rate * CHUNK_S))
            return stream, channels, rate
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def record_mic(*, seconds=None, max_seconds=30.0, stop=None, on_level=None, cfg=None, until_quiet=None):
    """Record the microphone until ``stop`` is set (or ``seconds`` / ``max_seconds`` pass).
    on_level(0..1) gets the loudness ~20 times a second. Returns 16 kHz mono WAV bytes.

    ``until_quiet`` (Try it): the take ends that long after the speech stops; with no speech it
    ends at ``seconds``, and it never runs past ``max_seconds``.

    It never blocks on the device (it polls what is there), so Stop always works at once. A
    microphone that sends nothing for NO_SOUND_S, or only zeros (muted, blocked by Windows, an
    idle virtual cable) raises MicSilent instead of passing silence on."""
    mod = audio_io._pyaudio()
    pa = mod.PyAudio()
    chunks = []
    stream = None
    got = 0                                   # samples so far (16 kHz): the take's own clock
    peak = 0
    try:
        stream, channels, rate = _open_mic(pa, mod, cfg)
        start = time.monotonic()
        limit = max_seconds if until_quiet or seconds is None else seconds
        end = start + float(limit)
        quiet_from = float(seconds if seconds is not None else limit)
        frames = heard = spoke = False
        floor = None
        last_loud = 0.0
        while time.monotonic() < end and not (stop is not None and stop.is_set()):
            avail = stream.get_read_available()
            if avail > 0:
                raw = stream.read(avail, exception_on_overflow=False)
                samples = np.frombuffer(raw, dtype=np.float32)
                pcm = audio_io.to_mono16k(samples, channels, rate)
                if pcm.size:
                    frames = True
                    heard = heard or bool(samples.any())       # exact zeros: a muted or blocked mic
                    chunks.append(pcm)
                    got += pcm.size
                    a = pcm.astype(np.float32)                 # (int16 squares wrap around)
                    level = float(np.sqrt(np.mean(a * a)))
                    peak = max(peak, int(np.abs(a).max()))
                    if on_level is not None:
                        try:
                            on_level(min(1.0, level / 6000.0))
                        except Exception:
                            pass
                    if until_quiet:
                        t = got / float(audio_io.RATE)
                        if floor is None or t <= FLOOR_S:
                            floor = level if floor is None else min(floor, level)
                        # speech starts loud and goes on while it stays above half that bar
                        if level >= max(SPEECH_RMS, 3.0 * floor) or (
                                spoke and level >= max(SPEECH_RMS / 2.0, 2.0 * floor)):
                            spoke, last_loud = True, t
                        if (spoke and t - last_loud >= until_quiet) or (not spoke and t >= quiet_from) \
                                or t >= limit:
                            break
            else:
                time.sleep(POLL_S)
            if not frames and time.monotonic() - start >= NO_SOUND_S:
                raise MicSilent("the microphone sent no sound")
            if frames and not heard and got >= ZEROS_S * audio_io.RATE:
                raise MicSilent("the microphone sent only silence")
    finally:
        if stream is not None:
            for f in (stream.stop_stream, stream.close):
                try:
                    f()
                except Exception:
                    pass
        try:
            pa.terminate()
        except Exception:
            pass
    if got >= NO_SOUND_S * audio_io.RATE and peak <= SILENT_PEAK:
        raise MicSilent("the microphone sent only silence")
    audio = np.concatenate(chunks) if chunks else np.zeros(0, np.int16)
    return voice_builder.to_wav_bytes(audio)


def play_pcm(pcm, rate=16000, *, stop=None):
    """Play 16-bit mono PCM on the default output; returns when it ends (or ``stop`` is set)."""
    if not pcm or (stop is not None and stop.is_set()):
        return
    mod = audio_io._pyaudio()
    pa = mod.PyAudio()
    stream = None
    try:
        stream = pa.open(format=mod.paInt16, channels=1, rate=int(rate or 16000), output=True)
        step = int(rate or 16000) // 10 * 2               # 0.1 s of bytes
        for i in range(0, len(pcm), step):
            if stop is not None and stop.is_set():
                break
            stream.write(bytes(pcm[i:i + step]))
    finally:
        if stream is not None:
            for f in (stream.stop_stream, stream.close):
                try:
                    f()
                except Exception:
                    pass
        try:
            pa.terminate()
        except Exception:
            pass


def try_it(app, my_lang, their_lang, *, on_phase=None, stop=None, voices=None):
    """Try it: record one sentence (it ends when the speech stops), transcribe it on Gusan Cloud,
    translate it, and play it back in the person's own voice (VoiceStore.preview; ``voices`` None:
    the words only). Returns {"said", "heard", "voice": bool}. Raises on failure (the page says
    one calm sentence). A stop (pressed again, or the page left) ends it at the next step: nothing
    more is paid for or played."""
    import cloud_speech
    import vocabulary
    from interpreter import translate

    def phase(name):
        if on_phase is not None:
            on_phase(name)

    def cancelled():
        return stop is not None and stop.is_set()

    cfg = getattr(app, "cfg", None) or {}
    auth = getattr(app, "auth", None)
    nothing = {"said": "", "heard": "", "voice": False}

    def token():
        fn = getattr(auth, "get_access_token", None)
        return fn() if callable(fn) else None

    phase("listening")
    wav = record_mic(seconds=TRY_SECONDS, max_seconds=TRY_MAX_S, until_quiet=TRY_QUIET_S, stop=stop, cfg=cfg)
    if cancelled():
        return nothing
    phase("translating")
    spec = cloud_speech.spec_from_cfg(cfg, "managed", "fast", language=my_lang, token_fn=token)
    said, _lang = cloud_speech.send(spec, wav)
    said = (said or "").strip()
    if not said or cancelled():
        return nothing
    # the Dictionary words leave the PC only when that is allowed (privacy), as in a live run
    heard = translate.translate_phrase(said, my_lang, their_lang, [], lambda _d: None, token=token(),
                                       glossary=vocabulary.cloud_terms(cfg))
    if cancelled():
        return nothing
    result = {"said": said, "heard": heard, "voice": False}
    if voices is None or not heard:
        return result
    phase("speaking")
    try:
        pcm, rate = voices.preview(heard, their_lang)
    except Exception as e:                     # the voice can't speak just now: the words still show
        logger.info("Try it: no voice sample (%s)", getattr(e, "code", type(e).__name__))
        return result
    if cancelled():
        return nothing
    result["voice"] = True
    play_pcm(pcm, rate, stop=stop)
    return result


__all__ = ["MicSilent", "NO_SOUND_S", "TRY_MAX_S", "TRY_QUIET_S", "TRY_SECONDS", "play_pcm", "record_mic",
           "try_it"]
