"""The hourly "Still recording" card (Gusan 2.1.11).

A meeting that is still being recorded after every full hour (pauses not counted) asks once: "Still
recording · 1 h" (2 h, ...) with the meeting's name, **Stop** and **Keep recording**. Keep recording closes
the card until the next full hour; Stop stops the meeting (its notes are written as after any Stop).
A recording nobody stops by mistake (a call that ended, a laptop left on) no longer runs for hours.

    card = StillRecordingCard(main_app=app)
    card.stop_clicked.connect(...)            # the meeting stops (AppController._on_still_stop)
    card.keep_clicked.connect(...)            # nothing: it asks again at the next full hour
    card.show_hours(2, "Weekly sync", anchor=tray.geometry())

Like the calendar reminder (ui/floating/meeting_reminder.py, whose glass plate, corner and screen-share
switch it shares): a frameless, always-on-top Tool window that never takes focus by itself, in the
bottom-right corner above the taskbar (top-right under the menu bar on a Mac) - above the calendar
reminder when that one is up - and out of screen capture while Settings > Privacy hides floating bars.
It shows whether or not Gusan's window is open; AppController also sends a system notification (the
tray's on Windows, Notification Center on a Mac) whose click gives the card the keyboard. Colours are
theme tokens only (the plate's two looks come from meeting_reminder).
"""
import logging

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QBrush, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui import glass, theme
from ui.components import Button, icons
from ui.floating import meeting_reminder as mr

logger = logging.getLogger("transcribe")

CARD_W = 320


def heading(hours):
    """"Still recording · 1 h"."""
    return "Still recording · %d h" % max(1, int(hours))


