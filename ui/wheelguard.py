"""Stop the mouse wheel from silently changing dropdown / spin-box values.

Qt delivers a wheel event to whatever widget sits under the cursor, and
QComboBox, QAbstractSpinBox and QSlider all treat a wheel tick as "change my
value". Inside a scrollable settings page that means simply scrolling past a
dropdown quietly rewrites a setting - which is how the Whisper model, spoken
language or meeting audio device could change without the user ever clicking
anything.

It replaces the wheel handling of those three classes - every dropdown, spin box and slider the app makes,
its own subclasses included: the wheel event goes to the nearest scroll area, so the page scrolls the way the
user expects. The values are still
fully changeable: click the widget and pick an entry, or use the keyboard (arrow keys / Page Up-Down) once it has
focus. Scrolling *inside* an open dropdown list is unaffected - that list is its own view widget.

It used to be an application-wide event filter: every event of every widget then went through Python twice
(with ui/popup_corners.py), and building a page took about twice as long (2026-11-16 speed audit: 8.1 s against
4.3 s for every page).
"""
from PySide6.QtWidgets import QAbstractScrollArea, QAbstractSlider, QAbstractSpinBox, QComboBox

# Widgets whose value must never move because the pointer happened to be over
# them while the user scrolled the page.
GUARDED_TYPES = (QComboBox, QAbstractSpinBox, QAbstractSlider)


def _enclosing_scroll_area(widget):
    """The nearest QAbstractScrollArea ancestor, or None."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


def _leave_it_to_the_page(self, event):
    """A guarded widget's wheel: never its value - the page scrolls instead (the event goes to the nearest scroll
    area, as the application-wide filter did; with none, it is simply dropped)."""
    area = _enclosing_scroll_area(self)
    if area is not None:
        from PySide6.QtWidgets import QApplication
        QApplication.sendEvent(area.viewport(), event)
    event.accept()


_installed = False


def install(app=None):
    """Guard every dropdown, spin box and slider. Idempotent; returns True once installed."""
    global _installed
    if not _installed:
        for cls in GUARDED_TYPES:
            cls.wheelEvent = _leave_it_to_the_page
        _installed = True
    return _installed
