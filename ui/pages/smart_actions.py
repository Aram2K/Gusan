"""Smart Actions page (Gusan 2.0): say what you want, the AI writes it.

Smart Actions have their own shortcut (cfg ``smart_actions_hotkey``, default
"alt+s", Option+S on a Mac); the dictation shortcut (Alt+R) always pastes the
exact words. There is no on/off switch: the hero draws the Smart Actions
shortcut as big keycaps, "Press, say what you want, press again." and a muted
"<Alt + R> pastes your exact words." (the real dictation keys). Free users see
a quiet tries counter here (and only here); used up = ONE Upgrade button;
guests get "Sign up free"; Pro reads "Unlimited". "What you can say" teaches
four examples (Email, To-do list, Translate, Polish; a 2 x 2 grid), each with
Try it (a small practice box the Smart Action pastes into; it takes the rich
paste, so lists and checkboxes show as they would in Word or Gmail). Recent
shows what was said -> what was pasted (one plain line; Copy puts the HTML +
plain text on the clipboard, ui.components.clipboard.copy_rich).

The rail: "Shortcut" (keys + Change, ui/components/hotkey_capture, as on the
Dictation page; anyone may change it, clashes with the dictation / Live
Assistant keys are refused), "Transcribes with" (cfg
``smart_actions_speech_model``: "" Same as Dictation, a Whisper model, or
Gusan Cloud PRO; engine_choice.smart_speech_choices / runs_on
"smart_actions_speech"), "Writes with" (This computer · Qwen 1.5B / Gusan
Cloud PRO · DeepSeek V4.1 Flash, from engine_choice) and the foot link "More
options in Speech & AI". There is no "Translate into" row: people say the
language ("to French"); without one, cfg translate_target (DEFAULT "en") is
used.

Every change goes through app.set_setting / set_settings (instant apply).
Pro actions for Free users open the current Pro window:
app._pro_upsell("Smart Actions") (Gusan Cloud speech: "Fast cloud
transcription").

Integration notes
-----------------
* Recent needs the raw words: main.AppController._stop stores ``"said"``
  (what was dictated) and ``"action"`` on the history entry. This page lists
  entries whose "action" is not transcribe_only and that have "said". After a
  Smart Action is saved, call ``page.reload_recent()`` (e.g. from a queued
  sig_history_changed) so an open page updates; on_open reloads anyway.
  History is read on a worker (api_keys.run_then).
* Tries: main._stop emits sig_auth_changed after each try, which reaches
  refresh_account() here through MainWindow.refresh_account.
* The shortcut: AppController._setup_smart_hotkey registers it; its press
  starts ONE dictation as a Smart Action (Pro, or Free with tries left;
  guests sign up, Free with none left get the Pro window). settings_apply
  re-registers it and refuses a clash with hotkey / live_assist_hotkey both
  ways (this page refuses clashes too). While the capture listens the page
  calls ``app.suspend_hotkeys(True)`` and ``(False)`` after (Alt+R would
  otherwise start a dictation mid-capture). "" (e.g. a 1.9.x user whose
  dictation key is Alt+S) shows "Set shortcut".
* "Transcribes with": the list never cuts a row (ui.components.model_combo's
  row marks): a real PRO pill on Gusan Cloud for Free and guests, a download
  icon on a Whisper model that is not on this computer, nothing on one that
  is. A model not on disk asks first (a mini popup, "Download <plain · real>?"
  Download / Not now); Download starts app.downloads.start(<id>) and saves
  the pick at once (the controller's model fetcher joins the same download
  if a Smart Action runs first); the line under the picker shows the
  download; Not now changes nothing. Gusan Cloud is always clickable: not Pro
  -> app._pro_upsell("Fast cloud transcription") and nothing changes; Pro in
  Privacy mode -> ui/privacy_prompt "Privacy mode is on" (Turn off Privacy
  mode / Keep it on; turning it off goes through app.set_settings together
  with the pick); Pro otherwise just selects it. Pro is checked first.
* Guests: Try it opens sign-up (app.show_auth_gate): an account is what
  grants the 5 free tries. Free users with no tries left and Gusan Cloud
  open the Pro window.
* "Writes with" shows what really writes (engine_choice.runs_on), not the
  saved choice: with action_model "rule_based" (main.DEFAULT, and what a
  Free user gets after Pro ends) only the built-in rules run, so no option
  is drawn as chosen and the row says "Pick one to write with AI.";
  Privacy mode pauses Gusan Cloud (faded, not clickable) and an own-key
  writer.
* "This computer" when the local writer isn't downloaded starts
  app.downloads.start(<model id>) (model_manager.ModelDownloads: progress /
  finished / state_changed / state / progress_of) and applies
  engine_choice.set_writer once it is ready; the line shows the download
  size first ("Qwen 1.5B · 1.12 GB download"). Without app.downloads it
  opens Settings › Speech & AI. The page connects to app.downloads the
  first time it sees it (build, on_open, a click), so the service may be
  created after the page. A writer picked anywhere while it downloads
  (sig_setting_changed "action_model") wins: the end of the download no
  longer switches to this computer.
* Navigation: the page opens routes through its MainWindow (open(route,
  **opts)); outside one it calls app.show_main(route, **opts). "More
  options in Speech & AI" opens settings(section="speech", focus="writer").
  on_open(focus="shortcut" | "hotkey") (Settings › General's shortcut list)
  puts the keyboard focus on the shortcut's Change.
"""

import logging
from datetime import datetime, timedelta

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGraphicsOpacityEffect, QGridLayout,
                               QHBoxLayout, QLabel, QRadioButton, QSizePolicy, QTextEdit, QVBoxLayout,
                               QWidget)

import rich_text
from ui import hy_model, privacy_prompt, theme
from ui.components import (Button, Card, IconButton, Keycaps, PageHeader, ProTag, SettingRow,
                           SettingsRail, icons, label)
from ui.components import clipboard, mini_prompt
from ui.components.controls import keycap_labels
from ui.components.hotkey_capture import HotkeyCaptureButton
from ui.components.model_combo import MARK_DOWNLOAD, PlainNameCombo, combo as _rail_combo
from ui.pages import home as H

logger = logging.getLogger("transcribe")

UPSELL = "Smart Actions"
UPSELL_SPEECH = "Fast cloud transcription"
ACTION_OFF = "transcribe_only"
HOTKEY_KEY = "smart_actions_hotkey"
DEFAULT_HOTKEY = "alt+s"
SPEECH_KEY = "smart_actions_speech_model"      # engine_choice.SMART_SPEECH_KEY
SPEECH_CLOUD = "cloud"                          # engine_choice.SMART_SPEECH_CLOUD
SPEECH_COMBO_WIDTH = 260                        # room for a real model name and its chips
RECENT_MAX = 5

HERO_LINE = "Press, say what you want, press again."
HERO_LINE_TWO = "Press, say what you want,\npress again."     # when one line doesn't fit
EXACT_TEXT = "pastes your exact words."
SET_SHORTCUT = "Set shortcut"

# Settings that change what this page shows.
WATCHED = frozenset({
    "hotkey", "live_assist_hotkey", HOTKEY_KEY, SPEECH_KEY,
    "action_model", "action_api_provider", "action_api_model", "action_api_key",
    "google_api_key", "mistral_api_key", "privacy_mode", "admin_tier_override",
    "backend", "whisper_model", "managed_provider", "mistral_stt_model", "google_stt_model",
})

