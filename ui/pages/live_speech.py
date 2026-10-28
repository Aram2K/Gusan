"""The Live Assistant page's "Transcribes with" row (Gusan 2.0.1).

    self.speech = LiveSpeechRow(app, open_settings=callable)
    rail.add_row(self.speech.row)
    ...
    self.speech.refresh()                    # the plan, a setting, a download changed
    self.speech.on_setting_changed(key)      # app.sig_setting_changed

What transcribes a Live Assistant call (cfg live_assist_speech_model, engine_choice.LIVE_*). The
picker lists REAL model names with a small chip for where each runs - LOCAL (this computer), CLOUD
(Gusan Cloud or your own key) and PRO where a Pro plan is needed - under headings for their group
(engine_choice.live_speech_rows; ui.components.model_combo.PlainNameCombo.fill_rows):

    Automatic · Fast  [CLOUD] [PRO]                 (names what it picks right now)
    ON THIS COMPUTER    Whisper Small [LOCAL] ...   (a download icon on one that is not here)
                        NVIDIA FastConformer Armenian [LOCAL]   (once it is available: see ui/hy_model.py)
    GUSAN CLOUD         Fast / Best accuracy  [CLOUD] [PRO]     (the server chooses the model)
    YOUR OWN KEY        Gemini 2.5 Pro · your Google key [CLOUD]   (once a Google key is saved)

* The box shows what is really in force (a Gusan Cloud pick paused by Privacy mode or a lapsed
  Pro reads Automatic); the muted line under it says what runs ("Fast · Cloud · for Armenian", or
  the model that answered - "GPT Transcribe · Cloud" - once one has) - while a session is being
  recorded, what REALLY ran in it (the recorder's own report: "Transcribing with GPT Transcribe ·
  Cloud"); under that, in plain words, the hint when local Whisper would be transcribing a language
  it is poor at (engine_choice.live_speech_note: it names the Armenian model when it can be had),
  and the calm line when the cloud could not read the latest piece of such a language ("Armenian
  couldn't be transcribed just now - retrying ...", gone by itself when the cloud answers again).
* The Armenian model: picking it while it is not on this computer asks first (Download / Not now), then
  downloads; the muted line shows the progress and a Cancel link stops it (and puts Automatic back).
  Armenian only: for any other language of the call Whisper runs (the row's line says so).
* A pick applies at once (set_settings). Gusan Cloud rows: Pro first (the Pro window), then
  Privacy mode (the "Privacy mode is on" question). Your own key: Privacy mode (its own question).
  A model that is not on this computer: the download question, then the download.
* Free, guests and Pro all see the row; Starting a session is the Pro action (the card's).
"""
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ui import hy_model, privacy_prompt
from ui.components import Button, SettingRow, label, mini_prompt
from ui.components.model_combo import MARK_DOWNLOAD, PlainNameCombo, combo as _rail_combo

logger = logging.getLogger("transcribe")

TITLE = "Transcribes with"
RUNNING_REFRESH_MS = 2000                           # how often a running session's line is looked at again
COMBO_WIDTH = 260                                   # room for "Automatic · Gemini 2.5 Flash" and its chips
UPSELL = "Fast cloud transcription"                 # engine_choice.UPSELL_SPEECH
SPEECH_KEY = "live_assist_speech_model"             # engine_choice.LIVE_SPEECH_KEY
CLOUD_VALUES = ("cloud", "cloud:best")              # engine_choice.LIVE_SPEECH_CLOUD / _BEST
OWN_PREFIX = "own:google"
OWN_PRIVACY_BODY = "Your Google key sends your voice to Google. Turn off Privacy mode to use it?"
DOWNLOAD_DIDNT_START = "The download didn't start. Try again."
# A pick made while a Live Assistant session is being recorded: that session keeps the engine it
# started with (the card says which), the pick is for the next one.
APPLIES_NEXT = "The session running now keeps what it started with. This applies from the next one."
DOWNLOAD_TAIL = "The Live Assistant will use it once it is ready."
HY_STOPPED = "The download was stopped. Automatic is back."

# Settings that change what this row shows.
WATCHED = frozenset({
    SPEECH_KEY, "live_assist_language", "language", "mix_languages", "privacy_mode",
    "admin_tier_override", "backend", "whisper_model", "managed_provider", "google_api_key",
    "google_stt_model", "mistral_api_key", "mistral_stt_model",
})


