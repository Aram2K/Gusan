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
   default "alt+s", "" = none set; SMART_SHORTCUT_KEYS) and
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
from PySide6.QtWidgets import (QAbstractButton, QBoxLayout, QComboBox, QDialog, QFrame, QGraphicsDropShadowEffect,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

import account_actions
import api_keys
import engine_choice
import hardware
import history_actions
import speech_hy
import updater
from app_info import APP_NAME, APP_VERSION
from ui import hy_model, theme
from ui.components import (Avatar, Button, Card, Chip, Divider, IconButton, IconTile, Keycap, NavItem,
                           PageHeader, Segmented, SettingRow, Toggle, icons, label, mini_prompt)
from ui.components.badges import ProTag
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
    (("hotkey",), "mic", "Dictate", "dictation", "Dictation", "alt+r"),
    (SMART_SHORTCUT_KEYS, "wand", "Smart Actions", "smart_actions", "Smart Actions", "alt+s"),
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
class _Bars(QWidget):
    def __init__(self, level, n=5, parent=None):
        super().__init__(parent)
        self._level, self._n = int(level), int(n)
        self.setFixedSize(n * 14 + (n - 1) * 3, 6)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        for i in range(self._n):
            p.setBrush(theme.qcolor("blue-l" if i < self._level else "line"))
            p.drawRoundedRect(QRectF(i * 17, 0, 14, 6), 3, 3)
        p.end()


class Meter(QWidget):
    """'Speed ▮▮▮▮▯' - readable by screen readers as 'Speed: 4 of 5'."""

    def __init__(self, name, level, parent=None):
        super().__init__(parent)
        self.setAccessibleName("%s: %d of 5" % (name, level))
        self.setFixedHeight(20)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lb = text_label(name, 12, color="muted", parent=self)
        lb.setFixedWidth(64)
        lay.addWidget(lb, 0, Qt.AlignVCenter)
        lay.addWidget(_Bars(level, parent=self), 0, Qt.AlignVCenter)
        lay.addStretch(1)


class Ribbon(QWidget):
    """The blue 'Best for your computer' ribbon that sits on a card's top edge."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self._text = text
        self._font = theme.font(11.5, 700)
        from PySide6.QtGui import QFontMetrics
        tw = QFontMetrics(self._font).horizontalAdvance(text)
        self.setFixedSize(10 + 12 + 5 + tw + 11, 22)
        self.setAccessibleName(text)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(6)
        eff.setOffset(0, 2)
        eff.setColor(theme.qcolor("rgba(15,23,42,0.08)"))
        self.setGraphicsEffect(eff)

    def text(self):
        return self._text

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(theme.qcolor("blue-h"), 1))
        p.setBrush(theme.qcolor("blue"))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.drawPixmap(10, 5, icons.pixmap("check", 12, "#ffffff", sw=2.4))
        p.setPen(QColor("#ffffff"))
        p.setFont(self._font)
        p.drawText(QRectF(27, 0, self.width() - 27, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self._text)
        p.end()


class CardsRow(QWidget):
    """A row of cards with room above for the recommendation ribbon."""

    def __init__(self, gap=12, top=12, parent=None):
        super().__init__(parent)
        self._top = top
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(0, top, 0, 0)
        self.lay.setSpacing(gap)
        self.ribbon = Ribbon("Best for your computer", self)
        self.ribbon.hide()
        self._target = None

    def add(self, w):
        self.lay.addWidget(w, 1)
        w.installEventFilter(self)
        return w

    def set_recommended(self, widget, room=None):
        self._target = widget
        if room is not None:
            self.lay.setContentsMargins(0, self._top if room else 0, 0, 0)
        self.ribbon.setVisible(widget is not None)
        self._place()

    def recommended(self):
        return self._target

    def eventFilter(self, obj, ev):
        if obj is self._target and ev.type() in (QEvent.Move, QEvent.Resize, QEvent.Show):
            self._place()
        return False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place()

    def _place(self):
        t = self._target
        if t is None:
            return
        self.ribbon.move(t.x() + 14, max(0, t.y() - 12))
        self.ribbon.raise_()


class ModelCard(QFrame):
    """A speech model card: plain name big, the real Whisper model and size
    under it, speed/accuracy bars, and one action at the bottom."""
    download = Signal(str)
    use = Signal(str)
    remove = Signal(str)

    def __init__(self, mid, parent=None):
        super().__init__(parent)
        self.mid = mid
        self.info = hardware.MODEL_INFO[mid]
        self._selected = False
        self._key = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self.setAccessibleName("%s, %s, %s" % (self.info.plain, self.info.real, self.info.size))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 20, 16, 16)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.name_label = text_label(self.info.plain, 19, 600, role="h2", display=True, parent=self)
        self.name_label.setFixedHeight(26)
        top.addWidget(self.name_label, 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.mark = icon_label("check_circle", 20, "blue", sw=2, parent=self)
        self.mark.setAccessibleName("Selected")
        top.addWidget(self.mark, 0, Qt.AlignVCenter)
        lay.addLayout(top)
        # Wraps in a narrow window instead of widening the card past the page.
        # No-break spaces keep "1.6 GB" whole and the dot on the first line.
        self.model_label = text_label("%s · %s" % (self.info.real, self.info.size.replace(" ", " ")),
                                      12.5, role="model", parent=self, wrap=True)
        lay.addWidget(self.model_label)
        meters = QVBoxLayout()
        meters.setContentsMargins(0, 12, 0, 0)
        meters.setSpacing(2)
        meters.addWidget(Meter("Speed", self.info.speed, self))
        meters.addWidget(Meter("Accuracy", self.info.accuracy, self))
        lay.addLayout(meters)
        self.note = text_label("", 12, role="hint", parent=self, wrap=True)
        self.note.hide()
        lay.addWidget(self.note)
        lay.addStretch(1)
        self.action = QWidget(self)
        self.action_lay = QHBoxLayout(self.action)
        self.action_lay.setContentsMargins(0, 14, 0, 0)
        self.action_lay.setSpacing(6)
        lay.addWidget(self.action)
        self.progress = None
        self.progress_text = None
        self.buttons = {}
        self._apply_selected(False)

    def _apply_selected(self, on):
        self._selected = bool(on)
        self.mark.setVisible(self._selected)
        if self._selected:
            eff = QGraphicsDropShadowEffect(self)
            eff.setBlurRadius(28)
            eff.setOffset(0, 10)
            eff.setColor(theme.qcolor("rgba(37,99,235,0.14)"))
            self.setGraphicsEffect(eff)
        else:
            self.setGraphicsEffect(None)
        self.update()

    def is_selected(self):
        return self._selected

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._selected:
            p.setPen(QPen(theme.qcolor("blue-l"), 2))
            p.setBrush(QColor("#ffffff"))
            r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        else:
            p.setPen(QPen(theme.qcolor("line"), 1))
            p.setBrush(theme.qcolor("rgba(255,255,255,0.72)"))
            r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(r, 14, 14)
        p.end()

    def set_note(self, text):
        self.note.setText(text or "")
        self.note.setVisible(bool(text))

    def set_state(self, state, selected=False, in_use_text="In use", pct=None, error="", removing=False,
                  enabled=True):
        """state: missing | downloading | ready | failed."""
        if selected != self._selected:
            self._apply_selected(selected)
        key = (state, bool(selected), in_use_text, bool(removing), error or "", bool(enabled))
        if key == self._key and state == "downloading":
            self._set_progress(pct)
            return
        if key == self._key:
            return
        self._key = key
        clear_layout(self.action_lay)
        self.buttons = {}
        self.progress = self.progress_text = None
        plain = self.info.plain
        if removing:
            self.action_lay.addWidget(text_label("Removing…", 13, role="muted", parent=self.action))
            self.action_lay.addStretch(1)
        elif state == "downloading":
            col = QVBoxLayout()
            col.setSpacing(6)
            self.progress_text = text_label("Downloading…", 12, role="muted", parent=self.action)
            self.progress = QProgressBar(self.action)
            self.progress.setRange(0, 100)
            self.progress.setTextVisible(False)
            self.progress.setFixedHeight(6)
            self.progress.setAccessibleName("Downloading %s" % plain)
            col.addWidget(self.progress_text)
            col.addWidget(self.progress)
            self.action_lay.addLayout(col, 1)
            self._set_progress(pct)
        elif state == "failed":
            col = QVBoxLayout()
            col.setSpacing(6)
            err = text_label(error or "The download stopped. Try again.", 12, role="error", parent=self.action,
                             wrap=True)
            col.addWidget(err)
            b = Button("Try again", "secondary", "s", icon="refresh", parent=self.action)
            b.setAccessibleName("Download %s again" % plain)
            b.clicked.connect(lambda _=False: self.download.emit(self.mid))
            self.buttons["download"] = b
            col.addWidget(b, 0, Qt.AlignLeft)
            self.action_lay.addLayout(col, 1)
        elif state == "ready":
            if selected:
                self.action_lay.addWidget(icon_label("check", 15, "ok", sw=2.6, parent=self.action), 0,
                                          Qt.AlignVCenter)
                t = text_label(in_use_text, 13, 700, color="ok-tx", parent=self.action)
                self.action_lay.addWidget(t, 0, Qt.AlignVCenter)
            else:
                b = Button("Use", "secondary", "s", parent=self.action)
                b.setAccessibleName("Use %s" % plain)
                b.clicked.connect(lambda _=False: self.use.emit(self.mid))
                self.buttons["use"] = b
                self.action_lay.addWidget(b, 0, Qt.AlignVCenter)
            self.action_lay.addStretch(1)
            r = Button("Remove", "ghost", "s", parent=self.action)
            r.setAccessibleName("Remove %s" % plain)
            r.clicked.connect(lambda _=False: self.remove.emit(self.mid))
            self.buttons["remove"] = r
            self.action_lay.addWidget(r, 0, Qt.AlignVCenter)
        else:
            b = Button("Download", "secondary", "s", icon="download", parent=self.action)
            b.setAccessibleName("Download %s (%s, %s)" % (plain, self.info.real, self.info.size))
            b.setEnabled(bool(enabled))
            b.clicked.connect(lambda _=False: self.download.emit(self.mid))
            self.buttons["download"] = b
            self.action_lay.addWidget(b, 0, Qt.AlignVCenter)
            self.action_lay.addStretch(1)

    def _set_progress(self, pct):
        if self.progress is None:
            return
        if pct is None:
            self.progress.setRange(0, 0)
            self.progress_text.setText("Downloading…")
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(pct))
            self.progress_text.setText("Downloading · %d%%" % int(pct))


class SizeTile(_Painted):
    """One of the 'More sizes' models: plain name, real model and size, and
    what a click does (download / use) at the end."""
    activated = Signal(str)
    remove = Signal(str)

    def __init__(self, mid, parent=None):
        super().__init__(parent)
        self.mid = mid
        self.info = hardware.MODEL_INFO[mid]
        self._state = ("missing", False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(0)
        name = text_label(self.info.plain, 13, 600, role="label", parent=self)
        name.setFixedHeight(18)
        col.addWidget(name)
        model = text_label("%s · %s" % (self.info.real, self.info.size), 12, role="model", parent=self)
        model.setFixedHeight(16)
        col.addWidget(model)
        lay.addLayout(col, 1)
        self.trail = QLabel(self)
        self.trail.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.trail, 0, Qt.AlignVCenter)
        self.remove_button = IconButton("trash", "Remove %s" % self.info.plain, size=26, icon_size=15,
                                        color="muted", parent=self)
        self.remove_button.clicked.connect(lambda _=False: self.remove.emit(self.mid))
        self.remove_button.hide()
        lay.addWidget(self.remove_button, 0, Qt.AlignVCenter)
        for ch in self.findChildren(QLabel):
            ch.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.clicked.connect(lambda _=False: self.activated.emit(self.mid))
        self.set_state("missing")

    def box(self):
        state, selected = self._state
        if selected:
            return "#ffffff", "blue-l", 1
        return "rgba(255,255,255,0.72)", ("line2" if self.underMouse() else "line"), 1

    def set_state(self, state, selected=False, pct=None, removing=False, enabled=True):
        self._state = (state, bool(selected))
        i = self.info
        self.trail.setStyleSheet("")
        self.trail.setPixmap(icons.pixmap("download", 16, "muted"))
        name = "Download %s (%s, %s)" % (i.plain, i.real, i.size)
        if removing:
            self.trail.setText("Removing…")
            self.trail.setFont(theme.font(12))
            tint(self.trail, "muted")
            name = "%s (%s), removing" % (i.plain, i.real)
        elif state == "downloading":
            self.trail.setText("%d%%" % pct if pct is not None else "…")
            self.trail.setFont(theme.font(12, 600))
            tint(self.trail, "muted")
            name = "%s (%s), downloading" % (i.plain, i.real)
        elif state == "failed":
            self.trail.setPixmap(icons.pixmap("alert", 16, "red-tx", sw=2))
            name = "Download %s (%s, %s) again" % (i.plain, i.real, i.size)
        elif state == "ready" and selected:
            self.trail.setPixmap(icons.pixmap("check", 16, "blue-tx", sw=2.2))
            name = "%s (%s), in use" % (i.plain, i.real)
        elif state == "ready":
            self.trail.setText("Use")
            self.trail.setFont(theme.font(12.5, 600))
            tint(self.trail, "blue-tx")
            name = "Use %s (%s)" % (i.plain, i.real)
        self.setAccessibleName(name)
        self.remove_button.setVisible(state == "ready" and not selected and not removing)
        self.setEnabled(bool(enabled) or state == "ready")
        self.update()


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


class ArmenianCard(_Box):
    """The Armenian speech model (speech_hy.py): one wide card under the Whisper sizes - its real name
    (NVIDIA is credited in it), "Armenian only · about 105 MB · free, works offline", and the action of
    the moment: Download / progress + Cancel / Use / In use + Remove / Try again. Hidden until the model
    is on this computer or has somewhere to be downloaded from (speech_hy.listed)."""
    download = Signal()
    cancel = Signal()
    use = Signal()
    remove = Signal()

    def __init__(self, parent=None):
        super().__init__("rgba(255,255,255,0.72)", "line", 1, 12, parent)
        self.setAccessibleName("Armenian speech model")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)
        lay.addWidget(IconTile("mic", size=32, icon_size=18, radius=10, parent=self), 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.title = text_label(hardware.HY_INFO.real, 13.5, 600, role="label", parent=self)
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

    def set_state(self, state, selected=False, pct=None, error="", removing=False, enabled=True):
        """state: missing | downloading | ready | failed."""
        key = (state, bool(selected), error or "", bool(removing), bool(enabled))
        if key == self._key and state == "downloading":
            self._progress(pct)
            return
        if key == self._key:
            return
        self._key = key
        clear_layout(self.action_lay)
        self.buttons = {}
        self.progress = None
        real = hardware.HY_INFO.real
        size = hardware.HY_INFO.size
        self.sub.setStyleSheet("")
        self.sub.setText(engine_choice.HY_ROW_NOTE_READY if state == "ready"
                         else engine_choice.HY_ROW_NOTE.format(size=size))
        if removing:
            self.action_lay.addWidget(text_label("Removing…", 13, role="muted", parent=self.action))
        elif state == "downloading":
            self.progress = QProgressBar(self.action)
            self.progress.setTextVisible(False)
            self.progress.setFixedSize(120, 6)
            self.progress.setAccessibleName("Downloading %s" % real)
            self.action_lay.addWidget(self.progress, 0, Qt.AlignVCenter)
            b = Button("Cancel", "ghost", "s", parent=self.action)
            b.setAccessibleName("Cancel the download of %s" % real)
            b.clicked.connect(lambda _=False: self.cancel.emit())
            self.buttons["cancel"] = b
            self.action_lay.addWidget(b)
            self._progress(pct)
        elif state == "failed":
            self.sub.setText(error or "The download stopped. Try again.")
            tint(self.sub, "red-tx")
            b = Button("Try again", "secondary", "s", icon="refresh", parent=self.action)
            b.setAccessibleName("Download %s again" % real)
            b.clicked.connect(lambda _=False: self.download.emit())
            self.buttons["download"] = b
            self.action_lay.addWidget(b)
        elif state == "ready":
            if selected:
                self.action_lay.addWidget(Chip("In use", "ok", "check", parent=self.action))
            else:
                b = Button("Use", "secondary", "s", parent=self.action)
                b.setAccessibleName("Use %s" % real)
                b.clicked.connect(lambda _=False: self.use.emit())
                self.buttons["use"] = b
                self.action_lay.addWidget(b)
            r = Button("Remove", "ghost", "s", parent=self.action)
            r.setAccessibleName("Remove %s" % real)
            r.clicked.connect(lambda _=False: self.remove.emit())
            self.buttons["remove"] = r
            self.action_lay.addWidget(r)
        else:
            b = Button("Download", "secondary", "s", icon="download", parent=self.action)
            b.setAccessibleName("Download %s (%s)" % (real, size))
            b.setEnabled(bool(enabled))
            b.clicked.connect(lambda _=False: self.download.emit())
            self.buttons["download"] = b
            self.action_lay.addWidget(b)

    def _progress(self, pct):
        if self.progress is None:
            return
        if pct is None:
            self.progress.setRange(0, 0)
            self.sub.setText("Downloading…")
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(pct))
            self.sub.setText("Downloading · %d%%" % int(pct))


class OptionTile(_Painted):
    """A radio choice drawn as a tile: name (+ PRO), hint line(s), optional
    extra widget (e.g. the AI writer sizes). The page decides the selection."""
    RADIUS = 12

    def __init__(self, group, name, pro=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setAutoExclusive(False)
        self.group, self.name = group, name
        self.pro = bool(pro)
        self._paused = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName("%s: %s%s" % (group, name, " (Pro)" if pro else ""))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14 + 18 + 12, 13, 14, 13)       # 12 px padding + the 1 px edge
        lay.setSpacing(0)
        col = QVBoxLayout()
        col.setSpacing(1)
        trow = QHBoxLayout()
        trow.setSpacing(6)
        self.name_label = text_label(name, 13.5, 600, role="label", parent=self)
        self.name_label.setMinimumHeight(20)
        trow.addWidget(self.name_label, 0, Qt.AlignVCenter)
        self.pro_tag = None
        if pro:
            self.pro_tag = ProTag("s", parent=self)
            trow.addWidget(self.pro_tag, 0, Qt.AlignVCenter)
        trow.addStretch(1)
        col.addLayout(trow)
        self.hint_box = QVBoxLayout()
        self.hint_box.setSpacing(1)
        col.addLayout(self.hint_box)
        self.extra_box = QVBoxLayout()
        self.extra_box.setSpacing(0)
        col.addLayout(self.extra_box)
        lay.addLayout(col, 1)
        self.hint_labels = []
        self._hints = None
        self.extra = None

    def set_hints(self, lines):
        """lines: [text | (icon, text)]."""
        lines = list(lines or [])
        if lines == self._hints:
            return
        self._hints = lines
        clear_layout(self.hint_box)
        self.hint_labels = []
        for line in lines:
            icon, text = (line if isinstance(line, tuple) else (None, line))
            lb = text_label(text, 12, role="hint", parent=self)
            lb.setFixedHeight(17)
            lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.hint_labels.append(lb)
            if icon:
                row = QHBoxLayout()
                row.setSpacing(4)
                row.addWidget(icon_label(icon, 13, "muted", parent=self), 0, Qt.AlignVCenter)
                row.addWidget(lb, 0, Qt.AlignVCenter)
                row.addStretch(1)
                self.hint_box.addLayout(row)
            else:
                self.hint_box.addWidget(lb)
        desc = " · ".join(lb.text() for lb in self.hint_labels)
        self.setAccessibleDescription(desc)

    def hints(self):
        return [lb.text() for lb in self.hint_labels]

    def set_extra(self, widget):
        self.extra = widget
        widget.setParent(self)
        self.extra_box.addWidget(widget)

    def set_paused(self, paused):
        self._paused = bool(paused)
        self.update()

    def box(self):
        if self.isChecked():
            return "#ffffff", "blue-l", 1
        return "rgba(255,255,255,0.80)", ("line2" if self.underMouse() else "line"), 1

    def paint_extra(self, p):
        if self.isChecked():
            p.setPen(QPen(theme.qcolor("rgba(59,130,246,0.12)"), 3))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(2.5, 2.5, -2.5, -2.5), 10, 10)
        # the radio (accent-color: var(--blue))
        if self._paused:
            p.setOpacity(0.45)
        c = QRectF(14, 14, 18, 18)
        if self.isChecked():
            p.setPen(QPen(theme.qcolor("blue"), 2))
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(c.adjusted(1, 1, -1, -1))
            p.setPen(Qt.NoPen)
            p.setBrush(theme.qcolor("blue"))
            p.drawEllipse(c.adjusted(5, 5, -5, -5))
        else:
            p.setPen(QPen(theme.qcolor("blue-l" if self.underMouse() else "muted"), 1.5))
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(c.adjusted(0.75, 0.75, -0.75, -0.75))
        p.setOpacity(1.0)


class WriterSize(_Painted):
    """One local AI writer model inside 'This computer' (Small / Medium / Large / Gemma)."""
    RADIUS = 8
    activated = Signal(str)
    remove = Signal(str)

    def __init__(self, mid, parent=None):
        super().__init__(parent)
        self.mid = mid
        i = hardware.LOCAL_AI_INFO[mid]
        self.info = i
        self.title = i.plain if i.plain != "Alternative" else i.real
        sub = "%s · %s" % (i.real, i.size) if self.title != i.real else i.size
        self._sel = False
        self._state = "missing"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)                   # 6 px padding + the 1 px edge
        lay.setSpacing(0)
        top = QHBoxLayout()
        top.setSpacing(4)
        title = text_label(self.title, 12.5, 600, role="label", parent=self)
        title.setFixedHeight(18)
        top.addWidget(title, 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.mark = QLabel(self)
        top.addWidget(self.mark, 0, Qt.AlignVCenter)
        lay.addLayout(top)
        sub_label = text_label(sub, 11.5, role="model", parent=self)
        sub_label.setFixedHeight(16)
        lay.addWidget(sub_label)
        for ch in self.findChildren(QLabel):
            ch.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.clicked.connect(lambda _=False: self.activated.emit(self.mid))
        self.set_state("missing")

    def box(self):
        if self._sel:
            return "blue-sub", "blue-l", 1
        return "#ffffff", ("line2" if self.underMouse() else "line"), 1

    def contextMenuEvent(self, e):
        if self._state != "ready":
            return
        menu = QMenu(self)
        act = menu.addAction("Remove %s" % self.title)
        if menu.exec(e.globalPos()) is act:
            self.remove.emit(self.mid)

    def set_state(self, state, selected=False, pct=None):
        self._state = state
        self._sel = bool(selected and state == "ready")
        self.mark.setStyleSheet("")
        self.mark.setText("")
        if state == "downloading":
            self.mark.setText("%d%%" % pct if pct is not None else "…")
            self.mark.setFont(theme.font(11, 600))
            tint(self.mark, "muted")
            name = "%s (%s), downloading" % (self.title, self.info.real)
        elif state == "ready" and self._sel:
            self.mark.setPixmap(icons.pixmap("check", 13, "blue-tx", sw=2.4))
            name = "%s (%s), in use" % (self.title, self.info.real)
        elif state == "ready":
            self.mark.clear()
            name = "Use %s (%s)" % (self.title, self.info.real)
        else:
            self.mark.setPixmap(icons.pixmap("alert" if state == "failed" else "download", 13,
                                             "red-tx" if state == "failed" else "muted"))
            name = "Download %s (%s, %s)" % (self.title, self.info.real, self.info.size)
        self.setAccessibleName(name)
        self.update()

    def is_selected(self):
        return self._sel


class KeyRow(QFrame):
    """One of 'My API keys': the key shown masked (Key ending 4f2), Replace /
    Remove / Add key, and an inline editor that tests the key as it is pasted."""
    save = Signal(str, str)        # field, key
    removed = Signal(str)
    test = Signal(str, str)        # field, key

    def __init__(self, field, name, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "row")
        self.setProperty("last", False)
        self.field, self.name = field, name
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 12, 0, 12)
        lay.setSpacing(16)
        self.name_label = text_label(name, 13.5, 600, role="label", parent=self)
        self.name_label.setFixedWidth(180)
        lay.addWidget(self.name_label, 0, Qt.AlignVCenter)

        self.view = QWidget(self)
        vl = QHBoxLayout(self.view)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(8)
        self.key_icon = icon_label("key", 16, "muted", parent=self.view)
        vl.addWidget(self.key_icon, 0, Qt.AlignVCenter)
        self.text = text_label("", 13.5, parent=self.view)
        vl.addWidget(self.text, 0, Qt.AlignVCenter)
        self.chip = Chip("Works", "ok", "check", parent=self.view)
        vl.addWidget(self.chip, 0, Qt.AlignVCenter)
        vl.addStretch(1)
        lay.addWidget(self.view, 1)

        self.edit = QWidget(self)
        el = QHBoxLayout(self.edit)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(8)
        self.input = field_font(QLineEdit(self.edit))
        self.input.setEchoMode(QLineEdit.Password)
        self.input.setPlaceholderText("Paste your %s key" % name)
        self.input.setAccessibleName("%s key" % name)
        self.input.setFixedHeight(32)
        self.input.textChanged.connect(self._changed)
        self.input.returnPressed.connect(self._save)
        el.addWidget(self.input, 1)
        self.status = text_label("", 12, 600, parent=self.edit)
        self.status.setAccessibleName("Key check")
        el.addWidget(self.status, 0, Qt.AlignVCenter)
        lay.addWidget(self.edit, 1)
        self.edit.hide()

        self.actions = QWidget(self)
        al = QHBoxLayout(self.actions)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(4)
        self.replace_button = Button("Replace", "ghost", "s", parent=self.actions)
        self.replace_button.setAccessibleName("Replace the %s key" % name)
        self.replace_button.clicked.connect(self.open_editor)
        self.remove_button = Button("Remove", "ghost", "s", parent=self.actions)
        self.remove_button.setAccessibleName("Remove the %s key" % name)
        self.remove_button.clicked.connect(lambda _=False: self.removed.emit(self.field))
        self.add_button = Button("Add key", "secondary", "s", icon="plus", parent=self.actions)
        self.add_button.setAccessibleName("Add a %s key" % name)
        self.add_button.clicked.connect(self.open_editor)
        self.save_button = Button("Save", "primary", "s", parent=self.actions)
        self.save_button.setAccessibleName("Save the %s key" % name)
        self.save_button.clicked.connect(self._save)
        self.cancel_button = Button("Cancel", "ghost", "s", parent=self.actions)
        self.cancel_button.setAccessibleName("Cancel")
        self.cancel_button.clicked.connect(self.close_editor)
        for b in (self.replace_button, self.remove_button, self.add_button, self.save_button,
                  self.cancel_button):
            al.addWidget(b)
        lay.addWidget(self.actions, 0, Qt.AlignVCenter)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._fire_test)
        self._editing = False
        self._has_key = False
        self.last_result = None          # (key, ok, message) of the newest test
        self.show_view({"has_key": False, "text": "No key yet"}, None)

    def set_last(self, last):
        theme.set_prop(self, "last", bool(last), children=False)

    def set_field(self, field, name):
        self.field, self.name = field, name
        self.name_label.setText(name)
        self.input.setPlaceholderText("Paste your %s key" % name)
        self.input.setAccessibleName("%s key" % name)

    def is_editing(self):
        return self._editing

    def show_view(self, view, tested):
        """view: api_keys.key_view(...); tested: (ok, msg) of this key's test, or None."""
        self._editing = False
        self._has_key = bool(view.get("has_key"))
        self.edit.hide()
        self.view.show()
        self.key_icon.setVisible(self._has_key)
        self.text.setText(view.get("text") or ("Key saved" if self._has_key else "No key yet"))
        self.text.setFont(theme.font(13.5))
        tint(self.text, "text" if self._has_key else "muted")
        ok = bool(self._has_key and tested and tested[0])
        bad = bool(self._has_key and tested and not tested[0])
        self.chip.setVisible(ok or bad)
        if ok or bad:
            self.chip.setText("Works" if ok else "Didn't work")
            theme.set_prop(self.chip, "tone", "ok" if ok else "red")
            if getattr(self.chip, "_icon", None) is not None:
                self.chip._icon.setPixmap(icons.pixmap("check" if ok else "alert", 13,
                                                       "ok-tx" if ok else "red-tx", sw=2))
            self.chip.setToolTip("" if ok else (tested[1] or ""))
        self.replace_button.setVisible(self._has_key)
        self.remove_button.setVisible(self._has_key)
        self.add_button.setVisible(not self._has_key)
        self.save_button.hide()
        self.cancel_button.hide()

    def open_editor(self):
        self._editing = True
        self.view.hide()
        self.edit.show()
        self.input.clear()
        self.status.setText("")
        for b in (self.replace_button, self.remove_button, self.add_button):
            b.hide()
        self.save_button.show()
        self.save_button.setEnabled(False)
        self.cancel_button.show()
        self.input.setFocus(Qt.OtherFocusReason)

    def close_editor(self):
        self._editing = False
        self._timer.stop()
        self.input.clear()
        self.edit.hide()
        self.view.show()
        self.replace_button.setVisible(self._has_key)
        self.remove_button.setVisible(self._has_key)
        self.add_button.setVisible(not self._has_key)
        self.save_button.hide()
        self.cancel_button.hide()

    def _changed(self, text):
        text = text.strip()
        self.save_button.setEnabled(bool(text))
        self.status.setText("")
        if len(text) >= 8:
            self._timer.start()
        else:
            self._timer.stop()

    def _fire_test(self):
        key = self.input.text().strip()
        if key:
            self.status.setText("Checking…")
            tint(self.status, "muted")
            self.test.emit(self.field, key)

    def show_test(self, key, ok, message):
        """A test answer: ignored unless it is for what is in the box now."""
        if not self._editing or key != self.input.text().strip():
            return
        self.last_result = (key, bool(ok), message)
        self.status.setText("✓ Works" if ok else (message or "That key didn't work."))
        tint(self.status, "ok-tx" if ok else "red-tx")
        self.status.setToolTip("" if ok else (message or ""))

    def _save(self):
        key = self.input.text().strip()
        if key:
            self.save.emit(self.field, key)


class WriterKeyPanel(Card):
    """'More options': the AI writer with any provider's key - provider, model,
    server address (OpenAI-compatible / Cerebras) and that provider's key."""
    use = Signal(str, str, str)     # provider, model, base url

    PROVIDERS = tuple(engine_choice.WRITER_PROVIDERS)

    def __init__(self, parent=None):
        super().__init__(parent, pad=(20, 4), gap=0)
        self.provider = field_font(QComboBox(self))
        self.provider.setAccessibleName("AI writer service")
        for pid in self.PROVIDERS:
            self.provider.addItem(engine_choice.WRITER_PROVIDERS[pid]["name"], pid)
        # Wide enough for "Qwen 3.8 27B · fastest, sees images" without clipping.
        self.provider.setFixedWidth(300)
        self.model = field_font(QComboBox(self))
        self.model.setAccessibleName("Model")
        self.model.setEditable(True)
        self.model.setFixedWidth(300)
        if self.model.lineEdit() is not None:
            field_font(self.model.lineEdit())
            self.model.lineEdit().setAccessibleName("Model")
        self.model.currentIndexChanged.connect(lambda _i: self._show_model_start())
        self.base = field_font(QLineEdit(self))
        self.base.setAccessibleName("Server address")
        self.base.setFixedWidth(300)
        self.row_provider = SettingRow("AI writer with your own key", self.provider)
        self.row_model = SettingRow("Model", self.model)
        self.row_base = SettingRow("Server address", self.base)
        self.key_row = KeyRow("action_api_key", "Key")
        foot = QWidget(self)
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(0, 12, 0, 12)
        self.note = text_label("", 12, role="hint", parent=foot, wrap=True)
        fl.addWidget(self.note, 1)
        self.use_button = Button("Use for AI writer", "primary", "s", parent=foot)
        self.use_button.clicked.connect(self._use)
        fl.addWidget(self.use_button, 0, Qt.AlignVCenter)
        for w in (self.row_provider, self.row_model, self.row_base, self.key_row, foot):
            self.add(w)
        self.provider.currentIndexChanged.connect(lambda _i: self._provider_changed())

    def current(self):
        return self.provider.currentData()

    def load(self, cfg):
        engine = engine_choice.writer_choice(cfg)
        pid = cfg.get("action_model") if engine == "own_key" else None
        pid = engine_choice._norm_provider(pid) or self.PROVIDERS[0]
        idx = self.provider.findData(pid)
        self.provider.blockSignals(True)
        self.provider.setCurrentIndex(max(0, idx))
        self.provider.blockSignals(False)
        self._provider_changed(cfg)

    def _provider_changed(self, cfg=None):
        pid = self.current()
        info = engine_choice.WRITER_PROVIDERS.get(pid, {})
        self.model.blockSignals(True)
        self.model.clear()
        for mid, name, note in engine_choice.writer_models(pid):
            self.model.addItem("%s%s" % (name, (" · " + note) if note else ""), mid)
        if cfg is not None:
            current = engine_choice.writer_model_for(pid, cfg)
            i = self.model.findData(current)
            if i >= 0:
                self.model.setCurrentIndex(i)
            elif current:
                self.model.addItem(current, current)
                self.model.setCurrentIndex(self.model.count() - 1)
        self.model.blockSignals(False)
        self._show_model_start()
        editable = info.get("base_url") == "editable"
        self.row_base.setVisible(editable)
        self.base.setPlaceholderText(info.get("default_url", ""))
        if editable and cfg is not None:
            self.base.setText(str(cfg.get("action_api_base_url") or ""))
        field = info.get("key_field") or "action_api_key"
        self.key_row.set_field(field, "%s key" % info.get("name", "AI writer"))
        self.key_row.set_last(False)
        if field == "google_api_key":
            self.note.setText("Uses your Google Gemini key.")
        elif info.get("key_fallback") == "mistral_api_key":
            self.note.setText("Empty: your Mistral key above is used.")
        else:
            self.note.setText("")

    def _show_model_start(self):
        """A long model name reads from its start, not its clipped end."""
        le = self.model.lineEdit()
        if le is not None:
            le.setCursorPosition(0)

    def selected_model(self):
        i = self.model.currentIndex()
        data = self.model.itemData(i) if i >= 0 else None
        text = self.model.currentText().strip()
        if data and self.model.itemText(i) == text:
            return data
        return text.split(" · ")[0].strip() if text else ""

    def _use(self):
        self.use.emit(self.current(), self.selected_model(), self.base.text().strip())


# ── Updates & about ──────────────────────────────────────────────────────────
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
        self._key_then_speech = None    # (key field, provider): "Your own key" waiting for its key
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
        self.hw_trail = text_label("", 12.5, role="muted", parent=sec)
        blk, bv = self._block("Speech model", trail=self.hw_trail, parent=sec)
        self.cards_row = CardsRow(parent=blk)
        self.model_cards = {}
        for mid in hardware.MAIN_SPEECH_MODELS:
            card = ModelCard(mid)
            card.download.connect(self._model_download)
            card.use.connect(self._model_use)
            card.remove.connect(self._model_remove)
            self.cards_row.add(card)
            self.model_cards[mid] = card
        bv.addWidget(self.cards_row)
        more = QVBoxLayout()
        more.setContentsMargins(0, 6, 0, 0)
        more.setSpacing(8)
        self.more_button = trailing_icon(Button("More sizes", "link", "m", icon="chev_d", parent=blk))
        self.more_button.setIconSize(QSize(14, 14))
        self.more_button.setFixedHeight(20)
        self.more_button.clicked.connect(lambda _=False: self._toggle_more())
        more.addWidget(self.more_button, 0, Qt.AlignLeft | Qt.AlignAbsolute)
        self.tiles_row = CardsRow(top=12, parent=blk)
        self.tiles_row.set_recommended(None, room=False)
        self.size_tiles = {}
        for mid in hardware.MORE_SPEECH_MODELS:
            t = SizeTile(mid)
            t.activated.connect(self._tile_clicked)
            t.remove.connect(self._model_remove)
            self.tiles_row.add(t)
            self.size_tiles[mid] = t
        more.addWidget(self.tiles_row)
        bv.addLayout(more)
        # The Armenian model: its own wide card under the sizes (hidden until it is available).
        self.hy_card = ArmenianCard(blk)
        self.hy_card.download.connect(self._hy_download)
        self.hy_card.cancel.connect(self._hy_cancel)
        self.hy_card.use.connect(lambda: self._model_use(hardware.HY_MODEL_ID))
        self.hy_card.remove.connect(lambda: self._model_remove(hardware.HY_MODEL_ID))
        self.hy_card.hide()
        bv.addWidget(self.hy_card)
        self.gpu_card = GpuCard(blk)
        self.gpu_card.start.connect(self._gpu_start)
        self.gpu_card.cancel.connect(lambda: call(self.app, "cancel_gpu_download"))
        self.gpu_card.retry.connect(self._gpu_retry)
        self.gpu_card.remove.connect(self._gpu_remove)
        self.gpu_card.hide()
        bv.addWidget(self.gpu_card)
        self._more_open = True
        self._sync_more()
        inner.addWidget(blk)

        two = QBoxLayout(QBoxLayout.LeftToRight)       # top to bottom in a narrow window
        two.setSpacing(20)
        self.speech_two = two
        left, lv = self._block("Speech runs on", sub="Dictation and meetings.", parent=sec)
        tiles = QVBoxLayout()
        tiles.setSpacing(8)
        self.speech_tiles = {
            "local": OptionTile("Speech runs on", engine_choice.THIS_COMPUTER),
            "cloud": OptionTile("Speech runs on", engine_choice.GUSAN_CLOUD, pro=True),
            "own_key": OptionTile("Speech runs on", engine_choice.OWN_KEY),
        }
        for k, t in self.speech_tiles.items():
            t.clicked.connect(lambda _=False, s=k: self._speech_pick(s))
            tiles.addWidget(t)
        self.speech_provider = Segmented(["Mistral", "Google Gemini"], 0, "Speech key", size="s")
        self.speech_provider.changed.connect(
            lambda i: self._speech_pick("own_key", provider=("mistral", "google")[i]))
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 8, 0, 0)
        hl.addWidget(self.speech_provider)
        hl.addStretch(1)
        self.speech_provider_box = holder
        self.speech_tiles["own_key"].set_extra(holder)
        lv.addLayout(tiles)
        two.addWidget(left, 1, Qt.AlignTop)

        right, rv = self._block("AI writer", sub="Smart Actions, meeting notes and Live Assistant.", parent=sec)
        wtiles = QVBoxLayout()
        wtiles.setSpacing(8)
        self.writer_tiles = {
            "cloud": OptionTile("AI writer", engine_choice.GUSAN_CLOUD, pro=True),
            "local": OptionTile("AI writer", engine_choice.THIS_COMPUTER),
            "own_key": OptionTile("AI writer", engine_choice.OWN_KEY),
        }
        self.writer_tiles["cloud"].set_hints(list(CLOUD_WRITER_HINTS))
        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 8, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        self.writer_sizes = {}
        for i, mid in enumerate(hardware.LOCAL_AI_MODEL_IDS):
            ws = WriterSize(mid)
            ws.activated.connect(self._writer_size_clicked)
            ws.remove.connect(self._model_remove)
            grid.addWidget(ws, i // 2, i % 2)
            self.writer_sizes[mid] = ws
        self.writer_tiles["local"].set_extra(grid_w)
        for k, t in self.writer_tiles.items():
            t.clicked.connect(lambda _=False, c=k: self._writer_pick(c))
            wtiles.addWidget(t)
        rv.addLayout(wtiles)
        two.addWidget(right, 1, Qt.AlignTop)
        inner.addLayout(two)

        kblk, kv = self._block("My API keys", parent=sec)
        self.key_rows = {}
        rows = []
        for field, name in SPEECH_KEYS:
            r = KeyRow(field, name)
            self._wire_key_row(r)
            self.key_rows[field] = r
            rows.append(r)
        self.keys_card = self._group(rows)
        kv.addWidget(self.keys_card)
        self.more_options = trailing_icon(Button("More options", "link", "m", icon="chev_r", parent=kblk))
        self.more_options.setIconSize(QSize(14, 14))
        self.more_options.setFixedHeight(20)
        self.more_options.clicked.connect(lambda _=False: self._toggle_more_options())
        kv.addWidget(self.more_options, 0, Qt.AlignLeft | Qt.AlignAbsolute)
        self.writer_panel = WriterKeyPanel(kblk)
        self._wire_key_row(self.writer_panel.key_row)
        self.writer_panel.use.connect(self._writer_own_key)
        self.writer_panel.hide()
        kv.addWidget(self.writer_panel)
        self.keys_block = kblk
        inner.addWidget(kblk)
        v.addLayout(inner)
        self._layout_speech_columns()

    def _toggle_more(self):
        self._more_open = not self._more_open
        self._sync_more()

    def _sync_more(self):
        self.tiles_row.setVisible(self._more_open)
        self.more_button.setIcon(icons.icon("chev_d" if self._more_open else "chev_r", 14, "blue-tx", sw=2))
        self.more_button.setAccessibleDescription("Expanded" if self._more_open else "Collapsed")

    def _toggle_more_options(self, show=None):
        show = (not self.writer_panel.isVisible()) if show is None else bool(show)
        if show:
            self.writer_panel.load(self.cfg)
            self._refresh_key_row(self.writer_panel.key_row)
        self.writer_panel.setVisible(show)
        self.more_options.setIcon(icons.icon("chev_d" if show else "chev_r", 14, "blue-tx", sw=2))
        self.more_options.setAccessibleDescription("Expanded" if show else "Collapsed")

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
        cfg = self.cfg
        dl = self._dl()
        sel = cfg.get("whisper_model") or hardware.DEFAULT_SPEECH_MODEL
        speech = self._speech_now()
        in_use = "In use" if speech.get("kind") == "local" else "Offline backup"
        on_gpu = bool(self._hardware is not None and getattr(self._hardware, "on_gpu", False))
        for mid, card in self.model_cards.items():
            st = self._state(mid)
            if st == "unknown":
                st = "ready" if mid == sel else "missing"
            prog = None
            err = ""
            removing = False
            if dl is not None:
                try:
                    p = dl.progress_of(mid)
                    prog = p[0] if p and (p[2] or p[0]) else None
                    err = dl.error(mid) or ""
                    removing = bool(dl.is_removing(mid))
                except Exception:
                    pass
            card.set_state(st, selected=(mid == sel), in_use_text=in_use, pct=prog, error=err,
                           removing=removing, enabled=dl is not None)
            card.set_note("A little slower here" if hardware.slower_here(mid, on_gpu) else "")
        for mid, tile in self.size_tiles.items():
            st = self._state(mid)
            if st == "unknown":
                st = "ready" if mid == sel else "missing"
            prog = None
            removing = False
            if dl is not None:
                try:
                    p = dl.progress_of(mid)
                    prog = p[0] if p and (p[2] or p[0]) else None
                    removing = bool(dl.is_removing(mid))
                except Exception:
                    pass
            tile.set_state(st, selected=(mid == sel), pct=prog, removing=removing, enabled=dl is not None)
        self._refresh_hy()
        rec = self._recommended()
        if rec in self.model_cards:
            self.cards_row.set_recommended(self.model_cards[rec])
            self.tiles_row.set_recommended(None, room=False)
        elif rec in self.size_tiles and self._more_open:
            self.cards_row.set_recommended(None)
            self.tiles_row.set_recommended(self.size_tiles[rec], room=True)
        else:
            self.cards_row.set_recommended(None)
            self.tiles_row.set_recommended(None, room=False)
        for mid, card in self.model_cards.items():
            card.setAccessibleDescription("Best for your computer" if mid == rec else "")

    def _refresh_hy(self):
        """The Armenian model's card: hidden while it isn't listed, else its state from the downloads."""
        card = getattr(self, "hy_card", None)
        if card is None:
            return
        shown = hy_model.state(self.app) != "hidden"
        card.setVisible(shown)
        if not shown:
            return
        dl = self._dl()
        st = hy_model.state(self.app)
        sel = str(self.cfg.get("whisper_model") or "") == hardware.HY_MODEL_ID
        removing = False
        if dl is not None:
            try:
                removing = bool(dl.is_removing(hardware.HY_MODEL_ID))
            except Exception:
                removing = False
        card.set_state(st, selected=sel, pct=hy_model.percent(self.app), error=hy_model.failure(self.app),
                       removing=removing, enabled=dl is not None)

    def _hy_download(self):
        """Download the Armenian model and use it when it is on this computer (like a Whisper card).
        Privacy mode does not stop it: it only fetches the model (speech_hy)."""
        self._model_download(hardware.HY_MODEL_ID)

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
        speech = self._speech_now()
        if speech.get("kind") == "local":
            res = engine_choice.set_speech_source(self.app, "local", model=mid, downloaded=self._downloaded_fn())
            if res["ok"]:
                apply_settings(self.app, res["changes"])
            elif res["problem"] == "needs_download":
                self._model_download(mid)
                return
        else:
            apply_settings(self.app, {"whisper_model": mid})
        self.refresh()

    def _tile_clicked(self, mid):
        st = self._state(mid)
        if st in ("missing", "failed"):
            self._model_download(mid)
        elif st == "ready" and mid != self.cfg.get("whisper_model"):
            self._model_use(mid)

    def _model_remove(self, mid):
        info = hardware.info(mid)
        plain = info.plain if info else mid
        if mid in hardware.LOCAL_AI_INFO:
            plain = info.plain if info.plain != "Alternative" else info.real
        detail = "%s · %s. You can download it again." % (info.real, info.size) if info else ""
        if ask(self, "Remove %s?" % plain, detail,
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
        if not ok and msg and "speech" in self._built and name in self.model_cards:
            self.model_cards[name].set_note(msg)
        if not ok and msg and "speech" in self._built and hardware.HY_MODEL_ID == name:
            self.hy_card.sub.setText(msg)
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
        # total 0 = the size isn't known yet: draw it indeterminate, not "0%".
        pct = None if (not total and not pct) else int(pct or 0)
        text = "…" if pct is None else "%d%%" % pct
        if name in self.model_cards:
            self.model_cards[name]._set_progress(pct)
        elif name == hardware.HY_MODEL_ID:
            self.hy_card._set_progress(pct)
        elif name in self.size_tiles:
            tile = self.size_tiles[name]
            if tile._state[0] == "downloading":
                tile.trail.setText(text)
        elif name in self.writer_sizes:
            ws = self.writer_sizes[name]
            if ws._state == "downloading":
                ws.mark.setText(text)

    # speech source
    def _speech_pick(self, source, provider=None, turn_off_privacy=False):
        cfg = self.cfg
        if source == "own_key" and provider is None:
            if cfg.get("backend") in ("mistral", "google") and engine_choice.speech_choice(cfg) == "own_key":
                provider = cfg.get("backend")
            elif (cfg.get("mistral_api_key") or "").strip():
                provider = "mistral"
            elif (cfg.get("google_api_key") or "").strip():
                provider = "google"
            else:
                provider = "mistral"
        res = engine_choice.set_speech_source(self.app, source, provider=provider,
                                              downloaded=self._downloaded_fn(),
                                              turn_off_privacy=turn_off_privacy)
        problem = res.get("problem")
        self._key_then_speech = None
        if res.get("ok"):
            apply_settings(self.app, res["changes"])
        elif problem == "pro":
            call(self.app, "_pro_upsell", res.get("upsell") or engine_choice.UPSELL_SPEECH)
        elif problem == "privacy_mode":
            if self._ask_privacy_off():
                return self._speech_pick(source, provider, turn_off_privacy=True)
        elif problem == "needs_key":
            field = "google_api_key" if provider in ("google", "gemini") else "mistral_api_key"
            self._key_then_speech = (field, provider)       # saving that key finishes the pick
            self.open_key_editor(field)
        elif problem == "needs_download":
            self._model_download(cfg.get("whisper_model") or hardware.DEFAULT_SPEECH_MODEL)
        self.refresh()
        return res

    def _ask_privacy_off(self):
        return ask(self, "Turn off Privacy mode?", "Gusan Cloud and your own keys are off while it's on.",
                   [("cancel", "Cancel", "ghost"), ("off", "Turn off", "primary")]) == "off"

    # AI writer
    def _writer_pick(self, choice, turn_off_privacy=False):
        cfg = self.cfg
        if choice == "local":
            mid = engine_choice.local_writer(cfg)
            st = self._state(mid)
            if st in ("missing", "failed"):
                # Picking the tile itself starts a big download: say how big first.
                i = hardware.LOCAL_AI_INFO.get(mid)
                if i is not None and ask(self, "Download %s?" % i.plain, "%s · %s. It works offline." % (i.real, i.size),
                                         [("cancel", "Cancel", "ghost"), ("go", "Download", "primary")]) != "go":
                    self.refresh()
                    return None
            return self._writer_local(mid, turn_off_privacy)
        if choice == "own_key":
            pid = None
            if engine_choice.writer_choice(cfg) == "own_key":
                pid = engine_choice._norm_provider(cfg.get("action_model"))
            if not pid:
                import actions
                for cand in (actions.API_MISTRAL_ID, actions.API_GEMINI_ID):
                    if actions.engine_has_key(cand, cfg):
                        pid = cand
                        break
            if not pid:
                self._toggle_more_options(True)
                self.writer_panel.key_row.open_editor()
                self._scroll_to_widget(self.writer_panel)
                self.refresh()
                return None
            return self._writer_own_key(pid, None, None, turn_off_privacy)
        res = engine_choice.set_writer(self.app, "cloud", turn_off_privacy=turn_off_privacy)
        return self._writer_result(res, lambda: self._writer_pick(choice, True))

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
        return self._writer_result(res, lambda: self._writer_local(mid, True))

    def _writer_own_key(self, provider, model, base, turn_off_privacy=False):
        res = engine_choice.set_writer(self.app, "own_key", model=model or None, provider=provider,
                                       base_url=(base if base else None), turn_off_privacy=turn_off_privacy)
        if res.get("problem") == "needs_key":
            self._toggle_more_options(True)
            idx = self.writer_panel.provider.findData(engine_choice._norm_provider(provider))
            if idx >= 0:
                self.writer_panel.provider.setCurrentIndex(idx)
            self.writer_panel.key_row.open_editor()
            self._scroll_to_widget(self.writer_panel)
            self.refresh()
            return res
        return self._writer_result(res, lambda: self._writer_own_key(provider, model, base, True))

    def _writer_result(self, res, retry_without_privacy):
        problem = res.get("problem")
        if res.get("ok"):
            apply_settings(self.app, res["changes"])
        elif problem == "pro":
            call(self.app, "_pro_upsell", res.get("upsell") or engine_choice.UPSELL_WRITER)
        elif problem == "privacy_mode":
            if self._ask_privacy_off():
                return retry_without_privacy()
        self.refresh()
        return res

    def _writer_size_clicked(self, mid):
        self._writer_local(mid)

    def _refresh_speech_choice(self):
        cfg = self.cfg
        speech = self._speech_now()
        kind = speech.get("kind")
        privacy = bool(cfg.get("privacy_mode"))
        sel = cfg.get("whisper_model") or hardware.DEFAULT_SPEECH_MODEL
        local = engine_choice._local_speech("dictation", sel)
        for k, t in self.speech_tiles.items():
            t.setChecked(k == kind)
            t.set_paused(privacy and k != "local")
        # The real model name and where it runs, as the pickers say it ("Whisper Small · Local").
        self.speech_tiles["local"].set_hints(["%s · %s" % (local["tagged"], local["plain"])])
        cloud = engine_choice.CLOUD_SPEECH_MODELS.get(str(cfg.get("managed_provider") or "gemini").lower(),
                                                      hardware.CLOUD_SPEECH_MODEL)
        self.speech_tiles["cloud"].set_hints(["Off while Privacy mode is on" if privacy else
                                              "%s · nothing to download"
                                              % engine_choice.tagged_name(cloud, "cloud")])
        if privacy:
            own = "Off while Privacy mode is on"
        elif kind == "own_key":
            own = speech.get("tagged") or "%s · %s" % (speech.get("plain"), speech.get("model_name"))
        else:
            own = "Mistral or Google Gemini"
        self.speech_tiles["own_key"].set_hints([own])
        both = bool((cfg.get("mistral_api_key") or "").strip() and (cfg.get("google_api_key") or "").strip())
        self.speech_provider_box.setVisible(kind == "own_key" and both)
        self.speech_provider.set_index(1 if cfg.get("backend") == "google" else 0)

    def _refresh_writer(self):
        cfg = self.cfg
        now = self._writer_now() or {}
        kind = now.get("kind")
        tile = {"cloud": "cloud", "own_key": "own_key"}.get(kind, "local")
        privacy = bool(cfg.get("privacy_mode"))
        for k, t in self.writer_tiles.items():
            t.setChecked(k == tile)
            t.set_paused(privacy and k != "local")
        if privacy:
            self.writer_tiles["cloud"].set_hints(["Off while Privacy mode is on"])
        else:
            self.writer_tiles["cloud"].set_hints(list(CLOUD_WRITER_HINTS))
        self.writer_tiles["local"].set_hints(["Works offline"])
        self.writer_tiles["own_key"].set_hints(["Off while Privacy mode is on" if privacy else self._own_key_hint(cfg)])
        # The local writer stays marked while the cloud writes: it is the offline backup.
        local_sel = engine_choice.local_writer(cfg)
        dl = self._dl()
        for mid, ws in self.writer_sizes.items():
            st = self._state(mid)
            if st == "unknown":
                try:
                    import local_llm
                    st = "ready" if local_llm.model_downloaded(mid) else "missing"
                except Exception:
                    st = "missing"
            pct = None
            if dl is not None and st == "downloading":
                try:
                    p = dl.progress_of(mid)
                    pct = p[0] if p and (p[2] or p[0]) else None
                except Exception:
                    pct = None
            ws.set_state(st, selected=(mid == local_sel), pct=pct)

    def _own_key_hint(self, cfg):
        import actions
        engine = actions.normalize_action_model(cfg.get("action_model"))
        pids = []
        if engine_choice.writer_choice(cfg) == "own_key":
            pids.append(engine)
        pids += [actions.API_MISTRAL_ID, actions.API_GEMINI_ID]
        for pid in pids:
            info = engine_choice.WRITER_PROVIDERS.get(pid)
            if not info:
                continue
            for field in (info.get("key_field"), info.get("key_fallback")):
                if not field:
                    continue
                try:
                    kv = api_keys.key_view(cfg, field)
                except ValueError:
                    continue
                if kv["has_key"]:
                    text = kv["text"]
                    return "%s · %s" % (info["name"], text[:1].lower() + text[1:])
        return "Mistral, Google Gemini and more"

    # keys
    def _wire_key_row(self, row):
        row.save.connect(self._save_key)
        row.removed.connect(self._remove_key)
        row.test.connect(self._test_key)
        row.cancel_button.clicked.connect(self._forget_key_then)

    def _refresh_key_row(self, row):
        if row.is_editing():
            return
        try:
            kv = api_keys.key_view(self.cfg, row.field)
        except ValueError:
            return
        key = str(self.cfg.get(row.field) or "").strip()
        row.show_view(kv, key_test_result(key))

    def _refresh_keys(self):
        for row in self.key_rows.values():
            self._refresh_key_row(row)
        if self.writer_panel.isVisible():
            self._refresh_key_row(self.writer_panel.key_row)

    def open_key_editor(self, field):
        self.ensure_built("speech")
        row = self.key_rows.get(field)
        if row is None:
            return
        row.open_editor()
        self._scroll_to_widget(self.keys_card)

    def _test_key(self, field, key):
        cfg = dict(self.cfg)
        row = self.sender() if isinstance(self.sender(), KeyRow) else self._row_for(field)
        if row is self.writer_panel.key_row and field == "action_api_key":
            pid = self.writer_panel.current()
            cfg.update(action_model=pid, action_api_provider=pid,
                       action_api_model=self.writer_panel.selected_model(),
                       action_api_base_url=self.writer_panel.base.text().strip())

        def done(res):
            ok, msg = res if isinstance(res, tuple) else (False, "")
            remember_key_test(key, ok, msg)
            if row is not None and alive(row):
                row.show_test(key, ok, msg)

        def failed(_e):
            done((False, "Couldn't check it right now."))
        run_bg(api_keys.test_key, field, key, cfg, then=done, error=failed, owner=self)

    def unsaved_edits(self):
        """True while a key is typed into an open "My API keys" editor and not saved yet. Installing an
        update closes Gusan: the controller (AppController._update_unsaved_edits) asks first."""
        rows = list(getattr(self, "key_rows", {}).values())
        panel = getattr(self, "writer_panel", None)           # (the Speech section may not be built yet)
        if panel is not None:
            rows.append(panel.key_row)
        for row in rows:
            try:
                if row.is_editing() and row.input.text().strip():
                    return True
            except RuntimeError:                  # a row already torn down
                continue
        return False

    def _row_for(self, field):
        if self.writer_panel.isVisible() and self.writer_panel.key_row.field == field and \
                self.writer_panel.key_row.is_editing():
            return self.writer_panel.key_row
        return self.key_rows.get(field) or (self.writer_panel.key_row
                                            if self.writer_panel.key_row.field == field else None)

    def _save_key(self, field, key):
        row = self.sender() if isinstance(self.sender(), KeyRow) else self._row_for(field)
        ok = False
        try:
            ok = bool(api_keys.save_key(self.app, field, key))
        except Exception:
            logger.warning("Saving the %s key failed", field, exc_info=True)
        if row is not None:
            if ok:
                if row.last_result and row.last_result[0] == key:
                    remember_key_test(key, row.last_result[1], row.last_result[2])
                row.close_editor()
            else:
                row.status.setText("Couldn't save it.")
                tint(row.status, "red-tx")
        then = getattr(self, "_key_then_speech", None)
        if ok and then and then[0] == field and row is self.key_rows.get(field):
            # "Your own key" was picked before there was a key: use it now.
            self._speech_pick("own_key", provider=then[1])
            return
        self.refresh()

    def _forget_key_then(self, *_args):
        self._key_then_speech = None

    def _remove_key(self, field):
        name = api_keys.KEY_NAMES.get(field, "this")
        if ask(self, "Remove the %s key?" % name, "You can paste it again later.",
               [("cancel", "Cancel", "ghost"), ("remove", "Remove", "danger")]) != "remove":
            return
        try:
            api_keys.clear_key(self.app, field)
        except Exception:
            logger.warning("Removing the %s key failed", field, exc_info=True)
        self.refresh()

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
                      "models": self.cards_row, "writer": self.writer_tiles["local"]}.get(focus)
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
