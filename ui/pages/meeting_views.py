"""Meetings page parts (the route is ui/pages/meetings.py): shared small widgets,
the live view (a recording in progress) and the notes view (one saved meeting).

Both views are driven by the page (``page`` below is meetings.MeetingsPage):
they read the app's MeetingSession through ``page.session()`` and never stop a
recording on their own - hiding the page or closing the window leaves it
running. Disk work (reading a meeting, exports, saving My notes) runs on a
worker thread through BgRunner; set ``SYNC_BG = True`` in tests to run it
inline.
"""

import html as html_mod
import logging
import os
import re
import tempfile
import threading
from collections import deque

from PySide6.QtCore import QByteArray, QEvent, QObject, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QDesktopServices, QGuiApplication, QIcon, QLinearGradient, QPainter,
                           QPen, QPixmap)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog,
                               QLabel, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QScrollArea,
                               QSizePolicy, QStackedWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

import rich_text
from app_info import APP_NAME
from ui import privacy_prompt, theme
from ui.components import clipboard as rich_clipboard
from ui.components import (AudioPlayer, Button, Card, Chip, Divider, IconButton, Keycaps, PageHeader, ProTag,
                           Segmented, icons, label, mini_prompt)

logger = logging.getLogger("transcribe")

SYNC_BG = False            # tests: run background work inline
FLASH_MS = 1800            # how long "Copied" and friends stay up
FLASH_ERROR_MS = 5000      # a failure to read: the Notion errors
NOTION_THEN = "Send this meeting"       # the Notion sheet's button when it was opened from here
NOTION_REASONS = ("auth", "no_access", "rate", "offline")      # telemetry: why a send failed (else "other")

# ── words ────────────────────────────────────────────────────────────────────
LANGS = (("auto", "Detect automatically"), ("multi", "Mixed languages"), ("en", "English"),
         ("ar", "Arabic"), ("hy", "Armenian"), ("fr", "French"), ("de", "German"),
         ("ru", "Russian"), ("es", "Spanish"))
LANG_LABEL = dict(LANGS)
# meeting_session audio modes -> (plain words, leading icons)
AUDIO_MODES = {
    "smart_meeting": ("Both sides of the call", ("mic", "volume")),
    "default_mic": ("Only my microphone", ("mic",)),
    "system_only": ("Only the call's sound", ("volume",)),
}
NOTICE_TEXT = "Heads up: I'm using %s to take notes on this call." % APP_NAME
UPSELL_RECORD = "Meeting recording"


def lang_name(code):
    """'en' -> 'English'; '' / 'auto' -> ''."""
    code = str(code or "").strip()
    if not code or code == "auto":
        return ""
    return LANG_LABEL.get(code, code.upper() if len(code) <= 3 else code)


