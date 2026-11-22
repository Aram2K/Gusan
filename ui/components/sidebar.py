"""The main window's left navigation (232 px), as in the mockups.

Logo + "Gusan", the six pages (no PRO tags: Pro features open the Pro
window when a Free user uses them), then "Invite friends" (the gift, with
three small progress dots - or a glowing READY pill when a free month is
ready: set_invite), the account at the bottom, and Settings last. The
account area by plan:

* Free: a small card - the first name, the FREE pill and one quiet violet
  "Upgrade to Pro".
* Guest (not signed in; also a super admin previewing Guest): the same card
  with a neutral person icon, the name "User" (never a profile name or an
  initial), the GUEST pill and ONE blue "Sign in / Sign up". No upgrade wording.
* Pro (and a trial): just the name and the PRO pill.

The labels live in account_actions (GUEST_NAME, SIGN_IN_UP, SIGN_IN_UP_A11Y,
UPGRADE).

Signals: navigate(route) (the invite entry navigates to "invite"), upgrade() (Free), sign_in() (guest),
account().
"""

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from account_actions import GUEST_NAME, SIGN_IN_UP, SIGN_IN_UP_A11Y, UPGRADE
from ui import theme
from ui.components import icons
from ui.components.badges import FreeBadge, GuestBadge, ProTag
from ui.components.buttons import Button

# route, icon, label, Pro feature
NAV = (
    ("home", "home", "Home", False),
    ("dictation", "mic", "Dictation", False),
    ("smart_actions", "wand", "Smart Actions", True),
    ("meetings", "people", "Meetings", True),
    ("live_assistant", "incognito", "Live Assistant", True),
    ("audio_to_text", "wave", "Audio to text", False),
)


