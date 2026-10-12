"""The main window's left navigation (232 px), as in the mockups.

Logo + "Gusan", the seven pages (no PRO tags: Pro features open the Pro
window when a Free user uses them), then the account at the bottom - for Free and guests a small
card with one quiet "Upgrade to Pro", for Pro just the name and the PRO pill -
and Settings last.

Signals: navigate(route), upgrade(), account().
"""

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from ui import theme
from ui.components import icons
from ui.components.badges import FreeBadge, GuestBadge, ProTag

# route, icon, label, Pro feature
NAV = (
    ("home", "home", "Home", False),
    ("dictation", "mic", "Dictation", False),
    ("smart_actions", "wand", "Smart Actions", True),
    ("meetings", "people", "Meetings", True),
    ("live_assistant", "incognito", "Live Assistant", True),
    ("audio_to_text", "wave", "Audio to text", False),
    ("dictionary", "book", "Dictionary", False),
)


class Avatar(QWidget):
    """Round avatar: the name's first letter on the blue gradient."""

    def __init__(self, name="", size=30, parent=None):
        super().__init__(parent)
        self._size = int(size)
        self._letter = (name or "?").strip()[:1].upper() or "?"
        self.setFixedSize(self._size, self._size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_name(self, name):
        self._letter = (name or "?").strip()[:1].upper() or "?"
        self.update()

    def sizeHint(self):
        return QSize(self._size, self._size)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0, 0, self._size, self._size)
        g = QLinearGradient(r.topLeft(), r.bottomRight())     # 135deg
        g.setColorAt(0, QColor("#60a5fa"))
        g.setColorAt(1, QColor("#2563eb"))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(r)
        p.setPen(QColor("#ffffff"))
        p.setFont(theme.font(int(self._size * 0.43), 700))
        p.drawText(r, Qt.AlignCenter, self._letter)
        p.end()


class NavItem(QPushButton):
    """One sidebar entry: icon, label and an optional PRO tag (38 px, radius 10)."""

    def __init__(self, route, icon, text, pro=False, parent=None):
        super().__init__(parent)
        self.route = route
        self._icon_name = icon
        self._text = text
        self._active = None
        self.setProperty("gusan", "nav")
        self.setProperty("active", False)
        self.setFixedHeight(38)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName(text + (", Pro feature" if pro else ""))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 8, 0)
        lay.setSpacing(8)
        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(19, 19)
        lay.addWidget(self.icon_label, 0, Qt.AlignVCenter)
        self.text_label = QLabel(text, self)
        self.text_label.setProperty("navText", True)
        lay.addWidget(self.text_label, 1, Qt.AlignVCenter)
        self.pro_tag = None
        if pro:
            self.pro_tag = ProTag("xs", parent=self)
            lay.addWidget(self.pro_tag, 0, Qt.AlignVCenter)
        for w in (self.icon_label, self.text_label, self.pro_tag):
            if w is not None:
                w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.set_active(False)

    def text(self):  # noqa: D401 - the visible label (QPushButton's own text stays empty)
        return self._text

    def is_active(self):
        return bool(self._active)

    def set_active(self, on):
        on = bool(on)
        if on == self._active:
            return
        self._active = on
        self.icon_label.setPixmap(icons.pixmap(self._icon_name, 19, "blue-tx" if on else "text2",
                                               sw=2 if on else 1.7))
        self.text_label.setFont(theme.font(13.5, 600 if on else 500))
        self.setAccessibleDescription("Current page" if on else "")
        theme.set_prop(self, "active", on)
        theme.elevate(self, "item" if on else None)


class _AccountRow(QPushButton):
    """The clickable avatar + name (+ tier pill) line; opens Account & plan."""

    def __init__(self, name, tier, avatar_size, height, pill_size, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "accountRow")
        self.setProperty("active", False)
        self.setFixedHeight(height)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        label = {"pro": "Pro", "free": "Free"}.get(tier, "Guest")
        self.setAccessibleName("Account: %s, %s" % (name, label))
        self.setToolTip("Account and plan")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8 if avatar_size >= 30 else 2, 0, 8 if avatar_size >= 30 else 2, 0)
        lay.setSpacing(10 if avatar_size >= 30 else 8)
        self.avatar = Avatar(name, avatar_size, self)
        lay.addWidget(self.avatar, 0, Qt.AlignVCenter)
        self.name_label = QLabel(name, self)
        self.name_label.setProperty("navText", True)
        self.name_label.setFont(theme.font(13.5, 600))
        self.name_label.setMinimumWidth(10)
        self.name_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.name_label, 1, Qt.AlignVCenter)
        if tier == "pro":
            self.badge = ProTag(pill_size, parent=self)
        elif tier == "free":
            self.badge = FreeBadge(pill_size, parent=self)
        else:
            self.badge = GuestBadge(pill_size, parent=self)
        lay.addWidget(self.badge, 0, Qt.AlignVCenter)
        for w in (self.avatar, self.name_label, self.badge):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_active(self, on):
        theme.set_prop(self, "active", bool(on))
        theme.elevate(self, "item" if on else None)


