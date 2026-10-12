"""The meeting recorder without widgets (Gusan 2.0).

One MeetingSession records one meeting at a time through the app's shared
AudioRecorder: start -> (pause / resume) -> stop -> save the recording ->
transcript -> speaker labels -> notes. It was moved out of the 1.x
MeetingsWindow method by method (that window is retired); the Meetings page,
Live Assistant and the calendar reminder all drive this one object, so a
recording keeps going whatever window is open or closed.

Integration notes for Wave 4
============================
Status: DONE - AppController builds app.meeting_session in __init__ (before
every window); _is_meeting_busy / _idle_sweep / _calendar_busy read
session.is_busy (a pause counts as busy, so dictation stays locked out and
Whisper is never swept); toggle_live_assist / show_live_assist feed the card
from sig_chunk / sig_summary (the replay-on-open reads state,
_meeting_title, _meeting_attendees, _live_text, _live_summary_text);
ui/live_assist.py Start/Stop call session.start(source="live_assist") /
stop() and read session.meeting_dir; Settings › Privacy keeps the active
meeting through history_actions.active_meeting_dir; meeting_language /
meeting_notes_model (not user-scoped; "managed" is reset to "" when Pro
lapses) / meeting_consent_reminder are in main.DEFAULT; show_meeting / the
tray / IPC "show_meeting" open the Meetings page.

Owning the session
  * ``get_session(app)`` returns ``app.meeting_session``, creating it on
    first use when an app without one (a test double) asks. AppController
    builds it in __init__ right after the recorder, on the GUI thread. It
    needs from the app: ``recorder``, ``cfg``, ``save_config`` (or
    ``set_setting``), ``track``, ``is_pro``, ``_resolve_action_engine``,
    ``release_models_after_session``, ``prewarm_speech_model``,
    ``force_stop_dictation``, ``is_rec``, ``_file_job_running``,
    ``live_assist``, ``show_tray_hint`` - all read with getattr.
Pages call
  * Start card / calendar: Pro gate (``app._pro_upsell("Meeting recording")``)
    -> ``ui.recording_prompts.ensure_recording_consent(parent, app)`` ->
    ``ok, code, msg =
    session.start(title, attendees, audio_mode, language, notes_engine,
    source="page"|"calendar", calendar_event_id=...)``. Codes:
    consent_needed, busy, no_recorder, file_job, folder, device (show ``msg``).
    Defaults: title "Meeting · Tue 14:30" (``default_title()``), audio mode
    from cfg ``meeting_audio_mode``, language from cfg ``meeting_language``
    (else the recorder's global ``language``), notes engine from cfg
    ``meeting_notes_model`` (in main.DEFAULT as ""; then ``action_model``,
    and a Pro/local default when that is rule-based).
  * Live view: ``state_changed(phase)`` (idle / recording / paused /
    processing / done / failed), ``elapsed_sec()`` for the timer (pauses
    excluded), ``levels(mic, call)`` 0..1 for the two meters (call is 0 in
    microphone-only mode), ``chunk_text(piece)`` + ``live_sentences()`` for
    the live transcript, or ``live_line(piece, who)`` with who "You" / "Them"
    / "" (a loudness hint, only in mic + system sound mode), ``recap(md)`` for
    "So far", ``status(text)`` while processing ("Writing notes…"),
    ``pause()`` / ``resume()``, ``stop(user_notes)``, ``abort()``.
    My notes: ``my_notes`` (prefill - a continued meeting has earlier ones),
    ``update_my_notes(text)`` while typing (saved to my_notes.md, debounced),
    ``stop(text)`` saves the final text.
  * Notes view: ``finished(folder, notes, error)``; error "" = notes ready.
    With an error and a transcript the phase is "failed" and
    ``retry_notes(engine)`` rewrites them; ``retry_notes(engine,
    folder=...)`` rewrites any saved meeting (the "⋯ Rewrite notes"), never
    while recording/processing. ``continue_meeting(folder)`` (Pro) records
    more into it. ``notices()`` / meta "notices" go above the notes; meta
    "notes_engine_label" is the "Written by …" line.
  * ``error(code, message)`` is emitted for every refused start (also returned)
    and when processing fails (code "notes" with the transcript kept,
    "empty" when nothing was captured) - for a toast on whatever page is open.
    After "empty" the phase stays "failed" (``last_error`` set, not busy)
    until the page calls ``reset()`` or the next ``start()``.
Telemetry: unchanged event names (meeting_recording_started/_failed,
meeting_notes_completed/_failed). Pause/resume send nothing - a new event
needs telemetry.ALLOWED_EVENTS + the server allow-list (founder deploy).
Config: start() may write ``meeting_audio_mode`` (only to heal a mode this
system can't capture, as 1.9 did); retry_notes(persist=True) writes
``action_model`` (nothing calls it that way since the 1.x window retired).
Nothing else is written. ``meeting_language`` and ``meeting_notes_model`` are
in main.DEFAULT (default "" = the global ``language`` / ``action_model``) as
plain set_setting keys.
Recorder (main.py AudioRecorder, already done in Wave 2 - nothing else in
main.py changed): ``set_paused(bool)`` / ``paused`` (the record thread keeps
draining the streams, keeps nothing, cuts the pending speech as one chunk at
Pause; still ONE segment, so one transcribe() + one speaker pass and the
2-hour cap work), ``on_source_levels(mic_rms, call_rms)`` (meetings only,
measured before the mix, nothing while paused, one 0, 0 when a system-sound-
only loopback goes silent; None for dictation) and ``chunk_source_energy(idx)``
(mic + system sound only). start_recording() clears ``paused``.
Library side effects to know about:
  * meta.json is now written at Start (title, people, ``started``,
    ``source``), so a meeting the app never finished stays listed.
  * The "engine isn't set up" banner is no longer written into notes.md; it
    is meta ``notices`` / ``notes_notice``, shown above the notes by the
    Meetings page's notes view.
  * meeting_store.update_meta is the only meta writer (read-merge-atomic);
    pages use set_title / mark_seen / meeting_status / search /
    list_live_sessions / save_my_notes.
Quitting mid-meeting (Wave 5 review)
  * ``save_for_quit()`` (AppController quit_app after the user said Quit, and
    a Windows log-off / shutdown without asking): a recording stops and is
    saved the way Stop saves first - My notes, the WAV, the transcript so far
    (the live pieces; QUIT_NOTICE says the last seconds may be missing) - and
    meta ``notes_pending`` marks the notes as not written yet; notes being
    written get the mark too once the recording is on disk.
  * The next launch writes them: AppController._write_pending_notes (+20 s)
    -> ``pending_notes_folders()`` -> ``write_pending_notes(folder)`` - the
    meeting's first notes (history entry, unseen, "Notes ready"). The notes
    view says "Notes not written yet · Write notes" until then. Any finished
    processing run removes the mark.
  * ``holds_recorder``: recording, or transcribing a recording. Notes written
    from a saved transcript (Retry, Rewrite, pending) don't use the recorder,
    so dictation stays available meanwhile (main.py _on_hotkey).
"""
import importlib.util
import json
import logging
import math
import re
import sys
import threading
import time
import types
from pathlib import Path

import actions
import diarization
import meeting_store
import storage
import telemetry
from app_info import APP_VERSION

logger = logging.getLogger("transcribe")

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_PROCESSING = "processing"
STATE_DONE = "done"
PHASE_PAUSED = "paused"
PHASE_FAILED = "failed"

AUDIO_MODES = ("smart_meeting", "default_mic", "system_only")
SOURCES = ("page", "live_assist", "calendar", "classic")

# Refusal codes of start() / continue_meeting() (also emitted as error(code, msg)).
ERR_CONSENT = "consent_needed"
ERR_BUSY = "busy"
ERR_NO_RECORDER = "no_recorder"
ERR_FILE_JOB = "file_job"
ERR_FOLDER = "folder"
ERR_DEVICE = "device"
# Processing failures (error signal only; finished carries the message too).
ERR_NOTES = "notes"
ERR_EMPTY = "empty"

RECAP_INTERVAL = 20.0      # seconds between "So far" updates...
RECAP_MIN_GROWTH = 160     # ...once this much NEW text arrived
SPEAKER_SHARE = 0.8        # one side's share of a chunk's energy to label it

# meta.json: the app quit (or Windows logged off) before this meeting's notes
# were written. The recording and transcript are saved; the next launch writes
# the notes (AppController._write_pending_notes), and until then the notes view
# says "Notes not written yet · Write notes". Any processing run removes it.
META_NOTES_PENDING = "notes_pending"
QUIT_NOTICE = ("Gusan was closed during this meeting, so the transcript may miss its last "
               "few seconds. The recording has everything.")
QUIT_CHUNK_WAIT = 3.0      # s: pieces being transcribed at Quit may still land