class StillRecordingCard(QWidget):
    """See the module docstring."""

    stop_clicked = Signal()
    keep_clicked = Signal()

    # the calendar reminder's plate, shadow and corner
    plate_rect = mr.MeetingReminder.plate_rect
    _shadow_image = mr.MeetingReminder._shadow_image
    _screen = mr.MeetingReminder._screen

    def __init__(self, main_app=None, parent=None):
        super().__init__(parent)
        self.app = main_app
        self._anchor = None
        self._hours = 0
        self._kbd_focus = False
        self._shadow_key = None
        self._shadow_img = None
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setWindowTitle("Still recording")
        self.setAccessibleName("Still recording")
        theme.load_fonts()
        self.setFont(theme.font(14))
        theme.use_stylesheet(self)
        theme.on_change(self, self._restyle)
        self._build()
        self.setFixedWidth(CARD_W + 2 * mr.SH_SIDE)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(mr.SH_SIDE + mr.PAD_L, mr.SH_TOP + mr.PAD_T, mr.SH_SIDE + mr.PAD_R,
                                mr.SH_BOTTOM + mr.PAD_B + 6)
        root.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.logo = QLabel(self)
        self.logo.setPixmap(icons.logo_pixmap(28))
        self.logo.setFixedSize(28, 28)
        self.logo.setAccessibleName("Gusan")
        head.addWidget(self.logo, 0, Qt.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.dot = QLabel(self)
        self.dot.setPixmap(mr.rec_dot_pixmap(8, 0))
        self.dot.setFixedSize(8, 8)
        top.addWidget(self.dot, 0, Qt.AlignVCenter)
        self.title_label = QLabel("", self)
        theme.set_role(self.title_label, "h3")
        self.title_label.setFont(theme.font(16, 700, display=True))
        top.addWidget(self.title_label, 1)
        words.addLayout(top)
        self.meeting_label = mr._ElidedLabel("", self)
        theme.set_role(self.meeting_label, "muted")
        self.meeting_label.setFixedHeight(18)
        words.addWidget(self.meeting_label)
        head.addLayout(words, 1)
        root.addLayout(head)

        btns = QHBoxLayout()
        btns.setContentsMargins(38, 0, 0, 0)
        btns.setSpacing(8)
        self.stop_button = Button("Stop", "danger", "s", icon="stop", parent=self)
        self.stop_button.clicked.connect(lambda _=False: self._on_stop())
        btns.addWidget(self.stop_button)
        self.keep_button = Button("Keep recording", "secondary", "s", parent=self)
        self.keep_button.clicked.connect(lambda _=False: self._on_keep())
        btns.addWidget(self.keep_button)
        btns.addStretch(1)
        root.addLayout(btns)
        self.setTabOrder(self.stop_button, self.keep_button)

    # ── state ──
    def hours(self):
        return self._hours

    def title_text(self):
        return self.title_label.text()

    def meeting_text(self):
        return self.meeting_label.full_text()

    def _cfg(self):
        cfg = getattr(self.app, "cfg", None)
        return cfg if isinstance(cfg, dict) else {}

    def _restyle(self):
        self._shadow_key = None
        self.dot.setPixmap(mr.rec_dot_pixmap(8, 0))
        self.update()

    def on_setting_changed(self, key):
        if key == mr.PRIVATE_KEY and self.isVisible():
            self._apply_private()

    # ── showing ──
    def show_hours(self, hours, title="", anchor=None):
        """Fill for ``hours`` of recording of the meeting ``title`` and show it (no focus taken)."""
        self._hours = max(1, int(hours))
        self.title_label.setText(heading(self._hours))
        self.meeting_label.set_full_text((title or "").strip() or "Your meeting")
        self.setAccessibleDescription("%s. %s" % (self.title_text(), self.meeting_text()))
        self._anchor = anchor
        lay = self.layout()
        lay.activate()
        self.setFixedHeight(max(lay.sizeHint().height(), lay.minimumSize().height()))
        self._shadow_key = None
        self.place()
        if mr._native_windows():
            self.winId()
            glass.set_no_activate(self)
        self.show()
        self.raise_()
        self._apply_private()

    def target_pos(self, screen=None):
        """The calendar reminder's corner - above it (below it on a Mac) when that one is up."""
        pos = mr.MeetingReminder.target_pos(self, screen)
        other = getattr(self.app, "_reminder", None) if self.app is not None else None
        try:
            if other is not None and other.isVisible():
                shift = other.height() - mr.SH_TOP
                pos.setY(pos.y() + shift if glass.IS_MAC else pos.y() - shift)
        except RuntimeError:
            pass
        return pos

    def place(self):
        self.move(self.target_pos())

    def _apply_private(self):
        want = bool(self._cfg().get(mr.PRIVATE_KEY, True))
        try:
            if mr._native_windows():
                if want and glass.capture_exclusion_supported() and not glass.is_remote_session():
                    glass.exclude_from_capture(self, True)
                else:
                    glass.exclude_from_capture(self, False)
            elif mr._native_mac():
                glass.exclude_from_capture(self, want)
        except Exception:
            logger.debug("still-recording capture exclusion failed", exc_info=True)

    def hide_card(self):
        self._kbd_focus = False
        self.hide()

    # ── actions ──
    def _on_stop(self):
        self.hide_card()
        self.stop_clicked.emit()

    def _on_keep(self):
        self.hide_card()
        self.keep_clicked.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._on_keep()
            return
        super().keyPressEvent(e)

    def take_focus(self):
        """The notification was clicked (or the tray icon): the keyboard reaches Stop / Keep recording."""
        if not self.isVisible():
            return
        if mr._native_windows():
            glass.set_no_activate(self, False)
        self._kbd_focus = True
        self.raise_()
        self.activateWindow()
        if self.isActiveWindow():
            self._give_keyboard_focus()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.ActivationChange and self._kbd_focus and self.isActiveWindow():
            self._give_keyboard_focus()

    def _give_keyboard_focus(self):
        self._kbd_focus = False
        self.keep_button.setFocus(Qt.TabFocusReason)
        theme.set_prop(self.keep_button, "focusVisible", self.keep_button.hasFocus(), children=False)

    # ── painting: the calendar reminder's solid plate ──
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        plate = self.plate_rect()
        path = QPainterPath()
        path.addRoundedRect(plate, mr.RADIUS, mr.RADIUS)
        p.drawImage(0, 0, self._shadow_image())
        p.fillPath(path, mr._plate("solid"))
        r0, r1, r2 = mr._plate("rim")
        rim = QLinearGradient(plate.topLeft(), plate.bottomRight())
        rim.setColorAt(0.0, r0)
        rim.setColorAt(0.5, r1)
        rim.setColorAt(1.0, r2)
        p.save()
        p.setClipPath(path)
        p.setPen(QPen(QBrush(rim), 3.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.restore()
        p.setPen(QPen(mr._plate("edge"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()


def is_showing(card):
    try:
        return bool(card is not None and card.isVisible())
    except RuntimeError:
        return False


__all__ = ["StillRecordingCard", "heading", "is_showing"]
