"""Audio to text: one transcript open (FileResult board), plus the small helpers
the Audio to text page shares with it (emoji, elided labels, dates, a worker).

    view = TranscriptView(main_app)
    view.back.connect(...)                  # "‹ Audio to text"
    view.show_loading(summary)              # header from a list row, body empty
    view.show_doc(doc)                      # a transcript_store.load_transcript() doc

Header: back link, the title, meta (length · speakers · language · real model ·
when), Copy text, Export ▾ (.txt / .md / .srt), Save Word. Body: one white
"paper" with the speaker turns (timestamp · Speaker N · text) that scrolls on
its own and fades out at the bottom while there is more below. The approved
mockup's Summary tab is not in 2.0.0 (implementation plan, section 4).

Every file write runs on a worker thread; a long transcript is laid out in
chunks so the window never freezes. No Qt-free logic lives here: the exports
are transcript_store's.
"""

import datetime
import logging
import math
import os
import threading

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QImage,
                           QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QTextBlockFormat,
                           QTextCharFormat, QTextCursor, QTextOption)
from PySide6.QtWidgets import (QBoxLayout, QFileDialog, QFrame, QGraphicsBlurEffect, QGraphicsScene, QHBoxLayout,
                               QLabel, QMenu, QPushButton, QSizePolicy, QTextBrowser, QVBoxLayout,
                               QWidget)

import docx_export
import engine_choice
import hardware
import transcript_store
from ui import theme
from ui.components import Button, Chip, icons, label

logger = logging.getLogger("transcribe")

# ── emoji (Audio to text only: one per item, BRIEF rev 2 rule 5) ──────────────
EMOJI_FAMILIES = ["Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"]
HEADPHONES = "\U0001F3A7"     # 🎧 drop zone
MEMO = "\U0001F4DD"           # 📝 text out (drop zone)
MIC = "\U0001F3A4"            # 🎤 voice recording (🎙️ is a pale blob in Segoe UI Emoji at row size)
FILM = "\U0001F3AC"           # 🎬 video
PHONE = "\U0001F4DE"          # 📞 call
DOC = "\U0001F4C4"            # 📄 the Word file
KIND_EMOJI = {"voice": MIC, "video": FILM, "call": PHONE}
KIND_LABEL = {"voice": "Voice recording", "video": "Video", "call": "Call"}

# Languages as the Audio to text rail lists them (main.LANG_NAMES; BRIEF rev 2 rule 4).
LANGUAGES = (("auto", "Detect automatically"), ("multi", "Mixed languages"), ("en", "English"),
             ("ar", "Arabic"), ("hy", "Armenian"), ("fr", "French"), ("de", "German"),
             ("ru", "Russian"), ("es", "Spanish"))
LANGUAGE_NAMES = dict(LANGUAGES)

# Speaker colours on the paper: Speaker 1 blue, Speaker 2 green (mockup), then more tokens.
SPEAKER_TONES = ("blue-tx", "ok-tx", "pro-tx", "amber-tx", "red-tx", "text2")

EXPORTS = ("txt", "md", "srt")          # Export ▾ (Word has its own button)

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def emoji_font(px):
    f = QFont()
    f.setFamilies(EMOJI_FAMILIES)
    f.setPixelSize(int(px))
    return f


def emoji_label(ch, px, accessible=None, parent=None):
    """A QLabel holding one colour emoji at ``px`` pixels."""
    lb = QLabel(ch, parent)
    lb.setFont(emoji_font(px))
    lb.setAlignment(Qt.AlignCenter)
    lb.setAccessibleName(accessible or "")
    return lb


def emoji_pixmap(ch, px):
    """The emoji drawn into a pixmap (for a button icon, e.g. 📄 on Save Word)."""
    dpr = 1.0
    try:
        scr = QGuiApplication.primaryScreen()
        dpr = max(1.0, float(scr.devicePixelRatio())) if scr else 1.0
    except Exception:
        pass
    size = int(px) + 4
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setFont(emoji_font(px))
    p.drawText(0, 0, size, size, Qt.AlignCenter, ch)
    p.end()
    return pm


