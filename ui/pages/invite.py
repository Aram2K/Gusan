"""Invite friends (route "invite"): 3 friends, 1 month of Pro.

Mockups: InviteFree.dc.html (the page and its states), InviteClaim.dc.html (the claim moment), source
canvas_gen/invite.py. Layout, top to bottom:

* the hero card: "3 friends, 1 month of Pro", one lead line that follows the progress, the friend-gift chip, the
  three friend slots on a track (a friend who joined - and so counts: their avatar with a green ring and a check
  that pops in; nobody yet: a dashed circle with +) - and the reward ticket on the right: locked (dashed violet,
  "2 more friends to go"), ready (violet, a slow shine and glow, "Claim my month"), active ("Pro is on until
  18 Dec"), credited (a subscriber: "Your next bill is free"), or - for Lifetime and admin accounts - what the
  friends get (months are for Free and subscription accounts);
* the share card (the link with Copy link -> a green "Copied" for 1.6 s, the person's own code "ARAM-7K2Q" with
  its own Copy, WhatsApp / Telegram / Email / X / LinkedIn / More) next to "Got a friend's code?" (while this
  account can still join one: any time, once) and "Your friends" (name, a meta line, Counts / Doesn't count);
* the three steps and the fine print.

A guest sees the same hero with a calm sign-in card in place of the link and the list.

Claiming (Ticket's "Claim my month"): the hub claims on a worker (ui/invite_hub.py) and refreshes the plan; then a
confetti burst in the Armenian flag's colours over the ticket (ui/components/celebrate.py), the ticket turns into
"Pro is on until <date>" (a subscriber: "Your next bill is free"), the sidebar's plan follows the refreshed
entitlement, and a toast says "Welcome to Pro" (or "Your next bill is free"). With Windows' animation effects off: no confetti, no shine, the ticket just changes.

Data: InviteHub.view() (invites.view of the cached "me" answer); on_open asks for a fresh one (with this computer's
one-way code once a day). Nothing here touches the network on the GUI thread.
"""

import logging
import math

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer, QUrl, QVariantAnimation
from PySide6.QtGui import (QColor, QDesktopServices, QFontMetricsF, QGuiApplication, QLinearGradient, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import (QAbstractButton, QBoxLayout, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

import invites
from ui import theme
from ui.components import Button, Card, Chip, PageHeader, icons
from ui.components import celebrate
from ui.components.celebrate import brand_violet, motion, on_accent, with_alpha

logger = logging.getLogger("transcribe")

TITLE = "3 friends, 1 month of Pro"
SIGNED_OUT_LEAD = "Sign in to get your link and code. When 3 friends join, you get a month of Pro."
STEPS = (("link", "They get your link or code"), ("user", "They sign up and join"),
         ("gift", "Every 3 friends = 1 month of Pro"))
EMPTY_FRIENDS = "Nobody yet. Friends show up here when they join with your link or code."
CODE_LABEL = "Your code"
APPLYING = "Applying your free month…"
CREDITING_POLL_MS = 5000    # while a claimed month is being applied, the page asks again this often (on screen only)
COPIED_MS = 1600
SHARE_TARGETS = (("whatsapp", "WhatsApp", "chat", "ok"), ("telegram", "Telegram", "plane", "blue-l"),
                 ("email", "Email", "mail", "icon-blue"), ("x", "X", "x", "ink"),
                 ("linkedin", "LinkedIn", "link", "blue-tx"), ("more", "More", "share", "muted"))

# What "invite_link_shared" reports (a channel word, never the link).
SHARE_CHANNELS = ("copy",) + tuple(k for k, _t, _i, _c in SHARE_TARGETS)

HERO_ROW_MIN = 884          # the page's width under which the ticket goes under the slots
GRID_MIN = 900              # ... and the friends list under the share card


def open_url(url):
    """Opens a share address in the browser / mail app (tests replace it)."""
    try:
        QDesktopServices.openUrl(QUrl(url))
    except Exception:
        logger.warning("Could not open a share link", exc_info=True)


def copy_text(text):
    try:
        QGuiApplication.clipboard().setText(text or "")
        return True
    except Exception:
        logger.debug("copy failed", exc_info=True)
        return False


def _call(app, method, *args, **kwargs):
    fn = getattr(app, method, None)
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("%s() failed", method, exc_info=True)
        return None


def tinted(text, px, weight=400, token="text", display=False, parent=None, wrap=False, spacing=None):
    lb = QLabel(text, parent)
    lb.setTextFormat(Qt.PlainText)
    lb.setFont(theme.font(px, weight, display=display, letter_spacing=spacing))
    lb.setProperty("inviteTone", token)
    _tint(lb)
    if wrap:
        lb.setWordWrap(True)
    return lb


def _tint(lb, token=None):
    """The label's words in its token's colour (``inviteTone``), kept in the active look by theme.sheet: a live
    switch re-colours it with no hook of the page's own."""
    if token is not None:
        lb.setProperty("inviteTone", token)
    theme.sheet(lb, lambda: "QLabel { color: %s; background: transparent; }"
                % theme.qss_color(lb.property("inviteTone") or "text"))


def _mix(a, b, t):
    a, b = QColor(a), QColor(b)
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))


def _pop(t):
    """CSS g-pop: 0 -> 0.86, 60% -> 1.04, 100% -> 1 (scale)."""
    if t <= 0.0:
        return 0.86
    if t < 0.6:
        return 0.86 + (1.04 - 0.86) * (t / 0.6)
    return 1.04 - 0.04 * ((t - 0.6) / 0.4)


