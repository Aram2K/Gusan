"""Buttons: the mockups' btn() kinds and sizes, and icon-only buttons."""

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QPushButton, QSizePolicy, QToolButton

from ui import theme
from ui.components import icons

# size -> (height, font px, icon px); gusan_ds.btn: s 30 / m 36 / l 44 / xl 52.
SIZES = {"s": (30, 13, 16), "m": (36, 13.5, 16), "l": (44, 15, 18), "xl": (52, 16, 18)}
# kind -> (font weight, icon colour)
KINDS = {
    "primary": (600, "#ffffff"),
    "secondary": (500, "text2"),
    "ghost": (500, "text2"),
    "link": (600, "blue-tx"),
    "pro": (700, "#ffffff"),
    "danger": (600, "#ffffff"),
    "dark": (600, "#ffffff"),
}


class Button(QPushButton):
    """A v2 button. kind: primary | secondary | ghost | link | pro | danger | dark;
    size: s (30) | m (36) | l (44) | xl (52); icon: a name from icons.IC.

    Focus comes from Tab only (a mouse click doesn't leave a focus ring), so
    the ring is the keyboard's "focus-visible"."""

    def __init__(self, text="", kind="secondary", size="m", icon=None, parent=None,
                 pill=False, full=False, trail=None):
        super().__init__(text, parent)
        if kind not in KINDS:
            raise ValueError("unknown button kind %r" % kind)
        h, px, isz = SIZES[size]
        weight, icol = KINDS[kind]
        self._kind = kind
        self.setProperty("gusan", "btn")
        self.setProperty("kind", kind)
        self.setProperty("gsize", size)  # not "size": QWidget already has a size property
        self.setProperty("pill", bool(pill))
        self.setFont(theme.font(px, weight))
        self.setFixedHeight(h)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        if icon:
            self.setIcon(icons.icon(icon, isz, icol))
            self.setIconSize(QSize(isz, isz))
        if full:
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        else:
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        if text:
            self.setAccessibleName(text)
        self._trail = trail
        if trail is not None:
            # A trailing widget (e.g. a ProTag) sits inside the button, right of
            # the text; the text is then left-aligned so the two never overlap.
            from PySide6.QtWidgets import QHBoxLayout
            self.setProperty("trail", True)
            lay = QHBoxLayout(self)
            lay.setContentsMargins(0, 0, 10, 0)
            lay.addStretch(1)
            trail.setParent(self)
            trail.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            lay.addWidget(trail, 0, Qt.AlignVCenter)

    def sizeHint(self):
        sh = super().sizeHint()
        if self._trail is not None:
            sh.setWidth(sh.width() + self._trail.sizeHint().width() + 4)
        return sh

    def kind(self):
        return self._kind


class IconButton(QToolButton):
    """An icon-only button. ``accessible_name`` is required: screen readers
    read it, and it is also the tooltip."""

    def __init__(self, icon, accessible_name, size=34, icon_size=18, color="text2",
                 parent=None, framed=False):
        super().__init__(parent)
        if not (accessible_name or "").strip():
            raise ValueError("IconButton needs an accessible name")
        self._icon_name = icon
        self.setProperty("gusan", "iconBtn")
        self.setProperty("framed", bool(framed))
        self.setIcon(icons.icon(icon, icon_size, color))
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(size, size)
        self.setAccessibleName(accessible_name)
        self.setToolTip(accessible_name)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.setAutoRaise(True)

    def set_icon(self, icon, color="text2", icon_size=None):
        isz = icon_size or self.iconSize().width()
        self._icon_name = icon
        self.setIcon(icons.icon(icon, isz, color))
