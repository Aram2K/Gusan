# The "Go Pro" window: one plan card in the website's look (gusan.aibuben.xyz/#pricing) -
# a Yearly / Monthly / Lifetime toggle, the price, what Pro gives, one button to checkout.

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

import account_actions
import pricing_policy

VIOLET = "#a855f7"
VIOLET_DARK = "#9333ea"
INK = "#0f172a"
MUTED = "#64748b"

# Each plan as the card shows it: (toggle label, price, per, struck "was" price, one detail line,
# the line under the button). The full commitment / withdrawal wording is in Stripe Checkout
# (pricing_policy.checkout_extras): here it is said once, plainly, under the button.
PLANS = {
    "yearly": ("Yearly", "€9.99", "/month", "€15.99", "€9.99 each month for 12 months (€119.88)",
               "12-month commitment, then €9.99 a month until you cancel. Secure checkout by Stripe."),
    "monthly": ("Monthly", "€15.99", "/month", None, "Billed every month",
                "€15.99 a month. Cancel anytime. Secure checkout by Stripe."),
    "lifetime": ("Lifetime", "€199", "once", None, "Pay once, keep Pro for good",
                 "One payment, for the first 200 people. Secure checkout by Stripe."),
}
PLAN_ORDER = ("yearly", "monthly", "lifetime")
YEARLY_SAVING = "−38%"
BENEFITS = ("Meeting recording with AI notes", "Smart Actions by voice",
            "Fast cloud transcription", "Priority models and support")


