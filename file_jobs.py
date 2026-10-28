"""Audio to text (Gusan 2.0): the job runner and the one job queue.

Integration notes for Wave 4
============================
Status (Wave 4, services): DONE in main.py - 1 (app.file_jobs with
downloader=app.downloads.fetch, shutdown in quit_app), 4 (FILE_DEFAULTS in
main.DEFAULT; file_language seeded from language for 1.9 configs by
main._migrate_config), 5a-c (_segments_with: multilingual=True for "multi",
_last_segments_language), 5d (dictation's refusal uses BUSY_TITLE /
BUSY_FOR_DICTATION), 5f (_verify_gpu skips while _file_job_running), 5g
(_is_meeting_busy reads app.meeting_session), 6 (notify_when_ready ->
"Transcript ready" tray hint unless the page is on screen), 5e
(show_file_transcribe opens the Audio to text page), 9 (Settings › Privacy
"Delete everything"), 11 (the 1.x file tab and its Settings window are
retired: nothing imports ui/file_transcribe.py). The notes below are kept
for reference.

main.py was NOT edited by this wave. What the controller and the pages must do:

1. One owner for file jobs. In AppController.__init__ (GUI thread, after
   self.recorder exists):
       import file_jobs
       self.file_jobs = file_jobs.FileJobController(self)
   It keeps the existing contract exactly: it sets self._file_job_running = True
   while a job runs (and between queued jobs) and False when the queue drains,
   so dictation (main.py _start), meetings (ui/meetings.py _start_meeting, and
   Live Assistance through it) and the idle sweep keep refusing / skipping.
   It sends the same telemetry (file_transcription_started / completed /
   failed {reason: cancelled|error} / saved) and calls
   self._note_local_wait("file", decode_seconds, model) after a finished job
   on this computer (started / completed also carry "engine").
   On quit: self.file_jobs.shutdown().
2. Start / cancel (pages, tray, drops on the window):
       items, msg = app.file_jobs.enqueue(paths)            # opts from cfg
       items, msg = app.file_jobs.enqueue(paths, opts)       # overrides; keys left
                                                             # out still come from cfg
   ``msg`` non-empty = refused (dictation / meeting / another file job in the
   classic window) or a path that is not a file: show it inline, no dialog.
   enqueue() takes any file (PyAV reads more than AUDIO_EXTS); a drop on the
   whole window should accept only is_audio_file() paths.
   app.file_jobs.cancel()          -> the running job ("Stopping" until it returns)
   app.file_jobs.cancel(item_id)   -> a queued one (removed) or the running one
   State for a page opened mid-job: is_running(), current() (item dict),
   pending() (queued items, FIFO).
3. Signals (all delivered on the GUI thread):
       queued(item) · started(item) · progress(int pct, str stage, str eta)
       finished(item) · failed(item, str message) · cancelled(item)
       running_changed(bool)
   failed carries the item first: the reason code is item["reason"]
   (no_audio | unreadable | memory | download | model | no_speech | cloud | error),
   the message is the one plain sentence for the card (an unexpected crash is
   MSG_ERROR; its traceback goes to the log). A cancel is its own signal,
   never a failure.
   ``stage`` is short ("Reading the file", "Downloading Whisper Small",
   "Loading Whisper Small", "Transcribing", "Finding speakers", "Finishing",
   "Stopping") so the card can say "Transcribing · 42%". ``eta`` is ready-made
   copy ("Ready in about 6 min" / "Ready in under a minute" / "").
   Item keys: id, path, name, kind ("voice"|"video"|"call"), opts, state
   ("queued"|"running"|"done"|"failed"|"cancelled"), pct, stage, eta,
   eta_seconds, duration (probed, then exact), model (MODELS key, or the
   cloud model's real id once the job starts), model_name ("Whisper Small",
   "Gemini 2.5 Flash"), engine ("local" | "gusan" | "google" | "mistral",
   set when the job starts), result (run_file_job's dict), transcript_id,
   transcript_dir, word_path, save_error, reason, error.
4. Config: merge FILE_DEFAULTS into main.DEFAULT (file_model "auto",
   file_language "auto", file_speakers, file_timestamps, file_save_word,
   notify_when_ready) and treat them as plain keys in set_setting (no side
   effects). options_from_cfg(cfg) reads them; enqueue() fills every option
   the caller leaves out from it. Existing installs: 1.9 file jobs used the
   global "language" - when load_config first adds file_language to a config
   that lacks it, seed it from cfg["language"] (not "auto"), so a user who set
   e.g. Russian keeps getting Russian files.
   "Save as Word" = after the job, "<stem> transcript.docx" next to the source
   (Documents/Gusan when that folder can't be written, " (2)" on a clash) -
   item["word_path"], tracked as file_transcription_saved
   {format: docx, auto: True}. The founder may still change what it means.
5. main.py fixes (a few lines each, Wave 4):
   a. run_file_job passes the language EXPLICITLY (opts["language"], default
      cfg file_language) to recorder.transcribe_segments, so a stale
      recorder.language_override from the last meeting no longer leaks into
      files. Nothing to change for that.
   b. AudioRecorder._segments_with: when the language is "multi", call
      model.transcribe(..., multilingual=True) (faster-whisper supports it;
      guard with `except TypeError` for an older wheel).
   c. Keep the detected language: right after model.transcribe in
      _segments_with set self._last_segments_language = getattr(_info,
      "language", None). run_file_job resets it to None before the call and
      reads it after, so the transcript header can say "English" when the
      setting was Detect automatically / Mixed languages.
   d. Refusal copy still says "Transcribe Files" (main.py _start ~3849-3852,
      ui/meetings.py ~662-666): use BUSY_TITLE and BUSY_FOR_DICTATION /
      BUSY_FOR_MEETING from this module.
   e. show_file_transcribe -> open the Audio to text page in the shell;
      `show_file_transcribe(paths=...)` can forward to enqueue().
   f. _verify_gpu (main.py ~3015-3025) reloads the model: skip it while
      _file_job_running.
   g. Owner D's meeting_session must refuse to start while
      app._file_job_running, and app._is_meeting_busy() must also report a
      session that runs without the old MeetingsWindow - refusal_message() uses
      it to refuse file jobs during a meeting.
6. "Notify me when notes or transcripts are ready" (cfg notify_when_ready):
   connect file_jobs.finished -> show_tray_hint("Transcript ready",
   item["name"], on_click=open Audio to text with item["transcript_id"]) when
   the setting is on and the page is not on screen.
7. Downloads: build it as FileJobController(app, downloader=app.downloads.fetch)
   (model_manager; worker-thread only, which is where run_file_job calls it as
   fn(name, on_progress=cb(pct|None, got, total))) - so a file job and the
   Speech & AI download card share one download, and a model_manager
   DownloadError's sentence ("Not enough free disk space for this model.")
   reaches the card. Default: main.download_whisper_model (it cannot be
   cancelled; Cancel takes effect when the download ends).
8. Pages (Wave 3/4) use transcript_store: list_transcripts(), load_transcript(id),
   rename_transcript(id, title), update_meta(id, timestamps=...),
   delete_transcript(id), plain_text(doc), export_to(doc, fmt, path),
   suggested_name(doc, fmt), EXPORT_LABELS, file_kind(name), format_duration(s);
   then app.file_jobs.track_saved(fmt) ("docx" | "clipboard" | "txt" | "md" |
   "srt"). Open files / folders with QDesktopServices.openUrl(QUrl.fromLocalFile()),
   never os.startfile (does nothing on Mac). Real model names: whisper_name(key).
9. Privacy "Delete history" also calls transcript_store.delete_all_transcripts().
   Transcripts are kept like meetings (they don't follow save_history).
10. After the queue drains the controller calls app._preload_speech_model()
    when a job on this computer used another model than dictation's, so the
    next Alt+R does not wait 4-9 s for a reload.
11. Retire ui/file_transcribe.py and its Settings tab (ui/settings.py:636-645)
    once the Audio to text page ships; it already runs on run_file_job.

What this module is
===================
* run_file_job(path, opts, recorder, on_progress, should_cancel) - Qt-free,
  lifted from the classic FileTranscribeTab._run_job with the same steps and
  order: probe, decode, model (auto pick / one-time download / explicit load),
  recorder.transcribe_segments, optional diarization, group_segments. Returns
  the result dict, raises JobCancelled or JobError(reason, message).
* Cloud models (file_model "cloud" / "own:google" / "own:mistral"): the queue
  resolves the pick as the job starts (engine_choice.file_session: Pro,
  Privacy mode, the key) into a cloud_speech.Spec, and run_file_job sends the
  decoded file in pieces (cloud_files) instead of loading Whisper; speaker
  labels and paragraphs are the same. A pick that can't run, or Gusan Cloud
  refusing the first piece, runs as Automatic on this computer. Gusan Cloud is asked for
  its "best" profile (the server chooses the engine) and the job stores the model that
  really answered ("gpt-transcribe", "scribe_v2": engine_choice.MODEL_NAMES).
* Automatic for a file in a language Whisper is poor at (Armenian): engine_choice.file_session
  with the job's language - Gusan Cloud (best) when it is allowed (signed-in Pro, no Privacy
  mode), else the Armenian model when it is on this computer, else Whisper with a hint
  (engine_choice.file_note). A cloud pick Gusan Cloud refuses at the first piece is read by the
  Armenian model when it is here instead of failing the file.
* No length limit for anyone (BRIEF rev 2). The file is decoded straight into
  one float32 array sized from the probed length (about 4 bytes per sample
  instead of faster-whisper's ~10 at peak), with progress and Cancel while it
  reads.
* FileJobQueue - the Qt-free queue (FIFO, one job at a time: the model and its
  inference lock are shared), refusal checks, the _file_job_running flag,
  telemetry and saving to transcript_store. FileJobController is its QObject
  face (PySide6 is imported only when that name is first used).
"""
import itertools
import logging
import os
import threading
import time
import weakref

