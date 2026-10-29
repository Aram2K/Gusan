"""Draws the pictures the Windows installer shows (installer/installer.iss).

  installer/wizard/slide<N>_<F>.png   the slideshow on the Installing page, 1000x400 (a 500x200 picture at 2x)
  installer/wizard/finish_<W>x<H>.png the big left picture of the last page, in every size Inno Setup asks for
  installer/wizard/small_<S>.png      the small square top-right picture of every inner page (transparent)
  installer/wizard/slides.json        slide titles, lines and the frame list with how long each one stays

Nothing here runs in CI and nothing at install time: run it by hand after a design change and commit the output.

    venv\\Scripts\\python.exe installer\\make_wizard_art.py            # everything
    venv\\Scripts\\python.exe installer\\make_wizard_art.py --only slides 3 5

Screens are painted with the app's own helpers (ui.theme tokens and fonts, the dictation bar, the Live Assistant
card, the icons), at 4x and then halved with Lanczos, so text stays crisp at 100% and at 200% screen scale.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "installer" / "wizard"

# Qt has to be told how to run before it is imported: no window, the Windows fonts, no keyring, a scratch data
# folder (the app code we import would otherwise look at the real one). QT_SCALE_FACTOR makes the icons come
# out 4x sharp (ui.components.icons draws at the screen's pixel ratio).
os.environ["QT_QPA_PLATFORM"] = "offscreen"
if os.path.isdir(r"C:\Windows\Fonts"):
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["QT_SCALE_FACTOR"] = "4"
os.environ["TRANSCRIBE_APP_DATA_DIR"] = tempfile.mkdtemp(prefix="gusan-wizard-art-")
os.environ["TRANSCRIBE_DISABLE_KEYRING"] = "1"
os.environ["TRANSCRIBE_SKIP_MIGRATION"] = "1"
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (QBrush, QColor, QFontMetricsF, QImage, QLinearGradient, QPainter,  # noqa: E402
                           QPainterPath, QPen, QRadialGradient)
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import theme  # noqa: E402

app = QApplication.instance() or QApplication([])
theme.install_light_scheme(app)
theme.load_fonts()

from ui.components import icons  # noqa: E402
from ui.onboarding_v2 import PILL_DONE, PILL_HAIR, PILL_TINT, WAVE_HEIGHTS, ink, paint_pill  # noqa: E402
from ui.pages import live_demo  # noqa: E402

q = theme.qcolor
SS = 4                       # painted at 4x of the logical size ...
SLIDE_W, SLIDE_H = 500, 200  # ... the slides are 500x200 logical, saved at 2x = 1000x400
SLIDE_OUT = 2

# ── what Inno Setup asks for ──────────────────────────────────────────────────
# WizardImageFile (the left picture, 164:314) by screen scale 100 .. 250 %, and WizardSmallImageFile (square).
# The small image sizes of the docs are 58 .. 159; 70 covers the 120% wizard size of Inno 6.6 on a 100% screen.
FINISH_SIZES = ((202, 386), (269, 515), (336, 643), (403, 772), (430, 824), (498, 953), (534, 1022))
SMALL_SIZES = (58, 70, 77, 97, 116, 124, 143, 159)

# ── the slideshow: title, one plain line, the accent colour (text colour) of each tool, and for how many ms each
# frame stays. Every frame time is a multiple of 100 ms (the timer tick) and a slide adds up to 6000 ms, so the
# loop of five is 30 s.
SLIDES = {
    1: dict(eyebrow="Dictation", icon="mic", title="Type with\nyour voice",
            line="Press a shortcut, talk, and it types anywhere.",
            accent=("#2563eb", "#1d4ed8"), ms=(600, 600, 700, 700, 700, 2700)),
    2: dict(eyebrow="Smart Actions", icon="wand", title="Ask for\nfinished text",
            line="Say what you need. The finished text is pasted.",
            accent=("#9333ea", "#7e22ce"), ms=(600, 600, 900, 900, 900, 2100)),
    3: dict(eyebrow="Meetings", icon="people", title="Meeting notes,\nno bot",
            line="Record a call. Get the summary and action items.",
            accent=("#ea580c", "#c2410c"), ms=(800, 900, 1000, 900, 1100, 1300)),
    4: dict(eyebrow="Live Assistant", icon="incognito", title="Help during\nyour calls",
            line="Answers while you talk, hidden from most screen shares.",
            accent=("#16a34a", "#15803d"), ms=(700, 800, 1000, 1500, 2000)),
    5: dict(eyebrow="Audio to text", icon="wave", title="Files become\ntranscripts",
            line="Drop a recording, get a transcript with speakers.",
            accent=("#3b82f6", "#1d4ed8"), ms=(800, 700, 1100, 800, 1500, 1100)),
}
BUDGET_BYTES = 4 * 1024 * 1024


# ── painting basics ───────────────────────────────────────────────────────────
class Canvas:
    """A QPainter on a QImage that takes logical units: ``w`` x ``h`` painted at ``scale``."""

    def __init__(self, w, h, scale=SS, alpha=False):
        self.w, self.h, self.scale, self.alpha = w, h, scale, alpha
        self.img = QImage(int(round(w * scale)), int(round(h * scale)),
                          QImage.Format_ARGB32_Premultiplied if alpha else QImage.Format_RGB32)
        self.img.fill(Qt.transparent if alpha else QColor("#ffffff"))
        self.p = QPainter(self.img)
        for hint in (QPainter.Antialiasing, QPainter.TextAntialiasing, QPainter.SmoothPixmapTransform):
            self.p.setRenderHint(hint, True)
        self.p.scale(scale, scale)

    def pil(self):
        self.p.end()
        img = self.img.convertToFormat(QImage.Format_RGBA8888)
        w, h = img.width(), img.height()
        assert img.bytesPerLine() == w * 4
        return Image.frombytes("RGBA", (w, h), bytes(img.constBits())[: w * h * 4])


def shadow(p, rect, radius, a=18, spread=9, dy=5, color=(15, 23, 42)):
    """A soft drop shadow made of stacked translucent rounded rects (ui.onboarding_v2._shadow_rects)."""
    base = QColor(*color, a)
    a0 = base.alphaF()
    steps = 5
    p.setPen(Qt.NoPen)
    for i in range(steps, 0, -1):
        f = i / float(steps)
        c = QColor(base)
        c.setAlphaF(a0 * (1.0 - f) * 0.5)
        grow = spread * f
        p.setBrush(c)
        p.drawRoundedRect(QRectF(rect).adjusted(-grow, -grow + dy, grow, grow + dy), radius + grow, radius + grow)


def corner_path(r, tl, tr, br, bl):
    """A rounded rect with its own (rx, ry) per corner (ui.onboarding_v2._corner_path)."""
    path = QPainterPath()
    path.moveTo(r.left() + tl[0], r.top())
    path.lineTo(r.right() - tr[0], r.top())
    path.arcTo(QRectF(r.right() - 2 * tr[0], r.top(), 2 * tr[0], 2 * tr[1]), 90, -90)
    path.lineTo(r.right(), r.bottom() - br[1])
    path.arcTo(QRectF(r.right() - 2 * br[0], r.bottom() - 2 * br[1], 2 * br[0], 2 * br[1]), 0, -90)
    path.lineTo(r.left() + bl[0], r.bottom())
    path.arcTo(QRectF(r.left(), r.bottom() - 2 * bl[1], 2 * bl[0], 2 * bl[1]), 270, -90)
    path.lineTo(r.left(), r.top() + tl[1])
    path.arcTo(QRectF(r.left(), r.top(), 2 * tl[0], 2 * tl[1]), 180, -90)
    path.closeSubpath()
    return path


def col(c, alpha=None):
    c = q(c) if not isinstance(c, QColor) else QColor(c)
    if alpha is not None:
        c.setAlpha(int(alpha))
    return c


def text(p, rect, s, px=12, weight=400, color="text", align=Qt.AlignLeft | Qt.AlignVCenter, display=False,
         spacing=None, wrap=False, italic=False, tabular=False):
    f = theme.font(px, weight, display=display, letter_spacing=spacing, tabular=tabular)
    if italic:
        f.setItalic(True)
    p.setFont(f)
    p.setPen(col(color))
    flags = int(align | Qt.TextWordWrap) if wrap else int(align)
    p.drawText(QRectF(rect), flags, s)


def text_w(s, px=12, weight=400, display=False, spacing=None):
    return QFontMetricsF(theme.font(px, weight, display=display, letter_spacing=spacing)).horizontalAdvance(s)


def icon(p, name, rect, color, sw=1.8):
    """A stroke icon of the app, sharp at the painter's scale."""
    r = QRectF(rect)
    size = max(8, int(round(r.width())))
    pm = icons.pixmap(name, size, color if isinstance(color, QColor) else col(color), sw)
    p.drawPixmap(r, pm, QRectF(pm.rect()))


