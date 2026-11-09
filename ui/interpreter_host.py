"""The Interpreter's one owner in the app: starts and stops interpreter.controller.Interpreter for the
Interpreter page, the live card's chip, the tray, Ctrl+Alt+I and the Meetings start card.

    host = InterpreterHost(app)            # AppController builds it once (app.interpreter)
    host.toggle(source="hotkey")           # Pro first (the Pro window), then Privacy mode, then start
    host.state                             # "idle" | "starting" | "running" | "stopping"
    host.state_changed.connect(...)        # the page's Start / Stop, the card's chip
    host.event.connect(...)                # engine events (captions), always on the GUI thread
    host.notice.connect(...)               # (code, one calm sentence) - "" clears it
    host.directions_changed.connect(...)   # the card's mic / headphones: host.direction_on(name)

Everything slow runs on worker threads: building the controller, starting it (a server call and the
sound devices), stopping it, switching a direction. The engine calls back on its own threads; every
event is re-emitted here on the GUI thread (a queued Qt signal), so a slot may touch widgets.

It stops by itself when Privacy mode goes on, on a sign-out, another account or the end of Pro, and
when the speech service is gone for good. A setting changed while it starts is applied once it runs;
Start or Stop pressed while it stops (or restarts) is kept, the newest press wins.

Config (main.DEFAULT): interpreter_my_language ("" = Dictation's language, else English),
interpreter_their_language, interpreter_hear / interpreter_speak (the two directions; flipping one
while running switches it live), interpreter_output, interpreter_duck, interpreter_keep_underneath,
interpreter_captions (the live card opens with captions when it starts), interpreter_voice_on,
interpreter_notice, interpreter_hotkey, interpreter_with_meetings (start with every meeting recording,
stop when it stops - the hook is ``_on_meeting_phase``).

Telemetry: interpreter_started {hear, speak, pair, output, source}, interpreter_stopped {reason,
minutes, latency}, interpreter_failed {code} - never words, never names.
"""

import logging
import sys
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

import speech_langs
from interpreter import controller as interp_controller
from interpreter import session as interp_session
from interpreter import voices as interp_voices
from interpreter.session import InterpreterError

logger = logging.getLogger("transcribe")

UPSELL = "Interpreter"                         # the Pro window's feature name
PRIVACY_FEATURE = "The interpreter"            # privacy_prompt: "<feature> sends your voice to our servers."
TRAY_TITLE = "Interpreter"

IDLE, STARTING, RUNNING, STOPPING = "idle", "starting", "running", "stopping"

NO_VIRTUAL_MIC = ("Install BlackHole so they can hear you." if sys.platform == "darwin"
                  else "Speak needs a virtual microphone. Hear works now.")
# Codes the engine (or this host) adds to interpreter.session.MESSAGES: one calm sentence each.
EXTRA_MESSAGES = {
    "no_virtual_mic": NO_VIRTUAL_MIC,
    "voice": "The translated voice stopped. Captions still work.",
    "stt": "Lost the speech service for a moment.",
    "low_time": "About 5 minutes of interpreter time left this month.",
    "no_direction": "Turn on at least one direction.",
    "start": "The interpreter couldn't start. Try again.",
    "no_mic_sound": "Your microphone isn't sending any sound. Check it in Settings › Microphone.",
    "stt_lost": "Lost the speech service, so the interpreter stopped.",
    "quota": "Too many translations today. Try again tomorrow.",
    "device_lost": "A sound device stopped working. Check your headphones, speakers or microphone.",
    "sleep": "The computer slept, so the interpreter stopped.",
    "privacy_start": "Privacy mode is on, so the interpreter didn't start.",
    "privacy_stop": "Privacy mode is on, so the interpreter stopped.",
    "account": "The account changed, so the interpreter stopped.",
    "mic_back": "Switch your call's microphone back to your own mic.",
    "no_call_sound": "Gusan can't hear the call. Allow System Audio Recording in Settings.",
    "mac_too_old": "Hearing the call needs macOS 14.2 or later.",
}
# Stops the person asked for: a run that had Speak on leaves the virtual microphone silent (a call
# that listens to it hears nothing), so they are told once (mic_back). Other stops say their reason.
MIC_BACK_REASONS = frozenset({"user", "meeting_ended"})
DIR_KEYS = {"hear": "interpreter_hear", "speak": "interpreter_speak"}
# The speech service gone for good (eleven.py retries for a minute first: Wi-Fi, sleep): started
# again with a fresh session at most this often; when it goes again sooner, it stops and says so.
STT_RETRY_S = 300.0
# ...unless the account or the plan closed it: a new session is refused too (the engine said why).
STT_ACCOUNT_CODES = frozenset({"auth", "pro_required", "allowance_used", "provider_credits"})
# Said once to the others when the person starts it with Speak on (interpreter_notice), in their
# language, through the Speak voice - the engine does that (Interpreter.announce, when it has it).
NOTICE_TEXT = "Just so you know, I'm using an AI interpreter."
# The voice I hear them in (interpreter/voices.py): a running interpreter takes a change at once.
VOICE_KEYS = ("interpreter_voice_mode", "interpreter_voice_female", "interpreter_voice_male",
              "interpreter_voice_fixed")