class EmojiTile(QLabel):
    """A rounded square holding one emoji (rows: 40 px on bg2; running card: 44 px on blue-sub)."""

    def __init__(self, ch, accessible, size=40, emoji_px=21, bg="bg2", radius=11, parent=None):
        super().__init__(ch, parent)
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self.setFont(emoji_font(emoji_px))
        self.setAccessibleName(accessible)
        self.setStyleSheet("background: %s; border: none; border-radius: %dpx;"
                           % (theme.qss_color(bg), radius))

    def set_emoji(self, ch, accessible):
        self.setText(ch)
        self.setAccessibleName(accessible)


class ElidedLabel(QLabel):
    """One line that ends in '…' when it doesn't fit; text() is always the full text."""

    def __init__(self, text="", role="body", parent=None, min_width=40):
        super().__init__(parent)
        theme.set_role(self, role)
        self._min_w = min_width
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text):
        text = text or ""
        super().setText(text)
        self.setAccessibleName(text)
        self.update()

    def minimumSizeHint(self):
        sh = super().minimumSizeHint()
        return QSize(min(sh.width(), self._min_w), sh.height())

    def paintEvent(self, _e):
        p = QPainter(self)
        r = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, r.width())
        self.setToolTip(self.text() if text != self.text() else "")
        self.style().drawItemText(p, r, int(self.alignment()) | int(Qt.AlignVCenter), self.palette(),
                                  self.isEnabled(), text, self.foregroundRole())
        p.end()


def sep_label(parent=None, px=12.5):
    """The faint '·' between meta bits."""
    lb = label("·", "faint", parent)
    lb.setFont(theme.font(px))
    lb.setAccessibleName("")
    return lb


def menu_qss():
    """QMenu look from the tokens (the v2 stylesheet has no menu rules)."""
    t = theme.qss_color
    return ("QMenu { background: #ffffff; border: 1px solid %s; border-radius: 8px; padding: 4px; }"
            "QMenu::item { color: %s; padding: 7px 26px 7px 12px; border-radius: 6px; }"
            "QMenu::item:selected { background: %s; color: %s; }"
            "QMenu::item:disabled { color: %s; }"
            "QMenu::separator { height: 1px; background: %s; margin: 4px 6px; }"
            % (t("line2"), t("text"), t("blue-sub"), t("blue-tx"), t("faint"), t("line")))


def make_menu(parent):
    m = QMenu(parent)
    m.setStyleSheet(menu_qss())
    m.setFont(theme.font(13.5))
    return m


