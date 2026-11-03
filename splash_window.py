"""The loading card (runs only in the splash process - see startup_splash.py).

A small frosted-glass card with the Gusan logo, in the look of the dictation pill
(the same plate, hairline, inner highlight and soft shadow), that opens while the
app starts: the logo pops in, soft rings travel out of it the way a voice does,
and five small bars breathe under the name. No sentences, no percentages: the
start has no real progress to show, so the card only says "Gusan is opening".

It follows the app's one light look from FIXED tokens and a QPainter - no
widgets, no palette, no style - so a dark Windows cannot change it. It sits on
the primary screen (where the app's window opens) and needs nothing from DPI
handling beyond Qt's own per-monitor scaling. Never in the way: no taskbar
button, no focus, clicks fall through it, and after TOPMOST_S it leaves the
always-on-top band, so a dialog of a start that failed is never covered by it.

Only PySide6.QtCore / QtGui and app_info (the logo's path) are used (ctypes only
for that step down, after TOPMOST_S), so the card process starts fast. The drawing is a plain function of time (SplashArt.paint),
which is what the tests and the preview sheet render.
"""
import math
import sys
import time

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath,
                           QPen, QRadialGradient, QRasterWindow, QSurfaceFormat)

from app_info import APP_NAME, resource_path
from startup_splash import FADE, FADE_S, QUIT

# ── The light look: ui/theme.py TOKENS, value for value (a test keeps them equal) ──
INK = (15, 23, 42)                  # TOKENS["ink"]
MUTED = (100, 116, 139)             # TOKENS["muted"]
BLUE = (37, 99, 235)                # TOKENS["blue"]
BLUE_L = (59, 130, 246)             # TOKENS["blue-l"]
GLASS_TOP = (255, 255, 255, 245)    # the glass plate: TOKENS glass-a / glass-b, denser (nothing blurred behind it)
GLASS_BOTTOM = (241, 245, 249, 238)
HAIRLINE = (15, 23, 42, 38)         # the dictation pill's light-glass hairline
EDGE = (230, 90)                    # ...and its inner highlight: white alpha at the top, at the bottom

# ── Shape, in logical pixels ──────────────────────────────────────────────────
WIN = 360                           # the window: the card and room for its shadow
CARD = (48, 48, 264, 264)           # x, y, w, h - the card's centre is the window's centre
RADIUS = 46.0
LOGO_D = 88.0                       # the logo's diameter
LOGO_CY = 139.0                     # ...its centre, from the window's top (the card spans 48..312)
WORD_CY = 219.0
BARS_CY = 255.0
RING_FROM, RING_TO = 52.0, 88.0     # the rings' radius range: inside the card (its top edge is 91 above the logo)
SHADOW_LAYERS, SHADOW_DY, SHADOW_A = 26, 10, 3
MIN_SHOWN_S = 0.6                   # a card that appeared is seen for a whole beat (intro 0.30 s, logo 0.55 s,
                                    # name 0.65 s), never as a half-formed blink: an early "window is up" waits
TOPMOST_S = 15.0                    # after this long the card leaves the always-on-top band (see step_down)
FRAME_MS = 33                       # 30 fps: the motion is slow, and this halves the card's CPU
STILL_BARS = (0.30, 0.65, 1.0, 0.65, 0.30)   # the bars' heights when nothing moves


def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def ease_out_cubic(x):
    return 1.0 - (1.0 - x) ** 3


def ease_out_quad(x):
    return 1.0 - (1.0 - x) ** 2


def smoothstep(x):
    """0 -> 1 with no slope at either end: the way out dissolves, it never ends in a step."""
    return x * x * (3.0 - 2.0 * x)