# Settings the engine reads when it starts: a running interpreter starts again with them (the
# microphone too: the one picked in Gusan's settings).
RESTART_KEYS = ("interpreter_my_language", "interpreter_their_language", "interpreter_output",
                "interpreter_voice_on", "interpreter_duck", "interpreter_keep_underneath",
                "input_device_index")
# Engine error codes that are about one phrase only: never said out loud.
QUIET_CODES = frozenset({"translate"})

# The languages the pickers offer (Scribe v2 Realtime and the v4 voice cover them all), A-Z.
LANG_CODES = ("ar", "hy", "bn", "bg", "zh", "hr", "cs", "da", "nl", "en", "fi", "fr", "ka", "de", "el",
              "he", "hi", "hu", "id", "it", "ja", "kk", "ko", "lv", "lt", "ms", "no", "fa", "pl", "pt",
              "ro", "ru", "sr", "sk", "es", "sw", "sv", "tl", "ta", "th", "tr", "uk", "ur", "vi")


def languages():
    """[(code, name)] for the pickers, sorted by name."""
    return sorted(((c, speech_langs.language_name(c)) for c in LANG_CODES), key=lambda cn: cn[1])


def message(code):
    """The sentence for an error code ("" for none)."""
    if not code:
        return ""
    return EXTRA_MESSAGES.get(code) or interp_session.MESSAGES.get(code) or interp_session.MESSAGES["server"]


def my_language(cfg):
    """The language I speak: interpreter_my_language, else Dictation's (when it is one language),
    else English."""
    cfg = cfg or {}
    mine = speech_langs.normalize(cfg.get("interpreter_my_language"), "")
    if mine and mine not in ("auto", "multi"):
        return mine
    dictation = speech_langs.normalize(cfg.get("language"), "")
    return dictation if dictation and dictation not in ("auto", "multi") else "en"


DEFAULT_THEIR_LANGUAGE = "hy"


def their_language(cfg):
    """The language they speak: interpreter_their_language, else Armenian (English when I speak
    Armenian myself). The same language as mine is never a pair: it counts as not picked (the saved
    default "hy" for someone who dictates in Armenian)."""
    mine = my_language(cfg)
    theirs = speech_langs.normalize((cfg or {}).get("interpreter_their_language"), "")
    if theirs and theirs not in ("auto", "multi") and theirs != mine:
        return theirs
    return DEFAULT_THEIR_LANGUAGE if mine != DEFAULT_THEIR_LANGUAGE else "en"


def lang_name(code):
    return speech_langs.language_name(code) or str(code or "")


def pair_names(cfg):
    """"English ⇄ Armenian"."""
    return "%s ⇄ %s" % (lang_name(my_language(cfg)), lang_name(their_language(cfg)))


def pair_codes(cfg):
    """"EN ⇄ HY" (the live card's chip)."""
    return "%s ⇄ %s" % (my_language(cfg).upper(), their_language(cfg).upper())


def settings_config(cfg):
    """The cfg the engine reads (interpreter.controller.Settings.from_config), with "" resolved."""
    out = dict(cfg or {})
    out["interpreter_my_language"] = my_language(cfg)
    out["interpreter_their_language"] = their_language(cfg)
    return out


