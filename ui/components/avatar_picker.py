"""Choosing an avatar (Account & plan): the avatar shows a pencil when the pointer is over it (or it has the
keyboard), and a click opens the picker under it - six letter colours, six pictures, and their own photo.
Each pick shows on every avatar at once (ui/components/avatars.py) and is saved in cfg ``avatar``.

    AvatarButton(name, size, parent)    an Avatar that can be pressed: ``clicked``
    AvatarPicker(name, parent)          the choices: ``chosen(value)``, ``upload_requested``, ``done``
"""

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui import theme
from ui.components import avatars, icons
from ui.components.buttons import Button
from ui.components.sidebar import Avatar

EDIT_NAME = "Change your avatar"


class AvatarButton(Avatar):
    """The avatar, pressable: a pencil over a dimmed circle while the pointer is on it or it has the keyboard."""
    clicked = Signal()

    def __init__(self, name="", size=44, parent=None):
        super().__init__(name, size, parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setToolTip(EDIT_NAME)
        self.setAccessibleName(EDIT_NAME)
        self._hover = False
        self._down = False

    def editing_shown(self):
        """Is the pencil showing (the pointer is on it, or it has the keyboard)?"""
        return self._hover or self.hasFocus()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._down = False
        self.update()
        super().leaveEvent(e)

    def focusInEvent(self, e):
        self.update()
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self.update()
        super().focusOutEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._down = True
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._down:
            self._down = False
            if self.rect().contains(e.position().toPoint()):
                self.clicked.emit()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.clicked.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self.editing_shown():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, self._size - 1, self._size - 1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 23, 42, 170))
        p.drawEllipse(r)
        isz = max(10, int(round(self._size * 0.4)))
        p.drawPixmap(int(round((self._size - isz) / 2.0)), int(round((self._size - isz) / 2.0)),
                     icons.pixmap("pen", isz, "#ffffff", sw=2.0))
        if self.hasFocus():
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(theme.qcolor("blue"), 2))
            p.drawEllipse(QRectF(1, 1, self._size - 2, self._size - 2))
        p.end()


class _Choice(QAbstractButton):
    """One choice in the picker: the avatar, a blue ring when it is the one shown."""
    SIZE = 36

    def __init__(self, value, letter, parent=None):
        super().__init__(parent)
        self.value = value
        self._letter = letter
        self._selected = False
        self.setCheckable(False)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedSize(self.SIZE + 8, self.SIZE + 8)
        self.setAttribute(Qt.WA_Hover, True)
        self._name()

    def _name(self):
        name = avatars.label(self.value)
        self.setAccessibleName(name + (", chosen" if self._selected else ""))
        self.setToolTip(name)

    def set_selected(self, on):
        if bool(on) != self._selected:
            self._selected = bool(on)
            self._name()
            self.update()

    def is_selected(self):
        return self._selected

    def sizeHint(self):
        return QSize(self.SIZE + 8, self.SIZE + 8)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        avatars.paint(p, QRectF(4, 4, self.SIZE, self.SIZE), self._letter, value=self.value)
        ring = None
        if self._selected:
            ring = QPen(theme.qcolor("blue"), 2)
        elif self.underMouse() or self.hasFocus():
            ring = QPen(theme.qcolor("line2") if not self.hasFocus() else theme.qcolor("blue"), 1.5)
        if ring is not None:
            p.setBrush(Qt.NoBrush)
            p.setPen(ring)
            p.drawEllipse(QRectF(1, 1, self.SIZE + 6, self.SIZE + 6))
        p.end()


class AvatarPicker(QWidget):
    """The choices, in the Account card under the avatar."""
    chosen = Signal(str)
    upload_requested = Signal()
    done = Signal()

    COLUMNS = 6

    def __init__(self, name="", parent=None):
        super().__init__(parent)
        self.setAccessibleName("Choose your avatar")
        letter = (name or "?").strip()[:1].upper() or "?"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(8)
        title = QLabel("Choose your avatar", self)
        theme.set_role(title, "muted")
        title.setFont(theme.font(12.5, 600))
        lay.addWidget(title)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(2)
        self.choices = []
        for i, value in enumerate(avatars.PRESETS):
            c = _Choice(value, letter, self)
            c.clicked.connect(lambda _=False, v=value: self._pick(v))
            grid.addWidget(c, i // self.COLUMNS, i % self.COLUMNS)
            self.choices.append(c)
        grid.setColumnStretch(self.COLUMNS, 1)
        lay.addLayout(grid)
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 0)
        row.setSpacing(8)
        self.photo_choice = _Choice(avatars.PHOTO, letter, self)
        self.photo_choice.clicked.connect(lambda _=False: self._pick(avatars.PHOTO))
        self.choices.append(self.photo_choice)
        row.addWidget(self.photo_choice, 0, Qt.AlignVCenter)
        self.upload_button = Button("Upload a photo", "secondary", "s", icon="upload", parent=self)
        self.upload_button.clicked.connect(lambda _=False: self.upload_requested.emit())
        row.addWidget(self.upload_button, 0, Qt.AlignVCenter)
        self.note = QLabel("", self)
        theme.set_role(self.note, "muted")
        self.note.setFont(theme.font(12))
        self.note.setVisible(False)
        row.addWidget(self.note, 1, Qt.AlignVCenter)
        row.addStretch(1)
        self.done_button = Button("Done", "link", "s", parent=self)
        self.done_button.clicked.connect(lambda _=False: self.done.emit())
        row.addWidget(self.done_button, 0, Qt.AlignVCenter)
        lay.addLayout(row)
        self.refresh()

    def _pick(self, value):
        self.chosen.emit(value)
        self.refresh(value)

    def refresh(self, value=None):
        """Ring the one shown; the photo choice only when a photo was uploaded."""
        current = avatars.normalize(value) if value is not None else avatars.choice()
        self.photo_choice.setVisible(avatars.has_photo())
        self.upload_button.setText("Change photo" if avatars.has_photo() else "Upload a photo")
        for c in self.choices:
            c.set_selected(c.value == current)
            c.update()

    def say(self, text):
        self.note.setText(text or "")
        self.note.setVisible(bool(text))
