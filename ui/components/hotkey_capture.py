"""HotkeyCaptureButton: the keys of a shortcut + "Change", which captures a new one.

    btn = HotkeyCaptureButton("alt+r", accessible_name="Dictation shortcut")
    btn.captured.connect(lambda combo: app.set_setting("hotkey", combo))
    btn.refused.connect(row.set_hint)          # "Add Ctrl, Alt, Shift or Win ..."

Idle it shows the keycaps (Keycaps.from_hotkey) and a "Change" link. Clicking
Change (or Space / Enter on it) starts a capture: the keys turn into
"Press keys…" and the link into "Cancel". Then

- a key press with a modifier (Ctrl / Alt / Shift / Win, or ⌘ ⌥ ⌃ ⇧ on a
  Mac), a function key alone, or the middle / back / forward mouse button
  ends it with ``captured(combo)`` in config form ("alt+r", "f9",
  "mouse:middle");
- a bare typing key ("r", "space") is refused: ``refused(message)``, and the
  capture keeps waiting for a real combination;
- Esc, Cancel, a click elsewhere or the window losing focus cancels it
  (``cancelled()``).

The button shows the new keys as soon as it emits ``captured``. The caller
applies the change (``app.set_setting``) and, when that is refused, calls
``set_hotkey(old)`` and shows why - this widget never writes config.
``capturing_changed(bool)`` lets the page pause the global hotkeys while the
user presses keys (see the Dictation page).

Keyboard: the Change link is a normal Tab stop. While capturing, every key
goes to the capture (shortcuts such as ⌘Q / Alt+F4-like mnemonics are
swallowed), so no other control reacts.

Integration notes for Wave 4
----------------------------
* Exported from ui/components/__init__.py (Wave 4): ``from ui.components
  import HotkeyCaptureButton`` (ui.components.hotkey_capture still works).
* The global hotkey hooks still fire while the user presses keys here (on
  Windows the keyboard hook sees Alt+R before Qt does). Give AppController a
  ``suspend_hotkeys(bool)`` (unhook / re-hook, no config change); pages call
  it from ``capturing_changed`` (the Dictation page already does when it
  exists). Live Assistant / Settings / onboarding can use the same button
  with ``app.set_setting("live_assist_hotkey", combo)``.
"""

import logging
import sys

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QSizePolicy, QWidget

from ui import theme
from ui.components.buttons import Button
from ui.components.controls import Keycaps

logger = logging.getLogger("transcribe")

IS_MAC = sys.platform == "darwin"

PROMPT_TEXT = "Press keys…"
CHANGE_TEXT = "Change"
CANCEL_TEXT = "Cancel"
NOT_SET_TEXT = "Not set"

_MOUSE = {Qt.MiddleButton: "mouse:middle", Qt.BackButton: "mouse:x1", Qt.ForwardButton: "mouse:x2"}


def needs_modifier_text(mac=IS_MAC):
    """The short refusal for a bare typing key (it would fire while you type)."""
    if mac:
        return "Add ⌘, ⌥, ⌃ or ⇧."
    return "Add Ctrl, Alt, Shift or Win."


UNSUPPORTED_TEXT = "Use a letter, a number or an F key."


def combo_from_key_event(event, mac=IS_MAC):
    """(combo, problem) for a key press; see hotkeys.from_key_event."""
    import hotkeys
    return hotkeys.from_key_event(event, mac=mac)