import docx_export
import speech_hy
import transcript_store
from transcript_store import VIDEO_EXTS, file_kind, format_duration  # noqa: F401  (re-exported)

logger = logging.getLogger("file_jobs")

SAMPLE_RATE = 16000

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
              ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4b", ".amr", ".3gp"}

# MODELS keys (main.MODELS), lightest first; the real names shown under plain ones.
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3-turbo", "large-v3")
# The Armenian model (speech_hy) is a local model too: a job may name it ("hy-fastconformer"). It
# reads Armenian only - run_file_job puts any other language on a Whisper model.
LOCAL_MODELS = WHISPER_MODELS + (speech_hy.MODEL_ID,)
WHISPER_NAMES = {"tiny": "Whisper Tiny", "base": "Whisper Base", "small": "Whisper Small",
                 "medium": "Whisper Medium", "large-v3-turbo": "Whisper Large v3 Turbo",
                 "large-v3": "Whisper Large v3"}
# Cloud picks (engine_choice.FILE_CLOUD_VALUES): Gusan Cloud, then your own key.
CLOUD_MODELS = ("cloud", "own:google", "own:mistral")

FILE_DEFAULTS = {
    "file_model": "auto",           # "auto" | a LOCAL_MODELS key | a CLOUD_MODELS value (rail: Accuracy)
    "file_language": "auto",        # "auto" | "multi" | a LANG_NAMES code (rail: Language)
    "file_speakers": True,          # rail: Speaker labels
    "file_timestamps": True,        # rail: Timestamps
    "file_save_word": True,         # rail: Save as Word (on in the approved mockup)
    "notify_when_ready": True,      # Settings > General
}

# Progress-bar share per stage (percent).
_P_READ_END = 6
_P_DOWNLOAD_END = 22
_P_TRANSCRIBE_END_WITH_SPK = 78
_P_TRANSCRIBE_END_NO_SPK = 96
_P_SPEAKERS_END = 96
_P_FINISHING = 97

STAGE_WAITING = "Waiting"
STAGE_STARTING = "Starting"
STAGE_READING = "Reading the file"
STAGE_TRANSCRIBING = "Transcribing"
STAGE_SPEAKER_MODEL = "Downloading the speaker model"
STAGE_SPEAKERS = "Finding speakers"
STAGE_FINISHING = "Finishing"
STAGE_STOPPING = "Stopping"
STAGE_CANCELLED = "Cancelled"
STAGE_DONE = "Done"

