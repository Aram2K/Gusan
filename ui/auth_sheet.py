# -*- coding: utf-8 -*-
"""Sign in / create account (Gusan 2.0): the sheet behind app.show_auth_gate(),
plus the pieces the onboarding's last step shares with it.

    sheet = AuthSheet(main_app, mode="signin")     # or "signup"
    sheet.signed_in.connect(...)                    # then the sheet closes itself
    sheet.show()

    show_auth_sheet(main_app, mode="signin", parent=None)   # one sheet, brought forward

What it does (same server calls as 1.9.x's account gate, never on the GUI thread):
- Continue with Google: auth.sign_in_with_google() on a worker
  (api_keys.run_then); while the browser is open the sheet says "Finish in
  your browser…" with Cancel (auth.cancel_google_login()).
- Email: Sign in (auth.sign_in_email) or Create account (auth.sign_up_email:
  Name optional, Email, Password 6+ characters with a show / hide eye). A
  sign-up that needs confirming shows "Check your inbox" with Resend and
  Wrong email?; when the window is active again (back from the mail app) the
  sheet quietly tries to sign in with what was typed, at most every 8 s.
- Forgot password? (auth.send_password_reset) on the sign-in form.
- Analytics as before: login_succeeded / login_failed (a fixed reason code from
  auth.last_error, never the server's text) / signup_verification_sent.
  A cancelled Google sign-in is not a failure (nothing is sent).

Shared with ui/onboarding_v2.py: AuthFlow (the workers), GoogleButton, Field /
PasswordField, PasswordHint, terms_label, mailbox_for, valid_email.

Integration notes for Wave 4
----------------------------
Status: DONE - show_auth_gate() -> show_auth_sheet(app, "signin") (signed in:
show_main("account")); signed_in -> show_main(); show_account_gate() uses
"signup" and sets account_gate_seen when it closes, but nothing opens it
automatically any more (existing users are never gated at launch).

* AppController.show_auth_gate(): replace the old account-only Onboarding
  with ``show_auth_sheet(self, "signin")`` (keep the "already signed in ->
  open Account" branch; route it to show_main("account")). Connect the
  sheet's ``signed_in`` to whatever should open next (e.g. show_main()).
  show_account_gate() can use the same sheet (mode "signup"); set
  account_gate_seen when it closes, as today.
* AppController.start_google_login() shows a tray hint and gives no result
  back; the sheet and the wizard don't use it (they call
  app.auth.sign_in_with_google on their own worker, as auth.py's notes say).
* The sheet listens to app.sig_auth_changed while it is open: a sign-in that
  completes elsewhere (the tray, a second sheet) closes it as signed in.
  Closing it disconnects again, so kept or reused sheets never pile up slots.
"""

import logging
import re
import time

from PySide6.QtCore import QByteArray, QEvent, QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QStyle, QStyleOptionButton,
                               QStylePainter, QToolButton, QVBoxLayout, QWidget)

from app_info import APP_NAME, resource_path
from ui import theme
from ui.components import Button, icons

logger = logging.getLogger("transcribe")

TERMS_URL = "https://aibuben.xyz/terms"
PRIVACY_URL = "https://aibuben.xyz/privacy"
MIN_PASSWORD = 6
SIGNIN_RETRY_S = 8.0

# Google's own button look (its sign-in branding guidelines), not theme colours.
GOOGLE_BORDER = "#dadce0"
GOOGLE_TEXT = "#1f1f1f"
GOOGLE_HOVER = "#f8faff"

# The mockup's eye (gusan_ds has no "eye" icon; the crossed one is icons "eye_off").
EYE = ('<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/>'
       '<circle cx="12" cy="12" r="3"/>')

MSG_UNAVAILABLE = "Sign-in is unavailable right now."
MSG_BAD_EMAIL = "Enter a valid email address."
MSG_SHORT_PASSWORD = "Use 6 or more characters."
MSG_GOOGLE = {
    "port_busy": "Another sign-in is still open. Close it and try again.",
    "timeout": "Google sign-in timed out. Try again.",
    "network": "Couldn't reach the server. Check your connection.",
}
MSG_GOOGLE_OTHER = "Couldn't sign in with Google. Try again."
MSG_NOT_SENT = "Couldn't send it. Check your connection."

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Webmail for "Open Gmail" after a sign-up: domain -> (button text, url).
_MAILBOXES = (
    (("gmail.com", "googlemail.com"), "Open Gmail", "https://mail.google.com/"),
    (("outlook.com", "hotmail.com", "live.com", "msn.com"), "Open Outlook", "https://outlook.live.com/mail/"),
    (("yahoo.com", "ymail.com"), "Open Yahoo Mail", "https://mail.yahoo.com/"),
    (("icloud.com", "me.com", "mac.com"), "Open iCloud Mail", "https://www.icloud.com/mail"),
    (("proton.me", "protonmail.com"), "Open Proton Mail", "https://mail.proton.me/"),
)