# ── soft shadows painted by a parent (no QGraphicsEffect on big cards) ────────
class SoftShadow:
    """theme.SHADOWS[level] around a card, painted by an ANCESTOR from a cached
    nine-slice image. A QGraphicsDropShadowEffect on the card itself re-renders
    and re-blurs the whole card on every repaint inside it: scrolling a long
    transcript was about 3x slower, and hovering a row of a long list repainted
    the whole list. This costs nine pixmap blits, at any size. Like CSS
    box-shadow it paints outside the card only, so a glass card stays as light
    as in the mockup."""

    def __init__(self, level="card", radius=14):
        self.blur, self.dy, self.colour = theme.SHADOWS[level]
        self.radius = radius
        self._cache = {}

    def margins(self):
        """How far the shadow reaches outside the card: (left, top, right, bottom)."""
        b, d = self.blur, self.dy
        return b, max(0, b - d), b, b + d

    def _slices(self):
        """Inner extents where the shadow still changes along an edge: (x, top, bottom)."""
        r, b, d = self.radius, self.blur, self.dy
        return r + b, r + b + d, max(1, r + b - d)

    def _image(self, dpr):
        key = round(float(dpr or 1.0), 2)
        pm = self._cache.get(key)
        if pm is not None:
            return pm
        left, top, right, bottom = self.margins()
        kx, kt, kb = self._slices()
        cw, ch = 2 * kx + 2, kt + kb + 2
        w, h = left + cw + right, top + ch + bottom
        img = QImage(int(math.ceil(w * key)), int(math.ceil(h * key)), QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(key)
        img.fill(Qt.transparent)
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(left, top, cw, ch), self.radius, self.radius)
        scene = QGraphicsScene(0, 0, w, h)
        item = scene.addPath(shape.translated(0, self.dy), QPen(Qt.NoPen), QBrush(theme.qcolor(self.colour)))
        blur = QGraphicsBlurEffect()
        blur.setBlurRadius(self.blur)
        blur.setBlurHints(QGraphicsBlurEffect.QualityHint)
        item.setGraphicsEffect(blur)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        scene.render(p, QRectF(0, 0, w, h), QRectF(0, 0, w, h))
        p.setCompositionMode(QPainter.CompositionMode_Clear)     # outside the card only
        p.fillPath(shape, QColor(0, 0, 0))
        p.end()
        pm = QPixmap.fromImage(img)
        self._cache[key] = pm
        return pm

    def paint(self, painter, rect, dpr=1.0):
        """Paint the shadow of a card whose geometry is ``rect`` (painter coordinates)."""
        r = QRectF(rect)
        if r.width() <= 0 or r.height() <= 0:
            return
        pm = self._image(dpr)
        scale = pm.devicePixelRatio()
        left, top, right, bottom = self.margins()
        kx, kt, kb = self._slices()
        sx = [0, left + kx, left + kx + 2, left + 2 * kx + 2 + right]
        sy = [0, top + kt, top + kt + 2, top + kt + kb + 2 + bottom]
        x1, y1 = r.left() + r.width(), r.top() + r.height()
        dx = [r.left() - left, r.left() + kx, x1 - kx, x1 + right]
        dy = [r.top() - top, r.top() + kt, y1 - kb, y1 + bottom]
        # A card smaller than the corners: split the overlap so nothing paints twice.
        if dx[2] < dx[1]:
            dx[1] = dx[2] = r.left() + r.width() / 2.0
        if dy[2] < dy[1]:
            dy[1] = dy[2] = r.top() + r.height() * kt / float(kt + kb)
        for i in range(3):
            for j in range(3):
                target = QRectF(dx[i], dy[j], dx[i + 1] - dx[i], dy[j + 1] - dy[j])
                if target.width() <= 0 or target.height() <= 0:
                    continue
                source = QRectF(sx[i] * scale, sy[j] * scale,
                                (sx[i + 1] - sx[i]) * scale, (sy[j + 1] - sy[j]) * scale)
                painter.drawPixmap(target, pm, source)


class ShadowPainter(QObject):
    """Paints SoftShadows for some descendant cards of ``host`` (call paint() from
    host.paintEvent) and repaints the host when a card, or a widget between it and
    the host, moves, resizes, shows or hides."""

    _EVENTS = (QEvent.Move, QEvent.Resize, QEvent.Show, QEvent.Hide)

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self._cards = []

    def add(self, card, shadow):
        self._cards.append((card, shadow))
        w = card
        while w is not None and w is not self.host:
            w.installEventFilter(self)
            w = w.parentWidget()
        return card

    def eventFilter(self, obj, ev):
        if ev.type() in self._EVENTS:
            self.host.update()
        return False

    def paint(self, painter):
        dpr = self.host.devicePixelRatioF()
        for card, shadow in self._cards:
            if not card.isVisibleTo(self.host):
                continue
            top_left = card.mapTo(self.host, QPoint(0, 0))
            shadow.paint(painter, QRectF(top_left.x(), top_left.y(), card.width(), card.height()), dpr)


# ── words ─────────────────────────────────────────────────────────────────────
def short_duration(sec):
    """List / running card length: 45 s · 24 min · 1 h 40 min ('' when unknown)."""
    try:
        s = int(round(float(sec or 0)))
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    if s < 60:
        return "%d s" % s
    m = int(round(s / 60.0))
    if m < 60:
        return "%d min" % m
    h, m = divmod(m, 60)
    return "%d h %d min" % (h, m) if m else "%d h" % h