# ── the three friend slots ───────────────────────────────────────────────────
class SlotsRow(QWidget):
    """Three slots on a track (456 x 136): done or empty (invites.view()["slots"]). A slot that turns
    "done" pops in with its check badge; the green-to-violet fill grows along the track."""

    W, H = 456, 136
    BOX = 92
    STEP = 162                                     # between two slot centres
    ANIM_MS = 1100

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.W, self.H)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._slots = [{"kind": "empty", "name": "Friend %d" % (i + 1), "initial": "", "sub": "Not yet"}
                       for i in range(3)]
        self._seen_done = set()
        self._fresh = set()                        # slot indexes popping in now
        self._fill_from, self._fill_to = 0.0, 0.0
        self._ms = self.ANIM_MS
        self._name_font = theme.font(14, 600)
        self._sub_font = theme.font(12, 600)
        self.anim = QVariantAnimation(self)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(float(self.ANIM_MS))
        self.anim.setDuration(self.ANIM_MS)
        self.anim.valueChanged.connect(self._on_tick)
        self.anim.finished.connect(self._on_done)

    def slots(self):
        return [dict(s) for s in self._slots]

    def fill_target(self, slots=None):
        """How far along the track the fill reaches: the share of the way to the month (a friend who joined is a
        whole third)."""
        slots = slots or self._slots
        part = sum(1.0 for s in slots if s["kind"] == "done")
        return float(2 * self.STEP) * min(1.0, part / 3.0)

    def set_slots(self, slots, animate=True):
        slots = [dict(s) for s in (slots or [])][:3]
        while len(slots) < 3:
            slots.append({"kind": "empty", "name": "Friend %d" % (len(slots) + 1), "initial": "", "sub": "Not yet"})
        keys = {i: "%s|%s" % (s["kind"], s["name"]) for i, s in enumerate(slots)}
        fresh = {i for i, s in enumerate(slots) if s["kind"] == "done" and keys[i] not in self._seen_done}
        changed = slots != self._slots
        target = self.fill_target(slots)
        self._slots = slots
        self._seen_done |= {keys[i] for i, s in enumerate(slots) if s["kind"] == "done"}
        self.setAccessibleName(self._spoken())
        if not changed and not fresh:
            return
        if animate and motion() and (fresh or abs(target - self._fill_to) > 0.5):
            self._fresh = fresh
            self._fill_from = self._current_fill()
            self._fill_to = target
            self._ms = 0.0
            self.anim.stop()
            self.anim.start()
        else:
            self.anim.stop()
            self._fresh = set()
            self._fill_from = self._fill_to = target
            self._ms = float(self.ANIM_MS)
        self.update()

    def animating(self):
        return self.anim.state() == QVariantAnimation.Running

    def _spoken(self):
        parts = []
        for s in self._slots:
            if s["kind"] == "done":
                parts.append("%s counts" % s["name"])
        return "; ".join(parts) or "No friends yet"

    def _current_fill(self):
        t = min(1.0, self._ms / 600.0)
        e = 1.0 - (1.0 - t) ** 3
        return self._fill_from + (self._fill_to - self._fill_from) * e

    def _on_tick(self, v):
        self._ms = float(v)
        self.update()

    def _on_done(self):
        self._ms = float(self.ANIM_MS)
        self._fresh = set()
        self._fill_from = self._fill_to
        self.update()

    def _slot_t(self, i, delay=0):
        """0..1 of slot i's pop (staggered 140 ms; ``delay`` more for its badge)."""
        if i not in self._fresh:
            return 1.0
        return max(0.0, min(1.0, (self._ms - i * 140 - delay) / 550.0))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        box = self.BOX
        cy = box / 2.0
        x0 = 66.0
        track = QRectF(x0, 44, 2 * self.STEP, 4)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("track"))
        p.drawRoundedRect(track, 2, 2)
        fill = self._current_fill()
        if fill > 0.5:
            g = QLinearGradient(QPointF(x0, 0), QPointF(x0 + 2 * self.STEP, 0))
            g.setColorAt(0, theme.qcolor("ok"))
            g.setColorAt(1, theme.qcolor("pro-l"))
            p.setBrush(g)
            p.drawRoundedRect(QRectF(x0, 44, fill, 4), 2, 2)
        surface = theme.qcolor("surface")
        for i, s in enumerate(self._slots):
            cx = x0 + i * self.STEP
            centre = QPointF(cx, cy)
            kind = s["kind"]
            if kind == "done":
                t = self._slot_t(i)
                if t <= 0.0:
                    self._paint_empty(p, centre, s)
                else:
                    k = _pop(t)
                    p.save()
                    p.translate(centre)
                    p.scale(k, k)
                    p.setOpacity(min(1.0, t / 0.6))
                    p.setPen(Qt.NoPen)
                    p.setBrush(theme.qcolor("ok"))
                    p.drawEllipse(QRectF(-45, -45, 90, 90))
                    p.setBrush(surface)
                    p.drawEllipse(QRectF(-42, -42, 84, 84))
                    celebrate.paint_friend(p, QRectF(-39, -39, 78, 78), s.get("initial"), s.get("name"), s.get("idx"))
                    p.restore()
                    bt = self._slot_t(i, 300)
                    if bt > 0.0:
                        kb = 1.35 * (bt / 0.7) if bt < 0.7 else 1.35 - 0.35 * ((bt - 0.7) / 0.3)
                        bc = QPointF(cx - box / 2.0 + 64 + 14, 62 + 14)
                        p.save()
                        p.translate(bc)
                        p.scale(kb, kb)
                        p.setPen(Qt.NoPen)
                        p.setBrush(surface)
                        p.drawEllipse(QRectF(-14, -14, 28, 28))
                        p.setBrush(theme.qcolor("ok"))
                        p.drawEllipse(QRectF(-11, -11, 22, 22))
                        p.drawPixmap(QPointF(-7, -7), icons.pixmap("check", 14, "on-accent", sw=3))
                        p.restore()
            else:
                self._paint_empty(p, centre, s)
        for text, font, rect, colour, align in self.words():           # the name and the line under each slot
            p.setFont(self.font_of(font))
            p.setPen(colour)
            p.drawText(rect, align, text)
        p.end()

    def font_of(self, key):
        return self._name_font if key == "name" else self._sub_font

    def words(self):
        """[(text, font key, QRectF, QColor, alignment)] under the slots, in widget pixels: painted from here, and
        what the contrast test measures."""
        out = []
        fm = QFontMetricsF(self._name_font)
        centre = Qt.AlignHCenter | Qt.AlignVCenter
        for i, s in enumerate(self._slots):
            cx = 66.0 + i * self.STEP
            out.append((fm.elidedText(s.get("name") or "", Qt.ElideRight, 128), "name",
                        QRectF(cx - 66, self.BOX + 8, 132, 18), theme.qcolor("ink"), centre))
            tone = "ok-tx" if s["kind"] == "done" else "muted"
            out.append((s.get("sub") or "", "sub", QRectF(cx - 66, self.BOX + 28, 132, 16), theme.qcolor(tone), centre))
        return out

    def _paint_empty(self, p, centre, s):
        r = QRectF(centre.x() - 41, centre.y() - 41, 82, 82)
        p.setBrush(theme.qcolor("surface2"))
        pen = QPen(theme.qcolor("line2"), 2)
        pen.setStyle(Qt.DashLine)
        pen.setDashPattern([3, 2.5])
        p.setPen(pen)
        p.drawEllipse(r)
        p.drawPixmap(QPointF(centre.x() - 13, centre.y() - 13), icons.pixmap("plus", 26, "muted", sw=1.8))