def fmt_duration(sec):
    """42 min / 1 h 05 min / 25 s (the list and the notes header)."""
    try:
        sec = max(0, int(sec or 0))
    except (TypeError, ValueError):
        sec = 0
    if sec < 60:
        return "%d s" % sec
    if sec < 3600:
        return "%d min" % max(1, int(round(sec / 60.0)))
    h, rem = divmod(sec, 3600)
    return "%d h %02d min" % (h, rem // 60)


def fmt_clock(sec):
    """12:34 or 1:02:03 (the live timer, the audio card)."""
    sec = max(0, int(sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def parse_started(text):
    from datetime import datetime
    try:
        return datetime.strptime(str(text or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def fmt_when(started, with_time=True):
    """'2026-09-29 11:00:00' -> 'Tue 29 Sep · 11:00' (or 'Tue 29 Sep')."""
    dt = parse_started(started)
    if dt is None:
        return str(started or "")
    day = "%s %d %s" % (dt.strftime("%a"), dt.day, dt.strftime("%b"))
    return "%s · %s" % (day, dt.strftime("%H:%M")) if with_time else day


# ── background work ──────────────────────────────────────────────────────────
class BgRunner(QObject):
    """fn() on a worker thread, on_done(result, error) back on the GUI thread.
    A runner parented to a view dies with it, and late results are dropped."""

    _done = Signal(int, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cbs = {}
        self._n = 0
        self._done.connect(self._finish)

    def run(self, fn, on_done=None):
        self._n += 1
        token = self._n
        self._cbs[token] = on_done
        if SYNC_BG:
            res, err = self._call(fn)
            self._finish(token, res, err)
            return token

        def work():
            res, err = self._call(fn)
            try:
                self._done.emit(token, res, err)
            except RuntimeError:
                pass                    # the view is gone

        threading.Thread(target=work, daemon=True, name="gusan-meetings").start()
        return token

    @staticmethod
    def _call(fn):
        try:
            return fn(), None
        except Exception as e:          # noqa: BLE001 - reported to the caller
            logger.warning("Meetings background task failed", exc_info=True)
            return None, e

    def _finish(self, token, res, err):
        cb = self._cbs.pop(token, None)
        if cb is not None:
            try:
                cb(res, err)
            except Exception:
                logger.warning("Meetings background result failed", exc_info=True)


def atomic_write(path, text):
    """Write a small text file through a temp file + one rename."""
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".gusan.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


_WRITE_LOCK = threading.Lock()
_WRITTEN = {}                   # path -> newest version on disk
_VERSION = [0]


def next_version():
    """A ticket taken on the GUI thread when a change is made (see write_latest)."""
    _VERSION[0] += 1
    return _VERSION[0]


def write_latest(path, text, version):
    """atomic_write, but an older change that reaches the disk late never
    overwrites a newer one (two quick ticks run on two worker threads)."""
    with _WRITE_LOCK:
        key = os.path.normcase(os.path.abspath(path))
        if _WRITTEN.get(key, 0) > version:
            return False
        atomic_write(path, text)
        _WRITTEN[key] = version
        return True


# ── the page's stylesheet (tokens only) ──────────────────────────────────────
def page_qss():
    c = theme.qss_color
    return f"""
QFrame[gusan="whiteCard"][radius="20"] {{ border-radius: 20px; }}
QFrame[gusan="whiteCard"][radius="16"] {{ border-radius: 16px; }}
QFrame[gusan="soFar"] {{ background: {c('blue-sub')}; border: 1px solid {c('blue-line')}; border-radius: 16px; }}
QFrame[gusan="banner"] {{ background: {c('amber-sub')}; border: 1px solid #fde68a; border-radius: 12px; }}
QFrame[gusan="banner"][tone="red"] {{ background: {c('red-sub')}; border: 1px solid #fecaca; }}
QFrame[gusan="banner"][tone="blue"] {{ background: {c('blue-sub')}; border: 1px solid {c('blue-line')}; }}
QFrame[gusan="banner"][tone="neutral"] {{ background: {c('bg2')}; border: 1px solid {c('line')}; }}
QFrame[gusan="peopleField"] {{ background: #ffffff; border: 1px solid {c('line2')}; border-radius: 10px; }}
QFrame[gusan="peopleField"][focus="true"] {{ border: 1px solid {c('blue-l')}; }}
QFrame[gusan="option"] {{ background: #ffffff; border: 1px solid {c('line')}; border-radius: 10px; }}
QFrame[gusan="option"]:hover {{ border: 1px solid {c('line2')}; }}
QFrame[gusan="upRow"] {{ background: transparent; border: 1px solid transparent; border-radius: 12px; }}
QFrame[gusan="upRow"][next="true"] {{ background: #ffffff; border: 1px solid {c('line')}; }}
QFrame[gusan="liveStrip"] {{ background: {c('red-sub')}; border: 1px solid #fecaca; border-radius: 12px; }}
QFrame[gusan="liveStrip"][tone="blue"] {{ background: {c('blue-sub')}; border: 1px solid {c('blue-line')}; }}
QLineEdit[gusan="bare"] {{ background: transparent; border: none; padding: 0px; }}
QLineEdit[gusan="titleEdit"] {{
    background: transparent; border: none; border-bottom: 1px solid transparent; border-radius: 0px;
    padding: 0px; color: {c('ink')};
}}
QLineEdit[gusan="titleEdit"]:focus {{ border-bottom: 1px solid {c('blue-l')}; }}
QLineEdit[gusan="search"] {{ padding: 0px 10px 0px 4px; }}
QPlainTextEdit[gusan="notesBox"] {{ border-radius: 10px; padding: 10px 12px; }}
QPlainTextEdit[gusan="bare"], QTextEdit[gusan="bare"], QTextBrowser[gusan="bare"] {{
    background: transparent; border: none; padding: 0px;
}}
QPushButton[gusan="personChip"] {{
    background: {c('bg2')}; border: 1px solid {c('line')}; border-radius: 13px; padding: 0px;
}}
QPushButton[gusan="personChip"]:hover {{ border: 1px solid {c('line2')}; }}
QPushButton[gusan="personChip"][focusVisible="true"] {{ border: 2px solid {c('blue-l')}; }}
QLabel[gusan="moreChip"] {{
    background: {c('bg2')}; border: 1px solid {c('line')}; border-radius: 13px; color: {c('text2')};
    padding: 0px 8px;
}}
QToolButton[gusan="bell"] {{ background: #ffffff; border: 1px solid {c('line2')}; border-radius: 9px; }}
QToolButton[gusan="bell"]:checked {{ background: {c('blue-sub')}; border: 1px solid {c('blue-line')}; }}
QToolButton[gusan="bell"]:hover {{ border: 1px solid {c('blue-l')}; }}
QToolButton[gusan="bell"][focusVisible="true"] {{ border: 2px solid {c('blue-l')}; }}
QPushButton[gusan="btn"][hero="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {c('blue-l')}, stop:1 {c('pro-l')});
    border: 1px solid transparent; color: #ffffff; padding: 0px 22px;
}}
QPushButton[gusan="btn"][hero="true"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {c('blue')}, stop:1 {c('pro')});
}}
QPushButton[gusan="btn"][hero="true"][focusVisible="true"] {{ border: 2px solid #93c5fd; }}
QPushButton[gusan="btn"][kind="link"][tone="muted"] {{ color: {c('text2')}; }}
QPushButton[gusan="btn"][kind="link"][tone="muted"]:hover {{ color: {c('ink')}; }}
QPushButton[gusan="btn"][kind="secondary"][open="true"] {{ background: {c('bg2')}; border: 1px solid {c('blue-line')}; }}
QMenu[gusan="menu"] {{
    background: #ffffff; border: 1px solid {c('line')}; border-radius: 12px; padding: 6px;
}}
QMenu[gusan="menu"]::item {{
    padding: 8px 14px 8px 10px; border-radius: 8px; color: {c('text')}; background: transparent;
}}
QMenu[gusan="menu"]::item:selected {{ background: {c('bg2')}; color: {c('ink')}; }}
QMenu[gusan="menu"]::item:disabled {{ color: {c('faint')}; }}
QMenu[gusan="menu"]::separator {{ height: 1px; background: {c('line')}; margin: 4px 6px; }}
QCheckBox[gusan="task"] {{ spacing: 0px; }}
QCheckBox[gusan="task"]::indicator {{ width: 15px; height: 15px; border-radius: 4px; }}
QCheckBox[gusan="task"][focusVisible="true"]::indicator {{ border: 2px solid {c('blue-l')}; }}
QLabel[mtone="ink"] {{ color: {c('ink')}; }}
QLabel[mtone="text"] {{ color: {c('text')}; }}
QLabel[mtone="text2"] {{ color: {c('text2')}; }}
QLabel[mtone="muted"] {{ color: {c('muted')}; }}
QLabel[mtone="faint"] {{ color: {c('faint')}; }}
QLabel[mtone="blue"] {{ color: {c('blue-tx')}; }}
QLabel[mtone="red"] {{ color: {c('red-tx')}; }}
QLabel[mtone="amber"] {{ color: {c('amber-tx')}; }}
QLabel[mtone="ok"] {{ color: {c('ok-tx')}; }}
QLabel[mtone="pro"] {{ color: {c('pro-tx')}; }}
"""


# ── text helpers ─────────────────────────────────────────────────────────────
def tlabel(text="", px=14, weight=400, tone="text", display=False, parent=None, wrap=False,
           letter_spacing=None, tabular=False):
    """A QLabel with an exact font and a token colour (``tone``)."""
    lb = QLabel(text, parent)
    lb.setProperty("mtone", tone)
    lb.setFont(theme.font(px, weight, display=display, letter_spacing=letter_spacing, tabular=tabular))
    if wrap:
        lb.setWordWrap(True)
    return lb


def set_tone(widget, tone):
    theme.set_prop(widget, "mtone", tone, children=False)


def eyebrow(text, tone="blue", parent=None):
    return tlabel(text.upper(), 11, 800, tone, parent=parent, letter_spacing=1.54)


class ElidedLabel(QLabel):
    """One line that ends in '…' when it doesn't fit (full text in the tooltip)."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text or ""
        self.setTextFormat(Qt.PlainText)        # a name from outside (a Notion workspace) is text, never markup
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._elide()

    def full_text(self):
        return self._full

    def setText(self, text):                                    # noqa: N802 (Qt name)
        self._full = text or ""
        self.setAccessibleName(self._full)
        self._elide()
        self.updateGeometry()

    def sizeHint(self):
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self._full) + 2, super().sizeHint().height())

    def minimumSizeHint(self):
        return QSize(16, super().minimumSizeHint().height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.FontChange:
            self._elide()

    def _elide(self):
        fm = self.fontMetrics()
        shown = fm.elidedText(self._full, Qt.ElideRight, max(0, self.width()))
        QLabel.setText(self, shown)
        self.setToolTip(self._full if shown != self._full else "")


# Markdown helpers live in rich_text (one renderer for display, copy, paste
# and export); these names stay for callers and tests.
md_inline = rich_text.inline_html       # escape, then **bold**, *italic* / _italic_; `code` as plain text
strip_md = rich_text.strip_inline       # no ** * _ ` markers


def with_line_height(html, px):
    """Rich text with a CSS-like line height (the notes read at 14.5/23 px)."""
    return "<div style='line-height:%dpx'>%s</div>" % (int(px), html)


# ── glyphs, dots, faces ──────────────────────────────────────────────────────
# Stroke glyphs the shared icon set doesn't have (same 24 grid, the mockup's paths).
_GLYPHS = {
    "video": '<rect x="3" y="6.5" width="12.5" height="11" rx="2.2"/><path d="m15.5 10.6 5-2.9v8.6l-5-2.9z"/>',
    "bell_on": '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "bell_off": ('<path d="M8.3 6.2A6 6 0 0 1 18 11v4.2M6 11v5.5l-1.5 2h12"/><path d="M10 20.5a2 2 0 0 0 4 0"/>'
                 '<path d="m3.5 3.5 17 17"/>'),
}
_CACHE = {}


def _dpr():
    try:
        scr = QGuiApplication.primaryScreen()
        return max(1.0, float(scr.devicePixelRatio())) if scr else 1.0
    except Exception:
        return 1.0


def glyph(name, size=16, color="muted", sw=1.8):
    """A tinted pixmap: the shared icon set, or one of the local glyphs above."""
    if name not in _GLYPHS:
        if name not in icons.IC:
            name = "info"
        return icons.pixmap(name, size, color, sw)
    col = theme.qcolor(color) if not isinstance(color, QColor) else color
    dpr = _dpr()
    key = ("g", name, int(size), col.rgba(), float(sw), dpr)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
           'stroke="#%02x%02x%02x" stroke-opacity="%.3f" stroke-width="%s" stroke-linecap="round" '
           'stroke-linejoin="round">%s</svg>' % (col.red(), col.green(), col.blue(), col.alpha() / 255.0,
                                                sw, _GLYPHS[name]))
    pm = QPixmap(max(1, int(round(size * dpr))), max(1, int(round(size * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(p, QRectF(0, 0, size, size))
    p.end()
    _CACHE[key] = pm
    return pm


def icon_label(name, size=16, color="muted", sw=1.8, parent=None):
    lb = QLabel(parent)
    lb.setPixmap(glyph(name, size, color, sw))
    lb.setFixedSize(size, size)
    lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return lb


def dot_pixmap(color, size=8, ring=None):
    """A filled dot (``ring``: (colour, width) drawn around it, e.g. the next meeting)."""
    dpr = _dpr()
    col = theme.qcolor(color)
    key = ("dot", col.rgba(), size, str(ring), dpr)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    pad = ring[1] if ring else 0
    full = size + 2 * pad
    pm = QPixmap(int(round(full * dpr)), int(round(full * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    if ring:
        p.setBrush(theme.qcolor(ring[0]))
        p.drawEllipse(QRectF(0, 0, full, full))
    p.setBrush(col)
    p.drawEllipse(QRectF(pad, pad, size, size))
    p.end()
    _CACHE[key] = pm
    return pm


def dot_label(color, size=8, parent=None):
    lb = QLabel(parent)
    lb.setPixmap(dot_pixmap(color, size))
    lb.setFixedSize(size, size)
    lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return lb


def google_pixmap(size=16):
    """The Google G (assets/google_g.svg)."""
    dpr = _dpr()
    key = ("google", size, dpr)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    from app_info import resource_path
    pm = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    path = resource_path("assets", "google_g.svg")
    if os.path.isfile(path):
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        QSvgRenderer(path).render(p, QRectF(0, 0, size, size))
        p.end()
    _CACHE[key] = pm
    return pm


def google_label(size=16, parent=None):
    lb = QLabel(parent)
    lb.setPixmap(google_pixmap(size))
    lb.setFixedSize(size, size)
    lb.setAccessibleName("Google")
    return lb


class GoogleTile(QWidget):
    """The Google G on a small white disc (the Connect card)."""

    def __init__(self, size=40, g=20, parent=None):
        super().__init__(parent)
        self._size, self._g = size, g
        self.setFixedSize(size, size + 2)
        self.setAccessibleName("Google")

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, self._size - 1, self._size - 1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 23, 42, 14))
        p.drawEllipse(r.translated(0, 1))
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(theme.qcolor("line"), 1))
        p.drawEllipse(r)
        off = (self._size - self._g) / 2.0
        p.drawPixmap(int(off), int(off), google_pixmap(self._g))
        p.end()


# Call-type marks: a plain camera glyph tinted per service (no brand logos).
CALL_TONES = {"Google Meet": ("ok-sub", "ok-tx"), "Zoom": ("blue-sub", "blue-tx"),
              "Teams": ("#ede9fe", "#5b21b6"), "Webex": ("bg2", "text2")}


def call_name(kind):
    if not kind:
        return ""
    return "Video call" if kind == "other" else kind


def call_mark(kind, size=18):
    bg, fg = CALL_TONES.get(kind, ("bg2", "text2"))
    dpr = _dpr()
    key = ("call", kind, size, dpr)
    hit = _CACHE.get(key)
    if hit is None:
        hit = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
        hit.setDevicePixelRatio(dpr)
        hit.fill(Qt.transparent)
        p = QPainter(hit)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("#ffffff"))
        p.drawRoundedRect(QRectF(0, 0, size, size), 6, 6)
        p.setBrush(theme.qcolor(bg))
        p.drawRoundedRect(QRectF(0, 0, size, size), 6, 6)
        g = size - 6
        p.drawPixmap(3, 3, glyph("video", g, fg, 2.0))
        p.end()
        _CACHE[key] = hit
    lb = QLabel()
    lb.setPixmap(hit)
    lb.setFixedSize(size, size)
    return lb


FACE_TONES = (("blue-sub", "blue-tx"), ("ok-sub", "ok-tx"), ("line", "text2"), ("pro-sub", "pro-tx"),
              ("#fce7f3", "#be185d"), ("amber-sub", "amber-tx"))


def face_tone(name):
    key = (name or "?").strip() or "?"
    return FACE_TONES[sum(ord(ch) for ch in key) % len(FACE_TONES)]


class Faces(QWidget):
    """Overlapping round initials (people of a meeting). ``me``: the user's
    first name, drawn on the blue gradient like the sidebar avatar."""

    def __init__(self, names=(), size=24, me="", parent=None, limit=3):
        super().__init__(parent)
        self._size = int(size)
        self._me = (me or "").strip().lower()
        self._limit = limit
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.set_names(names)

    def set_names(self, names):
        names = [n for n in (names or []) if (n or "").strip()]
        self._names = names[: self._limit] if self._limit else names
        self.setAccessibleName(", ".join(names))
        w = self._size + max(0, len(self._names) - 1) * (self._size - 7) if self._names else 0
        self.setFixedSize(w, self._size)
        self.update()

    def set_me(self, me):
        self._me = (me or "").strip().lower()
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = self._size
        f = theme.font(max(8, int(s * 0.42)), 700)
        p.setFont(f)
        for i, name in enumerate(self._names):
            x = i * (s - 7)
            outer = QRectF(x, 0, s, s)
            inner = outer.adjusted(2, 2, -2, -2)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(outer)
            first = name.strip().split()[0].lower() if name.strip() else ""
            if self._me and first == self._me:
                g = QLinearGradient(inner.topLeft(), inner.bottomRight())
                g.setColorAt(0, QColor("#60a5fa"))
                g.setColorAt(1, theme.qcolor("blue"))
                p.setBrush(g)
                p.drawEllipse(inner)
                fg = QColor("#ffffff")
            else:
                bg, fg_name = face_tone(name)
                p.setBrush(theme.qcolor(bg))
                p.drawEllipse(inner)
                fg = theme.qcolor(fg_name)
            p.setPen(fg)
            p.drawText(outer, Qt.AlignCenter, name.strip()[:1].upper() or "?")
        p.end()


def hline(parent=None):
    d = Divider(parent)
    return d


def clear_layout(lay):
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.deleteLater()
        elif it.layout() is not None:
            clear_layout(it.layout())


def white_card(radius=16, shadow="card", parent=None):
    """A plain white surface (start card r20, notes article r16)."""
    fr = QFrame(parent)
    fr.setProperty("gusan", "whiteCard")
    fr.setProperty("radius", str(radius))
    if shadow:
        theme.elevate(fr, shadow)
    return fr


def make_menu(parent):
    m = QMenu(parent)
    m.setProperty("gusan", "menu")
    m.setWindowFlags(m.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    m.setAttribute(Qt.WA_TranslucentBackground, True)
    m.setFont(theme.font(13.5))
    return m


def hidden_status(app):
    """The Live Assistant chip's HideStatus: what the open card reports, else
    live_context.hide_status for the saved choice; None when unknown."""
    card = getattr(app, "live_assist", None)
    if card is not None and callable(getattr(card, "hide_state", None)):
        try:
            if card.isVisible():
                st = card.hide_state()
                if st is not None:
                    return st
        except Exception:
            logger.debug("card hide state unavailable", exc_info=True)
    try:
        import live_context
        cfg = getattr(app, "cfg", None) or {}
        return live_context.hide_status(cfg.get("live_assist_private", True))
    except Exception:
        logger.debug("hide status unavailable", exc_info=True)
        return None


def runs_on(app, feature):
    try:
        import engine_choice
        return engine_choice.runs_on(app, feature)
    except Exception:
        logger.debug("runs_on(%s) failed", feature, exc_info=True)
        return None


# ── the live view ────────────────────────────────────────────────────────────
class Meter(QWidget):
    """One sound meter: 44 thin bars, the newest level on the right."""

    BARS, W, GAP, H = 44, 3, 3, 32

    def __init__(self, accessible, parent=None):
        super().__init__(parent)
        self._hist = deque([0.0] * self.BARS, maxlen=self.BARS)
        self.setFixedSize(self.BARS * (self.W + self.GAP) - self.GAP, self.H)
        self.setAccessibleName(accessible)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def push(self, level):
        try:
            v = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            v = 0.0
        self._hist.append(v)
        self.update()

    def clear(self):
        self._hist.extend([0.0] * self.BARS)
        self.update()

    def level(self):
        return self._hist[-1]

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("blue-l"))
        for i, v in enumerate(self._hist):
            h = 4.0 if v < 0.02 else max(4.0, self.H * (0.14 + 0.86 * v))
            x = i * (self.W + self.GAP)
            p.drawRoundedRect(QRectF(x, (self.H - h) / 2.0, self.W, h), 1.5, 1.5)
        p.end()


class _Fade(QWidget):
    """Older transcript lines scroll away under a soft fade."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _e):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        top = theme.qcolor("#fcfdfe")
        g.setColorAt(0, top)
        end = QColor(top)
        end.setAlpha(0)
        g.setColorAt(1, end)
        p.fillRect(self.rect(), g)
        p.end()


class TranscriptPane(Card):
    """'Live transcript': You / Them lines, newest at the bottom; older ones fade."""

    FADE = (0.8, 0.6, 0.46, 0.38, 0.3, 0.25, 0.2, 0.16, 0.12)
    FULL = 4
    LIMIT = 400

    def __init__(self, parent=None):
        super().__init__(parent, pad=(22, 18), gap=14)
        self.setAccessibleName("Live transcript")
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(label("Live transcript", "h3", self))
        head.addStretch(1)
        self.model_icon = icon_label("laptop", 15, "muted", parent=self)
        head.addWidget(self.model_icon)
        self.model_label = label("", "model", self)
        head.addWidget(self.model_label)
        self.add(head)

        self.scroll = QScrollArea(self)
        self.scroll.setProperty("gusan", "scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # wheel / keys still scroll
        self.scroll.viewport().setAutoFillBackground(False)
        body = QWidget()
        body.setProperty("gusan", "scrollContent")
        self._lay = QVBoxLayout(body)
        self._lay.setContentsMargins(0, 0, 6, 0)
        self._lay.setSpacing(16)
        self._lay.addStretch(1)
        self.empty = tlabel("Listening…", 14.5, 400, "muted", parent=body)
        self._lay.addWidget(self.empty)
        self.scroll.setWidget(body)
        self.add(self.scroll, 1)
        self._fade = _Fade(self.scroll.viewport())
        self.scroll.viewport().installEventFilter(self)
        self._lines = []                 # (row, who label, text label, who)
        self._labels = True
        bar = self.scroll.verticalScrollBar()
        bar.rangeChanged.connect(self._on_range)
        bar.valueChanged.connect(lambda _v: self._apply_fade())
        self._follow = True

    def eventFilter(self, obj, ev):
        if obj is self.scroll.viewport() and ev.type() == QEvent.Resize:
            self._fade.setGeometry(0, 0, obj.width(), 40)
            self._fade.raise_()
        return False

    def set_model(self, info):
        if not info:
            self.model_label.setText("")
            self.model_icon.hide()
            return
        kind = info.get("kind")
        ic = {"cloud": "cloud", "own_key": "key"}.get(kind, "laptop")
        self.model_icon.setPixmap(glyph(ic, 15, "muted"))
        self.model_icon.show()
        self.model_label.setText(info.get("label") or "")

    def set_speaker_labels(self, on):
        self._labels = bool(on)
        for _row, who_lb, _t, _w in self._lines:
            who_lb.setVisible(self._labels)

    def clear(self):
        for row, _a, _b, _c in self._lines:
            row.hide()
            row.deleteLater()
        self._lines = []
        self.empty.show()
        self._follow = True

    def set_lines(self, lines):
        self.clear()
        for who, text in lines or []:
            self.add_line(text, who, fade=False)
        self._apply_fade()

    def lines(self):
        return [(w, t.text()) for _r, _wl, t, w in self._lines]

    def add_line(self, text, who="", fade=True):
        text = (text or "").strip()
        if not text:
            return
        self.empty.hide()
        bar = self.scroll.verticalScrollBar()
        self._follow = bar.value() >= bar.maximum() - 4
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        who_lb = tlabel((who or "").upper(), 11, 800, "blue" if who == "You" else "muted", letter_spacing=0.88)
        who_lb.setFixedWidth(46)
        who_lb.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        who_lb.setContentsMargins(0, 3, 0, 0)
        who_lb.setVisible(self._labels)
        h.addWidget(who_lb, 0, Qt.AlignTop)
        t = tlabel(text, 14.5, 400, "ink", wrap=True)
        t.setTextInteractionFlags(Qt.TextSelectableByMouse)
        t.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        h.addWidget(t, 1)
        self._lay.addWidget(row)
        self._lines.append((row, who_lb, t, who or ""))
        while len(self._lines) > self.LIMIT:
            old = self._lines.pop(0)
            old[0].hide()
            old[0].deleteLater()
        if fade:
            self._apply_fade()

    def _on_range(self, _lo, hi):
        if self._follow:
            self.scroll.verticalScrollBar().setValue(hi)

    def _apply_fade(self):
        bar = self.scroll.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4
        n = len(self._lines)
        start = max(0, n - (self.FULL + len(self.FADE) + 2))
        for i in range(start, n):
            _row, who_lb, t, who = self._lines[i]
            age = n - 1 - i
            alpha = 1.0
            if follow and age >= self.FULL:
                k = age - self.FULL
                alpha = self.FADE[k] if k < len(self.FADE) else self.FADE[-1]
            base = "text2" if age == 0 else "ink"
            wbase = "blue-tx" if who == "You" else "muted"
            t.setStyleSheet("color: %s;" % _alpha_qss(base, alpha))
            who_lb.setStyleSheet("color: %s;" % _alpha_qss(wbase, alpha))
        if follow and start > 0:
            for i in range(0, start):
                _row, who_lb, t, who = self._lines[i]
                if t.property("faded") is not True:
                    t.setProperty("faded", True)
                    t.setStyleSheet("color: %s;" % _alpha_qss("ink", self.FADE[-1]))
                    who_lb.setStyleSheet("color: %s;" % _alpha_qss("muted", self.FADE[-1]))
        elif not follow:
            for i in range(0, start):
                _row, who_lb, t, who = self._lines[i]
                t.setProperty("faded", False)
                t.setStyleSheet("color: %s;" % _alpha_qss("ink", 1.0))
                who_lb.setStyleSheet("color: %s;" % _alpha_qss("blue-tx" if who == "You" else "muted", 1.0))


def _alpha_qss(token, alpha):
    c = theme.qcolor(token)
    return "rgba(%d, %d, %d, %d)" % (c.red(), c.green(), c.blue(), int(round(255 * alpha)))


class SoFarBox(QFrame):
    """'So far': the rolling recap as a few blue bullets."""

    MAX = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "soFar")
        self.setAccessibleName("So far")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(label("So far", "h3", self))
        head.addWidget(dot_label("blue-l", 7, self), 0, Qt.AlignVCenter)
        head.addStretch(1)
        lay.addLayout(head)
        self._items = QVBoxLayout()
        self._items.setSpacing(6)
        lay.addLayout(self._items)
        self.placeholder = tlabel("The main points show up here as you talk.", 13.5, 400, "muted", wrap=True)
        lay.addWidget(self.placeholder)
        self._text = ""

    def text(self):
        return self._text

    def set_text(self, md):
        self._text = md or ""
        clear_layout(self._items)
        items = []
        for line in (md or "").splitlines():
            s = line.strip()
            if not s:
                continue
            s = re.sub(r"^#+\s*", "", s)
            s = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", s)
            if s:
                items.append(s)
        italic_only = len(items) == 1 and re.fullmatch(r"_(.+)_", items[0] or "")
        if italic_only:
            self.placeholder.setText(strip_md(items[0].strip("_")))
            self.placeholder.show()
            return
        self.placeholder.setVisible(not items)
        for s in items[: self.MAX]:
            row = QHBoxLayout()
            row.setSpacing(10)
            holder = QVBoxLayout()
            holder.setContentsMargins(0, 7, 0, 0)
            holder.addWidget(dot_label("blue-l", 5))
            holder.addStretch(1)
            row.addLayout(holder)
            t = tlabel("", 13.5, 400, "text", wrap=True)
            t.setTextFormat(Qt.RichText)
            t.setText(md_inline(s))
            row.addWidget(t, 1)
            self._items.addLayout(row)


class LiveView(QWidget):
    """A recording in progress: timer, two meters, Pause / Stop, the live
    transcript, 'So far', My notes and Open Live Assistant. After Stop the
    hero shows the processing status; the page opens the notes when ready."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.app = page.app
        self._status_text = ""
        self._loading_notes = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = PageHeader("Meeting", "", parent=self)
        root.addWidget(self.header)
        body = QVBoxLayout()
        body.setContentsMargins(32, 0, 32, 28)
        body.setSpacing(16)
        self.hero = self._build_hero()
        body.addWidget(self.hero)

        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        self.transcript = TranscriptPane(self)
        bottom.addWidget(self.transcript, 1)
        right = QWidget(self)
        right.setFixedWidth(340)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(16)
        self.so_far = SoFarBox(right)
        rl.addWidget(self.so_far)
        notes_col = QVBoxLayout()
        notes_col.setSpacing(10)
        notes_head = QHBoxLayout()
        notes_head.setSpacing(8)
        notes_head.addWidget(label("My notes", "h3", right))
        notes_head.addStretch(1)
        notes_head.addWidget(label("Saved with the meeting", "muted", right))
        notes_col.addLayout(notes_head)
        self.my_notes = QPlainTextEdit(right)
        self.my_notes.setProperty("gusan", "notesBox")
        self.my_notes.setAccessibleName("My notes")
        self.my_notes.setFont(theme.font(14))
        self.my_notes.setTabChangesFocus(True)
        self.my_notes.setMinimumHeight(72)
        self.my_notes.textChanged.connect(self._on_notes_typed)
        notes_col.addWidget(self.my_notes, 1)
        rl.addLayout(notes_col, 1)
        rl.addWidget(self._build_assist_rows(right))
        bottom.addWidget(right)
        body.addLayout(bottom, 1)
        root.addLayout(body, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)

    # ── building ──
    def _build_hero(self):
        hero = white_card(20, "lg", self)
        hero.setAccessibleName("Recording")
        lay = QHBoxLayout(hero)
        lay.setContentsMargins(28, 22, 28, 22)
        lay.setSpacing(36)

        tcol = QVBoxLayout()
        tcol.setSpacing(4)
        ey = QHBoxLayout()
        ey.setSpacing(8)
        self.state_dot = dot_label("red", 10, hero)
        ey.addWidget(self.state_dot, 0, Qt.AlignVCenter)
        self.state_label = eyebrow("Recording", "red", hero)
        ey.addWidget(self.state_label, 0, Qt.AlignVCenter)
        ey.addStretch(1)
        tcol.addLayout(ey)
        self.timer_label = tlabel("0:00", 76, 700, "ink", display=True, parent=hero,
                                  letter_spacing=-1.52, tabular=True)
        self.timer_label.setFixedHeight(88)
        self.timer_label.setAccessibleName("Recorded time")
        tcol.addWidget(self.timer_label)
        lay.addLayout(tcol, 0)

        self.meters = QWidget(hero)
        ml = QVBoxLayout(self.meters)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(10)
        ml.addStretch(1)                # the two rows sit together, centred on the timer
        self.mic_meter = Meter("Your microphone level", self.meters)
        self.call_meter = Meter("The call's sound level", self.meters)
        for ic, text, meter in (("mic", "You", self.mic_meter), ("volume", "The call", self.call_meter)):
            row = QHBoxLayout()
            row.setSpacing(12)
            row.addWidget(icon_label(ic, 16, "muted"), 0, Qt.AlignVCenter)
            lb = tlabel(text, 13.5, 600, "text")
            lb.setFixedWidth(64)
            row.addWidget(lb, 0, Qt.AlignVCenter)
            row.addWidget(meter, 0, Qt.AlignVCenter)
            row.addStretch(1)
            ml.addLayout(row)
        ml.addStretch(1)
        lay.addWidget(self.meters, 1)

        self.status_box = QWidget(hero)
        sl = QVBoxLayout(self.status_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(10)
        self.status_label = tlabel("Writing notes…", 15, 600, "ink", wrap=True)
        sl.addWidget(self.status_label)
        self.progress = QProgressBar(self.status_box)
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setAccessibleName("Writing notes")
        sl.addWidget(self.progress)
        self.status_hint = tlabel("You can leave this page. The notes keep coming.", 12.5, 400, "muted", wrap=True)
        sl.addWidget(self.status_hint)
        self.status_box.hide()
        lay.addWidget(self.status_box, 1)

        acts = QWidget(hero)
        acts.setFixedWidth(240)
        al = QVBoxLayout(acts)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(10)
        self.stop_btn = Button("Stop and write notes", "danger", "l", icon="stop", full=True, parent=acts)
        self.stop_btn.clicked.connect(self._stop)
        al.addWidget(self.stop_btn)
        self.pause_btn = Button("Pause", "secondary", "l", icon="pause", full=True, parent=acts)
        self.pause_btn.clicked.connect(self._pause)
        al.addWidget(self.pause_btn)
        self.back_btn = Button("Back to meetings", "secondary", "l", icon="chev_l", full=True, parent=acts)
        self.back_btn.clicked.connect(self._back)
        self.back_btn.hide()
        al.addWidget(self.back_btn)
        lay.addWidget(acts, 0, Qt.AlignVCenter)
        return hero

    def _build_assist_rows(self, parent):
        self.assist_box = QWidget(parent)
        parent = self.assist_box
        col = QVBoxLayout(self.assist_box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self.assist_btn = Button("Open Live Assistant", "primary", "m", icon="incognito", parent=parent)
        self.assist_btn.setProperty("hero", True)
        self.assist_btn.setFont(theme.font(13.5, 700))
        self.assist_btn.setAccessibleName("Open Live Assistant, Pro feature")
        self.assist_btn.clicked.connect(self._open_assistant)
        r1.addWidget(self.assist_btn)
        r1.addWidget(ProTag("s", parent=parent), 0, Qt.AlignVCenter)
        r1.addStretch(1)
        self.hidden_chip = Chip("Hidden", "ok", "incognito", parent=parent)
        r1.addWidget(self.hidden_chip, 0, Qt.AlignVCenter)
        col.addLayout(r1)
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        r2.addWidget(label("Or press", "muted", parent))
        self.assist_keys = Keycaps.from_hotkey("ctrl+alt+a", parent=parent)
        r2.addWidget(self.assist_keys)
        r2.addStretch(1)
        col.addLayout(r2)
        self.sync_assist()
        return self.assist_box

    # ── state ──
    def sync_assist(self):
        cfg = getattr(self.app, "cfg", None) or {}
        hk = cfg.get("live_assist_hotkey") or ""
        try:
            self.assist_keys.set_hotkey(hk)
        except Exception:
            self.assist_keys.set_keys([])
        self.assist_keys.setVisible(bool(hk))
        st = hidden_status(self.app)
        if st is None:
            self.hidden_chip.hide()
            return
        self.hidden_chip.show()
        self.hidden_chip.setText(st.text)
        theme.set_prop(self.hidden_chip, "tone", st.tone)
        self.hidden_chip.setToolTip("Not seen when you share your screen" if st.key == "on" else st.tip)
        ic = st.icon if st.icon in icons.IC else ("eye_off" if st.key == "partial" else None)
        lab = getattr(self.hidden_chip, "_icon", None)
        if lab is not None:
            if ic:
                lab.setPixmap(icons.pixmap(ic, 13, Chip._ICON_TONE.get(st.tone, "text2"), sw=2))
            lab.setVisible(bool(ic))

    def sync_model(self):
        self.transcript.set_model(runs_on(self.app, "dictation"))

    def sync(self):
        """Read everything from the session (on show, on a phase change)."""
        s = self.page.session()
        if s is None:
            return
        title = getattr(s, "title", "") or "Meeting"
        self.header.set_title(title)
        people = []
        try:
            import meeting_store
            people = meeting_store.split_attendees(getattr(s, "attendees", ""))
        except Exception:
            pass
        snap = {}
        try:
            snap = s.snapshot() or {}
        except Exception:
            snap = {}
        lang = lang_name(snap.get("language") or getattr(s, "_session_language", "") or "")
        sub = " · ".join(p for p in (", ".join(people), lang) if p)
        self.header.set_sub(sub)
        mode = snap.get("audio_mode") or getattr(s, "_session_audio_mode", "") or ""
        self.transcript.set_speaker_labels(mode == "smart_meeting")
        self.sync_model()
        self.sync_assist()
        try:
            lines = list(s.live_lines())[-TranscriptPane.LIMIT:]
        except Exception:
            lines = []
        if [(w or "", (t or "").strip()) for w, t in lines] != self.transcript.lines():
            self.transcript.set_lines(lines)
        self.so_far.set_text(getattr(s, "_live_summary_text", "") or "")
        self._loading_notes = True
        try:
            text = getattr(s, "my_notes", "") or ""
            if self.my_notes.toPlainText() != text:
                self.my_notes.setPlainText(text)
        finally:
            self._loading_notes = False
        self.set_phase(getattr(s, "phase", "idle"))

    def set_phase(self, phase):
        s = self.page.session()
        recording = phase in ("recording", "paused")
        self.meters.setVisible(recording)
        self.status_box.setVisible(not recording)
        self.stop_btn.setVisible(recording)
        self.pause_btn.setVisible(recording)
        self.back_btn.setVisible(not recording)
        self.my_notes.setReadOnly(not recording)
        self.assist_box.setVisible(recording)
        if phase == "recording":
            self._state("Recording", "red", "red")
            self.pause_btn.setText("Pause")
            self.pause_btn.setAccessibleName("Pause")
            self.pause_btn.setIcon(icons.icon("pause", 18, "text2"))
        elif phase == "paused":
            self._state("Paused", "amber", "amber")
            self.pause_btn.setText("Resume")
            self.pause_btn.setAccessibleName("Resume")
            self.pause_btn.setIcon(icons.icon("play", 18, "text2"))
            self.mic_meter.clear()
            self.call_meter.clear()
        elif phase == "processing":
            self._state("Writing notes", "blue", "blue")
            self.status_label.setText(self._status_text or "Writing notes…")
            self.progress.show()
            self.status_hint.show()
            set_tone(self.status_label, "ink")
        elif phase == "failed":
            self._state("Nothing recorded", "red", "red")
            err = getattr(s, "last_error", "") if s is not None else ""
            self.status_label.setText(err or "Nothing was recorded.")
            set_tone(self.status_label, "text")
            self.progress.hide()
            self.status_hint.hide()
        else:
            self._state("Done", "ok", "ok")
            self.progress.hide()
            self.status_hint.hide()
        self._tick()
        if recording and self.isVisible():
            self._timer.start()
        elif not recording:
            self._timer.stop()

    def _state(self, text, tone, dot):
        self.state_label.setText(text.upper())
        set_tone(self.state_label, tone)
        self.state_dot.setPixmap(dot_pixmap({"red": "red", "amber": "amber", "blue": "blue-l",
                                             "ok": "ok"}.get(dot, dot), 10))

    def set_status(self, text):
        self._status_text = text or ""
        if self.status_box.isVisible() and self.progress.isVisible():
            self.status_label.setText(self._status_text or "Writing notes…")

    def add_line(self, piece, who=""):
        self.transcript.add_line(piece, who)

    def set_recap(self, text):
        self.so_far.set_text(text)

    def set_levels(self, mic, call):
        self.mic_meter.push(mic)
        self.call_meter.push(call)

    def _tick(self):
        s = self.page.session()
        if s is None:
            return
        try:
            self.timer_label.setText(fmt_clock(s.elapsed_sec()))
        except Exception:
            pass

    def showEvent(self, e):
        super().showEvent(e)
        s = self.page.session()
        if s is not None and getattr(s, "phase", "") in ("recording", "paused"):
            self._timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()             # the recording itself keeps going

    # ── actions ──
    def _on_notes_typed(self):
        if self._loading_notes:
            return
        s = self.page.session()
        if s is not None and hasattr(s, "update_my_notes"):
            try:
                s.update_my_notes(self.my_notes.toPlainText())
            except Exception:
                logger.debug("update_my_notes failed", exc_info=True)

    def _stop(self):
        s = self.page.session()
        if s is None:
            return
        try:
            s.stop(self.my_notes.toPlainText())
        except Exception:
            logger.warning("Stopping the meeting failed", exc_info=True)

    def _pause(self):
        s = self.page.session()
        if s is None:
            return
        try:
            if getattr(s, "phase", "") == "paused":
                s.resume()
            else:
                s.pause()
        except Exception:
            logger.warning("Pause/resume failed", exc_info=True)

    def _back(self):
        s = self.page.session()
        if s is not None and getattr(s, "phase", "") == "failed" and not (getattr(s, "transcript", "") or "").strip():
            try:
                s.reset()
            except Exception:
                pass
        self.page.show_list()

    def _open_assistant(self):
        """Open (never hide) the Live Assistant card - same path as its page,
        for every plan: the card itself asks for Pro when Start is pressed."""
        if callable(getattr(self.app, "show_live_assist", None)):
            self.page.call("show_live_assist")
            return
        card = getattr(self.app, "live_assist", None)
        if card is not None and callable(getattr(card, "open_card", None)):
            try:
                card.open_card()
                return
            except Exception:
                logger.warning("open_card failed", exc_info=True)
        self.page.call("toggle_live_assist")


# ── the notes view ───────────────────────────────────────────────────────────
# The notes parser moved to rich_text (same blocks, same line_index: a ticked
# checkbox rewrites its own notes.md line). These names stay for callers/tests:
#   parse_notes(md) -> ("head", text) / ("para", text) / ("bullet", text) /
#                      ("task", (done, text, line_index)) / ("quote", text)
parse_task = rich_text.parse_task               # '`Fix X` (Owner: David, Due: Wed)' -> (task, owner, due)
parse_notes = rich_text.parse_notes
toggle_task_line = rich_text.toggle_task_line   # the notes with one checkbox line flipped
notes_plain = rich_text.notes_plain             # notes as plain paragraphs: no #, - [ ] -> ☐ / ☑


class ActionItem(QWidget):
    """One action item: checkbox, face, **Owner** task, due."""

    toggled = Signal(int, bool)

    def __init__(self, done, text, line_index, me="", parent=None):
        super().__init__(parent)
        self.line_index = line_index
        task, owner, due = parse_task(text)
        self.task, self.owner, self.due = task, owner, due
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.setMinimumHeight(30)
        self.box = QCheckBox(self)
        self.box.setProperty("gusan", "task")
        self.box.setFixedSize(18, 18)       # the mockup's 18 px box, no text gap
        self.box.setChecked(bool(done))
        self.box.setAccessibleName(task or "Action item")
        self.box.setCursor(Qt.PointingHandCursor)
        theme.focus_ring(self.box)
        lay.addWidget(self.box, 0, Qt.AlignVCenter)
        if owner:
            lay.addWidget(Faces([owner], 22, me=me, parent=self), 0, Qt.AlignVCenter)
        self._me = (me or "").strip().lower()
        self.text_label = QLabel(self)
        self.text_label.setTextFormat(Qt.RichText)
        self.text_label.setWordWrap(True)
        self.text_label.setFont(theme.font(14.5))
        self.text_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self.text_label, 1, Qt.AlignVCenter)
        self.due_label = tlabel(due, 12.5, 400, "muted")
        self.due_label.setVisible(bool(due))
        lay.addWidget(self.due_label, 0, Qt.AlignVCenter)
        self.box.toggled.connect(self._on_toggle)
        self._render()

    def owner_shown(self):
        if self.owner and self._me and self.owner.split()[0].lower() == self._me:
            return "You"
        return self.owner

    def _render(self):
        done = self.box.isChecked()
        ink = theme.qss_color("ink")
        col = theme.qss_color("muted" if done else "text")
        body = md_inline(self.task)
        if done:
            body = "<s>%s</s>" % body
        who = self.owner_shown()
        head = ("<b style='color:%s'>%s</b>&nbsp;&nbsp;" % (ink, html_mod.escape(who))) if who else ""
        self.text_label.setText(with_line_height("%s<span style='color:%s'>%s</span>" % (head, col, body), 22))

    def _on_toggle(self, on):
        self._render()
        self.toggled.emit(self.line_index, bool(on))


class NotesDoc(QWidget):
    """The rendered notes (Summary, Decisions, Action items, Open questions)."""

    task_toggled = Signal(int, bool)
    LINE = 23                   # 14.5 / 23 px, as the mockup's notes
    BULLET_TOP = 12             # the bullet dot on the first line's x-height middle (Qt sets the
                                # baseline at 4/5 of a fixed line height)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "scrollContent")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(32, 28, 32, 28)
        self._lay.setSpacing(0)
        self.tasks = []
        self.me = ""

    def render(self, md, banner=None, notices=(), written_by=None, blocks=None):
        """``blocks`` (instead of parsing ``md``): parse_notes' blocks, plus
        ("html", rich text) paragraphs (the transcript shown when there are no notes)."""
        clear_layout(self._lay)
        self.tasks = []
        lay = self._lay
        if banner is not None:
            lay.addWidget(banner)
            lay.addSpacing(20)
        for n in notices or ():
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(icon_label("alert", 15, "amber-tx"), 0, Qt.AlignTop)
            row.addWidget(tlabel(str(n), 13, 400, "muted", wrap=True), 1)
            lay.addLayout(row)
            lay.addSpacing(10)
        if notices:
            lay.addSpacing(10)
        first = True
        prev = None
        for kind, data in (parse_notes(md) if blocks is None else blocks):
            if kind == "head":
                if not first:
                    lay.addSpacing(20)
                lb = tlabel(data, 18, 600, "ink", display=True, wrap=True)
                lb.setAccessibleName(data)
                lay.addWidget(lb)
                lay.addSpacing(10)
            elif kind == "task":
                if prev == "task":
                    lay.addSpacing(4)
                done, text, idx = data
                item = ActionItem(done, text, idx, me=self.me, parent=self)
                item.toggled.connect(self.task_toggled)
                self.tasks.append(item)
                lay.addWidget(item)
            elif kind == "bullet":
                if prev == "bullet":
                    lay.addSpacing(4)
                row = QHBoxLayout()
                row.setSpacing(10)
                holder = QVBoxLayout()
                holder.setContentsMargins(0, self.BULLET_TOP, 0, 0)
                holder.addWidget(dot_label("text2", 5))
                holder.addStretch(1)
                row.addLayout(holder)
                t = tlabel("", 14.5, 400, "text", wrap=True)
                t.setTextFormat(Qt.RichText)
                t.setText(with_line_height(md_inline(data), self.LINE))
                t.setTextInteractionFlags(Qt.TextSelectableByMouse)
                row.addWidget(t, 1)
                lay.addLayout(row)
            else:
                if prev in ("para", "quote", "html"):
                    lay.addSpacing(10)
                t = tlabel("", 14.5, 400, "muted" if kind == "quote" else "text", wrap=True)
                t.setTextFormat(Qt.RichText)
                t.setText(with_line_height(data if kind == "html" else md_inline(data), self.LINE))
                t.setTextInteractionFlags(Qt.TextSelectableByMouse)
                lay.addWidget(t)
            first = False
            prev = kind
        if written_by is not None:
            lay.addSpacing(20)
            lay.addWidget(Divider(self))
            lay.addSpacing(20)
            lay.addWidget(written_by)
        lay.addStretch(1)


def written_by_row(label_text, parent=None):
    """'Written by Gusan Cloud · Mistral Small' from meta notes_engine_label."""
    text = (label_text or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split(" · ")]
    head, model = parts[0], " · ".join(parts[1:])
    if head == "Gusan Cloud":
        ic, words = "cloud", "Written by Gusan Cloud"
    elif head == "This computer":
        ic, words = "laptop", "Written on this computer"
    elif head == "Built-in formatter":
        ic, words = "text", "Written by the built-in formatter"
    else:
        ic, words = "key", "Written by " + head
    w = QWidget(parent)
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(icon_label(ic, 15, "muted"), 0, Qt.AlignVCenter)
    row.addWidget(label(words, "muted", w))
    if model:
        sep = tlabel("·", 12.5, 400, "faint")
        row.addWidget(sep)
        m = label(model, "model", w)
        m.setFont(theme.font(12.5, tabular=True))
        row.addWidget(m)
    row.addStretch(1)
    w.setAccessibleName(words + (" · " + model if model else ""))
    return w


class NotesView(QWidget):
    """One saved meeting: Notes / Transcript / My notes, Copy, Export, Continue
    recording (Pro), and ⋯ (Rewrite notes, Rename, Show in folder, Delete)."""

    _notion_settings = Signal(dict)     # settings a Notion send changed (from its worker thread)

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.app = page.app
        self._notion_settings.connect(self._save_notion_settings)
        self.folder = None
        self.data = {}
        self._bg = BgRunner(self)
        self._load_token = 0
        self._notes_save = QTimer(self)
        self._notes_save.setSingleShot(True)
        self._notes_save.setInterval(800)
        self._notes_save.timeout.connect(self._save_my_notes)
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.setInterval(FLASH_MS)
        self._flash.timeout.connect(self._end_flash)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())
        root.addLayout(self._build_toolbar())
        body = QHBoxLayout()
        body.setContentsMargins(32, 0, 32, 28)
        body.setSpacing(24)
        self.article = white_card(16, "card", self)
        self.article.setAccessibleName("Meeting notes")
        self.article.setMinimumWidth(380)
        self.article.setMaximumWidth(620)
        self.article.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        al = QVBoxLayout(self.article)
        al.setContentsMargins(1, 1, 1, 1)
        al.setSpacing(0)
        self.stack = QStackedWidget(self.article)
        al.addWidget(self.stack)
        self.notes_scroll = QScrollArea(self.stack)
        self.notes_scroll.setProperty("gusan", "scroll")
        self.notes_scroll.setWidgetResizable(True)
        self.notes_scroll.setFrameShape(QFrame.NoFrame)
        self.notes_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.notes_scroll.viewport().setAutoFillBackground(False)
        self.doc = NotesDoc()
        self.doc.task_toggled.connect(self._on_task)
        self.notes_scroll.setWidget(self.doc)
        self.stack.addWidget(self.notes_scroll)
        self.transcript = QTextBrowser(self.stack)
        self.transcript.setProperty("gusan", "bare")
        self.transcript.setAccessibleName("Transcript")
        self.transcript.setFont(theme.font(14.5))
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setViewportMargins(32, 24, 24, 24)
        self.stack.addWidget(self.transcript)
        self.my_notes = QPlainTextEdit(self.stack)
        self.my_notes.setProperty("gusan", "bare")
        self.my_notes.setAccessibleName("My notes")
        self.my_notes.setFont(theme.font(14.5))
        self.my_notes.setPlaceholderText("Nothing written during this meeting.")
        self.my_notes.setViewportMargins(32, 24, 24, 24)
        self.my_notes.setTabChangesFocus(True)
        self.my_notes.textChanged.connect(self._on_my_notes)
        self._loading = False
        self.stack.addWidget(self.my_notes)
        body.addWidget(self.article, 1)

        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(16)
        side.addStretch(1)
        self.people_card = Card(self, pad=(18, 16), gap=12)
        self.people_card.setAccessibleName("People")
        self.people_card.add(label("People", "h3", self.people_card))
        self._people = QVBoxLayout()
        self._people.setSpacing(10)
        self.people_card.add(self._people)
        side.addWidget(self.people_card)
        self.audio_card = self._build_audio_card()
        side.addWidget(self.audio_card)
        sidew = QWidget(self)
        sidew.setLayout(side)
        sidew.setMinimumWidth(280)
        body.addWidget(sidew, 0)
        root.addLayout(body, 1)

    # ── building ──
    def _build_header(self):
        w = QWidget(self)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(32, 24, 32, 20)
        lay.setSpacing(6)
        self.back_link = Button("Meetings", "link", "s", icon="chev_l", parent=w)
        self.back_link.setFont(theme.font(13, 600))
        self.back_link.setIcon(icons.icon("chev_l", 14, "blue-tx", sw=2))
        self.back_link.setIconSize(QSize(14, 14))
        self.back_link.setFixedHeight(20)
        self.back_link.setAccessibleName("Back to Meetings")
        self.back_link.clicked.connect(lambda: self.page.show_list())
        lay.addWidget(self.back_link, 0, Qt.AlignLeft)
        self.title_label = label("", "title", w)
        self.title_label.setFixedHeight(34)
        lay.addWidget(self.title_label)
        self._meta = QHBoxLayout()
        self._meta.setSpacing(8)
        lay.addLayout(self._meta)
        w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        return w

    def _build_toolbar(self):
        bar = QHBoxLayout()
        bar.setContentsMargins(32, 0, 32, 16)
        bar.setSpacing(8)
        self.tabs = Segmented(["Notes", "Transcript", "My notes"], 0, "Meeting view", parent=self)
        self.tabs.changed.connect(self.stack_index)
        bar.addWidget(self.tabs, 0, Qt.AlignVCenter)
        bar.addStretch(1)
        self.flash_label = tlabel("", 12.5, 600, "ok")
        self.flash_label.hide()
        bar.addWidget(self.flash_label, 0, Qt.AlignVCenter)
        self.copy_btn = Button("Copy notes", "primary", "m", icon="copy", parent=self)
        self.copy_btn.clicked.connect(self.copy_notes)
        bar.addWidget(self.copy_btn)
        chev = QLabel(self)
        chev.setPixmap(icons.pixmap("chev_d", 15, "muted"))
        self.export_btn = Button("Export", "secondary", "m", icon="export", parent=self, trail=chev)
        self.export_btn.setAccessibleName("Export")
        self.export_menu = make_menu(self)
        for key, ic, text, hint in (("word", "file", "Word", ".docx"), ("markdown", "text", "Markdown", ".md"),
                                    ("notion", "notion", "Notion", ""), ("audio", "volume", "Audio", ".mp3")):
            act = self.export_menu.addAction(icons.icon(ic, 16, "muted"), text + ("\t" + hint if hint else ""))
            act.setData(key)
        self.export_menu.triggered.connect(lambda a: self.export(a.data()))
        self.export_menu.aboutToHide.connect(lambda: theme.set_prop(self.export_btn, "open", False))
        self.export_btn.clicked.connect(self._show_export)
        bar.addWidget(self.export_btn)
        self.continue_btn = Button("Continue recording", "secondary", "m", parent=self, trail=ProTag("s"))
        self.continue_btn.setIcon(QIcon(dot_pixmap("red", 8)))
        self.continue_btn.setIconSize(QSize(8, 8))
        self.continue_btn.setAccessibleName("Continue recording, Pro feature")
        self.continue_btn.clicked.connect(self.continue_recording)
        bar.addWidget(self.continue_btn)
        self.more_btn = IconButton("more", "More", size=36, parent=self, framed=True)
        self.more_menu = make_menu(self)
        self.act_rewrite = self.more_menu.addAction(icons.icon("refresh", 16, "muted"), "Rewrite notes")
        self.act_rename = self.more_menu.addAction(icons.icon("pen", 16, "muted"), "Rename")
        self.act_folder = self.more_menu.addAction(icons.icon("file", 16, "muted"), "Show in folder")
        self.more_menu.addSeparator()
        self.act_delete = self.more_menu.addAction(icons.icon("trash", 16, "red-tx"), "Delete")
        self.act_rewrite.triggered.connect(self.rewrite_notes)
        self.act_rename.triggered.connect(self.rename)
        self.act_folder.triggered.connect(self.show_in_folder)
        self.act_delete.triggered.connect(self.delete)
        self.more_btn.clicked.connect(lambda: self.more_menu.popup(
            self.more_btn.mapToGlobal(self.more_btn.rect().bottomRight()) - _menu_offset(self.more_menu)))
        bar.addWidget(self.more_btn)
        return bar

    def _build_audio_card(self):
        card = Card(self, pad=(18, 16), gap=6)
        card.setAccessibleName("Recording")
        # Plays inside the app (ui/components/audio_player.py): play / pause,
        # seek on the waveform, the parts of a continued meeting one after another.
        self.audio_player = AudioPlayer(card)
        self.audio_player.failed.connect(lambda _text: self.flash("Couldn't play the recording.", "red"))
        card.add(self.audio_player)
        self.play_btn = self.audio_player.play_btn
        self.audio_start = self.audio_player.time_label
        self.audio_len = self.audio_player.total_label
        return card

    # ── loading ──
    def load(self, folder, item=None):
        """Show the meeting in ``folder`` (read on a worker thread). My notes
        typed a moment ago are saved first, on the same worker, so a reload
        or another meeting never loses them."""
        pending = self._take_pending_notes()
        if str(folder) != self.folder:
            self.audio_player.stop(release=True)     # another meeting: let go of this recording
        self.folder = str(folder)
        self.tabs.set_index(0)
        self.stack.setCurrentIndex(0)
        if item:
            self.title_label.setText(item.get("title") or "Meeting")
        self._load_token += 1
        token = self._load_token
        folder = self.folder

        def work():
            if pending is not None:
                import meeting_store
                try:
                    meeting_store.save_my_notes(*pending)
                except Exception:
                    logger.warning("Saving My notes failed", exc_info=True)
            return read_meeting(folder)
        self._bg.run(work, lambda res, err, t=token: self._apply(t, res, err))

    def _take_pending_notes(self):
        """(folder, text) of a My notes save still waiting on its timer, or None."""
        if not self._notes_save.isActive() or not self.folder:
            return None
        self._notes_save.stop()
        return self.folder, self.my_notes.toPlainText()

    def reload(self):
        if self.folder:
            self.load(self.folder)

    def _apply(self, token, res, err):
        if token != self._load_token:
            return
        if err is not None or not res:
            self.data = {"folder": self.folder, "title": "Meeting", "notes": "", "transcript": "",
                         "my_notes": "", "meta": {}, "parts": []}
        else:
            self.data = res
        self.render()
        meta = self.data.get("meta") or {}
        if self.data.get("notes", "").strip() and meta.get("seen") is False:
            folder = self.folder

            def mark():
                import meeting_store
                return meeting_store.mark_seen(folder)
            self._bg.run(mark, lambda _r, _e: self.page.list_changed())

    def render(self):
        d = self.data
        meta = d.get("meta") or {}
        title = d.get("title") or "Meeting"
        self.title_label.setText(title)
        self.title_label.setAccessibleName(title)
        clear_layout(self._meta)
        people = d.get("people") or []
        parts = []
        when = fmt_when(d.get("started"), with_time=False)
        if when:
            parts.append(label(when, "muted"))
        if d.get("duration_sec"):
            parts.append(label(fmt_duration(d.get("duration_sec")), "muted"))
        if people:
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            h.addWidget(Faces(people, 22, me=self.page.me()), 0, Qt.AlignVCenter)
            h.addWidget(label(", ".join(people), "muted"))
            parts.append(w)
        lang = lang_name(meta.get("language"))
        if lang:
            parts.append(label(lang, "muted"))
        notion_url = str(meta.get("notion_url") or "")
        if notion_url.startswith("https://"):
            link = Button("In Notion", "link", "s")
            link.setFont(theme.font(13.5, 600))
            link.setAccessibleName("Open this meeting in Notion")
            link.clicked.connect(lambda _=False, u=notion_url: QDesktopServices.openUrl(QUrl(u)))
            parts.append(link)
        for i, wdg in enumerate(parts):
            if isinstance(wdg, QLabel):
                wdg.setFont(theme.font(13.5))
            if i:
                self._meta.addWidget(tlabel("·", 13.5, 400, "faint"))
            self._meta.addWidget(wdg, 0, Qt.AlignVCenter)
        self._meta.addStretch(1)

        notes = d.get("notes") or ""
        busy = self.page.is_active_folder(self.folder)
        banner = None
        has_transcript = bool((d.get("transcript") or "").strip())
        if busy:
            banner = self._banner("Writing notes…", "The notes show up here when they're ready.", "blue")
        elif meta.get("notes_pending") and has_transcript:
            # Gusan quit (or Windows logged off) mid-meeting: the next launch
            # writes them; until then one click does.
            banner = self._banner(
                "Notes not written yet." if not notes.strip() else "Notes not updated yet.",
                "Gusan closed before it could write them. The recording and transcript are saved.",
                "blue", "Write notes", self.rewrite_notes)
        elif not notes.strip():
            err = (meta.get("notes_error") or "").strip()
            if err and has_transcript:
                # A real failure (meta notes_error): amber, and try again.
                banner = self._banner("Notes couldn't be written.", err, "amber", "Write notes again",
                                      self.rewrite_notes)
            elif err:
                banner = self._banner("Nothing to show yet.", err, "amber")
            elif has_transcript:
                # D11: no notes and no error (e.g. Gusan quit while it was
                # transcribing): a calm line, nothing went wrong.
                banner = self._banner("No notes yet", "", "neutral", "Write notes", self.rewrite_notes)
            else:
                banner = self._banner("Nothing to show yet.", "This meeting has only its recording.", "neutral")
        notices = [n for n in (meta.get("notices") or []) if isinstance(n, str) and n.strip()]
        self.doc.me = self.page.me()
        blocks = None
        if not notes.strip() and has_transcript:
            # No notes: the saved transcript is still worth reading right here,
            # in readable paragraphs (D10).
            text2 = theme.qss_color("text2")
            blocks = [("head", "Transcript")] + [("html", transcript_row_html(row, text2))
                                                 for row in transcript_rows(d["transcript"])[:400]]
        self.doc.render(notes, banner=banner, notices=notices, blocks=blocks,
                        written_by=written_by_row(meta.get("notes_engine_label")) if notes.strip() else None)
        for task in self.doc.tasks:
            task.box.setEnabled(not busy)       # the notes are being rewritten: nothing to tick yet
        self.transcript.setHtml(transcript_html(d.get("transcript") or ""))
        mine = d.get("my_notes") or ""
        if self.my_notes.toPlainText() != mine:      # never jump the caret of someone typing
            self._loading = True
            try:
                self.my_notes.setPlainText(mine)
            finally:
                self._loading = False
        self._render_people(people)
        audio = d.get("parts") or []
        self.audio_card.setVisible(bool(audio))
        self.audio_player.set_parts(audio, d.get("duration_sec") or 0)     # the same parts: keeps playing
        self.copy_btn.setEnabled(bool(notes.strip()))
        self.sync_actions()

    def _render_people(self, people):
        clear_layout(self._people)
        me = self.page.me()
        rows = list(people)
        if me and not any(p.split()[0].lower() == me.lower() for p in rows if p.strip()):
            rows.append(me)
        for name in rows:
            r = QHBoxLayout()
            r.setSpacing(10)
            r.addWidget(Faces([name], 28, me=me), 0, Qt.AlignVCenter)
            r.addWidget(tlabel(name, 13.5, 600, "text"), 1, Qt.AlignVCenter)
            if me and name.split()[0].lower() == me.lower():
                r.addWidget(label("You", "muted"), 0, Qt.AlignVCenter)
            self._people.addLayout(r)
        self.people_card.setVisible(bool(rows))

    def _banner(self, title, text, tone="amber", button=None, slot=None):
        fr = QFrame()
        fr.setProperty("gusan", "banner")
        fr.setProperty("tone", tone)
        fr.setAccessibleName(title)
        h = QHBoxLayout(fr)
        h.setContentsMargins(14, 12, 14, 12)
        h.setSpacing(12)
        ic = {"amber": ("alert", "amber-tx"), "red": ("alert", "red-tx"), "blue": ("clock", "blue-tx"),
              "neutral": ("text", "muted")}[tone]
        h.addWidget(icon_label(ic[0], 18, ic[1]), 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(tlabel(title, 13.5, 600, "ink", wrap=True))
        if text:
            col.addWidget(tlabel(text, 12.5, 400, "text2", wrap=True))
        h.addLayout(col, 1)
        if button:
            b = Button(button, "secondary", "s", parent=fr)
            b.clicked.connect(slot)
            h.addWidget(b, 0, Qt.AlignVCenter)
            fr.button = b
        self.banner = fr
        return fr

    def sync_actions(self):
        """Pro tags stay; actions follow the session (nothing while it records/writes)."""
        d = self.data or {}
        busy_here = self.page.is_active_folder(self.folder)
        s = self.page.session()
        session_busy = bool(s is not None and getattr(s, "is_busy", False))
        self.continue_btn.setEnabled(not session_busy)
        has_transcript = bool((d.get("transcript") or "").strip())
        self.act_rewrite.setEnabled(has_transcript and not session_busy)
        self.act_delete.setEnabled(not busy_here)
        self.export_btn.setEnabled(bool(self.folder))
        if self.audio_player.is_playing and s is not None and getattr(s, "phase", "") == "recording":
            # A meeting records (from here, the tray, the calendar...): the
            # playback would be captured with the call's sound.
            self.audio_player.pause()

    def stack_index(self, i):
        self.stack.setCurrentIndex(i)
        if self.tabs.index() != i:
            self.tabs.set_index(i)

    # ── actions ──
    def flash(self, text, tone="ok", ms=None):
        """A short line by the toolbar; ``ms`` keeps a long one (an error to read) up longer."""
        self.flash_label.setText(text)
        set_tone(self.flash_label, tone)
        self.flash_label.show()
        self._flash.start(ms or FLASH_MS)

    def _end_flash(self):
        self.flash_label.hide()
        self.copy_btn.setText("Copy notes")
        self._flash.setInterval(FLASH_MS)       # the next "Copied" is short again

    def copy_notes(self):
        # D9: HTML (Word, Outlook, Gmail, Docs, Notion, Slack, Teams get real
        # headings, lists and bold) + nice plain text ("☐ Fix drift (David · by Wed)").
        notes = (self.data.get("notes") or "").strip()
        if not notes:
            return
        ok = bool(rich_clipboard.copy_rich(notes))
        if ok:
            self.copy_btn.setText("Copied")
            self._flash.start(FLASH_MS)
        else:
            self.flash("Couldn't copy the notes.", "red")
        self.page.track("meeting_exported", {"target": "clipboard", "ok": ok})

    def _show_export(self):
        theme.set_prop(self.export_btn, "open", True)
        self.export_menu.popup(self.export_btn.mapToGlobal(self.export_btn.rect().bottomLeft()))

    def export(self, kind):
        d = self.data or {}
        if not self.folder:
            return
        title = d.get("title") or "Meeting"
        from docx_export import safe_filename
        stem = safe_filename(title, "Meeting notes")
        docs = os.path.join(os.path.expanduser("~"), "Documents")
        base = docs if os.path.isdir(docs) else os.path.expanduser("~")
        if kind == "word":
            path, _ = QFileDialog.getSaveFileName(self, "Save as Word", os.path.join(base, stem + ".docx"),
                                                  "Word document (*.docx)")
            if not path:
                return
            if not path.lower().endswith(".docx"):
                path += ".docx"
            paras = word_paragraphs(d.get("notes") or "", d.get("transcript") or "")
            meta_line = self._meta_line()

            def work():
                import docx_export
                return docx_export.save_docx(path, title, paras, meta_line=meta_line, include_timestamps=False)
            self._bg.run(work, lambda r, e: self._exported("word", path, e))
        elif kind == "markdown":
            path, _ = QFileDialog.getSaveFileName(self, "Save as Markdown", os.path.join(base, stem + ".md"),
                                                  "Markdown (*.md)")
            if not path:
                return
            if not path.lower().endswith(".md"):
                path += ".md"
            text = "# %s\n\n%s\n\n%s\n" % (title, self._meta_line(), (d.get("notes") or "").strip())

            def work():
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                return path
            self._bg.run(work, lambda r, e: self._exported("markdown", path, e))
        elif kind == "notion":
            self.send_to_notion()
        elif kind == "audio":
            parts = list(d.get("parts") or [])
            if not parts:
                self.flash("No recording saved for this meeting.", "amber")
                return
            path, chosen = QFileDialog.getSaveFileName(self, "Save recording", os.path.join(base, stem + ".mp3"),
                                                       "MP3 audio (*.mp3);;WAV audio (*.wav)")
            if not path:
                return
            if "wav" in (chosen or "").lower() and not path.lower().endswith(".wav"):
                path += ".wav"
            elif not path.lower().endswith((".mp3", ".wav")):
                path += ".mp3"

            def work():
                import audio_export
                return audio_export.export_recording(parts, path)
            # export_recording returns None when nothing was written: not "Saved".
            self._bg.run(work, lambda r, e: self._exported("audio", path, e or (None if r else OSError(path))))

    def _meta_line(self):
        d = self.data or {}
        bits = [fmt_when(d.get("started")), fmt_duration(d.get("duration_sec")) if d.get("duration_sec") else "",
                ", ".join(d.get("people") or [])]
        return " · ".join(b for b in bits if b)

    def _exported(self, target, path, err):
        self.page.track("meeting_exported", {"target": target, "ok": err is None})
        if err is not None:
            self.flash("Couldn't save the file.", "red")
        else:
            self.flash("Saved " + os.path.basename(path))

    def send_to_notion(self):
        d = self.data or {}
        if not ((d.get("notes") or "").strip() or (d.get("transcript") or "").strip()):
            self.flash("Nothing to send yet.", "amber")
            return
        import notion_export
        cfg = getattr(self.app, "cfg", None) or {}
        if not notion_export.is_connected(cfg) or cfg.get("notion_reconnect"):
            # Connect first; the sheet ends in "Send this meeting" and the same click goes on.
            if not self.page.connect_notion(then=NOTION_THEN):
                return
            cfg = getattr(self.app, "cfg", None) or {}
        if cfg.get("privacy_mode") and not mini_prompt.ask(
                self, privacy_prompt.TITLE, "Send this meeting to Notion anyway?", "Send", "Not now",
                icon="incognito"):
            return
        folder, mode = self.folder, notion_export.connection_mode(cfg) or "own"
        self.flash("Sending to Notion…", "muted")

        def work():
            import notion_sync
            # persist runs on this worker: the signal hops to the GUI thread
            return notion_sync.send_meeting(folder, dict(cfg), auto=False, persist=self._notion_settings.emit)

        def done(res, err):
            res = res or {}
            ok = err is None and bool(res.get("ok"))
            props = {"target": "notion", "ok": ok, "auto": False, "mode": mode}
            code = str(res.get("code") or "other")
            if not ok:
                props["reason"] = code if code in NOTION_REASONS else "other"
            self.page.track("meeting_exported", props)
            if ok:
                self.flash("Sent to Notion")
                url = str(res.get("url") or "")
                if url.startswith("https://"):
                    QDesktopServices.openUrl(QUrl(url))
                if self.folder == folder:
                    self.reload()               # the meta row gets its "In Notion" link
            elif code == "busy":
                self.flash("Already sending this meeting.", "muted")
            else:
                self.flash(res.get("sentence") or "Couldn't send to Notion.", "red", FLASH_ERROR_MS)
        self._bg.run(work, done)

    def _save_notion_settings(self, updates):
        """A Notion send changed the connection (a fresh token, "connect again")."""
        self.page.set_settings(dict(updates))

    def continue_recording(self):
        if not self.folder:
            return
        if not self.page.is_pro():
            self.page.call("_pro_upsell", UPSELL_RECORD)
            return
        if not self.page.ensure_consent():
            return
        s = self.page.session(create=True)
        if s is None:
            self.flash("The recorder isn't ready yet.", "red")
            return
        self.audio_player.pause()          # never record the playback into the next part
        try:
            ok, _code, msg = s.continue_meeting(self.folder)
        except Exception as e:
            ok, msg = False, str(e)
        if ok:
            self.page.show_live()
        else:
            self.flash(msg or "Couldn't continue this meeting.", "red")

    def rewrite_notes(self):
        if not self.folder:
            return
        s = self.page.session(create=True)
        if s is None:
            self.flash("Notes can't be written right now.", "red")
            return
        try:
            ok = s.retry_notes(None, folder=self.folder)
        except Exception:
            logger.warning("Rewrite notes failed", exc_info=True)
            ok = False
        if ok:
            self.render()
        else:
            self.flash("Notes can't be rewritten right now.", "amber")

    def rename(self):
        if not self.folder:
            return
        current = (self.data or {}).get("title") or ""
        text, ok = QInputDialog.getText(self, "Rename meeting", "Name", text=current)
        if not ok:
            return
        self.apply_rename(text)

    def apply_rename(self, text):
        folder = self.folder

        def work():
            import meeting_store
            return meeting_store.set_title(folder, text)

        def done(ok, err):
            if ok and err is None:
                self.data["title"] = " ".join(str(text or "").split()) or "Untitled Meeting"
                self.title_label.setText(self.data["title"])
                self.page.list_changed()
            else:
                self.flash("Couldn't rename this meeting.", "red")
        self._bg.run(work, done)

    def show_in_folder(self):
        if self.folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.folder))

    def delete(self):
        if not self.folder or self.page.is_active_folder(self.folder):
            return
        title = (self.data or {}).get("title") or "this meeting"
        box = QMessageBox(self)
        box.setWindowTitle("Delete meeting")
        box.setText("Delete “%s”?" % title)
        box.setInformativeText("Its notes, transcript and recording are removed from this computer.")
        yes = box.addButton("Delete", QMessageBox.DestructiveRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is yes:
            self.apply_delete()

    def apply_delete(self):
        folder = self.folder
        # A loaded recording holds its file open: Windows would refuse the delete.
        self.audio_player.stop(release=True)

        def work():
            import meeting_store
            return meeting_store.delete_meeting(folder)

        def done(ok, err):
            if ok and err is None:
                # No telemetry here: "meeting_deleted" isn't allow-listed yet
                # (meetings.py Integration notes, Telemetry).
                self.folder = None
                self.audio_player.set_parts([])
                self.page.show_list(refresh=True)
            else:
                self.flash("Couldn't delete this meeting.", "red")
        self._bg.run(work, done)

    def play(self):
        """Play or pause the recording, right here."""
        self.audio_player.toggle()

    def _on_task(self, line_index, done):
        if not self.folder:
            return
        notes = toggle_task_line(self.data.get("notes") or "", line_index, done)
        self.data["notes"] = notes
        path = os.path.join(self.folder, "notes.md")
        version = next_version()
        self._bg.run(lambda: write_latest(path, notes, version), None)

    def _on_my_notes(self):
        if self._loading or not self.folder:
            return
        self.data["my_notes"] = self.my_notes.toPlainText()
        self._notes_save.start()

    def _save_my_notes(self):
        folder, text = self.folder, self.my_notes.toPlainText()
        if not folder:
            return

        def work():
            import meeting_store
            return meeting_store.save_my_notes(folder, text)
        self._bg.run(work, None)

    def hideEvent(self, e):
        super().hideEvent(e)
        if self._notes_save.isActive():
            self._notes_save.stop()
            self._save_my_notes()
        if not e.spontaneous():
            # Leaving the page or closing the window to the tray stops the
            # sound and lets go of the file (Settings › Delete all meetings).
            # Minimizing (a spontaneous hide) keeps it playing.
            self.audio_player.stop(release=True)


def _menu_offset(menu):
    from PySide6.QtCore import QPoint
    return QPoint(menu.sizeHint().width(), -6)


def read_meeting(folder):
    """Everything the notes view shows, read off the GUI thread."""
    import meeting_store
    meta = meeting_store.load_meta(folder)
    notes = meeting_store.load_notes(folder)
    transcript = meeting_store.load_transcript(folder)
    my_notes = meeting_store.load_my_notes(folder)
    parts = [str(p) for p in meeting_store.audio_parts(folder)]
    people = meta.get("attendees_list")
    if not (isinstance(people, list) and all(isinstance(p, str) for p in people)):
        people = meeting_store.split_attendees(meta.get("attendees") or "")
    started = meta.get("started") or meeting_store.folder_timestamp(os.path.basename(str(folder).rstrip("\\/")))
    duration = 0
    try:
        duration = max(0, int(meta.get("duration_sec") or 0))
    except (TypeError, ValueError):
        duration = 0
    if not duration and parts:
        try:
            import audio_export
            duration = int(audio_export.duration_seconds(parts) or 0)
        except Exception:
            duration = 0
    return {"folder": str(folder), "meta": meta, "title": str(meta.get("title") or "Untitled Meeting"),
            "notes": notes, "transcript": transcript, "my_notes": my_notes, "parts": parts,
            "people": list(people), "started": started, "duration_sec": duration}


_SPK_RE = rich_text.SPEAKER_RE          # "Speaker 1: …", "You: …", "Anna: …"


def transcript_rows(text):
    """The saved transcript as readable paragraphs (D10), done when it is
    shown, so old meetings read well too: [(label, body, marker)]. A long line
    (a whole meeting is often one line) is packed by sentences up to ~550
    characters (rich_text.transcript_paragraphs); ``label`` is the speaker on
    the first paragraph of a turn ("" on the rest); ``marker`` is True for a
    '— Resumed at 11:20 —' line."""
    rows = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        for i, para in enumerate(rich_text.transcript_paragraphs(line)):
            if rich_text.is_marker(para):
                rows.append(("", para, True))
            elif i == 0:
                label, body = rich_text.split_speaker(para)
                rows.append((label, body, False))
            else:
                rows.append(("", para, False))
    return rows


def transcript_row_html(row, label_color=None):
    """One transcript_rows() row as rich text: the speaker bold, a marker in italics."""
    label, body, marker = row
    if marker:
        return "<i>%s</i>" % html_mod.escape(body)
    if label:
        style = " style='color:%s'" % label_color if label_color else ""
        return "<b%s>%s</b>&nbsp; %s" % (style, html_mod.escape(label), html_mod.escape(body))
    return html_mod.escape(body)


def transcript_html(text):
    """The saved transcript: speaker names bold, readable paragraphs (D10)."""
    ink, muted, text2 = theme.qss_color("ink"), theme.qss_color("muted"), theme.qss_color("text2")
    rows = []
    for row in transcript_rows(text):
        if row[2]:
            rows.append("<p style='color:%s; font-style:italic; margin:0 0 10px 0; line-height:23px'>%s</p>"
                        % (muted, html_mod.escape(row[1])))
        else:
            rows.append("<p style='margin:0 0 10px 0; line-height:23px; color:%s'>%s</p>"
                        % (ink, transcript_row_html(row, text2)))
    if not rows:
        rows.append("<p style='color:%s'>No transcript for this meeting.</p>" % muted)
    return "<div>%s</div>" % "".join(rows)


def word_paragraphs(notes, transcript=""):
    """The Word export's paragraphs (docx_export.build_docx): the notes as
    styled blocks (bold headings, • / ☐ items, **bold** runs), then
    "Transcript" and its readable paragraphs with the speaker in bold."""
    import docx_export
    paras = docx_export.notes_paragraphs(notes or "")
    rows = transcript_rows(transcript)
    if rows:
        paras.append({"style": "heading", "text": "Transcript"})
        for label, body, marker in rows:
            if marker:
                paras.append({"style": "marker", "text": body})
            else:
                paras.append({"start": 0, "speaker": None, "label": label, "text": body})
    return paras
