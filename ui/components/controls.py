"""Form controls: Toggle (switch), Keycaps, Segmented."""

import sys

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QWidget)

from ui import theme


class Toggle(QAbstractButton):
    """The mockups' switch: a 40 x 22 track, blue when on, with an 18 px knob.

    A real checkable button (``toggled(bool)`` / ``isChecked()``), so it works
    with the keyboard (Tab to it, Space or Enter flips it) and screen readers
    read its accessible name and on/off state. The widget is 44 x 26: 2 px
    around the track for the keyboard focus ring."""

    TRACK_W, TRACK_H, KNOB = 40, 22, 18
    PAD = 2

    def __init__(self, accessible_name, checked=False, parent=None):
        super().__init__(parent)
        if not (accessible_name or "").strip():
            raise ValueError("Toggle needs an accessible name")
        self.setCheckable(True)
        self.setChecked(bool(checked))
        self.setAccessibleName(accessible_name)
        self.setFixedSize(self.sizeHint())
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_Hover, True)      # repaint on enter / leave

    def sizeHint(self):
        return QSize(self.TRACK_W + 2 * self.PAD, self.TRACK_H + 2 * self.PAD)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e):
        self._kbd_focus = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._kbd_focus = False
        super().focusOutEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        on = self.isChecked()
        x0 = y0 = float(self.PAD)
        track = QRectF(x0 + 0.5, y0 + 0.5, self.TRACK_W - 1, self.TRACK_H - 1)
        if not self.isEnabled():
            p.setOpacity(0.5)
        fill = theme.qcolor("blue" if on else "line2")
        edge = theme.qcolor("blue-h" if on else "line2")
        if self.underMouse() and self.isEnabled():
            fill = fill.darker(108)
        p.setPen(QPen(edge, 1))
        p.setBrush(fill)
        p.drawRoundedRect(track, self.TRACK_H / 2.0 - 0.5, self.TRACK_H / 2.0 - 0.5)
        # Knob: 1 px border + 18 px margin-left when on, 2 px when off (CSS).
        kx = x0 + 1 + (18 if on else 2)
        ky = y0 + (self.TRACK_H - self.KNOB) / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 23, 42, 40))                    # 0 1px 2px rgba(15,23,42,.25)
        p.drawEllipse(QRectF(kx, ky + 1, self.KNOB, self.KNOB))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(kx, ky, self.KNOB, self.KNOB))
        if self.hasFocus() and getattr(self, "_kbd_focus", False):
            p.setOpacity(1.0)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(theme.qcolor("blue-l"), 2))
            ring = QRectF(1, 1, self.width() - 2, self.height() - 2)
            p.drawRoundedRect(ring, ring.height() / 2.0, ring.height() / 2.0)
        p.end()


def keycap_labels(hotkey):
    """'alt+r' -> ['Alt', 'R'] (Windows); on a Mac the modifier symbols ⌥ ⌘ …"""
    import hotkeys
    hk = (hotkey or "").strip()
    if not hk:
        return []
    if hk.lower().startswith("mouse:"):
        return [hotkeys.display(hk)]
    if sys.platform == "darwin":
        out = []
        ps = hotkeys.parts(hk)
        mods = {("cmd" if p in ("win", "super") else p) for p in ps if p in hotkeys.MODIFIERS}
        for m in ("ctrl", "alt", "shift", "cmd"):
            if m in mods:
                out.append(hotkeys.display(m + "+x").replace("X", "").strip() or m)
        out += [hotkeys.display(k) for k in ps if k not in hotkeys.MODIFIERS]
        return out
    return [s.strip() for s in hotkeys.display(hk, mac=False).split(" + ") if s.strip()]


class Keycap(QLabel):
    """One key: small (24 px, white, slate edge) or big (56 px, display face)."""

    def __init__(self, text, big=False, parent=None):
        super().__init__(text, parent)
        self.setProperty("gusan", "kbd")
        self.setProperty("big", bool(big))
        self.setAlignment(Qt.AlignCenter)
        if big:
            self.setFont(theme.font(22, 700, display=True))
            self.setMinimumWidth(56)
            self.setFixedHeight(56)
            theme.elevate(self, "key")
        else:
            self.setFont(theme.font(12, 700))
            self.setMinimumWidth(24)
            self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)


class Keycaps(QWidget):
    """Keys joined by '+': Keycaps(["Alt", "R"]) or Keycaps.from_hotkey("alt+r")."""

    def __init__(self, keys=(), big=False, parent=None):
        super().__init__(parent)
        self._big = bool(big)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(8 if big else 4)
        self.set_keys(keys)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    @classmethod
    def from_hotkey(cls, hotkey, big=False, parent=None):
        return cls(keycap_labels(hotkey), big=big, parent=parent)

    def set_hotkey(self, hotkey):
        self.set_keys(keycap_labels(hotkey))

    def set_keys(self, keys):
        while self._lay.count():
            it = self._lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()            # deleteLater waits for the event loop; don't paint the old key meanwhile
                w.deleteLater()
        keys = list(keys)
        for i, k in enumerate(keys):
            if i:
                plus = QLabel("+", self)
                plus.setProperty("gusan", "kbdPlus")
                plus.setFont(theme.font(20 if self._big else 14, 700))
                plus.setAccessibleName("plus")
                self._lay.addWidget(plus, 0, Qt.AlignVCenter)
            self._lay.addWidget(Keycap(k, big=self._big, parent=self), 0, Qt.AlignVCenter)
        self.setAccessibleName(" + ".join(keys))
        self.setToolTip(" + ".join(keys))


class Segmented(QFrame):
    """Pill segmented control. Emits ``changed(index)`` when the user picks one."""

    changed = Signal(int)

    def __init__(self, options, selected=0, accessible_name="View", size="m", parent=None):
        super().__init__(parent)
        h = 30 if size == "s" else 34
        self.setProperty("gusan", "segmented")
        self.setProperty("gsize", size)  # not "size": QWidget already has a size property
        self.setFixedHeight(h)
        self.setAccessibleName(accessible_name)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = []
        for i, text in enumerate(options):
            b = QPushButton(text, self)
            b.setProperty("gusan", "seg")
            b.setCheckable(True)
            b.setFixedHeight(h - 6)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.TabFocus)
            theme.focus_ring(b)
            b.setAccessibleName(text)
            self._group.addButton(b, i)
            lay.addWidget(b)
            self._buttons.append(b)
        self._group.idClicked.connect(self._on_clicked)
        self.set_index(selected, emit=False)

    def _on_clicked(self, idx):
        self._restyle()
        self.changed.emit(idx)

    def _restyle(self):
        for b in self._buttons:
            on = b.isChecked()
            b.setFont(theme.font(13, 600 if on else 500))
            theme.elevate(b, "item" if on else None)

    def index(self):
        return self._group.checkedId()

    def set_index(self, idx, emit=False):
        if 0 <= idx < len(self._buttons):
            self._buttons[idx].setChecked(True)
            self._restyle()
            if emit:
                self.changed.emit(idx)

    def buttons(self):
        return list(self._buttons)
