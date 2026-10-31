"""Meeting reminder (Gusan 2.0): the "record this meeting?" toast by the tray.

Mockup: v2/canvas/project/MeetingReminder.dc.html (source v2/pages/floating.py
meeting_reminder(), states a "1 min before" and b "After it starts"). Calm Pro:
the PRO tag sits on Record for Free users only and always on Record + Live
Assistant; a Free user's tap opens the current Pro window, nothing else.

    toast = MeetingReminder(main_app)
    toast.record.connect(on_record)        # (meeting, with_assist) - Pro users only
    toast.dismissed.connect(on_dismissed)  # "Not this one" (or Esc)
    toast.join.connect(on_join)            # "Join call": the toast already opened the link
    toast.closed.connect(on_closed)        # (meeting, reason) after every close
    toast.show_meeting(meeting)            # a calendar_service meeting dict

The toast is a frameless, always-on-top Tool window that never takes focus
(WA_ShowWithoutActivating + glass.set_no_activate), out of screen capture
while cfg "overlay_private" is on (the same switch as the dictation bar:
Settings > Privacy "Hide the dictation bar from screen shares"). It sits in
the bottom-right corner above the taskbar (top-right under the menu bar on a
Mac), updates its "Starts in 1 min" / "Started 2 min ago · Record now?" line
every 15 s and closes itself when calendar_sync.should_prompt turns False
(5 min after the start, or when the meeting ends).

Glass: the toast paints its own frosted plate and soft shadow. On Windows it
blurs a sample of the screen behind it (one grab before it shows, while it
can't be in the picture; then every 3 s on a worker thread - as the
dictation bar does - while it is out of screen capture; a black
hardware-video hole is skipped). The OS acrylic (glass.apply_backdrop_blur)
and DWM rounding (glass.round_corners) are deliberately NOT used: they act on
the whole square window, shadow margin included, and draw a blurred rectangle
around the rounded toast (found on the Live Assistant card,
ui/live_assist.py _apply_glass); DWM's rounding is also 8 px, the mockup's 20.

Integration notes for Wave 4
----------------------------
Status: DONE in main.py (AppController._on_calendar_remind / _ensure_reminder /
record_from_calendar / _on_reminder_dismissed; the tray icon's click gives a
showing toast the keyboard focus after opening the window). record_from_calendar
starts app.meeting_session itself (not the page's record=True route).

* Create one toast lazily and reuse it (calendar_service ``remind`` signal)::

      def _on_calendar_remind(self, meeting):
          from ui.floating.meeting_reminder import MeetingReminder
          if self._reminder is None:
              self._reminder = MeetingReminder(main_app=self)
              self._reminder.record.connect(
                  lambda m, assist: self.record_from_calendar(m, with_assist=assist))
              self.sig_auth_changed.connect(self._reminder.refresh_account)
              self.sig_setting_changed.connect(self._reminder.on_setting_changed)
          self._reminder.show_meeting(meeting, anchor=self.tray_icon.geometry())

  ``anchor`` (optional) is the tray icon's QRect: the toast picks the screen
  that holds it; without it, the primary screen.
* ``_calendar_busy()`` should return True while ``self._reminder.is_showing()``
  (calendar_service note 3: one toast at a time).
* ``record`` fires only for Pro users (the toast itself calls
  ``app._pro_upsell("Meeting recording" | "Live Assistance")`` for Free users,
  hides while the Pro window is up and comes back afterwards if the meeting is
  still in its reminder window). It has already hidden itself, so
  ``AppController._on_reminder_record`` counts the tap (calendar_reminder_action
  record | record_assist) and ``record_from_calendar(m, with_assist)`` starts
  app.meeting_session with the title, the people on the invite and the event id
  (consent and fast chunks included). With_assist=True also builds the Live
  Assistant card first, sends the invite's agenda (the card's INVITE chip shows it,
  so the user can read and edit it) and opens the card; a plain Record sends the
  title and names only. Starting from here, the start card or the Live card marks
  the meeting asked (``note_calendar_start``).
* ``join`` is informational (telemetry: join): the toast opens the https link
  itself with QDesktopServices and stays up, so the user can still tap Record.
* ``dismissed`` needs no handling beyond a count (dismiss): calendar_service marks
  the meeting asked before ``remind`` fires (note 6), so "Not this one" only closes
  the toast. ``closed(m, "expired")`` is counted too (nobody answered).
* Methods the toast calls on main_app (all optional, looked up by name):
  ``is_pro()``, ``_pro_upsell(feature)``, ``cfg`` (dict, read only).
* Keyboard: the toast never takes focus by itself (it must not steal the
  user's typing). ``toast.take_focus()`` activates it and focuses Record
  (then Tab / Shift+Tab through Record, Record + Live Assistant, Not this
  one, Join call; Esc = Not this one). Give keyboard users a way in, e.g. call
  it when the tray icon is activated while ``is_showing()``, or from the tray
  balloon's on_click.
* Not built here (mockup states c and d): the meeting capsule after Record
  ("Recording · 0:48" with faces and a stop button) belongs with the
  recording UI, and the Windows-notification variant for when Gusan's window
  is closed was optional - this toast shows whether or not the window is open.
* Icons: the camera ("video") and external-link glyphs are drawn locally
  (ui/components/icons.py has neither); the call-type tints match
  ui/pages/meeting_views.py CALL_TONES. Moving both into icons.IC would let
  the two share them.
"""