def when_text(created, now=None):
    """'Today 09:12' · 'Yesterday 17:20' · 'Tue 29 Sep' · '29 Sep 2025' ('' when unknown)."""
    if isinstance(created, datetime.datetime):
        dt = created
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(created or ""))
        except ValueError:
            return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone()
        now = (now or datetime.datetime.now().astimezone()).astimezone()
    else:
        now = now or datetime.datetime.now()
        if now.tzinfo is not None:
            now = now.replace(tzinfo=None)
    d, today = dt.date(), now.date()
    hm = "%02d:%02d" % (dt.hour, dt.minute)
    if d == today:
        return "Today " + hm
    if d == today - datetime.timedelta(days=1):
        return "Yesterday " + hm
    if d.year == today.year:
        return "%s %d %s" % (_DAYS[d.weekday()], d.day, _MONTHS[d.month - 1])
    return "%d %s %d" % (d.day, _MONTHS[d.month - 1], d.year)


def language_text(doc):
    code = doc.get("language")
    if code:
        return LANGUAGE_NAMES.get(code, str(code).upper())
    if doc.get("language_setting") == "multi":
        return LANGUAGE_NAMES["multi"]
    return ""


def speakers_text(doc):
    n = int(doc.get("speaker_count") or 0)
    if n <= 0:
        return ""
    return "1 speaker" if n == 1 else "%d speakers" % n


def model_text(model_id):
    """What a transcript was made with, as the pickers name it: 'small' -> 'Whisper Small ·
    Local'; a cloud model's id -> its real name and where it ran ('gemini-2.5-flash' ->
    'Gemini 2.5 Flash · Cloud'); a name nothing knows as it is."""
    if not model_id:
        return ""
    try:
        return engine_choice.model_tagged(model_id)
    except Exception:
        if hardware.info(model_id):
            return hardware.real_name(model_id)
        return engine_choice.model_display_name(model_id)


# ── a worker for disk work (results come back on the GUI thread) ──────────────
def _spawn(fn):
    threading.Thread(target=fn, daemon=True, name="audio-to-text").start()


class Bridge(QObject):
    """Runs closures on the thread this object lives in (the GUI thread)."""
    deliver = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.deliver.connect(self._run)

    def _run(self, fn):
        try:
            fn()
        except Exception:
            logger.warning("Audio to text: a result handler failed", exc_info=True)


_BRIDGE = None


def _bridge():
    """One long-lived Bridge on the GUI thread (made by the first caller, which is
    on it), so a worker never emits on a page that is being deleted."""
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = Bridge()
    return _BRIDGE


def _alive(obj):
    try:
        import shiboken6
        return shiboken6.isValid(obj)
    except Exception:
        return True


def run_bg(owner, work, done=None):
    """work() on a worker thread, then done(result, error) on the GUI thread -
    unless ``owner`` (the widget asking) was deleted meanwhile. Call on the GUI thread."""
    bridge = _bridge()

    def deliver(res, err):
        if _alive(owner):
            done(res, err)

    def job():
        try:
            res, err = work(), None
        except Exception as e:                      # shown inline by the caller
            logger.warning("Audio to text: background work failed: %s", e)
            res, err = None, e
        if done is None:
            return
        try:
            bridge.deliver.emit(lambda: deliver(res, err))
        except RuntimeError:
            pass                                    # the app is quitting
    _spawn(job)


def track_saved(app, fmt):
    """file_transcription_saved {format} through the file job controller."""
    fj = getattr(app, "file_jobs", None)
    fn = getattr(fj, "track_saved", None)
    try:
        if callable(fn):
            fn(fmt)
            return
        tr = getattr(app, "track", None)
        if callable(tr):
            tr("file_transcription_saved", {"format": fmt})
    except Exception:
        logger.debug("track_saved failed", exc_info=True)


def open_path(path):
    """Open a file or folder with the system (QDesktopServices: works on Mac too)."""
    if not path:
        return False
    try:
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
    except Exception:
        logger.debug("open %s failed", path, exc_info=True)
        return False


