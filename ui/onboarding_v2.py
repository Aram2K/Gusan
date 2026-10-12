# -*- coding: utf-8 -*-
"""Gusan 2.0 onboarding: a 960 x 640 wizard in five steps.

    wiz = OnboardingWizard(main_app)          # or create(main_app)
    wiz.accepted.connect(lambda: main_app.show_main("home"))
    wiz.show()

1 Hello      "Speak. Gusan types.", the real shortcut as big keys, a typing demo
             with the dictation bar; Get started / Have an account? Sign in.
2 Model      "Pick your speech model": Your computer chips and "Best for your
             computer" from the hardware probe, three cards (Fast / Balanced /
             Most accurate) with the real Whisper names, sizes and meters, Gusan
             Cloud (PRO), More sizes. The button follows the pick ("Download
             Balanced · 460 MB"); it sets whisper_model (app.set_setting) and
             starts app.downloads.start(model). Gusan Cloud: Free -> the current
             Pro window (app._pro_upsell("Fast cloud transcription")), Pro -> it is
             used (engine_choice.set_speech_source + app.set_settings).
3 Mic        A live level ring (mic_monitor.MicMonitor, open only on this step),
             the microphone picker (input_device_index, applied at once), "Say hi"
             turning into "We hear you" (the button then reads "Sounds good";
             "Next" before that and on "Can’t open this mic" / "No mic found"),
             the model download chip.
4 Try it     The real shortcut lit up, the practice box that receives the real
             paste, a dictation bar that follows the real dictation (app.is_rec /
             app._busy), "Works in any app" after the first paste, Change shortcut.
5 Sign up    "Keep your words." with the first dictation quoted back; Continue
             with Google (auth worker, "Finish in your browser…" + Cancel),
             Continue with email -> 5b form -> 5c Check your inbox (Resend, Wrong
             email?); Skip for now; Share anonymous usage stats; Terms / Privacy.

Finishing (signed in, Skip for now, or Next on step 4 when already signed in)
writes onboarding_done, account_gate_seen and analytics_enabled with one
app.set_settings call and emits ``accepted``. Closing the window writes nothing
else (the model choice was already applied on step 2). Esc doesn't close it.
"Skip" (top right of steps 1-4) goes to Sign up, or finishes when signed in.
Either way done() closes everything the wizard opened (mic stream, shortcut
capture, a waiting Google sign-in, the sign-in sheet) and disconnects from the
controller and app.downloads; showing it again reconnects.
Keyboard focus follows the steps (each step's main control); "Checking…" on
step 2 gives way after 15 s without a hardware answer.

Integration notes for Wave 4
----------------------------
Status: DONE - AppController.show_onboarding() builds this wizard lazily on the
first run only (onboarding_done False; an onboarded user gets show_main()),
probes the hardware then, and accepted -> show_main("home"). ui/onboarding.py
is no longer used by main.py.

* main.py: build ``OnboardingWizard(self)`` instead of ui.onboarding.Onboarding
  for first runs (keep ``accepted -> show_main("home")``). Build it lazily when
  onboarding_done is False, and call ``self.downloads.probe_hardware()`` then,
  so step 2 has the chips by the time the user gets there. The wizard listens
  to ``sig_setting_changed`` and ``sig_auth_changed`` on its own.
* Services it uses when present (getattr; it degrades without them):
    app.downloads     hardware / recommendation / probe_hardware(), state(),
                      start(), progress_of(), signals progress / finished /
                      state_changed / hardware_ready. Without it the chips come
                      from hardware.snapshot(probe_gpu=False) on a worker, the
                      button says "Next" and nothing downloads.
    app.mic_monitor   a shared mic_monitor.MicMonitor (see mic_monitor.py);
                      otherwise the wizard makes its own on app.recorder.audio.
    app.suspend_hotkeys(bool)   while "Change shortcut" waits for keys (the
                      Dictation page asks for the same method).
* Recording state on step 4 is polled (app.is_rec, app._busy every 150 ms,
  only while that step shows). A ``sig_recording_changed(bool)`` /
  ``sig_busy_changed(bool)`` on the controller would replace the poll.
* The practice box gets the real paste: the dictation must paste into the
  wizard (it is the active window with the box focused), as today.
* Step 4 while the chosen model still downloads: the dictation must wait on
  app.downloads (model_manager's notes) - the wizard only shows the chip.
* GPU: if the recommendation needs the NVIDIA download and the user keeps that
  model, finishing sets gpu_offer_intro_done and calls
  app.start_gpu_download("onboarding") (as 1.9.x did).
* show_auth_gate / the account gate: ui/auth_sheet.py (show_auth_sheet).
* Already signed in when the wizard opens (or after "Sign in" on Hello): step 5
  is skipped - step 4's button reads "Done" and finishes.
"""

import logging
import math
import time
from datetime import datetime

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFontMetricsF, QIcon, QLinearGradient, QPainter,
                           QPainterPath, QPen, QRadialGradient, QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QTextEdit, QVBoxLayout,
                               QWidget)

import hardware
import mic_monitor
from app_info import APP_NAME
from ui import theme
from ui.auth_sheet import (MIN_PASSWORD, MSG_BAD_EMAIL, MSG_SHORT_PASSWORD, AuthFlow, AuthSheet, Field, GoogleButton,
                           PasswordField, PasswordHint, brand_row, link_button, mailbox_for, svg_pixmap, terms_label,
                           text_label, tint, valid_email)
from ui.components import Button, Card, Chip, Divider, ProTag, WhiteCard, icons
from ui.components.controls import keycap_labels
from ui.components.hotkey_capture import HotkeyCaptureButton

logger = logging.getLogger("transcribe")

W, H = 960, 640
TOP, BOT, PAD = 72, 84, 40
STEPS = 5
PAGES = ("hello", "model", "mic", "try", "signup", "email", "inbox")
STEP_OF = {"hello": 1, "model": 2, "mic": 3, "try": 4, "signup": 5, "email": 5, "inbox": 5}
BACK_TO = {"model": "hello", "mic": "model", "try": "mic", "signup": "try", "email": "signup", "inbox": "email"}
CLOUD = "cloud"
UPSELL_CLOUD = "Fast cloud transcription"
GPU_INTRO_KEY = "gpu_offer_intro_done"          # gpu_accel.CFG_INTRO
REC_POLL_MS = 150
FRAME_MS = 33
# Room a drop shadow ("card": blur 24, dy 8) needs beyond a widget's edge: a
# graphics effect is clipped by its parent, so a column whose card or button
# touches its own edge would show a hard seam where the shadow stops.
SHADOW_ROOM = 14
HW_GIVE_UP_MS = 15000            # no hardware answer by then: drop "Checking…"

DEMO_SENTENCE = "Can we move the review to Tuesday at 3? I’ll bring the notes from the call."
WAVE_HEIGHTS = (12, 20, 34, 25, 46, 32, 18, 39, 52, 30, 44, 23, 34, 16, 10)
RING_HEIGHTS = (16, 23, 34, 26, 39, 31, 18, 12, 21, 36, 29, 16, 10, 18, 31, 42, 34, 23, 13, 9, 16, 26)
PILL_WAVE = (4, 7, 11, 15, 19, 22, 20, 17, 21, 16, 12, 9, 6, 4, 3)

# The dictation bar's light glass (ui/overlay.py, gusan_ds.PILL_THEME["light"]).
PILL_TINT = QColor(250, 250, 252, 240)
PILL_INK = (15, 23, 42)
PILL_REC = QColor("#E9152D")
PILL_DONE = QColor("#16A34A")
PILL_HAIR = QColor(15, 23, 42, 38)
# The mockup's pressed keycap on Try it, and its illustration colours.
LIT_FILL = QColor("#eff6ff")
LIT_EDGE = QColor("#93c5fd")
ENV_FLAP = QColor("#d3deec")
ENV_BACK = QColor("#dbe4f0")
ENV_FRONT = QColor("#eef3f9")
ANNA_A, ANNA_B = QColor("#f472b6"), QColor("#db2777")
TASK_APP_A, TASK_APP_B = QColor("#cbd5e1"), QColor("#d6dee8")
WINDOW_CTL = '<rect x="4" y="4" width="16" height="16" rx="1.5"/>'


# ── helpers ──────────────────────────────────────────────────────────────────
def _cfg(app):
    c = getattr(app, "cfg", None)
    return c if isinstance(c, dict) else {}


def _call(app, method, *args, **kwargs):
    fn = getattr(app, method, None)
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("%s() failed", method, exc_info=True)
        return None


def _norm_hotkey(hk):
    """'Alt + S' -> 'alt+s' (settings_apply.norm_hotkey)."""
    return "+".join(p.strip().lower() for p in str(hk or "").split("+") if p.strip())


def hotkey_refusal(cfg, combo):
    """Why a dictation shortcut was refused: it is another feature's keys
    ("Alt + S runs Smart Actions. Pick another."), or a plain retry line."""
    cfg = cfg or {}
    combo_n = _norm_hotkey(combo)
    shown = " + ".join(keycap_labels(combo)) or str(combo or "")
    smart = cfg.get("smart_actions_hotkey", "alt+s")          # main.DEFAULT when missing
    if combo_n and combo_n == _norm_hotkey(smart):
        return "%s runs Smart Actions. Pick another." % shown
    if combo_n and combo_n == _norm_hotkey(cfg.get("live_assist_hotkey")):
        return "%s opens Live Assistant. Pick another." % shown
    return "That shortcut can’t be used. Try another."


def _signed_in(app):
    return bool(getattr(getattr(app, "auth", None), "is_authenticated", False))


def _is_pro(app):
    try:
        return bool(app.is_pro())
    except Exception:
        return False


def _now():
    return datetime.now()


def _run_then(fn, *args, then=None, error=None, **kwargs):
    import api_keys
    return api_keys.run_then(fn, *args, then=then, error=error, name="gusan-onboarding", **kwargs)


def ink(alpha):
    return QColor(PILL_INK[0], PILL_INK[1], PILL_INK[2], int(round(alpha * 255)))


def mailbox(email):
    """(button text, url) for 5c's button: the address's webmail when known,
    else Gmail (the mockup's "Open Gmail"; Google Workspace domains use it too)."""
    return mailbox_for(email) or ("Open Gmail", "https://mail.google.com/")


def title_label(text, px, lh, parent=None):
    """A display-face headline (700, -0.02em) on a fixed CSS line height."""
    lb = text_label(text, px, 700, "ink", display=True, ls=-0.02 * px, parent=parent)
    lb.setFixedHeight(lh)
    lb.setAccessibleName(text)
    return lb


def _plain_link(text, px=13.5, color="muted", parent=None):
    """A quiet text button ("Skip")."""
    b = Button(text, "ghost", "m", parent=parent)
    b.setFont(theme.font(px, 600))
    b.setStyleSheet("QPushButton { color: %s; padding: 0px 4px; background: transparent; }"
                    "QPushButton:hover { color: %s; }" % (theme.qss_color(color), theme.qss_color("text2")))
    b.setAutoDefault(False)
    return b


def _back_button(parent=None):
    b = Button("Back", "ghost", "l", parent=parent)
    b.setIcon(icons.icon("chev_l", 18, "muted"))
    b.setStyleSheet("QPushButton { color: %s; padding: 0px 16px 0px 10px; }" % theme.qss_color("muted"))
    b.setAutoDefault(False)
    return b


def _primary(text, icon=None, kind="primary", parent=None):
    b = Button(text, kind, "l", icon=icon, parent=parent)
    b.setStyleSheet("QPushButton { padding: 0px 24px; }")
    b.setAutoDefault(False)
    return b


def _set_button(b, text, icon=None, kind=None):
    """Change a Button's text / icon / kind in place (the model step's CTA)."""
    b.setText(text)
    b.setAccessibleName(text)
    if kind and b.property("kind") != kind:
        b.setProperty("kind", kind)
        b._kind = kind
        theme.repolish(b, children=False)
        weight = 700 if kind == "pro" else 600
        b.setFont(theme.font(15, weight))
    if icon:
        b.setIcon(icons.icon(icon, 18, "#ffffff"))
        b.setIconSize(QSize(18, 18))
    else:
        b.setIcon(QIcon())
    b.updateGeometry()


class _Painted(QWidget):
    """A plain widget whose paintEvent is ``painter(widget)`` (PySide only
    dispatches overrides defined on the class, not ones assigned later)."""

    def __init__(self, painter, parent=None):
        super().__init__(parent)
        self._painter = painter

    def paintEvent(self, _e):
        self._painter(self)


def _shadow_rects(p, rect, radius, color, spread=8, dy=6, steps=6):
    """A soft drop shadow drawn with stacked translucent rounded rects (cheap
    stand-in for CSS box-shadow where a QGraphicsEffect would repaint too much)."""
    base = QColor(color)
    a0 = base.alphaF()
    p.setPen(Qt.NoPen)
    for i in range(steps, 0, -1):
        f = i / float(steps)
        c = QColor(base)
        c.setAlphaF(a0 * (1.0 - f) * 0.5)
        grow = spread * f
        p.setBrush(c)
        p.drawRoundedRect(rect.adjusted(-grow, -grow + dy, grow, grow + dy), radius + grow, radius + grow)


# ── small widgets ────────────────────────────────────────────────────────────
class StepDots(QWidget):
    """Five dots, the current one long and blue ("Step N of 5")."""

    def __init__(self, step, total=STEPS, parent=None):
        super().__init__(parent)
        self.step, self.total = int(step), int(total)
        self.setFixedSize(24 + 8 * (total - 1) + 6 * (total - 1), 8)
        self.setAccessibleName("Step %d of %d" % (self.step, self.total))
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        x = 0.0
        for i in range(1, self.total + 1):
            w = 24.0 if i == self.step else 8.0
            if i == self.step:
                p.setBrush(theme.qcolor("blue"))
            elif i < self.step:
                p.setBrush(QColor(37, 99, 235, 89))
            else:
                p.setBrush(theme.qcolor("line2"))
            p.drawRoundedRect(QRectF(x, 0, w, 8), 4, 4)
            x += w + 6
        p.end()


