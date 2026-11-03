# Interactive "Go Pro" upgrade dialog - benefits + plan picker + checkout.

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame
)

import account_actions
import pricing_policy


class ProDialog(QDialog):
    """A friendly, visual upgrade dialog. Shows what Pro unlocks, lets the user
    pick Monthly / Annual, and sends them to Stripe checkout (after sign-in)."""

    def __init__(self, main_app=None, feature=None, parent=None):
        # parent: the Gusan window while it is on screen, so the Pro window
        # opens over it (never behind it).
        super().__init__(parent)
        self.app = main_app
        self._plan = "yearly"  # default to the committed yearly price
        self.setWindowTitle("Gusan Pro")
        self.setMinimumWidth(440)
        self.setSizeGripEnabled(True)
        if self.app and hasattr(self.app, "style_content"):
            self.setStyleSheet(self.app.style_content)
        self._build(feature)

    def showEvent(self, event):
        super().showEvent(event)
        from ui.winfit import settle_on_screen
        settle_on_screen(self)

    def _build(self, feature):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(12)

        eyebrow = QLabel("GUSAN PRO", self)
        eyebrow.setAlignment(Qt.AlignCenter)
        eyebrow.setStyleSheet("font-size: 11px; font-weight: 800; letter-spacing: 2px; color: #a855f7;")
        root.addWidget(eyebrow)

        title = QLabel("Everything you get with Pro", self)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 22px; font-weight: 800; color: #0f172a;")
        root.addWidget(title)

        if feature:
            sub = QLabel(self._feature_line(feature), self)
            sub.setAlignment(Qt.AlignCenter)
            sub.setObjectName("subtitleLabel")
            sub.setWordWrap(True)
            root.addWidget(sub)

        # ── Benefits (clean checkmarks, no emojis) ──
        card = QFrame(self)
        card.setObjectName("glassCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 16, 20, 16)
        cl.setSpacing(10)
        for head, desc in (
            ("Meeting recording with AI notes", "Record any call and get clean, summarized minutes."),
            ("Smart Actions", "Translate, rewrite, summarize and draft emails by voice."),
            ("Fast cloud transcription", "Accurate, with no API key, no setup and no timeouts."),
            ("Priority models and support", "The best models first, and a direct line to us."),
        ):
            row = QHBoxLayout()
            row.setSpacing(12)
            ic = QLabel("✓", card)
            ic.setStyleSheet("font-size: 16px; font-weight: 800; color: #a855f7;")
            row.addWidget(ic, 0, Qt.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(1)
            h = QLabel(head, card)
            h.setStyleSheet("font-weight: 700; font-size: 14px; color: #0f172a;")
            h.setWordWrap(True)
            d = QLabel(desc, card)
            d.setObjectName("subtitleLabel")
            d.setWordWrap(True)
            col.addWidget(h)
            col.addWidget(d)
            row.addLayout(col, 1)
            cl.addLayout(row)
        root.addWidget(card)

        # ── Plan picker ──
        plans = QVBoxLayout()
        plans.setSpacing(8)
        self.card_monthly = self._plan_card("Monthly", "€15.99", "/mo", None, "Cancel anytime", "monthly")
        self.card_yearly = self._plan_card(
            "Yearly", "€9.99", "/mo", "€15.99", "12-month commitment", "yearly",
            legal=pricing_policy.YEARLY_NOTE)
        self.card_lifetime = self._plan_card(
            "Lifetime", "€199", " once", None, "First 200 people", "lifetime",
            legal=pricing_policy.LIFETIME_NOTE)
        plans.addWidget(self.card_monthly)
        plans.addWidget(self.card_yearly)
        plans.addWidget(self.card_lifetime)
        root.addLayout(plans)
        self.spots_label = self.card_lifetime.findChild(QLabel, "planBadge")

        # ── CTA ──
        self.btn_go = QPushButton(pricing_policy.CHECKOUT_BUTTON, self)
        self.btn_go.setObjectName("primaryButton")
        self.btn_go.setMinimumHeight(46)
        self.btn_go.clicked.connect(self._go)
        root.addWidget(self.btn_go)

        self.note = QLabel("", self)
        self.note.setObjectName("subtitleLabel")
        self.note.setAlignment(Qt.AlignCenter)
        self.note.setWordWrap(True)
        root.addWidget(self.note)

        later = QPushButton("Maybe later", self)
        later.setFlat(True)
        later.setStyleSheet("color: #64748b; border: none;")
        later.clicked.connect(self.reject)
        root.addWidget(later, alignment=Qt.AlignCenter)

        self._select_plan("yearly")
        self._refresh_note()
        self._ask_spots()

    def _feature_line(self, feature):
        if feature == "Meeting notes":
            return "You've used this month's free cloud meeting note."
        if feature == "Live Assistant session":
            return "You've used this month's free Live Assistant session."
        return f"{feature} is a Pro feature."

    def _ask_spots(self):
        fn = getattr(self.app, "lifetime_spots_left", None) if self.app else None
        if not callable(fn):
            return
        # A daemon thread, not a QThread parented to this window. Closing the
        # window while the request is in flight must not destroy a running
        # thread: Qt aborts the process ("QThread: Destroyed while thread is
        # still running"), which is what froze the offscreen test run.
        from PySide6.QtCore import QObject, Signal
        import threading

        class _Bridge(QObject):
            got = Signal(object)

        bridge = _Bridge()
        bridge.got.connect(self._show_spots)
        self._spots_bridge = bridge

        def work():
            try:
                value = fn()
            except Exception:
                value = None
            # Queued to the GUI thread. deleteLater from here is posted there too,
            # so the bridge outlives the emit even if this window is already gone.
            bridge.got.emit(value)
            bridge.deleteLater()

        threading.Thread(target=work, daemon=True, name="gusan-lifetime-spots").start()

    def _show_spots(self, n):
        if not isinstance(n, int):
            return
        try:
            badge = getattr(self, "spots_label", None)
            if badge is not None:
                badge.setText(f"{n} of 200 left" if n else "Sold out")
        except RuntimeError:
            return  # the window was closed before the answer arrived

    def _plan_card(self, name, price, per, original, badge, plan_id, legal=None):
        card = QFrame(self)
        card.setObjectName("cardFrame")
        card.setCursor(Qt.PointingHandCursor)
        # Tall enough for title + the 30px price line + badge row; a smaller
        # minimum let the layout squeeze the price label, clipping the digits.
        card.setMinimumHeight(112)
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(6)

        top = QLabel(name, card)
        top.setStyleSheet("font-weight: 700; color: #334155; font-size: 13px;")
        v.addWidget(top)

        # Price row, with an optional struck-through "was" price before it.
        prow = QHBoxLayout()
        prow.setSpacing(5)
        if original:
            o = QLabel(f"<s>{original}</s>", card)
            o.setStyleSheet("color: #94a3b8; font-size: 15px;")
            prow.addWidget(o, 0, Qt.AlignBottom)
        p = QLabel(price, card)
        p.setStyleSheet("font-size: 30px; font-weight: 800; color: #0f172a;")
        # Never let the layout squeeze the price below its glyph height
        # (30px font needs ~42px with ascenders/descenders) - that's what
        # was cutting the bottom off the price.
        p.setMinimumHeight(42)
        prow.addWidget(p)
        u = QLabel(per, card)
        u.setObjectName("subtitleLabel")
        prow.addWidget(u, 0, Qt.AlignBottom)
        prow.addStretch()
        v.addLayout(prow)

        # Bottom slot, aligned across both cards: a "Save" pill or a plain note.
        if badge:
            b = QLabel(badge, card)
            b.setObjectName("planBadge" if plan_id == "lifetime" else "proBadge")
        else:
            b = QLabel("billed monthly", card)
            b.setObjectName("subtitleLabel")
        v.addWidget(b, 0, Qt.AlignLeft)
        if legal:
            # Next to this plan: the same commitment / withdrawal wording Checkout shows.
            leg = QLabel(legal, card)
            leg.setWordWrap(True)
            leg.setObjectName("subtitleLabel")
            v.addWidget(leg)

        card.mousePressEvent = lambda _e, pid=plan_id: self._select_plan(pid)
        return card

    def _select_plan(self, plan_id):
        self._plan = plan_id
        for card, pid in ((self.card_monthly, "monthly"), (self.card_yearly, "yearly"),
                          (self.card_lifetime, "lifetime")):
            selected = (pid == plan_id)
            card.setStyleSheet(
                "QFrame#cardFrame { border: 2px solid %s; border-radius: 12px; background: %s; }"
                % (
                    "#a855f7" if selected else "#e2e8f0",
                    "#faf5ff" if selected else "rgba(255, 255, 255, 200)",
                )
            )
        if getattr(self, "note", None) is not None:
            self._refresh_note()

    def _is_guest(self):
        """Not signed in - or a super admin previewing Guest (signed in underneath):
        a subscription must be linked to an account, so the first step is signing in."""
        if not self.app:
            return True
        try:
            return account_actions.is_guest(self.app)
        except Exception:
            auth = getattr(self.app, "auth", None)
            return not bool(auth is not None and getattr(auth, "is_authenticated", False))

    def _refresh_note(self):
        if self._is_guest():
            # Sign in first: a subscription has to belong to an account. The purple
            # button is checkout, so this one stays the app's sign-in colour.
            self.btn_go.setText(account_actions.SIGN_IN_TO_UPGRADE)
            self.btn_go.setStyleSheet("font-size: 15px; font-weight: 700;")
            self.note.setText(account_actions.SIGN_IN_FIRST)
        else:
            self.btn_go.setText(pricing_policy.CHECKOUT_BUTTON)
            self.btn_go.setStyleSheet(
                "font-size: 15px; font-weight: 700;"
                "background-color: #a855f7; border-color: #9333ea;"
            )
            notes = {
                "monthly": pricing_policy.MONTHLY_NOTE,
                "yearly": pricing_policy.YEARLY_NOTE,
                "lifetime": pricing_policy.LIFETIME_NOTE,
            }
            self.note.setText(notes.get(self._plan, pricing_policy.MONTHLY_NOTE))

    def _go(self):
        if not self.app:
            return
        if self._is_guest():
            # A subscription must be linked to an account.
            self.accept()
            if hasattr(self.app, "show_auth_gate"):
                self.app.show_auth_gate()
            return
        if hasattr(self.app, "open_checkout"):
            self.app.open_checkout(self._plan)
        self.accept()