# ("line", text, px, role, weight) | ("check", text) | ("rich", bold, rest) | ("signoff",)
EXAMPLES = (
    ("mail", "Email", "Email Anna: the demo moves to Thursday. Ask if the deck is ready.",
     (("line", "Subject: Demo moves to Thursday", 11.5, "muted", 400),
      ("line", "Hi Anna,"),
      ("line", "The demo moves to Thursday. Is the deck ready?"),
      ("signoff",))),
    ("list", "To-do list", "To-do: call David, book the room, send the slides",
     (("check", "Call David"), ("check", "Book the room"), ("check", "Send the slides"))),
    ("translate", "Translate", "Translate to Spanish: see you tomorrow at ten",
     (("line", "Spanish", 11.5, "muted", 400),
      ("line", "Nos vemos mañana a las diez.", 15, "h3", 500))),
    ("pen", "Polish", "Clean this up: so um we need the, like, numbers by Monday",
     (("line", "We need the numbers by Monday.", 13.5, "h3", 400),)),
)


# ── small helpers ─────────────────────────────────────────────────────────────
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


def _call(app, method, *args, **kwargs):
    fn = getattr(app, method, None)
    if not callable(fn):
        logger.debug("main_app has no %s()", method)
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("%s() failed", method, exc_info=True)
        return None


def _set(app, key, value):
    """app.set_setting -> True when applied."""
    fn = getattr(app, "set_setting", None)
    if not callable(fn):
        return False
    try:
        return fn(key, value) is not False
    except Exception:
        logger.warning("set_setting(%s) failed", key, exc_info=True)
        return False


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
    return all([_set(app, k, v) for k, v in changes.items()])


def _upsell(app):
    _call(app, "_pro_upsell", UPSELL)


def _text(text, role, px, weight=400, parent=None, wrap=False, italic=False, display=False):
    """A label with a role's colour and an exact font."""
    lb = label(text, role, parent, wrap=wrap)
    f = theme.font(px, weight, display=display)
    if italic:
        f.setItalic(True)
    lb.setFont(f)
    return lb


def _icon_label(name, size, color, parent=None, sw=1.7):
    lb = QLabel(parent)
    lb.setPixmap(icons.pixmap(name, size, color, sw=sw))
    lb.setFixedSize(size, size)
    lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return lb


def _frame(parent, name, css):
    """A QFrame styled by its own rule (theme tokens only)."""
    fr = QFrame(parent)
    fr.setObjectName(name)
    fr.setStyleSheet("QFrame#%s { %s }" % (name, css))
    return fr


def link_button(text, parent=None):
    """A link with a trailing chevron ("More options in Speech & AI ›")."""
    b = Button(text.replace("&", "&&"), "link", "s", parent=parent)
    b.setAccessibleName(text)
    b.setFont(theme.font(13, 600))
    b.setIcon(icons.icon("chev_r", 14, "blue-tx", sw=2))
    b.setIconSize(QSize(14, 14))
    b.setLayoutDirection(Qt.RightToLeft)          # the chevron after the text
    b.setFixedWidth(b.sizeHint().width() + 2)
    return b


# Below this content width the rail moves under the page (the window's
# minimum is 960 px: 728 px for the page, too narrow for a side rail).
NARROW_AT = 940


class RailLayoutMixin:
    """Put the rail beside the main column, or under it when the window is narrow."""
    _stacked = False
    _watched_parent = None
    _body = None
    _rail_widget = None

    def _init_responsive(self, body, rail):
        self._body = body
        self._rail_widget = rail
        self._stacked = False
        self._watched_parent = None

    def _available_width(self):
        p = self.parentWidget()
        return p.width() if p is not None and p.width() > 0 else self.width()

    def _relayout(self):
        if not getattr(self, "_body", None) or not getattr(self, "_rail_widget", None):
            return
        from PySide6.QtWidgets import QBoxLayout
        stacked = self._available_width() < NARROW_AT
        if stacked == getattr(self, "_stacked", False):
            return
        self._stacked = stacked
        self._body.setDirection(QBoxLayout.TopToBottom if stacked else QBoxLayout.LeftToRight)
        self._body.setAlignment(self._rail_widget, (Qt.AlignLeft | Qt.AlignTop) if stacked else Qt.AlignTop)

    def is_stacked(self):
        return getattr(self, "_stacked", False)

    def _watch_parent(self):
        p = self.parentWidget()
        if p is not None and p is not self._watched_parent:
            p.installEventFilter(self)
            self._watched_parent = p

    def eventFilter(self, obj, ev):
        if obj is self._watched_parent and ev.type() == QEvent.Resize:
            self._relayout()
        return False

    def showEvent(self, e):
        self._watch_parent()
        self._relayout()
        super().showEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()


def _first_name(app):
    """The sign-off on the example emails: the signed-in person's first name -
    "" for a guest, a super admin previewing Guest included."""
    import account_actions
    auth = getattr(app, "auth", None)
    if not getattr(auth, "is_authenticated", False):
        return ""
    return account_actions.first_name(app)


def smart_hotkey(app):
    """The Smart Actions shortcut (config form); "" when none is set (e.g. it
    clashed with the dictation key at the upgrade). A missing key is the
    default (main.DEFAULT "alt+s")."""
    return str(_cfg(app).get(HOTKEY_KEY, DEFAULT_HOTKEY) or "").strip().lower()


def dictation_hotkey(app):
    return str(_cfg(app).get("hotkey") or "alt+r").strip().lower()


def _keys_text(hotkey):
    """'alt+s' -> 'Alt + S' (⌥ S on a Mac), as the keycaps read."""
    return " + ".join(keycap_labels(hotkey)) or hotkey


def _hotkey_text(app):
    """How the Smart Actions shortcut reads ("Alt + S"); "" when none is set."""
    hk = smart_hotkey(app)
    if not hk:
        return ""
    import hotkeys
    return hotkeys.display(hk)


def practice_placeholder(app):
    text = _hotkey_text(app)
    return ("Press %s and say it." % text) if text else "Set a shortcut, then say it."


def _norm(hk):
    try:
        import settings_apply
        return settings_apply.norm_hotkey(hk)
    except Exception:
        return "+".join(p.strip().lower() for p in str(hk or "").split("+") if p.strip())


def _copy(text):
    try:
        QGuiApplication.clipboard().setText(text or "")
        return True
    except Exception:
        logger.debug("copy failed", exc_info=True)
        return False


def plan_state(app):
    """(tier, tries left, tries total). Smart Actions are unlimited for Free
    and Pro, so tries left is None for both. Guests sign in first."""
    total = 5
    if _is_pro(app):
        return "pro", None, total
    tier = None
    fn = getattr(app, "current_tier", None)
    if callable(fn):
        try:
            tier = fn()
        except Exception:
            tier = None
    if tier not in ("free", "guest"):
        signed_in = bool(getattr(getattr(app, "auth", None), "is_authenticated", False))
        tier = "free" if signed_in else "guest"
    if tier == "guest":
        return "guest", None, total
    return "free", None, total


def can_use(app):
    """Smart Actions can run now (anyone signed in)."""
    tier, _left, _total = plan_state(app)
    return tier in ("pro", "free")


# ── Recent: history entries that were Smart Actions ──────────────────────────
def _one_line(text):
    return " ".join(str(text or "").split())


