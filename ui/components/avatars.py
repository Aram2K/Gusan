"""The person's avatar: the first letter on a colour, a picture on a colour (preset avatars) or their own photo.
One choice for every avatar in the app (the sidebar, Settings, Account & plan), kept in cfg ``avatar`` and on
this computer only (the photo is a 256 px copy in Gusan's data folder, avatar.png).

    ""  / "blue"                  the first letter on Gusan blue (the default)
    "violet" "teal" "coral" "pink" "slate"      the first letter on another colour
    "sparkle" "star" "bolt" "sun" "wave" "globe"  a white picture on a colour
    "photo"                       their own photo (avatar.png), cut to a circle

paint(painter, rect, letter, guest=False) draws it; set_choice(value) changes it and repaints every avatar on
screen (sidebar.Avatar registers itself). A guest always gets the neutral person icon, never a letter or a photo.
The founder (2026-11-09): "allow us to choose an avatar with edit over hover", "preselected avatars".
"""

import logging
import os
import weakref

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap

from ui import theme
from ui.components import icons

logger = logging.getLogger("transcribe")

CFG_KEY = "avatar"
DEFAULT = "blue"
PHOTO = "photo"
PHOTO_SIZE = 256
PHOTO_FILE = "avatar.png"

# id -> (picture or "" for the letter, gradient start, gradient end, name for screen readers)
PRESETS = {
    "blue": ("", "#60a5fa", "#2563eb", "Blue"),
    "violet": ("", "#a78bfa", "#7c3aed", "Violet"),
    "teal": ("", "#2dd4bf", "#0d9488", "Teal"),
    "coral": ("", "#fb923c", "#ea580c", "Coral"),
    "pink": ("", "#f472b6", "#db2777", "Pink"),
    "slate": ("", "#94a3b8", "#475569", "Slate"),
    "sparkle": ("sparkle", "#c084fc", "#7c3aed", "Sparkle"),
    "star": ("star", "#fcd34d", "#f59e0b", "Star"),
    "bolt": ("bolt", "#818cf8", "#4338ca", "Lightning"),
    "sun": ("sun", "#fdba74", "#f97316", "Sun"),
    "wave": ("wave", "#5eead4", "#0f766e", "Sound wave"),
    "globe": ("globe", "#7dd3fc", "#0284c7", "Globe"),
}

_state = {"choice": DEFAULT, "photo": None}       # the photo as a QImage, loaded once
_widgets = weakref.WeakSet()


def normalize(value):
    value = str(value or "").strip().lower()
    if value == PHOTO or value in PRESETS:
        return value
    return DEFAULT


def photo_path():
    import storage
    return str(storage.path_for(PHOTO_FILE))


def choice():
    """What every avatar shows now: a PRESETS id or PHOTO (a missing photo falls back to the default)."""
    value = _state["choice"]
    if value == PHOTO and _photo() is None:
        return DEFAULT
    return value


def register(widget):
    _widgets.add(widget)


def set_choice(value):
    """Show ``value`` (cfg ``avatar``) on every avatar."""
    value = normalize(value)
    if value == PHOTO:
        _state["photo"] = None                  # a new photo may have been saved: read it again
    _state["choice"] = value
    for w in list(_widgets):
        try:
            w.update()
        except RuntimeError:
            pass


def _photo():
    if _state["photo"] is None:
        path = photo_path()
        img = QImage(path) if os.path.isfile(path) else QImage()
        _state["photo"] = img if not img.isNull() else False
    return _state["photo"] or None


def save_photo(source):
    """Their own photo: the middle square of ``source`` (a picture file), 256 px, saved as avatar.png. True when
    it worked; the choice is then PHOTO (the caller saves cfg ``avatar``)."""
    img = QImage(str(source or ""))
    if img.isNull():
        return False
    side = min(img.width(), img.height())
    img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
    img = img.scaled(PHOTO_SIZE, PHOTO_SIZE, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    path = photo_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return False
    if not img.save(path, "PNG"):
        return False
    _state["photo"] = None
    return True


def has_photo():
    return os.path.isfile(photo_path())


def paint(p, rect, letter, guest=False, value=None):
    """Draw the avatar in ``rect`` (a QRectF): ``value`` (a PRESETS id or PHOTO) or the current choice. Half a pixel
    inside the rect, so its edge is never cut at 125 / 150 percent scaling."""
    r = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    size = r.width()
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    if guest:
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, QColor("#e8eef6"))
        g.setColorAt(1, QColor("#cbd5e1"))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(r)
        isz = int(round(size * 0.58))
        p.drawPixmap(int(round(r.center().x() - isz / 2.0)), int(round(r.center().y() - isz / 2.0)),
                     icons.pixmap("user", isz, "#475569", sw=1.9))
        return
    value = normalize(value) if value is not None else choice()
    if value == PHOTO:
        img = _photo()
        if img is not None:
            path = QPainterPath()
            path.addEllipse(r)
            p.save()
            p.setClipPath(path)
            p.drawImage(r, img)
            p.restore()
            return
        value = DEFAULT
    picture, c0, c1, _name = PRESETS[value]
    g = QLinearGradient(r.topLeft(), r.bottomRight())     # 135deg
    g.setColorAt(0, QColor(c0))
    g.setColorAt(1, QColor(c1))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawEllipse(r)
    if picture:
        isz = max(8, int(round(size * 0.56)))
        pm = icons.pixmap(picture, isz, "#ffffff", sw=2.0)
        p.drawPixmap(int(round(r.center().x() - isz / 2.0)), int(round(r.center().y() - isz / 2.0)), pm)
        return
    p.setPen(QColor("#ffffff"))
    p.setFont(theme.font(max(6, int(size * 0.43)), 700))
    p.drawText(r, Qt.AlignCenter, (letter or "?")[:1].upper())


def label(value):
    """A screen reader's name for an avatar choice."""
    value = normalize(value)
    if value == PHOTO:
        return "Your photo"
    return PRESETS[value][3]


def pixmap(value, size, letter="A"):
    """A preset (or the photo) as a QPixmap of ``size`` px: the picker's buttons."""
    ratio = 2.0
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    paint(p, QRectF(0, 0, size, size), letter, value=value)
    p.end()
    return pm