def ease_out_back(x):
    """A soft overshoot: 0 -> 1 with a 6% bounce past it."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    c1 = 1.2
    c3 = c1 + 1.0
    return 1.0 + c3 * (x - 1.0) ** 3 + c1 * (x - 1.0) ** 2


def _color(rgb, alpha=None):
    a = rgb[3] if len(rgb) > 3 else 255
    if alpha is not None:
        a = int(round(255 * clamp(alpha)))
    return QColor(rgb[0], rgb[1], rgb[2], a)


class SplashArt:
    """The card's drawing. ``paint`` is a pure function of the time since the card
    appeared and of how far its way out has gone; the glass layer and the logo are
    made once per pixel ratio."""

    INTRO_S = 0.30
    POP_S = 0.42
    PERIOD_S = 2.4                  # one breath, and the time a ring takes to leave
    RING_EVERY_S = 0.8
    FIRST_RING_S = 0.45
    STILL_IN_S = 0.18               # the card that does not move: its fade in, after which the picture never changes

    def __init__(self, motion=True):
        # motion=False: Windows' "Animation effects" is off (Settings > Accessibility > Visual
        # effects, which the dictation pill honours too) - the same card, standing still.
        self.motion = bool(motion)
        self._dpr = None
        self._static = None
        self._logo = None
        self._font = None

    # -- made once -------------------------------------------------------------
    def prepare(self, dpr):
        """Make the layers for this pixel ratio (the first paint does it otherwise)."""
        if self._dpr == dpr and self._static is not None:
            return
        self._dpr = dpr
        self._static = self._glass(dpr)
        side = int(math.ceil(LOGO_D * dpr))
        logo = QImage(resource_path("assets", "icon_512.png"))
        if not logo.isNull():
            logo = logo.scaled(side, side, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            logo.setDevicePixelRatio(dpr)
        self._logo = logo
        font = QFont()
        font.setFamilies(["Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI", "Helvetica Neue", "Arial"])
        font.setPixelSize(27)
        font.setWeight(QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 0.5)
        self._font = font
        # The first text a process draws sets up the font database, the font engine and the glyphs
        # (hundreds of ms): paid here, before the clock starts, not in the middle of the intro.
        warm = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
        warm.setDevicePixelRatio(dpr)
        q = QPainter(warm)
        q.setRenderHint(QPainter.TextAntialiasing, True)
        self._draw_name(q, 0.0)
        q.end()

    @staticmethod
    def _glass(dpr):
        """The shadow, the plate, its hairline and its inner highlight: one
        device-pixel image, drawn once."""
        side = int(math.ceil(WIN * dpr))
        img = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        q = QPainter(img)
        q.setRenderHint(QPainter.Antialiasing, True)
        card = QRectF(*CARD)
        q.setPen(Qt.NoPen)
        for i in range(SHADOW_LAYERS, 0, -1):                 # stacked outlines: a soft, even falloff
            q.setBrush(QColor(INK[0], INK[1], INK[2], SHADOW_A))
            q.drawRoundedRect(card.adjusted(-i, -i + SHADOW_DY, i, i + SHADOW_DY), RADIUS + i, RADIUS + i)
        path = QPainterPath()
        path.addRoundedRect(card, RADIUS, RADIUS)
        q.setCompositionMode(QPainter.CompositionMode_Clear)      # no shadow under the plate: its colours are the tokens'
        q.fillPath(path, Qt.black)
        q.setCompositionMode(QPainter.CompositionMode_SourceOver)
        body = QLinearGradient(card.topLeft(), card.bottomLeft())
        body.setColorAt(0.0, _color(GLASS_TOP))
        body.setColorAt(1.0, _color(GLASS_BOTTOM))
        q.fillPath(path, body)
        q.setBrush(Qt.NoBrush)
        q.setPen(QPen(_color(HAIRLINE), 1.0))
        hair = QPainterPath()
        hair.addRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS - 0.5, RADIUS - 0.5)
        q.drawPath(hair)
        edge = QLinearGradient(card.topLeft(), card.bottomLeft())
        edge.setColorAt(0.0, QColor(255, 255, 255, EDGE[0]))
        edge.setColorAt(1.0, QColor(255, 255, 255, EDGE[1]))
        q.setPen(QPen(edge, 1.0))
        inner = QPainterPath()
        inner.addRoundedRect(card.adjusted(1.5, 1.5, -1.5, -1.5), RADIUS - 1.5, RADIUS - 1.5)
        q.drawPath(inner)
        q.end()
        return img

    # -- every frame -----------------------------------------------------------
    def paint(self, p, dpr, t, out=0.0):
        """Draw the card ``t`` seconds after it appeared; ``out`` (0..1) is how
        far its way out has gone. ``p`` paints in window pixels (0..WIN)."""
        self.prepare(dpr)
        if self.motion:
            shown = ease_out_cubic(clamp(t / self.INTRO_S)) * (1.0 - smoothstep(clamp(out)))
            pop = ease_out_back(clamp(t / self.POP_S))
            scale = (1.0 - 0.06 * (1.0 - pop)) * (1.0 + 0.03 * ease_out_cubic(clamp(out)))
        else:                                   # a plain fade in and out
            shown = clamp(t / self.STILL_IN_S) * (1.0 - smoothstep(clamp(out)))
            scale = 1.0
        if shown <= 0.002:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        centre = QPointF(WIN / 2.0, WIN / 2.0)
        if abs(scale - 1.0) > 0.0005:
            p.translate(centre)
            p.scale(scale, scale)
            p.translate(-centre)
        p.setOpacity(shown)
        p.drawImage(QPointF(0.0, 0.0), self._static)
        self._content(p, t, shown)
        p.restore()

    def _draw_name(self, p, top):
        p.setFont(self._font)
        p.setPen(_color(INK))
        p.drawText(QRectF(0.0, top, WIN, 40.0), Qt.AlignCenter, APP_NAME)

    def _content(self, p, t, shown):
        cx = WIN / 2.0
        still = not self.motion
        breath = 0.5 if still else 0.5 - 0.5 * math.cos(2.0 * math.pi * max(0.0, t - self.FIRST_RING_S) / self.PERIOD_S)
        # a soft blue glow behind the logo, breathing with it
        glow_in = 1.0 if still else clamp((t - 0.20) / 0.40)
        glow = QRadialGradient(QPointF(cx, LOGO_CY), 104.0)
        glow.setColorAt(0.0, _color(BLUE_L, (0.10 + 0.08 * breath) * glow_in))
        glow.setColorAt(0.55, _color(BLUE_L, (0.04 + 0.03 * breath) * glow_in))
        glow.setColorAt(1.0, _color(BLUE_L, 0.0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx, LOGO_CY), 104.0, 104.0)
        # rings leave the logo one after another, like a voice
        born = -1.0 if still else (t - self.FIRST_RING_S) / self.RING_EVERY_S
        for k in range(int(math.floor(born)), int(math.floor(born)) - 4, -1):
            if k < 0:
                break
            age = (t - self.FIRST_RING_S - k * self.RING_EVERY_S) / self.PERIOD_S
            if not 0.0 <= age < 1.0:
                continue
            r = RING_FROM + (RING_TO - RING_FROM) * ease_out_quad(age)
            a = 0.30 * (1.0 - age) ** 1.4 * ease_out_cubic(clamp(age / 0.18))     # born from nothing, not in one frame
            ring = QRadialGradient(QPointF(cx, LOGO_CY), r)
            ring.setColorAt(max(0.0, (r - 20.0) / r), _color(BLUE_L, 0.0))
            ring.setColorAt(max(0.0, (r - 3.0) / r), _color(BLUE_L, a))
            ring.setColorAt(1.0, _color(BLUE_L, 0.0))
            p.setBrush(ring)
            p.drawEllipse(QPointF(cx, LOGO_CY), r, r)
        # the logo: a pop, then a slow breath; a soft shadow under it
        logo_in = 1.0 if still else clamp((t - 0.10) / 0.45)
        if logo_in > 0.0:
            s = 1.0 if still else ease_out_back(logo_in) * (1.0 + 0.022 * breath)
            p.save()
            p.setOpacity(p.opacity() * clamp(logo_in * 2.5))
            lift = QRadialGradient(QPointF(cx, LOGO_CY + 7.0), LOGO_D * 0.62 * s)
            lift.setColorAt(0.0, _color(INK, 0.20))
            lift.setColorAt(0.70, _color(INK, 0.10))
            lift.setColorAt(1.0, _color(INK, 0.0))
            p.setPen(Qt.NoPen)
            p.setBrush(lift)
            p.drawEllipse(QPointF(cx, LOGO_CY + 7.0), LOGO_D * 0.62 * s, LOGO_D * 0.62 * s)
            p.translate(cx, LOGO_CY)
            p.scale(s, s)
            if not self._logo.isNull():
                p.drawImage(QRectF(-LOGO_D / 2, -LOGO_D / 2, LOGO_D, LOGO_D), self._logo)
            p.restore()
        # the name
        word_in = 1.0 if still else clamp((t - 0.30) / 0.35)
        if word_in > 0.0:
            p.save()
            p.setOpacity(p.opacity() * ease_out_cubic(word_in))
            self._draw_name(p, WORD_CY - 20.0 + 6.0 * (1.0 - ease_out_cubic(word_in)))
            p.restore()
        # five bars: a wave that travels along them
        bars_in = 1.0 if still else clamp((t - 0.45) / 0.35)
        if bars_in > 0.0:
            p.save()
            p.setOpacity(p.opacity() * ease_out_cubic(bars_in))
            p.setPen(Qt.NoPen)
            width, gap = 5.0, 7.0
            x0 = cx - (5 * width + 4 * gap) / 2.0
            for i in range(5):
                h = 7.0 + 12.0 * (STILL_BARS[i] if still else 0.5 + 0.5 * math.sin(2.0 * math.pi * t / 1.15 - i * 0.85))
                p.setBrush(_color(BLUE, 0.92 - 0.10 * abs(i - 2)))
                p.drawRoundedRect(QRectF(x0 + i * (width + gap), BARS_CY - h / 2.0, width, h), width / 2.0, width / 2.0)
            p.restore()


def render(art, t, out=0.0, dpr=1.0, background=None):
    """One frame as an image (window pixels x dpr); transparent unless a
    ``background`` colour is given. For the tests and the preview sheet."""
    side = int(math.ceil(WIN * dpr))
    img = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(background if background is not None else Qt.transparent)
    p = QPainter(img)
    art.paint(p, dpr, t, out)
    p.end()
    return img


class SplashWindow(QRasterWindow):
    """The frameless, translucent, always-on-top window the card is drawn in."""

    def __init__(self, art=None, clock=time.monotonic):
        super().__init__()
        self.art = art or SplashArt()
        self.clock = clock
        self.t0 = None              # when it appeared
        self.out0 = None            # when its way out began
        self._painted = False
        fmt = QSurfaceFormat()
        fmt.setAlphaBufferSize(8)
        self.setFormat(fmt)
        # The flags of the app's own floating windows (the pill, the reminder), which are known
        # to give no taskbar button and take no focus on Windows, plus clicks falling through.
        self.setFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                      | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setTitle(APP_NAME)
        self.resize(WIN, WIN)

    def place(self, screen=None):
        """Centre on the primary screen's work area: where the app's own window opens."""
        screen = screen or QGuiApplication.primaryScreen()
        if screen is None:
            return
        r = QRect(0, 0, WIN, WIN)
        r.moveCenter(screen.availableGeometry().center())
        self.setGeometry(r)

    def appear(self):
        self.place()
        self.art.prepare(self.devicePixelRatio())      # layers and text engine built before the clock starts: the intro never stutters
        self.t0 = self.clock()                         # re-stamped when the first frame is really painted
        self.show()

    def step_down(self):
        """Leave the always-on-top band (to the bottom of the z-order, still visible on a bare
        desktop) so a dialog of the app - e.g. the bootloader's "Unhandled exception" box - is
        never covered. HWND_BOTTOM takes the topmost style away; HWND_NOTOPMOST would leave the
        card above every window that is not topmost, such a box included."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            u.SetWindowPos(int(self.winId()), 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # NOSIZE | NOMOVE | NOACTIVATE
        except Exception:
            pass

    def begin_fade(self):
        if self.out0 is None:
            self.out0 = self.clock()

    @property
    def fading(self):
        return self.out0 is not None

    def faded_out(self):
        return self.out0 is not None and self.clock() - self.out0 >= FADE_S

    def paintEvent(self, event):
        if self.t0 is not None and not self._painted:
            self._painted = True
            self.t0 = self.clock()          # the intro starts when the first frame is really drawn (show() takes time)
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)      # a translucent window starts from nothing
        p.fillRect(QRect(0, 0, self.width(), self.height()), QColor(0, 0, 0, 0))
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if self.t0 is not None:
            now = self.clock()
            out = 0.0 if self.out0 is None else clamp((now - self.out0) / FADE_S)
            self.art.paint(p, self.devicePixelRatio(), now - self.t0, out)
        p.end()