def _parse_ts(value):
    try:
        return datetime.strptime(str(value or "")[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def day_label(when, now=None):
    """"Today" / "Yesterday" / "Tue 29 Sep"."""
    now = now or datetime.now()
    if when.date() == now.date():
        return "Today"
    if when.date() == (now - timedelta(days=1)).date():
        return "Yesterday"
    return "%s %d %s" % (when.strftime("%a"), when.day, when.strftime("%b"))


def recent_actions(entries, limit=RECENT_MAX):
    """[{"when", "said", "pasted"}] newest first, from history entries that
    carry "said" and a Smart Action "action" (see the Wave 4 notes)."""
    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        action = str(e.get("action") or "")
        said = str(e.get("said") or "").strip()
        pasted = str(e.get("text") or "").strip()
        if not action or action == ACTION_OFF or not said or not pasted:
            continue
        when = _parse_ts(e.get("timestamp"))
        if when is None:
            continue
        out.append({"when": when, "said": said, "pasted": pasted})
        if len(out) >= limit:
            break
    return out


def load_recent(limit=RECENT_MAX):
    """Worker thread: read history.json and keep the Smart Actions."""
    import history
    return recent_actions(history.load(), limit)


# ── widgets ───────────────────────────────────────────────────────────────────
class ElidedLabel(QLabel):
    """One line, cut with … to its width (CSS text-overflow: ellipsis)."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full if len(self._full) > 40 else "")
        self.setAccessibleName(self._full)
        super().setText(self._full)
        self._elide()

    def full_text(self):
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        w = max(0, self.width())
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, w) if w else self._full
        if shown != super().text():
            QLabel.setText(self, shown)

    def minimumSizeHint(self):
        return QSize(16, super().minimumSizeHint().height())

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 2, super().sizeHint().height())


class _HeroLine(QLabel):
    """The hero's line: one line when it fits, else broken after "want,"
    (never "again." alone on the second line)."""

    def __init__(self, one, two, parent=None):
        super().__init__(one, parent)
        self._one, self._two = one, two
        self.setWordWrap(True)
        self.setAccessibleName(one)

    def full_text(self):
        return self._one

    def _pick(self):
        w = self.width()
        fits = w <= 0 or self.fontMetrics().horizontalAdvance(self._one) <= w
        text = self._one if fits else self._two
        if text != self.text():
            self.setText(text)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._pick()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.FontChange:
            self._pick()


class Pips(QWidget):
    """Five 22 x 6 bars: green for each try left (decorative; the text says it)."""

    def __init__(self, total=5, parent=None):
        super().__init__(parent)
        self._total = total
        self._left = total
        self.setFixedSize(total * 22 + (total - 1) * 3, 6)
        self.setAccessibleName("")

    def set_left(self, left):
        self._left = left
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        for i in range(self._total):
            p.setBrush(theme.qcolor("ok" if i < self._left else "line2"))
            p.drawRoundedRect(QRectF(i * 25, 0, 22, 6), 3, 3)
        p.end()


class _CheckBox(QWidget):
    """The 12 px empty checkbox drawn in a to-do result."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(12, 12)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(theme.qcolor("line2"), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.75, 0.75, 10.5, 10.5), 3, 3)
        p.end()


class PracticeEdit(QTextEdit):
    """Try it's practice box. A Smart Action pastes HTML + plain text (D9):
    the HTML flavour is inserted, so lists, checkboxes and headings show as
    they would in Word or Gmail; anything else goes in as plain text."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(True)
        self.setTabChangesFocus(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def insertFromMimeData(self, source):
        if source is None:
            return
        cur = self.textCursor()
        if source.hasHtml() and (source.html() or "").strip():
            cur.insertHtml(source.html())
        elif source.hasText():
            cur.insertText(source.text())
        else:
            return
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def paste_rich(self, plain, html=""):
        """Insert as if pasted from the clipboard (tests, previews)."""
        from PySide6.QtCore import QMimeData
        md = QMimeData()
        md.setText(plain or "")
        if html:
            md.setHtml(html)
        self.insertFromMimeData(md)


class ExampleCard(Card):
    """What you say -> what it writes, with Try it (a practice box)."""

    def __init__(self, page, icon, name, said, lines, parent=None):
        super().__init__(parent, pad=(16, 14), gap=10)
        self.page = page
        self.name = name
        self.signoff_label = None
        self.setAccessibleName(name)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(_icon_label(icon, 18, "blue-tx", self))
        head.addWidget(_text(name, "h3", 14, 700, self))
        head.addStretch(1)
        self.try_button = Button("Try it", "secondary", "s", parent=self)
        self.try_button.setAccessibleName("Try %s" % name)
        self.try_button.clicked.connect(self._on_try)
        head.addWidget(self.try_button)
        self.add(head)

        said_row = QHBoxLayout()
        said_row.setSpacing(6)
        mic = _icon_label("mic", 14, "faint", self)
        said_row.addWidget(mic, 0, Qt.AlignTop)
        said_row.setAlignment(mic, Qt.AlignTop)
        self.said_label = _text("“%s”" % said, "muted", 12.5, 400, self, wrap=True, italic=True)
        said_row.addWidget(self.said_label, 1)
        self.add(said_row)

        self.paper = _frame(self, "paper",
                            "background: #ffffff; border: 1px solid %s; border-radius: 10px;"
                            % theme.qss_color("line"))
        pl = QVBoxLayout(self.paper)
        pl.setContentsMargins(12, 10, 12, 10)
        pl.setSpacing(3)
        for spec in lines:
            pl.addWidget(self._line(spec))
        pl.addStretch(1)
        self.add(self.paper, 1)

        self.practice = PracticeEdit(self)
        self.practice.setAccessibleName("%s practice box" % name)
        self.practice.setFont(theme.font(13))
        self.practice.document().setDefaultFont(theme.font(13))
        self.practice.hide()
        self.add(self.practice, 1)

    def _line(self, spec):
        kind = spec[0]
        if kind == "check":
            w = QWidget(self.paper)
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            h.addWidget(_CheckBox(w), 0, Qt.AlignVCenter)
            h.addWidget(_text(spec[1], "label", 12.5, 400, w), 1)
            return w
        if kind == "rich":
            lb = _text("", "label", 12.5, 400, self.paper, wrap=True)
            lb.setTextFormat(Qt.RichText)
            lb.setText('<span style="font-weight:700; color:%s">%s</span>%s'
                       % (theme.qss_color("ink"), spec[1], spec[2]))
            lb.setAccessibleName(spec[1] + spec[2])
            return lb
        if kind == "signoff":
            self.signoff_label = _text("", "label", 12.5, 400, self.paper, wrap=True)
            self.set_signoff(_first_name(self.page.app))
            return self.signoff_label
        text = spec[1]
        px = spec[2] if len(spec) > 2 else 12.5
        role = spec[3] if len(spec) > 3 else "label"
        weight = spec[4] if len(spec) > 4 else 400
        return _text(text, role, px, weight, self.paper, wrap=True)

    def set_signoff(self, first_name):
        """The email example signs off with the account's first name."""
        if self.signoff_label is not None:
            self.signoff_label.setText("Best, %s" % first_name if first_name else "Best,")

    def practicing(self):
        return not self.practice.isHidden()

    def _on_try(self):
        if self.practicing():
            self.close_practice()
            return
        self.page.try_example(self)

    def open_practice(self, placeholder):
        self.practice.setPlaceholderText(placeholder)
        # Same size as the result it replaces, so the grid doesn't jump.
        h = self.paper.height() if self.paper.height() > 0 else self.paper.sizeHint().height()
        self.practice.setFixedHeight(max(64, h))
        self.paper.hide()
        self.practice.show()
        self.practice.setFocus(Qt.OtherFocusReason)
        self.try_button.setText("Done")
        self.try_button.setAccessibleName("Done trying %s" % self.name)
        self.try_button.setFixedWidth(self.try_button.sizeHint().width())

    def close_practice(self):
        self.practice.hide()
        self.practice.clear()
        self.paper.show()
        self.try_button.setText("Try it")
        self.try_button.setAccessibleName("Try %s" % self.name)
        self.try_button.setFixedWidth(self.try_button.sizeHint().width())


class RecentRow(QFrame):
    """11:05 · “what you said” / what was pasted · Use my exact words · Copy."""

    def __init__(self, item, top_border=False, parent=None):
        super().__init__(parent)
        self.item = item
        self.setObjectName("recentRow")
        if top_border:
            self.setStyleSheet("QFrame#recentRow { border: none; border-top: 1px solid %s; }"
                               % theme.qss_color("line"))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(12)
        tm = _text(item["when"].strftime("%H:%M"), "muted", 12.5, 400, self)
        tm.setFixedWidth(40)
        lay.addWidget(tm)
        mid = QVBoxLayout()
        mid.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(_icon_label("mic", 13, "faint", self))
        # The pasted result is Markdown (history keeps the machine format):
        # one line of what it reads as ("☐ Call David ☐ Book the room").
        said, pasted = _one_line(item["said"]), _one_line(rich_text.to_plain(item["pasted"]))
        self.said_label = ElidedLabel("“%s”" % said, self)
        theme.set_role(self.said_label, "muted")
        self.said_label.setFont(theme.font(12.5))
        self.said_label.setFixedHeight(18)               # CSS line-height 18px
        top.addWidget(self.said_label, 1)
        mid.addLayout(top)
        self.pasted_label = ElidedLabel(pasted, self)
        theme.set_role(self.pasted_label, "label")
        self.pasted_label.setFont(theme.font(13.5))
        self.pasted_label.setFixedHeight(20)             # CSS line-height 20px
        mid.addWidget(self.pasted_label)
        lay.addLayout(mid, 1)
        self.exact_button = Button("Use my exact words", "ghost", "s", parent=self)
        self.exact_button.setToolTip("Copy what you said")
        # Every row has the same two buttons: say which one this is.
        self.exact_button.setAccessibleDescription("Copies “%s”" % said)
        self.exact_button.clicked.connect(self._copy_said)
        lay.addWidget(self.exact_button)
        self.copy_button = IconButton("copy", "Copy result", size=32, icon_size=16, parent=self)
        self.copy_button.setAccessibleDescription(pasted)
        self.copy_button.clicked.connect(self._copy_pasted)
        lay.addWidget(self.copy_button)

    def _copy_said(self):
        if _copy(self.item["said"]):
            self.exact_button.setText("Copied")
            QTimer.singleShot(1500, lambda: _alive(self) and self.exact_button.setText("Use my exact words"))

    def _copy_pasted(self):
        # HTML (Word, Gmail, Slack get real lists) + plain text with nice characters.
        if clipboard.copy_rich(self.item["pasted"]):
            self.copy_button.set_icon("check", "ok-tx")
            QTimer.singleShot(1500, lambda: _alive(self) and self.copy_button.set_icon("copy"))


class _WriterOption(QRadioButton):
    """A "Writes with" radio card: plain name (+ PRO), the real model under it."""

    def __init__(self, choice, name, model_text, pro=False, parent=None):
        super().__init__(parent)
        self.choice = choice
        self.pro = bool(pro)
        self._kbd = False
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setAccessibleName(name)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12 + 16 + 10, 10, 12, 10)
        lay.setSpacing(0)
        col = QVBoxLayout()
        col.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.name_label = _text(name, "label", 13.5, 600, self)
        self.name_label.setFixedHeight(20)               # CSS line-height 20px
        top.addWidget(self.name_label)
        if pro:
            top.addWidget(ProTag("s", parent=self))
        top.addStretch(1)
        col.addLayout(top)
        self.model_label = label(model_text, "model", self)
        self.model_label.setFixedHeight(16)              # ds.model_name: line-height 16px
        col.addWidget(self.model_label)
        lay.addLayout(col, 1)
        for ch in self.findChildren(QWidget):
            ch.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.set_model(model_text)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_model(self, text):
        self.model_label.setText(text)
        self.setAccessibleDescription(text)

    def sizeHint(self):
        return self.layout().sizeHint()

    def minimumSizeHint(self):
        return self.layout().minimumSize()

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def changeEvent(self, e):
        if e.type() == QEvent.EnabledChange:
            # Paused (Privacy mode): the whole card fades, labels too; the row's hint says why.
            if self.isEnabled():
                self.setGraphicsEffect(None)
            else:
                fx = QGraphicsOpacityEffect(self)
                fx.setOpacity(0.5)
                self.setGraphicsEffect(fx)
        super().changeEvent(e)

    def focusInEvent(self, e):
        self._kbd = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._kbd = False
        super().focusOutEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        on = self.isChecked()
        purple = on and self.pro
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        edge = theme.qcolor("pro-line" if purple else "line")
        if self.underMouse() and not purple and self.isEnabled():
            edge = theme.qcolor("line2")
        p.setPen(QPen(edge, 1))
        p.setBrush(theme.qcolor("pro-sub" if purple else "white"))
        p.drawRoundedRect(rect, 10, 10)
        # The radio (16 px, CSS accent-color blue).
        dot = QRectF(12.75, 12.75, 14.5, 14.5)
        if on:
            p.setPen(QPen(theme.qcolor("blue"), 1.5))
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(dot)
            p.setPen(Qt.NoPen)
            p.setBrush(theme.qcolor("blue"))
            p.drawEllipse(QRectF(16.5, 16.5, 7, 7))
        else:
            p.setPen(QPen(theme.qcolor("muted"), 1.2))
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(dot)
        if self.hasFocus() and self._kbd:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(theme.qcolor("blue-l"), 2))
            p.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 10, 10)
        p.end()


