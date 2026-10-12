"""Pills and chips: the PRO tag, FREE / GUEST badges, small status chips."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy

from ui import theme
from ui.components import icons

# size -> (font px, letter spacing px, height); gusan_ds._pill: xs/s 10px, m 11px, line 14px.
# The 10 px pills drop the mockup's 0.02-0.04em tracking: a fraction of a pixel
# that hinting rounds into visible gaps ("FR EE").
_PILL = {"xs": (10, 0, 18), "s": (10, 0, 18), "m": (11, 0.44, 20)}


class _Pill(QLabel):
    TONE = "pro"

    def __init__(self, text, size="s", parent=None, upper=True):
        super().__init__(text.upper() if upper else text, parent)
        px, ls, h = _PILL.get(size, _PILL["m"])
        self.setProperty("gusan", "pill")
        self.setProperty("tone", self.TONE)
        self.setProperty("gsize", size)  # not "size": QWidget already has a size property
        self.setFont(theme.font(px, 700, letter_spacing=ls))
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(h)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setAccessibleName(self.text())


class ProTag(_Pill):
    """The current app's PRO pill (purple glass). Put it on every Pro feature:
    nav items, the main Pro buttons, Gusan Cloud options, setting rows."""
    TONE = "pro"

    def __init__(self, size="s", text="PRO", parent=None):
        super().__init__(text, size, parent)
        self.setToolTip("Pro feature")


class FreeBadge(_Pill):
    """The green FREE pill (also the Smart Actions counter: FreeBadge(text="4/5 FREE"))."""
    TONE = "free"

    def __init__(self, size="m", text="FREE", parent=None):
        super().__init__(text, size, parent)


class GuestBadge(_Pill):
    TONE = "guest"

    def __init__(self, size="m", text="GUEST", parent=None):
        super().__init__(text, size, parent)


def TierBadge(tier, size="m", parent=None):
    """PRO / FREE / GUEST pill for an entitlements tier string."""
    if tier == "pro":
        return ProTag(size=size, parent=parent)
    if tier == "free":
        return FreeBadge(size=size, parent=parent)
    return GuestBadge(size=size, parent=parent)


class Chip(QFrame):
    """Small rounded status chip (24 px): tone neutral | blue | pro | ok | red | amber,
    optional leading icon (13 px, stroke 2)."""

    _ICON_TONE = {"neutral": "text2", "blue": "blue-tx", "pro": "pro-tx", "ok": "ok-tx",
                  "red": "red-tx", "amber": "amber-tx"}

    def __init__(self, text, tone="neutral", icon=None, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "chip")
        self.setProperty("tone", tone)
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(5)
        self._icon = None
        if icon:
            self._icon = QLabel(self)
            self._icon.setPixmap(icons.pixmap(icon, 13, self._ICON_TONE.get(tone, "text2"), sw=2))
            lay.addWidget(self._icon)
        self._text = QLabel(text, self)
        self._text.setFont(theme.font(12, 600))
        lay.addWidget(self._text)
        self.setAccessibleName(text)

    def text(self):
        return self._text.text()

    def setText(self, text):
        self._text.setText(text)
        self.setAccessibleName(text)