_SIGNALS = (
    # name, Qt argument types
    ("state_changed", (str,)),
    ("chunk_text", (str,)),
    ("live_line", (str, str)),
    ("recap", (str,)),
    ("status", (str,)),
    ("levels", (float, float)),
    ("finished", (str, str, str)),
    ("error", (str, str)),
    # 1.9 names, read by main.py / Live Assistant: emitted from worker threads.
    ("sig_chunk", (str,)),
    ("sig_summary", (str,)),
    ("sig_status", (str,)),
    # internal: a transcribed piece + speaker hint, worker -> GUI thread.
    ("_piece", (str, str)),
)


# ── pure helpers ─────────────────────────────────────────────────────────────

def _main_attr(name, default=None):
    """An attribute of the already-loaded main.py, never importing it (the app
    runs it as __main__; ``import main`` would build a second copy)."""
    for key in ("main", "__main__"):
        mod = sys.modules.get(key)
        if mod is not None and hasattr(mod, name):
            return getattr(mod, name)
    return default


def has_loopback():
    """True when this system can capture system sound (WASAPI loopback via
    pyaudiowpatch, Windows)."""
    flag = _main_attr("HAS_LOOPBACK")
    if flag is not None:
        return bool(flag)
    try:
        return importlib.util.find_spec("pyaudiowpatch") is not None
    except (ImportError, ValueError):
        return False


def available_audio_modes(loopback=None):
    """The "Record from" choices this system can capture, best first."""
    loopback = has_loopback() if loopback is None else loopback
    return AUDIO_MODES if loopback else ("default_mic",)


def resolve_audio_mode(saved, requested=None, loopback=None):
    """(mode for this session, healed saved mode). A saved mode this system
    can't capture heals to the best one it can; ``requested`` (Live Assistant
    asks for both sides) wins when capturable, without changing the saved one."""
    valid = available_audio_modes(loopback)
    healed = saved if saved in valid else valid[0]
    mode = requested if requested in valid else healed
    return mode, healed


def _file_job_message():
    """Why a meeting can't start during a file job - Audio to text's own copy
    (file_jobs.BUSY_FOR_MEETING), so both say the same."""
    try:
        from file_jobs import BUSY_FOR_MEETING
        return BUSY_FOR_MEETING
    except Exception:
        return ("A file is being converted right now. Wait for it to finish, or cancel "
                "it on the Audio to text page. Meetings use the same speech model.")


def default_title(now=None):
    """The start card's title when the user typed none: "Meeting · Tue 14:30"."""
    t = time.localtime(now if now is not None else time.time())
    return "Meeting · " + time.strftime("%a %H:%M", t)


def meter_level(rms):
    """A mic/call RMS (0..1 float audio) as a 0..1 meter value on a dB scale:
    -60 dB and below is empty, -10 dB and above is full."""
    try:
        rms = float(rms)
    except (TypeError, ValueError):
        return 0.0
    if not rms > 0.0:
        return 0.0
    db = 20.0 * math.log10(rms)
    return max(0.0, min(1.0, (db + 60.0) / 50.0))


def speaker_hint(energies, share=SPEAKER_SHARE):
    """"You" when the mic carried at least ``share`` of a chunk's energy,
    "Them" when the call did, else "" (both talked, or too quiet). A loudness
    hint only - the real speaker labels come from diarization after Stop."""
    if not energies:
        return ""
    try:
        mic, call = (float(x) for x in energies)
    except (TypeError, ValueError):
        return ""
    total = mic + call
    if not total > 1e-6:
        return ""
    if mic / total >= share:
        return "You"
    if call / total >= share:
        return "Them"
    return ""


def split_sentences(text):
    """The live transcript as one sentence per line (Whisper pieces split
    mid-sentence; re-flowing them reads better)."""
    return [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s]


def engine_label(engine):
    """Plain "who wrote the notes" text for an engine id (meta
    notes_engine_label; the page prefixes "Written by")."""
    local = {"qwen_tiny": "Qwen 1.5B", "qwen_3b": "Qwen 3B", "qwen_7b": "Qwen 7B",
             "gemma_2b": "Gemma 2 2B"}
    if engine == actions.API_MANAGED_ID:
        try:
            import hardware
            model = hardware.CLOUD_WRITER_MODEL
        except Exception:
            model = "Claude Sonnet 5.5"
        return "Gusan Cloud · " + model
    if engine == actions.RULE_BASED_ID:
        return "Built-in formatter"
    if engine in local:
        return "This computer · " + local[engine]
    info = actions.ACTION_MODELS.get(engine) or {}
    if info.get("kind") == "local_llm":
        return "This computer · " + str(info.get("label") or engine)
    if info.get("kind") == "cloud":
        return str(info.get("label") or engine) + " · your key"
    return str(engine or "")


def pending_notes_folders():
    """Meetings whose notes a Quit left for later (meta notes_pending), newest
    first. Disk work: call it on a worker."""
    out = []
    try:
        folders = [p for p in meeting_store.meetings_dir().iterdir() if p.is_dir()]
    except Exception:
        return out
    for folder in sorted(folders, key=lambda p: p.name, reverse=True):
        if meeting_store.load_meta(folder).get(META_NOTES_PENDING):
            out.append(str(folder))
    return out


def default_engine_id(app):
    """Notes engine to use when none is chosen (the saved one is rule-based):
    Gusan Cloud for Pro, else a local model that is actually downloaded, else
    Gusan Cloud (which then degrades with a notice) - never a default that
    would go straight to the built-in formatter."""
    try:
        if app is not None and app.is_pro():
            return actions.API_MANAGED_ID
        import local_llm
        for mid in local_llm.MODEL_CATALOG:
            if local_llm.model_downloaded(mid):
                return mid
    except Exception:
        pass
    return actions.API_MANAGED_ID


class _PySignal:
    """A Qt-like signal for the Qt-free engine: connect/disconnect/emit, slots
    run synchronously on the emitting thread."""

    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def disconnect(self, slot=None):
        if slot is None:
            self._slots.clear()
        else:
            self._slots = [s for s in self._slots if s != slot]

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


# ── the engine ───────────────────────────────────────────────────────────────