BUSY_TITLE = "Audio to text is running"
# BUSY_FOR_DICTATION / BUSY_FOR_MEETING (main.py, meeting_session.py) are read
# through __getattr__ at the bottom: these while a job runs on this computer,
# BUSY_CLOUD while a cloud job runs (it doesn't use the shared speech model).
_BUSY_LOCAL = {
    "BUSY_FOR_DICTATION": ("Wait for it to finish, or cancel it on the Audio to text page. "
                           "Dictation uses the same speech model."),
    "BUSY_FOR_MEETING": ("A file is being converted right now. Wait for it to finish, or "
                         "cancel it on the Audio to text page. Meetings use the same speech model."),
}
BUSY_CLOUD = "Gusan is converting a file. Try again when it's done."
MSG_RECORDING = "Finish the recording first. Audio to text uses the same speech model."
MSG_OTHER_JOB = "A file is already being converted. Wait for it to finish."
MSG_NO_AUDIO = "No audio could be read from this file."
MSG_UNREADABLE = "This file couldn't be read. It may be damaged or not an audio or video file."
MSG_MEMORY = "There isn't enough free memory to read this file. Close some apps and try again."
MSG_NO_SPEECH = "No speech was detected in this file."
MSG_ERROR = "Something went wrong while converting this file. Try again."
# Gusan Cloud could not take the file and this computer's Whisper is poor at its language
# (speech_langs.LOCAL_WHISPER_POOR): the file is never put through Whisper under a cloud pick.
MSG_POOR_LOCAL = ("Gusan Cloud couldn't take this file just now, and Whisper on this computer isn't "
                  "reliable for {language}. Try again in a moment, or use your own Google key.")


class JobCancelled(Exception):
    """The user cancelled the job."""