def pill(p, rect, fill, border=None, radius=None):
    r = QRectF(rect)
    radius = r.height() / 2.0 if radius is None else radius
    p.setPen(QPen(col(border), 1) if border else Qt.NoPen)
    p.setBrush(col(fill))
    p.drawRoundedRect(r.adjusted(.5, .5, -.5, -.5) if border else r, radius, radius)


def glass(p, rect, radius=12, alpha=240, with_shadow=True):
    """A frosted card: near-white, a hairline, a soft shadow."""
    r = QRectF(rect)
    if with_shadow:
        shadow(p, r, radius)
    path = QPainterPath()
    path.addRoundedRect(r.adjusted(.5, .5, -.5, -.5), radius, radius)
    g = QLinearGradient(r.topLeft(), r.bottomLeft())
    g.setColorAt(0, QColor(255, 255, 255, alpha))
    g.setColorAt(1, QColor(246, 249, 252, alpha))
    p.fillPath(path, QBrush(g))
    p.setPen(QPen(QColor(203, 213, 225, 160), 1))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)


def window_gradient(w, h):
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, q("window-a"))
    g.setColorAt(0.5, q("window-b"))
    g.setColorAt(1, q("window-c"))
    return QBrush(g)


def logo_image(px):
    """The real logo (assets/icon_512.png) at exactly ``px`` pixels, Lanczos."""
    im = Image.open(ROOT / "assets" / "icon_512.png").convert("RGBA").resize((px, px), Image.LANCZOS)
    return QImage(im.tobytes("raw", "RGBA"), px, px, px * 4, QImage.Format_RGBA8888).copy()


def draw_logo(p, x, y, d):
    p.drawImage(QRectF(x, y, d, d), logo_image(int(round(d * p.worldTransform().m11()))))