# ── the view ─────────────────────────────────────────────────────────────────
class BackLink(QPushButton):
    """'‹ Audio to text': 13 px semibold, muted, no box."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setIcon(icons.icon("chev_l", 15, "muted", sw=2))
        self.setIconSize(QSize(15, 15))
        self.setFont(theme.font(13, 600))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAccessibleName("Back to " + text)
        self.setFixedHeight(18)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        theme.focus_ring(self)
        t = theme.qss_color
        self.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid transparent; border-radius: 4px;"
            " color: %s; padding: 0px 2px 0px 0px; text-align: left; }"
            "QPushButton:hover { color: %s; }"
            "QPushButton[focusVisible=\"true\"] { border: 2px solid %s; }"
            % (t("muted"), t("text2"), t("blue-l")))


class _MetaItem(QWidget):
    """Icon (15 px, muted) + text (13 px, text2)."""

    def __init__(self, icon, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        ic = QLabel(self)
        ic.setPixmap(icons.pixmap(icon, 15, "muted"))
        lay.addWidget(ic)
        self.text = label("", "body", self)
        self.text.setFont(theme.font(13))
        self.text.setFixedHeight(20)
        lay.addWidget(self.text)

    def set_text(self, text):
        self.text.setText(text)
        self.setAccessibleName(text)
        self.setVisible(bool(text))


class _Fade(QWidget):
    """The paper's bottom fade: the transcript goes on below."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _e):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0.0, QColor(255, 255, 255, 0))
        g.setColorAt(0.85, QColor(255, 255, 255, 255))
        g.setColorAt(1.0, QColor(255, 255, 255, 255))
        p.fillRect(self.rect(), g)
        p.end()


