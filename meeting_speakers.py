"""Who said what in a recorded meeting - the one place that turns audio into
"You: ..." / "Speaker 1: ..." lines (Gusan 2.0.1).

Used right after Stop, when an interrupted meeting is finished, and by
"Identify speakers" on a saved meeting - the same steps (build_transcript):

  1. the speaker tool must be here (the speaker model is downloaded once; never
     in Privacy mode);
  2. each recording part is read again with timestamps (Whisper, one pass) - except a
     session whose words came from a cloud engine (Live Assistant, Gusan 2.0.1): it hands
     build_transcript(reader=...) its live pieces as the segments, so the cloud's words
     stay (a local Whisper re-read would replace them) and only the labels are worked out;
  3. a mic + system sound meeting (both tracks kept): every segment is labelled
     by SOURCE first - the microphone is "You" - and only what the system sound
     carries is diarized (on the system track alone), into "Speaker 1..N"
     (speaker_labels.py); without the two tracks the mixed audio is diarized
     as before;
  4. the transcript is written with consistent numbering across parts.

Notes first, speaker names after (Gusan 2.0.1): at Stop the slow step 3 is left
for later. build_transcript(diarize=False) does only the fast half - the re-read
and the labels by source - and the transcript says "You" and "Them" (the notes are
written from it at once; the segments are kept in segments.json). Then, in the
background and only while the computer is free, refine_meeting() turns "Them" into
"Speaker 1..N" from those saved segments and the call's own sound (name_the_call):
no second reading of the recording, and transcript.txt is replaced in one atomic
step - a crash at any point keeps the You / Them transcript.

Nothing here raises, and nothing is silent: every way this can end without
labels comes back as an Outcome with a ``reason`` code and a plain ``notice``
sentence for the meeting's notices, and logs one line with the reason (INFO for the
ones that are no problem: a recording too short to label, nobody on the call).
"""
import logging
import time
from pathlib import Path

import diarization
import meeting_store
import speaker_labels

logger = logging.getLogger("transcribe")

SAMPLE_RATE = 16000
MIN_SEC = 1.0
# The call's speech (seconds, the "Them" segments added up) under which the voices are
# not told apart: too little to cluster, and one other voice at most is likely.
MIN_CALL_SEC = 20.0

# Reason codes of an Outcome that ended without (all) labels.
R_NO_AUDIO = "no_audio"
R_TOO_SHORT = "too_short"
R_NO_COMPONENT = "no_component"
R_PRIVACY = "privacy"
R_DOWNLOAD = "download"
R_NO_SEGMENTS = "no_segments"
R_NO_VOICES = "no_voices"
R_TIMEOUT = "timeout"
R_ERROR = "error"
R_CANCELLED = "cancelled"
R_BRIEF = "brief"          # the call spoke too little to tell voices apart: it stays "Them"
R_NO_CALL = "no_call"      # nobody on the call spoke: nothing to tell apart
# Ends that are no problem: logged as INFO, and the automatic passes (Stop, the
# background one) add no notice for them - a recording too short or silent has
# nothing to label, and saying so would only worry.
BENIGN = (R_NO_AUDIO, R_TOO_SHORT, R_NO_CALL)

TRY_AGAIN = "Try again from ⋯ › Identify speakers."
_WHY = {
    R_NO_AUDIO: "there is no recording to read",
    R_TOO_SHORT: "the recording was too short",
    R_NO_COMPONENT: "the speaker tool isn't part of this copy of Gusan",
    R_PRIVACY: "the speaker model isn't on this computer yet, and Privacy mode keeps Gusan offline",
    R_DOWNLOAD: "the speaker model couldn't be downloaded - check your internet connection",
    R_NO_SEGMENTS: "the recording couldn't be read again",
    R_NO_VOICES: "no separate voices were found",
    R_TIMEOUT: "telling the voices apart took too long on this computer",
    R_ERROR: "something went wrong while reading the recording",
}
CALL_TOGETHER = ("People on the call couldn't be told apart, so they are shown as \"Them\". "
                 + TRY_AGAIN)
CALL_TOO_LONG = ("Telling the people on the call apart took too long, so they are shown as \"Them\". "
                 + TRY_AGAIN)
