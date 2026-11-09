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
"""
import logging
import sys

from PySide6.QtCore import QEvent, QObject

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
            if event.type() == QEvent.Show and obj.metaObject().className() == CONTAINER \
                    and not obj.property("gusanCard"):
                obj.setProperty("gusanCard", True)
                if round_window(obj):
                    # The system draws the card's border and rounds it: no square border of our own.
                    obj.setStyleSheet("QComboBoxPrivateContainer { border: none; }")
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
    return _filter
