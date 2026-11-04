# -*- coding: utf-8 -*-
"""Settings (Gusan 2.0): one scrolling page - General, Microphone, Speech & AI,
Dictionary, Privacy, Updates & about - with a section list on the left that
follows the scroll. Every change applies at once (app.set_setting / set_settings); there is
no Save button.

    win.open("settings")                                  # General
    win.open("settings", section="speech")                # general | microphone | speech | dictionary | privacy | updates
    win.open("settings", section="speech", focus="gpu")   # focus: gpu | keys | models | writer

Feature options live on their own pages; the shortcut rows and the footer link
there. The Dictionary is NOT a page: it is the "Dictionary" section here (the
words, replacements and removals of ui/pages/dictionary.py, embedded; the old
"dictionary" route opens this section). Nothing here touches the network or scans the disk on the GUI thread:
key tests, update checks, history counts, the hardware probe and the microphone
all run on workers (api_keys.run_then) and come back only while the page exists.

Integration notes for Wave 4
----------------------------
1. Config keys this page writes that main.DEFAULT doesn't have yet (plain keys,
   no side effect in settings_apply unless noted):
     app_theme               "system" | "light" | "dark" (default "system").
                             ui/theme.py only has the light tokens, so 2.0.0
                             hides the App look row (SHOW_APP_LOOK = False);
                             the key stays. Flip it once a dark palette exists.
     notify_when_ready       file_jobs.FILE_DEFAULTS (default True); meeting
                             notes should honour it too.
     install_updates_on_quit default True: download the update ahead
                             (updater.download) and updater.launch() it from
                             quit_app.
     input_device_index      already in DEFAULT: an int PyAudio index (default
                             host API) or None for the default microphone.
   Read only: hotkey, smart_actions_hotkey (the Smart Actions page's key,
   default "alt+s" ("ctrl+s" on a Mac), "" = none set; SMART_SHORTCUT_KEYS) and
   live_assist_hotkey, listed in that order (Dictate, Smart Actions, Live
   Assistant). A shortcut row opens its page with focus="hotkey": Dictation
   and Smart Actions put the keyboard focus on their shortcut control; Live
   Assistant ignores it today (Wave 4: accept focus="hotkey" there too).
   Removing a Whisper model that Smart Actions transcribe with
   (smart_actions_speech_model) puts that key back to "" (Same as Dictation).
2. Services (getattr; the page degrades without them; connected at build and
   again on every open, once per object, so a service made after the window
   is still heard):
     app.downloads   model_manager.ModelDownloads - cards, More sizes, the AI
                     writer sizes, "Best for your computer" (recommendation /
                     hardware, probe_hardware()). Without it the current model
                     shows "In use" and Download is disabled; the hardware chips
                     come from hardware.snapshot(probe_gpu=False) on a worker.
     app.mic_monitor optional: devices() -> [(index, name)], start(index),
                     stop(), level (0..1, attr or method), error. Owner 7's
                     mic_monitor can be exposed there. Otherwise the page opens
                     its own level-only PyAudio stream on app.recorder.audio,
                     only while the Microphone card is on screen; nothing at all
                     without app.recorder.
   GPU card (only when the hardware snapshot's gpu_state is available / ready /
   failed, i.e. an NVIDIA card on Windows): app.start_gpu_download("settings"),
   app.cancel_gpu_download(), app.retry_gpu(), app.gpu_phase(),
   app.sig_gpu_progress(int, float, float), app.sig_gpu_done(str, str); after
   sig_gpu_done the page asks app.downloads.probe_hardware(force=True).
   main.show_gpu_settings() opens show_main("settings", section="speech",
   focus="gpu"). "Remove" (ready, Gusan's own download only: app.gpu_removable())
   calls app.remove_gpu_download(); the card follows the re-probe.
3. Updates: "Install update" calls app.install_update(tag, on_done=...) when the
   controller has it: it opens the update window (download progress, Cancel,
   "Installing", Try again; ui/update_window.py), installs quietly and quits;
   it calls on_done(ok, info) only when it did NOT install ("" = a Cancel or the
   browser took over), so the row can say why. Without it the page runs
   updater.install itself and emits app.sig_quit. unsaved_edits() lets the
   controller ask before an install closes Gusan over a typed API key.
   After "Check now" the page calls app.note_update_check(status, tag) if
   present, so the controller can store pending_update_version (a PROTECTED
   key pages can't write). The status shown before any check comes from cfg
   pending_update_version.
4. Delete all history: history_actions.counts / clear plus
   transcript_store.delete_all_transcripts() for "Delete everything"; then
   app.sig_history_changed.emit() if the controller has that signal (add it:
   Home, Meetings and Audio to text should refresh on it).
5. API keys: api_keys.save_key / clear_key / key_view / test_key. After an
   account switch (_reconcile_user_secrets) _on_auth_changed calls
   main_win.refresh_account(), so the rows re-read the keys.
   Key tests are remembered for the run only (never written to config).
6. Pro: the speech and AI-writer radios show what really runs
   (engine_choice.runs_on). The mockup shows Gusan Cloud preselected for Pro;
   if that is wanted, set backend="managed" when someone becomes Pro (Wave 4).
   Free users tapping a Gusan Cloud option get app._pro_upsell("Fast cloud
   transcription") / ("Smart Actions").
"""

import hashlib
import logging
import sys
import threading
import time

from PySide6.QtCore import QCoreApplication, QEvent, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QBoxLayout, QComboBox, QDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

import account_actions
import api_keys
import engine_choice
import hardware
import history_actions
import hotkeys
import speech_hy
import updater
from app_info import APP_NAME, APP_VERSION
from ui import hy_model, theme
from ui.components import (Avatar, Button, Card, Chip, Divider, IconTile, Keycap, NavItem,
                           PageHeader, Segmented, SettingRow, Toggle, icons, label, mini_prompt)
from ui.components.model_combo import MARK_DOWNLOAD, NOTE_ROLE, PlainNameCombo
from ui.components.controls import keycap_labels

logger = logging.getLogger("transcribe")

IS_MAC = sys.platform == "darwin"

SECTIONS = (("general", "gear", "General"), ("microphone", "mic", "Microphone"),
            ("speech", "wave", "Speech & AI"), ("dictionary", "book", "Dictionary"),
            ("privacy", "shield", "Privacy"), ("updates", "refresh", "Updates & about"))
SECTION_IDS = tuple(s for s, _i, _t in SECTIONS)
_SECTION_ALIASES = {
    "mic": "microphone", "audio": "microphone", "speech_ai": "speech", "ai": "speech", "models": "speech",
    "model": "speech", "engines": "speech", "keys": "speech", "api_keys": "speech", "gpu": "speech",
    "privacy_updates": "privacy", "history": "privacy", "about": "updates", "update": "updates",
    "account": "general", "words": "dictionary", "vocabulary": "dictionary", "replacements": "dictionary",
}
APP_LOOKS = (("system", "Match system"), ("light", "Light"), ("dark", "Dark"))
# 2.0.0 is light only (ui/theme.py has no dark palette yet): the App look row
# is hidden, and app_theme stays in the config for when it comes.
SHOW_APP_LOOK = False
SMART_SHORTCUT_KEYS = ("smart_actions_hotkey",)        # ui/pages/smart_actions.py HOTKEY_KEY
# (cfg keys, icon, name, route, link text, default hotkey)
SHORTCUTS = (
    (("hotkey",), "mic", "Dictate", "dictation", "Dictation", hotkeys.DICTATION_DEFAULT),
    (SMART_SHORTCUT_KEYS, "wand", "Smart Actions", "smart_actions", "Smart Actions", hotkeys.SMART_ACTIONS_DEFAULT),
    (("live_assist_hotkey",), "incognito", "Live Assistant", "live_assistant", "Live Assistant", "ctrl+alt+a"),
)
SMART_SPEECH_KEY = engine_choice.SMART_SPEECH_KEY      # Smart Actions' "Transcribes with"
FEATURE_PAGES = (("mic", "Dictation", "dictation"), ("wand", "Smart Actions", "smart_actions"),
                 ("people", "Meetings", "meetings"), ("incognito", "Live Assistant", "live_assistant"),
                 ("wave", "Audio to text", "audio_to_text"))
WEBSITE_URL = "https://gusan.aibuben.xyz"          # Gusan's own site
AIBUBEN_URL = "https://aibuben.xyz"               # "Part of the AIBUBEN community"
LINKEDIN_URL = "https://www.linkedin.com/in/aram-adamyan-2k/"
WHAT_WE_COLLECT = ("Anonymous: which features you use, the app version, your system, your plan and a "
                   "random install ID. Never your voice, text, keys or files.")
SPEECH_KEYS = (("mistral_api_key", "Mistral"), ("google_api_key", "Google Gemini"))
# The Gusan Cloud writer tile: Smart Actions and Live Assistant share one model.
CLOUD_WRITER_HINTS = (engine_choice.CLOUD_SMART_MODEL, "%s for meeting notes" % engine_choice.CLOUD_WRITER_MODEL)
# Speech & AI's two cards: "Runs on" (where the model runs) in each card's order.
SPEECH_SOURCES_UI = (("local", engine_choice.THIS_COMPUTER), ("cloud", engine_choice.GUSAN_CLOUD),
                     ("own_key", engine_choice.OWN_KEY))
WRITER_SOURCES_UI = SPEECH_SOURCES_UI          # the same order in both cards
SPEECH_KIND = "Speech-to-text (STT)"
SPEECH_HELP = ("The speech-to-text (STT) model turns your voice into text. "
               "Dictation and meetings use it.")
WRITER_KIND = "Language model (LLM)"
WRITER_HELP = ("The large language model (LLM) writes for you: Smart Actions, meeting notes "
               "and the Live Assistant's answers. On Gusan Cloud, the model you pick answers "
               "first; meeting notes use Gusan's own choice.")
PICK_A_MODEL = "Select a model"
PAUSED_TEXT = "Off while Privacy mode is on"

# Settings keys whose change redraws something here (anything else is ignored).
_WATCHED = {
    "launch_at_login", "app_theme", "notify_when_ready", "hotkey", "live_assist_hotkey", "input_device_index",
    "backend", "whisper_model", "managed_provider", "mistral_stt_model", "google_stt_model", "privacy_mode",
    "action_model", "action_api_provider", "action_api_model", "action_api_base_url", "action_api_key",
    "mistral_api_key", "google_api_key", "save_history", "analytics_enabled", "overlay_private",
    "live_assist_private", "install_updates_on_quit",
    "pending_update_version", "admin_tier_override",
} | set(SMART_SHORTCUT_KEYS)


# ── shared helpers (ui/pages/account.py uses them too) ──────────────────────
def cfg_of(app):
    cfg = getattr(app, "cfg", None)
    return cfg if isinstance(cfg, dict) else {}


def call(app, method, *args, **kwargs):
    """app.<method>(...) when the controller has it; None (logged) otherwise."""
    fn = getattr(app, method, None)
    if not callable(fn):
        logger.debug("main_app has no %s()", method)
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("%s() failed", method, exc_info=True)
        return None


def is_pro(app):
    try:
        return bool(app.is_pro())
    except Exception:
        return False


def is_signed_in(app):
    return bool(getattr(getattr(app, "auth", None), "is_authenticated", False))


def track(app, event, props=None):
    call(app, "track", event, props or {})


def apply_settings(app, changes):
    """Apply {key: value} through the controller's instant-apply setter.
    Returns {key: ok}. An older controller without it: write cfg + save."""
    changes = dict(changes or {})
    if not changes:
        return {}
    fn = getattr(app, "set_settings", None)
    if callable(fn):
        try:
            return dict(fn(changes) or {})
        except Exception:
            logger.warning("set_settings failed", exc_info=True)
            return {k: False for k in changes}
    one = getattr(app, "set_setting", None)
    if callable(one):
        out = {}
        for k, v in changes.items():
            try:
                out[k] = bool(one(k, v))
            except Exception:
                logger.warning("set_setting(%s) failed", k, exc_info=True)
                out[k] = False
        return out
    cfg = getattr(app, "cfg", None)
    if isinstance(cfg, dict):
        cfg.update(changes)
        if call(app, "schedule_config_save") is None:
            call(app, "save_config")
        return {k: True for k in changes}
    return {k: False for k in changes}


def alive(obj):
    try:
        from shiboken6 import isValid
        return obj is not None and isValid(obj)
    except Exception:
        return obj is not None


def run_bg(fn, *args, then=None, error=None, owner=None, **kwargs):
    """fn(*args) on a worker; then(result) / error(exc) back on the GUI thread,
    skipped once ``owner`` (a widget) is gone."""
    def guard(cb):
        if cb is None:
            return None

        def _run(value):
            if owner is None or alive(owner):
                cb(value)
        return _run
    return api_keys.run_then(fn, *args, then=guard(then), error=guard(error), **kwargs)


def open_url(url):
    try:
        QDesktopServices.openUrl(QUrl(url))
    except Exception:
        logger.warning("Could not open %s", url, exc_info=True)


def go(widget, app, route, **opts):
    """Open another page of the main window (or ask the controller to)."""
    win = widget.window() if widget is not None else None
    fn = getattr(win, "open", None)
    if callable(fn) and hasattr(win, "current_route"):
        try:
            return fn(route, **opts)
        except Exception:
            logger.warning("Opening %s failed", route, exc_info=True)
            return None
    return call(app, "show_main", route, **opts)


def text_label(text, px, weight=400, color=None, role=None, display=False, parent=None, wrap=False):
    """A QLabel at an exact size/weight; colour from a theme token."""
    lb = QLabel(text, parent)
    if role:
        lb.setProperty("textRole", role)
    lb.setFont(theme.font(px, weight, display=display))
    if color:
        tint(lb, color)
    if wrap:
        lb.setWordWrap(True)
    return lb


def tint(lb, color):
    lb.setStyleSheet("QLabel { color: %s; }" % theme.qss_color(color))
    return lb


def icon_label(name, size, color="muted", sw=1.7, parent=None):
    lb = QLabel(parent)
    lb.setPixmap(icons.pixmap(name, size, color, sw))
    lb.setFixedSize(size, size)
    lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return lb


def field_font(widget, px=13.5):
    """Inputs and drop-downs get the body face explicitly: a QComboBox otherwise
    keeps the platform's class font instead of the window's."""
    widget.setFont(theme.font(px))
    view = getattr(widget, "view", None)
    if callable(view):
        try:
            view().setFont(theme.font(px))
        except Exception:
            pass
    return widget


def trailing_icon(button):
    """Put a Button's icon after its text (the mockups' 'Dictation ›' links)."""
    button.setLayoutDirection(Qt.RightToLeft)
    return button


def clear_layout(lay):
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif it.layout() is not None:
            clear_layout(it.layout())
            it.layout().deleteLater()


def red_button(text, size="s", parent=None):
    """The quiet destructive button: secondary with red text and a red edge."""
    b = Button(text, "secondary", size, parent=parent)
    b.setObjectName("redBtn")
    b.setStyleSheet("QPushButton#redBtn { color: %s; border-color: %s; }"
                    "QPushButton#redBtn:hover { background: %s; }"
                    % (theme.qss_color("red-tx"), theme.qss_color("#fecaca"), theme.qss_color("red-sub")))
    return b


class AskDialog(mini_prompt.MiniPrompt):
    """A small v2 question: a title, one line, and buttons [(key, text, kind)]. ``typed`` asks the user
    to type a word before a danger button works. The same card as every other question in the app
    (ui/components/mini_prompt.py); a question with a red button opens on its "cancel" button, so a
    stray Enter never confirms. ``choice`` is the key pressed (None for Cancel / Esc)."""

    def __init__(self, parent, title, text, buttons, typed=None):
        danger = any(kind == "danger" for _key, _text, kind in buttons)
        keys = [key for key, _text, _kind in buttons]
        super().__init__(parent, title, text, None, None, icon="trash" if danger else "info",
                         tone="danger" if danger else "info", buttons=buttons, type_word=typed,
                         default="cancel" if danger and "cancel" in keys else None)


