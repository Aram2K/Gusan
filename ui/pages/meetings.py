"""Meetings (route "meetings"): record a call, get the notes.

One route, three views (ui/pages/meeting_views.py has the last two):
- list  - the start card (Record a meeting PRO, title / people / Record from /
          Language, the consent line), Coming up (Google Calendar), Your
          meetings, and the "Meeting settings" rail;
- live  - a recording in progress (timer, two meters, live transcript, So
          far, My notes, Open Live Assistant, Pause / Stop and write notes,
          then the "Writing notes…" status);
- notes - one saved meeting (Notes / Transcript / My notes, Copy, Export,
          Continue recording PRO, ⋯ Rewrite notes / Rename / Show in folder /
          Delete; a failed-notes banner with "Write notes again").

The page follows ``app.meeting_session`` (whoever started the recording) and
never stops it: leaving the page or closing the window keeps it running.

on_open options: ``view="list"|"live"|"notes"``, ``folder=<meeting folder>``
(or ``meeting=``; opens its notes), and for a recording started from elsewhere (the calendar
reminder, the tray) ``title=``, ``attendees=`` (list or comma string),
``calendar_event_id=`` to prefill the start card, plus ``record=True`` to press
Record (Pro gate and consent included).

Integration notes for Wave 4
----------------------------
* Services read with getattr, so the page works before they exist:
  ``app.meeting_session`` (created on the first Record through
  ``meeting_session.get_session(app)`` when ``app.recorder`` exists - build it in
  AppController.__init__ as its docstring says), ``app.calendar``
  (CalendarService; every calendar part is hidden while it is missing or
  ``not calendar.available``), ``app.downloads`` (``state()`` /
  ``progress_of()`` / ``start()`` to fetch the local notes model when "This
  computer" is picked, and its progress / state_changed / finished signals so
  the rail shows "Qwen 1.5B · downloading 42%" or "· download failed" live).
* While ``meeting_session`` rewrites a saved meeting's notes (phase
  "processing" with its ``_rewrite_only`` flag) the page stays on that
  meeting's notes ("Writing notes…" banner) instead of the live view, which
  would show the last recording's transcript. A public ``is_rewrite``
  property on MeetingSession would be cleaner than reading the flag.
* Config keys this page writes through ``app.set_setting`` (never
  ``meeting_audio_mode``: every meeting starts on Both sides of the call and a
  start-card pick is for that meeting only - session.start(audio_mode=...)):
  ``meeting_language`` (new; "" = the global ``language``), ``meeting_notes_model``
  (new; "managed" = Gusan Cloud or a local_llm id; read by MeetingSession.start),
  ``meeting_consent_reminder`` (new, default True: shows the consent line on the
  start card), ``calendar_ask_to_record`` / ``calendar_only_with_link``
  (calendar_service.DEFAULTS), and the Notion connection (ui/notion_sheet.py
  writes ``notion_api_key``, ``notion_refresh_token``, ``notion_auth_mode``
  ("mcp" for the Connect button), ``notion_workspace_name``,
  ``notion_parent_page`` / ``_title``, ``notion_hub_page``, ``notion_reconnect``,
  ``notion_token_expires``, ``notion_mcp_client_id`` and ``notion_mcp_redirects``
  (the return addresses that client knows) in one ``set_settings`` call;
  ``notion_auto_send`` is the rail's switch). A Notion send that fails because the
  workspace is full (code "full") flashes its own plain sentence in the notes toolbar
  (notion_export.FULL_TEXT); nothing is flagged and the rail stays "Connected".
  Add the three new keys to main.DEFAULT.
  Every write falls back to cfg + save_config only when set_setting is missing.
* ``ui.recording_prompts.ensure_recording_consent`` writes ``meeting_consent_ack``
  through ``app.set_setting`` (cfg + ``save_config`` without it); the session
  refuses to start without that ack.
* Calendar wiring this page expects (calendar_service names): ``available``,
  ``is_connected``, ``is_connecting``, ``email``, ``meetings()``,
  ``current_meeting()``, ``refresh()``, ``set_muted(id, muted)``,
  ``connect_calendar()``, ``cancel_connect()``, ``disconnect_calendar()`` and the
  signals changed / connected / disconnected / connecting / error.
* ``AppController.record_from_calendar(m)`` can route here:
  ``show_main("meetings", record=True, title=m["title"], attendees=m["others"],
  calendar_event_id=m["id"], agenda=...)`` - the page runs the Pro gate and consent.
  (It also opens here, with the start card filled in, when a calendar recording
  could not start, and when a "Coming up" row is pressed.)
* Google Calendar (docs/redesign/google-calendar-spec.md, decisions 11, 14, 16, 17):
  every Connect button goes through ``MeetingsPage.connect_calendar()`` (the code is
  ui/calendar_door.py's, which Home's invitation card calls too), which asks
  with the disclosure Google requires BEFORE the browser opens ("Not now" does
  nothing). "Coming up" shows today and tomorrow in day groups (a weekday name
  later), five meetings and "Show more" (up to 20); a row shows the time ("in 5 min"
  / "Now"), the title, the call type, who is on it, a Join link (https only) and the
  bell, and pressing it fills the start card (``start_from_meeting``; Home's "Coming up"
  rows fill it through the same ``invite_context.card_prefill``, by opening this page with
  those options). The start card
  shows the invite's agenda - plain text the user can edit or clear - and passes it
  to ``session.start(agenda=...)`` with the title and names; Record then tells the
  controller (``note_calendar_start``) so that meeting is not asked about again. The
  muted line under the list and in the rail is ``calendar.status``.
  ``show_meeting`` / tray "Record Meeting" / IPC: ``show_main("meetings")``
  (the live view opens by itself while a meeting records).
* Live view "Open Live Assistant" (Pro) opens and never hides the card:
  ``app.show_live_assist()`` when the controller has it, else
  ``app.live_assist.open_card()``, else ``toggle_live_assist()`` - the same
  path as the Live Assistant page.
* Telemetry: only existing names are sent (meeting_exported with target
  clipboard / word / markdown / notion / audio) and ``meeting_deleted`` (a count and
  where it came from: row / selection / key / live / notes; never a name) from a delete in
  a list (ui/pages/meeting_delete.py) or in the open meeting. It is in telemetry.ALLOWED_EVENTS
  and in the server's list in the source; the server drops it until that function is deployed.
* Delete from the list: a trash icon on each row (hover / keyboard), Select to tick
  several, the Delete key; one question (mini_prompt.confirm), then the folder
  moves to meetings/.trash for ten seconds of Undo (meeting_trash.py; the "Deleted 3
  meetings - Undo" toast floats over the bottom of the page, in sight however far the
  list is scrolled). The open
  meeting's own Delete (meeting_views.py) asks the same question and deletes at once.
* Audio export writes MP3 (or WAV), not the mockup's .m4a (audio_export can't);
  the menu says ".mp3". Play plays the recording inside the app
  (ui/components/audio_player.py, QtMultimedia): the parts of a continued
  meeting are chained, nothing is exported to a temp file.
"""

import logging
import re
import time
from datetime import datetime, timezone

from PySide6.QtCore import QEvent, QPropertyAnimation, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QDesktopServices, QGuiApplication, QIcon, QKeySequence, QPainter, QPen,  # noqa: F401
                           QPixmap, QRadialGradient, QShortcut)    # (QDesktopServices: tests patch openUrl through it)
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QComboBox, QFrame, QGraphicsOpacityEffect,
                               QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QRadioButton,
                               QScrollArea, QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

import calendar_sync
import invite_context
import live_context
import notion_export
from ui import calendar_door, notion_sheet, theme
# The Connect box's words moved to ui/calendar_door.py (Home's card asks the same); the names stay here.
from ui.calendar_door import (CONNECT_BODY, CONNECT_DETAIL, CONNECT_LINK, CONNECT_TITLE,  # noqa: F401
                              EXPIRED_NOTE)
from ui.components import (Button, Card, Chip, Divider, IconButton, PageHeader, ProTag, SettingRow,
                           SettingsRail, Toggle, icons, label, row_actions)
from ui.pages import meeting_delete
from ui.pages import meeting_views as views
from ui.pages.meeting_views import (AUDIO_MODES, LANGS, NOTICE_TEXT, UPSELL_RECORD, BgRunner, ElidedLabel,
                                    Faces, call_mark, call_name, dot_label, dot_pixmap, fmt_duration, fmt_when,
                                    glyph, google_label, icon_label, tlabel)

logger = logging.getLogger("transcribe")

ASK_KEY = "calendar_ask_to_record"
LINK_ONLY_KEY = "calendar_only_with_link"       # "Only meetings with other people" (the key keeps its old name)
COMING_FIRST = 5                # "Coming up" rows before "Show more"
COMING_MAX = 20
# Coming up's own style (the page's shared sheet is views.page_qss): a meeting row you can press.
_CALENDAR_QSS = """
QFrame[gusan="upRow"][focusVisible="true"] { border: 2px solid %s; }
QFrame[gusan="upRow"]:hover { background: %s; }
"""
CONSENT_KEY = "meeting_consent_reminder"
NOTES_KEY = "meeting_notes_model"
MANAGED = "managed"            # actions.API_MANAGED_ID
LOCAL_DEFAULT = "qwen_tiny"    # local_llm.QWEN_TINY_ID

_SETTING_KEYS = {
    "meeting_audio_mode", "meeting_language", "language", NOTES_KEY, "action_model", "backend",
    "whisper_model", "privacy_mode", "managed_provider", "google_stt_model", "mistral_stt_model",
    ASK_KEY, LINK_ONLY_KEY, CONSENT_KEY, "notion_api_key", "notion_parent_page", "notion_auth_mode",
    "notion_workspace_name", "notion_parent_title", "notion_auto_send", "notion_reconnect",
    "live_assist_hotkey", "live_assist_private", "google_oauth_client_id", "google_oauth_client_secret",
}


def _now_ts():
    return time.time()


def _utcnow():
    return datetime.now(timezone.utc)


_service_status = calendar_door.service_status       # the service's muted line ("" when it has none)
_paused_text = calendar_door.paused_text             # "Paused in Privacy mode."


def _ensure_consent(parent, app):
    """The one-time recording notice (ui/recording_prompts.py); True to go on."""
    try:
        from ui.recording_prompts import ensure_recording_consent
    except Exception:
        logger.warning("Consent notice unavailable", exc_info=True)
        return bool((getattr(app, "cfg", None) or {}).get("meeting_consent_ack"))
    return ensure_recording_consent(parent, app)


def _audio_modes():
    try:
        import meeting_session
        return tuple(m for m in meeting_session.available_audio_modes() if m in AUDIO_MODES)
    except Exception:
        return ("default_mic",)


def _default_title():
    try:
        import meeting_session
        return meeting_session.default_title(_now_ts())
    except Exception:
        return "Meeting · " + time.strftime("%a %H:%M", time.localtime(_now_ts()))


def _local_writers():
    try:
        import engine_choice
        return dict(engine_choice.LOCAL_WRITERS)
    except Exception:
        return {LOCAL_DEFAULT: ("Small", "Qwen 1.5B", "1.12 GB", "8 GB")}


def _cloud_writer_model():
    try:
        import engine_choice
        return engine_choice.CLOUD_WRITER_MODEL
    except Exception:
        return "Mistral Small"


def create(main_app):
    return MeetingsPage(main_app)


def _scroller(widget):
    sc = QScrollArea()
    sc.setProperty("gusan", "scroll")
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.NoFrame)
    sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sc.viewport().setAutoFillBackground(False)
    widget.setProperty("gusan", "scrollContent")
    sc.setWidget(widget)
    return sc


