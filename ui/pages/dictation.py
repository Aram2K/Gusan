"""Dictation (Gusan 2.0): the hotkey drawn big + a practice box, what it runs
on, your dictations, and the page's own settings rail.

Mockup: v2/canvas/project/Dictation.dc.html (source v2/pages/core.py
dictation()). The dictation shortcut (Alt+R) always pastes the exact words;
one quiet line under the practice box points to Smart Actions and their own
shortcut ("For emails, lists and translations press <Alt + S> · Smart
Actions ›"). Calm Pro: the plain PRO tag on the "Gusan Cloud speech" row; a
Free user who turns it on gets the current Pro window (app._pro_upsell) and
the toggle goes back. No limits.

    page = create(main_app)        # what ui/shell.py calls
    page.refresh(sync=True)        # tests / previews: load the list now

Every change applies at once through app.set_setting / app.set_settings (no
Save button). A refused change (False) puts the control back to the config.
Settings this page writes:
    hotkey                      Shortcut (HotkeyCaptureButton)
    language                    Language
    mix_languages               Languages you mix (only shown for Mixed languages)
    cleanup_enabled             Clean up mistakes
    cleanup_remove_fillers      Remove filler words
    restore_clipboard           Put my clipboard back
    overlay_theme               Dictation bar look: auto | light | dark
    overlay_private             Hide bar in screen shares
    backend (+ managed_provider) Gusan Cloud speech, via engine_choice.set_speech_source
It reads smart_actions_hotkey (the Smart Actions line's keycaps; Smart
Actions' own page changes it) and never output_action (legacy 1.9.x key).

Integration notes for Wave 4
----------------------------
* Hotkey capture: while the Shortcut button waits for keys the page calls
  ``app.suspend_hotkeys(True)`` and ``(False)`` after, when that method
  exists, so pressing the current Alt+R (or the Live Assistant keys) doesn't
  start a dictation mid-capture. Please add it to AppController (unhook /
  re-hook the global listeners; no config change). Without it the capture
  still works.
* "Listening" in the practice box: the page polls ``app.is_rec`` every
  250 ms while it is visible and the box has focus. A controller signal
  ``sig_recording_changed(bool)`` would replace the poll (connected here when
  it exists).
* Your dictations: history.load() on a worker on every open and when
  history.json changes (an os.stat every 3 s while visible), or on
  ``app.sig_history_changed`` when it exists (see ui/pages/home.py). Meeting
  entries (backend "meeting" / audio_dir) are left out - they live on Meetings.
* Routes this page opens: settings with section="speech" ("Change" on the
  Runs on line and the rail's "More options"); the Settings page's on_open
  already reads it (the speech model lives there). The Mixed-languages picks
  are this rail's own "Languages you mix" row.
* Narrow windows (under ~1116 px): the settings rail moves into the page,
  under the hero and its "Runs on" line, above the list.
* on_open options this page understands: entry=<history timestamp>
  (scrolls to and highlights that dictation - Home's Open uses it),
  focus="hotkey" (puts focus on the Shortcut's Change), focus="practice".
* The Smart Actions line's link opens the smart_actions route. A dictation
  shortcut that equals the Smart Actions one is refused by the controller;
  the row then says "Alt + S runs Smart Actions. Pick another."
"""

import logging
import os

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QCheckBox, QComboBox, QFileDialog,
                               QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QLabel, QMenu, QPlainTextEdit,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ui import theme
from ui.components import (Button, Card, Divider, IconButton, Keycaps, PageHeader, SettingRow, SettingsRail,
                           Toggle, icons, label)
from ui.components.controls import keycap_labels
from ui.components.hotkey_capture import HotkeyCaptureButton
from ui.pages import home as H

logger = logging.getLogger("transcribe")

_now = H._now                      # previews / tests pin it (dictation._now = ...)

LIST_PAGE = 60
REC_POLL_MS = 250

LANGUAGES = [
    ("auto", "Detect automatically"), ("multi", "Mixed languages"), ("en", "English"), ("ar", "Arabic"),
    ("hy", "Armenian"), ("fr", "French"), ("de", "German"), ("ru", "Russian"), ("es", "Spanish"),
]
BAR_LOOKS = [("auto", "Auto"), ("light", "Light"), ("dark", "Dark")]
# Mixed languages: the ones you switch between (speech_langs.NAMES order).
MIX_LANGUAGES = [("en", "English"), ("ar", "Arabic"), ("hy", "Armenian"), ("fr", "French"),
                 ("de", "German"), ("ru", "Russian"), ("es", "Spanish")]
MIX_HINT = "Tick two or more. Each word stays in the language you spoke."

UPSELL_CLOUD = "Fast cloud transcription"

# The quiet line under the practice box (D7).
SMART_HOTKEY_KEY = "smart_actions_hotkey"
SMART_HOTKEY_DEFAULT = "alt+s"                 # main.DEFAULT (D2)
SMART_LINE = "For emails, lists and translations press"
SMART_LINE_NO_KEY = "For emails, lists and translations, use"
SMART_LINK = "Smart Actions"


def smart_hotkey(cfg):
    """The Smart Actions shortcut; "" when none is set (a missing key is the default)."""
    return str((cfg or {}).get(SMART_HOTKEY_KEY, SMART_HOTKEY_DEFAULT) or "").strip().lower()

# The dictation bar's own glass (ui/overlay.py _PALETTES, as the mockup's
# ds.pill draws it) and the mockup's backdrops for the three look previews.
BAR_GLASS = {
    "light": {"tint": QColor(250, 250, 252, 245), "ink": (15, 23, 42), "rec": QColor("#E9152D"),
              "hair": QColor(15, 23, 42, 38), "shadow": QColor(15, 23, 42, 56)},
    "dark": {"tint": QColor(18, 20, 26, 235), "ink": (255, 255, 255), "rec": QColor("#FF4245"),
             "hair": QColor(0, 0, 0, 80), "shadow": QColor(0, 0, 0, 97)},
}
BACKDROP_LIGHT = QColor("#eef2f7")
BACKDROP_DARK = QColor("#1f2430")
_WAVE = [4, 7, 11, 15, 19, 22, 20, 17, 21, 16, 12, 9, 6, 4, 3]


def _ink(theme_name, alpha):
    r, g, b = BAR_GLASS[theme_name]["ink"]
    return QColor(r, g, b, int(round(alpha * 255)))