def ask(parent, title, text, buttons, typed=None):
    """Show AskDialog; the chosen key, or None (Cancel / Esc). Tests replace this."""
    dlg = AskDialog(parent, title, text, buttons, typed=typed)
    try:
        dlg.exec()
        return dlg.choice
    finally:
        dlg.deleteLater()


def autostart_state():
    """(supported, enabled) for "Start Gusan when Windows starts" - a registry
    read, quick enough for the GUI thread. Unsupported when run from source."""
    try:
        import autostart
        if not autostart.supported():
            return False, False
        return True, bool(autostart.is_enabled())
    except Exception:
        return False, False


# Key tests already run this session: sha256(key) -> (ok, message). Never saved.
_TESTED = {}


def _key_hash(key):
    return hashlib.sha256(str(key or "").strip().encode("utf-8")).hexdigest()


def remember_key_test(key, ok, message=""):
    _TESTED[_key_hash(key)] = (bool(ok), message or "")


def key_test_result(key):
    return _TESTED.get(_key_hash(key)) if key else None


# ── painted building blocks ─────────────────────────────────────────────────
class _Painted(QAbstractButton):
    """A button that paints its own rounded box (no native look) and lays out
    child labels; Tab focus shows a ring, Enter clicks."""
    RADIUS = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self._kbd = False

    def focusInEvent(self, e):
        self._kbd = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):
        self._kbd = False
        super().focusOutEvent(e)
        self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(e)

    def box(self):
        """(background, border, border width) - tokens or CSS colours."""
        return "rgba(255,255,255,0.72)", ("line2" if self.underMouse() else "line"), 1

    def paint_extra(self, p):
        pass

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        bg, border, width = self.box()
        half = width / 2.0
        r = QRectF(self.rect()).adjusted(half, half, -half, -half)
        if not self.isEnabled():
            p.setOpacity(0.55)
        p.setPen(QPen(theme.qcolor(border), width))
        p.setBrush(theme.qcolor(bg))
        p.drawRoundedRect(r, self.RADIUS, self.RADIUS)
        self.paint_extra(p)
        if self.hasFocus() and self._kbd:
            p.setOpacity(1.0)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(theme.qcolor("blue-l"), 2))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), self.RADIUS, self.RADIUS)
        p.end()


class _Box(QFrame):
    """A QFrame that paints a rounded box from tokens (bg, border, width, radius)."""

    def __init__(self, bg="rgba(255,255,255,0.72)", border="line", width=1, radius=12, parent=None):
        super().__init__(parent)
        self._look = (bg, border, width, radius)

    def set_look(self, bg, border, width=1, radius=None):
        self._look = (bg, border, width, self._look[3] if radius is None else radius)
        self.update()

    def paintEvent(self, _e):
        bg, border, width, radius = self._look
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        half = width / 2.0
        p.setPen(QPen(theme.qcolor(border), width) if border else Qt.NoPen)
        p.setBrush(theme.qcolor(bg) if bg else Qt.NoBrush)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(half, half, -half, -half), radius, radius)
        p.end()


class BigToggle(Toggle):
    """The Privacy mode switch (52 x 30 track, 24 px knob)."""
    TRACK_W, TRACK_H, KNOB = 52, 30, 24

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        on = self.isChecked()
        x0 = y0 = float(self.PAD)
        track = QRectF(x0 + 0.5, y0 + 0.5, self.TRACK_W - 1, self.TRACK_H - 1)
        if not self.isEnabled():
            p.setOpacity(0.5)
        fill = theme.qcolor("blue" if on else "line2")
        edge = theme.qcolor("blue-h" if on else "line2")
        if self.underMouse() and self.isEnabled():
            fill = fill.darker(108)
        p.setPen(QPen(edge, 1))
        p.setBrush(fill)
        p.drawRoundedRect(track, self.TRACK_H / 2.0 - 0.5, self.TRACK_H / 2.0 - 0.5)
        kx = x0 + 1 + (24 if on else 2)
        ky = y0 + (self.TRACK_H - self.KNOB) / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 23, 42, 40))
        p.drawEllipse(QRectF(kx, ky + 1, self.KNOB, self.KNOB))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(kx, ky, self.KNOB, self.KNOB))
        if self.hasFocus() and getattr(self, "_kbd_focus", False):
            p.setOpacity(1.0)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(theme.qcolor("blue-l"), 2))
            ring = QRectF(1, 1, self.width() - 2, self.height() - 2)
            p.drawRoundedRect(ring, ring.height() / 2.0, ring.height() / 2.0)
        p.end()


# ── section list ─────────────────────────────────────────────────────────────
class _SectionItem(NavItem):
    """A Settings section entry (36 px, 17 px icon) - the sidebar's look."""

    def __init__(self, sid, icon, text, parent=None):
        super().__init__(sid, icon, text, parent=parent)
        self.setFixedHeight(36)
        lay = self.layout()
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(10)
        self.icon_label.setFixedSize(17, 17)
        self._active = None
        self.set_active(False)

    def set_active(self, on):
        on = bool(on)
        if on == self._active:
            return
        self._active = on
        self.icon_label.setPixmap(icons.pixmap(self._icon_name, 17, "blue-tx" if on else "text2",
                                               sw=2 if on else 1.7))
        self.text_label.setFont(theme.font(13.5, 600 if on else 500))
        self.setAccessibleDescription("Current section" if on else "")
        theme.set_prop(self, "active", on)
        theme.elevate(self, "item" if on else None)


class _AccountLink(QPushButton):
    def __init__(self, name, parent=None, guest=False):
        super().__init__(parent)
        self.setProperty("gusan", "accountRow")
        self.setProperty("active", False)
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.setAccessibleName("Account & plan")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(10)
        self.avatar = Avatar(name, 22, self, guest=guest)       # a guest: the person icon, never a letter
        lay.addWidget(self.avatar, 0, Qt.AlignVCenter)
        text = text_label("Account & plan", 13.5, 500, color="text2", parent=self)
        text.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(text, 1, Qt.AlignVCenter)
        lay.addWidget(icon_label("chev_r", 14, "muted", parent=self), 0, Qt.AlignVCenter)

    def sizeHint(self):
        return QSize(196, 40)


class SectionNav(QWidget):
    picked = Signal(str)
    account = Signal()

    def __init__(self, name, parent=None, guest=False):
        super().__init__(parent)
        self.setFixedWidth(196)
        self.setAccessibleName("Settings sections")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.items = {}
        for sid, icon, text in SECTIONS:
            it = _SectionItem(sid, icon, text, self)
            it.clicked.connect(lambda _=False, s=sid: self.picked.emit(s))
            lay.addWidget(it)
            self.items[sid] = it
        lay.addSpacing(10)
        dv = QHBoxLayout()
        dv.setContentsMargins(4, 0, 4, 0)
        dv.addWidget(Divider(self))
        lay.addLayout(dv)
        lay.addSpacing(10)
        self.account_link = _AccountLink(name, self, guest=guest)
        self.account_link.clicked.connect(lambda _=False: self.account.emit())
        lay.addWidget(self.account_link)
        lay.addStretch(1)
        self._active = None

    def set_active(self, sid):
        self._active = sid
        for s, it in self.items.items():
            it.set_active(s == sid)

    def active(self):
        return self._active


# ── General ──────────────────────────────────────────────────────────────────
def mid_keys(keys, parent=None):
    """32 px keycaps joined by '+' (the Settings shortcut list)."""
    w = QWidget(parent)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    for i, k in enumerate(keys):
        if i:
            plus = QLabel("+", w)
            plus.setProperty("gusan", "kbdPlus")
            plus.setFont(theme.font(14, 700))
            plus.setAccessibleName("plus")
            lay.addWidget(plus, 0, Qt.AlignVCenter)
        cap = Keycap(k, parent=w)
        cap.setObjectName("midKey")
        cap.setFixedHeight(32)
        cap.setMinimumWidth(32)
        cap.setFont(theme.font(14, 700, display=True))
        cap.setStyleSheet("QLabel#midKey { border-radius: 8px; border-bottom: 3px solid %s; padding: 0px 10px; }"
                          % theme.qss_color("line2"))
        lay.addWidget(cap, 0, Qt.AlignVCenter)
    for ch in w.findChildren(QWidget):
        ch.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return w


def not_set_chip(parent=None):
    f = QFrame(parent)
    f.setObjectName("notSet")
    f.setFixedHeight(32)
    f.setStyleSheet("QFrame#notSet { border: 1px dashed %s; border-radius: 8px; background: transparent; }"
                    % theme.qss_color("line2"))
    lay = QHBoxLayout(f)
    lay.setContentsMargins(12, 0, 12, 0)
    lay.setSpacing(6)
    lay.addWidget(icon_label("plus", 14, "muted", parent=f), 0, Qt.AlignVCenter)
    t = text_label("Not set", 13, 600, color="muted", parent=f)
    t.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    lay.addWidget(t, 0, Qt.AlignVCenter)
    return f


class _KeysButton(QPushButton):
    """The keycaps of one shortcut, as a button that opens its page."""

    def __init__(self, accessible, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "iconBtn")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.setAccessibleName(accessible)
        self.setToolTip(accessible)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._inner = None

    def set_inner(self, widget):
        if self._inner is not None:
            self._inner.hide()
            self._inner.deleteLater()
        self._inner = widget
        widget.setParent(self)
        widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._lay.addWidget(widget)
        self.setFixedSize(self.sizeHint())

    def sizeHint(self):
        if self._inner is not None:
            return self._inner.sizeHint().expandedTo(self._inner.minimumSize()).expandedTo(QSize(0, 32))
        return QSize(32, 32)


class ShortcutRow(QFrame):
    open_page = Signal(str)

    def __init__(self, keys_cfg, icon, name, route, link_text, default, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "row")
        self.setProperty("last", False)
        self.keys_cfg, self.route, self.default = keys_cfg, route, default
        self.name = name
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 14, 0, 14)
        lay.setSpacing(16)
        left = QWidget(self)
        left.setFixedWidth(200)
        ll = QHBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(10)
        ll.addWidget(icon_label(icon, 18, "muted", parent=left), 0, Qt.AlignVCenter)
        ll.addWidget(text_label(name, 14, 600, role="label", parent=left), 1, Qt.AlignVCenter)
        lay.addWidget(left, 0, Qt.AlignVCenter)
        self.keys = _KeysButton("Change the %s shortcut" % name, self)
        self.keys.clicked.connect(lambda _=False: self.open_page.emit(self.route))
        lay.addWidget(self.keys, 0, Qt.AlignVCenter)
        lay.addStretch(1)
        right = QWidget(self)
        right.setFixedWidth(150)
        rl = QHBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addStretch(1)
        self.link = trailing_icon(Button(link_text, "link", "m", icon="chev_r", parent=right))
        self.link.setIconSize(QSize(14, 14))
        self.link.setFixedHeight(24)
        self.link.setAccessibleName("Open %s" % link_text)
        self.link.clicked.connect(lambda _=False: self.open_page.emit(self.route))
        rl.addWidget(self.link)
        lay.addWidget(right, 0, Qt.AlignVCenter)
        self._shown = None

    def hotkey(self, cfg):
        for k in self.keys_cfg:
            v = cfg.get(k)
            if v:
                return str(v)
        if self.keys_cfg[0] in cfg:
            return ""
        return self.default

    def refresh(self, cfg):
        hk = self.hotkey(cfg)
        if hk == self._shown:
            return
        self._shown = hk
        keys = keycap_labels(hk) if hk else []
        if keys:
            self.keys.set_inner(mid_keys(keys))
            self.keys.setAccessibleName("Change the %s shortcut (%s)" % (self.name, " + ".join(keys)))
        else:
            self.keys.set_inner(not_set_chip())
            self.keys.setAccessibleName("Set a %s shortcut" % self.name)

    def set_last(self, last):
        theme.set_prop(self, "last", bool(last), children=False)