class SmartShortcut(HotkeyCaptureButton):
    """The rail's Smart Actions shortcut: the Dictation page's keys + Change
    (ui/components/hotkey_capture). With none set (it clashed at the upgrade)
    it reads "Not set · Set shortcut". There is no remove: D2 offers Change
    only. The page checks clashes and writes the config."""

    def __init__(self, hotkey="", parent=None):
        super().__init__(hotkey, parent=parent, accessible_name="Smart Actions shortcut", allow_mouse=False)
        # The rail's Change is a text link beside 24 px keycaps (as on Dictation).
        self.button.setFixedHeight(24)
        # While it waits, "Press keys…" sits where the keycaps were, as an
        # empty dashed key slot.
        self.prompt_label.setFixedHeight(24)
        self.prompt_label.setStyleSheet(
            "QLabel { color: %s; background: %s; border: 1px dashed %s; border-radius: 6px; padding: 0px 8px; }"
            % (theme.qss_color("blue-tx"), theme.qss_color("blue-sub"), theme.qss_color("blue-l")))

    def _show_keys(self):
        super()._show_keys()
        if not self._hotkey and not self._capturing:
            self.button.setText(SET_SHORTCUT)
            self.button.setAccessibleName("Set the Smart Actions shortcut")
        self.updateGeometry()