# ── small helpers ────────────────────────────────────────────────────────────
def valid_email(text):
    return bool(_EMAIL.match((text or "").strip()))


def mailbox_for(email):
    """(button text, url) of the webmail for ``email``'s domain, or None."""
    dom = (email or "").strip().lower().rpartition("@")[2]
    for domains, text, url in _MAILBOXES:
        if dom in domains:
            return text, url
    return None


def text_label(text="", px=14, weight=400, color="text", display=False, ls=None, wrap=False, parent=None,
               rich=False):
    """A QLabel in one of the theme's colours (token name or CSS colour)."""
    lb = QLabel(text, parent)
    lb.setFont(theme.font(px, weight, display=display, letter_spacing=ls))
    tint(lb, color)
    if wrap:
        lb.setWordWrap(True)
    if rich:
        lb.setTextFormat(Qt.RichText)
    else:
        lb.setTextFormat(Qt.PlainText)
    return lb


def tint(widget, color):
    widget.setStyleSheet("color: %s; background: transparent;" % theme.qss_color(color))


def svg_pixmap(inner, size, color="muted", sw=1.7):
    """A tinted pixmap of a 24x24 stroke icon given as SVG markup."""
    c = theme.qcolor(color)
    hexc = "#%02x%02x%02x" % (c.red(), c.green(), c.blue())
    doc = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
           'stroke="%s" stroke-width="%s" stroke-linecap="round" stroke-linejoin="round">%s</svg>' % (hexc, sw, inner))
    dpr = icons._dpr()
    pm = QPixmap(max(1, int(round(size * dpr))), max(1, int(round(size * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    QSvgRenderer(QByteArray(doc.encode("utf-8"))).render(p, QRectF(0, 0, size, size))
    p.end()
    return pm


def google_pixmap(size):
    """Google's "G" (assets/google_g.svg) at ``size`` logical px."""
    dpr = icons._dpr()
    pm = QPixmap(max(1, int(round(size * dpr))), max(1, int(round(size * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    r = QSvgRenderer(resource_path("assets", "google_g.svg"))
    if r.isValid():
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        r.render(p, QRectF(0, 0, size, size))
        p.end()
    return pm


def link_button(text, px=13.5, weight=600, color="blue-tx", underline=False, parent=None):
    """A text link (a real button: Tab, Space, Enter, screen readers)."""
    b = Button(text, "link", "s", parent=parent)
    f = theme.font(px, weight)
    f.setUnderline(bool(underline))
    b.setFont(f)
    b.setFixedHeight(max(20, int(px * 1.5)))
    b.setStyleSheet("QPushButton { color: %s; padding: 0px 1px; }" % theme.qss_color(color))
    b.setAutoDefault(False)
    return b


def terms_label(lead="By continuing you agree to the", parent=None, px=12, center=False):
    """'… the Terms and Privacy Policy.' with real links (open in the browser)."""
    blue = theme.qss_color("blue-tx")
    html = ('%s <a href="%s" style="color: %s;">Terms</a> and <a href="%s" style="color: %s;">Privacy Policy</a>.'
            % (lead, TERMS_URL, blue, PRIVACY_URL, blue))
    lb = text_label(html, px, 400, "muted", wrap=True, parent=parent, rich=True)
    lb.setOpenExternalLinks(True)
    lb.setTextInteractionFlags(Qt.TextBrowserInteraction)
    lb.setAccessibleName("By continuing you agree to the Terms and Privacy Policy.")
    if center:
        lb.setAlignment(Qt.AlignHCenter)
    return lb


def _call(app, method, *args, **kwargs):
    fn = getattr(app, method, None)
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("%s() failed", method, exc_info=True)
        return None


def _run_then(fn, *args, then=None, error=None, **kwargs):
    """fn on a worker, then(result) / error(exc) on the GUI thread (tests replace it)."""
    import api_keys
    return api_keys.run_then(fn, *args, then=then, error=error, name="gusan-auth", **kwargs)


# ── the workers ──────────────────────────────────────────────────────────────
class AuthFlow(QObject):
    """Google / email sign-in and sign-up on worker threads.

    google_waiting(bool)        the browser is open (True) / not any more (False)
    done(action, status, msg)   action: google | signin | signup | resend | reset
                                status: ok | verify (signup) | error | cancelled (google)
                                msg: a plain sentence for errors, the email for verify"""

    google_waiting = Signal(bool)
    done = Signal(str, str, str)

    def __init__(self, main_app, parent=None, run=None):
        super().__init__(parent)
        self.app = main_app
        self._run = run
        self._attempt = 0
        self._google_on = False
        self._email_busy = False

    # ── state ──
    def auth(self):
        return getattr(self.app, "auth", None)

    def signed_in(self):
        return bool(getattr(self.auth(), "is_authenticated", False))

    def google_running(self):
        return self._google_on

    def email_busy(self):
        return self._email_busy

    def _go(self, fn, *args, then=None, error=None):
        run = self._run or _run_then
        return run(fn, *args, then=then, error=error)

    def _track(self, event, props=None):
        _call(self.app, "track", event, props or {})

    def _reason(self):
        return getattr(self.auth(), "last_error", "") or "other"

    # ── Google ──
    def google(self):
        """Start "Continue with Google". False when it can't start."""
        auth = self.auth()
        if self._google_on:
            return True
        if auth is None or not callable(getattr(auth, "sign_in_with_google", None)):
            self.done.emit("google", "error", MSG_UNAVAILABLE)
            return False
        self._attempt += 1
        attempt = self._attempt
        self._google_on = True
        self.google_waiting.emit(True)
        self._go(auth.sign_in_with_google,
                 then=lambda ok, a=attempt: self._google_done(a, ok),
                 error=lambda exc, a=attempt: self._google_done(a, False))
        return True

    def cancel_google(self):
        """The "Cancel" next to "Finish in your browser…"."""
        if not self._google_on:
            return
        self._attempt += 1                       # the worker's late answer is ignored
        self._google_on = False
        _call(self.auth(), "cancel_google_login")
        self.google_waiting.emit(False)
        self.done.emit("google", "cancelled", "")

    def _google_done(self, attempt, ok):
        if ok:
            # Even an attempt the user cancelled a moment too late really signed in.
            current = attempt == self._attempt
            if current:
                self._google_on = False
                self.google_waiting.emit(False)
            self._track("login_succeeded", {"method": "google"})
            self.done.emit("google", "ok", "")
            return
        if attempt != self._attempt:
            return                               # cancelled or superseded: already handled
        self._google_on = False
        self.google_waiting.emit(False)
        reason = self._reason()
        if reason in ("cancelled", "superseded"):
            self.done.emit("google", "cancelled", "")
            return
        self._track("login_failed", {"method": "google", "reason": reason})
        self.done.emit("google", "error", MSG_GOOGLE.get(reason, MSG_GOOGLE_OTHER))

    # ── email ──
    def sign_in(self, email, password, quiet=False):
        """quiet: a background retry (after confirming the email) - no analytics."""
        return self._email("signin", email, password, None, quiet)

    def sign_up(self, email, password, name=None):
        return self._email("signup", email, password, name, False)

    def _email(self, mode, email, password, name, quiet):
        auth = self.auth()
        if self._email_busy:
            return False
        fn = getattr(auth, "sign_up_email" if mode == "signup" else "sign_in_email", None)
        if not callable(fn):
            self.done.emit(mode, "error", MSG_UNAVAILABLE)
            return False
        self._email_busy = True
        args = (email, password, name or None) if mode == "signup" else (email, password)
        self._go(fn, *args,
                 then=lambda res, m=mode, q=quiet: self._email_done(m, res, q),
                 error=lambda exc, m=mode, q=quiet: self._email_done(m, ("error", MSG_UNAVAILABLE), q))
        return True

    def _email_done(self, mode, result, quiet):
        self._email_busy = False
        try:
            status, msg = result
        except Exception:
            status, msg = "error", MSG_UNAVAILABLE
        status = status if status in ("ok", "verify", "error") else "error"
        if not quiet:
            if status == "ok":
                self._track("login_succeeded", {"method": "email", "mode": mode})
            elif status == "verify":
                self._track("signup_verification_sent")
            else:
                self._track("login_failed", {"method": "email", "mode": mode, "reason": self._reason()})
        self.done.emit(mode, status, msg or "")

    def resend(self, email):
        fn = getattr(self.auth(), "resend_verification", None)
        if not callable(fn):
            self.done.emit("resend", "error", MSG_UNAVAILABLE)
            return False
        self._go(fn, email,
                 then=lambda ok: self.done.emit("resend", "ok" if ok else "error", ""),
                 error=lambda exc: self.done.emit("resend", "error", ""))
        return True

    def reset(self, email):
        fn = getattr(self.auth(), "send_password_reset", None)
        if not callable(fn):
            self.done.emit("reset", "error", MSG_UNAVAILABLE)
            return False
        self._go(fn, email,
                 then=lambda ok: self.done.emit("reset", "ok" if ok else "error", ""),
                 error=lambda exc: self.done.emit("reset", "error", ""))
        return True


# ── widgets ──────────────────────────────────────────────────────────────────
class GoogleButton(QPushButton):
    """The white "Continue with Google" button (Google's branding: white,
    #dadce0 edge, the four-colour G, 12 px between the G and the words)."""

    GAP = 12

    def __init__(self, text="Continue with Google", height=56, px=16, g=22, radius=12, parent=None,
                 shadow=True):
        super().__init__(text, parent)
        self._g = g
        self._gpix = google_pixmap(g)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFont(theme.font(px, 600))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAutoDefault(False)
        self.setAccessibleName(text)
        theme.focus_ring(self)
        self.setProperty("gusan", "google")
        blue = theme.qss_color("blue-l")
        self.setStyleSheet(
            "QPushButton { background: #ffffff; color: %s; border: 1px solid %s; border-radius: %dpx; "
            "padding: 0px 20px; }"
            "QPushButton:hover { background: %s; }"
            "QPushButton:pressed { background: %s; }"
            "QPushButton:disabled { background: #ffffff; color: %s; }"
            "QPushButton[focusVisible=\"true\"] { border: 2px solid %s; }"
            % (GOOGLE_TEXT, GOOGLE_BORDER, radius, GOOGLE_HOVER, theme.qss_color("bg2"),
               theme.qss_color("muted"), blue))
        if shadow:
            eff = QGraphicsDropShadowEffect(self)
            eff.setBlurRadius(24)
            eff.setOffset(0, 8)
            eff.setColor(QColor(15, 23, 42, 26))
            self.setGraphicsEffect(eff)

    def sizeHint(self):
        fm = QFontMetricsF(self.font())
        return QSize(int(self._g + self.GAP + fm.horizontalAdvance(self.text()) + 44), self.height())

    def paintEvent(self, _e):
        # The panel from the stylesheet (no label), then the G and the words, centred.
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        opt.text = ""
        opt.icon = QIcon()
        sp = QStylePainter(self)
        sp.drawControl(QStyle.CE_PushButton, opt)
        fm = QFontMetricsF(self.font())
        tw = fm.horizontalAdvance(self.text())
        x = (self.width() - (self._g + self.GAP + tw)) / 2.0
        sp.setRenderHint(QPainter.Antialiasing, True)
        sp.drawPixmap(QRectF(x, (self.height() - self._g) / 2.0, self._g, self._g).toRect(), self._gpix)
        sp.setFont(self.font())
        sp.setPen(theme.qcolor(GOOGLE_TEXT if self.isEnabled() else "muted"))
        sp.drawText(QRectF(x + self._g + self.GAP, 0, tw + 4, self.height()), Qt.AlignVCenter | Qt.AlignLeft,
                    self.text())
        sp.end()


class FieldBox(QFrame):
    """A 44 px white input box (radius 10): 1 px slate edge; while its line
    edit has focus a 1.5 px blue edge with a soft blue band inside it (the
    mockup's focus glow, kept inside so boxes line up with the buttons);
    red after a refused value."""

    GLOW = 0              # no room outside the box: the glow is drawn inside

    def __init__(self, edit, trail=None, height=44, parent=None):
        super().__init__(parent)
        self.edit = edit
        self._error = False
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 5, 0)
        lay.setSpacing(6)
        edit.setParent(self)
        edit.setFrame(False)
        edit.setStyleSheet("QLineEdit { background: transparent; border: none; padding: 0px; color: %s; }"
                           % theme.qss_color("ink"))
        edit.setFont(theme.font(14.5))
        edit.setMinimumHeight(height - 4)
        edit.installEventFilter(self)
        lay.addWidget(edit, 1)
        if trail is not None:
            trail.setParent(self)
            lay.addWidget(trail, 0, Qt.AlignVCenter)

    def set_error(self, on):
        self._error = bool(on)
        self.update()

    def eventFilter(self, obj, ev):
        if obj is self.edit and ev.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self.update()
        return False

    def has_error(self):
        return self._error

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        box = QRectF(0, 0, self.width(), self.height())
        focused = self.edit.hasFocus()
        edge = "red" if self._error else ("blue-l" if focused else "line2")
        w = 1.5 if (focused or self._error) else 1.0
        p.setPen(QPen(theme.qcolor(edge), w))
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(box.adjusted(w / 2, w / 2, -w / 2, -w / 2), 10, 10)
        if focused:
            glow = theme.qcolor("rgba(220,38,38,0.14)" if self._error else "rgba(59,130,246,0.14)")
            p.setPen(QPen(glow, 3))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box.adjusted(3, 3, -3, -3), 8.5, 8.5)
        p.end()


class Field(QWidget):
    """Label (+ "(optional)") over a FieldBox. ``.edit`` is the QLineEdit."""

    def __init__(self, label_text, optional=False, placeholder="", echo=QLineEdit.Normal, trail=None,
                 parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        opt = (' <span style="font-weight: 400; color: %s;">(optional)</span>' % theme.qss_color("muted")
               if optional else "")
        self.label = text_label(label_text + opt, 13, 600, "text", parent=self, rich=True)
        self.label.setFixedHeight(18)
        lay.addWidget(self.label)
        self.edit = QLineEdit(self)
        self.edit.setEchoMode(echo)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setAccessibleName(label_text + (" (optional)" if optional else ""))
        self.label.setBuddy(self.edit)
        self.box = FieldBox(self.edit, trail=trail, parent=self)
        lay.addWidget(self.box)
        self.extra = QVBoxLayout()        # e.g. the password hint: 6 px under the box's edge
        self.extra.setContentsMargins(0, 0, 0, 0)
        self.extra.setSpacing(0)
        lay.addLayout(self.extra)

    def text(self):
        return self.edit.text()

    def set_error(self, on):
        self.box.set_error(on)


class PasswordField(Field):
    """Password with the show / hide eye."""

    def __init__(self, label_text="Password", parent=None):
        eye = QToolButton()
        eye.setProperty("gusan", "iconBtn")
        eye.setFixedSize(34, 34)
        eye.setIconSize(QSize(18, 18))
        eye.setCursor(Qt.PointingHandCursor)
        eye.setFocusPolicy(Qt.TabFocus)
        eye.setAutoRaise(True)
        theme.focus_ring(eye)
        super().__init__(label_text, echo=QLineEdit.Password, trail=eye, parent=parent)
        self.eye = eye
        self.eye.clicked.connect(self.toggle)
        self._show_state()

    def visible(self):
        return self.edit.echoMode() == QLineEdit.Normal

    def toggle(self):
        self.edit.setEchoMode(QLineEdit.Password if self.visible() else QLineEdit.Normal)
        self._show_state()

    def _show_state(self):
        shown = self.visible()
        name = "Hide password" if shown else "Show password"
        self.eye.setIcon(QIcon(icons.pixmap("eye_off", 18, "muted") if shown else svg_pixmap(EYE, 18, "muted")))
        self.eye.setAccessibleName(name)
        self.eye.setToolTip(name)


class PasswordHint(QWidget):
    """"6+ characters": muted until the password is long enough, then green with a check."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        self.icon = QLabel(self)
        self.icon.setFixedSize(14, 14)
        lay.addWidget(self.icon, 0, Qt.AlignVCenter)
        self.text = text_label("6+ characters", 12, 400, "muted", parent=self)
        lay.addWidget(self.text, 0, Qt.AlignVCenter)
        lay.addStretch(1)
        self.setFixedHeight(16)
        self._ok = None
        self.set_ok(False)

    def is_ok(self):
        return bool(self._ok)

    def set_ok(self, ok):
        ok = bool(ok)
        if ok == self._ok:
            return
        self._ok = ok
        if ok:
            self.icon.setPixmap(icons.pixmap("check", 14, "ok", sw=2.4))
            tint(self.text, "ok-tx")
        else:
            self.icon.setPixmap(icons.pixmap("circle", 14, "faint", sw=2))
            tint(self.text, "muted")
        self.setAccessibleName("6+ characters" + (", done" if ok else ""))


class _OrRow(QWidget):
    """—— or ——"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        for i in range(3):
            if i == 1:
                lay.addWidget(text_label("or", 12, 400, "muted", parent=self))
            else:
                line = QFrame(self)
                line.setProperty("gusan", "divider")
                line.setFixedHeight(1)
                lay.addWidget(line, 1, Qt.AlignVCenter)


def brand_row(parent=None, size=26, px=18):
    """Logo + "Gusan" (the top-left of every onboarding step and the sheet)."""
    w = QWidget(parent)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(9)
    logo = QLabel(w)
    logo.setPixmap(icons.logo_pixmap(size))
    logo.setFixedSize(size, size)
    logo.setAccessibleName(APP_NAME)
    lay.addWidget(logo)
    lay.addWidget(text_label(APP_NAME, px, 700, "ink", display=True, ls=-0.01 * px, parent=w))
    w.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return w


# ── the sheet ────────────────────────────────────────────────────────────────
class AuthSheet(QDialog):
    """Sign in / create an account. See the module docstring."""

    signed_in = Signal()

    W, H = 440, 640

    def __init__(self, main_app, mode="signin", parent=None, run=None):
        super().__init__(parent)
        self.app = main_app
        self.mode = "signup" if mode == "signup" else "signin"
        self._pending_email = ""
        self._pending_password = ""
        self._last_retry = 0.0
        self._done = False
        theme.apply(self)
        self.setWindowTitle("Sign in to %s" % APP_NAME if self.mode == "signin" else "Create your %s account" % APP_NAME)
        self.setMinimumSize(360, 420)
        self.resize(self.W, self.H)

        self.flow = AuthFlow(main_app, self, run=run)
        self.flow.google_waiting.connect(self._on_google_waiting)
        self.flow.done.connect(self._on_done)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setProperty("gusan", "scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(scroll)
        body = QWidget()
        body.setProperty("gusan", "scrollContent")
        scroll.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(32, 26, 32, 24)
        root.setSpacing(0)
        root.addWidget(brand_row(body), 0, Qt.AlignLeft)
        root.addSpacing(26)
        self.stack = QStackedWidget(body)
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_form())
        self.stack.addWidget(self._build_google())
        self.stack.addWidget(self._build_inbox())
        root.addSpacing(16)
        self.terms = terms_label(parent=body, center=True)
        root.addWidget(self.terms)
        self._apply_mode()

        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
            b.setDefault(False)
        self._auth_sig = None
        self._listen(True)

    def _listen(self, on):
        """Follow app.sig_auth_changed while the sheet is open (a sign-in that
        completes elsewhere closes it); a closed sheet lets go of it."""
        if on and self._auth_sig is None:
            sig = getattr(self.app, "sig_auth_changed", None)
            if sig is not None and hasattr(sig, "connect"):
                try:
                    sig.connect(self._on_auth_changed)
                    self._auth_sig = sig
                except Exception:
                    logger.debug("auth sheet: no sig_auth_changed", exc_info=True)
        elif not on and self._auth_sig is not None:
            sig, self._auth_sig = self._auth_sig, None
            try:
                sig.disconnect(self._on_auth_changed)
            except (RuntimeError, TypeError):
                pass

    # ── pages ──
    def _build_form(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.title = text_label("", 28, 700, "ink", display=True, ls=-0.56, parent=page)
        self.title.setFixedHeight(34)
        v.addWidget(self.title)
        v.addSpacing(20)
        self.google_btn = GoogleButton(height=50, px=15, g=20, radius=10, parent=page, shadow=False)
        self.google_btn.clicked.connect(self._google)
        v.addWidget(self.google_btn)
        v.addSpacing(16)
        v.addWidget(_OrRow(page))
        v.addSpacing(12)
        self.name_field = Field("Name", optional=True, parent=page)
        self.email_field = Field("Email", placeholder="you@example.com", parent=page)
        self.password_field = PasswordField(parent=page)
        self.hint = PasswordHint(page)
        self.password_field.extra.addWidget(self.hint)
        last = (getattr(self.app, "cfg", {}) or {}).get("last_signin_email") or ""
        if last and self.mode == "signin":
            self.email_field.edit.setText(str(last))
        for f in (self.name_field, self.email_field, self.password_field):
            v.addWidget(f)
            v.addSpacing(12)
        self.password_field.edit.textChanged.connect(lambda t: self.hint.set_ok(len(t) >= MIN_PASSWORD))
        self.password_field.edit.returnPressed.connect(self._submit)
        self.email_field.edit.returnPressed.connect(lambda: self.password_field.edit.setFocus(Qt.TabFocusReason))
        self.message = text_label("", 12.5, 400, "red-tx", wrap=True, parent=page)
        self.message.hide()
        v.addWidget(self.message)
        v.addSpacing(6)
        self.submit = Button("Sign in", "primary", "l", parent=page, full=True)
        self.submit.clicked.connect(self._submit)
        v.addWidget(self.submit)
        v.addSpacing(10)
        self.forgot = link_button("Forgot password?", 13, parent=page)
        self.forgot.clicked.connect(self._forgot)
        v.addWidget(self.forgot, 0, Qt.AlignHCenter)
        v.addStretch(1)
        v.addSpacing(16)
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addStretch(1)
        self.switch_lead = text_label("", 13.5, 400, "muted", parent=page)
        row.addWidget(self.switch_lead)
        self.switch_btn = link_button("", 13.5, parent=page)
        self.switch_btn.clicked.connect(self._switch_mode)
        row.addWidget(self.switch_btn)
        row.addStretch(1)
        v.addLayout(row)
        return page

    def _build_google(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addStretch(1)
        g = QLabel(page)
        g.setPixmap(google_pixmap(40))
        g.setFixedSize(40, 40)
        g.setAccessibleName("Google")
        v.addWidget(g, 0, Qt.AlignHCenter)
        v.addSpacing(18)
        t = text_label("Finish in your browser…", 22, 700, "ink", display=True, parent=page)
        t.setAlignment(Qt.AlignHCenter)
        v.addWidget(t)
        v.addSpacing(20)
        self.google_cancel = Button("Cancel", "secondary", "m", parent=page)
        self.google_cancel.clicked.connect(self.flow.cancel_google)
        v.addWidget(self.google_cancel, 0, Qt.AlignHCenter)
        v.addStretch(2)
        return page

    def _build_inbox(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addStretch(1)
        tile = QLabel(page)
        tile.setFixedSize(56, 56)
        tile.setAlignment(Qt.AlignCenter)
        tile.setPixmap(icons.pixmap("mail", 28, "blue-tx"))
        tile.setStyleSheet("background: %s; border-radius: 16px;" % theme.qss_color("blue-sub"))
        v.addWidget(tile, 0, Qt.AlignLeft)
        v.addSpacing(18)
        v.addWidget(text_label("Check your inbox", 28, 700, "ink", display=True, ls=-0.56, parent=page))
        v.addSpacing(8)
        self.inbox_line = text_label("", 15, 400, "text2", wrap=True, parent=page, rich=True)
        v.addWidget(self.inbox_line)
        v.addSpacing(18)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(text_label("No email?", 13.5, 400, "muted", parent=page))
        self.resend_btn = link_button("Resend", 13.5, parent=page)
        self.resend_btn.clicked.connect(self._resend)
        row.addWidget(self.resend_btn)
        row.addWidget(text_label("·", 13.5, 400, "faint", parent=page))
        self.wrong_btn = link_button("Wrong email?", 13.5, parent=page)
        self.wrong_btn.clicked.connect(self._wrong_email)
        row.addWidget(self.wrong_btn)
        row.addStretch(1)
        v.addLayout(row)
        v.addSpacing(24)
        self.inbox_message = text_label("", 12.5, 400, "muted", wrap=True, parent=page)
        self.inbox_message.hide()
        v.addWidget(self.inbox_message)
        self.inbox_signin = Button("Sign in", "primary", "l", parent=page, full=True)
        self.inbox_signin.clicked.connect(lambda: self._retry_signin(quiet=False))
        v.addWidget(self.inbox_signin)
        v.addStretch(2)
        return page

    # ── modes ──
    def _apply_mode(self):
        up = self.mode == "signup"
        self.title.setText("Create your account" if up else "Sign in")
        self.title.setAccessibleName(self.title.text())
        self.name_field.setVisible(up)
        self.hint.setVisible(up)
        self.forgot.setVisible(not up)
        self.submit.setText("Create account" if up else "Sign in")
        self.submit.setAccessibleName(self.submit.text())
        self.switch_lead.setText("Already have an account?" if up else "New to %s?" % APP_NAME)
        self.switch_btn.setText("Sign in" if up else "Create an account")
        self.switch_btn.setAccessibleName(self.switch_btn.text())
        self.setWindowTitle("Create your %s account" % APP_NAME if up else "Sign in to %s" % APP_NAME)
        self._show_message("")

    def set_mode(self, mode):
        self.mode = "signup" if mode == "signup" else "signin"
        self._apply_mode()
        self.stack.setCurrentIndex(0)

    def _switch_mode(self):
        self.set_mode("signin" if self.mode == "signup" else "signup")

    def page_name(self):
        return ("form", "google", "inbox")[self.stack.currentIndex()]

    # ── actions ──
    def _show_message(self, text, ok=False):
        self.message.setText(text or "")
        tint(self.message, "ok-tx" if ok else "red-tx")
        self.message.setVisible(bool(text))

    def _google(self):
        self._show_message("")
        self.flow.google()

    def _on_google_waiting(self, on):
        if on:
            self.stack.setCurrentIndex(1)
        elif self.stack.currentIndex() == 1:
            self.stack.setCurrentIndex(0)

    def _submit(self):
        email = self.email_field.text().strip()
        pw = self.password_field.text()
        self.email_field.set_error(False)
        self.password_field.set_error(False)
        if not valid_email(email):
            self.email_field.set_error(True)
            self._show_message(MSG_BAD_EMAIL)
            self.email_field.edit.setFocus(Qt.OtherFocusReason)
            return
        if len(pw) < MIN_PASSWORD:
            self.password_field.set_error(True)
            self._show_message(MSG_SHORT_PASSWORD)
            self.password_field.edit.setFocus(Qt.OtherFocusReason)
            return
        self._show_message("")
        # Busy first: the answer may come back before sign_in()/sign_up() returns.
        self._busy(True)
        if self.mode == "signup":
            started = self.flow.sign_up(email, pw, self.name_field.text().strip() or None)
        else:
            started = self.flow.sign_in(email, pw)
        if not started and not self._done:
            self._busy(False)

    def _busy(self, on):
        for w in (self.name_field, self.email_field, self.password_field, self.google_btn, self.switch_btn, self.forgot):
            w.setEnabled(not on)
        self.submit.setEnabled(not on)
        if on:
            self.submit.setText("Creating…" if self.mode == "signup" else "Signing in…")
        else:
            self.submit.setText("Create account" if self.mode == "signup" else "Sign in")

    def _forgot(self):
        email = self.email_field.text().strip()
        if not valid_email(email):
            self.email_field.set_error(True)
            self._show_message("Enter your email first.")
            self.email_field.edit.setFocus(Qt.OtherFocusReason)
            return
        # The message first: a failure that comes back at once replaces it.
        self._show_message("If there's an account, a reset link is on its way.", ok=True)
        self.flow.reset(email)

    def _resend(self):
        if self._pending_email:
            self.inbox_message.hide()
            # "Sent" first: the answer may come back before resend() returns.
            self.resend_btn.setText("Sent")
            self.resend_btn.setEnabled(False)
            QTimer.singleShot(20000, self._resend_again)
            self.flow.resend(self._pending_email)

    def _resend_again(self):
        try:
            self.resend_btn.setText("Resend")
            self.resend_btn.setEnabled(True)
        except RuntimeError:
            pass

    def _wrong_email(self):
        self.set_mode("signup")
        self.stack.setCurrentIndex(0)
        self.email_field.edit.setFocus(Qt.OtherFocusReason)
        self.email_field.edit.selectAll()

    def _retry_signin(self, quiet=False):
        if not (self._pending_email and self._pending_password) or self.flow.email_busy():
            return
        self._last_retry = time.monotonic()
        self._retry_quiet = bool(quiet)
        if not quiet:
            self.inbox_signin.setEnabled(False)          # the answer re-enables it
        self.flow.sign_in(self._pending_email, self._pending_password, quiet=quiet)

    def _on_done(self, action, status, msg):
        if action in ("signin", "signup"):
            self._busy(False)
            self.inbox_signin.setEnabled(True)
        if status == "ok" and action in ("google", "signin", "signup"):
            self._finish()
            return
        if action == "signup" and status == "verify":
            self._pending_email = msg or self.email_field.text().strip()
            self._pending_password = self.password_field.text()
            self.inbox_line.setText('We sent a link to <b style="color: %s;">%s</b>'
                                    % (theme.qss_color("ink"), _esc(self._pending_email)))
            self.inbox_line.setAccessibleName("We sent a link to %s" % self._pending_email)
            self.inbox_message.hide()
            self.stack.setCurrentIndex(2)
            return
        if action == "signin" and self.stack.currentIndex() == 2:
            # A retry after the window came back stays silent (the email is
            # probably not confirmed yet); a clicked Sign in says why.
            if not getattr(self, "_retry_quiet", False) and msg:
                self.inbox_message.setText(msg)
                self.inbox_message.show()
            return
        if action == "google" and status == "error":
            self.stack.setCurrentIndex(0)
            self._show_message(msg)
            return
        if action in ("signin", "signup") and status == "error":
            self._show_message(msg or "Something went wrong. Try again.")
            return
        if action == "reset" and status == "error":
            # _forgot() said "on its way" at once; the server says otherwise.
            self._show_message(MSG_NOT_SENT)
            return
        if action == "resend" and status == "error":
            self.inbox_message.setText(MSG_NOT_SENT)
            self.inbox_message.show()
            self._resend_again()

    def _on_auth_changed(self):
        if not self._done and bool(getattr(getattr(self.app, "auth", None), "is_authenticated", False)):
            if self.isVisible():
                self._finish()

    def _finish(self):
        if self._done:
            return
        self._done = True
        self._pending_password = ""
        self.signed_in.emit()
        self.accept()

    # ── window ──
    def event(self, ev):
        if (ev.type() == QEvent.WindowActivate and self.stack.currentIndex() == 2
                and time.monotonic() - self._last_retry > SIGNIN_RETRY_S):
            QTimer.singleShot(0, lambda: self._retry_signin(quiet=True))
        return super().event(ev)

    def showEvent(self, ev):
        super().showEvent(ev)
        self._done = False
        self._listen(True)
        try:
            from ui.winfit import settle_on_screen
            settle_on_screen(self)
        except Exception:
            logger.debug("fit on screen failed", exc_info=True)

    def done(self, result):
        # Closing the sheet stops a Google sign-in still waiting for the browser.
        self.flow.cancel_google()
        self._pending_password = ""
        self._listen(False)
        super().done(result)


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_SHEET = {}


def show_auth_sheet(main_app, mode="signin", parent=None):
    """Show the one sign-in sheet for ``main_app`` (a new one once it closed)."""
    sheet = _SHEET.get(id(main_app))
    try:
        alive = sheet is not None and sheet.isVisible()
    except RuntimeError:
        alive = False
    if not alive:
        sheet = AuthSheet(main_app, mode=mode, parent=parent)
        sheet.setAttribute(Qt.WA_DeleteOnClose, True)
        _SHEET[id(main_app)] = sheet
    elif sheet.page_name() == "form" and sheet.mode != mode:
        sheet.set_mode(mode)
    sheet.show()
    sheet.raise_()
    sheet.activateWindow()
    return sheet
