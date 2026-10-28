"""Delete straight from a list: the small pieces every list of recordings shares.

A row shows a little trash icon at its right end while the pointer is over it (it fades in, 150 ms),
quietly (40 %) while the row has the keyboard or the window is narrow (a touch screen has no
hover), and never otherwise. Pressing it asks first (ui/components/mini_prompt.py ``confirm``) and
never opens the row. One component, so the Meetings list, the Dictation list, Audio to text and the
Live Assistant's past sessions look and behave the same:

    btn = row_actions.attach_trash(row, row_layout, "Q4 roadmap", on_press)   # an icon at the layout's end
    btn.set_blocked("Recording now")          # greyed, with an honest tooltip (nothing to press)
    btn.set_blocked("")                       # back to normal

``ActionBar`` is the calm strip under a list's header: "3 selected - Delete - Cancel", and the
"Deleted 3 meetings - Undo" line (``UndoBar``).
"""

import logging

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPointF, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QSizePolicy, QToolButton, QWidget)

from ui import theme
from ui.components import icons
from ui.components.buttons import Button, IconButton

logger = logging.getLogger("transcribe")

FADE_MS = 150
REST_OPACITY = 0.4            # a row with the keyboard on it, and narrow windows (no hover on a touch screen)
NARROW_BELOW = 520            # a row narrower than this keeps its icon at REST_OPACITY
ANIMATE = True                # tests switch the fade off to read the final opacity at once


class TrashButton(QToolButton):
    """A 28 px trash icon. Not a Tab stop (a list of 50 rows would need 100 Tabs): the keyboard's way
    to delete is the Delete key on the row; this button is the pointer's, and a screen reader finds it
    by its name, "Delete <name>"."""

    def __init__(self, name="", parent=None, size=28):
        super().__init__(parent)
        self.setProperty("gusan", "trashBtn")
        self.setFixedSize(size, size)
        self.setIconSize(QSize(16, 16))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAutoRaise(True)
        ic = QIcon()
        ic.addPixmap(icons.pixmap("trash", 16, "muted", sw=1.8), QIcon.Normal)
        ic.addPixmap(icons.pixmap("trash", 16, "red-tx", sw=1.8), QIcon.Active)
        ic.addPixmap(icons.pixmap("trash", 16, "faint", sw=1.8), QIcon.Disabled)
        self.setIcon(ic)
        sp = self.sizePolicy()
        sp.setRetainSizeWhenHidden(True)             # its place stays: a row never jumps when it fades
        self.setSizePolicy(sp)
        self._name = ""
        self._reason = ""
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._after_fade)
        self._target = 0.0
        self.set_name(name)
        self.hide()

    # ── words ──
    def set_name(self, name):
        self._name = str(name or "")
        self._words()

    def _words(self):
        who = self._name or "this item"
        self.setAccessibleName("Delete %s" % who)
        self.setToolTip(self._reason or "Delete")

    def set_blocked(self, reason):
        """A reason (plain words: "Recording now") greys the icon out and says why; "" frees it."""
        self._reason = str(reason or "")
        self.setEnabled(not self._reason)
        self._words()
        if self._reason:
            self.setAccessibleDescription(self._reason)
        else:
            self.setAccessibleDescription("")

    def blocked(self):
        return self._reason

    # ── the fade ──
    def target_opacity(self):
        return self._target

    def shown_opacity(self):
        return self._effect.opacity() if self.isVisible() else 0.0

    def fade_to(self, value, animate=None):
        """Fade to ``value`` (0 hides it - its place stays). Quick calls with the same value do nothing."""
        value = max(0.0, min(1.0, float(value)))
        if value == self._target and (value == 0.0 or self.isVisibleTo(self.parentWidget())):
            return
        self._target = value
        animate = ANIMATE if animate is None else animate
        self._anim.stop()
        if value > 0.0:
            self.show()
        if not animate or not self.isVisible():
            self._effect.setOpacity(value)
            self._after_fade()
            return
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(value)
        self._anim.start()

    def _after_fade(self):
        if self._target == 0.0 and self._effect.opacity() <= 0.01:
            self.hide()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.EnabledChange and self._target > 0.0:
            self.update()