# ── the reward ticket ────────────────────────────────────────────────────────
class Ticket(QWidget):
    """The 248 x 176 reward ticket (plus room for its glow). States: locked | ready | active | credited (a subscriber's
    claimed month: the next bill is free) | ineligible.
    "Claim my month" (``claim_button``) is a real button on the ready ticket."""

    TW, TH = 248, 176
    M = 16                                         # room around the ticket for the glow

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.TW + 2 * self.M, self.TH + 2 * self.M)
        self._state = "locked"
        self._to_go = invites.to_go(0)
        self._until = ""
        self._swap = 1.0                           # 0..1 of the swap-in pop
        self._clock = 0.0                          # ms, drives the shine and the glow
        self.claim_button = QPushButton("Claim my month", self)
        self.claim_button.setCursor(Qt.PointingHandCursor)
        self.claim_button.setFont(theme.font(14, 700))
        self.claim_button.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self.claim_button)
        self.claim_button.setGeometry(self.M + 20, self.M + self.TH - 18 - 38, self.TW - 40, 38)
        theme.sheet(self.claim_button, self._button_sheet)
        self.claim_button.hide()
        self.swap_anim = QVariantAnimation(self)
        self.swap_anim.setStartValue(0.0)
        self.swap_anim.setEndValue(1.0)
        self.swap_anim.setDuration(600)
        self.swap_anim.valueChanged.connect(self._on_swap)
        self.shine = QVariantAnimation(self)        # the ready ticket's shine (2.8 s) and glow (2.6 s), looping
        self.shine.setStartValue(0.0)
        self.shine.setEndValue(2800.0 * 13)
        self.shine.setDuration(2800 * 13)
        self.shine.setLoopCount(-1)
        self.shine.valueChanged.connect(self._on_clock)
        self._fonts = {"eyebrow": theme.font(12, 700, letter_spacing=0.72), "big": theme.font(40, 800, display=True,
                       letter_spacing=-0.8), "mid": theme.font(26, 800, display=True), "sub": theme.font(14, 600),
                       "foot": theme.font(12.5, 600), "pill": theme.font(11, 700, letter_spacing=0.44)}
        self._sync_a11y()

    # state
    def state(self):
        return self._state

    def set_state(self, state, to_go=None, until=None, animate=False):
        if to_go is not None:
            self._to_go = to_go
        if until is not None:
            self._until = until
        changed = state != self._state
        self._state = state
        self.claim_button.setVisible(state in ("ready", "crediting"))
        if state == "crediting":                     # ready, busy: the month is being applied
            self.claim_button.setEnabled(False)
            self.claim_button.setText(APPLYING)
        elif state != "ready":
            self.set_busy(False)
        if changed and animate and motion():
            self._swap = 0.0
            self.swap_anim.stop()
            self.swap_anim.start()
        elif changed:
            self._swap = 1.0
        self._sync_motion()
        self._sync_a11y()
        self.update()

    def set_busy(self, busy):
        self.claim_button.setEnabled(not busy)
        self.claim_button.setText("Claiming…" if busy else "Claim my month")

    def shining(self):
        return self.shine.state() == QVariantAnimation.Running

    def swapping(self):
        return self.swap_anim.state() == QVariantAnimation.Running

    @staticmethod
    def _button_sheet():
        """"Claim my month": white with deep-violet words on the violet ticket, in both looks (the ticket is the
        brand's strong violet in both)."""
        def a(alpha):
            c = with_alpha(on_accent(), alpha)
            return "rgba(%d, %d, %d, %d)" % (c.red(), c.green(), c.blue(), c.alpha())
        return ("QPushButton { background: %s; color: %s; border: none; border-radius: 10px; }"
                "QPushButton:hover { background: %s; }"
                "QPushButton:disabled { background: %s; color: %s; }"
                "QPushButton[focusVisible=\"true\"] { border: 2px solid %s; }"
                % (theme.qss_color("on-accent"), theme.qss_color("pro-deep"), a(0.88), a(0.78),
                   theme.qss_color("pro-deep"), theme.qss_color("pro-focus")))

    def _sync_a11y(self):
        names = {"locked": "Your reward: 1 month of Gusan Pro. %s." % self._to_go,
                 "ready": "Your month of Gusan Pro is ready to claim.",
                 "active": "Pro is on%s." % ((" until " + self._until) if self._until else ""),
                 "credited": "Your next bill is free. A one-month credit is on your subscription.",
                 "crediting": "Your free month is being applied.",
                 "ineligible": invites.claim_error("not_eligible")}
        self.setAccessibleName(names.get(self._state, ""))

    def _sync_motion(self):
        want = self._state == "ready" and self.isVisible() and motion()
        if want and self.shine.state() != QVariantAnimation.Running:
            self.shine.start()
        elif not want and self.shine.state() == QVariantAnimation.Running:
            self.shine.stop()

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_motion()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sync_motion()

    def _on_swap(self, v):
        self._swap = float(v)
        self.update()

    def _on_clock(self, v):
        self._clock = float(v)
        self.update()

    def ticket_rect(self):
        return QRectF(self.M, self.M, self.TW, self.TH)

    # painting
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.ticket_rect()
        if self._swap < 1.0:
            k = _pop(self._swap)
            p.translate(r.center())
            p.scale(k, k)
            p.translate(-r.center())
            p.setOpacity(min(1.0, self._swap / 0.5))
        st = self._state
        if st in ("ready", "crediting"):
            self._paint_ready(p, r)
        elif st in ("active", "credited"):
            self._paint_claimed(p, r, st)
        else:
            self._paint_locked(p, r, st)
        p.end()

    def words(self):
        """What the ticket writes in its state: [(text, font key, QRectF, QColor, alignment flags)] in widget pixels
        - painted from here, and what the contrast test measures."""
        r = self.ticket_rect()
        x, w = r.left() + 20, r.width() - 40
        left = Qt.AlignLeft | Qt.AlignVCenter
        st = self._state
        if st in ("ready", "crediting"):
            white = on_accent()
            return [("UNLOCKED", "eyebrow", QRectF(x, r.top() + 18, w, 18), with_alpha(white, 0.92), left),
                    ("1 month", "big", QRectF(x, r.top() + 48, w, 44), white, left),
                    ("of Gusan Pro", "sub", QRectF(x, r.top() + 92, w, 20), with_alpha(white, 0.92), left)]
        if st == "credited":                     # two lines each: the big words and the line under them
            wrap = Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap
            return [("PRO", "pill", self._pill_rect(), on_accent(), Qt.AlignCenter),
                    ("Your next bill is free", "mid", QRectF(x, r.top() + 46, w, 70), theme.qcolor("ink"), wrap),
                    ("A one-month credit is on your subscription", "sub", QRectF(x, r.top() + 122, w, 40),
                     theme.qcolor("text2"), wrap)]
        if st == "active":
            big, small = "Pro is on", ("until %s" % self._until) if self._until else "for a month"
            return [("PRO", "pill", self._pill_rect(), on_accent(), Qt.AlignCenter),
                    (big, "mid", QRectF(x, r.top() + 70, w, 32), theme.qcolor("ink"), left),
                    (small, "sub", QRectF(x, r.top() + 104, w, 40), theme.qcolor("text2"),
                     Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap),
                    ("Invite 3 more for another month", "foot", QRectF(x, r.bottom() - 36, w, 18),
                     theme.qcolor("muted"), left)]
        pro_tx = theme.qcolor("pro-tx")          # (the foot is text3: muted read 4.47:1 on the dark look's violet)
        if st == "ineligible":
            return [("FOR YOUR FRIENDS", "eyebrow", QRectF(x, r.top() + 18, w, 18), pro_tx, left),
                    ("2 weeks", "big", QRectF(x, r.top() + 48, w, 44), pro_tx, left),
                    ("of Pro for each friend", "sub", QRectF(x, r.top() + 92, w, 20), theme.qcolor("text2"), left),
                    (invites.NOT_ELIGIBLE_SHORT, "foot", QRectF(x, r.bottom() - 18 - 34, w, 34), theme.qcolor("text3"),
                     Qt.AlignLeft | Qt.AlignBottom | Qt.TextWordWrap)]
        return [("YOUR REWARD", "eyebrow", QRectF(x, r.top() + 18, w, 18), pro_tx, left),
                ("1 month", "big", QRectF(x, r.top() + 64, w, 44), pro_tx, left),
                ("of Gusan Pro", "sub", QRectF(x, r.top() + 108, w, 20), theme.qcolor("text2"), left),
                (self._to_go, "foot", QRectF(x, r.bottom() - 36, w, 18), theme.qcolor("text3"), left)]

    def font_of(self, key):
        return self._fonts[key]

    def _pill_rect(self):
        r = self.ticket_rect()
        return QRectF(r.left() + 20, r.top() + 20, QFontMetricsF(self._fonts["pill"]).horizontalAdvance("PRO") + 20, 20)

    def _paint_words(self, p):
        for text, font, rect, colour, align in self.words():
            p.setFont(self._fonts[font])
            p.setPen(colour)
            p.drawText(rect, align, text)

    def _paint_locked(self, p, r, st):
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        sub = theme.qcolor("pro-sub")
        g.setColorAt(0, sub)
        g.setColorAt(1, _mix(sub, theme.qcolor("pro-l"), 0.10))
        pen = QPen(theme.qcolor("pro-line"), 1.5)
        pen.setStyle(Qt.DashLine)
        pen.setDashPattern([4, 3])
        p.setPen(pen)
        p.setBrush(g)
        p.drawRoundedRect(r.adjusted(0.75, 0.75, -0.75, -0.75), 18, 18)
        p.drawPixmap(QPointF(r.right() - 20 - 18, r.top() + 18),
                     icons.pixmap("gift" if st == "ineligible" else "lock", 18, "pro-tx", sw=1.9))
        self._paint_words(p)

    def _paint_ready(self, p, r):
        t_glow = (self._clock % 2600.0) / 2600.0
        glow = 0.5 - 0.5 * math.cos(2 * math.pi * t_glow)          # 0 -> 1 -> 0
        base = theme.qcolor("pro")
        path = QPainterPath()
        path.addRoundedRect(r, 18, 18)
        p.setPen(Qt.NoPen)
        for i in range(7):                                           # the soft glow under the ticket
            grow = 2.0 + i * 2.0
            a = (0.05 + 0.05 * glow) * (1.0 - i / 7.0)
            p.setBrush(with_alpha(base, a))
            p.drawRoundedRect(r.adjusted(-grow + 2, -grow + 6, grow - 2, grow + 6), 18 + grow, 18 + grow)
        g = QLinearGradient(r.topLeft(), r.bottomRight())       # the brand's strong violet, the same in both looks
        g.setColorAt(0, theme.qcolor("pro-deep"))
        g.setColorAt(0.55, theme.qcolor("pro"))
        g.setColorAt(1, theme.qcolor("pro-btn"))
        p.fillPath(path, g)
        if self.shining():                                          # the shine: a slanted light band sweeping by
            t = (self._clock % 2800.0) / 2800.0
            e = 0.5 - 0.5 * math.cos(math.pi * t)
            bx = r.left() - 90 + (r.width() + 160) * e
            p.save()
            p.setClipPath(path)
            band = QPainterPath()
            band.moveTo(bx + 20, r.top() - 20)
            band.lineTo(bx + 90, r.top() - 20)
            band.lineTo(bx + 50, r.bottom() + 20)
            band.lineTo(bx - 20, r.bottom() + 20)
            band.closeSubpath()
            lg = QLinearGradient(QPointF(bx - 20, 0), QPointF(bx + 90, 0))
            white = on_accent()
            lg.setColorAt(0, with_alpha(white, 0.0))
            lg.setColorAt(0.5, with_alpha(white, 0.32))
            lg.setColorAt(1, with_alpha(white, 0.0))
            p.fillPath(band, lg)
            p.restore()
        p.drawPixmap(QPointF(r.right() - 20 - 18, r.top() + 18), icons.pixmap("sparkle", 18, "on-accent", sw=1.9))
        self._paint_words(p)

    def _paint_claimed(self, p, r, st):
        p.setPen(QPen(theme.qcolor("pro-line"), 1.5))
        p.setBrush(theme.qcolor("surface"))
        p.drawRoundedRect(r.adjusted(0.75, 0.75, -0.75, -0.75), 18, 18)
        pill = self._pill_rect()                                     # the PRO pill (its word: words())
        p.setPen(Qt.NoPen)
        p.setBrush(brand_violet(pill))
        p.drawRoundedRect(pill, 10, 10)
        c = QRectF(r.right() - 20 - 26, r.top() + 17, 26, 26)         # the green check
        p.setBrush(theme.qcolor("ok"))
        p.drawEllipse(c)
        p.drawPixmap(QPointF(c.center().x() - 7.5, c.center().y() - 7.5), icons.pixmap("check", 15, "on-accent", sw=3))
        self._paint_words(p)