def paint_pill(p, rect, state, t=0.0, animate=False):
    """The dictation bar (light glass), as gusan_ds.pill draws it:
    recording (red dot, wave, ↵ esc), transcribing (spinner), pasted (green check)."""
    h = rect.height()
    r = h / 2.0
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QPen(PILL_HAIR, 1))
    p.setBrush(PILL_TINT)
    body = rect.adjusted(0.5, 0.5, -0.5, -0.5)
    p.drawRoundedRect(body, r - 0.5, r - 0.5)
    p.setPen(QPen(QColor(255, 255, 255, 230), 1))
    p.drawLine(QPointF(body.left() + r, body.top() + 1.0), QPointF(body.right() - r, body.top() + 1.0))
    cy = rect.center().y()
    x = rect.left() + 13.0
    p.setPen(Qt.NoPen)
    if state == "recording":
        halo = QColor(PILL_REC)
        halo.setAlpha(51)
        p.setBrush(halo)
        p.drawEllipse(QPointF(x + 5, cy), 9, 9)
        glow = QColor(PILL_REC)
        glow.setAlpha(60)
        p.setBrush(glow)
        p.drawEllipse(QPointF(x + 5, cy), 7, 7)
        p.setBrush(PILL_REC)
        p.drawEllipse(QPointF(x + 5, cy), 5, 5)
        x += 10 + 10
        for i, v in enumerate(PILL_WAVE):
            k = 1.0
            if animate:
                k = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(t * (5.0 + (i % 4)) + i * 0.9))
            hh = max(3.0, v * k)
            p.setBrush(ink(0.55 + 0.45 * v / 22.0))
            p.drawRoundedRect(QRectF(x, cy - hh / 2.0, 3, hh), 1.5, 1.5)
            x += 6
        f = theme.font(10, 600)
        p.setFont(f)
        fm = QFontMetricsF(f)
        right = rect.right() - 16.0
        for text, minw in (("esc", 25), ("↵", 19)):
            w = max(minw, fm.horizontalAdvance(text) + 8)
            box = QRectF(right - w, cy - 8, w, 16)
            p.setPen(QPen(ink(0.235), 1))
            p.setBrush(ink(0.055))
            p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
            p.setPen(ink(0.84))
            p.drawText(box, Qt.AlignCenter, text)
            right -= w + 6
    elif state == "transcribing":
        box = QRectF(x, cy - 8.5, 17, 17)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(ink(0.16), 2.0))
        p.drawEllipse(box.adjusted(1.6, 1.6, -1.6, -1.6))
        pen = QPen(ink(0.94), 2.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        start = int((90 - (t * 360.0 * 1.2) % 360) * 16)
        p.drawArc(box.adjusted(1.6, 1.6, -1.6, -1.6), start, -70 * 16)
        p.setFont(theme.font(13, 600))
        p.setPen(ink(0.94))
        p.drawText(QRectF(x + 27, rect.top(), rect.width(), h), Qt.AlignVCenter | Qt.AlignLeft, "Transcribing")
    elif state == "pasted":
        p.setBrush(PILL_DONE)
        p.drawEllipse(QRectF(x, cy - 9, 18, 18))
        p.drawPixmap(QRectF(x + 3, cy - 6, 12, 12).toRect(), icons.pixmap("check", 12, "#ffffff", sw=2.6))
        p.setFont(theme.font(13, 600))
        p.setPen(ink(0.94))
        p.drawText(QRectF(x + 28, rect.top(), rect.width(), h), Qt.AlignVCenter | Qt.AlignLeft, "Pasted to cursor")


def pill_width(state):
    if state == "recording":
        return 204
    if state == "transcribing":
        return int(13 + 17 + 10 + QFontMetricsF(theme.font(13, 600)).horizontalAdvance("Transcribing") + 17)
    if state == "pasted":
        return int(13 + 18 + 10 + QFontMetricsF(theme.font(13, 600)).horizontalAdvance("Pasted to cursor") + 17)
    return 0


_PILL_NAMES = {"recording": "Recording", "transcribing": "Transcribing", "pasted": "Pasted to cursor"}


class DemoPill(QWidget):
    """The dictation bar as a widget (states: idle (hidden), recording,
    transcribing, pasted). Animates only while it shows."""

    def __init__(self, state="recording", parent=None, animate=True):
        super().__init__(parent)
        self._state = None
        self._animate = bool(animate)
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self.update)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        theme.elevate(self, None)
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(14)
        eff.setOffset(0, 6)
        eff.setColor(QColor(15, 23, 42, 56))
        self.setGraphicsEffect(eff)
        self.set_state(state)

    def state(self):
        return self._state

    def set_animate(self, on):
        self._animate = bool(on)
        self._sync_timer()

    def set_state(self, state):
        state = state if state in ("recording", "transcribing", "pasted") else "idle"
        if state == self._state:
            return
        self._state = state
        if state != "idle":
            self.setFixedSize(pill_width(state), 40)
        self.setAccessibleName(_PILL_NAMES.get(state, ""))
        self._sync_timer()
        self.update()

    def _sync_timer(self):
        run = self._animate and self.isVisible() and self._state in ("recording", "transcribing")
        if run and not self._timer.isActive():
            self._timer.start()
        elif not run:
            self._timer.stop()

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_timer()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, _e):
        if self._state == "idle":
            return
        p = QPainter(self)
        paint_pill(p, QRectF(0, 0, self.width(), self.height()), self._state,
                   time.monotonic() - self._t0, self._animate)
        p.end()


class VoiceWave(QWidget):
    """The blue voice waveform after "Speak." (15 bars, g-wave)."""

    def __init__(self, parent=None, animate=True):
        super().__init__(parent)
        self._animate = bool(animate)
        self._t0 = time.monotonic()
        self.setFixedSize(15 * 6 + 14 * 4, 52)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self.update)

    def set_animate(self, on):
        self._animate = bool(on)
        if not on:
            self._timer.stop()
        elif self.isVisible():
            self._timer.start()
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        if self._animate:
            self._timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("blue-l"))
        t = time.monotonic() - self._t0
        cy = self.height() / 2.0
        for i, h in enumerate(WAVE_HEIGHTS):
            k = 1.0
            if self._animate:
                dur = 0.9 + (i % 5) * 0.12
                ph = ((t - 0.17 * ((i * 4) % 7)) / dur) % 1.0
                k = 0.35 + 0.65 * (0.5 - 0.5 * math.cos(2 * math.pi * ph))
            hh = max(4.0, h * k)
            p.drawRoundedRect(QRectF(i * 10.0, cy - hh / 2.0, 6, hh), 3, 3)
        p.end()


