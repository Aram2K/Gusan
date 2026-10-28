"""Gusan stroke icons (the mockups' gusan_ds.IC set), drawn from SVG.

    icons.pixmap("mic", 18, "#1d4ed8")      -> QPixmap (device-pixel sharp)
    icons.icon("gear", 19, "#334155")       -> QIcon
    icons.logo_pixmap(30)                   -> the Gusan logo (assets/icon_512.png)

24x24 viewBox, round caps and joins, stroke 1.7 by default (2 for an active
nav item). Colours are any CSS colour or theme token name ("blue-tx").
Rendered once per (name, size, colour, stroke, DPR) and cached.
"""

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from app_info import resource_path

IC = {
    "home": '<path d="M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5.5h-5V20H5a1 1 0 0 1-1-1z"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>',
    "sparkle": '<path d="M12 3.5l1.9 6.6 6.6 1.9-6.6 1.9-1.9 6.6-1.9-6.6L3.5 12l6.6-1.9z"/>',
    # The Smart Actions magic wand (the founder's pick, "classic wand"): a
    # rounded wand with a band near its tip and three outline sparkles.
    # ui/overlay.py draws the same shape (Overlay._WAND_*).
    "wand": ('<rect x="7.9" y="6" width="3.2" height="17" rx="1.6" transform="rotate(45 9.5 14.5)"/>'
             '<path d="M12.1 9.6 14.4 11.9"/>'
             '<path d="M18.8 1.8Q19.35 4.05 21.6 4.6Q19.35 5.15 18.8 7.4Q18.25 5.15 16 4.6Q18.25 4.05 18.8 1.8Z"/>'
             '<path stroke-width="1.5" d="M6.6 3.5Q6.98 5.02 8.5 5.4Q6.98 5.78 6.6 7.3Q6.22 5.78 4.7 5.4'
             'Q6.22 5.02 6.6 3.5Z"/>'
             '<path stroke-width="1.4" d="M20.6 10.9Q20.9 12.1 22.1 12.4Q20.9 12.7 20.6 13.9Q20.3 12.7 19.1 12.4'
             'Q20.3 12.1 20.6 10.9Z"/>'),
    "people": ('<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5"/>'
               '<circle cx="16.5" cy="9" r="2.5"/><path d="M15.8 14.1c2.4-.2 4.2 1.4 4.7 4.4"/>'),
    "headset": ('<path d="M4 14v-2a8 8 0 0 1 16 0v2"/><rect x="3.5" y="13.5" width="4" height="6" rx="1.5"/>'
                '<rect x="16.5" y="13.5" width="4" height="6" rx="1.5"/><path d="M18.5 19.5c0 1.2-1.5 2-3.5 2h-2"/>'),
    "incognito": ('<path d="M3.5 11.5h17"/><path d="M6.5 11.5 8 6.2a1 1 0 0 1 1.2-.7l2.8.8 2.8-.8a1 1 0 0 1 1.2.7l1.5 5.3"/>'
                  '<circle cx="8" cy="16.5" r="2.7"/><circle cx="16" cy="16.5" r="2.7"/>'
                  '<path d="M10.7 16.3c.8-.5 1.8-.5 2.6 0"/>'),
    "bubble": ('<path d="M5 5h14a1.5 1.5 0 0 1 1.5 1.5v9A1.5 1.5 0 0 1 19 17h-7l-4.5 3.5V17H5a1.5 1.5 0 0 1'
               '-1.5-1.5v-9A1.5 1.5 0 0 1 5 5z"/>'),
    "file": '<path d="M6 3h8l4 4v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M14 3v4h4M8.5 13h7M8.5 16.5h5"/>',
    "upload": '<path d="M12 15V4M7.5 8.5 12 4l4.5 4.5M5 14v5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5"/>',
    "download": '<path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 14v5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5"/>',
    "book": ('<path d="M5 5.5A2.5 2.5 0 0 1 7.5 3H19v14H7.5A2.5 2.5 0 0 0 5 19.5z"/>'
             '<path d="M5 19.5A1.5 1.5 0 0 0 6.5 21H19v-4"/>'),
    "gear": ('<circle cx="12" cy="12" r="2.6"/><circle cx="12" cy="12" r="6.2"/>'
             '<path d="M12 3.2v2.6M12 18.2v2.6M3.2 12h2.6M18.2 12h2.6M5.8 5.8l1.8 1.8M16.4 16.4l1.8 1.8'
             'M5.8 18.2l1.8-1.8M16.4 7.6l1.8-1.8"/>'),
    "user": '<circle cx="12" cy="8.5" r="3.8"/><path d="M4.5 20c.9-3.9 3.8-6 7.5-6s6.6 2.1 7.5 6"/>',
    "shield": '<path d="M12 3 5 6v5.5c0 4.3 3 7.8 7 9.5 4-1.7 7-5.2 7-9.5V6z"/>',
    "eye_off": ('<path d="M3 3l18 18M10.6 6.1A9.9 9.9 0 0 1 12 6c5 0 8.5 4.5 9.5 6-.5.8-1.6 2.3-3.2 3.6'
                'M6.4 7.6C4.6 8.9 3.2 10.7 2.5 12c1 1.5 4.5 6 9.5 6 1.4 0 2.7-.3 3.8-.9"/>'
                '<path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>'),
    "search": '<circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5 5"/>',
    "copy": ('<rect x="8" y="8" width="12" height="12" rx="2"/>'
             '<path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8"/>'),
    "more": '<circle cx="5.5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="18.5" cy="12" r="1.6"/>',
    # The paper-clip in the corner of the Context box: "add documents" (Live Assistant).
    "paperclip": ('<path d="M20.5 11.2 11.9 19.8a5.2 5.2 0 0 1-7.4-7.4l9-9a3.5 3.5 0 0 1 4.9 4.9l-9 9'
                  'a1.8 1.8 0 0 1-2.5-2.5l8.3-8.3"/>'),
    "stop": '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
    "pause": '<path d="M9 6.5v11M15 6.5v11"/>',
    "play": '<path d="M8 5.5v13l10.5-6.5z"/>',
    "chev_r": '<path d="m9.5 6 6 6-6 6"/>',
    "chev_l": '<path d="m14.5 6-6 6 6 6"/>',
    "chev_d": '<path d="m6 9.5 6 6 6-6"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "check_circle": '<circle cx="12" cy="12" r="9"/><path d="m8 12.3 2.8 2.8L16.2 9.6"/>',
    "circle": '<circle cx="12" cy="12" r="8"/>',
    "x": '<path d="m6.5 6.5 11 11M17.5 6.5l-11 11"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M6 12h12"/>',
    "send": '<path d="M4.5 12 19.5 4.5 15 19.5l-3.3-6.3z"/><path d="M11.7 13.2 19.5 4.5"/>',
    "screen": '<rect x="3" y="4" width="18" height="12.5" rx="1.5"/><path d="M8.5 20.5h7M12 16.5v4"/>',
    "laptop": '<rect x="4" y="5" width="16" height="10.5" rx="1.5"/><path d="M2.5 19h19"/>',
    "chip": ('<rect x="6" y="6" width="12" height="12" rx="2"/>'
             '<path d="M9 2.5v3.5M15 2.5v3.5M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5"/>'),
    "memory": '<rect x="3" y="7" width="18" height="10" rx="1.5"/><path d="M7 7v10M11 7v10M15 7v10M3 20h18"/>',
    "cloud": '<path d="M7.2 18.5h10.3a4 4 0 0 0 .5-7.97A6 6 0 0 0 6.5 9.3a4.6 4.6 0 0 0 .7 9.2z"/>',
    "bolt": '<path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/>',
    "target": '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
    "key": '<circle cx="8" cy="15" r="3.8"/><path d="m10.8 12.3 8.2-8.2M16 7l2.5 2.5M13.8 9.2l2 2"/>',
    "lock": '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
    "globe": ('<circle cx="12" cy="12" r="9"/>'
              '<path d="M3 12h18M12 3c2.4 2.5 3.6 5.5 3.6 9s-1.2 6.5-3.6 9c-2.4-2.5-3.6-5.5-3.6-9S9.6 5.5 12 3z"/>'),
    "translate": ('<path d="M4 5h8M8 3v2M5.5 5c.8 3 3 5.5 6 6.5M10.5 5c-.8 3.5-3 6.5-6.5 8"/>'
                  '<path d="m12 21 4.5-10L21 21M13.5 17.5h6"/>'),
    "mail": '<rect x="3" y="5.5" width="18" height="13" rx="2"/><path d="m3.5 7 8.5 6.5L20.5 7"/>',
    "list": ('<path d="M9 6.5h11M9 12h11M9 17.5h11"/>'
             '<path d="m3.5 6.5 1.2 1.2 2-2.2M3.5 12l1.2 1.2 2-2.2M3.5 17.5l1.2 1.2 2-2.2"/>'),
    "text": '<path d="M5 6.5h14M5 11h14M5 15.5h9"/>',
    "pen": '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
    "clipboard": '<rect x="6" y="4.5" width="12" height="16" rx="2"/><path d="M9.5 4.5v-1h5v1"/>',
    "bell": '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "sun": ('<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2'
            'M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/>'),
    "power": '<path d="M12 3v8"/><path d="M6.6 6.6a7.5 7.5 0 1 0 10.8 0"/>',
    "refresh": '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4.2h-4.2"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.9v.1"/>',
    "alert": ('<path d="M10.3 4.6 2.9 17.5A2 2 0 0 0 4.6 20.5h14.8a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0z"/>'
              '<path d="M12 9.5v4.5M12 17v.1"/>'),
    "crown": '<path d="M4 8.5 7.5 12 12 5.5 16.5 12 20 8.5 18.5 18h-13z"/><path d="M5.5 21h13"/>',
    "star": '<path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/>',
    "trash": '<path d="M4 6.5h16M9.5 6.5v-2h5v2M6.5 6.5l1 13.5h9l1-13.5"/>',
    "pin": '<path d="M9 3.5h6l-1 5.5 3.5 3.5h-11L10 9z"/><path d="M12 12.5v8"/>',
    "keyboard": '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M9.5 10h.01M13 10h.01M16.5 10h.01M7.5 14h9"/>',
    "volume": '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4.5 4.5 0 0 1 0 6M18 6.5a8 8 0 0 1 0 11"/>',
    "link": ('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/>'
             '<path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
    "arrow_r": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "export": '<path d="M12 15V4M7.5 8.5 12 4l4.5 4.5M5 14v5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5"/>',
    "notion": '<rect x="4" y="3.5" width="16" height="17" rx="2"/><path d="M8.5 16.5v-9l7 9v-9"/>',
    "wave": '<path d="M3 12h2M7 8v8M11 5v14M15 8v8M19 10.5v3M21 12h0"/>',
    "speaker": '<rect x="6" y="3" width="12" height="18" rx="2"/><circle cx="12" cy="14" r="3.5"/><path d="M12 7v.1"/>',
}
FILLED = {"more", "play"}