CALL_BRIEF = ("The other side spoke for only a short time, so it is shown as \"Them\". "
              "One other voice at most is likely.")


def notice_for(reason):
    """The plain sentence for the meeting's notices ("" for a cancel, and for the
    ends that need none)."""
    if reason == R_BRIEF:
        return CALL_BRIEF
    if reason == R_CANCELLED or reason not in _WHY:
        return ""
    return "Speaker labels weren't added: %s. %s" % (_WHY[reason], TRY_AGAIN)


def is_speaker_notice(text):
    """Is this one of the notices a speaker pass writes (so a newer pass can
    replace it)?"""
    t = str(text or "")
    return t.startswith("Speaker labels weren't added") or t in (CALL_TOGETHER, CALL_TOO_LONG, CALL_BRIEF)


def is_benign(reason):
    """An end that is no problem (see BENIGN): INFO in the log, no notice."""
    return reason in BENIGN


class Outcome:
    """What a speaker pass came to."""

    def __init__(self):
        self.texts = []          # one transcript per part; [] when there is nothing to use
        self.reason = ""         # why it ended without (all) labels; "" = fine
        self.notice = ""         # plain sentence for the meeting's notices ("" = none)
        self.mode = ""           # "sources" (mic + system sound), "call" (system sound only) or
        #                          "voices" (mixed audio)
        self.speakers = 0        # people told apart on the call (or voices, in "voices")
        self.segments = 0
        self.timings = {}
        self.parts = []          # per part: its labelled segments (start / end / text / source / speaker)
        self.call_seconds = 0.0  # how long the call spoke (the "Them" segments added up)

    @property
    def text(self):
        return self.texts[0] if self.texts else ""

    @property
    def ok(self):
        return bool(self.texts) and not self.reason

    @property
    def cancelled(self):
        return self.reason == R_CANCELLED


class Part:
    """One recording part: its mix (what Whisper reads) and, for a mic + system
    sound meeting, the microphone and system tracks (int16, 16 kHz).
    ``call_only``: the mix IS the call (a meeting recorded from the system sound
    only) - with build_transcript(diarize=False) every segment is "Them"."""

    def __init__(self, audio=None, mic=None, system=None, loader=None, has_tracks=None,
                 call_only=False):
        self._audio, self._mic, self._system, self._loader = audio, mic, system, loader
        self.has_tracks = (mic is not None and system is not None) if has_tracks is None else bool(has_tracks)
        self.call_only = bool(call_only)

    def known_seconds(self):
        """The part's length when it is already in memory, else None."""
        return len(self._audio) / float(SAMPLE_RATE) if self._audio is not None else None

    def load(self):
        """(mix float32, mic int16 | None, system int16 | None)."""
        if self._loader is not None:
            return self._loader()
        return self._audio, self._mic, self._system

    @classmethod
    def from_files(cls, mix_path, mic_path=None, system_path=None):
        """A saved part: audio_partN.wav and, when both exist, its two tracks."""
        both = mic_path is not None and system_path is not None

        def loader():
            import audio_export
            audio, _rate = audio_export.read_wav_float32(mix_path)
            if not both:
                return audio, None, None
            mic, _r1 = audio_export.read_wav_int16(mic_path)
            system, _r2 = audio_export.read_wav_int16(system_path)
            return audio, mic, system
        return cls(loader=loader, has_tracks=both)


def _say(fn):
    def say(text):
        if fn is None:
            return
        try:
            fn(text)
        except Exception:
            logger.debug("status callback failed", exc_info=True)
    return say


def _fail(out, reason, detail=""):
    out.reason = reason
    out.notice = notice_for(reason)
    if reason == R_CANCELLED:
        logger.info("Speaker labels cancelled")
    elif reason in BENIGN:
        # Expected, no problem (a recording too short to label): not a warning.
        logger.info("Speaker labels skipped (%s): %s", reason, detail or _WHY.get(reason, ""))
    else:
        logger.warning("Speaker labels skipped (%s): %s", reason, detail or _WHY.get(reason, ""))
    return out


def _percent_reporter(say, label, step=2):
    last = [-step]

    def report(done, total):
        try:
            frac = float(done) / float(total) if total else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            return
        pct = max(0, min(100, int(frac * 100)))
        if pct >= last[0] + step:
            last[0] = pct
            say("%s %d%%" % (label, pct))
    return report