def make_step(app, win, life, show_at=None, motion=True):
    """The card's tick: what to do now, from the lifecycle's verdict and the window's own clock."""
    flags = {"stepped": False, "settled": False}

    def step():
        verdict = life.poll()
        if verdict == QUIT:
            app.quit()
            return
        shown = win.t0 is not None
        if verdict == FADE:
            if not shown:
                app.quit()                  # the app was ready before the card showed: no flash
                return
            if win.clock() - win.t0 >= MIN_SHOWN_S:
                win.begin_fade()            # younger than that it keeps painting: the verdict is sticky, so the fade starts on a later tick
        if win.faded_out():
            app.quit()
            return
        if not shown:
            if show_at is not None and time.time() < show_at:
                return
            win.appear()
        age = win.clock() - win.t0
        if not flags["stepped"] and age >= TOPMOST_S:
            flags["stepped"] = True
            win.step_down()
        if motion or win.fading or not flags["settled"]:
            win.update()
            if not motion and not win.fading and age >= SplashArt.STILL_IN_S:
                flags["settled"] = True     # this paint is at full opacity: the still card is final until its way out
    return step


def run(life, show_at=None, motion=True):
    """The card process's event loop. ``life`` (startup_splash.Lifecycle) says when
    to stop; ``show_at`` is the earliest wall-clock time to appear (a launch that
    ends at once shows nothing); ``motion`` False draws the card without movement."""
    app = QGuiApplication([sys.argv[0] if sys.argv else "gusan-splash"])
    win = SplashWindow(SplashArt(motion))
    timer = QTimer()
    timer.setTimerType(Qt.PreciseTimer)
    timer.setInterval(FRAME_MS)
    step = make_step(app, win, life, show_at, motion)

    def tick():
        try:
            step()
        except Exception:
            app.quit()                      # a card that cannot keep going must not stay up

    timer.timeout.connect(tick)
    timer.start()
    code = app.exec()
    win.close()
    return code
