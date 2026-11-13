"""The live captions' look on the Meetings card (founder 2026-11-12: "choose the colour and the font ... not a lot of
combinations, just the fonts with the visual representation"): rows of small tiles - the fonts (captions.FONTS),
each showing "Aa" in it, the colour pairs (captions.COLORS), each showing the call's and your colour, and the sizes
(captions.SIZES, founder 2026-11-13: "a bigger size, also in the middle of a meeting"), each an "Aa" of that size - on a
dark little screen, outlined like the captions themselves. One tile of a row is picked; clicking another picks it at
once. ``compact`` tiles fit the live card.

    tiles = StyleTiles("font", on_pick=lambda key: ...)      # or "colors", "size"
    tiles.set_value("bold")
"""
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QButtonGroup, QHBoxLayout, QWidget

import captions

TILE_W, TILE_H = 60, 44
COMPACT_W, COMPACT_H = 52, 36
SCREEN = QColor(36, 39, 46)
ACCENT = "#2563eb"                    # theme TOKENS "blue"


class _Tile(QAbstractButton):
    def __init__(self, kind, key, name, parent=None, compact=False, scale=1.0):
        super().__init__(parent)
        self.kind, self.key, self.scale = kind, key, scale
        self._w, self._h = (COMPACT_W, COMPACT_H) if compact else (TILE_W, TILE_H)
        self.setCheckable(True)
        self.setFixedSize(self._w, self._h)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(name)
        self.setToolTip(name)

    def sizeHint(self):
        return QSize(self._w, self._h)

    def _text(self, p, text, font, rgb, x, baseline):
        path = QPainterPath()
        path.addText(QPointF(x, baseline), font, text)
        p.strokePath(path, QPen(QColor(0, 0, 0, 220), 3.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.fillPath(path, QColor(*rgb))

    def paintEvent(self, _e):
        from ui.captions_overlay import caption_font, color_pair
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        p.setPen(Qt.NoPen)
        p.setBrush(SCREEN)
        p.drawRoundedRect(r, 9, 9)
        if self.kind in ("font", "size"):
            if self.kind == "font":
                font = caption_font(self.key, 19 if self._h >= TILE_H else 16)
            else:
                font = caption_font("clean", round((11 if self._h < TILE_H else 13) * self.scale) + 2)
            w = QPainterPath()
            w.addText(QPointF(0, 0), font, "Aa")
            box = w.boundingRect()
            self._text(p, "Aa", font, (255, 255, 255), r.center().x() - box.width() / 2 - box.left(),
                       r.center().y() + box.height() / 2 - box.bottom() + 1)
        else:
            them, me = color_pair(self.key)
            font = caption_font("clean", 15)
            for i, rgb in enumerate((them, me)):
                y = r.top() + 18 + i * 15
                self._text(p, "Aa", font, rgb, r.left() + 10 + i * 18, y)
        ring = QColor(ACCENT) if self.isChecked() else QColor(255, 255, 255, 40)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(ring, 2.0 if self.isChecked() else 1.0))
        p.drawRoundedRect(r, 9, 9)
        if self.hasFocus():
            p.setPen(QPen(QColor(ACCENT), 1.0, Qt.DotLine))
            p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 10, 10)
        p.end()


class StyleTiles(QWidget):
    """A row of tiles: kind "font" (captions.FONTS), "colors" (captions.COLORS) or "size" (captions.SIZES)."""

    picked = Signal(str)

    def __init__(self, kind, on_pick=None, parent=None, compact=False):
        super().__init__(parent)
        self.kind = kind
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        if kind == "font":
            options = [(k, n, 1.0) for k, n in captions.FONTS]
        elif kind == "size":
            options = list(captions.SIZES)
        else:
            options = [(k, n, 1.0) for k, n, _a, _b in captions.COLORS]
        self.tiles = []
        for key, name, scale in options:
            tile = _Tile(kind, key, name, self, compact=compact, scale=scale)
            self._group.addButton(tile)
            tile.clicked.connect(lambda _c=False, k=key: self._pick(k))
            lay.addWidget(tile)
            self.tiles.append(tile)
        lay.addStretch(1)
        self.setAccessibleName({"font": "Caption font", "size": "Caption size"}.get(kind, "Caption colors"))
        if on_pick is not None:
            self.picked.connect(on_pick)
        self.set_value(captions.DEFAULT_SIZE if kind == "size" else options[0][0])

    def value(self):
        for t in self.tiles:
            if t.isChecked():
                return t.key
        return ""

    def set_value(self, key):
        for t in self.tiles:
            t.blockSignals(True)
            t.setChecked(t.key == key)
            t.blockSignals(False)
        if not self.value() and self.tiles:
            self.tiles[0].setChecked(True)
        self.update()

    def _pick(self, key):
        self.set_value(key)
        self.picked.emit(key)


__all__ = ["StyleTiles"]