class Paper(QFrame):
    """The white transcript sheet (radius 14, hairline) with its own scroll. Its
    soft shadow is painted by the TranscriptView (SoftShadow), never by an effect
    on the sheet: that would re-blur the whole sheet on every scroll step."""

    FADE_H = 72

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "whiteCard")
        self.setStyleSheet("QFrame[gusan=\"whiteCard\"] { border-radius: 14px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 6, 4)
        lay.setSpacing(0)
        self.browser = QTextBrowser(self)
        self.browser.setFrameShape(QFrame.NoFrame)
        self.browser.setOpenLinks(False)
        self.browser.setAccessibleName("Transcript")
        self.browser.setFocusPolicy(Qt.TabFocus | Qt.ClickFocus)
        self.browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.browser.setStyleSheet("QTextBrowser { background: transparent; border: none; padding: 0px; }")
        self.browser.viewport().setAutoFillBackground(False)
        doc = self.browser.document()
        doc.setDocumentMargin(0)
        doc.setDefaultFont(theme.font(14))
        root = doc.rootFrame().frameFormat()
        root.setRightMargin(18)
        root.setBottomMargin(26)
        doc.rootFrame().setFrameFormat(root)
        lay.addWidget(self.browser)
        self.fade = _Fade(self.browser.viewport())
        self.browser.viewport().installEventFilter(self)
        bar = self.browser.verticalScrollBar()
        bar.valueChanged.connect(self._update_fade)
        bar.rangeChanged.connect(self._update_fade)
        self._update_fade()

    def eventFilter(self, obj, ev):
        if obj is self.browser.viewport() and ev.type() == ev.Type.Resize:
            self._place_fade()
        return False

    def _place_fade(self):
        vp = self.browser.viewport()
        self.fade.setGeometry(0, max(0, vp.height() - self.FADE_H), vp.width(), self.FADE_H)

    def _update_fade(self, *_a):
        bar = self.browser.verticalScrollBar()
        self._place_fade()
        self.fade.setVisible(bar.maximum() > 0 and bar.value() < bar.maximum())
        self.fade.raise_()


class TranscriptView(QWidget):
    """One transcript: header + paper. ``back`` when the user goes back to the list."""

    back = Signal()
    CHUNK = 160                    # turns laid out per event-loop turn

    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.doc = None
        self.summary = None
        self._gen = 0
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._hide_status)
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(lambda: self.copy_button.setText("Copy text"))
        # Dialogs are attributes so tests (and Wave 4) can replace them.
        self.ask_save_path = self._ask_save_path

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # header (padding 22 32 20, actions bottom-aligned)
        head = QWidget(self)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(32, 22, 32, 20)
        hl.setSpacing(16)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        self.back_link = BackLink("Audio to text", head)
        self.back_link.clicked.connect(lambda _=False: self.back.emit())
        col.addWidget(self.back_link, 0, Qt.AlignLeft)
        self.title_label = ElidedLabel("", "title", head)
        self.title_label.setFixedHeight(34)
        col.addWidget(self.title_label)
        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(8)
        self.meta_duration = _MetaItem("clock", head)
        self.meta_speakers = _MetaItem("people", head)
        self.meta_language = _MetaItem("globe", head)
        self.meta_model = _MetaItem("chip", head)
        self.meta_when = label("", "muted", head)
        self.meta_when.setFont(theme.font(13))
        self.meta_when.setFixedHeight(20)
        self._meta = [self.meta_duration, self.meta_speakers, self.meta_language, self.meta_model,
                      self.meta_when]
        self._seps = []
        for i, w in enumerate(self._meta):
            if i:
                s = sep_label(head, 13)
                self._seps.append(s)
                meta.addWidget(s)
            meta.addWidget(w)
        meta.addStretch(1)
        col.addLayout(meta)
        hl.addLayout(col, 1)
        self._head_lay = hl
        self._meta_lay = meta
        self._stacked = False

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.saved_chip = Chip("Saved", "ok", "check", head)
        self.saved_chip.hide()
        actions.addWidget(self.saved_chip, 0, Qt.AlignVCenter)
        self.error_chip = Chip("Couldn't save", "red", "alert", head)
        self.error_chip.hide()
        actions.addWidget(self.error_chip, 0, Qt.AlignVCenter)
        self.copy_button = Button("Copy text", "secondary", "m", icon="copy", parent=head)
        self.copy_button.clicked.connect(self.copy_text)
        actions.addWidget(self.copy_button)
        chev = QLabel(head)
        chev.setPixmap(icons.pixmap("chev_d", 15, "muted"))
        self.export_button = Button("Export", "secondary", "m", parent=head, trail=chev)
        self.export_button.setAccessibleName("Export")
        self.export_button.clicked.connect(self._show_export_menu)
        actions.addWidget(self.export_button)
        self.export_menu = make_menu(self)
        self.export_actions = {}
        for fmt in EXPORTS:
            act = self.export_menu.addAction(transcript_store.EXPORT_LABELS[fmt])
            act.triggered.connect(lambda _=False, f=fmt: self.export(f))
            self.export_actions[fmt] = act
        self.word_button = Button("Save Word", "primary", "m", parent=head)
        self.word_button.setIcon(QIcon(emoji_pixmap(DOC, 16)))
        self.word_button.setIconSize(QSize(20, 20))
        self.word_button.clicked.connect(self.save_word)
        actions.addWidget(self.word_button)
        hl.addLayout(actions, 0)
        hl.setAlignment(actions, Qt.AlignBottom | Qt.AlignRight)
        self._actions_lay = actions
        root.addWidget(head)

        # body (padding 0 32 28)
        body = QVBoxLayout()
        body.setContentsMargins(32, 0, 32, 28)
        body.setSpacing(14)
        self.paper = Paper(self)
        self.browser = self.paper.browser
        body.addWidget(self.paper, 1)
        root.addLayout(body, 1)
        self.shadows = ShadowPainter(self)
        self.shadows.add(self.paper, SoftShadow("card", radius=14))
        self._set_actions_enabled(False)

    def paintEvent(self, _e):
        p = QPainter(self)
        self.shadows.paint(p)
        p.end()

    # ── header fit: the actions go under the meta when both don't fit on one line ──
    def is_header_stacked(self):
        return self._stacked

    def _fit_header(self):
        self._meta_lay.invalidate()              # fresh hints after texts / chips changed
        self._actions_lay.invalidate()
        m = self._head_lay.contentsMargins()
        room = self.width() - m.left() - m.right()
        need = (self._meta_lay.sizeHint().width() + self._head_lay.spacing()
                + self._actions_lay.sizeHint().width())
        stacked = need > room
        if stacked == self._stacked:
            return
        self._stacked = stacked
        lay = self._head_lay
        lay.setDirection(QBoxLayout.TopToBottom if stacked else QBoxLayout.LeftToRight)
        lay.setSpacing(14 if stacked else 16)
        lay.setAlignment(self._actions_lay, Qt.AlignBottom | (Qt.AlignLeft if stacked else Qt.AlignRight))
        lay.invalidate()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_header()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.back.emit()
            return
        super().keyPressEvent(e)

    # ── content ──
    def _set_actions_enabled(self, on):
        for b in (self.copy_button, self.export_button, self.word_button):
            b.setEnabled(on)

    def _hide_status(self):
        self.saved_chip.hide()
        self.error_chip.hide()
        self._fit_header()

    def meta_texts(self):
        """The visible meta bits, in order (tests, accessibility)."""
        out = [w.text.text() for w in self._meta[:-1] if w.text.text()]
        if self.meta_when.text():
            out.append(self.meta_when.text())
        return out

    def _fill_header(self, d):
        title = d.get("title") or d.get("source_name") or "Transcript"
        self.title_label.setText(title)
        self.meta_duration.set_text(transcript_store.format_duration(d.get("duration_sec"))
                                    if d.get("duration_sec") else "")
        self.meta_speakers.set_text(speakers_text(d) if d.get("speakers", True) else "")
        self.meta_language.set_text(language_text(d))
        self.meta_model.set_text(model_text(d.get("model")))
        self.meta_when.setText(when_text(d.get("created")))
        self.meta_when.setVisible(bool(self.meta_when.text()))
        visible = [bool(w.text.text()) for w in self._meta[:-1]] + [bool(self.meta_when.text())]
        # A separator shows only between two visible bits.
        for i, s in enumerate(self._seps):
            s.setVisible(any(visible[:i + 1]) and visible[i + 1])
        self.setAccessibleName(title)
        self._fit_header()

    def show_loading(self, summary=None):
        """Header from a list row while the transcript loads."""
        self._gen += 1
        self.doc = None
        self.summary = dict(summary or {})
        self._hide_status()
        self.copy_button.setText("Copy text")
        self._fill_header(self.summary)
        self.browser.clear()
        self._set_actions_enabled(False)

    def show_doc(self, doc):
        """Show a loaded transcript (transcript_store.load_transcript)."""
        self._gen += 1
        gen = self._gen
        self.doc = doc
        self.summary = {k: v for k, v in doc.items() if k not in ("segments", "paragraphs")}
        self._hide_status()
        self.copy_button.setText("Copy text")
        self._fill_header(doc)
        self.browser.clear()
        self.browser.verticalScrollBar().setValue(0)
        paragraphs = list(doc.get("paragraphs") or [])
        if not paragraphs:
            cur = QTextCursor(self.browser.document())
            fmt = QTextCharFormat()
            fmt.setFont(theme.font(14))
            fmt.setForeground(theme.qcolor("muted"))
            cur.insertText("No speech in this file.", fmt)
        else:
            self._layout = self._turn_layout(doc, paragraphs)
            self._fill(gen, paragraphs, 0)
        self._set_actions_enabled(True)

    def _turn_layout(self, doc, paragraphs):
        show_ts = bool(doc.get("timestamps", True))
        show_spk = any(p.get("speaker") is not None for p in paragraphs)
        stops, x = [], 0
        if show_ts:
            x += 48 + 16
            stops.append(x)
        if show_spk:
            x += 84 + 16
            stops.append(x)
        bf = QTextBlockFormat()
        bf.setLeftMargin(x)
        bf.setTextIndent(-x)
        bf.setBottomMargin(10)
        bf.setLineHeight(22, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        bf.setTabPositions([QTextOption.Tab(s, QTextOption.LeftTab) for s in stops])
        f_ts = QTextCharFormat()
        f_ts.setFont(theme.font(12))
        f_ts.setForeground(theme.qcolor("muted"))
        f_tx = QTextCharFormat()
        f_tx.setFont(theme.font(14))
        f_tx.setForeground(theme.qcolor("text"))
        spk_fmts = {}
        for i, tone in enumerate(SPEAKER_TONES):
            f = QTextCharFormat()
            f.setFont(theme.font(13, 700))
            f.setForeground(theme.qcolor(tone))
            spk_fmts[i] = f
        return {"ts": show_ts, "spk": show_spk, "block": bf, "f_ts": f_ts, "f_tx": f_tx,
                "f_spk": spk_fmts, "names": doc.get("speaker_names") or {}}

    def _fill(self, gen, paragraphs, start):
        if gen != self._gen:
            return                       # another transcript opened meanwhile
        lay = self._layout
        doc = self.browser.document()
        cur = QTextCursor(doc)
        cur.movePosition(QTextCursor.End)
        cur.beginEditBlock()
        end = min(len(paragraphs), start + (self.CHUNK if start else self.CHUNK // 2))
        for i in range(start, end):
            p = paragraphs[i]
            if i == 0:
                cur.setBlockFormat(lay["block"])
            else:
                cur.insertBlock(lay["block"])
            if lay["ts"]:
                cur.insertText(docx_export.format_timestamp(p.get("start", 0)), lay["f_ts"])
                cur.insertText("\t", lay["f_ts"])
            if lay["spk"]:
                spk = p.get("speaker")
                f = lay["f_spk"][(int(spk) if spk is not None else 0) % len(SPEAKER_TONES)]
                cur.insertText(docx_export.speaker_name(spk, lay["names"]), f)
                cur.insertText("\t", f)
            cur.insertText(str(p.get("text") or ""), lay["f_tx"])
        cur.endEditBlock()
        if end < len(paragraphs):
            # Tied to this view: a view deleted mid-layout never gets the call.
            QTimer.singleShot(0, self, lambda: self._fill(gen, paragraphs, end))
        else:
            self.paper._update_fade()

    def plain_text(self):
        return transcript_store.plain_text(self.doc) if self.doc else ""

    # ── actions ──
    def copy_text(self):
        if not self.doc:
            return
        try:
            QGuiApplication.clipboard().setText(self.plain_text())
        except Exception:
            logger.debug("copy failed", exc_info=True)
            return
        self.copy_button.setText("Copied")
        self._copy_timer.start(1600)
        track_saved(self.app, "clipboard")

    def _show_export_menu(self):
        if self.doc:
            self.export_menu.popup(self.export_button.mapToGlobal(self.export_button.rect().bottomLeft()))

    def _default_dir(self):
        src = (self.doc or {}).get("source_path") or ""
        folder = os.path.dirname(src)
        if folder and os.path.isdir(folder):
            return folder
        return str(transcript_store.documents_dir())

    def _ask_save_path(self, caption, suggested, filt):
        path, _ = QFileDialog.getSaveFileName(self, caption, suggested, filt)
        return path

    def export(self, fmt):
        """Export ▾ .txt / .md / .srt: ask where, write on a worker, say Saved."""
        if not self.doc or fmt not in EXPORTS:
            return
        label_text = transcript_store.EXPORT_LABELS[fmt]
        suggested = os.path.join(self._default_dir(), transcript_store.suggested_name(self.doc, fmt))
        path = self.ask_save_path("Export transcript", suggested, "%s (*.%s)" % (label_text, fmt))
        if not path:
            return
        doc = self.doc
        run_bg(self, lambda: transcript_store.export_to(doc, fmt, path),
               lambda res, err: self._saved(fmt, res, err))

    def save_word(self):
        """Save Word: ask where (next to the audio by default), write the .docx on a worker."""
        if not self.doc:
            return
        suggested = os.path.join(self._default_dir(), transcript_store.suggested_name(self.doc, "docx"))
        path = self.ask_save_path("Save Word document", suggested, "Word document (*.docx)")
        if not path:
            return
        doc = self.doc
        tid = doc.get("id")

        def work():
            out = transcript_store.export_to(doc, "docx", path)
            if tid:
                transcript_store.update_meta(tid, word_path=str(out))
            return out

        run_bg(self, work, lambda res, err: self._saved("docx", res, err))

    def _saved(self, fmt, path, err):
        self._hide_status()
        if err is not None or not path:
            self.error_chip.setToolTip(str(err or ""))
            self.error_chip.show()
        else:
            self.saved_chip.setToolTip(str(path))
            self.saved_chip.show()
            if fmt == "docx" and self.doc is not None:
                self.doc["word_path"] = str(path)
            track_saved(self.app, fmt)
        self._fit_header()
        self._status_timer.start(4000)
