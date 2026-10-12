"""GlassOrb: a liquid-glass circle holding an emoji (Audio to text's 🎧 → 📝).

A translucent, glossy 3D look in the app's blue: a soft blue shadow under
the orb, a glass body lit from the top left, depth at the bottom edge, light
refracted back up inside, the emoji lifted by its own soft shadow, a specular
gloss over the top and a bright rim. Drawn once per size, screen scale and
state, then cached (no animation, no per-frame cost).
"""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap, QRadialGradient)
from PySide6.QtWidgets import QSizePolicy, QWidget

SIDE = 9           # room for the shadow left and right
TOP = 4
BOTTOM = 20        # the shadow falls below


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
        key = (round(dpr, 3), self._active, self.width(), self.height())
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
        s.fillRect(QRectF(0, 0, self._d, self._d), QColor(30, 64, 175))
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

        # 1. The shadow on the page: a wide soft blue pool and a tighter contact shadow.
        for dy, rx, ry, alpha in ((6.0, 0.48, 0.15, 46 if not self._active else 70),
                                  (2.0, 0.40, 0.10, 40)):
            g = QRadialGradient(QPointF(0, 0), d * rx)
            g.setColorAt(0.0, QColor(37, 99, 235, alpha))
            g.setColorAt(1.0, QColor(37, 99, 235, 0))
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
        body.setColorAt(0.00, QColor(255, 255, 255, 252))
        body.setColorAt(0.45, QColor(241, 246, 255, 240))
        body.setColorAt(1.00, QColor(205, 224, 252, 232))
        p.setBrush(body)
        p.drawPath(circle)

        p.save()
        p.setClipPath(circle)
        # 3. Depth: the far edge darkens (bottom right), like a thick lens.
        depth = QRadialGradient(QPointF(c.x() - d * 0.10, c.y() - d * 0.16), d * 0.66)
        depth.setColorAt(0.70, QColor(147, 197, 253, 0))
        depth.setColorAt(1.00, QColor(59, 130, 246, 95 if not self._active else 120))
        p.setBrush(depth)
        p.drawPath(circle)
        # 4. Light refracted back up inside, along the bottom.
        caus = QRadialGradient(QPointF(0, 0), d * 0.36)
        caus.setColorAt(0.0, QColor(255, 255, 255, 175))
        caus.setColorAt(1.0, QColor(255, 255, 255, 0))
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
        lg.setColorAt(0.0, QColor(255, 255, 255, 190))
        lg.setColorAt(0.55, QColor(255, 255, 255, 60))
        lg.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(lg)
        p.drawEllipse(gloss)
        # A small sharp highlight, top left.
        p.save()
        p.translate(r.left() + d * 0.27, r.top() + d * 0.19)
        p.rotate(-38.0)
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawEllipse(QPointF(0, 0), d * 0.065, d * 0.028)
        p.restore()
        p.restore()

        # 7. The rim: bright where the light hits, blue where it bends away.
        rim = QLinearGradient(r.topLeft(), r.bottomRight())
        rim.setColorAt(0.0, QColor(255, 255, 255, 255))
        rim.setColorAt(0.45, QColor(219, 234, 254, 200))
        rim.setColorAt(1.0, QColor(96, 165, 250, 210 if not self._active else 255))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QBrush(rim), 1.5))
        p.drawEllipse(r.adjusted(0.75, 0.75, -0.75, -0.75))
        # A thin inner light line along the top (the glass's thickness).
        p.setPen(QPen(QColor(255, 255, 255, 200), 1.1, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(r.adjusted(3.2, 3.2, -3.2, -3.2), 35 * 16, 110 * 16)
        p.end()
        return pm
