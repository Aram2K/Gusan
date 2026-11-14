"""The live captions' look on the Meetings card, small (founder 2026-11-14: the two rows of dark tiles and the
stacked slider were "taking too much space", the tiles "a bit weird", the slider "broken and cut a bit"):

  * Style - ONE line: the font, a dropdown like the card's other settings (captions.FONTS, each named in its own
    font in the list), and the colours, four small dots (captions.COLORS: the call's colour on a dot's left half,
    yours on the right; the picked one has a ring).
  * Size - a slider on one line (founder 2026-11-14: "maybe a slider, must be more clear"): a small "A", the slider,
    a big "A" - the usual text-size control - in steps of captions.SIZE_STEP percent, with the percent beside it. It
    sits on the Meetings card and, compact, on the live card while a meeting runs.

    font = FontCombo(on_pick=lambda key: ...)
    font.set_value("bold")
    colors = ColorDots(on_pick=lambda key: ...)
    size = SizeSlider(on_pick=lambda percent: ...)
    size.set_value(120)
"""
from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QButtonGroup, QComboBox, QHBoxLayout, QLabel, QSlider, QWidget

import captions

ACCENT = "#2563eb"                    # theme TOKENS "blue"
SAVE_AFTER_MS = 150                   # the size is saved once the slider rests
DOT = 22                              # px: a colour dot's button (the dot itself is DOT_FILL across)
DOT_FILL = 14
DOT_EDGE = QColor(100, 116, 139)      # its thin edge (white must show on the white card)
HANDLE = 18                           # px: the slider's round handle, edge included


class FontCombo(QComboBox):
    """The caption font as a dropdown. ``picked(key)`` when one is picked."""

    picked = Signal(str)

    def __init__(self, on_pick=None, parent=None):
        super().__init__(parent)
        try:
            from ui import theme
            self.setFont(theme.font(13.5))
        except Exception:
            pass
        self.setFixedHeight(36)
        from ui.captions_overlay import caption_font
        for key, name in captions.FONTS:
            self.addItem(name, key)
            self.setItemData(self.count() - 1, caption_font(key, 14), Qt.FontRole)       # the list shows each one
        self.setAccessibleName("Caption font")
        self.setToolTip("Caption font")
        self.activated.connect(lambda _i: self.picked.emit(self.value()))
        if on_pick is not None:
            self.picked.connect(on_pick)

    def value(self):
        return str(self.currentData() or "")

    def set_value(self, key):
        i = self.findData(key)
        self.blockSignals(True)
        self.setCurrentIndex(i if i >= 0 else 0)
        self.blockSignals(False)


class _Dot(QAbstractButton):
    """One colour pair: a small round dot, the call's colour on its left half, yours on the right."""

    def __init__(self, key, name, parent=None):
        super().__init__(parent)
        self.key = key
        self.setCheckable(True)
        self.setFixedSize(DOT, DOT)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(name)
        self.setToolTip("%s: the call, then you" % name)

    def sizeHint(self):
        return QSize(DOT, DOT)

    def paintEvent(self, _e):
        from ui.captions_overlay import color_pair
        them, me = color_pair(self.key)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = QRectF(self.rect()).center()
        r = QRectF(c.x() - DOT_FILL / 2.0, c.y() - DOT_FILL / 2.0, DOT_FILL, DOT_FILL)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(*them))
        p.drawPie(r, 90 * 16, 180 * 16)                             # the left half
        p.setBrush(QColor(*me))
        p.drawPie(r, -90 * 16, 180 * 16)                            # the right half
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(DOT_EDGE, 1.0))
        p.drawEllipse(r)
        if self.isChecked() or self.hasFocus():
            ring = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
            p.setPen(QPen(QColor(ACCENT), 2.0 if self.isChecked() else 1.0,
                          Qt.SolidLine if self.isChecked() else Qt.DotLine))
            p.drawEllipse(ring)
        p.end()