def minutes_text(seconds):
    """48 min, 1 h 5 min, 5 h."""
    try:
        mins = max(0, int(round(float(seconds) / 60.0)))
    except (TypeError, ValueError):
        return ""
    if mins < 60:
        return "%d min" % mins
    h, m = divmod(mins, 60)
    return "%d h %d min" % (h, m) if m else "%d h" % h


def _median(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    return xs[len(xs) // 2] if xs else None


class InterpreterHost(QObject):
    """See the module docstring. ``factory(token_getter, on_event, settings)`` builds the engine
    (tests pass a fake); ``voices`` is the interpreter.session.VoiceStore the My voice tab uses."""

    state_changed = Signal(str)
    event = Signal(object)
    notice = Signal(str, str)
    usage_changed = Signal()
    directions_changed = Signal()
    _from_worker = Signal(object)

    def __init__(self, app, factory=None, voices=None, parent=None):
        super().__init__(parent)
        self.app = app
        self._factory = factory or self._default_factory
        self._voices = voices
        self._ctl = None
        self._state = IDLE
        self._gen = 0
        self._source = ""
        self._stop_wanted = ""
        self._restart_source = ""
        self._restart_queued = False
        self._start_after = ""             # Start pressed while it was stopping: started once stopped
        self._again = False                # this run is a restart, not a start the person asked for
        self._announce = False             # the spoken notice is still to be said (interpreter_notice)
        self._pending = set()              # settings changed while it was starting
        self._switching = {}               # direction -> switches under way
        self._dir_lock = threading.RLock()
        self._user = None                  # the account it was started with
        self._stt_restart_at = None
        self._stt_closed_early = None      # the speech service closed while it started: handled once it runs
        self._had_speak = False            # Speak ran in this run (the call may listen to the virtual mic)
        self._started_at = 0.0
        self._latencies = []
        self._meeting_started = False
        self.seconds_left = None
        self.allowance_seconds = None
        self.own_voice = None
        self.directions = ()
        self.speakers_mode = False
        self.last_notice = ("", "")
        self._from_worker.connect(self._on_worker)
        # (sig_auth_changed comes from any thread: the slot runs here, on the GUI thread.)
        for owner, name, slot in ((app, "sig_setting_changed", self.on_setting_changed),
                                  (app, "sig_auth_changed", self._on_auth_changed),
                                  (getattr(app, "meeting_session", None), "state_changed", self._on_meeting_phase)):
            sig = getattr(owner, name, None) if owner is not None else None
            if sig is not None:
                try:
                    sig.connect(slot)
                except Exception:
                    logger.debug("interpreter host: %s not connected", name, exc_info=True)

    # ── plumbing (tests replace spawn with an inline call) ──
    @staticmethod
    def spawn(fn, name="interp-worker"):
        threading.Thread(target=fn, name=name, daemon=True).start()

    @staticmethod
    def _default_factory(token_getter, on_event, settings):
        return interp_controller.Interpreter(token_getter, on_event, settings)

    def _token(self):
        auth = getattr(self.app, "auth", None)
        fn = getattr(auth, "get_access_token", None)
        try:
            return fn() if callable(fn) else None
        except Exception:
            logger.debug("interpreter: no access token", exc_info=True)
            return None

    def voices(self):
        """The person's cloned voice on the server (interpreter.session.VoiceStore)."""
        if self._voices is None:
            self._voices = interp_session.VoiceStore(self._token)
        return self._voices

    def _cfg(self):
        cfg = getattr(self.app, "cfg", None)
        return cfg if isinstance(cfg, dict) else {}

    def _call(self, method, *args, **kwargs):
        fn = getattr(self.app, method, None)
        if not callable(fn):
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.warning("interpreter: %s() failed", method, exc_info=True)
            return None

    def _set(self, key, value):
        fn = getattr(self.app, "set_setting", None)
        if callable(fn):
            try:
                return bool(fn(key, value))
            except Exception:
                logger.warning("set_setting(%s) failed", key, exc_info=True)
                return False
        self._cfg()[key] = value
        return True

    def _track(self, event, props=None):
        self._call("track", event, props or {})

    def _is_pro(self):
        try:
            return bool(self.app.is_pro())
        except Exception:
            return False

    # ── state ──
    @property
    def state(self):
        return self._state

    @property
    def running(self):
        return self._state == RUNNING

    @property
    def active(self):
        """On, as the chip, the tray and the switches show it: starting or running, or stopping only
        to start again (``restarting``) - not once a stop was asked for while it starts."""
        return (self._state == RUNNING or (self._state == STARTING and not self._stop_wanted)
                or self.restarting)

    @property
    def restarting(self):
        """Stopping only to start again (a new language pair, or Start pressed while it stopped): the
        surfaces keep showing it as on meanwhile."""
        return bool(self._restart_queued or self._restart_source or self._start_after)

    @property
    def controller(self):
        return self._ctl

    def _set_state(self, state):
        if state != self._state:
            self._state = state
            # "Interpreter: running" ... "Interpreter: idle" in the log: a restart of the app can
            # tell a session is still on.
            logger.info("Interpreter: %s", state)
            self.state_changed.emit(state)

    def latency(self):
        """The delay to show (seconds) or None."""
        ctl = self._ctl
        if ctl is None or self._state != RUNNING:
            return None
        try:
            return ctl.latency()
        except Exception:
            return None

    def has_direction(self, name):
        return name in self.directions

    def direction_on(self, name):
        """What a direction's switch shows while it runs: the person's choice while a switch is under
        way, else what the engine runs (Speak stays off without a virtual microphone)."""
        if self._switching.get(name):
            return bool(self._cfg().get(DIR_KEYS.get(name, ""), True))
        return name in self.directions

    # ── start / stop ──
    def toggle(self, source="page", parent=None):
        if self.active:
            self.stop("user")
            return False
        return self.start(source=source, parent=parent)

    def start(self, source="page", parent=None, quiet=False, again=False):
        """Start it. Pro first (a Free person or a guest gets the Pro window - none with
        ``quiet``), then Privacy mode (the app's question: turn it off for this? - with ``quiet``
        it only says why it didn't start). True when a start is under way (or it already runs).
        ``again``: a restart (the live card is not opened again)."""
        if self.active:
            return True
        if not self._is_pro():
            if not quiet:
                self._call("_pro_upsell", UPSELL)
            return False
        cfg = self._cfg()
        if cfg.get("privacy_mode"):
            if quiet:
                self.say("privacy_start")
                return False
            from ui import privacy_prompt
            if not privacy_prompt.confirm_turn_off_privacy(parent, PRIVACY_FEATURE):
                return False
            if self.active:
                return True                          # started meanwhile (the question waits; a second press doesn't)
            if not self._set("privacy_mode", False):
                return False
            cfg = self._cfg()
        if not (cfg.get("interpreter_hear", True) or cfg.get("interpreter_speak", True)):
            self.say("no_direction")
            return False
        try:
            settings = interp_controller.Settings.from_config(settings_config(cfg))
        except Exception:
            logger.warning("interpreter settings unreadable", exc_info=True)
            self.say("start")
            return False
        if self._state == STARTING:                  # a stop was asked for while it starts: it stays on
            self._stop_wanted = ""
            self.state_changed.emit(self._state)
            return True
        if self._state == STOPPING:                  # still stopping: it starts as soon as that is done
            self._start_after = source or "page"
            self.state_changed.emit(self._state)
            return True
        self._gen += 1
        gen = self._gen
        self._source = source or ""
        self._again = bool(again)
        if not again:
            self._announce = True
            self._stt_restart_at = None
            self._had_speak = False
        self._stt_closed_early = None
        self._stop_wanted = ""
        self._pending = set()
        self._switching = {}
        self._latencies = []
        self._user = getattr(getattr(self.app, "auth", None), "user_id", None)
        self.directions = ()
        self.say("")
        self._set_state(STARTING)
        token_getter = self._token
        factory = self._factory
        emit = self._from_worker.emit

        def work():
            try:
                ctl = factory(token_getter, lambda e, g=gen: emit(("event", g, dict(e or {}))), settings)
            except Exception:
                logger.warning("interpreter could not be built", exc_info=True)
                emit(("started", gen, None, "start"))
                return
            try:
                ctl.start()
            except InterpreterError as e:
                emit(("started", gen, None, e.code or "server"))
                return
            except Exception:
                logger.warning("interpreter start failed", exc_info=True)
                emit(("started", gen, None, "start"))
                return
            emit(("started", gen, ctl, None))

        self.spawn(work, "interp-start")
        return True

    def stop(self, reason="user"):
        """Stop it (on a worker: the server is told how long it ran). A start waiting for a stop (a
        restart, or Start pressed while it stopped) is called off too."""
        self._cancel_restart()
        self._stop(reason)

    def _cancel_restart(self):
        if self._restart_queued or self._restart_source or self._start_after:
            self._restart_queued = False
            self._restart_source = self._start_after = ""
            self.state_changed.emit(self._state)      # the surfaces show it off now

    def _stop(self, reason):
        if self._state == STARTING:
            if not self._stop_wanted:
                self._stop_wanted = reason or "user"
                self.state_changed.emit(self._state)  # shown as off at once
            return
        ctl = self._ctl
        if self._state != RUNNING or ctl is None:
            return
        self._set_state(STOPPING)
        gen = self._gen
        emit = self._from_worker.emit

        def work():
            try:
                ctl.stop(reason)
            except Exception:
                logger.warning("interpreter stop failed", exc_info=True)
            emit(("stopped", gen, reason))

        self.spawn(work, "interp-stop")

    def shutdown(self, wait=1.5):
        """App quit: stop it, waiting a moment for the server to hear it. The call's volume comes
        back first, here: the stop (sockets, threads, the server) can take longer than the wait."""
        ctl, self._ctl = self._ctl, None
        self._gen += 1
        self._restart_queued = False
        self._restart_source = self._start_after = ""
        self._set_state(IDLE)
        if ctl is None:
            return
        _restore_call_volume(ctl)
        t = threading.Thread(target=lambda: ctl.stop("quit"), name="interp-quit", daemon=True)
        t.start()
        t.join(wait)
        _restore_call_volume(ctl)                    # (in case it was turned down again meanwhile)

    # ── directions ──
    def set_direction(self, name, on):
        """Hear / Speak on or off: the setting (the page shows it) and, while running, at once."""
        key = "interpreter_hear" if name == "hear" else "interpreter_speak"
        on = bool(on)
        before = bool(self._cfg().get(key, True))
        ok = self._set(key, on)
        if ok and before == on:
            self._apply_direction(name, on)          # no announcement comes for an unchanged value
        return ok

    def _apply_direction(self, name, on):
        """Switch a direction of the running engine to the setting. Switches run one at a time, each
        with the newest setting (quick taps), and it stops when nothing would be left on."""
        ctl = self._ctl
        if ctl is None or self._state != RUNNING or name not in DIR_KEYS:
            return
        if not self._switching.get(name) and (name in self.directions) == on:
            return
        cfg = self._cfg()
        others = [d for d, k in DIR_KEYS.items() if d != name and cfg.get(k, True)
                  and (d in self.directions or self._switching.get(d))]
        if not on and not others:
            self.stop("user")                        # both directions off: nothing left to do
            return
        key = DIR_KEYS[name]
        self._switching[name] = self._switching.get(name, 0) + 1
        self.directions_changed.emit()
        gen, emit, lock, read = self._gen, self._from_worker.emit, self._dir_lock, self._cfg

        def work():
            try:
                with lock:
                    ctl.set_direction(name, bool(read().get(key, True)))
            except Exception:
                logger.warning("interpreter: switching %s failed", name, exc_info=True)
            finally:
                emit(("switched", gen, name))

        self.spawn(work, "interp-direction")

    def _switched(self, name):
        n = self._switching.get(name, 0) - 1
        if n > 0:
            self._switching[name] = n
        else:
            self._switching.pop(name, None)
        self.directions_changed.emit()
        if self._state == RUNNING and not self._switching and not self.directions:
            self.stop("user")                        # the switches are done and nothing runs

    def on_setting_changed(self, key):
        cfg = self._cfg()
        if key == "privacy_mode":
            if cfg.get("privacy_mode") and self.active:
                self.stop("privacy")                 # nothing may leave the computer now
                self.say("privacy_stop")
            return
        if self._state == STARTING and (key in DIR_KEYS.values() or key in VOICE_KEYS
                                        or key in RESTART_KEYS or key == "language"):
            self._pending.add(key)                   # the engine starts with the old value: applied once it runs
            return
        if key in ("interpreter_hear", "interpreter_speak"):
            name = "hear" if key == "interpreter_hear" else "speak"
            self._apply_direction(name, bool(cfg.get(key, True)))
        elif key in VOICE_KEYS:
            self.set_voice_choice(interp_voices.VoiceChoice.from_config(cfg))
        elif self._state == RUNNING and (key in RESTART_KEYS or (
                key == "language" and not cfg.get("interpreter_my_language"))):
            self.restart()

    def _apply_pending(self, keys):
        """Settings changed while it started (the engine took the ones before): True when that
        restarts it."""
        cfg = self._cfg()
        if any(k in RESTART_KEYS for k in keys) or ("language" in keys and not cfg.get("interpreter_my_language")):
            self.restart()
            return True
        for name, key in DIR_KEYS.items():
            if key in keys:
                self._apply_direction(name, bool(cfg.get(key, True)))
        if any(k in VOICE_KEYS for k in keys):
            self.set_voice_choice(interp_voices.VoiceChoice.from_config(cfg))
        return False

    def set_voice_choice(self, choice):
        """The voice I hear them in, for the next phrase (a running engine that can take it)."""
        ctl = self._ctl
        fn = getattr(ctl, "set_voice_choice", None) if ctl is not None else None
        if self._state != RUNNING or not callable(fn):
            return False

        def work():
            try:
                fn(choice)
            except Exception:
                logger.warning("interpreter: the voice change failed", exc_info=True)

        self.spawn(work, "interp-voice")
        return True

    def restart(self):
        """Stop and start again (a new language pair): the same source, no questions. Several
        changes at once (a swap writes two keys) restart it once."""
        if self._state != RUNNING or self._restart_queued:
            return
        self._restart_queued = True
        QTimer.singleShot(0, self._restart_now)

    def _restart_now(self):
        if not self._restart_queued:
            return                                   # called off meanwhile (a stop)
        self._restart_queued = False
        if self._state != RUNNING:
            return
        self._restart_source = self._source or "page"
        self._stop("restart")

    # ── Meetings: start with the recording, stop with it ──
    def _on_meeting_phase(self, phase):
        """meeting_session.state_changed (GUI thread). With interpreter_with_meetings on, a
        recording that starts starts the interpreter (quietly: no Pro window over a meeting), and
        one that ends stops it - only a run the meeting started (a restart of it too)."""
        if phase == "recording":
            if (not self._meeting_started and self._state == IDLE
                    and self._cfg().get("interpreter_with_meetings", False)):
                self._meeting_started = bool(self.start(source="meeting", quiet=True))
        elif phase in ("idle", "processing", "done", "failed"):
            if self._meeting_started:
                self._meeting_started = False
                if self.active and self._source == "meeting" and not self._start_after:
                    self.stop("meeting_ended")

    # ── the account: signed out, another account, Pro over ──
    def _on_auth_changed(self):
        """app.sig_auth_changed (GUI thread). A run never outlives the account it started with: a
        sign-out, another account or the end of Pro (once the server says so) stops it."""
        if not self.active:
            return
        auth = getattr(self.app, "auth", None)
        user = getattr(auth, "user_id", None)
        if auth is not None and not getattr(auth, "is_authenticated", True):
            code = "auth"
        elif self._user and user and user != self._user:
            code = "account"
        elif getattr(auth, "entitlement_known", True) and not self._is_pro():
            code = "pro_required"
        else:
            return
        self.stop(code)
        self.say(code)

    # ── what the worker threads say (GUI thread) ──
    def _on_worker(self, msg):
        kind, gen = msg[0], msg[1]
        if gen != self._gen:
            if kind == "started" and msg[2] is not None:
                stale = msg[2]
                self.spawn(lambda: stale.stop("quit"), "interp-stale")
            return
        if kind == "started":
            self._on_started(msg[2], msg[3])
        elif kind == "event":
            self._handle_event(msg[2])
        elif kind == "stopped":
            self._finish(msg[2])
        elif kind == "switched":
            self._switched(msg[2])

    def _on_started(self, ctl, code):
        if ctl is None:
            stopped = bool(self._stop_wanted)
            self._stop_wanted = ""
            self._set_state(IDLE)
            self._track("interpreter_failed", {"code": code or "start"})
            if stopped:
                return                                   # turned off meanwhile: nothing to say
            if code == "bad_request" and self.last_notice[0] == "no_virtual_mic":
                return                                   # Speak only, and no virtual microphone: that says it
            self.say(code or "start")
            return
        self._ctl = ctl
        self._started_at = time.monotonic()
        info = getattr(getattr(ctl, "session", None), "info", None) or {}
        if isinstance(info, dict):
            self.own_voice = info.get("own_voice")
            if info.get("allowance_seconds") is not None:
                self.allowance_seconds = info.get("allowance_seconds")
            if info.get("seconds_left") is not None and self.seconds_left is None:
                self.seconds_left = info.get("seconds_left")
                self.usage_changed.emit()
        if not self.directions:
            self.directions = tuple(sorted(getattr(ctl, "_dirs", None) or ()))
        self._had_speak = self._had_speak or "speak" in self.directions
        self.speakers_mode = bool(getattr(ctl, "speakers_mode", self.speakers_mode))
        self._set_state(RUNNING)
        cfg = self._cfg()
        self._track("interpreter_started", {
            "hear": "hear" in self.directions, "speak": "speak" in self.directions,
            "pair": "%s-%s" % (my_language(cfg), their_language(cfg)),
            "output": "speakers" if self.speakers_mode else "headphones",
            "source": self._source or "page"})
        pending, self._pending = self._pending, set()
        if self._stop_wanted:
            reason, self._stop_wanted = self._stop_wanted, ""
            self._stop(reason)
            return
        if pending and not (cfg.get("interpreter_hear", True) or cfg.get("interpreter_speak", True)):
            self._stop("user")                           # both directions turned off while it started
            return
        closed, self._stt_closed_early = self._stt_closed_early, None
        if closed is not None:
            self._on_stt_closed(closed)                  # the speech service closed while it started
            if self._state != RUNNING or self.restarting:
                return
        self.directions_changed.emit()
        if cfg.get("interpreter_captions", True) and not self._again:
            card = getattr(self.app, "live_assist", None)
            try:
                shown = card is not None and card.isVisible()
            except RuntimeError:
                shown = False
            if not shown:
                self._call("show_live_assist")          # the captions live on the live card
        if pending and self._apply_pending(pending):
            return
        if self._announce:
            self._announce = False
            self._say_notice(ctl, cfg)

    def _say_notice(self, ctl, cfg):
        """The others are told once that an AI interpreter is on (interpreter_notice), in the
        person's own voice - when Speak runs and the engine can say it."""
        fn = getattr(ctl, "announce", None)
        if "speak" not in self.directions or not cfg.get("interpreter_notice", True) or not callable(fn):
            return

        def work():
            try:
                fn(NOTICE_TEXT)
            except Exception:
                logger.warning("interpreter: the notice wasn't said", exc_info=True)

        self.spawn(work, "interp-notice")

    def _handle_event(self, e):
        t = e.get("type")
        if t == "session":
            if e.get("seconds_left") is not None:
                self.seconds_left = e.get("seconds_left")
                self.usage_changed.emit()
            if "directions" in e:
                dirs = tuple(e.get("directions") or ())
                self._had_speak = self._had_speak or "speak" in dirs
                if dirs != self.directions:
                    self.directions = dirs
                    self.directions_changed.emit()
            if "speakers_mode" in e:
                self.speakers_mode = bool(e.get("speakers_mode"))
            if e.get("state") == "stopped":
                self.event.emit(e)
                if self._state != STARTING:              # a start that fails says so once (_on_started)
                    self._finish(e.get("reason") or "user")
                return
        elif t == "state" and e.get("state") == "stt_closed":
            self._on_stt_closed(str(e.get("code") or ""))
        elif t == "spoken" and e.get("latency") is not None:
            self._latencies.append(e.get("latency"))
            del self._latencies[:-20]
        elif t == "error":
            code = str(e.get("code") or "")
            # A run being stopped (a socket closed mid-phrase) says nothing; a start's own does.
            live = self._state in (STARTING, RUNNING) or code == "allowance_used"
            if code and code not in QUIET_CODES and live:
                if code != "no_virtual_mic":
                    self._track("interpreter_failed", {"code": code})
                self.say(code)
        elif t == "low_time":
            self.say("low_time")
        self.event.emit(e)

    def _on_stt_closed(self, code=""):
        """The speech service gave up after its own retries (Wi-Fi gone, the laptop slept): start
        again with a fresh session - at most once in STT_RETRY_S, saying nothing; sooner, it stops
        and says so (the one sentence: the engine leaves it to this). Closed by the account or the
        plan: it stops (the engine has said why). Closed while it starts: handled once it runs, so
        a run that can't hear is never left on (and billed)."""
        if self._state == STARTING:
            if not self._stop_wanted:
                self._stt_closed_early = code
            return
        if self._state != RUNNING or self.restarting:
            return
        if code in STT_ACCOUNT_CODES:
            self.stop("stt")
            return
        now = time.monotonic()
        if self._stt_restart_at is None or now - self._stt_restart_at > STT_RETRY_S:
            self._stt_restart_at = now
            self.restart()
        else:
            self.stop("stt")
            self.say("stt_lost")

    def _finish(self, reason):
        if self._state == IDLE and self._ctl is None:
            return
        minutes = round((time.monotonic() - self._started_at) / 60.0, 1) if self._started_at else 0
        self._ctl = None
        self._started_at = 0.0
        self.directions = ()
        self._switching = {}
        self._restart_queued = False                  # (a restart asked for as it stopped by itself: moot)
        # (_meeting_started stays until the meeting ends: a run stopped by hand during a meeting
        # is not started again when the recording resumes after a pause.)
        again = self._restart_source if reason == "restart" else ""
        asked = self._start_after
        if not (again or asked):
            self._restart_source = ""                 # (``restarting`` is over: the surfaces show it off)
        self._set_state(IDLE)
        self.directions_changed.emit()
        self._track("interpreter_stopped", {"reason": reason or "user", "minutes": minutes,
                                            "latency": _median(self._latencies)})
        self._restart_source = self._start_after = ""
        source = asked or again
        if source and not self.start(source=source, quiet=True, again=not asked):
            self.state_changed.emit(self._state)      # it did not start again after all: shown as off
        elif not source and reason == "sleep":
            self.say("sleep")                         # the engine stopped it after a long sleep
        elif not source and self._had_speak and reason in MIC_BACK_REASONS:
            self.say("mic_back")                      # nothing feeds the virtual microphone now (F89)

    # ── one calm sentence ──
    def say(self, code, text=None):
        """Tell the surfaces (the page's line, the card's footer) - and, when none of them is on
        screen, the tray. "" clears it."""
        text = message(code) if text is None else text
        self.last_notice = (code or "", text or "")
        self.notice.emit(code or "", text or "")
        if text and not self._surface_visible():
            self._call("show_tray_hint", TRAY_TITLE, text)

    def _surface_visible(self):
        """The live card or the Interpreter page on screen: the two places that show the notice (the
        Live Assistant and Meetings pages don't - the tray says it then)."""
        card = getattr(self.app, "live_assist", None)
        try:
            if card is not None and card.isVisible():
                return True
        except RuntimeError:
            pass
        win = getattr(self.app, "main_win", None)
        try:
            if win is not None and win.isVisible() and not win.isMinimized():
                return win.current_route() == "interpreter"
        except (RuntimeError, AttributeError):
            pass
        return False


def _restore_call_volume(ctl):
    """Put the call apps' volume back if the engine turned it down (quit: before the slow stop).
    close() also keeps it from being turned down again; an older Ducker only has restore()."""
    duck = getattr(ctl, "_duck", None)
    fn = getattr(duck, "close", None) or getattr(duck, "restore", None)
    if not callable(fn):
        return
    try:
        fn()
    except Exception:
        logger.debug("interpreter: the call's volume couldn't be put back", exc_info=True)


__all__ = ["EXTRA_MESSAGES", "IDLE", "InterpreterHost", "NO_VIRTUAL_MIC", "RUNNING", "STARTING", "STOPPING",
           "UPSELL", "languages", "message", "minutes_text", "my_language", "pair_codes", "pair_names",
           "their_language"]