# ── the share card ───────────────────────────────────────────────────────────
class LinkField(QFrame):
    """The link in a field-like box: the link icon, "gusan.aibuben.xyz/i/" and the code in blue (selectable)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("inviteLink")
        self.setFixedHeight(44)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(10)
        self.icon = QLabel(self)
        self.icon.setFixedSize(17, 17)
        lay.addWidget(self.icon, 0, Qt.AlignVCenter)
        self.text = QLabel(self)
        self.text.setFont(theme.font(15, 600, letter_spacing=0.15))
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text.setTextFormat(Qt.RichText)
        self.text.setMinimumWidth(40)
        self.text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.text, 1, Qt.AlignVCenter)
        self._link = ""
        self._placeholder = ""
        theme.sheet(self, "QFrame#inviteLink { background: @input; border: 1px solid @line2; border-radius: 10px; }")
        self.icon.setPixmap(icons.pixmap("link", 17, "text2", sw=1.9))      # (a token icon: re-tinted by a switch)
        theme.on_change(self, self._render)                                 # the rich text's colours

    def link(self):
        return self._link

    def shown_text(self):
        return self.text.text()

    def set_link(self, link, placeholder=""):
        self._link = link or ""
        self._placeholder = placeholder
        self._render()

    def _render(self):
        import html
        shown = invites.display_link(self._link)
        if not shown:
            self.text.setText('<span style="color: %s; font-weight: 500">%s</span>'
                              % (theme.qss_color("muted"), html.escape(self._placeholder)))
            self.text.setAccessibleName(self._placeholder)
            return
        head, _, code = shown.rpartition("/")
        self.text.setText('<span style="color: %s">%s/</span><span style="color: %s">%s</span>'
                          % (theme.qss_color("ink"), html.escape(head), theme.qss_color("blue-tx"), html.escape(code)))
        self.text.setAccessibleName("Your invite link: " + shown)



class ShareChip(QAbstractButton):
    """A round share chip: a small coloured mark in a bubble and the target's name (36 px)."""

    def __init__(self, key, text, icon, colour, parent=None):
        super().__init__(parent)
        self.key = key
        self._text, self._icon, self._colour = text, icon, colour
        self._font = theme.font(13, 600)
        self._hover = False
        self.setText(text)
        self.setAccessibleName("Share on %s" % text if key not in ("email", "more") else
                               ("Share by email" if key == "email" else "Copy the message"))
        self.setToolTip(self.accessibleName())
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.setFixedHeight(36)
        self.setFixedWidth(int(math.ceil(8 + 24 + 8 + QFontMetricsF(self._font).horizontalAdvance(text) + 14)))

    def label(self):
        return self._text

    def set_label(self, text):
        self._text = text
        self.setFixedWidth(int(math.ceil(8 + 24 + 8 + QFontMetricsF(self._font).horizontalAdvance(text) + 14)))
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        focus = bool(self.property("focusVisible"))
        p.setPen(QPen(theme.qcolor("blue-l" if (self._hover or focus) else "line2"), 2 if focus else 1))
        p.setBrush(theme.qcolor("surface"))
        p.drawRoundedRect(r, r.height() / 2.0, r.height() / 2.0)
        b = QRectF(8, (self.height() - 24) / 2.0, 24, 24)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("bg2"))
        p.drawEllipse(b)
        p.drawPixmap(QPointF(b.center().x() - 7, b.center().y() - 7), icons.pixmap(self._icon, 14, self._colour, sw=2))
        p.setFont(self._font)
        p.setPen(theme.qcolor("text"))
        p.drawText(QRectF(40, 0, self.width() - 40 - 10, self.height()), Qt.AlignLeft | Qt.AlignVCenter, self._text)
        p.end()