class Avatar(QWidget):
    """Round avatar: the person's choice (ui/components/avatars.py: the name's first letter on a colour, a preset
    picture or their own photo; Gusan blue with the letter by default). A guest (``guest=True``) gets a neutral
    person icon instead - never a letter."""

    def __init__(self, name="", size=30, parent=None, guest=False):
        super().__init__(parent)
        self._size = int(size)
        self._guest = bool(guest)
        self._letter = (name or "?").strip()[:1].upper() or "?"
        self.setFixedSize(self._size, self._size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        from ui.components import avatars
        avatars.register(self)                  # a new choice repaints it

    def set_name(self, name, guest=None):
        if guest is not None:
            self._guest = bool(guest)
        self._letter = (name or "?").strip()[:1].upper() or "?"
        self.update()

    def is_guest(self):
        return self._guest

    def letter(self):
        """The letter it shows ("" for a guest's person icon)."""
        return "" if self._guest else self._letter

    def sizeHint(self):
        return QSize(self._size, self._size)

    def paintEvent(self, _e):
        from ui.components import avatars
        p = QPainter(self)
        # (avatars.paint draws half a pixel inside: the circle's edge was cut at the top and left at 125 percent)
        avatars.paint(p, QRectF(0, 0, self._size, self._size), self._letter, guest=self._guest)
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


class InviteNavItem(NavItem):
    """"Invite friends", just above the account card: the gift, and on the right the program's progress - three
    dots (filled = a friend who counts, a ring = one on the way) or a glowing READY pill when a month is ready
    (ui/components/celebrate.py). Nothing on the right until the first answer is known."""

    def __init__(self, parent=None):
        super().__init__("invite", "gift", "Invite friends", False, parent)
        from ui.components.celebrate import InviteDots, ReadyPill
        lay = self.layout()
        self.dots = InviteDots(self)
        self.ready_pill = ReadyPill(self)
        for w in (self.dots, self.ready_pill):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            w.hide()
            lay.addWidget(w, 0, Qt.AlignVCenter)
        self._invite = (0, 0, False, False)

    def invite_state(self):
        """(counted, progress, ready, known)."""
        return self._invite

    def set_invite(self, counted=0, progress=0, ready=False, known=True):
        state = (int(counted or 0), int(progress or 0), bool(ready), bool(known))
        if state == self._invite:
            return
        self._invite = state
        self.dots.set_state(state[0], state[1], animate=self.isVisible())
        self.dots.setVisible(known and not ready)
        self.ready_pill.setVisible(bool(ready))
        # The pill carries its own 6 px of glow: the row's right edge gives it back.
        self.layout().setContentsMargins(10, 0, 2 if ready else 8, 0)
        if ready:
            what = "your free month is ready"
        elif known:
            what = "%d of 3 friends" % state[0]
        else:
            what = ""
        self.setAccessibleName("Invite friends" + (", " + what if what else ""))


class _AccountRow(QPushButton):
    """The clickable avatar + name (+ tier pill) line; opens Account & plan."""

    def __init__(self, name, tier, avatar_size, height, pill_size, parent=None, badge=""):
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
        self.avatar = Avatar(name, avatar_size, self, guest=(tier not in ("pro", "free")))
        lay.addWidget(self.avatar, 0, Qt.AlignVCenter)
        self.name_label = QLabel(name, self)
        self.name_label.setProperty("navText", True)
        self.name_label.setFont(theme.font(13.5, 600))
        self.name_label.setMinimumWidth(10)
        self.name_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.name_label, 1, Qt.AlignVCenter)
        if tier == "pro":
            # Pro from invites only: "PRO · 14 DAYS" (account_actions.invite_badge), else the plain PRO pill.
            self.badge = ProTag(pill_size, text=badge or "PRO", parent=self)
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
    sign_in = Signal()
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

        # Not in ``items`` (the pages): its own entry, right above the account card.
        self.invite_item = InviteNavItem(self)
        self.invite_item.clicked.connect(lambda _=False: self.navigate.emit("invite"))
        root.addWidget(self.invite_item)
        root.addSpacing(11)                 # the mockup's 8 px spacer + the 3 px gap on its far side

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
        self.upgrade_button = None          # Free's violet "Upgrade to Pro" (None for guests and Pro)
        self.sign_in_button = None          # a guest's blue "Sign in / Sign up" (None for Free and Pro)
        self._tier = None
        self._active_route = None
        self.set_account("guest", GUEST_NAME)

    # ── account ──
    def tier(self):
        return self._tier

    def set_account(self, tier, name, badge=""):
        """tier: 'pro' | 'free' | 'guest'. Rebuilds the account area. A guest
        is always "User": a name handed in for one (a profile the session still
        holds) is ignored here too, so no caller can put a person on the card.
        ``badge``: the Pro pill's words when Pro comes from invites ("Pro · 14 days")."""
        tier = tier if tier in ("pro", "free") else "guest"
        name = GUEST_NAME if tier == "guest" else ((name or "").strip() or "Account")
        badge = badge if tier == "pro" else ""
        if (tier, name, badge) == (self._tier, getattr(self, "_name", None), getattr(self, "_badge", "")):
            return
        self._tier, self._name, self._badge = tier, name, badge
        while self._acct_lay.count():
            w = self._acct_lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.upgrade_button = None
        self.sign_in_button = None
        if tier == "pro":
            self.account_row = _AccountRow(name, tier, 30, 44, "m", self._acct_slot, badge=badge)
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
            if tier == "guest":
                # The app's primary blue (the other blue buttons), not violet.
                btn = Button(SIGN_IN_UP, "primary", "s", parent=card, full=True)
                btn.setFont(theme.font(12.5, 700))
                btn.setAccessibleName(SIGN_IN_UP_A11Y)
                btn.clicked.connect(lambda _=False: self.sign_in.emit())
                self.sign_in_button = btn
            else:
                btn = QPushButton(UPGRADE, card)
                btn.setProperty("gusan", "upgradeQuiet")
                btn.setFixedHeight(30)
                btn.setFont(theme.font(12.5, 700))
                btn.setCursor(Qt.PointingHandCursor)
                btn.setFocusPolicy(Qt.TabFocus)
                theme.focus_ring(btn)
                btn.setAccessibleName(UPGRADE)
                btn.clicked.connect(lambda _=False: self.upgrade.emit())
                self.upgrade_button = btn
            bl = QHBoxLayout()
            bl.setContentsMargins(2, 0, 2, 0)
            bl.addWidget(btn)
            cl.addLayout(bl)
            self._acct_lay.addWidget(card)
        # Same rule as set_active: only the Pro row lights up on Account & plan.
        self.account_row.set_active(self._active_route == "account" and tier == "pro")

    # ── selection ──
    def set_invite(self, counted=0, progress=0, ready=False, known=True, visible=True):
        """The invite entry's progress (ui/invite_hub.InviteHub.sidebar_state)."""
        self.invite_item.set_invite(counted, progress, ready, known)
        self.invite_item.setVisible(bool(visible))

    def set_active(self, route):
        self._active_route = route
        for r, it in self.items.items():
            it.set_active(r == route)
        self.invite_item.set_active(route == "invite")
        if self.account_row is not None:
            self.account_row.set_active(route == "account" and self._tier == "pro")

    def active_route(self):
        return self._active_route