# ── keycaps and the dictation bar (the app's own look) ───────────────────────
def keycap(p, rect, label, lit=False, px=17):
    r = QRectF(rect)
    if lit:
        shadow(p, r, 10, 34, 7, 4, (37, 99, 235))
        p.setPen(QPen(QColor(59, 130, 246, 40), 4))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(-2, -2, 2, 2), 12, 12)
        p.setPen(QPen(QColor("#93c5fd"), 1))
        p.setBrush(QColor("#eff6ff"))
        p.drawRoundedRect(r.adjusted(.5, .5, -.5, -.5), 10, 10)
        text(p, r, label, px, 700, "blue-tx", Qt.AlignCenter, display=True)
        return
    shadow(p, r, 10, 18, 6, 4)
    outer = QPainterPath()
    outer.addRoundedRect(r, 10, 10)
    p.fillPath(outer, q("line2"))
    inner = corner_path(r.adjusted(1, 1, -1, -3), (9, 9), (9, 9), (9, 7), (9, 7))
    p.fillPath(inner, QColor("#ffffff"))
    text(p, r.adjusted(1, 1, -1, -3), label, px, 700, "ink", Qt.AlignCenter, display=True)


def keys(p, cx, cy, a, b, lit=False, size=34):
    w = size * 1.25
    gap = 16
    x = cx - (w * 2 + gap) / 2
    keycap(p, QRectF(x, cy - size / 2, w, size), a, lit, px=size * 0.5)
    text(p, QRectF(x + w, cy - size / 2, gap, size), "+", 13, 700, "blue-l" if lit else "faint", Qt.AlignCenter)
    keycap(p, QRectF(x + w + gap, cy - size / 2, w, size), b, lit, px=size * 0.5)