# ── Microphone ───────────────────────────────────────────────────────────────
class _Segments(QWidget):
    """Up to 26 level segments (6 px + 3 px gap); fewer when the row is
    narrow, so a long status ("Can't open this microphone") never clips."""
    N = 26
    MIN_N = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(self.MIN_N * 9 - 3, 16)
        self.setMaximumSize(self.N * 9 - 3, 16)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._level = 0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def sizeHint(self):
        return QSize(self.N * 9 - 3, 16)

    def count(self):
        return max(1, min(self.N, (self.width() + 3) // 9))

    def lit(self):
        return round(self._level * self.count())

    def set_level(self, level):
        """level 0..1 of the whole meter."""
        level = max(0.0, min(1.0, float(level or 0.0)))
        if round(level * self.count()) != self.lit():
            self._level = level
            self.update()
        else:
            self._level = level

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        on, off = theme.qcolor("blue-l"), theme.qcolor("line")
        lit = self.lit()
        for i in range(self.count()):
            p.setBrush(on if i < lit else off)
            p.drawRoundedRect(QRectF(i * 9, 0, 6, 16), 2, 2)
        p.end()


class LevelMeter(_Box):
    """The live microphone level: 26 segments and a short status."""

    def __init__(self, parent=None):
        super().__init__("#ffffff", "line", 1, 8, parent)
        self.setFixedHeight(36)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName("Microphone level")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(12)
        self.segments = _Segments(self)
        lay.addWidget(self.segments, 0, Qt.AlignVCenter)
        lay.addStretch(1)
        self.status_icon = QLabel(self)
        self.status_icon.setFixedSize(16, 16)
        lay.addWidget(self.status_icon, 0, Qt.AlignVCenter)
        self.status = text_label("", 13, 600, parent=self)
        lay.addWidget(self.status, 0, Qt.AlignVCenter)
        self._state = None
        self.set_state("idle")

    def set_state(self, state):
        """idle | quiet | heard | error."""
        if state == self._state:
            return
        self._state = state
        if state == "heard":
            self.status_icon.setPixmap(icons.pixmap("check_circle", 16, "ok", sw=2))
            self.status_icon.show()
            self.status.setText("We hear you")
            tint(self.status, "ok-tx")
        elif state == "quiet":
            self.status_icon.hide()
            self.status.setText("Say something")
            tint(self.status, "muted")
        elif state == "error":
            self.status_icon.setPixmap(icons.pixmap("alert", 16, "red-tx", sw=2))
            self.status_icon.show()
            self.status.setText("Can't open this microphone")
            tint(self.status, "red-tx")
        else:
            self.status_icon.hide()
            self.status.setText("")
        self.setAccessibleDescription(self.status.text())

    def state(self):
        return self._state


class PyAudioLevel:
    """A level-only input stream on the app's PyAudio (no audio is kept).
    start/stop run on workers; ``level`` (0..1) is read on the GUI thread."""

    def __init__(self, pa):
        self._pa = pa
        self._stream = None
        self._lock = threading.Lock()
        self._gen = 0
        self.level = 0.0
        self.error = ""

    def devices(self):
        out, seen = [], set()
        try:
            host = self._pa.get_default_host_api_info().get("index")
        except Exception:
            host = None
        try:
            count = int(self._pa.get_device_count())
        except Exception:
            return out
        for i in range(count):
            try:
                d = self._pa.get_device_info_by_index(i)
            except Exception:
                continue
            if int(d.get("maxInputChannels", 0) or 0) <= 0:
                continue
            if host is not None and d.get("hostApi") != host:
                continue
            name = str(d.get("name") or "").strip()
            low = name.lower()
            if (not name or name in seen or "loopback" in low or "sound mapper" in low
                    or "primary sound capture" in low):
                continue
            seen.add(name)
            out.append((i, name))
        return out

    def start(self, device_index=None):
        with self._lock:
            self._close()
            gen = self._gen
            try:
                import numpy as np

                def _cb(data, _frames, _time, _status):
                    try:
                        a = np.frombuffer(data, dtype=np.int16).astype(np.int32)
                        self.level = float(np.abs(a).max()) / 32768.0 if a.size else 0.0
                    except Exception:
                        pass
                    return (None, 0)          # paContinue
                kw = dict(format=self._pa.get_format_from_width(2), channels=1, rate=16000, input=True,
                          frames_per_buffer=800, stream_callback=_cb)
                if device_index is not None:
                    kw["input_device_index"] = int(device_index)
                stream = self._pa.open(**kw)
                if gen != self._gen:
                    stream.close()
                    return
                self._stream = stream
                self.error = ""
            except Exception as e:
                logger.info("Mic level stream failed: %s", e)
                self.error = str(e) or "error"
                self._stream = None

    def _close(self):
        s, self._stream = self._stream, None
        self.level = 0.0
        if s is not None:
            try:
                s.stop_stream()
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass

    def stop(self):
        self._gen += 1
        with self._lock:
            self._close()


# ── Speech & AI ──────────────────────────────────────────────────────────────


class GpuCard(_Box):
    """'Speed up with your graphics card' - only on a PC with an NVIDIA card."""
    start = Signal()
    cancel = Signal()
    retry = Signal()
    remove = Signal()

    def __init__(self, parent=None):
        super().__init__("rgba(255,255,255,0.72)", "line", 1, 12, parent)
        self.setAccessibleName("Graphics card speed-up")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)
        lay.addWidget(IconTile("bolt", size=32, icon_size=18, radius=10, parent=self), 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.title = text_label("Speed up with your graphics card", 13.5, 600, role="label", parent=self)
        self.sub = text_label("", 12, role="model", parent=self, wrap=True)
        col.addWidget(self.title)
        col.addWidget(self.sub)
        lay.addLayout(col, 1)
        self.action = QWidget(self)
        self.action_lay = QHBoxLayout(self.action)
        self.action_lay.setContentsMargins(0, 0, 0, 0)
        self.action_lay.setSpacing(8)
        lay.addWidget(self.action, 0, Qt.AlignVCenter)
        self.progress = None
        self.buttons = {}
        self._key = None

    def set_state(self, state, gpu="", pct=None, message="", removable=False):
        """state: available | download | verify | ready | failed. ``removable``:
        "ready" with Gusan's own download on disk - offer Remove."""
        try:
            import gpu_accel
            size = "%d MB" % gpu_accel.DOWNLOAD_MB
        except Exception:
            size = "528 MB"
        gpu = gpu or "Your NVIDIA card"
        key = (state, gpu, message, bool(removable))
        if key == self._key and state == "download":
            self._progress(pct)
            return
        self._key = key
        clear_layout(self.action_lay)
        self.buttons = {}
        self.progress = None
        if state == "ready":
            self.title.setText("Your graphics card is on")
            self.sub.setText("%s · speech runs on it" % gpu)
            self.action_lay.addWidget(Chip("On", "ok", "check", parent=self.action))
            if removable:
                b = Button("Remove", "ghost", "s", parent=self.action)
                b.setAccessibleName("Remove the graphics card speed-up")
                b.clicked.connect(lambda _=False: self.remove.emit())
                self.buttons["remove"] = b
                self.action_lay.addWidget(b)
        elif state == "download":
            self.title.setText("Speed up with your graphics card")
            self.sub.setText("Downloading…")
            self.progress = QProgressBar(self.action)
            self.progress.setTextVisible(False)
            self.progress.setFixedSize(120, 6)
            self.progress.setAccessibleName("Graphics card download")
            self.action_lay.addWidget(self.progress, 0, Qt.AlignVCenter)
            b = Button("Cancel", "ghost", "s", parent=self.action)
            b.setAccessibleName("Cancel the graphics card download")
            b.clicked.connect(lambda _=False: self.cancel.emit())
            self.buttons["cancel"] = b
            self.action_lay.addWidget(b)
            self._progress(pct)
        elif state == "verify":
            self.title.setText("Speed up with your graphics card")
            self.sub.setText("Checking your graphics card…")
        elif state == "failed":
            self.title.setText("Your graphics card couldn't start")
            self.sub.setText("%s · updating the NVIDIA driver usually fixes it" % gpu)
            b = Button("Try again", "secondary", "s", icon="refresh", parent=self.action)
            b.setAccessibleName("Try the graphics card again")
            b.clicked.connect(lambda _=False: self.retry.emit())
            self.buttons["retry"] = b
            self.action_lay.addWidget(b)
        else:
            self.title.setText("Speed up with your graphics card")
            self.sub.setText("%s · about 20× faster · %s" % (gpu, size))
            b = Button("Download", "secondary", "s", icon="download", parent=self.action)
            b.setAccessibleName("Download graphics card speed-up (%s)" % size)
            b.clicked.connect(lambda _=False: self.start.emit())
            self.buttons["start"] = b
            self.action_lay.addWidget(b)
        if message and state not in ("download", "ready"):
            self.sub.setText(message)
            tint(self.sub, "red-tx")
        else:
            self.sub.setStyleSheet("")          # back to the "model" text role's colour

    def _progress(self, pct):
        if self.progress is None:
            return
        if pct is None or pct < 0:
            self.progress.setRange(0, 0)
            self.sub.setText("Checking your graphics card…" if pct is not None else "Downloading…")
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(pct))
            self.sub.setText("Downloading · %d%%" % int(pct))


class ApiKeyRow(QFrame):
    """One saved key of "My API keys": the name the person gave it, its provider and model, the
    key shown masked, whether it worked when tested, which card uses it; Test / Edit / Remove."""
    test = Signal(str)            # key id
    edit = Signal(str)
    removed = Signal(str)

    def __init__(self, entry, view, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "row")
        self.setProperty("last", False)
        self.key_id = entry["id"]
        # Two lines, so a narrow window never has to scroll sideways: the name, which card uses
        # it and the buttons; under it the provider and model, the masked key and its test.
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 10, 0, 10)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = text_label(view["title"], 13.5, 600, role="label", parent=self)
        top.addWidget(self.title, 0, Qt.AlignVCenter)
        self.use_chip = Chip("", "blue", parent=self)
        self.use_chip.setVisible(bool(view.get("in_use")))
        self.use_chip.setText(view.get("in_use") or "")
        self.use_chip.setToolTip("Used by %s now" % view.get("in_use") if view.get("in_use") else "")
        top.addWidget(self.use_chip, 0, Qt.AlignVCenter)
        top.addStretch(1)
        name = view["title"]
        self.test_button = Button("Test", "ghost", "s", parent=self)
        self.test_button.setAccessibleName("Test the key %s" % name)
        self.test_button.clicked.connect(lambda _=False: self.test.emit(self.key_id))
        self.edit_button = Button("Edit", "ghost", "s", parent=self)
        self.edit_button.setAccessibleName("Edit the key %s" % name)
        self.edit_button.clicked.connect(lambda _=False: self.edit.emit(self.key_id))
        self.remove_button = Button("Remove", "ghost", "s", parent=self)
        self.remove_button.setAccessibleName("Remove the key %s" % name)
        self.remove_button.clicked.connect(lambda _=False: self.removed.emit(self.key_id))
        for b in (self.test_button, self.edit_button, self.remove_button):
            top.addWidget(b, 0, Qt.AlignVCenter)
        lay.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.sub = text_label(view["sub"], 12.5, role="muted", parent=self)
        bottom.addWidget(self.sub, 0, Qt.AlignVCenter)
        self.key_icon = icon_label("key", 14, "muted", parent=self)
        bottom.addWidget(self.key_icon, 0, Qt.AlignVCenter)
        self.masked = text_label(view["masked"], 12.5, role="muted", parent=self)
        bottom.addWidget(self.masked, 0, Qt.AlignVCenter)
        self.chip = Chip("Works", "ok", "check", parent=self)
        self.chip.hide()
        bottom.addWidget(self.chip, 0, Qt.AlignVCenter)
        bottom.addStretch(1)
        lay.addLayout(bottom)

    def set_last(self, last):
        theme.set_prop(self, "last", bool(last), children=False)

    def show_test(self, result):
        """None: not tested; "checking"; or (ok, message)."""
        if result == "checking":
            self.chip.setText("Checking…")
            theme.set_prop(self.chip, "tone", "neutral")
            self.chip.setToolTip("")
            self.chip.show()
            return
        if not result:
            self.chip.hide()
            return
        ok, msg = result
        self.chip.setText("Works" if ok else "Didn't work")
        theme.set_prop(self.chip, "tone", "ok" if ok else "red")
        if getattr(self.chip, "_icon", None) is not None:
            self.chip._icon.setPixmap(icons.pixmap("check" if ok else "alert", 13, "ok-tx" if ok else "red-tx", sw=2))
        self.chip.setToolTip("" if ok else (msg or ""))
        self.chip.show()


class ApiKeyEditor(Card):
    """Add or edit one key: a name of your own, the provider, the model, the server address (for an
    OpenAI-compatible service or Cerebras), the key itself, and Test before Save."""
    saved = Signal(dict)
    cancelled = Signal()
    test = Signal(str, str, str, str)      # provider, key, model, base url

    def __init__(self, parent=None):
        super().__init__(parent, pad=(20, 16), gap=12)
        self.heading = text_label("Add a key", 14, 700, parent=self)
        self.add(self.heading)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        self.name = self._line("Name", "e.g. My OpenAI key")
        self.provider = field_font(QComboBox(self))
        self.provider.setAccessibleName("Provider")
        for pid in api_keys.ENTRY_PROVIDERS:
            self.provider.addItem(api_keys.ENTRY_PROVIDER_LABELS[pid], pid)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.model = field_font(QComboBox(self))
        self.model.setEditable(True)                       # any model id the service has
        self.model.setAccessibleName("Model")
        self.model.setInsertPolicy(QComboBox.NoInsert)
        self.model.lineEdit().setAccessibleName("Model")
        self.base = self._line("Server address", "https://api.example.com/v1")
        self.key = self._line("Key", "Paste the key")
        self.key.setEchoMode(QLineEdit.Password)
        self.key.textChanged.connect(self._key_changed)
        self.base_label = text_label("Server address", 13, 600, parent=self)
        rows = (("Name", self.name), ("Provider", self.provider), ("Model", self.model),
                (self.base_label, self.base), ("Key", self.key))
        for r, (lab, widget) in enumerate(rows):
            lb = lab if isinstance(lab, QLabel) else text_label(lab, 13, 600, parent=self)
            lb.setFixedWidth(110)
            grid.addWidget(lb, r, 0, Qt.AlignVCenter)
            widget.setFixedHeight(32)
            grid.addWidget(widget, r, 1)
        self.add(grid)
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.test_button = Button("Test key", "secondary", "s", parent=self)
        self.test_button.setAccessibleName("Test this key")
        self.test_button.clicked.connect(self._test)
        foot.addWidget(self.test_button, 0, Qt.AlignVCenter)
        self.status = text_label("", 12.5, 600, parent=self, wrap=True)
        self.status.setAccessibleName("Key check")
        foot.addWidget(self.status, 1, Qt.AlignVCenter)
        self.cancel_button = Button("Cancel", "ghost", "s", parent=self)
        self.cancel_button.setAccessibleName("Cancel")
        self.cancel_button.clicked.connect(self._cancel)
        foot.addWidget(self.cancel_button, 0, Qt.AlignVCenter)
        self.save_button = Button("Save", "primary", "s", parent=self)
        self.save_button.setAccessibleName("Save the key")
        self.save_button.clicked.connect(self._save)
        foot.addWidget(self.save_button, 0, Qt.AlignVCenter)
        self.add(foot)
        self._editing_id = None
        self._provider_changed()

    def _line(self, name, placeholder):
        w = field_font(QLineEdit(self))
        w.setPlaceholderText(placeholder)
        w.setAccessibleName(name)
        return w

    def editing_id(self):
        return self._editing_id

    def open(self, entry=None, provider=None):
        """A new key (``provider`` preselected), or ``entry`` to edit (its key stays unless replaced)."""
        self._editing_id = entry["id"] if entry else None
        self.heading.setText("Edit key" if entry else "Add a key")
        pid = (entry or {}).get("provider") or provider or api_keys.ENTRY_PROVIDERS[0]
        i = self.provider.findData(pid)
        self.provider.setCurrentIndex(max(0, i))
        self._provider_changed()
        self.name.setText((entry or {}).get("name") or "")
        if entry:
            self._set_model(entry.get("model") or "")
            self.base.setText(entry.get("base_url") or "")
        self.key.clear()
        self.key.setPlaceholderText("Leave empty to keep the saved key" if entry else "Paste the key")
        self.status.setText("")
        self._key_changed(self.key.text())
        self.show()
        (self.key if entry else self.name).setFocus(Qt.OtherFocusReason)

    def _set_model(self, mid):
        i = self.model.findData(mid)
        if i >= 0:
            self.model.setCurrentIndex(i)
        else:
            self.model.setEditText(mid)

    def _provider_changed(self, *_):
        pid = self.provider.currentData()
        info = engine_choice.WRITER_PROVIDERS.get(pid) or {}
        self.model.blockSignals(True)
        self.model.clear()
        for mid, name, note in engine_choice.writer_models(pid):
            self.model.addItem("%s · %s" % (name, note) if note else name, mid)
        self.model.blockSignals(False)
        self._set_model(info.get("default", ""))
        editable = api_keys.provider_has_address(pid)
        self.base_label.setVisible(editable)
        self.base.setVisible(editable)
        self.base.setPlaceholderText(info.get("default_url", ""))
        if not editable:
            self.base.clear()
        self.status.setText("")

    def model_id(self):
        i = self.model.currentIndex()
        text = self.model.currentText().strip()
        if i >= 0 and self.model.itemText(i) == text:
            return str(self.model.itemData(i) or "")
        return text                                       # typed by hand

    def _key_changed(self, text):
        self.save_button.setEnabled(bool(text.strip()) or self._editing_id is not None)
        self.test_button.setEnabled(bool(text.strip()))
        self.status.setText("")

    def _test(self):
        key = self.key.text().strip()
        if not key:
            return
        self.status.setText("Checking…")
        tint(self.status, "muted")
        self.test.emit(self.provider.currentData(), key, self.model_id(), self.base.text().strip())

    def show_test(self, key, ok, message):
        """A test answer: shown only while that key is still the one typed."""
        if self.isHidden() or key != self.key.text().strip():
            return
        self.status.setText("✓ Works" if ok else (message or "That key didn't work."))
        tint(self.status, "ok-tx" if ok else "red-tx")
        self.status.setToolTip("" if ok else (message or ""))

    def show_error(self, message):
        self.status.setText(message)
        tint(self.status, "red-tx")

    def has_unsaved_key(self):
        return not self.isHidden() and bool(self.key.text().strip())

    def _save(self):
        key = self.key.text().strip()
        if not key and self._editing_id is None:
            return
        self.saved.emit({"id": self._editing_id, "name": self.name.text().strip(),
                         "provider": self.provider.currentData(), "model": self.model_id(),
                         "base_url": self.base.text().strip(), "key": key})

    def _cancel(self):
        self.hide()
        self.cancelled.emit()


