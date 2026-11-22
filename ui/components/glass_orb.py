"""GlassOrb: a liquid-glass circle holding an emoji (Audio to text's 🎧 → 📝).

A translucent, glossy 3D look in the app's blue: a soft blue shadow under
the orb, a glass body lit from the top left, depth at the bottom edge, light
refracted back up inside, the emoji lifted by its own soft shadow, a specular
gloss over the top and a bright rim. Drawn once per size, screen scale, state
and look, then cached (no animation, no per-frame cost).

The glass is a material of its own, defined for both looks in _ORB (light: clear
frosted glass; dark: a smoked blue glass that does not glow white on the dark
window). The cache is keyed by the look, so a switch redraws it.
"""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap, QRadialGradient)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ui import theme

SIDE = 9           # room for the shadow left and right
TOP = 4
BOTTOM = 20        # the shadow falls below

# The glass, per look: (r, g, b, a) for each part of the drawing.
_ORB = {
    "light": {
        "glyph_shadow": (30, 64, 175, 255), "pool": (37, 99, 235), "body0": (255, 255, 255, 252),
        "body1": (241, 246, 255, 240), "body2": (205, 224, 252, 232), "depth0": (147, 197, 253, 0),
        "depth1": (59, 130, 246), "depth_a": (95, 120), "caus": (255, 255, 255, 175), "gloss0": (255, 255, 255, 190),
        "gloss1": (255, 255, 255, 60), "spark": (255, 255, 255, 235), "rim0": (255, 255, 255, 255),
        "rim1": (219, 234, 254, 200), "rim2": (96, 165, 250), "rim_a": (210, 255), "line": (255, 255, 255, 200),
    },
    "dark": {
        "glyph_shadow": (0, 0, 0, 255), "pool": (37, 99, 235), "body0": (120, 160, 235, 92),
        "body1": (59, 100, 200, 74), "body2": (30, 58, 138, 96), "depth0": (96, 165, 250, 0),
        "depth1": (37, 99, 235), "depth_a": (110, 140), "caus": (147, 197, 253, 95), "gloss0": (255, 255, 255, 72),
        "gloss1": (255, 255, 255, 18), "spark": (255, 255, 255, 170), "rim0": (191, 219, 254, 170),
        "rim1": (96, 165, 250, 110), "rim2": (59, 130, 246), "rim_a": (170, 230), "line": (255, 255, 255, 70),
    },
}


def _c(rgba, alpha=None):
    c = QColor(*rgba[:3])
    c.setAlpha(alpha if alpha is not None else (rgba[3] if len(rgba) > 3 else 255))
    return c


