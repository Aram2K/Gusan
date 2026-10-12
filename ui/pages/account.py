# -*- coding: utf-8 -*-
"""Account & plan (Gusan 2.0): the plan card (the page's one Upgrade, or
Manage subscription for Pro), who you are (Sign out / Sign in), Smart Action
tries, Terms and Privacy, and a quiet Delete account card.

Everything shown comes from account_actions.account_view(app.auth, app.cfg)
and is redrawn by refresh_account() (sign-in, sign-out, plan changes) and
every time the page opens (the tries counter moves).

Integration notes for Wave 4
----------------------------
* Buttons call the controller as it is: Upgrade -> app._pro_upsell() (the
  current Pro window; a guest's offers sign-up first), Manage subscription ->
  app.open_billing(), Sign out -> app.sign_out(), Sign in ->
  app.show_auth_gate() (Wave 4 / owner 7: point it at the new auth sheet),
  Preview as (super admins only) -> app.set_admin_tier_override(value).
* Delete account: typed confirmation (account_actions.DELETE_WORD), then
  account_actions.delete_account on a worker; on success the page calls
  app.sign_out() and shows the server's message. The controller's
  sig_auth_changed -> main_win.refresh_account() then redraws this page.
* Prices in the Free plan line ("3-day free trial · €7.99/mo or €59/yr") are
  the mockup's copy; if they change, change PRICE_LINE (the Pro window has
  its own copy in ui/pro_dialog.py).
"""

import logging

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QBoxLayout, QFrame, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

import account_actions
from ui import theme
from ui.components import (Avatar, Button, Card, Chip, Divider, FreeBadge, PlanCard, GuestBadge, PageHeader,
                           ProTag, Segmented, label)
from ui.pages import settings as S

logger = logging.getLogger("transcribe")

PRICE_LINE = "3-day free trial · €7.99/mo or €59/yr"
FREE_SUB = "Dictation, audio to text and your dictionary."
# icon, name, detail (None = the green "Hidden on screen share" chip)
PRO_FEATURES = (
    ("people", "Meetings with AI notes", "Record a call, get the notes"),
    ("incognito", "Live Assistant", None),
    ("wand", "Unlimited Smart Actions", "Emails, lists and translations by voice"),
    ("cloud", "Gusan Cloud", "Fastest speech, nothing to download"),
)
ADMIN_TIERS = (("auto", "Auto"), ("guest", "Guest"), ("free", "Free"), ("pro", "Pro"))
SIDE_W = 330
# Narrower than this (the page's visible width), the side cards go under the
# plan card: the plan card's feature rows need about 530 px (the boards are
# 1280 px; the shell's smallest window is 960).
STACK_BELOW = 32 + 540 + 20 + SIDE_W + 32


def _badge(tier, size="m"):
    if tier == "pro":
        return ProTag(size)
    if tier == "free":
        return FreeBadge(size)
    return GuestBadge(size)


class FeatureRow(QFrame):
    """One Pro feature: purple icon, name, a short detail (or the Hidden chip),
    and a green check when the plan includes it. 48 px, faint hairline."""

    def __init__(self, icon, name, detail, have, last=False, parent=None):
        super().__init__(parent)
        self._last = last
        self.setFixedHeight(48)
        self.setAccessibleName(name + (", included" if have else ""))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(S.icon_label(icon, 19, "pro-tx", parent=self), 0, Qt.AlignVCenter)
        lay.addWidget(S.text_label(name, 14.5, 600, color="ink", parent=self), 0, Qt.AlignVCenter)
        lay.addStretch(1)
        if detail is None:
            self.detail = Chip("Hidden on screen share", "ok", "incognito", parent=self)
        else:
            self.detail = S.text_label(detail, 13, role="muted", parent=self)
        lay.addWidget(self.detail, 0, Qt.AlignVCenter)
        self.check = None
        if have:
            self.check = S.icon_label("check", 16, "ok", sw=2.6, parent=self)
            lay.addWidget(self.check, 0, Qt.AlignVCenter)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._last:
            return
        p = QPainter(self)
        p.setPen(QPen(theme.qcolor("rgba(15,23,42,0.06)"), 1))
        y = self.height() - 0.5
        p.drawLine(0, int(y), self.width(), int(y))
        p.end()