def _ensure_speaker_tool(d, privacy, say, out):
    """The speaker tool must be here (sherpa-onnx and its model; the model is
    downloaded once, never in Privacy mode). None when it is, else ``out`` already
    failed with the reason."""
    if not d.sherpa_installed():
        return _fail(out, R_NO_COMPONENT, "sherpa-onnx is not installed")
    if not d.models_downloaded():
        if privacy:
            return _fail(out, R_PRIVACY, "speaker model missing, Privacy mode is on")
        say("Downloading the speaker model…")
        d.download_models(on_progress=say)
        if not d.models_downloaded():
            return _fail(out, R_DOWNLOAD, "the speaker model download did not finish")
    if not d.is_available():
        return _fail(out, R_NO_COMPONENT, "speaker tool not available after the download")
    return None


def _render(d, labeled_parts, names, out):
    """Fill ``out`` from the labelled parts: the speakers' numbering (consistent
    across parts) and one transcript per part. False when every text is empty."""
    base = speaker_labels.speaker_names(names) if out.mode in ("sources", "call") else dict(names or {})
    flat = [seg for ls in labeled_parts for seg in ls]
    label_map = d.speaker_label_map(flat, names=base)
    all_names = dict(label_map)
    all_names.update(base)
    out.parts = labeled_parts
    out.texts = [d.build_speaker_transcript(ls, names=all_names).strip() for ls in labeled_parts]
    out.speakers = len(label_map)
    if not any(out.texts):
        out.texts = []
        return False
    return True