class ShareCard(Card):
    def __init__(self, page, parent=None):
        super().__init__(parent, pad=(22, 20), gap=14)
        self.page = page
        self.setAccessibleName("Your invite link")
        self.title = tinted("Your invite link", 15, 700, "ink", parent=self)
        self.add(self.title)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.field = LinkField(self)
        row.addWidget(self.field, 1)
        self.copy_button = Button("Copy link", "primary", "l", icon="copy", parent=self)
        self.copy_button.setFixedHeight(44)
        self.copy_button.clicked.connect(self.copy_link)
        row.addWidget(self.copy_button, 0)
        self.add(row)
        # The person's own code, plainly (a friend can type it in Gusan instead of opening the link).
        crow = QHBoxLayout()
        crow.setSpacing(10)
        self.code_caption = tinted(CODE_LABEL, 13, 600, "text2", parent=self)
        crow.addWidget(self.code_caption, 0, Qt.AlignVCenter)
        self.code_label = QLabel("", self)
        self.code_label.setObjectName("inviteCode")
        self.code_label.setTextFormat(Qt.PlainText)
        self.code_label.setFont(theme.font(15, 700, letter_spacing=1.2))
        self.code_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.code_label.setFixedHeight(36)
        theme.sheet(self.code_label, "QLabel#inviteCode { color: @ink; background: @bg2; border: 1px dashed @line2; "
                                     "border-radius: 8px; padding: 0px 12px; }")
        crow.addWidget(self.code_label, 0, Qt.AlignVCenter)
        self.copy_code_button = Button("Copy", "secondary", "m", icon="copy", parent=self)
        self.copy_code_button.setAccessibleName("Copy your code")
        self.copy_code_button.clicked.connect(self.copy_code)
        crow.addWidget(self.copy_code_button, 0, Qt.AlignVCenter)
        crow.addStretch(1)
        self.code_row = QWidget(self)
        self.code_row.setLayout(crow)
        self.add(self.code_row)
        from ui.pages.home import FlowLayout
        self.chips_box = QWidget(self)
        self.chips = FlowLayout(spacing=8)
        self.chips_box.setLayout(self.chips)
        self.chip = {}
        for key, text, icon, colour in SHARE_TARGETS:
            c = ShareChip(key, text, icon, colour, self.chips_box)
            c.clicked.connect(lambda _=False, k=key: self.share(k))
            self.chips.addWidget(c)
            self.chip[key] = c
        self.add(self.chips_box)
        self.note = tinted("", 12.5, 400, "muted", parent=self, wrap=True)
        self.note.hide()
        self.add(self.note)
        self.retry_button = Button("Try again", "link", "s", parent=self)
        self.retry_button.clicked.connect(lambda _=False: self.page.retry())
        self.retry_button.hide()
        self.add(self.retry_button, 0, Qt.AlignLeft)
        self._copied = QTimer(self)
        self._copied.setSingleShot(True)
        self._copied.timeout.connect(self._uncopy)
        self._more = QTimer(self)
        self._more.setSingleShot(True)
        self._more.timeout.connect(lambda: self.chip["more"].set_label("More"))
        self._code_copied = QTimer(self)
        self._code_copied.setSingleShot(True)
        self._code_copied.timeout.connect(self._uncopy_code)
        self._link = ""
        self._code = ""

    def code(self):
        """The code as shown ("ARAM-7K2Q"), "" before it is known."""
        return self._code

    def set_link(self, link, state="ok", code=""):
        """state: ok | loading | failed. ``code``: the person's own code (shown upper-case)."""
        self._link = link or ""
        self._code = invites.display_code(code)
        self.code_label.setText(self._code)
        self.code_label.setAccessibleName("Your code: " + " ".join(self._code) if self._code else "")
        self.code_row.setVisible(bool(self._code))
        has = bool(self._link)
        self.field.set_link(self._link, {"loading": "Getting your link…",
                                         "failed": "Your link couldn't load."}.get(state, ""))
        self.copy_button.setEnabled(has)
        for c in self.chip.values():
            c.setEnabled(has)
        self.retry_button.setVisible(state == "failed" and not has)

    def copied(self):
        return self.copy_button.text() == "Copied"

    def _shared(self, channel):
        hub = getattr(self.page, "hub", None)
        if hub is not None:
            hub.track("invite_link_shared", {"channel": channel})

    def copy_link(self):
        if not self._link:
            return
        copy_text(self._link)
        self._shared("copy")
        self.copy_button.setText("Copied")
        self.copy_button.setIcon(icons.icon("check", 18, "on-accent", sw=2.4))
        theme.sheet(self.copy_button, "QPushButton { background: @ok; border: 1px solid @ok; color: @on-accent; }")
        self._copied.start(COPIED_MS)

    def _uncopy(self):
        self.copy_button.setText("Copy link")
        self.copy_button.setIcon(icons.icon("copy", 18, "on-accent"))
        theme.sheet(self.copy_button, None)

    def copy_code(self):
        if not self._code:
            return
        copy_text(self._code)
        self._shared("copy")
        self.copy_code_button.setText("Copied")
        self.copy_code_button.setIcon(icons.icon("check", 16, "ok-tx", sw=2.4))
        self._code_copied.start(COPIED_MS)

    def code_copied(self):
        return self.copy_code_button.text() == "Copied"

    def _uncopy_code(self):
        self.copy_code_button.setText("Copy")
        self.copy_code_button.setIcon(icons.icon("copy", 16, "text2"))

    def share(self, key):
        if not self._link:
            return
        text = invites.share_text(self._link, self._code)
        self._shared(key if key in SHARE_CHANNELS else "more")
        if key == "more":
            copy_text(text)
            self.chip["more"].set_label("Message copied")
            self._more.start(COPIED_MS)
            return
        url = invites.share_urls(self._link, text, self._code).get(key)
        if url:
            open_url(url)