def _alive(obj):
    try:
        import shiboken6
        return shiboken6.isValid(obj)
    except Exception:
        return True


def _cfg(app):
    cfg = getattr(app, "cfg", None)
    return cfg if isinstance(cfg, dict) else {}


def _is_pro(app):
    try:
        return bool(app.is_pro())
    except Exception:
        return False


def _signed_in(app):
    return bool(getattr(getattr(app, "auth", None), "is_authenticated", _is_pro(app)))


def _live_session_running(app):
    """Is a session started from the Live Assistant being recorded right now?"""
    session = getattr(app, "meeting_session", None)
    try:
        return (getattr(session, "state", "") == "recording"
                and getattr(session, "source", "") == "live_assist")
    except Exception:
        return False


def _call(app, method, *args, **kwargs):
    fn = getattr(app, method, None)
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("%s() failed", method, exc_info=True)
        return None


def _set_many(app, changes):
    """app.set_settings (one save), or one set_setting per key."""
    if not changes:
        return True
    fn = getattr(app, "set_settings", None)
    if callable(fn):
        try:
            res = fn(dict(changes))
            return all(res.values()) if isinstance(res, dict) else res is not False
        except Exception:
            logger.warning("set_settings failed", exc_info=True)
            return False
    one = getattr(app, "set_setting", None)
    ok = True
    for key, value in dict(changes).items():
        try:
            ok = (one(key, value) is not False) and ok if callable(one) else False
        except Exception:
            logger.warning("set_setting(%s) failed", key, exc_info=True)
            ok = False
    return ok