class ProDialog(QDialog):
    """The upgrade window: pick a plan, see its price, go to Stripe checkout (after sign-in)."""

    def __init__(self, main_app=None, feature=None, parent=None):
        # parent: the Gusan window while it is on screen, so the Pro window
        # opens over it (never behind it).
        super().__init__(parent)
        self.app = main_app
        self._plan = "yearly"  # the committed yearly price leads, as on the website
        self.setWindowTitle("Gusan Pro")
        self.setFixedWidth(420)
        if self.app and hasattr(self.app, "style_content"):
            self.setStyleSheet(self.app.style_content)
        self._build(feature)

    def showEvent(self, event):
        super().showEvent(event)
        from ui.winfit import settle_on_screen
        settle_on_screen(self)

    # ── layout ──
    def _build(self, feature):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18)
        root.setSpacing(14)

        title = QLabel("Gusan Pro", self)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 22px; font-weight: 800; color: %s;" % INK)
        root.addWidget(title)
        sub = QLabel(self._feature_line(feature) if feature else
                     "Meeting notes, Smart Actions and fast cloud speech.", self)
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet("font-size: 13px; color: %s;" % MUTED)
        root.addWidget(sub)

        root.addWidget(self._toggle(), 0, Qt.AlignCenter)

        card = QFrame(self)
        card.setObjectName("proPlanCard")
        card.setStyleSheet(
            "QFrame#proPlanCard { background: #ffffff; border: 1px solid #e9d5ff; border-radius: 16px; }")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(22, 18, 22, 18)
        cv.setSpacing(6)
        price_row = QHBoxLayout()
        price_row.setSpacing(8)
        self.was_label = QLabel(card)
        self.was_label.setStyleSheet("font-size: 16px; color: #94a3b8; border: none;")
        price_row.addWidget(self.was_label, 0, Qt.AlignBottom)
        self.price_label = QLabel(card)
        self.price_label.setStyleSheet("font-size: 34px; font-weight: 800; color: %s; border: none;" % INK)
        self.price_label.setMinimumHeight(44)        # the digits are never clipped
        price_row.addWidget(self.price_label, 0, Qt.AlignBottom)
        self.per_label = QLabel(card)
        self.per_label.setStyleSheet("font-size: 14px; color: %s; border: none; padding-bottom: 6px;" % MUTED)
        price_row.addWidget(self.per_label, 0, Qt.AlignBottom)
        price_row.addStretch(1)
        cv.addLayout(price_row)
        self.detail_label = QLabel(card)
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("font-size: 13px; color: %s; border: none;" % MUTED)
        cv.addWidget(self.detail_label)
        # The lifetime spots ("143 of 200 left"), filled in when the server answers.
        self.spots_label = QLabel("", card)
        self.spots_label.setObjectName("planBadge")
        self.spots_label.hide()
        cv.addWidget(self.spots_label, 0, Qt.AlignLeft)
        cv.addSpacing(8)
        for text in BENEFITS:
            row = QHBoxLayout()
            row.setSpacing(10)
            tick = QLabel("✓", card)
            tick.setStyleSheet("font-size: 14px; font-weight: 800; color: %s; border: none;" % VIOLET)
            row.addWidget(tick, 0, Qt.AlignTop)
            lab = QLabel(text, card)
            lab.setWordWrap(True)
            lab.setStyleSheet("font-size: 14px; color: %s; border: none;" % INK)
            row.addWidget(lab, 1)
            cv.addLayout(row)
        cv.addSpacing(10)
        self.btn_go = QPushButton(pricing_policy.CHECKOUT_BUTTON, card)
        self.btn_go.setObjectName("primaryButton")
        self.btn_go.setMinimumHeight(44)
        self.btn_go.setCursor(Qt.PointingHandCursor)
        self.btn_go.clicked.connect(self._go)
        cv.addWidget(self.btn_go)
        self.note = QLabel("", card)
        self.note.setAlignment(Qt.AlignCenter)
        self.note.setWordWrap(True)
        self.note.setStyleSheet("font-size: 12px; color: %s; border: none;" % MUTED)
        cv.addWidget(self.note)
        root.addWidget(card)

        terms = QLabel('<a href="%s" style="color: %s; text-decoration: none;">Terms</a>'
                       % (pricing_policy.TERMS_URL, MUTED), self)
        terms.setOpenExternalLinks(True)
        terms.setAlignment(Qt.AlignCenter)
        terms.setStyleSheet("font-size: 12px;")
        root.addWidget(terms)
        later = QPushButton("Maybe later", self)
        later.setFlat(True)
        later.setCursor(Qt.PointingHandCursor)
        later.setStyleSheet("color: %s; border: none; font-size: 13px;" % MUTED)
        later.clicked.connect(self.reject)
        root.addWidget(later, 0, Qt.AlignCenter)

        self._select_plan("yearly")
        self._ask_spots()

    def _toggle(self):
        """The pill toggle the website has: Yearly (−38%) | Monthly | Lifetime."""
        box = QFrame(self)
        box.setObjectName("planToggle")
        box.setStyleSheet("QFrame#planToggle { background: #f1f5f9; border-radius: 19px; }")
        h = QHBoxLayout(box)
        h.setContentsMargins(4, 4, 4, 4)
        h.setSpacing(2)
        self.plan_buttons = {}
        for pid in PLAN_ORDER:
            text = PLANS[pid][0] + ("  " + YEARLY_SAVING if pid == "yearly" else "")
            b = QPushButton(text, box)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(30)
            b.setAccessibleName("%s plan" % PLANS[pid][0])
            b.clicked.connect(lambda _=False, p=pid: self._select_plan(p))
            h.addWidget(b)
            self.plan_buttons[pid] = b
        return box

    def _feature_line(self, feature):
        if feature == "Meeting notes":
            return "You've used this month's free cloud meeting note."
        if feature == "Live Assistant session":
            return "You've used this month's free Live Assistant session."
        return f"{feature} is a Pro feature."

    # ── lifetime spots ──
    def _ask_spots(self):
        fn = getattr(self.app, "lifetime_spots_left", None) if self.app else None
        if not callable(fn):
            return
        # A daemon thread, not a QThread parented to this window. Closing the
        # window while the request is in flight must not destroy a running
        # thread: Qt aborts the process ("QThread: Destroyed while thread is
        # still running"), which is what froze the offscreen test run.
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
            self._spots = n
            self.spots_label.setText(f"{n} of 200 left" if n else "Sold out")
            self.spots_label.setVisible(self._plan == "lifetime")
        except RuntimeError:
            return  # the window was closed before the answer arrived

    # ── the plan ──
    def _select_plan(self, plan_id):
        self._plan = plan_id
        _label, price, per, was, detail, _foot = PLANS[plan_id]
        for pid, b in self.plan_buttons.items():
            on = pid == plan_id
            b.setChecked(on)
            b.setStyleSheet(
                "QPushButton { border: none; border-radius: 15px; padding: 0 16px; font-size: 13px;"
                " font-weight: %s; color: %s; background: %s; }"
                % ("700" if on else "500", "#ffffff" if on else "#334155", INK if on else "transparent"))
        self.was_label.setText("<s>%s</s>" % was if was else "")
        self.was_label.setVisible(bool(was))
        self.price_label.setText(price)
        self.per_label.setText(per)
        self.detail_label.setText(detail)
        self.spots_label.setVisible(plan_id == "lifetime" and bool(self.spots_label.text()))
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
            # Sign in first: a subscription has to belong to an account. The violet
            # button is checkout, so this one stays the app's sign-in colour.
            self.btn_go.setText(account_actions.SIGN_IN_TO_UPGRADE)
            self.btn_go.setStyleSheet("font-size: 15px; font-weight: 700;")
            self.note.setText(account_actions.SIGN_IN_FIRST)
            return
        self.btn_go.setText(pricing_policy.CHECKOUT_BUTTON)
        self.btn_go.setStyleSheet(
            "QPushButton { font-size: 15px; font-weight: 700; color: #ffffff; border-radius: 12px;"
            " background-color: %s; border: 1px solid %s; }"
            "QPushButton:hover { background-color: %s; }" % (VIOLET, VIOLET_DARK, VIOLET_DARK))
        self.note.setText(PLANS[self._plan][5])

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