class _CaptureFilter(QObject):
    """App-wide filter while a capture runs: keys and mouse buttons anywhere
    in the app go to the capture (only then is it installed)."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner

    def eventFilter(self, obj, ev):
        try:
            return self.owner._filter(obj, ev)
        except Exception:
            logger.debug("hotkey capture filter failed", exc_info=True)
            return False


class HotkeyCaptureButton(QWidget):
    """Keycaps + Change; captures a new shortcut. See the module notes."""

    captured = Signal(str)
    refused = Signal(str)
    cancelled = Signal()
    capturing_changed = Signal(bool)

    def __init__(self, hotkey="", parent=None, accessible_name="Shortcut", change_text=CHANGE_TEXT,
                 allow_mouse=True, big=False):
        super().__init__(parent)
        self._hotkey = (hotkey or "").strip().lower()
        self._capturing = False
        self._allow_mouse = bool(allow_mouse)
        self._change_text = change_text
        self._name = accessible_name or "Shortcut"
        self._filter_obj = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.keys = Keycaps(parent=self, big=big)
        lay.addWidget(self.keys, 0, Qt.AlignVCenter)
        self.empty_label = QLabel(NOT_SET_TEXT, self)
        theme.set_role(self.empty_label, "muted")
        lay.addWidget(self.empty_label, 0, Qt.AlignVCenter)
        self.prompt_label = QLabel(PROMPT_TEXT, self)
        theme.set_role(self.prompt_label, "link")
        self.prompt_label.setFont(theme.font(12.5, 600))
        self.prompt_label.setAccessibleName(PROMPT_TEXT)
        self.prompt_label.hide()
        lay.addWidget(self.prompt_label, 0, Qt.AlignVCenter)
        self.button = Button(change_text, "link", "s", parent=self)
        self.button.setFont(theme.font(12.5, 600))
        self.button.clicked.connect(self._on_button)
        self.button.installEventFilter(self)    # Enter / Return start it too (a QPushButton only takes Space)
        lay.addWidget(self.button, 0, Qt.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setAccessibleName(self._name)
        self._show_keys()

    # ── public API ──
    def hotkey(self):
        return self._hotkey

    def set_hotkey(self, hotkey):
        """Show ``hotkey`` (config form). Ends a running capture quietly."""
        if self._capturing:
            self._end_capture()
        self._hotkey = (hotkey or "").strip().lower()
        self._show_keys()

    def is_capturing(self):
        return self._capturing

    def start_capture(self):
        if self._capturing:
            return
        self._capturing = True
        self.keys.hide()
        self.empty_label.hide()
        self.prompt_label.show()
        self.button.setText(CANCEL_TEXT)
        self.button.setAccessibleName("Cancel changing the %s" % self._name.lower())
        app = QApplication.instance()
        if app is not None:
            self._filter_obj = _CaptureFilter(self)
            app.installEventFilter(self._filter_obj)
        # Keys land here (the filter sees them first): keep focus in this widget.
        self.button.setFocus(Qt.OtherFocusReason)
        self.capturing_changed.emit(True)

    def cancel_capture(self):
        if not self._capturing:
            return
        self._end_capture()
        self.cancelled.emit()

    # ── internals ──
    def _show_keys(self):
        has = bool(self._hotkey)
        self.keys.set_hotkey(self._hotkey if has else "")
        self.keys.setVisible(has and not self._capturing)
        self.empty_label.setVisible(not has and not self._capturing)
        self.prompt_label.setVisible(self._capturing)
        shown = self.keys.accessibleName() if has else NOT_SET_TEXT
        self.button.setText(CANCEL_TEXT if self._capturing else self._change_text)
        self.button.setAccessibleName("Change the %s (%s)" % (self._name.lower(), shown))
        self.setToolTip(shown)
        self.updateGeometry()

    def _end_capture(self):
        self._capturing = False
        app = QApplication.instance()
        if app is not None and self._filter_obj is not None:
            app.removeEventFilter(self._filter_obj)
        if self._filter_obj is not None:
            self._filter_obj.deleteLater()
        self._filter_obj = None
        self._show_keys()
        self.capturing_changed.emit(False)

    def _on_button(self):
        if self._capturing:
            self.cancel_capture()
        else:
            self.start_capture()

    def _finish(self, combo):
        self._hotkey = combo
        self._end_capture()
        self.captured.emit(combo)

    def eventFilter(self, obj, ev):
        # The Change link only (the app-wide capture filter is _CaptureFilter).
        if (obj is self.button and ev.type() == QEvent.KeyPress and not self._capturing
                and ev.key() in (Qt.Key_Return, Qt.Key_Enter) and not ev.isAutoRepeat()):
            self.start_capture()
            return True
        return False

    def _inside(self, obj):
        w = obj if isinstance(obj, QWidget) else None
        while w is not None:
            if w is self:
                return True
            w = w.parentWidget()
        return False

    def _filter(self, obj, ev):
        if not self._capturing:
            return False
        t = ev.type()
        if t == QEvent.ShortcutOverride:
            ev.accept()                     # every key goes to the capture, no shortcut fires
            return True
        if t == QEvent.KeyPress:
            self._key(ev)
            return True
        if t == QEvent.KeyRelease:
            return True
        if t == QEvent.MouseButtonPress:
            btn = ev.button()
            if self._allow_mouse and btn in _MOUSE:
                QTimer.singleShot(0, lambda c=_MOUSE[btn]: self._finish(c) if self._capturing else None)
                return True
            # Widgets only: a QWindow-level copy of a press on our own Cancel
            # must not cancel first (Cancel's click would then restart it).
            if isinstance(obj, QWidget) and not self._inside(obj):
                # A click elsewhere: stop waiting, let the click through.
                QTimer.singleShot(0, self.cancel_capture)
            return False
        if t == QEvent.ApplicationDeactivate or (t == QEvent.WindowDeactivate and obj is self.window()):
            QTimer.singleShot(0, self.cancel_capture)
            return False
        return False

    def _key(self, ev):
        if ev.key() == Qt.Key_Escape and not ev.modifiers():
            self.cancel_capture()
            return
        combo, problem = combo_from_key_event(ev)
        if problem == "modifier_only":
            return                          # keep waiting for the key itself
        if problem == "needs_modifier":
            self.refused.emit(needs_modifier_text())
            return
        if problem or not combo:
            self.refused.emit(UNSUPPORTED_TEXT)
            return
        self._finish(combo)

    def hideEvent(self, ev):
        if self._capturing:
            self.cancel_capture()
        super().hideEvent(ev)