class ApiKeysPanel(QWidget):
    """Settings › My API keys: every saved key in one list, "+ Add key", and the editor."""
    test = Signal(str, str, str, str, object)    # provider, key, model, base url, token (for the answer)
    test_saved = Signal(str)                       # a saved key's id
    saved = Signal(dict)
    removed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        self.list_card = Card(self, pad=(20, 4), gap=0)      # shown once there is a key
        self.list_card.hide()
        v.addWidget(self.list_card)
        self.add_button = Button("Add key", "secondary", "s", icon="plus", parent=self)
        self.add_button.setAccessibleName("Add an API key")
        self.add_button.clicked.connect(lambda _=False: self.open_new())
        v.addWidget(self.add_button, 0, Qt.AlignLeft)
        self.editor = ApiKeyEditor(self)
        self.editor.saved.connect(self.saved.emit)
        self.editor.cancelled.connect(self._editor_closed)
        self.editor.test.connect(lambda p, k, m, b: self.test.emit(p, k, m, b, ("editor", k)))
        self.editor.hide()
        v.addWidget(self.editor)
        self.rows = {}
        self._entries = {}
        self._tests = {}                  # key id -> (ok, message) of its last test

    def show_entries(self, items):
        """items: [(entry, api_keys.entry_view(...))] - the list is drawn again."""
        for row in self.rows.values():
            row.hide()
            row.deleteLater()
        self.rows, self._entries = {}, {}
        last = None
        for entry, view in items:
            row = ApiKeyRow(entry, view, self.list_card)
            row.test.connect(self._test_row)
            row.edit.connect(self._edit_row)
            row.removed.connect(self.removed.emit)
            row.show_test(self._tests.get(entry["id"]))
            self.list_card.add(row)
            self.rows[entry["id"]] = row
            self._entries[entry["id"]] = (entry, view)
            row.set_last(False)
            last = row
        if last is not None:
            last.set_last(True)
        self.list_card.setVisible(bool(items))

    def _test_row(self, key_id):
        row = self.rows.get(key_id)
        if row is not None:
            row.show_test("checking")
        self.test_saved.emit(key_id)

    def _edit_row(self, key_id):
        if key_id in self._entries:
            self.open_edit(*self._entries[key_id])

    def open_new(self, provider=None):
        self.editor.open(None, provider)
        self.add_button.hide()

    def open_edit(self, entry, _view=None):
        self.editor.open(entry)
        self.add_button.hide()

    def close_editor(self):
        self.editor.hide()
        self._editor_closed(emit=False)

    def _editor_closed(self, emit=True):
        self.add_button.show()
        if emit:
            self.cancelled.emit()

    def has_unsaved_key(self):
        return self.editor.has_unsaved_key()

    def show_test(self, token, ok, message):
        kind, ident = token
        if kind == "editor":
            self.editor.show_test(ident, ok, message)
            return
        self._tests[ident] = (bool(ok), message)
        row = self.rows.get(ident)
        if row is not None and alive(row):
            row.show_test((bool(ok), message))


class EngineCard(Card):
    """One of Speech & AI's two cards (Speech, AI writer), both in one format: the title and what
    it is for (the hardware on the right); "Runs on" - where the model runs - and "Model" - the
    model, as the pickers list it (real names, LOCAL / CLOUD / PRO chips, a download icon on one
    that is not on this computer); one status line with its one action under them. The page
    fills it (fill / set_source / set_status) and reads runs_on.changed / combo picks."""
    LABEL_W = 76
    COMBO_W = 320

    action_clicked = Signal()

    def __init__(self, title, sub, sources, kind="", help_text="", parent=None):
        super().__init__(parent, pad=(22, 18), gap=12)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = text_label(title, 15, 700, parent=self)
        head.addWidget(self.title, 0, Qt.AlignVCenter)
        self.kind = text_label(kind, 12.5, 600, role="muted", parent=self)
        head.addWidget(self.kind, 0, Qt.AlignVCenter)
        self.help = QLabel(self)
        self.help.setPixmap(icons.pixmap("help", 15, "muted", sw=1.8))
        self.help.setFixedSize(16, 16)
        self.help.setToolTip(help_text)
        self.help.setAccessibleName("What is the %s model?" % title)
        self.help.setAccessibleDescription(help_text)
        self.help.setCursor(Qt.WhatsThisCursor)
        self.help.setVisible(bool(help_text))
        head.addWidget(self.help, 0, Qt.AlignVCenter)
        head.addStretch(1)
        self.trail = text_label("", 12.5, role="muted", parent=self)
        head.addWidget(self.trail, 0, Qt.AlignVCenter)
        self.add(head)
        self.sub = text_label(sub, 12.5, role="muted", parent=self, wrap=True)
        self.sub.setVisible(bool(sub))
        self.add(self.sub)
        self.sources = tuple(k for k, _n in sources)
        self.runs_on = Segmented([n for _k, n in sources], 0, "%s runs on" % title, size="s", parent=self)
        self.add(self._row("Runs on", self.runs_on))
        self.combo = PlainNameCombo(self)
        self.combo.setAccessibleName("%s model" % title)
        self.combo.setFont(theme.font(13.5))
        self.combo.setCursor(Qt.PointingHandCursor)
        self.combo.setFixedWidth(self.COMBO_W)
        self.combo.enable_row_marks()
        self.combo.setPlaceholderText(PICK_A_MODEL)        # a "Runs on" just picked: nothing chosen yet
        self.add(self._row("Model", self.combo))
        status = QHBoxLayout()
        status.setContentsMargins(self.LABEL_W + 12, 0, 0, 0)
        status.setSpacing(10)
        self.status = text_label("", 12.5, role="muted", parent=self, wrap=True)
        status.addWidget(self.status, 1, Qt.AlignVCenter)
        self.action = Button("", "link", "s", parent=self)
        self.action.setAccessibleName("%s model action" % title)
        self.action.clicked.connect(self.action_clicked.emit)
        self.action.hide()
        status.addWidget(self.action, 0, Qt.AlignVCenter)
        self.add(status)
        self.extra = QVBoxLayout()           # the NVIDIA card under Speech
        self.extra.setContentsMargins(0, 0, 0, 0)
        self.add(self.extra)

    def _row(self, name, widget):
        row = QHBoxLayout()
        row.setSpacing(12)
        lb = text_label(name, 13.5, 600, parent=self)
        lb.setFixedWidth(self.LABEL_W)
        row.addWidget(lb, 0, Qt.AlignVCenter)
        row.addWidget(widget, 0, Qt.AlignLeft | Qt.AlignVCenter)
        row.addStretch(1)
        return row

    def source(self):
        return self.sources[self.runs_on.index()]

    def set_source(self, key):
        if key in self.sources and self.runs_on.index() != self.sources.index(key):
            self.runs_on.blockSignals(True)
            self.runs_on.set_index(self.sources.index(key))
            self.runs_on.blockSignals(False)

    def fill(self, rows, value):
        """The rows, with ``value`` selected (what is really set). None, or a value that isn't listed:
        nothing selected - the box says "Select a model"."""
        box = self.combo
        box.blockSignals(True)
        try:
            box.fill_rows(rows)
            idx = box.findData(value) if value else -1      # (a heading's data is None too)
            box.setCurrentIndex(idx if idx >= 0 and not box.is_heading(idx) else -1)
        finally:
            box.blockSignals(False)

    def set_status(self, text, action="", tone="muted"):
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.status.setProperty("textRole", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.action.setText(action)
        self.action.setAccessibleName("%s: %s" % (action, self.title.text()) if action
                                      else "%s model action" % self.title.text())
        self.action.setVisible(bool(action))


class _Dot(QWidget):
    def __init__(self, color="ok", size=7, parent=None):
        super().__init__(parent)
        self._color = color
        self.setFixedSize(size, size)

    def set_color(self, color):
        self._color = color
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor(self._color))
        p.drawEllipse(QRectF(self.rect()))
        p.end()


class FeedbackDialog(QDialog):
    """Send feedback: one box, Send. Guests are asked to sign in first."""

    def __init__(self, page, app):
        super().__init__(page.window() if page is not None else None)
        theme.apply(self)
        self.app = app
        self.setWindowTitle("Send feedback")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(10)
        lay.addWidget(label("Send feedback", "h2", self))
        self.box = None
        self.result = label("", "muted", self, wrap=True)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.cancel_button = Button("Close", "ghost", "m", parent=self)
        self.cancel_button.clicked.connect(self.reject)
        row.addWidget(self.cancel_button)
        if is_signed_in(app):
            lay.addWidget(label("Ideas, bugs, a language you need.", "muted", self))
            self.box = field_font(QPlainTextEdit(self))
            self.box.setAccessibleName("Your feedback")
            self.box.setPlaceholderText("What should we fix or add?")
            self.box.setMinimumHeight(140)
            lay.addWidget(self.box)
            self.send_button = Button("Send", "primary", "m", icon="send", parent=self)
            self.send_button.clicked.connect(self._send)
            row.addWidget(self.send_button)
        else:
            lay.addWidget(label("Sign in so we can reply.", "body", self))
            self.send_button = Button("Sign in", "primary", "m", parent=self)
            self.send_button.clicked.connect(self._sign_in)
            row.addWidget(self.send_button)
        lay.addWidget(self.result)
        lay.addLayout(row)

    def _sign_in(self):
        self.accept()
        call(self.app, "show_auth_gate")

    def _send(self):
        text = self.box.toPlainText().strip() if self.box is not None else ""
        if not text:
            self.result.setText("Type a message first.")
            return
        self.send_button.setEnabled(False)
        self.send_button.setText("Sending…")
        run_bg(account_actions.send_feedback, getattr(self.app, "auth", None), text, then=self._sent,
               error=lambda e: self._sent((False, "Couldn't send it. Try again.")), owner=self)

    def _sent(self, res):
        ok, msg = res if isinstance(res, tuple) else (False, str(res))
        self.result.setText(msg)
        self.send_button.setText("Send")
        self.send_button.setEnabled(not ok)
        if ok:
            track(self.app, "feedback_sent", {"category": "feedback"})
            if self.box is not None:
                self.box.setEnabled(False)
            self.cancel_button.setText("Done")


# ── the page ─────────────────────────────────────────────────────────────────
def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def describe_all(n):
    """{"dictations": 3, "meetings": 1, "transcripts": 2} -> '3 dictations, 1 meeting and 2 transcripts'."""
    parts = [_plural(n[k], w) for k, w in (("dictations", "dictation"), ("meetings", "meeting"),
                                             ("transcripts", "transcript")) if n.get(k)]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _count_history(keep):
    n = dict(history_actions.counts(keep))
    try:
        import transcript_store
        n["transcripts"] = len(transcript_store.list_transcripts())
    except Exception:
        n["transcripts"] = 0
    return n


def _clear_history(include_everything, keep, track_fn):
    res = dict(history_actions.clear(include_everything, keep, track=track_fn))
    res["transcripts"] = 0
    if include_everything:
        try:
            import transcript_store
            res["transcripts"] = transcript_store.delete_all_transcripts()
        except Exception:
            logger.warning("Deleting transcripts failed", exc_info=True)
    return res


def clear_result_text(res):
    done = describe_all(res) or "nothing"
    extra = history_actions.result_text(dict(res, dictations=0, meetings=0))
    prefix = "Deleted nothing."
    extra = extra[len(prefix):] if extra.startswith(prefix) else ""
    return "Deleted %s.%s" % (done, extra)


