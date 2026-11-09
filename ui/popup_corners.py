"""Every dropdown's list as one clean card.

A QComboBox list is a square top-level window (QComboBoxPrivateContainer) holding the list. The stylesheet drew a
rounded card inside that square window - and the window cannot be made see-through (changing its flags made the
list stop selecting and crashed once, 2.1.2) - so square corners and a square shadow showed around the rounded card
(the founder: "weird edges and corners", 2026-11-09).

Now the window itself is the card: it is filled white with one thin border (styles.py), the list inside has no
border or rounding of its own, and on Windows 11 the system rounds the window's corners (DwmSetWindowAttribute
DWMWA_WINDOW_CORNER_PREFERENCE = round) - smooth edges and a shadow that follows them, like Windows' own menus. The
system draws the border then, so the window's own square one is dropped. Windows 10 and macOS keep the clean square
card. Nothing about the window's flags changes.

``install(app)`` once: an application event filter that does this when a list window is first shown.

Opening without a jolt (the founder, 2026-11-09: the list "vibrating at the beginning and getting out of the scope
while opening"): Windows' roll-down animation for lists is turned off for Gusan (UI_AnimateCombo) - the list grew
from its top while being placed - and the list's own border is dropped when the list window is first polished,
before it is shown, so it no longer changes size by its border in the middle of opening.
"""
import logging
import sys

from PySide6.QtCore import QEvent, QObject, Qt

logger = logging.getLogger(__name__)

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
CONTAINER = "QComboBoxPrivateContainer"


def windows_rounds_corners():
    """Windows 11 (build 22000 and later): the system can round a window's corners."""
    if sys.platform != "win32":
        return False
    try:
        return sys.getwindowsversion().build >= 22000
    except Exception:
        return False


def round_window(widget):
    """Ask Windows 11 to round this top-level window's corners. True when it did."""
    if not windows_rounds_corners():
        return False
    try:
        import ctypes
        hwnd = int(widget.winId())
        value = ctypes.c_int(DWMWCP_ROUND)
        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
                                                          ctypes.byref(value), ctypes.sizeof(value))
        return res == 0
    except Exception:
        logger.debug("Could not round a list window", exc_info=True)
        return False


class _PopupCorners(QObject):
    def eventFilter(self, obj, event):
        try:
            kind = event.type()
            if kind not in (QEvent.Polish, QEvent.Show) or obj.metaObject().className() != CONTAINER:
                return False
            if kind == QEvent.Polish and windows_rounds_corners() and not obj.property("gusanNoBorder"):
                # Before it is first shown: the system will draw the card's border and round it, so no square
                # border of our own - set now, never in the middle of opening (it changed the list's size).
                obj.setProperty("gusanNoBorder", True)
                obj.setStyleSheet("QComboBoxPrivateContainer { border: none; }")
            elif kind == QEvent.Show and not obj.property("gusanCard"):
                obj.setProperty("gusanCard", True)
                if not round_window(obj) and obj.property("gusanNoBorder"):
                    obj.setStyleSheet("")               # the system did not round it: our own border back
        except Exception:
            logger.debug("List window styling failed", exc_info=True)
        return False


_filter = None


def install(app):
    """Once per application."""
    global _filter
    if _filter is None and app is not None:
        _filter = _PopupCorners(app)
        app.installEventFilter(_filter)
        try:
            app.setEffectEnabled(Qt.UI_AnimateCombo, False)     # no roll-down: the list opens in place, at once
        except Exception:
            logger.debug("Could not turn the list animation off", exc_info=True)
    return _filter