def bar(p, cx, y, state, label=None, t=0.4):
    """The dictation bar centred on ``cx``, top at ``y`` (36 high). ``label`` replaces the bar's words for the
    Smart Actions bar ("Writing your email" / "Email pasted")."""
    h = 36.0
    if label is None:
        w = {"recording": 204.0, "transcribing": 140.0, "pasted": 160.0}[state]
        paint_pill(p, QRectF(cx - w / 2, y, w, h), state, t, True)
        return
    f = theme.font(13, 600)
    w = 13 + 17 + 10 + QFontMetricsF(f).horizontalAdvance(label) + 17
    rect = QRectF(cx - w / 2, y, w, h)
    r = h / 2.0
    p.setPen(QPen(PILL_HAIR, 1))
    p.setBrush(PILL_TINT)
    body = rect.adjusted(0.5, 0.5, -0.5, -0.5)
    p.drawRoundedRect(body, r - 0.5, r - 0.5)
    p.setPen(QPen(QColor(255, 255, 255, 230), 1))
    p.drawLine(QPointF(body.left() + r, body.top() + 1.0), QPointF(body.right() - r, body.top() + 1.0))
    x, cy = rect.left() + 13.0, rect.center().y()
    if state == "transcribing":
        box = QRectF(x, cy - 8.5, 17, 17).adjusted(1.6, 1.6, -1.6, -1.6)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(ink(0.16), 2.0))
        p.drawEllipse(box)
        pen = QPen(ink(0.94), 2.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(box, int((90 - (t * 360.0 * 1.2) % 360) * 16), -70 * 16)
    else:
        p.setPen(Qt.NoPen)
        p.setBrush(PILL_DONE)
        p.drawEllipse(QRectF(x, cy - 9, 18, 18))
        icon(p, "check", QRectF(x + 3, cy - 6, 12, 12), QColor("#ffffff"), 2.6)
    text(p, QRectF(x + (27 if state == "transcribing" else 28), rect.top(), rect.width(), h), label, 13, 600, ink(0.94))


# ── the common slide frame: panel, feature name, title, line, dots ───────────
def slide_chrome(p, n):
    s = SLIDES[n]
    acc, acc_tx = s["accent"]
    path = QPainterPath()
    path.addRoundedRect(QRectF(.5, .5, SLIDE_W - 1, SLIDE_H - 1), 14, 14)
    p.fillPath(path, window_gradient(SLIDE_W, SLIDE_H))
    glow = QRadialGradient(QPointF(SLIDE_W * 0.92, SLIDE_H * 0.1), 230)
    glow.setColorAt(0, col(acc, 22))
    glow.setColorAt(1, col(acc, 0))
    p.fillPath(path, QBrush(glow))
    p.setPen(QPen(q("glass-line"), 1))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    # the tool: its icon and its real name
    icon(p, s["icon"], QRectF(24, 24, 15, 15), col(acc_tx), 1.9)
    text(p, QRectF(45, 22, 150, 19), s["eyebrow"].upper(), 10, 800, col(acc_tx), spacing=1.4)
    # the headline and the plain line
    text(p, QRectF(24, 48, 170, 60), s["title"], 22, 700, "ink", Qt.AlignLeft | Qt.AlignTop, display=True,
         spacing=-0.3, wrap=True)
    text(p, QRectF(24, 112, 164, 56), s["line"], 12.5, 400, "muted", Qt.AlignLeft | Qt.AlignTop, wrap=True)
    # five dots, the current one long and in the tool's colour
    x = 24.0
    p.setPen(Qt.NoPen)
    for i in range(1, 6):
        on = i == n
        w = 16.0 if on else 6.0
        p.setBrush(col(acc) if on else q("line2"))
        p.drawRoundedRect(QRectF(x, 177, w, 6), 3, 3)
        x += w + 5


SX, SY, SW, SH = 204.0, 14.0, 284.0, 172.0     # the scene's box, right of the text column


# ── slide 1: Dictation ───────────────────────────────────────────────────────
def mail_window(p, r, to, lines, subject=None, caret=False, header="New message", body_px=12, line_h=17):
    r = QRectF(r)
    shadow(p, r, 9, 16, 8, 5)
    path = QPainterPath()
    path.addRoundedRect(r, 9, 9)
    p.fillPath(path, QColor("#ffffff"))
    p.save()
    p.setClipPath(path)
    y = r.top()
    if header:
        p.fillRect(QRectF(r.left(), y, r.width(), 22), q("bg"))
        p.fillRect(QRectF(r.left(), y + 22, r.width(), 1), q("line"))
        text(p, QRectF(r.left() + 11, y, 140, 22), header, 11, 600, "text2")
        y += 23
    text(p, QRectF(r.left() + 11, y, 30, 22), "To", 11.5, 400, "muted")
    chip = QRectF(r.left() + 36, y + 4, 52, 15)
    pill(p, chip, "bg2")
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#db2777"))
    p.drawEllipse(QRectF(chip.left() + 2, chip.top() + 2, 11, 11))
    text(p, QRectF(chip.left() + 2, chip.top() + 2, 11, 11), to[0], 7, 700, "#ffffff", Qt.AlignCenter)
    text(p, QRectF(chip.left() + 16, chip.top(), 36, 15), to, 10.5, 600, "text")
    y += 22
    p.fillRect(QRectF(r.left(), y, r.width(), 1), q("line"))
    if subject is not None:
        text(p, QRectF(r.left() + 11, y, 52, 21), "Subject", 11.5, 400, "muted")
        text(p, QRectF(r.left() + 62, y, r.width() - 70, 21), subject, 11.5, 700, "text")
        y += 21
        p.fillRect(QRectF(r.left(), y, r.width(), 1), q("line"))
    y += 8
    if caret:
        p.fillRect(QRectF(r.left() + 11, y + 1.5, 1.2, 14), q("blue"))
    for ln in lines:
        text(p, QRectF(r.left() + 11, y, r.width() - 20, line_h), ln, body_px, 400, "text")
        y += line_h
    p.restore()


def scene1(p, f):
    body = []
    if f == 6:
        body = ["Hi Anna,", "Can we move the review to Tuesday", "at 3? I’ll bring the notes from the call."]
    mail_window(p, QRectF(SX, SY, SW, 118), "Anna", body, caret=f in (1, 3, 5))
    cx, by = SX + SW / 2, 142
    if f in (1, 2):
        keys(p, cx, by + 18, "Alt", "R", lit=(f == 2))
    elif f == 3:
        bar(p, cx, by, "recording", t=0.3)
    elif f == 4:
        bar(p, cx, by, "recording", t=0.95)
    elif f == 5:
        bar(p, cx, by, "transcribing", t=0.3)
    else:
        bar(p, cx, by, "pasted")


# ── slide 2: Smart Actions ───────────────────────────────────────────────────
def scene2(p, f):
    # what you say (the page's own words until you speak)
    strip = QRectF(SX, 12, SW, 32)
    glass(p, strip, 10, 235, with_shadow=False)
    if f <= 2:
        icon(p, "keyboard", QRectF(strip.left() + 11, strip.center().y() - 8, 16, 16), q("faint"), 1.6)
        text(p, QRectF(strip.left() + 34, strip.top(), strip.width() - 44, strip.height()),
             "Press, say what you want, press again.", 11.5, 400, "muted")
    else:
        icon(p, "mic", QRectF(strip.left() + 11, strip.center().y() - 8, 16, 16), q("pro"), 1.8)
        text(p, QRectF(strip.left() + 34, strip.top() + 1, strip.width() - 44, strip.height() - 2),
             "“Email Anna: the demo moves to Thursday. Ask if the deck is ready.”", 11, 400, "text2",
             wrap=True, italic=True)
    # the mail it writes
    subject = "Demo moves to Thursday" if f >= 5 else ""
    lines = []
    if f == 5:
        lines = ["Hi Anna,"]
    elif f == 6:
        lines = ["Hi Anna,", "The demo moves to Thursday. Is the deck ready?", "Best,"]
    mail_window(p, QRectF(SX, 48, SW, 100), "Anna", lines, subject=subject, header=None, body_px=11.5, line_h=14.5)
    cx, by = SX + SW / 2, 152
    if f in (1, 2):
        keys(p, cx, by + 18, "Alt", "S", lit=(f == 2))
    elif f == 3:
        bar(p, cx, by, "recording", t=0.6)
    elif f == 4:
        bar(p, cx, by, "transcribing", "Writing your email", t=0.3)
    else:
        bar(p, cx, by, "pasted", "Email pasted")


# ── slide 3: Meetings ────────────────────────────────────────────────────────
FACES = {"A": ("#fce7f3", "#be185d"), "D": ("#e2e8f0", "#475569"), "M": ("#fef3c7", "#92400e")}


def avatar(p, cx, cy, d, letter, ring=True):
    bg, fg = FACES[letter]
    if ring:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(cx, cy), d / 2 + 1.5, d / 2 + 1.5)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(bg))
    p.drawEllipse(QPointF(cx, cy), d / 2, d / 2)
    text(p, QRectF(cx - d / 2, cy - d / 2, d, d), letter, d * 0.46, 700, fg, Qt.AlignCenter)


def rec_button(p, cx, cy, r=26):
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(233, 21, 45, 26))
    p.drawEllipse(QPointF(cx, cy), r + 7, r + 7)
    shadow(p, QRectF(cx - r, cy - r, 2 * r, 2 * r), r, 22, 6, 3)
    p.setPen(QPen(q("line2"), 1))
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QPointF(cx, cy), r - .5, r - .5)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#E9152D"))
    p.drawEllipse(QPointF(cx, cy), r * 0.42, r * 0.42)


def spinner(p, cx, cy, d, color="blue", t=0.3):
    box = QRectF(cx - d / 2, cy - d / 2, d, d)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(col(color, 40), 2.0))
    p.drawEllipse(box)
    pen = QPen(col(color), 2.0)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.drawArc(box, int((90 - (t * 360.0 * 1.2) % 360) * 16), -80 * 16)