class LiveSpeechRow(QWidget):
    """See the module docstring. ``self.row`` is the SettingRow to add to the rail."""

    def __init__(self, app, open_settings=None, parent=None):
        super().__init__(parent)
        self.app = app
        self._open_settings = open_settings
        self._note = ""                         # e.g. a download that didn't start
        self._dl_connected = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.combo = _rail_combo(PlainNameCombo(self), TITLE, width=COMBO_WIDTH)
        self.combo.enable_row_marks()           # whole rows: chips and a download icon at the right
        self._rows_seen = None
        self._fill()
        self.combo.currentIndexChanged.connect(self._on_picked)
        lay.addWidget(self.combo, 0, Qt.AlignLeft)
        self.line = label("", "model", self)
        self.line.setContentsMargins(13, 0, 0, 0)
        self.line.setWordWrap(True)
        lay.addWidget(self.line)
        # While the Armenian model downloads: a way to stop it.
        self.cancel_button = Button(hy_model.CANCEL + " download", "link", "s", parent=self)
        self.cancel_button.setAccessibleName("Cancel the Armenian model download")
        self.cancel_button.clicked.connect(self._cancel_download)
        self.cancel_button.hide()
        lay.addWidget(self.cancel_button, 0, Qt.AlignLeft)
        # The hint when local Whisper is poor at the call's language, and what runs when the
        # server answered with another model than the one asked for (plain words, never a warning).
        self.note = label("", "hint", self, wrap=True)
        self.note.setContentsMargins(13, 0, 0, 0)
        self.note.hide()
        lay.addWidget(self.note)
        self.setAccessibleName(TITLE)
        self.row = SettingRow(TITLE, self, stacked=True)
        # While a session is recorded the line says what REALLY ran and whether the cloud could not
        # read a piece just now (the recorder's report): nothing in the settings changes then, so a
        # slow timer looks again - only while the row is on screen and a session runs.
        self._was_running = False
        self._timer = QTimer(self)
        self._timer.setInterval(RUNNING_REFRESH_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.refresh()

    def _tick(self):
        if not _alive(self):
            return
        running = _live_session_running(self.app)
        if (running and self.isVisible()) or running != self._was_running:
            self._was_running = running
            self.refresh()

    # ── what the picker lists ──
    def _engine(self):
        try:
            import engine_choice
            return engine_choice.runs_on(self.app, "live_assistant_speech",
                                         downloaded=self._downloaded())
        except Exception:
            logger.debug("runs_on live_assistant_speech failed", exc_info=True)
            return {}

    def _rows(self):
        """The picker's rows (engine_choice.live_speech_rows): what each row is, and what
        Automatic would pick right now for the call's language."""
        try:
            import engine_choice
            return engine_choice.live_speech_rows(_cfg(self.app), _is_pro(self.app), _signed_in(self.app))
        except Exception:
            logger.debug("live_speech_rows failed", exc_info=True)
            return [{"value": "", "name": "Automatic", "title": "Automatic", "line": "Automatic",
                     "note": "", "tags": (), "pro": False, "group": "", "tip": ""}]

    def _fill(self, rows=None):
        rows = self._rows() if rows is None else rows
        box = self.combo
        box.blockSignals(True)
        box.fill_rows(rows)
        box.blockSignals(False)
        self._rows_seen = rows
        self._mark_rows()

    def _state(self, mid):
        """A Whisper model's state in app.downloads ("ready", "missing", "downloading",
        "failed"), or None when not known (no download service, or no first look at the disk)."""
        dl = getattr(self.app, "downloads", None)
        if dl is None or not callable(getattr(dl, "state", None)):
            return None
        scanned = getattr(dl, "is_scanned", None)
        if callable(scanned):
            try:
                if not scanned():
                    return None
            except Exception:
                return None
        try:
            return dl.state(mid)
        except Exception:
            return None

    def _downloaded(self):
        """Is a Whisper model on disk (app.downloads' map), or None when unknown."""
        dl = getattr(self.app, "downloads", None)
        if dl is None or not callable(getattr(dl, "state", None)):
            return None

        def on_disk(mid):
            try:
                return dl.state(mid) == "ready"
            except Exception:
                return True
        return on_disk

    def _mark_rows(self):
        """A download icon on a Whisper model that is not on this computer; nothing on the rest
        (the chips - LOCAL, CLOUD, PRO - are part of the row)."""
        box = self.combo
        try:
            import hardware
            info = hardware.info
            is_model = hardware.is_speech_id
        except Exception:
            def info(_v):
                return None

            def is_model(_v):
                return False
        for i in range(box.count()):
            if box.is_heading(i):
                continue
            value = str(box.itemData(i) or "")
            mark, note = "", ""
            if value and is_model(value) and self._state(value) in ("missing", "failed"):
                mark = MARK_DOWNLOAD
                note = getattr(info(value), "size", "") or ""
            box.set_choice_mark(i, mark, note)

    # ── what is in force ──
    def value(self):
        """The picker value really in force (engine_choice.live_speech_pick)."""
        try:
            import engine_choice
            return engine_choice.live_speech_pick(_cfg(self.app), _is_pro(self.app), _signed_in(self.app))
        except Exception:
            return ""

    def _line(self, ro):
        """The muted line under the picker: what really runs ("Gemini 2.5 Flash · Cloud · for
        Armenian", "Whisper Small · Local", "Whisper Small · 460 MB download")."""
        if not ro:
            return ""
        kind = ro.get("kind")
        value = self.value()
        if kind in ("cloud", "own_key"):
            text = ro.get("tagged") or ro.get("label") or ""
            return text + (" · for %s" % ro.get("language_name") if kind == "cloud"
                           and ro.get("reason") == "language" and ro.get("language_name") else "")
        name = ro.get("model_name") or ""
        mid = ro.get("model_id") or value
        dl = getattr(self.app, "downloads", None)
        state = None
        if dl is not None and value:
            try:
                state = dl.state(mid)
            except Exception:
                state = None
        if state == "downloading":
            pct = None
            try:
                prog = dl.progress_of(mid)
                pct = prog[0] if prog else None
            except Exception:
                pct = None
            return ("%s · downloading %d%%" % (name, pct)) if pct is not None else "%s · downloading" % name
        if ro.get("problem") == "needs_download":
            size = ro.get("size") or ""
            return ("%s · %s download" % (name, size)) if size else "%s · not downloaded" % name
        if hy_model.is_hy(mid) and not ro.get("auto"):
            # a pick of the Armenian model: say what it is for, so another language isn't a surprise
            return (ro.get("tagged") or name) + " · Armenian only"
        if not value and ro.get("auto") and ro.get("reason") == "language" and ro.get("language_name"):
            return (ro.get("tagged") or name) + " · for %s" % ro["language_name"]      # Automatic moved it (the Armenian model)
        return (ro.get("tagged") or name) + ("" if value else " · same as Dictation")

    def _running_report(self):
        """The recorder's speech_report() of the session being recorded now (what it REALLY
        runs and what has answered), or None when no session of this page's kind runs."""
        if not _live_session_running(self.app):
            return None
        rec = getattr(self.app, "recorder", None)
        getter = getattr(rec, "speech_report", None)
        if not callable(getter):
            return None
        try:
            report = getter()
        except Exception:
            logger.debug("speech_report failed", exc_info=True)
            return None
        return report if isinstance(report, dict) else None

    def _running_lines(self, report):
        """(line, note) of the session being recorded: what ran ("Transcribing with Gemini 2.5
        Flash · Cloud"), and the calm words when a language local Whisper is poor at could not be
        read just now. The same words as the card's."""
        import engine_choice
        cfg = _cfg(self.app)
        take = engine_choice.take_engine(cfg, report)
        line = engine_choice.live_speech_status(take, report)
        note = engine_choice.live_speech_note(cfg, _is_pro(self.app), report.get("language"),
                                              _signed_in(self.app), engine=take, report=report)
        return line, note

    def refresh(self):
        """Redraw from cfg + plan (cheap: no network, a stat at most)."""
        if not _alive(self):
            return
        rows = self._rows()
        if rows != self._rows_seen:
            self._fill(rows)                          # Automatic's pick, the plan and the own-key rows follow
        if self._note == APPLIES_NEXT and not _live_session_running(self.app):
            self._note = ""                           # the session it spoke of is over
        self._connect_downloads()
        self._mark_rows()
        box = self.combo
        i = box.findData(self.value())
        box.blockSignals(True)
        box.setCurrentIndex(i if i >= 0 else 0)
        box.blockSignals(False)
        ro = self._engine()
        report = self._running_report()
        if report is not None:
            try:
                line, note = self._running_lines(report)
            except Exception:
                logger.debug("running lines failed", exc_info=True)
                line, note = self._line(ro), (ro or {}).get("note") or ""
        else:
            line = self._line(ro)
            note = " ".join(x for x in ((ro or {}).get("note"), (ro or {}).get("downgrade")) if x)
        self.line.setText(line)
        self.line.setVisible(bool(line))
        self.cancel_button.setVisible(hy_model.can_cancel(self.app))
        self.note.setText(note)
        self.note.setVisible(bool(note))
        box.setAccessibleDescription(" ".join(x for x in (line, note) if x))
        self.row.set_hint(self._note)

    def watches(self, key):
        return key in WATCHED

    def on_setting_changed(self, key):
        if self.watches(key):
            self.refresh()

    # ── a pick ──
    def _on_picked(self, _i):
        """A row was picked. Gusan Cloud, your own key and a model that is not on this computer
        ask first (each puts the real choice back at once, so the box never shows a pick that
        did not happen); the rest apply."""
        value = self.combo.currentData()
        value = "" if value is None else str(value)
        self._note = ""
        if value in CLOUD_VALUES:
            self.refresh()                           # back to what is really set
            self._pick_gusan_cloud(value)
        elif value.startswith(OWN_PREFIX):
            self.refresh()
            self._pick_own_key(value)
        elif value and self._state(value) in ("missing", "failed"):
            self.refresh()
            self._pick_missing_model(value)
        else:
            self._apply(value)

    def _pick_gusan_cloud(self, value):
        """Pro first (the Pro window), then Privacy mode (turn it off, or keep it), else select."""
        if not _is_pro(self.app):
            _call(self.app, "_pro_upsell", UPSELL)
            return
        turn_off = False
        if _cfg(self.app).get("privacy_mode"):
            if not privacy_prompt.confirm_turn_off_privacy(self, "Gusan Cloud"):
                return
            turn_off = True
        self._apply(value, turn_off_privacy=turn_off)

    def _pick_own_key(self, value):
        """Your own Google key: Privacy mode asks (it would send the voice to Google)."""
        turn_off = False
        if _cfg(self.app).get("privacy_mode"):
            if not mini_prompt.ask(self, privacy_prompt.TITLE, OWN_PRIVACY_BODY,
                                   privacy_prompt.YES, privacy_prompt.NO, icon="incognito"):
                return
            turn_off = True
        self._apply(value, turn_off_privacy=turn_off)

    def _pick_missing_model(self, mid):
        """A Whisper model not on this computer: a small popup asks; Download starts it and
        picks it, Not now leaves everything as it is."""
        if hy_model.is_hy(mid):
            if hy_model.confirm_download(self, "The Live Assistant"):
                self._apply(mid)
            return
        try:
            import hardware
            info = hardware.MODEL_INFO.get(mid)
        except Exception:
            info = None
        plain = getattr(info, "plain", "") or mid
        real = getattr(info, "real", "")
        size = getattr(info, "size", "")
        body = (size + " - " if size else "") + DOWNLOAD_TAIL      # (mini_prompt's own words name Smart Actions)
        if mini_prompt.ask(self, mini_prompt.download_title(plain, real), body, "Download", "Not now",
                           icon="download"):
            self._apply(mid)

    def _cancel_download(self):
        """Stop the Armenian model's download. A pick of it that is not on this computer would only
        start the download again at the next session, so Automatic is put back."""
        if not hy_model.cancel(self.app):
            return
        if self.value() == hy_model.HY_ID:
            _set_many(self.app, {SPEECH_KEY: ""})
        self._note = HY_STOPPED
        self.refresh()

    def _apply(self, value, turn_off_privacy=False):
        """Save the pick through the controller (and start its download when the model is not
        on disk); then draw what is really set."""
        try:
            import engine_choice
            res = engine_choice.set_live_speech(self.app, value, turn_off_privacy=turn_off_privacy)
        except Exception:
            logger.warning("set_live_speech failed", exc_info=True)
            res = {"ok": False, "problem": "error", "changes": {}, "upsell": ""}
        if not res.get("ok"):
            problem = res.get("problem")
            self.refresh()                           # back to what is really set
            if problem == "pro":
                _call(self.app, "_pro_upsell", res.get("upsell") or UPSELL)
            elif problem == "needs_key" and callable(self._open_settings):
                self._open_settings()                # Settings › Speech & AI: add the key
            return
        if value and value not in CLOUD_VALUES and not value.startswith(OWN_PREFIX):
            self._download(value)
        _set_many(self.app, res.get("changes"))      # refused: the redraw shows the real value
        if _live_session_running(self.app):
            self._note = self._note or APPLIES_NEXT   # (a failed download's own words come first)
        self.refresh()

    def _download(self, mid):
        """A Whisper model not on disk yet: start it now (the controller's fetcher joins this
        download if a session starts first)."""
        on_disk = self._downloaded()
        if on_disk is None or on_disk(mid):
            return
        self._connect_downloads()
        dl = getattr(self.app, "downloads", None)
        try:
            dl.start(mid)
        except Exception:
            logger.warning("Speech model download failed to start", exc_info=True)
            self._note = DOWNLOAD_DIDNT_START

    # ── downloads ──
    def _connect_downloads(self):
        dl = getattr(self.app, "downloads", None)
        if dl is None or self._dl_connected is dl:
            return
        try:
            dl.progress.connect(self._on_dl_progress)
            dl.finished.connect(self._on_dl_finished)
            changed = getattr(dl, "state_changed", None)
            if changed is not None:
                changed.connect(self._on_dl_state)
            scanned = getattr(dl, "scanned", None)
            if scanned is not None:
                scanned.connect(self._on_dl_scanned)
            catalog = getattr(dl, "catalog_changed", None)
            if catalog is not None:
                catalog.connect(self._on_dl_scanned)        # the Armenian model became available (or went)
            self._dl_connected = dl
        except Exception:
            logger.debug("downloads signals unavailable", exc_info=True)

    def _listed(self, name):
        return bool(name) and self.combo.findData(name) >= 0

    def _on_dl_progress(self, name, *_rest):
        if _alive(self) and self._listed(name):
            self.refresh()

    def _on_dl_state(self, name, *_rest):
        if _alive(self) and self._listed(name):
            self.refresh()                           # the row's download icon follows

    def _on_dl_scanned(self, *_rest):
        if _alive(self):
            self.refresh()                           # the first look at the disk: icons mean something now

    def showEvent(self, e):
        super().showEvent(e)
        hy_model.refresh_catalog(self.app)               # (a worker; once a day at most, never in Privacy mode)

    def _on_dl_finished(self, name, state, msg="", *_rest):
        if not _alive(self):
            return
        if name == self.value():
            self._note = "" if state == "ready" else (msg or "The download stopped. Try again.")
        if self._listed(name):
            self.refresh()


__all__ = ["LiveSpeechRow", "TITLE", "WATCHED"]