import logging
import threading
from datetime import datetime, timezone

from PySide6.QtCore import QByteArray, QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFontMetrics, QGuiApplication, QIcon,
                           QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

import calendar_sync
from ui import glass, theme
from ui.components import Button, ProTag, icons

logger = logging.getLogger("transcribe")

TOAST_W = 352                       # the mockup's TOAST_W
PAD_L, PAD_T, PAD_R, PAD_B = 16, 14, 16, 8
RADIUS = 20
SH_SIDE, SH_TOP, SH_BOTTOM = 14, 12, 20     # transparent margin for the drop shadow
REFRESH_MS = 15 * 1000              # "Starts in 1 min" -> "Started 2 min ago"
SAMPLE_MS = 3000                    # backdrop re-sample while out of screen capture
PRIVATE_KEY = "overlay_private"     # Settings > Privacy: hide floating bars from screen shares

FEATURE_RECORD = "Meeting recording"
FEATURE_ASSIST = "Live Assistance"
RECORD_NOW = "Record now?"
SEP = "  ·  "   # the mockup's "Google Meet  ·  Anna, David +2"
REC_RED = "#ff3b4e"                 # the record dot (mockup rec_dot)

# Call-type marks: a plain camera glyph tinted per service (no brand logos);
# the same tints as the Meetings page (ui/pages/meeting_views.py CALL_TONES).
CALL_TONES = {"Google Meet": ("ok-sub", "ok-tx"), "Zoom": ("blue-sub", "blue-tx"),
              "Teams": ("#ede9fe", "#5b21b6"), "Webex": ("bg2", "text2")}
_GLYPHS = {
    "video": '<rect x="3" y="6.5" width="12.5" height="11" rx="2.2"/><path d="m15.5 10.6 5-2.9v8.6l-5-2.9z"/>',
    "external": ('<path d="M13.5 4.5h6v6M19.5 4.5 11 13M17.5 14v4.5a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-11'
                 'a1 1 0 0 1 1-1H10"/>'),
}
_CACHE = {}


# ── helpers ──────────────────────────────────────────────────────────────────
def _call(app, method, *args, **kwargs):
    fn = getattr(app, method, None) if app is not None else None
    if not callable(fn):
        logger.debug("main_app has no %s()", method)
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("%s() failed", method, exc_info=True)
        return None


def _platform():
    try:
        return QGuiApplication.platformName()
    except Exception:
        return ""


def _native_windows():
    """A real Win32 window - never the offscreen platform's fake ids."""
    return glass.IS_WINDOWS and _platform() == "windows"


def _native_mac():
    return glass.IS_MAC and _platform() == "cocoa"


def _dpr():
    try:
        scr = QGuiApplication.primaryScreen()
        return max(1.0, float(scr.devicePixelRatio())) if scr else 1.0
    except Exception:
        return 1.0


def glyph(name, size=16, color="muted", sw=1.8):
    """A tinted stroke glyph: the shared icon set, or one of the local ones above."""
    if name not in _GLYPHS:
        return icons.pixmap(name if name in icons.IC else "info", size, color, sw)
    col = color if isinstance(color, QColor) else theme.qcolor(color)
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


def call_name(kind):
    """What the meta line calls the service ("" without a link; "other" has no brand)."""
    return calendar_sync.call_label(kind)