def check_box(p, rect, on=False):
    r = QRectF(rect)
    p.setPen(QPen(q("blue") if on else q("line2"), 1.3))
    p.setBrush(q("blue") if on else QColor("#ffffff"))
    p.drawRoundedRect(r.adjusted(.65, .65, -.65, -.65), 4, 4)
    if on:
        icon(p, "check", r.adjusted(2, 2, -2, -2), QColor("#ffffff"), 3.0)


def scene3(p, f):
    card = QRectF(SX, SY, SW, SH)
    glass(p, card, 16)
    L, R = card.left() + 16, card.right() - 16
    if f == 1:
        rec_button(p, card.left() + 54, card.center().y(), 26)
        pill(p, QRectF(card.left() + 100, 70, 142, 24), "#ffffff", "#cbd5e1")
        icon(p, "headset", QRectF(card.left() + 109, 74, 16, 16), q("muted"), 1.6)
        text(p, QRectF(card.left() + 130, 70, 112, 24), "Both sides of the call", 10.5, 600, "text2")
        text(p, QRectF(card.left() + 100, 100, 160, 34), "Let everyone know you’re recording.", 10.5, 400,
             "muted", Qt.AlignLeft | Qt.AlignTop, wrap=True)
        return
    if f in (2, 3):
        # live: the first words, then the notes being written
        if f == 2:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#E9152D"))
            p.drawEllipse(QPointF(L + 5, card.top() + 25), 4.5, 4.5)
            text(p, QRectF(L + 16, card.top() + 14, 120, 22), "00:12", 12, 700, "ink", tabular=True)
        else:
            spinner(p, L + 6, card.top() + 25, 13, "meet", 0.3)
            text(p, QRectF(L + 20, card.top() + 14, 150, 22), "Writing notes", 12, 700, "ink")
        p.fillRect(QRectF(L, card.top() + 42, card.width() - 32, 1), q("line"))
        rows = [("A", "Can we move the launch to Friday?"), ("D", "Yes, if QA signs off on Thursday."),
                ("A", "Great. I’ll tell the team today.")]
        for i, (who, line) in enumerate(rows):
            if f == 2 and i == 2:
                continue
            y = card.top() + 56 + i * 34
            if f == 3:
                avatar(p, L + 10, y + 12, 20, who)
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(q("line2"))
                p.drawEllipse(QPointF(L + 10, y + 12), 3, 3)
            p.save()
            if f == 2 and i == 1:
                p.setOpacity(0.55)
            text(p, QRectF(L + 28, y, R - L - 28, 24), line, 12, 400, "text")
            p.restore()
        return
    # the notes
    avatar(p, L + 10, card.top() + 23, 20, "A")
    avatar(p, L + 26, card.top() + 23, 20, "D")
    text(p, QRectF(L + 44, card.top() + 12, 130, 22), "Launch plan", 12.5, 700, "ink")
    text(p, QRectF(R - 60, card.top() + 12, 60, 22), "12 min", 11, 400, "muted", Qt.AlignRight | Qt.AlignVCenter)
    p.fillRect(QRectF(L, card.top() + 38, card.width() - 32, 1), q("line"))
    text(p, QRectF(L, card.top() + 47, 150, 14), "SUMMARY", 9.5, 800, "text2", spacing=1.3)
    text(p, QRectF(L, card.top() + 62, R - L, 34), "The launch moves to Friday if QA signs off on Thursday.", 12, 400,
         "text", Qt.AlignLeft | Qt.AlignTop, wrap=True)
    if f >= 5:
        text(p, QRectF(L, card.top() + 101, 150, 14), "ACTION ITEMS", 9.5, 800, "text2", spacing=1.3)
        for i, (who, name, task) in enumerate((("D", "David", "QA sign-off by Thursday"),
                                               ("A", "Anna", "Tell the team"))):
            y = card.top() + 118 + i * 21
            check_box(p, QRectF(L, y + 2, 14, 14), on=(f == 6 and i == 1))
            avatar(p, L + 31, y + 9, 14, who, ring=False)
            text(p, QRectF(L + 44, y, 44, 18), name, 11.5, 700, "ink")
            text(p, QRectF(L + 44 + text_w(name, 11.5, 700) + 6, y, 160, 18), task, 11.5, 400, "text")


# ── slide 4: Live Assistant ──────────────────────────────────────────────────
TILES = (("#7dd3fc", "#818cf8", "M"), ("#fcd34d", "#fb923c", "D"), ("#6ee7b7", "#2dd4bf", "L"),
         ("#f0abfc", "#a78bfa", "You"))