class JobError(Exception):
    """A job failed. ``reason``: no_audio | unreadable | memory | download |
    model | no_speech | cloud | error. ``message``: one plain sentence for the card."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason
        self.message = message


# ── small helpers (also used by the classic window) ───────────────────────────

def whisper_name(model):
    if not model:
        return "Whisper"
    if speech_hy.is_hy_model(model):
        return speech_hy.REAL_NAME
    return WHISPER_NAMES.get(model, f"Whisper {model}")


def display_name(model):
    """The plain model name a card shows: "Whisper Small", or a cloud model's
    real name ("gemini-2.5-flash" -> "Gemini 2.5 Flash")."""
    if not model or model in WHISPER_NAMES or speech_hy.is_hy_model(model):
        return whisper_name(model)
    try:
        import engine_choice
        return engine_choice.model_display_name(model)
    except Exception:
        return str(model)


def is_audio_file(path):
    return os.path.splitext(str(path or ""))[1].lower() in AUDIO_EXTS


def _fmt_dur(sec):
    return format_duration(sec)


def _fmt_eta(sec):
    """Classic window: "about 6 min left"."""
    if sec < 50:
        return "under a minute left"
    return f"about {max(1, int(round(sec / 60.0)))} min left"


def ready_in_text(sec):
    """Running card: "Ready in about 6 min"; "" when not known yet."""
    if sec is None:
        return ""
    if sec < 50:
        return "Ready in under a minute"
    mins = max(1, int(round(sec / 60.0)))
    if mins < 60:
        return f"Ready in about {mins} min"
    h, m = divmod(mins, 60)
    return f"Ready in about {h} h {m:02d} min" if m else f"Ready in about {h} h"


def normalize_options(opts=None):
    """{"model": key|None (None = Automatic), "language": code|None (None = the
    recorder's default), "speakers", "timestamps", "save_word"}."""
    o = dict(opts or {})
    model = o.get("model")
    if not model or str(model).lower() == "auto":
        model = None
    language = o.get("language") or None
    return {"model": model, "language": language,
            "speakers": bool(o.get("speakers", True)),
            "timestamps": bool(o.get("timestamps", True)),
            "save_word": bool(o.get("save_word", False))}


def options_from_cfg(cfg):
    """Job options from the Audio to text rail settings (FILE_DEFAULTS keys).
    A cloud pick stays as it is: the queue resolves it when the job starts."""
    get = (cfg or {}).get
    model = get("file_model", FILE_DEFAULTS["file_model"])
    if model not in LOCAL_MODELS and model not in CLOUD_MODELS:
        model = None
    return normalize_options({
        "model": model,
        "language": get("file_language", FILE_DEFAULTS["file_language"]) or "auto",
        "speakers": get("file_speakers", FILE_DEFAULTS["file_speakers"]),
        "timestamps": get("file_timestamps", FILE_DEFAULTS["file_timestamps"]),
        "save_word": get("file_save_word", FILE_DEFAULTS["file_save_word"]),
    })


def refusal_message(app):
    """"" when a file job may start now, else why not (shown inline). The
    recorder and its model are shared with dictation and meetings."""
    if app is None:
        return ""
    if getattr(app, "is_rec", False) or getattr(app, "_busy", False):
        return MSG_RECORDING
    busy = getattr(app, "_is_meeting_busy", None)
    if callable(busy):
        try:
            if busy():
                return MSG_RECORDING
        except Exception:
            pass
    if getattr(app, "_file_job_running", False):
        return MSG_OTHER_JOB
    return ""


def probe_duration(path):
    """Container-metadata duration in seconds (fast, no decode), or None."""
    try:
        import av
        with av.open(path) as c:
            if c.duration:
                return c.duration / 1_000_000.0
            for s in c.streams:
                if s.duration and s.time_base:
                    return float(s.duration * s.time_base)
    except Exception:
        pass
    return None


def pick_auto_model(recorder=None, *, device=None, downloaded=None):
    """Best model for THIS machine: quality-first on CUDA, speed-first on CPU,
    preferring what is already downloaded so Automatic never surprises the user
    with a multi-GB download."""
    if device is None:
        from main import AudioRecorder
        device = AudioRecorder._whisper_device()[0]
    if downloaded is None:
        from main import model_downloaded as downloaded
    if recorder is not None and getattr(recorder, "_cuda_usable", None) is False:
        # A CUDA device exists but its runtime libs are proven broken - the
        # model WILL run on CPU, so a large model would turn a one-hour file
        # into an hours-long job.
        device = "cpu"
    if device == "cuda":
        order = ["large-v3-turbo", "large-v3", "medium", "small", "base", "tiny"]
        fallback = "large-v3-turbo"
    else:
        order = ["small", "base", "tiny"]
        fallback = "base"
    for m in order:
        if downloaded(m):
            return m
    return fallback


# ── decoding (no length limit) ────────────────────────────────────────────────

def _initial_capacity(expected_seconds, sr):
    if expected_seconds and 0 < expected_seconds < 24 * 3600:
        return int(expected_seconds * sr * 1.02) + sr
    return 10 * 60 * sr


def _decode_streaming(path, sr, expected_seconds, on_progress, should_cancel):
    """faster_whisper.decode_audio, frame by frame into one float32 array:
    same resampler (s16 mono at ``sr``), same values, ~4 bytes per sample."""
    import gc

    import av
    import numpy as np

    def valid(frames):
        it = iter(frames)
        while True:
            try:
                yield next(it)
            except StopIteration:
                return
            except av.error.InvalidDataError:
                continue

    def grouped(frames, num_samples=500000):
        fifo = av.audio.fifo.AudioFifo()
        for frame in frames:
            frame.pts = None
            fifo.write(frame)
            if fifo.samples >= num_samples:
                yield fifo.read()
        if fifo.samples > 0:
            yield fifo.read()

    try:
        out = np.empty(_initial_capacity(expected_seconds, sr), dtype=np.float32)
    except MemoryError:
        # Container metadata can overstate the length by hours: start small
        # and grow, so a short file is never refused for a lying header.
        out = np.empty(_initial_capacity(None, sr), dtype=np.float32)
    n = 0
    expected = float(expected_seconds or 0) * sr
    scale = np.float32(1.0 / 32768.0)            # 2**-15: exact, like "/ 32768.0"
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=sr)
    try:
        with av.open(path, mode="r", metadata_errors="ignore") as container:
            frames = grouped(valid(container.decode(audio=0)))
            for group in itertools.chain(frames, [None]):     # None flushes
                if should_cancel is not None and should_cancel():
                    raise JobCancelled()
                for rf in resampler.resample(group):
                    a = rf.to_ndarray().reshape(-1)
                    k = a.shape[0]
                    if n + k > out.shape[0]:
                        bigger = np.empty(max(n + k, int(out.shape[0] * 1.5)), dtype=np.float32)
                        bigger[:n] = out[:n]
                        out = bigger
                    np.multiply(a, scale, out=out[n:n + k])
                    n += k
                if on_progress is not None and expected > 0:
                    on_progress(min(1.0, n / expected))
    finally:
        del resampler
        gc.collect()                 # PyAV resampler objects linger otherwise
    if n < 0.9 * out.shape[0]:
        return out[:n].copy()
    return out[:n]


def decode_file(path, sampling_rate=SAMPLE_RATE, expected_seconds=None,
                on_progress=None, should_cancel=None):
    """The whole file as 16 kHz mono float32. Streams into one array; falls
    back to faster-whisper's decoder if PyAV's frame API misbehaves."""
    try:
        return _decode_streaming(path, sampling_rate, expected_seconds,
                                 on_progress, should_cancel)
    except (JobCancelled, MemoryError):
        raise
    except Exception as e:
        logger.info("Streaming decode failed (%s); using faster-whisper's decoder", e)
    if should_cancel is not None and should_cancel():
        raise JobCancelled()
    import numpy as np
    from faster_whisper import decode_audio
    return np.asarray(decode_audio(path, sampling_rate=sampling_rate), dtype=np.float32)


# ── the job ───────────────────────────────────────────────────────────────────

def run_file_job(path, opts, recorder, on_progress=None, should_cancel=None, *,
                 on_info=None, decode=None, probe=None, model_downloaded=None,
                 download_model=None, auto_model=None, diarizer=None,
                 cloud=None, cloud_send=None):
    """Transcribe one file, on this computer or on a cloud model. Runs on a
    worker thread.

    ``opts``: see normalize_options. ``recorder``: the shared AudioRecorder
    (load_model, transcribe_segments). ``on_progress(pct, stage, eta_seconds)``
    with eta_seconds None while unknown. ``should_cancel()`` is polled.
    ``on_info(dict)`` gets {"duration"} / {"model"} as soon as they are known.
    ``cloud``: a cloud_speech.Spec - the decoded file goes to that model in
    pieces (cloud_files) instead of Whisper; if Gusan Cloud can't be used at
    the first piece, the job goes on here with Automatic. Without it a cloud
    pick in opts runs as Automatic. The keyword hooks replace main /
    faster-whisper / diarization / cloud_speech.send (``cloud_send``) in tests.

    Returns {"path", "name", "duration", "segments" (start, end, text, speaker),
    "paragraphs", "timestamps", "speakers", "speaker_count", "model" (the real
    id: "small", "gemini-2.5-flash"), "engine" ("local" | "gusan" | "google" |
    "mistral"), "language", "language_setting", "decode_seconds"}.
    Raises JobCancelled, or JobError(reason, message).
    """
    opts = normalize_options(opts)
    cancelled = should_cancel or (lambda: False)
    last = {"pct": -1, "stage": None, "eta": None}

    def emit(pct, stage, eta=None):
        pct = int(max(0, min(100, pct)))
        if (pct == last["pct"] and stage == last["stage"]
                and (eta is None or (last["eta"] is not None and abs(eta - last["eta"]) < 5))):
            return
        last.update(pct=pct, stage=stage, eta=eta)
        if on_progress is not None:
            try:
                on_progress(pct, stage, eta)
            except Exception:
                pass

    def info(**kw):
        if on_info is not None:
            try:
                on_info(kw)
            except Exception:
                pass

    def check():
        if cancelled():
            raise JobCancelled()

    # 1. Length from the container (fast) - only for the card and the decode buffer.
    emit(0, STAGE_READING)
    probed = (probe or probe_duration)(path)
    if probed:
        info(duration=float(probed))
    check()

    # 2. Decode the whole file, any length.
    try:
        audio = (decode or decode_file)(
            path, SAMPLE_RATE, expected_seconds=probed,
            on_progress=lambda frac: emit(1 + (_P_READ_END - 1) * frac, STAGE_READING),
            should_cancel=cancelled)
    except (JobCancelled, JobError):
        raise
    except MemoryError:
        raise JobError("memory", MSG_MEMORY) from None
    except Exception as e:
        logger.warning("Could not decode %s: %s", os.path.basename(str(path)), e)
        raise JobError("unreadable", MSG_UNREADABLE) from None
    duration = len(audio) / float(SAMPLE_RATE)
    if duration < 0.5:
        raise JobError("no_audio", MSG_NO_AUDIO)
    info(duration=duration)
    check()
    emit(_P_READ_END, STAGE_READING)
    t_end = _P_TRANSCRIBE_END_WITH_SPK if opts["speakers"] else _P_TRANSCRIBE_END_NO_SPK
    segments, engine, detected, decode_s = None, "local", None, None

    # 3-4 on a cloud model: the file goes out in pieces, a few at a time.
    # Speaker labels and paragraphs (5-6) are the same as on this computer.
    if cloud is not None:
        import cloud_files
        model = getattr(cloud, "model_id", "") or ""
        info(model=model)
        try:
            out = cloud_files.transcribe_file(
                audio, cloud, emit=emit, cancelled=cancelled, send=cloud_send,
                start_pct=_P_READ_END, end_pct=t_end, stage=STAGE_TRANSCRIBING)
        except cloud_files.CloudUnavailable as e:
            # Gusan Cloud refused the first piece (daily cap, Pro ended, no
            # token). Nothing came back, so the whole file runs here instead:
            # models are never mixed - except in a language Whisper here is poor at
            # (Armenian): that file is never put through it, the person is told.
            import speech_langs
            poor = speech_langs.local_whisper_poor(
                getattr(cloud, "language", "auto"), {"mix_languages": list(getattr(cloud, "languages", ()) or ())})
            if poor and not speech_hy.is_hy_model(
                    _armenian_if_ready("", getattr(cloud, "language", None) or opts["language"], recorder)):
                logger.info("Gusan Cloud can't take this file (%s); %s is not read on this computer",
                            e.kind, poor)
                raise JobError("cloud", MSG_POOR_LOCAL.format(language=poor)) from None
            logger.info("Gusan Cloud can't take this file (%s); converting it on this computer%s",
                        e.kind, " with the Armenian model" if poor else "")
        except cloud_files.Cancelled:
            raise JobCancelled() from None
        except cloud_files.NoSpeech:
            raise JobError("no_speech", MSG_NO_SPEECH) from None
        except cloud_files.CloudFileError as e:
            raise JobError("cloud", e.message) from None
        else:
            segments = [dict(s) for s in out["segments"]]
            model = out.get("model_id") or model
            engine = getattr(cloud, "engine", "") or "cloud"
            detected = out.get("language")
        check()

    if segments is None:
        # 3. The model: picked, downloaded once, then loaded with a real error.
        # A cloud pick that reaches here (no Spec, or refused above) is Automatic.
        if opts["model"] and opts["model"] not in CLOUD_MODELS:
            model = opts["model"]
        else:
            model = (auto_model or pick_auto_model)(recorder)
            model = _armenian_if_ready(model, opts["language"] or getattr(cloud, "language", None), recorder)
        model = _model_for_language(model, opts["language"], recorder, model_downloaded)
        name = whisper_name(model)
        info(model=model)
        if model_downloaded is None:
            from main import model_downloaded
        # Decide the stage layout NOW - after the download the same check would
        # always say "downloaded" and misplace the bar.
        did_download = not model_downloaded(model)
        if did_download:
            label = f"Downloading {name}"
            emit(_P_READ_END + 1, label)

            def dl_progress(pct, got=0, total=0):
                if pct is not None:
                    emit(_P_READ_END + (_P_DOWNLOAD_END - _P_READ_END) * pct / 100.0, label)
            if download_model is None:
                from main import download_whisper_model as download_model
            try:
                download_model(model, on_progress=dl_progress)
            except Exception as e:
                logger.warning("Model download failed (%s): %s", model, e)
                # model_manager.DownloadError already carries one plain sentence
                # (disk full, model being removed...); anything else is technical.
                why = str(e).strip() if type(e).__name__ == "DownloadError" else ""
                raise JobError("download", f"{name} couldn't be downloaded. "
                               + (why or "Check your internet connection and try again.")) from None
        check()

        t_start = _P_DOWNLOAD_END if did_download else _P_READ_END
        # Inside transcribe_segments a load failure comes back as [], which would
        # read as "no speech in this file" - so load here, with the real reason.
        emit(t_start, f"Loading {name}")
        try:
            if speech_hy.is_hy_model(model):
                recorder.load_model(model, language=opts["language"])
            else:
                recorder.load_model(model)
        except Exception as e:
            raise JobError("model", f"{name} couldn't be loaded: {str(e)[:200]}") from None
        check()

        # 4. Transcribe (the shared inference lock is held inside).
        stage_t0 = time.monotonic()

        def on_tr_progress(done, total):
            frac = done / total if total else 0.0
            elapsed = time.monotonic() - stage_t0
            eta = elapsed * (1 - frac) / frac if frac > 0.03 and elapsed > 5 else None
            emit(t_start + (t_end - t_start) * frac, STAGE_TRANSCRIBING, eta)

        emit(t_start, STAGE_TRANSCRIBING)
        try:
            recorder._last_segments_language = None
        except Exception:
            pass
        segments = recorder.transcribe_segments(
            audio, language=opts["language"], model_name=model,
            on_progress=on_tr_progress, should_cancel=cancelled)
        if segments is None or cancelled():
            raise JobCancelled()
        if not segments:
            raise JobError("no_speech", MSG_NO_SPEECH)
        segments = [dict(s) for s in segments]
        detected = getattr(recorder, "_last_segments_language", None)
        decode_s = getattr(recorder, "_last_segments_seconds", None)

    # 5. Speaker labels (the speaker pass runs in its own process: Cancel ends it
    # within a second; checked before and after as well).
    if opts["speakers"]:
        if diarizer is None:
            import diarization as diarizer
        emit(t_end, STAGE_SPEAKERS)
        if diarizer.sherpa_installed() and not diarizer.models_downloaded():
            emit(t_end, STAGE_SPEAKER_MODEL)
            diarizer.download_models()
        check()
        if diarizer.is_available():
            def on_sp_progress(done, total):
                frac = done / total if total else 0.0
                emit(t_end + (_P_SPEAKERS_END - t_end) * frac, STAGE_SPEAKERS)
            kw = {"should_cancel": cancelled} if _takes(diarizer.diarize, "should_cancel") else {}
            turns = diarizer.diarize(audio, on_progress=on_sp_progress, **kw)
            if turns:
                segments = diarizer.attribute_segments(segments, turns)
    # A cancel pressed during the speakers stage must not surface a result the
    # user already walked away from.
    check()

    # 6. Paragraphs.
    emit(_P_FINISHING, STAGE_FINISHING)
    for s in segments:
        s.setdefault("speaker", None)
    paragraphs = docx_export.group_segments(segments)
    speakers = sorted({p["speaker"] for p in paragraphs if p.get("speaker") is not None})
    setting = opts["language"] or "auto"
    language = setting if setting not in ("auto", "multi") else (
        detected if isinstance(detected, str) and detected else None)
    return {
        "path": path,
        "name": os.path.basename(str(path)),
        "duration": duration,
        "segments": segments,
        "paragraphs": paragraphs,
        "timestamps": opts["timestamps"],
        "speakers": opts["speakers"],
        "speaker_count": len(speakers),
        "model": model,
        "engine": engine,
        "language": language,
        "language_setting": setting,
        "decode_seconds": decode_s if isinstance(decode_s, (int, float)) else None,
    }


def _armenian_if_ready(model, language, recorder):
    """Automatic, for an Armenian file, picks the Armenian model when it is already on this
    computer (never a download for it, and never for any other language): Whisper on this
    computer is poor at Armenian."""
    try:
        if (not speech_hy.is_hy_model(model) and speech_hy.ready() and speech_hy.listed()
                and speech_hy.is_hy_model(recorder.engine_model(speech_hy.MODEL_ID, language))):
            return speech_hy.MODEL_ID
    except Exception:                       # a stand-in recorder without engine_model, a odd state
        pass
    return model


def _model_for_language(model, language, recorder, downloaded=None):
    """The model that really reads this job: the Armenian one only for an Armenian file; for any
    other language (or Automatic) a Whisper model - the best one on this computer (the recorder
    decides, AudioRecorder.engine_model)."""
    if not speech_hy.is_hy_model(model):
        return model
    try:
        return recorder.engine_model(model, language)
    except Exception:                       # a stand-in recorder without engine_model
        pass
    if speech_hy.covers(language or "auto") and speech_hy.listed():
        return model
    return speech_hy.fallback_whisper(downloaded)


def _takes(fn, name):
    """Does ``fn`` accept a keyword called ``name``? (A speaker tool that predates
    should_cancel - or a test's stand-in - is called without it.)"""
    try:
        import inspect
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


# ── the queue (Qt-free core of FileJobController) ─────────────────────────────

def _spawn_thread(fn):
    threading.Thread(target=fn, daemon=True, name="file-job").start()


# Every queue (the app has one), so the busy sentences can tell whether the
# running job is a cloud one.
_QUEUES = weakref.WeakSet()


def _cloud_job_running():
    return any((q.current() or {}).get("engine", "local") != "local" for q in list(_QUEUES))


class FileJobQueue:
    """One file at a time, first in first out.

    ``spawn(fn)`` runs fn on a worker thread; ``post(fn)`` runs fn on the
    owner's (GUI) thread; ``listener(event, *args)`` gets queued / started /
    progress / finished / failed / cancelled / running_changed. Every state
    change happens in the owner's thread. ``store`` is transcript_store (or a
    stand-in with save_transcript(result) and save_word_copy(doc)).
    """

    def __init__(self, app=None, *, runner=None, store=None, spawn=None, post=None,
                 listener=None, downloader=None, persist=True):
        self.app = app
        self._runner = runner or run_file_job
        self._store = store if store is not None else transcript_store
        self._spawn = spawn or _spawn_thread
        self._post = post or (lambda fn: fn())
        self._listener = listener or (lambda *a: None)
        self._downloader = downloader
        self._persist = persist
        self._queue = []
        self._current = None
        self._seq = 0
        self._reported_running = False
        self._local_model = None        # the last Whisper model a job used, until the queue drains
        _QUEUES.add(self)

    # public
    def is_running(self):
        return self._current is not None

    def current(self):
        return self._current

    def pending(self):
        return list(self._queue)

    def enqueue(self, paths, opts=None):
        """Queue files. Returns (items, message); message is "" or why something
        was not queued (shown inline). ``opts`` overrides the Audio to text
        settings; whatever it leaves out (above all the language) comes from
        them, so the recorder's stale per-meeting language never reaches a file."""
        if isinstance(paths, (str, os.PathLike)):
            paths = [paths]
        paths = [os.fspath(p) for p in (paths or []) if p]
        base = options_from_cfg(getattr(self.app, "cfg", None))
        opts = normalize_options(dict(base, **dict(opts or {})))
        if not paths:
            return [], ""
        if self._current is None and not self._queue:
            msg = refusal_message(self.app)
            if msg:
                return [], msg
        active = {self._key(i["path"]) for i in self._queue}
        if self._current is not None:
            active.add(self._key(self._current["path"]))
        items, missing = [], 0
        for p in paths:
            if not os.path.isfile(p):
                missing += 1
                continue
            key = self._key(p)
            if key in active:
                continue
            active.add(key)
            item = self._new_item(p, opts)
            self._queue.append(item)
            items.append(item)
            self._emit("queued", item)
        msg = ""
        if missing:
            msg = ("That file couldn't be found." if len(paths) == 1
                   else f"{missing} of the files couldn't be found.")
        self._start_next()
        return items, msg

    def cancel(self, item_id=None):
        cur = self._current
        if cur is not None and (item_id is None or cur["id"] == item_id):
            if not cur["_cancel"].is_set():
                cur["_cancel"].set()
                cur.update(stage=STAGE_STOPPING, eta="", eta_seconds=None)
                self._emit("progress", cur["pct"], STAGE_STOPPING, "")
            return True
        if item_id is None:
            return False
        for i, item in enumerate(self._queue):
            if item["id"] == item_id:
                self._queue.pop(i)
                item["state"] = "cancelled"
                self._emit("cancelled", item)
                return True
        return False

    def cancel_all(self):
        while self._queue:
            self.cancel(self._queue[0]["id"])
        self.cancel()

    shutdown = cancel_all

    def track_saved(self, fmt, **props):
        self._track("file_transcription_saved", dict(props, format=fmt))

    # internals
    @staticmethod
    def _key(path):
        return os.path.normcase(os.path.abspath(path))

    def _new_item(self, path, opts):
        self._seq += 1
        model = opts["model"]
        return {"id": self._seq, "path": path, "name": os.path.basename(path),
                "kind": file_kind(path), "opts": dict(opts), "state": "queued",
                "pct": 0, "stage": STAGE_WAITING, "eta": "", "eta_seconds": None,
                "duration": None, "model": model, "model_name": self._planned_name(model),
                "engine": "local", "result": None, "transcript_id": None, "transcript_dir": None,
                "word_path": None, "save_error": None, "reason": None, "error": None,
                "queued_at": time.time(), "started_at": None,
                "_cancel": threading.Event(), "_cloud": None}

    def _is_pro(self):
        is_pro = getattr(self.app, "is_pro", None)
        try:
            return bool(is_pro()) if callable(is_pro) else False
        except Exception:
            return False

    def _signed_in(self):
        """Is a person signed in (None when the app can't say: engine_choice then asks is_pro)."""
        auth = getattr(self.app, "auth", None)
        value = getattr(auth, "is_authenticated", None)
        return bool(value) if value is not None else None

    def _planned_name(self, model):
        """The card's model name while queued: the Whisper name, the cloud
        model's name when that pick can run now, "" for Automatic."""
        if not model:
            return ""
        if model not in CLOUD_MODELS:
            return whisper_name(model)
        try:
            import engine_choice
            cfg = getattr(self.app, "cfg", None) or {}
            return engine_choice.file_engine(cfg, model, self._is_pro())["model_name"]
        except Exception:
            return ""

    def _resolve_cloud(self, item):
        """GUI thread, as the job starts: a cloud pick gets its
        cloud_speech.Spec when it can run now (Pro for Gusan Cloud, Privacy
        mode off, the key saved). Otherwise - or on any error here, so the
        job always starts - it runs as Automatic on this computer."""
        opts = item["opts"]
        automatic = not opts["model"]
        if opts["model"] not in CLOUD_MODELS and not automatic:
            return
        spec = None
        try:
            import cloud_speech
            import engine_choice
            cfg = getattr(self.app, "cfg", None) or {}
            backend, model = engine_choice.file_session(
                dict(cfg, **{engine_choice.FILE_KEY: opts["model"] or engine_choice.FILE_AUTO}),
                self._is_pro(), language=opts["language"] or "auto", signed_in=self._signed_in())
            if automatic and backend != "managed":
                return                      # Automatic stays on this computer (a model there is picked later)
            if backend != "local":
                # The live cfg: Privacy mode and the key are checked again before every piece.
                token_fn = getattr(getattr(self.app, "recorder", None), "get_auth_token", None)
                spec = cloud_speech.spec_from_cfg(cfg, backend, model,
                                                  language=opts["language"] or "auto",
                                                  token_fn=token_fn if callable(token_fn) else None)
        except Exception:
            logger.warning("Cloud model not usable; converting on this computer", exc_info=True)
            spec = None
        if spec is None:
            if automatic:
                return
            opts["model"] = None
            item.update(model=None, model_name="", engine="local", _cloud=None)
        else:
            item.update(model=spec.model_id, model_name=spec.model_name, engine=spec.engine,
                        _cloud=spec)

    def _emit(self, event, *args):
        try:
            self._listener(event, *args)
        except Exception:
            logger.exception("file job listener failed on %s", event)

    def _track(self, event, props):
        track = getattr(self.app, "track", None)
        if callable(track):
            try:
                track(event, props)
            except Exception:
                pass

    def _set_flag(self, on):
        if self.app is not None:
            try:
                self.app._file_job_running = bool(on)
            except Exception:
                pass

    def _start_next(self):
        if self._current is not None or not self._queue:
            return
        item = self._queue.pop(0)
        self._resolve_cloud(item)           # never raises: a problem means Automatic
        item.update(state="running", started_at=time.time(), pct=0, stage=STAGE_STARTING)
        self._current = item
        # Two-way exclusion: dictation / meetings refuse to start while this is
        # up (a local job would hold the shared model's inference lock for the
        # whole file; a cloud job still blocks them, with BUSY_CLOUD).
        self._set_flag(True)
        opts = item["opts"]
        self._track("file_transcription_started", {
            "model": item["model"] if item["_cloud"] is not None else opts["model"],
            "engine": item["engine"], "speakers": opts["speakers"],
            "timestamps": opts["timestamps"]})
        self._emit("started", item)
        if not self._reported_running:
            self._reported_running = True
            self._emit("running_changed", True)
        self._spawn(lambda: self._work(item))

    def _post_safe(self, fn):
        try:
            self._post(fn)
        except RuntimeError:
            pass                    # the controller is gone (app quitting)

    def _work(self, item):
        """Worker thread: run, then save; state changes are posted back."""
        result, err, extra = None, None, {}
        cancel = item["_cancel"]
        try:
            recorder = getattr(self.app, "recorder", None)
            if recorder is None:
                from main import AudioRecorder
                recorder = AudioRecorder()
            kwargs = {"on_info": lambda info: self._post_safe(lambda: self._on_info(item, info))}
            if self._downloader is not None:
                kwargs["download_model"] = self._downloader
            if item.get("_cloud") is not None:
                kwargs["cloud"] = item["_cloud"]
            result = self._runner(
                item["path"], item["opts"], recorder,
                lambda pct, stage, eta=None: self._post_safe(
                    lambda: self._on_progress(item, pct, stage, eta)),
                cancel.is_set, **kwargs)
            if cancel.is_set():
                raise JobCancelled()
            if self._persist:
                extra = self._save(item, result)
        except JobCancelled:
            result, err = None, ("cancelled", "")
        except JobError as e:
            result, err = None, (e.reason, e.message)
        except MemoryError:
            logger.warning("File job ran out of memory")
            result, err = None, ("memory", MSG_MEMORY)
        except Exception:
            # The details go to the log; the card gets one plain sentence.
            logger.exception("File job failed")
            result, err = None, ("error", MSG_ERROR)
        self._post_safe(lambda: self._on_done(item, result, err, extra))

    def _save(self, item, result):
        extra = {}
        try:
            folder = self._store.save_transcript(result)
            extra["transcript_dir"] = str(folder)
            extra["transcript_id"] = os.path.basename(str(folder))
        except Exception as e:
            logger.warning("Transcript not saved: %s", e)
            extra["save_error"] = "The transcript couldn't be saved on this computer."
        if item["opts"].get("save_word"):
            try:
                path = str(self._store.save_word_copy(result))
                extra["word_path"] = path
                if extra.get("transcript_id"):
                    try:
                        self._store.update_meta(extra["transcript_id"], word_path=path)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("Word copy not saved: %s", e)
                extra["save_error"] = "The Word document couldn't be saved."
        return extra

    def _on_info(self, item, info):
        if item is not self._current:
            return
        if info.get("duration"):
            item["duration"] = float(info["duration"])
        if info.get("model"):
            item["model"] = info["model"]
            item["model_name"] = display_name(info["model"])
            if info["model"] in LOCAL_MODELS:
                item["engine"] = "local"        # Gusan Cloud refused it: this computer now
        self._emit("progress", item["pct"], item["stage"], item["eta"])

    def _on_progress(self, item, pct, stage, eta):
        if item is not self._current:
            return
        if item["_cancel"].is_set():
            stage, eta = STAGE_STOPPING, None
        text = ready_in_text(eta)
        item.update(pct=max(item["pct"], int(pct)), stage=stage, eta=text, eta_seconds=eta)
        self._emit("progress", item["pct"], stage, text)

    def _on_done(self, item, result, err, extra):
        took = round((time.time() - (item.get("started_at") or time.time())) / 60, 1)
        if item is self._current:
            self._current = None
        item.update(extra)
        item["eta"], item["eta_seconds"] = "", None
        if err is None and result is not None:
            model = result.get("model")
            local = model in LOCAL_MODELS
            engine = result.get("engine") or ("local" if local else item.get("engine") or "local")
            item.update(state="done", pct=100, stage=STAGE_DONE, result=result,
                        duration=result.get("duration"), model=model,
                        model_name=display_name(model), engine=engine)
            note = getattr(self.app, "_note_local_wait", None)
            if local and callable(note):
                # Decode time only - not the model download or diarization.
                decode = result.get("decode_seconds")
                try:
                    note("file", decode if decode is not None else took * 60, model)
                except Exception:
                    pass
            self._track("file_transcription_completed", {
                "model": model,
                "engine": engine,
                "audio_minutes": round((result.get("duration") or 0) / 60, 1),
                "took_minutes": took,
                "speakers": result.get("speaker_count", 0),
                "timestamps": result.get("timestamps", False),
            })
            if item.get("word_path"):
                self._track("file_transcription_saved", {"format": "docx", "auto": True})
            outcome = ("finished", item)
        elif err[0] == "cancelled":
            item.update(state="cancelled", stage=STAGE_CANCELLED, reason="cancelled")
            self._track("file_transcription_failed", {"reason": "cancelled", "took_minutes": took})
            outcome = ("cancelled", item)
        else:
            item.update(state="failed", reason=err[0], error=err[1])
            self._track("file_transcription_failed", {"reason": "error", "took_minutes": took})
            outcome = ("failed", item, err[1])
        if item.get("model") in LOCAL_MODELS:
            self._local_model = item["model"]
        if self._current is None and not self._queue:
            self._set_flag(False)
        self._emit(*outcome)
        # A listener may have queued (and started) another file meanwhile.
        if self._current is None:
            if self._queue:
                self._start_next()
            elif self._reported_running:
                self._reported_running = False
                self._emit("running_changed", False)
                self._restore_dictation_model()

    def _restore_dictation_model(self):
        """A job on this computer may have evicted dictation's model: load it
        back (only if it's on disk, in the background) so the next Alt+R is
        instant. Cloud jobs never load a Whisper model."""
        app = self.app
        reload = getattr(app, "_preload_speech_model", None)
        want = (getattr(app, "cfg", None) or {}).get("whisper_model")
        used, self._local_model = self._local_model, None
        if callable(reload) and used and want and used != want:
            try:
                reload()
            except Exception:
                pass


# ── the QObject face (PySide6 imported on first use) ──────────────────────────

def _make_controller_class():
    from PySide6.QtCore import QObject, Qt, Signal

    class FileJobController(QObject):
        """Owned by AppController. See the integration notes at the top."""
        queued = Signal(object)
        started = Signal(object)
        progress = Signal(int, str, str)        # pct, stage, eta text
        finished = Signal(object)               # item
        failed = Signal(object, str)            # item, message
        cancelled = Signal(object)              # item
        running_changed = Signal(bool)
        _call = Signal(object)

        def __init__(self, app=None, parent=None, **kwargs):
            super().__init__(parent)
            # Worker threads post closures here; they run on this object's thread.
            self._call.connect(self._run_posted, Qt.QueuedConnection)
            self.core = FileJobQueue(app, post=self._call.emit, listener=self._relay, **kwargs)

        def _run_posted(self, fn):
            fn()

        def _relay(self, event, *args):
            getattr(self, event).emit(*args)

        def enqueue(self, paths, opts=None):
            return self.core.enqueue(paths, opts)

        def cancel(self, item_id=None):
            return self.core.cancel(item_id)

        def cancel_all(self):
            self.core.cancel_all()

        def shutdown(self):
            self.core.cancel_all()

        def is_running(self):
            return self.core.is_running()

        def current(self):
            return self.core.current()

        def pending(self):
            return self.core.pending()

        def track_saved(self, fmt, **props):
            self.core.track_saved(fmt, **props)

    FileJobController.__qualname__ = "FileJobController"
    return FileJobController


_controller_cls = None


def __getattr__(name):
    global _controller_cls
    if name == "FileJobController":
        if _controller_cls is None:
            _controller_cls = _make_controller_class()
        return _controller_cls
    if name in _BUSY_LOCAL:
        # Read when dictation / a meeting is refused, so it fits the job running now.
        return BUSY_CLOUD if _cloud_job_running() else _BUSY_LOCAL[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