def build_transcript(rec, parts, *, language=None, privacy=False, status=None,
                     should_cancel=None, names=None, diarizer=None, diarize=True,
                     reader=None, model=None):
    """Label ``parts`` (a list of Part) and return an Outcome whose ``texts`` are
    the speaker transcripts, one per part.

    ``rec``: the recorder (transcribe_segments); ``language``: what to decode
    ("multi" for mixed languages, a code, or None = detect); ``status(text)``:
    the status line (also first-use download progress); ``should_cancel()``;
    ``names``: {diarization speaker id: name} for a later step that knows who is
    who (names read off a shared screen).

    ``reader(audio, language=, should_cancel=, on_progress=)`` replaces the re-read of each
    part's audio with its own timestamped segments ([{"start", "end", "text"}], or None to
    cancel): a session whose words came from a cloud engine passes its live pieces here
    (AudioRecorder.piece_segments), so labels and names are worked out on those words instead
    of on a local Whisper text that would replace them. ``model``: the Whisper model the
    re-read uses (a Live Assistant session's own pick); None = the recorder's (Dictation's).

    ``diarize=False`` is the fast half, used at Stop: the re-read and the labels by
    SOURCE only - the transcript says "You" (the microphone) and "Them" (everyone on
    the call), no speaker tool is needed and nothing slow runs. For a part with both
    tracks, or a ``call_only`` part (a meeting recorded from the system sound only:
    every segment is "Them"). ``Outcome.parts`` holds the labelled segments to keep
    (segments.json); name_the_call() later turns "Them" into Speaker 1..N from them."""
    d = diarizer or diarization
    say = _say(status)
    out = Outcome()
    cancelled = should_cancel or (lambda: False)
    t_start = time.monotonic()
    try:
        parts = list(parts or [])
        if not parts:
            return _fail(out, R_NO_AUDIO, "no recording parts")

        known = [p.known_seconds() for p in parts]
        if all(k is not None for k in known) and sum(known) < MIN_SEC:
            return _fail(out, R_TOO_SHORT, "%.1f s of audio" % sum(known))

        # 1. the speaker tool (the fast half does not diarize: it needs none)
        if diarize:
            failed = _ensure_speaker_tool(d, privacy, say, out)
            if failed is not None:
                return failed
            if cancelled():
                return _fail(out, R_CANCELLED)

        call = (not diarize) and all(p.call_only for p in parts)
        sources = (not call) and all(p.has_tracks for p in parts)
        if not diarize and not (sources or call):
            return _fail(out, R_ERROR, "nothing to label by source: the two tracks are missing")
        out.mode = "call" if call else ("sources" if sources else "voices")

        # 2. every part, read again with timestamps
        data = []                      # per part: dict(segments, mic_rms, sys_rms, audio, system, offset, seconds)
        offset = 0.0
        t_read = time.monotonic()
        for i, part in enumerate(parts):
            if cancelled():
                return _fail(out, R_CANCELLED)
            audio, mic, system = part.load()
            seconds = (len(audio) / float(SAMPLE_RATE)) if audio is not None else 0.0
            row = {"segments": [], "mic_rms": [], "sys_rms": [], "audio": None, "system": None,
                   "offset": offset, "seconds": seconds}
            data.append(row)
            offset += seconds
            if seconds < MIN_SEC:
                continue
            if reader is not None:
                # The words are already there (their own timestamps): nothing is read again.
                segments = reader(audio, language=language, should_cancel=should_cancel,
                                  on_progress=None)
            else:
                label = "Re-reading the recording…" if len(parts) == 1 else \
                    "Re-reading recording %d of %d…" % (i + 1, len(parts))
                say(label)
                extra = {"model_name": model} if model else {}
                segments = rec.transcribe_segments(
                    audio, language=language, should_cancel=should_cancel,
                    on_progress=_percent_reporter(say, label), **extra)
            if segments is None:
                return _fail(out, R_CANCELLED)
            row["segments"] = [dict(s) for s in segments]
            if sources and segments:
                say("Working out who spoke…")
                row["mic_rms"] = speaker_labels.frame_rms(mic)
                row["sys_rms"] = speaker_labels.frame_rms(system)
                if diarize:
                    row["system"] = system     # only the fast half has no use for the call's sound here
            elif not (sources or call):
                row["audio"] = audio
        audio = mic = system = None          # the last part's arrays: the rows keep what is still needed
        out.timings["reread"] = round(time.monotonic() - t_read, 1)
        total_seconds = offset
        if total_seconds < MIN_SEC:
            return _fail(out, R_TOO_SHORT, "%.1f s of audio" % total_seconds)
        out.segments = sum(len(r["segments"]) for r in data)
        if not out.segments:
            return _fail(out, R_NO_SEGMENTS, "the speech model returned nothing")
        if cancelled():
            return _fail(out, R_CANCELLED)

        # 3. who is who
        labels = None
        turns = []
        joined, mapping = None, []
        need_turns = bool(diarize)
        if call:
            # A meeting recorded from the system sound only: everyone is the call.
            labels = [[{"source": speaker_labels.SRC_THEM, "you_share": 0.0, "basis": "call"}
                       for _ in r["segments"]] for r in data]
        elif sources:
            labels = [speaker_labels.label_segments(r["segments"], r["mic_rms"], r["sys_rms"])
                      for r in data]
            need_turns = need_turns and any(lab["source"] == speaker_labels.SRC_THEM
                                            for ls in labels for lab in ls)
            if need_turns:
                # Only the stretches where the call made sound go to the
                # diarizer (it costs time per second of audio it reads).
                joined, mapping = speaker_labels.compact_system(
                    [(r["system"], r["sys_rms"], r["offset"]) for r in data if r["system"] is not None])
            for r in data:
                r["system"] = None           # only the joined stretches are read from here on
        else:
            joined = _concat([r["audio"] if r["audio"] is not None else _zeros(r["seconds"])
                              for r in data])
        if need_turns:
            say("Telling the voices apart…")
            t_dia = time.monotonic()
            ran = joined is not None
            if ran:
                out.timings["diarized_sec"] = round(len(joined) / float(SAMPLE_RATE), 1)
                turns = _diarize(d, joined, say, should_cancel)
                if sources:
                    turns = speaker_labels.expand_turns(turns, mapping)
            out.timings["diarize"] = round(time.monotonic() - t_dia, 1)
            joined = None
            if not turns:
                # (last_error is the LAST run's: only read when this pass ran the diarizer)
                why = (str(getattr(d, "last_error", "") or "") if ran else "") or "no speaker turns"
                if cancelled() or why in ("cancelled", "app quitting"):
                    return _fail(out, R_CANCELLED)
                reason = (R_TIMEOUT if why == "timed out"
                          else R_NO_VOICES if why in ("no speaker turns", "no speech turns")
                          else R_ERROR)
                if not sources:
                    return _fail(out, reason, "diarization returned no turns (%s)" % why)
                out.reason = reason
                out.notice = CALL_TOO_LONG if reason == R_TIMEOUT else CALL_TOGETHER
                logger.warning("Speaker labels: people on the call not told apart (%s) - "
                               "shown as Them", why)
        if cancelled():
            return _fail(out, R_CANCELLED)

        # 4. the transcripts
        labeled_parts = []
        for k, r in enumerate(data):
            if sources or call:
                labeled = speaker_labels.attribute(
                    r["segments"], r["mic_rms"], r["sys_rms"], turns, labels=labels[k],
                    offset=r["offset"])
            else:
                labeled = d.attribute_segments(r["segments"], turns, offset=r["offset"])
            labeled_parts.append(labeled)
        if not _render(d, labeled_parts, names, out):
            return _fail(out, R_NO_SEGMENTS, "the labelled transcript is empty")
        out.timings["total"] = round(time.monotonic() - t_start, 1)
        flat = [seg for ls in labeled_parts for seg in ls]
        you = sum(1 for seg in flat if seg.get("source") == speaker_labels.SRC_YOU)
        logger.info("Speaker labels done: mode=%s parts=%d segments=%d you=%d call-speakers=%d "
                    "(re-read %ss, voices %ss, total %ss)%s",
                    out.mode, len(parts), out.segments, you, out.speakers,
                    out.timings.get("reread"), out.timings.get("diarize", 0), out.timings["total"],
                    "" if not out.reason else " [%s]" % out.reason)
        return out
    except Exception as e:                      # never into the recording pipeline
        logger.warning("Speaker labels failed", exc_info=True)
        out.texts = []
        return _fail(out, R_ERROR, "%s: %s" % (type(e).__name__, str(e)[:200]))


