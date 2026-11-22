"""Small moments of the invite program (mockups InviteClaim / InviteTouches): a confetti burst, a toast, the
sidebar's three progress dots and its glowing "Ready" pill, and the friends' letter avatars.

Everything is painted from the theme's tokens at paint time (ui/theme.py qcolor), so it follows the colour scheme
without being rebuilt; the strong violet fills under white words (pro-btn -> pro-deep) are the same in both looks,
and so are the confetti's Armenian-flag colours (flag-red / flag-blue / flag-orange: the logo's tokens). Motion is
decorative only: with Windows "Animation effects" off (ui/glass.animations_enabled) - or set_motion(False) - the
confetti is skipped, the pill does not pulse and the toast just appears.

    Confetti.burst(host, QPoint(x, y))                  # ~36 pieces for ~1.3 s, then it deletes itself
    show_toast(window, "Mariam counts", "2 more friends for your free month", avatar=("M", "Mariam"), dots=(1, 0))
    InviteDots(parent).set_state(counted, progress)     # filled = counts, ring = on the way
    ReadyPill(parent)                                   # READY, with a slow glow
"""

import logging
import math
import random
import zlib

from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ui import theme
from ui.components import icons

logger = logging.getLogger("transcribe")

_MOTION = {"forced": None}


def set_motion(on):
    """Tests and previews: force motion on (True) or off (False); None follows the system again."""
    _MOTION["forced"] = on


def motion():
    """False when decorative motion should stop (Windows "Animation effects" off)."""
    if _MOTION["forced"] is not None:
        return bool(_MOTION["forced"])
    try:
        from ui import glass
        return bool(glass.animations_enabled())
    except Exception:
        return True


def on_accent():
    """Text and marks on a saturated fill (the violet ticket, the green badge): white in both schemes."""
    return theme.qcolor("on-accent")


def brand_violet(rect, horizontal=True):
    """The brand's strong violet as a fill under white words (the READY pill, the toast's tile, a claimed ticket's
    PRO pill): the same in both looks (pro-btn -> pro-deep, white on it >= 4:1)."""
    end = rect.topRight() if horizontal else rect.bottomRight()
    g = QLinearGradient(rect.topLeft(), end)
    g.setColorAt(0, theme.qcolor("pro-btn"))
    g.setColorAt(1, theme.qcolor("pro-deep"))
    return g


def with_alpha(color, alpha):
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, float(alpha))))
    return c


# ── friends' avatars ─────────────────────────────────────────────────────────
FRIEND_COLOURS = ("pink", "teal", "coral", "violet", "blue")


def friend_colour(name, index=None):
    """A friend's avatar colour (a ui/components/avatars preset): by their place in the list when known (friends
    side by side never share one), else stable for the name."""
    if isinstance(index, int) and index >= 0:
        return FRIEND_COLOURS[index % len(FRIEND_COLOURS)]
    return FRIEND_COLOURS[zlib.crc32(str(name or "").encode("utf-8")) % len(FRIEND_COLOURS)]


def paint_friend(p, rect, initial, name, index=None):
    """A friend's letter avatar in ``rect`` (QRectF); no initial (a friend whose name the server keeps): the neutral
    avatar with the person icon, never a made-up letter."""
    from ui.components import avatars
    if not initial:
        avatars.paint(p, rect, "", guest=True)
        return
    avatars.paint(p, rect, initial[:1], value=friend_colour(name, index))


# ── confetti ─────────────────────────────────────────────────────────────────
CONFETTI_TOKENS = ("flag-red", "flag-blue", "flag-orange", "pro-l", "blue-l", "ok")
CONFETTI_COUNT = 36
CONFETTI_MS = 1300


