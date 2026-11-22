"""The small picture kit the calendar parts share: stroke glyphs, dots, the Google G and its tile,
and the call-type marks (a tinted camera, no brand logos).

Moved out of ui/pages/meeting_views.py so that Home's "Coming up" can draw them without importing
the whole Meetings page code (meeting_views still imports every name from here, so its callers and
tests see no change). Light on purpose: only Qt, the theme and the icon set.

Every picture here is drawn in theme tokens and follows the light / dark look: a label or button
showing one drawn in a token colour gets it again after a switch (_retint, a theme.on_change hook).
"""

import os

from PySide6.QtCore import QByteArray, QObject, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from ui import theme
from ui.components import icons

# ── glyphs, dots, faces ──────────────────────────────────────────────────────
# Stroke glyphs the shared icon set doesn't have (same 24 grid, the mockup's paths).
_GLYPHS = {
    "video": '<rect x="3" y="6.5" width="12.5" height="11" rx="2.2"/><path d="m15.5 10.6 5-2.9v8.6l-5-2.9z"/>',
    "bell_on": '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "bell_off": ('<path d="M8.3 6.2A6 6 0 0 1 18 11v4.2M6 11v5.5l-1.5 2h12"/><path d="M10 20.5a2 2 0 0 0 4 0"/>'
                 '<path d="m3.5 3.5 17 17"/>'),
}
_CACHE = {}
# cache key of a picture drawn in token colours -> a function that draws it again in the active look
_RECIPES = {}
_HOOK = {"owner": None}


def _remember(pm, again):
    """Keep how to draw ``pm`` again (it follows a switch of the look)."""
    _RECIPES[pm.cacheKey()] = again
    if _HOOK["owner"] is None:
        _HOOK["owner"] = QObject()
        theme.on_change(_HOOK["owner"], _retint)
    return pm


def _tokens(*colours):
    return all(isinstance(c, str) and theme.is_token(c) for c in colours if c is not None)


def _retint():
    """The look changed: every label (and button icon made by glyph_icon) showing a picture from here
    gets it again in the new colours."""
    app = QApplication.instance()
    if app is None or not _RECIPES:
        return
    for w in app.allWidgets():
        try:
            if isinstance(w, QLabel):
                pm = w.pixmap()
                if pm is not None and not pm.isNull():
                    again = _RECIPES.get(pm.cacheKey())
                    if again is not None:
                        w.setPixmap(again())
        except RuntimeError:
            continue


def _dpr():
    try:
        scr = QGuiApplication.primaryScreen()
        return max(1.0, float(scr.devicePixelRatio())) if scr else 1.0
    except Exception:
        return 1.0