_CACHE = {}


def names():
    return sorted(IC)


def _dpr():
    try:
        scr = QGuiApplication.primaryScreen()
        return max(1.0, float(scr.devicePixelRatio())) if scr else 1.0
    except Exception:
        return 1.0


def _colour(color):
    if isinstance(color, QColor):
        return QColor(color)
    from ui import theme
    return theme.qcolor(color or "text2")


def svg(name, color="#334155", sw=1.7):
    """The icon as an SVG document (bytes) in one solid colour."""
    c = _colour(color)
    hexc = "#%02x%02x%02x" % (c.red(), c.green(), c.blue())
    op = "%.3f" % (c.alpha() / 255.0)
    if name in FILLED:
        paint = 'fill="%s" fill-opacity="%s" stroke="none"' % (hexc, op)
    else:
        paint = ('fill="none" stroke="%s" stroke-opacity="%s" stroke-width="%s" '
                 'stroke-linecap="round" stroke-linejoin="round"' % (hexc, op, sw))
    # A part drawn with fill="currentColor" (the wand's star) takes the icon colour.
    body = IC[name].replace("currentColor", hexc)
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" %s>%s</svg>'
            % (paint, body)).encode("utf-8")


def pixmap(name, size=18, color="#334155", sw=1.7):
    """A tinted, device-pixel-sharp QPixmap of icon ``name`` at ``size`` logical px."""
    if name not in IC:
        raise KeyError("unknown icon %r" % name)
    c = _colour(color)
    dpr = _dpr()
    key = (name, int(size), c.rgba(), float(sw), dpr)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    pm = QPixmap(max(1, int(round(size * dpr))), max(1, int(round(size * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    renderer = QSvgRenderer(QByteArray(svg(name, c, sw)))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    _CACHE[key] = pm
    return pm


def icon(name, size=18, color="#334155", sw=1.7, active_color=None):
    """A QIcon; ``active_color`` adds the Active/Selected look (e.g. a checked item)."""
    ic = QIcon()
    ic.addPixmap(pixmap(name, size, color, sw), QIcon.Normal, QIcon.Off)
    if active_color:
        act = pixmap(name, size, active_color, sw)
        ic.addPixmap(act, QIcon.Normal, QIcon.On)
        ic.addPixmap(act, QIcon.Active, QIcon.Off)
        ic.addPixmap(act, QIcon.Selected, QIcon.Off)
    return ic


def logo_pixmap(size=30):
    """The Gusan logo (the round flag-coloured mic) at ``size`` logical px."""
    dpr = _dpr()
    key = ("__logo__", int(size), dpr)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    src = QPixmap(resource_path("assets", "icon_512.png"))
    if src.isNull():
        pm = QPixmap(max(1, int(size * dpr)), max(1, int(size * dpr)))
        pm.fill(Qt.transparent)
    else:
        pm = src.scaled(int(round(size * dpr)), int(round(size * dpr)),
                        Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    _CACHE[key] = pm
    return pm