# ── your friends ─────────────────────────────────────────────────────────────
class FriendAvatar(QWidget):
    def __init__(self, initial, name, size=32, parent=None, index=None):
        super().__init__(parent)
        self._initial, self._name, self._index = initial, name, index
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _e):
        p = QPainter(self)
        celebrate.paint_friend(p, QRectF(0, 0, self.width(), self.height()), self._initial, self._name, self._index)
        p.end()


class FriendRow(QWidget):
    """A friend: avatar, name, a meta line, and the chip (Counts / Day n of 3 / Invited / Doesn't count)."""

    def __init__(self, row, first=False, parent=None):
        super().__init__(parent)
        self.row = dict(row)
        self._first = first
        self.setFixedHeight(52)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(FriendAvatar(row["initial"], row["name"], 32, self, row.get("idx")), 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.name_label = tinted(row["name"], 13.5, 600, "ink", parent=self)
        col.addWidget(self.name_label)
        self.meta_label = tinted(row["meta"], 12, 400, "muted", parent=self)
        self.meta_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        col.addWidget(self.meta_label)
        lay.addLayout(col, 1)
        self.chip = Chip(row["chip"], row["tone"], "check" if row["status"] == "counted" else None, self)
        lay.addWidget(self.chip, 0, Qt.AlignVCenter)
        self.setAccessibleName("%s: %s. %s" % (row["name"], row["chip"], row["meta"]))

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._first:
            return
        p = QPainter(self)
        p.setPen(QPen(theme.qcolor("line"), 1))
        p.drawLine(0, 0, self.width(), 0)
        p.end()


class FriendsCard(Card):
    def __init__(self, parent=None):
        super().__init__(parent, pad=(20, 18), gap=0)
        self.layout().setContentsMargins(20, 18, 20, 10)
        self.setAccessibleName("Your friends")
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 8)
        self.title = tinted("Your friends", 15, 700, "ink", parent=self)
        head.addWidget(self.title)
        head.addStretch(1)
        self.count_label = tinted("", 12.5, 600, "muted", parent=self)
        head.addWidget(self.count_label)
        self.add(head)
        self.list_box = QWidget(self)
        self.list_lay = QVBoxLayout(self.list_box)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(0)
        self.add(self.list_box)
        self.empty = tinted(EMPTY_FRIENDS, 13.5, 400, "text2", parent=self, wrap=True)
        self.empty.setContentsMargins(0, 10, 0, 8)
        self.add(self.empty)
        self._rows_key = None

    def rows(self):
        return [w for w in self.list_box.findChildren(FriendRow) if not w.isHidden()]

    def set_friends(self, rows, loading=False):
        key = (bool(loading),) + tuple((r["name"], r["chip"], r["meta"]) for r in rows)
        if key == self._rows_key:
            return
        self._rows_key = key
        while self.list_lay.count():
            w = self.list_lay.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        for i, r in enumerate(rows):
            self.list_lay.addWidget(FriendRow(r, first=(i == 0), parent=self.list_box))
        self.count_label.setText(("%d invited" % len(rows)) if rows else "")
        self.empty.setText("" if loading else EMPTY_FRIENDS)
        self.empty.setVisible(not rows and not loading)