def call_mark_pixmap(kind, size=18):
    """The call-type mark: a rounded tinted tile with the camera glyph."""
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
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(QRectF(0, 0, size, size), 6, 6)
        p.setBrush(theme.qcolor(bg))
        p.drawRoundedRect(QRectF(0, 0, size, size), 6, 6)
        g = size - 6
        p.drawPixmap(3, 3, glyph("video", g, fg, 2.0))
        p.end()
        _CACHE[key] = hit
    return hit


def rec_dot_pixmap(size=10, ring=2):
    """The red record dot with its white ring (mockup rec_dot)."""
    dpr = _dpr()
    key = ("rec", size, ring, dpr)
    hit = _CACHE.get(key)
    if hit is None:
        full = size + 2 * ring
        hit = QPixmap(int(round(full * dpr)), int(round(full * dpr)))
        hit.setDevicePixelRatio(dpr)
        hit.fill(Qt.transparent)
        p = QPainter(hit)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(0, 0, full, full))
        p.setBrush(QColor(REC_RED))
        p.drawEllipse(QRectF(ring, ring, size, size))
        p.end()
        _CACHE[key] = hit
    return hit


def meeting_now(meeting, clock=None):
    """'now' comparable with the meeting's start (aware, or naive local)."""
    now = clock() if clock is not None else datetime.now(timezone.utc)
    start = meeting.get("start") if isinstance(meeting, dict) else None
    if isinstance(start, datetime):
        if start.tzinfo is None and now.tzinfo is not None:
            now = now.astimezone().replace(tzinfo=None)
        elif start.tzinfo is not None and now.tzinfo is None:
            now = now.astimezone()
    return now


def when_line(meeting, now):
    """(text, started): "Starts in 1 min" (blue) or "Started 2 min ago · Record now?" (amber)."""
    try:
        text = calendar_sync.reminder_text(meeting, now)
        started = (meeting["start"] - now).total_seconds() <= 0
    except Exception:
        return "", False
    if started:
        text = text + " · " + RECORD_NOW
    return text, started


def meta_line(meeting):
    """"Google Meet  ·  Anna, David +2" (either part may be missing)."""
    m = meeting or {}
    people = m.get("people")
    if people is None:
        people = calendar_sync.people_label(m.get("others") or m.get("attendees"))
    parts = [p for p in (call_name(m.get("call_type")), str(people or "").strip()) if p]
    return SEP.join(parts)


def join_link(meeting):
    """The join URL when it is https (the only kind the toast opens), else ""."""
    link = str((meeting or {}).get("link") or "").strip()
    return link if link.lower().startswith("https://") else ""