class Confetti(QWidget):
    """A light overlay over ``host``: CONFETTI_COUNT painted pieces fly out of ``origin`` and fade, driven by one
    QVariantAnimation (0 -> 1 over CONFETTI_MS). Clicks pass through; it deletes itself when done."""

    finished = Signal()

    def __init__(self, host, origin, count=CONFETTI_COUNT, duration=CONFETTI_MS, seed=7):
        super().__init__(host)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAccessibleName("")
        self.setGeometry(host.rect())
        self._origin = QPointF(origin)
        self._t = 0.0
        rnd = random.Random(seed)
        self.pieces = []
        for i in range(count):
            # Mostly up and out (the mockup's fan), a few sideways and down.
            if i % 6 == 5:
                ang = math.radians(rnd.uniform(-20, 200))
            else:
                ang = math.radians(rnd.uniform(-170, -10))
            dist = rnd.uniform(150, 260)
            self.pieces.append({
                "dx": math.cos(ang) * dist, "dy": math.sin(ang) * dist,
                "spin": rnd.choice((-1, 1)) * rnd.uniform(200, 340),
                "w": 6 + (i % 3) * 2, "h": 10 if i % 2 else 6, "round": i % 4 == 0,
                "colour": CONFETTI_TOKENS[i % len(CONFETTI_TOKENS)], "delay": (i % 6) * 0.04,
            })
        self.anim = QVariantAnimation(self)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setDuration(int(duration))
        self.anim.valueChanged.connect(self._tick)
        self.anim.finished.connect(self._done)
        host.installEventFilter(self)

    @classmethod
    def burst(cls, host, origin, **kw):
        """Start a burst over ``host`` at ``origin`` (host coordinates). None (nothing shown) without motion."""
        if host is None or not motion():
            return None
        c = cls(host, origin, **kw)
        c.raise_()
        c.show()
        c.anim.start()
        return c

    def progress(self):
        return self._t

    def running(self):
        return self.anim.state() == QVariantAnimation.Running

    def _tick(self, v):
        self._t = float(v)
        self.update()

    def _done(self):
        self._t = 1.0
        self.hide()
        self.finished.emit()
        self.deleteLater()

    def eventFilter(self, obj, ev):
        if obj is self.parent() and ev.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rims = self._rims()
        for piece in self.pieces:
            t = (self._t - piece["delay"]) / max(0.01, 1.0 - piece["delay"])
            if t <= 0.0:
                continue
            t = min(1.0, t)
            e = 1.0 - (1.0 - t) ** 3                     # ease-out, like cubic-bezier(.15,.75,.35,1)
            x = self._origin.x() + piece["dx"] * e
            y = self._origin.y() + piece["dy"] * e + 60.0 * t * t     # a little gravity at the end
            p.save()
            p.translate(x, y)
            p.rotate(piece["spin"] * e)
            p.setOpacity(max(0.0, 1.0 - t ** 1.6))
            rim = rims.get(piece["colour"])
            p.setPen(QPen(rim, 1.2) if rim is not None else Qt.NoPen)
            p.setBrush(theme.qcolor(piece["colour"]))
            w, h = piece["w"], piece["h"]
            r = QRectF(-w / 2.0, -h / 2.0, w, h)
            if piece["round"]:
                p.drawEllipse(r)
            else:
                p.drawRoundedRect(r, 2, 2)
            p.restore()
        p.end()

    @staticmethod
    def _rims():
        """{colour token: rim colour} for the pieces that would vanish on the window (the flag's deep blue on the
        dark one): they get a thin light edge (confetti-rim; nothing in the light look)."""
        rim = theme.qcolor("confetti-rim")
        if rim.alpha() == 0:
            return {}
        bg = theme.qcolor("bg")
        out = {}
        for name in set(CONFETTI_TOKENS):
            if _contrast(theme.qcolor(name), bg) < 2.0:
                out[name] = rim
        return out


def _luminance(c):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def _contrast(a, b):
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# ── toast ────────────────────────────────────────────────────────────────────
TOAST_MS = 5500