def bar_theme(cfg):
    return "dark" if str(cfg.get("overlay_theme") or "auto") == "dark" else "light"


def _ask_save_path(parent, fmt):
    """The save dialog (tests replace this)."""
    import history_actions
    folder = os.path.join(os.path.expanduser("~"), "Documents")
    if not os.path.isdir(folder):
        folder = os.path.expanduser("~")
    start = os.path.join(folder, history_actions.default_export_name(fmt))
    filt = "Spreadsheet (*.csv)" if fmt == "csv" else "Text file (*.txt)"
    path, _ = QFileDialog.getSaveFileName(parent, "Export dictations", start, filt)
    return path


def dictation_entries(entries):
    """History entries that are plain dictations: meeting entries live on
    Meetings, Smart Action results (entry["action"]) on Smart Actions."""
    return [e for e in entries or [] if isinstance(e, dict) and str(e.get("text") or "").strip()
            and not H.is_meeting_entry(e) and not H.entry_action(e)]


def filter_entries(entries, query):
    words = [w for w in str(query or "").lower().split() if w]
    if not words:
        return list(entries)
    out = []
    for e in entries:
        hay = (str(e.get("text") or "") + " " + str(e.get("language") or "")).lower()
        if all(w in hay for w in words):
            out.append(e)
    return out


# ── painted pieces ───────────────────────────────────────────────────────────
class BarPreview(QWidget):
    """The dictation bar while recording (red dot, wave, ↵ esc), as in the mockup."""

    W, H = 204, 40

    def __init__(self, look="light", parent=None):
        super().__init__(parent)
        self._look = look
        self.setFixedSize(self.W, self.H)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("Dictation bar while you talk")
        self._eff = QGraphicsDropShadowEffect(self)
        self._eff.setBlurRadius(16)
        self._eff.setOffset(0, 6)
        self.setGraphicsEffect(self._eff)
        self.set_look(look)

    def look(self):
        return self._look

    def set_look(self, look):
        self._look = "dark" if look == "dark" else "light"
        self._eff.setColor(BAR_GLASS[self._look]["shadow"])
        self.update()

    def paintEvent(self, _e):
        g = BAR_GLASS[self._look]
        t = self._look
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setPen(QPen(g["hair"], 1))
        p.setBrush(g["tint"])
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        cy = self.height() / 2.0
        halo = QColor(g["rec"])
        halo.setAlpha(51)
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QPointF(18, cy), 9, 9)
        p.setBrush(g["rec"])
        p.drawEllipse(QPointF(18, cy), 5, 5)
        x = 33.0
        for v in _WAVE:
            p.setBrush(_ink(t, 0.55 + 0.45 * v / 22.0))
            p.drawRoundedRect(QRectF(x, cy - v / 2.0, 3, v), 1.5, 1.5)
            x += 6
        f = theme.font(10, 600)
        p.setFont(f)
        right = self.width() - 16.0
        for text, minw in (("esc", 25), ("↵", 19)):
            w = max(minw, QFontMetricsF(f).horizontalAdvance(text) + 8)
            box = QRectF(right - w, cy - 8, w, 16)
            p.setPen(QPen(_ink(t, 0.235), 1))
            p.setBrush(_ink(t, 0.055))
            p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
            p.setPen(_ink(t, 0.84))
            p.drawText(box, Qt.AlignCenter, text)
            right -= w + 6
        p.end()


class PracticeBox(QWidget):
    """"Try it here": a white box to dictate into, blue while it listens."""

    INSET = 3                                   # room for the 3 px focus glow

    def __init__(self, parent=None):
        super().__init__(parent)
        self._listening = False
        self.setFixedHeight(140 + 2 * self.INSET)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QVBoxLayout(self)
        i = self.INSET
        lay.setContentsMargins(16 + i, 14 + i, 16 + i, 10 + i)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = H.text_label("Try it here", 12, 700, "muted", parent=self)
        head.addWidget(self.title, 0, Qt.AlignVCenter)
        head.addStretch(1)
        self.rec_dot = H.Dot("#E9152D", 7, self)
        head.addWidget(self.rec_dot, 0, Qt.AlignVCenter)
        self.rec_label = H.text_label("Listening", 12, 600, "red-tx", parent=self)
        head.addWidget(self.rec_label, 0, Qt.AlignVCenter)
        lay.addLayout(head)
        self.edit = QPlainTextEdit(self)
        self.edit.setPlaceholderText("Your words land here.")
        self.edit.setAccessibleName("Practice box")
        self.edit.setFrameShape(QPlainTextEdit.NoFrame)
        self.edit.setStyleSheet("QPlainTextEdit { background: transparent; border: none; padding: 0px; }")
        self.edit.setFont(theme.font(15))
        self.edit.setTabChangesFocus(True)
        self.edit.installEventFilter(self)
        lay.addWidget(self.edit, 1)
        self.set_listening(False)

    def eventFilter(self, obj, ev):
        if obj is self.edit and ev.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self.update()
        return False

    def is_listening(self):
        return self._listening

    def set_listening(self, on):
        self._listening = bool(on)
        self.rec_dot.setVisible(self._listening)
        self.rec_label.setVisible(self._listening)
        self.update()

    def active(self):
        return self._listening or self.edit.hasFocus()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        i = self.INSET
        box = QRectF(i, i, self.width() - 2 * i, self.height() - 2 * i)
        if self.active():
            glow = theme.qcolor("rgba(59,130,246,0.12)")
            p.setPen(QPen(glow, 2 * i))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box, 12, 12)
            p.setPen(QPen(theme.qcolor("blue-l"), 1.5))
        else:
            p.setPen(QPen(theme.qcolor("line2"), 1))
        p.setBrush(theme.qcolor("white"))
        p.drawRoundedRect(box.adjusted(0.75, 0.75, -0.75, -0.75), 12, 12)
        p.end()