# ── the page ─────────────────────────────────────────────────────────────────
class MeetingsPage(QWidget):
    wants_scroll = False          # each view scrolls itself

    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self._ready = False             # services bind once every view exists
        self._session = None
        self._calendar = None
        self._downloads = None
        self._view = None
        self.setAccessibleName("Meetings")
        self.setStyleSheet(views.page_qss() + _CALENDAR_QSS % (theme.qss_color("blue-l"), theme.qss_color("bg")))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.stack = QStackedWidget(self)
        lay.addWidget(self.stack)
        self.list_view = ListView(self)
        self.live_view = views.LiveView(self)
        self.notes_view = views.NotesView(self)
        self._holders = {}
        for name, view in (("list", self.list_view), ("live", self.live_view), ("notes", self.notes_view)):
            holder = _scroller(view)
            self._holders[name] = holder
            self.stack.addWidget(holder)
        self._tick = QTimer(self)
        self._tick.setInterval(30 * 1000)
        self._tick.timeout.connect(self._on_tick)
        self._ready = True
        self.session()
        self.calendar()
        self.downloads()
        self.list_view.sync_calendar()
        # The list shows until the first on_open picks the view (live while recording).
        self.stack.setCurrentWidget(self._holders["list"])
        self.list_view.refresh()

    # ── app helpers ──
    def call(self, method, *args, **kwargs):
        fn = getattr(self.app, method, None)
        if not callable(fn):
            logger.debug("main_app has no %s()", method)
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.warning("%s() failed", method, exc_info=True)
            return None

    def is_pro(self):
        try:
            return bool(self.app.is_pro())
        except Exception:
            return False

    def cfg(self):
        return getattr(self.app, "cfg", None) or {}

    def track(self, event, props=None):
        self.call("track", event, props or {})

    def set_setting(self, key, value):
        fn = getattr(self.app, "set_setting", None)
        if callable(fn):
            try:
                return bool(fn(key, value))
            except Exception:
                logger.warning("set_setting(%s) failed", key, exc_info=True)
                return False
        cfg = getattr(self.app, "cfg", None)
        if cfg is None:
            return False
        cfg[key] = value
        self.call("save_config")
        self.on_setting_changed(key)
        return True

    def set_settings(self, updates):
        fn = getattr(self.app, "set_settings", None)
        if callable(fn):
            try:
                res = fn(dict(updates)) or {}
                return all(res.get(k, False) for k in updates)
            except Exception:
                logger.warning("set_settings failed", exc_info=True)
                return False
        return all([self.set_setting(k, v) for k, v in updates.items()])

    def me(self):
        """The signed-in user's first name ('' for a guest)."""
        auth = getattr(self.app, "auth", None)
        if not getattr(auth, "is_authenticated", False):
            return ""
        name = (getattr(auth, "user_name", None) or "").strip()
        if not name:
            email = (getattr(auth, "user_email", None) or "").strip()
            name = email.split("@")[0] if email else ""
        return name.split()[0] if name else ""

    def open_route(self, route, **opts):
        fn = getattr(self.app, "show_main", None)
        if callable(fn):
            try:
                fn(route, **opts)
                return
            except Exception:
                logger.warning("show_main(%s) failed", route, exc_info=True)
        win = self.window()
        if hasattr(win, "open") and win is not self:
            win.open(route, **opts)

    def ensure_consent(self):
        return _ensure_consent(self, self.app)

    # ── services ──
    def session(self, create=False):
        """The app's MeetingSession (bound to this page), or None."""
        s = getattr(self.app, "meeting_session", None)
        if s is None and create and (getattr(self.app, "recorder", None) is not None
                                     or callable(getattr(self.app, "_resolve_action_engine", None))):
            try:
                import meeting_session
                s = meeting_session.get_session(self.app)
            except Exception:
                logger.warning("Couldn't create the meeting session", exc_info=True)
                s = None
        if s is not None and s is not self._session and self._ready:
            self._bind_session(s)
        return s

    def _bind_session(self, s):
        self._session = s
        for name, slot in (("state_changed", self._on_phase), ("live_line", self._on_line),
                           ("recap", self.live_view.set_recap), ("status", self.live_view.set_status),
                           ("levels", self.live_view.set_levels), ("finished", self._on_finished),
                           ("error", self._on_error), ("speakers_changed", self._on_speakers_changed)):
            sig = getattr(s, name, None)
            if sig is None:
                continue
            try:
                sig.connect(slot)
            except Exception:
                logger.debug("Couldn't connect session.%s", name, exc_info=True)

    def calendar(self):
        """The app's CalendarService when this build has a Google client, else None."""
        cal = getattr(self.app, "calendar", None)
        if cal is not None and cal is not self._calendar and self._ready:
            self._calendar = cal
            for name, slot in (("changed", self._on_calendar_changed), ("connected", self._on_calendar_state),
                               ("disconnected", self._on_calendar_disconnected),
                               ("connecting", self.list_view.set_connecting),
                               ("status", self.list_view.set_calendar_status),
                               ("error", self.list_view.calendar_error)):
                sig = getattr(cal, name, None)
                if sig is None:
                    continue
                try:
                    sig.connect(slot)
                except Exception:
                    logger.debug("Couldn't connect calendar.%s", name, exc_info=True)
        if cal is None:
            return None
        try:
            return cal if cal.available else None
        except Exception:
            return None

    def connect_calendar(self):
        """Every Connect button's one door. Google asks that the app says what it reads
        and why BEFORE the sign-in opens, so a card says it first; "Not now" does
        nothing. (In Privacy mode there is nothing to ask - the service says it is
        paused.) True when the browser sign-in started. The code is ui/calendar_door.py's: Home's
        "Coming up" card goes through the same door."""
        return calendar_door.connect_calendar(self, self.calendar(), self.cfg().get("privacy_mode"))

    def start_from_meeting(self, m):
        """A "Coming up" row was pressed: the start card, filled with that meeting
        (title, people, agenda) and in view, so Record names it."""
        self.list_view.start_card.prefill(**invite_context.card_prefill(m))
        self.show_list()
        self._scroll_list_to_top()

    def _scroll_list_to_top(self):
        """The start card is at the top of the list: bring it into view."""
        holder = self._holders.get("list")
        if holder is not None:
            holder.verticalScrollBar().setValue(0)

    def downloads(self):
        """The app's model downloads (app.downloads), bound once so the
        "This computer" line shows the notes model's progress live."""
        dl = getattr(self.app, "downloads", None)
        if dl is not None and dl is not self._downloads and self._ready:
            self._downloads = dl
            for name in ("progress", "state_changed", "finished"):
                sig = getattr(dl, name, None)
                if sig is None:
                    continue
                try:
                    sig.connect(self._on_download)
                except Exception:
                    logger.debug("Couldn't connect downloads.%s", name, exc_info=True)
        return dl

    def _on_download(self, name="", *_rest):
        if str(name or "") in _local_writers():
            self.list_view.rail.sync_notes_by()

    def phase(self):
        s = self.session()
        return getattr(s, "phase", "idle") if s is not None else "idle"

    def is_rewriting(self):
        """True while the session rewrites a saved meeting's notes (the
        "⋯ Rewrite notes" / "Write notes again"): nothing records, so the notes
        view (with its "Writing notes…" banner) is the place, not the live view."""
        s = self.session()
        return bool(s is not None and getattr(s, "phase", "") == "processing"
                    and getattr(s, "_rewrite_only", False))

    def shows_live(self):
        """The live view has this session to show: recording, paused, or the
        notes of a just-recorded meeting being written."""
        phase = self.phase()
        return phase in ("recording", "paused") or (phase == "processing" and not self.is_rewriting())

    def is_active_folder(self, folder):
        s = self.session()
        if s is None or not folder:
            return False
        try:
            active = s.active_dir()
        except Exception:
            active = None
        if not active:
            return False
        import os
        try:
            return os.path.normcase(os.path.abspath(str(active))) == os.path.normcase(os.path.abspath(str(folder)))
        except Exception:
            return str(active) == str(folder)

    # ── views ──
    def current_view(self):
        return self._view

    def _show(self, name):
        self._view = name
        self.stack.setCurrentWidget(self._holders[name])

    def show_list(self, refresh=False):
        self._show("list")
        self.list_view.sync_session()
        if refresh:
            self.list_view.refresh()

    def show_live(self):
        self._show("live")
        self.live_view.sync()

    def open_notes(self, folder, item=None):
        if self.is_active_folder(folder) and self.shows_live():
            self.show_live()
            return
        self._show("notes")
        self.notes_view.load(folder, item)

    def list_changed(self):
        self.list_view.refresh()

    def forget_vanished_notes(self):
        """The meeting open in the notes view may have been deleted from another page (the Live
        Assistant's Past sessions) while this page was away: show the list, not an empty meeting."""
        import os
        folder = self.notes_view.folder
        if folder and not os.path.isdir(folder):
            self.notes_view.forget_folder(folder)
            if self._view == "notes":
                self.show_list(refresh=True)

    def on_open(self, **opts):
        self.session()
        self.calendar()
        self.downloads()
        self.forget_vanished_notes()
        phase = self.phase()
        folder = opts.get("folder") or opts.get("meeting")      # Live Assistant's Past sessions send meeting=
        view = opts.get("view")
        prefill = {k: opts[k] for k in ("title", "attendees", "calendar_event_id", "agenda") if k in opts}
        if prefill:
            self.list_view.start_card.prefill(**prefill)
        if folder:
            self.open_notes(folder)
        elif view == "live" and phase != "idle" and not self.is_rewriting():
            self.show_live()
        elif view in ("list", "notes"):
            if view == "notes" and self.notes_view.folder:
                self._show("notes")
                self.notes_view.reload()
            else:
                self.show_list(refresh=True)
        elif phase in ("recording", "paused"):
            self.show_live()
        elif (phase == "processing" and not self.is_rewriting()) or (
                phase == "failed" and not (getattr(self._session, "transcript", "") or "").strip()):
            if self._view in (None, "live"):
                self.show_live()
            elif self._view == "notes" and self.notes_view.folder:
                self.notes_view.reload()
            else:
                self.show_list(refresh=True)
        elif self._view == "notes" and self.notes_view.folder:
            self.notes_view.reload()
        else:
            self.show_list(refresh=True)
        cal = self.calendar()
        if cal is not None:
            try:
                cal.refresh()
            except Exception:
                logger.debug("calendar refresh failed", exc_info=True)
        self.list_view.sync_calendar()
        if prefill and self._view == "list":
            self._scroll_list_to_top()          # a meeting was picked elsewhere (Home): its card is in view
        if opts.get("record"):
            if self._view != "list" and phase not in ("recording", "paused"):
                self._show("list")
            self.record()

    def on_setting_changed(self, key):
        if key not in _SETTING_KEYS:
            return
        self.list_view.sync_settings()
        self.live_view.sync_model()
        self.live_view.sync_assist()
        if key in ("google_oauth_client_id", "google_oauth_client_secret"):
            self.list_view.sync_calendar()

    def refresh_account(self):
        self.list_view.sync_settings()
        self.list_view.refresh()
        if self._view == "notes":
            self.notes_view.render()

    # ── recording ──
    def record(self):
        """Record a meeting: consent notice -> session.start.

        Recording is free. A cloud meeting note is metered (one a month) when
        the notes engine is Gusan Cloud; the server says so, and the app asks
        to upgrade."""
        card = self.list_view.start_card
        card.show_error("")
        s = self.session()
        if s is not None and getattr(s, "phase", "") in ("recording", "paused"):
            self.show_live()
            return False
        if not self.ensure_consent():
            return False
        s = self.session(create=True)
        if s is None:
            card.show_error("The recorder isn't ready yet. Try again in a moment.")
            return False
        cfg = self.cfg()
        event_id = card.calendar_event_id
        agenda = card.agenda()
        people = card.people.names()
        try:
            # The agenda goes with it only when the card shows it (the user can read it
            # and edit it there); the notes then get it with the title and the names.
            ok, _code, msg = live_context.start_session(
                s, agenda=agenda, title=card.title(), attendees=", ".join(people),
                audio_mode=card.audio_mode(), language=card.language(),
                notes_engine=cfg.get(NOTES_KEY) or None,
                source="calendar" if event_id else "page",
                calendar_event_id=event_id or None)
        except Exception as e:
            logger.warning("Starting the meeting failed", exc_info=True)
            ok, msg = False, str(e)
        if not ok:
            card.show_error(msg or "Couldn't start the recording.")
            return False
        if event_id:        # that meeting counts as asked: no reminder for it any more
            self.call("note_calendar_start", {"event_id": event_id, "agenda": agenda,
                                              "others": people}, "start_card", False)
        card.reset()
        self.show_live()
        return True

    # ── session signals ──
    def _on_phase(self, phase):
        self.live_view.sync()
        self.list_view.sync_session()
        self.notes_view.sync_actions()
        if phase in ("recording", "paused") and self._view == "list":
            self.show_live()

    def _on_line(self, piece, who=""):
        self.live_view.add_line(piece, who)

    def _on_finished(self, folder, notes, error):
        self.list_changed()
        s = self._session
        has_transcript = bool((getattr(s, "transcript", "") or "").strip()) if s is not None else False
        if self._view == "live":
            if folder and (notes or has_transcript):
                self.open_notes(folder)
            else:
                self.live_view.sync()
        elif self._view == "notes" and self.notes_view.folder and folder and self.is_same(folder):
            self.notes_view.reload()

    def _on_speakers_changed(self, folder):
        """The background speaker pass of a meeting (the names that come after its
        notes) was queued, started or ended. The meeting open in the notes view
        shows its banner meanwhile and, once the pass ended, its new transcript."""
        self.list_view.meetings.sync_blocked()          # "Finishing up": it can't be deleted until it ends
        if self._view != "notes" or not self.notes_view.folder or not folder or not self.is_same(folder):
            return
        s = self._session
        working = bool(callable(getattr(s, "speakers_state", None)) and s.speakers_state(folder))
        # Whoever is reading this meeting (its Transcript, or typing in My notes) keeps the
        # tab and the place: the page only changes under them, never moves them.
        if not working:
            self.notes_view.reload(keep_view=True)
        elif str((self.notes_view.data or {}).get("folder") or "") == str(self.notes_view.folder):
            self.notes_view.render_keeping_view()    # (a meeting still loading renders when it is read)

    def is_same(self, folder):
        import os
        try:
            return os.path.normcase(os.path.abspath(folder)) == os.path.normcase(os.path.abspath(self.notes_view.folder))
        except Exception:
            return folder == self.notes_view.folder

    def _on_error(self, code, message):
        if code == "empty" and self._view == "live":
            self.live_view.sync()

    # ── calendar signals ──
    def _on_calendar_changed(self, rows=None):
        self.list_view.set_upcoming(rows)

    def _on_calendar_state(self, *_):
        """calendar.connected: whatever error the last tries left is over."""
        self.list_view.calendar_error("")
        self.list_view.sync_calendar()

    def _on_calendar_disconnected(self, reason="", *_):
        """calendar.disconnected: an old error goes away; an expiry says so on the Connect
        card and in the rail (a tray balloon alone is easy to miss)."""
        self.list_view.calendar_error(EXPIRED_NOTE if reason == "expired" else "", tone="muted")
        self.list_view.sync_calendar()

    # ── visibility ──
    def _on_tick(self):
        self.list_view.tick()

    def showEvent(self, e):
        super().showEvent(e)
        if not self._tick.isActive():
            self._on_tick()             # back after a while: the default name and "in N min" are current
        self._tick.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._tick.stop()               # never touches the recording

    # ── Notion ──
    def connect_notion(self, then="", reason=""):
        """Open the Notion sheet (connect, switch, where notes go). True when
        the caller's ``then`` button was pressed - or, with no ``then``, when
        Notion is connected as the sheet closes."""
        return notion_sheet.show_notion_sheet(self, then=then, reason=reason)


