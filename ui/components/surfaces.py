"""Surfaces: glass Card (r16), WhiteCard (r12), PlanCard, Divider, IconTile, label()."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ui import theme
from ui.components import icons


def label(text="", role="body", parent=None, wrap=False, selectable=False):
    """A QLabel with a text role (title, h2, h3, subtitle, body, label, hint,
    muted, faint, model, eyebrow, link, error) - see ui/theme.py ROLES."""
    lb = QLabel(text, parent)
    theme.set_role(lb, role)
    if wrap:
        lb.setWordWrap(True)
    if selectable:
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


class _Surface(QFrame):
    KIND = "card"
    PAD = (20, 18)        # (horizontal, vertical), CSS "18px 20px"
    GAP = 12
    SHADOW = "card"

    def __init__(self, parent=None, pad=None, gap=None, shadow=None):
        super().__init__(parent)
        self.setProperty("gusan", self.KIND)
        ph, pv = pad if pad is not None else self.PAD
        lay = QVBoxLayout(self)
        lay.setContentsMargins(ph, pv, ph, pv)
        lay.setSpacing(self.GAP if gap is None else gap)
        level = self.SHADOW if shadow is None else (self.SHADOW if shadow is True else shadow)
        if level:
            theme.elevate(self, level)

    def add(self, item, stretch=0, align=None):
        """Add a widget or a layout to the card's column."""
        lay = self.layout()
        if isinstance(item, QWidget):
            if align is not None:
                lay.addWidget(item, stretch, align)
            else:
                lay.addWidget(item, stretch)
        else:
            lay.addLayout(item, stretch)
        return item


class Card(_Surface):
    """Frosted glass card - the current app's #cardFrame (radius 16, soft shadow).
    shadow=False drops the shadow (use it for long lists: a shadow effect
    repaints the whole card through an off-screen pixmap)."""
    KIND = "card"


class WhiteCard(_Surface):
    """Plain white card, radius 12, hairline border, no shadow."""
    KIND = "whiteCard"
    PAD = (18, 16)
    GAP = 10
    SHADOW = None


class PlanCard(_Surface):
    """The Free/Guest plan card: light blue frosted glass with a faint blue edge (Account page only)."""
    KIND = "tierCard"
    SHADOW = None


class Divider(QFrame):
    """A 1 px hairline (var(--line))."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "divider")
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)


class IconTile(QLabel):
    """A rounded square holding one icon (40 px, blue tint by default)."""

    def __init__(self, icon, color="blue-tx", bg="blue-sub", size=40, icon_size=20, radius=12,
                 parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "iconTile")
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self.setPixmap(icons.pixmap(icon, icon_size, color))
        if bg != "blue-sub" or radius != 12:
            self.setStyleSheet("background: %s; border-radius: %dpx;" % (theme.qss_color(bg), radius))