# ── the page ──────────────────────────────────────────────────────────────────
class SmartActionsPage(RailLayoutMixin, QWidget):
    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.setProperty("gusan", "page")
        self._recent_items = []
        self._recent_loaded = False
        self._recent_gen = 0             # a newer reload makes an older answer stale
        self._pending_download = None
        self._dl_connected = None        # the app.downloads object this page listens to
        self._hotkeys_suspended = False
        self._writer_note = ""           # e.g. a failed download, under "Writes with"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = PageHeader("Smart Actions", parent=self)
        root.addWidget(self.header)

        body = QHBoxLayout()
        body.setContentsMargins(32, 0, 32, 28)
        body.setSpacing(24)
        main = QVBoxLayout()
        main.setSpacing(16)
        main.addLayout(self._build_hero())
        main.addWidget(self._build_examples())
        main.addWidget(self._build_recent())
        main.addStretch(1)
        body.addLayout(main, 1)
        self.rail = self._build_rail()
        body.addWidget(self.rail, 0, Qt.AlignTop)
        root.addLayout(body, 1)
        self._init_responsive(body, self.rail)

        self._connect_downloads()
        self.refresh()
        self.reload_recent()

    # ── hero ──
    def _build_hero(self):
        # A layout on the page, not a widget: the big keys' soft shadow then
        # paints onto the page instead of being clipped at the hero's edge.
        w = self
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 2, 0, 10)
        lay.setSpacing(22)
        # The Smart Actions shortcut drawn big (Home / Dictation's HeroKeys).
        # A long shortcut (Ctrl + Shift + Space) is drawn smaller so it fits.
        self.hero_keys = H.HeroKeys(smart_hotkey(self.app) or DEFAULT_HOTKEY, 56, 24, w, max_width=190)
        lay.addWidget(self.hero_keys, 0, Qt.AlignVCenter)
        # None set (it clashed at the upgrade): one button that starts the capture.
        self.hero_set_button = Button(SET_SHORTCUT, "secondary", "m", icon="keyboard", parent=w)
        self.hero_set_button.setAccessibleName("Set the Smart Actions shortcut")
        self.hero_set_button.clicked.connect(self._on_set_shortcut)
        self.hero_set_button.hide()
        lay.addWidget(self.hero_set_button, 0, Qt.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(8)
        # Wraps in a narrow window (the tries / sign-up state needs its room).
        self.hero_line = _HeroLine(HERO_LINE, HERO_LINE_TWO, w)
        theme.set_role(self.hero_line, "h2")
        self.hero_line.setFont(theme.font(19, 700, display=True))
        self.hero_line.setMinimumWidth(160)
        col.addWidget(self.hero_line)
        sub = QHBoxLayout()
        sub.setSpacing(8)
        # "<Alt + R> pastes your exact words." with the real dictation keys.
        self.exact_keys = Keycaps.from_hotkey(dictation_hotkey(self.app), parent=w)
        sub.addWidget(self.exact_keys, 0, Qt.AlignVCenter)
        self.exact_label = _text(EXACT_TEXT, "muted", 13.5, 400, w)
        sub.addWidget(self.exact_label, 0, Qt.AlignVCenter)
        sub.addStretch(1)
        col.addLayout(sub)
        lay.addLayout(col, 1)

        # Right: one of four quiet states.
        self.state_tries = QWidget(w)
        v = QVBoxLayout(self.state_tries)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.tries_label = _text("5 of 5 free tries left", "muted", 12.5, 600, self.state_tries)
        v.addWidget(self.tries_label, 0, Qt.AlignRight)
        self.pips = Pips(5, self.state_tries)
        v.addWidget(self.pips, 0, Qt.AlignRight)

        self.state_used = QWidget(w)
        v = QVBoxLayout(self.state_used)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(_text("No free tries left", "muted", 12.5, 600, self.state_used), 0, Qt.AlignRight)
        self.upgrade_button = Button("Upgrade to Pro", "pro", "m", parent=self.state_used)
        self.upgrade_button.setFont(theme.font(13, 700))
        self.upgrade_button.setFixedHeight(34)
        self.upgrade_button.clicked.connect(lambda: _upsell(self.app))
        v.addWidget(self.upgrade_button, 0, Qt.AlignRight)

        self.state_guest = QWidget(w)
        v = QVBoxLayout(self.state_guest)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(_text("Sign in to use Smart Actions", "muted", 12.5, 600, self.state_guest), 0, Qt.AlignRight)
        self.signup_button = Button("Sign up free", "primary", "s", parent=self.state_guest)
        self.signup_button.clicked.connect(self._sign_up)
        v.addWidget(self.signup_button, 0, Qt.AlignRight)

        self.state_pro = QWidget(w)
        h = QHBoxLayout(self.state_pro)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(_icon_label("check_circle", 16, "ok", self.state_pro))
        h.addWidget(_text("Unlimited", "body", 13, 600, self.state_pro))

        for s in (self.state_tries, self.state_used, self.state_guest, self.state_pro):
            s.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            lay.addWidget(s, 0, Qt.AlignVCenter | Qt.AlignRight)
        return lay

    # ── What you can say ──
    def _build_examples(self):
        w = QWidget(self)
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        col.addWidget(label("What you can say", "h2", w))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self.examples = []
        for i, (icon, name, said, lines) in enumerate(EXAMPLES):
            card = ExampleCard(self, icon, name, said, lines, w)
            grid.addWidget(card, i // 2, i % 2)
            self.examples.append(card)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        col.addLayout(grid)
        # (The hero's muted line already says Alt+R pastes the exact words.)
        return w

    def example(self, name):
        for card in self.examples:
            if card.name == name:
                return card
        return None

    # ── Recent ──
    def _build_recent(self):
        w = QWidget(self)
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        col.addWidget(label("Recent", "h2", w))
        self.recent_card = Card(w, pad=(18, 2), gap=0)
        self.recent_card.layout().setContentsMargins(18, 2, 18, 6)
        self.recent_card.setAccessibleName("Recent Smart Actions")
        col.addWidget(self.recent_card)
        self.recent_empty = _text("Your Smart Actions show up here.", "muted", 12.5, 400, self.recent_card)
        self.recent_empty.setContentsMargins(0, 12, 0, 8)
        self.recent_card.add(self.recent_empty)
        self.recent_rows = []
        return w

    def reload_recent(self):
        """Read history on a worker; the rows update when it answers (only
        the newest reload's answer is drawn)."""
        self._recent_gen += 1
        gen = self._recent_gen
        try:
            import api_keys
            api_keys.run_then(load_recent, then=lambda items, g=gen: self._on_recent(items, g),
                              error=lambda _exc, g=gen: self._on_recent([], g))
        except Exception:
            logger.debug("Recent Smart Actions could not load", exc_info=True)
            self._on_recent([], gen)

    def _on_recent(self, items, gen=None):
        if not _alive(self) or (gen is not None and gen != self._recent_gen):
            return
        self._recent_loaded = True
        self.set_recent(items or [])

    def set_recent(self, items):
        lay = self.recent_card.layout()
        for row in self.recent_rows:
            lay.removeWidget(row)
            row.hide()
            row.deleteLater()
        self.recent_rows = []
        self._recent_items = list(items)
        self.recent_empty.setVisible(not items)
        group = None
        now = datetime.now()
        first_in_group = True
        for item in items:
            g = day_label(item["when"], now)
            if g != group:
                group = g
                head = _text(g, "muted", 12, 700, self.recent_card)
                head.setContentsMargins(0, 10, 0, 2)
                lay.addWidget(head)
                self.recent_rows.append(head)
                first_in_group = True
            row = RecentRow(item, top_border=not first_in_group, parent=self.recent_card)
            lay.addWidget(row)
            self.recent_rows.append(row)
            first_in_group = False
        # New widgets join the end of the Tab chain (after the rail): put the
        # rows where they are on screen, after "What you can say".
        prev = self.examples[-1].practice
        for row in self.recent_widgets():
            for w in (row.exact_button, row.copy_button):
                QWidget.setTabOrder(prev, w)
                prev = w

    def recent_widgets(self):
        return [r for r in self.recent_rows if isinstance(r, RecentRow)]

    # ── rail ──
    def _build_rail(self):
        rail = SettingsRail("Smart Actions settings", parent=self)

        # Shortcut: keys + Change (the Dictation page's control). Not Pro.
        self.shortcut = SmartShortcut(smart_hotkey(self.app), parent=rail)
        self.shortcut.captured.connect(self._on_shortcut_captured)
        self.shortcut.refused.connect(self._on_shortcut_problem)
        self.shortcut.cancelled.connect(self._on_shortcut_cancelled)
        self.shortcut.capturing_changed.connect(self._on_capturing)
        self.shortcut_row = rail.add_row(SettingRow("Shortcut", self.shortcut))
        self._problem_timer = QTimer(self)
        self._problem_timer.setSingleShot(True)
        self._problem_timer.setInterval(8000)
        self._problem_timer.timeout.connect(lambda: self._on_shortcut_problem(""))

        # Transcribes with: Same as Dictation / a Whisper model / Gusan Cloud (PRO).
        sp = QWidget(rail)
        sv = QVBoxLayout(sp)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(4)
        self.speech_combo = _rail_combo(PlainNameCombo(sp), "Transcribes with", width=SPEECH_COMBO_WIDTH)
        self.speech_combo.enable_row_marks()      # whole rows: chips and a download icon at the right
        self._fill_speech_combo()
        self.speech_combo.currentIndexChanged.connect(self._on_speech_model)
        sv.addWidget(self.speech_combo, 0, Qt.AlignLeft)
        self.speech_line = label("", "model", sp)
        self.speech_line.setContentsMargins(13, 0, 0, 0)
        sv.addWidget(self.speech_line)
        # While the Armenian model downloads: a way to stop it.
        self.speech_cancel = Button(hy_model.CANCEL + " download", "link", "s", parent=sp)
        self.speech_cancel.setAccessibleName("Cancel the Armenian model download")
        self.speech_cancel.clicked.connect(self._cancel_speech_download)
        self.speech_cancel.hide()
        sv.addWidget(self.speech_cancel, 0, Qt.AlignLeft)
        sp.setAccessibleName("Transcribes with")
        self.speech_row = rail.add_row(SettingRow("Transcribes with", sp, stacked=True))
        self._speech_note = ""            # e.g. a download that didn't start

        wr = QWidget(rail)
        self._writers_lay = QVBoxLayout(wr)
        self._writers_lay.setContentsMargins(0, 0, 0, 0)
        self._writers_lay.setSpacing(8)
        self.writer_group = QButtonGroup(wr)
        self.writer_group.setExclusive(True)
        self.opt_local = _WriterOption("local", "This computer", "", parent=wr)
        self.opt_cloud = _WriterOption("cloud", "Gusan Cloud", "", pro=True, parent=wr)
        self.opt_own = _WriterOption("own_key", "Your own key", "", parent=wr)
        for opt in (self.opt_local, self.opt_cloud, self.opt_own):
            self.writer_group.addButton(opt)
            self._writers_lay.addWidget(opt)
            opt.clicked.connect(lambda _=False, o=opt: self._on_writer(o.choice))
        self.opt_own.hide()
        wr.setAccessibleName("Writes with")
        self.writer_row = rail.add_row(SettingRow("Writes with", wr, stacked=True))

        foot = QWidget(rail)
        fh = QHBoxLayout(foot)
        fh.setContentsMargins(0, 0, 0, 0)
        more = link_button("More options in Speech & AI", foot)
        more.clicked.connect(lambda: self._open_route("settings", section="speech", focus="writer"))
        fh.addWidget(more)
        fh.addStretch(1)
        self.more_button = more
        rail.set_foot(foot)
        return rail

    # ── state ──
    def refresh(self):
        """Redraw everything from cfg + plan (cheap: no network, a stat at most)."""
        cfg = _cfg(self.app)
        tier, left, total = plan_state(self.app)
        hk = smart_hotkey(self.app)
        if hk:
            self.hero_keys.set_hotkey(hk)
        self.hero_keys.setVisible(bool(hk))
        self.hero_set_button.setVisible(not hk)
        self.exact_keys.set_hotkey(dictation_hotkey(self.app))
        self.exact_label.setAccessibleName("%s %s" % (_keys_text(dictation_hotkey(self.app)), EXACT_TEXT))
        # Free and Pro both have unlimited Smart Actions. The old try counter stays
        # in the tree so a layout test can still find it, but it is not shown.
        self.state_pro.setVisible(tier in ("pro", "free"))
        self.state_guest.setVisible(tier == "guest")
        self.state_used.setVisible(False)
        self.state_tries.setVisible(False)

        if not self.shortcut.is_capturing():
            self.shortcut.set_hotkey(hk)
        self._connect_downloads()
        self._refresh_speech()
        self._refresh_writers()
        for card in self.examples:
            if card.practicing():
                card.practice.setPlaceholderText(practice_placeholder(self.app))

    # ── Transcribes with ──
    def _speech_rows(self):
        """The picker's rows (engine_choice.smart_speech_rows): real model names, a LOCAL / CLOUD
        chip and PRO where a plan is needed; "Same as Dictation" names the model Dictation uses."""
        try:
            import engine_choice
            return engine_choice.smart_speech_rows(_cfg(self.app), _is_pro(self.app))
        except Exception:
            logger.debug("smart_speech_rows failed", exc_info=True)
            return [{"value": "", "name": "Same as Dictation", "title": "Same as Dictation",
                     "line": "Same as Dictation", "note": "", "tags": (), "pro": False, "group": "",
                     "tip": ""}]

    def _fill_speech_combo(self, rows=None):
        rows = self._speech_rows() if rows is None else rows
        box = self.speech_combo
        box.blockSignals(True)
        box.fill_rows(rows)
        box.blockSignals(False)
        self._speech_fill = rows
        self._mark_speech_rows()

    def _speech_fill_key(self):
        """What the list depends on: its rows (the plan, the model Dictation uses, the Gusan Cloud
        provider's model name)."""
        return self._speech_rows()

    def _speech_state(self, mid):
        """A Whisper model's state in app.downloads ("ready", "missing",
        "downloading", "failed"), or None when it isn't known: no download
        service, or its first look at the disk hasn't run yet."""
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

    def _mark_speech_rows(self):
        """A download icon on a Whisper model that is not on this computer; nothing on the rest
        (the chips - LOCAL, CLOUD, PRO - are part of the row)."""
        box = self.speech_combo
        try:
            import hardware
            info = hardware.info
            is_model = hardware.is_speech_id         # a Whisper model or the Armenian one
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
            if value and value != SPEECH_CLOUD and is_model(value) and self._speech_state(value) in ("missing", "failed"):
                mark = MARK_DOWNLOAD
                note = getattr(info(value), "size", "") or ""
            box.set_choice_mark(i, mark, note)

    def _speech_downloaded(self):
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

    def _speech_engine(self):
        try:
            import engine_choice
            return engine_choice.runs_on(self.app, "smart_actions_speech", downloaded=self._speech_downloaded())
        except Exception:
            logger.debug("runs_on smart_actions_speech failed", exc_info=True)
            return {}

    def speech_value(self):
        """What really transcribes a Smart Action now, as a picker value: a
        "cloud" pick paused by Privacy mode (or a lapsed Pro) runs like
        Dictation, so it reads Same as Dictation."""
        value = str(_cfg(self.app).get(SPEECH_KEY) or "").strip()
        try:
            import engine_choice
            backend, model = engine_choice.smart_speech_session(_cfg(self.app), _is_pro(self.app))
        except Exception:
            return ""
        if backend == "managed":
            return SPEECH_CLOUD
        if backend == "local":
            return model
        return "" if value == SPEECH_CLOUD or self.speech_combo.findData(value) < 0 else value

    def _speech_line(self):
        """The muted line under the picker: what it really runs on."""
        ro = self._speech_engine()
        if not ro:
            return ""
        value = self.speech_value()
        if not value:
            # The picker already says "Same as Dictation": just the model, with its tag.
            return ro.get("tagged") or ro.get("model_name") or ""
        if ro.get("kind") == "cloud":
            return ro.get("tagged") or ro.get("label") or ""
        name = ro.get("model_name") or ""
        mid = ro.get("model_id") or value
        dl = getattr(self.app, "downloads", None)
        state = None
        if dl is not None:
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
        if hy_model.is_hy(mid):
            return (ro.get("tagged") or name) + " · Armenian only"      # (any other language: Whisper reads it)
        return ro.get("tagged") or name

    def _refresh_speech(self):
        rows = self._speech_rows()
        if rows != getattr(self, "_speech_fill", None):
            self._fill_speech_combo(rows)           # the plan, Dictation's model and the cloud model's name follow
        box = self.speech_combo
        self._mark_speech_rows()                    # the download icons follow the disk
        i = box.findData(self.speech_value())
        box.blockSignals(True)
        box.setCurrentIndex(i if i >= 0 else 0)
        box.blockSignals(False)
        line = self._speech_line()
        self.speech_line.setText(line)
        self.speech_line.setVisible(bool(line))
        self.speech_cancel.setVisible(hy_model.can_cancel(self.app))
        box.setAccessibleDescription(line)
        self.speech_row.set_hint(self._speech_note)

    def _on_speech_model(self, _i):
        """A row was picked. Gusan Cloud and a model that is not on this
        computer ask first (each puts the real choice back at once, so the
        box never shows a pick that did not happen); the rest apply."""
        value = self.speech_combo.currentData()
        value = "" if value is None else str(value)
        self._speech_note = ""
        if value == SPEECH_CLOUD:
            self._refresh_speech()                  # back to what is really set
            self._pick_gusan_cloud()
        elif value and self._speech_state(value) in ("missing", "failed"):
            self._refresh_speech()
            self._pick_missing_model(value)
        else:
            self._apply_speech(value)

    def _pick_gusan_cloud(self):
        """Gusan Cloud: Pro first (the Pro window), then Privacy mode (turn it
        off, or keep it), else just select it."""
        if not _is_pro(self.app):
            _call(self.app, "_pro_upsell", UPSELL_SPEECH)
            return
        turn_off = False
        if _cfg(self.app).get("privacy_mode"):
            if not privacy_prompt.confirm_turn_off_privacy(self, "Gusan Cloud"):
                return
            turn_off = True
        self._apply_speech(SPEECH_CLOUD, turn_off_privacy=turn_off)

    def _pick_missing_model(self, mid):
        """A Whisper model that is not on this computer: a small popup asks;
        Download starts it and picks it, Not now leaves everything as it is."""
        if hy_model.is_hy(mid):
            if hy_model.confirm_download(self, "Smart Actions"):
                self._apply_speech(mid)
            return
        try:
            import hardware
            info = hardware.MODEL_INFO.get(mid)
        except Exception:
            info = None
        plain = getattr(info, "plain", "") or mid
        real = getattr(info, "real", "")
        size = getattr(info, "size", "")
        if mini_prompt.confirm_download(self, plain, real, size):
            self._apply_speech(mid)

    def _cancel_speech_download(self):
        """Stop the Armenian model's download. A pick of it that is not on this computer would only
        start the download again at the next Smart Action, so "Same as Dictation" is put back."""
        if not hy_model.cancel(self.app):
            return
        if self.speech_value() == hy_model.HY_ID:
            _set_many(self.app, {SPEECH_KEY: ""})
        self._speech_note = "The download was stopped. Same as Dictation is back."
        self._refresh_speech()

    def _apply_speech(self, value, turn_off_privacy=False):
        """Save the pick through the controller (and start its download when
        the model is not on disk); then draw what is really set."""
        try:
            import engine_choice
            res = engine_choice.set_smart_speech(self.app, value, turn_off_privacy=turn_off_privacy)
        except Exception:
            logger.warning("set_smart_speech failed", exc_info=True)
            res = {"ok": False, "problem": "error", "changes": {}, "upsell": ""}
        if not res.get("ok"):
            problem = res.get("problem")
            self._refresh_speech()                  # back to what is really set
            if problem == "pro":
                _call(self.app, "_pro_upsell", res.get("upsell") or UPSELL_SPEECH)
            return
        if value and value != SPEECH_CLOUD:
            self._download_speech_model(value)
        _set_many(self.app, res.get("changes"))     # refused: the redraw shows the real value
        self._refresh_speech()

    def _download_speech_model(self, mid):
        """A Whisper model not on disk yet: start it now (the controller's
        fetcher joins this download if a Smart Action runs first)."""
        on_disk = self._speech_downloaded()
        if on_disk is None or on_disk(mid):
            return
        self._connect_downloads()
        dl = getattr(self.app, "downloads", None)
        try:
            dl.start(mid)
        except Exception:
            logger.warning("Speech model download failed to start", exc_info=True)
            self._speech_note = "The download didn't start. Try again."

    def _writer_state(self):
        """(checked choice or None, own-key line or None) from what really
        writes (engine_choice.runs_on). None: no AI model writes yet - the
        built-in rules run (a Free user's default "rule_based", or Privacy
        mode pausing a cloud writer), so no option is drawn as chosen."""
        try:
            import engine_choice
            ro = engine_choice.runs_on(self.app, "smart_actions")
        except Exception:
            logger.debug("engine_choice failed", exc_info=True)
            return "local", None
        kind = ro.get("kind")
        if kind == "cloud":
            return "cloud", None
        if kind == "own_key":
            if ro.get("problem") == "needs_key":
                return "own_key", "%s · needs a key" % (ro.get("plain") or "")
            return "own_key", " · ".join(x for x in (ro.get("plain"), ro.get("model_name")) if x)
        if kind == "local":
            return "local", None
        return None, None

    def _local_line(self):
        """("Qwen 1.5B · offline" / "· downloading 42%" / "· 1.12 GB download", model id)."""
        try:
            import engine_choice
            mid = engine_choice.local_writer(_cfg(self.app))
            info = engine_choice.LOCAL_WRITERS.get(mid, (mid, mid, "", ""))
            name, size = info[1], (info[2] if len(info) > 2 else "")
        except Exception:
            return "Qwen 1.5B · offline", None
        dl = getattr(self.app, "downloads", None)
        state = None
        if dl is not None:
            try:
                state = dl.state(mid)
            except Exception:
                state = None
        if state == "downloading" or self._pending_download == mid:
            pct = None
            try:
                prog = dl.progress_of(mid) if dl is not None else None
                pct = prog[0] if prog else None
            except Exception:
                pct = None
            if pct is None:
                return "%s · downloading" % name, mid
            return "%s · downloading %d%%" % (name, pct), mid
        ready = state == "ready"
        if not ready:
            try:
                import local_llm
                ready = bool(local_llm.model_downloaded(mid))      # a stat
            except Exception:
                ready = False
        if ready:
            return "%s · offline" % name, mid
        # Picking it starts the download at once: say how big it is first.
        return ("%s · %s download" % (name, size)) if size else ("%s · not downloaded" % name), mid

    def _refresh_writers(self):
        try:
            import engine_choice
            cloud_model = engine_choice.CLOUD_SMART_MODEL
        except Exception:
            cloud_model = "DeepSeek V4.1 Flash"
        line, _mid = self._local_line()
        self.opt_local.set_model(line)
        self.opt_cloud.set_model("%s · fastest" % cloud_model)
        checked, own_line = self._writer_state()
        self.opt_own.setVisible(checked == "own_key")
        if own_line:
            self.opt_own.set_model(own_line)
        target = {"local": self.opt_local, "cloud": self.opt_cloud, "own_key": self.opt_own}.get(checked)
        if target is not None:
            target.setChecked(True)
        else:
            # Nothing writes with AI yet: no option is drawn as the chosen one.
            self.writer_group.setExclusive(False)
            for opt in (self.opt_local, self.opt_cloud, self.opt_own):
                opt.setChecked(False)
            self.writer_group.setExclusive(True)
        privacy = bool(_cfg(self.app).get("privacy_mode"))
        self.opt_cloud.setEnabled(not privacy)           # Privacy mode keeps the words on this computer
        for opt in (self.opt_local, self.opt_cloud, self.opt_own):
            opt.update()
        if privacy:
            hint = "Privacy mode keeps it on this computer."
        else:
            hint = self._writer_note or ("Pick one to write with AI." if checked is None else "")
        self.writer_row.set_hint(hint)

    def checked_writer(self):
        b = self.writer_group.checkedButton()
        return b.choice if b is not None else None

    # ── actions ──
    def _gate(self):
        """Smart Actions can't run: a guest is asked to sign in. Signed-in
        Free and Pro are not counted and are not sent to the Pro window."""
        if plan_state(self.app)[0] == "guest":
            self._sign_up()
        else:
            _upsell(self.app)

    def try_example(self, card):
        """Try it (if Smart Actions can run): a practice box to paste into.
        The Smart Actions shortcut does the rest; nothing is switched on."""
        if not can_use(self.app):
            self._gate()
            return
        for other in self.examples:
            if other is not card and other.practicing():
                other.close_practice()
        card.open_practice(practice_placeholder(self.app))
        self.refresh()

    def _sign_up(self):
        if callable(getattr(self.app, "show_auth_gate", None)):
            _call(self.app, "show_auth_gate")
        else:
            _upsell(self.app)

    def _on_set_shortcut(self):
        """The hero's "Set shortcut": start the rail's capture."""
        if not self.shortcut.is_capturing():
            self.shortcut.start_capture()

    def _on_capturing(self, on):
        # Anyone may change the shortcut (only using it is gated).
        if on:
            self._on_shortcut_problem("")
        # The global hooks would see Alt+R / Alt+S first and start a dictation.
        if bool(on) != self._hotkeys_suspended:
            self._hotkeys_suspended = bool(on)
            _call(self.app, "suspend_hotkeys", bool(on))

    def _on_shortcut_cancelled(self):
        self.shortcut.set_hotkey(smart_hotkey(self.app))

    def _on_shortcut_problem(self, text):
        row = self.shortcut_row
        row.set_hint(text)
        theme.set_role(row.hint_label, "error" if text else "hint")
        if text:
            self._problem_timer.start()

    def shortcut_problem(self):
        """The refusal shown under "Shortcut" ("" when none)."""
        hl = self.shortcut_row.hint_label
        return hl.text() if not hl.isHidden() else ""

    def _on_shortcut_captured(self, hk):
        cfg = _cfg(self.app)
        hk = _norm(hk)
        shown = _keys_text(hk)
        problem = ""
        if hk and hk == _norm(cfg.get("hotkey") or "alt+r"):
            problem = "%s starts dictation. Pick another." % shown
        elif hk and hk == _norm(cfg.get("live_assist_hotkey")):
            problem = "%s opens Live Assistant. Pick another." % shown
        elif not hk or not _set(self.app, HOTKEY_KEY, hk):
            problem = "Couldn't use %s. Try another." % shown
        self._on_shortcut_problem(problem)
        self.shortcut.set_hotkey(smart_hotkey(self.app))   # the keys the config really has
        self.refresh()

    def _on_writer(self, choice):
        try:
            import engine_choice
        except Exception:
            self.refresh()
            return
        self._writer_note = ""
        if choice == "own_key":
            self._refresh_writers()
            return
        res = engine_choice.set_writer(self.app, choice)
        problem = res.get("problem")
        if res.get("ok"):
            if choice != "local":
                self._pending_download = None       # don't switch back when a download ends
            _set_many(self.app, res.get("changes"))
        elif problem == "pro":
            self._refresh_writers()                 # not drawn as chosen behind the Pro window
            _upsell(self.app)
        elif problem == "needs_download":
            self._download_local_writer()
        self._refresh_writers()

    def _download_local_writer(self):
        try:
            import engine_choice
            mid = engine_choice.local_writer(_cfg(self.app))
        except Exception:
            mid = None
        self._connect_downloads()
        dl = getattr(self.app, "downloads", None)
        started = False
        if dl is not None and mid:
            try:
                started = bool(dl.start(mid))
            except Exception:
                logger.warning("Writer download failed to start", exc_info=True)
        if started:
            self._pending_download = mid
            self._refresh_writers()
        else:
            self._open_route("settings", section="speech", focus="writer")

    def _connect_downloads(self):
        """Listen to app.downloads once it exists (Wave 4 may create it after
        this page). PySide drops the connections when the page is deleted."""
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

    def _is_local_writer(self, name):
        try:
            import engine_choice
            return name == engine_choice.local_writer(_cfg(self.app))
        except Exception:
            return False

    def _is_speech_model(self, name):
        """``name`` is the Whisper model the Smart Actions picker shows."""
        return bool(name) and name == self.speech_value()

    def _on_dl_progress(self, name, *_rest):
        if not _alive(self):
            return
        if self._is_local_writer(name):
            line, _mid = self._local_line()
            self.opt_local.set_model(line)
        if self._is_speech_model(name):
            self._refresh_speech()

    def _on_dl_state(self, name, *_rest):
        # A scan, a removal in Settings, a download started elsewhere.
        if not _alive(self):
            return
        if self._is_local_writer(name):
            self._refresh_writers()
        if self._is_speech_model(name) or self._in_speech_list(name):
            self._refresh_speech()                  # the row's download icon follows

    def _in_speech_list(self, name):
        return bool(name) and self.speech_combo.findData(name) >= 0

    def _on_dl_scanned(self, *_rest):
        # The first look at the disk: only now do the download icons mean something.
        if _alive(self):
            self._refresh_speech()

    def _on_dl_finished(self, name, state, msg="", *_rest):
        if not _alive(self):
            return
        if self._is_speech_model(name):
            self._speech_note = "" if state == "ready" else (msg or "The download stopped. Try again.")
        if self._is_speech_model(name) or self._in_speech_list(name):
            self._refresh_speech()
        if name == self._pending_download:
            self._pending_download = None
            if state == "ready":
                try:
                    import engine_choice
                    res = engine_choice.set_writer(self.app, "local", model=name)
                    if res.get("ok"):
                        _set_many(self.app, res.get("changes"))
                except Exception:
                    logger.warning("Applying the local writer failed", exc_info=True)
            else:
                self._writer_note = msg or "The download stopped. Try again."
        self._refresh_writers()

    def _open_route(self, route, **opts):
        w = self.parentWidget()
        while w is not None:
            if callable(getattr(w, "open", None)) and callable(getattr(w, "current_route", None)):
                try:
                    w.open(route, **opts)
                    return
                except Exception:
                    logger.warning("open(%s) failed", route, exc_info=True)
                    break
            w = w.parentWidget()
        if callable(getattr(self.app, "show_main", None)):
            _call(self.app, "show_main", route, **opts)
        else:
            _call(self.app, "show_settings")

    # ── shell hooks ──
    def on_open(self, **opts):
        hy_model.refresh_catalog(self.app)          # a worker: once a day at most, never in Privacy mode
        self.refresh()
        self.reload_recent()
        if opts.get("focus") in ("shortcut", "hotkey"):
            # Settings › General's shortcut list links here.
            self.shortcut.button.setFocus(Qt.TabFocusReason)

    def on_setting_changed(self, key):
        if key == "action_model":
            # A writer was picked (here or in Settings › Speech & AI) after a
            # download started: when it ends it must not switch back.
            self._pending_download = None
        if key in WATCHED:
            self.refresh()

    def refresh_account(self):
        self.refresh()
        first = _first_name(self.app)
        for card in self.examples:
            card.set_signoff(first)


def create(main_app):
    return SmartActionsPage(main_app)