# ── list view ────────────────────────────────────────────────────────────────
class ListView(QWidget):
    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.app = page.app
        self._bg = BgRunner(self)
        self._items = []
        self._refresh_token = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = PageHeader("Meetings", "Record a call. Get the notes.", parent=self)
        root.addWidget(self.header)
        body = QHBoxLayout()
        body.setContentsMargins(32, 0, 32, 28)
        body.setSpacing(24)
        main = QVBoxLayout()
        main.setSpacing(16)
        self.live_strip = LiveStrip(page, self)
        main.addWidget(self.live_strip)
        self.start_card = StartCard(page, self)
        main.addWidget(self.start_card)
        self.connect_card = ConnectCard(page, self)
        main.addWidget(self.connect_card)
        self.coming_up = ComingUp(page, self)
        main.addWidget(self.coming_up)
        self.meetings = MeetingsList(page, self)
        main.addWidget(self.meetings)
        main.addStretch(1)
        body.addLayout(main, 1)
        self.rail = MeetingRail(page, self)
        body.addWidget(self.rail, 0, Qt.AlignTop)
        root.addLayout(body, 1)
        self.sync_calendar()
        self.sync_session()

    # ── data ──
    def refresh(self):
        """Re-read the meetings library on a worker thread (only the newest
        read is shown: an older one finishing late is dropped)."""
        def work():
            import meeting_store
            return meeting_store.list_meetings()
        self._refresh_token += 1
        token = self._refresh_token
        self._bg.run(work, lambda items, err: self._on_items(items, err, token))

    def _on_items(self, items, err, token=None):
        if token is not None and token != self._refresh_token:
            return
        if err is not None:
            return                      # keep what is shown; the next refresh tries again
        self._items = list(items or [])
        self.meetings.set_items(self._items)

    def items(self):
        return list(self._items)

    def forget(self, folder):
        """A meeting left the library (deleted from the list): a re-render must not bring it back."""
        self._items = [m for m in self._items if m.get("dir") != folder]

    def sync_session(self):
        self.live_strip.sync()
        self.meetings.set_items(self._items)

    def sync_settings(self):
        self.start_card.sync_settings()
        self.rail.sync_settings()

    def tick(self):
        self.start_card.sync_default()
        self.live_strip.sync()
        cal = self.page.calendar()
        if cal is not None and getattr(cal, "is_connected", False):
            self.set_upcoming(None)

    # ── calendar ──
    def sync_calendar(self):
        cal = self.page.calendar()
        connected = bool(cal is not None and getattr(cal, "is_connected", False))
        self.connect_card.setVisible(cal is not None and not connected)
        self.connect_card.sync()
        self.coming_up.setVisible(connected)
        self.rail.sync_calendar()
        self.coming_up.set_status(_service_status(cal) if connected else "")
        if connected:
            self.set_upcoming(None)
        else:
            self.coming_up.set_meetings([])
            # A disconnect releases a picked meeting; with no calendar service at all
            # (nothing was ever connected) there is nothing to release.
            self.start_card.set_calendar_meeting(None, () if cal is not None else None)

    def set_calendar_status(self, text=""):
        """calendar.status: one muted line under "Coming up" (and in the rail's hint)."""
        self.coming_up.set_status(text)
        self.rail.sync_calendar()

    def set_upcoming(self, rows=None):
        cal = self.page.calendar()
        if cal is None or not getattr(cal, "is_connected", False):
            self.coming_up.set_meetings([])
            self.coming_up.hide()
            return
        if rows is None:
            try:
                rows = cal.meetings()
            except Exception:
                rows = []
        self.coming_up.set_meetings(rows or [])
        self.coming_up.show()
        try:
            current = cal.current_meeting()
        except Exception:
            current = None
        self.start_card.set_calendar_meeting(current, {m.get("id") for m in rows or []})

    def set_connecting(self, on):
        self.connect_card.set_connecting(on)
        self.rail.sync_calendar()

    def calendar_error(self, text, tone="red"):
        """A line for the Connect card and the rail while the calendar isn't connected
        ("" clears it): the last Connect's error, or that access expired."""
        self.connect_card.set_error(text, tone)
        self.rail.set_calendar_error(text)


class LiveStrip(QFrame):
    """'Recording · Q4 roadmap   12:34   [Open]' above the start card while a
    meeting records or its notes are being written."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.setProperty("gusan", "liveStrip")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 10, 8)
        lay.setSpacing(10)
        self.dot = dot_label("red", 8, self)
        lay.addWidget(self.dot, 0, Qt.AlignVCenter)
        self.text = ElidedLabel("", self)
        self.text.setFont(theme.font(13.5, 600))
        self.text.setProperty("mtone", "ink")
        lay.addWidget(self.text, 1)
        self.open_btn = Button("Open", "secondary", "s", parent=self)
        self.open_btn.setAccessibleName("Open the recording")
        self.open_btn.clicked.connect(self._open)
        lay.addWidget(self.open_btn)
        self.hide()

    def _open(self):
        if self.page.is_rewriting():            # a saved meeting's notes: its notes view
            s = self.page.session()
            try:
                folder = s.active_dir() if s is not None else None
            except Exception:
                folder = None
            if folder:
                self.page.open_notes(str(folder))
                return
        self.page.show_live()

    def sync(self):
        s = self.page.session()
        phase = getattr(s, "phase", "idle") if s is not None else "idle"
        if phase not in ("recording", "paused", "processing"):
            self.hide()
            return
        words = {"recording": "Recording", "paused": "Paused", "processing": "Writing notes"}[phase]
        title = getattr(s, "title", "") or "Meeting"
        extra = ""
        if phase != "processing":
            try:
                extra = "  ·  " + views.fmt_clock(s.elapsed_sec())
            except Exception:
                extra = ""
        self.text.setText("%s · %s%s" % (words, title, extra))
        self.dot.setPixmap(dot_pixmap("amber" if phase == "paused" else ("blue-l" if phase == "processing" else "red"), 8))
        theme.set_prop(self, "tone", "blue" if phase == "processing" else "red", children=False)
        self.show()


# ── the start card ───────────────────────────────────────────────────────────
class RecordButton(QAbstractButton):
    """The big record button, drawn as an object: a white disc with a red dot,
    'Record a meeting' and the PRO tag under it."""

    W, CIRCLE, RING = 196, 112, 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Record a meeting, Pro feature")
        self.setToolTip("Record a meeting")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_Hover, True)
        theme.focus_ring(self)
        self.setFixedSize(self.W, self.RING + self.CIRCLE + self.RING + 6 + 20 + 2)
        lab = QWidget(self)
        lab.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        h = QHBoxLayout(lab)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addStretch(1)
        self.text_label = tlabel("Record a meeting", 15, 700, "ink", parent=lab)
        h.addWidget(self.text_label, 0, Qt.AlignVCenter)
        self.pro_tag = ProTag("s", parent=lab)
        h.addWidget(self.pro_tag, 0, Qt.AlignVCenter)
        h.addStretch(1)
        lab.setGeometry(0, self.RING * 2 + self.CIRCLE + 6, self.W, 20)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        cx = self.W / 2.0
        cy = self.RING + self.CIRCLE / 2.0
        r = self.CIRCLE / 2.0
        # soft shadow under the disc (0 24px 48px rgba(15,23,42,.12)), then the 10 px red ring
        g = QRadialGradient(cx, cy + 14, r + 14)
        g.setColorAt(0.70, QColor(15, 23, 42, 34))
        g.setColorAt(1.0, QColor(15, 23, 42, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QRectF(cx - r - 14, cy + 14 - r - 14, 2 * (r + 14), 2 * (r + 14)))
        ring = QColor(220, 38, 38, 26 if self.underMouse() else 15)
        p.setBrush(ring)
        p.drawEllipse(QRectF(cx - r - self.RING, cy - r - self.RING, 2 * (r + self.RING), 2 * (r + self.RING)))
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(theme.qcolor("line2"), 1))
        p.drawEllipse(QRectF(cx - r + 0.5, cy - r + 0.5, 2 * r - 1, 2 * r - 1))
        # the red dot with its glow (0 6px 14px rgba(220,38,38,.35))
        dr = 22.0
        glow = QRadialGradient(cx, cy + 6, dr + 10)
        glow.setColorAt(0.6, QColor(220, 38, 38, 70))
        glow.setColorAt(1.0, QColor(220, 38, 38, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QRectF(cx - dr - 10, cy + 6 - dr - 10, 2 * (dr + 10), 2 * (dr + 10)))
        red = theme.qcolor("red")
        if self.isDown():
            red = red.darker(115)
        elif self.underMouse():
            red = red.darker(106)
        p.setBrush(red)
        p.drawEllipse(QRectF(cx - dr, cy - dr, 2 * dr, 2 * dr))
        if self.hasFocus() and self.property("focusVisible"):
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(theme.qcolor("blue-l"), 2))
            rr = r + self.RING - 1
            p.drawEllipse(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr))
        p.end()


class TitleEdit(QLineEdit):
    """The start card's title: big display text, as wide as its words."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "titleEdit")
        self.setFont(theme.font(24, 700, display=True))
        self.setFixedHeight(32)
        self.setAccessibleName("Meeting name")
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.textChanged.connect(lambda _t: self.updateGeometry())

    def sizeHint(self):
        fm = self.fontMetrics()
        text = self.text() or self.placeholderText()
        return QSize(fm.horizontalAdvance(text) + 8, 32)

    def minimumSizeHint(self):
        return QSize(60, 32)


class PersonChip(QPushButton):
    """A person in the People field (click to take them off)."""

    def __init__(self, name, me="", parent=None):
        super().__init__(parent)
        self.name = name
        self.setProperty("gusan", "personChip")
        self.setFixedHeight(26)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.setAccessibleName("Remove %s" % name)
        self.setToolTip(theme.plain_tip("Remove %s" % name))     # a name from an invite is text, never markup
        h = QHBoxLayout(self)
        h.setContentsMargins(2, 0, 10, 0)
        h.setSpacing(6)
        h.addWidget(Faces([name], 22, me=me, parent=self), 0, Qt.AlignVCenter)
        t = tlabel(name, 12.5, 600, "text2", parent=self)
        t.setTextFormat(Qt.PlainText)
        t.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        h.addWidget(t, 0, Qt.AlignVCenter)
        self.setFixedWidth(h.sizeHint().width())


class PeopleField(QFrame):
    """Chips for the people on the call + a box to add more (Enter or comma)."""

    changed = Signal()
    MAX_SHOWN = 3

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.setProperty("gusan", "peopleField")
        self.setFixedHeight(40)
        self._names = []
        self._placeholder = "Add people (optional)"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 8, 0)
        lay.setSpacing(6)
        lay.addWidget(icon_label("people", 16, "muted", parent=self), 0, Qt.AlignVCenter)
        self._chips = QHBoxLayout()
        self._chips.setSpacing(6)
        lay.addLayout(self._chips)
        self.more = QLabel(self)
        self.more.setProperty("gusan", "moreChip")
        self.more.setFont(theme.font(12.5, 700))
        self.more.setFixedHeight(26)
        self.more.setMinimumWidth(32)
        self.more.setAlignment(Qt.AlignCenter)
        self.more.hide()
        lay.addWidget(self.more, 0, Qt.AlignVCenter)
        self.input = QLineEdit(self)
        self.input.setProperty("gusan", "bare")
        self.input.setFont(theme.font(13.5))
        self.input.setAccessibleName("People (optional)")
        self.input.setPlaceholderText(self._placeholder)
        self.input.setTextMargins(4, 0, 0, 0)
        self.input.returnPressed.connect(self.commit)
        self.input.textEdited.connect(self._on_edit)
        self.input.installEventFilter(self)
        lay.addWidget(self.input, 1)

    def names(self):
        pending = [n.strip() for n in re.split(r"[,;]", self.input.text()) if n.strip()]
        out = list(self._names)
        for n in pending:
            if n.lower() not in {x.lower() for x in out}:
                out.append(n)
        return out

    def set_names(self, names, placeholder=None):
        self._names = []
        for n in names or []:
            n = (n or "").strip()
            if n and n.lower() not in {x.lower() for x in self._names}:
                self._names.append(n)
        if placeholder is not None:
            self._placeholder = placeholder
        self.input.clear()
        self._render()

    def add(self, name):
        name = (name or "").strip()
        if name and name.lower() not in {x.lower() for x in self._names}:
            self._names.append(name)
            self._render()
            self.changed.emit()

    def remove(self, name):
        self._names = [n for n in self._names if n != name]
        self._render()
        self.changed.emit()
        self.input.setFocus()

    def commit(self):
        parts = [n.strip() for n in re.split(r"[,;]", self.input.text()) if n.strip()]
        self.input.clear()
        for n in parts:
            self.add(n)

    def _on_edit(self, text):
        if "," in text or ";" in text:
            self.commit()

    def eventFilter(self, obj, ev):
        if obj is self.input:
            t = ev.type()
            if t == QEvent.KeyPress and ev.key() == Qt.Key_Backspace and not self.input.text() and self._names:
                self.remove(self._names[-1])
                return True
            if t == QEvent.FocusIn:
                theme.set_prop(self, "focus", True, children=False)
            elif t == QEvent.FocusOut:
                theme.set_prop(self, "focus", False, children=False)
                if self.input.text().strip():
                    self.commit()
        return False

    def _render(self):
        views.clear_layout(self._chips)
        names = self._names
        shown = names if len(names) <= self.MAX_SHOWN else names[:2]
        me = self.page.me()
        for n in shown:
            chip = PersonChip(n, me=me, parent=self)
            chip.clicked.connect(lambda _=False, name=n: self.remove(name))
            self._chips.addWidget(chip, 0, Qt.AlignVCenter)
        hidden = names[len(shown):]
        if hidden:
            self.more.setText("+%d" % len(hidden))
            self.more.setToolTip(theme.plain_tip(", ".join(hidden)))
            self.more.setAccessibleName("%d more: %s" % (len(hidden), ", ".join(hidden)))
            self.more.show()
        else:
            self.more.hide()
        self.input.setPlaceholderText(self._placeholder)


