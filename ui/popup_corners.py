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

``install(app)`` once: from then on every dropdown does this when its list opens (QComboBox.showPopup - every
dropdown the app makes, its own subclasses included). It used to be an application-wide event filter, which put
every event of every widget through Python (2026-11-16 speed audit: pages built about twice as slowly).

Opening without a jolt (the founder, 2026-11-09: the list "vibrating at the beginning and getting out of the scope
while opening"): Windows' roll-down animation for lists is turned off for Gusan (UI_AnimateCombo) - the list grew
from its top while being placed - and the list's own border is dropped when the list window is first polished,
before it is shown, so it no longer changes size by its border in the middle of opening.
"""
import logging
import sys

from PySide6.QtCore import Qt

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


def before_first_open(container):
    """The list window, before it first shows: the system will draw the card's border and round it, so no square
    border of our own - set now, never in the middle of opening (it changed the list's size)."""
    if container is None or container.property("gusanNoBorder") or container.property("gusanCard"):
        return
    if container.metaObject().className() != CONTAINER or not windows_rounds_corners():
        return
    container.setProperty("gusanNoBorder", True)
    container.setStyleSheet("QComboBoxPrivateContainer { border: none; }")


def after_first_open(container):
    """The list window, just shown the first time: rounded by the system - or, when it would not, our own border
    back."""
    if container is None or container.property("gusanCard") or container.metaObject().className() != CONTAINER:
        return
    container.setProperty("gusanCard", True)
    if not round_window(container) and container.property("gusanNoBorder"):
        container.setStyleSheet("")


_installed = False


def install(app):
    """Once per application."""
    global _installed
    if _installed or app is None:
        return _installed
    from PySide6.QtWidgets import QComboBox
    show_popup = QComboBox.showPopup

    def showPopup(self):
        container = None
        try:
            container = self.view().parentWidget()
            before_first_open(container)
        except Exception:
            logger.debug("List window styling failed", exc_info=True)
        show_popup(self)
        try:
            after_first_open(container)
        except Exception:
            logger.debug("List window styling failed", exc_info=True)

    QComboBox.showPopup = showPopup
    try:
        app.setEffectEnabled(Qt.UI_AnimateCombo, False)     # no roll-down: the list opens in place, at once
    except Exception:
        logger.debug("Could not turn the list animation off", exc_info=True)
    _installed = True
    return _installed