# ── pieces ───────────────────────────────────────────────────────────────────
class _ElidedLabel(QLabel):
    """One line that ends in "…" when it doesn't fit; the full text is the tooltip.
    Always plain text: the title and names come from a calendar invite, which
    anyone can send, and a QLabel would read "<img src=...>" in them as markup and
    load that image (from the disk, or from a network share) with no click."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.PlainText)            # before the first text
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMinimumWidth(10)
        self.set_full_text(text)

    def full_text(self):
        return self._full

    def set_full_text(self, text):
        self._full = str(text or "")
        self.setAccessibleName(self._full)
        self._fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        w = self.width() if self.width() > 20 else 10_000
        shown = QFontMetrics(self.font()).elidedText(self._full, Qt.ElideRight, w)
        QLabel.setText(self, shown)
        self.setToolTip(theme.plain_tip(self._full) if shown != self._full else "")

    def sizeHint(self):
        fm = QFontMetrics(self.font())
        return QSize(fm.horizontalAdvance(self._full) + 2, fm.height())


class _ContentButton(Button):
    """A full-width v2 Button whose label group is centred: [lead] text [PRO].

    (Button's own ``trail`` pins the tag to the right edge; the mockup centres
    the dot, the words and the tag together.)"""

    def __init__(self, text, kind="primary", lead=None, pro=False, parent=None):
        super().__init__("", kind, "m", parent=parent, full=True)
        self.setAccessibleName(text)
        weight = 600 if kind == "primary" else 500
        colour = "#ffffff" if kind == "primary" else theme.qss_color("text2")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(8)
        lay.addStretch(1)
        self.lead = lead
        if lead is not None:
            lead.setParent(self)
            lead.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            lay.addWidget(lead, 0, Qt.AlignVCenter)
        self.text_label = QLabel(text, self)
        self.text_label.setFont(theme.font(13.5, weight))
        self.text_label.setStyleSheet("color: %s; background: transparent;" % colour)
        self.text_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.text_label, 0, Qt.AlignVCenter)
        self.pro_tag = ProTag("s", parent=self)
        self.pro_tag.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.pro_tag, 0, Qt.AlignVCenter)
        lay.addStretch(1)
        self.set_pro(pro)

    def text(self):
        return self.text_label.text()

    def set_pro(self, on):
        self.pro_tag.setVisible(bool(on))
        name = self.text_label.text()
        self.setAccessibleDescription("Pro feature" if on else "")
        self.setToolTip("Pro feature" if on else "")
        self.setAccessibleName(name)

    def has_pro_tag(self):
        return not self.pro_tag.isHidden()


# ── the toast ────────────────────────────────────────────────────────────────
class MeetingReminder(QWidget):
    """The reminder toast. See the module docstring."""

    record = Signal(object, bool)       # (meeting, with_assist) - Pro users only
    dismissed = Signal(object)          # "Not this one" / Esc
    join = Signal(object)               # "Join call" (already opened)
    closed = Signal(object, str)        # (meeting, "record" | "dismissed" | "expired" | "hidden")
    _sampled = Signal(object, int, bool)  # worker -> GUI: (blurred QImage | None, generation, got a grab)

    def __init__(self, main_app=None, parent=None, clock=None):
        super().__init__(parent)
        self.app = main_app
        self._clock = clock
        self._meeting = None
        self._started = False
        self._anchor = None
        self._excluded = False
        self._bd = None                 # blurred backdrop sample (QImage) or None
        self._hwnd = 0                  # the native window id, read on the GUI thread
        self._sample_gen = 0
        self._sampling = False          # a worker grab is on its way
        self._shadow_key = None
        self._shadow_img = None
        self._upselling = False
        self._kbd_focus = False         # take_focus() waiting for the activation
        self._sampled.connect(self._on_sampled)

        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setWindowTitle("Meeting reminder")
        self.setAccessibleName("Meeting reminder")
        theme.load_fonts()
        self.setFont(theme.font(14))
        self.setStyleSheet(theme.stylesheet())

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._sample_timer = QTimer(self)
        self._sample_timer.setInterval(SAMPLE_MS)
        self._sample_timer.timeout.connect(self._resample)
        self._build()
        self.setFixedWidth(TOAST_W + 2 * SH_SIDE)

    # ── layout ──
    def _build(self):
        # The footer's "Not this one" pulls 8 px left (its hover box sits in the
        # padding, the words line up with the rest), so the column starts 8 px
        # early and every other row adds those 8 px back.
        root = QVBoxLayout(self)
        root.setContentsMargins(SH_SIDE + PAD_L - 8, SH_TOP + PAD_T, SH_SIDE + PAD_R, SH_BOTTOM + PAD_B)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.setContentsMargins(8, 0, 0, 0)
        head.setSpacing(10)
        self.logo = QLabel(self)
        self.logo.setPixmap(icons.logo_pixmap(28))
        self.logo.setFixedSize(28, 28)
        self.logo.setAccessibleName("Gusan")
        head.addWidget(self.logo, 0, Qt.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(1)
        self.title_label = _ElidedLabel("", self)
        theme.set_role(self.title_label, "h3")               # ink colour from the stylesheet
        self.title_label.setFont(theme.font(16, 700, display=True))
        self.title_label.setFixedHeight(21)
        words.addWidget(self.title_label)
        self.when_label = _ElidedLabel("", self)
        self.when_label.setFont(theme.font(13, 600))
        self.when_label.setFixedHeight(18)
        words.addWidget(self.when_label)
        head.addLayout(words, 1)
        root.addLayout(head)

        self.meta_row = QWidget(self)
        meta = QHBoxLayout(self.meta_row)
        meta.setContentsMargins(8 + 38, 0, 0, 0)
        meta.setSpacing(6)
        self.call_mark = QLabel(self.meta_row)
        self.call_mark.setFixedSize(18, 18)
        meta.addWidget(self.call_mark, 0, Qt.AlignVCenter)
        self.meta_label = _ElidedLabel("", self.meta_row)
        theme.set_role(self.meta_label, "muted")
        self.meta_label.setFixedHeight(18)
        meta.addWidget(self.meta_label, 1, Qt.AlignVCenter)
        root.addWidget(self.meta_row)

        btns = QVBoxLayout()
        btns.setContentsMargins(8, 4, 0, 0)
        btns.setSpacing(8)
        dot = QLabel()
        dot.setPixmap(rec_dot_pixmap())
        dot.setFixedSize(14, 14)
        self.record_button = _ContentButton("Record", "primary", lead=dot, parent=self)
        self.record_button.clicked.connect(lambda _=False: self._on_record(False))
        btns.addWidget(self.record_button)
        cam = QLabel()
        cam.setPixmap(icons.pixmap("incognito", 16, "text2"))
        cam.setFixedSize(16, 16)
        self.assist_button = _ContentButton("Record + Live Assistant", "secondary", lead=cam, pro=True,
                                            parent=self)
        # The board's rgba(255,255,255,0.78): a touch of the glass shows through.
        # (A widget's own rule beats the inherited :hover, so hover / pressed are restated.)
        self.assist_button.setStyleSheet(
            'QPushButton[gusan="btn"][kind="secondary"] { background: %s; }'
            'QPushButton[gusan="btn"][kind="secondary"]:hover { background: %s; }'
            'QPushButton[gusan="btn"][kind="secondary"]:pressed { background: %s; }'
            % (theme.qss_color("rgba(255,255,255,0.78)"), theme.qss_color("bg"), theme.qss_color("bg2")))
        self.assist_button.clicked.connect(lambda _=False: self._on_record(True))
        btns.addWidget(self.assist_button)
        root.addLayout(btns)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.setSpacing(8)
        self.dismiss_button = Button("Not this one", "ghost", "s", parent=self)
        self.dismiss_button.setStyleSheet(
            'QPushButton[gusan="btn"][kind="ghost"] { color: %s; padding: 0px 8px; }'
            'QPushButton[gusan="btn"][kind="ghost"]:hover { color: %s; }'
            % (theme.qss_color("muted"), theme.qss_color("text")))
        self.dismiss_button.clicked.connect(lambda _=False: self.dismiss())
        foot.addWidget(self.dismiss_button, 0, Qt.AlignVCenter)
        foot.addStretch(1)
        self.join_button = Button("Join call", "link", "s", parent=self)
        self.join_button.setLayoutDirection(Qt.RightToLeft)      # the arrow after the words
        self.join_button.setIcon(QIcon(glyph("external", 13, "blue-tx", 2.0)))
        self.join_button.setIconSize(QSize(13, 13))
        self.join_button.clicked.connect(lambda _=False: self._on_join())
        foot.addWidget(self.join_button, 0, Qt.AlignVCenter)
        root.addLayout(foot)

        self.setTabOrder(self.record_button, self.assist_button)
        self.setTabOrder(self.assist_button, self.dismiss_button)
        self.setTabOrder(self.dismiss_button, self.join_button)

    # ── state ──
    def meeting(self):
        return self._meeting

    def is_showing(self):
        """Up on screen - or only hidden while the Pro window it opened is up."""
        return (self.isVisible() or self._upselling) and self._meeting is not None

    def when_text(self):
        return self.when_label.full_text()

    def started(self):
        return self._started

    def _is_pro(self):
        if self.app is None:
            return True
        try:
            return bool(self.app.is_pro())
        except Exception:
            return False

    def _cfg(self):
        cfg = getattr(self.app, "cfg", None)
        return cfg if isinstance(cfg, dict) else {}

    def _now(self):
        return meeting_now(self._meeting or {}, self._clock)

    def set_meeting(self, meeting):
        """Fill the toast for ``meeting`` (a calendar_service meeting dict)."""
        self._meeting = dict(meeting or {})
        m = self._meeting
        title = str(m.get("title") or "").strip() or "Meeting"
        self.title_label.set_full_text(title)
        meta = meta_line(m)
        kind = m.get("call_type") or ""
        self.call_mark.setVisible(bool(kind))
        if kind:
            self.call_mark.setPixmap(call_mark_pixmap(kind, 18))
        self.meta_label.set_full_text(meta)
        self.meta_row.setVisible(bool(meta))
        link = join_link(m)
        self.join_button.setVisible(bool(link))
        host = ""
        if link:
            try:
                from urllib.parse import urlsplit
                host = urlsplit(link).hostname or ""
            except ValueError:
                host = ""
        self.join_button.setToolTip(theme.plain_tip(host))
        self.refresh_account()
        self._update_when()
        self.setAccessibleDescription("%s. %s" % (title, self.when_text()))
        self._fit_height()

    def _fit_height(self):
        lay = self.layout()
        lay.activate()
        h = max(lay.sizeHint().height(), lay.minimumSize().height())
        self.setFixedHeight(h)
        self._shadow_key = None

    def _update_when(self):
        if not self._meeting:
            return
        text, started = when_line(self._meeting, self._now())
        self._started = started
        self.when_label.set_full_text(text)
        tone = "amber-tx" if started else "blue-tx"
        self.when_label.setStyleSheet("color: %s; background: transparent;" % theme.qss_color(tone))

    def refresh_account(self):
        """PRO on Record only for Free users (sign-in / plan changes)."""
        self.record_button.set_pro(not self._is_pro())
        self.assist_button.set_pro(True)

    def on_setting_changed(self, key):
        """sig_setting_changed: the privacy switch takes effect on a toast that is up."""
        if key == PRIVATE_KEY and self.isVisible():
            self._apply_private()
            if self._excluded:
                self._sample_timer.start()

    def still_due(self):
        """True while calendar_sync.should_prompt holds for the meeting."""
        if not self._meeting:
            return False
        try:
            return bool(calendar_sync.should_prompt(self._meeting, self._now()))
        except Exception:
            return False

    def refresh(self):
        """The 15 s tick: the when-line, and close once the reminder window is over."""
        if not self._meeting:
            return
        if not self.still_due():
            self._finish("expired")
            return
        self._update_when()

    # ── showing ──
    def show_meeting(self, meeting, anchor=None):
        """Fill, place (bottom-right above the taskbar) and show without taking focus.
        Returns False (and shows nothing) when the meeting is no longer due."""
        self.set_meeting(meeting)
        self._anchor = anchor
        if not self.still_due():
            self._finish("expired")
            return False
        self._show_now()
        return True

    def _show_now(self):
        up = self.isVisible()                                # show_meeting again while it is up
        self.place()
        self._sample_gen += 1                                # drop a grab from before (re-show)
        self._sampling = False
        if not up:
            self._bd = None
        native = _native_windows()
        if native:
            self._hwnd = int(self.winId())                   # the HWND, before it maps
            glass.set_no_activate(self)
            self._apply_private()
            if not up:
                self._sample_backdrop()                      # not visible yet: never in the sample
        self.show()
        self.raise_()
        if native or _native_mac():
            QTimer.singleShot(0, self._after_show)
        self._timer.start()

    def _after_show(self):
        if not self.isVisible():
            return
        if _native_windows():
            glass.set_no_activate(self)                      # Qt may rebuild styles on show
        self._apply_private()
        if self._excluded:
            self._sample_timer.start()

    def _screen(self):
        anchor = self._anchor
        if isinstance(anchor, QRect) and anchor.isValid():
            scr = QGuiApplication.screenAt(anchor.center())
            if scr is not None:
                return scr
        return QGuiApplication.primaryScreen()

    def target_pos(self, screen=None):
        """Window top-left: the toast's corner SH_SIDE / SH_BOTTOM px inside the
        available area (above the taskbar); top-right under the menu bar on a Mac.
        The whole window stays inside the available area, so its shadow never
        covers the taskbar's tray icons."""
        scr = screen or self._screen()
        if scr is None:
            return self.pos()
        a = scr.availableGeometry()
        x = a.x() + a.width() - self.width()
        if glass.IS_MAC:
            y = a.y()
        else:
            y = a.y() + a.height() - self.height()
        return QRect(x, y, self.width(), self.height()).topLeft()

    def place(self):
        self.move(self.target_pos())

    def _apply_private(self):
        want = bool(self._cfg().get(PRIVATE_KEY, True))
        self._excluded = False
        try:
            if _native_windows():
                if want and glass.capture_exclusion_supported() and not glass.is_remote_session():
                    self._excluded = bool(glass.exclude_from_capture(self, True))
                else:
                    glass.exclude_from_capture(self, False)
            elif _native_mac():
                glass.exclude_from_capture(self, want)
        except Exception:
            logger.debug("reminder capture exclusion failed", exc_info=True)
        if not self._excluded:
            self._sample_timer.stop()

    def _sample_backdrop(self):
        """Blur what is behind the toast (Windows), once, right before it shows:
        not mapped yet, so it can't be in the picture. One grab (about a frame);
        the refreshes while it is up run on a worker (_resample)."""
        try:
            scr = self._screen()
            if scr is None:
                return
            g, sg = self.frameGeometry(), scr.geometry()
            pm = scr.grabWindow(0, g.x() - sg.x(), g.y() - sg.y(), g.width(), g.height())
            if pm.isNull():
                return
            self._bd = blur_backdrop(pm.toImage(), max(1, self.width()), max(1, self.height()))
            self.update()
        except Exception:
            logger.debug("reminder backdrop sample failed", exc_info=True)

    def _resample(self):
        """The 3 s tick while out of screen capture: grab what is behind on a
        worker thread (a grab waits for the next DWM frame - never on the GUI
        thread). The excluded toast is never in its own sample."""
        if not (self.isVisible() and self._excluded and self._hwnd):
            self._sample_timer.stop()
            return
        if self._sampling:
            return
        self._sample_gen += 1
        self._sampling = True
        size = (max(1, self.width()), max(1, self.height()))
        threading.Thread(target=self._sample_worker, args=(self._hwnd, size, self._sample_gen),
                         name="reminder-backdrop", daemon=True).start()

    def _sample_worker(self, hwnd, size, gen):
        """Any thread: grab, blur, hand the picture to the GUI thread."""
        img, got = None, False
        try:
            wr = glass.window_rect(hwnd)
            if wr and wr[2] > wr[0] and wr[3] > wr[1]:
                x, y, w, h = wr[0], wr[1], wr[2] - wr[0], wr[3] - wr[1]
                raw = glass.grab_screen(x, y, w, h)
                if raw:
                    img = blur_backdrop(QImage(raw, w, h, w * 4, QImage.Format_RGB32).copy(), *size)
                    got = True
        except Exception:
            logger.debug("reminder backdrop re-sample failed", exc_info=True)
        try:
            self._sampled.emit(img, gen, got)
        except RuntimeError:
            pass                                             # the toast was deleted meanwhile

    def _on_sampled(self, img, gen, got):
        if gen != self._sample_gen:
            return
        self._sampling = False
        if got and self.isVisible():
            self._bd = img                                   # None: hardware video, plain plate
            self.update()

    # ── actions ──
    def _on_record(self, with_assist):
        m = self._meeting
        if m is None or self._upselling:
            return
        self._finish("record")
        self.record.emit(m, bool(with_assist))

    def dismiss(self):
        """"Not this one": close; the meeting stays asked (calendar_service)."""
        m = self._meeting
        if m is None:
            return
        self._finish("dismissed")
        self.dismissed.emit(m)

    def _on_join(self):
        m = self._meeting
        link = join_link(m)
        if not link:
            return
        try:
            QDesktopServices.openUrl(QUrl(link))
        except Exception:
            logger.warning("Couldn't open the call link", exc_info=True)
        self.join.emit(m)

    def _hide_quietly(self):
        self._kbd_focus = False
        self._timer.stop()
        self._sample_timer.stop()
        self._sample_gen += 1                                # a grab on its way is for nobody now
        self._sampling = False
        self.hide()

    def _finish(self, reason):
        m = self._meeting
        was_visible = self.isVisible()
        self._hide_quietly()
        if m is not None and (was_visible or reason in ("record", "dismissed", "expired")):
            self.closed.emit(m, reason)

    def hide_reminder(self):
        """Close without an answer (e.g. a recording started elsewhere)."""
        if self.isVisible():
            self._finish("hidden")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(e)

    def take_focus(self):
        """Let a keyboard user reach the buttons (the toast never activates on
        its own): drop WS_EX_NOACTIVATE, activate, focus Record with its focus
        ring. Activation can land later (the OS), so the focus is given when
        it does - a focus that arrives with the activation itself would not
        count as keyboard focus and would show no ring."""
        if not self.isVisible():
            return
        if _native_windows():
            glass.set_no_activate(self, False)
        self._kbd_focus = True
        self.raise_()
        self.activateWindow()
        if self.isActiveWindow():
            self._give_keyboard_focus()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.ActivationChange and self._kbd_focus and self.isActiveWindow():
            self._give_keyboard_focus()

    def _give_keyboard_focus(self):
        self._kbd_focus = False
        b = self.record_button
        b.setFocus(Qt.TabFocusReason)
        theme.set_prop(b, "focusVisible", b.hasFocus(), children=False)

    # ── painting ──
    def plate_rect(self):
        return QRectF(SH_SIDE + 0.5, SH_TOP + 0.5, self.width() - 2 * SH_SIDE - 1,
                      self.height() - SH_TOP - SH_BOTTOM - 1)

    def _shadow_image(self):
        """0 18px 40px rgba(15,23,42,0.22), squeezed into the margin: the plate's
        silhouette blurred by a down/up-scale (as the Live Assistant card does)."""
        key = (self.width(), self.height())
        if self._shadow_key == key and self._shadow_img is not None:
            return self._shadow_img
        w, h = self.width(), self.height()
        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 23, 42, 92))
        path = QPainterPath()
        path.addRoundedRect(QRectF(SH_SIDE + 3, SH_TOP + 7, w - 2 * SH_SIDE - 6,
                                   h - SH_TOP - SH_BOTTOM - 5), RADIUS, RADIUS)
        p.drawPath(path)
        p.end()
        small = img.scaled(max(1, w // 7), max(1, h // 7), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self._shadow_img = small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self._shadow_key = key
        return self._shadow_img

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        plate = self.plate_rect()
        path = QPainterPath()
        path.addRoundedRect(plate, RADIUS, RADIUS)
        p.drawImage(0, 0, self._shadow_image())

        p.save()
        p.setClipPath(path)
        if self._bd is not None:
            p.drawImage(self.rect(), self._bd)
            tint = QColor(241, 245, 250, 219)                 # rgba(241,245,250,0.86) over the blur
        else:
            tint = QColor(241, 245, 250, 245)                 # no blur behind: nearly solid
        p.fillPath(path, tint)
        glow = QRadialGradient(QPointF(plate.left() + plate.width() * 0.22, plate.top()), 240)
        glow.setColorAt(0.0, QColor(255, 255, 255, 140))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, QBrush(glow))
        sheen = QLinearGradient(plate.topLeft(), QPointF(plate.left(), plate.top() + 70))
        sheen.setColorAt(0.0, QColor(255, 255, 255, 89))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, QBrush(sheen))
        # Inner rim: bright at the top-left, softer at the bottom-right.
        rim = QLinearGradient(plate.topLeft(), plate.bottomRight())
        rim.setColorAt(0.0, QColor(255, 255, 255, 235))
        rim.setColorAt(0.5, QColor(255, 255, 255, 70))
        rim.setColorAt(1.0, QColor(255, 255, 255, 120))
        p.setPen(QPen(QBrush(rim), 3.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.restore()
        p.setPen(QPen(QColor(15, 23, 42, 31), 1))             # rgba(15,23,42,0.12)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()


def blur_backdrop(img, w, h):
    """A screen grab -> the toast-sized frosted backdrop (a 1/12 down/up-scale
    blur), or None when the grab is a black hardware-video hole. Any thread."""
    small = img.scaled(max(1, w // 12), max(1, h // 12), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    if _looks_like_hole(small):
        return None
    return small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def _looks_like_hole(img):
    """A BitBlt of hardware-accelerated video comes back solid black."""
    try:
        w, h = img.width(), img.height()
        if w <= 0 or h <= 0:
            return True
        total, n = 0, 0
        for y in range(0, h, max(1, h // 6)):
            for x in range(0, w, max(1, w // 8)):
                c = img.pixelColor(x, y)
                total += c.red() + c.green() + c.blue()
                n += 1
        return n == 0 or total / (3.0 * n) < 6
    except Exception:
        return False


def is_showing(toast):
    """Helper for AppController._calendar_busy (None-safe)."""
    try:
        return bool(toast is not None and toast.is_showing())
    except RuntimeError:
        return False


__all__ = ["MeetingReminder", "call_name", "meta_line", "when_line", "join_link", "is_showing"]


if __name__ == "__main__":                                   # pragma: no cover - manual check
    import sys
    from datetime import timedelta
    qa = QApplication(sys.argv)
    now = datetime.now(timezone.utc)
    t = MeetingReminder()
    t.show_meeting({"id": "x", "title": "Weekly product sync", "start": now + timedelta(seconds=50),
                    "end": now + timedelta(minutes=30), "call_type": "Google Meet",
                    "people": "Anna, David +2", "link": "https://meet.google.com/abc-defg-hij"})
    sys.exit(qa.exec())