# ── the three steps ──────────────────────────────────────────────────────────
class _StepTile(QWidget):
    def __init__(self, icon, parent=None):
        super().__init__(parent)
        self._icon = icon
        self.setFixedSize(34, 34)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("blue-sub"))
        p.drawRoundedRect(QRectF(0, 0, 34, 34), 10, 10)
        p.drawPixmap(QPointF(8, 8), icons.pixmap(self._icon, 18, "blue-tx", sw=1.9))
        p.end()


class HowStrip(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 4, 2, 4)
        lay.setSpacing(14)
        self.texts = []
        for i, (ic, text) in enumerate(STEPS):
            step = QHBoxLayout()
            step.setSpacing(10)
            step.addWidget(_StepTile(ic, self), 0, Qt.AlignVCenter)
            lb = tinted(text, 13, 400, "text2", parent=self, wrap=True)
            lb.setMinimumWidth(60)
            self.texts.append(lb)
            step.addWidget(lb, 1, Qt.AlignVCenter)
            lay.addLayout(step, 1)
            if i < len(STEPS) - 1:
                chev = QLabel(self)
                chev.setPixmap(icons.pixmap("chev_r", 16, "faint", sw=2))
                chev.setFixedSize(16, 16)
                lay.addWidget(chev, 0, Qt.AlignVCenter)
        self.setAccessibleName("How it works: " + " ".join(t + "." for _i, t in STEPS))