def call_window(p, r):
    r = QRectF(r)
    shadow(p, r, 10, 22, 9, 6)
    path = QPainterPath()
    path.addRoundedRect(r, 10, 10)
    p.fillPath(path, QColor("#1e293b"))
    inner = r.adjusted(4, 4, -4, -4)
    ipath = QPainterPath()
    ipath.addRoundedRect(inner, 7, 7)
    p.fillPath(ipath, QColor("#e8edf5"))
    gap = 6.0
    tw, th = (inner.width() - 3 * gap) / 2, (inner.height() - 3 * gap) / 2
    for i, (a, b, name) in enumerate(TILES):
        tr = QRectF(inner.left() + gap + (i % 2) * (tw + gap), inner.top() + gap + (i // 2) * (th + gap), tw, th)
        g = QLinearGradient(tr.topLeft(), tr.bottomRight())
        g.setColorAt(0, QColor(a))
        g.setColorAt(1, QColor(b))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(g))
        p.drawRoundedRect(tr, 6, 6)
        text(p, tr.adjusted(0, 0, 0, -22), name, 15, 700, QColor(255, 255, 255, 235), Qt.AlignCenter, display=True)
    return inner


def label_chip(p, rect, s):
    r = QRectF(rect)
    pill(p, r, QColor(30, 41, 59, 225))
    text(p, r, s, 10, 600, "#ffffff", Qt.AlignCenter)


def card_image(scale):
    """The Live Assistant card (the page's own miniature painter) on a transparent picture of its own, so a
    clip on the slide cuts it whole (the painter sets clips of its own, which would replace ours)."""
    cs = live_demo.card_size(scale)
    c = Canvas(cs.width(), cs.height(), SS, alpha=True)
    live_demo.paint_card(c.p, 0, 0, "Hidden", "ok", "incognito", scale, None, "light")
    c.p.end()
    return c.img, cs


def scene4(p, f):
    win = QRectF(SX, SY, SW, SH)
    inner = call_window(p, win)
    scale = 0.34
    img, cs = card_image(scale)
    cx, cy = inner.right() - cs.width() - 9, inner.bottom() - cs.height() - 9
    if f == 1:
        # the question, as the call's own caption
        cap = QRectF(inner.left() + 12, inner.bottom() - 36, inner.width() - 24, 26)
        pill(p, cap, QColor(15, 23, 42, 205), radius=8)
        text(p, cap.adjusted(10, 0, -8, 0), "Mariam: When will the Q4 roadmap be ready?", 10.5, 500,
             "#ffffff")
        return
    split = {2: None, 3: None, 4: 0.62, 5: 0.28}[f]
    x_cut = cx + cs.width() if split is None else min(cx + cs.width(), win.left() + win.width() * split)
    vis = x_cut - cx                                   # the part of the card that is on the left of the line
    if vis > 0:
        p.save()
        p.setOpacity(0.5 if f == 2 else 1.0)
        p.setClipRect(QRectF(win.left(), win.top(), x_cut - win.left(), win.height()))
        shadow(p, QRectF(cx, cy, cs.width(), cs.height()), 8, 34, 8, 4)
        p.restore()
        p.save()
        p.setOpacity(0.5 if f == 2 else 1.0)
        p.drawImage(QRectF(cx, cy, vis, cs.height()), img, QRectF(0, 0, vis * SS, cs.height() * SS))
        p.restore()
    if split is None:
        return
    x = win.left() + win.width() * split
    mid = win.center().y()
    p.setPen(QPen(QColor("#0f172a"), 1.6))
    p.drawLine(QPointF(x, win.top() + 4), QPointF(x, win.bottom() - 4))
    shadow(p, QRectF(x - 11, mid - 11, 22, 22), 11, 30, 4, 2)
    p.setPen(QPen(QColor("#0f172a"), 1))
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QPointF(x, mid), 11, 11)
    p.setPen(QPen(QColor("#0f172a"), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    for d in (-1, 1):
        p.drawPolyline([QPointF(x + d * 2.2, mid - 3.8), QPointF(x + d * 5.2, mid), QPointF(x + d * 2.2, mid + 3.8)])
    label_chip(p, QRectF(win.left() + 12, win.top() + 12, 54, 19), "You see")
    label_chip(p, QRectF(win.right() - 66, win.top() + 12, 54, 19), "They see")
    if f == 5:
        chip = QRectF(win.left() + win.width() * 0.72 - 46, mid - 13, 92, 26)
        shadow(p, chip, 13, 24, 5, 3)
        pill(p, chip, "#dcfce7", "#86efac")
        icon(p, "incognito", QRectF(chip.left() + 9, chip.top() + 5, 16, 16), q("ok-tx"), 1.7)
        text(p, QRectF(chip.left() + 29, chip.top(), 60, 26), "Hidden", 11.5, 700, "ok-tx")


# ── slide 5: Audio to text ───────────────────────────────────────────────────
def file_chip(p, cx, cy, name="interview_mariam.m4a", angle=0.0, icon_name="mic", tile="#e8f0fe", tint="blue",
              check=False):
    w = text_w(name, 11, 600) + 50 + (18 if check else 0)
    r = QRectF(-w / 2, -15, w, 30)
    p.save()
    p.translate(cx, cy)
    p.rotate(angle)
    shadow(p, r, 9, 26, 8, 5)
    glass(p, r, 9, 255, with_shadow=False)
    t = QRectF(r.left() + 5, r.top() + 5, 20, 20)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(tile))
    p.drawRoundedRect(t, 6, 6)
    icon(p, icon_name, t.adjusted(4, 4, -4, -4), q(tint), 1.9)
    text(p, QRectF(r.left() + 32, r.top(), r.width() - 36, r.height()), name, 11, 600, "ink")
    if check:
        p.setPen(Qt.NoPen)
        p.setBrush(PILL_DONE)
        p.drawEllipse(QRectF(r.right() - 22, r.center().y() - 7, 14, 14))
        icon(p, "check", QRectF(r.right() - 19.5, r.center().y() - 4.5, 9, 9), QColor("#ffffff"), 3.0)
    p.restore()


def drop_zone(p, r, active=False):
    r = QRectF(r)
    path = QPainterPath()
    path.addRoundedRect(r.adjusted(.7, .7, -.7, -.7), 16, 16)
    p.fillPath(path, QColor(59, 130, 246, 38 if active else 22))
    pen = QPen(QColor("#60a5fa") if active else QColor("#a5c4f6"), 1.4)
    pen.setDashPattern([4.0, 3.0])
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    x = r.left() + 22
    text(p, QRectF(x, r.top() + 60, r.width() - 40, 24), "Drop audio or video here", 14.5, 700, "ink", display=True,
         spacing=-0.1)
    text(p, QRectF(x, r.top() + 85, r.width() - 40, 18), "MP3, WAV, M4A, FLAC or video · any length", 10.5, 400,
         "muted")
    btn = QRectF(x, r.top() + 110, 112, 28)
    p.setPen(QPen(q("blue-h"), 1))
    p.setBrush(q("blue"))
    p.drawRoundedRect(btn.adjusted(.5, .5, -.5, -.5), 8, 8)
    icon(p, "upload", QRectF(btn.left() + 11, btn.top() + 6, 16, 16), QColor("#ffffff"), 1.9)
    text(p, QRectF(btn.left() + 31, btn.top(), 80, btn.height()), "Choose a file", 11.5, 600, "#ffffff")


WAVE = [4, 7, 10, 14, 9, 17, 22, 15, 11, 19, 26, 18, 9, 13, 21, 28, 20, 12, 8, 15, 24, 17, 10, 6, 12, 19, 25, 16,
        9, 13, 7, 4]


def scene5(p, f):
    zone = QRectF(SX, SY + 8, SW, SH - 16)
    if f in (1, 2):
        drop_zone(p, zone, active=(f == 2))
        if f == 1:
            file_chip(p, SX + SW - 84, 34, angle=-6)
        else:
            file_chip(p, SX + SW - 100, 54, angle=0)
        return
    if f in (3, 4):
        pct = 0.42 if f == 3 else 1.0
        card = QRectF(SX, SY + 10, SW, 84)
        glass(p, card, 14)
        tile = QRectF(card.left() + 14, card.top() + 14, 34, 34)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e8f0fe"))
        p.drawRoundedRect(tile, 10, 10)
        icon(p, "mic", tile.adjusted(8, 8, -8, -8), q("blue"), 1.9)
        x0 = card.left() + 58
        text(p, QRectF(x0, card.top() + 12, 150, 20), "interview_mariam.m4a", 12, 700, "ink")
        if f == 4:
            p.setPen(Qt.NoPen)
            p.setBrush(PILL_DONE)
            p.drawEllipse(QRectF(card.right() - 32, card.top() + 13, 18, 18))
            icon(p, "check", QRectF(card.right() - 28, card.top() + 17, 10, 10), QColor("#ffffff"), 3.0)
        track = QRectF(x0, card.top() + 37, card.width() - 58 - 16, 8)
        pill(p, track, "#e2e8f0")
        pill(p, QRectF(track.left(), track.top(), max(8.0, track.width() * pct), track.height()),
             "#22c55e" if f == 4 else "#2563eb")
        status = "Transcribing · 42% · Whisper Small · Local" if f == 3 else "Done · 2 speakers · Whisper Small · Local"
        text(p, QRectF(x0, card.top() + 50, card.width() - 70, 22), status, 10.5, 400, "text2")
        # the sound, drawn as the work moves through it
        wy, ww = SY + 140, SW - 24
        n = len(WAVE)
        step = ww / n
        for i, v in enumerate(WAVE):
            done = (i + 0.5) / n <= pct
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(37, 99, 235, 175) if done else QColor(148, 163, 184, 90))
            h = v * 1.45
            p.drawRoundedRect(QRectF(SX + 12 + i * step + step * 0.2, wy - h / 2, step * 0.55, h), 1.6, 1.6)
        return
    # the transcript, then the Word file
    paper = QRectF(SX, SY + 2, SW, SH - 4)
    glass(p, paper, 14)
    L = paper.left() + 16
    p.save()
    if f == 6:
        p.setOpacity(0.35)
    text(p, QRectF(L, paper.top() + 12, 170, 18), "interview_mariam.m4a", 12.5, 700, "ink")
    text(p, QRectF(L, paper.top() + 30, 190, 16), "English · 24 min · 2 speakers", 10.5, 400, "muted")
    p.fillRect(QRectF(L, paper.top() + 52, paper.width() - 32, 1), q("line"))
    rows = (("00:00", "Speaker 1", "#1d4ed8", "Thanks for coming in, Mariam."),
            ("00:04", "Speaker 2", "#15803d", "Easy, thanks. Traffic was kind today."),
            ("00:09", "Speaker 1", "#1d4ed8", "Good. Tell me about the project."))
    for i, (tm, who, c, line) in enumerate(rows):
        y = paper.top() + 62 + i * 34
        text(p, QRectF(L, y, 40, 14), tm, 9.5, 400, "faint", tabular=True)
        text(p, QRectF(L + 38, y, 90, 14), who, 10, 700, c)
        text(p, QRectF(L, y + 14, paper.width() - 32, 17), line, 12, 400, "text")
    p.restore()
    bw = 98
    b = QRectF(paper.right() - 16 - bw, paper.top() + 12, bw, 28)
    pressed = (f == 6)
    p.setPen(QPen(q("blue-h") if pressed else q("line2"), 1))
    p.setBrush(q("blue") if pressed else QColor("#ffffff"))
    p.drawRoundedRect(b.adjusted(.5, .5, -.5, -.5), 8, 8)
    icon(p, "file", QRectF(b.left() + 9, b.top() + 6, 16, 16), QColor("#ffffff") if pressed else q("text2"), 1.7)
    text(p, QRectF(b.left() + 30, b.top(), bw - 30, b.height()), "Save Word", 11, 600,
         "#ffffff" if pressed else "text")
    if f == 6:
        file_chip(p, paper.center().x(), paper.top() + 112, "interview_mariam.docx", 0.0, "file", "#dbeafe", "blue-h",
                  check=True)