class MeetingCore:
    """Everything the session does, without Qt. Subclasses provide the signals
    (``_make_signals``) and timers. No ``__init__`` on purpose: PySide6 calls
    a mixin's __init__ from QObject.__init__; ``_setup(app)`` runs instead."""

    STATE_IDLE = STATE_IDLE
    STATE_RECORDING = STATE_RECORDING
    STATE_PROCESSING = STATE_PROCESSING
    STATE_DONE = STATE_DONE

    # ── construction ──
    def _setup(self, app=None):
        self.app = app
        self.state = STATE_IDLE
        self.paused = False
        self._failed = False
        self.last_error = ""
        self._last_phase = STATE_IDLE
        self._chunks = []
        self._chunks_lock = threading.Lock()
        self._record_started_at = None
        self._record_stopped_at = None
        self._pause_started_at = None
        self._paused_total = 0.0
        self._meeting_dir = None
        # Resume-session state (set by start(resume_dir=...), cleared when the
        # notes land).
        self._resume_dir = None
        self._resume_prior = ""
        self._resume_prior_duration = 0
        self._chunks_path = None
        self._final_notes = ""
        self._final_transcript = ""
        self._final_lang = ""
        self._meeting_title = ""
        self._meeting_attendees = ""
        self._summary_downgraded = False
        # Caveats about the transcript/recording itself (a part that couldn't
        # be transcribed, the recording cap) - kept across Retry.
        self._transcript_notices = []
        # The notes engine wasn't set up (built-in formatter used instead).
        self._notes_notice = ""
        self._live_text = ""
        self._live_lines = []
        self._live_summary_text = ""
        self._last_summary_at = 0.0
        self._summary_running = False
        self._summary_len_at_last = 0
        self._user_notes = ""
        self._my_notes_draft = ""
        self._my_notes_at_start = ""
        self._notes_engine = None
        self._session_audio_mode = ""
        self._session_language = None
        self._source = "page"
        self._calendar_event_id = ""
        self._meeting_minutes = None
        self._rewrite_only = False
        # Processing that only writes notes from a saved transcript (Retry,
        # Rewrite, pending notes): the recorder isn't used (holds_recorder).
        self._notes_only = False
        # Worker milestones save_for_quit waits on / reads (any thread).
        self._segment_saved = threading.Event()
        self._transcript_saved = threading.Event()     # this run's transcript.txt
        self._notes_saved = threading.Event()
        self._saved_callbacks = None
        self._make_signals()
        self._piece.connect(self._on_piece)
        self.sig_summary.connect(self._set_live_summary)
        self.sig_status.connect(self._forward_status)
        self.proc_signals.finished.connect(self._on_processing_finished)

    def _make_signals(self):
        raise NotImplementedError

    # Hooks the Qt session implements with QTimers; the plain engine has none.
    def _start_timers(self):
        pass

    def _stop_timers(self):
        pass

    def _schedule_my_notes_save(self):
        self._flush_my_notes()

    def _spawn(self, target, *args):
        threading.Thread(target=target, args=args, daemon=True).start()

    # ── read-only views ──
    @property
    def meeting_dir(self):
        return self._meeting_dir

    @property
    def phase(self):
        """idle / recording / paused / processing / done / failed."""
        if self.state == STATE_RECORDING:
            return PHASE_PAUSED if self.paused else STATE_RECORDING
        if self.state == STATE_PROCESSING:
            return STATE_PROCESSING
        if self._failed:
            return PHASE_FAILED
        return self.state

    @property
    def is_busy(self):
        """Recording (paused included) or processing: the session owns the
        shared recorder / speech model."""
        return self.state in (STATE_RECORDING, STATE_PROCESSING)

    @property
    def holds_recorder(self):
        """The shared recorder is this session's: recording (paused included)
        or turning a recording into its transcript. Writing notes from a saved
        transcript (Retry, Rewrite, notes left by a Quit) uses neither the
        microphone nor the speech model, so a dictation may run meanwhile."""
        if self.state == STATE_RECORDING:
            return True
        return self.state == STATE_PROCESSING and not self._notes_only

    def active_dir(self):
        """The folder being recorded or processed (never delete it), or None."""
        return self._meeting_dir if self.is_busy else None

    @property
    def title(self):
        return self._meeting_title

    @property
    def attendees(self):
        return self._meeting_attendees

    @property
    def my_notes(self):
        return self._my_notes_draft

    @property
    def notes(self):
        return self._final_notes

    @property
    def transcript(self):
        return self._final_transcript

    def notices(self):
        """Caveats to show above the notes (never part of the notes text)."""
        out = list(self._transcript_notices or [])
        if self._notes_notice:
            out.append(self._notes_notice)
        return out

    def live_sentences(self):
        return split_sentences(self._live_text)

    def live_lines(self):
        """[(who, piece)] in arrival order; who is "You" / "Them" / ""."""
        return list(self._live_lines)

    def elapsed_sec(self, now=None):
        """Recorded time of this segment, pauses excluded."""
        start = self._record_started_at
        if not start:
            return 0.0
        now = time.time() if now is None else now
        end = self._record_stopped_at or (self._pause_started_at if self.paused else None) or now
        return max(0.0, end - start - self._paused_total)

    def snapshot(self):
        return {
            "phase": self.phase, "folder": str(self._meeting_dir or ""),
            "title": self._meeting_title, "attendees": self._meeting_attendees,
            "attendees_list": meeting_store.split_attendees(self._meeting_attendees),
            "audio_mode": self._session_audio_mode, "language": self._session_language or "",
            "source": self._source, "calendar_event_id": self._calendar_event_id,
            "elapsed_sec": self.elapsed_sec(), "resumed": self._resume_dir is not None,
            "error": self.last_error,
        }

    # ── helpers ──
    def _cfg(self):
        return getattr(self.app, "cfg", None) or {}

    def _set_cfg(self, key, value):
        app = self.app
        if app is None or app.cfg.get(key) == value:
            return
        setter = getattr(app, "set_setting", None)
        if callable(setter):
            try:
                setter(key, value)
                return
            except Exception:
                logger.debug("set_setting(%s) failed, saving directly", key, exc_info=True)
        app.cfg[key] = value
        try:
            app.save_config()
        except Exception:
            pass

    def _track(self, event, props=None):
        try:
            if self.app is not None and hasattr(self.app, "track"):
                self.app.track(event, props or {})
        except Exception:
            pass

    def _emit_state(self):
        phase = self.phase
        if phase != self._last_phase:
            self._last_phase = phase
            self.state_changed.emit(phase)

    def _refuse(self, code, message):
        self.error.emit(code, message)
        return False, code, message

    def _emit_status(self, text):
        try:
            self.sig_status.emit(text)
        except RuntimeError:
            pass  # session already destroyed

    def _forward_status(self, text):
        self.status.emit(text)

    def _clear_resume_state(self):
        self._resume_dir = None
        self._resume_prior = ""
        self._resume_prior_duration = 0

    def _load_resume(self, folder):
        meta = meeting_store.load_meta(folder)
        self._resume_dir = Path(folder)
        self._resume_prior = meeting_store.load_transcript(folder).strip()
        self._resume_prior_duration = int(meta.get("duration_sec") or 0)
        return meta

    def _effective_notes_engine(self):
        """The engine override for the notes, or None (= the saved
        action_model). The saved one is used unless it is the built-in
        formatter, which is never picked for meeting notes."""
        if self._notes_engine:
            return self._notes_engine
        saved = actions.normalize_action_model(self._cfg().get("action_model"))
        if saved == actions.RULE_BASED_ID:
            return default_engine_id(self.app)
        return None

    # ── start ──
    def start(self, title="", attendees="", audio_mode=None, language=None,
              notes_engine=None, resume_dir=None, source=None, calendar_event_id=None):
        """Start recording. Returns (ok, code, message); on refusal also emits
        error(code, message). ``audio_mode``/``language``/``notes_engine`` are
        this meeting's (never saved); None = the saved meeting settings.
        ``resume_dir`` continues a saved meeting in its folder. ``source``:
        page / live_assist / calendar (or "classic", the 1.x window's; None:
        live_assist when Live Assistant switched the recorder to fast pieces,
        else page).
        Consent is the caller's (ensure_recording_consent); without the saved
        acknowledgement this refuses with "consent_needed"."""
        app = self.app
        rec = getattr(app, "recorder", None) if app is not None else None
        if rec is None:
            return self._refuse(ERR_NO_RECORDER, "The recorder isn't ready yet.")
        # The previous meeting may still be saving/transcribing on its worker:
        # restarting the shared recorder now would hand it this session's audio
        # (the old recording is lost) and leave the new one unstoppable.
        if self.is_busy:
            return self._refuse(ERR_BUSY, "A meeting is already being recorded or saved.")

        # Every Start is a NEW meeting unless it continues a saved one: a
        # finished (or failed resumed) meeting's folder and transcript are
        # never inherited.
        self._clear_resume_state()
        meta = {}
        if resume_dir is not None:
            meta = self._load_resume(resume_dir)

        cfg = app.cfg
        if not cfg.get("meeting_consent_ack"):
            self._clear_resume_state()
            return self._refuse(ERR_CONSENT, "Confirm the recording notice first.")

        # Same exclusion as dictation: a running file transcription owns the
        # shared Whisper model, and a meeting would block on it for its length.
        if getattr(app, "_file_job_running", False):
            self._clear_resume_state()
            return self._refuse(ERR_FILE_JOB, _file_job_message())

        # A plain Alt-R dictation shares this same AudioRecorder + audio device.
        # If one is running, stop it first (and tell the user) so the two don't
        # fight over the device - which crashes - or leak dictation audio into
        # the meeting recording.
        dictation_was_running = False
        try:
            if getattr(app, "is_rec", False) and hasattr(app, "force_stop_dictation"):
                dictation_was_running = bool(app.force_stop_dictation())
        except Exception:
            pass

        title = (title or "").strip() or (meta.get("title") if resume_dir is not None else "")
        if resume_dir is not None and not (attendees or "").strip():
            attendees = meta.get("attendees") or ""
        self._meeting_title = (title or "").strip() or "Untitled Meeting"
        self._meeting_attendees = (attendees or "").strip()

        # Capture mode: this meeting's, else the saved one - healed (and the
        # heal saved, as 1.9 did) when this system can't capture it.
        meeting_mode, healed = resolve_audio_mode(cfg.get("meeting_audio_mode"), audio_mode)
        self._set_cfg("meeting_audio_mode", healed)
        self._session_audio_mode = meeting_mode
        if language is None:
            language = cfg.get("meeting_language") or None
        self._session_language = language
        self._notes_engine = notes_engine or cfg.get("meeting_notes_model") or None
        if source not in SOURCES:
            source = ("live_assist" if getattr(rec, "fast_live_chunks", False) else "page")
        self._source = source
        self._calendar_event_id = str(calendar_event_id or "")

        # Local timestamp folder for auto-save recovery (a resumed meeting
        # keeps writing into its original folder).
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        try:
            if self._resume_dir is not None:
                self._meeting_dir = self._resume_dir
            else:
                # A new meeting never shares a folder - not even with one
                # started (or aborted) within the same second.
                base = Path(storage.path_for("meetings")) / timestamp
                self._meeting_dir, n = base, 2
                while self._meeting_dir.exists():
                    self._meeting_dir = base.with_name(f"{timestamp}_{n}")
                    n += 1
            self._meeting_dir.mkdir(parents=True, exist_ok=True)
            self._chunks_path = self._meeting_dir / "chunks.jsonl"
        except OSError as e:
            self._track("meeting_recording_failed", {"reason": "folder"})
            self._clear_resume_state()
            return self._refuse(ERR_FOLDER, f"Could not create the meeting folder:\n{e}")

        # Save the recorder's existing (dictation/overlay) callbacks so they can
        # be restored when the meeting ends - otherwise dictation's live level
        # meter and partials stay broken after a meeting. While the meeting owns
        # the recorder, silence the overlay callbacks so the dictation overlay
        # never pops up mid-meeting.
        self._saved_callbacks = {
            "on_chunk_complete": getattr(rec, "on_chunk_complete", None),
            "on_levels": getattr(rec, "on_levels", None),
            "on_partial": getattr(rec, "on_partial", None),
            "on_lang_detected": getattr(rec, "on_lang_detected", None),
            "on_source_levels": getattr(rec, "on_source_levels", None),
        }
        rec.on_chunk_complete = self._on_chunk_transcribed
        rec.on_levels = self._on_audio_levels
        rec.on_partial = lambda *a, **k: None
        rec.on_lang_detected = lambda *a, **k: None
        rec.on_source_levels = self._on_source_levels

        # Commit to the recording state before starting so early chunks aren't
        # dropped by the state guard.
        self.state = STATE_RECORDING
        self.paused = False
        self._failed = False
        self.last_error = ""
        self._chunks = []
        self._final_transcript = ""
        self._final_notes = ""
        self._final_lang = ""
        self._summary_downgraded = False
        self._transcript_notices = []
        self._notes_notice = ""
        self._record_stopped_at = None
        self._pause_started_at = None
        self._paused_total = 0.0
        self._live_text = ""
        self._live_lines = []
        self._live_summary_text = ""
        self._last_summary_at = time.time()
        self._summary_len_at_last = 0
        self._summary_running = False
        self._user_notes = ""
        self._rewrite_only = False
        self._my_notes_draft = (meeting_store.load_my_notes(self._meeting_dir)
                                if self._resume_dir is not None else "")
        # What a continued meeting's my_notes.md held: Abort puts it back.
        self._my_notes_at_start = self._my_notes_draft
        self._record_started_at = time.time()

        # Start recording - guard against device-open failures (busy mic, no
        # loopback device, driver errors) so a failure never crashes the app or
        # leaves a fake "recording" state.
        try:
            rec.start_recording(capture_mode=meeting_mode, language=language)
            # Load the speech model while the first seconds are recorded (the
            # idle sweep may have freed it), not when the first piece is due.
            if hasattr(app, "prewarm_speech_model"):
                try:
                    app.prewarm_speech_model()
                except Exception:
                    pass                       # never a recording failure
        except Exception as e:
            self._restore_recorder_callbacks()
            self.state = STATE_IDLE
            self._record_started_at = None
            self._clear_resume_state()
            self._track("meeting_recording_failed", {"reason": "device"})
            self._emit_state()
            return self._refuse(
                ERR_DEVICE,
                f"Could not start the meeting recording:\n\n{e}\n\n"
                "Close any other app that may be using the microphone or audio "
                "device and try again.")

        # Title, people and the real start time now: a meeting the app never
        # finishes (closed mid-meeting) is still listed under its own name.
        first = {}
        if self._resume_dir is None:
            first["started"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(self._record_started_at))
            first["source"] = self._source
        elif not meeting_store.load_meta(self._meeting_dir).get("started"):
            # Continuing a meeting from before 2.0: its folder name is its start.
            started = meeting_store.folder_timestamp(self._meeting_dir.name)
            if started != self._meeting_dir.name:
                first["started"] = started
        if self._calendar_event_id:
            first["calendar_event_id"] = self._calendar_event_id
        if language and language != "auto":
            first["language"] = language
        self._write_meta(**first)

        # Only now that the device is open does the overlay go live (a failed
        # start used to leave it showing a running timer and a dead Stop).
        la = getattr(app, "live_assist", None)
        if la is not None:
            try:
                la.set_meeting_active(True, self._meeting_title, self._meeting_attendees)
                if self._resume_prior:
                    la.feed_transcript(self._resume_prior[-1500:], answer=False)  # context only
            except Exception:
                logger.debug("Live Assistance didn't take the meeting start", exc_info=True)

        if dictation_was_running and hasattr(app, "show_tray_hint"):
            try:
                app.show_tray_hint(
                    "Dictation Stopped",
                    "Your Alt-R dictation was stopped so it isn't mixed into this "
                    "meeting recording.")
            except Exception:
                pass

        telemetry.track("meeting_recording_started", {}, cfg, APP_VERSION)
        self._start_timers()
        self._emit_state()
        return True, "", ""

    def continue_meeting(self, folder, audio_mode=None, language=None,
                         notes_engine=None, source=None):
        """Record more into a saved meeting (same folder, next audio part; the
        transcript continues after a "Resumed" marker and the notes are
        rewritten from everything). Returns start()'s (ok, code, message)."""
        if self.is_busy:
            return self._refuse(ERR_BUSY, "Stop the current meeting and let its notes finish "
                                          "before continuing another.")
        meta = meeting_store.load_meta(folder)
        return self.start(title=meta.get("title") or "Untitled Meeting",
                          attendees=meta.get("attendees") or "", audio_mode=audio_mode,
                          language=language, notes_engine=notes_engine,
                          resume_dir=folder, source=source)

    # ── pause ──
    def pause(self):
        """Pause the recording: nothing is kept until resume(); the timer stops.
        True when paused."""
        if self.state != STATE_RECORDING or self.paused:
            return False
        self._set_recorder_paused(True)
        self.paused = True
        self._pause_started_at = time.time()
        try:
            self.levels.emit(0.0, 0.0)
        except RuntimeError:
            pass
        self._emit_state()
        return True

    def resume(self):
        """Resume a paused recording. True when recording again."""
        if self.state != STATE_RECORDING or not self.paused:
            return False
        self._fold_pause()
        self._set_recorder_paused(False)
        self._emit_state()
        return True

    def _fold_pause(self, now=None):
        if self.paused:
            now = time.time() if now is None else now
            if self._pause_started_at:
                self._paused_total += max(0.0, now - self._pause_started_at)
            self._pause_started_at = None
            self.paused = False

    def _set_recorder_paused(self, on):
        rec = getattr(self.app, "recorder", None) if self.app else None
        if rec is None:
            return
        setter = getattr(rec, "set_paused", None)
        if callable(setter):
            setter(on)
        else:
            rec.paused = bool(on)

    # ── my notes ──
    def update_my_notes(self, text):
        """The My notes box changed: kept, and saved to my_notes.md shortly
        (a crash mid-meeting keeps them)."""
        self._my_notes_draft = text or ""
        if self._meeting_dir is not None and self.state == STATE_RECORDING:
            self._schedule_my_notes_save()

    def _flush_my_notes(self):
        folder = self._meeting_dir
        if folder is None:
            return
        text = self._my_notes_draft or ""
        if text.strip() or (Path(folder) / meeting_store.MY_NOTES_FILE).is_file():
            meeting_store.save_my_notes(folder, text)

    # ── stop ──
    def stop(self, user_notes=None, notes_engine=None):
        """Stop and write the notes (on a worker: save the recording,
        transcript, speakers, notes). ``user_notes``: the My notes text
        (None = the last update_my_notes text). True when stopping."""
        if not self.app or self.state != STATE_RECORDING:
            return False
        # Stop time, not "whenever the notes land": the saved duration must not
        # include processing (or time spent on the Done page before a Retry).
        now = time.time()
        self._fold_pause(now)
        self._record_stopped_at = now
        started = self._record_started_at
        self._meeting_minutes = (round(self.elapsed_sec(now) / 60, 1) if started else None)
        la = getattr(self.app, "live_assist", None)
        if la is not None:
            try:
                la.set_meeting_active(False)
            except Exception:
                logger.debug("Live Assistance didn't take the meeting stop", exc_info=True)

        self.state = STATE_PROCESSING
        self._notes_only = False
        self._segment_saved.clear()
        self._transcript_saved.clear()
        self._notes_saved.clear()
        self._stop_timers()
        if user_notes is not None:
            self._my_notes_draft = user_notes
        self._user_notes = (self._my_notes_draft or "").strip()
        self._flush_my_notes()
        if notes_engine:
            self._notes_engine = notes_engine
        self._emit_state()

        # Everything heavy - stopping the recorder (which joins the recording
        # thread for up to 5s), transcription, diarization and summary - runs on
        # a worker thread so the window never goes "Not Responding".
        self._spawn(self._finalize_meeting)
        return True

    def _finalize_meeting(self):
        # Worker thread: stop the recorder, restore dictation callbacks, then
        # transcribe -> diarize -> summarize.
        try:
            self.app.recorder.stop_recording()
            self._restore_recorder_callbacks()
        except Exception as e:
            logger.warning("Error stopping recorder for meeting finalize: %s", e)
        self._process_meeting_notes()

    # ── abort / reset ──
    def abort(self):
        """Discard the meeting being recorded (a NEW meeting's folder is hidden
        from the list; a continued one keeps everything it had). From a
        finished or failed meeting this just returns to idle. Never while the
        notes are being written. True when done."""
        if self.state == STATE_PROCESSING:
            return False
        recording = self.state == STATE_RECORDING
        # Tell the overlay the session is over (it resets its fast pieces and
        # live state) - but a discarded session has no recording to offer.
        la = getattr(self.app, "live_assist", None) if self.app else None
        if la is not None and recording:
            try:
                la.set_meeting_active(False)
                la.discard_session_audio()
            except Exception:
                logger.debug("Live Assistance didn't take the discard", exc_info=True)
        # A NEW meeting's folder holds only this session's live chunks - keep
        # them out of History (it lists chunk-only folders so a crashed
        # meeting stays recoverable). A resumed meeting's folder is untouched.
        discarded = self._meeting_dir if recording and self._resume_dir is None else None
        continued = self._meeting_dir if recording and self._resume_dir is not None else None
        if recording and self.app and getattr(self.app, "recorder", None):
            rec = self.app.recorder
            # Nothing is kept, so no post-roll: the record thread ends at its
            # next read and the join in stop_recording (GUI thread) is short.
            # (start_recording clears the flag again.)
            req = getattr(rec, "request_abort", None)
            if callable(req):
                try:
                    req()
                except Exception:
                    pass
            rec.stop_recording()
            self._restore_recorder_callbacks()
        self._reset()
        if discarded:
            meeting_store.mark_discarded(discarded)
        elif continued is not None:
            # My notes were autosaved while typing: a discarded continuation
            # leaves the saved meeting's notes as they were.
            meeting_store.save_my_notes(continued, self._my_notes_at_start)
        return True

    def reset(self):
        """Back to idle (a finished meeting stays in the library). Refused
        while recording or processing."""
        if self.is_busy:
            return False
        self._reset()
        return True

    def _reset(self):
        self.state = STATE_IDLE
        self.paused = False
        self._failed = False
        self.last_error = ""
        self._stop_timers()
        # Reset/Abort must drop any pending resume, or the NEXT new meeting
        # would write into the resumed folder and inherit its transcript.
        self._clear_resume_state()
        self._emit_state()

    # ── recorder callbacks (worker / record threads) ──
    def _restore_recorder_callbacks(self):
        """Restore the recorder callbacks that were active before the meeting
        took over (the dictation/overlay handlers). Falls back to safe no-ops so
        a missing saved handler can never leave a dangling reference."""
        if not (self.app and getattr(self.app, "recorder", None)):
            return
        rec = self.app.recorder
        saved = self._saved_callbacks or {}
        rec.on_chunk_complete = saved.get("on_chunk_complete") or (lambda idx, text, lang: None)
        rec.on_levels = saved.get("on_levels") or (lambda lvls: None)
        rec.on_partial = saved.get("on_partial") or (lambda text: None)
        rec.on_lang_detected = saved.get("on_lang_detected") or (lambda code, name: None)
        rec.on_source_levels = saved.get("on_source_levels")

    def _on_audio_levels(self, levels):
        pass  # the dictation bar's 20 bands - unused by meetings

    def _on_source_levels(self, mic_rms, call_rms):
        # Record thread: only emit.
        if self.state != STATE_RECORDING or self.paused:
            return
        try:
            self.levels.emit(meter_level(mic_rms), meter_level(call_rms))
        except RuntimeError:
            pass

    def _speaker_for(self, idx):
        if self._session_audio_mode != "smart_meeting":
            return ""
        rec = getattr(self.app, "recorder", None) if self.app else None
        getter = getattr(rec, "chunk_source_energy", None)
        if not callable(getter):
            return ""
        try:
            return speaker_hint(getter(idx))
        except Exception:
            return ""

    def _on_chunk_transcribed(self, idx, text, lang):
        # NOTE: this runs on a background transcription worker thread. A chunk
        # cut at Pause still arrives while paused (state stays "recording").
        if self.state != STATE_RECORDING:
            return
        chunk = {
            "index": idx,
            "text": text,
            "language": lang,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with self._chunks_lock:
            self._chunks.append(chunk)
        # Recoverable write to JSONL log
        try:
            with open(self._chunks_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(chunk, ensure_ascii=False) + "\n")
        except (OSError, TypeError):
            pass
        # Hand the text to the GUI thread via signals - never touch widgets
        # from this thread (that crashed the meeting recorder on Windows).
        if text:
            try:
                self.sig_chunk.emit(text)
                self._piece.emit(text, self._speaker_for(idx))
            except RuntimeError:
                pass  # session already destroyed

    def _on_piece(self, text, who):
        # GUI thread: the running live text, re-flowed into sentences by readers.
        if self.state != STATE_RECORDING:
            return
        piece = (text or "").strip()
        if not piece:
            return
        self._live_text = (self._live_text + " " + piece).strip()
        self._live_lines.append((who or "", piece))
        self.chunk_text.emit(piece)
        self.live_line.emit(piece, who or "")

    # ── rolling recap ("So far") ──
    def _maybe_recap(self, now=None):
        """Called about once a second while recording (the Qt session's timer):
        regenerate the recap off the GUI thread every RECAP_INTERVAL seconds
        once enough NEW speech arrived - a call per tick would burn quota
        re-summarising the same text. True when a recap started."""
        if self.state != STATE_RECORDING or not self._record_started_at or self.paused:
            return False
        now = time.time() if now is None else now
        grown = len(self._live_text) - self._summary_len_at_last
        # Not during a Live Assistance session: nothing shows the recap there,
        # and every recap call would compete with the answers for the engine.
        rec = getattr(self.app, "recorder", None) if self.app else None
        live_prompter = bool(getattr(rec, "fast_live_chunks", False))
        if (self._summary_running or live_prompter or not self._live_text.strip()
                or grown < RECAP_MIN_GROWTH or now - self._last_summary_at < RECAP_INTERVAL):
            return False
        self._last_summary_at = now
        self._summary_len_at_last = len(self._live_text)
        self._summary_running = True
        self._spawn(self._compute_live_summary, self._live_text)
        return True

    def _live_engine(self):
        """A REAL engine for live features (no rule-based): the configured
        notes engine with Pro routing, else a downloaded local model, else
        (None, cfg)."""
        engine, cfg = self.app._resolve_action_engine()
        info = actions.ACTION_MODELS.get(engine, {})
        kind = info.get("kind")
        has_key = actions.engine_has_key(engine, self.app.cfg)
        has_token = bool((cfg or {}).get("_managed_token"))
        if engine == actions.RULE_BASED_ID or (kind == "cloud" and not has_key) \
                or (kind == "managed" and not has_token):
            local = actions._first_downloaded_local_model()
            return (local, cfg) if local else (None, cfg)
        return engine, cfg

    def _compute_live_summary(self, transcript):
        # Worker thread: the rolling "so far" panel is written by a real LLM
        # (live_recap mode - short bullets). No rule-based fallback: if no
        # engine can run, the panel says how to connect one instead of
        # showing keyword extraction dressed up as a summary.
        summary = ""
        try:
            engine, cfg = self._live_engine()
            if engine is None:
                summary = ("_Connect an AI engine for a live summary: sign in to "
                           "Gusan Pro, or add a key in Settings._")
            else:
                summary = actions.process(
                    transcript, actions.ACTION_LIVE_RECAP, model=engine, config=cfg)
        except Exception as e:
            logger.debug("Live summary failed: %s", e)
        finally:
            self._summary_running = False
        if summary and self.state == STATE_RECORDING:
            try:
                self.sig_summary.emit(summary)
            except RuntimeError:
                pass  # session gone

    def _set_live_summary(self, text):
        # GUI thread (connected to sig_summary).
        if self.state != STATE_RECORDING:
            return
        self._live_summary_text = text
        self.recap.emit(text)

    # ── processing pipeline (worker thread) ──
    def _save_recording_segment(self):
        """Write the raw audio of this segment to the meeting folder. Called
        FIRST in post-processing so a transcription or summary failure can
        never lose the recording itself."""
        try:
            import audio_export
            audio = self.app.recorder.get_full_audio()
            if audio is not None and len(audio) > 1600 and self._meeting_dir:
                self._emit_status("Saving the recording…")
                audio_export.write_wav(
                    meeting_store.next_audio_part_path(self._meeting_dir), audio)
        except Exception as e:
            logger.warning("Could not save meeting audio: %s", e)

    def _duration_sec(self):
        started = self._record_started_at
        if not started:
            return 0
        stopped = self._record_stopped_at or time.time()
        return max(int(self.elapsed_sec(stopped)), 0)

    def _write_meta(self, **extra):
        """meta.json - title, people, recorded duration (all segments, pauses
        excluded) plus ``extra`` keys - merged into what is there.
        Best-effort: never turns a meeting into an error."""
        try:
            if not self._meeting_dir:
                return
            fields = {
                "title": self._meeting_title,
                "attendees": self._meeting_attendees,
                "attendees_list": meeting_store.split_attendees(self._meeting_attendees),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if self._record_started_at:
                fields["duration_sec"] = (self._duration_sec()
                                          + int(self._resume_prior_duration or 0))
            fields.update(extra)
            if meeting_store.update_meta(self._meeting_dir, **fields) is None:
                logger.warning("Could not save meeting details")
        except Exception:
            logger.warning("Could not save meeting details", exc_info=True)

    def _chunks_transcript(self):
        """This segment's live chunks in spoken order, and their most common
        language - the fallback when the recorder's final pass reports an
        error (it then hands back no text at all)."""
        with self._chunks_lock:
            chunks = sorted(self._chunks, key=lambda c: c.get("index") or 0)
        texts = [(c.get("text") or "").strip() for c in chunks]
        langs = [c.get("language") for c in chunks if c.get("language")]
        lang = max(set(langs), key=langs.count) if langs else ""
        # The recorder's own record is complete - it includes chunks that
        # finished after Stop (dropped by the live chunk callback) and the
        # final tail; the live chunk list is only the fallback.
        rec = getattr(self.app, "recorder", None) if self.app else None
        partial = (getattr(rec, "partial_text", "") or "").strip()
        if partial:
            return partial, lang
        return " ".join(t for t in texts if t), lang

    def _recording_cap_notice(self):
        cap = getattr(self.app.recorder, "_full_audio_max", None) if self.app else None
        secs = (cap / diarization.SAMPLE_RATE
                if isinstance(cap, (int, float)) and cap > 0 else 2 * 3600)
        return (f"The saved recording ends at about {meeting_store.format_duration(secs)} "
                "- the most one session keeps. The transcript and notes still cover the "
                "whole meeting (without speaker labels).")

    def _process_meeting_notes(self):
        try:
            self._transcript_notices = []
            self._save_recording_segment()
            # Title/attendees/duration now, not only once notes succeed: a
            # meeting whose transcription or summary fails must still show up
            # in History under its own name.
            self._write_meta()
            self._segment_saved.set()               # a Quit from here on keeps it
            self._emit_status("Finalizing transcript…")
            rec = self.app.recorder
            prior = self._resume_prior
            # 1. Drain the chunk threads and transcribe the tail. ``text`` is
            # every chunk's result in spoken order, tail included - the ONE
            # source of this segment's transcript (self._chunks is in completion
            # order and misses the tail; adding both doubled the transcript).
            text, detected_lang = rec.transcribe()
            text = (text or "").strip()

            # A "!"-prefixed lang is the recorder reporting a real failure
            # (device error, cloud error, transcription exception) - and it then
            # returns no text at all, even if only one chunk failed. Rebuild from
            # the chunks that DID transcribe and say what's missing; only a
            # session with nothing to show reports the error on its own.
            failure = ""
            capture_failed = False
            if str(detected_lang).startswith("!"):
                failure = str(detected_lang).split(":", 1)[-1].strip() or "Audio capture failed."
                capture_failed = str(detected_lang).startswith("!audio")
                text, detected_lang = self._chunks_transcript()
                if not text and not prior:
                    self._final_lang = ""
                    self.proc_signals.finished.emit("", failure)
                    return
                logger.warning("Meeting transcription incomplete (%s); keeping %d chars "
                               "from the live chunks", failure, len(text))
                self._emit_status("Part of the audio couldn't be transcribed - "
                                  "keeping everything that was…")
            elif not text:
                # Nothing joined yet chunks arrived (shouldn't happen - every
                # live chunk is in transcribe()'s result): never call it empty.
                text = self._chunks_transcript()[0]
            self._final_lang = detected_lang or ""
            self._final_transcript = text

            # A resumed session with no NEW speech still has its earlier
            # transcript - regenerate the notes from that instead of calling
            # the meeting empty (which would also reset the resume state).
            if not self._final_transcript and not prior:
                msg = "No transcription recorded. The meeting is empty."
                # In a system-audio mode, a dead-silent loopback means the
                # sound the user heard was playing on a DIFFERENT output
                # device than the Windows default - tell them exactly that.
                mode = (self._session_audio_mode
                        or self._cfg().get("meeting_audio_mode", ""))
                loop_peak = float(getattr(self.app.recorder, "_loopback_peak", 0.0) or 0.0)
                if mode in ("smart_meeting", "system_only") and loop_peak < 0.01:
                    msg = ("No system audio reached the recorder - the sound you "
                           "played is going to a different output device than the "
                           "Windows default one (for example headphones).\n\n"
                           "Make the device you actually listen on the default "
                           "output in Windows sound settings, then start the "
                           "meeting again.")
                self.proc_signals.finished.emit("", msg)
                return

            if getattr(rec, "_full_audio_truncated", False) is True:
                # The recorder stops keeping full audio at its memory cap
                # (~2 h). Speaker labels come from that audio, so they would
                # REPLACE the complete live transcript with one that ends at the
                # cap - keep the complete one (and skip the long diarization).
                self._transcript_notices.append(self._recording_cap_notice())
                self._emit_status("Long meeting - keeping the full transcript "
                                  "(the saved recording ends at the 2-hour cap)…")
            else:
                # Upgrade to a speaker-attributed transcript when local
                # diarization is available: re-transcribe the full audio with
                # timestamps, diarize it, and label each segment ("Speaker 1/2/3:
                # ..."). Falls back silently to the plain transcript on failure.
                try:
                    attributed = self._build_attributed_transcript()
                    if attributed:
                        self._final_transcript = attributed
                        # That pass re-transcribed ALL the retained audio, so
                        # chunks that failed live are covered now; audio a dead
                        # device never captured still isn't.
                        if not capture_failed:
                            failure = ""
                except Exception as e:
                    logger.warning("Speaker attribution failed, using plain transcript: %s", e)

            if failure:
                reason = failure.rstrip(". ")
                marker, notice = (
                    ("The recording stopped here",
                     f"The recording stopped early: {reason}. The transcript and "
                     "notes cover what was captured.")
                    if capture_failed else
                    ("Part of the audio couldn't be transcribed",
                     f"Part of the audio couldn't be transcribed: {reason}. The "
                     "transcript and notes cover the rest."))
                self._final_transcript = f"{self._final_transcript}\n\n— {marker} —".strip()
                self._transcript_notices.insert(0, notice)

            # A resumed meeting continues its earlier transcript.
            if prior:
                new_part = self._final_transcript.strip()
                self._final_transcript = (
                    f"{prior}\n\n— Resumed {time.strftime('%Y-%m-%d %H:%M')} —\n\n{new_part}"
                    if new_part else prior)

            # Persist the transcript to disk IMMEDIATELY - before the summary -
            # so a summary failure (missing key, network, server error) can never
            # lose the recording. This is the durable copy alongside chunks.jsonl.
            try:
                if self._meeting_dir:
                    with open(self._meeting_dir / "transcript.txt", "w", encoding="utf-8") as f:
                        f.write(self._final_transcript)
                    self._transcript_saved.set()        # a Quit from here on keeps it
            except Exception:
                logger.warning("Could not save the meeting transcript", exc_info=True)

            # 2. Summarize as a separate step so Retry can re-run just this
            # part on the saved transcript (no re-record).
            self._summarize_and_finish()
        except Exception as e:
            self._segment_saved.set()
            self.proc_signals.finished.emit("", str(e))

    def _build_attributed_transcript(self):
        """Re-transcribe + diarize the full meeting audio into a
        "Speaker N: ..." transcript. Returns "" when diarization is unavailable
        or anything fails (the caller then keeps the plain transcript). Runs on
        the processing thread after the live chunk threads have drained."""
        rec = self.app.recorder if self.app else None
        if rec is None:
            return ""
        audio = rec.get_full_audio()
        if audio is None or len(audio) < diarization.SAMPLE_RATE:  # < 1s
            return ""

        # First-use one-time model download (~47 MB); no-op once present.
        # Privacy Mode keeps the app off the network: labels only when the
        # models are already here.
        if diarization.sherpa_installed() and not diarization.models_downloaded():
            if self._cfg().get("privacy_mode"):
                return ""
            diarization.download_models()
        if not diarization.is_available():
            return ""

        self._emit_status("Re-transcribing for speaker labels…")
        # Mixed languages stay mixed (the recorder decodes them per window);
        # otherwise the language the live pieces found.
        setting = self._session_language or self._cfg().get("language") or ""
        lang = "multi" if setting == "multi" else (self._final_lang or None)
        segments = rec.transcribe_segments(audio, language=lang)
        if not segments:
            return ""
        self._emit_status("Identifying speakers…")
        turns = diarization.diarize(audio)
        if not turns:
            return ""
        labeled = diarization.attribute_segments(segments, turns)
        return diarization.build_speaker_transcript(labeled).strip()

    def _summarize_and_finish(self):
        """Generate the AI notes for the already-finalized transcript and emit
        the result. Idempotent and safe to call again (Retry / Rewrite notes)
        without re-recording or re-transcribing."""
        try:
            transcript = self._final_transcript or ""
            if not transcript.strip():
                self.proc_signals.finished.emit("", "No transcript to summarize.")
                return

            note_context = transcript
            if self._user_notes:
                note_context += ("\n\nAdditional visual meeting notes provided by "
                                 f"attendee:\n{self._user_notes}")

            # Resolve the engine with Pro routing - a signed-in Pro user runs
            # through the managed cloud (no BYO key). For a non-Pro user whose
            # configured engine is cloud-without-a-key, degrade to the local
            # rule-based summary so notes always generate instead of erroring
            # (which used to strand the whole recording).
            engine, engine_config = self.app._resolve_action_engine(
                action_model=self._effective_notes_engine())
            info = actions.ACTION_MODELS.get(engine, {})
            kind = info.get("kind")
            # Key check is PROVIDER-aware: a Google key only counts for the
            # Gemini engine (matching actions.process's own fallback).
            has_key = actions.engine_has_key(engine, self.app.cfg)
            has_token = bool((engine_config or {}).get("_managed_token"))
            downgraded_from = ""
            if (kind == "cloud" and not has_key) or (kind == "managed" and not has_token):
                # Degrading keeps the notes alive - but NEVER silently: the
                # notice says what happened, and Retry stays offered.
                downgraded_from = engine_label(engine) if kind == "managed" \
                    else info.get("label", engine)
                engine = actions.RULE_BASED_ID
            used = engine
            if (engine_config or {}).get("privacy_mode") and kind in ("cloud", "managed"):
                used = actions.RULE_BASED_ID       # actions.process does the same

            self._emit_status("Writing notes…")
            notes = actions.process(
                note_context,
                actions.ACTION_MEETING_NOTES,
                source_lang=self._final_lang or "auto",
                target_lang="en",
                model=engine,
                config=engine_config,
            )
            self._summary_downgraded = bool(downgraded_from)
            self._notes_notice = ""
            if downgraded_from:
                reason = ("sign in with a Pro account to use it" if kind == "managed"
                          else "add its API key to use it")
                # Shown above the notes and kept in meta - never written into
                # notes.md, so Copy, Word and Notion exports stay clean.
                self._notes_notice = (f"{downgraded_from} isn't set up - {reason}. These "
                                      "notes come from the built-in formatter; pick another "
                                      "engine and try again for AI notes.")

            # Save finalized artifacts. Best-effort: a failure to WRITE the
            # sidecar files must never turn a finished summary into an error.
            try:
                if self._meeting_dir:
                    with open(self._meeting_dir / "notes.md", "w", encoding="utf-8") as f:
                        f.write(notes)
            except Exception:
                logger.warning("Could not save meeting artifacts", exc_info=True)
            self._write_meta(**self._result_meta(
                notes_engine=used, notes_engine_label=engine_label(used), notes_error=None))
            self._notes_saved.set()

            self.proc_signals.finished.emit(notes, "")
        except Exception as e:
            self.proc_signals.finished.emit("", str(e))

    def _result_meta(self, **extra):
        """Meta after a processing run. New notes are unseen - except a rewrite
        the user asked for from the notes they are looking at."""
        fields = {"notices": self.notices(), "notes_notice": self._notes_notice or None,
                  META_NOTES_PENDING: None}       # this run wrote them (or said why not)
        if not self._rewrite_only:
            fields["seen"] = False
        if self._final_lang:
            fields["language"] = self._final_lang
        fields.update(extra)
        return fields

    # ── outcome (GUI thread) ──
    def _on_processing_finished(self, notes, error_msg):
        # The session's AI work is over (notes or not): free the local model
        # now rather than idling with it for minutes.
        if self.app and hasattr(self.app, "release_models_after_session"):
            try:
                self.app.release_models_after_session()
            except Exception:
                pass
        transcript = self._final_transcript or ""
        folder = str(self._meeting_dir or "")
        rewrite = self._rewrite_only
        self._rewrite_only = False

        if error_msg:
            self._track("meeting_notes_failed", {
                "minutes": self._meeting_minutes,
                "transcript_kept": bool(transcript.strip()),
            })
            self._failed = True
            self.last_error = error_msg
            self._final_notes = ""
            if transcript.strip():
                # The recording is NOT lost: the transcript is kept and the
                # notes can be retried (resume state kept for that).
                self.state = STATE_DONE
                self._write_meta(**self._result_meta(notes_error=error_msg))
                code = ERR_NOTES
            else:
                # A true capture failure (nothing transcribed).
                self.state = STATE_IDLE
                self._clear_resume_state()
                if self._meeting_dir and Path(self._meeting_dir).is_dir():
                    self._write_meta(notes_error=error_msg, seen=False, **{META_NOTES_PENDING: None})
                code = ERR_EMPTY
            self._emit_state()
            self.finished.emit(folder, "", error_msg)
            self.error.emit(code, error_msg)
            return

        self.state = STATE_DONE
        self._failed = False
        self.last_error = ""
        self._final_notes = notes
        self._clear_resume_state()

        # Persist to the app history (like dictations) so the notes are
        # findable in the History tab later - unless the user disabled
        # history or is in Privacy Mode. A rewrite of a saved meeting is
        # already there.
        try:
            cfg = self._cfg()
            if not rewrite and cfg.get("save_history", True) and not cfg.get("privacy_mode", False):
                import history as hist
                entry = (f"{self._meeting_title} - Meeting Notes\n\n{notes}"
                         f"\n\n--- Full Transcript ---\n\n{self._final_transcript}")
                hist.save_entry(entry, self._final_lang or "", "meeting",
                                audio_dir=self._meeting_dir)
        except Exception:
            pass

        telemetry.track("meeting_notes_completed", {"minutes": self._meeting_minutes},
                        self._cfg(), APP_VERSION)
        self._emit_state()
        self.finished.emit(folder, notes, "")

    # ── retry / rewrite ──
    def retry_notes(self, engine=None, folder=None, persist=False):
        """Rewrite only the notes from the saved transcript (no re-record) with
        ``engine`` (None = the meeting's engine). ``folder``: a saved meeting
        other than the one just finished. ``persist`` saves the engine as the
        app's action_model (the 1.x window's Retry did). True when started."""
        if self.is_busy or not self.app:
            return False
        if folder is not None and (self._meeting_dir is None
                                   or Path(folder).resolve() != Path(self._meeting_dir).resolve()
                                   or not self._final_transcript.strip()):
            if not self._load_for_rewrite(folder):
                return False
        if not (self._final_transcript or "").strip():
            return False
        # Apply the pick: a retry that can't change the engine would fail
        # identically (same model, same transcript, same context limit).
        if engine:
            self._notes_engine = engine
            if persist:
                self._set_cfg("action_model", engine)
        self.state = STATE_PROCESSING
        self._notes_only = True                 # the saved transcript: no recorder
        self._notes_saved.clear()
        self._failed = False
        self.last_error = ""
        self._emit_state()
        try:
            import local_llm
            local_llm.forget_load_failures()           # Retry = really try the model again
        except Exception:
            pass
        self._spawn(self._summarize_and_finish)
        return True

    def _load_for_rewrite(self, folder):
        """Point the session at a saved meeting so its notes can be rewritten."""
        transcript = meeting_store.load_transcript(folder).strip()
        if not transcript:
            return False
        meta = meeting_store.load_meta(folder)
        self._meeting_dir = Path(folder)
        self._chunks_path = self._meeting_dir / "chunks.jsonl"
        self._meeting_title = str(meta.get("title") or "Untitled Meeting")
        self._meeting_attendees = str(meta.get("attendees") or "")
        self._final_transcript = transcript
        self._final_notes = meeting_store.load_notes(folder)
        self._final_lang = str(meta.get("language") or "")
        self._user_notes = meeting_store.load_my_notes(folder).strip()
        self._my_notes_draft = self._user_notes
        # This meeting's own details, not the last recorded one's: its notes
        # engine is the saved meeting setting (unless retry_notes picks one).
        self._notes_engine = self._cfg().get("meeting_notes_model") or None
        self._source = str(meta.get("source") or "")
        self._calendar_event_id = str(meta.get("calendar_event_id") or "")
        self._summary_downgraded = False
        notices = meta.get("notices") if isinstance(meta.get("notices"), list) else []
        old_notice = meta.get("notes_notice") or ""
        self._transcript_notices = [str(n) for n in notices if n and n != old_notice]
        self._notes_notice = ""
        self._record_started_at = self._record_stopped_at = None   # keep its duration
        self._paused_total = 0.0
        self._clear_resume_state()
        self._meeting_minutes = round(int(meta.get("duration_sec") or 0) / 60, 1)
        # Notes a Quit left for later are the meeting's FIRST notes: a history
        # entry and "unseen", like any finished meeting. Otherwise a rewrite.
        self._rewrite_only = not meta.get(META_NOTES_PENDING)
        return True

    def write_pending_notes(self, folder):
        """Write the notes a Quit (or a Windows log-off) left for later - meta
        notes_pending - from the saved transcript, as that meeting's first
        notes (history entry, unseen, the "Notes ready" notice). True when
        started. A folder with nothing to write them from loses the mark (its
        notes view then shows what there is)."""
        if self.is_busy or not self.app:
            return False
        if not self._load_for_rewrite(folder):
            meeting_store.update_meta(folder, **{META_NOTES_PENDING: None})
            return False
        return self.retry_notes(None)

    # ── quitting ──
    def save_for_quit(self, wait=8.0):
        """The app is quitting (Quit, after the user said so) or Windows is
        logging off / shutting down: keep everything without writing notes.
        A meeting being recorded stops; its recording and the transcript so far
        are saved the way Stop saves them first, and meta marks the notes as
        not written yet (notes_pending) - the next launch writes them. A
        meeting whose notes are being written gets the same mark once its
        recording is on disk (at most ``wait`` s). GUI thread, never a dialog.
        Returns the meeting folder (str), or "" when there was nothing to keep."""
        folder = self._meeting_dir
        if not self.app or folder is None or self.state not in (STATE_RECORDING, STATE_PROCESSING):
            return ""
        if self.state == STATE_RECORDING:
            self._save_recording_for_quit()
            return str(folder)
        # Notes being written (a Stop's full run, or notes from a transcript).
        if not self._notes_only:
            self._segment_saved.wait(max(0.0, float(wait)))   # the worker saves the WAV first
            # Not "is there a transcript.txt": a continued meeting already has
            # its earlier one, which lacks everything said in this part.
            if not self._transcript_saved.is_set():
                self._write_quit_transcript()
        if self._notes_saved.is_set():
            return str(folder)                                 # they made it
        if self._notes_only and self._rewrite_only and (Path(folder) / "notes.md").is_file():
            return str(folder)        # a rewrite of notes it has: those stay as they were
        self._write_meta(seen=False, **{META_NOTES_PENDING: True})
        return str(folder)

    def _save_recording_for_quit(self):
        now = time.time()
        self._fold_pause(now)
        self._record_stopped_at = now
        started = self._record_started_at
        self._meeting_minutes = (round(self.elapsed_sec(now) / 60, 1) if started else None)
        self._stop_timers()                         # also saves a pending My notes edit
        self._user_notes = (self._my_notes_draft or "").strip()
        try:
            self._flush_my_notes()
        except Exception:
            logger.debug("saving My notes at quit failed", exc_info=True)
        la = getattr(self.app, "live_assist", None)
        if la is not None:
            try:
                la.set_meeting_active(False)
            except Exception:
                logger.debug("Live Assistance didn't take the quit", exc_info=True)
        rec = getattr(self.app, "recorder", None)
        if rec is not None:
            # No post-roll: the record thread ends at its next read and closes
            # its own streams (stop_recording joins it) - never from here.
            req = getattr(rec, "request_abort", None)
            if callable(req):
                try:
                    req()
                except Exception:
                    pass
            try:
                rec.stop_recording()
            except Exception:
                logger.warning("Stopping the recorder at quit failed", exc_info=True)
            # Pieces being transcribed right now still count (a few seconds).
            deadline = time.time() + QUIT_CHUNK_WAIT
            for t in list(getattr(rec, "_chunk_threads", None) or []):
                left = deadline - time.time()
                if left <= 0:
                    break
                try:
                    t.join(timeout=left)
                except Exception:
                    pass
        self._save_recording_segment()
        self._write_quit_transcript()
        notices = list(self._transcript_notices or [])
        if QUIT_NOTICE not in notices:
            notices.append(QUIT_NOTICE)
        extra = {}
        with self._chunks_lock:
            langs = [c.get("language") for c in self._chunks if c.get("language")]
        if langs:
            extra["language"] = max(set(langs), key=langs.count)
        self._write_meta(seen=False, notices=notices, notes_error=None,
                         **extra, **{META_NOTES_PENDING: True})
        self._restore_recorder_callbacks()
        self.state = STATE_IDLE
        self.paused = False
        self._failed = False
        self._clear_resume_state()
        self._emit_state()

    def _quit_transcript(self):
        """This segment's live pieces in spoken order (the tail Stop would
        transcribe is in the saved recording), after a continued meeting's
        earlier transcript."""
        with self._chunks_lock:
            chunks = sorted(self._chunks, key=lambda c: c.get("index") or 0)
        text = " ".join(t for t in ((c.get("text") or "").strip() for c in chunks) if t)
        prior = (self._resume_prior or "").strip()
        if prior and text:
            return f"{prior}\n\n— Resumed {time.strftime('%Y-%m-%d %H:%M')} —\n\n{text}"
        return prior or text

    def _write_quit_transcript(self):
        text = self._quit_transcript()
        if not text.strip() or not self._meeting_dir:
            return
        try:
            with open(Path(self._meeting_dir) / "transcript.txt", "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            logger.warning("Could not save the meeting transcript at quit", exc_info=True)


class MeetingEngine(MeetingCore):
    """The session without Qt: plain callback signals that run on the emitting
    thread. For tests and tools; the app uses MeetingSession."""

    def __init__(self, app=None):
        self._setup(app)

    def _make_signals(self):
        for name, _types in _SIGNALS:
            setattr(self, name, _PySignal())
        self.proc_signals = types.SimpleNamespace(finished=_PySignal())


def get_session(app):
    """The app's one MeetingSession (created on first use and kept as
    ``app.meeting_session``). A None app gets a private session."""
    if app is None:
        return MeetingSession(None)
    session = getattr(app, "meeting_session", None)
    if session is None:
        session = MeetingSession(app)
        try:
            app.meeting_session = session
        except Exception:
            pass
    return session


# ── the Qt session ───────────────────────────────────────────────────────────
try:
    from PySide6.QtCore import QObject as _QObject, QTimer as _QTimer, Signal as _QtSignal
except Exception:          # no Qt (tools): MeetingSession falls back to the plain engine
    _QObject = None

if _QObject is not None:
    class MeetingProcessingSignal(_QObject):
        finished = _QtSignal(str, str)  # notes, error_msg

    class MeetingSession(_QObject, MeetingCore):
        """The app's meeting recorder. Lives on the GUI thread; worker threads
        only emit, and every slot that touches state runs on the GUI thread."""

        state_changed = _QtSignal(str)
        chunk_text = _QtSignal(str)
        live_line = _QtSignal(str, str)
        recap = _QtSignal(str)
        status = _QtSignal(str)
        levels = _QtSignal(float, float)
        finished = _QtSignal(str, str, str)
        error = _QtSignal(str, str)
        sig_chunk = _QtSignal(str)
        sig_summary = _QtSignal(str)
        sig_status = _QtSignal(str)
        _piece = _QtSignal(str, str)

        MY_NOTES_SAVE_MS = 1500
        RECAP_TICK_MS = 1000

        def __init__(self, app=None, parent=None):
            _QObject.__init__(self, parent)
            self._recap_timer = _QTimer(self)
            self._recap_timer.setInterval(self.RECAP_TICK_MS)
            self._recap_timer.timeout.connect(self._maybe_recap)
            self._notes_timer = _QTimer(self)
            self._notes_timer.setSingleShot(True)
            self._notes_timer.setInterval(self.MY_NOTES_SAVE_MS)
            self._notes_timer.timeout.connect(self._flush_my_notes)
            self._setup(app)

        def _make_signals(self):
            # Class-level Qt signals; the processing result crosses threads
            # through its own object, as in 1.9.
            self.proc_signals = MeetingProcessingSignal()

        def _start_timers(self):
            self._recap_timer.start()

        def _stop_timers(self):
            self._recap_timer.stop()
            if self._notes_timer.isActive():
                self._notes_timer.stop()
                self._flush_my_notes()

        def _schedule_my_notes_save(self):
            self._notes_timer.start()
else:
    MeetingProcessingSignal = None
    MeetingSession = MeetingEngine