class BarLookOption(QAbstractButton):
    """One of Auto / Light / Dark: a tiny backdrop with the bar on it."""

    def __init__(self, key, text, parent=None):
        super().__init__(parent)
        self.key = key
        self.setText(text)
        self.setAccessibleName("Dictation bar look: " + text)
        self.setCheckable(True)
        self.setFixedHeight(68)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)

    def sizeHint(self):
        return QSize(84, 68)

    def minimumSizeHint(self):
        return QSize(60, 68)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(e)

    def _mini_pill(self, p, cx, cy, dark):
        bars = (3, 6, 8, 5, 7, 4)
        w = 7 + 5 + 4 + len(bars) * 2 + (len(bars) - 1) * 1.5 + 5
        r = QRectF(cx - w / 2.0, cy - 7, w, 14)
        p.setPen(QPen(QColor(0, 0, 0, 77) if dark else QColor(15, 23, 42, 41), 1))
        p.setBrush(QColor(18, 20, 26, 209) if dark else QColor(250, 250, 252, 242))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#E9152D"))
        p.drawEllipse(QRectF(r.left() + 5, cy - 2.5, 5, 5))
        x = r.left() + 5 + 5 + 4
        p.setBrush(QColor(255, 255, 255, 217) if dark else QColor(15, 23, 42, 179))
        for h in bars:
            p.drawRoundedRect(QRectF(x, cy - h / 2.0, 2, h), 1, 1)
            x += 3.5

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        on = self.isChecked()
        r = QRectF(0.75, 0.75, self.width() - 1.5, self.height() - 1.5)
        focus = self.hasFocus() and self.property("focusVisible")
        p.setPen(QPen(theme.qcolor("blue-l" if (on or focus) else "line"), 2 if focus else (1.5 if on else 1)))
        p.setBrush(theme.qcolor("white"))
        p.drawRoundedRect(r, 10, 10)
        back = QRectF(5, 5, self.width() - 10, 34)
        p.setPen(Qt.NoPen)
        if self.key == "auto":
            p.setBrush(BACKDROP_DARK)
            p.drawRoundedRect(back, 6, 6)
            p.setBrush(BACKDROP_LIGHT)
            p.save()
            p.setClipRect(QRectF(back.left(), back.top(), back.width() / 2.0, back.height()))
            p.drawRoundedRect(back, 6, 6)
            p.restore()
        else:
            p.setBrush(BACKDROP_DARK if self.key == "dark" else BACKDROP_LIGHT)
            p.drawRoundedRect(back, 6, 6)
        self._mini_pill(p, back.center().x(), back.center().y(), self.key == "dark")
        p.setFont(theme.font(12, 700 if on else 500))
        p.setPen(theme.qcolor("blue-tx" if on else "text2"))
        p.drawText(QRectF(0, 45, self.width(), 17), Qt.AlignCenter, self.text())
        p.end()


class BarLook(QWidget):
    """Auto / Light / Dark for the dictation bar (overlay_theme)."""

    def __init__(self, value="auto", on_pick=None, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Dictation bar look")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.options = {}
        for key, text in BAR_LOOKS:
            b = BarLookOption(key, text, self)
            self.group.addButton(b)
            lay.addWidget(b, 1)
            self.options[key] = b
        self._on_pick = on_pick
        self.set_value(value)
        for key, b in self.options.items():
            b.clicked.connect(lambda _=False, k=key: self._picked(k))

    def value(self):
        for k, b in self.options.items():
            if b.isChecked():
                return k
        return "auto"

    def set_value(self, value):
        b = self.options.get(value if value in self.options else "auto")
        if b is not None and not b.isChecked():
            b.setChecked(True)
        for o in self.options.values():
            o.update()

    def _picked(self, key):
        for o in self.options.values():
            o.update()
        if self._on_pick:
            self._on_pick(key)


class LangTag(QWidget):
    """EN / ES / FR: a 30 x 20 outlined tag."""

    def __init__(self, code, parent=None):
        super().__init__(parent)
        self._code = code
        self.setFixedSize(30, 20)
        self.setAccessibleName(code)
        self.setToolTip(code)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(theme.qcolor("line"), 1))
        p.setBrush(theme.qcolor("rgba(255,255,255,0.7)"))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 6, 6)
        p.setFont(theme.font(10.5, 700, letter_spacing=0.4))
        p.setPen(theme.qcolor("muted"))
        p.drawText(QRectF(0, 0, self.width(), self.height()), Qt.AlignCenter, self._code)
        p.end()


class DictationRow(H._Hairline):
    """time · one line · EN · Copy (46 px, plain on the window)."""

    def __init__(self, entry, now, parent, first=False):
        super().__init__(parent, first=False, token="line2" if first else "line")
        self.entry = entry
        self.setFixedHeight(47)                         # 46 px + the 1 px line above (CSS border-top)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 0)
        lay.setSpacing(14)
        when = H.parse_when(entry.get("timestamp"))
        self.time_label = H.text_label(H.time_text(when, now), 12.5, 400, "muted", parent=self, tabular=True)
        self.time_label.setFixedWidth(40)
        lay.addWidget(self.time_label, 0, Qt.AlignVCenter)
        text = H.one_line(entry.get("text"))
        self.main = H.Runs([(text, theme.font(13.5), "text")], parent=self)
        lay.addWidget(self.main, 1, Qt.AlignVCenter)
        code = H.lang_code(entry.get("language"))
        self.lang = None
        if code:
            self.lang = LangTag(code, self)
            lay.addWidget(self.lang, 0, Qt.AlignVCenter)
        self.copy_button = IconButton("copy", "Copy", size=32, icon_size=16, color="muted", parent=self)
        self.copy_button.setAccessibleName("Copy “%s”" % text[:40])
        self.copy_button.clicked.connect(self._copy)
        lay.addWidget(self.copy_button, 0, Qt.AlignVCenter)

    def timestamp(self):
        return str(self.entry.get("timestamp") or "")

    def _copy(self):
        if H.copy_text(str(self.entry.get("text") or "")):
            H.flash_copied(self.copy_button)


class _HeroCard(Card):
    """The hero card; keeps the bar preview centred on the practice box's
    bottom edge (it overlaps the edge, so it can't live inside the box)."""

    def __init__(self, parent=None):
        super().__init__(parent, pad=(24, 26), gap=0, shadow="lg")
        self.layout().setContentsMargins(24, 26, 24, 18)
        self.box = None
        self.pill = None

    def attach(self, box, pill):
        self.box, self.pill = box, pill
        box.installEventFilter(self)
        pill.raise_()

    def place_pill(self):
        if self.box is None or self.pill is None:
            return
        try:
            top_left = self.box.mapTo(self, self.box.rect().topLeft())
            cx = top_left.x() + self.box.width() / 2.0
            bottom = top_left.y() + self.box.height() - PracticeBox.INSET
            self.pill.move(int(cx - self.pill.width() / 2.0), int(bottom - self.pill.height() / 2.0))
            self.pill.raise_()
        except RuntimeError:
            pass                                # a queued call after the page was deleted

    def eventFilter(self, obj, ev):
        if obj is self.box and ev.type() in (QEvent.Move, QEvent.Resize, QEvent.Show):
            QTimer.singleShot(0, self.place_pill)
        return False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.place_pill()