def _concat(arrays):
    """The parts' audio joined in order (one float32 array for the diarizer); a
    single part is not copied."""
    import numpy as np
    arrays = [a for a in arrays if a is not None and len(a)]
    if not arrays:
        return None
    if len(arrays) == 1:
        return np.asarray(arrays[0], dtype=np.float32)
    return np.concatenate([np.asarray(a, dtype=np.float32) for a in arrays])


def _zeros(seconds):
    import numpy as np
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def _diarize(d, audio, say, should_cancel):
    """d.diarize with progress in the status line; ``should_cancel`` ends the
    diarizer's child process early where the diarizer supports it."""
    progress = _percent_reporter(say, "Telling the voices apart…")
    try:
        return d.diarize(audio, on_progress=progress, should_cancel=should_cancel)
    except TypeError:                          # a diarizer without should_cancel
        return d.diarize(audio, on_progress=progress)


# ── later: "Them" becomes Speaker 1..N, from the segments kept at Stop ───────────

class CallPart:
    """One recording part for name_the_call: the segments the re-read found at Stop
    (dicts with start / end / text and source "you" | "them") and a loader for the
    call's own sound (samples at 16 kHz - int16 or float - or None)."""

    def __init__(self, segments, loader, seconds=0.0):
        self.segments = [dict(s) for s in (segments or [])]
        self.seconds = float(seconds or 0.0)
        self._loader = loader

    def load(self):
        return self._loader() if self._loader is not None else None


def _is_them(seg):
    return seg.get("source") != speaker_labels.SRC_YOU