class MailDemo(QWidget):
    """The mock "New message" window where a sentence types itself."""

    W, H = 364, 325

    def __init__(self, parent=None, animate=True):
        super().__init__(parent)
        self._animate = bool(animate)
        self._t0 = time.monotonic()
        self.setFixedSize(self.W, self.H)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self.update)
        self._words = DEMO_SENTENCE.split(" ")
        self._caret_at = 0.3 + (len(self._words) - 1) * 0.14 + 0.42
        self._loop = self._caret_at + 4.2
        self._ctl = svg_pixmap(WINDOW_CTL, 11, "muted", sw=2)

    def set_animate(self, on):
        self._animate = bool(on)
        if not on:
            self._timer.stop()
        elif self.isVisible():
            self._timer.start()
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        self._t0 = time.monotonic()
        if self._animate:
            self._timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def _time(self):
        if not self._animate:
            return self._caret_at + 0.1                # the finished sentence, caret on
        return (time.monotonic() - self._t0) % self._loop

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        w, h = float(self.width()), float(self.height())
        outer = QPainterPath()
        outer.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 12, 12)
        p.fillPath(outer, QColor("#ffffff"))
        p.save()
        p.setClipPath(outer)
        line = theme.qcolor("line")
        p.fillRect(QRectF(0, 0, w, 36), theme.qcolor("bg"))
        p.fillRect(QRectF(0, 36, w, 1), line)
        p.setFont(theme.font(12.5, 600))
        p.setPen(theme.qcolor("text2"))
        p.drawText(QRectF(16, 0, 200, 36), Qt.AlignVCenter | Qt.AlignLeft, "New message")
        x = w - 14 - 13
        p.drawPixmap(QRectF(x, 11.5, 13, 13).toRect(), icons.pixmap("x", 13, "muted", sw=2))
        x -= 16 + 11
        p.drawPixmap(QRectF(x, 12.5, 11, 11).toRect(), self._ctl)
        x -= 16 + 13
        p.drawPixmap(QRectF(x, 11.5, 13, 13).toRect(), icons.pixmap("minus", 13, "muted", sw=2))
        # To / Subject
        y = 37.0
        p.setFont(theme.font(13))
        p.setPen(theme.qcolor("muted"))
        p.drawText(QRectF(16, y, 58, 38), Qt.AlignVCenter | Qt.AlignLeft, "To")
        chip = QRectF(74, y + 6, 0, 26)
        f600 = theme.font(13, 600)
        chip.setWidth(3 + 20 + 7 + QFontMetricsF(f600).horizontalAdvance("Anna") + 10)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("bg2"))
        p.drawRoundedRect(chip, 13, 13)
        grad = QLinearGradient(chip.left() + 3, chip.top() + 3, chip.left() + 23, chip.top() + 23)
        grad.setColorAt(0, ANNA_A)
        grad.setColorAt(1, ANNA_B)
        p.setBrush(QBrush(grad))
        av = QRectF(chip.left() + 3, chip.top() + 3, 20, 20)
        p.drawEllipse(av)
        p.setFont(theme.font(9, 700))
        p.setPen(QColor("#ffffff"))
        p.drawText(av, Qt.AlignCenter, "A")
        p.setFont(f600)
        p.setPen(theme.qcolor("text"))
        p.drawText(QRectF(av.right() + 7, chip.top(), 80, 26), Qt.AlignVCenter | Qt.AlignLeft, "Anna")
        p.fillRect(QRectF(0, y + 38, w, 1), line)
        y += 39
        p.setFont(theme.font(13))
        p.setPen(theme.qcolor("muted"))
        p.drawText(QRectF(16, y, 58, 38), Qt.AlignVCenter | Qt.AlignLeft, "Subject")
        p.setFont(f600)
        p.setPen(theme.qcolor("ink"))
        p.drawText(QRectF(74, y, w - 90, 38), Qt.AlignVCenter | Qt.AlignLeft, "Q4 roadmap review")
        p.fillRect(QRectF(0, y + 38, w, 1), line)
        y += 39
        # Body
        f = theme.font(14.5)
        fm = QFontMetricsF(f)
        p.setFont(f)
        lh = 23.0
        base = (lh - fm.height()) / 2.0 + fm.ascent()
        p.setPen(theme.qcolor("text"))
        p.drawText(QPointF(16, y + 16 + base), "Hi Anna,")
        t = self._time()
        top = y + 16 + lh + 10
        maxw = w - 32
        space = fm.horizontalAdvance(" ")
        cx, cy_ = 16.0, top
        end_x, end_y = cx, cy_
        for i, word in enumerate(self._words):
            ww = fm.horizontalAdvance(word)
            if cx > 16 and cx + ww > 16 + maxw:
                cx, cy_ = 16.0, cy_ + lh
            k = (t - (0.3 + i * 0.14)) / 0.42
            if k > 0:
                k = min(1.0, k)
                e = 1.0 - (1.0 - k) ** 2
                c = theme.qcolor("text")
                c.setAlphaF(e)
                p.setPen(c)
                p.drawText(QPointF(cx, cy_ + base + 8.0 * (1.0 - e)), word)
                end_x, end_y = cx + ww, cy_
            cx += ww + space
        if t >= self._caret_at and ((t - self._caret_at) % 1.05) < 0.525:
            p.fillRect(QRectF(end_x + 2, end_y + (lh - 19) / 2.0 + 2, 2, 19), theme.qcolor("blue"))
        p.restore()
        p.setPen(QPen(line, 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(outer)
        p.end()


class HelloScreen(QWidget):
    """The right half of Hello: a little desktop with the mail window, the
    dictation bar and a taskbar (decorative; screen readers get one name)."""

    W, H = 418, 480

    def __init__(self, parent=None, animate=True):
        super().__init__(parent)
        self.setFixedSize(self.W, self.H)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("%s typing an email while you speak" % APP_NAME)
        self.mail = MailDemo(self, animate=animate)
        self.mail.move(27, 27)
        theme.elevate(self.mail, "lg")
        self.pill = DemoPill("recording", self, animate=animate)
        self.pill.move((self.W - self.pill.width()) // 2, self.H - 1 - 56 - 40)
        self._clock = _now().strftime("%H:%M")

    def set_animate(self, on):
        self.mail.set_animate(on)
        self.pill.set_animate(on)

    def set_clock(self, text):
        self._clock = text
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        self._clock = _now().strftime("%H:%M")

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = float(self.width()), float(self.height())
        frame = QRectF(0.5, 0.5, w - 1, h - 1)
        path = QPainterPath()
        path.addRoundedRect(frame, 20, 20)
        p.fillPath(path, QColor(255, 255, 255, 107))
        p.save()
        p.setClipPath(path)
        bar = QRectF(0, h - 1 - 38, w, 38)
        p.fillRect(bar, QColor(255, 255, 255, 184))
        p.fillRect(QRectF(0, bar.top(), w, 1), theme.qcolor("glass-line"))
        cy = bar.center().y()
        x = (w - (15.5 + 16 + 18 + 16 + 18 + 16 + 18)) / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("blue-l"))
        for dx, dy in ((0, 0), (8.5, 0), (0, 8.5), (8.5, 8.5)):
            p.drawRoundedRect(QRectF(x + dx, cy - 7.75 + dy, 7, 7), 1.5, 1.5)
        x += 15.5 + 16
        p.setBrush(TASK_APP_A)
        p.drawRoundedRect(QRectF(x, cy - 9, 18, 18), 5, 5)
        x += 18 + 16
        p.drawPixmap(QRectF(x, cy - 11, 18, 18).toRect(), icons.pixmap("mail", 18, "blue"))
        p.setBrush(theme.qcolor("blue"))
        p.drawRoundedRect(QRectF(x + 5, cy + 10, 8, 2), 1, 1)
        x += 18 + 16
        p.setBrush(TASK_APP_B)
        p.drawRoundedRect(QRectF(x, cy - 9, 18, 18), 5, 5)
        f = theme.font(11, 600)
        tw = QFontMetricsF(f).horizontalAdvance(self._clock)
        rx = w - 14 - tw
        p.setFont(f)
        p.setPen(theme.qcolor("text2"))
        p.drawText(QRectF(rx, bar.top(), tw + 2, 38), Qt.AlignVCenter | Qt.AlignLeft, self._clock)
        p.drawPixmap(QRectF(rx - 8 - 14, cy - 7, 14, 14).toRect(), icons.logo_pixmap(14))
        p.restore()
        p.setPen(QPen(theme.qcolor("glass-line"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()


class Meter(QWidget):
    """"Speed" + five little bars (n of 5)."""

    def __init__(self, text, level, parent=None):
        super().__init__(parent)
        self._level = int(level)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lb = text_label(text, 12.5, 400, "muted", parent=self)
        lb.setFixedWidth(62)
        lay.addWidget(lb)
        self.bars = _Painted(self._paint_bars, self)
        self.bars.setFixedSize(5 * 16 + 4 * 3, 6)
        lay.addWidget(self.bars, 0, Qt.AlignVCenter)
        lay.addStretch(1)
        self.setAccessibleName("%s: %d of 5" % (text, self._level))
        self.setFixedHeight(20)                  # the cards' 20 px line height

    def level(self):
        return self._level

    def _paint_bars(self, w):
        p = QPainter(w)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        for i in range(5):
            p.setBrush(theme.qcolor("blue-l" if i < self._level else "line"))
            p.drawRoundedRect(QRectF(i * 19.0, 0, 16, 6), 3, 3)
        p.end()


def _paint_pick_dot(p, center, selected, color="blue", r=11.0, check=13):
    if selected:
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor(color))
        p.drawEllipse(center, r, r)
        p.drawPixmap(QRectF(center.x() - check / 2.0, center.y() - check / 2.0, check, check).toRect(),
                     icons.pixmap("check", check, "#ffffff", sw=2.8 if check > 11 else 3))
    else:
        p.setPen(QPen(theme.qcolor("line2"), 2))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(center, r - 1, r - 1)


class _Choice(QAbstractButton):
    """A checkable card-like option (Tab, Space / Enter, screen readers)."""

    INSET = (6, 6, 6, 6)        # left, top, right, bottom: room for the ring and its glow
    RADIUS = 16
    RING = "blue"
    GLOW = QColor(59, 130, 246, 36)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self._kbd = False

    def body(self):
        l, t, r, b = self.INSET
        return QRectF(l, t, self.width() - l - r, self.height() - t - b)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e):
        self._kbd = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._kbd = False
        super().focusOutEvent(e)

    def paint_frame(self, p, fill, edge, edge_w=1.0):
        body = self.body()
        rad = self.RADIUS
        if self.isChecked():
            # CSS: a 2 px ring 1 px outside the edge, then a 4 px glow (box-shadow 0 0 0 4px).
            p.setPen(QPen(self.GLOW, 4))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(body.adjusted(-3, -3, 3, 3), rad + 3, rad + 3)
        p.setPen(QPen(edge, edge_w))
        p.setBrush(fill)
        p.drawRoundedRect(body.adjusted(edge_w / 2, edge_w / 2, -edge_w / 2, -edge_w / 2), rad, rad)
        if self.isChecked():
            p.setPen(QPen(theme.qcolor(self.RING), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(body, rad, rad)
        elif self.hasFocus() and self._kbd:
            p.setPen(QPen(theme.qcolor("blue-l"), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(body, rad, rad)


class _Ribbon(QWidget):
    """"✓ Best for your computer" on top of a model card."""

    TEXT = "Best for your computer"

    def __init__(self, parent=None):
        super().__init__(parent)
        f = theme.font(12, 700)
        self._font = f
        self.setFixedSize(int(10 + 13 + 6 + QFontMetricsF(f).horizontalAdvance(self.TEXT) + 12 + 2), 26)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setPen(QPen(theme.qcolor("blue-h"), 1))
        p.setBrush(theme.qcolor("blue"))
        p.drawRoundedRect(r, 13, 13)
        p.drawPixmap(QRectF(10, 6.5, 13, 13).toRect(), icons.pixmap("check", 13, "#ffffff", sw=2.2))
        p.setFont(self._font)
        p.setPen(QColor("#ffffff"))
        p.drawText(QRectF(29, 0, self.width() - 29, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.TEXT)
        p.end()


class ModelCard(_Choice):
    """Fast / Balanced / Most accurate: plain name, real model and size, an
    optional note, Speed and Accuracy meters, the round pick mark."""

    INSET = (6, 14, 6, 6)

    def __init__(self, model_id, parent=None):
        super().__init__(parent)
        self.model_id = model_id
        info = hardware.MODEL_INFO[model_id]
        self.info = info
        l, t, r, b = self.INSET
        lay = QVBoxLayout(self)
        lay.setContentsMargins(l + 22, t + 26, r + 22, b + 24)
        lay.setSpacing(0)
        self.name_label = text_label(info.plain, 22, 700, "ink", display=True, parent=self)
        self.name_label.setFixedHeight(28)
        lay.addWidget(self.name_label)
        lay.addSpacing(3)
        self.model_label = text_label("%s · %s" % (info.real, info.size), 12.5, 400, "muted", parent=self)
        self.model_label.setFont(theme.font(12.5, 400, tabular=True))
        self.model_label.setFixedHeight(16)
        lay.addWidget(self.model_label)
        self.note = QWidget(self)
        nl = QHBoxLayout(self.note)
        nl.setContentsMargins(0, 6, 0, 0)
        nl.setSpacing(5)
        ic = QLabel(self.note)
        ic.setPixmap(icons.pixmap("clock", 14, "muted"))
        nl.addWidget(ic)
        self.note_label = text_label("A little slower here", 12.5, 400, "text2", parent=self.note)
        nl.addWidget(self.note_label)
        nl.addStretch(1)
        self.note.hide()
        lay.addWidget(self.note)
        lay.addStretch(1)
        lay.addSpacing(22)
        self.speed = Meter("Speed", info.speed, self)
        self.accuracy = Meter("Accuracy", info.accuracy, self)
        lay.addWidget(self.speed)
        lay.addSpacing(10)
        lay.addWidget(self.accuracy)
        for wdg in self.findChildren(QWidget):
            wdg.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.ribbon = _Ribbon(self)
        self.ribbon.move(l + 18, t - 13)
        self.ribbon.hide()
        self.setMinimumHeight(192 + t + b)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        theme.elevate(self, "card")
        self._sync_name()

    def set_best(self, on):
        self.ribbon.setVisible(bool(on))
        self._sync_name()

    def is_best(self):
        return not self.ribbon.isHidden()

    def set_note(self, text):
        self.note_label.setText(text or "")
        self.note.setVisible(bool(text))
        self._sync_name()

    def note_text(self):
        return self.note_label.text() if not self.note.isHidden() else ""

    def _sync_name(self):
        parts = [self.info.plain, self.info.real, self.info.size]
        if self.is_best():
            parts.append(_Ribbon.TEXT)
        if self.note_text():
            parts.append(self.note_text())
        self.setAccessibleName(", ".join(parts))
        self.setAccessibleDescription("Speed %d of 5, accuracy %d of 5" % (self.info.speed, self.info.accuracy))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        self.paint_frame(p, QColor("#ffffff"), theme.qcolor("line"))
        body = self.body()
        _paint_pick_dot(p, QPointF(body.right() - 20 - 11, body.top() + 22 + 11), self.isChecked())
        p.end()


class CloudCard(_Choice):
    """Gusan Cloud (PRO): the cloud model's real name, "Fastest. Nothing to download." """

    RING = "pro"
    GLOW = QColor(168, 85, 247, 41)

    def __init__(self, parent=None, side=0):
        """side: extra empty room left and right (it lines up with the model
        cards, whose row is wider for their shadows)."""
        super().__init__(parent)
        l, t, r, b = _Choice.INSET
        self.INSET = (l + side, t, r + side, b)
        l, t, r, b = self.INSET
        self.setFixedHeight(60 + t + b)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(l + 20, t, r + 60, b)
        lay.setSpacing(14)
        ic = QLabel(self)
        ic.setPixmap(icons.pixmap("cloud", 26, "pro-l"))
        lay.addWidget(ic, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        col.addStretch(1)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.name_label = text_label("Gusan Cloud", 15, 700, "ink", parent=self)
        self.name_label.setFixedHeight(20)
        top.addWidget(self.name_label)
        self.pro_tag = ProTag("s", parent=self)
        top.addWidget(self.pro_tag, 0, Qt.AlignVCenter)
        top.addStretch(1)
        col.addLayout(top)
        self.model_label = text_label(hardware.CLOUD_SPEECH_MODEL, 12.5, 400, "muted", parent=self)
        self.model_label.setFixedHeight(16)
        col.addWidget(self.model_label)
        col.addStretch(1)
        lay.addLayout(col)
        lay.addStretch(1)
        self.tagline = text_label("Fastest. Nothing to download.", 13, 400, "text2", parent=self)
        lay.addWidget(self.tagline, 0, Qt.AlignVCenter)
        for wdg in self.findChildren(QWidget):
            wdg.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("Gusan Cloud, %s, Pro feature, fastest, nothing to download"
                               % hardware.CLOUD_SPEECH_MODEL)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        self.paint_frame(p, theme.qcolor("pro-sub"), QColor(168, 85, 247, 71))
        body = self.body()
        _paint_pick_dot(p, QPointF(body.right() - 20 - 11, body.top() + 19 + 11), self.isChecked(), "pro")
        p.end()


class SizeOption(_Choice):
    """One of "More sizes": a small radio pill with the plain and real names."""

    INSET = (4, 4, 4, 4)
    RADIUS = 10

    def __init__(self, model_id, parent=None):
        super().__init__(parent)
        self.model_id = model_id
        info = hardware.MODEL_INFO[model_id]
        self.info = info
        l, t, r, b = self.INSET
        self.setFixedHeight(34 + t + b)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(l + 11 + 16 + 8, t, r + 12, b)
        lay.setSpacing(8)
        self.name_label = text_label(info.plain, 13, 700, "ink", parent=self)
        lay.addWidget(self.name_label, 0, Qt.AlignVCenter)
        self.model_label = text_label("%s · %s" % (info.real, info.size), 12, 400, "muted", parent=self)
        self.model_label.setFont(theme.font(12, 400, tabular=True))
        lay.addWidget(self.model_label, 0, Qt.AlignVCenter)
        self.best = Chip("Best for your computer", "blue", "check", self)
        self.best.hide()
        lay.addWidget(self.best, 0, Qt.AlignVCenter)
        for wdg in self.findChildren(QWidget):
            wdg.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._sync_name()

    def set_best(self, on):
        self.best.setVisible(bool(on))
        self._sync_name()

    def _sync_name(self):
        parts = [self.info.plain, self.info.real, self.info.size]
        if not self.best.isHidden():
            parts.append("Best for your computer")
        self.setAccessibleName(", ".join(parts))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        self.paint_frame(p, QColor("#ffffff"), theme.qcolor("line"))
        body = self.body()
        c = QPointF(body.left() + 11 + 8, body.center().y())
        if self.isChecked():
            p.setPen(Qt.NoPen)
            p.setBrush(theme.qcolor("blue"))
            p.drawEllipse(c, 8, 8)
            p.drawPixmap(QRectF(c.x() - 5, c.y() - 5, 10, 10).toRect(), icons.pixmap("check", 10, "#ffffff", sw=3))
        else:
            p.setPen(QPen(theme.qcolor("line2"), 2))
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(c, 7, 7)
        p.end()


class DownloadChip(QFrame):
    """The chosen model, still downloading behind steps 3-4:
    ring + "Balanced" + "Whisper Small" + "62%" / ✓ "ready" / failed + Try again."""

    retry = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(32)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._pct = 0
        self._state = "hidden"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 12, 0)
        lay.setSpacing(6)
        self.icon = _Painted(self._paint_icon, self)
        self.icon.setFixedSize(22, 22)
        lay.addWidget(self.icon, 0, Qt.AlignVCenter)
        self.plain = text_label("", 12.5, 600, "text2", parent=self)
        lay.addWidget(self.plain, 0, Qt.AlignVCenter)
        self.real = text_label("", 12.5, 400, "muted", parent=self)
        lay.addWidget(self.real, 0, Qt.AlignVCenter)
        self.status = text_label("", 12.5, 600, "text2", parent=self)
        self.status.setFont(theme.font(12.5, 600, tabular=True))
        lay.addWidget(self.status, 0, Qt.AlignVCenter)
        self.retry_btn = link_button("Try again", 12.5, parent=self)
        self.retry_btn.clicked.connect(self.retry.emit)
        self.retry_btn.hide()
        lay.addWidget(self.retry_btn, 0, Qt.AlignVCenter)
        self.hide()

    def state(self):
        return self._state

    def show_model(self, model_id, state, pct=0):
        """state: downloading | ready | failed | hidden."""
        info = hardware.MODEL_INFO.get(model_id)
        if info is None or state == "hidden":
            self._state = "hidden"
            self.hide()
            return
        self._state = state
        self._pct = max(0, min(100, int(pct or 0)))
        self.plain.setText(info.plain)
        self.real.setText(info.real)
        self.retry_btn.setVisible(state == "failed")
        if state == "ready":
            self.status.setText("ready")
            tint(self.status, "ok-tx")
            spoken = "%s, %s, ready" % (info.plain, info.real)
        elif state == "failed":
            self.status.setText("")
            spoken = "%s, %s, download failed" % (info.plain, info.real)
        else:
            self.status.setText("%d%%" % self._pct)
            tint(self.status, "text2")
            spoken = "%s, %s, downloading, %d%%" % (info.plain, info.real, self._pct)
        self.status.setVisible(state != "failed")
        self.setAccessibleName(spoken)
        self.show()
        self.updateGeometry()
        self.icon.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(theme.qcolor("glass-line"), 1))
        p.setBrush(QColor(255, 255, 255, 153))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 16, 16)
        p.end()

    def _paint_icon(self, w):
        p = QPainter(w)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._state == "ready":
            p.drawPixmap(0, 0, icons.pixmap("check_circle", 22, "ok", sw=2))
        elif self._state == "failed":
            p.drawPixmap(0, 0, icons.pixmap("alert", 22, "red-tx", sw=2))
        else:
            box = QRectF(1.5, 1.5, 19, 19)
            p.setPen(QPen(theme.qcolor("line"), 3))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(box)
            if self._pct > 0:
                pen = QPen(theme.qcolor("blue-l"), 3)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawArc(box, 90 * 16, -int(self._pct / 100.0 * 360 * 16))
        p.end()


class MicVisual(QWidget):
    """The mic check's level ring: 44 radial bars around a blue mic button,
    longer and livelier the louder the sound."""

    SIZE = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("Microphone level")
        self._level = 0.0
        self._t = 0.0
        self._static = False
        self._mic = icons.pixmap("mic", 58, "#ffffff", sw=1.6)

    def level(self):
        return self._level

    def set_level(self, value, static=False):
        self._level = max(0.0, min(1.0, float(value or 0.0)))
        self._static = bool(static)
        self.update()

    def advance(self, dt):
        self._t += dt
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = self.SIZE / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(59, 130, 246, 15))
        p.drawEllipse(QPointF(c, c), c, c)
        blue = theme.qcolor("blue-l")
        n = 44
        lv = self._level
        for i in range(n):
            base = RING_HEIGHTS[(i * 5) % len(RING_HEIGHTS)]
            if self._static:
                k = lv
            else:
                wob = 0.5 + 0.5 * math.sin(self._t * (5.0 + (i % 5) * 0.9) + i * 1.7)
                k = 0.18 + 0.82 * lv * (0.55 + 0.45 * wob)
            h = max(3.0, base * k)
            col = QColor(blue)
            col.setAlphaF(0.4 + 0.6 * base / 42.0)
            p.save()
            p.translate(c, c)
            p.rotate(i * 360.0 / n)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(-2, -92 - h, 4, h), 2, 2)
            p.restore()
        # The button: white ring, soft blue shadow, blue disc, white mic.
        g = QRadialGradient(QPointF(c, c + 16), 74 + 36)
        g.setColorAt(0.0, QColor(37, 99, 235, 82))
        g.setColorAt(0.62, QColor(37, 99, 235, 40))
        g.setColorAt(1.0, QColor(37, 99, 235, 0))
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(c, c + 16), 74 + 36, 74 + 36)
        p.setBrush(QColor(255, 255, 255, 179))
        p.drawEllipse(QPointF(c, c), 82, 82)
        p.setBrush(theme.qcolor("blue"))
        p.drawEllipse(QPointF(c, c), 74, 74)
        p.drawPixmap(QRectF(c - 29, c - 29, 58, 58).toRect(), self._mic)
        p.end()


class StatusBadge(QWidget):
    """The round badge before "We hear you" (green check) or an error (red !)."""

    def __init__(self, parent=None, size=34):
        super().__init__(parent)
        self._d = size
        self._kind = "ok"
        self.setFixedSize(size + 10, size + 10)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_kind(self, kind):
        self._kind = kind
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        r = self._d / 2.0
        col = theme.qcolor("ok" if self._kind == "ok" else "red")
        halo = QColor(col)
        halo.setAlpha(36)
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, r + 5, r + 5)
        p.setBrush(col)
        p.drawEllipse(c, r, r)
        if self._kind == "ok":
            s = 19
            p.drawPixmap(QRectF(c.x() - s / 2.0, c.y() - s / 2.0, s, s).toRect(),
                         icons.pixmap("check", s, "#ffffff", sw=2.6))
        else:
            p.setPen(QColor("#ffffff"))
            p.setFont(theme.font(18, 800))
            p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignCenter, "!")
        p.end()


class LitKeys(QWidget):
    """The shortcut as big lit (pressed) keys: Alt + R on Try it."""

    KEY = 64
    MARGIN = 8                  # room for the glow and the shadow

    def __init__(self, hotkey="alt+r", parent=None):
        super().__init__(parent)
        self._keys = []
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.set_hotkey(hotkey)

    def keys(self):
        return list(self._keys)

    def set_hotkey(self, hotkey):
        self._keys = keycap_labels(hotkey) or ["Alt", "R"]
        f = theme.font(24, 700, display=True)
        fm = QFontMetricsF(f)
        fp = QFontMetricsF(theme.font(20, 700))
        self._widths = [max(float(self.KEY), fm.horizontalAdvance(k) + 36) for k in self._keys]
        plus = fp.horizontalAdvance("+")
        total = sum(self._widths) + (len(self._keys) - 1) * (plus + 20)
        self._plus = plus
        self.setFixedSize(int(total + 16 + 2), self.KEY + 18)
        self.setAccessibleName(" + ".join(self._keys))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        x = 8.0
        f = theme.font(24, 700, display=True)
        for i, k in enumerate(self._keys):
            w = self._widths[i]
            r = QRectF(x, 8 + 2, w, self.KEY)
            # box-shadow: 0 0 0 5px rgba(59,130,246,0.14), 0 8px 22px rgba(37,99,235,0.20)
            _shadow_rects(p, r, 14, QColor(37, 99, 235, 30), spread=8, dy=6, steps=4)
            p.setPen(QPen(QColor(59, 130, 246, 36), 5))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(-2.5, -2.5, 2.5, 2.5), 16.5, 16.5)
            p.setPen(QPen(LIT_EDGE, 1))
            p.setBrush(LIT_FILL)
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
            p.setPen(QPen(LIT_EDGE, 1))
            p.drawLine(QPointF(r.left() + 12, r.bottom() - 1.0), QPointF(r.right() - 12, r.bottom() - 1.0))
            p.setFont(f)
            p.setPen(theme.qcolor("blue-tx"))
            p.drawText(r, Qt.AlignCenter, k)
            x += w
            if i < len(self._keys) - 1:
                p.setFont(theme.font(20, 700))
                p.setPen(theme.qcolor("blue-l"))
                p.drawText(QRectF(x + 10, 8 + 2, self._plus, self.KEY), Qt.AlignCenter, "+")
                x += self._plus + 20
        p.end()


