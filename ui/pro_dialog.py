# The "Go Pro" window, in the website's look (gusan.aibuben.xyz/#pricing): one plan toggle whose
# dark thumb slides along a single track, the plans side by side in a strip you swipe or scroll
# (the neighbours peek in at the edges) - each card says its price, what it gives and what it
# commits you to - and one button to checkout.

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPropertyAnimation, QRectF, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath
from PySide6.QtWidgets import (QAbstractButton, QDialog, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QLayout, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

import account_actions
import pricing_policy
from ui import styles, theme

# Theme tokens (ui/theme.py): the window follows the light / dark look, also while it is open.
VIOLET = "pro-l"          # the plan names and ticks (#a855f7 in the light look)
VIOLET_DARK = "pro"       # the checkout button's edge and hover (#9333ea, both looks)
INK = "ink"
MUTED = "muted"

# Each plan as its card shows it: (name, price, per, struck "was" price, one detail line, what it
# gives, the line at the foot of the card). The full commitment / withdrawal wording is in Stripe
# Checkout (pricing_policy.checkout_extras): the card says it once, plainly.
BENEFITS = ("Meeting recording with AI notes", "Live Assistant in your calls", "Smart Actions by voice",
            "Fast cloud transcription", "Priority models and support")
LIFETIME_BENEFITS = ("Every Pro feature you have today", "All future Pro features included",
                     "No monthly bill, ever", "Priority models and support")
PLANS = {
    "yearly": ("Yearly", "€9.99", "/month", "€15.99", "€9.99 each month for 12 months (€119.88)",
               BENEFITS, "12-month commitment, then €9.99 a month until you cancel."),
    "monthly": ("Monthly", "€15.99", "/month", None, "Billed every month",
                BENEFITS, "Cancel anytime."),
    "lifetime": ("Lifetime", "€199", "once", None, "Pay once, Pro for life",
                 LIFETIME_BENEFITS, "One payment, for the first 200 people."),
}
PLAN_ORDER = ("yearly", "monthly", "lifetime")
DEFAULT_PLAN = "yearly"                     # what a buyer sees first, as on the website
YEARLY_SAVING = "−38%"
SECURE = "Secure checkout by Stripe"
# Stripe checkout takes promotion codes for Monthly and Yearly (not for Lifetime): one small muted line says so.
PROMO_LINE = "Have a promo code? Add it at checkout."
PROMO_PLANS = ("monthly", "yearly")

CARD_W = 284          # a plan card; the strip shows one whole and a slice of each neighbour
CARD_GAP = 12
DIM = 0.45            # a neighbour card's opacity
SLIDE_MS = 260


def _sheet(widget, template):
    """A widget's own sheet with ``@token`` colours, kept in the active look (theme.sheet)."""
    theme.sheet(widget, template)


def _live_text(label, build):
    """Rich text with a colour in it: set now and again after a look switch."""
    label._live_text = build
    label.setText(build())
    theme.on_change(label, lambda: label.setText(label._live_text()))


def _font(px, weight=QFont.Normal):
    f = QFont()
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


class PlanToggle(QWidget):
    """Yearly −38% | Monthly | Lifetime on ONE rounded track, with one dark thumb. The thumb sits
    at ``position`` (0 = Yearly ... 2 = Lifetime, fractions in between), so it glides when a plan
    is picked and follows the cards while they are being scrolled."""
    picked = Signal(int)

    PAD = 4
    H = 38
    GAP = 6                               # between a plan's word and its badge

    def __init__(self, labels, badge_index=0, badge="", parent=None):
        super().__init__(parent)
        self._labels = list(labels)
        self._badge_index, self._badge = badge_index, badge
        self._pos = 0.0
        self._anim = None                 # the thumb's glide (slide_to)
        self._keyboard_focus = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("Plan")
        self.setFixedHeight(self.H)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    # geometry
    def _f(self, px, weight):
        """A font as the painter really draws it: QPainter resolves a font against the widget's own (its family comes
        from the window's sheet), so it is measured resolved too - measured in one face and drawn in a wider one, the
        words were cut ("Month") and ran under the -38% badge."""
        return _font(px, weight).resolve(self.font())

    def _word_w(self, text):
        """The room a plan's word takes: the widest of its two weights (picked / not), with a pixel for rounding."""
        return max(QFontMetricsF(self._f(13, wt), self).horizontalAdvance(text)
                   for wt in (QFont.DemiBold, QFont.Medium)) + 1.0

    def _seg_w(self):
        widest = 0.0
        for i, text in enumerate(self._labels):
            w = self._word_w(text)
            if i == self._badge_index and self._badge:
                w += self.GAP + self._badge_w()
            widest = max(widest, w)
        return widest + 22

    def _badge_w(self):
        return QFontMetricsF(self._f(11, QFont.Bold), self).horizontalAdvance(self._badge) + 10

    def layout_of(self, index):
        """(word box, badge box or None) of plan ``index``, in widget pixels: the word ends GAP before the badge."""
        seg = self._seg_w()
        x0 = self.PAD + index * seg
        text_w = self._word_w(self._labels[index])
        badge = index == self._badge_index and self._badge
        total = text_w + (self.GAP + self._badge_w() if badge else 0)
        x = x0 + (seg - total) / 2
        word = QRectF(x, 0, text_w, self.height())
        if not badge:
            return word, None
        return word, QRectF(x + text_w + self.GAP, (self.height() - 18) / 2, self._badge_w(), 18)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() in (QEvent.FontChange, QEvent.StyleChange):
            self.updateGeometry()                     # its width follows the face it is drawn in

    def sizeHint(self):
        return QSize(int(self._seg_w() * len(self._labels) + 2 * self.PAD), self.H)

    def minimumSizeHint(self):
        return self.sizeHint()

    # state
    def position(self):
        return self._pos

    def index(self):
        return int(round(self._pos))

    def set_position(self, pos):
        """Put the thumb at ``pos`` at once (it follows the cards' scroll)."""
        if self._anim is not None:
            self._anim.stop()
        self._pos = max(0.0, min(float(pos), len(self._labels) - 1.0))
        self.setAccessibleDescription(self._labels[self.index()])
        self.update()

    def slide_to(self, index):
        """Glide the thumb to ``index``."""
        from PySide6.QtCore import QVariantAnimation
        if self._anim is not None:
            self._anim.stop()
        anim = QVariantAnimation(self)
        anim.setDuration(SLIDE_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(float(self._pos))
        anim.setEndValue(float(index))
        anim.valueChanged.connect(self._on_anim)
        self._anim = anim
        anim.start()
        self.setAccessibleDescription(self._labels[index])

    def _on_anim(self, v):
        self._pos = float(v)
        self.update()

    # input
    def focusInEvent(self, e):
        self._keyboard_focus = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._keyboard_focus = False
        super().focusOutEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            i = int((e.position().x() - self.PAD) // self._seg_w())
            if 0 <= i < len(self._labels):
                self.picked.emit(i)
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        step = {Qt.Key_Left: -1, Qt.Key_Right: 1}.get(e.key())
        if step:
            i = max(0, min(self.index() + step, len(self._labels) - 1))
            if i != self.index():
                self.picked.emit(i)
            return
        super().keyPressEvent(e)

    # drawing
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        track = QPainterPath()
        track.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.fillPath(track, theme.qcolor("pro-track"))
        p.setPen(theme.qcolor("line"))
        p.drawPath(track)
        seg = self._seg_w()
        th = QRectF(self.PAD + self._pos * seg, self.PAD, seg, r.height() - 2 * self.PAD + 1)
        thumb = QPainterPath()
        thumb.addRoundedRect(th, th.height() / 2, th.height() / 2)
        p.fillPath(thumb, theme.qcolor("inv-bg"))           # ink in the light look, near-white in the dark one
        if self.hasFocus() and self._keyboard_focus:        # a ring only for the keyboard (Tab)
            p.setPen(theme.qcolor(VIOLET))
            p.drawPath(thumb)
        off, on_c = theme.qcolor("text2"), theme.qcolor("inv-fg")
        for i, text in enumerate(self._labels):
            on = 1.0 - min(1.0, abs(self._pos - i))           # 1 under the thumb, 0 away from it
            col = QColor(int(off.red() + (on_c.red() - off.red()) * on),
                         int(off.green() + (on_c.green() - off.green()) * on),
                         int(off.blue() + (on_c.blue() - off.blue()) * on))
            word, br = self.layout_of(i)
            p.setFont(self._f(13, QFont.DemiBold if on > 0.5 else QFont.Medium))
            p.setPen(col)
            # Right-aligned in its box: the word ends GAP before the badge whatever face it is drawn in.
            p.drawText(word, Qt.AlignRight | Qt.AlignVCenter | Qt.TextDontClip, text)
            if br is not None:
                pill = QPainterPath()
                pill.addRoundedRect(br, 9, 9)
                p.fillPath(pill, theme.qcolor("save-on" if on > 0.5 else "ok-sub"))
                p.setFont(self._f(11, QFont.Bold))
                p.setPen(theme.qcolor("save-on-tx" if on > 0.5 else "ok-tx"))
                p.drawText(br, Qt.AlignCenter, self._badge)
        p.end()


class PlanCard(QFrame):
    """One plan in the strip, centred: its name, the price (and the struck price it replaces), one
    line, what it gives, and what it commits you to."""
    clicked = Signal(str)

    def __init__(self, plan_id, parent=None):
        super().__init__(parent)
        self.plan_id = plan_id
        name, price, per, was, detail, benefits, foot = PLANS[plan_id]
        self.setObjectName("proPlanCard")
        self.setFixedWidth(CARD_W)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("%s plan, %s %s" % (name, price, per))
        clear = " border: none; background: transparent;"
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(4)
        title = QLabel(name.upper(), self)
        title.setAlignment(Qt.AlignCenter)
        _sheet(title, "font-size: 11px; font-weight: 800; letter-spacing: 1.5px; color: @%s;%s" % (VIOLET, clear))
        v.addWidget(title)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        if was:
            w = QLabel("<s>%s</s>" % was, self)
            # struck through, quiet: faint in the light look (the 2.0 design), muted in the dark one (faint
            # would read 3.5:1 on the dark card)
            _sheet(w, lambda: "font-size: 15px; color: %s;%s"
                   % (theme.qss_color("muted" if theme.is_dark() else "faint"), clear))
            row.addWidget(w, 0, Qt.AlignBottom)
        self.price_label = QLabel(price, self)
        _sheet(self.price_label, "font-size: 32px; font-weight: 800; color: @%s;%s" % (INK, clear))
        self.price_label.setMinimumHeight(42)        # the digits are never clipped
        row.addWidget(self.price_label, 0, Qt.AlignBottom)
        p = QLabel(per, self)
        _sheet(p, "font-size: 13px; color: @%s; padding-bottom: 6px;%s" % (MUTED, clear))
        row.addWidget(p, 0, Qt.AlignBottom)
        row.addStretch(1)
        v.addLayout(row)
        d = QLabel(detail, self)
        d.setAlignment(Qt.AlignCenter)
        d.setWordWrap(True)
        _sheet(d, "font-size: 12.5px; color: @%s;%s" % (MUTED, clear))
        v.addWidget(d)
        # Lifetime: the spots left ("143 of 200 left"), filled in when the server answers.
        self.extra = QLabel("", self)
        self.extra.setAlignment(Qt.AlignCenter)
        _sheet(self.extra, "font-size: 12.5px; font-weight: 700; color: @%s;%s" % (INK, clear))
        self.extra.hide()
        v.addWidget(self.extra)
        v.addSpacing(10)
        # What it gives: a block of ticks, centred in the card (each line left-aligned in it).
        ticks = QWidget(self)
        ticks.setStyleSheet("background: transparent;")
        tv = QVBoxLayout(ticks)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(6)
        for text in benefits:
            r = QHBoxLayout()
            r.setSpacing(8)
            tick = QLabel("✓", ticks)
            _sheet(tick, "font-size: 13px; font-weight: 800; color: @%s;%s" % (VIOLET, clear))
            r.addWidget(tick, 0, Qt.AlignTop)
            lab = QLabel(text, ticks)
            _sheet(lab, "font-size: 13.5px; color: @%s;%s" % (INK, clear))
            r.addWidget(lab, 0)
            r.addStretch(1)
            tv.addLayout(r)
        centre = QHBoxLayout()
        centre.addStretch(1)
        centre.addWidget(ticks)
        centre.addStretch(1)
        v.addLayout(centre)
        v.addSpacing(10)
        self.foot = QLabel(foot, self)
        self.foot.setAlignment(Qt.AlignCenter)
        self.foot.setWordWrap(True)
        _sheet(self.foot, "font-size: 12px; color: @%s;%s" % (MUTED, clear))
        v.addWidget(self.foot)
        v.addStretch(1)
        self.set_current(False)

    def needed_height(self):
        """The height its text needs at CARD_W (wrapped lines included)."""
        lay = self.layout()
        h = lay.totalHeightForWidth(CARD_W) if lay.hasHeightForWidth() else -1
        return max(h, self.sizeHint().height(), self.minimumSizeHint().height())

    def set_current(self, on):
        _sheet(self, "QFrame#proPlanCard { background: @surface; border: %s; border-radius: 16px; }"
               % ("2px solid @%s" % VIOLET if on else "1px solid @pro-edge"))


class PlanStrip(QScrollArea):
    """The plan cards side by side, one centred and its neighbours peeking in. It moves by
    dragging, a trackpad swipe or the mouse wheel, and always comes to rest on one card.
    ``moved(position)`` while it moves (0.0 ... 2.0), ``settled(index)`` when it rests."""
    moved = Signal(float)
    settled = Signal(int)

    def __init__(self, cards, parent=None):
        super().__init__(parent)
        self.cards = cards
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWidgetResizable(False)
        self.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.viewport().setAutoFillBackground(False)
        row = QWidget()
        row.setAttribute(Qt.WA_TranslucentBackground, True)
        self._row = QHBoxLayout(row)
        self._row.setSpacing(CARD_GAP)
        for c in cards:
            self._row.addWidget(c)
            c.installEventFilter(self)
        self.setWidget(row)
        self.viewport().installEventFilter(self)
        self._anim = QPropertyAnimation(self.horizontalScrollBar(), b"value", self)
        self._anim.setDuration(SLIDE_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._done)
        self._rest = QTimer(self)                      # a swipe / wheel has stopped: come to rest
        self._rest.setSingleShot(True)
        self._rest.setInterval(140)
        self._rest.timeout.connect(self.snap)
        self._drag = None                              # (start x, start value, moved)
        self.horizontalScrollBar().valueChanged.connect(self._scrolled)
        h = max(c.needed_height() for c in cards)
        for c in cards:
            c.setFixedHeight(h)
        self.setFixedHeight(h + 4)

    def _step(self):
        return CARD_W + CARD_GAP

    def _layout_row(self):
        side = max(0, (self.viewport().width() - CARD_W) // 2)
        self._row.setContentsMargins(side, 2, side, 2)
        self.widget().adjustSize()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout_row()
        self.horizontalScrollBar().setValue(int(self.index() * self._step()))

    def index(self):
        return int(round(self.horizontalScrollBar().value() / self._step()))

    def go_to(self, i, animate=True):
        target = int(i * self._step())
        self._anim.stop()
        if not animate or not self.isVisible():
            self.horizontalScrollBar().setValue(target)
            self.settled.emit(i)
            return
        self._anim.setStartValue(self.horizontalScrollBar().value())
        self._anim.setEndValue(target)
        self._target = i
        self._anim.start()

    def _done(self):
        self.settled.emit(getattr(self, "_target", self.index()))

    def snap(self):
        if self._drag is None:
            self.go_to(max(0, min(self.index(), len(self.cards) - 1)))

    def _scrolled(self, value):
        self.moved.emit(value / self._step())
        if self._anim.state() != QPropertyAnimation.Running and self._drag is None:
            self._rest.start()

    def wheelEvent(self, e):
        # A trackpad swipes sideways; a plain mouse wheel turns the strip too.
        d = e.angleDelta()
        delta = d.x() if abs(d.x()) > abs(d.y()) else d.y()
        if e.pixelDelta().x() or e.pixelDelta().y():
            pd = e.pixelDelta()
            delta = pd.x() if abs(pd.x()) > abs(pd.y()) else pd.y()
        self._anim.stop()
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - int(delta))
        self._rest.start()
        e.accept()

    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.MouseButtonPress and e.button() == Qt.LeftButton:
            self._anim.stop()
            self._drag = (e.globalPosition().x(), self.horizontalScrollBar().value(), False)
            return False
        if t == QEvent.MouseMove and self._drag is not None:
            x0, v0, _moved = self._drag
            dx = e.globalPosition().x() - x0
            if abs(dx) > 4:
                self._drag = (x0, v0, True)
                self.horizontalScrollBar().setValue(int(v0 - dx))
            return True
        if t == QEvent.MouseButtonRelease and self._drag is not None:
            moved = self._drag[2]
            self._drag = None
            if moved:
                self.snap()
            elif isinstance(obj, PlanCard):
                self.go_to(self.cards.index(obj))   # a click on a card (a neighbour too) picks it
            return moved
        return super().eventFilter(obj, e)


INVITE_LINE = "Or get it free: invite 3 friends"


class InviteLine(QAbstractButton):
    """The one quiet line at the bottom: the gift, "Or get it free: invite 3 friends" and a chevron, violet on a
    pale violet (theme tokens, painted). It opens Invite friends."""

    H = 34

    def __init__(self, parent=None):
        super().__init__(parent)
        from ui import theme
        self._font = theme.font(13, 600)
        self.setText(INVITE_LINE)
        self.setAccessibleName(INVITE_LINE)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.setFixedHeight(self.H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._hover = False

    def sizeHint(self):
        return QSize(int(QFontMetricsF(self._font).horizontalAdvance(INVITE_LINE)) + 16 + 14 + 32, self.H)

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        from ui import theme
        from ui.components import icons
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        bg = theme.qcolor("pro-sub")
        if self._hover:
            bg = theme.qcolor("pro-line")
            bg.setAlphaF(min(1.0, bg.alphaF() * 0.45))
        p.setPen(QColor(theme.qcolor("pro-l")) if self.property("focusVisible") else Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 9, 9)
        tw = QFontMetricsF(self._font).horizontalAdvance(INVITE_LINE)
        total = 16 + 8 + tw + 6 + 14
        x = (self.width() - total) / 2.0
        cy = self.height() / 2.0
        colour = theme.qcolor("pro-tx")
        p.drawPixmap(int(x), int(cy - 8), icons.pixmap("gift", 16, colour, sw=2))
        p.setFont(self._font)
        p.setPen(colour)
        p.drawText(QRectF(x + 24, 0, tw + 2, self.height()), Qt.AlignLeft | Qt.AlignVCenter, INVITE_LINE)
        p.drawPixmap(int(x + 24 + tw + 6), int(cy - 7), icons.pixmap("chev_r", 14, colour, sw=2.2))
        p.end()


class ProDialog(QDialog):
    """The upgrade window: pick a plan, see its price, go to Stripe checkout (after sign-in)."""

    def __init__(self, main_app=None, feature=None, parent=None):
        # parent: the Gusan window while it is on screen, so the Pro window
        # opens over it (never behind it).
        super().__init__(parent)
        self.app = main_app
        self._plan = DEFAULT_PLAN
        self.setWindowTitle("Gusan Pro")
        self.setFixedWidth(420)
        # The 1.x sheet (ui/styles.py) in the active look, kept current when the look changes.
        theme.sheet(self, styles.stylesheet)
        self._build(feature)

    def showEvent(self, event):
        super().showEvent(event)
        self.adjustSize()                              # as tall as its content, on every system font
        from ui.winfit import settle_on_screen
        settle_on_screen(self)
        self.strip.go_to(PLAN_ORDER.index(self._plan), animate=False)

    # ── layout ──
    def _build(self, feature):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 22, 0, 18)
        root.setSpacing(14)
        # Never smaller than what it shows: a wrapped line can't squeeze another onto the button.
        root.setSizeConstraint(QLayout.SetMinimumSize)
        side = 24

        title = QLabel("Gusan Pro", self)
        title.setAlignment(Qt.AlignCenter)
        _sheet(title, "font-size: 22px; font-weight: 800; color: @%s;" % INK)
        root.addWidget(title)
        sub = QLabel(self._feature_line(feature) if feature else
                     "Meetings, the Live Assistant, Smart Actions and fast cloud speech.", self)
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        sub.setContentsMargins(side, 0, side, 0)
        _sheet(sub, "font-size: 13px; color: @%s;" % MUTED)
        root.addWidget(sub)

        self.toggle = PlanToggle([PLANS[p][0] for p in PLAN_ORDER], badge_index=0, badge=YEARLY_SAVING,
                                 parent=self)
        self.toggle.picked.connect(lambda i: self._select_plan(PLAN_ORDER[i]))
        root.addWidget(self.toggle, 0, Qt.AlignCenter)

        self.cards = {pid: PlanCard(pid) for pid in PLAN_ORDER}
        self.strip = PlanStrip([self.cards[p] for p in PLAN_ORDER], self)
        self.strip.moved.connect(self.toggle.set_position)        # the thumb follows the cards
        self.strip.settled.connect(lambda i: self._select_plan(PLAN_ORDER[i], move=False))
        self.dims = {}
        for pid, card in self.cards.items():
            eff = QGraphicsOpacityEffect(card)
            eff.setOpacity(1.0)
            card.setGraphicsEffect(eff)
            self.dims[pid] = eff
        root.addWidget(self.strip)
        self.spots_label = self.cards["lifetime"].extra

        body = QVBoxLayout()
        body.setContentsMargins(side + 8, 0, side + 8, 0)
        body.setSpacing(6)
        self.btn_go = QPushButton(pricing_policy.CHECKOUT_BUTTON, self)
        self.btn_go.setObjectName("primaryButton")
        self.btn_go.setMinimumHeight(44)
        self.btn_go.setCursor(Qt.PointingHandCursor)
        self.btn_go.clicked.connect(self._go)
        body.addWidget(self.btn_go)
        # Under the button: "Secure checkout by Stripe" (or, for a guest, sign in first).
        self.note = QLabel("", self)
        self.note.setAlignment(Qt.AlignCenter)
        self.note.setWordWrap(True)
        _sheet(self.note, "font-size: 12px; color: @%s;" % MUTED)
        body.addWidget(self.note)
        self.promo_note = QLabel(PROMO_LINE, self)
        self.promo_note.setAlignment(Qt.AlignCenter)
        self.promo_note.setWordWrap(True)
        _sheet(self.promo_note, "font-size: 12px; color: @%s;" % MUTED)
        keep = self.promo_note.sizePolicy()
        keep.setRetainSizeWhenHidden(True)                      # the window doesn't jump between plans
        self.promo_note.setSizePolicy(keep)
        body.addWidget(self.promo_note)
        root.addLayout(body)

        foot = QHBoxLayout()
        foot.setSpacing(14)
        foot.addStretch(1)
        terms = QLabel(self)
        _live_text(terms, lambda: '<a href="%s" style="color: %s; text-decoration: none;">Terms</a>'
                   % (pricing_policy.TERMS_URL, theme.qss_color(MUTED)))
        terms.setOpenExternalLinks(True)
        terms.setStyleSheet("font-size: 12px;")
        foot.addWidget(terms, 0, Qt.AlignVCenter)
        later = QPushButton("Maybe later", self)
        later.setFlat(True)
        later.setCursor(Qt.PointingHandCursor)
        _sheet(later, "color: @%s; border: none; font-size: 13px;" % MUTED)
        later.clicked.connect(self.reject)
        foot.addWidget(later, 0, Qt.AlignVCenter)
        foot.addStretch(1)
        root.addLayout(foot)

        # Invite friends: one quiet line at the very bottom (never for Pro: this window is not shown to them).
        self.invite_line = InviteLine(self)
        self.invite_line.clicked.connect(self._invite)
        irow = QHBoxLayout()
        irow.setContentsMargins(side + 8, 0, side + 8, 0)
        irow.addWidget(self.invite_line)
        root.addLayout(irow)

        self._select_plan(DEFAULT_PLAN, move=False)
        self._ask_spots()

    def _feature_line(self, feature):
        """What brought the person here, in one line ("Meeting recording comes with Gusan Pro.")."""
        name = {"Meeting notes": "Cloud meeting notes", "Live Assistant session": "Live Assistant"}.get(
            feature, feature)
        return f"{name} comes with Gusan Pro."

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
            self.spots_label.setText(f"{n} of 200 left" if n else "Sold out")
            self.spots_label.show()
        except RuntimeError:
            return  # the window was closed before the answer arrived

    # ── the plan ──
    def _select_plan(self, plan_id, move=True):
        """Make ``plan_id`` the plan: the thumb glides to it, its card comes to the middle (unless
        the strip itself just came to rest there), the others dim."""
        changed = plan_id != self._plan or not getattr(self, "_shown_once", False)
        self._plan = plan_id
        self._shown_once = True
        i = PLAN_ORDER.index(plan_id)
        if changed or abs(self.toggle.position() - i) > 0.01:
            self.toggle.slide_to(i)
        if move:
            self.strip.go_to(i)
        for pid, card in self.cards.items():
            on = pid == plan_id
            card.set_current(on)
            self.dims[pid].setOpacity(1.0 if on else DIM)
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
            _sheet(self.btn_go, "font-size: 15px; font-weight: 700;")
            self.note.setText(account_actions.SIGN_IN_FIRST)
            self.promo_note.setVisible(False)
            return
        self.promo_note.setVisible(self._plan in PROMO_PLANS)
        self.btn_go.setText(pricing_policy.CHECKOUT_BUTTON)
        _sheet(self.btn_go,
               "QPushButton { font-size: 15px; font-weight: 700; color: @on-accent; border-radius: 12px;"
               " background-color: @pro-btn; border: 1px solid @%s; }"
               "QPushButton:hover { background-color: @%s; }" % (VIOLET_DARK, VIOLET_DARK))
        self.note.setText(SECURE + ".")

    def _invite(self):
        """"Or get it free": the Invite friends page (a guest gets there too: it asks to sign in)."""
        self.accept()
        if self.app and hasattr(self.app, "show_main"):
            self.app.show_main("invite")

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