class GlassOrb(QWidget):
    def __init__(self, emoji, glyph_px, diameter=104, name="", font=None, parent=None):
        super().__init__(parent)
        self._emoji = emoji
        self._glyph_px = glyph_px
        self._d = float(diameter)
        self._font = font
        self._active = False
        self._cache = {}
        self.setFixedSize(int(diameter + 2 * SIDE), int(diameter + TOP + BOTTOM))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        if name:
            self.setAccessibleName(name)

    # QLabel-like, for callers and tests that read the glyph
    def text(self):
        return self._emoji

    def sizeHint(self):
        return QSize(self.width(), self.height())

    def set_active(self, on):
        """Files dragged over the page: the orb glows a little brighter."""
        if bool(on) != self._active:
            self._active = bool(on)
            self.update()

    def orb_rect(self):
        return QRectF(SIDE, TOP, self._d, self._d)

    def paintEvent(self, _e):
        dpr = self.devicePixelRatioF()
        key = (round(dpr, 3), self._active, self.width(), self.height(), theme.scheme())
        pm = self._cache.get(key)
        if pm is None:
            self._cache.clear()
            pm = self._cache[key] = self._render(dpr)
        p = QPainter(self)
        p.drawPixmap(0, 0, pm)
        p.end()

    # ── drawing ──
    def _glyph_font(self):
        if self._font is not None:
            f = QFont(self._font)
        else:
            f = QFont()
            f.setFamilies(["Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"])
        f.setPixelSize(int(self._glyph_px))
        return f

    def _glyph(self, dpr):
        """The emoji alone and its soft shadow, as images the orb's size."""
        m = _ORB[theme.scheme()]
        size = int(self._d * dpr)
        img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        g = QPainter(img)
        g.setRenderHint(QPainter.Antialiasing, True)
        g.setRenderHint(QPainter.TextAntialiasing, True)
        g.setFont(self._glyph_font())
        g.drawText(QRectF(0, 0, self._d, self._d), Qt.AlignCenter, self._emoji)
        g.end()
        # Its shadow: the shape in the glass's deep blue, softened by stacking.
        sil = QImage(img)
        s = QPainter(sil)
        s.setCompositionMode(QPainter.CompositionMode_SourceIn)
        s.fillRect(QRectF(0, 0, self._d, self._d), _c(m["glyph_shadow"]))
        s.end()
        shadow = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        shadow.setDevicePixelRatio(dpr)
        shadow.fill(Qt.transparent)
        b = QPainter(shadow)
        b.setRenderHint(QPainter.SmoothPixmapTransform, True)
        offsets = [(dx, dy) for dx in (-2.0, -1.0, 0.0, 1.0, 2.0) for dy in (-2.0, -1.0, 0.0, 1.0, 2.0)]
        b.setOpacity(1.0 / len(offsets) * 1.6)
        for dx, dy in offsets:
            b.drawImage(QPointF(dx, dy), sil)
        b.end()
        return img, shadow

    def _render(self, dpr):
        w, h = self.width(), self.height()
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        d = self._d
        r = self.orb_rect()
        c = r.center()
        p.setPen(Qt.NoPen)
        m = _ORB[theme.scheme()]

        # 1. The shadow on the page: a wide soft blue pool and a tighter contact shadow.
        for dy, rx, ry, alpha in ((6.0, 0.48, 0.15, 46 if not self._active else 70),
                                  (2.0, 0.40, 0.10, 40)):
            g = QRadialGradient(QPointF(0, 0), d * rx)
            g.setColorAt(0.0, _c(m["pool"], alpha))
            g.setColorAt(1.0, _c(m["pool"], 0))
            p.save()
            p.translate(c.x(), r.bottom() + dy - d * 0.04)
            p.scale(1.0, ry / rx)
            p.setBrush(g)
            p.drawEllipse(QPointF(0, 0), d * rx, d * rx)
            p.restore()

        circle = QPainterPath()
        circle.addEllipse(r)

        # 2. The glass body, lit from the top left.
        body = QRadialGradient(QPointF(c.x() - d * 0.18, c.y() - d * 0.24), d * 0.78)
        body.setColorAt(0.00, _c(m["body0"]))
        body.setColorAt(0.45, _c(m["body1"]))
        body.setColorAt(1.00, _c(m["body2"]))
        p.setBrush(body)
        p.drawPath(circle)

        p.save()
        p.setClipPath(circle)
        # 3. Depth: the far edge darkens (bottom right), like a thick lens.
        depth = QRadialGradient(QPointF(c.x() - d * 0.10, c.y() - d * 0.16), d * 0.66)
        depth.setColorAt(0.70, _c(m["depth0"]))
        depth.setColorAt(1.00, _c(m["depth1"], m["depth_a"][1 if self._active else 0]))
        p.setBrush(depth)
        p.drawPath(circle)
        # 4. Light refracted back up inside, along the bottom.
        caus = QRadialGradient(QPointF(0, 0), d * 0.36)
        caus.setColorAt(0.0, _c(m["caus"]))
        caus.setColorAt(1.0, _c(m["caus"], 0))
        p.save()
        p.translate(c.x(), r.bottom() - d * 0.11)
        p.scale(1.0, 0.36)
        p.setBrush(caus)
        p.drawEllipse(QPointF(0, 0), d * 0.36, d * 0.36)
        p.restore()

        # 5. The emoji, lifted off the glass by a soft shadow below it.
        img, shadow = self._glyph(dpr)
        p.setOpacity(0.30)
        p.drawImage(QPointF(r.left(), r.top() + 3.5), shadow)
        p.setOpacity(1.0)
        p.drawImage(QPointF(r.left(), r.top() - 1.0), img)

        # 6. The gloss: a bright band over the top that fades by the middle.
        gloss = QRectF(r.left() + d * 0.13, r.top() + d * 0.035, d * 0.74, d * 0.42)
        lg = QLinearGradient(gloss.topLeft(), gloss.bottomLeft())
        lg.setColorAt(0.0, _c(m["gloss0"]))
        lg.setColorAt(0.55, _c(m["gloss1"]))
        lg.setColorAt(1.0, _c(m["gloss1"], 0))
        p.setBrush(lg)
        p.drawEllipse(gloss)
        # A small sharp highlight, top left.
        p.save()
        p.translate(r.left() + d * 0.27, r.top() + d * 0.19)
        p.rotate(-38.0)
        p.setBrush(_c(m["spark"]))
        p.drawEllipse(QPointF(0, 0), d * 0.065, d * 0.028)
        p.restore()
        p.restore()

        # 7. The rim: bright where the light hits, blue where it bends away.
        rim = QLinearGradient(r.topLeft(), r.bottomRight())
        rim.setColorAt(0.0, _c(m["rim0"]))
        rim.setColorAt(0.45, _c(m["rim1"]))
        rim.setColorAt(1.0, _c(m["rim2"], m["rim_a"][1 if self._active else 0]))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QBrush(rim), 1.5))
        p.drawEllipse(r.adjusted(0.75, 0.75, -0.75, -0.75))
        # A thin inner light line along the top (the glass's thickness).
        p.setPen(QPen(_c(m["line"]), 1.1, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(r.adjusted(3.2, 3.2, -3.2, -3.2), 35 * 16, 110 * 16)
        p.end()
        return pm