def glyph(name, size=16, color="muted", sw=1.8):
    """A tinted pixmap: the shared icon set, or one of the local glyphs above."""
    if name not in _GLYPHS:
        if name not in icons.IC:
            name = "info"
        return icons.pixmap(name, size, color, sw)
    col = theme.qcolor(color) if not isinstance(color, QColor) else color
    dpr = _dpr()
    key = ("g", name, int(size), col.rgba(), float(sw), dpr, _tokens(color))
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
           'stroke="#%02x%02x%02x" stroke-opacity="%.3f" stroke-width="%s" stroke-linecap="round" '
           'stroke-linejoin="round">%s</svg>' % (col.red(), col.green(), col.blue(), col.alpha() / 255.0,
                                                sw, _GLYPHS[name]))
    pm = QPixmap(max(1, int(round(size * dpr))), max(1, int(round(size * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(p, QRectF(0, 0, size, size))
    p.end()
    _CACHE[key] = pm
    if _tokens(color):
        _remember(pm, lambda: glyph(name, size, color, sw))
    return pm


def glyph_icon(name, size=16, color="muted", sw=1.8):
    """glyph() as a QIcon that follows the look (a button's icon)."""
    from ui.components import icons
    ic = QIcon(glyph(name, size, color, sw))
    if _tokens(color):
        icons.remember_icon(ic, lambda: glyph_icon(name, size, color, sw))
    return ic


def icon_label(name, size=16, color="muted", sw=1.8, parent=None):
    lb = QLabel(parent)
    lb.setPixmap(glyph(name, size, color, sw))
    lb.setFixedSize(size, size)
    lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return lb


def dot_pixmap(color, size=8, ring=None):
    """A filled dot (``ring``: (colour, width) drawn around it, e.g. the next meeting)."""
    dpr = _dpr()
    col = theme.qcolor(color)
    ring_rgba = theme.qcolor(ring[0]).rgba() if ring else None
    key = ("dot", col.rgba(), size, str(ring), ring_rgba, dpr, _tokens(color, ring[0] if ring else None))
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    pad = ring[1] if ring else 0
    full = size + 2 * pad
    pm = QPixmap(int(round(full * dpr)), int(round(full * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    if ring:
        p.setBrush(theme.qcolor(ring[0]))
        p.drawEllipse(QRectF(0, 0, full, full))
    p.setBrush(col)
    p.drawEllipse(QRectF(pad, pad, size, size))
    p.end()
    _CACHE[key] = pm
    if _tokens(color, ring[0] if ring else None):
        _remember(pm, lambda: dot_pixmap(color, size, ring))
    return pm


def dot_icon(color, size=8, ring=None):
    """dot_pixmap() as a QIcon that follows the look (a button's icon)."""
    from ui.components import icons
    ic = QIcon(dot_pixmap(color, size, ring))
    if _tokens(color, ring[0] if ring else None):
        icons.remember_icon(ic, lambda: dot_icon(color, size, ring))
    return ic


def dot_label(color, size=8, parent=None):
    lb = QLabel(parent)
    lb.setPixmap(dot_pixmap(color, size))
    lb.setFixedSize(size, size)
    lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return lb


def google_pixmap(size=16, asset="google_g.svg"):
    """The Google G (assets/google_g.svg), or another of the assets' pictures."""
    dpr = _dpr()
    key = (asset, size, dpr)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    from app_info import resource_path
    pm = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    path = resource_path("assets", asset)
    if os.path.isfile(path):
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        QSvgRenderer(path).render(p, QRectF(0, 0, size, size))
        p.end()
    _CACHE[key] = pm
    return pm


def google_label(size=16, parent=None):
    lb = QLabel(parent)
    lb.setPixmap(google_pixmap(size))
    lb.setFixedSize(size, size)
    lb.setAccessibleName("Google")
    return lb


def calendar_pixmap(size=16):
    """The Google Calendar mark (assets/google_calendar.svg) - wherever the Google picture stands for the calendar
    (founder 2026-11-12: "I don't want the Google sign, I want the Google Calendar sign")."""
    return google_pixmap(size, "google_calendar.svg")


def calendar_label(size=16, parent=None):
    lb = QLabel(parent)
    lb.setPixmap(calendar_pixmap(size))
    lb.setFixedSize(size, size)
    lb.setAccessibleName("Google Calendar")
    return lb


class GoogleTile(QWidget):
    """The Google Calendar mark on a small disc of the card's surface (white in the light look)."""

    def __init__(self, size=40, g=20, parent=None):
        super().__init__(parent)
        self._size, self._g = size, g
        self.setFixedSize(size, size + 2)
        self.setAccessibleName("Google Calendar")

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, self._size - 1, self._size - 1)
        p.setPen(Qt.NoPen)
        shade = theme.qcolor("shade")
        shade.setAlpha(60 if theme.is_dark() else 14)
        p.setBrush(shade)
        p.drawEllipse(r.translated(0, 1))
        p.setBrush(theme.qcolor("surface"))
        p.setPen(QPen(theme.qcolor("line2" if theme.is_dark() else "line"), 1))
        p.drawEllipse(r)
        off = (self._size - self._g) / 2.0
        p.drawPixmap(int(off), int(off), calendar_pixmap(self._g))
        p.end()


# Call-type marks: a plain camera glyph tinted per service (no brand logos).
CALL_TONES = {"Google Meet": ("ok-sub", "ok-tx"), "Zoom": ("blue-sub", "blue-tx"),
              "Teams": ("teams-sub", "teams-tx"), "Webex": ("bg2", "text2")}


def call_name(kind):
    """"Google Meet" / "Zoom" ... / "Video call" (a link of no known service) / "" (no link):
    calendar_sync.call_label, the one mapping the reminder and both lists share."""
    try:
        import calendar_sync
        return calendar_sync.call_label(kind)
    except Exception:
        if not kind:
            return ""
        return "Video call" if kind == "other" else str(kind)


def call_mark_pixmap(kind, size=18):
    bg, fg = CALL_TONES.get(kind, ("bg2", "text2"))
    dpr = _dpr()
    key = ("call", kind, size, dpr, theme.scheme())
    hit = _CACHE.get(key)
    if hit is None:
        hit = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
        hit.setDevicePixelRatio(dpr)
        hit.fill(Qt.transparent)
        p = QPainter(hit)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("surface"))
        p.drawRoundedRect(QRectF(0, 0, size, size), 6, 6)
        p.setBrush(theme.qcolor(bg))
        p.drawRoundedRect(QRectF(0, 0, size, size), 6, 6)
        g = size - 6
        p.drawPixmap(3, 3, glyph("video", g, fg, 2.0))
        p.end()
        _CACHE[key] = hit
        _remember(hit, lambda: call_mark_pixmap(kind, size))
    return hit


def call_mark(kind, size=18):
    hit = call_mark_pixmap(kind, size)
    lb = QLabel()
    lb.setPixmap(hit)
    lb.setFixedSize(size, size)
    return lb


__all__ = ["glyph", "glyph_icon", "icon_label", "dot_pixmap", "dot_icon", "dot_label", "google_pixmap",
           "google_label", "GoogleTile", "CALL_TONES", "call_name", "call_mark", "call_mark_pixmap"]