class ColorDots(QWidget):
    """The caption colours: one dot a pair (captions.COLORS). ``picked(key)`` when one is clicked."""

    picked = Signal(str)

    def __init__(self, on_pick=None, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.dots = []
        for key, name, _them, _me in captions.COLORS:
            dot = _Dot(key, name, self)
            self._group.addButton(dot)
            dot.clicked.connect(lambda _c=False, k=key: self._pick(k))
            lay.addWidget(dot)
            self.dots.append(dot)
        self.setAccessibleName("Caption colors")
        if on_pick is not None:
            self.picked.connect(on_pick)
        self.set_value(captions.COLORS[0][0])

    def value(self):
        for d in self.dots:
            if d.isChecked():
                return d.key
        return ""

    def set_value(self, key):
        """Pick ``key`` (the first pair when it is not one of them). One dot is always picked: the group lets a
        dot be unpicked only by picking another."""
        target = next((d for d in self.dots if d.key == key), self.dots[0])
        target.blockSignals(True)
        target.setChecked(True)
        target.blockSignals(False)
        for d in self.dots:
            d.update()

    def _pick(self, key):
        self.set_value(key)
        self.picked.emit(key)


def _slider_style():
    try:
        from ui import theme
        t = theme.TOKENS
        line, blue, white = t["line2"], t["blue"], t["white"]
    except Exception:
        line, blue, white = "#cbd5e1", ACCENT, "#ffffff"
    return f"""
QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: {line}; }}
QSlider::sub-page:horizontal {{ height: 4px; border-radius: 2px; background: {blue}; }}
QSlider::add-page:horizontal {{ height: 4px; border-radius: 2px; background: {line}; }}
QSlider::handle:horizontal {{ width: {HANDLE - 4}px; margin: -{(HANDLE - 4) // 2}px 0; border-radius: {HANDLE // 2}px;
                              background: {white}; border: 2px solid {blue}; }}
QSlider:focus {{ outline: none; }}
"""


class SizeSlider(QWidget):
    """Caption size: a small "A", a slider, a big "A" and the percent (captions.SIZE_MIN..SIZE_MAX, in SIZE_STEP
    steps; 100 = the size captions always had). ``picked(percent)`` once the slider rests (SAVE_AFTER_MS)."""

    picked = Signal(int)

    def __init__(self, on_pick=None, parent=None, compact=False):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        try:
            from ui import theme
            small_font, big_font, pct_font = theme.font(11, 600), theme.font(17 if compact else 19, 600), theme.font(12)
        except Exception:
            small_font = big_font = pct_font = None
        self.small = QLabel("A", self)
        self.big = QLabel("A", self)
        for lab, f in ((self.small, small_font), (self.big, big_font)):
            if f is not None:
                lab.setFont(f)
        self.small.setToolTip("Smaller")
        self.big.setToolTip("Bigger")
        self.slider = QSlider(Qt.Horizontal, self)
        self.slider.setRange(captions.SIZE_MIN // captions.SIZE_STEP, captions.SIZE_MAX // captions.SIZE_STEP)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(1)
        self.slider.setFocusPolicy(Qt.StrongFocus)
        self.slider.setCursor(Qt.PointingHandCursor)
        self.slider.setAccessibleName("Caption size")
        self.slider.setStyleSheet(_slider_style())
        # Room for the whole handle: it stands HANDLE px tall around a 4 px groove, and a slider's own height is less
        # (founder 2026-11-14: "the slider is ... broken and cut a bit" - its top and bottom were clipped).
        self.slider.setFixedHeight(HANDLE + 6)
        self.slider.setMinimumWidth(90 if compact else 150)
        self.pct = QLabel("", self)
        if pct_font is not None:
            self.pct.setFont(pct_font)
        self.pct.setMinimumWidth(self.pct.fontMetrics().horizontalAdvance("180%") + 2)
        lay.addWidget(self.small, 0, Qt.AlignVCenter)
        lay.addWidget(self.slider, 1, Qt.AlignVCenter)
        lay.addWidget(self.big, 0, Qt.AlignVCenter)
        lay.addWidget(self.pct, 0, Qt.AlignVCenter)
        self.pct.setVisible(not compact)
        self.setAccessibleName("Caption size")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_AFTER_MS)
        self._timer.timeout.connect(lambda: self.picked.emit(self.value()))
        self.slider.valueChanged.connect(self._moved)
        if on_pick is not None:
            self.picked.connect(on_pick)
        self.set_value(captions.DEFAULT_SIZE)

    def value(self):
        return int(self.slider.value() * captions.SIZE_STEP)

    def set_value(self, value):
        """Show the saved size - not while the slider is in the person's hand (held, or moved and not saved yet): a
        refresh of the page in that moment would pull it back to the old size."""
        if self.slider.isSliderDown() or self._timer.isActive():
            return
        pct = captions.size_percent(value)
        self.slider.blockSignals(True)
        self.slider.setValue(pct // captions.SIZE_STEP)
        self.slider.blockSignals(False)
        self._show(pct)

    def _show(self, pct):
        self.pct.setText("%d%%" % pct)
        tip = "Caption size: %d%%%s" % (pct, " (as it always was)" if pct == captions.DEFAULT_SIZE else "")
        self.slider.setToolTip(tip)
        self.slider.setAccessibleDescription(tip)

    def _moved(self, _step):
        pct = self.value()
        self._show(pct)
        self._timer.start()


__all__ = ["ColorDots", "FontCombo", "SizeSlider"]