class RowReveal(QObject):
    """Drives a row's TrashButton: 100 % while the pointer is on the row, 40 % while the row has the
    keyboard (or the row is narrow), hidden otherwise. Lives and dies with the row."""

    def __init__(self, row, button, narrow_below=NARROW_BELOW):
        super().__init__(row)
        self.row = row
        self.button = button
        self.narrow_below = narrow_below
        self.enabled = True                     # False (while a list is selecting): the icon stays away
        self._over = False
        row.installEventFilter(self)
        button.installEventFilter(self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.refresh)

    def target(self):
        b, row = self.button, self.row
        if not self.enabled:
            return 0.0
        if b.blocked() and not (self._over or row.underMouse()):
            return REST_OPACITY if self._focused() or self._narrow() else 0.0
        if self._over or row.underMouse() or b.underMouse():
            return 1.0
        if self._focused() or self._narrow():
            return REST_OPACITY
        return 0.0

    def _focused(self):
        """The keyboard is on the row. A row that follows ``theme.focus_ring`` says so itself
        (``focusVisible``: Tab, never the window's first button taking focus as it opens)."""
        row = self.row
        try:
            fw = QApplication.focusWidget()
            inside = row.hasFocus() or (fw is not None and row.isAncestorOf(fw))
            if not inside:
                return False
            visible = row.property("focusVisible")
            if visible is None and fw is not None:
                visible = fw.property("focusVisible")
            return True if visible is None else bool(visible)
        except RuntimeError:
            return False

    def _narrow(self):
        return 0 < self.row.width() < self.narrow_below

    def refresh(self, animate=None):
        try:
            self.button.fade_to(self.target(), animate)
        except RuntimeError:
            pass                                          # the row was deleted meanwhile

    def eventFilter(self, obj, ev):
        t = ev.type()
        if obj is self.row:
            if t == QEvent.Enter:
                self._over = True
            elif t in (QEvent.Leave, QEvent.Hide):
                self._over = False
            elif t not in (QEvent.FocusIn, QEvent.FocusOut, QEvent.Resize, QEvent.Show, QEvent.HoverEnter,
                           QEvent.HoverLeave, QEvent.WindowActivate, QEvent.WindowDeactivate):
                return False
            self._timer.start()
        elif obj is self.button and t in (QEvent.Enter, QEvent.Leave):
            self._timer.start()
        return False


def attach_trash(row, layout, name, slot, *, size=28, index=None):
    """Put a TrashButton at the end of ``layout`` (or at ``index``) in ``row`` and wire its reveal.
    ``slot()`` runs when it is pressed. Returns the button (``.reveal`` is its RowReveal)."""
    btn = TrashButton(name, row, size)
    if index is None:
        layout.addWidget(btn, 0, Qt.AlignVCenter)
    else:
        layout.insertWidget(index, btn, 0, Qt.AlignVCenter)
    btn.clicked.connect(lambda _checked=False: slot())
    btn.reveal = RowReveal(row, btn)
    row.__dict__.setdefault("_trash_reveals", []).append(btn.reveal)      # lives as long as the row's own object
    return btn