class Toast(QWidget):
    """A small card at the bottom right of a window: a leading avatar or icon tile, a bold line, a muted line and
    optional progress dots. It rises in, stays TOAST_MS, and goes. A click runs ``on_click``."""

    clicked = Signal()

    def __init__(self, host, title, sub="", avatar=None, icon=None, dots=None, on_click=None):
        super().__init__(host)
        self._title, self._sub = str(title or ""), str(sub or "")
        self._avatar, self._icon, self._dots = avatar, icon, dots
        self._on_click = on_click
        self._rise = 1.0
        self.setAccessibleName(("%s. %s" % (self._title, self._sub)) if self._sub else self._title)
        self.setCursor(Qt.PointingHandCursor if on_click else Qt.ArrowCursor)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._ft = theme.font(13.5, 700)
        self._fs = theme.font(12.5, 400)
        w = 12 + 38 + 12 + int(max(QFontMetricsF(self._ft).horizontalAdvance(self._title),
                                   QFontMetricsF(self._fs).horizontalAdvance(self._sub))) + 18
        if dots is not None:
            w += 8 + 29
        self.setFixedSize(min(460, max(240, w)), 62)
        self._dot_widget = None
        if dots is not None:
            self._dot_widget = InviteDots(self)
            self._dot_widget.set_state(*dots, animate=False)
        theme.elevate(self, "lg")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(450)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._on_rise)
        host.installEventFilter(self)

    def title(self):
        return self._title

    def sub(self):
        return self._sub

    def popup(self, stay_ms=TOAST_MS):
        self._place()
        self.raise_()
        self.show()
        if motion():
            self.anim.setStartValue(0.0)
            self.anim.setEndValue(1.0)
            self.anim.start()
        if stay_ms:
            self._timer.start(int(stay_ms))

    def dismiss(self):
        self._timer.stop()
        self.anim.stop()
        self.hide()
        self.deleteLater()

    def _on_rise(self, v):
        self._rise = float(v)
        self._place()

    def _place(self):
        host = self.parentWidget()
        if host is None:
            return
        x = host.width() - self.width() - 28
        y = host.height() - self.height() - 26 + int(round((1.0 - self._rise) * 16))
        self.move(max(0, x), max(0, y))
        if self._dot_widget is not None:
            self._dot_widget.move(self.width() - 16 - self._dot_widget.width(),
                                  (self.height() - self._dot_widget.height()) // 2)

    def eventFilter(self, obj, ev):
        if obj is self.parentWidget() and ev.type() == QEvent.Resize and self.isVisible():
            self._place()
        return False

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
            if callable(self._on_click):
                try:
                    self._on_click()
                except Exception:
                    logger.debug("toast click failed", exc_info=True)
            self.dismiss()
            return
        super().mouseReleaseEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        # A floating card: the popup surface, and a firmer edge in the dark look (the glass edge is lost on it).
        p.setPen(QPen(theme.qcolor("line2" if theme.is_dark() else "glass-line"), 1))
        p.setBrush(theme.qcolor("popup"))
        p.drawRoundedRect(r, 14, 14)
        lead = QRectF(12, (self.height() - 38) / 2.0, 38, 38)
        if self._avatar:
            initial, name = self._avatar
            paint_friend(p, lead, initial, name)
        else:
            tile = QRectF(12, (self.height() - 34) / 2.0, 34, 34)
            p.setPen(Qt.NoPen)
            p.setBrush(brand_violet(tile, horizontal=False))
            p.drawRoundedRect(tile, 10, 10)
            pm = icons.pixmap(self._icon or "sparkle", 18, "on-accent", sw=2)
            p.drawPixmap(int(tile.center().x() - 9), int(tile.center().y() - 9), pm)
        x = 12 + 38 + 12
        right = self.width() - 16 - ((29 + 8) if self._dot_widget is not None else 0)
        p.setFont(self._ft)
        p.setPen(theme.qcolor("ink"))
        fm = QFontMetricsF(self._ft)
        p.drawText(QRectF(x, 11, right - x, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(self._title, Qt.ElideRight, right - x))
        p.setFont(self._fs)
        p.setPen(theme.qcolor("muted"))
        fs = QFontMetricsF(self._fs)
        p.drawText(QRectF(x, 31, right - x, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   fs.elidedText(self._sub, Qt.ElideRight, right - x))
        p.end()


def _toast_host(window):
    host = window.centralWidget() if hasattr(window, "centralWidget") and window.centralWidget() else window
    return host


def show_toast(window, title, sub="", avatar=None, icon=None, dots=None, on_click=None, stay_ms=TOAST_MS):
    """One toast at a time in ``window`` (the Gusan window): a newer one replaces the one on screen."""
    host = _toast_host(window)
    old = getattr(host, "_gusan_toast", None)
    if old is not None:
        try:
            old.dismiss()
        except RuntimeError:
            pass
    t = Toast(host, title, sub, avatar=avatar, icon=icon, dots=dots, on_click=on_click)
    host._gusan_toast = t
    t.popup(stay_ms)
    return t


def current_toast(window):
    host = _toast_host(window)
    t = getattr(host, "_gusan_toast", None)
    try:
        return t if t is not None and t.isVisible() else None
    except RuntimeError:
        return None


# ── sidebar progress ─────────────────────────────────────────────────────────
class InviteDots(QWidget):
    """Three 7 px dots: filled = a friend who counts, a ring = one on the way, grey = nobody yet. A dot that just
    filled pops in (scale 0 -> 1.35 -> 1)."""

    SIZE, GAP = 7, 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._counted, self._progress = 0, 0
        self._pop = {}                          # dot index -> 0..1 of its pop
        self.setFixedSize(3 * self.SIZE + 2 * self.GAP + 4, self.SIZE + 6)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.anim = QVariantAnimation(self)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setDuration(500)
        self.anim.valueChanged.connect(self._on_pop)
        self.anim.finished.connect(self._pop_done)
        self._popping = []

    def state(self):
        return self._counted, self._progress

    def set_state(self, counted, progress=0, animate=True):
        counted = max(0, min(3, int(counted or 0)))
        progress = max(0, min(3 - counted, int(progress or 0)))
        fresh = [i for i in range(self._counted, counted)]
        self._counted, self._progress = counted, progress
        self.setToolTip("%d of 3 friends" % counted)
        self.setAccessibleName("%d of 3 friends" % counted)
        if animate and fresh and motion():
            self._popping = fresh
            self.anim.stop()
            self.anim.start()
        self.update()

    def _on_pop(self, v):
        for i in self._popping:
            self._pop[i] = float(v)
        self.update()

    def _pop_done(self):
        self._pop = {}
        self._popping = []
        self.update()

    @staticmethod
    def _scale(t):
        if t <= 0.0:
            return 0.0
        if t < 0.7:
            return 1.35 * (t / 0.7)
        return 1.35 - 0.35 * ((t - 0.7) / 0.3)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        s, gap = float(self.SIZE), float(self.GAP)
        y = (self.height() - s) / 2.0
        for i in range(3):
            x = 2 + i * (s + gap)
            r = QRectF(x, y, s, s)
            if i < self._counted:
                k = self._scale(self._pop[i]) if i in self._pop else 1.0
                rr = QRectF(r.center().x() - s * k / 2, r.center().y() - s * k / 2, s * k, s * k)
                p.setPen(Qt.NoPen)
                p.setBrush(theme.qcolor("pro-l"))
                p.drawEllipse(rr)
            elif i < self._counted + self._progress:
                p.setPen(QPen(theme.qcolor("pro-l"), 2))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(r.adjusted(1, 1, -1, -1))
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(theme.qcolor("dot-off"))
                p.drawEllipse(r)
        p.end()


class ReadyPill(QWidget):
    """READY: the violet gradient pill with a slow glow (2.4 s), only while it is on screen."""

    def __init__(self, parent=None, text="Ready"):
        super().__init__(parent)
        self._text = text.upper()
        self._font = theme.font(10, 700)
        tw = QFontMetricsF(self._font).horizontalAdvance(self._text)
        self._pad = 6                                    # room for the glow
        self.setFixedSize(int(math.ceil(tw)) + 16 + 2 * self._pad, 16 + 2 * self._pad)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("Ready")
        self._phase = 0.0
        self.anim = QVariantAnimation(self)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setDuration(2400)
        self.anim.setLoopCount(-1)
        self.anim.valueChanged.connect(self._on_phase)

    def text(self):
        return self._text

    def glowing(self):
        return self.anim.state() == QVariantAnimation.Running

    def _on_phase(self, v):
        self._phase = float(v)
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        if motion() and self.anim.state() != QVariantAnimation.Running:
            self.anim.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.anim.stop()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pad = self._pad
        r = QRectF(pad, pad, self.width() - 2 * pad, self.height() - 2 * pad)
        glow = 0.30 + 0.25 * (0.5 - 0.5 * math.cos(self._phase * 2 * math.pi))
        base = theme.qcolor("pro")
        p.setPen(Qt.NoPen)
        for i, a in enumerate((0.10, 0.16, 0.24)):
            grow = (3 - i) * 1.6
            p.setBrush(with_alpha(base, a * glow * 2.0))
            p.drawRoundedRect(r.adjusted(-grow, -grow + 1, grow, grow + 1), 9 + grow, 9 + grow)
        p.setBrush(brand_violet(r))
        p.drawRoundedRect(r, r.height() / 2.0, r.height() / 2.0)
        p.setPen(on_accent())
        p.setFont(self._font)
        p.drawText(r, Qt.AlignCenter, self._text)
        p.end()


__all__ = ["Confetti", "Toast", "show_toast", "current_toast", "InviteDots", "ReadyPill", "motion", "set_motion",
           "friend_colour", "paint_friend", "on_accent", "with_alpha", "brand_violet", "CONFETTI_TOKENS"]