SCENES = {1: scene1, 2: scene2, 3: scene3, 4: scene4, 5: scene5}


def slide_frame(n, f):
    c = Canvas(SLIDE_W, SLIDE_H, SS)
    slide_chrome(c.p, n)
    SCENES[n](c.p, f)
    im = c.pil().convert("RGB")
    return im.resize((SLIDE_W * SLIDE_OUT, SLIDE_H * SLIDE_OUT), Image.LANCZOS)


# ── the Finish page's left picture ───────────────────────────────────────────
def finish_panel(w_px, h_px):
    """The picture for an image area of w_px x h_px: 202 logical px wide whatever the size, painted at 3x."""
    scale = w_px / 202.0
    lh = h_px / scale
    c = Canvas(202, lh, scale * 3)
    p = c.p
    p.fillRect(QRectF(0, 0, 202, lh), window_gradient(202, lh))
    glow = QRadialGradient(QPointF(202 * 0.95, lh * 0.8), 180)
    glow.setColorAt(0, QColor(59, 130, 246, 50))
    glow.setColorAt(1, QColor(59, 130, 246, 0))
    p.fillRect(QRectF(0, 0, 202, lh), QBrush(glow))
    glow2 = QRadialGradient(QPointF(0, 0), 170)
    glow2.setColorAt(0, QColor(255, 255, 255, 150))
    glow2.setColorAt(1, QColor(255, 255, 255, 0))
    p.fillRect(QRectF(0, 0, 202, lh), QBrush(glow2))
    # the sound, rising from the bottom edge
    n = len(WAVE_HEIGHTS)
    bw, gap, x0 = 6.0, 6.0, 18.0
    for i, hgt in enumerate(WAVE_HEIGHTS):
        hh = hgt * 1.45
        c2 = QColor(59, 130, 246)
        c2.setAlpha(int(70 + 90 * (hgt / 52.0)))
        p.setPen(Qt.NoPen)
        p.setBrush(c2)
        p.drawRoundedRect(QRectF(x0 + i * (bw + gap), lh - 18 - hh, bw, hh), 3, 3)
    # the logo and the name
    draw_logo(p, 20, 24, 38)
    text(p, QRectF(68, 24, 120, 38), "Gusan", 25, 700, "ink", display=True, spacing=-0.3)
    text(p, QRectF(20, 88, 170, 56), "Speak.\nGusan types.", 17, 700, "ink", Qt.AlignLeft | Qt.AlignTop, display=True,
         spacing=-0.2, wrap=True)
    # the shortcut and the bar, on one frosted card
    card = QRectF(14, 150, 174, 118)
    glass(p, card, 16, 205)
    keys(p, 101, 150 + 36, "Alt", "R", lit=True, size=40)
    bar(p, 101, 150 + 70, "pasted")
    return c.pil().convert("RGB").resize((w_px, h_px), Image.LANCZOS)