class Sidebar(QFrame):
    navigate = Signal(str)
    upgrade = Signal()
    account = Signal()

    WIDTH = 232

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "sidebar")
        self.setFixedWidth(self.WIDTH)
        self.setAccessibleName("Main")
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 16, 12, 16)
        root.setSpacing(3)

        brand = QWidget(self)
        brand.setFixedHeight(44)
        bl = QHBoxLayout(brand)
        bl.setContentsMargins(8, 0, 8, 0)
        bl.setSpacing(10)
        self.logo = QLabel(brand)
        self.logo.setPixmap(icons.logo_pixmap(30))
        self.logo.setFixedSize(30, 30)
        self.logo.setAccessibleName("Gusan")
        bl.addWidget(self.logo)
        self.brand_label = QLabel("Gusan", brand)
        theme.set_role(self.brand_label, "brand")
        bl.addWidget(self.brand_label)
        bl.addStretch(1)
        root.addWidget(brand)
        root.addSpacing(10)

        self.items = {}
        for route, icon, text, _pro in NAV:
            # No PRO tags in the list (the founder, 2.0): a Free user who
            # uses a Pro feature gets the Pro window right there instead.
            it = NavItem(route, icon, text, False, self)
            it.clicked.connect(lambda _=False, r=route: self.navigate.emit(r))
            root.addWidget(it)
            self.items[route] = it
        root.addStretch(1)

        self._acct_slot = QWidget(self)
        self._acct_lay = QVBoxLayout(self._acct_slot)
        self._acct_lay.setContentsMargins(0, 0, 0, 0)
        self._acct_lay.setSpacing(0)
        root.addWidget(self._acct_slot)
        # The mockup's 6 px spacer is a flex item, so the 3 px gap sits on both
        # sides of it (12 px). A Qt spacer gets no layout spacing: 9 + 3.
        root.addSpacing(9)

        self.settings_item = NavItem("settings", "gear", "Settings", False, self)
        self.settings_item.clicked.connect(lambda _=False: self.navigate.emit("settings"))
        root.addWidget(self.settings_item)
        self.items["settings"] = self.settings_item

        self.account_row = None
        self.upgrade_button = None
        self._tier = None
        self._active_route = None
        self.set_account("guest", "Guest")

    # ── account ──
    def tier(self):
        return self._tier

    def set_account(self, tier, name):
        """tier: 'pro' | 'free' | 'guest'. Rebuilds the account area."""
        tier = tier if tier in ("pro", "free") else "guest"
        name = (name or "").strip() or ("Guest" if tier == "guest" else "Account")
        if (tier, name) == (self._tier, getattr(self, "_name", None)):
            return
        self._tier, self._name = tier, name
        while self._acct_lay.count():
            w = self._acct_lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.upgrade_button = None
        if tier == "pro":
            self.account_row = _AccountRow(name, tier, 30, 44, "m", self._acct_slot)
            self.account_row.clicked.connect(lambda _=False: self.account.emit())
            self._acct_lay.addWidget(self.account_row)
        else:
            card = QFrame(self._acct_slot)
            card.setProperty("gusan", "planCard")
            cl = QVBoxLayout(card)
            # 10 px padding as in the mockup; the account row reaches 2 px into
            # it on each side for its focus ring (its own margins give them
            # back), so the avatar and badge still sit on the 10 px line.
            cl.setContentsMargins(8, 10, 8, 10)
            cl.setSpacing(8)
            self.account_row = _AccountRow(name, tier, 26, 26, "s", card)
            self.account_row.clicked.connect(lambda _=False: self.account.emit())
            cl.addWidget(self.account_row)
            btn = QPushButton("Upgrade to Pro", card)
            btn.setProperty("gusan", "upgradeQuiet")
            btn.setFixedHeight(30)
            btn.setFont(theme.font(12.5, 700))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.TabFocus)
            theme.focus_ring(btn)
            btn.setAccessibleName("Upgrade to Pro")
            btn.clicked.connect(lambda _=False: self.upgrade.emit())
            bl = QHBoxLayout()
            bl.setContentsMargins(2, 0, 2, 0)
            bl.addWidget(btn)
            cl.addLayout(bl)
            self.upgrade_button = btn
            self._acct_lay.addWidget(card)
        # Same rule as set_active: only the Pro row lights up on Account & plan.
        self.account_row.set_active(self._active_route == "account" and tier == "pro")

    # ── selection ──
    def set_active(self, route):
        self._active_route = route
        for r, it in self.items.items():
            it.set_active(r == route)
        if self.account_row is not None:
            self.account_row.set_active(route == "account" and self._tier == "pro")

    def active_route(self):
        return self._active_route