def _span(seg):
    try:
        return max(0.0, float(seg.get("end", 0.0)) - float(seg.get("start", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def name_the_call(parts, *, privacy=False, status=None, should_cancel=None, names=None,
                  diarizer=None):
    """The second half of the speaker pass: tell the people on the call apart.

    ``parts``: a list of CallPart - the segments already read and labelled by source
    at Stop (no second reading of the recording), and each part's call sound. Only the
    stretches where the call made sound go to the diarizer (a child process, ended at
    once by ``should_cancel``). Returns an Outcome like build_transcript's: ``texts``
    are the new transcripts, one per part ("You: ..." / "Speaker 1: ..."); when no one
    could be told apart they are the You / Them text again, with ``reason`` and
    ``notice``. A call that spoke too little (MIN_CALL_SEC) is not worth separating:
    nothing runs, ``reason`` is R_BRIEF and the transcript stays as it is. A single
    remote person is "Speaker 1" only when the diarizer found exactly one voice (voices
    that talk for only moments are folded into the others: merge_minor_speakers); no
    names are invented. Never raises."""
    d = diarizer or diarization
    say = _say(status)
    out = Outcome()
    out.mode = "call"
    cancelled = should_cancel or (lambda: False)
    t_start = time.monotonic()
    try:
        parts = list(parts or [])
        if not parts:
            return _fail(out, R_NO_AUDIO, "no recording parts")
        out.segments = sum(len(p.segments) for p in parts)
        out.call_seconds = round(sum(_span(s) for p in parts for s in p.segments if _is_them(s)), 1)
        if out.call_seconds <= 0.0:
            out.reason = R_NO_CALL
            logger.info("Speaker labels: nobody on the call spoke, nothing to tell apart")
            return out
        if out.call_seconds < MIN_CALL_SEC:
            out.reason = R_BRIEF
            out.notice = CALL_BRIEF
            logger.info("Speaker labels: the call spoke for %.0f s only, kept as Them", out.call_seconds)
            return out

        failed = _ensure_speaker_tool(d, privacy, say, out)
        if failed is not None:
            return failed
        if cancelled():
            return _fail(out, R_CANCELLED)

        # The call's own sound of every part, on one timeline (the parts follow each other).
        rows, offset = [], 0.0
        t_read = time.monotonic()
        for part in parts:
            if cancelled():
                return _fail(out, R_CANCELLED)
            say("Reading the call's sound…")
            samples = part.load()
            seconds = max(part.seconds, (len(samples) / float(SAMPLE_RATE)) if samples is not None else 0.0)
            rms = speaker_labels.frame_rms(samples) if samples is not None and len(samples) else []
            rows.append({"system": samples if rms else None, "rms": rms, "offset": offset})
            offset += seconds
        samples = None
        out.timings["read"] = round(time.monotonic() - t_read, 1)
        joined, mapping = speaker_labels.compact_system(
            [(r["system"], r["rms"], r["offset"]) for r in rows if r["system"] is not None])
        for r in rows:
            r["system"] = None                   # only the joined stretches are read from here on
        if cancelled():
            return _fail(out, R_CANCELLED)

        say("Telling the voices apart…")
        t_dia = time.monotonic()
        turns = []
        ran = joined is not None
        if ran:
            out.timings["diarized_sec"] = round(len(joined) / float(SAMPLE_RATE), 1)
            turns = speaker_labels.expand_turns(_diarize(d, joined, say, should_cancel), mapping)
        out.timings["diarize"] = round(time.monotonic() - t_dia, 1)
        joined = None
        if not turns:
            why = (str(getattr(d, "last_error", "") or "") if ran else "") or "no speaker turns"
            if cancelled() or why in ("cancelled", "app quitting"):
                return _fail(out, R_CANCELLED)
            reason = (R_TIMEOUT if why == "timed out"
                      else R_NO_VOICES if why in ("no speaker turns", "no speech turns")
                      else R_ERROR)
            out.reason = reason
            out.notice = CALL_TOO_LONG if reason == R_TIMEOUT else CALL_TOGETHER
            logger.warning("Speaker labels: people on the call not told apart (%s) - "
                           "shown as Them", why)
        if cancelled():
            return _fail(out, R_CANCELLED)

        labeled_parts = []
        for part, row in zip(parts, rows):
            segments = [{"start": s.get("start", 0.0), "end": s.get("end", 0.0),
                         "text": s.get("text", "")} for s in part.segments]
            labels = [{"source": speaker_labels.SRC_THEM if _is_them(s) else speaker_labels.SRC_YOU}
                      for s in part.segments]
            labeled_parts.append(speaker_labels.attribute(
                segments, [], [], turns, labels=labels, offset=row["offset"]))
        if not _render(d, labeled_parts, names, out):
            return _fail(out, R_NO_SEGMENTS, "the labelled transcript is empty")
        out.timings["total"] = round(time.monotonic() - t_start, 1)
        logger.info("Speaker names done: parts=%d segments=%d call=%.0fs call-speakers=%d "
                    "(read %ss, voices %ss, total %ss)%s",
                    len(parts), out.segments, out.call_seconds, out.speakers,
                    out.timings.get("read"), out.timings.get("diarize", 0), out.timings["total"],
                    "" if not out.reason else " [%s]" % out.reason)
        return out
    except Exception as e:                      # never into the app
        logger.warning("Speaker names failed", exc_info=True)
        out.texts = []
        return _fail(out, R_ERROR, "%s: %s" % (type(e).__name__, str(e)[:200]))


class PassResult:
    """What refine_meeting came to for one meeting folder."""

    DONE = "done"              # "Them" became Speaker 1..N: transcript.txt was replaced
    CONCLUDED = "concluded"    # it ran to its end without changing the transcript (a short call,
    #                            nobody told apart): the meeting's notice says so
    NOTHING = "nothing"        # nothing to do for this folder (no segments, already done, folder gone)
    CANCELLED = "cancelled"    # stopped on request; nothing changed
    FAILED = "failed"          # could not label (reason + notice say why); nothing changed

    def __init__(self, state, reason="", notice="", speakers=0):
        self.state = state
        self.reason = reason
        self.notice = notice
        self.speakers = speakers

    def __repr__(self):
        return "PassResult(%s, %r)" % (self.state, self.reason)


def _call_sound_loader(path, kind):
    """A loader for the call's sound saved with a meeting: the system track (int16)
    of a mic + system sound meeting, or the recording itself when it IS the call."""
    def load():
        try:
            import audio_export
            if kind == "sources":
                samples, _rate = audio_export.read_wav_int16(path)
            else:
                samples, _rate = audio_export.read_wav_float32(path)
            return samples
        except Exception:
            logger.warning("Could not read the call's sound of a meeting", exc_info=True)
            return None
    return load


def _call_sound_path(folder, entry):
    """Where the call's sound of a segments.json part is, or None when it isn't there."""
    if not entry.get("audio"):
        return None
    part = Path(folder) / str(entry.get("audio"))
    path = meeting_store.track_path(part, "system") if entry.get("kind") == "sources" else part
    try:
        return path if path.is_file() and path.stat().st_size > 44 else None
    except OSError:
        return None


def _saved_transcript(folder):
    return meeting_store.load_transcript(folder) if meeting_store.has_transcript_file(folder) else ""


WRITE_TRIES = 4            # the finished pass cost minutes: a file held for a moment is waited out
WRITE_WAIT_S = 1.0


def _write_transcript_patiently(folder, text):
    """meeting_store.write_transcript, tried again a few times a second apart: on
    Windows a virus scanner or a reader holding transcript.txt makes the replace fail
    for a moment, and the whole pass would be lost for it. True when written."""
    for attempt in range(WRITE_TRIES):
        if meeting_store.write_transcript(folder, text):
            return True
        if attempt + 1 < WRITE_TRIES:
            if not Path(folder).is_dir():
                return False
            time.sleep(WRITE_WAIT_S)
    return False


def _record_notice(folder, notice, concluded):
    """The meeting's notices after a pass: an older speaker notice is replaced by
    this pass's (if any); ``speakers_at`` says a pass reached its end."""
    meta = meeting_store.load_meta(folder)
    old = meta.get("notices") if isinstance(meta.get("notices"), list) else []
    notices = [n for n in old if not (isinstance(n, str) and is_speaker_notice(n))]
    if notice and notice not in notices:
        notices.append(notice)
    fields = {"notices": notices}
    if concluded:
        fields["speakers_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meeting_store.update_meta(folder, **fields)


def _finish_pass(folder, data, used, out):
    """After name_the_call: replace the transcript (one atomic write), then
    segments.json and the meeting's notices. Returns a PassResult."""
    if out.cancelled or not Path(folder).is_dir():       # (a deleted meeting is nothing to finish)
        return PassResult(PassResult.CANCELLED, R_CANCELLED)
    if not _saved_transcript(folder).strip():
        # The transcript went while the names were worked out (a delete that could not finish
        # because a file was held open): nothing to name, and nothing of the meeting's text may
        # be written back into what is left of the folder.
        return PassResult(PassResult.CANCELLED, R_CANCELLED)
    if out.reason not in ("", R_NO_VOICES, R_BRIEF, R_NO_CALL, R_TIMEOUT):
        _record_notice(folder, out.notice, concluded=False)        # why, and how to try again
        return PassResult(PassResult.FAILED, out.reason, out.notice)
    changed = False
    if out.texts:
        # Read again just before writing: nothing else may have changed it meanwhile.
        now = _saved_transcript(folder)
        for entry, block in zip(used, out.texts):
            old, new = str(entry["text"]), (block or "").strip()
            if new and new != old and now.count(old) == 1:
                now = now.replace(old, new, 1)
                entry["text"] = new
                changed = True
        if changed and not _write_transcript_patiently(folder, now):
            logger.warning("Speaker names: transcript.txt of %s could not be replaced; it stays "
                           "You / Them", Path(folder).name)
            return PassResult(PassResult.FAILED, R_ERROR)
    concluded = out.reason != R_TIMEOUT
    if concluded:
        for entry in used:
            entry["done"] = True
        meeting_store.write_segments(folder, data)       # best effort: the transcript is already right
    _record_notice(folder, out.notice, concluded=concluded)
    return PassResult(PassResult.DONE if changed else PassResult.CONCLUDED,
                      out.reason, out.notice, out.speakers)


def refine_meeting(folder, *, privacy=False, status=None, should_cancel=None, names=None,
                   diarizer=None):
    """The background speaker pass for one saved meeting: "Them" -> "Speaker 1..N".

    Reads segments.json (written at Stop) and the call's saved sound, runs
    name_the_call, and replaces each part's block in transcript.txt in ONE atomic
    step; the old text is kept as transcript.before_speakers.txt until the pass ends
    (and stays if it dies with an error). The meeting's notices and ``speakers_at``
    follow. The folder is never left half written: a crash, a cancel or an error at
    any point leaves the You / Them transcript exactly as it was. Returns a
    PassResult; never raises."""
    folder = Path(folder)
    try:
        if not folder.is_dir():
            return PassResult(PassResult.NOTHING, R_NO_AUDIO)
        ask = should_cancel

        def should_cancel():
            # A meeting deleted while its names are worked out is not worth the rest of
            # the minutes (and its files would only be recreated by the writes).
            return bool((ask() if ask is not None else False) or not folder.is_dir())
        data = meeting_store.load_segments(folder)
        current = _saved_transcript(folder)
        entries = [e for e in ((data or {}).get("parts") or [])
                   if isinstance(e, dict) and not e.get("done") and e.get("text")
                   and isinstance(e.get("segments"), list) and e.get("kind") in ("sources", "call")]
        # Only the parts whose block is still in the transcript, once (a rewrite or another
        # run may have changed it since): never replace what is not exactly this.
        entries = [e for e in entries if current.count(str(e["text"])) == 1]
        if not entries:
            return PassResult(PassResult.NOTHING)
        used, parts = [], []
        for e in entries:
            path = _call_sound_path(folder, e)
            if path is None:
                logger.info("Speaker names: the call's sound of %s is not saved, skipped", e.get("audio"))
                continue
            used.append(e)
            parts.append(CallPart(
                [{"start": s.get("s", 0.0), "end": s.get("e", 0.0), "text": s.get("t", ""),
                  "source": s.get("src")} for s in e["segments"] if isinstance(s, dict)],
                _call_sound_loader(path, e.get("kind")), e.get("seconds") or 0.0))
        if not parts:
            return PassResult(PassResult.NOTHING, R_NO_AUDIO)

        kept = meeting_store.keep_before_speakers(folder, current)
        out = name_the_call(parts, privacy=privacy, status=status, should_cancel=should_cancel,
                            names=names, diarizer=diarizer)
        try:
            result = _finish_pass(folder, data, used, out)
        except Exception as e:
            # The backup stays (the run did not end); the transcript is only ever replaced whole.
            logger.warning("Finishing the speaker pass of %s failed: %s", folder.name, e, exc_info=True)
            return PassResult(PassResult.FAILED, R_ERROR, notice_for(R_ERROR))
        if kept:
            meeting_store.drop_before_speakers(folder)
        return result
    except Exception as e:
        logger.warning("The speaker pass of %s failed: %s", folder.name, e, exc_info=True)
        return PassResult(PassResult.FAILED, R_ERROR, notice_for(R_ERROR))