def small_image(px):
    big = Image.new("RGBA", (px * 4, px * 4), (0, 0, 0, 0))
    d = int(round(px * 0.64)) * 4
    logo = Image.open(ROOT / "assets" / "icon_512.png").convert("RGBA").resize((d, d), Image.LANCZOS)
    off = (px * 4 - d) // 2
    big.paste(logo, (off, off), logo)
    return big.resize((px, px), Image.LANCZOS)


# ── writing ──────────────────────────────────────────────────────────────────
def save_png(im, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True, compress_level=9)
    return path.stat().st_size


def build_slides(which):
    frames = {}
    for n in which:
        for f in range(1, len(SLIDES[n]["ms"]) + 1):
            frames[(n, f)] = slide_frame(n, f)
    return frames


def write_slides(frames, which):
    """Saves every frame of the chosen slides; returns the manifest."""
    manifest = {"canvas": [SLIDE_W * SLIDE_OUT, SLIDE_H * SLIDE_OUT], "tick_ms": 100,
                "note": "Show the frames of slide 1..5 in order and loop; each frame stays for ms.", "slides": []}
    for n in which:
        s = SLIDES[n]
        entry = {"n": n, "eyebrow": s["eyebrow"], "title": s["title"].replace("\n", " "), "line": s["line"],
                 "frames": []}
        for f in range(1, len(s["ms"]) + 1):
            name = "slide%d_%d.png" % (n, f)
            save_png(frames[(n, f)], OUT / name)
            entry["frames"].append({"file": name, "ms": s["ms"][f - 1]})
        assert sum(fr["ms"] for fr in entry["frames"]) == 6000, (n, entry)
        manifest["slides"].append(entry)
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", nargs="+", metavar="WHAT",
                    help="slides [N ...] | finish | small (default: everything)")
    args = ap.parse_args()
    what = args.only or ["slides", "finish", "small"]
    OUT.mkdir(parents=True, exist_ok=True)
    if "slides" in what:
        nums = [int(x) for x in what if x.isdigit()] or sorted(SLIDES)
        frames = build_slides(nums)
        manifest = write_slides(frames, nums)
        if len(nums) == len(SLIDES):
            (OUT / "slides.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if "finish" in what:
        for w, h in FINISH_SIZES:
            save_png(finish_panel(w, h), OUT / ("finish_%dx%d.png" % (w, h)))
    if "small" in what:
        for s in SMALL_SIZES:
            save_png(small_image(s), OUT / ("small_%d.png" % s))
    total = sum(f.stat().st_size for f in OUT.glob("*.png"))
    print("%d files, %.2f MB in %s" % (len(list(OUT.glob("*.png"))), total / 1048576.0, OUT))
    assert total < BUDGET_BYTES, "the pictures are over the %d byte budget" % BUDGET_BYTES


if __name__ == "__main__":
    main()
