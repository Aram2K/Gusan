"""One direction of the interpreter: sound in -> translated voice out, every step streaming.

    feed(pcm) -> STT (live words, committed phrases) -> PhraseCutter -> translate worker
              -> speak worker -> voice -> sink (headphones/speakers, or the Gusan Microphone)

Two workers overlap the steps: phrase N+1 is translated while phrase N is being spoken.

Keep-up rules (a human interpreter never falls further and further behind):
  * captions are never held back: live words and each translation are sent out at once;
  * the voice runs at most ``ahead_s`` ahead of what is heard: later phrases wait as text;
  * at most ``max_waiting`` translated phrases wait for the voice; more are merged into one turn;
  * when the voice is far behind (a phrase would be heard ``behind_s`` after it was said), short
    filler phrases ("yes", "okay", "um") are shown but not spoken.

Made-up text (guard.py) is caught before a phrase is queued: a repeat is dropped, a flip is shown
but not voiced, and on Hear a phrase with almost none of their voice under it while Gusan's own
voice played is shown but not voiced. The translator is warmed up when the direction starts and
after a minute without a request, so a phrase never pays for a cold connection.

Everything here is plain threads + callbacks so it can be tested with fakes; the real STT, voice
and sinks live in eleven.py and audio_io.py. No Qt.
"""
import itertools
import logging
import queue
import threading
import time

from action_api import ReplaceText
from interpreter.guard import VOICE_LOOKBACK_S, VoiceMeter
from interpreter.segmenter import PhraseCutter, units

logger = logging.getLogger(__name__)

# Acknowledgements and hesitations only: a "no" or a number always carries meaning.
FILLERS = {
    "yes", "yeah", "yep", "ok", "okay", "um", "uh", "mm", "hmm", "right", "so", "well", "sure",
    "oui", "bon", "alors", "ja", "si", "vale", "да", "ну", "ага",
    "はい", "ええ", "うん", "嗯", "对", "好", "네", "예", "այո", "հա",
}

# Errors about the account, not the phrase (every phrase fails the same way): said at once.
ACCOUNT_CODES = frozenset(("auth", "pro_required", "quota", "allowance_used", "provider_credits"))
FAILS_TO_TELL = 3           # other translation failures are said once this many come in a row
TICK_S = 0.05               # the cutter looks again this often with no new partial (from feed)
WARM_IDLE_S = 60.0          # no request this long: the translator is warmed up again

_STOP = object()


class Phrase:
    _ids = itertools.count(1)

    def __init__(self, text, t_end):
        self.id = next(self._ids)
        self.text = text
        self.t_end = t_end          # clock time the speaker finished it (the STT's wait taken off)
        self.dst = ""
        self.gender = None          # "female" / "male" / None (pitch.GenderGuess)
        self.held = ""              # why it is shown but not voiced ("flip", "quiet"); "" = voiced
        self.t_translated = None
        self.t_first_audio = None


def is_filler(text):
    """A short "yes", "okay" or "um" - never a "no", a number or any other short word."""
    t = text.strip().lower().strip(".!?,。！？、 ")
    return units(text) <= 2 and t in FILLERS


