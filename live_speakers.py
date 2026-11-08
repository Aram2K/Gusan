"""Who is speaking, live: every sentence a meeting's live stream finishes gets a speaker label while the meeting runs.

The founder (2026-11-08): several people can share one microphone and a call can hold many - "You" / "Them" is not
enough. Gladia and ElevenLabs only tell speakers apart in recorded files, so this uses the app's own speaker model
(sherpa-onnx's voice prints, diarization.build_extractor - the one the speaker pass after Stop uses): the sentence's
audio becomes one voice print, matched against the voices heard so far in this meeting by cosine similarity to each
voice's average print:

  * at least MATCH similar to a known voice: that voice;
  * a new voice only when the sentence is clearly unlike every known one (under NEW_VOICE_BELOW) and at least
    NEW_VOICE_MIN_S long - one voice print is noisy (the founder's own 21 s Armenian take, 2-3 s windows: median
    0.34 between two windows, 0.69 to the average; at 0.45 one person became four "speakers"); in between: the
    most similar voice;
  * the two sides of a mic + call meeting never mix: someone on the call is never "You", a voice heard on the
    microphone is never one of the call's (the side comes from which source was loud: speaker_hint);
  * at most MAX_VOICES; past that, the most similar one.

Names: on a mic + call meeting the first voice heard on the microphone side is "You"; every other voice - a second
person at your microphone, each person on the call - is "Speaker 1", "Speaker 2" ... in the order they first spoke.
A meeting with one source numbers everyone. The labels are the live view's; the speaker pass after Stop still
writes the notes' speakers (and names from the screen).

Qt-free. ``labeler`` is None when the speaker model is not installed (the "You" / "Them" hint stays).
"""
import logging
import math
import threading

logger = logging.getLogger(__name__)

MATCH = 0.30              # cosine similarity to a voice's average print from which it is that voice
NEW_VOICE_BELOW = 0.15    # a new voice only when the sentence is less similar than this to every known one ...
NEW_VOICE_MIN_S = 2.5     # ... and at least this long
MIN_AUDIO_S = 0.5         # shorter than this: no voice print at all (the side's last voice)
MAX_VOICES = 8
SAMPLE_RATE = 16000


def _unit(vector):
    try:
        values = [float(x) for x in vector]
    except (TypeError, ValueError):
        return None
    if not values or not all(math.isfinite(x) for x in values):
        return None
    norm = math.sqrt(sum(x * x for x in values))
    return [x / norm for x in values] if norm > 1e-9 else None


def _cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


class Voice:
    def __init__(self, label, vector, side=""):
        self.label = label
        self.side = side
        self.total = list(vector)
        self.count = 1

    def mean(self):
        return _unit(self.total) or self.total

    def add(self, vector):
        self.total = [x + y for x, y in zip(self.total, vector)]
        self.count += 1


class LiveSpeakers:
    """One meeting's voices. ``print_of(samples)`` -> a voice print (16 kHz mono float32); ``two_sources``: a mic +
    call meeting (its first microphone voice is "You")."""

    def __init__(self, print_of, two_sources=False):
        self._print_of = print_of
        self._two_sources = bool(two_sources)
        self.voices = []
        self._last = {}            # side -> the last label on it
        self._numbered = 0
        self._lock = threading.Lock()

    def _new_label(self, side):
        if self._two_sources and side == "mic" and not any(v.label == "You" for v in self.voices):
            return "You"
        self._numbered += 1
        return "Speaker %d" % self._numbered

    def label(self, samples, side=""):
        """The label of one sentence's audio. ``side``: "mic" / "call" (who was loud meanwhile) or "" (unknown)."""
        seconds = (len(samples) / float(SAMPLE_RATE)) if samples is not None else 0.0
        with self._lock:
            if seconds < MIN_AUDIO_S:
                return self._last.get(side, "")
            try:
                vector = _unit(self._print_of(samples))
            except Exception:
                logger.debug("Live speakers: no voice print", exc_info=True)
                vector = None
            if vector is None:
                return self._last.get(side, "")
            best, best_sim = None, -1.0
            for voice in self.voices:
                if side and voice.side and voice.side != side:
                    continue                          # the call's people are never "You", and the other way round
                sim = _cosine(vector, voice.mean())
                if sim > best_sim:
                    best, best_sim = voice, sim
            new_voice = seconds >= NEW_VOICE_MIN_S and (best is None or best_sim < NEW_VOICE_BELOW)
            if best is not None and (best_sim >= MATCH or not new_voice or len(self.voices) >= MAX_VOICES):
                best.add(vector)
                chosen = best.label
            elif best is None and not new_voice:
                return self._last.get(side, "")
            else:
                voice = Voice(self._new_label(side), vector, side)
                self.voices.append(voice)
                chosen = voice.label
            self._last[side] = chosen
            return chosen


def labeler(two_sources=False):
    """A LiveSpeakers on the app's speaker model, or None when it is not installed / not downloaded (the speaker
    pass after Stop downloads it). Builds the model (about a second): call it off the GUI thread."""
    try:
        import diarization
        if not diarization.is_available():
            return None
        extractor = diarization.build_extractor(threads=2)
    except Exception:
        logger.info("Live speakers: the speaker model is not available", exc_info=True)
        return None

    def print_of(samples):
        return diarization.voice_print(extractor, samples)
    return LiveSpeakers(print_of, two_sources=two_sources)