def _combo_icon(names, width=34, size=15):
    """Leading icons for a combo item, left-aligned on one canvas width."""
    dpr = views._dpr()
    pm = QPixmap(int(round(width * dpr)), int(round(size * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    x = 0
    for n in names:
        p.drawPixmap(x, 0, icons.pixmap(n, size, "muted"))
        x += size + 4
    p.end()
    return QIcon(pm)


class ModeCombo(QComboBox):
    """'Record from': Both sides of the call / Only my microphone / Only the call's sound."""

    def __init__(self, with_icons=False, accessible="Record from", parent=None):
        super().__init__(parent)
        self.setAccessibleName(accessible)
        self.setFont(theme.font(13.5))
        self.setFixedHeight(36)
        if with_icons:
            self.setIconSize(QSize(34, 15))
        for mode in _audio_modes():
            text, ics = AUDIO_MODES[mode]
            if with_icons:
                self.addItem(_combo_icon(ics), text, mode)
            else:
                self.addItem(text, mode)

    def set_mode(self, mode):
        i = self.findData(mode)
        self.blockSignals(True)
        self.setCurrentIndex(i if i >= 0 else 0)
        self.blockSignals(False)


class LangCombo(QComboBox):
    def __init__(self, with_icons=False, accessible="Meeting language", parent=None):
        super().__init__(parent)
        self.setAccessibleName(accessible)
        self.setFont(theme.font(13.5))
        self.setFixedHeight(36)
        if with_icons:
            self.setIconSize(QSize(15, 15))
        for code, text in LANGS:
            if with_icons:
                self.addItem(icons.icon("globe", 15, "muted"), text, code)
            else:
                self.addItem(text, code)

    def set_code(self, code):
        i = self.findData(code)
        self.blockSignals(True)
        self.setCurrentIndex(i if i >= 0 else 0)
        self.blockSignals(False)


def _default_mode():
    """'Record from' for every new meeting: Both sides of the call whenever
    this system can capture the call's sound (else the best it can). A pick on
    the start card is for that one meeting only - it is never saved."""
    modes = _audio_modes()
    return "smart_meeting" if "smart_meeting" in modes else (modes[0] if modes else "default_mic")


def _saved_lang(cfg):
    return cfg.get("meeting_language") or cfg.get("language") or "auto"


class StartCard(QFrame):
    """The start card: Record a meeting (PRO), the meeting's name, people,
    Record from, Language and the consent line with Copy notice."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.setProperty("gusan", "whiteCard")
        self.setProperty("radius", "20")
        self.setAccessibleName("Record a meeting")
        theme.elevate(self, "lg")
        self.calendar_event_id = ""
        self._edited = False
        self._picked = False            # a "Coming up" row (or a reminder) filled it: kept until that meeting is over
        self._cal_meeting_id = ""
        self._has_agenda = False        # the invite had an agenda (the box shows it)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(18)
        top = QHBoxLayout()
        top.setSpacing(28)
        self.record_btn = RecordButton(self)
        self.record_btn.clicked.connect(lambda: self.page.record())
        top.addWidget(self.record_btn, 0, Qt.AlignVCenter)

        form = QVBoxLayout()
        form.setSpacing(14)
        tblock = QVBoxLayout()
        tblock.setSpacing(2)
        trow = QHBoxLayout()
        trow.setSpacing(6)
        self.title_edit = TitleEdit(self)
        self.title_edit.textEdited.connect(self._on_title_edited)
        trow.addWidget(self.title_edit, 0, Qt.AlignVCenter)
        self.pen_btn = IconButton("pen", "Rename meeting", size=28, icon_size=15, color="muted", parent=self)
        self.pen_btn.clicked.connect(self._focus_title)
        trow.addWidget(self.pen_btn, 0, Qt.AlignVCenter)
        trow.addStretch(1)
        tblock.addLayout(trow)
        self.cal_sub = QWidget(self)
        cs = QHBoxLayout(self.cal_sub)
        cs.setContentsMargins(0, 0, 0, 0)
        cs.setSpacing(6)
        cs.addWidget(google_label(14, self.cal_sub), 0, Qt.AlignVCenter)
        self.cal_sub_label = label("", "muted", self.cal_sub)
        cs.addWidget(self.cal_sub_label, 0, Qt.AlignVCenter)
        cs.addStretch(1)
        self.cal_sub.hide()
        tblock.addWidget(self.cal_sub)
        form.addLayout(tblock)

        self.people = PeopleField(page, self)
        self.people.changed.connect(self._on_people_changed)
        form.addWidget(self.people)

        # The invite's agenda (a meeting from the calendar that has one): plain text
        # the user can read, edit or clear. It goes into the notes' header and to Live
        # Assistant with the title and the names - never anything the user can't see.
        self.agenda_box = QWidget(self)
        ab = QVBoxLayout(self.agenda_box)
        ab.setContentsMargins(0, 0, 0, 0)
        ab.setSpacing(6)
        ah = QHBoxLayout()
        ah.setContentsMargins(0, 0, 0, 0)
        ah.setSpacing(8)
        ah.addWidget(tlabel("Agenda from your invite", 12.5, 600, "muted", parent=self.agenda_box))
        ah.addStretch(1)
        self.agenda_clear = Button("Clear", "link", "s", parent=self.agenda_box)
        self.agenda_clear.setProperty("tone", "muted")
        self.agenda_clear.setFont(theme.font(12.5, 600))
        self.agenda_clear.setFixedHeight(22)
        self.agenda_clear.setAccessibleName("Clear the agenda")
        self.agenda_clear.clicked.connect(self._clear_agenda)
        ah.addWidget(self.agenda_clear)
        ab.addLayout(ah)
        self.agenda_edit = QPlainTextEdit(self.agenda_box)
        self.agenda_edit.setProperty("gusan", "notesBox")
        self.agenda_edit.setAccessibleName("Agenda from your invite")
        self.agenda_edit.setFont(theme.font(13.5))
        self.agenda_edit.setTabChangesFocus(True)
        self.agenda_edit.setFixedHeight(84)
        self.agenda_edit.setToolTip("Goes into your notes and to Live Assistant. Edit it or clear it.")
        self.agenda_edit.textChanged.connect(self._on_agenda_edited)
        ab.addWidget(self.agenda_edit)
        self.agenda_box.hide()
        form.addWidget(self.agenda_box)

        fields = QVBoxLayout()
        fields.setSpacing(10)
        self.mode_combo = ModeCombo(with_icons=True, accessible="Record from", parent=self)
        self.mode_combo.activated.connect(self._on_mode)
        fields.addLayout(self._field("Record from", self.mode_combo))
        self.lang_combo = LangCombo(with_icons=True, accessible="Meeting language", parent=self)
        self.lang_combo.activated.connect(self._on_lang)
        fields.addLayout(self._field("Language", self.lang_combo))
        form.addLayout(fields)
        top.addLayout(form, 1)
        lay.addLayout(top)

        self.consent_divider = Divider(self)
        lay.addWidget(self.consent_divider)
        self.consent_row = QWidget(self)
        cr = QHBoxLayout(self.consent_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.setSpacing(10)
        cr.addWidget(icon_label("bubble", 16, "muted", parent=self.consent_row), 0, Qt.AlignVCenter)
        cr.addWidget(tlabel("Let everyone know you’re recording.", 13.5, 400, "text2", parent=self.consent_row))
        cr.addStretch(1)
        self.copy_notice_btn = Button("Copy notice", "secondary", "s", icon="copy", parent=self.consent_row)
        self.copy_notice_btn.setToolTip(NOTICE_TEXT)
        self.copy_notice_btn.clicked.connect(self._copy_notice)
        cr.addWidget(self.copy_notice_btn)
        lay.addWidget(self.consent_row)
        self.error_label = label("", "error", self, wrap=True)
        self.error_label.hide()
        lay.addWidget(self.error_label)
        self._copied = QTimer(self)
        self._copied.setSingleShot(True)
        self._copied.setInterval(1600)
        self._copied.timeout.connect(lambda: self.copy_notice_btn.setText("Copy notice"))
        self.sync_settings()
        self.sync_default(force=True)

    @staticmethod
    def _field(text, control):
        row = QHBoxLayout()
        row.setSpacing(12)
        lb = tlabel(text, 12.5, 600, "muted")
        lb.setFixedWidth(84)
        row.addWidget(lb, 0, Qt.AlignVCenter)
        row.addWidget(control, 1)
        return row

    # ── values ──
    def title(self):
        return self.title_edit.text().strip() or _default_title()

    def audio_mode(self):
        return self.mode_combo.currentData() or None

    def language(self):
        return self.lang_combo.currentData() or None

    def agenda(self):
        """The agenda to start with: what the box shows (the user's own edit of the
        invite's text), "" when there is none or no calendar meeting is on the card."""
        if not (self.calendar_event_id and self._has_agenda):
            return ""
        return invite_context.clip_agenda(self.agenda_edit.toPlainText())

    # ── sync ──
    def sync_settings(self):
        cfg = self.page.cfg()
        self.mode_combo.set_mode(getattr(self, "_mode_pick", None) or _default_mode())
        self.lang_combo.set_code(_saved_lang(cfg))
        on = bool(cfg.get(CONSENT_KEY, True))
        self.consent_divider.setVisible(on)
        self.consent_row.setVisible(on)

    def sync_default(self, force=False):
        """Keep the default name current ('Meeting · Tue 14:30') until the user types one."""
        if self._edited and not force:
            return
        if self.calendar_event_id:
            return
        self.title_edit.setText(_default_title())

    def set_calendar_meeting(self, m, live_ids=None):
        """Fill the card from the meeting on now (or starting within 5 min).
        ``live_ids``: the ids of the meetings still in the list. A meeting the user
        picked (a "Coming up" row) stays on the card while it is in it, whichever
        meeting is on now; once it is over the card follows the calendar again."""
        if self._edited:
            return
        if self._picked:
            if live_ids is None or self._cal_meeting_id in live_ids:
                return                  # the meeting the user picked stays until it is over
            self._picked = False        # it ended or is gone: follow the calendar again
        if not m:
            if self.calendar_event_id and self._cal_meeting_id:
                self.calendar_event_id = ""
                self._cal_meeting_id = ""
                self.cal_sub.hide()
                self.people.set_names([], "Add people (optional)")
                self._set_agenda("")
                self.sync_default(force=True)
            return
        if m.get("id") == self._cal_meeting_id:
            self._sync_cal_time(m)
            return
        self._cal_meeting_id = m.get("id") or ""
        self.calendar_event_id = self._cal_meeting_id
        self.title_edit.setText(m.get("title") or _default_title())
        self._sync_cal_time(m)
        self.cal_sub.show()
        others = m.get("others")
        if not isinstance(others, list):
            others = [p.strip() for p in str(m.get("people") or "").split(",") if p.strip() and "+" not in p]
        self.people.set_names(others, "Add people")
        self._set_agenda("" if m.get("private") else m.get("agenda"))

    def _sync_cal_time(self, m):
        when = ""
        try:
            import calendar_sync
            when = calendar_sync.when_parts(m, _utcnow()).get("time", "")
        except Exception:
            start = m.get("start")
            when = start.astimezone().strftime("%H:%M") if hasattr(start, "astimezone") else ""
        self.cal_sub_label.setText("From your calendar · %s" % when if when else "From your calendar")

    def prefill(self, title=None, attendees=None, calendar_event_id=None, agenda=None):
        """Fill the card from outside (the calendar reminder, a "Coming up" row)."""
        if title is not None:
            self.title_edit.setText(str(title))
        if attendees is not None:
            names = attendees if isinstance(attendees, (list, tuple)) else \
                [n.strip() for n in str(attendees).split(",") if n.strip()]
            self.people.set_names(names, "Add people")
        if calendar_event_id is not None:
            self.calendar_event_id = str(calendar_event_id or "")
            self._cal_meeting_id = self.calendar_event_id
            self.cal_sub.setVisible(bool(self.calendar_event_id))
            if self.calendar_event_id:
                self.cal_sub_label.setText("From your calendar")
            if agenda is None:
                self._set_agenda("")            # another meeting: not the last one's agenda
        if agenda is not None:
            self._set_agenda(agenda)
        if calendar_event_id:
            # A picked calendar meeting: kept until it is over, or until the user types
            # (programmatic setText / set_names don't count as typing).
            self._picked, self._edited = True, False
        else:
            self._edited = True

    def reset(self):
        """After a recording started: back to a fresh card."""
        self._edited = False
        self._picked = False
        self.calendar_event_id = ""
        self._cal_meeting_id = ""
        self.cal_sub.hide()
        self.people.set_names([], "Add people (optional)")
        self._set_agenda("")
        self._mode_pick = None                  # the next meeting: Both sides again
        self.mode_combo.set_mode(_default_mode())
        self.sync_default(force=True)
        self.show_error("")

    def show_error(self, text):
        self.error_label.setText(text or "")
        self.error_label.setVisible(bool(text))

    def _set_agenda(self, text):
        """Fill the agenda box (the invite's text); it shows only for a calendar
        meeting that has one."""
        text = invite_context.clip_agenda(text)
        self.agenda_edit.blockSignals(True)
        self.agenda_edit.setPlainText(text)
        self.agenda_edit.blockSignals(False)
        self._has_agenda = bool(text)
        self._sync_agenda_box()

    def _sync_agenda_box(self):
        self.agenda_box.setVisible(bool(self.calendar_event_id and self._has_agenda))

    def _on_agenda_edited(self):
        self._edited = True                     # what the user changed is not replaced by a refresh

    def _clear_agenda(self):
        self._set_agenda("")
        self._edited = True

    # ── user actions ──
    def _on_title_edited(self, _t):
        self._edited = True

    def _on_people_changed(self):
        self._edited = True

    def _focus_title(self):
        self.title_edit.setFocus(Qt.OtherFocusReason)
        self.title_edit.selectAll()

    def _on_mode(self, _i):
        """This meeting only: start() gets it as audio_mode; never saved."""
        self._mode_pick = self.mode_combo.currentData() or None

    def _on_lang(self, _i):
        code = self.lang_combo.currentData()
        if code:
            self.page.set_setting("meeting_language", code)

    def _copy_notice(self):
        QGuiApplication.clipboard().setText(NOTICE_TEXT)
        self.copy_notice_btn.setText("Copied")
        self._copied.start()


# ── Google Calendar ──────────────────────────────────────────────────────────
class ConnectCard(Card):
    """Calendar not connected: one compact card with the Google G."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self._connecting = False
        self._error = ""                        # shown until the next connect (set_error)
        self._error_tone = "red"
        self.setAccessibleName("Connect Google Calendar")
        self.layout().setContentsMargins(14, 14, 16, 14)
        row = QHBoxLayout()
        row.setSpacing(14)
        row.addWidget(views.GoogleTile(40, 20, self), 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(tlabel("Connect Google Calendar", 14.5, 700, "ink", parent=self))
        self.sub = tlabel(calendar_door.INVITE_SUB, 13, 400, "muted", parent=self, wrap=True)
        col.addWidget(self.sub)
        row.addLayout(col, 1)
        self.connect_btn = Button("Connect", "secondary", "s", parent=self)
        self.connect_btn.setAccessibleName("Connect Google Calendar")
        self.connect_btn.clicked.connect(self._connect)
        row.addWidget(self.connect_btn, 0, Qt.AlignVCenter)
        self.cancel_btn = Button("Cancel", "ghost", "s", parent=self)
        self.cancel_btn.setAccessibleName("Cancel connecting Google Calendar")
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.hide()
        row.addWidget(self.cancel_btn, 0, Qt.AlignVCenter)
        self.add(row)

    def sync(self):
        cal = self.page.calendar()
        if cal is not None and getattr(cal, "is_connected", False):
            self._error = ""                    # connected: an old error is over
        self.set_connecting(bool(cal is not None and getattr(cal, "is_connecting", False)))

    def set_connecting(self, on):
        self.connect_btn.setVisible(not on)
        self.cancel_btn.setVisible(bool(on))
        self._connecting = bool(on)
        if on:
            self._error = ""                    # a new try: the last one's error is over
        self._show_line()

    def set_error(self, text, tone="red"):
        """The line under the title for a Connect error or an expired grant; it stays
        until the next connect (or "" clears it)."""
        self._error, self._error_tone = str(text or ""), tone
        self._show_line()

    def _show_line(self):
        text, tone = calendar_door.line_for(self._connecting, self._error, self._error_tone)
        self.sub.setText(text)
        views.set_tone(self.sub, tone)

    def _connect(self):
        self.page.connect_calendar()            # the disclosure first, then the browser

    def _cancel(self):
        calendar_door.cancel_connect(self.page.calendar())


class TimelineDot(QWidget):
    """The line through 'Coming up' and one meeting's dot on it."""

    def __init__(self, first, last, nxt, parent=None):
        super().__init__(parent)
        self._first, self._last, self._next = first, last, nxt
        self.setFixedWidth(12)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("line2"))
        if not self._first:
            p.drawRect(QRectF(cx - 1, 0, 2, cy - 8))
        if not self._last:
            p.drawRect(QRectF(cx - 1, cy + 8, 2, self.height() - cy - 8))
        if self._next:
            p.setBrush(QColor(59, 130, 246, 46))
            p.drawEllipse(QRectF(cx - 9, cy - 9, 18, 18))
            p.setBrush(theme.qcolor("blue"))
            p.drawEllipse(QRectF(cx - 5, cy - 5, 10, 10))
        else:
            p.setBrush(QColor("#ffffff"))
            p.setPen(QPen(theme.qcolor("line2"), 2))
            p.drawEllipse(QRectF(cx - 4, cy - 4, 8, 8))
        p.end()


class BellButton(QToolButton):
    """'Ask me to record' for one meeting (checked = on)."""

    def __init__(self, title, on, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "bell")
        self.setCheckable(True)
        self.setChecked(bool(on))
        self.setFixedSize(32, 32)
        self.setIconSize(QSize(16, 16))
        self.setAccessibleName("Ask me to record: %s" % title)
        self.setToolTip("Ask me to record")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.toggled.connect(lambda _on: self._paint_icon())
        self._paint_icon()

    def _paint_icon(self):
        on = self.isChecked()
        self.setIcon(QIcon(glyph("bell_on" if on else "bell_off", 16, "blue-tx" if on else "muted",
                                 1.9 if on else 1.7)))


_day_word = calendar_sync.day_word       # "Today", "Tomorrow" or "Thursday" (Home's rows say it too)


class UpcomingRow(QFrame):
    """One meeting under "Coming up": time (and "in 5 min" / "Now"), title, the call
    type and who is on it, a Join link (https only) and the bell. Pressing the row
    fills the start card with it; there is no Record button here (calm)."""

    clicked = Signal(object)

    def __init__(self, page, m, first, last, nxt, now, parent=None):
        super().__init__(parent)
        self.page = page
        self.meeting = m
        self.setProperty("gusan", "upRow")
        self.setProperty("next", bool(nxt))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        if nxt:
            theme.elevate(self, "item")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 12, 0)
        lay.setSpacing(14)
        parts = {"time": "", "rel": "", "day": ""}
        try:
            import calendar_sync
            parts = calendar_sync.when_parts(m, now)
        except Exception:
            start = m.get("start")
            if hasattr(start, "astimezone"):
                parts["time"] = start.astimezone().strftime("%H:%M")
        tcol = QWidget(self)
        tcol.setFixedWidth(52)
        tl = QVBoxLayout(tcol)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        tl.addStretch(1)
        self.time_label = tlabel(parts.get("time", ""), 14.5, 700, "ink", parent=tcol, tabular=True)
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        tl.addWidget(self.time_label)
        # The day is the group header's job now; this line is "in 5 min" or "Now".
        rel = parts.get("rel") or ""
        self.rel_label = tlabel(rel, 11.5, 700 if (nxt or rel == "Now") else 600,
                                "blue" if (rel == "Now" or (nxt and rel)) else "muted", parent=tcol)
        self.rel_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.rel_label.setVisible(bool(rel))
        tl.addWidget(self.rel_label)
        tl.addStretch(1)
        lay.addWidget(tcol)
        lay.addWidget(TimelineDot(first, last, nxt, self))
        body = QVBoxLayout()
        body.setContentsMargins(0, 10, 0, 10)
        body.setSpacing(3)
        title = m.get("title") or "Untitled meeting"
        self.title_label = ElidedLabel(title, self)
        self.title_label.setFont(theme.font(14.5, 700 if nxt else 600))
        self.title_label.setProperty("mtone", "ink")
        body.addWidget(self.title_label)
        call = call_name(m.get("call_type"))
        people = m.get("people") or ""
        meta_text = " · ".join(x for x in (call, people) if x)
        if meta_text:
            meta = QHBoxLayout()
            meta.setSpacing(6)
            if m.get("call_type"):
                meta.addWidget(call_mark(m.get("call_type"), 18), 0, Qt.AlignVCenter)
            ml = ElidedLabel(meta_text, self)
            ml.setFont(theme.font(12.5))
            ml.setProperty("mtone", "muted")
            meta.addWidget(ml, 1)
            body.addLayout(meta)
        lay.addLayout(body, 1)
        names = [str(n) for n in (m.get("others") or []) if n]
        self.setToolTip(theme.plain_tip(", ".join(names)))      # everyone, when the label says "+2"
        link = calendar_sync.join_url(m)                   # https only: the only kind a Join opens
        self.join_button = None
        self.join_link = ""
        if link:
            self.join_link = link
            self.join_button = Button("Join", "link", "s", parent=self)
            self.join_button.setFont(theme.font(12.5, 600))
            self.join_button.setAccessibleName("Join %s" % title)
            try:
                from urllib.parse import urlsplit
                self.join_button.setToolTip(theme.plain_tip(urlsplit(link).hostname or ""))
            except ValueError:
                pass
            self.join_button.clicked.connect(lambda _=False: self._join())
            lay.addWidget(self.join_button, 0, Qt.AlignVCenter)
        self.bell = BellButton(title, not m.get("muted"), self)
        self.bell.toggled.connect(self._on_bell)
        lay.addWidget(self.bell, 0, Qt.AlignVCenter)
        self.setMinimumHeight(62)
        who = (" with " + people) if people else ""
        self.setAccessibleName("%s, %s%s" % (title, parts.get("time", ""), who))
        self.setAccessibleDescription("Press to fill the start card with this meeting")

    def _on_bell(self, on):
        cal = self.page.calendar()
        if cal is None:
            return
        try:
            cal.set_muted(self.meeting.get("id"), not on)
        except Exception:
            logger.warning("set_muted failed", exc_info=True)

    def _join(self):
        calendar_door.open_join(self.join_link)

    def mousePressEvent(self, e):
        e.accept()                                          # so the release comes here

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.meeting)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit(self.meeting)
            return
        super().keyPressEvent(e)


class ComingUp(QWidget):
    """Your meetings from Google Calendar, today and tomorrow, as a simple time line:
    a "Today" / "Tomorrow" header (a weekday name later), the first five meetings and
    "Show more" for the rest (up to 20), one muted line when the calendar can't be
    read or is paused."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.setAccessibleName("Coming up")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 12, 0)
        head.setSpacing(8)
        head.addWidget(label("Coming up", "h2", self), 0, Qt.AlignVCenter)
        head.addWidget(google_label(16, self), 0, Qt.AlignVCenter)
        head.addStretch(1)
        head.addWidget(icon_label("bell_on", 14, "muted", parent=self), 0, Qt.AlignVCenter)
        head.addWidget(label("Ask me to record", "muted", self), 0, Qt.AlignVCenter)
        lay.addLayout(head)
        self._rows = QVBoxLayout()
        self._rows.setSpacing(0)
        lay.addLayout(self._rows)
        self.more = Button("Show more", "link", "s", parent=self)
        self.more.setFont(theme.font(12.5, 600))
        self.more.clicked.connect(self._toggle_more)
        self.more.hide()
        lay.addWidget(self.more, 0, Qt.AlignLeft)
        self.empty = label("No meetings coming up.", "muted", self)
        self.empty.setContentsMargins(6, 4, 0, 4)
        self.empty.hide()
        lay.addWidget(self.empty)
        self.status = label("", "muted", self, wrap=True)
        self.status.setContentsMargins(6, 0, 0, 0)
        self.status.hide()
        lay.addWidget(self.status)
        self.rows = []              # the meeting rows (the day headers are not in it)
        self.headers = []
        self._all = []
        self._expanded = False
        self._key = None

    def set_status(self, text):
        """The muted line under the list ("" hides it)."""
        text = str(text or "")
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _toggle_more(self):
        self._expanded = not self._expanded
        self._key = None                                    # a different slice: rebuild
        self.set_meetings(self._all)

    def set_meetings(self, meetings):
        now = _utcnow()
        meetings = list(meetings or [])[:COMING_MAX]
        self._all = meetings
        shown = meetings if self._expanded else meetings[:COMING_FIRST]
        # "No meetings coming up." only after a read: with Privacy mode on (or no
        # network) at launch nothing was read yet, and an empty list says nothing.
        read = bool(getattr(self.page.calendar(), "has_read", True))
        key = tuple((m.get("id"), str(m.get("start")), m.get("title"), m.get("people"), m.get("call_type"),
                     m.get("link"), len(m.get("others") or [])) for m in shown) + (
            now.strftime("%H:%M"), len(meetings), read)
        if key == self._key and len(self.rows) == len(shown):
            # Only a bell changed (set_muted re-emits the list): update the
            # bells in place, so the one just toggled keeps keyboard focus.
            for row, m in zip(self.rows, shown):
                row.meeting = m
                row.bell.blockSignals(True)
                row.bell.setChecked(not m.get("muted"))
                row.bell.blockSignals(False)
                row.bell._paint_icon()
            return
        self._key = key
        views.clear_layout(self._rows)
        self.rows, self.headers = [], []
        # The meetings sit in day groups; the time line runs inside a group.
        groups = []
        for m in shown:
            day = _day_word(m, now)
            if groups and groups[-1][0] == day:
                groups[-1][1].append(m)
            else:
                groups.append((day, [m]))
        index = 0
        for gi, (day, rows) in enumerate(groups):
            head = tlabel(day, 12.5, 700, "muted", parent=self)
            head.setContentsMargins(6, 10 if gi else 2, 0, 4)
            head.setAccessibleName(day)
            self._rows.addWidget(head)
            self.headers.append(head)
            for i, m in enumerate(rows):
                row = UpcomingRow(self.page, m, i == 0, i == len(rows) - 1, index == 0, now, self)
                row.clicked.connect(self.page.start_from_meeting)
                self._rows.addWidget(row)
                self.rows.append(row)
                index += 1
        self.empty.setVisible(not shown and read)
        self.more.setVisible(len(meetings) > COMING_FIRST)
        self.more.setText("Show less" if self._expanded else "Show more")
        self.more.setAccessibleName("Show fewer meetings" if self._expanded else "Show more meetings")


# ── Your meetings ────────────────────────────────────────────────────────────
STATUS = {
    "recording": ("Recording", "red", None),
    "processing": ("Writing notes…", "blue", None),
    "notes_failed": ("No notes", "amber", "alert"),
    "audio_only": ("Audio only", "neutral", None),
}


class MeetingRow(QAbstractButton):
    """One past meeting: title (dot when the notes are new), date, status,
    people, length. A trash icon at its right end (hover, or the keyboard on the row) deletes it
    without opening it; while the list is selecting, a check box in front ticks it
    (MeetingsList; ui/components/row_actions.py)."""

    def __init__(self, page, m, status, first=False, parent=None, owner=None):
        super().__init__(parent)
        self.page = page
        self.owner = owner              # the MeetingsList (None: a row on its own)
        self.meeting = m
        self.status = status
        self._first = first
        self._selected = False
        self._select_mode = False
        self._deleting = False
        self._blocked = ""
        self._base_name = ""
        # A button's default policy cannot shrink below its size hint, so one long title made the whole
        # column (and the page) wider than the window. The title elides; the row may be narrower.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(64)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAttribute(Qt.WA_Hover, True)
        theme.focus_ring(self)
        new = status == "notes_ready" and not m.get("seen", True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 6, 0)
        lay.setSpacing(16)
        self.check = row_actions.RowCheck(self)       # in front of the row while the list is selecting
        self.check.hide()
        lay.addWidget(self.check, 0, Qt.AlignVCenter)
        left = QVBoxLayout()
        left.setSpacing(2)
        left.addStretch(1)
        top = QHBoxLayout()
        top.setSpacing(8)
        if new:
            top.addWidget(dot_label("blue", 7, self), 0, Qt.AlignVCenter)
        title = m.get("title") or "Untitled Meeting"
        self.title_label = ElidedLabel(title, self)
        self.title_label.setFont(theme.font(14.5, 700 if new else 600))
        self.title_label.setProperty("mtone", "ink")
        top.addWidget(self.title_label, 1)
        left.addLayout(top)
        when = fmt_when(m.get("started") or m.get("timestamp"))
        self.when_label = tlabel(when, 12.5, 400, "muted", parent=self)
        left.addWidget(self.when_label)
        left.addStretch(1)
        lay.addLayout(left, 1)
        chip_box = QHBoxLayout()
        chip_box.addStretch(1)
        chip_text = ""
        if new:
            chip_text = "Notes ready"
            chip_box.addWidget(Chip("Notes ready", "ok", "check", self))
        elif status in STATUS:
            text, tone, ic = STATUS[status]
            chip_text = text
            chip_box.addWidget(Chip(text, tone, ic, self))
        holder = QWidget(self)
        holder.setFixedWidth(120)
        holder.setLayout(chip_box)
        chip_box.setContentsMargins(0, 0, 0, 0)
        self.deleting_label = tlabel("Deleting…", 12.5, 600, "muted", parent=holder)
        self.deleting_label.hide()                    # takes the chip's place while the folder goes
        chip_box.addWidget(self.deleting_label)
        lay.addWidget(holder)
        faces_box = QWidget(self)
        faces_box.setFixedWidth(64)
        fb = QHBoxLayout(faces_box)
        fb.setContentsMargins(0, 0, 0, 0)
        fb.addStretch(1)
        people = m.get("attendees_list") or []
        fb.addWidget(Faces(people, 24, me=page.me(), parent=faces_box), 0, Qt.AlignVCenter)
        lay.addWidget(faces_box)
        length = fmt_duration(m.get("duration_sec")) if m.get("duration_sec") else ""
        self.length_label = tlabel(length, 13, 400, "text2", parent=self, tabular=True)
        self.length_label.setFixedWidth(72)               # "1 h 05 min" fits (it was cut to "h 05 min")
        self.length_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.length_label)
        end = QHBoxLayout()                           # the chevron, then the trash icon's place
        end.setContentsMargins(0, 0, 0, 0)
        end.setSpacing(4)
        end.addWidget(icon_label("chev_r", 16, "faint", parent=self), 0, Qt.AlignVCenter)
        lay.addLayout(end)
        for w in self.findChildren(QWidget):          # the row takes every click...
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # ...except the trash icon's: it is pressed on its own and never opens the meeting.
        self.trash = row_actions.attach_trash(self, end, title, self._ask_delete)
        self._base_name = ", ".join(x for x in (title, when, length, chip_text) if x)
        self.setAccessibleName(self._base_name)
        self.clicked.connect(self._on_click)

    def _open(self):
        self.page.open_notes(self.meeting.get("dir"), self.meeting)

    def _on_click(self):
        """Pressed: open the meeting - or, while the list is selecting, tick it."""
        if self._deleting:
            return
        if self.owner is not None and self.owner.select_mode:
            self.owner.toggle_row(self, shift=bool(QGuiApplication.keyboardModifiers() & Qt.ShiftModifier))
            return
        self._open()

    def _ask_delete(self):
        if self.owner is not None and not self._deleting:
            self.owner.ask_delete([self.meeting], "row")

    def keyPressEvent(self, e):
        key, owner = e.key(), self.owner
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        if owner is not None:
            if key == Qt.Key_Delete:
                owner.key_delete(self)
                return
            if key == Qt.Key_A and e.modifiers() & Qt.ControlModifier:
                owner.select_all()
                return
            if key == Qt.Key_Escape and owner.select_mode:
                owner.leave_select()
                return
        super().keyPressEvent(e)

    # ── delete / select state (driven by MeetingsList) ──
    def set_first(self, first):
        if bool(first) != self._first:
            self._first = bool(first)
            self.update()

    def _sync_name(self):
        name = self._base_name
        if self._select_mode:
            name += ", selected" if self._selected else ", not selected"
        self.setAccessibleName(name)

    def set_select_mode(self, on):
        self._select_mode = bool(on)
        self.check.setVisible(self._select_mode)
        self.trash.reveal.enabled = not self._select_mode
        self.trash.reveal.refresh(animate=False)
        if not on:
            self._selected = False
            self.check.setChecked(False)
        self.update()
        self._sync_name()

    def is_checked(self):
        return self._selected

    def set_checked(self, on):
        on = bool(on) and not self._blocked and not self._deleting
        self._selected = on
        self.check.setChecked(on)
        self.update()
        self._sync_name()

    def blocked(self):
        return self._blocked

    def set_blocked(self, reason):
        """Why this meeting can't be deleted right now ("Recording now" / "Finishing up"), or ""."""
        self._blocked = str(reason or "")
        self.trash.set_blocked(self._blocked)
        self.check.setEnabled(not self._blocked)
        if self._blocked and self._selected:
            self.set_checked(False)
        self.trash.reveal.refresh(animate=False)

    def is_deleting(self):
        return self._deleting

    def set_deleting(self, on):
        """The folder is on its way to the trash: the row dims, says so, and can't be pressed."""
        on = bool(on)
        if on == self._deleting:
            return
        self._deleting = on
        if on:
            self._selected = False
            self.check.setChecked(False)
        for chip in self.findChildren(Chip):
            chip.setVisible(not on)
        self.deleting_label.setVisible(on)
        self.setEnabled(not on)
        self.trash.setEnabled(not on and not self._blocked)
        if on:
            self.trash.reveal.enabled = False
            self.trash.reveal.refresh(animate=False)
            fx = QGraphicsOpacityEffect(self)
            fx.setOpacity(0.5)
            self.setGraphicsEffect(fx)
        else:
            self.setGraphicsEffect(None)
            self.trash.reveal.enabled = not self._select_mode
            self.trash.reveal.refresh(animate=False)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._selected:
            p.setPen(Qt.NoPen)
            p.setBrush(theme.qcolor("blue-sub"))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0, 1, 0, 0), 10, 10)
        if self.underMouse() or self.isDown():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(15, 23, 42, 10 if not self.isDown() else 16))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0, 1, 0, 0), 10, 10)
        if not self._first:
            p.setPen(Qt.NoPen)
            p.setBrush(theme.qcolor("line"))
            p.drawRect(QRectF(0, 0, self.width(), 1))
        if self.hasFocus() and self.property("focusVisible"):
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(theme.qcolor("blue-l"), 2))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 2, -1, -1), 10, 10)
        p.end()