def _corner_path(r, tl, tr, br, bl):
    """A rounded rect with its own (rx, ry) per corner (CSS's inner border
    radius when one side's border is thicker)."""
    path = QPainterPath()
    path.moveTo(r.left() + tl[0], r.top())
    path.lineTo(r.right() - tr[0], r.top())
    path.arcTo(QRectF(r.right() - 2 * tr[0], r.top(), 2 * tr[0], 2 * tr[1]), 90, -90)
    path.lineTo(r.right(), r.bottom() - br[1])
    path.arcTo(QRectF(r.right() - 2 * br[0], r.bottom() - 2 * br[1], 2 * br[0], 2 * br[1]), 0, -90)
    path.lineTo(r.left() + bl[0], r.bottom())
    path.arcTo(QRectF(r.left(), r.bottom() - 2 * bl[1], 2 * bl[0], 2 * bl[1]), 270, -90)
    path.lineTo(r.left(), r.top() + tl[1])
    path.arcTo(QRectF(r.left(), r.top(), 2 * tl[0], 2 * tl[1]), 180, -90)
    path.closeSubpath()
    return path


class BigKeys(QWidget):
    """Hello's shortcut as big white keys (the mockup's kbd: 56 px, 1 px slate
    edge with a 4 px bottom, radius 12, soft shadow; a small "+" between).
    Painted so the thick bottom edge keeps smooth corners. The widget keeps
    ``MARGIN`` free above, below and on the right for the shadow; the left
    edge is the first key's edge, so it lines up with the headline."""

    KEY = 56
    MARGIN = 12
    GAP = 8

    def __init__(self, hotkey="alt+r", parent=None):
        super().__init__(parent)
        self._keys = []
        self._widths = []
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.set_hotkey(hotkey)

    def keys(self):
        return list(self._keys)

    def set_hotkey(self, hotkey):
        self._keys = keycap_labels(hotkey) or ["Alt", "R"]
        fm = QFontMetricsF(theme.font(22, 700, display=True))
        self._widths = [max(float(self.KEY), math.ceil(fm.horizontalAdvance(k)) + 32 + 2) for k in self._keys]
        self._plus = QFontMetricsF(theme.font(14, 700)).horizontalAdvance("+")
        total = sum(self._widths) + (len(self._keys) - 1) * (self._plus + 2 * self.GAP)
        self.setFixedSize(int(math.ceil(total)) + self.MARGIN, self.KEY + 2 * self.MARGIN)
        self.setAccessibleName(" + ".join(self._keys))
        self.setToolTip(" + ".join(self._keys))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        edge = theme.qcolor("line2")
        x, y = 0.0, float(self.MARGIN)
        f = theme.font(22, 700, display=True)
        for i, k in enumerate(self._keys):
            w = self._widths[i]
            r = QRectF(x, y, w, self.KEY)
            # box-shadow: 0 1px 2px rgba(15,23,42,.04), 0 8px 24px rgba(15,23,42,.06)
            _shadow_rects(p, r, 12, QColor(15, 23, 42, 16), spread=10, dy=6, steps=6)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(15, 23, 42, 10))
            p.drawRoundedRect(r.translated(0, 1), 12, 12)
            outer = QPainterPath()
            outer.addRoundedRect(r, 12, 12)
            p.fillPath(outer, edge)
            inner = _corner_path(r.adjusted(1, 1, -1, -4), (11, 11), (11, 11), (11, 8), (11, 8))
            p.fillPath(inner, QColor("#ffffff"))
            p.setFont(f)
            p.setPen(theme.qcolor("ink"))
            p.drawText(r.adjusted(1, 1, -1, -4), Qt.AlignCenter, k)
            x += w
            if i < len(self._keys) - 1:
                p.setFont(theme.font(14, 700))
                p.setPen(theme.qcolor("faint"))
                p.drawText(QRectF(x + self.GAP, y, self._plus, self.KEY), Qt.AlignCenter, "+")
                x += self._plus + 2 * self.GAP
        p.end()


class PracticeEdit(QTextEdit):
    """The practice box's text: what is pasted is tinted blue (the mockup's
    highlight) and announced with ``pasted(text)``. Typing stays plain; a
    paste with an HTML flavour (a Smart Action: real lists and checkboxes)
    goes in as HTML, everything else as plain text."""

    pasted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("QTextEdit { background: transparent; border: none; padding: 0px; color: %s; }"
                           % theme.qss_color("ink"))
        self.setFont(theme.font(20))
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAccessibleName("Practice box")
        self.document().setDocumentMargin(0)

    def insertFromMimeData(self, source):
        if source is None:
            return
        text = source.text() if source.hasText() else ""
        html = source.html() if source.hasHtml() else ""
        if not text and not html.strip():
            return
        cur = self.textCursor()
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(59, 130, 246, 31))
        if html.strip():
            start = cur.selectionStart()
            cur.insertHtml(html)
            tint = QTextCursor(self.document())
            tint.setPosition(start)
            tint.setPosition(cur.position(), QTextCursor.KeepAnchor)
            tint.mergeCharFormat(fmt)
            if not text:
                text = tint.selectedText().replace(" ", "\n").replace(" ", "\n")
        else:
            cur.insertText(text, fmt)
        self.setTextCursor(cur)
        self.setCurrentCharFormat(QTextCharFormat())
        self.pasted.emit(text)

    def paste_text(self, text, html=""):
        """Insert ``text`` (and an HTML flavour) as if pasted (tests, previews)."""
        from PySide6.QtCore import QMimeData
        md = QMimeData()
        md.setText(text)
        if html:
            md.setHtml(html)
        self.insertFromMimeData(md)


class PracticeBox(QFrame):
    """The white box on Try it (blue edge and glow while it's the target)."""

    G = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        g = self.G
        self.setMinimumHeight(176 + 2 * g)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(176 + 2 * g)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24 + g, 22 + g, 24 + g, 14 + g)
        self.edit = PracticeEdit(self)
        self.edit.installEventFilter(self)
        lay.addWidget(self.edit)
        self._active = True
        theme.elevate(self, "card")

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self.update()
        return False

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        g = self.G
        box = QRectF(g, g, self.width() - 2 * g, self.height() - 2 * g)
        p.setPen(QPen(QColor(59, 130, 246, 31), 2 * g))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(box, 14, 14)
        p.setPen(QPen(theme.qcolor("blue-l"), 1.5))
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(box.adjusted(0.75, 0.75, -0.75, -0.75), 14, 14)
        p.end()