class DictationPage(QWidget):
    """The Dictation page. See the module notes."""

    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.sync_io = False
        self._entries = []
        self._loaded = False
        self._gen = 0
        self._sig = None
        self._query = ""
        self._limit = LIST_PAGE
        self._syncing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = PageHeader("Dictation", parent=self)
        root.addWidget(self.header)
        body = QHBoxLayout()
        body.setContentsMargins(32, 0, 32, 28)
        body.setSpacing(24)
        root.addLayout(body, 1)
        self._body = body

        main = QVBoxLayout()
        main.setSpacing(22)
        top = QVBoxLayout()
        top.setSpacing(14)
        top.addWidget(self._build_hero())
        top.addLayout(self._build_runs_on())
        main.addLayout(top)
        main.addLayout(self._build_list())
        main.addStretch(1)
        body.addLayout(main, 1)
        self._main = main
        self._stacked = False
        self.rail = self._build_rail()
        body.addWidget(self.rail, 0, Qt.AlignTop)

        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(REC_POLL_MS)
        self._rec_timer.timeout.connect(self._poll_recording)
        self._poll = QTimer(self)
        self._poll.setInterval(H.POLL_MS)
        self._poll.timeout.connect(self._poll_store)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._render_list)
        self._problem_timer = QTimer(self)
        self._problem_timer.setSingleShot(True)
        self._problem_timer.setInterval(8000)
        self._problem_timer.timeout.connect(lambda: self._show_shortcut_problem(""))
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._unflash_rows)

        self._connect_services()
        self.sync_from_cfg()
        self._render_list()
        QTimer.singleShot(0, self._first_load)

    # ── building ──
    def _build_hero(self):
        card = _HeroCard(self)
        card.setAccessibleName("Try dictation")
        self.hero_card = card
        row = QHBoxLayout()
        row.setSpacing(28 - PracticeBox.INSET)
        # A layout (236 px wide through the caption), not a widget: the
        # keycaps' shadow paints onto the card instead of being clipped.
        lcol = QVBoxLayout()
        lcol.setContentsMargins(0, 0, 0, 0)
        lcol.setSpacing(20)
        self.hero_keys = H.HeroKeys(H.cfg_of(self.app).get("hotkey", "alt+r"), 76, 31, card, max_width=236)
        lcol.addWidget(self.hero_keys, 0, Qt.AlignLeft)
        self.hero_caption = H.text_label("Press to talk.\nPress again to paste.", 20, 600, "ink", display=True,
                                         parent=card)
        self.hero_caption.setFixedWidth(236)
        lcol.addWidget(self.hero_caption)
        row.addLayout(lcol, 0)
        self.practice = PracticeBox(card)
        row.addWidget(self.practice, 1, Qt.AlignVCenter)
        card.add(row)
        card.layout().addSpacing(26)
        card.add(Divider(card))
        card.layout().addSpacing(14)
        # One quiet line (D7): Alt+R pastes the exact words; Smart Actions
        # have their own shortcut.
        smart = QHBoxLayout()
        smart.setSpacing(7)
        self.smart_line = H.text_label(SMART_LINE, 13, 400, "muted", parent=card)
        smart.addWidget(self.smart_line, 0, Qt.AlignVCenter)
        self.smart_keys = Keycaps.from_hotkey(smart_hotkey(H.cfg_of(self.app)), parent=card)
        smart.addWidget(self.smart_keys, 0, Qt.AlignVCenter)
        self.smart_sep = H.text_label("·", 13, 400, "faint", parent=card)
        smart.addWidget(self.smart_sep, 0, Qt.AlignVCenter)
        chev = QLabel()
        chev.setPixmap(icons.pixmap("chev_r", 14, "blue-tx", sw=2))
        chev.setContentsMargins(2, 0, 0, 0)
        self.smart_link = Button(SMART_LINK, "link", "s", parent=card, trail=chev)
        self.smart_link.setFont(theme.font(13, 600))
        self.smart_link.setFixedHeight(22)                  # a text link in the line, not a 30 px button
        self.smart_link.setAccessibleName("Open Smart Actions")
        self.smart_link.clicked.connect(lambda: H.go(self, self.app, "smart_actions"))
        smart.addWidget(self.smart_link, 0, Qt.AlignVCenter)
        smart.addStretch(1)
        card.add(smart)
        self.bar_preview = BarPreview(bar_theme(H.cfg_of(self.app)), card)
        card.attach(self.practice, self.bar_preview)
        return card

    def _sync_smart_line(self):
        """The Smart Actions line's keycaps follow smart_actions_hotkey ("" = none set)."""
        hk = smart_hotkey(H.cfg_of(self.app))
        if hk:
            self.smart_keys.set_hotkey(hk)
        self.smart_keys.setVisible(bool(hk))
        self.smart_sep.setVisible(bool(hk))
        self.smart_line.setText(SMART_LINE if hk else SMART_LINE_NO_KEY)
        shown = " + ".join(keycap_labels(hk)) if hk else ""
        self.smart_line.setAccessibleName(" ".join(x for x in (self.smart_line.text(), shown, SMART_LINK) if x))

    def smart_line_text(self):
        """The line as read: "For emails, lists and translations press Alt + S · Smart Actions"."""
        parts = [self.smart_line.text()]
        if not self.smart_keys.isHidden():
            parts += [self.smart_keys.accessibleName(), "·"]
        parts.append(self.smart_link.text())
        return " ".join(p for p in parts if p)

    def _build_runs_on(self):
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(7)
        self.runs_icon = QLabel(self)
        row.addWidget(self.runs_icon, 0, Qt.AlignVCenter)
        self.runs_lead = H.text_label("Runs on", 13, 400, "muted", parent=self)
        self.runs_plain = H.text_label("", 13, 700, "ink", parent=self)
        self.runs_sep1 = H.text_label("·", 13, 400, "faint", parent=self)
        self.runs_model = H.text_label("", 13, 400, "muted", parent=self)
        self.runs_sep2 = H.text_label("·", 13, 400, "faint", parent=self)
        self.runs_where = H.text_label("", 13, 400, "text2", parent=self)
        self.runs_sep3 = H.text_label("·", 13, 400, "faint", parent=self)
        self.runs_extra = H.text_label("", 13, 600, "amber-tx", parent=self)
        self.runs_sep4 = H.text_label("·", 13, 400, "faint", parent=self)
        self.runs_change = Button("Change", "link", "s", parent=self)
        self.runs_change.setFont(theme.font(13, 600))
        self.runs_change.setFixedHeight(22)             # a text link in an 18 px line, not a 30 px button
        self.runs_change.setAccessibleName("Change the speech model")
        self.runs_change.clicked.connect(lambda: H.go(self, self.app, "settings", section="speech"))
        for w in (self.runs_lead, self.runs_plain, self.runs_sep1, self.runs_model, self.runs_sep2, self.runs_where,
                  self.runs_sep3, self.runs_extra, self.runs_sep4, self.runs_change):
            row.addWidget(w, 0, Qt.AlignVCenter)
        row.addStretch(1)
        return row

    def _build_list(self):
        col = QVBoxLayout()
        col.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.list_title = label("Your dictations", "h2", self)
        head.addWidget(self.list_title, 0, Qt.AlignVCenter)
        head.addStretch(1)
        self.search = H.search_field("Search dictations", 220, 34, self)
        self.search.textChanged.connect(self._on_search)
        head.addWidget(self.search, 0, Qt.AlignVCenter)
        chev = QLabel()
        chev.setPixmap(icons.pixmap("chev_d", 14, "muted"))
        self.export_button = Button("Export", "secondary", "s", icon="download", parent=self, trail=chev)
        self.export_button.setAccessibleName("Export dictations")
        self.export_button.clicked.connect(self._export_menu)
        head.addWidget(self.export_button, 0, Qt.AlignVCenter)
        col.addLayout(head)
        self.list_box = QWidget(self)
        self.list_box.setAccessibleName("Your dictations")
        self.list_lay = QVBoxLayout(self.list_box)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(0)
        col.addWidget(self.list_box)
        self.more_button = Button("Show more", "link", "s", parent=self)
        self.more_button.clicked.connect(self._show_more)
        self.more_button.hide()
        col.addWidget(self.more_button, 0, Qt.AlignLeft)
        return col

    def _build_rail(self):
        cfg = H.cfg_of(self.app)
        rail = SettingsRail("Dictation settings", icon="gear", parent=self)
        self.shortcut = HotkeyCaptureButton(cfg.get("hotkey", "alt+r"), accessible_name="Dictation shortcut")
        self.shortcut.captured.connect(self._on_hotkey_captured)
        self.shortcut.refused.connect(self._show_shortcut_problem)
        self.shortcut.capturing_changed.connect(self._on_capturing)
        # The rail's Change is a text link beside 24 px keycaps (the row is 48 px, as drawn).
        self.shortcut.button.setFixedHeight(24)
        # While it waits, "Press keys…" sits where the keycaps were, as an empty
        # dashed key slot - not a second blue link beside Cancel.
        self.shortcut.prompt_label.setFixedHeight(24)
        self.shortcut.prompt_label.setStyleSheet(
            "QLabel { color: %s; background: %s; border: 1px dashed %s; border-radius: 6px; padding: 0px 8px; }"
            % (theme.qss_color("blue-tx"), theme.qss_color("blue-sub"), theme.qss_color("blue-l")))
        self.row_shortcut = rail.add_row(SettingRow("Shortcut", self.shortcut))

        self.language = QComboBox()
        self.language.setFont(theme.font(13.5))
        # The theme's 30 px right padding sits under the arrow, which Qt takes
        # off the text again: without it "Detect automatically" fits in 176 px.
        # 32 px tall: the rail's small select (the theme's default is 36).
        self.language.setStyleSheet("QComboBox { padding-right: 0px; min-height: 30px; }")
        self.language.setFixedSize(176, 32)
        self.language.setAccessibleName("Language")
        for code, name in LANGUAGES:
            self.language.addItem(name, code)
        self.language.currentIndexChanged.connect(self._on_language)
        self.row_language = rail.add_row(SettingRow("Language", self.language))

        # Shown only for "Mixed languages" (the 1.x Settings window had it).
        self.mix_box = QWidget()
        self.mix_box.setAccessibleName("Languages you mix")
        grid = QGridLayout(self.mix_box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self.mix_checks = {}
        for i, (code, name) in enumerate(MIX_LANGUAGES):
            cb = QCheckBox(name, self.mix_box)
            cb.setFont(theme.font(13))
            cb.toggled.connect(lambda on, c=code: self._on_mix_toggled(c, on))
            self.mix_checks[code] = cb
            grid.addWidget(cb, i // 3, i % 3)
        self.row_mix = rail.add_row(SettingRow("Languages you mix", self.mix_box, hint=MIX_HINT, stacked=True))
        self.row_mix.setVisible(str(cfg.get("language") or "auto") == "multi")

        self.toggles = {}

        def toggle_row(key, text, hint=None, default=True):
            t = Toggle(text, bool(cfg.get(key, default)))
            t.toggled.connect(lambda on, k=key: self._on_toggle(k, on))
            self.toggles[key] = (t, default)
            return rail.add_row(SettingRow(text, t, hint=hint))

        self.row_cleanup = toggle_row("cleanup_enabled", "Clean up mistakes", "Drops made-up “Thank you.” lines")
        self.row_fillers = toggle_row("cleanup_remove_fillers", "Remove filler words", "um, uh, you know", False)
        self.row_clipboard = toggle_row("restore_clipboard", "Put my clipboard back", "After each paste")
        self.bar_look = BarLook(str(cfg.get("overlay_theme") or "auto"), on_pick=self._on_bar_look)
        self.row_bar = rail.add_row(SettingRow("Dictation bar look", self.bar_look, stacked=True))
        self.row_private = toggle_row("overlay_private", "Hide bar in screen shares")

        try:
            import engine_choice
            cloud_model = engine_choice.CLOUD_SPEECH_MODELS.get("gemini", "Gemini 2.5 Flash")
        except Exception:
            cloud_model = "Gemini 2.5 Flash"
        self.cloud = Toggle("Gusan Cloud speech", False)
        self.cloud.toggled.connect(self._on_cloud)
        self.row_cloud = rail.add_row(SettingRow("Gusan Cloud speech", self.cloud, pro=True, model=cloud_model))

        chev = QLabel()
        chev.setPixmap(icons.pixmap("chev_r", 14, "blue-tx", sw=2))
        chev.setContentsMargins(4, 0, 0, 0)
        more = Button("More options", "link", "s", trail=chev)
        more.setFont(theme.font(13, 600))
        more.setFixedHeight(22)
        more.setAccessibleName("More dictation options")
        more.clicked.connect(lambda: H.go(self, self.app, "settings", section="speech"))
        self.more_options = rail.set_foot(more)
        return rail

    # ── services ──
    def _connect_services(self):
        # Bound methods (dropped with the page), each connected once; on_open
        # calls this again for a service the controller created later.
        dl = getattr(self.app, "downloads", None)
        H.connect_signals(self, (
            (self.app, "sig_history_changed", self._on_history_changed),
            (self.app, "sig_recording_changed", self._on_recording_changed),
            (dl, "state_changed", self._on_download_event),
            (dl, "progress", self._on_download_event),
            (dl, "finished", self._on_download_event),
        ))

    def _on_history_changed(self, *_):
        self.refresh()

    def _on_recording_changed(self, *_):
        self._poll_recording()

    def _on_download_event(self, *_):
        self._update_runs_on()

    # ── config -> controls ──
    def sync_from_cfg(self):
        """Every control from app.cfg (after a change from anywhere)."""
        cfg = H.cfg_of(self.app)
        self._syncing = True
        try:
            hk = cfg.get("hotkey", "alt+r")
            self.hero_keys.set_hotkey(hk)
            if not self.shortcut.is_capturing():
                self.shortcut.set_hotkey(hk)
            lang = str(cfg.get("language") or "auto")
            idx = self.language.findData(lang)
            if idx < 0:
                self.language.addItem(lang.upper(), lang)
                idx = self.language.findData(lang)
            self.language.blockSignals(True)
            self.language.setCurrentIndex(idx)
            self.language.blockSignals(False)
            self._sync_mix(cfg, lang)
            for key, (t, default) in self.toggles.items():
                t.blockSignals(True)
                t.setChecked(bool(cfg.get(key, default)))
                t.blockSignals(False)
            self.bar_look.set_value(str(cfg.get("overlay_theme") or "auto"))
            self.bar_preview.set_look(bar_theme(cfg))
            self._sync_smart_line()
            self._sync_cloud()
            self._update_runs_on()
            self._update_private_hint()
        finally:
            self._syncing = False

    def _cloud_on(self):
        cfg = H.cfg_of(self.app)
        return (str(cfg.get("backend") or "local") == "managed" and not cfg.get("privacy_mode")
                and H.is_pro(self.app))

    def _sync_cloud(self):
        self.cloud.blockSignals(True)
        self.cloud.setChecked(self._cloud_on())
        self.cloud.blockSignals(False)
        # The model Gusan Cloud runs (managed_provider), under the row's name.
        try:
            import engine_choice
            prov = str(H.cfg_of(self.app).get("managed_provider") or "gemini").lower()
            name = engine_choice.CLOUD_SPEECH_MODELS.get(prov) or engine_choice.CLOUD_SPEECH_MODELS["gemini"]
            self.row_cloud.set_model(name)
        except Exception:
            logger.debug("cloud model name failed", exc_info=True)

    def _update_runs_on(self):
        plain, model, where, kind, info = H.speech_bits(self.app)
        name = {"cloud": "cloud", "own_key": "key"}.get(kind, "laptop")
        self.runs_icon.setPixmap(icons.pixmap(name, 16, "muted"))
        self.runs_plain.setText(plain or "This computer")
        show_model = bool(model) and model != plain
        self.runs_model.setText(model)
        self.runs_model.setVisible(show_model)
        self.runs_sep1.setVisible(show_model)
        show_where = bool(where) and kind != "cloud"
        self.runs_where.setText(where)
        self.runs_where.setVisible(show_where)
        self.runs_sep2.setVisible(show_where)
        state, pct = H.model_download_state(self.app, info)
        extra = ""
        if state == "downloading":
            extra = "Downloading %d%%" % pct if pct else "Downloading"
        self.runs_extra.setText(extra)
        self.runs_extra.setVisible(bool(extra))
        self.runs_sep3.setVisible(bool(extra))
        line = " · ".join(x for x in (plain, model if show_model else "", where if show_where else "", extra) if x)
        self.runs_plain.setAccessibleName("Runs on " + line)

    def runs_on_text(self):
        """"Runs on Balanced · Whisper Small · on this computer" as shown."""
        parts = [self.runs_plain.text()]
        for w in (self.runs_model, self.runs_where, self.runs_extra):
            if not w.isHidden():
                parts.append(w.text())
        return "Runs on " + " · ".join(p for p in parts if p)

    def _update_private_hint(self):
        cfg = H.cfg_of(self.app)
        hint = ""
        if cfg.get("overlay_private", True):
            try:
                import live_context
                st = live_context.hide_status(True)
                if st.key != "on":
                    hint = st.headline
            except Exception:
                hint = ""
        self.row_private.set_hint(hint)

    # ── controls -> config ──
    def _apply(self, key, value):
        """app.set_setting(key, value); a refusal (or no controller) puts the
        controls back to the config."""
        ok = H.call(self.app, "set_setting", key, value)
        if not ok:
            QTimer.singleShot(0, self.sync_from_cfg)
            return False
        return True

    def _on_toggle(self, key, on):
        if self._syncing:
            return
        self._apply(key, bool(on))
        if key == "overlay_private":
            self._update_private_hint()

    def _on_language(self, _idx):
        if self._syncing:
            return
        code = self.language.currentData()
        if code:
            self.row_mix.setVisible(code == "multi")
            self._apply("language", code)

    def _sync_mix(self, cfg, lang):
        """The mix checkboxes from cfg (shown only for Mixed languages)."""
        try:
            import speech_langs
            picked = speech_langs.saved_mix(cfg)
        except Exception:
            picked = []
        for code, cb in self.mix_checks.items():
            cb.blockSignals(True)
            cb.setChecked(code in picked)
            cb.blockSignals(False)
        self.row_mix.setVisible(lang == "multi")

    def _on_mix_toggled(self, _code, _on):
        if self._syncing:
            return
        # Saved as ticked; it takes effect from two languages (speech_langs).
        self._apply("mix_languages", [c for c, cb in self.mix_checks.items() if cb.isChecked()])

    def _on_bar_look(self, key):
        if self._syncing:
            return
        if self._apply("overlay_theme", key):
            self.bar_preview.set_look("dark" if key == "dark" else "light")

    def _on_cloud(self, on):
        if self._syncing:
            return
        try:
            import engine_choice
            res = engine_choice.set_speech_source(self.app, "cloud" if on else "local")
        except Exception:
            logger.warning("set_speech_source failed", exc_info=True)
            res = {"ok": False, "problem": "error", "changes": {}, "upsell": ""}
        if res.get("ok"):
            changes = res.get("changes") or {}
            ok = H.call(self.app, "set_settings", changes) if changes else {}
            if not isinstance(ok, dict) or not all(ok.values()):
                QTimer.singleShot(0, self.sync_from_cfg)        # refused, or no controller: back to cfg
            else:
                self.row_cloud.set_hint("")
                self._update_runs_on()
            return
        self._sync_cloud_later()
        problem = res.get("problem")
        if problem == "pro":
            H.call(self.app, "_pro_upsell", res.get("upsell") or UPSELL_CLOUD)
        elif problem == "privacy_mode":
            self.row_cloud.set_hint("Off while Privacy mode is on")

    def _sync_cloud_later(self):
        QTimer.singleShot(0, self._sync_cloud)

    # ── shortcut ──
    def _on_capturing(self, on):
        H.call(self.app, "suspend_hotkeys", bool(on))
        if on:
            self._show_shortcut_problem("")

    def _on_hotkey_captured(self, combo):
        cfg = H.cfg_of(self.app)
        old = cfg.get("hotkey", "alt+r")
        ok = H.call(self.app, "set_setting", "hotkey", combo)
        if ok:
            self._show_shortcut_problem("")
            self.hero_keys.set_hotkey(combo)
            self.shortcut.set_hotkey(combo)
            return
        self.shortcut.set_hotkey(old)
        shown = " + ".join(keycap_labels(combo))
        assist = str(cfg.get("live_assist_hotkey") or "")
        smart = smart_hotkey(cfg)
        try:
            import settings_apply
            norm = settings_apply.norm_hotkey
        except Exception:
            norm = lambda hk: str(hk or "").replace(" ", "").lower()    # noqa: E731
        if assist and norm(assist) == norm(combo):
            self._show_shortcut_problem("%s opens Live Assistant. Pick another." % shown)
        elif smart and norm(smart) == norm(combo):
            self._show_shortcut_problem("%s runs Smart Actions. Pick another." % shown)
        else:
            self._show_shortcut_problem("Couldn't use %s. Try another." % shown)

    def _show_shortcut_problem(self, text):
        row = self.row_shortcut
        row.set_hint(text)
        theme.set_role(row.hint_label, "error" if text else "hint")
        if text:
            self._problem_timer.start()

    def shortcut_problem(self):
        return self.row_shortcut.hint_label.text() if not self.row_shortcut.hint_label.isHidden() else ""

    # ── practice box ──
    def set_listening(self, on):
        self.practice.set_listening(on)

    def _poll_recording(self):
        rec = bool(getattr(self.app, "is_rec", False))
        want = rec and self.practice.edit.hasFocus()
        if want != self.practice.is_listening():        # 4 polls a second: repaint only on a change
            self.practice.set_listening(want)

    # ── list ──
    def _first_load(self):
        if self._gen == 0:                      # nothing asked for it yet (on_open usually has)
            self.refresh(sync=self.sync_io)

    def refresh(self, sync=None):
        sync = self.sync_io if sync is None else bool(sync)
        self._gen += 1
        gen = self._gen
        try:
            self._sig = H.store_signature()[:1]
        except Exception:
            self._sig = None

        def load():
            import history
            return history.load()

        def then(entries, g=gen):
            if g != self._gen:
                return
            try:
                self._entries = dictation_entries(entries)
                self._loaded = True
                self._render_list()
                self._render_after_load()
            except RuntimeError:
                pass

        H.run_later(load, then, error=lambda e: logger.warning("Dictations failed to load: %s", e), sync=sync)

    def entries(self):
        return list(self._entries)

    def shown_rows(self):
        return [w for w in self.list_box.findChildren(DictationRow) if not w.isHidden()]

    def _on_search(self, text):
        self._query = text
        self._limit = LIST_PAGE
        self._search_timer.start()

    def _show_more(self):
        self._limit += LIST_PAGE
        self._render_list()

    def _clear_list(self):
        while self.list_lay.count():
            it = self.list_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

    def _render_list(self):
        self._clear_list()
        self.more_button.hide()
        if not self._loaded:
            return
        now = _now()
        found = filter_entries(self._entries, self._query)
        self.export_button.setEnabled(bool(self._entries))
        if not found:
            cfg = H.cfg_of(self.app)
            if self._query.strip():
                text = "No matches."
            elif cfg.get("privacy_mode"):
                text = "Privacy mode is on: nothing new is saved."
            elif cfg.get("save_history", True) is False:
                text = "History is off."
            else:
                text = "Nothing yet. Try it in the box above."
            lb = H.text_label(text, 13.5, 400, "muted", parent=self.list_box)
            lb.setContentsMargins(2, 4, 2, 4)
            self.list_lay.addWidget(lb)
            return
        shown = found[:self._limit]
        group = None
        first_group = True
        first = True
        for e in shown:
            g = H.group_of(H.parse_when(e.get("timestamp")), now)
            if g != group:
                group = g
                lb = H.text_label(g, 12, 700, "muted", parent=self.list_box)
                lb.setContentsMargins(2, 2 if first_group else 18, 2, 8)
                self.list_lay.addWidget(lb)
                first_group = False
                first = True
            self.list_lay.addWidget(DictationRow(e, now, self.list_box, first=first))
            first = False
        self.more_button.setVisible(len(found) > len(shown))
        self._fix_tab_order()

    def _poll_store(self):
        if not self.isVisible():
            return
        try:
            sig = H.store_signature()[:1]
        except Exception:
            return
        if sig != self._sig:
            self.refresh()

    def highlight(self, timestamp):
        """Scroll to the dictation saved at ``timestamp`` and light it up briefly."""
        if not timestamp:
            return False
        if self._query:
            self.search.setText("")
            self._query = ""
        idx = next((i for i, e in enumerate(self._entries) if str(e.get("timestamp")) == timestamp), None)
        if idx is None:
            return False
        if idx >= self._limit:
            self._limit = idx + 1
        self._render_list()
        row = next((r for r in self.shown_rows() if r.timestamp() == timestamp), None)
        if row is None:
            return False
        row.set_flash(True)
        self._flash_timer.start(1800)
        QTimer.singleShot(0, lambda r=row: self._scroll_to(r))
        return True

    def _scroll_to(self, row):
        w = self.parentWidget()
        while w is not None and not isinstance(w, QScrollArea):
            w = w.parentWidget()
        if isinstance(w, QScrollArea):
            try:
                w.ensureWidgetVisible(row, 0, 120)
            except RuntimeError:
                pass

    def _unflash_rows(self):
        for r in self.shown_rows():
            r.set_flash(False)

    # ── export ──
    def _export_menu(self):
        menu = getattr(self, "_menu", None)
        if menu is None:                        # built once, reused (not a new menu per click)
            menu = QMenu(self)
            menu.setAccessibleName("Export dictations")
            menu.addAction("Text file (.txt)", lambda: self.export("txt"))
            menu.addAction("Spreadsheet (.csv)", lambda: self.export("csv"))
            self._menu = menu
        menu.popup(self.export_button.mapToGlobal(self.export_button.rect().bottomLeft()))

    def export(self, fmt, path=None):
        """Save the dictations shown (search applied) as .txt or .csv."""
        entries = filter_entries(self._entries, self._query)
        if not entries:
            return False
        path = path or _ask_save_path(self, fmt)
        if not path:
            return False

        def work():
            import history_actions
            fn = history_actions.export_csv if fmt == "csv" else history_actions.export_txt
            return fn(path, entries)

        def done(n):
            H.call(self.app, "track", "history_exported", {"format": fmt, "count": int(n or 0), "from": "dictation"})
            try:
                self.export_button.setText("Saved")
                QTimer.singleShot(2000, lambda: self._reset_export())
            except RuntimeError:
                pass

        def failed(e):
            logger.warning("Export failed: %s", e)
            try:
                self.export_button.setText("Couldn't save")
                QTimer.singleShot(3000, lambda: self._reset_export())
            except RuntimeError:
                pass

        H.run_later(work, done, error=failed, sync=self.sync_io)
        return True

    def _reset_export(self):
        try:
            self.export_button.setText("Export")
        except RuntimeError:
            pass

    # ── shell hooks ──
    def on_open(self, **opts):
        self._connect_services()                # a service created after the page (once each)
        self.sync_from_cfg()
        entry = opts.get("entry")
        if entry:
            self._pending_entry = str(entry)    # highlighted once the fresh list is drawn
        self.refresh()
        focus = opts.get("focus")
        if focus == "hotkey":
            self.shortcut.button.setFocus(Qt.TabFocusReason)
        elif focus == "practice":
            self.practice.edit.setFocus(Qt.OtherFocusReason)

    def _render_after_load(self):
        pending = getattr(self, "_pending_entry", None)
        if pending:
            self._pending_entry = None
            self.highlight(pending)

    def on_setting_changed(self, key):
        if key in ("hotkey", "language", "mix_languages", "cleanup_enabled", "cleanup_remove_fillers",
                   "restore_clipboard", "overlay_theme", "overlay_private", SMART_HOTKEY_KEY, "backend", "whisper_model",
                   "managed_provider", "privacy_mode", "mistral_stt_model", "google_stt_model",
                   "mistral_api_key", "google_api_key"):
            self.sync_from_cfg()
        if key in ("save_history", "privacy_mode"):
            self.refresh()

    def refresh_account(self):
        self._sync_cloud()
        self._update_runs_on()

    # ── narrow windows: the rail moves into the page, above the list ──
    WIDE = 32 + 480 + 24 + 316 + 32          # main column needs ~480 px beside the rail

    def _available_width(self):
        p = self.parentWidget()
        if p is not None and p.objectName() == "qt_scrollarea_viewport":
            return p.width()
        return self.width()

    def _reflow(self):
        stacked = self._available_width() < self.WIDE
        if stacked == self._stacked:
            return
        self._stacked = stacked
        # Under the hero and its "Runs on" line, not after a long list of
        # dictations (it would be out of reach at the bottom).
        if stacked:
            self._body.removeWidget(self.rail)
            self._main.insertWidget(1, self.rail, 0, Qt.AlignLeft | Qt.AlignTop)
        else:
            self._main.removeWidget(self.rail)
            self._body.addWidget(self.rail, 0, Qt.AlignTop)
        self._fix_tab_order()

    def is_stacked(self):
        return self._stacked

    def _rail_focusables(self):
        # Not a combo's popup list: it lives in its own window, and a tab
        # order across windows is refused (a Qt warning on every render,
        # and the link around it is lost).
        win = self.rail.window()
        return [w for w in self.rail.findChildren(QWidget)
                if w.focusPolicy() & Qt.TabFocus and w.window() is win]

    def _fix_tab_order(self):
        """Tab follows what you see: the list's rows are built after the rail,
        so without this they would come last, and a stacked rail would be
        reached only after the list."""
        try:
            rows = [r.copy_button for r in self.shown_rows()]
            list_part = [self.search, self.export_button] + rows + [self.more_button]
            rail = self._rail_focusables()
            chain = [self.runs_change] + (rail + list_part if self._stacked else list_part + rail)
            for a, b in zip(chain, chain[1:]):
                QWidget.setTabOrder(a, b)
        except RuntimeError:
            pass

    def tab_chain(self):
        """The Tab order from "Change" on the Runs on line (tests)."""
        out, w = [], self.runs_change
        for _ in range(5000):
            if (w.focusPolicy() & Qt.TabFocus) and w.isVisibleTo(self):
                out.append(w)
            w = w.nextInFocusChain()
            if w is self.runs_change or w is None:
                break
        return out

    def eventFilter(self, obj, ev):
        if obj is self.parentWidget() and ev.type() == QEvent.Resize:
            self._reflow()
        return False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow()

    def showEvent(self, e):
        super().showEvent(e)
        p = self.parentWidget()
        if p is not None and not getattr(self, "_watching_parent", False):
            p.installEventFilter(self)
            self._watching_parent = True
        self._reflow()
        self._rec_timer.start()
        self._poll.start()
        self.hero_card.place_pill()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._rec_timer.stop()
        self._poll.stop()


def create(main_app):
    return DictationPage(main_app)


__all__ = ["create", "DictationPage", "BarPreview", "BarLook", "PracticeBox", "dictation_entries",
           "filter_entries", "LANGUAGES"]