# ── the page ─────────────────────────────────────────────────────────────────
class InvitePage(QWidget):
    """The Invite friends page. See the module notes."""

    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.opts = {}
        from ui import invite_hub
        self.hub = invite_hub.hub(main_app)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = PageHeader("Invite friends", parent=self)
        root.addWidget(self.header)
        body = QVBoxLayout()
        body.setContentsMargins(32, 0, 32, 28)
        body.setSpacing(16)
        root.addLayout(body)
        root.addStretch(1)

        # hero (no drop shadow: the ticket animates, and a shadow effect re-renders the whole card each frame)
        self.hero = Card(self, pad=(28, 26), gap=0, shadow=False)
        self.hero.setAccessibleName(TITLE)
        self.hero_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.hero_row.setSpacing(32)
        left = QVBoxLayout()
        left.setSpacing(22)
        texts = QVBoxLayout()
        texts.setSpacing(10)
        self.title_label = tinted(TITLE, 30, 700, "ink", display=True, parent=self.hero, spacing=-0.45)
        self.title_label.setFixedHeight(36)
        texts.addWidget(self.title_label)
        self.lead = tinted("", 15, 400, "text2", parent=self.hero, wrap=True)
        texts.addWidget(self.lead)
        self.gift_chip = Chip(invites.GIFT_LINE, "pro", "gift", self.hero)
        texts.addWidget(self.gift_chip, 0, Qt.AlignLeft)
        left.addLayout(texts)
        self.slots = SlotsRow(self.hero)
        left.addWidget(self.slots, 0, Qt.AlignLeft)
        self.hero_row.addLayout(left, 1)
        right = QVBoxLayout()
        right.setSpacing(4)
        right.addStretch(1)
        self.ticket = Ticket(self.hero)
        self.ticket.claim_button.clicked.connect(self.claim)
        right.addWidget(self.ticket, 0, Qt.AlignHCenter)
        self._ticket_col = right
        self.claim_note = tinted("", 12.5, 400, "red-tx", parent=self.hero, wrap=True)
        self.claim_note.setFixedWidth(Ticket.TW)
        self.claim_note.hide()
        right.addWidget(self.claim_note, 0, Qt.AlignHCenter)
        right.addStretch(1)
        self.hero_row.addLayout(right, 0)
        self.hero.add(self.hero_row)
        self.hero.layout().setContentsMargins(28, 26, 28 - Ticket.M, 26)      # the ticket's glow room is its own margin
        body.addWidget(self.hero)

        # share + friends (signed in) or the sign-in card (a guest)
        self.grid = QBoxLayout(QBoxLayout.LeftToRight)
        self.grid.setSpacing(16)
        left_col = QVBoxLayout()
        left_col.setSpacing(16)
        self.share = ShareCard(self, self)
        left_col.addWidget(self.share)
        self.signin_card = self._build_signin()
        left_col.addWidget(self.signin_card)
        self.how = HowStrip(self)
        left_col.addWidget(self.how)
        left_col.addStretch(1)
        self.grid.addLayout(left_col, 5)
        self.friends = FriendsCard(self)
        self.friends_col = QVBoxLayout()
        self.friends_col.setSpacing(16)
        self.join_card = None                   # "Got a friend's code?": built while this account can still join
        self.friends_col.addWidget(self.friends)
        self.friends_col.addStretch(1)
        self.grid.addLayout(self.friends_col, 4)
        body.addLayout(self.grid)

        self._wide = None
        self._viewport = None
        self._claiming = False
        self._crediting_poll = False
        self._last_confetti = None
        self.hub.changed.connect(self.render)
        self.render(animate=False)

    def _build_signin(self):
        card = Card(self, pad=(22, 20), gap=12)
        card.setAccessibleName("Sign in to invite friends")
        row = QHBoxLayout()
        row.setSpacing(14)
        tile = _StepTile("gift", card)
        row.addWidget(tile, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(tinted("Sign in to get your invite link", 15, 700, "ink", parent=card))
        col.addWidget(tinted("Your friends get 2 weeks of Pro when they join.", 13, 400, "text2", parent=card,
                             wrap=True))
        row.addLayout(col, 1)
        card.add(row)
        import account_actions
        self.signin_button = Button(account_actions.SIGN_IN_UP, "primary", "m", parent=card)
        self.signin_button.setAccessibleName(account_actions.SIGN_IN_UP_A11Y)
        self.signin_button.clicked.connect(lambda _=False: _call(self.app, "show_auth_gate"))
        card.add(self.signin_button, 0, Qt.AlignLeft)
        return card

    # ── state ──
    def view(self):
        return self.hub.view()

    def signed_in(self):
        return self.hub.signed_in()

    def render(self, animate=True):
        """Draw the hub's current state (in place: nothing is rebuilt but the friends list when it changed)."""
        try:
            self._render(animate)
        except RuntimeError:
            pass                                # the page is going away

    def _render(self, animate):
        signed = self.signed_in()
        v = self.view()
        me = self.hub.me() if signed else None
        visible = self.isVisible()
        if not signed:
            self.lead.setText(SIGNED_OUT_LEAD)
            self.slots.set_slots(invites.view(None)["slots"], animate=False)
            self.ticket.set_state("locked", to_go=invites.to_go(0))
        else:
            self.lead.setText(v["lead"])
            self.slots.set_slots(v["slots"], animate=animate and visible)
            if not self._claiming:
                self.ticket.set_state(v["ticket"], to_go=v["to_go"], until=v["until"], animate=animate and visible)
        self.lead.setAccessibleName(self.lead.text())
        if signed and v["ticket"] == "crediting" and visible and not self._crediting_poll:
            self._crediting_poll = True
            QTimer.singleShot(CREDITING_POLL_MS, self._poll_crediting)
        self.share.setVisible(signed)
        self.friends.setVisible(signed)
        self.signin_card.setVisible(not signed)
        if signed:
            if me is not None:
                self.share.set_link(me.get("link") or "", "ok", me.get("code") or "")
            else:
                self.share.set_link("", "failed" if self.hub.failed() else "loading")
            self.friends.set_friends(v["friends"], loading=me is None and not self.hub.failed())
        self._sync_join(signed)

    def _sync_join(self, signed):
        """"Got a friend's code?" - any time, once: shown while the account can still join (and with its answer
        right after it joined), above Your friends."""
        want = signed and (self.hub.can_join() or self.hub.joined_now() is not None)
        if want and self.join_card is None:
            from ui.pages.invite_join import CodeJoinCard
            self.join_card = CodeJoinCard(self.hub, self)
            self.friends_col.insertWidget(0, self.join_card)
        if self.join_card is not None:
            self.join_card.setVisible(want)

    def retry(self):
        self.hub.retry()
        self.render()

    def _poll_crediting(self):
        """A claimed month is being applied: ask again (it turns into "Your next bill is free")."""
        try:
            self._crediting_poll = False
            if self.isVisible() and self.view()["ticket"] == "crediting":
                self.hub.refresh(force=True)
                self.render()
        except RuntimeError:
            pass

    # ── claiming ──
    def claim(self):
        rid = self.view().get("ready_id")
        if not rid or self._claiming:
            return
        self._claiming = True
        self.ticket.set_busy(True)
        self.claim_note.hide()
        self.hub.claim(rid, then=self._claimed)

    def _claimed(self, res):
        try:
            self._claimed_ui(res)
        except RuntimeError:
            pass

    def _claimed_ui(self, res):
        self._claiming = False
        if not res or not res.get("ok"):
            reason = (res or {}).get("reason")
            self.ticket.set_busy(False)
            if reason == "not_eligible":                 # a Lifetime / admin account: the ticket explains, calmly
                self.ticket.set_state("ineligible", animate=True)
            # Calm for what is nobody's fault (not eligible, the card company busy); red for the rest.
            _tint(self.claim_note, "muted" if reason in ("not_eligible", "stripe_error", "server") else "red-tx")
            self.claim_note.setText(invites.claim_error(reason))
            self.claim_note.show()
            return
        status = "credited" if res.get("status") == "next_bill_free" else "active"
        until = invites.short_date(res.get("ends_at"))
        self.ticket.set_state(status, until=until or None, animate=True)
        self.celebrate_claim(status, until)
        self.render()

    def celebrate_claim(self, status="active", until=""):
        """The claim moment: confetti over the ticket and the toast."""
        win = self.window()
        host = win.centralWidget() if hasattr(win, "centralWidget") and win.centralWidget() is not None else self
        centre = self.ticket.mapTo(host, self.ticket.ticket_rect().center().toPoint())
        self._last_confetti = celebrate.Confetti.burst(host, QPoint(centre.x(), centre.y() - 20))
        if status == "credited":
            title, sub = "Your next bill is free", "A one-month credit is on your subscription."
        else:
            title = "Welcome to Pro"
            sub = ("Every Pro feature is on until %s." % until) if until else "Every Pro feature is on."
        if hasattr(win, "centralWidget"):
            celebrate.show_toast(win, title, sub, icon="sparkle")
        return self._last_confetti

    # ── width ──
    def _visible_width(self):
        vp = self._viewport
        try:
            return vp.width() if vp is not None else self.width()
        except RuntimeError:
            return self.width()

    def _apply_width(self):
        w = self._visible_width()
        wide = (w >= HERO_ROW_MIN, w >= GRID_MIN)
        if wide == self._wide:
            return
        self._wide = wide
        self.hero_row.setDirection(QBoxLayout.LeftToRight if wide[0] else QBoxLayout.TopToBottom)
        self.hero_row.setSpacing(32 if wide[0] else 4)
        # Under the slots the ticket lines up with them (its glow room hangs out to the left).
        self._ticket_col.setAlignment(self.ticket, Qt.AlignHCenter if wide[0] else Qt.AlignLeft)
        self.grid.setDirection(QBoxLayout.LeftToRight if wide[1] else QBoxLayout.TopToBottom)

    def is_wide(self):
        return self._wide

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_width()

    def eventFilter(self, obj, ev):
        if obj is self._viewport and ev.type() == QEvent.Resize:
            self._apply_width()
        return False

    def showEvent(self, e):
        super().showEvent(e)
        parent = self.parentWidget()
        if parent is not None and parent is not self._viewport:
            try:
                if self._viewport is not None:
                    self._viewport.removeEventFilter(self)
            except RuntimeError:
                pass
            parent.installEventFilter(self)
            self._viewport = parent
        self._apply_width()
        QTimer.singleShot(0, lambda: self.render(animate=True))

    # ── shell hooks ──
    def on_open(self, **opts):
        self.opts = dict(opts)
        self.hub.track("invite_page_opened", {"signed_in": bool(self.signed_in())})
        self.hub.open_page()
        self.render()

    def refresh_account(self):
        self.render(animate=False)


def create(main_app):
    return InvitePage(main_app)