class Envelope(QWidget):
    """5c's picture: a letter from Gusan in an envelope."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(300, 250)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("An email from %s in an envelope" % APP_NAME)
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(28)
        eff.setOffset(0, 18)
        eff.setColor(QColor(15, 23, 42, 26))
        self.setGraphicsEffect(eff)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        line2 = theme.qcolor("line2")
        flap = QPainterPath()
        flap.moveTo(10, 106)
        flap.lineTo(150, 30)
        flap.lineTo(290, 106)
        flap.closeSubpath()
        pen = QPen(ENV_FLAP, 6)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(ENV_FLAP)
        p.drawPath(flap)
        p.setPen(QPen(line2, 1))
        p.setBrush(ENV_BACK)
        p.drawRoundedRect(QRectF(1, 100, 298, 149), 18, 18)
        # The letter
        letter = QRectF(40, 6, 220, 186)
        _shadow_rects(p, letter, 12, QColor(15, 23, 42, 20), spread=8, dy=6, steps=4)
        p.setPen(QPen(theme.qcolor("line"), 1))
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(letter.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
        f = theme.font(14, 700, display=True)
        tw = QFontMetricsF(f).horizontalAdvance(APP_NAME)
        x = letter.center().x() - (22 + 7 + tw) / 2.0
        p.drawPixmap(QRectF(x, 26, 22, 22).toRect(), icons.logo_pixmap(22))
        p.setFont(f)
        p.setPen(theme.qcolor("ink"))
        p.drawText(QRectF(x + 29, 26, tw + 4, 22), Qt.AlignVCenter | Qt.AlignLeft, APP_NAME)
        p.setFont(theme.font(13, 700))
        p.drawText(QRectF(letter.left(), 60, letter.width(), 18), Qt.AlignCenter, "Confirm your email")
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("line"))
        inner = letter.width() - 40
        for y, frac in ((88, 0.78), (105, 0.56)):
            bw = inner * frac
            p.drawRoundedRect(QRectF(letter.center().x() - bw / 2.0, y, bw, 7), 3.5, 3.5)
        bf = theme.font(12, 700)
        bw = QFontMetricsF(bf).horizontalAdvance("Confirm") + 28
        btn = QRectF(letter.center().x() - bw / 2.0, 126, bw, 26)
        p.setBrush(theme.qcolor("blue"))
        p.drawRoundedRect(btn, 7, 7)
        p.setFont(bf)
        p.setPen(QColor("#ffffff"))
        p.drawText(btn, Qt.AlignCenter, "Confirm")
        # The front pocket
        front = QPainterPath()
        front.moveTo(1, 120)
        front.lineTo(150, 198)
        front.lineTo(299, 120)
        front.lineTo(299, 231)
        front.quadTo(299, 249, 281, 249)
        front.lineTo(19, 249)
        front.quadTo(1, 249, 1, 231)
        front.closeSubpath()
        pen = QPen(line2, 1)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(ENV_FRONT)
        p.drawPath(front)
        p.end()


class _ShortcutLink(HotkeyCaptureButton):
    """"Change shortcut": the capture button without its own keycaps (the big
    lit keys above already show the shortcut)."""

    def _show_keys(self):
        super()._show_keys()
        self.keys.hide()
        self.empty_label.hide()


# ── steps ────────────────────────────────────────────────────────────────────
class _Step(QWidget):
    """One 960 x 640 board: top bar (brand, extras, Skip), content, bottom bar
    (Back, the step dots, the main button)."""

    name = ""

    def __init__(self, wizard, brand=True, skip=True, back=True):
        super().__init__()
        self.wizard = wizard
        self.app = wizard.app
        self.setFixedSize(W, H)
        self.top = QWidget(self)
        self.top.setGeometry(0, 0, W, TOP)
        tl = QHBoxLayout(self.top)
        tl.setContentsMargins(PAD, 0, PAD, 0)
        tl.setSpacing(20)
        self.brand = None
        if brand:
            self.brand = brand_row(self.top)
            tl.addWidget(self.brand, 0, Qt.AlignVCenter)
        tl.addStretch(1)
        self.top_right = QHBoxLayout()
        self.top_right.setSpacing(20)
        tl.addLayout(self.top_right)
        self.skip_btn = None
        if skip:
            self.skip_btn = _plain_link("Skip", parent=self.top)
            self.skip_btn.setAccessibleName("Skip setup")
            self.skip_btn.clicked.connect(wizard.skip)
            tl.addWidget(self.skip_btn, 0, Qt.AlignVCenter)
        self.body = QWidget(self)
        self.body.setGeometry(PAD, TOP, W - 2 * PAD, H - TOP - BOT)
        self.bottom = QWidget(self)
        self.bottom.setGeometry(0, H - BOT, W, BOT)
        self.dots = StepDots(STEP_OF.get(self.name, 1), parent=self.bottom)
        self.dots.move((W - self.dots.width()) // 2, (BOT - self.dots.height()) // 2)
        bl = QHBoxLayout(self.bottom)
        bl.setContentsMargins(PAD, 0, PAD, 0)
        bl.setSpacing(12)
        self.back_btn = None
        if back:
            self.back_btn = _back_button(self.bottom)
            self.back_btn.clicked.connect(wizard.back)
            bl.addWidget(self.back_btn, 0, Qt.AlignVCenter)
        bl.addStretch(1)
        self.bottom_right = QHBoxLayout()
        self.bottom_right.setSpacing(12)
        bl.addLayout(self.bottom_right)

    def enter(self):
        pass

    def leave(self):
        pass

    def focus_target(self):
        """The control keyboard focus moves to when this step opens."""
        return None


class HelloStep(_Step):
    name = "hello"

    def __init__(self, wizard):
        super().__init__(wizard, brand=False, skip=True, back=False)
        app = self.app
        self.body.hide()
        self.hero = QWidget(self)
        self.hero.setGeometry(64, 40, 410, H - BOT - 40)
        v = QVBoxLayout(self.hero)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addStretch(1)
        logo = QLabel(self.hero)
        logo.setPixmap(icons.logo_pixmap(56))
        logo.setFixedSize(56, 56)
        logo.setAccessibleName(APP_NAME)
        v.addWidget(logo)
        v.addSpacing(30)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(22)
        self.speak = title_label("Speak.", 56, 62, self.hero)
        row.addWidget(self.speak)
        self.wave = VoiceWave(self.hero)
        row.addWidget(self.wave, 0, Qt.AlignVCenter)
        row.addStretch(1)
        v.addLayout(row)
        self.types = title_label("%s types." % APP_NAME, 56, 62, self.hero)
        v.addWidget(self.types)
        # BigKeys keeps MARGIN around the keys for their shadow: take it off
        # the spacing so the row sits where the mockup has it.
        m = BigKeys.MARGIN
        v.addSpacing(34 - m)
        keys = QHBoxLayout()
        keys.setSpacing(18 - m)
        self.keys = BigKeys(_cfg(app).get("hotkey") or "alt+r", parent=self.hero)
        keys.addWidget(self.keys, 0, Qt.AlignVCenter)
        keys.addWidget(text_label("in any app", 15, 400, "muted", parent=self.hero), 0, Qt.AlignVCenter)
        keys.addStretch(1)
        v.addLayout(keys)
        v.addSpacing(40 - m)
        cta = QHBoxLayout()
        cta.setSpacing(20)
        self.start_btn = Button("Get started", "primary", "l", parent=self.hero)
        self.start_btn.setStyleSheet("QPushButton { padding: 0px 28px; }")
        self.start_btn.setAutoDefault(False)
        self.start_btn.clicked.connect(lambda: wizard.go("model"))
        cta.addWidget(self.start_btn, 0, Qt.AlignVCenter)
        self.signin_row = QWidget(self.hero)
        sl = QHBoxLayout(self.signin_row)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        sl.addWidget(text_label("Have an account?", 13.5, 400, "muted", parent=self.signin_row))
        self.signin_btn = link_button("Sign in", 13.5, parent=self.signin_row)
        self.signin_btn.setAccessibleName("Sign in")
        self.signin_btn.clicked.connect(lambda: wizard.open_sign_in("hello"))
        sl.addWidget(self.signin_btn)
        cta.addWidget(self.signin_row, 0, Qt.AlignVCenter)
        cta.addStretch(1)
        v.addLayout(cta)
        v.addStretch(1)
        self.screen = HelloScreen(self)
        self.screen.move(500, 52)
        self.top.raise_()
        self.refresh()

    def set_animate(self, on):
        self.wave.set_animate(on)
        self.screen.set_animate(on)

    def refresh(self):
        self.keys.set_hotkey(_cfg(self.app).get("hotkey") or "alt+r")
        self.signin_row.setVisible(not _signed_in(self.app))

    def enter(self):
        self.refresh()

    def focus_target(self):
        return self.start_btn


class ModelStep(_Step):
    name = "model"
    INSET = 6

    def __init__(self, wizard):
        super().__init__(wizard)
        self._user_picked = False
        self._snapshot = None
        self._hw_unknown = False
        # The cards carry INSET for their ring. The row that holds them (their
        # parent, which clips their shadows) reaches SHADOW_ROOM further out
        # on both sides; every other row is indented by as much (``side``).
        sr = SHADOW_ROOM
        side = self.INSET + sr
        self.body.setGeometry(PAD - side, TOP, W - 2 * PAD + 2 * side, H - TOP - BOT)
        v = QVBoxLayout(self.body)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(side, 0, side, 0)
        head.addWidget(title_label("Pick your speech model", 30, 36, self.body))
        v.addLayout(head)
        v.addSpacing(12)
        comp = QHBoxLayout()
        comp.setContentsMargins(side, 0, side, 0)
        comp.setSpacing(8)
        self.your_computer = text_label("Your computer", 12.5, 600, "muted", parent=self.body)
        comp.addWidget(self.your_computer)
        comp.addSpacing(2)
        self.chips_box = QHBoxLayout()
        self.chips_box.setSpacing(8)
        comp.addLayout(self.chips_box)
        comp.addStretch(1)
        offline = QHBoxLayout()
        offline.setSpacing(6)
        lap = QLabel(self.body)
        lap.setPixmap(icons.pixmap("laptop", 15, "muted"))
        offline.addWidget(lap)
        offline.addWidget(text_label("Works offline", 12.5, 400, "muted", parent=self.body))
        offline.addWidget(text_label("·", 12.5, 400, "faint", parent=self.body))
        offline.addWidget(text_label("Change it later in Settings", 12.5, 400, "muted", parent=self.body))
        comp.addLayout(offline)
        compw = QWidget(self.body)
        compw.setLayout(comp)
        compw.setFixedHeight(26)
        v.addWidget(compw)
        v.addSpacing(36 - ModelCard.INSET[1])
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        cards = QHBoxLayout()
        # The gap under the cards is the row's own bottom margin, so their
        # shadows have room there too (_order adds no spacing after the row).
        self._below_cards = 20 - 2 * self.INSET
        cards.setContentsMargins(sr, 0, sr, self._below_cards)
        cards.setSpacing(16 - 2 * self.INSET)
        self.cards = {}
        for mid in hardware.MAIN_SPEECH_MODELS:
            card = ModelCard(mid, self.body)
            card.clicked.connect(lambda _=False, m=mid: self.select(m, user=True))
            self.group.addButton(card)
            cards.addWidget(card, 1)
            self.cards[mid] = card
        self.cards_row = QWidget(self.body)
        self.cards_row.setLayout(cards)
        self.cloud = CloudCard(self.body, side=sr)
        self.cloud.clicked.connect(lambda _=False: self.select(CLOUD, user=True))
        self.group.addButton(self.cloud)
        more = QHBoxLayout()
        more.setContentsMargins(side, 0, sr, 0)
        more.setSpacing(8 - SizeOption.INSET[0])
        more.addWidget(text_label("More sizes", 13, 600, "muted", parent=self.body))
        more.addSpacing(4)
        self.sizes = {}
        for mid in hardware.MORE_SPEECH_MODELS:
            opt = SizeOption(mid, self.body)
            opt.clicked.connect(lambda _=False, m=mid: self.select(m, user=True))
            self.group.addButton(opt)
            more.addWidget(opt)
            self.sizes[mid] = opt
        more.addStretch(1)
        self.more_row = QWidget(self.body)
        self.more_row.setLayout(more)
        self.stack_box = QVBoxLayout()
        self.stack_box.setSpacing(0)
        v.addLayout(self.stack_box)
        self._order(cloud_first=False)
        v.addStretch(1)
        self.cta = _primary("Next", parent=self.bottom)
        self.cta.clicked.connect(wizard.model_next)
        self.bottom_right.addWidget(self.cta, 0, Qt.AlignVCenter)
        self.problem = text_label("", 12.5, 400, "muted", parent=self.bottom)
        self.problem.hide()
        self.bottom_right.insertWidget(0, self.problem, 0, Qt.AlignVCenter)
        self.set_hardware(None)
        self.select(self._initial(), user=False)

    def _order(self, cloud_first):
        while self.stack_box.count():
            self.stack_box.takeAt(0)
        # 20 px between the visible edges; each widget carries its ring insets
        # (and the cards' row its bottom margin).
        to_more = 20 - self.INSET - SizeOption.INSET[1]
        if cloud_first:
            seq = (self.cloud, 20 - self.INSET - ModelCard.INSET[1], self.cards_row,
                   max(0, to_more - self._below_cards), self.more_row)
        else:
            seq = (self.cards_row, max(0, 20 - 2 * self.INSET - self._below_cards), self.cloud, to_more,
                   self.more_row)
        for item in seq:
            if isinstance(item, int):
                self.stack_box.addSpacing(item)
            else:
                self.stack_box.addWidget(item)

    def _initial(self):
        cfg = _cfg(self.app)
        if cfg.get("backend") == "managed" and _is_pro(self.app):
            return CLOUD
        dl = getattr(self.app, "downloads", None)
        rec = getattr(dl, "recommendation", None) if dl is not None else None
        if rec is not None and getattr(rec, "model", None) in hardware.MODEL_INFO:
            return rec.model
        mid = cfg.get("whisper_model") or hardware.DEFAULT_SPEECH_MODEL
        return mid if mid in hardware.MODEL_INFO else hardware.DEFAULT_SPEECH_MODEL

    # ── hardware ──
    def set_hardware(self, snap):
        self._snapshot = snap
        while self.chips_box.count():
            it = self.chips_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        if snap is None:
            if not self._hw_unknown:
                self.chips_box.addWidget(Chip("Checking…", "neutral", "chip", self.body))
        else:
            self._hw_unknown = False
            for text, icon in ((snap.cpu_chip, "chip"), (snap.ram_chip, "memory"), (snap.gpu_chip, "screen")):
                if text:
                    self.chips_box.addWidget(Chip(text, "neutral", icon, self.body))
        self.your_computer.setVisible(self.chips_box.count() > 0)
        rec = getattr(snap, "recommended", None)
        best = getattr(rec, "model", None)
        on_gpu = bool(getattr(snap, "on_gpu", False))
        for mid, card in self.cards.items():
            card.set_best(mid == best)
            card.set_note("A little slower here" if snap is not None and hardware.slower_here(mid, on_gpu) else None)
        for mid, opt in self.sizes.items():
            opt.set_best(mid == best)
        self._order(cloud_first=bool(getattr(rec, "suggest_cloud", False)))
        if best in hardware.MODEL_INFO and not self._user_picked and self.selected() != CLOUD:
            self.select(best, user=False)

    def snapshot(self):
        return self._snapshot

    def hardware_unknown(self):
        """The probe never answered (or failed): drop "Checking…" and the
        "Your computer" label; the cards work as they are."""
        if self._snapshot is not None:
            return
        self._hw_unknown = True
        self.set_hardware(None)

    # ── picking ──
    def buttons(self):
        out = dict(self.cards)
        out.update(self.sizes)
        out[CLOUD] = self.cloud
        return out

    def selected(self):
        for mid, b in self.buttons().items():
            if b.isChecked():
                return mid
        return None

    def select(self, mid, user=False):
        b = self.buttons().get(mid)
        if b is None:
            return
        if user:
            self._user_picked = True
        b.setChecked(True)
        self.problem.hide()
        self.refresh_cta()

    def refresh_cta(self):
        sel = self.selected()
        if sel == CLOUD:
            if _is_pro(self.app):
                _set_button(self.cta, "Use Gusan Cloud", kind="primary")
            else:
                _set_button(self.cta, "Try Pro free", kind="pro")
            return
        info = hardware.MODEL_INFO.get(sel)
        dl = getattr(self.app, "downloads", None)
        state = None
        if dl is not None and info is not None:
            try:
                state = dl.state(sel)
            except Exception:
                state = None
        if info is None or dl is None or state in ("ready", "downloading"):
            _set_button(self.cta, "Next", kind="primary")
        else:
            _set_button(self.cta, "Download %s · %s" % (info.plain, info.size), icon="download", kind="primary")

    def show_problem(self, text):
        self.problem.setText(text or "")
        self.problem.setVisible(bool(text))

    def enter(self):
        self.refresh_cta()

    def focus_target(self):
        return self.buttons().get(self.selected()) or self.cta


class MicStep(_Step):
    name = "mic"

    def __init__(self, wizard):
        super().__init__(wizard)
        self.chip = DownloadChip(self.top)
        self.chip.retry.connect(wizard.retry_download)
        self.top_right.addWidget(self.chip, 0, Qt.AlignVCenter)
        h = QHBoxLayout(self.body)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        left = QWidget(self.body)
        left.setFixedWidth(440)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.visual = MicVisual(left)
        ll.addWidget(self.visual, 0, Qt.AlignCenter)
        h.addWidget(left)
        right = QVBoxLayout()
        right.setContentsMargins(24, 0, 0, 0)
        right.setSpacing(0)
        right.addStretch(1)
        status = QHBoxLayout()
        status.setSpacing(16 - 5)
        self.badge = StatusBadge(self.body)
        status.addWidget(self.badge, 0, Qt.AlignVCenter)
        self.status = title_label("Say hi", 38, 44, self.body)
        status.addWidget(self.status, 0, Qt.AlignVCenter)
        status.addStretch(1)
        right.addLayout(status)
        right.addSpacing(36)
        self.picker = QComboBox(self.body)
        self.picker.setFixedWidth(340)
        self.picker.setFixedHeight(36)
        self.picker.setFont(theme.font(13.5))
        self.picker.setAccessibleName("Microphone")
        self.picker.addItem("Default microphone", None)
        self.picker.currentIndexChanged.connect(self._picked)
        right.addWidget(self.picker, 0, Qt.AlignLeft)
        right.addSpacing(14)
        note = QHBoxLayout()
        note.setSpacing(6)
        sh = QLabel(self.body)
        sh.setPixmap(icons.pixmap("shield", 15, "muted"))
        note.addWidget(sh)
        note.addWidget(text_label("Nothing is saved.", 12.5, 400, "muted", parent=self.body))
        note.addStretch(1)
        right.addLayout(note)
        right.addStretch(1)
        h.addLayout(right, 1)
        self.next_btn = _primary("Next", parent=self.bottom)
        self.next_btn.clicked.connect(lambda: wizard.go("try"))
        self.bottom_right.addWidget(self.next_btn, 0, Qt.AlignVCenter)
        self._state = None
        self._loading = False
        self._listed = False
        self.set_state("listening")

    def state(self):
        return self._state

    def set_state(self, state, reason=""):
        """listening ("Say hi") | heard ("We hear you") | error ("Can’t open
        this mic", or "No mic found" when reason is mic_monitor.MSG_NO_MIC).
        The button says "Sounds good" only once the mic was heard."""
        if state == "heard":
            kind, text = "ok", "We hear you"
        elif state == "error":
            kind = "error"
            text = "No mic found" if reason == mic_monitor.MSG_NO_MIC else "Can’t open this mic"
        else:
            state, kind, text = "listening", None, "Say hi"
        if state == self._state and text == self.status.text():
            return
        self._state = state
        if kind:
            self.badge.set_kind(kind)
            self.badge.show()
        else:
            self.badge.hide()
        self.status.setText(text)
        self.status.setAccessibleName(text)
        self.visual.setAccessibleDescription(text)
        _set_button(self.next_btn, "Sounds good" if state == "heard" else "Next")

    @staticmethod
    def _device_from(value):
        """input_device_index from the config: an int, or None for the default
        mic (also for the meeting-mode sentinels main.py treats as default)."""
        if value in (None, "", "default", "default_mic", "smart_meeting", "system_only"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def set_devices(self, devices, default=None):
        """[(index, name)]: the default microphone first, under its own name."""
        want = self._device_from(_cfg(self.app).get("input_device_index"))
        self._listed = True
        self._loading = True
        self.picker.blockSignals(True)
        self.picker.clear()
        names = dict(devices or [])
        if default is not None and default in names:
            self.picker.addItem(names[default], None)
        else:
            self.picker.addItem("Default microphone", None)
        for idx, name in devices or []:
            if idx == default:
                continue
            self.picker.addItem(name, idx)
        sel = self.picker.findData(want) if want is not None else 0
        self.picker.setCurrentIndex(sel if sel >= 0 else 0)
        self.picker.blockSignals(False)
        self._loading = False

    def device_index(self):
        """The mic to listen to: the picker's, or the configured one while the
        list is still coming (so the check never opens another mic than the
        one the picker then shows)."""
        if not self._listed:
            return self._device_from(_cfg(self.app).get("input_device_index"))
        return self.picker.currentData()

    def _picked(self, _i):
        if self._loading:
            return
        self.wizard.mic_picked(self.picker.currentData())

    def enter(self):
        self.wizard.sync_chips()
        self.wizard.start_mic()

    def leave(self):
        self.wizard.stop_mic()

    def focus_target(self):
        return self.next_btn


class TryStep(_Step):
    name = "try"

    def __init__(self, wizard):
        super().__init__(wizard)
        app = self.app
        self.chip = DownloadChip(self.top)
        self.chip.retry.connect(wizard.retry_download)
        self.top_right.addWidget(self.chip, 0, Qt.AlignVCenter)
        # The lit keys keep 8 px and the practice box 4 px around themselves
        # for their glow: the content bleeds that far so the visible edges sit
        # where the mockup has them (40 and 920).
        g, kg = PracticeBox.G, LitKeys.MARGIN
        self.body.setGeometry(PAD - kg, TOP, W - 2 * PAD + g + kg + SHADOW_ROOM, H - TOP - BOT)
        h = QHBoxLayout(self.body)
        h.setContentsMargins(0, 0, SHADOW_ROOM, 0)
        h.setSpacing(40 - g)
        left = QWidget(self.body)
        left.setFixedWidth(360 + kg)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        lv.addStretch(1)

        def indent(widget):
            row = QHBoxLayout()
            row.setContentsMargins(kg, 0, 0, 0)
            row.addWidget(widget, 0, Qt.AlignLeft)
            row.addStretch(1)
            lv.addLayout(row)

        indent(title_label("Try it", 44, 50, left))
        lv.addSpacing(30 - kg)
        hk = _cfg(app).get("hotkey") or "alt+r"
        self.keys = LitKeys(hk, left)
        lv.addWidget(self.keys, 0, Qt.AlignLeft)
        lv.addSpacing(24 - kg)
        line = text_label("Press to talk. Press again to paste.", 15, 400, "text2", parent=left)
        line.setFixedHeight(22)
        indent(line)
        lv.addSpacing(14 - 5)
        self.shortcut = _ShortcutLink(hk, parent=left, accessible_name="Dictation shortcut",
                                      change_text="Change shortcut", allow_mouse=True)
        self.shortcut.button.setFont(theme.font(13.5, 600))
        self.shortcut.button.setStyleSheet("QPushButton { padding: 0px; }")
        self.shortcut.prompt_label.setFont(theme.font(13.5, 600))
        self.shortcut.captured.connect(wizard.hotkey_captured)
        self.shortcut.refused.connect(self.show_shortcut_problem)
        self.shortcut.capturing_changed.connect(lambda on: _call(app, "suspend_hotkeys", bool(on)))
        indent(self.shortcut)
        self.shortcut_problem = text_label("", 12.5, 400, "red-tx", wrap=True, parent=left)
        self.shortcut_problem.setContentsMargins(kg, 4, 0, 0)
        self.shortcut_problem.hide()
        lv.addWidget(self.shortcut_problem)
        lv.addStretch(1)
        h.addWidget(left)
        right = QVBoxLayout()
        right.setSpacing(0)
        right.addStretch(1)
        # The pill's own drop shadow (dy 6, blur 14) needs ~12 px under it:
        # the row keeps that room and the gap to the box shrinks by as much.
        pill_room = 12
        pill_row = QWidget(self.body)
        pill_row.setFixedHeight(40 + pill_room)
        pl = QHBoxLayout(pill_row)
        pl.setContentsMargins(0, 0, 0, pill_room)
        self.pill = DemoPill("idle", pill_row)
        pl.addWidget(self.pill, 0, Qt.AlignHCenter | Qt.AlignTop)
        right.addWidget(pill_row)
        right.addSpacing(16 - PracticeBox.G - pill_room)
        self.box = PracticeBox(self.body)
        self.edit = self.box.edit
        self.edit.pasted.connect(wizard.dictation_pasted)
        right.addWidget(self.box)
        right.addSpacing(14 - PracticeBox.G)
        self.works = QWidget(self.body)
        wl = QHBoxLayout(self.works)
        wl.setContentsMargins(g, 0, 0, 0)
        wl.setSpacing(7)
        ic = QLabel(self.works)
        ic.setPixmap(icons.pixmap("check_circle", 16, "ok", sw=2))
        wl.addWidget(ic)
        wl.addWidget(text_label("Works in any app", 13, 600, "ok-tx", parent=self.works))
        wl.addStretch(1)
        self.works.setFixedHeight(18)
        sp = self.works.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.works.setSizePolicy(sp)
        self.works.hide()
        right.addWidget(self.works)
        right.addStretch(1)
        h.addLayout(right, 1)
        self.next_btn = _primary("Next", parent=self.bottom)
        self.next_btn.clicked.connect(wizard.try_next)
        self.bottom_right.addWidget(self.next_btn, 0, Qt.AlignVCenter)
        self._poll = QTimer(self)
        self._poll.setInterval(REC_POLL_MS)
        self._poll.timeout.connect(self.poll)
        self._pasted = False
        self.refresh()

    def refresh(self):
        hk = _cfg(self.app).get("hotkey") or "alt+r"
        self.keys.set_hotkey(hk)
        if not self.shortcut.is_capturing():
            self.shortcut.set_hotkey(hk)
        done = _signed_in(self.app)
        txt = "Done" if done else "Next"
        self.next_btn.setText(txt)
        self.next_btn.setAccessibleName(txt)

    def show_shortcut_problem(self, text):
        self.shortcut_problem.setText(text or "")
        self.shortcut_problem.setVisible(bool(text))

    def set_pasted(self):
        self._pasted = True
        self.works.show()
        self.pill.set_state("pasted")

    def poll(self):
        rec = bool(getattr(self.app, "is_rec", False))
        busy = bool(getattr(self.app, "_busy", False))
        if rec:
            self.pill.set_state("recording")
        elif busy:
            self.pill.set_state("transcribing")
        elif self._pasted:
            self.pill.set_state("pasted")
        else:
            self.pill.set_state("idle")

    def enter(self):
        self.refresh()
        self.wizard.sync_chips()
        self.poll()
        self._poll.start()
        QTimer.singleShot(0, lambda: self.edit.setFocus(Qt.OtherFocusReason) if self.isVisible() else None)

    def leave(self):
        self._poll.stop()
        if self.shortcut.is_capturing():
            self.shortcut.cancel_capture()

    def focus_target(self):
        return self.edit


class SignupStep(_Step):
    name = "signup"

    def __init__(self, wizard):
        super().__init__(wizard, skip=False)
        app = self.app
        # The quote card's column keeps SHADOW_ROOM on both sides, the
        # buttons' column on its right, for their shadows (the visible edges
        # stay at 40 / 480 and 536 / 920).
        sr = SHADOW_ROOM
        self.body.setGeometry(PAD - sr, TOP, W - 2 * PAD + 2 * sr, H - TOP - BOT)
        h = QHBoxLayout(self.body)
        h.setContentsMargins(0, 0, sr, 0)
        h.setSpacing(56 - sr)
        left = QWidget(self.body)
        left.setFixedWidth(440 + 2 * sr)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(sr, 0, sr, 0)
        lv.setSpacing(0)
        lv.addStretch(1)
        lv.addWidget(title_label("Keep your words.", 46, 52, left))
        self.first = WhiteCard(left, pad=(18, 14), gap=6, shadow="card")
        head = QHBoxLayout()
        head.setSpacing(6)
        mic = QLabel(self.first)
        mic.setPixmap(icons.pixmap("mic", 14, "muted"))
        head.addWidget(mic)
        self.first_meta = text_label("", 12, 400, "muted", parent=self.first)
        head.addWidget(self.first_meta)
        head.addStretch(1)
        self.first.add(head)
        self.first_text = text_label("", 16, 400, "ink", wrap=True, parent=self.first)
        self.first_text.setMinimumHeight(24)           # the mockup's 24 px line
        self.first.add(self.first_text)
        self.first_gap = QWidget(left)
        self.first_gap.setFixedHeight(22)
        lv.addWidget(self.first_gap)
        lv.addWidget(self.first)
        lv.addSpacing(22)
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.benefit = Chip("5 free Smart Actions", "blue", "wand", left)
        chips.addWidget(self.benefit)
        chips.addStretch(1)
        lv.addLayout(chips)
        lv.addStretch(1)
        h.addWidget(left)
        right = QVBoxLayout()
        right.setSpacing(0)
        right.addStretch(1)
        self.google = GoogleButton(parent=self.body)
        self.google.clicked.connect(wizard.google)
        right.addWidget(self.google)
        right.addSpacing(12)
        self.email_btn = Button("Continue with email", "secondary", "l", icon="mail", parent=self.body, full=True)
        self.email_btn.setAutoDefault(False)
        self.email_btn.clicked.connect(lambda: wizard.go("email"))
        right.addWidget(self.email_btn)
        right.addSpacing(14)
        self.skip_link = link_button("Skip for now", 13, 600, "text2", underline=True, parent=self.body)
        self.skip_link.clicked.connect(self._skip_or_cancel)
        right.addWidget(self.skip_link, 0, Qt.AlignHCenter)
        self.error = text_label("", 12.5, 400, "red-tx", wrap=True, parent=self.body)
        self.error.setAlignment(Qt.AlignHCenter)
        self.error.hide()
        right.addWidget(self.error)
        right.addSpacing(22)
        right.addWidget(Divider(self.body))
        right.addSpacing(18)
        self.stats = QCheckBox("Share anonymous usage stats", self.body)
        self.stats.setFont(theme.font(13.5))
        self.stats.setChecked(bool(_cfg(app).get("analytics_enabled", True)))
        self.stats.setAccessibleName("Share anonymous usage stats")
        theme.focus_ring(self.stats)
        right.addWidget(self.stats)
        right.addSpacing(10)
        right.addWidget(terms_label(parent=self.body))
        right.addStretch(1)
        h.addLayout(right, 1)
        signin = QWidget(self.bottom)
        sl = QHBoxLayout(signin)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        sl.addWidget(text_label("Already have an account?", 13.5, 400, "muted", parent=signin))
        self.signin_btn = link_button("Sign in", 13.5, parent=signin)
        self.signin_btn.clicked.connect(lambda: wizard.open_sign_in("signup"))
        sl.addWidget(self.signin_btn)
        self.bottom_right.addWidget(signin, 0, Qt.AlignVCenter)
        self._waiting = False
        self.set_first(None)

    def set_first(self, text, when=None):
        """The first dictation from Try it, quoted back (hidden without one)."""
        text = (text or "").strip()
        self.first.setVisible(bool(text))
        self.first_gap.setVisible(bool(text))
        if text:
            if len(text) > 160:
                text = text[:157].rstrip() + "…"
            when = when or _now()
            self.first_meta.setText("Your first dictation · Today %s" % when.strftime("%H:%M"))
            self.first_text.setText("“%s”" % text)
            self.first.setAccessibleName("Your first dictation: %s" % text)

    def set_google_waiting(self, on):
        self._waiting = bool(on)
        self.google.setEnabled(not on)
        self.google.setText("Finish in your browser…" if on else "Continue with Google")
        self.google.setAccessibleName(self.google.text())
        self.email_btn.setEnabled(not on)
        self.skip_link.setText("Cancel" if on else "Skip for now")
        self.skip_link.setAccessibleName("Cancel Google sign-in" if on else "Skip for now")
        if on:
            self.show_error("")

    def show_error(self, text):
        self.error.setText(text or "")
        self.error.setVisible(bool(text))

    def _skip_or_cancel(self):
        if self._waiting:
            self.wizard.flow.cancel_google()
        else:
            self.wizard.finish()

    def enter(self):
        if not self.wizard.stats_touched:
            self.stats.blockSignals(True)
            self.stats.setChecked(bool(_cfg(self.app).get("analytics_enabled", True)))
            self.stats.blockSignals(False)

    def leave(self):
        if self._waiting:
            self.wizard.flow.cancel_google()

    def focus_target(self):
        return self.google if self.google.isEnabled() else self.skip_link


class EmailStep(_Step):
    name = "email"

    def __init__(self, wizard):
        super().__init__(wizard, skip=False)
        self.body.setGeometry(PAD, TOP, W - 2 * PAD + SHADOW_ROOM, H - TOP - BOT)   # the card's shadow
        h = QHBoxLayout(self.body)
        h.setContentsMargins(0, 0, SHADOW_ROOM, 0)
        h.setSpacing(40)
        left = QWidget(self.body)
        left.setFixedWidth(400)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        lv.addStretch(1)
        lv.addWidget(title_label("Sign up", 42, 48, left))
        lv.addWidget(title_label("with email", 42, 48, left))
        lv.addSpacing(20)
        lock = QHBoxLayout()
        lock.setSpacing(7)
        lk = QLabel(left)
        lk.setPixmap(icons.pixmap("lock", 15, "muted"))
        lock.addWidget(lk)
        lock.addWidget(text_label("Your history stays on this computer.", 13, 400, "muted", parent=left))
        lock.addStretch(1)
        lv.addLayout(lock)
        lv.addStretch(1)
        h.addWidget(left)
        right = QVBoxLayout()
        right.setSpacing(0)
        right.addStretch(1)
        self.card = Card(self.body, pad=(24, 24), gap=18)
        self.name_field = Field("Name", optional=True, parent=self.card)
        self.email_field = Field("Email", parent=self.card)
        self.password_field = PasswordField(parent=self.card)
        self.hint = PasswordHint(self.card)
        self.password_field.extra.addWidget(self.hint)
        for f in (self.name_field, self.email_field, self.password_field):
            self.card.add(f)
        pw = self.password_field.edit
        pw.textChanged.connect(lambda t: self.hint.set_ok(len(t) >= MIN_PASSWORD))
        pw.returnPressed.connect(wizard.create_account)
        self.email_field.edit.returnPressed.connect(lambda: pw.setFocus(Qt.TabFocusReason))
        self.name_field.edit.returnPressed.connect(lambda: self.email_field.edit.setFocus(Qt.TabFocusReason))
        right.addWidget(self.card)
        right.addSpacing(10)
        self.error = text_label("", 12.5, 400, "red-tx", wrap=True, parent=self.body)
        self.error.setContentsMargins(4, 0, 4, 0)
        self.error.hide()
        right.addWidget(self.error)
        right.addSpacing(4)
        terms = terms_label("By creating an account you agree to the", parent=self.body)
        terms.setContentsMargins(4, 0, 4, 0)
        right.addWidget(terms)
        right.addStretch(1)
        h.addLayout(right, 1)
        self.create_btn = _primary("Create account", parent=self.bottom)
        self.create_btn.clicked.connect(wizard.create_account)
        self.bottom_right.addWidget(self.create_btn, 0, Qt.AlignVCenter)

    def show_error(self, text):
        self.error.setText(text or "")
        self.error.setVisible(bool(text))

    def set_busy(self, on):
        for w in (self.name_field, self.email_field, self.password_field):
            w.setEnabled(not on)
        self.create_btn.setEnabled(not on)
        self.create_btn.setText("Creating…" if on else "Create account")
        self.create_btn.setAccessibleName(self.create_btn.text())
        if self.back_btn is not None:
            self.back_btn.setEnabled(not on)

    def values(self):
        """(name, email, password) as typed (name and email stripped)."""
        return (self.name_field.text().strip(), self.email_field.text().strip(), self.password_field.text())

    def enter(self):
        QTimer.singleShot(0, self._focus)

    def _focus(self):
        if not self.isVisible():
            return
        self.focus_target().setFocus(Qt.OtherFocusReason)

    def focus_target(self):
        return self.email_field.edit if self.name_field.text() else self.name_field.edit


class InboxStep(_Step):
    name = "inbox"

    def __init__(self, wizard):
        super().__init__(wizard, skip=False)
        self._email = ""
        h = QHBoxLayout(self.body)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(40)
        left = QWidget(self.body)
        left.setFixedWidth(420)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.envelope = Envelope(left)
        ll.addWidget(self.envelope, 0, Qt.AlignCenter)
        h.addWidget(left)
        right = QVBoxLayout()
        right.setSpacing(0)
        right.addStretch(1)
        right.addWidget(title_label("Check your inbox", 40, 46, self.body))
        right.addSpacing(14)
        self.sent = text_label("", 16, 400, "text2", wrap=True, parent=self.body, rich=True)
        self.sent.setMinimumHeight(24)
        right.addWidget(self.sent)
        right.addSpacing(28)
        links = QHBoxLayout()
        links.setSpacing(8)
        links.addWidget(text_label("No email?", 13.5, 400, "muted", parent=self.body))
        self.resend_btn = link_button("Resend", 13.5, parent=self.body)
        self.resend_btn.clicked.connect(wizard.resend)
        links.addWidget(self.resend_btn)
        links.addWidget(text_label("·", 13.5, 400, "faint", parent=self.body))
        self.wrong_btn = link_button("Wrong email?", 13.5, parent=self.body)
        self.wrong_btn.clicked.connect(wizard.wrong_email)
        links.addWidget(self.wrong_btn)
        links.addStretch(1)
        right.addLayout(links)
        self.message = text_label("", 12.5, 400, "muted", wrap=True, parent=self.body)
        self.message.hide()
        right.addSpacing(10)
        right.addWidget(self.message)
        right.addStretch(1)
        h.addLayout(right, 1)
        self.open_btn = _primary("Open Gmail", icon="mail", parent=self.bottom)
        self.open_btn.clicked.connect(wizard.open_mailbox)
        self.bottom_right.addWidget(self.open_btn, 0, Qt.AlignVCenter)

    def set_email(self, email):
        self._email = email or ""
        ink_c = theme.qss_color("ink")
        safe = self._email.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.sent.setText('We sent a link to <b style="color: %s; font-weight: 700;">%s</b>' % (ink_c, safe))
        self.sent.setAccessibleName("We sent a link to %s" % self._email)
        self.open_btn.setText(mailbox(self._email)[0])
        self.open_btn.setAccessibleName(self.open_btn.text())
        self.message.hide()

    def email(self):
        return self._email

    def resend_sent(self):
        self.resend_btn.setText("Sent")
        self.resend_btn.setEnabled(False)
        QTimer.singleShot(20000, self._resend_again)

    def _resend_again(self):
        try:
            self.resend_btn.setText("Resend")
            self.resend_btn.setEnabled(True)
        except RuntimeError:
            pass

    def focus_target(self):
        return self.open_btn


# ── the wizard ───────────────────────────────────────────────────────────────
class OnboardingWizard(QDialog):
    """The five-step first-run wizard. See the module docstring."""

    page_changed = Signal(str)

    def __init__(self, main_app, parent=None, run=None, animate=True):
        super().__init__(parent)
        self.app = main_app
        self._run = run
        self._animate = bool(animate)
        self._finished = False
        self._page = None
        self._monitor = None
        self._own_monitor = False
        self._mic_on = False
        self._mic_t = 0.0
        self._mic_level = 0.0
        self._mic_loud = 0
        self._mic_started = 0.0
        self._mic_device = None
        self._devices_loaded = False
        self._hw_requested = False
        self._links = []                 # (signal, slot) this wizard connected on shared objects
        self._shown_once = False
        self._last_retry = 0.0
        self._pending_password = ""
        self._first = None
        self.model_choice = None
        self.stats_touched = False
        self.sheet = None

        theme.apply(self)
        self.setWindowTitle("Welcome to %s" % APP_NAME)
        self.setMinimumSize(480, 400)
        self.resize(W, H)

        self.flow = AuthFlow(main_app, self, run=run)
        self.flow.google_waiting.connect(self._google_waiting)
        self.flow.done.connect(self._auth_done)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(self)
        self.scroll.setProperty("gusan", "scroll")
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(self.scroll)
        self.stack = QStackedWidget()
        self.stack.setFixedSize(W, H)
        self.stack.setProperty("gusan", "scrollContent")
        self.scroll.setWidget(self.stack)

        self.hello = HelloStep(self)
        self.model = ModelStep(self)
        self.mic = MicStep(self)
        self.try_ = TryStep(self)
        self.signup = SignupStep(self)
        self.email_step = EmailStep(self)
        self.inbox = InboxStep(self)
        self.pages = {"hello": self.hello, "model": self.model, "mic": self.mic, "try": self.try_,
                      "signup": self.signup, "email": self.email_step, "inbox": self.inbox}
        for name in PAGES:
            self.stack.addWidget(self.pages[name])
        self.signup.stats.toggled.connect(self._stats_toggled)
        self.hello.set_animate(self._animate)

        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
            b.setDefault(False)

        self._mic_timer = QTimer(self)
        self._mic_timer.setInterval(FRAME_MS)
        self._mic_timer.timeout.connect(self._mic_tick)
        self._hw_timer = QTimer(self)
        self._hw_timer.setSingleShot(True)
        self._hw_timer.setInterval(HW_GIVE_UP_MS)
        self._hw_timer.timeout.connect(self.hardware_unknown)

        self._connect_services()
        self.go("hello")

    # ── services ──
    def _link(self, owner, sig_name, slot):
        sig = getattr(owner, sig_name, None)
        if sig is None or not hasattr(sig, "connect"):
            return
        try:
            sig.connect(slot)
            self._links.append((sig, slot))
        except Exception:
            logger.debug("onboarding: can't connect %s", sig_name, exc_info=True)

    def _connect_services(self):
        """Listen to the controller and app.downloads (once; done() lets go)."""
        if self._links:
            return
        app = self.app
        self._link(app, "sig_setting_changed", self.on_setting_changed)
        self._link(app, "sig_auth_changed", self.on_auth_changed)
        dl = getattr(app, "downloads", None)
        if dl is not None:
            for sig_name, slot in (("hardware_ready", self._on_hardware), ("progress", self._on_progress),
                                   ("finished", self._on_dl_finished), ("state_changed", self._on_dl_state)):
                self._link(dl, sig_name, slot)
        if isinstance(self._monitor, mic_monitor.MicMonitor):
            self._link(self._monitor, "devices_ready", self._on_devices)
        self.request_hardware()

    def _disconnect_services(self):
        """Stop listening to the shared objects (a closed wizard that Wave 4
        keeps around must not react to settings, sign-ins or downloads)."""
        links, self._links = self._links, []
        for sig, slot in links:
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _go_bg(self, fn, *args, then=None, error=None):
        run = self._run or _run_then
        return run(fn, *args, then=then, error=error)

    def request_hardware(self):
        """The 'Your computer' chips: app.downloads' probe, else a quick
        CPU / memory snapshot on a worker."""
        # Once - unless the wizard was closed before the answer came: done()
        # stopped listening (and the give-up timer), so it asks again when it
        # shows again, and app.downloads' cached probe fills the chips.
        if self._hw_requested and (self.model.snapshot() is not None or self._hw_timer.isActive()):
            return
        self._hw_requested = True
        self._hw_timer.start()            # a probe that never answers must not say "Checking…" forever
        dl = getattr(self.app, "downloads", None)
        if dl is not None:
            snap = getattr(dl, "hardware", None)
            if snap is None:
                try:
                    snap = dl.probe_hardware()      # non-blocking: a worker -> hardware_ready
                except Exception:
                    logger.debug("probe_hardware failed", exc_info=True)
                    snap = None
            if snap is not None:
                self._on_hardware(snap)
            return
        cfg = dict(_cfg(self.app))
        self._go_bg(hardware.snapshot, cfg, False, then=self._on_hardware, error=self._hardware_failed)

    def _hardware_failed(self, exc):
        logger.debug("hardware snapshot failed: %s", exc)
        self.hardware_unknown()

    def hardware_unknown(self):
        try:
            self._hw_timer.stop()
            self.model.hardware_unknown()
        except RuntimeError:
            pass

    def _on_hardware(self, snap):
        if snap is None:
            return
        try:
            self._hw_timer.stop()
            self.model.set_hardware(snap)
        except RuntimeError:
            pass

    def recommendation(self):
        snap = self.model.snapshot()
        return getattr(snap, "recommended", None)

    def _dl(self):
        return getattr(self.app, "downloads", None)

    def _on_progress(self, name, pct, _done=0, _total=0):
        if name == self.model_choice:
            for chip in (self.mic.chip, self.try_.chip):
                chip.show_model(name, "downloading", pct)

    def _on_dl_finished(self, name, _state, _msg=""):
        if name == self.model_choice:
            self.sync_chips()

    def _on_dl_state(self, name, _state):
        if name == self.model_choice:
            self.sync_chips()
        if name == self.model.selected():
            self.model.refresh_cta()

    def sync_chips(self):
        mid = self.model_choice
        dl = self._dl()
        state, pct = "hidden", 0
        if mid in hardware.MODEL_INFO and dl is not None:
            try:
                st = dl.state(mid)
            except Exception:
                st = None
            if st == "ready":
                state = "ready"
            elif st == "downloading":
                state = "downloading"
                prog = None
                try:
                    prog = dl.progress_of(mid)
                except Exception:
                    prog = None
                pct = prog[0] if prog else 0
            elif st == "failed":
                state = "failed"
        for chip in (self.mic.chip, self.try_.chip):
            chip.show_model(mid, state, pct)

    def retry_download(self):
        dl = self._dl()
        if dl is not None and self.model_choice in hardware.MODEL_INFO:
            _call(dl, "start", self.model_choice)
            self.sync_chips()

    # ── navigation ──
    def current(self):
        return self._page

    def step_number(self):
        return STEP_OF.get(self._page, 1)

    def page(self, name):
        return self.pages[name]

    def go(self, name):
        if name not in self.pages or self._finished:
            return
        old = self.pages.get(self._page)
        if old is not None and self._page != name:
            try:
                old.leave()
            except Exception:
                logger.warning("onboarding: leaving %s failed", self._page, exc_info=True)
        # Focus on the step being left (the button just pressed) follows to
        # the new step's main control; Qt would drop it on the next widget in
        # the tab chain (the new step's Skip).
        fw = self.focusWidget()
        follow = fw is None or old is None or old.isAncestorOf(fw) or not fw.isVisible()
        # Reached by keyboard (its ring showed)? Then the next control shows one too.
        kbd = fw is not None and (bool(fw.property("focusVisible")) or bool(getattr(fw, "_kbd", False)))
        self._page = name
        page = self.pages[name]
        self.stack.setCurrentWidget(page)
        self.setAccessibleDescription("Step %d of %d" % (self.step_number(), STEPS))
        try:
            page.enter()
        except Exception:
            logger.warning("onboarding: entering %s failed", name, exc_info=True)
        if follow:
            self._focus_page(page, kbd)
        self.page_changed.emit(name)

    def _focus_page(self, page, keyboard=False):
        """Put keyboard focus on ``page``'s main control, so Tab continues from
        there. Its focus ring shows only when the user is on the keyboard."""
        if page is None or not self.isVisible():
            return
        try:
            target = page.focus_target()
        except Exception:
            target = None
        if target is not None and target.isVisible() and target.isEnabled():
            target.setFocus(Qt.TabFocusReason if keyboard else Qt.OtherFocusReason)

    def back(self):
        prev = BACK_TO.get(self._page)
        if prev:
            self.go(prev)

    def skip(self):
        """Top-right Skip on steps 1-4: straight to Sign up (or done when signed in)."""
        if _signed_in(self.app):
            self.finish()
        else:
            self.go("signup")

    # ── step 2 ──
    def model_next(self):
        app = self.app
        sel = self.model.selected()
        if sel == CLOUD:
            if not _is_pro(app):
                _call(app, "_pro_upsell", UPSELL_CLOUD)
                self.model.refresh_cta()
                return
            try:
                import engine_choice
                res = engine_choice.set_speech_source(app, "cloud")
            except Exception:
                logger.warning("Gusan Cloud choice failed", exc_info=True)
                res = {"ok": False, "problem": "unknown", "changes": {}, "upsell": ""}
            if res.get("ok"):
                self.apply(res.get("changes") or {})
                self.model_choice = CLOUD
                self.go("mic")
            elif res.get("problem") == "pro":
                _call(app, "_pro_upsell", res.get("upsell") or UPSELL_CLOUD)
            elif res.get("problem") == "privacy_mode":
                self.model.show_problem("Privacy mode is on.")
            else:
                self.model.show_problem("Couldn’t switch to Gusan Cloud.")
            return
        if sel not in hardware.MODEL_INFO:
            self.go("mic")
            return
        cfg = _cfg(app)
        if (cfg.get("backend") or "local") != "local" and not cfg.get("privacy_mode"):
            self.apply({"backend": "local", "whisper_model": sel})
        else:
            _call(app, "set_setting", "whisper_model", sel)
        dl = self._dl()
        if dl is not None:
            try:
                if dl.state(sel) not in ("ready", "downloading"):
                    dl.start(sel)
            except Exception:
                logger.warning("Starting the %s download failed", sel, exc_info=True)
        self.model_choice = sel
        self.sync_chips()
        self.go("mic")

    # ── step 3 ──
    def monitor(self):
        if self._monitor is None:
            mon = getattr(self.app, "mic_monitor", None)
            if mon is None:
                pa = getattr(getattr(self.app, "recorder", None), "audio", None)
                mon = mic_monitor.MicMonitor(pa=pa, parent=self)
                self._own_monitor = True
            self._monitor = mon
            if isinstance(mon, mic_monitor.MicMonitor):
                self._link(mon, "devices_ready", self._on_devices)
        return self._monitor

    def _load_devices(self):
        if self._devices_loaded:
            return
        self._devices_loaded = True
        mon = self.monitor()
        if isinstance(mon, mic_monitor.MicMonitor):
            mon.refresh_devices()
        elif hasattr(mon, "devices"):
            def _list():
                devs = mon.devices()
                default = None
                fn = getattr(mon, "default_index", None)
                if callable(fn):
                    try:
                        default = fn()
                    except Exception:
                        default = None
                return devs, default
            self._go_bg(_list, then=lambda r: self._on_devices(*r),
                        error=lambda exc: logger.debug("mic devices failed: %s", exc))

    def _on_devices(self, devices, default=None):
        try:
            self.mic.set_devices(devices, default)
            if self._mic_on and self.mic.device_index() != self._mic_device:
                # The configured mic isn't in the list (unplugged): listen to
                # the one the picker shows instead.
                self.stop_mic()
                self.start_mic()
        except RuntimeError:
            pass

    def start_mic(self):
        if self._mic_on:
            return
        mon = self.monitor()
        self._load_devices()
        self._mic_on = True
        self._mic_level = 0.0
        self._mic_loud = 0
        self._mic_started = time.monotonic()
        idx = self.mic.device_index()
        self._mic_device = idx
        if isinstance(mon, mic_monitor.MicMonitor):
            mon.start(idx)
        else:
            self._go_bg(mon.start, idx, error=lambda exc: logger.debug("mic start failed: %s", exc))
        if self.mic.state() == "error":
            self.mic.set_state("listening")
        self._mic_timer.start()

    def stop_mic(self):
        self._mic_timer.stop()
        if not self._mic_on:
            return
        self._mic_on = False
        mon = self._monitor
        if mon is None:
            return
        if isinstance(mon, mic_monitor.MicMonitor):
            mon.stop()
        else:
            self._go_bg(mon.stop, error=lambda exc: logger.debug("mic stop failed: %s", exc))

    def mic_picked(self, index):
        _call(self.app, "set_setting", "input_device_index", index)
        if self._mic_on:
            self.stop_mic()
            self.mic.set_state("listening")
            self.start_mic()

    def _mic_tick(self):
        mon = self._monitor
        if mon is None:
            return
        rms = getattr(mon, "rms", None)
        if rms is not None and not callable(rms):
            target = mic_monitor.display_level(rms)
        else:
            lv = getattr(mon, "level", 0.0)
            lv = lv() if callable(lv) else lv
            try:
                target = min(1.0, math.sqrt(max(0.0, float(lv or 0.0))) * 1.3)
            except (TypeError, ValueError):
                target = 0.0
        # Fast attack, slow release: the ring jumps with the voice, then settles.
        if target > self._mic_level:
            self._mic_level += (target - self._mic_level) * 0.6
        else:
            self._mic_level += (target - self._mic_level) * 0.12
        self.mic.visual.set_level(self._mic_level)
        self.mic.visual.advance(FRAME_MS / 1000.0)
        if target > 0.35:
            self._mic_loud += 1
        else:
            self._mic_loud = 0
        err = getattr(mon, "error", "") or ""
        if self.mic.state() != "heard":
            if self._mic_loud >= 3:
                self.mic.set_state("heard")
            elif err and time.monotonic() - self._mic_started > 0.4:
                self.mic.set_state("error", err)

    # ── step 4 ──
    def hotkey_captured(self, combo):
        app = self.app
        old = _cfg(app).get("hotkey") or "alt+r"
        ok = _call(app, "set_setting", "hotkey", combo)
        if ok:
            self.try_.show_shortcut_problem("")
            self.try_.keys.set_hotkey(combo)
            self.hello.keys.set_hotkey(combo)
            if not self.try_.shortcut.is_capturing():
                self.try_.shortcut.set_hotkey(combo)
        else:
            self.try_.shortcut.set_hotkey(old)
            self.try_.show_shortcut_problem(hotkey_refusal(_cfg(app), combo))

    def dictation_pasted(self, text):
        if self._first is None and (text or "").strip():
            self._first = (text.strip(), _now())
            self.signup.set_first(*self._first)
        self.try_.set_pasted()

    def first_dictation(self):
        return self._first[0] if self._first else ""

    def try_next(self):
        if _signed_in(self.app):
            self.finish()
        else:
            self.go("signup")

    # ── step 5 ──
    def _stats_toggled(self, _on):
        self.stats_touched = True

    def open_sign_in(self, where="hello"):
        if self.sheet is not None:
            try:
                self.sheet.show()
                self.sheet.raise_()
                self.sheet.activateWindow()
                return self.sheet
            except RuntimeError:
                self.sheet = None
        sheet = AuthSheet(self.app, mode="signin", parent=self, run=self._run)
        sheet.setAttribute(Qt.WA_DeleteOnClose, True)      # a new one each time: never pile them up
        sheet.setWindowModality(Qt.WindowModal)
        sheet.signed_in.connect(lambda w=where: self._sheet_signed_in(w))
        sheet.finished.connect(lambda _r: self._sheet_closed())
        self.sheet = sheet
        sheet.show()
        return sheet

    def _sheet_closed(self):
        self.sheet = None

    def _sheet_signed_in(self, where):
        self.hello.refresh()
        self.try_.refresh()
        if where == "hello" and self._page == "hello":
            self.go("model")
        elif self._page in ("signup", "email", "inbox"):
            self.finish()

    def google(self):
        self.signup.show_error("")
        self.flow.google()

    def _google_waiting(self, on):
        self.signup.set_google_waiting(on)

    def create_account(self):
        st = self.email_step
        name, email, pw = st.values()
        st.email_field.set_error(False)
        st.password_field.set_error(False)
        if not valid_email(email):
            st.email_field.set_error(True)
            st.show_error(MSG_BAD_EMAIL)
            st.email_field.edit.setFocus(Qt.OtherFocusReason)
            return
        if len(pw) < MIN_PASSWORD:
            st.password_field.set_error(True)
            st.show_error(MSG_SHORT_PASSWORD)
            st.password_field.edit.setFocus(Qt.OtherFocusReason)
            return
        st.show_error("")
        # Busy first: the answer may come back before sign_up() returns.
        st.set_busy(True)
        if not self.flow.sign_up(email, pw, name or None):
            st.set_busy(False)

    def _auth_done(self, action, status, msg):
        if self._finished:
            return
        if action == "google":
            if status == "ok":
                self.finish()
            elif status == "error":
                self.signup.show_error(msg)
            return
        if action == "signup":
            st = self.email_step
            st.set_busy(False)
            if status == "ok":
                self.finish()
            elif status == "verify":
                _name, email, pw = st.values()
                self._pending_password = pw
                self.inbox.set_email(msg or email)
                self.go("inbox")
            else:
                st.show_error(msg or "Something went wrong. Try again.")
            return
        if action == "signin":
            # Only the quiet retry on 5c signs in here: success finishes,
            # a failure (email not confirmed yet) stays silent.
            if status == "ok":
                self.finish()
            return
        if action == "resend" and status == "error":
            self.inbox.message.setText("Couldn’t send it. Check your connection.")
            self.inbox.message.show()
            self.inbox._resend_again()                  # let them try again now, not in 20 s

    def resend(self):
        email = self.inbox.email()
        if email:
            self.inbox.message.hide()
            self.inbox.resend_sent()                # first: the answer may come back at once
            self.flow.resend(email)

    def wrong_email(self):
        self._pending_password = ""
        self.go("email")
        edit = self.email_step.email_field.edit
        edit.setFocus(Qt.OtherFocusReason)
        edit.selectAll()

    def open_mailbox(self):
        try:
            QDesktopServices.openUrl(QUrl(mailbox(self.inbox.email())[1]))
        except Exception:
            logger.debug("open mailbox failed", exc_info=True)

    def retry_sign_in(self):
        """After the email was confirmed (the window is active again): sign in
        with what was typed. Quiet: no analytics, no error line."""
        email = self.inbox.email()
        if not (email and self._pending_password) or self.flow.email_busy():
            return False
        self._last_retry = time.monotonic()
        return self.flow.sign_in(email, self._pending_password, quiet=True)

    # ── finish ──
    def apply(self, updates):
        """Settings through the controller (instant apply), never cfg directly."""
        app = self.app
        if not updates:
            return {}
        fn = getattr(app, "set_settings", None)
        if callable(fn):
            try:
                return fn(dict(updates)) or {}
            except Exception:
                logger.warning("set_settings failed", exc_info=True)
                return {}
        out = {}
        for k, v in updates.items():
            out[k] = bool(_call(app, "set_setting", k, v))
        return out

    def _gpu_wanted(self):
        rec = self.recommendation()
        return bool(rec is not None and getattr(rec, "needs_gpu_download", False)
                    and self.model_choice == getattr(rec, "model", None))

    def finish(self):
        """Write the onboarding flags and close (accepted)."""
        if self._finished:
            return
        app = self.app
        self._finished = True
        self._stop_everything()
        self.apply({"onboarding_done": True, "account_gate_seen": True,
                    "analytics_enabled": bool(self.signup.stats.isChecked())})
        if self._gpu_wanted():
            self.apply({GPU_INTRO_KEY: True})
            _call(app, "start_gpu_download", "onboarding")
        # After saving, so a user who just opted out of analytics sends nothing.
        cfg = _cfg(app)
        _call(app, "track", "onboarding_completed", {
            "account_only": False, "signed_in": _signed_in(app),
            "backend": cfg.get("backend", "local"), "language": cfg.get("language", "auto"),
            "model": self.model_choice or "",
        })
        self._pending_password = ""
        self.accept()

    def is_finished(self):
        return self._finished

    # ── controller signals ──
    def on_setting_changed(self, key):
        try:
            if key == "hotkey":
                self.hello.refresh()
                self.try_.refresh()
            elif key in ("whisper_model", "backend"):
                self.model.refresh_cta()
            elif key == "analytics_enabled" and not self.stats_touched:
                self.signup.stats.blockSignals(True)
                self.signup.stats.setChecked(bool(_cfg(self.app).get("analytics_enabled", True)))
                self.signup.stats.blockSignals(False)
        except RuntimeError:
            pass

    def on_auth_changed(self):
        try:
            self.hello.refresh()
            self.try_.refresh()
            self.model.refresh_cta()
        except RuntimeError:
            return
        if (not self._finished and _signed_in(self.app) and self.isVisible()
                and self._page in ("signup", "email", "inbox")):
            self.finish()

    # ── window ──
    def _stop_everything(self):
        """Close what the wizard opened: the mic stream (and its own monitor's
        PyAudio), the shortcut capture (hotkeys back on), a Google sign-in
        waiting for the browser, the sign-in sheet."""
        try:
            self.stop_mic()
            self.try_.leave()
            if self.flow.google_running():
                self.flow.cancel_google()
            if self._own_monitor and self._monitor is not None:
                self._monitor.close()
            if self.sheet is not None:
                sheet, self.sheet = self.sheet, None
                sheet.close()
        except RuntimeError:
            pass

    def event(self, ev):
        if (ev.type() == QEvent.WindowActivate and self._page == "inbox"
                and time.monotonic() - self._last_retry > 8.0):
            QTimer.singleShot(0, self.retry_sign_in)
        return super().event(ev)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            ev.ignore()                     # Esc must not close the setup
            return
        super().keyPressEvent(ev)

    def showEvent(self, ev):
        super().showEvent(ev)
        if not ev.spontaneous():
            self._connect_services()          # again after a done() (no-op otherwise)
        page = self.pages.get(self._page)
        if page is not None and self._page in ("mic", "try"):
            page.enter()
        if not self._shown_once:
            self._shown_once = True
            # Start on the step's main control ("Get started"), not on Skip.
            QTimer.singleShot(0, self._first_focus)
        try:
            from ui.winfit import settle_on_screen
            settle_on_screen(self, recenter=True)
        except Exception:
            logger.debug("fit on screen failed", exc_info=True)

    def _first_focus(self):
        try:
            self._focus_page(self.pages.get(self._page))
        except RuntimeError:
            pass

    def hideEvent(self, ev):
        super().hideEvent(ev)
        if not self._finished:
            self.stop_mic()
            try:
                self.try_.leave()
            except RuntimeError:
                pass

    def done(self, result):
        # Finished or closed with X: nothing keeps running or listening.
        self._stop_everything()
        self._hw_timer.stop()
        self._disconnect_services()
        super().done(result)


def create(main_app):
    """The wizard for ``main_app`` (not shown yet)."""
    return OnboardingWizard(main_app)