class TriesBar(QWidget):
    """Five segments: the Smart Action tries left (green) and used (grey)."""

    def __init__(self, left, total, parent=None):
        super().__init__(parent)
        self._left, self._total = int(left or 0), max(1, int(total or 5))
        self.setFixedHeight(8)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName("%d of %d Smart Action tries left" % (self._left, self._total))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        gap = 4.0
        w = (self.width() - gap * (self._total - 1)) / float(self._total)
        for i in range(self._total):
            p.setBrush(theme.qcolor("ok" if i < self._left else "line"))
            p.drawRoundedRect(QRectF(i * (w + gap), 0, w, 8), 4, 4)
        p.end()


class AccountPage(QWidget):
    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.opts = {}
        self.view = {}
        self.setProperty("gusan", "page")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = PageHeader("Account & plan", parent=self)
        root.addWidget(self.header)
        body = QBoxLayout(QBoxLayout.LeftToRight)
        body.setContentsMargins(32, 0, 32, 28)
        body.setSpacing(20)
        self.body = body
        self.plan_holder = QVBoxLayout()
        self.plan_holder.setContentsMargins(0, 0, 0, 0)
        body.addLayout(self.plan_holder, 1)
        self.side = QWidget(self)
        self.side.setFixedWidth(SIDE_W)
        self.side_lay = QVBoxLayout(self.side)
        self.side_lay.setContentsMargins(0, 0, 0, 0)
        self.side_lay.setSpacing(14)
        body.addWidget(self.side, 0)
        root.addLayout(body)
        root.addStretch(1)
        self._stacked = False
        self._viewport = None
        self.buttons = {}
        self.delete_note = None
        self.notice_label = None
        self._notice = None             # (text, colour) kept across rebuilds, e.g. after deleting the account
        self._busy = False
        self.refresh_account()

    # ── building ──
    def _read_view(self):
        try:
            view = account_actions.account_view(getattr(self.app, "auth", None), S.cfg_of(self.app))
        except Exception:
            logger.warning("account_view failed", exc_info=True)
            view = {"signed_in": S.is_signed_in(self.app), "name": "Guest", "email": "", "tier": "guest",
                    "pro": False, "plan_line": "", "smart_text": "", "smart_tries_left": None,
                    "smart_tries_total": 5, "is_admin": False, "admin_tier": "auto",
                    "buttons": {"sign_in": True, "sign_out": False, "upgrade": True, "manage": False,
                                "delete": False, "admin": False}}
        # The controller's own Pro check is the one every gate uses.
        if S.is_pro(self.app) and not view.get("pro"):
            line = view.get("plan_line")
            view = dict(view, pro=True, tier="pro", plan_line=line if line and line != "Free" else "Pro",
                        smart_text="Unlimited", smart_tries_left=None)
            view["buttons"] = dict(view.get("buttons") or {}, upgrade=False)
        return view

    def refresh_account(self):
        self.view = self._read_view()
        S.clear_layout(self.plan_holder)
        S.clear_layout(self.side_lay)
        self.buttons = {}
        self.plan_card = self._plan_card(self.view)
        self.plan_holder.addWidget(self.plan_card)
        self.account_card = self._account_card(self.view)
        self.side_lay.addWidget(self.account_card)
        self.usage_card = self._usage_card(self.view)
        self.side_lay.addWidget(self.usage_card)
        if self.view.get("buttons", {}).get("admin"):
            self.side_lay.addWidget(self._admin_card(self.view))
        self.side_lay.addStretch(1)
        self.side_lay.addWidget(self._legal())
        self.delete_card = None
        if self.view.get("buttons", {}).get("delete"):
            self.delete_card = self._delete_card()
            self.side_lay.addWidget(self.delete_card)

    def _plan_card(self, v):
        tier = v.get("tier") or "guest"
        pro = bool(v.get("pro"))
        if pro:
            card = Card(pad=(28, 24), gap=12)
            eyebrow_color, name, sub, list_title = "pro-tx", "Pro", v.get("plan_line") or "Pro", "Included"
        else:
            card = PlanCard(pad=(28, 24), gap=12)
            eyebrow_color = "blue-tx"
            name = "Free" if tier == "free" else "Guest"
            sub, list_title = FREE_SUB, "Pro adds"
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        card.setAccessibleName("Your plan: %s" % name)
        head = QVBoxLayout()
        head.setSpacing(4)
        eb = label("Your plan", "eyebrow", card)
        S.tint(eb, eyebrow_color)
        head.addWidget(eb)
        nrow = QHBoxLayout()
        nrow.setSpacing(12)
        self.plan_name = S.text_label(name, 40, 700, role="title", display=True, parent=card)
        self.plan_name.setFont(theme.font(40, 700, display=True, letter_spacing=-0.8))
        self.plan_name.setFixedHeight(46)
        nrow.addWidget(self.plan_name, 0, Qt.AlignVCenter)
        self.plan_badge = _badge("pro" if pro else tier)
        nrow.addWidget(self.plan_badge, 0, Qt.AlignVCenter)
        nrow.addStretch(1)
        head.addLayout(nrow)
        self.plan_sub = S.text_label(sub, 14, role="body", parent=card, wrap=True)
        head.addWidget(self.plan_sub)
        card.add(head)
        card.layout().addSpacing(6)            # the divider's 6 px top margin (+ the 12 px gap)
        card.add(Divider(card))
        card.add(S.text_label(list_title, 14, 600, role="h3", parent=card))
        feats = QVBoxLayout()
        feats.setSpacing(0)
        self.feature_rows = []
        for i, (icon, fname, detail) in enumerate(PRO_FEATURES):
            r = FeatureRow(icon, fname, detail, have=pro, last=(i == len(PRO_FEATURES) - 1), parent=card)
            self.feature_rows.append(r)
            feats.addWidget(r)
        card.add(feats)
        buttons = v.get("buttons") or {}
        has_foot = (not pro and buttons.get("upgrade", True)) or (pro and buttons.get("manage"))
        if not has_foot:                        # e.g. a Pro trial: no portal yet, so no empty footer
            card.layout().addStretch(1)
            return card
        card.add(Divider(card))
        card.layout().addSpacing(4)
        foot = QHBoxLayout()
        foot.setSpacing(16)
        if not pro and buttons.get("upgrade", True):
            b = Button("Upgrade to Pro", "pro", "l", parent=card)
            b.setFixedHeight(40)
            b.clicked.connect(lambda _=False: S.call(self.app, "_pro_upsell"))
            self.buttons["upgrade"] = b
            foot.addWidget(b, 0, Qt.AlignVCenter)
            foot.addWidget(S.text_label(PRICE_LINE, 13, role="muted", parent=card), 0, Qt.AlignVCenter)
        elif pro and buttons.get("manage"):
            b = Button("Manage subscription", "secondary", "l", parent=card)
            b.clicked.connect(lambda _=False: S.call(self.app, "open_billing"))
            self.buttons["manage"] = b
            foot.addWidget(b, 0, Qt.AlignVCenter)
            foot.addWidget(S.text_label("Card, invoices and cancelling.", 13, role="muted", parent=card), 0,
                           Qt.AlignVCenter)
        foot.addStretch(1)
        card.add(foot)
        card.layout().addStretch(1)
        return card

    def _account_card(self, v):
        signed = bool(v.get("signed_in"))
        tier = "pro" if v.get("pro") else (v.get("tier") or "guest")
        card = Card(pad=(20, 18), gap=14)
        name = v.get("name") or ("Guest" if not signed else "Your account")
        card.setAccessibleName("Account: %s" % name)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.avatar = Avatar(name, 44, card)
        top.addWidget(self.avatar, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        nrow = QHBoxLayout()
        nrow.setSpacing(10)
        self.name_label = S.text_label(name, 20, 600, role="h2", display=True, parent=card)
        self.name_label.setFixedHeight(26)
        nrow.addWidget(self.name_label, 0, Qt.AlignVCenter)
        self.badge = _badge(tier)
        nrow.addWidget(self.badge, 0, Qt.AlignVCenter)
        nrow.addStretch(1)
        col.addLayout(nrow)
        self.email_label = S.text_label(v.get("email") if signed else "Not signed in", 13.5, role="muted",
                                        parent=card)
        col.addWidget(self.email_label)
        top.addLayout(col, 1)
        card.add(top)
        card.add(Divider(card))
        row = QHBoxLayout()
        row.setSpacing(12)
        if signed:
            b = Button("Sign out", "secondary", "s", icon="power", parent=card)
            b.clicked.connect(lambda _=False: S.call(self.app, "sign_out"))
            self.buttons["sign_out"] = b
        else:
            b = Button("Sign in", "primary", "s", parent=card)
            b.clicked.connect(lambda _=False: S.call(self.app, "show_auth_gate"))
            self.buttons["sign_in"] = b
        row.addWidget(b, 0, Qt.AlignVCenter)
        row.addWidget(S.text_label("Your history stays on this computer", 12, role="muted", parent=card,
                                   wrap=True), 1, Qt.AlignVCenter)
        card.add(row)
        self.notice_label = None
        if self._notice:
            text, color = self._notice
            self.notice_label = S.text_label(text, 12.5, 600, color=color, parent=card, wrap=True)
            self.notice_label.setAccessibleName(text)
            card.add(self.notice_label)
        return card

    def _usage_card(self, v):
        card = Card(pad=(20, 18), gap=12)
        card.setAccessibleName("Usage")
        eb = label("Usage", "eyebrow", card)
        S.tint(eb, "muted")
        card.add(eb)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.tries_bar = None
        if v.get("pro"):
            row.addWidget(S.text_label("Smart Actions", 14, 600, role="h3", parent=card))
            row.addStretch(1)
            self.usage_text = S.text_label("Unlimited", 13.5, 600, color="ok-tx", parent=card)
            row.addWidget(self.usage_text)
            card.add(row)
        else:
            row.addWidget(S.text_label("Smart Action tries", 14, 600, role="h3", parent=card))
            row.addStretch(1)
            self.usage_text = S.text_label(v.get("smart_text") or "", 13, role="muted", parent=card)
            row.addWidget(self.usage_text)
            card.add(row)
            left = v.get("smart_tries_left")
            if left is not None:
                self.tries_bar = TriesBar(left, v.get("smart_tries_total") or 5, card)
                card.add(self.tries_bar)
        return card

    def _admin_card(self, v):
        card = Card(pad=(20, 14), gap=10)
        card.setAccessibleName("Preview as")
        eb = label("Preview as", "eyebrow", card)
        S.tint(eb, "muted")
        card.add(eb)
        values = [k for k, _t in ADMIN_TIERS]
        cur = v.get("admin_tier") or "auto"
        seg = Segmented([t for _k, t in ADMIN_TIERS], values.index(cur) if cur in values else 0,
                        "Preview as", size="s", parent=card)
        seg.changed.connect(lambda i: S.call(self.app, "set_admin_tier_override", values[i]))
        self.admin_segmented = seg
        card.add(seg)
        return card

    def _legal(self):
        w = QWidget(self.side)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(8)
        for i, (text, url) in enumerate((("Terms of Service", account_actions.TERMS_URL),
                                         ("Privacy Policy", account_actions.PRIVACY_URL))):
            if i:
                lay.addWidget(S.text_label("·", 13, color="faint", parent=w), 0, Qt.AlignVCenter)
            b = Button(text, "link", "s", parent=w)
            b.clicked.connect(lambda _=False, u=url: S.open_url(u))
            self.buttons[text] = b
            lay.addWidget(b, 0, Qt.AlignVCenter)
        lay.addStretch(1)
        return w

    def _delete_card(self):
        box = S._Box("rgba(255,255,255,0.45)", "line", 1, 16, self.side)
        box.setAccessibleName("Delete account")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        b = S.red_button("Deleting…" if self._busy else "Delete account…", parent=box)
        b.setAccessibleName("Delete account")
        b.setEnabled(not self._busy)            # a redraw mid-delete keeps showing it
        b.clicked.connect(lambda _=False: self._delete_account())
        self.buttons["delete"] = b
        lay.addWidget(b, 0, Qt.AlignLeft)
        self.delete_note = S.text_label("Also cancels any subscription.", 12.5, role="muted", parent=box, wrap=True)
        lay.addWidget(self.delete_note)
        return box

    # ── actions ──
    def _delete_account(self):
        if self._busy:
            return
        word = account_actions.DELETE_WORD
        choice = S.ask(self, "Delete your account?",
                       "This cancels any subscription right away and deletes your account. "
                       "Your history stays on this computer.",
                       [("cancel", "Cancel", "ghost"), ("delete", "Delete account", "danger")], typed=word)
        if choice != "delete":
            return
        self._busy = True
        b = self.buttons.get("delete")
        if b is not None:
            b.setEnabled(False)
            b.setText("Deleting…")
        S.run_bg(account_actions.delete_account, getattr(self.app, "auth", None), then=self._deleted,
                 error=lambda e: self._deleted((False, "Couldn't reach the server. Try again.")), owner=self)

    def _deleted(self, res):
        self._busy = False
        ok, msg = res if isinstance(res, tuple) else (False, "")
        if ok:
            # sign_out() redraws the page as a guest a moment later (sig_auth_changed):
            # keep the answer on the account card so it doesn't vanish with the old one.
            self._notice = (msg or "Your account was deleted.", "ok-tx")
            S.call(self.app, "sign_out")
        b = self.buttons.get("delete")
        if b is not None and S.alive(b):
            b.setEnabled(not ok)
            b.setText("Delete account…")
        if self.delete_note is not None and S.alive(self.delete_note):
            self.delete_note.setText(msg or ("Your account was deleted." if ok else "Couldn't delete the account."))
            S.tint(self.delete_note, "ok-tx" if ok else "red-tx")

    # ── narrow windows ──
    def _visible_width(self):
        """The width the page really gets: the shell's scroll viewport (the
        page itself can't get narrower than its content's minimum)."""
        vp = self._viewport
        return vp.width() if vp is not None and S.alive(vp) else self.width()

    def _apply_width(self):
        stacked = self._visible_width() < STACK_BELOW
        if stacked == self._stacked:
            return
        self._stacked = stacked
        self.body.setDirection(QBoxLayout.TopToBottom if stacked else QBoxLayout.LeftToRight)
        self.body.setSpacing(14 if stacked else 20)
        if stacked:
            self.side.setMinimumWidth(0)
            self.side.setMaximumWidth(16777215)
        else:
            self.side.setFixedWidth(SIDE_W)

    def is_stacked(self):
        return self._stacked

    def showEvent(self, e):
        super().showEvent(e)
        parent = self.parentWidget()
        if parent is not None and parent is not self._viewport:
            if self._viewport is not None and S.alive(self._viewport):
                self._viewport.removeEventFilter(self)
            parent.installEventFilter(self)
            self._viewport = parent
        self._apply_width()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_width()

    def eventFilter(self, obj, ev):
        if obj is self._viewport and ev.type() == QEvent.Resize:
            self._apply_width()
        return False

    # ── shell hooks ──
    def on_open(self, **opts):
        self.opts = dict(opts)
        if not self._busy:
            self._notice = None             # a one-time answer: gone once the page opens again
            self.refresh_account()

    def on_setting_changed(self, key):
        if key in ("admin_tier_override", "privacy_mode") and not self._busy:
            self.refresh_account()


def create(main_app):
    return AccountPage(main_app)