class _Square(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(14, 14)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(theme.qcolor("line2"), 1.5))
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(QRectF(0.75, 0.75, 12.5, 12.5), 4, 4)
        p.end()


class ExampleCard(Card):
    """No meetings yet: one sample marked 'Example' (what the notes look like)."""

    def __init__(self, parent=None):
        super().__init__(parent, pad=(22, 18), gap=14)
        self.setAccessibleName("Example meeting notes")
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(Faces(["Anna", "David", "Mariam"], 24, parent=self), 0, Qt.AlignVCenter)
        top.addWidget(label("Q4 roadmap", "h3", self), 0, Qt.AlignVCenter)
        top.addWidget(label("42 min", "muted", self), 0, Qt.AlignVCenter)
        top.addWidget(Chip("Example", "amber", parent=self), 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.add(top)
        self.add(Divider(self))
        summary = QVBoxLayout()
        summary.setSpacing(6)
        summary.addWidget(self._lab("Summary"))
        summary.addWidget(tlabel("Live Assistant launches on 14 October. Features freeze this week.", 13.5, 400,
                                 "text", parent=self, wrap=True))
        self.add(summary)
        acts = QVBoxLayout()
        acts.setSpacing(8)
        acts.addWidget(self._lab("Action items"))
        for who, text in (("David", "Fix speaker-label drift"), ("Anna", "Send the onboarding copy"),
                          ("Mariam", "Record the launch demo")):
            r = QHBoxLayout()
            r.setSpacing(8)
            r.addWidget(_Square(self), 0, Qt.AlignVCenter)
            r.addWidget(Faces([who], 20, parent=self), 0, Qt.AlignVCenter)
            t = QLabel(self)
            t.setTextFormat(Qt.RichText)
            t.setFont(theme.font(13))
            t.setText("<b style='color:%s'>%s</b>&nbsp; <span style='color:%s'>%s</span>"
                      % (theme.qss_color("ink"), who, theme.qss_color("text"), text))
            r.addWidget(t, 1, Qt.AlignVCenter)
            acts.addLayout(r)
        self.add(acts)

    def _lab(self, text):
        return tlabel(text.upper(), 11, 800, "muted", parent=self, letter_spacing=1.1)


class MeetingsList(QWidget):
    """'Your meetings': search + rows; the Example card while there are none."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self._bg = BgRunner(self)
        self._all = []
        self._shown = []
        self._search_token = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(label("Your meetings", "h2", self), 0, Qt.AlignVCenter)
        head.addStretch(1)
        self.select_button = Button("Select", "ghost", "s", parent=self)
        self.select_button.setAccessibleName("Select meetings to delete")
        self.select_button.setToolTip("Select meetings to delete")
        self.select_button.clicked.connect(lambda _=False: self.enter_select())
        head.addWidget(self.select_button, 0, Qt.AlignVCenter)
        self.search = QLineEdit(self)
        self.search.setProperty("gusan", "search")
        self.search.setPlaceholderText("Search meetings")
        self.search.setAccessibleName("Search meetings")
        self.search.setFont(theme.font(13.5))
        self.search.setFixedSize(220, 34)
        search_act = self.search.addAction(QIcon(icons.pixmap("search", 16, "muted")), QLineEdit.LeadingPosition)
        search_act.setText("Search")
        self.search.setClearButtonEnabled(True)
        for b in self.search.findChildren(QToolButton):      # the line edit's own icon buttons
            b.setAccessibleName("Search" if b.defaultAction() is search_act else "Clear search")
            b.setFocusPolicy(Qt.NoFocus)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        head.addWidget(self.search, 0, Qt.AlignVCenter)
        lay.addLayout(head)
        # Delete from the list: a trash icon on each row, or Select and tick several (see
        # ui/pages/meeting_delete.py for what happens, and meeting_trash.py for Undo).
        self.select_mode = False
        self.selected = set()               # folders ticked while selecting
        self._anchor = None                 # the folder a Shift-click range starts from
        self._focus_left = None             # the folder whose row had the keyboard when its delete began
        self.select_bar = row_actions.SelectionBar(self)
        self.select_bar.all_clicked.connect(self.select_all)
        self.select_bar.delete_clicked.connect(self.delete_selected)
        self.select_bar.cancel_clicked.connect(self.leave_select)
        lay.addWidget(self.select_bar)
        self.deleter = meeting_delete.MeetingDeleter(page.app, self, release=self._release_folder, dock=page)
        self.deleter.removed.connect(self._on_removed)
        self.deleter.busy.connect(self._on_busy)
        self.deleter.restored.connect(self._on_restored)      # (its "Deleted 3 meetings - Undo" toast floats over the page)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._run_search)
        self.rows_box = QWidget(self)
        self._rows = QVBoxLayout(self.rows_box)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(0)
        lay.addWidget(self.rows_box)
        # Ctrl+A on a row selects every meeting shown; Esc leaves selecting (only while it is on).
        self._all_shortcut = QShortcut(QKeySequence.SelectAll, self.rows_box)
        self._all_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._all_shortcut.activated.connect(self.select_all)
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._esc_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._esc_shortcut.setEnabled(False)
        self._esc_shortcut.activated.connect(self.leave_select)
        self.no_match = label("No meetings match.", "muted", self)
        self.no_match.setContentsMargins(14, 8, 0, 8)
        self.no_match.hide()
        lay.addWidget(self.no_match)
        self.example = ExampleCard(self)
        lay.addWidget(self.example)
        lay.setSpacing(8)
        self.rows = []
        self.set_items([])

    def set_items(self, items):
        # (a read that began before a delete and arrived after it must not bring the meeting back)
        self._all = [m for m in (items or []) if not self.deleter.is_gone(m.get("dir"))]
        has = bool(self._all)
        self.search.setVisible(has)
        self.select_button.setVisible(has and not self.select_mode)
        self.example.setVisible(not has)
        if not has:
            self.leave_select()
            self._search_token += 1
            self._render([])
            self.no_match.hide()
            return
        self.selected &= {m.get("dir") for m in self._all}          # a meeting that went is not ticked
        self._run_search()

    def _run_search(self):
        q = self.search.text().strip()
        items = list(self._all)
        self._search_token += 1             # a slower, older search never replaces a newer list
        token = self._search_token
        if not q:
            self._render(items)
            return

        def work():
            import meeting_store
            return meeting_store.search(q, meetings=items)

        def done(res, err):
            if token == self._search_token:
                self._render(res if err is None else [], searching=True)
        self._bg.run(work, done)

    # A library of 300 meetings used to build all 300 rows in one go on the GUI
    # thread (about 12 ms each: the window froze for seconds at every refresh -
    # after each meeting, a rename, a delete). The first rows come at once; the
    # rest follow in slices of at most SLICE_S, so the window keeps answering.
    FIRST_ROWS = 12
    SLICE_S = 0.03

    def _render(self, items, searching=False):
        focused = next((r.meeting.get("dir") for r in self.rows if r.hasFocus()), None)
        timer = getattr(self, "_row_timer", None)
        if timer is None:
            timer = self._row_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(0)
            timer.timeout.connect(self._continue_rows)
        timer.stop()                         # a newer list replaces one still being built
        self._row_state = None
        views.clear_layout(self._rows)
        self.rows = []
        self._shown = list(items)            # what the list shows now (Ctrl+A / a Shift range read it)
        s = self.page.session()
        active = None
        phase = "idle"
        if s is not None:
            try:
                active = s.active_dir()
            except Exception:
                active = None
            phase = getattr(s, "phase", "idle")
        try:
            import meeting_store
        except Exception:
            meeting_store = None
        self._row_state = {"it": iter(enumerate(items)), "active": active, "phase": phase,
                           "store": meeting_store, "focused": focused}
        self._build_rows(self.FIRST_ROWS)
        self.no_match.setVisible(bool(searching and not items and self._all))
        self._sync_select_ui()                   # "3 selected" counts what is on the list now

    def _continue_rows(self):
        self._build_rows(1)

    def _build_rows(self, at_least):
        """Build rows until the list is done, or - after ``at_least`` rows - this
        slice has used SLICE_S; the timer brings the next slice."""
        st = self._row_state
        if not st:
            return
        t0 = time.perf_counter()
        built = 0
        while True:
            if built >= at_least and time.perf_counter() - t0 >= self.SLICE_S:
                self._row_timer.start()
                return
            nxt = next(st["it"], None)
            if nxt is None:
                self._row_state = None
                return
            i, m = nxt
            if self.deleter.is_gone(m.get("dir")):
                continue                                # deleted while the list was still being built
            store = st["store"]
            status = store.meeting_status(m, st["active"], st["phase"]) if store else "notes_ready"
            row = MeetingRow(self.page, m, status, first=(not self.rows), parent=self.rows_box, owner=self)
            self._rows.addWidget(row)
            self.rows.append(row)
            self._prepare_row(row)
            if st["focused"] and m.get("dir") == st["focused"]:
                row.setFocus(Qt.TabFocusReason)     # a re-render never drops keyboard focus
            built += 1

    # ── delete, and select to delete ──
    def _release_folder(self, folder):
        nv = getattr(self.page, "notes_view", None)         # (built after the list)
        if nv is not None:
            nv.release_folder(folder)

    def _prepare_row(self, row):
        """A new row picks up what the list is doing: selecting, a delete on its way, a meeting that
        can't go right now."""
        folder = row.meeting.get("dir")
        row.set_blocked(self.deleter.blocked(row.meeting))
        if self.select_mode:
            row.set_select_mode(True)
            row.set_checked(folder in self.selected)
        if self.deleter.is_deleting(folder):
            row.set_deleting(True)

    def row_for(self, folder):
        return next((r for r in self.rows if r.meeting.get("dir") == folder), None)

    def sync_blocked(self):
        """A recording began or ended, or a names pass did: which meetings can go has changed."""
        for r in self.rows:
            r.set_blocked(self.deleter.blocked(r.meeting))
        self.selected = {f for f in self.selected if not self.deleter.blocked(self._meeting_of(f) or {})}
        self._sync_select_ui()

    def _meeting_of(self, folder):
        return next((m for m in self._shown if m.get("dir") == folder), None)

    def ask_delete(self, meetings, source="row"):
        """Ask once, then delete these meetings (a themed card; nothing opens). True when it started."""
        started = self.deleter.ask(meetings, source)
        if started and self.select_mode:
            self.leave_select()
        return started

    def key_delete(self, row):
        """The Delete key on a row: the ticked meetings while selecting, else that row's."""
        picked = self.selected_items()
        if self.select_mode and picked:
            self.ask_delete(picked, "selection")
        else:
            self.ask_delete([row.meeting], "key")

    def delete_selected(self):
        picked = self.selected_items()
        if picked:
            self.ask_delete(picked, "selection")

    def selected_items(self):
        return [m for m in self._shown if m.get("dir") in self.selected]

    def enter_select(self):
        if self.select_mode or not self._all:
            return
        self.select_mode = True
        self.selected = set()
        self._anchor = None
        self.select_button.hide()
        self.select_bar.show()
        self._esc_shortcut.setEnabled(True)
        for r in self.rows:
            r.set_select_mode(True)
        self._sync_select_ui()

    def leave_select(self):
        if not self.select_mode:
            return
        self.select_mode = False
        self.selected = set()
        self._anchor = None
        self.select_bar.hide()
        self.select_button.setVisible(bool(self._all))
        self._esc_shortcut.setEnabled(False)
        for r in self.rows:
            r.set_select_mode(False)

    def toggle_row(self, row, shift=False):
        """A click on a row while selecting: tick it (Shift: the whole range from the last one)."""
        folder = row.meeting.get("dir")
        if row.blocked() or row.is_deleting():
            return
        order = [m.get("dir") for m in self._shown]
        if shift and self._anchor in order and folder in order:
            a, b = sorted((order.index(self._anchor), order.index(folder)))
            for f in order[a:b + 1]:
                m = self._meeting_of(f)
                if m is not None and not self.deleter.blocked(m) and not self.deleter.is_deleting(f):
                    self.selected.add(f)
        else:
            if folder in self.selected:
                self.selected.discard(folder)
            else:
                self.selected.add(folder)
            self._anchor = folder
        self._apply_ticks()

    def select_all(self):
        """Ctrl+A / "Select all": every meeting shown that can go."""
        if not self.select_mode:
            self.enter_select()
        if not self.select_mode:
            return
        self.selected = {m.get("dir") for m in self._shown
                         if not self.deleter.blocked(m) and not self.deleter.is_deleting(m.get("dir"))}
        self._anchor = None
        self._apply_ticks()

    def _apply_ticks(self):
        for r in self.rows:
            r.set_checked(r.meeting.get("dir") in self.selected)
        self._sync_select_ui()

    def _sync_select_ui(self):
        # the count is what Delete would delete: the ticked meetings that are on the list now (a search
        # can hide some of them; they stay ticked for when they come back)
        self.select_bar.set_count(len(self.selected_items()))

    def _on_busy(self, folder, on):
        row = self.row_for(folder)
        if row is not None:
            if on and row.hasFocus():
                self._focus_left = folder          # (a row that dims is disabled, and Qt then moves the keyboard on)
            row.set_deleting(on)
            if not on and self._focus_left == folder:
                self._focus_left = None            # it could not go: the keyboard comes back to it
                row.setFocus(Qt.TabFocusReason)
        if on:
            self.selected.discard(folder)
            self._sync_select_ui()

    def _on_removed(self, folder):
        """A folder is in the trash: its row leaves, one at a time, with no rebuild of the list."""
        self._all = [m for m in self._all if m.get("dir") != folder]
        self._shown = [m for m in self._shown if m.get("dir") != folder]
        try:
            self.page.list_view.forget(folder)
            self.page.notes_view.forget_folder(folder)
        except Exception:
            logger.debug("could not forget a deleted meeting", exc_info=True)
        self.selected.discard(folder)
        row = self.row_for(folder)
        heir = None
        had_focus = self._focus_left == folder
        if had_focus:
            self._focus_left = None
        if row is not None:
            if (had_focus or row.hasFocus()) and len(self.rows) > 1:   # the keyboard was on it: a neighbour gets it
                i = self.rows.index(row)                               # (else Tab would start from the top of the window)
                order = self.rows[i + 1:] + self.rows[:i][::-1]        # the next row, else the one before
                heir = next((r for r in order if not r.is_deleting()), None)   # (not one that is on its way out too)
            self.rows.remove(row)
            self._collapse(row)
        for i, r in enumerate(self.rows):
            r.set_first(i == 0)
        if heir is not None:
            heir.setFocus(Qt.TabFocusReason)
        if not self._all:
            self.set_items([])                                    # the Example card is back
        else:
            self.no_match.setVisible(bool(not self._shown and self.search.text().strip()))
        self._sync_select_ui()

    def _collapse(self, row):
        """The row folds away (150 ms) and goes; its neighbours slide up."""
        if not row_actions.ANIMATE or not row.isVisible():
            row.hide()
            row.deleteLater()
            return
        row.setMinimumHeight(0)
        anim = QPropertyAnimation(row, b"maximumHeight", row)
        anim.setDuration(row_actions.FADE_MS)
        anim.setStartValue(row.height())
        anim.setEndValue(0)
        anim.finished.connect(row.deleteLater)
        anim.start()

    def _on_restored(self, _n=0):
        self.page.list_changed()                                  # Undo: read the library again


# ── the rail ─────────────────────────────────────────────────────────────────
def _label_icon(row, widget):
    """Put an icon (the Google G, a bell) in front of a SettingRow's label."""
    def find(lay):
        for i in range(lay.count()):
            it = lay.itemAt(i)
            if it.widget() is row.label:
                return lay
            if it.layout() is not None:
                hit = find(it.layout())
                if hit is not None:
                    return hit
        return None
    lay = find(row.layout())
    if lay is not None:
        widget.setParent(row)
        lay.insertWidget(0, widget, 0, Qt.AlignVCenter)


class Radio(QRadioButton):
    """A 16 px radio drawn like the mockup's (blue disc, white centre when on)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)

    def sizeHint(self):
        return QSize(20, 20)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _e):
        # The mockup's native radio (accent blue): a ring with a dot when on,
        # a plain grey ring when off.
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(2, 2, 16, 16)
        p.setBrush(QColor("#ffffff"))
        if self.isChecked():
            p.setPen(QPen(theme.qcolor("blue"), 1.6))
            p.drawEllipse(r.adjusted(0.8, 0.8, -0.8, -0.8))
            p.setPen(Qt.NoPen)
            p.setBrush(theme.qcolor("blue"))
            p.drawEllipse(QRectF(6, 6, 8, 8))
        else:
            p.setPen(QPen(theme.qcolor("blue-l" if self.underMouse() else "muted"), 1.1))
            p.drawEllipse(r.adjusted(0.6, 0.6, -0.6, -0.6))
        if self.hasFocus() and self.property("focusVisible"):
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(theme.qcolor("blue-l"), 2))
            p.drawEllipse(QRectF(1, 1, 18, 18))
        p.end()


class NotesOption(QFrame):
    """One 'Notes written by' choice: the plain name with the real model under it."""

    def __init__(self, text, model, icon, trail=None, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "option")
        self.setFixedHeight(50)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 12, 0)
        lay.setSpacing(8)
        self.radio = Radio(self)
        lay.addWidget(self.radio, 0, Qt.AlignVCenter)
        lay.addWidget(icon_label(icon, 16, "muted", parent=self), 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addStretch(1)               # name and model stay together (18 + 16 px lines)
        self.name_label = tlabel(text, 13.5, 600, "text", parent=self)
        col.addWidget(self.name_label)
        self.model_label = label(model, "model", self)
        col.addWidget(self.model_label)
        col.addStretch(1)
        lay.addLayout(col, 1)
        if trail is not None:
            trail.setParent(self)
            lay.addWidget(trail, 0, Qt.AlignVCenter)
        self.set_model(model)
        self._text = text

    def set_model(self, model):
        self.model_label.setText(model)
        self.radio.setAccessibleName("%s · %s" % (self.name_label.text(), model) if model else self.name_label.text())

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.radio.click()          # also when already on: retries a failed model download
        super().mousePressEvent(e)


class MeetingRail(SettingsRail):
    """'Meeting settings': Google Calendar, Send notes to Notion (right below it),
    Meeting language, Notes written by, Remind me to tell people; the speech
    model this runs on in the foot."""

    def __init__(self, page, parent=None):
        super().__init__("Meeting settings", icon="gear", parent=parent)
        self.page = page
        # Google Calendar
        self.cal_control = QWidget()
        cl = QHBoxLayout(self.cal_control)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        self.cal_connect = Button("Connect", "secondary", "s", parent=self.cal_control)
        self.cal_connect.setAccessibleName("Connect Google Calendar")
        self.cal_connect.clicked.connect(self._cal_connect)
        cl.addWidget(self.cal_connect)
        self.cal_disconnect = Button("Disconnect", "link", "s", parent=self.cal_control)
        self.cal_disconnect.setProperty("tone", "muted")
        self.cal_disconnect.setFont(theme.font(12.5, 600))
        self.cal_disconnect.setAccessibleName("Disconnect Google Calendar")
        self.cal_disconnect.clicked.connect(self._cal_disconnect)
        cl.addWidget(self.cal_disconnect)
        self.cal_cancel = Button("Cancel", "link", "s", parent=self.cal_control)
        self.cal_cancel.setProperty("tone", "muted")
        self.cal_cancel.setFont(theme.font(12.5, 600))
        self.cal_cancel.setAccessibleName("Cancel connecting Google Calendar")
        self.cal_cancel.clicked.connect(self._cal_cancel)
        cl.addWidget(self.cal_cancel)
        self.cal_row = self.add_row(SettingRow("Google Calendar", self.cal_control))
        _label_icon(self.cal_row, google_label(16))
        self.ask_toggle = Toggle("Ask me to record before each meeting", True)
        self.ask_toggle.toggled.connect(lambda on: self.page.set_setting(ASK_KEY, bool(on)))
        self.ask_row = self.add_row(SettingRow("Ask me to record", self.ask_toggle, hint="1 min before"))
        _label_icon(self.ask_row, icon_label("bell_on", 16, "muted"))
        self.link_toggle = Toggle("Only meetings with other people", True)
        self.link_toggle.toggled.connect(lambda on: self.page.set_setting(LINK_ONLY_KEY, bool(on)))
        self.link_row = self.add_row(SettingRow("Only meetings with other people", self.link_toggle))
        # Notion. The actions stack (Manage over Disconnect, Reconnect over Disconnect):
        # side by side they would not fit beside the label in this narrow rail.
        self.notion_control = QWidget()
        nl = QVBoxLayout(self.notion_control)
        nl.setContentsMargins(0, 0, 0, 0)
        nl.setSpacing(2)
        self.notion_connect = Button("Connect", "secondary", "s", parent=self.notion_control)
        self.notion_connect.setAccessibleName("Connect Notion")
        self.notion_connect.clicked.connect(self._notion_connect)
        self.notion_reconnect = Button("Reconnect", "secondary", "s", parent=self.notion_control)
        self.notion_reconnect.setAccessibleName("Reconnect Notion")
        self.notion_reconnect.clicked.connect(self._notion_connect)
        self.notion_manage = Button("Manage", "link", "s", parent=self.notion_control)
        self.notion_manage.setAccessibleName("Manage Notion")
        self.notion_manage.clicked.connect(self._notion_manage)
        self.notion_disconnect = Button("Disconnect", "link", "s", parent=self.notion_control)
        self.notion_disconnect.setProperty("tone", "muted")
        self.notion_disconnect.setAccessibleName("Disconnect Notion")
        self.notion_disconnect.clicked.connect(self._notion_disconnect)
        for link in (self.notion_manage, self.notion_disconnect):
            link.setFont(theme.font(12.5, 600))
            link.setFixedHeight(22)
        for w in (self.notion_connect, self.notion_reconnect, self.notion_manage, self.notion_disconnect):
            nl.addWidget(w, 0, Qt.AlignRight)
        self.notion_row = self.add_row(SettingRow("Send notes to Notion", self.notion_control))
        self.notion_row.hint_label.setTextFormat(Qt.PlainText)    # the workspace name is Notion's text, not markup
        _label_icon(self.notion_row, notion_sheet.mark_label(16))
        self.notion_auto_toggle = Toggle(notion_sheet.AUTO_NAME, False)
        self.notion_auto_toggle.toggled.connect(lambda on: self.page.set_setting("notion_auto_send", bool(on)))
        self.notion_auto_row = self.add_row(SettingRow("Send automatically", self.notion_auto_toggle,
                                                       hint=notion_sheet.RAIL_AUTO_HINT))
        # Language (no "Record from" here: every meeting starts on Both sides of
        # the call; the start card picks another for one meeting)
        self.lang_combo = LangCombo(accessible="Meeting language")
        self.lang_combo.setFixedWidth(278)
        self.lang_combo.activated.connect(self._on_lang)
        self.add_row(SettingRow("Meeting language", self.lang_combo, stacked=True))
        # Notes written by
        opts = QWidget()
        ol = QVBoxLayout(opts)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(6)
        self.opt_cloud = NotesOption("Gusan Cloud", _cloud_writer_model(), "cloud", ProTag("s"), opts)
        self.opt_local = NotesOption("This computer", "Qwen 1.5B", "laptop", None, opts)
        ol.addWidget(self.opt_cloud)
        ol.addWidget(self.opt_local)
        opts.setAccessibleName("Notes written by")
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.addButton(self.opt_cloud.radio, 0)
        self._group.addButton(self.opt_local.radio, 1)
        self.opt_cloud.radio.clicked.connect(self._pick_cloud)
        self.opt_local.radio.clicked.connect(self._pick_local)
        self.add_row(SettingRow("Notes written by", opts, stacked=True))
        # Consent
        self.consent_toggle = Toggle("Remind me to tell people", True)
        self.consent_toggle.toggled.connect(lambda on: self.page.set_setting(CONSENT_KEY, bool(on)))
        self.add_row(SettingRow("Remind me to tell people", self.consent_toggle))
        # Foot: what transcribes meetings
        foot = QWidget()
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(8)
        fc = QVBoxLayout()
        fc.setSpacing(0)
        self.runs_label = label("", "muted", foot)
        fc.addWidget(self.runs_label)
        self.runs_model = label("", "model", foot)
        fc.addWidget(self.runs_model)
        fl.addLayout(fc, 1)
        self.change_btn = Button("Change", "link", "s", parent=foot)
        self.change_btn.setFont(theme.font(12.5, 600))
        self.change_btn.setAccessibleName("Change the speech model")
        self.change_btn.clicked.connect(lambda: self.page.open_route("settings", section="speech"))
        fl.addWidget(self.change_btn, 0, Qt.AlignVCenter)
        self.set_foot(foot)
        self._cal_error = ""
        self.sync_settings()
        self.sync_calendar()

    # ── sync ──
    def sync_settings(self):
        cfg = self.page.cfg()
        self.lang_combo.set_code(_saved_lang(cfg))
        for tog, key in ((self.ask_toggle, ASK_KEY), (self.link_toggle, LINK_ONLY_KEY),
                         (self.consent_toggle, CONSENT_KEY)):
            tog.blockSignals(True)
            tog.setChecked(bool(cfg.get(key, True)))
            tog.blockSignals(False)
            tog.update()
        self.sync_notes_by()
        self.sync_notion(cfg)
        info = views.runs_on(self.page.app, "dictation")
        if info:
            kind = info.get("kind")
            if kind == "cloud":
                self.runs_label.setText("Runs on Gusan Cloud")
                self.runs_model.setText(info.get("model_name") or "")
            elif kind == "own_key":
                self.runs_label.setText("Runs with your own key")
                self.runs_model.setText(" · ".join(x for x in (info.get("plain"), info.get("model_name")) if x))
            else:
                self.runs_label.setText("Runs on %s · this computer" % (info.get("plain") or "Balanced"))
                self.runs_model.setText(info.get("model_name") or "")
        else:
            self.runs_label.setText("Runs on this computer")
            self.runs_model.setText("")

    def sync_notion(self, cfg=None):
        """The Notion rows: Connect | Manage + Disconnect | Reconnect + Disconnect,
        the workspace as the hint, and the automatic-send switch once connected."""
        cfg = self.page.cfg() if cfg is None else cfg
        notion = notion_export.is_connected(cfg)
        again = notion and bool(cfg.get("notion_reconnect"))
        auto = bool(cfg.get("notion_auto_send"))
        self.notion_connect.setVisible(not notion)
        self.notion_manage.setVisible(notion and not again)
        self.notion_reconnect.setVisible(again)
        self.notion_disconnect.setVisible(notion)
        if again:
            hint = notion_sheet.RAIL_RECONNECT_HINT
        elif notion:
            hint = (cfg.get("notion_workspace_name") or "").strip() or "Connected"
        else:
            hint = ""
        self.notion_row.set_hint(hint)
        # The one hint that asks for something reads in the darker body colour; the
        # workspace name is passive and keeps the shared muted one.
        role = "body" if again else "hint"
        if self.notion_row.hint_label.property("textRole") != role:
            theme.set_role(self.notion_row.hint_label, role)
        self.notion_auto_row.setVisible(notion)
        self.notion_auto_row.set_hint(notion_sheet.RAIL_PAUSED_HINT if auto and cfg.get("privacy_mode")
                                      else notion_sheet.RAIL_AUTO_HINT)
        self.notion_auto_toggle.blockSignals(True)
        self.notion_auto_toggle.setChecked(auto)
        self.notion_auto_toggle.blockSignals(False)
        self.notion_auto_toggle.update()

    def notes_choice(self):
        """("cloud" | "local", local model id) - what the radios show."""
        cfg = self.page.cfg()
        writers = _local_writers()
        val = cfg.get(NOTES_KEY) or ""
        local = val if val in writers else None
        if local is None:
            act = cfg.get("action_model") or ""
            local = act if act in writers else LOCAL_DEFAULT
        auth = getattr(self.page.app, "auth", None)
        signed = bool(auth is not None and getattr(auth, "is_authenticated", False))
        if not self.page.is_pro() and not signed:
            return "local", local
        if val == MANAGED:
            return "cloud", local
        if val in writers:
            return "local", val
        if self.page.is_pro():
            return "cloud", local           # Pro default: Gusan Cloud (meeting_session.default_engine_id)
        return "local", local

    def sync_notes_by(self):
        choice, local = self.notes_choice()
        writers = _local_writers()
        model = writers.get(local, ("", local))[1]
        dl = getattr(self.page.app, "downloads", None)
        if dl is not None and choice == "local":
            try:
                st = dl.state(local)
                if st == "downloading":
                    prog = dl.progress_of(local)
                    model += " · downloading %d%%" % (prog[0] if prog else 0)
                elif st == "failed":
                    model += " · download failed"
            except Exception:
                pass
        self.opt_local.set_model(model)
        self.opt_cloud.set_model(_cloud_writer_model())
        for b in (self.opt_cloud.radio, self.opt_local.radio):
            b.blockSignals(True)
        (self.opt_cloud.radio if choice == "cloud" else self.opt_local.radio).setChecked(True)
        for b in (self.opt_cloud.radio, self.opt_local.radio):
            b.blockSignals(False)

    def sync_calendar(self):
        cal = self.page.calendar()
        visible = cal is not None
        connected = bool(visible and getattr(cal, "is_connected", False))
        connecting = bool(visible and getattr(cal, "is_connecting", False))
        self.cal_row.setVisible(visible)
        self.ask_row.setVisible(connected)
        self.link_row.setVisible(connected)
        self.cal_connect.setVisible(visible and not connected and not connecting)
        self.cal_cancel.setVisible(connecting)
        self.cal_disconnect.setVisible(connected)
        if connecting or connected:
            # An old error must not come back under "Google Calendar" after a later
            # disconnect or expiry (a read error while connected is the list's line).
            self._cal_error = ""
        if connecting:
            self.cal_row.set_hint("Waiting for Google…")
        elif connected:
            # The account, and - while Privacy mode holds the reads back - why nothing
            # new arrives (what is shown is the list from before).
            hint = getattr(cal, "email", "") or ""
            status = _service_status(cal)
            if status and status == _paused_text():
                hint = (hint + "\n" + status) if hint else status
            self.cal_row.set_hint(hint)
        else:
            self.cal_row.set_hint(self._cal_error)

    def set_calendar_error(self, text):
        self._cal_error = text or ""
        self.sync_calendar()

    # ── actions ──
    def _on_lang(self, _i):
        code = self.lang_combo.currentData()
        if code:
            self.page.set_setting("meeting_language", code)

    def _pick_cloud(self):
        auth = getattr(self.page.app, "auth", None)
        signed = bool(auth is not None and getattr(auth, "is_authenticated", False))
        if not self.page.is_pro() and not signed:
            self.page.call("_pro_upsell", UPSELL_RECORD)
            self.sync_notes_by()
            return
        if not self.page.set_setting(NOTES_KEY, MANAGED):
            self.sync_notes_by()

    def _pick_local(self):
        _choice, local = self.notes_choice()
        cfg_val = self.page.cfg().get(NOTES_KEY) or ""
        writers = _local_writers()
        local = cfg_val if cfg_val in writers else local
        if not self.page.set_setting(NOTES_KEY, local):
            self.sync_notes_by()
            return
        dl = getattr(self.page.app, "downloads", None)
        if dl is not None:
            try:
                if dl.state(local) != "ready":
                    dl.start(local)
            except Exception:
                logger.debug("download start failed", exc_info=True)
        self.sync_notes_by()

    def _cal_connect(self):
        self._cal_error = ""
        self.page.connect_calendar()            # the disclosure first, then the browser
        self.sync_calendar()

    def _cal_cancel(self):
        cal = self.page.calendar()
        if cal is not None:
            try:
                cal.cancel_connect()
            except Exception:
                pass
        self.sync_calendar()

    def _cal_disconnect(self):
        cal = self.page.calendar()
        if cal is not None:
            try:
                cal.disconnect_calendar()
            except Exception:
                logger.warning("disconnect_calendar failed", exc_info=True)
        self.page.list_view.sync_calendar()

    def _notion_connect(self):
        self.page.connect_notion()
        self.sync_settings()

    def _notion_manage(self):
        self.page.connect_notion()          # connected: the sheet opens on "Connected"
        self.sync_settings()

    def _notion_disconnect(self):
        notion_sheet.disconnect_notion(self.page)
        self.sync_settings()