class SettingsPage(QWidget):
    wants_scroll = False             # the content column scrolls; header and section list stay

    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.opts = {}
        self.setProperty("gusan", "page")
        self._sections = {}
        self.dictionary = None          # the embedded Dictionary (built with its section)
        self._pending = None            # (section, focus) waiting for a layout / show
        self._jump = None               # (section, scroll value) we jumped to
        self._refresh_queued = False
        self._pending_use = {}          # model id -> "speech" | "writer" after its download
        self._hardware = None
        self._hw_asked = False
        self._mic = None
        self._mic_on = False
        self._mic_heard_at = 0.0
        self._mic_shown = 0.0
        self._devices_loaded = False
        self._update = None             # (status, tag)
        self._gpu = {"phase": None, "pct": None, "message": ""}
        self._services = set()
        self._narrow = False            # see _apply_width

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = PageHeader("Settings", "Changes save on their own.", parent=self)
        root.addWidget(self.header)
        body = QHBoxLayout()
        body.setContentsMargins(32, 0, 0, 0)
        body.setSpacing(32)
        tier_name = self._account_name()
        self.nav = SectionNav(tier_name, self, guest=self._account_guest())
        self.nav.picked.connect(lambda sid: self.show_section(sid))
        self.nav.account.connect(lambda: go(self, self.app, "account"))
        body.addWidget(self.nav, 0, Qt.AlignTop)
        self.scroll = QScrollArea(self)
        self.scroll.setProperty("gusan", "scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.setAccessibleName("Settings")
        content = QWidget()
        content.setProperty("gusan", "scrollContent")
        self.content = content
        self.col = QVBoxLayout(content)
        self.col.setContentsMargins(0, 0, 22, 28)       # + the 10 px scrollbar = the mockup's 32
        self.col.setSpacing(30)
        # Room under the last section so every section can scroll to the top.
        self._tail = QWidget(content)
        self._tail.setFixedHeight(0)
        self.col.addWidget(self._tail)
        self.col.addStretch(1)
        # General and Microphone now; the rest right after the first paint (one
        # section per event-loop turn) or at once when a section is asked for.
        self._built = set()
        self._lazy = ["speech", "dictionary", "privacy", "updates"]
        self._build_general()
        self._build_microphone()
        self.scroll.setWidget(content)
        body.addWidget(self.scroll, 1)
        root.addLayout(body, 1)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self.scroll.viewport().installEventFilter(self)
        self.nav.set_active("general")

        self._mic_timer = QTimer(self)
        self._mic_timer.setInterval(40)
        self._mic_timer.timeout.connect(self._mic_tick)

        self._connect_services()
        self.refresh()
        self._ask_hardware()
        QTimer.singleShot(40, self._build_next)

    # ── small plumbing ──
    @property
    def cfg(self):
        return cfg_of(self.app)

    def _dl(self):
        return getattr(self.app, "downloads", None)

    def _account_name(self):
        try:
            from ui.shell import account_state
            return account_state(self.app)[1]
        except Exception:
            return "A"

    def _account_guest(self):
        """A guest's "Account & plan" link wears the person icon, not a letter."""
        try:
            from ui.shell import account_state
            return account_state(self.app)[0] == "guest"
        except Exception:
            return False

    def _set(self, key, value):
        """One setting from a control; the control shows the truth again after."""
        ok = apply_settings(self.app, {key: value}).get(key, False)
        if not ok:
            self._queue_refresh()
        return ok

    def _queue_refresh(self):
        if self._refresh_queued:
            return
        self._refresh_queued = True
        QTimer.singleShot(0, self._run_queued_refresh)

    def _run_queued_refresh(self):
        self._refresh_queued = False
        if alive(self):
            self.refresh()

    def _toggle(self, key, name, default):
        t = Toggle(name, bool(self.cfg.get(key, default)), self)
        t.clicked.connect(lambda on, k=key: self._set(k, bool(on)))
        t._cfg = (key, default)
        return t

    def _sync_toggle(self, t):
        key, default = t._cfg
        t.blockSignals(True)
        t.setChecked(bool(self.cfg.get(key, default)))
        t.blockSignals(False)
        t.update()

    def _row(self, text, control=None, hint=None, pro=False):
        row = SettingRow(text, control, hint=hint, pro=pro)
        row.label.setMinimumHeight(20)              # the mockups' 20 px label line
        row.hint_label.setMinimumHeight(17)         # and 17 px hint line
        if not hint and isinstance(control, Toggle):
            row.layout().setContentsMargins(0, 10, 0, 10)
        return row

    def _group(self, rows, pad=(20, 4)):
        card = Card(pad=pad, gap=0)
        last = None
        for r in rows:
            card.add(r)
            if hasattr(r, "set_last"):
                r.set_last(False)
                last = r
        if last is not None:
            last.set_last(True)
        return card

    def _add(self, widget):
        """Add a block to the content column (above the tail spacer)."""
        self.col.insertWidget(self.col.indexOf(self._tail), widget)

    def _section(self, sid, title, gap=12):
        w = QWidget(self.content)
        w.setAccessibleName(title)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(gap)
        h = label(title, "h2", w)
        h.setFont(theme.font(19, 600, display=True))
        h.setFixedHeight(24)
        v.addWidget(h)
        self._sections[sid] = w
        self._add(w)
        return w, v

    # ── building the later sections ──
    _BUILDERS = {"speech": "_build_speech", "dictionary": "_build_dictionary",
                 "privacy": "_build_privacy", "updates": "_build_updates"}

    def is_built(self, sid):
        return sid in self._built or sid in ("general", "microphone")

    def ensure_built(self, upto=None):
        """Build the lazy sections now (all, or up to ``upto``)."""
        while self._lazy:
            sid = self._lazy[0]
            if upto is not None and self.is_built(upto):
                break
            self._build_one()

    def _build_one(self):
        sid = self._lazy.pop(0)
        getattr(self, self._BUILDERS[sid])()
        self._built.add(sid)
        if sid == "speech":
            self._refresh_speech_all()
        elif sid == "privacy":
            self._refresh_privacy()
        elif sid == "updates":
            self._refresh_updates()
        if self._hardware is not None:
            self._on_hardware(self._hardware)
        self._fit_tail()
        # A jump to a section near the end could only go as far as the page was
        # long then: aim again now that there is more room below (unless the
        # user scrolled meanwhile - _settle_jump checks).
        if self._jump is not None and getattr(self, "_jump_target", None):
            QTimer.singleShot(0, self._settle_jump)

    def _build_next(self):
        if not alive(self) or not self._lazy:
            return
        self._build_one()
        if self._lazy:
            QTimer.singleShot(0, self._build_next)

    def eventFilter(self, obj, ev):
        if obj is self.scroll.viewport() and ev.type() == QEvent.Resize:
            self._fit_tail()
        elif obj is getattr(self, "_watched_window", None) and ev.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, self._update_mic)      # minimized / restored: close or reopen the mic
        return False

    def _fit_tail(self):
        last = self._sections.get("updates")
        if last is None:
            return
        h = max(last.height(), last.sizeHint().height())
        room = self.scroll.viewport().height() - h - 28 - self.col.spacing()
        self._tail.setFixedHeight(max(0, room))

    def _block(self, title, sub=None, trail=None, parent=None):
        w = QWidget(parent)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        head = QVBoxLayout()
        head.setSpacing(0)
        trow = QHBoxLayout()
        trow.setSpacing(12)
        t = text_label(title, 14, 700, role="label", parent=w)
        t.setFixedHeight(20)
        trow.addWidget(t, 0, Qt.AlignVCenter)
        trow.addStretch(1)
        if trail is not None:
            trow.addWidget(trail, 0, Qt.AlignVCenter)
        head.addLayout(trow)
        if sub:
            head.addWidget(label(sub, "muted", w))
        v.addLayout(head)
        return w, v

    # ── Dictionary ──
    def _build_dictionary(self):
        """Your words, replacements and removals: the Dictionary page's content,
        embedded. A failure here must not stop the sections after it."""
        sec, v = self._section("dictionary", "Dictionary")
        try:
            import ui.pages.dictionary as dictionary_page      # plain import: PyInstaller bundles it
            page = dictionary_page.DictionaryPage(self.app, embedded=True)    # no parent: a failure leaves nothing in the section
            v.addWidget(page)
            self.dictionary = page
        except Exception:
            logger.warning("The Dictionary section failed to build", exc_info=True)
            self.dictionary = None
            v.addWidget(label("The dictionary couldn't open. Restart Gusan and try again.", "muted", sec, wrap=True))

    # ── General ──
    def _build_general(self):
        sec, v = self._section("general", "General")
        inner = QVBoxLayout()
        inner.setSpacing(22)
        start_text = "Start Gusan when your Mac starts" if IS_MAC else "Start Gusan when Windows starts"
        supported, enabled = autostart_state()
        self.t_login = Toggle(start_text, enabled, self)
        self.t_login.setEnabled(supported)
        self.t_login.clicked.connect(self._on_login_clicked)
        self.row_login = self._row(start_text, self.t_login,
                                   hint=None if supported else "Works in the installed app.")
        values = [k for k, _t in APP_LOOKS]
        cur = self.cfg.get("app_theme", "system")
        self.seg_look = Segmented([t for _k, t in APP_LOOKS], values.index(cur) if cur in values else 0,
                                  "App look", parent=self)
        self.seg_look.changed.connect(lambda i: self._set("app_theme", values[i]))
        self.t_notify = self._toggle("notify_when_ready", "Notify me when notes or transcripts are ready", True)
        rows = [self.row_login]
        self.row_look = None
        if SHOW_APP_LOOK:
            self.row_look = self._row("App look", self.seg_look)
            rows.append(self.row_look)
        else:
            self.seg_look.hide()             # built (app_theme is kept), never shown
        rows.append(self._row("Notify me when notes or transcripts are ready", self.t_notify))
        self.row_mac_perms = None
        if IS_MAC:
            # The macOS permissions guide (main.py), whenever the shortcut or
            # pasting stops working - not only on the first launch.
            b = Button("Check", "secondary", "s", parent=self)
            b.clicked.connect(lambda: call(self.app, "show_macos_permissions_guide"))
            self.row_mac_perms = self._row("Mac permissions", b,
                                           hint="Microphone, Accessibility and Input Monitoring")
            rows.append(self.row_mac_perms)
        inner.addWidget(self._group(rows))
        blk, bv = self._block("Shortcuts", parent=sec)
        self.shortcut_rows = []
        for spec in SHORTCUTS:
            r = ShortcutRow(*spec)
            r.open_page.connect(lambda route: go(self, self.app, route, focus="hotkey"))
            self.shortcut_rows.append(r)
        bv.addWidget(self._group(self.shortcut_rows))          # padding 4px 20px, as on the board
        inner.addWidget(blk)
        v.addLayout(inner)

    def _on_login_clicked(self, on):
        ok = self._set("launch_at_login", bool(on))
        if not ok:
            QTimer.singleShot(0, self._sync_login)

    def _sync_login(self):
        supported, enabled = autostart_state()
        if not supported:
            return
        self.t_login.blockSignals(True)
        self.t_login.setChecked(enabled)
        self.t_login.blockSignals(False)
        self.t_login.update()

    # ── Microphone ──
    def _build_microphone(self):
        sec, v = self._section("microphone", "Microphone")
        card = Card(pad=(20, 16))
        row = QHBoxLayout()
        row.setSpacing(16)
        self.mic_combo = field_font(QComboBox(card))
        self.mic_combo.setAccessibleName("Microphone")
        self.mic_combo.setFixedSize(260, 36)
        self.mic_combo.addItem("Default microphone", None)
        self.mic_combo.activated.connect(self._on_mic_picked)
        row.addWidget(self.mic_combo, 0, Qt.AlignVCenter)
        self.level = LevelMeter(card)
        row.addWidget(self.level, 1)
        card.add(row)
        self.mic_card = card
        v.addWidget(card)
        # The pages that carry their own options.
        foot = QVBoxLayout()
        foot.setSpacing(8)
        foot.addWidget(text_label("Feature options are on their pages", 13, role="muted", parent=self.content))
        links = QHBoxLayout()
        links.setSpacing(14)
        self.feature_links = {}
        for icon, name, route in FEATURE_PAGES:
            b = Button(name, "link", "s", icon=icon, parent=self.content)
            b.setIconSize(QSize(15, 15))
            b.setAccessibleName("Open %s" % name)
            b.clicked.connect(lambda _=False, r=route: go(self, self.app, r))
            links.addWidget(b)
            self.feature_links[route] = b
        links.addStretch(1)
        foot.addLayout(links)
        holder = QWidget(self.content)
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addLayout(foot)
        self._add(holder)

    def _mic_source(self):
        if self._mic is not None:
            return self._mic
        mon = getattr(self.app, "mic_monitor", None)
        if mon is not None:
            self._mic = mon
            return mon
        pa = getattr(getattr(self.app, "recorder", None), "audio", None)
        if pa is not None:
            self._mic = PyAudioLevel(pa)
        return self._mic

    def _load_devices(self):
        src = self._mic_source()
        if src is None or self._devices_loaded or not hasattr(src, "devices"):
            return
        self._devices_loaded = True
        run_bg(src.devices, then=self._on_devices, owner=self)

    def _on_devices(self, devices):
        cfg_index = self.cfg.get("input_device_index")
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        self.mic_combo.addItem("Default microphone", None)
        for idx, name in devices or []:
            self.mic_combo.addItem(name, idx)
        self.mic_combo.blockSignals(False)
        self._sync_mic_combo(cfg_index)

    def _sync_mic_combo(self, cfg_index=None):
        if cfg_index is None:
            cfg_index = self.cfg.get("input_device_index")
        try:
            want = int(cfg_index) if cfg_index not in (None, "", "default") else None
        except (TypeError, ValueError):
            want = None
        i = self.mic_combo.findData(want) if want is not None else 0
        self.mic_combo.blockSignals(True)
        self.mic_combo.setCurrentIndex(i if i >= 0 else 0)
        self.mic_combo.blockSignals(False)

    def _on_mic_picked(self, i):
        idx = self.mic_combo.itemData(i)
        self._set("input_device_index", idx)
        if self._mic_on:
            self._stop_mic()
            self._start_mic()

    def _mic_in_view(self):
        if not self.isVisible():
            return False
        win = self.window()
        if win is not None and win.isMinimized():
            return False                    # minimized windows stay "visible" to Qt
        vp = self.scroll.viewport()
        top = self.mic_card.mapTo(self.content, self.mic_card.rect().topLeft()).y()
        val = self.scroll.verticalScrollBar().value()
        return top + self.mic_card.height() > val and top < val + vp.height()

    def _update_mic(self):
        if self._mic_in_view():
            self._load_devices()
            if not self._mic_on:
                self._start_mic()
        elif self._mic_on:
            self._stop_mic()

    def _start_mic(self):
        src = self._mic_source()
        if src is None:
            self.level.set_state("idle")
            return
        self._mic_on = True
        self._mic_heard_at = 0.0
        self._mic_started = time.monotonic()
        idx = self.mic_combo.currentData()
        run_bg(src.start, idx, owner=self)
        self.level.set_state("quiet")
        self._mic_timer.start()

    def _stop_mic(self):
        self._mic_on = False
        self._mic_timer.stop()
        self.level.segments.set_level(0)
        self.level.set_state("idle")
        src = self._mic
        if src is not None:
            try:
                run_bg(src.stop)
            except Exception:
                logger.debug("mic stop failed", exc_info=True)

    def _mic_tick(self):
        src = self._mic
        if src is None:
            return
        lv = getattr(src, "level", 0.0)
        lv = lv() if callable(lv) else lv
        try:
            lv = max(0.0, min(1.0, float(lv or 0.0)))
        except (TypeError, ValueError):
            lv = 0.0
        shown = min(1.0, (lv ** 0.5) * 1.3)
        self._mic_shown = max(shown, self._mic_shown * 0.85)
        self.level.segments.set_level(self._mic_shown)
        now = time.monotonic()
        if shown > 0.22:
            self._mic_heard_at = now
        if getattr(src, "error", "") and now - getattr(self, "_mic_started", now) > 0.3:
            self.level.set_state("error")
        elif self._mic_heard_at and now - self._mic_heard_at < 1.5:
            self.level.set_state("heard")
        else:
            self.level.set_state("quiet")

    # ── Speech & AI ──
    def _build_speech(self):
        sec, v = self._section("speech", "Speech & AI", gap=14)
        inner = QVBoxLayout()
        inner.setSpacing(30)
        # Two cards in one format: Speech (dictation and meetings) and the AI writer.
        self.speech_card = EngineCard("Speech", "", SPEECH_SOURCES_UI,
                                      kind=SPEECH_KIND, help_text=SPEECH_HELP, parent=sec)
        self.hw_trail = self.speech_card.trail
        self.speech_card.runs_on.changed.connect(self._speech_source_clicked)
        self.speech_card.combo.currentIndexChanged.connect(self._speech_model_picked)
        self.speech_card.action_clicked.connect(self._speech_action)
        self.gpu_card = GpuCard(self.speech_card)
        self.gpu_card.start.connect(self._gpu_start)
        self.gpu_card.cancel.connect(lambda: call(self.app, "cancel_gpu_download"))
        self.gpu_card.retry.connect(self._gpu_retry)
        self.gpu_card.remove.connect(self._gpu_remove)
        self.gpu_card.hide()
        self.speech_card.extra.addWidget(self.gpu_card)
        inner.addWidget(self.speech_card)

        self.writer_card = EngineCard("AI writer", "", WRITER_SOURCES_UI, kind=WRITER_KIND, help_text=WRITER_HELP, parent=sec)
        self.writer_card.runs_on.changed.connect(self._writer_source_clicked)
        self.writer_card.combo.currentIndexChanged.connect(self._writer_model_picked)
        self.writer_card.action_clicked.connect(self._writer_action)
        inner.addWidget(self.writer_card)
        self._speech_action_fn = self._writer_action_fn = None
        self._model_errors = {}
        self._progress = {}                   # model id -> the last percent heard (None = size unknown)
        # A "Runs on" picked but no model yet: the card shows that source's list with nothing
        # selected ("Select a model"); nothing changes until a model is picked.
        self._preview = {"speech": None, "writer": None}
        self._preview_truth = {"speech": None, "writer": None}   # what was set when the preview began
        self._key_then = None                 # "speech" / "writer": a key being added for that card

        kblk, kv = self._block("My API keys", parent=sec)
        self.keys_panel = ApiKeysPanel(kblk)
        self.keys_panel.test.connect(self._test_entry)
        self.keys_panel.test_saved.connect(self._test_saved_entry)
        self.keys_panel.saved.connect(self._entry_saved)
        self.keys_panel.removed.connect(self._remove_entry)
        self.keys_panel.cancelled.connect(lambda: setattr(self, "_key_then", None))
        kv.addWidget(self.keys_panel)
        self.keys_block = kblk
        inner.addWidget(kblk)
        v.addLayout(inner)
        self._layout_speech_columns()

    # model state helpers
    def _state(self, mid):
        dl = self._dl()
        if dl is None:
            return "unknown"
        try:
            return dl.state(mid)
        except Exception:
            return "unknown"

    def _downloaded_fn(self):
        dl = self._dl()
        if dl is None:
            return None
        return lambda m: self._state(m) == "ready"

    def _speech_now(self):
        try:
            return engine_choice.runs_on(self.app, "dictation")
        except Exception:
            logger.warning("runs_on(dictation) failed", exc_info=True)
            return engine_choice._local_speech("dictation", self.cfg.get("whisper_model") or "base")

    def _writer_now(self):
        try:
            return engine_choice.runs_on(self.app, "smart_actions", downloaded=self._downloaded_fn())
        except Exception:
            logger.warning("runs_on(smart_actions) failed", exc_info=True)
            return None

    def _recommended(self):
        dl = self._dl()
        rec = getattr(dl, "recommendation", None) if dl is not None else None
        if rec is None and self._hardware is not None:
            rec = getattr(self._hardware, "recommended", None)
        return getattr(rec, "model", None)

    def _refresh_models(self):
        """The two cards' model lists, download icons and status lines (after a download, a
        removal, the hardware scan)."""
        if "speech" not in self._built:
            return
        self._refresh_speech_choice()
        self._refresh_writer()

    def _model_progress(self, mid):
        """A running download's percent, or None (not running / size not known yet)."""
        if mid in self._progress:
            return self._progress[mid]
        dl = self._dl()
        if dl is None:
            return None
        if mid == hardware.HY_MODEL_ID:
            return hy_model.percent(self.app)
        try:
            p = dl.progress_of(mid)
            return int(p[0]) if p and (p[2] or p[0]) else None
        except Exception:
            return None

    def _speech_model_state(self, mid):
        """"ready" | "missing" | "downloading" | "failed" for a speech model (the Armenian one
        through hy_model). Unknown (no downloads yet): the model in use counts as here."""
        if mid == hardware.HY_MODEL_ID:
            st = hy_model.state(self.app)
            return "missing" if st == "hidden" else st
        st = self._state(mid)
        if st == "unknown":
            st = "ready" if mid == (self.cfg.get("whisper_model") or hardware.DEFAULT_SPEECH_MODEL) else "missing"
        return st

    def _writer_model_state(self, mid):
        st = self._state(mid)
        if st == "unknown":
            try:
                import local_llm
                st = "ready" if local_llm.model_downloaded(mid) else "missing"
            except Exception:
                st = "missing"
        return st

    def _mark_rows(self, card, state_of, info_of):
        box = card.combo
        for i in range(box.count()):
            if box.is_heading(i):
                continue
            value = str(box.itemData(i) or "")
            info = info_of(value)
            mark, note = "", ""
            if info is not None and state_of(value) in ("missing", "failed"):
                mark, note = MARK_DOWNLOAD, info.size
            box.set_choice_mark(i, mark, note)

    def _download_status(self, mid, info, st, ready_text):
        """(text, action, tone) for a local model's status line. No downloads service (an early
        start, a test stub): the line only, no button."""
        text, action, tone = self._download_line(mid, info, st, ready_text)
        return (text, action if self._dl() is not None else "", tone)

    def _download_line(self, mid, info, st, ready_text):
        if st != "downloading":
            self._progress.pop(mid, None)
        if st == "downloading":
            pct = self._model_progress(mid)
            text = "Downloading %s…%s" % (info.plain, (" %d%%" % pct) if pct is not None else "")
            return text, ("Cancel" if mid == hardware.HY_MODEL_ID else ""), "muted"
        if st == "failed":
            why = self._model_errors.get(mid)
            if not why and self._dl() is not None:
                try:
                    why = self._dl().error(mid)
                except Exception:
                    why = ""
            return why or "The download didn't finish.", "Try again", "muted"
        if st == "missing":
            return "Not on this computer yet · %s" % info.size, "Download", "muted"
        return ready_text, "Remove", "muted"

    def _hy_cancel(self):
        hy_model.cancel(self.app)
        self._pending_use.pop(hardware.HY_MODEL_ID, None)
        self._refresh_models()

    def _model_download(self, mid, use_as="speech"):
        dl = self._dl()
        if dl is None:
            return
        self._pending_use[mid] = use_as
        try:
            started = dl.start(mid)
        except Exception:
            logger.warning("Download %s failed to start", mid, exc_info=True)
            started = False
        if not started and self._state(mid) == "ready":
            self._pending_use.pop(mid, None)
            if use_as == "writer":
                self._writer_local(mid)
            else:
                self._model_use(mid)
            return
        self._refresh_models()
        self._refresh_writer()

    def _model_use(self, mid):
        """Speech runs on this computer with ``mid`` (picked, or just downloaded for it)."""
        self._apply_speech(mid)

    def _model_remove(self, mid):
        info = hardware.info(mid)
        real = info.real if info else mid
        detail = "%s · %s. You can download it again." % (info.plain, info.size) if info else ""
        if ask(self, "Remove %s?" % real, detail,
               [("cancel", "Cancel", "ghost"), ("remove", "Remove", "danger")]) != "remove":
            return
        dl = self._dl()
        if dl is not None:
            try:
                dl.remove(mid)
            except Exception:
                logger.warning("Remove %s failed", mid, exc_info=True)
        self._refresh_models()
        self._refresh_writer()

    def _on_removed(self, name, ok, msg):
        cfg = self.cfg
        dl = self._dl()
        if ok and name == cfg.get("whisper_model") and dl is not None:
            others = [m for m in dl.ready("whisper") if m != name]
            if others:
                apply_settings(self.app, {"whisper_model": others[0]})
        if ok and name == cfg.get("action_model") and dl is not None:
            others = [m for m in dl.ready("llm") if m != name]
            if others:
                res = engine_choice.set_writer(self.app, "local", model=others[0], downloaded=self._downloaded_fn())
                if res["ok"]:
                    apply_settings(self.app, res["changes"])
        if ok and name and name == str(cfg.get(SMART_SPEECH_KEY) or ""):
            # Smart Actions transcribed with it: back to Same as Dictation
            # (not a fresh download at the next Smart Action).
            apply_settings(self.app, {SMART_SPEECH_KEY: engine_choice.SMART_SPEECH_SAME})
        if ok and name and hardware.is_speech_id(name):
            # The Live Assistant and Audio to text picked it: back to Automatic, the same reason.
            if name == str(cfg.get(engine_choice.LIVE_SPEECH_KEY) or ""):
                apply_settings(self.app, {engine_choice.LIVE_SPEECH_KEY: engine_choice.LIVE_SPEECH_AUTO})
            if name == str(cfg.get(engine_choice.FILE_KEY) or ""):
                apply_settings(self.app, {engine_choice.FILE_KEY: engine_choice.FILE_AUTO})
        if not ok and msg and "speech" in self._built:
            self._model_errors[name] = msg            # the card's status line says it
        self._queue_refresh()

    def _on_finished(self, name, state, msg):
        use = self._pending_use.pop(name, None)
        if state == "ready" and use == "speech":
            self._model_use(name)
        elif state == "ready" and use == "writer":
            self._writer_local(name)
        self._queue_refresh()

    def _on_progress(self, name, pct, _done=0, total=0):
        if "speech" not in self._built:
            return
        # The status line of the card whose model downloads ("Downloading Balanced… 45%").
        # total 0 = the size isn't known yet: no percent, not "0%".
        self._progress[name] = None if (not total and not pct) else int(pct or 0)
        if name == (self.cfg.get("whisper_model") or "") or name in self._pending_use:
            self._speech_status()
        if name == engine_choice.local_writer(self.cfg) or name in self._pending_use:
            self._writer_status()

    # ── picking (both cards go through here) ──
    def _ask_privacy_off(self):
        return ask(self, "Turn off Privacy mode?", "Gusan Cloud and your own keys are off while it's on.",
                   [("cancel", "Cancel", "ghost"), ("off", "Turn off", "primary")]) == "off"

    def _apply_result(self, card, res, retry, upsell):
        """A set_dictation_speech / set_writer_value answer: apply it, or ask what it needs."""
        problem = res.get("problem")
        if res.get("ok"):
            self._preview[card] = None
            apply_settings(self.app, res["changes"])
        elif problem == "pro":
            call(self.app, "_pro_upsell", res.get("upsell") or upsell)
        elif problem == "privacy_mode":
            if self._ask_privacy_off():
                return retry()
        elif problem == "needs_key":
            self._key_then = card
            self.keys_panel.open_new("api_mistral" if card == "speech" else None)
            self._scroll_to_widget(self.keys_panel)
        self.refresh()
        return res

    def _apply_speech(self, value, turn_off_privacy=False):
        res = engine_choice.set_dictation_speech(self.app, value, downloaded=self._downloaded_fn(),
                                                 turn_off_privacy=turn_off_privacy)
        if res.get("problem") == "needs_download":
            self._model_download(value)
            return res
        return self._apply_result("speech", res, lambda: self._apply_speech(value, True),
                                  engine_choice.UPSELL_SPEECH)

    def _writer_local(self, mid, turn_off_privacy=False):
        res = engine_choice.set_writer(self.app, "local", model=mid, downloaded=self._downloaded_fn(),
                                       turn_off_privacy=turn_off_privacy)
        if res.get("problem") == "needs_download":
            if self._state(mid) == "downloading":
                self._pending_use[mid] = "writer"
            else:
                self._model_download(mid, use_as="writer")
            self.refresh()
            return res
        return self._apply_result("writer", res, lambda: self._writer_local(mid, True),
                                  engine_choice.UPSELL_WRITER)

    def _apply_writer(self, value, turn_off_privacy=False):
        choice, _ident, mid = engine_choice.parse_writer_value(value)
        if choice == "local":
            return self._writer_local(mid, turn_off_privacy)
        res = engine_choice.set_writer_value(self.app, value, downloaded=self._downloaded_fn(),
                                             turn_off_privacy=turn_off_privacy)
        return self._apply_result("writer", res, lambda: self._apply_writer(value, True),
                                  engine_choice.UPSELL_WRITER)

    # ── the two cards ──
    def _speech_source_clicked(self, i):
        """A "Runs on" segment: show that source's models, nothing chosen yet. Nothing changes until a
        model is picked (the segment of what is set just shows it again)."""
        src = self.speech_card.sources[i]
        truth = engine_choice.speech_choice(self.cfg)
        self._preview["speech"] = None if src == truth else src
        self._preview_truth["speech"] = truth
        self._refresh_speech_choice()

    def _speech_model_picked(self, _i):
        value = str(self.speech_card.combo.currentData() or "")
        self._refresh_speech_choice()           # what is really set, until the pick applies
        if not value:
            return
        if engine_choice.dictation_speech_source(value) == "local":
            st = self._speech_model_state(value)
            if st in ("missing", "failed"):
                if self._confirm_download(value):
                    self._model_download(value)
                return
            if st == "downloading":
                self._pending_use[value] = "speech"
                return
        self._apply_speech(value)

    def _confirm_download(self, mid):
        i = hardware.info(mid)
        if i is None:
            return False
        return ask(self, "Download %s?" % i.real, "%s · %s. It works offline." % (i.plain, i.size),
                   [("cancel", "Not now", "ghost"), ("go", "Download", "primary")]) == "go"

    def _speech_action(self):
        fn = self._speech_action_fn
        if fn is not None:
            fn()

    def _shown_source(self, card, truth):
        """The source the card shows: the one just clicked (nothing picked yet), unless it is what
        is set now, or the setting changed somewhere else since (then what is really set)."""
        shown = self._preview.get(card)
        if shown == truth or (shown and self._preview_truth.get(card) != truth):
            shown = self._preview[card] = None
        return shown or truth

    def _refresh_speech_choice(self):
        cfg = self.cfg
        card = self.speech_card
        truth = engine_choice.speech_choice(cfg)
        src = self._shown_source("speech", truth)
        card.set_source(src)
        rows = engine_choice.dictation_speech_rows(cfg, is_pro(self.app), source=src)
        value = engine_choice.dictation_speech_value(cfg) if src == truth else None
        if src == "local" and value and self._speech_model_state(value) in ("missing", "failed"):
            value = None          # not on this computer: "Select a model" - a pick asks to download it
        card.fill(rows, value)
        if src == "local":
            self._mark_rows(card, self._speech_model_state, hardware.info)
            rec = self._recommended()
            i = card.combo.findData(rec) if rec else -1
            if i >= 0 and hardware.info(rec) is not None:
                card.combo.setItemData(i, "%s · Best for this computer" % hardware.info(rec).plain, NOTE_ROLE)
        self._speech_status()

    def _preview_status(self, card, src, has_rows):
        """The line under a "Runs on" just picked (no model chosen yet)."""
        if src == "own_key" and not has_rows:
            return "", "Add key"
        return "", ""

    def _open_key_for(self, card):
        self._key_then = card
        self.keys_panel.open_new("api_mistral" if card == "speech" else None)
        self._scroll_to_widget(self.keys_panel)

    def _speech_status(self):
        cfg = self.cfg
        truth = engine_choice.speech_choice(cfg)
        src = self._preview.get("speech") or truth
        fn, action = None, ""
        if src != truth:
            text, action = self._preview_status("speech", src, self.speech_card.combo.count() > 0)
            if action:
                def fn():
                    self._open_key_for("speech")
        elif src == "cloud":
            text = ""
        elif src == "own_key":
            e = api_keys.entry(cfg, cfg.get("speech_key_id"))
            text = ""
            if e is not None:
                action = "Edit key"

                def fn():
                    self.keys_panel.open_edit(e, api_keys.entry_view(cfg, e))
                    self._scroll_to_widget(self.keys_panel)
        else:
            sel = cfg.get("whisper_model") or hardware.DEFAULT_SPEECH_MODEL
            info = hardware.info(sel)
            text = ""
            if info is not None:
                st = self._speech_model_state(sel)
                on_gpu = bool(self._hardware is not None and getattr(self._hardware, "on_gpu", False))
                ready = "On this computer · %s" % info.size
                if hardware.slower_here(sel, on_gpu):
                    ready += " · a little slower here"
                text, action, _tone = self._download_status(sel, info, st, ready)
                if st == "missing":
                    text, action = "", ""          # the box says "Select a model"
                if action == "Remove":
                    def fn():
                        self._model_remove(sel)
                elif action == "Cancel":
                    fn = self._hy_cancel
                elif action:
                    def fn():
                        self._model_download(sel)
            if cfg.get("privacy_mode"):
                text = (text + "\n" if text else "") + "Privacy mode is on."
        self._speech_action_fn = fn
        self.speech_card.set_status(text, action)

    def _writer_kind(self):
        kind = (self._writer_now() or {}).get("kind")
        return kind if kind in ("cloud", "own_key") else "local"

    def _writer_source_clicked(self, i):
        src = self.writer_card.sources[i]
        truth = self._writer_kind()
        self._preview["writer"] = None if src == truth else src
        self._preview_truth["writer"] = truth
        self._refresh_writer()

    def _writer_model_picked(self, _i):
        value = str(self.writer_card.combo.currentData() or "")
        self._refresh_writer()
        if not value:
            return
        choice, _ident, mid = engine_choice.parse_writer_value(value)
        if choice == "local":
            st = self._writer_model_state(mid)
            if st in ("missing", "failed"):
                i = hardware.LOCAL_AI_INFO.get(mid)
                if i is not None and ask(self, "Download %s?" % i.real, "%s · %s. It works offline." % (i.plain, i.size),
                                         [("cancel", "Not now", "ghost"), ("go", "Download", "primary")]) == "go":
                    self._model_download(mid, use_as="writer")
                return
            if st == "downloading":
                self._pending_use[mid] = "writer"
                return
        self._apply_writer(value)

    def _writer_action(self):
        fn = self._writer_action_fn
        if fn is not None:
            fn()

    def _refresh_writer(self):
        cfg = self.cfg
        card = self.writer_card
        kind = self._writer_kind()
        src = self._shown_source("writer", kind)
        card.set_source(src)
        # The row of what really writes (runs_on: a Pro default writes on Gusan Cloud too).
        value = None
        if src == kind == "cloud":
            fam = engine_choice.cloud_writer_family(cfg)
            value = "cloud" if fam == "deepseek" else "cloud:" + fam
        elif src == kind:
            value = engine_choice.local_writer(cfg) if kind == "local" else engine_choice.writer_value(cfg)
        if src == "local" and value and self._writer_model_state(value) in ("missing", "failed"):
            value = None          # not on this computer: "Select a model" - a pick asks to download it
        if src == kind == "local" and (self._writer_now() or {}).get("kind") != "local":
            value = None          # the built-in rules write: no model is in use, so none is shown chosen
        card.fill(engine_choice.writer_rows(cfg, is_pro(self.app), choice=src), value)
        if src == "local":
            self._mark_rows(card, self._writer_model_state, hardware.LOCAL_AI_INFO.get)
        self._writer_status()

    def _writer_status(self):
        cfg = self.cfg
        now = self._writer_now() or {}
        kind = now.get("kind")
        src = self._preview.get("writer")
        fn, action = None, ""
        if src:
            text, action = self._preview_status("writer", src, self.writer_card.combo.count() > 0)
            if action:
                def fn():
                    self._open_key_for("writer")
        elif cfg.get("privacy_mode") and engine_choice.writer_choice(cfg) != "local":
            text = PAUSED_TEXT + "."
        elif kind == "cloud":
            text = ""
        elif kind == "own_key":
            e = api_keys.entry(cfg, cfg.get("writer_key_id"))
            text = ""
            if e is not None:
                action = "Edit key"

                def fn():
                    self.keys_panel.open_edit(e, api_keys.entry_view(cfg, e))
                    self._scroll_to_widget(self.keys_panel)
        elif kind == "local":
            mid = engine_choice.local_writer(cfg)
            info = hardware.LOCAL_AI_INFO.get(mid)
            st = self._writer_model_state(mid)
            text, action, _tone = self._download_status(mid, info, st,
                                                        "On this computer · %s · works offline" % info.size)
            if st == "missing":
                text, action = "", ""              # the box says "Select a model"
            if action == "Remove":
                def fn():
                    self._model_remove(mid)
            elif action:
                def fn():
                    self._model_download(mid, use_as="writer")
        else:
            text = ""                              # no AI model yet: the box says "Select a model"
        self._writer_action_fn = fn
        self.writer_card.set_status(text, action)

    # ── My API keys ──
    def _refresh_keys(self):
        moved = api_keys.slot_migration(self.cfg)
        if moved:
            apply_settings(self.app, moved)
        cfg = self.cfg
        self.keys_panel.show_entries([(e, api_keys.entry_view(cfg, e)) for e in api_keys.entries(cfg)])

    def open_key_editor(self, field=None):
        """Open "Add key" (for a Mistral / Google key when ``field`` says so)."""
        self.ensure_built("speech")
        provider = {"mistral_api_key": "api_mistral", "google_api_key": "api_gemini"}.get(field)
        self.keys_panel.open_new(provider)
        self._scroll_to_widget(self.keys_panel)

    def _test_entry(self, provider, key, model, base, token):
        def done(res):
            ok, msg = res if isinstance(res, tuple) else (False, "")
            if alive(self.keys_panel):
                self.keys_panel.show_test(token, ok, msg)

        def failed(_e):
            done((False, "Couldn't check it right now."))
        run_bg(api_keys.test_entry, provider, key, model, base, then=done, error=failed, owner=self)

    def _test_saved_entry(self, key_id):
        e = api_keys.entry(self.cfg, key_id)
        if e is None:
            return
        self._test_entry(e["provider"], api_keys.secret_of(self.cfg, key_id), e.get("model") or "",
                         e.get("base_url") or "", ("row", key_id))

    def _entry_saved(self, data):
        key_id = data.get("id")
        try:
            if key_id:
                ok = api_keys.update_entry(self.app, key_id, data["name"], data["provider"], data["model"],
                                           key=data.get("key"), base_url=data.get("base_url", ""))
            else:
                key_id = api_keys.add_entry(self.app, data["name"], data["provider"], data["model"],
                                            data["key"], base_url=data.get("base_url", ""))
                ok = bool(key_id)
        except Exception:
            logger.warning("Saving an API key failed", exc_info=True)
            ok = False
        if not ok:
            self.keys_panel.editor.show_error("Couldn't save it.")
            return
        self.keys_panel.close_editor()
        then, self._key_then = self._key_then, None
        value = engine_choice.KEY_VALUE_PREFIX + key_id
        if then == "speech" and data["provider"] in engine_choice.SPEECH_KEY_PROVIDERS:
            self._apply_speech(value)               # the card asked for a key: use it now
            return
        if then == "writer":
            self._apply_writer(value)
            return
        self.refresh()

    def _remove_entry(self, key_id):
        e = api_keys.entry(self.cfg, key_id)
        if e is None:
            return
        name = api_keys.entry_view(self.cfg, e)["title"]
        if ask(self, "Remove “%s”?" % name, "A card that uses it stops using it. You can add it again later.",
               [("cancel", "Cancel", "ghost"), ("remove", "Remove", "danger")]) != "remove":
            return
        try:
            api_keys.remove_entry(self.app, key_id)
        except Exception:
            logger.warning("Removing an API key failed", exc_info=True)
        self.refresh()

    def unsaved_edits(self):
        """True while a key is typed into the open key editor and not saved yet. Installing an
        update closes Gusan: the controller (AppController._update_unsaved_edits) asks first."""
        panel = getattr(self, "keys_panel", None)           # (the Speech section may not be built yet)
        try:
            return bool(panel is not None and panel.has_unsaved_key())
        except RuntimeError:
            return False

    # GPU
    def _gpu_state(self):
        if "speech" not in self._built:
            return None
        snap = self._hardware
        if snap is None or IS_MAC:
            return None
        state = getattr(snap, "gpu_state", None)
        if state not in ("available", "ready", "failed"):
            return None
        phase = self._gpu.get("phase")
        if phase is None:
            fn = getattr(self.app, "gpu_phase", None)
            phase = call(self.app, "gpu_phase") if callable(fn) else None
        if phase in ("download", "verify"):
            return phase
        return state

    def _refresh_gpu(self):
        if "speech" not in self._built:
            return
        state = self._gpu_state()
        self.gpu_card.setVisible(state is not None)
        if state is not None:
            removable = state == "ready" and bool(call(self.app, "gpu_removable"))
            self.gpu_card.set_state(state, gpu=getattr(self._hardware, "gpu_chip", "") or
                                    getattr(self._hardware, "gpu_name", ""),
                                    pct=self._gpu.get("pct"), message=self._gpu.get("message", ""),
                                    removable=removable)

    def _gpu_start(self):
        if call(self.app, "start_gpu_download", "settings"):
            self._gpu.update(phase="download", pct=0, message="")
        self._refresh_gpu()

    def _gpu_retry(self):
        if call(self.app, "retry_gpu"):
            self._gpu.update(phase="verify", pct=-1, message="")
        self._refresh_gpu()

    def _gpu_remove(self):
        if ask(self, "Remove the graphics card speed-up?",
               "Frees about 736 MB. Speech goes back to the processor. You can download it again any time.",
               [("cancel", "Cancel", "ghost"), ("remove", "Remove", "danger")]) != "remove":
            return
        # The controller re-probes the hardware when it is done: the card
        # follows (hardware_ready) and shows Download again.
        call(self.app, "remove_gpu_download")

    def _on_gpu_progress(self, pct, _done=0.0, _total=0.0):
        self._gpu["phase"] = "verify" if pct is not None and pct < 0 else "download"
        self._gpu["pct"] = pct
        self._refresh_gpu()

    def _on_gpu_done(self, state, message=""):
        # A failed download leaves the card on "Download": say why it is back
        # there (the controller's message can be technical, so a plain one).
        # A graphics card that couldn't run shows its own "couldn't start" card.
        self._gpu.update(phase=None, pct=None,
                         message="The download stopped. Try again." if state == "failed" else "")
        dl = self._dl()
        if dl is not None:
            call(dl, "probe_hardware", None, True)
        self._refresh_gpu()

    # ── Privacy ──
    def _build_privacy(self):
        sec, v = self._section("privacy", "Privacy")
        inner = QVBoxLayout()
        inner.setSpacing(12)
        hero = Card(pad=(24, 20))
        hrow = QHBoxLayout()
        hrow.setSpacing(18)
        hrow.addWidget(icon_label("shield", 32, "blue", sw=1.6, parent=hero), 0, Qt.AlignVCenter)
        hcol = QVBoxLayout()
        hcol.setSpacing(2)
        t = text_label("Privacy mode", 20, 600, role="h2", display=True, parent=hero)
        t.setFixedHeight(26)
        hcol.addWidget(t)
        hcol.addWidget(text_label("Everything stays on this computer. Gusan Cloud is off.", 14, role="body",
                                  parent=hero, wrap=True))
        hrow.addLayout(hcol, 1)
        self.t_privacy = BigToggle("Privacy mode", bool(self.cfg.get("privacy_mode")), hero)
        self.t_privacy._cfg = ("privacy_mode", False)
        self.t_privacy.clicked.connect(lambda on: (self._set("privacy_mode", bool(on)), self.refresh()))
        hrow.addWidget(self.t_privacy, 0, Qt.AlignVCenter)
        hero.add(hrow)
        inner.addWidget(hero)

        self.t_history = self._toggle("save_history", "Save my history", True)
        self.btn_delete = red_button("Delete…")
        self.btn_delete.setAccessibleName("Delete all history")
        self.btn_delete.clicked.connect(lambda _=False: self._delete_history())
        self.t_stats = self._toggle("analytics_enabled", "Share usage statistics", True)
        self.t_bar = self._toggle("overlay_private", "Hide the dictation bar from screen shares", True)
        self.t_card = self._toggle("live_assist_private", "Hide Live Assistant from screen shares", True)
        hide = QWidget()
        hl = QHBoxLayout(hide)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(20)
        for tg, text in ((self.t_bar, "Dictation bar"), (self.t_card, "Live Assistant")):
            pair = QHBoxLayout()
            pair.setSpacing(8)
            pair.addWidget(tg, 0, Qt.AlignVCenter)
            pair.addWidget(text_label(text, 13, role="body", parent=hide), 0, Qt.AlignVCenter)
            hl.addLayout(pair)
        self.row_history = self._row("Save my history", self.t_history, hint="Kept on this computer.")
        self.row_delete = self._row("Delete all history", self.btn_delete, hint="Can’t be undone.")
        self.row_stats = self._row("Share usage statistics", self.t_stats)
        self._collect_open = False
        self._set_stats_hint()
        self.row_stats.hint_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.row_stats.hint_label.setOpenExternalLinks(False)
        self.row_stats.hint_label.linkActivated.connect(lambda _l: self._toggle_collect())
        self.row_hide = self._row("Hide from screen shares", hide, hint=self._hide_hint())
        self._prepend_icon(self.row_hide, "incognito", "ok-tx")
        inner.addWidget(self._group([self.row_history, self.row_delete, self.row_stats, self.row_hide]))
        v.addLayout(inner)

    def _prepend_icon(self, row, icon, color):
        try:
            head = row.layout().itemAt(0).layout()
            lrow = head.itemAt(0).layout()
            lrow.insertWidget(0, icon_label(icon, 17, color, parent=row), 0, Qt.AlignVCenter)
        except Exception:
            logger.debug("row icon failed", exc_info=True)

    def _set_stats_hint(self):
        link = theme.qss_color("blue-tx")
        if self._collect_open:
            text = ('%s <a href="#collect" style="color: %s; font-weight: 600; text-decoration: none">Hide</a>'
                    % (WHAT_WE_COLLECT, link))
        else:
            text = ('Anonymous. <a href="#collect" style="color: %s; font-weight: 600; text-decoration: none">'
                    'What we collect</a>' % link)
        self.row_stats.set_hint(text)

    def _toggle_collect(self):
        self._collect_open = not self._collect_open
        self._set_stats_hint()

    def _hide_hint(self):
        try:
            import live_context
            st = live_context.hide_status(True)
            if st.key == "on":
                return "People on the call won’t see them."
            return st.headline
        except Exception:
            return "People on the call won’t see them."

    def _delete_history(self):
        keep = history_actions.active_meeting_dir(self.app)
        self.btn_delete.setEnabled(False)
        run_bg(_count_history, keep, then=lambda n: self._confirm_delete(n, keep),
               error=lambda e: self._history_done(None), owner=self)

    def _confirm_delete(self, n, keep):
        self.btn_delete.setEnabled(True)
        if not any(n.get(k) for k in ("dictations", "meetings", "transcripts")):
            self._flash_hint(self.row_delete, history_actions.EMPTY_TEXT, "Can’t be undone.")
            return
        buttons = [("cancel", "Cancel", "ghost")]
        if n.get("dictations") and (n.get("meetings") or n.get("transcripts")):
            buttons.append(("dictations", "Only dictations", "secondary"))
        buttons.append(("all", "Delete everything", "danger"))
        choice = ask(self, "Delete all history?", "Permanently delete %s? This can’t be undone." % describe_all(n),
                     buttons)
        if choice not in ("all", "dictations"):
            return
        self.btn_delete.setEnabled(False)
        track_fn = getattr(self.app, "track", None)
        run_bg(_clear_history, choice == "all", keep, track_fn, then=self._history_done,
               error=lambda e: self._history_done(None), owner=self)

    def _history_done(self, res):
        self.btn_delete.setEnabled(True)
        if res is None:
            self._flash_hint(self.row_delete, "Something went wrong. Try again.", "Can’t be undone.")
            return
        self._flash_hint(self.row_delete, clear_result_text(res), "Can’t be undone.")
        sig = getattr(self.app, "sig_history_changed", None)
        if sig is not None and hasattr(sig, "emit"):
            try:
                sig.emit()
            except Exception:
                logger.debug("sig_history_changed failed", exc_info=True)

    def _flash_hint(self, row, text, back, ms=6000):
        row.set_hint(text)
        QTimer.singleShot(ms, lambda: alive(row) and row.set_hint(back))

    # ── Updates & about ──
    def _build_updates(self):
        sec, v = self._section("updates", "Updates & about")
        rows = []
        ver = QFrame()
        ver.setProperty("gusan", "row")
        ver.setProperty("last", False)
        vl = QHBoxLayout(ver)
        vl.setContentsMargins(0, 16, 0, 16)
        vl.setSpacing(14)
        logo = QLabel(ver)
        logo.setPixmap(icons.logo_pixmap(40))
        logo.setFixedSize(40, 40)
        logo.setAccessibleName(APP_NAME)
        vl.addWidget(logo, 0, Qt.AlignVCenter)
        vcol = QVBoxLayout()
        vcol.setSpacing(0)
        self.version_label = text_label("%s %s" % (APP_NAME, APP_VERSION), 18, 600, role="h2", display=True,
                                        parent=ver)
        self.version_label.setFixedHeight(24)
        vcol.addWidget(self.version_label)
        srow = QHBoxLayout()
        srow.setSpacing(6)
        self.update_dot = _Dot("ok", 7, ver)
        srow.addWidget(self.update_dot, 0, Qt.AlignVCenter)
        self.update_status = text_label("Up to date", 12.5, 600, color="ok-tx", parent=ver)
        self.update_status.setMinimumHeight(18)
        self.update_status.setAccessibleName("Update status")
        srow.addWidget(self.update_status, 0, Qt.AlignVCenter)
        srow.addStretch(1)
        vcol.addLayout(srow)
        vl.addLayout(vcol, 1)
        self.update_button = Button("Check now", "secondary", "s", icon="refresh", parent=ver)
        self.update_button.clicked.connect(lambda _=False: self._update_clicked())
        vl.addWidget(self.update_button, 0, Qt.AlignVCenter)
        self.version_row = ver
        rows.append(ver)
        self.t_install = self._toggle("install_updates_on_quit", "Install updates when I quit", True)
        rows.append(self._row("Install updates when I quit", self.t_install))
        chips = QWidget()
        cl = QHBoxLayout(chips)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        self.chips_lay = cl
        self.hw_chips = []
        self.row_computer = self._row("This computer", chips)
        rows.append(self.row_computer)
        fb = Button("Send feedback", "secondary", "s", icon="bubble")
        fb.clicked.connect(lambda _=False: self._feedback())
        self.feedback_button = fb
        rows.append(self._row("Send feedback", fb, hint="Ideas, bugs, a language you need."))
        # The credit NVIDIA's licence asks for (CC BY 4.0), shown while the Armenian model is
        # listed anywhere in the app (speech_hy.listed): on this computer, or ready to download.
        credit = speech_hy.credit()
        cbtn = Button(credit["name"], "link", "s")
        cbtn.setAccessibleName("Open the page of %s" % credit["name"])
        cbtn.clicked.connect(lambda _=False, u=credit["url"]: open_url(u))
        self.credit_button = cbtn
        self.row_credit = self._row("Armenian speech model", cbtn,
                                    hint="By NVIDIA, license %s. Converted for Gusan; not made or endorsed by NVIDIA."
                                         % credit["license"])
        self.row_credit.setVisible(bool(speech_hy.listed()))
        rows.append(self.row_credit)
        links = QFrame()
        links.setProperty("gusan", "row")
        links.setProperty("last", True)
        ll = QHBoxLayout(links)
        ll.setContentsMargins(0, 14, 0, 14)
        ll.setSpacing(8)
        self.about_links = {}
        items = (("Website", WEBSITE_URL), ("LinkedIn", LINKEDIN_URL), ("Source code", updater.PROJECT_GITHUB_URL),
                 ("Terms", account_actions.TERMS_URL), ("Privacy", account_actions.PRIVACY_URL))
        for i, (text, url) in enumerate(items):
            if i:
                sep = text_label("·", 13, color="faint", parent=links)
                sep.setAccessibleName("")
                ll.addWidget(sep, 0, Qt.AlignVCenter)
            b = Button(text, "link", "s", parent=links)
            b.setAccessibleName("Open %s" % ("the privacy policy" if text == "Privacy" else text))
            b.clicked.connect(lambda _=False, u=url: open_url(u))
            ll.addWidget(b, 0, Qt.AlignVCenter)
            self.about_links[text] = b
        ll.addStretch(1)
        ll.addWidget(text_label("Part of the", 12.5, role="muted", parent=links), 0, Qt.AlignVCenter)
        community = Button("AIBUBEN community", "link", "s", parent=links)
        community.setAccessibleName("Open the AIBUBEN community website")
        community.clicked.connect(lambda _=False: open_url(AIBUBEN_URL))
        ll.addWidget(community, 0, Qt.AlignVCenter)
        self.about_links["AIBUBEN community"] = community
        rows.append(links)
        card = Card(pad=(20, 0), gap=0)
        for r in rows:
            card.add(r)
        v.addWidget(card)
        self._show_hardware_chips()

    def _initial_update_state(self):
        pending = str(self.cfg.get("pending_update_version") or "")
        if pending and updater.is_newer(pending):
            return updater.AVAILABLE, pending
        return updater.LATEST, ""

    def _show_update(self, status, tag=""):
        self._update = (status, tag)
        _title, text = updater.status_line(status, tag)
        self.update_status.setText(text or "")
        color = {updater.LATEST: ("ok", "ok-tx"), updater.AVAILABLE: ("blue", "blue-tx"),
                 updater.DISMISSED: ("blue", "blue-tx"), updater.ERROR: ("amber", "amber-tx"),
                 "checking": ("faint", "muted"), "installing": ("blue", "blue-tx")}.get(status, ("faint", "muted"))
        if status == "installing":
            self.update_status.setText("Updating %s…" % tag)
        self.update_dot.set_color(color[0])
        tint(self.update_status, color[1])
        self.update_status.setAccessibleDescription(self.update_status.text())
        b = self.update_button
        if status in (updater.AVAILABLE, updater.DISMISSED):
            b.setText("Install update")
            b.setIcon(icons.icon("download", 16, "text2"))
            b.setAccessibleName("Install update %s" % tag)
            b.setEnabled(True)
        elif status == "checking":
            b.setText("Checking…")
            b.setEnabled(False)
        elif status == "installing":
            b.setText("Updating…")
            b.setEnabled(False)
        else:
            b.setText("Check now")
            b.setIcon(icons.icon("refresh", 16, "text2"))
            b.setAccessibleName("Check for updates now")
            b.setEnabled(True)
        b.updateGeometry()

    def _update_clicked(self):
        status, tag = self._update or (updater.LATEST, "")
        if status in (updater.AVAILABLE, updater.DISMISSED) and tag:
            self._install_update(tag)
        else:
            self._check_updates()

    def _check_updates(self):
        self._show_update("checking")
        run_bg(updater.check_latest, dict(self.cfg), manual=True, then=self._checked,
               error=lambda e: self._checked((updater.ERROR, "")), owner=self)

    def _checked(self, res):
        status, tag = res if isinstance(res, tuple) else (updater.ERROR, "")
        track(self.app, "update_check_result", updater.check_props(status, manual=True))
        if callable(getattr(self.app, "note_update_check", None)):
            call(self.app, "note_update_check", status, tag)
        self._show_update(status, tag)

    def _install_update(self, tag):
        track(self.app, "update_install_started", {"manual": True, "to": tag})
        if callable(getattr(self.app, "install_update", None)):
            # The controller installs, tracks and quits; it calls back only
            # when it didn't work, so the row can say why.
            self._show_update("installing", tag)

            def _failed(_ok, info, tag=tag):
                if alive(self):
                    self._install_failed(tag, info)
            call(self.app, "install_update", tag, on_done=_failed)
            return
        self._show_update("installing", tag)
        run_bg(updater.install, tag, then=lambda r: self._installed(tag, r),
               error=lambda e: self._installed(tag, (False, "Couldn't install the update.")), owner=self)

    def _install_failed(self, tag, info):
        """app.install_update didn't install (it tracked it; on macOS it
        opened the release page)."""
        self._show_update(updater.AVAILABLE, tag)
        if info and info != updater.UNSUPPORTED:
            self.update_status.setText(info)
            tint(self.update_status, "red-tx")

    def _installed(self, tag, res):
        ok, info = res if isinstance(res, tuple) else (False, "")
        track(self.app, "update_install_result", {"manual": True, "ok": bool(ok)})
        if ok:
            sig = getattr(self.app, "sig_quit", None)
            if sig is not None and hasattr(sig, "emit"):
                sig.emit()
            return
        if info == updater.UNSUPPORTED:
            open_url(updater.release_page(tag))
            self._show_update(updater.AVAILABLE, tag)
            return
        self._show_update(updater.AVAILABLE, tag)
        self.update_status.setText(info or "Couldn't install the update.")
        tint(self.update_status, "red-tx")

    def _feedback(self):
        dlg = FeedbackDialog(self, self.app)
        self._feedback_dialog = dlg
        dlg.exec()
        dlg.deleteLater()
        self._feedback_dialog = None

    # hardware
    def _ask_hardware(self):
        dl = self._dl()
        snap = getattr(dl, "hardware", None) if dl is not None else None
        if snap is not None:
            self._on_hardware(snap)
            return
        if self._hw_asked:
            return
        self._hw_asked = True
        if dl is not None and callable(getattr(dl, "probe_hardware", None)):
            try:
                snap = dl.probe_hardware()
            except Exception:
                snap = None
            if snap is not None:
                self._on_hardware(snap)
            return
        cfg = dict(self.cfg)
        run_bg(hardware.snapshot, cfg, False, then=self._on_hardware, owner=self)

    def _on_hardware(self, snap):
        if snap is None:
            return
        self._hardware = snap
        if "speech" in self._built:
            short = hardware.short_cpu_name(getattr(snap, "cpu_name", None))
            ram = getattr(snap, "ram_display_gb", 0)
            parts = [p for p in (short, ("%d GB" % ram) if ram else "") if p]
            self.hw_trail.setText(("This computer: " + " · ".join(parts)) if parts else "")
            self._refresh_models()
            self._refresh_gpu()
        if "updates" in self._built:
            self._show_hardware_chips()

    def _show_hardware_chips(self):
        clear_layout(self.chips_lay)
        snap = self._hardware
        if snap is None:
            self.chips_lay.addWidget(text_label("Checking…", 12.5, role="muted"))
            return
        chips = [(getattr(snap, "cpu_chip", ""), "chip"), (getattr(snap, "ram_chip", ""), "memory"),
                 (getattr(snap, "gpu_chip", ""), "screen")]
        for text, icon in chips:
            if text:
                chip = Chip(text, "neutral", icon)
                if self._narrow and icon == "screen":
                    # "NVIDIA GeForce RTX 4060 Laptop GPU" would push the row past a narrow page.
                    from PySide6.QtGui import QFontMetrics
                    lb = getattr(chip, "_text", None)
                    if lb is not None:
                        short = QFontMetrics(lb.font()).elidedText(text, Qt.ElideRight, 150)
                        if short != text:
                            lb.setText(short)
                            chip.setToolTip(text)
                chip.setAccessibleName(text)
                self.chips_lay.addWidget(chip)

    # ── narrow windows ──
    # The boards are 1280 px wide. Below that (down to the shell's 960 px) the
    # section list makes room first, then Speech runs on / AI writer stack.
    NAV_ROOM = 196 + 32              # the section list and its gap
    EDGES = 32 + 32                  # left margin; right margin + scrollbar
    MIN_CONTENT = 664                # the narrowest content column with the list shown
    TWO_COLUMNS = 700                # the two engine columns side by side from here

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_width()

    def _apply_width(self):
        w = self.width()
        nav = w - self.EDGES - self.NAV_ROOM >= self.MIN_CONTENT
        if nav == self.nav.isHidden():
            self.nav.setVisible(nav)
        content = w - self.EDGES - (self.NAV_ROOM if nav else 0)
        narrow = content < self.TWO_COLUMNS
        if narrow != self._narrow:
            self._narrow = narrow
            self._layout_speech_columns()
            if "updates" in self._built:
                self._show_hardware_chips()

    def is_narrow(self):
        return self._narrow

    def _layout_speech_columns(self):
        two = getattr(self, "speech_two", None)
        if two is None:
            return
        two.setDirection(QBoxLayout.TopToBottom if self._narrow else QBoxLayout.LeftToRight)
        two.setSpacing(30 if self._narrow else 20)

    # services
    def _dl_slots(self):
        return (("state_changed", self._on_dl_state), ("progress", self._on_progress),
                ("finished", self._on_finished), ("removed", self._on_removed),
                ("hardware_ready", self._on_hardware), ("scanned", self._on_dl_state),
                ("catalog_changed", self._on_dl_state))

    def _connect_services(self):
        """Connect app.downloads and the GPU signals - once per object. Runs at
        build and on every open, so a service the controller creates after
        the window (Wave 4) is still heard; a replaced downloads object is
        let go. Bound methods only: Qt drops them when the page is deleted."""
        dl = self._dl()
        old = getattr(self, "_dl_bound", None)
        if dl is not old:
            if old is not None:
                for sig, slot in self._dl_slots():
                    try:
                        getattr(old, sig).disconnect(slot)
                    except Exception:
                        pass
            self._dl_bound = None
            if dl is not None:
                for sig, slot in self._dl_slots():
                    s = getattr(dl, sig, None)
                    if s is not None and hasattr(s, "connect"):
                        try:
                            s.connect(slot)
                            self._services.add(sig)
                        except Exception:
                            logger.debug("connect %s failed", sig, exc_info=True)
                self._dl_bound = dl
                self._hw_asked = False          # the new manager may know the hardware
        for sig, slot in (("sig_gpu_progress", self._on_gpu_progress), ("sig_gpu_done", self._on_gpu_done)):
            if sig in self._services:
                continue
            s = getattr(self.app, sig, None)
            if s is not None and hasattr(s, "connect"):
                try:
                    s.connect(slot)
                    self._services.add(sig)
                except Exception:
                    logger.debug("connect %s failed", sig, exc_info=True)

    def _on_dl_state(self, *_args):
        self._queue_refresh()

    # ── refresh / hooks ──
    def refresh(self):
        """Show the config as it is now (cheap: no disk, no network)."""
        cfg = self.cfg
        supported, _enabled = autostart_state()
        if supported:
            self._sync_login()
        values = [k for k, _t in APP_LOOKS]
        cur = cfg.get("app_theme", "system")
        if self.seg_look.index() != (values.index(cur) if cur in values else 0):
            self.seg_look.set_index(values.index(cur) if cur in values else 0)
        self._sync_toggle(self.t_notify)
        for r in self.shortcut_rows:
            r.refresh(cfg)
        self._sync_mic_combo()
        if "speech" in self._built:
            self._refresh_speech_all()
        if "privacy" in self._built:
            self._refresh_privacy()
        if "updates" in self._built:
            self._refresh_updates()

    def _refresh_speech_all(self):
        self._refresh_models()
        self._refresh_speech_choice()
        self._refresh_writer()
        self._refresh_keys()
        self._refresh_gpu()

    def _refresh_privacy(self):
        for t in (self.t_history, self.t_stats, self.t_bar, self.t_card, self.t_privacy):
            self._sync_toggle(t)

    def _refresh_updates(self):
        self._sync_toggle(self.t_install)
        credit_row = getattr(self, "row_credit", None)
        if credit_row is not None:
            credit_row.setVisible(bool(speech_hy.listed()))
        # Before any "Check now": what the startup check left in the config.
        st, tag = self._initial_update_state()
        if self._update is None or (self._update[0] == updater.LATEST and st == updater.AVAILABLE):
            self._show_update(st, tag)

    def on_open(self, **opts):
        self.opts = dict(opts)
        self._connect_services()
        hy_model.refresh_catalog(self.app)  # a worker: once a day at most, never in Privacy mode
        self._queue_refresh()               # catch up with anything changed while hidden
        if self.dictionary is not None:
            self.dictionary.on_open()       # its lists and the "now spelled right" proof
        sid = opts.get("section")
        focus = opts.get("focus")
        if sid or focus:
            self.show_section(sid or "general", focus=focus)
        self._ask_hardware()

    def on_setting_changed(self, key):
        if key in _WATCHED:
            self._queue_refresh()
        if self.dictionary is not None:
            self.dictionary.on_setting_changed(key)       # words / replacements / removals / cloud share

    def refresh_account(self):
        name = self._account_name()
        self.nav.account_link.avatar.set_name(name, guest=self._account_guest())
        self._queue_refresh()

    # ── sections and scrolling ──
    def section_widget(self, sid):
        return self._sections.get(_SECTION_ALIASES.get(sid, sid))

    def current_section(self):
        return self.nav.active()

    def show_section(self, sid, focus=None):
        sid = _SECTION_ALIASES.get(sid, sid)
        if sid not in SECTION_IDS:
            sid = "general"
        if focus in ("gpu", "keys", "api_keys", "models", "writer", "history", "mic") and sid == "general":
            sid = {"history": "privacy", "mic": "microphone"}.get(focus, "speech")
        self.ensure_built(sid)
        self._pending = (sid, focus)
        self.nav.set_active(sid)
        if self.isVisible():
            self._apply_pending()
        QTimer.singleShot(0, self._apply_pending)

    def _apply_pending(self):
        if not self._pending or not alive(self):
            return
        if not self.isVisible():
            return
        sid, focus = self._pending
        self._pending = None
        self._fit_tail()
        self.content.layout().activate()
        target = None
        if "speech" in self._built:
            target = {"gpu": self.gpu_card, "keys": self.keys_block, "api_keys": self.keys_block,
                      "models": self.speech_card, "writer": self.writer_card}.get(focus)
        self._jump_target = (sid, target)
        self._jump_to()
        # Freshly built sections can still grow while they are polished and
        # laid out: aim again until it settles (unless the user scrolls).
        for ms in (0, 60, 200):
            QTimer.singleShot(ms, self._settle_jump)
        self._update_mic()

    def _settle_layout(self):
        """Run the pending layout passes now, so the scroll range already
        includes sections that were just built."""
        for _ in range(3):
            QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)

    def _jump_to(self):
        self._settle_layout()
        sid, target = self._jump_target
        if target is not None and target.isVisible():
            y = max(0, target.mapTo(self.content, target.rect().topLeft()).y() - 16)
        else:
            y = self._sections[sid].y()
        bar = self.scroll.verticalScrollBar()
        value = min(y, bar.maximum())
        self._jump = (sid, value)
        if bar.value() != value:
            bar.setValue(value)
        self.nav.set_active(sid)

    def _settle_jump(self):
        if not alive(self) or self._jump is None or not getattr(self, "_jump_target", None):
            return
        if self.scroll.verticalScrollBar().value() != self._jump[1]:
            return                              # the user scrolled meanwhile
        self._fit_tail()
        self.content.layout().activate()
        self._jump_to()

    def _scroll_to_widget(self, widget):
        if not self.isVisible():
            return
        self.content.layout().activate()
        top = widget.mapTo(self.content, widget.rect().topLeft()).y()
        bar = self.scroll.verticalScrollBar()
        vp = self.scroll.viewport().height()
        if top < bar.value() or top + min(widget.height(), vp) > bar.value() + vp:
            bar.setValue(min(bar.maximum(), max(0, top - 24)))

    def _on_scrolled(self, value):
        if self._jump is not None and value == self._jump[1]:
            self.nav.set_active(self._jump[0])
        else:
            self._jump = None
            current = SECTION_IDS[0]
            for sid in SECTION_IDS:
                if sid in self._sections and self._sections[sid].y() <= value + 40:
                    current = sid
            self.nav.set_active(current)
            if self._lazy and value + 2 * self.scroll.viewport().height() > self.content.height():
                self.ensure_built()          # scrolling down faster than the idle build
        self._update_mic()

    def showEvent(self, e):
        super().showEvent(e)
        win = self.window()
        if win is not None and win is not self and win is not getattr(self, "_watched_window", None):
            win.installEventFilter(self)
            self._watched_window = win
        if self._pending:
            QTimer.singleShot(0, self._apply_pending)
        QTimer.singleShot(0, self._update_mic)

    def hideEvent(self, e):
        super().hideEvent(e)
        if self._mic_on:
            self._stop_mic()
        self._devices_loaded = False        # list the microphones again next time (one may be plugged in)


def create(main_app):
    return SettingsPage(main_app)