class DirectionPipeline:
    """direction: "hear" or "speak" (only used in events and logs).

    stt_factory(on_event) -> object with start(), send_audio(bytes), close();
        on_event(kind, text) with kind "partial" | "committed" | "error" | "dropped" | "closed".
    translate(text, src, dst, context, on_delta) -> str (raises on failure; e.code "cut" with
        e.partial for an answer that stopped early). on_delta(piece) adds a piece; an
        action_api.ReplaceText replaces what came before.
    voice: object with open(), speak(text, on_audio, should_stop) -> seconds, close().
    sink: object with play(bytes), queued_seconds() -> float, clear(), and (for min_voice_s)
        is_playing(tail_s) -> True when it played anything in the last tail_s seconds.
    on_event(dict): {"type": "live"|"phrase"|"translation"|"spoken"|"skipped"|"error"|"state", ...}.
    guard: guard.Guard, or None - made-up text is checked before a phrase is queued.
    min_voice_s: Hear, voiced - a phrase with less of their voice under it (feed's sound) while
        Gusan's voice played is shown, not voiced (0: never measured).
    warm_up: () -> None, called on the translate thread at the start and after WARM_IDLE_S without
        a request; it must not block (translate.warm_up sends on a side thread).
    """

    def __init__(self, direction, src_lang, dst_lang, *, stt_factory, translate, voice, sink,
                 on_event=None, clock=time.monotonic, max_waiting=3, behind_s=6.0, ahead_s=1.5,
                 context_size=3, cutter=None, speak_enabled=True, phrase_info=None, voice_for=None,
                 voice_rest_s=5.0, cut_retry_s=1.5, guard=None, min_voice_s=0.0, warm_up=None):
        self.direction = direction
        self.src_lang = src_lang
        self.dst_lang = dst_lang
        self._stt_factory = stt_factory
        self._translate = translate
        self._voice = voice
        self._sink = sink
        self._emit_cb = on_event or (lambda e: None)
        self._clock = clock
        self.max_waiting = max_waiting
        self.behind_s = behind_s
        self.ahead_s = ahead_s              # the voice's audio queued ahead of the ear, at most
        self.voice_rest_s = voice_rest_s    # after the voice fails: captions only this long (x2 each time)
        self.cut_retry_s = cut_retry_s      # a translation cut off this soon is asked once more
        self.context_size = context_size
        self.speak_enabled = speak_enabled
        self._phrase_info = phrase_info     # (now) -> {"gender": ...} for the phrase that just ended
        self._voice_for = voice_for         # (phrase) -> voice id, or None for the voice's default
        self._cutter = cutter or PhraseCutter()
        # the cutter is used on the STT reader thread and (tick) on the capture thread: one at a
        # time, and each cut's phrases queued before the next cut, so they stay in order
        self._cut_lock = threading.Lock()
        self._tick_at = 0.0
        self._guard = guard
        self.min_voice_s = min_voice_s
        # their voice is only measured when it can matter: Hear, voiced (one rms per chunk)
        self._meter = VoiceMeter() if min_voice_s and speak_enabled else None
        self._heard_from = 0.0        # (clock) the last phrase's end: the next one's voice is counted from here
        self._warm_up = warm_up
        self.warm_idle_s = WARM_IDLE_S
        self._asked_at = 0.0          # (monotonic) the translator's last request, a warm-up too
        self._to_translate = queue.Queue()
        self._to_speak = queue.Queue()
        self._context = []
        self._stt = None
        self._threads = []
        self._running = False
        self._lock = threading.Lock()
        self._voice_fails = 0         # phrases in a row the voice failed on
        self._voice_rest_until = 0.0  # (clock) phrases are shown, not spoken, until then
        self._fails = 0               # translations in a row that failed
        self._told = set()            # error codes already said in this run of failures
        self._stt_said = False        # speech-to-text trouble already said (until words come back)
        self._voice_ready = threading.Event()   # the voice's first connection is done (or failed)
        self._voice_ready.set()
        self.latencies = []           # seconds from phrase end to its first audio being played

    # -- life cycle --------------------------------------------------------------------------
    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
        self._heard_from = self._clock()
        self._emit({"type": "state", "state": "starting"})
        if self.speak_enabled:
            # The voice connects and warms up (~0.3 s, ~3 s for a cloned voice) beside the
            # speech-to-text, never in front of it: what is said meanwhile is heard and translated,
            # and only a phrase ready before the voice waits for it (_wait_for_voice).
            self._voice_ready = ready = threading.Event()
            threading.Thread(target=self._open_voice, args=(ready,), name=f"interp-{self.direction}-voice",
                             daemon=True).start()
        self._stt = self._stt_factory(self._on_stt)
        self._stt.start()
        for target in (self._translate_loop, self._speak_loop):
            t = threading.Thread(target=target, name=f"interp-{self.direction}-{target.__name__}", daemon=True)
            t.start()
            self._threads.append(t)
        self._emit({"type": "state", "state": "running"})

    def stop(self, wait=2.0):
        with self._lock:
            if not self._running:
                return
            self._running = False
        for closer in (getattr(self._stt, "close", None), getattr(self._voice, "close", None)):
            try:
                if closer:
                    closer()
            except Exception:  # noqa: BLE001
                logger.debug("interpreter close failed", exc_info=True)
        try:
            self._sink.clear()            # stop talking at once when it is switched off
        except Exception:  # noqa: BLE001
            pass
        self._to_translate.put(_STOP)
        self._to_speak.put(_STOP)
        for t in self._threads:
            t.join(wait)
        self._threads = []
        with self._cut_lock:
            self._cutter.reset()
            if self._guard is not None:
                self._guard.reset()
        self._emit({"type": "state", "state": "stopped"})

    def _open_voice(self, ready):
        try:
            self._voice.open()
        except Exception as e:  # noqa: BLE001 - captions still work without a voice
            if self._running:
                logger.warning("interpreter %s: voice did not open: %s", self.direction, e)
            self._voice_failed(e)            # a later phrase tries again
        finally:
            ready.set()

    @property
    def running(self):
        return self._running

    def feed(self, pcm):
        """16 kHz mono 16-bit PCM from the source (mic or the call's sound). Also the cutter's
        clock: live words that stopped changing (a short pause) are cut on time, without waiting
        for the next partial - a clock check per chunk, a look at most every TICK_S. On a voiced
        Hear direction, one rms per chunk for their voice (min_voice_s)."""
        stt = self._stt
        if self._running and stt is not None:
            stt.send_audio(pcm)
            now = self._clock()
            if self._meter is not None:
                self._meter.feed(pcm, now)
            if now >= self._tick_at:
                self._tick_at = now + TICK_S
                with self._cut_lock:
                    for p in self._cutter.tick(now):
                        self._queue_phrase(p, now, getattr(self._cutter, "stable_s", 0.0))

    # -- STT events (called on the STT client's reader thread) --------------------------------
    def _on_stt(self, kind, text=""):
        if not self._running:
            return
        now = self._clock()
        if kind == "partial":
            self._stt_said = False
            self._emit({"type": "live", "text": text})
            with self._cut_lock:
                for p in self._cutter.partial(text, now):
                    # cut once the words stood still for stable_s: they were said at least that long ago
                    self._queue_phrase(p, now, getattr(self._cutter, "stable_s", 0.0))
        elif kind == "committed":
            self._stt_said = False
            with self._cut_lock:
                for p in self._cutter.committed(text, now):
                    # committed after vad_silence of quiet: the speaker stopped at least that long ago
                    self._queue_phrase(p, now, getattr(self._stt, "vad_silence", 0.0) or 0.0)
        elif kind == "error":
            self._tell_stt("stt", text)
        elif kind in ("dropped", "closed"):
            # the connection broke before the service committed what was said: the words it
            # already showed go out now, and the next utterance starts afresh
            with self._cut_lock:
                for p in self._cutter.flush():
                    self._queue_phrase(p, now)
            if kind == "closed":
                # gone for good (eleven.py already tried to reconnect): whoever runs this direction
                # says it (the app starts again with a fresh session, or stops and says so) - only
                # a reason about the account is said here, at once
                if text in ACCOUNT_CODES:
                    self._tell_stt(text, text)
                self._emit({"type": "state", "state": "stt_closed", "code": text})

    def _tell_stt(self, code, message):
        """Speech-to-text trouble, said once until words come back (each new connection that
        fails the same way is not said again); a reason about the account is always said."""
        if code == "stt" and self._stt_said:
            return
        self._stt_said = True
        self._emit({"type": "error", "code": code, "message": message})

    def _queue_phrase(self, text, now, lag=0.0):
        t_end = now - (lag or 0.0)
        quiet = self._too_quiet(now, t_end)
        say = True
        if self._guard is not None:
            try:
                got = self._guard.check(text, now)
            except Exception:  # noqa: BLE001 - a check must never cost a phrase
                logger.debug("made-up text check failed", exc_info=True)
                got = (text, True)
            if got is None:
                # made up (a repeat of what was just sent): never translated, never said
                self._emit({"type": "skipped", "reason": "repeat", "text": text})
                return
            text, say = got
        ph = Phrase(text, t_end)
        ph.held = "flip" if not say else ("quiet" if quiet else "")
        if self._phrase_info:
            try:
                ph.gender = (self._phrase_info(now) or {}).get("gender")
            except Exception:  # noqa: BLE001 - a guess must never cost a phrase
                logger.debug("phrase info failed", exc_info=True)
        self._emit({"type": "phrase", "id": ph.id, "text": text, "gender": ph.gender})
        self._to_translate.put(ph)

    def _too_quiet(self, now, t_end):
        """Hear: under min_voice_s of their voice since the last phrase ended (and at least
        VOICE_LOOKBACK_S before its own end), while Gusan's own voice played - what was heard is our
        own voice, or the silence the gate left ("Սպիտակ" heard while only Gusan spoke). Once per
        phrase; the window moves on either way."""
        if self._meter is None:
            return False
        since = self._heard_from
        self._heard_from = max(since, t_end)
        # a tail said right after an early cut ends before that cut's end: its words still count
        since = min(since, t_end - VOICE_LOOKBACK_S)
        if self._meter.since(since) >= self.min_voice_s:
            return False
        try:
            return bool(self._sink.is_playing(tail_s=now - since + 0.3))
        except Exception:  # noqa: BLE001 - when in doubt it is voiced, as before
            return False

    # -- translate worker -----------------------------------------------------------------------
    def _translate_loop(self):
        self._warm()                            # the first phrase finds the translator awake
        while True:
            try:
                ph = self._to_translate.get(timeout=self._idle_wait())
            except queue.Empty:
                self._warm()                    # a minute without a request: awake again
                continue
            if ph is _STOP:
                return
            if not self._running:
                continue
            parts = []
            shown = [""]                    # what the caption shows of it so far

            def on_delta(d, ph=ph):
                if isinstance(d, ReplaceText):
                    parts[:] = [str(d)]     # reasoning the model leaked: start over with the answer
                else:
                    parts.append(d)
                shown[0] = "".join(parts)
                self._emit({"type": "translation", "id": ph.id, "text": shown[0], "final": False})
            ctx = list(self._context[-self.context_size:])
            asked = self._clock()
            self._asked_at = time.monotonic()
            try:
                try:
                    out = self._translate(ph.text, self.src_lang, self.dst_lang, ctx, on_delta)
                except Exception as e:  # noqa: BLE001
                    # cut off at once (a dropped connection): asked once more, while it is still fresh
                    if (getattr(e, "code", "") != "cut" or not self._running
                            or self._clock() - asked > self.cut_retry_s):
                        raise
                    parts.clear()
                    out = self._translate(ph.text, self.src_lang, self.dst_lang, ctx, on_delta)
                ph.dst = (out or "").strip()
            except Exception as e:  # noqa: BLE001 - one bad phrase must not stop the call
                self._translate_failed(ph, e, shown[0])
                continue
            self._fails = 0
            self._told.clear()
            ph.t_translated = self._clock()
            self._context.append(ph.text)
            del self._context[:-self.context_size]
            self._emit({"type": "translation", "id": ph.id, "text": ph.dst, "final": True})
            if ph.dst and ph.held:
                # shown, never voiced - and kept out of the speak queue, where it could be merged
                self._emit({"type": "skipped", "id": ph.id, "reason": ph.held})
            elif ph.dst:
                self._to_speak.put(ph)

    def _idle_wait(self):
        """How long the translate worker may wait for a phrase before warming up again."""
        if self._warm_up is None:
            return None
        return max(0.0, self._asked_at + self.warm_idle_s - time.monotonic())

    def _warm(self):
        """Wake the translator from this (the translate) thread, so it is this thread's connection
        that gets warm. Never in a phrase's way: warm_up sends on a side thread; a failure is ignored."""
        self._asked_at = time.monotonic()
        if self._warm_up is None or not self._running:
            return
        try:
            self._warm_up()
        except Exception:  # noqa: BLE001
            logger.debug("interpreter warm-up failed", exc_info=True)

    def _translate_failed(self, ph, e, shown):
        code = str(getattr(e, "code", "") or "")
        logger.warning("interpreter %s: translation failed (%s): %s", self.direction, code, e)
        partial = (getattr(e, "partial", "") or shown or "").strip()
        if partial:
            # what came stays in the captions, marked as cut off; it is never said
            self._emit({"type": "translation", "id": ph.id, "text": partial + " …", "final": True, "cut": True})
        self._emit({"type": "error", "code": "translate", "message": str(e), "id": ph.id})
        self._context.append(ph.text)
        del self._context[:-self.context_size]
        if not self._running:
            return
        self._fails += 1
        # signed out, Pro ended, the day's cap: every phrase fails the same way, so it is said at
        # once; anything else once a few phrases in a row failed
        if code in ACCOUNT_CODES:
            tell = code
        elif self._fails >= FAILS_TO_TELL:
            tell = "network" if code == "network" else "server"
        else:
            return
        if tell not in self._told:
            self._told.add(tell)
            self._emit({"type": "error", "code": tell, "message": str(e)})

    # -- speak worker ---------------------------------------------------------------------------
    def _speak_loop(self):
        while True:
            ph = self._to_speak.get()
            if ph is _STOP:
                return
            if not self._running or not self.speak_enabled:
                continue
            self._wait_for_voice()
            self._wait_for_sink()
            if not self._running:
                continue
            ph = self._merge_waiting(ph)
            if ph is None:
                return
            if self._clock() < self._voice_rest_until:
                self._emit({"type": "skipped", "id": ph.id, "reason": "voice"})
                continue
            queued = self._sink_queued()
            # how late it would be heard: since it was said, plus the audio still in front of it
            late = (self._clock() - ph.t_end) + queued
            if late >= self.behind_s and is_filler(ph.dst):
                self._emit({"type": "skipped", "id": ph.id, "reason": "behind"})
                continue
            self._speak(ph, queued)

    def _wait_for_voice(self):
        """The voice is still connecting (it just started): the phrase waits for it, never lost
        (later phrases wait behind it as text, merged when they pile up)."""
        ready = self._voice_ready
        while self._running and not ready.wait(0.05):
            pass

    def _wait_for_sink(self):
        """The voice stays at most ``ahead_s`` ahead of what is heard: later phrases wait here as
        text (merged, or skipped when filler) instead of piling up as audio no one can take back.
        A device that stopped playing (no progress for half a second) is not waited for."""
        last, since = None, time.monotonic()
        while self._running:
            q = self._sink_queued()
            if q <= self.ahead_s:
                return
            if last is None or q < last:
                last, since = q, time.monotonic()
            elif time.monotonic() - since > 0.5:
                return
            time.sleep(0.02)

    def _merge_waiting(self, ph):
        """When more than ``max_waiting`` phrases are waiting, say them as one turn."""
        waiting = [ph]
        while self._to_speak.qsize() >= self.max_waiting:
            try:
                nxt = self._to_speak.get_nowait()
            except queue.Empty:
                break
            if nxt is _STOP:
                return None
            waiting.append(nxt)
        if len(waiting) == 1:
            return ph
        merged = Phrase(" ".join(p.dst for p in waiting), waiting[-1].t_end)
        merged.dst = merged.text
        merged.id = waiting[-1].id
        merged.gender = waiting[-1].gender
        self._emit({"type": "merged", "ids": [p.id for p in waiting]})
        return merged

    def _speak(self, ph, queued):
        first = []
        extra = {}
        if self._voice_for:
            try:
                vid = self._voice_for(ph)
            except Exception:  # noqa: BLE001
                vid = None
            if vid:
                extra["voice_id"] = vid

        def on_audio(pcm):
            if not first:
                first.append(self._clock())
            self._sink.play(pcm)

        def say():
            self._voice.speak(ph.dst, on_audio, lambda: not self._running, **extra)
        try:
            say()
        except Exception as e:  # noqa: BLE001
            if not self._running:
                return                  # switched off mid-phrase: the voice was closed on purpose
            logger.warning("interpreter %s: voice failed: %s", self.direction, e)
            if not self._reopen_voice():
                self._voice_failed(e)
                return
            # a fresh connection: the phrase again, unless part of it was already heard
            # (the listener never hears the same words twice)
            if not first:
                try:
                    say()
                except Exception as e2:  # noqa: BLE001
                    if self._running:
                        self._voice_failed(e2)
                    return
        self._voice_fails = 0
        if first:
            ph.t_first_audio = first[0]
            # the listener hears it after the audio already queued in front of it
            heard_after = (first[0] - ph.t_end) + queued
            self.latencies.append(heard_after)
            self._emit({"type": "spoken", "id": ph.id, "latency": round(heard_after, 2)})

    def _voice_failed(self, e):
        """The voice failed on a phrase: for a short rest (twice as long each time in a row) the
        phrases are shown but not spoken, then the next one tries a fresh connection. Said once."""
        if not self._running:
            return
        self._voice_fails += 1
        rest = min(self.voice_rest_s * 2 ** (self._voice_fails - 1), 60.0)
        self._voice_rest_until = self._clock() + rest
        if self._voice_fails == 1:
            self._emit({"type": "error", "code": "voice", "message": str(e)})

    def _reopen_voice(self):
        reopen = getattr(self._voice, "reopen", None)
        if not reopen:
            return False
        try:
            reopen()
            return True
        except Exception:  # noqa: BLE001
            logger.debug("voice reopen failed", exc_info=True)
            return False

    def _sink_queued(self):
        try:
            return float(self._sink.queued_seconds())
        except Exception:  # noqa: BLE001
            return 0.0

    def _emit(self, event):
        event.setdefault("direction", self.direction)
        try:
            self._emit_cb(event)
        except Exception:  # noqa: BLE001 - a UI bug must not stop the pipeline
            logger.debug("interpreter event handler failed", exc_info=True)

    def recent_latency(self, n=5):
        """Median of the last ``n`` phrase latencies (for the delay shown on the live card)."""
        xs = sorted(self.latencies[-n:])
        return xs[len(xs) // 2] if xs else None


def wait_until(pred, timeout=2.0, step=0.01):
    """Test helper: poll ``pred`` until true or the timeout passes."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()