class RowCheck(QAbstractButton):
    """The check box in front of a row while a list is selecting. Painted, 18 px; the row takes the
    clicks (so it ignores the mouse)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(18, 18)
        self.setAccessibleName("Select")
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def sizeHint(self):
        return QSize(18, 18)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
        if not self.isEnabled():
            p.setPen(QPen(theme.qcolor("line"), 1.5))
            p.setBrush(theme.qcolor("bg2"))
            p.drawRoundedRect(r, 5, 5)
        elif self.isChecked():
            p.setPen(QPen(theme.qcolor("blue-h"), 1.5))
            p.setBrush(theme.qcolor("blue"))
            p.drawRoundedRect(r, 5, 5)
            p.setPen(QPen(QColor("#ffffff"), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPolyline([QPointF(4.6, 9.4), QPointF(7.6, 12.4), QPointF(13.4, 5.8)])
        else:
            p.setPen(QPen(theme.qcolor("line2"), 1.5))
            p.setBrush(QColor("#ffffff"))
            p.drawRoundedRect(r, 5, 5)
        p.end()


class ActionBar(QFrame):
    """A calm strip: one line of words and a few buttons on the right. ``tone``: info | error."""

    def __init__(self, tone="info", parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "actionBar")
        self.setProperty("tone", tone)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 8, 8)
        lay.setSpacing(8)
        self.message = QLabel("", self)
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        theme.set_role(self.message, "label")
        self.message.setFont(theme.font(13, 600))
        self.message.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self.message, 1, Qt.AlignVCenter)
        self.buttons_layout = lay

    def set_message(self, text, tone=None):
        from ui.components.mini_prompt import breakable          # a name with no spaces wraps instead of being cut
        self.message.setText(breakable(text))
        self.setAccessibleName(str(text or ""))
        if tone is not None and tone != self.property("tone"):
            theme.set_prop(self, "tone", tone)

    def add_button(self, text, kind="ghost", slot=None, size="s"):
        b = Button(text, kind, size, parent=self)
        if slot is not None:
            b.clicked.connect(lambda _=False: slot())
        self.buttons_layout.addWidget(b, 0, Qt.AlignVCenter)
        return b

    def add_close(self, slot, name="Dismiss"):
        b = IconButton("x", name, size=28, icon_size=14, color="muted", parent=self)
        b.clicked.connect(lambda _=False: slot())
        self.buttons_layout.addWidget(b, 0, Qt.AlignVCenter)
        return b


class SelectionBar(ActionBar):
    """"3 selected - Select all - Delete - Cancel": the bar over a list while it is selecting."""

    all_clicked = Signal()
    delete_clicked = Signal()
    cancel_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__("info", parent)
        self.all_button = self.add_button("Select all", "link", self.all_clicked.emit)
        self.delete_button = self.add_button("Delete", "danger", self.delete_clicked.emit)
        self.cancel_button = self.add_button("Cancel", "ghost", self.cancel_clicked.emit)
        self.set_count(0)
        self.hide()

    def set_count(self, n):
        self.set_message("%d selected" % n if n else "Choose the meetings to delete", "info")
        self.delete_button.setEnabled(n > 0)
        self.delete_button.setAccessibleName("Delete %d selected" % n if n else "Delete")


class UndoBar(ActionBar):
    """"Deleted 3 meetings - Undo" (and a close button). The owner starts the timer.

    ``dock(page)`` makes it a toast: it floats over the bottom of ``page`` - a widget that does not
    scroll - instead of sitting in a layout at the top of the list. A list is long and the person
    deletes from wherever they have scrolled to: a bar at the list's top would be out of sight, and an
    Undo nobody can see is no Undo."""

    undo = Signal()
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__("info", parent)
        self._host = None
        self._margin = 24
        self._max_width = 560
        self.undo_button = self.add_button("Undo", "secondary", self.undo.emit)
        self.undo_button.setAccessibleName("Undo")
        self.close_button = self.add_close(self.closed.emit)
        self.hide()

    def dock(self, host, margin=24, max_width=560):
        """Float over the bottom of ``host`` (centred, at most ``max_width`` wide)."""
        self._host, self._margin, self._max_width = host, int(margin), int(max_width)
        self.setParent(host)
        theme.set_prop(self, "docked", "true")             # opaque white with a shadow: it floats over rows
        theme.elevate(self, "lg")
        host.installEventFilter(self)
        self.hide()

    def docked(self):
        return self._host is not None

    def place(self):
        host = self._host
        if host is None or host.width() < 120:
            return
        w = max(260, min(self._max_width, host.width() - 2 * self._margin))
        lay = self.layout()
        self.setFixedWidth(w)
        lay.activate()
        h = lay.totalHeightForWidth(w) if lay.hasHeightForWidth() else lay.sizeHint().height()
        self.setFixedHeight(max(h, 44))
        self.move(max(0, (host.width() - w) // 2), max(0, host.height() - self.height() - self._margin))
        self.raise_()

    def set_message(self, text, tone=None):
        super().set_message(text, tone)
        if self._host is not None:
            self.place()

    def showEvent(self, e):
        super().showEvent(e)
        self.place()

    def eventFilter(self, obj, ev):
        if obj is self._host and ev.type() == QEvent.Resize and self.isVisible():
            self.place()
        return False
